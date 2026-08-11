"""
Điều khiển gemini.google.com bằng Playwright.

LƯU Ý QUAN TRỌNG:
  - Đây là tự động hoá giao diện web, KHÔNG phải API chính thức.
  - Google có thể đổi DOM bất cứ lúc nào -> selector trong SELECTORS bên dưới
    được viết theo kiểu "thử nhiều cách", nhưng bạn vẫn có thể phải sửa tay.
  - Chạy quá nhanh / quá nhiều có thể bị hạn chế tài khoản. Giữ delay mặc định.
  - Lần chạy đầu: headless=false, tự đăng nhập Google trong cửa sổ hiện ra.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from pathlib import Path

try:
    from playwright.sync_api import (
        Page,
        TimeoutError as PWTimeout,
        sync_playwright,
    )
except ImportError:  # backend: api không cần Playwright
    Page = object  # type: ignore[assignment,misc]

    class PWTimeout(Exception):  # type: ignore[no-redef]
        pass

    def sync_playwright():  # type: ignore[misc]
        raise RuntimeError(
            "Chưa cài Playwright. Chạy `pip install playwright && playwright install chrome`, "
            "hoặc dùng backend: api trong config.yaml."
        )

log = logging.getLogger(__name__)


# Nhiều selector cho mỗi mục đích -> thử lần lượt cho tới khi thấy.
SELECTORS = {
    "prompt_box": [
        'div.ql-editor[contenteditable="true"]',
        'rich-textarea div[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'textarea[aria-label*="prompt" i]',
    ],
    "send_button": [
        'button[aria-label*="Send" i]',
        'button[aria-label*="Gửi" i]',
        'button.send-button',
        'button[mattooltip*="Send" i]',
    ],
    "stop_button": [
        'button[aria-label*="Stop" i]',
        'button[aria-label*="Dừng" i]',
        'button.stop-icon',
    ],
    # Ảnh do model sinh ra nằm trong lượt trả lời cuối
    "response_container": [
        "model-response",
        "message-content",
        'div[data-response-index]',
    ],
    # Thứ tự QUAN TRỌNG: ưu tiên ảnh nằm trong lượt trả lời của model.
    # Selector chung chung (img[src^=lh3...]) để cuối vì nó dính cả avatar.
    "generated_image": [
        "model-response single-image img",
        "model-response generated-image img",
        "model-response img",
        "message-content img",
        "single-image img",
        "generated-image img",
        'img[src^="blob:"]',
        'img[src^="data:image"]',
        'img[src^="https://lh3.googleusercontent.com"]',
    ],
    # Trình xem ảnh phóng to (mở ra sau khi bấm vào ảnh trong khung chat).
    # QUAN TRỌNG: ảnh nằm trong khung chat chỉ là bản xem trước độ phân giải
    # thấp. Phải bấm vào nó để Gemini nạp bản gốc thì mới lấy được ảnh in được.
    "image_viewer": [
        "image-viewer img",
        'div[role="dialog"] img',
        "lightbox img",
        ".image-panel img",
    ],
    "download_button": [
        'button[aria-label*="Download" i]',
        'button[aria-label*="Tải xuống" i]',
        'button[aria-label*="Tải về" i]',
        'a[download]',
    ],
    "close_viewer": [
        'button[aria-label*="Close" i]',
        'button[aria-label*="Đóng" i]',
    ],
    "new_chat": [
        'button[aria-label*="New chat" i]',
        'button[aria-label*="Cuộc trò chuyện mới" i]',
        'a[aria-label*="New chat" i]',
    ],
}


def flatten(text: str) -> str:
    """Gộp prompt nhiều dòng thành một dòng duy nhất trước khi gửi."""
    return " ".join(line.strip() for line in text.split("\n") if line.strip())


# ---------------------------------------------------------------- lọc ảnh
#
# Bài học xương máu: bộ lọc cũ chỉ bỏ ảnh nhỏ hơn 120px nên avatar/icon của
# giao diện Gemini (235x235) lọt qua, và mọi trang đều tải về CÙNG một ảnh.
# Giờ có ba lớp chặn: bỏ URL của ảnh giao diện, đòi kích thước tối thiểu,
# và quan trọng nhất là MỞ FILE RA KIỂM TRA sau khi tải.

MIN_ART_PX = 512          # cạnh ngắn nhất của một tranh thật
MIN_BOX_PX = 260          # cạnh nhỏ nhất của thẻ <img> trên trang

# Ảnh giao diện: avatar tài khoản, logo, icon...
UI_ASSET_HINTS = (
    "/a/acg8",            # ảnh đại diện Google
    "/a-/",
    "gstatic.com",
    "ssl.gstatic",
    "googlelogo",
    "/favicon",
)


def is_ui_asset(src: str) -> bool:
    low = src.lower()
    if any(h in low for h in UI_ASSET_HINTS):
        return True
    # đuôi kích thước bé: =s32, =s64-c, =w96-h96...
    m = re.search(r"=[sw](\d+)", low)
    return bool(m and int(m.group(1)) < MIN_BOX_PX)


def upscale_url(src: str) -> str:
    """Xin bản gốc lớn nhất: .../abc=s235-c -> .../abc=s0"""
    tail = src.rsplit("/", 1)[-1]
    if "=" in tail:
        return src[: src.rindex("=")] + "=s0"
    return src


def is_real_art(path: Path, min_px: int = MIN_ART_PX) -> bool:
    """Mở file kiểm tra đây có phải tranh thật không, không phải icon/thumbnail."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
    except Exception as e:  # noqa: BLE001
        log.debug("Không mở được %s: %s", path, e)
        return False
    if min(w, h) < min_px:
        log.warning("Bỏ %s: chỉ %dx%d, quá nhỏ để là tranh (cần >= %dpx).",
                    path.name, w, h, min_px)
        return False
    return True


class GeminiDriver:
    """Context manager mở Chrome có profile lưu sẵn và nói chuyện với Gemini."""

    def __init__(self, cfg: dict):
        b = cfg["browser"]
        self.url = b["gemini_url"]
        self.user_data_dir = Path(b["user_data_dir"]).resolve()
        self.headless = b.get("headless", False)
        self.timeout = b.get("generation_timeout", 240)
        self.delay_range = b.get("delay_between_prompts", [8, 20])
        self.max_retries = b.get("max_retries", 3)
        self._pw = None
        self._ctx = None
        self.page: Page | None = None

    # ---------- lifecycle ----------

    def __enter__(self) -> "GeminiDriver":
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            channel="chrome",              # dùng Chrome thật, ít bị chặn hơn Chromium
            viewport={"width": 1440, "height": 960},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
            ],
        )
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # Ẩn navigator.webdriver
        self.page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self.page.goto(self.url, wait_until="domcontentloaded", timeout=90_000)
        self._ensure_logged_in()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _ensure_logged_in(self) -> None:
        """Chờ tới khi thấy ô nhập prompt. Nếu chưa đăng nhập -> yêu cầu user làm tay."""
        try:
            self._find(SELECTORS["prompt_box"], timeout=20_000)
            log.info("Đã đăng nhập Gemini.")
            return
        except PWTimeout:
            pass

        if self.headless:
            raise RuntimeError(
                "Chưa đăng nhập Google và đang chạy headless. "
                "Hãy đặt browser.headless: false trong config.yaml, chạy lại, "
                "đăng nhập trong cửa sổ Chrome, rồi mới bật headless."
            )

        print("\n" + "=" * 62)
        print("  Cửa sổ Chrome đã mở. Hãy ĐĂNG NHẬP Google và mở Gemini.")
        print("  Đăng nhập xong, quay lại đây và nhấn Enter...")
        print("=" * 62 + "\n")
        input()
        self._find(SELECTORS["prompt_box"], timeout=60_000)
        log.info("Đăng nhập thành công. Profile đã lưu, lần sau không cần làm lại.")

    # ---------- helper ----------

    def _find(self, selectors: list[str], timeout: int = 10_000):
        """Thử từng selector, trả về locator đầu tiên nhìn thấy được."""
        last = None
        per = max(1500, timeout // max(1, len(selectors)))
        for sel in selectors:
            try:
                loc = self.page.locator(sel).first
                loc.wait_for(state="visible", timeout=per)
                return loc
            except PWTimeout as e:
                last = e
        raise PWTimeout(f"Không tìm thấy element nào khớp: {selectors}") from last

    def _sleep_jitter(self) -> None:
        lo, hi = self.delay_range
        t = random.uniform(lo, hi)
        log.info("Nghỉ %.1fs trước prompt tiếp theo...", t)
        time.sleep(t)

    def new_chat(self) -> None:
        """Mở hội thoại mới để mỗi ảnh không bị ảnh hưởng bởi ngữ cảnh trước."""
        try:
            self._find(SELECTORS["new_chat"], timeout=5_000).click()
            self.page.wait_for_timeout(2_000)
        except PWTimeout:
            self.page.goto(self.url, wait_until="domcontentloaded")
            self.page.wait_for_timeout(3_000)

    # ---------- gửi prompt ----------

    def send_prompt(self, text: str) -> None:
        box = self._find(SELECTORS["prompt_box"], timeout=30_000)
        box.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        # QUAN TRỌNG - hai điều rút ra sau khi gỡ lỗi:
        #  1. KHÔNG dùng Shift+Enter. Prompt nhiều dòng khiến Gemini trả lời
        #     "I'm having a hard time fulfilling your request".
        #  2. PHẢI gõ bằng phím thật (keyboard.type). insert_text/Ctrl+V qua CDP
        #     không kích hoạt change-detection của Angular -> nút Send kẹt disabled.
        self.page.keyboard.type(flatten(text), delay=random.randint(3, 10))
        self._wake_editor(box)
        self.page.wait_for_timeout(1_200)
        if not self._click_send(box):
            raise RuntimeError("Không bấm được nút Send.")
        log.info("Đã gửi prompt (%d ký tự).", len(text))

    def _wake_editor(self, box) -> None:
        """Kích hoạt change-detection của Gemini để nút Send bật sáng.

        insert_text qua CDP không sinh sự kiện bàn phím thật nên Angular không
        biết ô nhập đã có chữ -> nút Send kẹt ở trạng thái disabled.
        """
        try:
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.type(".", delay=60)
            self.page.wait_for_timeout(250)
            self.page.keyboard.press("Backspace")
            self.page.wait_for_timeout(250)
        except Exception as e:  # noqa: BLE001
            log.debug("wake_editor bàn phím: %s", e)
        try:
            box.evaluate("""el => {
                for (const t of ['input','change','keyup','compositionend'])
                    el.dispatchEvent(new Event(t,{bubbles:true,composed:true}));
            }""")
        except Exception as e:  # noqa: BLE001
            log.debug("wake_editor event: %s", e)

    def _wait_send_enabled(self, timeout_s: float = 12.0):
        """Chờ một nút gửi bật sáng. BỎ QUA nút đang ở trạng thái Stop."""
        deadline = time.time() + timeout_s
        loc = self.page.locator(", ".join(SELECTORS["send_button"]))
        while time.time() < deadline:
            try:
                for b in loc.all():
                    if not (b.is_visible() and b.is_enabled()):
                        continue
                    aria = (b.get_attribute("aria-label") or "").lower()
                    if "stop" in aria or "dừng" in aria:
                        continue
                    return b
            except Exception:
                pass
            self.page.wait_for_timeout(400)
        return None

    def _click_send(self, box) -> bool:
        """Gửi prompt bằng ĐÚNG MỘT cú bấm.

        CẢNH BÁO: nút Send và nút Stop của Gemini là cùng một nút, chỉ đổi
        aria-label sau khi gửi. Bản cũ có 4 phương án dự phòng, bấm liên tiếp
        cho tới khi 'sent()' trả True - cú bấm thứ hai rơi trúng nút Stop và
        huỷ luôn câu trả lời đang vẽ ("Bạn đã dừng câu trả lời này"), nên
        không bao giờ có ảnh. Đừng bao giờ bấm lần hai.
        """
        stop = self.page.locator(", ".join(SELECTORS["stop_button"])).first

        def started() -> bool:
            """Gửi thành công = nút Stop hiện ra HOẶC ô nhập trống đi."""
            for _ in range(16):                       # tối đa ~8 giây
                try:
                    if stop.is_visible():
                        return True
                except Exception:
                    pass
                try:
                    if len(box.inner_text().strip()) < 10:
                        return True
                except Exception:
                    return True
                self.page.wait_for_timeout(500)
            return False

        b = self._wait_send_enabled()
        if b is not None:
            try:
                b.click(timeout=5_000)
                return started()
            except Exception as e:  # noqa: BLE001
                log.debug("Bấm nút gửi lỗi: %s", e)

        try:                                          # dự phòng: phím Enter
            box.click()
            self.page.keyboard.press("End")
            self.page.keyboard.press("Enter")
            return started()
        except Exception:
            return False

    def wait_for_generation(self) -> None:
        """Chờ tới khi nút Stop biến mất (nghĩa là model trả lời xong)."""
        deadline = time.time() + self.timeout
        # đợi nút Stop xuất hiện (bắt đầu sinh)
        for sel in SELECTORS["stop_button"]:
            try:
                self.page.locator(sel).first.wait_for(state="visible", timeout=15_000)
                break
            except PWTimeout:
                continue
        # đợi nó biến mất
        while time.time() < deadline:
            visible = False
            for sel in SELECTORS["stop_button"]:
                try:
                    if self.page.locator(sel).first.is_visible():
                        visible = True
                        break
                except Exception:
                    pass
            if not visible:
                self.page.wait_for_timeout(2_500)  # đệm cho ảnh render xong
                return
            self.page.wait_for_timeout(1_500)
        raise TimeoutError(f"Gemini không trả lời xong trong {self.timeout}s.")

    # ---------- lấy ảnh ----------

    def _collect_image_urls(self) -> list[str]:
        """Ảnh ứng viên, to nhất đứng trước. Xem ghi chú ở đầu file về bộ lọc."""
        best: dict[str, float] = {}
        for sel in SELECTORS["generated_image"]:
            for el in self.page.locator(sel).all():
                try:
                    src = el.get_attribute("src") or ""
                except Exception:
                    continue
                if not src or is_ui_asset(src):
                    continue
                area = 0.0
                try:
                    box = el.bounding_box()
                    if box:
                        if min(box["width"], box["height"]) < MIN_BOX_PX:
                            continue
                        area = box["width"] * box["height"]
                except Exception:
                    pass
                best[src] = max(best.get(src, 0.0), area)
        return [s for s, _ in sorted(best.items(), key=lambda kv: -kv[1])]

    def _download(self, src: str, dest: Path) -> bool:
        """Tải ảnh về. Xử lý được data:, blob: và http(s)."""
        dest.parent.mkdir(parents=True, exist_ok=True)

        if src.startswith("data:image"):
            b64 = src.split(",", 1)[1]
            dest.write_bytes(base64.b64decode(b64))
            return True

        if src.startswith("blob:"):
            # đọc blob trong trang rồi chuyển sang base64
            b64 = self.page.evaluate(
                """async (url) => {
                    const r = await fetch(url);
                    const b = await r.blob();
                    return await new Promise(res => {
                        const fr = new FileReader();
                        fr.onload = () => res(fr.result.split(',')[1]);
                        fr.readAsDataURL(b);
                    });
                }""",
                src,
            )
            dest.write_bytes(base64.b64decode(b64))
            return True

        # http(s): xin bản gốc độ phân giải cao nhất từ googleusercontent
        for candidate in (upscale_url(src), src):
            try:
                resp = self.page.request.get(candidate, timeout=60_000)
                if not resp.ok:
                    continue
                body = resp.body()
                if len(body) < 20_000:
                    log.debug("Bỏ %s: chỉ %d byte.", candidate, len(body))
                    continue
                dest.write_bytes(body)
                return True
            except Exception as e:  # noqa: BLE001
                log.debug("Tải %s lỗi: %s", candidate, e)
        return False

    # ---------- API chính ----------

    def generate_image(self, prompt: str, dest: Path) -> Path | None:
        """Gửi 1 prompt, chờ, tải ảnh về dest. Trả về đường dẫn hoặc None."""
        for attempt in range(1, self.max_retries + 1):
            try:
                self.new_chat()
                before = set(self._collect_image_urls())
                self.send_prompt(prompt)
                self.wait_for_generation()

                # Chờ tới khi có ảnh thật, mốc là ẢNH chứ không phải nút Stop.
                new_urls: list[str] = []
                deadline = time.time() + self.timeout
                while time.time() < deadline:
                    new_urls = [u for u in self._collect_image_urls()
                                if u not in before]
                    if new_urls:
                        break
                    self.page.wait_for_timeout(2_000)
                if not new_urls:
                    raise RuntimeError(f"Không thấy ảnh sau {self.timeout}s.")

                for url in new_urls[:4]:
                    if self._download(url, dest) and is_real_art(dest):
                        log.info("OK -> %s", dest.name)
                        return dest
                    if dest.exists():
                        dest.unlink()
                raise RuntimeError("Không lấy được ảnh thật (chỉ thấy icon/thumbnail).")

            except Exception as e:  # noqa: BLE001
                log.warning("Lần %d/%d thất bại: %s", attempt, self.max_retries, e)
                shot = dest.with_name(f"debug_{dest.stem}_try{attempt}.png")
                try:
                    self.page.screenshot(path=str(shot), full_page=True)
                    log.warning("Ảnh chụp màn hình gỡ lỗi: %s", shot)
                except Exception:
                    pass
                if attempt < self.max_retries:
                    time.sleep(random.uniform(15, 35))
            finally:
                pass
        return None

    def ask_text(self, prompt: str) -> str:
        """Hỏi Gemini và lấy về câu trả lời dạng text (dùng để sinh danh sách chủ đề)."""
        self.new_chat()
        self.send_prompt(prompt)
        self.wait_for_generation()
        for sel in SELECTORS["response_container"]:
            loc = self.page.locator(sel).last
            try:
                if loc.is_visible():
                    return loc.inner_text()
            except Exception:
                continue
        return ""


def parse_subject_list(raw: str, want: int) -> list[str]:
    """Bóc danh sách chủ đề từ câu trả lời text của Gemini."""
    # thử JSON trước
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            items = json.loads(m.group(0))
            if isinstance(items, list):
                return [str(x).strip() for x in items][:want]
        except Exception:
            pass
    lines = []
    for line in raw.splitlines():
        line = re.sub(r"^\s*(?:\d+[.)]|[-*•])\s*", "", line).strip().strip('"')
        if 8 < len(line) < 200:
            lines.append(line)
    return lines[:want]
