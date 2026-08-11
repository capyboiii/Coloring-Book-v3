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


def paths_of(cfg: dict) -> dict[str, Path]:
    return {k: (ROOT / v) for k, v in cfg["paths"].items()}


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


def build_jobs(cfg: dict, subjects: list[str], raw: Path,
               state: dict) -> list[tuple[str, str, Path]]:
    """Danh sách việc còn thiếu: [(key, prompt, dest)] - đã bỏ ảnh đã có."""
    n = cfg["book"]["num_images"]
    tmpl = cfg["prompts"]["page"]
    jobs: list[tuple[str, str, Path]] = []

    for i, subj in enumerate(subjects[:n], start=1):
        key = f"page_{i:03d}"
        dest = raw / f"{key}.png"
        if key in state["done"] and dest.exists():
            continue
        jobs.append((key, tmpl.format(i=i, subject=subj), dest))

    for key, tpl in [("cover_front", cfg["prompts"]["front_cover"]),
                     ("cover_back", cfg["prompts"]["back_cover"])]:
        dest = raw / f"{key}.png"
        if key in state["done"] and dest.exists():
            continue
        jobs.append((key, tpl.format(title=cfg["book"]["title"]), dest))

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
            log.info("Xong: %d/%d ảnh.", len(results) - len(failed), len(results))
            if failed:
                log.warning("Chạy lại `python main.py generate` để làm nốt: %s",
                            ", ".join(failed))

    asyncio.run(run())
    log.info("Ảnh gốc ở: %s", raw)


def cmd_generate(cfg: dict) -> None:
    from bookgen.gemini_driver import parse_subject_list

    backend = (cfg.get("backend") or "api").lower()
    if backend == "web" and int(cfg["browser"].get("concurrency", 1)) > 1:
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
    p = cfg["print"]
    dpi = p["dpi"]
    w_in = p["trim_width"] + 2 * p["bleed"]
    h_in = p["trim_height"] + 2 * p["bleed"]
    w_px, h_px = int(w_in * dpi), int(h_in * dpi)

    raw, proc = P["raw_dir"], P["processed_dir"]
    if not raw.exists():
        log.error("Chưa có ảnh gốc. Chạy `python main.py generate` trước.")
        return

    for f in sorted(raw.glob("page_*.png")):
        imaging.process_lineart(f, proc / f.name, w_px, h_px)

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

def archive_old_raw(raw: Path, state_file: Path) -> None:
    """Dời ảnh của cuốn sách cũ sang thư mục backup để đánh số lại từ 1."""
    import datetime
    import shutil

    if not any(raw.glob("*.png")):
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = raw.parent / f"01_raw_backup_{stamp}"
    shutil.move(str(raw), str(dest))
    raw.mkdir(parents=True, exist_ok=True)
    if state_file.exists():
        state_file.rename(state_file.with_name(f"state_backup_{stamp}.json"))
    print(f"  Đã dời ảnh cũ sang: {dest}")


def cmd_ask(cfg: dict) -> None:
    """Hỏi chủ đề + số trang, rồi vẽ TOÀN BỘ sách: trang ruột + 2 bìa.

    Gemini tự nghĩ ra từng cảnh cho mỗi trang dựa trên chủ đề bạn nhập.
    """
    import asyncio

    from bookgen.gemini_driver import parse_subject_list
    from bookgen.gemini_pool import GeminiPool

    P = paths_of(cfg)
    raw = P["raw_dir"]

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

    # Chủ đề mới = sách mới -> dọn ảnh cũ để đánh số lại từ page_001
    if any(raw.glob("*.png")):
        ans = input(f"\nĐang có ảnh cũ trong {raw.name}. "
                    "Dời sang backup và làm sách mới? [Y/n]> ").strip().lower()
        if ans in ("", "y", "yes"):
            archive_old_raw(raw, P["state_file"])
        else:
            print("Huỷ. Chạy `python main.py generate` nếu muốn làm tiếp sách cũ.")
            return

    # Cho toàn bộ prompt (trang ruột lẫn bìa) dùng chủ đề vừa nhập
    cfg["book"]["title"] = theme
    cfg["book"]["num_images"] = n
    raw.mkdir(parents=True, exist_ok=True)
    state = {"done": [], "subjects": []}

    print(f"\n  Chủ đề: {theme}")
    print(f"  Sẽ vẽ: {n} trang ruột + bìa trước + bìa sau = {n + 2} ảnh")
    print(f"  Ước tính: {(n + 2) * 45 // 60}-{(n + 2) * 90 // 60} phút\n")

    async def run() -> None:
        async with GeminiPool(cfg) as pool:
            # 1) nhờ Gemini nghĩ n cảnh từ chủ đề
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
            print(f"\n  Xong {len(results) - len(failed)}/{len(results)} ảnh.")
            if failed:
                print(f"  Thiếu: {', '.join(failed)}")
                print("  Chạy `python main.py generate` để vẽ nốt phần thiếu.")

    asyncio.run(run())
    print(f"\n  Ảnh ở: {raw}")
    print("  Bước tiếp: python main.py process && python main.py build\n")
    log.info("Ảnh nằm ở: %s", raw)


# --------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(description="Coloring Book Generator (Gemini -> Lulu)")
    ap.add_argument("command",
                    choices=["ask", "generate", "process", "build", "all",
                             "check", "demo"])
    ap.add_argument("-c", "--config", default="config.yaml")
    args = ap.parse_args()

    cfg = load_cfg(ROOT / args.config)

    if args.command == "all":
        cmd_generate(cfg)
        cmd_process(cfg)
        cmd_build(cfg)
        cmd_check(cfg)
    else:
        {"ask": cmd_ask, "generate": cmd_generate, "process": cmd_process,
         "build": cmd_build, "check": cmd_check,
         "demo": cmd_demo}[args.command](cfg)


if __name__ == "__main__":
    main()
