#!/usr/bin/env python3
"""
Script thử nghiệm ĐỘC LẬP: chỉ làm 1 việc — gửi 1 prompt lên gemini.google.com
và tải ảnh về. Không dựng PDF, không xử lý gì thêm.

Mục đích: xác minh phần khó nhất (điều khiển UI Gemini) hoạt động trước,
rồi mới nối vào pipeline.

CÁCH DÙNG — KHUYẾN NGHỊ (chế độ CDP, không bị Gemini chặn)
    pip install playwright pillow
    playwright install chrome

    1. Đóng hết Chrome, double-click start_chrome.bat
    2. Đăng nhập Google trong cửa sổ vừa mở, đợi thấy giao diện chat
    3. GIỮ cửa sổ đó mở, terminal khác chạy:
         python test_gemini.py --cdp
         python test_gemini.py --cdp --prompt "a happy fox under a mushroom"
         python test_gemini.py --cdp --inspect

CÁCH CŨ (Playwright tự mở Chrome — hay bị trang trắng, chỉ dùng nếu CDP hỏng)
    python test_gemini.py --login
    python test_gemini.py --prompt "..."
"""

from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

HERE = Path(__file__).parent
PROFILE = HERE / ".chrome-profile"
OUT = HERE / "output" / "01_raw"
URL = "https://gemini.google.com/app"

PROMPT_TMPL = (
    "Create a black and white coloring book page for children ages 4-8.\n"
    "Subject: {subject}.\n"
    "Style: clean bold black outlines only, pure white background, NO shading, "
    "NO grayscale, NO color, NO text anywhere. Thick uniform lines, simple shapes, "
    "large open areas easy to color. Vertical portrait, aspect ratio 8.5:11. "
    "High resolution."
)

SEL_PROMPT = [
    'div.ql-editor[contenteditable="true"]',
    'rich-textarea div[contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
]
SEL_SEND = [
    'button[aria-label*="Send" i]',
    'button[aria-label*="Gửi" i]',
    "button.send-button",
]
SEL_STOP = [
    'button[aria-label*="Stop" i]',
    'button[aria-label*="Dừng" i]',
    "button.stop-icon",
]
SEL_IMG = [
    "model-response img",
    'img[src^="https://lh3.googleusercontent.com"]',
    'img[src^="blob:"]',
    'img[src^="data:image"]',
    "single-image img",
]


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def set_clipboard(text: str) -> bool:
    """Đặt clipboard của Windows. Ctrl+V sau đó = dán tay 100%."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:  # fallback: tkinter có sẵn trong Python
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return True
    except Exception as e:
        log(f"không đặt được clipboard: {e}")
        return False


def enter_prompt(page, box, text: str, mode: str) -> str:
    """Nhập prompt bằng 1 trong 3 cách. Trả về nội dung đọc lại được từ ô nhập."""
    if mode != "manual":
        box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.wait_for_timeout(300)

    if mode == "manual":
        # Script KHÔNG nhập gì. Bạn tự dán tay, script chỉ bấm Send + tải ảnh.
        set_clipboard(text)
        print("\n" + "=" * 60)
        print("  CHẾ ĐỘ MANUAL — prompt đã nằm sẵn trong clipboard của bạn.")
        print("  Sang cửa sổ Chrome, bấm vào ô nhập, Ctrl+V dán vào.")
        print("  ĐỪNG bấm Send. Quay lại đây nhấn Enter, script sẽ bấm Send.")
        print("=" * 60)
        input()
    elif mode == "paste":
        if set_clipboard(text):
            page.keyboard.press("Control+V")
        else:
            page.keyboard.insert_text(text)
    elif mode == "insert":
        page.keyboard.insert_text(text)
    else:  # "type" - gõ từng ký tự
        page.keyboard.type(text, delay=6)

    if mode != "manual":
        wake_editor(page, box)

    page.wait_for_timeout(1_500)
    try:
        got = box.inner_text().strip()
    except Exception:
        got = ""
    return got


def wake_editor(page, box) -> None:
    """
    Đánh thức Angular/Quill để nút Send bật sáng.

    insert_text / Ctrl+V qua CDP đưa chữ vào DOM nhưng KHÔNG sinh chuỗi sự kiện
    bàn phím thật, nên change-detection của Gemini không chạy -> nút Send vẫn
    disabled. Cách chữa: gõ thêm rồi xoá 1 ký tự bằng phím THẬT, kèm bắn tay
    các event input/change để chắc ăn.
    """
    try:
        box.click()
        page.keyboard.press("End")
        page.keyboard.type(".", delay=60)      # phím thật -> kích hoạt detection
        page.wait_for_timeout(250)
        page.keyboard.press("Backspace")       # xoá đi, nội dung giữ nguyên
        page.wait_for_timeout(250)
    except Exception as e:
        log(f"wake_editor (bàn phím) lỗi: {e}")

    try:
        box.evaluate("""el => {
            for (const t of ['input','change','keyup','compositionend']) {
                el.dispatchEvent(new Event(t, {bubbles: true, composed: true}));
            }
            el.dispatchEvent(new KeyboardEvent('keydown',
                {key:'a', bubbles:true, composed:true}));
        }""")
    except Exception as e:
        log(f"wake_editor (event) lỗi: {e}")


def wait_send_enabled(page, timeout_ms: int = 12_000):
    """Chờ nút Send chuyển sang enabled. Trả về locator nút, hoặc None."""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for sel in SEL_SEND:
            try:
                for b in page.locator(sel).all():
                    if b.is_visible() and b.is_enabled():
                        log(f"nút Send đã bật: {sel}")
                        return b
            except Exception:
                continue
        page.wait_for_timeout(500)
    log("hết giờ chờ, nút Send vẫn disabled.")
    return None


def select_image_tool(page) -> bool:
    """
    Bật công cụ tạo ảnh trước khi gửi prompt.

    Gemini có nút '+' bên trái ô nhập, trong đó có mục 'Tạo hình ảnh' /
    'Create images'. Không bật mục này thì model Flash cố trả lời bằng chữ
    và hay trả về 'Sorry, something went wrong'.
    """
    plus = [
        'button[aria-label*="Thêm" i]',
        'button[aria-label*="Add" i]',
        'button[aria-label*="tệp" i]',
        'button[aria-label*="tool" i]',
        'button[aria-label*="công cụ" i]',
        'uploader-button-wrapper button',
    ]
    opened = False
    for sel in plus:
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=4_000)
                page.wait_for_timeout(1_200)
                opened = True
                log(f"đã mở menu công cụ: {sel}")
                break
        except Exception:
            continue

    if not opened:
        log("không tìm thấy nút '+' — bỏ qua bước chọn công cụ.")
        return False

    for pat in ("Tạo hình ảnh", "Create image", "Tạo ảnh", "Image", "Hình ảnh"):
        try:
            opt = page.get_by_text(pat, exact=False).first
            if opt.count() and opt.is_visible():
                opt.click(timeout=4_000)
                page.wait_for_timeout(1_000)
                log(f"đã bật công cụ tạo ảnh: '{pat}'")
                return True
        except Exception:
            continue

    log("mở được menu nhưng không thấy mục tạo ảnh — in các lựa chọn:")
    try:
        for el in page.locator('[role="menuitem"], [role="option"]').all()[:15]:
            if el.is_visible():
                print("    *", (el.inner_text() or "").strip()[:60])
    except Exception:
        pass
    page.keyboard.press("Escape")
    return False


def click_send(page, box) -> bool:
    """
    Bấm Send bằng nhiều chiến lược, dừng ở cái đầu tiên có tác dụng.
    Gemini hay đổi aria-label và để nút ở trạng thái disabled tới khi có nội dung,
    nên cần thử lần lượt thay vì tin vào 1 selector.
    """
    def sent() -> bool:
        """Coi như đã gửi nếu ô nhập trống đi (nội dung đã bay vào hội thoại)."""
        page.wait_for_timeout(1_500)
        try:
            return len(box.inner_text().strip()) < 10
        except Exception:
            return True

    # 0) chờ nút Send bật sáng rồi bấm — đường đúng nhất
    b = wait_send_enabled(page)
    if b is not None:
        try:
            b.click(timeout=5_000)
            if sent():
                log("gửi được bằng nút Send.")
                return True
        except Exception as e:
            log(f"bấm nút Send lỗi: {e}")

    # 1) Enter — cách tự nhiên nhất, Gemini gửi bằng Enter
    log("thử gửi bằng phím Enter...")
    try:
        box.click()
        page.keyboard.press("End")
        page.keyboard.press("Enter")
        if sent():
            log("gửi được bằng Enter.")
            return True
    except Exception as e:
        log(f"Enter lỗi: {e}")

    # 2) các selector đã biết, chỉ lấy nút đang enabled
    for sel in SEL_SEND:
        try:
            for b in page.locator(sel).all():
                if b.is_visible() and b.is_enabled():
                    log(f"thử bấm: {sel}")
                    b.click(timeout=5_000)
                    if sent():
                        log("gửi được bằng nút.")
                        return True
        except Exception:
            continue

    # 3) dò theo icon Material "send" bên trong button
    log("dò nút theo icon 'send'...")
    try:
        cands = page.locator("button:has(mat-icon), button:has(svg)").all()
        for b in cands:
            try:
                txt = (b.inner_text() or "").strip().lower()
                aria = (b.get_attribute("aria-label") or "").lower()
                if "send" in txt or "send" in aria or "gửi" in aria:
                    if b.is_visible() and b.is_enabled():
                        log(f"thử bấm nút aria='{aria}' text='{txt}'")
                        b.click(timeout=5_000)
                        if sent():
                            return True
            except Exception:
                continue
    except Exception:
        pass

    # 4) thất bại -> in ra toàn bộ nút để tôi sửa selector
    print("\n--- TẤT CẢ NÚT ĐANG HIỂN THỊ (gửi cho tôi phần này) ---")
    try:
        for b in page.locator("button").all():
            try:
                if not b.is_visible():
                    continue
                print(f"  aria={b.get_attribute('aria-label')!r} "
                      f"enabled={b.is_enabled()} "
                      f"class={(b.get_attribute('class') or '')[:60]!r} "
                      f"text={(b.inner_text() or '').strip()[:30]!r}")
            except Exception:
                continue
    except Exception:
        pass
    print("--------------------------------------------------------")
    page.screenshot(path=str(HERE / "debug_send.png"), full_page=True)
    print(f"Ảnh màn hình: {HERE / 'debug_send.png'}")
    return False


def flatten(text: str) -> str:
    """Gộp prompt nhiều dòng thành 1 dòng.

    Gemini xử lý prompt có xuống dòng (Shift+Enter) khác hẳn prompt dán 1 khối,
    và hay trả về 'I'm having a hard time fulfilling your request'.
    """
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


def find(page, selectors, timeout=10_000):
    per = max(1500, timeout // max(1, len(selectors)))
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per)
            log(f"khớp selector: {sel}")
            return loc
        except PWTimeout:
            continue
    raise PWTimeout(f"Không selector nào khớp: {selectors}")


CDP_PORT = 9222


def open_browser(pw, headless=False):
    """Chế độ cũ: Playwright tự mở Chrome với profile riêng."""
    PROFILE.mkdir(parents=True, exist_ok=True)
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        headless=headless,
        channel="chrome",
        viewport={"width": 1440, "height": 960},
        args=["--disable-blink-features=AutomationControlled",
              "--no-first-run", "--no-default-browser-check"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    return ctx, page


def attach_cdp(pw):
    """
    Chế độ khuyến nghị: gắn vào Chrome THẬT đang chạy ở cổng 9222.
    Chrome do bạn tự mở (start_chrome.bat) nên Gemini coi như phiên bình thường.
    """
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=15_000)
    except Exception as e:
        print("\n✗ Không kết nối được Chrome ở cổng 9222.")
        print("  Hãy: đóng hết Chrome -> double-click start_chrome.bat -> để cửa sổ đó MỞ")
        print(f"  (chi tiết: {e})")
        sys.exit(1)

    ctx = browser.contexts[0] if browser.contexts else browser.new_context()

    # tìm tab Gemini có sẵn, không có thì mở tab mới
    page = None
    for p in ctx.pages:
        if "gemini.google.com" in (p.url or ""):
            page = p
            break
    if page is None:
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)

    page.bring_to_front()
    log(f"đã gắn vào Chrome thật, tab: {page.url}")
    return browser, page


def get_page(pw, use_cdp: bool):
    """Trả về (đối tượng cần đóng, page). Ở chế độ CDP KHÔNG đóng trình duyệt của bạn."""
    if use_cdp:
        browser, page = attach_cdp(pw)
        return None, page   # None = đừng đóng, đó là Chrome của user
    ctx, page = open_browser(pw)
    page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
    return ctx, page


# ------------------------------------------------------------------ lệnh

def do_login():
    with sync_playwright() as pw:
        ctx, page = open_browser(pw)
        page.goto(URL, wait_until="domcontentloaded", timeout=90_000)
        print("\n" + "=" * 60)
        print("  Đăng nhập Google trong cửa sổ Chrome vừa mở.")
        print("  Khi đã thấy giao diện chat Gemini, quay lại đây nhấn Enter.")
        print("=" * 60)
        input()
        try:
            find(page, SEL_PROMPT, 30_000)
            print("\n✓ OK — profile đã lưu ở .chrome-profile, lần sau không cần đăng nhập lại.")
        except PWTimeout:
            print("\n✗ Vẫn chưa thấy ô nhập prompt. Chạy `--inspect` để xem DOM thật.")
        ctx.close()


def do_inspect(use_cdp: bool = False):
    """Dò DOM thật của Gemini và in ra selector khả dụng — dùng khi Google đổi giao diện."""
    with sync_playwright() as pw:
        ctx, page = get_page(pw, use_cdp)
        page.wait_for_timeout(6_000)

        print("\n--- contenteditable ---")
        for el in page.locator('[contenteditable="true"]').all()[:10]:
            print("   ", el.evaluate(
                "e=>e.tagName+' class='+e.className+' aria='+(e.getAttribute('aria-label')||'')"))

        print("\n--- button có aria-label ---")
        for el in page.locator("button[aria-label]").all()[:30]:
            print("   ", el.get_attribute("aria-label"), "| class=", el.get_attribute("class"))

        print("\n--- custom element (thẻ có dấu -) ---")
        tags = page.evaluate(
            "()=>[...new Set([...document.querySelectorAll('*')]"
            ".map(e=>e.tagName.toLowerCase()).filter(t=>t.includes('-')))]")
        print("   ", tags)

        shot = HERE / "inspect.png"
        page.screenshot(path=str(shot), full_page=True)
        print(f"\nẢnh màn hình: {shot}")
        print("Gửi output này cho tôi nếu selector cần sửa. Nhấn Enter để kết thúc.")
        input()
        if ctx:
            ctx.close()


def download(page, src: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.startswith("data:image"):
        dest.write_bytes(base64.b64decode(src.split(",", 1)[1]))
        return True
    if src.startswith("blob:"):
        b64 = page.evaluate(
            """async u=>{const r=await fetch(u);const b=await r.blob();
               return await new Promise(res=>{const f=new FileReader();
               f.onload=()=>res(f.result.split(',')[1]);f.readAsDataURL(b);});}""", src)
        dest.write_bytes(base64.b64decode(b64))
        return True
    # xin bản gốc lớn nhất
    hi = re.sub(r"=[swh]\d+(-[a-z0-9-]+)?$", "=s0", src)
    for u in (hi, src):
        try:
            r = page.request.get(u, timeout=60_000)
            if r.ok and len(r.body()) > 5_000:
                dest.write_bytes(r.body())
                return True
        except Exception as e:
            log(f"tải lỗi {u[:60]}...: {e}")
    return False


def collect(page) -> list[str]:
    urls = []
    for sel in SEL_IMG:
        for el in page.locator(sel).all():
            try:
                src = el.get_attribute("src") or ""
                box = el.bounding_box()
            except Exception:
                continue
            if not src or src in urls:
                continue
            if box and (box["width"] < 120 or box["height"] < 120):
                continue  # bỏ avatar/icon
            urls.append(src)
    return urls


def do_generate(subject: str, timeout: int, use_cdp: bool = False,
                mode: str = "type", pause: bool = False, no_tool: bool = False):
    prompt = PROMPT_TMPL.format(subject=subject)
    with sync_playwright() as pw:
        ctx, page = get_page(pw, use_cdp)

        try:
            find(page, SEL_PROMPT, 25_000)
        except PWTimeout:
            print("✗ Chưa đăng nhập hoặc DOM đã đổi.")
            print("  Thử: python test_gemini.py --cdp --inspect")
            if ctx:
                ctx.close()
            sys.exit(1)

        before = set(collect(page))

        text = flatten(prompt)

        if not no_tool:
            select_image_tool(page)

        box = find(page, SEL_PROMPT)

        log(f"nhập prompt bằng chế độ '{mode}' ({len(text)} ký tự)...")
        got = enter_prompt(page, box, text, mode)

        # --- đối chiếu: ô nhập có đúng nội dung không? ---
        print("\n--- NỘI DUNG THỰC SỰ TRONG Ô NHẬP ---")
        print(got if got else "(TRỐNG!)")
        print(f"--- {len(got)} / {len(text)} ký tự ---\n")
        if len(got) < len(text) * 0.9:
            print("⚠ Ô nhập KHÔNG khớp prompt -> đây chính là nguyên nhân lỗi.")
            print("  Thử chế độ khác: --mode paste | --mode insert | --mode type")

        if pause:
            print("Đã dừng. Kiểm tra cửa sổ Chrome, rồi nhấn Enter để bấm Send...")
            input()

        if not click_send(page, box):
            print("✗ Không bấm được Send bằng mọi cách. Xem danh sách nút ở trên.")
            if ctx:
                ctx.close()
            sys.exit(1)
        log("đã gửi, đang chờ Gemini vẽ...")

        # chờ bắt đầu rồi chờ kết thúc
        for sel in SEL_STOP:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=15_000)
                break
            except PWTimeout:
                continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = any(
                page.locator(s).first.is_visible() for s in SEL_STOP
                if page.locator(s).count()
            )
            if not busy:
                break
            page.wait_for_timeout(1_500)
        page.wait_for_timeout(3_000)

        new = [u for u in collect(page) if u not in before]
        log(f"tìm thấy {len(new)} ảnh mới")
        if not new:
            # in ra Gemini đã trả lời gì
            print("\n--- GEMINI TRẢ LỜI ---")
            for sel in ("model-response", "message-content", "[data-response-index]"):
                try:
                    loc = page.locator(sel).last
                    if loc.count() and loc.is_visible():
                        print(loc.inner_text()[:800])
                        break
                except Exception:
                    continue
            print("----------------------")
            shot = HERE / "debug_no_image.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"\n✗ Không có ảnh. Xem {shot} — có thể Gemini trả lời bằng chữ,")
            print("  hoặc model đang chọn không hỗ trợ sinh ảnh.")
            if ctx:
                ctx.close()
            sys.exit(1)

        dest = OUT / "test_page.png"
        if download(page, new[-1], dest):
            from PIL import Image
            with Image.open(dest) as im:
                w, h = im.size
            print(f"\n✓ THÀNH CÔNG: {dest}")
            print(f"  Kích thước: {w} x {h} px")
            print(f"  In khổ 8.5x11 in => {min(w/8.5, h/11):.0f} DPI "
                  f"({'đủ' if min(w/8.5, h/11) >= 300 else 'THIẾU, cần upscale'})")
        else:
            print("\n✗ Thấy ảnh nhưng tải về thất bại.")

        if ctx:
            print("\nNhấn Enter để đóng trình duyệt.")
            input()
            ctx.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", action="store_true",
                    help="gắn vào Chrome thật ở cổng 9222 (chạy start_chrome.bat trước)")
    ap.add_argument("--login", action="store_true", help="[cũ] Playwright tự mở Chrome")
    ap.add_argument("--inspect", action="store_true", help="dò selector khi DOM đổi")
    ap.add_argument("--prompt", default="a happy fox sitting under a big mushroom")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--mode", choices=["type", "paste", "insert", "manual"],
                    default="type",
                    help="type=gõ phím thật, 1 dòng (mặc định, bật được nút Send) | "
                         "paste=Ctrl+V | manual=bạn tự dán, script chỉ bấm Send")
    ap.add_argument("--pause", action="store_true",
                    help="dừng trước khi bấm Send để bạn kiểm tra ô nhập")
    ap.add_argument("--no-tool", action="store_true",
                    help="không tự bật công cụ tạo ảnh (nếu bạn đã bật sẵn)")
    a = ap.parse_args()

    if a.login:
        do_login()
    elif a.inspect:
        do_inspect(a.cdp)
    else:
        do_generate(a.prompt, a.timeout, a.cdp, a.mode, a.pause, a.no_tool)


if __name__ == "__main__":
    main()
