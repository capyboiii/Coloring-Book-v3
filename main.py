#!/usr/bin/env python3
"""
Coloring Book Generator - tự động hoá Gemini web -> PDF chuẩn Lulu.

Cách dùng:
    python main.py generate      # gọi Gemini sinh ảnh (có thể chạy nhiều lần, tự resume)
    python main.py process       # làm sạch ảnh -> 300 DPI đen trắng
    python main.py build         # dựng interior.pdf + cover.pdf
    python main.py all           # chạy cả 3 bước
    python main.py check         # kiểm tra PDF đã dựng
    python main.py demo          # dựng thử bằng ảnh placeholder (không cần Gemini)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

import yaml

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from bookgen import imaging, pdf_builder  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bookgen")


# --------------------------------------------------------------- tiện ích

def load_cfg(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


BOOKS_DIR = ROOT / "output" / "books"
POINTER = ROOT / "output" / "current_book.txt"


def slugify(text: str) -> str:
    """'Magical Forest Animals!' -> 'magical-forest-animals'"""
    import re as _re
    import unicodedata

    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = _re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "untitled"


def set_current_book(slug: str) -> None:
    POINTER.parent.mkdir(parents=True, exist_ok=True)
    POINTER.write_text(slug, encoding="utf-8")


def get_current_book() -> str | None:
    if POINTER.exists():
        slug = POINTER.read_text(encoding="utf-8").strip()
        if slug and (BOOKS_DIR / slug).exists():
            return slug
    return None


def list_books() -> list[str]:
    if not BOOKS_DIR.exists():
        return []
    return sorted(d.name for d in BOOKS_DIR.iterdir() if d.is_dir())


def paths_of(cfg: dict) -> dict[str, Path]:
    """Mỗi chủ đề một thư mục riêng: output/books/<chủ-đề>/...

    Chủ đề đang chọn lấy từ cfg['_book'] (cờ --book) hoặc file con trỏ
    output/current_book.txt do lệnh `ask` ghi. Không có thì dùng đường dẫn
    phẳng cũ trong config.yaml để các dự án đang dở không bị gãy.
    """
    slug = cfg.get("_book") or get_current_book()
    if not slug:
        return {k: (ROOT / v) for k, v in cfg["paths"].items()}
    base = BOOKS_DIR / slug
    return {
        "raw_dir": base / "01_raw",
        "processed_dir": base / "02_processed",
        "pdf_dir": base / "03_pdf",
        "preview_dir": base / "04_previews",
        "state_file": base / "state.json",
    }


def load_state(f: Path) -> dict:
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"done": [], "subjects": []}


def save_state(f: Path, state: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------- generate

def make_driver(cfg: dict):
    """Chọn nguồn sinh ảnh theo config: api (mặc định) hoặc web."""
    backend = (cfg.get("backend") or "api").lower()
    if backend == "web":
        from bookgen.gemini_driver import GeminiDriver
        return GeminiDriver(cfg)
    from bookgen.gemini_api import GeminiApiDriver
    return GeminiApiDriver(cfg)


def subjects_prompt(cfg: dict, subjects: list[str], need: int) -> str:
    return (
        f'I am making a children\'s coloring book titled "{cfg["book"]["title"]}" '
        f'({cfg["book"]["subtitle"]}). Give me exactly {need} NEW scene ideas, '
        f"different from these: {subjects}. "
        "Each idea must be one short English sentence describing a single simple "
        "scene suitable for a bold-outline coloring page. "
        "Respond ONLY with a JSON array of strings, nothing else."
    )


def build_jobs(cfg: dict, subjects: list[str], raw: Path, state: dict) -> list:
    """Danh sách việc còn thiếu, đã bỏ ảnh đã có.

    Trang ruột: mỗi trang một việc riêng, chạy song song thoải mái.
    Hai bìa: gộp thành một CHUỖI để vẽ nối tiếp trong cùng một cuộc trò chuyện
    -> bìa sau nhìn thấy bìa trước nên khớp tông màu và phong cách.
    """
    n = cfg["book"]["num_images"]
    tmpl = cfg["prompts"]["page"]
    jobs: list = []

    for i, subj in enumerate(subjects[:n], start=1):
        key = f"page_{i:03d}"
        dest = raw / f"{key}.png"
        if key in state["done"] and dest.exists():
            continue
        jobs.append((key, tmpl.format(i=i, subject=subj), dest))

    title = cfg["book"]["title"]
    style = cfg["prompts"].get("cover_style", "")
    covers = []
    for key, tpl in [("cover_front", cfg["prompts"]["front_cover"]),
                     ("cover_back", cfg["prompts"]["back_cover"])]:
        dest = raw / f"{key}.png"
        if key in state["done"] and dest.exists():
            continue
        covers.append((key, tpl.format(title=title, style=style), dest))

    if len(covers) == 2:
        jobs.append(covers)          # chuỗi: cùng chat, cùng tab
    else:
        jobs.extend(covers)          # chỉ còn thiếu một bìa -> chạy đơn lẻ

    return jobs


def cmd_generate_parallel(cfg: dict) -> None:
    """Mở nhiều tab Gemini và sinh ảnh song song (chỉ dùng cho backend: web)."""
    import asyncio

    from bookgen.gemini_driver import parse_subject_list
    from bookgen.gemini_pool import GeminiPool

    P = paths_of(cfg)
    raw = P["raw_dir"]
    state = load_state(P["state_file"])
    n = cfg["book"]["num_images"]

    subjects = [s for s in (cfg.get("subjects") or []) if s and s.strip()]

    async def run() -> None:
        nonlocal subjects
        async with GeminiPool(cfg) as pool:
            if len(subjects) < n:
                cached = state.get("subjects", [])
                if len(cached) >= n:
                    subjects = cached[:n]
                else:
                    need = n - len(subjects)
                    log.info("Nhờ Gemini nghĩ thêm %d chủ đề...", need)
                    raw_text = await pool.ask_text(subjects_prompt(cfg, subjects, need))
                    subjects += parse_subject_list(raw_text, need)
                    state["subjects"] = subjects
                    save_state(P["state_file"], state)

            while len(subjects) < n:
                subjects.append(f"a cute forest animal scene number {len(subjects)+1}")

            jobs = build_jobs(cfg, subjects, raw, state)
            if not jobs:
                log.info("Không còn ảnh nào cần sinh.")
                return
            log.info("Còn %d ảnh, chạy tối đa %d phiên song song.",
                     len(jobs), pool.workers)

            def on_done(key: str, ok: bool) -> None:
                if ok:
                    state["done"].append(key)
                    save_state(P["state_file"], state)
                else:
                    log.error("THẤT BẠI: %s", key)

            results = await pool.run_jobs(jobs, on_done)
            failed = [k for k, ok in results.items() if not ok]
            if pool.quota_hit:
                log.error("Hết hạn mức tạo ảnh. Vẽ được %d ảnh; đợi reset rồi "
                          "chạy lại lệnh này để làm tiếp.",
                          len(results) - len(failed))
                return
            log.info("Xong: %d/%d ảnh.", len(results) - len(failed), len(results))
            if failed:
                log.warning("Chạy lại `python main.py generate` để làm nốt: %s",
                            ", ".join(failed))

    asyncio.run(run())
    log.info("Ảnh gốc ở: %s", raw)


def cmd_generate(cfg: dict) -> None:
    from bookgen.gemini_driver import parse_subject_list

    backend = (cfg.get("backend") or "api").lower()
    
    b = cfg.get("browser", {})
    profiles = b.get("profiles", [])
    if not profiles and "user_data_dir" in b:
        profiles = [b["user_data_dir"]]
    workers = int(b.get("concurrency_per_profile", b.get("concurrency", 1))) * max(1, len(profiles))
    
    if backend == "web" and (workers > 1 or b.get("use_cdp")):
        return cmd_generate_parallel(cfg)

    P = paths_of(cfg)
    raw = P["raw_dir"]
    state = load_state(P["state_file"])
    n = cfg["book"]["num_images"]

    subjects = list(cfg.get("subjects") or [])
    subjects = [s for s in subjects if s and s.strip()]

    with make_driver(cfg) as g:
        # 1) đủ chủ đề chưa? thiếu thì nhờ Gemini nghĩ thêm
        if len(subjects) < n:
            cached = state.get("subjects", [])
            if len(cached) >= n:
                subjects = cached[:n]
            else:
                need = n - len(subjects)
                log.info("Nhờ Gemini nghĩ thêm %d chủ đề...", need)
                subjects += parse_subject_list(
                    g.ask_text(subjects_prompt(cfg, subjects, need)), need
                )
                state["subjects"] = subjects
                save_state(P["state_file"], state)

        while len(subjects) < n:  # phòng khi Gemini trả thiếu
            subjects.append(f"a cute forest animal scene number {len(subjects)+1}")

        # 2) sinh từng trang
        tmpl = cfg["prompts"]["page"]
        for i, subj in enumerate(subjects[:n], start=1):
            key = f"page_{i:03d}"
            dest = raw / f"{key}.png"
            if key in state["done"] and dest.exists():
                log.info("[%d/%d] Bỏ qua (đã có): %s", i, n, key)
                continue
            log.info("[%d/%d] %s", i, n, subj)
            if g.generate_image(tmpl.format(i=i, subject=subj), dest):
                state["done"].append(key)
                save_state(P["state_file"], state)
            else:
                log.error("[%d/%d] THẤT BẠI: %s", i, n, subj)
            g._sleep_jitter()

        # 3) hai ảnh bìa
        for key, tpl in [
            ("cover_front", cfg["prompts"]["front_cover"]),
            ("cover_back", cfg["prompts"]["back_cover"]),
        ]:
            dest = raw / f"{key}.png"
            if key in state["done"] and dest.exists():
                log.info("Bỏ qua (đã có): %s", key)
                continue
            log.info("Đang tạo %s...", key)
            if g.generate_image(tpl.format(title=cfg["book"]["title"]), dest):
                state["done"].append(key)
                save_state(P["state_file"], state)
            g._sleep_jitter()

    log.info("Xong bước generate. Ảnh gốc ở: %s", raw)


# --------------------------------------------------------------- process

def cmd_process(cfg: dict) -> None:
    P = paths_of(cfg)
    p = cfg.get("print", {})
    dpi = int(p.get("dpi", 300))
    bleed = float(p.get("bleed", 0.125))
    trim_w = float(p.get("trim_width", 8.5))
    trim_h = float(p.get("trim_height", 11.0))
    w_in = trim_w + 2 * bleed
    h_in = trim_h + 2 * bleed
    w_px, h_px = int(w_in * dpi), int(h_in * dpi)

    raw, proc = P["raw_dir"], P["processed_dir"]
    if not raw.exists():
        log.error("Chưa có ảnh gốc. Chạy `python main.py generate` trước.")
        return

    pr = cfg.get("process") or {}
    for f in sorted(raw.glob("page_*.png")):
        imaging.process_lineart(
            f, proc / f.name, w_px, h_px,
            threshold=int(pr.get("threshold", 165)),
            pure_bw=bool(pr.get("pure_bw", False)),
            sharpen=bool(pr.get("sharpen", True)),
        )

    for name in ("cover_front", "cover_back"):
        src = raw / f"{name}.png"
        if src.exists():
            imaging.prepare_cover_art(src, proc / f"{name}.png", w_px, h_px)
        else:
            log.warning("Thiếu %s.png - bìa sẽ dùng nền màu trơn.", name)

    log.info("Xong bước process -> %s", proc)


# --------------------------------------------------------------- build

def cmd_build(cfg: dict) -> None:
    P = paths_of(cfg)
    proc, out = P["processed_dir"], P["pdf_dir"]
    images = sorted(proc.glob("page_*.png"))
    if not images:
        log.error("Không có ảnh đã xử lý. Chạy `python main.py process` trước.")
        return

    interior, pages = pdf_builder.build_interior(images, out / "interior.pdf", cfg)

    fc = proc / "cover_front.png"
    bc = proc / "cover_back.png"
    cover = pdf_builder.build_cover(
        fc if fc.exists() else None,
        bc if bc.exists() else None,
        out / "cover.pdf",
        cfg,
        pages,
    )

    binding = cfg["print"].get("binding", "perfect")
    if "spine_width" in cfg["print"] and cfg["print"]["spine_width"] is not None:
        spine = float(cfg["print"]["spine_width"])
    elif binding in ["coil", "saddle"]:
        spine = 0.0
    else:
        spine = pdf_builder.spine_width(pages, cfg["print"]["paper_thickness"])
    print(f"""
================= SẴN SÀNG TẢI LÊN LULU =================
  Interior : {interior}
             {pages} trang, {cfg['print']['trim_width']+2*cfg['print']['bleed']} x """
          f"""{cfg['print']['trim_height']+2*cfg['print']['bleed']} in (đã gồm bleed)
  Cover    : {cover}
             gáy {spine:.4f} in
  Trim size chọn trên Lulu: {cfg['print']['trim_width']} x {cfg['print']['trim_height']} in
  Nhớ chọn đúng loại giấy khớp paper_thickness trong config.yaml,
  vì Lulu tính lại độ rộng gáy theo giấy bạn chọn.
=========================================================
""")


# --------------------------------------------------------------- check

def cmd_check(cfg: dict) -> None:
    from pypdf import PdfReader

    P = paths_of(cfg)
    ok = True
    for name in ("interior.pdf", "cover.pdf"):
        f = P["pdf_dir"] / name
        if not f.exists():
            log.error("Thiếu %s", name)
            ok = False
            continue
        r = PdfReader(str(f))
        box = r.pages[0].mediabox
        log.info("%-13s %d trang, %.3f x %.3f in",
                 name, len(r.pages), float(box.width) / 72, float(box.height) / 72)
        if name == "interior.pdf" and len(r.pages) % 2:
            log.error("  -> Số trang lẻ, Lulu sẽ từ chối.")
            ok = False

    for f in sorted((P["processed_dir"]).glob("page_*.png")):
        good, dpi = imaging.check_dpi(
            f, cfg["print"]["trim_width"] + 2 * cfg["print"]["bleed"],
            cfg["print"]["trim_height"] + 2 * cfg["print"]["bleed"],
        )
        if not good:
            log.warning("%s chỉ đạt %.0f DPI (<300)", f.name, dpi)
            ok = False

    print("\nKẾT QUẢ:", "ĐẠT ✓" if ok else "CÓ CẢNH BÁO ✗")


# --------------------------------------------------------------- demo

def cmd_demo(cfg: dict) -> None:
    """Dựng sách thử bằng ảnh placeholder - kiểm tra pipeline không cần Gemini."""
    from PIL import Image, ImageDraw

    P = paths_of(cfg)
    raw = P["raw_dir"]
    raw.mkdir(parents=True, exist_ok=True)
    n = min(cfg["book"]["num_images"], 8)

    for i in range(1, n + 1):
        im = Image.new("RGB", (1024, 1325), "white")
        d = ImageDraw.Draw(im)
        for r in range(60, 460, 70):
            d.ellipse([512 - r, 662 - r, 512 + r, 662 + r], outline="black", width=6)
        d.rectangle([40, 40, 984, 1285], outline="black", width=8)
        im.save(raw / f"page_{i:03d}.png")

    for name, col in [("cover_front", "#7FB77E"), ("cover_back", "#F1F0C0")]:
        Image.new("RGB", (1024, 1325), col).save(raw / f"{name}.png")

    log.info("Đã tạo %d ảnh placeholder.", n + 2)
    cmd_process(cfg)
    cmd_build(cfg)
    cmd_check(cfg)


# --------------------------------------------------------------- ask

def cmd_books(cfg: dict) -> None:
    """Liệt kê các cuốn sách đã tạo."""
    books = list_books()
    if not books:
        print("Chưa có cuốn nào. Chạy `python main.py ask` để bắt đầu.")
        return
    cur = get_current_book()
    print(f"\nCác chủ đề trong {BOOKS_DIR}:\n")
    for b in books:
        base = BOOKS_DIR / b
        raw = len(list((base / "01_raw").glob("page_*.png")))
        covers = len(list((base / "01_raw").glob("cover_*.png")))
        pdf = "có PDF" if (base / "03_pdf" / "interior.pdf").exists() else "chưa dựng"
        mark = " <- đang chọn" if b == cur else ""
        print(f"  {b:<40} {raw} trang, {covers}/2 bìa, {pdf}{mark}")
    print(f"\nChọn cuốn khác: python main.py build --book {books[0]}\n")


def cmd_ask(cfg: dict) -> None:
    """Hỏi chủ đề + số trang, rồi vẽ TOÀN BỘ sách: trang ruột + 2 bìa.

    Gemini tự nghĩ ra từng cảnh cho mỗi trang dựa trên chủ đề bạn nhập.
    """
    import asyncio

    from bookgen.gemini_driver import parse_subject_list
    from bookgen.gemini_pool import GeminiPool

    print("\n" + "=" * 62)
    print("  TẠO SÁCH TÔ MÀU")
    print("=" * 62)

    theme = ""
    while not theme:
        theme = input("Chủ đề cuốn sách (vd: magical forest animals)> ").strip()

    n = 0
    while n <= 0:
        try:
            n = int(input("Số trang ruột cần vẽ (vd: 30)> ").strip())
        except ValueError:
            print("  Nhập một số nguyên.")

    # Mỗi chủ đề một thư mục riêng -> không đụng vào sách đã làm trước đó
    slug = slugify(theme)
    cfg["_book"] = slug
    set_current_book(slug)
    P = paths_of(cfg)
    raw = P["raw_dir"]
    raw.mkdir(parents=True, exist_ok=True)

    cfg["book"]["title"] = theme
    cfg["book"]["num_images"] = n

    # Cùng chủ đề chạy lại -> làm tiếp phần còn thiếu, không vẽ lại từ đầu
    state = load_state(P["state_file"])
    have = len(list(raw.glob("page_*.png")))
    if have:
        print(f"\n  Thư mục này đã có {have} trang, sẽ chỉ vẽ phần còn thiếu.")

    print(f"\n  Thư mục: {raw}")
    print(f"  Chủ đề: {theme}")
    print(f"  Sẽ vẽ: {n} trang ruột + bìa trước + bìa sau = {n + 2} ảnh")
    print(f"  Ước tính: {(n + 2) * 45 // 60}-{(n + 2) * 90 // 60} phút\n")

    async def run() -> None:
        async with GeminiPool(cfg) as pool:
            # 1) nhờ Gemini nghĩ n cảnh từ chủ đề (lần chạy sau dùng lại cảnh cũ)
            cached = state.get("subjects", [])
            if len(cached) >= n:
                subjects = cached[:n]
                log.info("Dùng lại %d cảnh đã lưu.", n)
            else:
                log.info("Nhờ Gemini nghĩ %d cảnh cho chủ đề '%s'...", n, theme)
                subjects = parse_subject_list(
                    await pool.ask_text(subjects_prompt(cfg, [], n)), n
                )
            while len(subjects) < n:
                subjects.append(f"a simple {theme} scene number {len(subjects)+1}")
            state["subjects"] = subjects
            save_state(P["state_file"], state)
            print("\n  Các cảnh sẽ vẽ:")
            for i, s in enumerate(subjects[:n], 1):
                print(f"    {i:>3}. {s}")
            print()

            # 2) vẽ trang ruột + 2 bìa
            jobs = build_jobs(cfg, subjects, raw, state)
            log.info("Bắt đầu vẽ %d ảnh, tối đa %d phiên song song.",
                     len(jobs), pool.workers)

            def on_done(key: str, ok: bool) -> None:
                if ok:
                    state["done"].append(key)
                    save_state(P["state_file"], state)
                    print(f"  ✓ {key}.png")
                else:
                    print(f"  ✗ {key} thất bại")

            results = await pool.run_jobs(jobs, on_done)
            failed = [k for k, ok in results.items() if not ok]
            done_n = len(results) - len(failed)

            if pool.quota_hit:
                left = len(jobs) - done_n
                print(f"\n  ĐÃ HẾT HẠN MỨC TẠO ẢNH của tài khoản.")
                print(f"  Vẽ được {done_n} ảnh, còn khoảng {left} ảnh chưa làm.")
                print("  Ảnh đã xong được giữ nguyên. Đợi hạn mức reset rồi chạy")
                print(f"  `python main.py generate` để làm tiếp cuốn này.")
                return

            print(f"\n  Xong {done_n}/{len(results)} ảnh.")
            if failed:
                print(f"  Thiếu: {', '.join(failed)}")
                print("  Chạy `python main.py generate` để vẽ nốt phần thiếu.")

    asyncio.run(run())
    print(f"\n  Ảnh ở: {raw}")
    print("  Bước tiếp: python main.py process && python main.py build\n")


def get_preview_attachments_map(cfg: dict) -> dict[str, list[Path]]:
    """Tự động xác định ảnh bìa & các trang ruột thực tế cần đính kèm cho 5 ảnh preview marketing."""
    P = paths_of(cfg)
    raw_dir = P["raw_dir"]
    proc_dir = P["processed_dir"]
    title = cfg.get("book", {}).get("title", "Coloring Book")
    subtitle = cfg.get("book", {}).get("subtitle", "")
    author = cfg.get("book", {}).get("author", "")

    cover_front_titled = proc_dir / "cover_front_titled.png"
    if cover_front_titled.exists():
        cover_front = cover_front_titled
    else:
        cover_front_raw = (proc_dir / "cover_front.png") if (proc_dir / "cover_front.png").exists() else (raw_dir / "cover_front.png")
        cover_front = cover_front_raw
        if cover_front_raw.exists():
            try:
                cover_front = imaging.render_titled_cover(
                    cover_front_raw, cover_front_titled,
                    title, subtitle, author
                )
            except Exception:
                cover_front = cover_front_raw

    # Đảm bảo các trang ruột trong 01_raw được làm sạch nét và đóng khung viền đen vào 02_processed trước khi đính kèm vào preview
    raw_pages = sorted(list(raw_dir.glob("page_*.png"))) + sorted(list(raw_dir.glob("page_*.jpg")))
    proc_dir.mkdir(parents=True, exist_ok=True)
    p = cfg.get("print", {})
    dpi = int(p.get("dpi", 300))
    bleed = float(p.get("bleed", 0.125))
    trim_w = float(p.get("trim_width", 8.5))
    trim_h = float(p.get("trim_height", 11.0))
    w_px = int((trim_w + 2 * bleed) * dpi)
    h_px = int((trim_h + 2 * bleed) * dpi)
    pr = cfg.get("process") or {}

    for r_file in raw_pages[:5]:
        p_file = proc_dir / r_file.name
        try:
            imaging.process_lineart(
                r_file, p_file, w_px, h_px,
                threshold=int(pr.get("threshold", 165)),
                pure_bw=bool(pr.get("pure_bw", False)),
                sharpen=bool(pr.get("sharpen", True)),
            )
        except Exception:
            pass

    interior_pages = sorted(list(proc_dir.glob("page_*.png")))
    if not interior_pages:
        interior_pages = sorted(list(proc_dir.glob("page_*.jpg")))
    if not interior_pages:
        interior_pages = sorted(list(raw_dir.glob("page_*.png")))
    if not interior_pages:
        interior_pages = sorted(list(raw_dir.glob("page_*.jpg")))

    p1 = interior_pages[0] if len(interior_pages) > 0 else None
    p2 = interior_pages[1] if len(interior_pages) > 1 else p1
    p3 = interior_pages[2] if len(interior_pages) > 2 else p1
    p4 = interior_pages[3] if len(interior_pages) > 3 else p2

    return {
        "preview_1": [f for f in dict.fromkeys([cover_front]) if f and f.exists()],
        "preview_2": [f for f in dict.fromkeys([p1, p2]) if f and f.exists()],
        "preview_3": [f for f in dict.fromkeys([p1]) if f and f.exists()],
        "preview_4": [f for f in dict.fromkeys([p1, p2, p3, p4]) if f and f.exists()],
        "preview_5": [f for f in dict.fromkeys([cover_front]) if f and f.exists()],
    }


def cmd_preview_parallel(cfg: dict) -> None:
    """Mở nhiều tab Gemini và sinh 5 ảnh Preview Marketing song song."""
    import asyncio
    from bookgen.gemini_pool import GeminiPool

    P = paths_of(cfg)
    raw_dir = P["raw_dir"]
    preview_dir = P.get("preview_dir") or (raw_dir.parent / "04_previews")
    preview_dir.mkdir(parents=True, exist_ok=True)
    
    title = cfg["book"].get("title", "Coloring Book")
    attachments_map = get_preview_attachments_map(cfg)

    templates = cfg.get("prompts", {}).get("previews", {})
    if not templates:
        templates = {
            "preview_1": "Generate a professional 3D product mockup photograph of a paperback coloring book standing isolated on a solid white background with a soft drop shadow. The attached image is the exact FRONT COVER. Printed on the front cover of the book mockup, render the attached image with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, title, words, letters, logos, or drawings. The front cover must be an exact 1:1 replica of the attached image without any extra added text, words, or illustrations. High resolution ecommerce product shot.",
            "preview_2": "Generate a realistic photograph of an open paperback coloring book laying flat on a warm wooden table next to colored pencils and a cup of tea. I have ATTACHED the exact image files of the INTERIOR COLORING PAGES. Render these attached black-and-white line-art pages printed clearly on the open left and right pages of the book with 100% exact visual fidelity, including the neat black border frame box around each page illustration.",
            "preview_3": "Generate a close-up photograph of hands actively coloring an open page inside a coloring book with colored pencils. I have ATTACHED the exact image file of the INTERIOR COLORING PAGE. Render this attached line-art drawing printed on the page with 100% exact visual fidelity, including the thin black border frame box around the page artwork.",
            "preview_4": "Generate a clean overhead flatlay photograph displaying a 2x2 grid collage of 4 interior coloring book pages on a bright wooden surface with colored pencils. I have ATTACHED the exact 4 image files of the INTERIOR COLORING PAGES. Render these 4 ATTACHED black-and-white line-art drawings clearly inside the 2x2 grid layout, one attached page per grid slot with 100% exact visual fidelity, reproducing the exact black border frame box around each of the 4 pages.",
            "preview_5": "Generate a warm aesthetic lifestyle photograph of a paperback coloring book on a cozy desk next to house plants, watercolor set, and soft morning sunlight. The attached image is the exact FRONT COVER. Display the physical book showing this exact cover image printed with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, titles, words, letters, logos, or drawings on the book cover. Keep all graphics and text 100% identical to the attached image without adding any new text or extra artwork.",
        }

    jobs = []
    for idx in range(1, 6):
        key = f"preview_{idx}"
        dest = preview_dir / f"{key}.png"
        if dest.exists() and dest.stat().st_size > 20_000:
            log.info("Bỏ qua %s (đã tồn tại: %d KB).", key, dest.stat().st_size // 1024)
            continue
        raw_prompt = templates.get(key, f"Coloring book mockup {idx}")
        prompt = raw_prompt.format(title=title)
        attach = attachments_map.get(key, [])
        jobs.append((key, prompt, dest, attach))

    if not jobs:
        log.info("✓ Tất cả 5 ảnh Preview Marketing đã sinh xong trước đó!")
        return

    async def run() -> None:
        async with GeminiPool(cfg) as pool:
            log.info("🖼️ Bắt đầu sinh SONG SONG 5 ảnh Preview Marketing trên %d tab...", pool.workers)
            def on_done(key: str, ok: bool) -> None:
                if ok:
                    print(f"  ✓ {key}.png")
                else:
                    print(f"  ✗ {key} thất bại")

            await pool.run_jobs(jobs, on_done)

    asyncio.run(run())


def cmd_preview(cfg: dict) -> None:
    """Tạo 5 ảnh Preview Marketing dùng ĐÚNG ảnh bìa & ảnh ruột thực tế vừa gen."""
    backend = cfg.get("backend", "web")
    b = cfg.get("browser", {})
    profiles = b.get("profiles", [])
    if not profiles and "user_data_dir" in b:
        profiles = [b["user_data_dir"]]
    workers = int(b.get("concurrency_per_profile", b.get("concurrency", 1))) * max(1, len(profiles))

    if backend == "web" and workers > 1:
        return cmd_preview_parallel(cfg)

    P = paths_of(cfg)
    raw_dir = P["raw_dir"]
    preview_dir = P.get("preview_dir") or (raw_dir.parent / "04_previews")
    preview_dir.mkdir(parents=True, exist_ok=True)
    
    title = cfg["book"].get("title", "Coloring Book")
    attachments_map = get_preview_attachments_map(cfg)

    templates = cfg.get("prompts", {}).get("previews", {})
    if not templates:
        templates = {
            "preview_1": "Generate a professional 3D product mockup photograph of a paperback coloring book standing isolated on a solid white background with a soft drop shadow. The attached image is the exact FRONT COVER. Printed on the front cover of the book mockup, render the attached image with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, title, words, letters, logos, or drawings. The front cover must be an exact 1:1 replica of the attached image without any extra added text, words, or illustrations. High resolution ecommerce product shot.",
            "preview_2": "Generate a realistic photograph of an open paperback coloring book laying flat on a warm wooden table next to colored pencils and a cup of tea. The attached images are the exact INTERIOR COLORING PAGES. Render these attached line-art pages printed on the open left and right pages of the book with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, titles, captions, words, or drawings on the open pages. The pages inside the book must showcase the exact attached black-and-white line-art drawings without any added text or modified artwork.",
            "preview_3": "Generate a close-up photograph of hands actively coloring an open page inside a coloring book with colored pencils. The attached image is the exact INTERIOR COLORING PAGE. Render this attached line-art drawing printed on the page with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, titles, words, or drawings on the page. Do not generate any new text or extra illustrations. Only realistic colored pencil shading applied within the existing attached line art.",
            "preview_4": "Generate a clean overhead flatlay photograph displaying 4 interior coloring pages arranged in a 2x2 grid collage on a bright wooden surface with colored pencils. The attached images are the exact INTERIOR COLORING PAGES. Render all 4 attached line-art pages with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, titles, words, numbers, or artwork on any of the 4 pages. Keep all line-art drawings and text 100% identical to the attached images without adding any new text or extra drawings.",
            "preview_5": "Generate a warm aesthetic lifestyle photograph of a paperback coloring book on a cozy desk next to house plants, watercolor set, and soft morning sunlight. The attached image is the exact FRONT COVER. Display the physical book showing this exact cover image printed with 100% exact visual fidelity. ABSOLUTE RULE: DO NOT add, modify, or remove any text, titles, words, letters, logos, or drawings on the book cover. Keep all graphics and text 100% identical to the attached image without adding any new text or extra artwork.",
        }

    log.info("🖼️ Bắt đầu tạo 5 ảnh Preview Marketing dùng ĐÚNG ảnh thật của '%s'...", title)
    with make_driver(cfg) as g:
        for idx in range(1, 6):
            from bookgen.cancel import check_cancel
            check_cancel()
            
            key = f"preview_{idx}"
            dest = preview_dir / f"{key}.png"
            if dest.exists() and dest.stat().st_size > 20_000:
                log.info("[%d/5] Bỏ qua %s (đã tồn tại: %d KB).", idx, key, dest.stat().st_size // 1024)
                continue

            raw_prompt = templates.get(key, f"Coloring book mockup {idx}")
            prompt = raw_prompt.format(title=title)
            attach = attachments_map.get(key, [])
            
            log.info("[%d/5] Đang tạo %s (đính kèm %d ảnh thật)...", idx, key, len(attach))
            if g.generate_image(prompt, dest, attach_files=attach):
                log.info("[%d/5] ✓ Hoàn thành %s!", idx, key)
            else:
                log.error("[%d/5] ✗ Thất bại khi tạo %s.", idx, key)
            g._sleep_jitter()


# --------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="Coloring Book Generator (Gemini -> Lulu)")
    ap.add_argument("command",
                    choices=["ask", "books", "generate", "process", "build",
                             "all", "check", "demo", "preview"])
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-b", "--book", default=None,
                    help="Chọn chủ đề (tên thư mục trong output/books). "
                         "Bỏ trống = cuốn vừa làm gần nhất.")
    args = ap.parse_args()

    cfg = load_cfg(ROOT / args.config)
    if args.book:
        if not (BOOKS_DIR / args.book).exists():
            print(f"Không có cuốn '{args.book}'. Các cuốn hiện có: "
                  f"{', '.join(list_books()) or '(chưa có)'}")
            return
        cfg["_book"] = args.book

    if args.command == "all":
        cmd_generate(cfg)
        cmd_process(cfg)
        cmd_build(cfg)
        cmd_check(cfg)
        cmd_preview(cfg)
    else:
        {"ask": cmd_ask, "books": cmd_books, "generate": cmd_generate,
         "process": cmd_process, "build": cmd_build, "check": cmd_check,
         "demo": cmd_demo, "preview": cmd_preview}[args.command](cfg)


if __name__ == "__main__":
    main()
