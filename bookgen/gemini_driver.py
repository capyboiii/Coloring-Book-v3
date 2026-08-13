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
        'button[aria-label*="Submit" i]',
        '.send-button-container button',
        'button:has(mat-icon[fonticon*="send"])',
        'button[data-test-id="send-button"]',
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

MIN_ART_PX = 300          # cạnh ngắn nhất của một tranh thật (chấp nhận từ 300px trở lên)
MIN_BOX_PX = 80           # cạnh nhỏ nhất của thẻ <img> trên trang

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


def calc_dhash(img_bytes_or_path, hash_size: int = 16) -> int:
    """Tính 256-bit dHash cho ảnh để so sánh độ tương đồng thị giác (bỏ qua nén/đổi định dạng)."""
    try:
        from PIL import Image
        from io import BytesIO
        if isinstance(img_bytes_or_path, (str, Path)):
            im = Image.open(img_bytes_or_path)
        else:
            im = Image.open(BytesIO(img_bytes_or_path))
        im = im.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
        raw = im.tobytes()
        diff = []
        for row in range(hash_size):
            row_start = row * (hash_size + 1)
            for col in range(hash_size):
                p1 = raw[row_start + col]
                p2 = raw[row_start + col + 1]
                diff.append(p1 > p2)
        val = 0
        for bit in diff:
            val = (val << 1) | bit
        return val
    except Exception:
        return 0


def is_similar_dhash(hash1: int, hash2: int, hash_size: int = 16, threshold: float = 0.22) -> bool:
    """Kiểm tra 2 ảnh có giống hệt nhau về mặt thị giác hay không (chênh lệch bit < 22%)."""
    if not hash1 or not hash2:
        return False
    xor_val = hash1 ^ hash2
    diff_bits = bin(xor_val).count("1")
    total_bits = hash_size * hash_size
    return (diff_bits / total_bits) < threshold


def is_real_art(path: Path, min_px: int = MIN_ART_PX, ignore_files: list[Path] | None = None) -> bool:
    """Mở file kiểm tra đây có phải tranh thật không, và không trùng thị giác với ảnh đính kèm."""
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
    
    if ignore_files:
        path_dh = calc_dhash(path)
        if path_dh:
            for ig in ignore_files:
                if ig and Path(ig).exists():
                    ig_dh = calc_dhash(Path(ig))
                    if ig_dh and is_similar_dhash(path_dh, ig_dh):
                        log.warning("Bỏ %s: Trùng thị giác (>80%%) với ảnh đính kèm %s!", path.name, Path(ig).name)
                        return False
    return True


class ImageCatcher:
    """Bắt ảnh ngay ở tầng mạng thay vì đoán qua DOM (dùng lại cơ chế của lệnh gen hàng loạt)."""

    def __init__(self, min_bytes: int = 10_000):
        self.min_bytes = min_bytes
        self.images: list[bytes] = []
        self.ignore_dhashes: list[int] = []
        self.armed = False

    def arm(self, ignore_files: list[Path] | None = None) -> None:
        """Bật bắt ảnh. Tự động tính dHash cho các file ảnh đính kèm để loại trừ ảnh trùng thị giác."""
        self.images.clear()
        self.ignore_dhashes = []
        if ignore_files:
            for f in ignore_files:
                if f and Path(f).exists():
                    dh = calc_dhash(Path(f))
                    if dh:
                        self.ignore_dhashes.append(dh)
        self.armed = True

    def clear(self) -> None:
        self.images.clear()

    def disarm(self) -> None:
        self.armed = False
        self.images.clear()

    def on_response(self, resp) -> None:
        if not self.armed:
            return
        try:
            ct = (resp.headers or {}).get("content-type", "").lower()
            if not ct.startswith("image/") or "svg" in ct or "gif" in ct:
                return
            if is_ui_asset(resp.url):
                return
            body = resp.body()
            if len(body) >= self.min_bytes:
                if self.ignore_dhashes:
                    body_dh = calc_dhash(body)
                    if body_dh:
                        for target_dh in self.ignore_dhashes:
                            if is_similar_dhash(body_dh, target_dh):
                                log.info("Bỏ qua ảnh đính kèm (trùng 80%%+ thị giác với ảnh prompt gốc): %d KB", len(body) // 1024)
                                return
                self.images.append(body)
                log.debug("Bắt được ảnh từ mạng: %d KB (%s)", len(body) // 1024, ct)
        except Exception:
            pass

    def best(self) -> bytes | None:
        """Ảnh lớn nhất đạt chuẩn kích thước, hoặc None."""
        from io import BytesIO
        from PIL import Image

        best_bytes, best_area = None, 0
        for b in self.images:
            try:
                with Image.open(BytesIO(b)) as im:
                    w, h = im.size
            except Exception:
                continue
            if min(w, h) < MIN_ART_PX:
                continue
            if w * h > best_area:
                best_bytes, best_area = b, w * h
        return best_bytes


def copy_file_safe(src: Path, dst: Path) -> None:
    """Sao chép file an toàn ngay cả khi file đang được mở bởi tiến trình Chrome khác."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dst.write_bytes(data)
    except Exception:
        try:
            import shutil
            shutil.copy2(src, dst)
        except Exception:
            pass


def clone_profile_cookies(source_dir: Path, target_dir: Path) -> None:
    """Sao chép cookie & session đăng nhập từ source_dir sang target_dir để không bị đòi đăng nhập lại."""
    if source_dir == target_dir or not source_dir.exists():
        return
    import shutil
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        src_default = source_dir / "Default"
        dst_default = target_dir / "Default"
        if src_default.exists():
            dst_default.mkdir(parents=True, exist_ok=True)
            for item_name in ["Cookies", "Network", "Local Storage", "Preferences", "Secure Preferences", "Session Storage"]:
                s = src_default / item_name
                d = dst_default / item_name
                if s.exists():
                    try:
                        if s.is_dir():
                            shutil.copytree(s, d, dirs_exist_ok=True)
                        else:
                            copy_file_safe(s, d)
                    except Exception:
                        pass
    except Exception as e:
        log.warning("Không copy được session đăng nhập: %s", e)


def is_profile_locked(profile_dir: Path) -> bool:
    """Kiểm tra xem profile Chrome có đang bị chiếm giữ bởi tiến trình Chrome khác không."""
    lock_file = profile_dir / "SingletonLock"
    if not lock_file.exists():
        return False
    try:
        lock_file.unlink()
        return False
    except Exception:
        return True


def get_unique_profile_dir(base_dir: Path) -> Path:
    """Trả về thư mục profile khả dụng không bị khóa bởi tiến trình Chrome khác, tự động giữ đăng nhập."""
    base_dir.mkdir(parents=True, exist_ok=True)
    if not is_profile_locked(base_dir):
        return base_dir
        
    for idx in range(1, 10):
        alt = base_dir.parent / f"{base_dir.name}-p{idx}"
        alt.mkdir(parents=True, exist_ok=True)
        if not is_profile_locked(alt):
            clone_profile_cookies(base_dir, alt)
            return alt
            
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix=f"{base_dir.name}-run-"))
    clone_profile_cookies(base_dir, tmp)
    return tmp


def is_user_signed_in(page: Page) -> bool:
    """Kiểm tra thực tế xem trình duyệt đã đăng nhập tài khoản Google trên Gemini chưa."""
    try:
        signin_btn = page.locator(
            'a[href*="ServiceLogin"], '
            'a:has-text("Đăng nhập"), a:has-text("Sign in"), '
            'button:has-text("Đăng nhập"), button:has-text("Sign in")'
        )
        cnt = signin_btn.count()
        if cnt > 0:
            for i in range(cnt):
                try:
                    txt = signin_btn.nth(i).inner_text().strip().lower()
                    if signin_btn.nth(i).is_visible() and ("đăng nhập" in txt or "sign in" in txt):
                        return False
                except Exception:
                    pass
    except Exception:
        pass
        
    return True


class GeminiDriver:
    """Điều khiển một trình duyệt duy nhất thông qua Playwright."""

    def __init__(self, cfg: dict):
        b = cfg.get("browser", {})
        self.url = b.get("gemini_url", "https://gemini.google.com/app")
        profiles = b.get("profiles", [])
        if not profiles and "user_data_dir" in b:
            profiles = [b["user_data_dir"]]
        if not profiles:
            profiles = ["./.chrome-data"]
            
        self.user_data_dir = Path(profiles[0]).resolve()
        self.headless = b.get("headless", False)
        self.timeout = b.get("generation_timeout", 240)
        self.delay_range = b.get("delay_between_prompts", [8, 20])
        self.max_retries = b.get("max_retries", 3)
        self._pw = None
        self._ctx = None
        self.page: Page | None = None

    # ---------- lifecycle ----------

    def __enter__(self) -> "GeminiDriver":
        actual_dir = get_unique_profile_dir(self.user_data_dir)
        self._pw = sync_playwright().start()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-default-browser-check",
            "--no-first-run",
        ]
        
        for retry in range(10):
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    user_data_dir=str(actual_dir),
                    headless=self.headless,
                    channel="chrome",
                    viewport={"width": 1440, "height": 960},
                    args=launch_args,
                )
                break
            except Exception as e:
                if ("ProcessSingleton" in str(e) or "already in use" in str(e)) and retry < 9:
                    actual_dir = self.user_data_dir.parent / f"{self.user_data_dir.name}-run{retry+1}"
                    actual_dir.mkdir(parents=True, exist_ok=True)
                    for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                        f = actual_dir / lock_name
                        if f.exists():
                            try:
                                f.unlink()
                            except Exception:
                                pass
                    clone_profile_cookies(self.user_data_dir, actual_dir)
                else:
                    raise
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # Bắt ảnh trực tiếp ở tầng mạng (network response sniffing)
        self.catcher = ImageCatcher()
        self.page.on("response", self.catcher.on_response)
        
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
        """Kiểm tra đăng nhập thực tế. Nếu chưa đăng nhập -> yêu cầu user đăng nhập."""
        try:
            self._find(SELECTORS["prompt_box"], timeout=20_000)
        except Exception:
            pass

        # Chờ 2s để Google nhận dạng Cookie đăng nhập từ profile
        time.sleep(2.0)

        if is_user_signed_in(self.page):
            log.info("✓ Đã xác nhận đăng nhập Google Gemini thành công.")
            return

        log.warning("⚠️ Chưa đăng nhập Google trên Chrome (Phát hiện thấy nút 'Đăng nhập')!")
        if self.headless:
            raise RuntimeError(
                "Chưa đăng nhập tài khoản Google trên Chrome! "
                "Hãy mở file config.yaml, tạm thời đặt 'browser.headless: false', sau đó khởi chạy lại để Chrome hiện lên và đăng nhập Google 1 lần."
            )

        print("\n" + "=" * 65)
        print("  Cửa sổ Chrome đã mở. Hãy ĐĂNG NHẬP tài khoản Google của bạn.")
        print("  Sau khi đăng nhập xong trên Chrome, hãy quay lại đây nhấn Enter...")
        print("=" * 65 + "\n")
        input()
        time.sleep(2.0)
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
        
        # Dán trực tiếp toàn bộ prompt siêu tốc thay vì gõ từng phím
        clean_text = flatten(text)
        try:
            box.evaluate("(el, txt) => { el.innerText = txt; el.dispatchEvent(new Event('input', {bubbles: true, composed: true})); }", clean_text)
        except Exception:
            try:
                box.fill(clean_text)
            except Exception:
                self.page.keyboard.type(clean_text, delay=1)
                
        self._wake_editor(box)
        self.page.wait_for_timeout(400)
        if not self._click_send(box):
            raise RuntimeError("Không bấm được nút Send.")
        log.info("Đã dán siêu tốc & gửi prompt (%d ký tự).", len(text))

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
                if started():
                    return True
            except Exception as e:  # noqa: BLE001
                log.debug("Bấm nút gửi lỗi: %s", e)

        try:                                          # dự phòng 1: Control+Enter
            box.focus()
            self.page.keyboard.press("Control+Enter")
            if started():
                return True
        except Exception:
            pass

        try:                                          # dự phòng 2: phím Enter
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
            try:
                for el in self.page.locator(sel).all():
                    try:
                        is_user = el.evaluate("""
                            node => {
                                let p = node.closest('user-query, rich-textarea, .query-text, .user-prompt, [class*="attachment"], [class*="uploader"]');
                                return p !== null;
                            }
                        """)
                        if is_user:
                            continue
                        src = el.get_attribute("src") or ""
                    except Exception:
                        continue
                    if not src or is_ui_asset(src):
                        continue
                    area = 100.0
                    try:
                        box = el.bounding_box()
                        if box:
                            if min(box["width"], box["height"]) < MIN_BOX_PX:
                                continue
                            area = box["width"] * box["height"]
                    except Exception:
                        pass
                    best[src] = max(best.get(src, 0.0), area)
            except Exception:
                continue
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
                if len(body) < 10_000:
                    log.debug("Bỏ %s: chỉ %d byte.", candidate, len(body))
                    continue
                dest.write_bytes(body)
                return True
            except Exception as e:  # noqa: BLE001
                log.debug("Tải %s lỗi: %s", candidate, e)
        return False

    def attach_images(self, file_paths: list[Path]) -> bool:
        """Đính kèm danh sách ảnh (bìa, trang ruột) vào ô chat Gemini."""
        valid_paths = [Path(p).resolve() for p in file_paths if p and Path(p).exists()]
        if not valid_paths:
            return False
        
        wait_ms = max(4_000, len(valid_paths) * 2_500)

        # 1. ƯU TIÊN HÀNG ĐẦU: Dán (Paste) ảnh trực tiếp qua Clipboard / DataTransfer (không bật popup, cực nhanh)
        try:
            file_payloads = []
            for p in valid_paths:
                data_b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
                mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                file_payloads.append({
                    "base64": data_b64,
                    "mime": mime,
                    "name": p.name
                })
            
            js_paste = """
            (payloads) => {
                const editor = document.querySelector('rich-textarea div[contenteditable="true"]') 
                            || document.querySelector('div[contenteditable="true"]') 
                            || document.querySelector('textarea') 
                            || document.activeElement;
                if (!editor) return false;
                
                editor.focus();
                const dt = new DataTransfer();
                for (const item of payloads) {
                    const bstr = atob(item.base64);
                    const u8arr = new Uint8Array(bstr.length);
                    for (let i = 0; i < bstr.length; i++) {
                        u8arr[i] = bstr.charCodeAt(i);
                    }
                    const file = new File([u8arr], item.name, { type: item.mime });
                    dt.items.add(file);
                }
                
                const pasteEvent = new ClipboardEvent('paste', {
                    bubbles: true,
                    cancelable: true,
                    clipboardData: dt
                });
                editor.dispatchEvent(pasteEvent);
                return true;
            }
            """
            pasted = self.page.evaluate(js_paste, file_payloads)
            if pasted:
                self.page.wait_for_timeout(wait_ms)
                log.info("Đã dán (Paste) %d ảnh thực tế trực tiếp qua Clipboard (chờ %dms).", len(valid_paths), wait_ms)
                return True
        except Exception as e_paste:
            log.debug("Dán ảnh Clipboard lỗi: %s, chuyển sang set_input_files", e_paste)

        # 2. Thử set_input_files trực tiếp lên input[type="file"]
        str_paths = [str(p) for p in valid_paths]
        try:
            inp = self.page.locator('input[type="file"]').first
            if inp.count() > 0:
                inp.set_input_files(str_paths, timeout=4_000)
                self.page.wait_for_timeout(wait_ms)
                log.info("Đã đính kèm %d ảnh thực tế qua input[type=file] (chờ %dms).", len(valid_paths), wait_ms)
                return True
        except Exception as e1:
            log.debug("Trực tiếp set_input_files thất bại: %s, chuyển sang nút Upload", e1)

        # 3. Dự phòng: Mở menu Upload và chọn Tải tệp lên (Lọc bỏ Google Workspace & Google Drive để không bị dính popup)
        try:
            plus_btn = self.page.locator(
                'button[aria-label*="tải" i], button[aria-label*="upload" i], '
                'button[aria-label*="thêm" i], button[aria-label*="đính kèm" i], '
                'button[aria-label*="add" i], button.uploader-button'
            ).first
            
            if plus_btn.is_visible():
                plus_btn.click()
                self.page.wait_for_timeout(500)
                
                upload_item = self.page.locator(
                    'div[role="menuitem"]:has-text("Tải tệp"), div[role="menuitem"]:has-text("Upload"), '
                    'div[role="menuitem"]:has-text("tải lên"), button:has-text("Tải tệp"), button:has-text("Upload")'
                ).filter(has_not_text="Workspace").filter(has_not_text="Drive").first
                
                if upload_item.is_visible():
                    with self.page.expect_file_chooser(timeout=4_000) as fc_info:
                        upload_item.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(str_paths)
                    self.page.wait_for_timeout(wait_ms)
                    log.info("Đã đính kèm %d ảnh qua menu Upload (chờ %dms).", len(valid_paths), wait_ms)
                    return True
        except Exception as e2:
            log.debug("Upload qua menu thất bại: %s", e2)

        log.warning("Không tìm thấy nút hoặc ô đính kèm ảnh.")
        return False

    # ---------- API chính ----------

    def generate_image(self, prompt: str, dest: Path, attach_files: list[Path] | None = None) -> Path | None:
        """Gửi 1 prompt + đính kèm ảnh thực tế (nếu có), chờ, tải ảnh về dest."""
        for attempt in range(1, self.max_retries + 1):
            from bookgen.cancel import check_cancel
            check_cancel()
            try:
                if hasattr(self, 'catcher') and self.catcher:
                    eff_ignore = None if "preview" in dest.stem else attach_files
                    self.catcher.arm(ignore_files=eff_ignore)
                if attach_files:
                    self.attach_images(attach_files)
                    self.page.wait_for_timeout(1_000)

                self.send_prompt(prompt)

                # QUAN TRỌNG: Ngay sau khi gửi prompt, xóa sạch toàn bộ ảnh upload/paste lỡ bị catcher bắt trước đó!
                if hasattr(self, 'catcher') and self.catcher:
                    self.catcher.clear()

                before = set(self._collect_image_urls())
                self.wait_for_generation()

                # 1. Cơ chế BẮT ẢNH TỪ TẦNG MẠNG (dùng lại của gen hàng loạt):
                if hasattr(self, 'catcher') and self.catcher:
                    deadline_net = time.time() + 8.0
                    while time.time() < deadline_net:
                        best_b = self.catcher.best()
                        if best_b:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(best_b)
                            if is_real_art(dest):
                                log.info("OK (bắt trực tiếp từ mạng) -> %s", dest.name)
                                return dest
                        time.sleep(0.5)

                # 2. Dự phòng: Quét qua DOM nếu chưa bắt được từ mạng
                new_urls: list[str] = []
                deadline = time.time() + self.timeout
                while time.time() < deadline:
                    candidates = self._collect_image_urls()
                    new_urls = [u for u in candidates if u not in before]
                    if not new_urls and candidates:
                        new_urls = candidates
                    if new_urls:
                        break
                    self.page.wait_for_timeout(1_500)
                if not new_urls:
                    raise RuntimeError(f"Không thấy ảnh sau {self.timeout}s.")

                for url in new_urls[:4]:
                    if self._download(url, dest) and is_real_art(dest):
                        log.info("OK (tải từ DOM) -> %s", dest.name)
                        return dest
                    if dest.exists():
                        dest.unlink()
                raise RuntimeError("Không lấy được ảnh thật.")

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
                if hasattr(self, 'catcher') and self.catcher:
                    self.catcher.disarm()
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
