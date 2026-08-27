"""
Chạy nhiều phiên Gemini song song trong MỘT cửa sổ Chrome (nhiều tab).

Vì sao dùng async chứ không dùng thread:
    Playwright bản sync cấm dùng chung một context giữa nhiều thread, và
    launch_persistent_context không mở được 2 lần trên cùng user_data_dir.
    Vì vậy: 1 context - N tab - N task asyncio.

Cơ chế tự giảm tốc (Throttle):
    Quota Gemini tính theo TÀI KHOẢN chứ không theo tab, nên bắn 5 tab cùng lúc
    rất dễ ăn lỗi "I encountered an error". Mỗi lần fail, pool tự hạ số tab đang
    chạy xuống; chạy trơn được vài ảnh thì nâng dần trở lại. Tab bị "treo" chỉ
    ngủ chờ chứ không đóng, nên không mất phiên đăng nhập.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import (
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

from bookgen.gemini_driver import (
    MIN_ART_PX,
    MIN_BOX_PX,
    SELECTORS,
    flatten,
    is_real_art,
    is_ui_asset,
    upscale_url,
)

log = logging.getLogger(__name__)

# Trần thời gian cho bước nâng ảnh lên bản gốc. Vượt mốc treo của lane (120s)
# thì cả ảnh bị bỏ và gen lại -> thà giữ bản xem trước còn hơn.
# 60s = đủ cho hai lần bấm nút download (22s mỗi lần + thao tác hover/click),
# vẫn cách xa mốc 120s.
FULLRES_BUDGET_SEC = 60


# --------------------------------------------------------------- throttle

class Throttle:
    """Giới hạn số tab được chạy đồng thời, tự co giãn theo tỉ lệ lỗi."""

    def __init__(self, max_workers: int, min_workers: int = 1):
        self.max = max_workers
        self.min = max(1, min_workers)
        self.limit = max_workers
        self.active = 0
        self._fails = 0
        self._oks = 0
        self._cond = asyncio.Condition()
        self.cooldown_until = 0.0

    async def acquire(self) -> None:
        async with self._cond:
            while self.active >= self.limit:
                await self._cond.wait()
            self.active += 1

    async def release(self) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def on_fail(self) -> None:
        """Hai lần fail liên tiếp -> bớt 1 tab.

        KHÔNG bắt cả pool ngồi chờ nữa. Bản cũ đặt cooldown chung, nên một tab
        lỗi là mọi tab khoẻ cũng phải đứng im 20-45 giây - đúng thứ làm cả pool
        ì ạch. Giờ tab nào lỗi thì tab đó tự nghỉ, tab khác chạy tiếp.
        """
        async with self._cond:
            self._oks = 0
            self._fails += 1
            if self._fails >= 2 and self.limit > self.min:
                self.limit -= 1
                self._fails = 0
                log.warning("Nhiều lỗi liên tiếp -> giảm còn %d phiên song song.",
                            self.limit)
            self._cond.notify_all()

    async def on_ok(self) -> None:
        """Ba ảnh trơn tru -> thử nâng lại 1 tab."""
        async with self._cond:
            self._fails = 0
            self._oks += 1
            if self._oks >= 3 and self.limit < self.max:
                self.limit += 1
                self._oks = 0
                log.info("Ổn định -> tăng lên %d phiên song song.", self.limit)
                self._cond.notify_all()


# --------------------------------------------------------------- pool

_UA_CACHE: str | None = None


async def real_user_agent(pw) -> str | None:
    """User-Agent của Chrome ở chế độ hiện, dùng để vá cho chế độ ẩn.

    Chrome chạy headless tự khai báo "HeadlessChrome/151.0.0.0" thay vì
    "Chrome/151.0.0.0". Đo trên máy này thì ĐÓ LÀ KHÁC BIỆT DUY NHẤT giữa hai chế
    độ - GPU (ANGLE/D3D11), tốc độ dựng DOM, fps, media query pointer/hover và cả
    Client Hints brands đều y hệt nhau. Nhưng Gemini đọc chuỗi đó và trả về trải
    nghiệm khác: DOM dựng chậm/khác đi, hay chèn "I encountered an error", ô nhập
    mount muộn nên _find hết giờ.

    Lấy chuỗi thật từ chính binary Chrome đang dùng rồi bỏ chữ "Headless", thay vì
    ghi cứng - ghi cứng là mỗi lần Chrome lên phiên bản mới lại sai.
    """
    global _UA_CACHE
    if _UA_CACHE:
        return _UA_CACHE
    try:
        browser = await pw.chromium.launch(headless=True, channel="chrome")
        try:
            page = await browser.new_page()
            ua = await page.evaluate("() => navigator.userAgent")
        finally:
            await browser.close()
        _UA_CACHE = ua.replace("HeadlessChrome", "Chrome")
        return _UA_CACHE
    except Exception as e:  # noqa: BLE001
        log.warning("Không lấy được User-Agent thật (%s) - chạy ẩn với UA mặc "
                    "định, Gemini có thể trả lời khác đi.", e)
        return None


class QuotaExhausted(RuntimeError):
    """Tài khoản đã hết hạn mức tạo ảnh trong ngày.

    Gặp cái này thì retry, nhắc lại hay mở tab mới đều vô nghĩa - hạn mức tính
    theo tài khoản. Dừng cả pool, giữ nguyên state để mai chạy tiếp.
    """


class NoImageInReply(RuntimeError):
    """Model đã trả lời xong nhưng chỉ có chữ, không có ảnh.

    Hay gặp với bìa sau: prompt mở đầu bằng "Now create the matching BACK
    COVER..." nghe như đang trò chuyện, nên model đáp lại bằng lời kiểu
    "I have designed the matching back cover..." mà chẳng vẽ gì.
    """


class ImageCatcher:
    """Bắt ảnh ngay ở tầng mạng thay vì đoán qua DOM.

    Khi Gemini vẽ xong, trình duyệt BẮT BUỘC phải tải ảnh về qua một response
    có content-type image/*. Đó là bằng chứng chắc chắn model đã sinh ảnh, và
    ta lấy được luôn bytes gốc - khỏi phải rê chuột, bấm nút tải, hay đoán URL
    '=s0'. Không có response ảnh nào tức là Gemini không vẽ, dừng sớm được.
    """

    def __init__(self, min_bytes: int = 20_000):
        self.min_bytes = min_bytes
        self.images: list[bytes] = []
        self.ignore_dhashes: list[int] = []
        self.armed = False

    def arm(self, ignore_files: list[Path] | None = None) -> None:
        """Bật bắt, xoá kết quả cũ. Tự động tính dHash các file đính kèm để loại trừ ảnh trùng thị giác."""
        from bookgen.gemini_driver import calc_dhash
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

    async def on_response(self, resp) -> None:
        if not self.armed:
            return
        try:
            ct = (resp.headers or {}).get("content-type", "").lower()
            if not ct.startswith("image/") or "svg" in ct or "gif" in ct:
                return
            if is_ui_asset(resp.url):
                return
            body = await resp.body()
            if len(body) >= self.min_bytes:
                if self.ignore_dhashes:
                    from bookgen.gemini_driver import calc_dhash, is_similar_dhash
                    body_dh = calc_dhash(body)
                    if body_dh:
                        for target_dh in self.ignore_dhashes:
                            if is_similar_dhash(body_dh, target_dh):
                                log.info("Bỏ qua ảnh đính kèm (trùng 80%%+ thị giác với ảnh prompt gốc): %d KB", len(body) // 1024)
                                return
                self.images.append(body)
                log.debug("Bắt được ảnh từ mạng: %d KB (%s)",
                          len(body) // 1024, ct)
        except Exception:      # response bị huỷ, body đã mất... bỏ qua
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


async def is_user_signed_in_async(page: Page) -> bool:
    """Kiểm tra thực tế xem trình duyệt đã đăng nhập tài khoản Google trên Gemini chưa (bản async)."""
    try:
        signin_btn = page.locator(
            'a[href*="ServiceLogin"], '
            'a:has-text("Đăng nhập"), a:has-text("Sign in"), '
            'button:has-text("Đăng nhập"), button:has-text("Sign in")'
        )
        cnt = await signin_btn.count()
        if cnt > 0:
            for i in range(cnt):
                try:
                    txt = (await signin_btn.nth(i).inner_text()).strip().lower()
                    if (await signin_btn.nth(i).is_visible()) and ("đăng nhập" in txt or "sign in" in txt):
                        return False
                except Exception:
                    pass
    except Exception:
        pass
        
    return True


class GeminiPool:
    """N tab Gemini cùng rút việc từ một hàng đợi."""

    def __init__(self, cfg: dict):
        b = cfg["browser"]
        self.url = b["gemini_url"]
        
        profiles = b.get("profiles", [])
        if not profiles and "user_data_dir" in b:
            profiles = [b["user_data_dir"]]
        if not profiles:
            profiles = ["./.chrome-profile"]
            
        self.profile_dirs = [Path(p).resolve() for p in profiles]
        self.headless = b.get("headless", False)
        self.timeout = b.get("generation_timeout", 240)
        self.delay_range = b.get("delay_between_prompts", [8, 20])
        self.typing_delay = int(b.get("typing_delay_ms", 1))
        self.max_retries = b.get("max_retries", 3)
        self.workers_per_profile = max(1, int(b.get("concurrency_per_profile", b.get("concurrency", 2))))
        self.workers = self.workers_per_profile * len(self.profile_dirs)

        # Extension MonkeyX.
        #
        # ĐÃ THỬ VÀ THẤT BẠI: nạp bằng --load-extension. Chrome 137+ vô hiệu hoá
        # công tắc dòng lệnh này; đo trên Chrome 151 thì extension không hề vào
        # extensions.settings, kể cả khi thêm --enable-unsafe-extension-debugging
        # hay --disable-features=DisableLoadExtensionCommandLineSwitch.
        #
        # Vì vậy MonkeyX phải được cài TAY một lần vào từng profile bằng "Load
        # unpacked" (chạy setup_extension.bat). Chrome không copy file vào profile
        # mà chỉ ghi ĐƯỜNG DẪN tuyệt đối vào Preferences -> đừng đổi chỗ thư mục
        # extension, đổi là ID đổi và công tắc "Allow user scripts" reset.
        #
        # Ở đây chỉ còn một việc: KHÔNG được truyền --disable-extensions, vì cờ đó
        # tắt luôn cả extension đã cài tay.
        ext = b.get("extension_dir") or ""
        self.extension_dir = Path(ext).resolve() if ext else None
        if self.extension_dir and not (self.extension_dir / "manifest.json").exists():
            log.warning("browser.extension_dir không có manifest.json: %s -> bỏ qua.",
                        self.extension_dir)
            self.extension_dir = None
        
        self.recycle_every = max(0, int(b.get("recycle_tab_every", 5)))
        # Đồng hồ canh PHẢI rộng hơn thời gian vẽ một ảnh, nếu không nó chém ngang
        # ảnh đang vẽ hoàn toàn bình thường ("đang gen tự nhiên tắt"). Chốt ở đây
        # chứ không ở UI, vì còn đường sửa tay config.yaml và đường chạy CLI.
        self.stall_timeout = float(b.get("stall_timeout", 360))
        floor = self.timeout + 60
        if self.stall_timeout < floor:
            log.warning("stall_timeout=%.0fs nhỏ hơn generation_timeout=%.0fs "
                        "-> nâng lên %.0fs để không chém ngang ảnh đang vẽ.",
                        self.stall_timeout, self.timeout, floor)
            self.stall_timeout = floor
        self.max_requeue = int(b.get("max_requeue", 2))
        self.max_nudges = int(b.get("max_nudges", 2))
        # Ảnh bắt ở tầng mạng là bản xem trước ~800-1024px. Nếu cạnh dài nhỏ hơn
        # ngưỡng này, thử mở ảnh/bấm Tải xuống để lấy BẢN GỐC (2K+, 3-6MB) rồi
        # giữ bản lớn hơn. Đặt 0 để tắt (chỉ dùng bản xem trước cho nhanh).
        self.raw_min_long_edge = int(b.get("raw_min_long_edge", 1500))
        self.nudge_prompt = (cfg.get("prompts", {}).get("nudge")
                             or "Generate the image now. Output only the image, "
                                "with no explanation and no text in your reply.")
        self._pw = None
        self.contexts = []
        self.pages: list[Page] = []
        self.catchers: dict[Page, ImageCatcher] = {}
        self.throttle = Throttle(self.workers)
        self.exhausted_contexts = set()
        self.quota_hit = False

    # ---------- lifecycle ----------

    async def __aenter__(self) -> "GeminiPool":
        self._pw = await async_playwright().start()
        from bookgen.gemini_driver import get_unique_profile_dir
        
        from bookgen.gemini_driver import is_profile_locked

        # Chỉ cần khi chạy ẩn; chế độ hiện vốn đã có UA đúng.
        ua = await real_user_agent(self._pw) if self.headless else None
        if ua:
            log.info("Chạy ẩn với User-Agent của Chrome thường: %s", ua)

        for p_dir in self.profile_dirs:
            locked = is_profile_locked(p_dir)
            actual_dir = get_unique_profile_dir(p_dir)
            if locked:
                # Hay gặp nhất: cửa sổ Chrome do nút "Mở Chrome" trên dashboard
                # bật lên (POST /api/gemini/launch-chrome) vẫn còn mở.
                log.warning("Profile %s đang bị một Chrome khác chiếm giữ "
                            "-> dùng bản sao %s. Đóng cửa sổ Chrome đó để chạy "
                            "thẳng trên profile gốc.", p_dir.name, actual_dir.name)
            else:
                log.info("Profile dùng cho phiên này: %s", actual_dir.name)
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                "--disable-dev-shm-usage",
            ]
            # BO "--js-flags=--max-old-space-size=512": tran heap V8 512MB sinh ra
            # de chan da leo RAM, nhung recycle_tab_every=1 xu ly viec do triet de
            # hon (dong han tab -> tra bo nho ve OS). Giu ca hai thi tran thap lai
            # thanh thu de lam trang treo giua luc render anh.
            # Playwright TU NHET "--disable-extensions" vao moi lan khoi dong
            # (danh sach mac dinh trong driver, coreBundle.js). Xoa co do khoi
            # launch_args la vo nghia - phai bao Playwright bo qua chinh no.
            # Do thuc te: khong co dong nay thi chrome://extensions RONG TRON,
            # MonkeyX khong nap, userscript khong bao gio chay.
            ignore_default = ["--disable-extensions"] if self.extension_dir else []

            ctx = None
            for retry in range(10):
                try:
                    ctx = await self._pw.chromium.launch_persistent_context(
                        user_data_dir=str(actual_dir),
                        headless=self.headless,
                        channel="chrome",
                        viewport={"width": 1440, "height": 960},
                        args=launch_args,
                        ignore_default_args=ignore_default,
                        user_agent=ua,
                    )
                    break
                except Exception as e:
                    # Lưới an toàn cho trường hợp is_profile_locked() nhìn sót:
                    # Chrome bị mở đè lên profile đang chạy sẽ bàn giao cho
                    # instance cũ rồi thoát exitCode=21, Playwright báo lại thành
                    # TargetClosedError - chuỗi này không chứa "ProcessSingleton"
                    # nên bản trước không retry mà chết luôn.
                    msg = str(e)
                    retryable = ("ProcessSingleton" in msg
                                 or "already in use" in msg
                                 or "exitCode=21" in msg
                                 or "Target page, context or browser has been closed" in msg)
                    if retryable and retry < 9:
                        actual_dir = p_dir.parent / f"{p_dir.name}-run{retry+1}"
                        actual_dir.mkdir(parents=True, exist_ok=True)
                        for lock_name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
                            f = actual_dir / lock_name
                            if f.exists():
                                try:
                                    f.unlink()
                                except Exception:
                                    pass
                        from bookgen.gemini_driver import clone_profile_cookies
                        clone_profile_cookies(p_dir, actual_dir)
                    else:
                        raise

            self.contexts.append(ctx)
            first = ctx.pages[0] if ctx.pages else await ctx.new_page()
            self.pages.append(first)
            for _ in range(self.workers_per_profile - 1):
                self.pages.append(await ctx.new_page())

        for p in self.pages:
            await self._prepare(p)

        for ctx in self.contexts:
            if ctx.pages:
                await self._ensure_logged_in(ctx.pages[0])
                
        log.info("Đã mở %d phiên Gemini từ %d tài khoản.", len(self.pages), len(self.contexts))
        return self

    async def __aexit__(self, *exc):
        try:
            for ctx in self.contexts:
                await ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _ensure_logged_in(self, page: Page) -> None:
        try:
            await self._find(page, SELECTORS["prompt_box"], timeout=20_000)
        except Exception:
            pass

        # Chờ 2s để Google nhận dạng Cookie đăng nhập từ profile
        await asyncio.sleep(2.0)

        if await is_user_signed_in_async(page):
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
        await asyncio.get_running_loop().run_in_executor(None, input)
        await asyncio.sleep(2.0)

    # ---------- helper ----------

    async def _find(self, page: Page, selectors: list[str], timeout: int = 10_000):
        """Chờ MỘT lần trên tất cả selector gộp lại.

        Bản cũ chờ lần lượt từng selector và chia nhỏ timeout, nên selector nào
        không khớp cũng ngốn trọn phần thời gian của nó. Gộp bằng dấu phẩy thì
        Playwright kiểm tra song song, khớp cái nào xong ngay cái đó.

        ĐỪNG quay lại dùng .first: lúc Gemini vừa mount SPA (hay gặp khi
        recycle_tab_every=1 nên mọi việc đều khởi đầu trên tab mới) có NHIỀU phần
        tử khớp cùng lúc - editor cũ chưa gỡ, editor mới chưa hiện. .first vớ
        phải cái không bao giờ visible rồi ngồi chờ hết timeout, trong khi ô nhập
        thật nằm ở match khác. Dấu hiệu nhận biết trong log: "locator resolved to
        visible <div ...>" mà vẫn Timeout 30000ms exceeded.

        Cũng ĐỪNG tự quét bằng loc.all() rồi trả về phần tử tìm được: all() trả
        về locator dạng nth(i), tức buộc theo VỊ TRÍ chứ không theo phần tử. Kiểm
        tra visible lúc tìm là đúng, nhưng tới lúc click thì Gemini đã tráo vai -
        nth(0) giờ trỏ vào editor cũ đang ẩn và treo trọn 30s. Dấu hiệu trong log:
        "Locator.click: Timeout 30000ms exceeded ... waiting for locator(...).first"
        (Playwright in nth(0) ra thành .first).

        Cách đúng: để bộ lọc 'visible=true' nằm TRONG selector, nhờ vậy nó được
        giải lại ở mỗi thao tác và luôn trúng phần tử đang hiển thị.
        """
        loc = page.locator(f'{", ".join(selectors)} >> visible=true').first
        await loc.wait_for(state="visible", timeout=timeout)
        return loc

    async def _prepare(self, page: Page) -> Page:
        """Cài init script, gắn bộ bắt ảnh, mở sẵn trang Gemini."""
        catcher = ImageCatcher()
        self.catchers[page] = catcher
        page.on("response",
                lambda r: asyncio.create_task(catcher.on_response(r)))
        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        await page.goto(self.url, wait_until="domcontentloaded", timeout=90_000)
        # domcontentloaded chỉ nói HTML đã tải xong; Gemini là SPA Angular nên ô
        # nhập mount muộn hơn nhiều. Chờ mù 1.5s là đủ khi tab được dùng lại nhiều
        # lần, nhưng với recycle_tab_every=1 thì MỌI việc đều khởi đầu trên tab
        # vừa mở -> phần nạp SPA còn thiếu bị đẩy sang cho _find của việc kế tiếp
        # gánh, và nó chỉ có 30s nên báo timeout.
        #
        # Chờ ở ĐÂY còn rẻ hơn: _prepare/_recycle chạy ngoài đồng hồ canh
        # stall_timeout, nên thời gian nạp trang không ăn vào ngân sách của việc.
        try:
            await self._find(page, SELECTORS["prompt_box"], timeout=60_000)
        except Exception as e:  # noqa: BLE001
            log.warning("Tab mới chưa thấy ô nhập sau 60s (%s) - chạy tiếp, "
                        "_send_prompt sẽ thử lại.", e)

        # Chờ userscript sẵn sàng NGAY TẠI ĐÂY, không để _send_via_extension gánh.
        # MonkeyX gỡ sạch rồi mới đăng ký lại mỗi lần service worker khởi động
        # (syncAll: unregister -> register), nên có một khoảng trống lúc Chrome
        # vừa bật; tab nào nạp Gemini trúng khoảng đó thì không có script và cả
        # ảnh đó phải lùi về đường Playwright. Chờ ở _prepare không tốn ngân sách
        # của việc vì nó chạy ngoài đồng hồ canh stall_timeout.
        if self.extension_dir:
            try:
                await page.wait_for_function(
                    "() => document.documentElement.dataset.mxReady === '1'",
                    timeout=20_000)
            except PWTimeout:
                log.warning("Tab mới: userscript MonkeyX chưa sẵn sàng sau 20s "
                            "-> ảnh đầu trên tab này sẽ dùng đường Playwright.")
        await page.wait_for_timeout(400)
        return page

    async def _recycle(self, idx: int) -> Page:
        """Đóng hẳn tab rồi mở tab mới.

        Reload trang không trả bộ nhớ về hệ điều hành vì Chrome dùng lại tiến
        trình renderer cũ. Đóng tab mới thực sự giải phóng. Cookie đăng nhập nằm
        ở profile nên tab mới vẫn đăng nhập sẵn.
        """
        old = self.pages[idx - 1]
        new = await old.context.new_page()
        self.catchers.pop(old, None)
        try:
            await old.close()
        except Exception as e:  # noqa: BLE001
            log.debug("Đóng tab %d lỗi: %s", idx, e)
        await self._prepare(new)
        self.pages[idx - 1] = new
        log.info("[tab %d] đã tái tạo để giải phóng RAM.", idx)
        return new

    async def _ensure_fresh_chat(self, page: Page) -> None:
        """Đảm bảo tab đang ở một cuộc trò chuyện TRỐNG trước khi gửi prompt mới.

        Bản trước _one_job không hề mở chat mới - _new_chat chỉ được gọi trong
        ask_text. Hậu quả: mọi ảnh trên một tab đều rơi vào cùng một cuộc trò
        chuyện. Tab 1 tệ nhất vì ask_text dùng nó để hỏi danh sách chủ đề, nên
        prompt vẽ ảnh nối vào một thread đang trò chuyện -> Gemini đáp lại bằng
        LỜI thay vì vẽ ("chỉ trả lời bằng chữ, nhắc lại"), rồi hỏng cả loạt.
        recycle_tab_every=1 vô tình che lỗi này: tab mới thì chat cũng mới, nên
        chỉ những tab kẹt trong vòng thử lại mới lộ ra.

        Kiểm tra trước rồi mới bấm: tab vừa tái tạo đã trống sẵn, gọi _new_chat
        vô ích có thể rơi vào nhánh dự phòng goto và nạp lại cả SPA lần nữa.
        """
        try:
            n = await page.locator(", ".join(SELECTORS["response_container"])).count()
        except Exception:                      # không đọc được -> cứ mở mới cho chắc
            n = 1
        if n == 0:
            return
        await self._new_chat(page)

    async def _new_chat(self, page: Page) -> None:
        """Mở hội thoại mới. Ưu tiên bấm nút thay vì goto - goto nạp lại cả SPA,
        chậm hơn nhiều và cũng chẳng giải phóng bộ nhớ."""
        try:
            btn = await self._find(page, SELECTORS["new_chat"], timeout=3_000)
            await btn.click()
            await page.wait_for_timeout(600)
            await self._find(page, SELECTORS["prompt_box"], timeout=8_000)
            return
        except (PWTimeout, Exception) as e:  # noqa: BLE001
            log.info("Không bấm được nút chat mới (%s) -> nạp lại cả trang, "
                     "mất thêm khoảng 20s.", e)
        await page.goto(self.url, wait_until="domcontentloaded")
        # Cùng lý do như trong _prepare: chờ ô nhập thật, không đếm giờ mù.
        try:
            await self._find(page, SELECTORS["prompt_box"], timeout=45_000)
        except Exception as e:  # noqa: BLE001
            log.warning("Chat mới chưa thấy ô nhập sau 45s (%s).", e)
        await page.wait_for_timeout(400)

    async def _wake_editor(self, page: Page, box) -> None:
        try:
            await box.focus()
            await box.type(" .")
            await page.keyboard.press("Backspace")
            await page.keyboard.press("Backspace")
        except Exception:
            pass

    async def _wait_started(self, page: Page, box) -> bool:
        """Gửi thành công = nút Stop hiện ra HOẶC ô nhập trống đi."""
        stop = page.locator(", ".join(SELECTORS["stop_button"])).first
        for _ in range(16):                          # tối đa ~8 giây
            try:
                if await stop.is_visible():
                    return True
            except Exception:
                pass
            try:
                if len((await box.inner_text()).strip()) < 10:
                    return True
            except Exception:
                return True
            await page.wait_for_timeout(500)
        return False

    async def _send_via_extension(self, page: Page, text: str, box) -> bool:
        """Giao prompt cho userscript MonkeyX gõ và bấm gửi.

        Vì sao không để Playwright tự làm: đường cũ đặt el.innerText = txt, chỉ
        sửa DOM chứ không đi qua pipeline soạn thảo, nên Quill của Gemini không
        cập nhật model nội bộ. Userscript dùng execCommand('insertText') - đúng
        đường mà bàn phím thật đi qua.

        Trả False nếu extension vắng mặt hoặc báo lỗi TRƯỚC khi bấm; lúc đó
        _send_prompt quay về đường Playwright cũ. Userscript chỉ ghi 'error' khi
        chưa bấm, nên nhánh dự phòng không bao giờ thành cú bấm thứ hai.
        """
        try:
            # 15s chứ không phải 5s: userscript chạy ở document-idle, mà với
            # recycle_tab_every=1 thì mọi việc đều bắt đầu trên tab vừa mở - dưới
            # headless, SPA nạp chậm nên 5s hay hụt, và hụt là mất luôn đường
            # extension cho cả ảnh đó.
            await page.wait_for_function(
                "() => document.documentElement.dataset.mxReady === '1'",
                timeout=15_000)
        except PWTimeout:
            log.warning("Userscript MonkeyX không có mặt trên tab này "
                        "-> dùng đường Playwright.")
            return False

        await page.evaluate("""(txt) => {
            const r = document.documentElement;
            delete r.dataset.mxStatus;
            delete r.dataset.mxError;
            const old = document.getElementById('__mx_job');
            if (old) old.remove();
            const n = document.createElement('div');
            n.id = '__mx_job';
            n.style.display = 'none';
            n.textContent = txt;
            r.appendChild(n);
        }""", text)

        try:
            await page.wait_for_function(
                "() => ['clicked','error']"
                ".includes(document.documentElement.dataset.mxStatus || '')",
                timeout=40_000)
        except PWTimeout:
            log.warning("Userscript không phản hồi sau 40s -> dùng đường Playwright.")
            return False

        status, err = await page.evaluate(
            "() => [document.documentElement.dataset.mxStatus,"
            "       document.documentElement.dataset.mxError || '']")
        if status != "clicked":
            log.warning("Userscript báo lỗi (%s) -> dùng đường Playwright.", err)
            return False

        log.info("Userscript đã gõ & gửi prompt (%d ký tự).", len(text))

        # ĐÃ BẤM RỒI THÌ TUYỆT ĐỐI KHÔNG TRẢ False.
        #
        # Bản trước trả thẳng _wait_started(...). Khi nó không xác nhận được
        # (nút Stop chưa kịp hiện, ô nhập chưa kịp trống - hay gặp trên tab chậm
        # ở chế độ ẩn), _send_prompt hiểu là "chưa gửi" rồi chạy trọn đường
        # Playwright: evaluate + _wake_editor + _click_send với đủ vòng dự phòng
        # Control+Enter và Enter. Hậu quả kép:
        #   - GỬI LẠI prompt lần hai vào đúng chat đang sinh ảnh
        #   - mỗi bước dự phòng lại chờ started() nên _send_prompt kẹt hàng phút
        # Đo trên log: userscript bấm lúc 00:47:00, tới 00:49:18 mới thoát.
        #
        # Không xác nhận được thì cứ để _capture_image phán xử; hỏng thật thì
        # vòng thử lại của _one_job sẽ mở chat mới sạch và làm lại từ đầu.
        if not await self._wait_started(page, box):
            log.warning("Userscript đã bấm gửi nhưng chưa thấy dấu hiệu bắt đầu "
                        "sinh ảnh - vẫn chờ tiếp, KHÔNG gửi lại để tránh bấm "
                        "trúng nút Stop.")
        return True

    async def _send_prompt(self, page: Page, text: str) -> None:
        text = flatten(text)
        box = await self._find(page, SELECTORS["prompt_box"], timeout=30_000)

        if self.extension_dir and await self._send_via_extension(page, text, box):
            return

        try:
            await box.evaluate("""(el, txt) => {
                el.innerText = txt;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", text)
            await self._wake_editor(page, box)
        except Exception:
            await box.click()
            await box.fill(text)
            await self._wake_editor(page, box)

        await page.wait_for_timeout(300)
        if not await self._click_send(page, box):
            raise RuntimeError("Không bấm được nút Send.")

    async def _click_send(self, page: Page, box) -> bool:
        # ===================== ĐỌC KỸ TRƯỚC KHI SỬA =====================
        # Trong giao diện Gemini, nút Send và nút Stop LÀ CÙNG MỘT NÚT, chỉ đổi
        # aria-label sau khi gửi. Bản trước bấm trong vòng lặp: cú bấm đầu gửi
        # prompt, cú bấm thứ hai rơi trúng nút Stop và HUỶ luôn câu trả lời đang
        # vẽ -> chat hiện "Bạn đã dừng câu trả lời này", không có ảnh nào.
        # Vì vậy: bấm ĐÚNG MỘT LẦN, và bỏ qua mọi nút có nhãn Stop/Dừng.
        # ================================================================
        async def started() -> bool:
            return await self._wait_started(page, box)

        send = page.locator(", ".join(SELECTORS["send_button"]))
        deadline = time.time() + 8
        while time.time() < deadline:
            for b in await send.all():
                try:
                    if not (await b.is_visible() and await b.is_enabled()):
                        continue
                    aria = ((await b.get_attribute("aria-label")) or "").lower()
                    if "stop" in aria or "dừng" in aria:
                        continue                     # tuyệt đối không bấm
                    await b.click(timeout=5_000)
                    if await started():
                        return True
                except Exception:
                    continue
            await page.wait_for_timeout(300)

        # Dự phòng 1: Gửi bằng tổ hợp phím Control + Enter (Phím tắt chính thức của Gemini)
        try:
            await box.focus()
            await page.keyboard.press("Control+Enter")
            if await started():
                return True
        except Exception:
            pass

        # Dự phòng 2: Gửi bằng phím Enter
        try:
            await box.click()
            await page.keyboard.press("End")
            await page.keyboard.press("Enter")
            return await started()
        except Exception:
            return False

    async def _wait_for_generation(self, page: Page) -> None:
        """Chờ nút Stop hiện rồi tắt.

        Trước đây vòng lặp thử 3 selector, mỗi cái timeout 15s -> nếu DOM đổi
        thì mất tới 45 giây chờ vô ích cho MỖI ảnh. Giờ gộp selector, chờ tối đa
        8 giây cho lượt xuất hiện.
        """
        stop = page.locator(", ".join(SELECTORS["stop_button"])).first
        deadline = time.time() + self.timeout
        try:
            # 20s chứ không phải 8s: model đôi khi "nghĩ" một lúc mới bắt đầu
            # trả lời, hạ thấp quá thì bỏ cuộc trong khi nó vẫn đang chạy.
            await stop.wait_for(state="visible", timeout=20_000)
        except PWTimeout:
            log.debug("Không thấy nút Stop, có thể model trả lời rất nhanh.")

        while time.time() < deadline:
            try:
                if not await stop.is_visible():
                    await page.wait_for_timeout(1_200)   # đệm cho ảnh render nốt
                    return
            except Exception:
                await page.wait_for_timeout(1_200)
                return
            await page.wait_for_timeout(800)
        raise TimeoutError(f"Gemini không trả lời xong trong {self.timeout}s.")

    async def _image_urls(self, page: Page) -> list[str]:
        """Ảnh ứng viên, ảnh TO NHẤT đứng trước.

        Sắp theo diện tích hiển thị chứ không lấy phần tử cuối như trước:
        tranh do model vẽ luôn là ảnh lớn nhất trong lượt trả lời, còn
        avatar/icon thì bé.

        Tốc độ: lấy src + kích thước của MỌI ảnh trong một lần chạy JS. Bản cũ
        gọi get_attribute và bounding_box cho từng thẻ, mỗi lần là một vòng CDP,
        trang nhiều ảnh thì tốn vài giây mỗi lượt quét (mà mỗi ảnh quét 2 lượt).
        """
        try:
            found = await page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
                    const r = el.getBoundingClientRect();
                    return {src: el.currentSrc || el.src || '',
                            w: r.width, h: r.height,
                            nw: el.naturalWidth || 0, nh: el.naturalHeight || 0};
                })""",
                ", ".join(SELECTORS["generated_image"]),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("Quét ảnh lỗi: %s", e)
            return []

        best: dict[str, float] = {}
        for it in found:
            src = it.get("src") or ""
            if not src or is_ui_asset(src):
                continue
            nw, nh = it.get("nw", 0), it.get("nh", 0)
            # naturalWidth = số pixel THẬT của ảnh, không phải kích thước hiển
            # thị. Placeholder/skeleton lúc đang vẽ có natural rất bé, nên đây
            # là cách chắc chắn nhất để biết ảnh đã tải xong hay chưa.
            if nw and nh and min(nw, nh) < MIN_ART_PX:
                continue
            w, h = it.get("w", 0), it.get("h", 0)
            if w and h and min(w, h) < MIN_BOX_PX:
                continue
            best[src] = max(best.get(src, 0.0), (nw * nh) or (w * h))
        return [s for s, _ in sorted(best.items(), key=lambda kv: -kv[1])]

    async def _image_rect(self, page: Page, src: str) -> dict | None:
        """Toạ độ ảnh trên màn hình, khớp theo currentSrc||src."""
        try:
            return await page.evaluate(
                """([sel, target]) => {
                    const els = Array.from(document.querySelectorAll(sel));
                    const el = els.find(e => (e.currentSrc || e.src) === target)
                        || els.sort((a, b) =>
                             (b.naturalWidth * b.naturalHeight) -
                             (a.naturalWidth * a.naturalHeight))[0];
                    if (!el) return null;
                    el.scrollIntoView({block: 'center'});
                    const r = el.getBoundingClientRect();
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                }""",
                [", ".join(SELECTORS["generated_image"]), src],
            )
        except Exception as e:  # noqa: BLE001
            log.debug("Không lấy được toạ độ ảnh: %s", e)
            return None

    async def _download_button_rect(self, page: Page) -> dict | None:
        """Tìm nút tải xuống trên thanh công cụ nổi của ảnh.

        Không dựa vào một aria-label cố định: giao diện Gemini đổi theo ngôn
        ngữ (Download / Tải xuống) và nhiều nút chỉ là icon chữ ligature
        'download'. Nên dò cả aria-label, data-test-id lẫn nội dung text.
        """
        try:
            return await page.evaluate(
                """() => {
                    const looksLikeDownload = (el) => {
                        const a = (el.getAttribute('aria-label') || '').toLowerCase();
                        const d = (el.getAttribute('data-test-id') || '').toLowerCase();
                        const t = (el.textContent || '').trim().toLowerCase();
                        return a.includes('download') || a.includes('tải')
                            || d.includes('download')
                            || t === 'download' || t === 'file_download'
                            || t === 'save_alt';
                    };
                    const btns = Array.from(
                        document.querySelectorAll('button, a[download]')
                    ).filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && looksLikeDownload(b);
                    });
                    if (!btns.length) return null;
                    const r = btns[btns.length - 1].getBoundingClientRect();
                    return {x: r.x, y: r.y, w: r.width, h: r.height};
                }""",
            )
        except Exception:
            return None

    async def _download_via_toolbar(self, page: Page, src: str, dest: Path) -> bool:
        """Rê chuột lên ảnh để hiện thanh công cụ rồi bấm nút tải xuống.

        Đây là cách sát với thao tác tay nhất, và file nhận được là bản gốc
        Gemini gửi ra chứ không phải ảnh preview trong DOM.
        """
        rect = await self._image_rect(page, src)
        if not rect or rect["w"] < 20:
            return False
        try:
            await page.mouse.move(rect["x"] + rect["w"] / 2,
                                  rect["y"] + rect["h"] / 2)
            await page.wait_for_timeout(700)          # đợi thanh công cụ hiện
            btn = await self._download_button_rect(page)
            if not btn:
                log.debug("Chưa thấy nút tải xuống khi rê chuột.")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with page.expect_download(timeout=60_000) as info:
                await page.mouse.click(btn["x"] + btn["w"] / 2,
                                       btn["y"] + btn["h"] / 2)
            dl = await info.value
            await dl.save_as(str(dest))
            return dest.exists() and dest.stat().st_size > 20_000
        except Exception as e:  # noqa: BLE001
            log.debug("Tải qua thanh công cụ lỗi: %s", e)
            return False

    async def _open_viewer(self, page: Page, src: str) -> bool:
        """Bấm vào ảnh trong khung chat để mở trình xem phóng to.

        Ảnh inline trong chat là bản xem trước nén nhỏ; Gemini chỉ nạp bản gốc
        khi người dùng bấm mở ảnh. Không làm bước này thì dù có sửa URL thành
        =s0 cũng chỉ nhận được bản preview.
        """
        rect = await self._image_rect(page, src)
        if not rect or rect["w"] < 20 or rect["h"] < 20:
            return False
        try:
            await page.wait_for_timeout(300)          # đợi cuộn xong
            # Bấm bằng chuột thật thay vì el.click() của JS, để Angular nhận
            # đúng sự kiện tin cậy.
            await page.mouse.click(rect["x"] + rect["w"] / 2,
                                   rect["y"] + rect["h"] / 2)
            await page.wait_for_timeout(1_500)
            return True                                # đã bấm được vào ảnh
        except Exception as e:  # noqa: BLE001
            log.debug("Mở trình xem ảnh lỗi: %s", e)
        return False

    async def _close_viewer(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
        except Exception:
            pass

    async def _viewer_image_urls(self, page: Page) -> list[str]:
        """URL ảnh bên trong trình xem, bản to nhất đứng trước."""
        try:
            found = await page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel)).map(el => ({
                    src: el.currentSrc || el.src || '',
                    nw: el.naturalWidth || 0, nh: el.naturalHeight || 0}))""",
                ", ".join(SELECTORS["image_viewer"]),
            )
        except Exception:
            return []
        good = [(it["nw"] * it["nh"], it["src"]) for it in found
                if it.get("src") and not is_ui_asset(it["src"])
                and min(it.get("nw", 0), it.get("nh", 0)) >= MIN_ART_PX]
        return [s for _, s in sorted(good, reverse=True)]

    async def _try_download_button(self, page: Page, dest: Path) -> bool:
        """Dùng luôn nút Tải xuống của trình xem - đây là bản gốc chuẩn nhất."""
        try:
            btn = page.locator(", ".join(SELECTORS["download_button"])).first
            try:
                await btn.wait_for(state="visible", timeout=2_500)
            except PWTimeout:
                return False
            async with page.expect_download(timeout=45_000) as info:
                await btn.click(timeout=5_000)
            dl = await info.value
            dest.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(dest))
            return dest.exists() and dest.stat().st_size > 20_000
        except Exception as e:  # noqa: BLE001
            log.debug("Nút tải xuống không dùng được: %s", e)
            return False

    async def _download_fullsize_button(self, page: Page, dest: Path) -> bool:
        """Bấm THẲNG nút 'Tải ảnh kích thước đầy đủ' trên trang (không cần viewer).

        Nút <download-generated-image-button data-test-id=...> có sẵn ngay trong
        khung chat sau khi vẽ xong; bấm nó là Gemini tải bản GỐC về. Đây là cách
        sát thao tác tay nhất và không phụ thuộc việc mở được lightbox.
        """
        loc = page.locator(
            '[data-test-id="download-generated-image-button"] button, '
            '[data-test-id="download-generated-image-button"]').last
        try:
            n = await loc.count()
        except Exception:  # noqa: BLE001
            n = 0
        if not n:
            log.info("[fullres] không thấy nút download trên trang.")
            return False
        try:
            try:
                await loc.scroll_into_view_if_needed(timeout=2_000)
                await loc.hover(timeout=2_000)      # nút chỉ hiện khi rê chuột
            except Exception:  # noqa: BLE001
                pass
            # 22s: đây là ĐƯỜNG DUY NHẤT lấy được bản gốc, nên không cắt gắt.
            # Trên log thật lần nào ăn thì file về trong 5-15s; cắt xuống 8s
            # không tiết kiệm được mấy mà lại đánh rơi ảnh 2336px xuống còn
            # 1024px - đúng thứ sinh ra cảnh báo "phải phóng 2.9 lần".
            # Hai lần bấm 22s vẫn nằm gọn dưới mốc treo 120s của lane.
            async with page.expect_download(timeout=22_000) as info:
                await loc.click(timeout=5_000, force=True)
            dl = await info.value
            dest.parent.mkdir(parents=True, exist_ok=True)
            await dl.save_as(str(dest))
            ok = dest.exists() and dest.stat().st_size > 20_000
            log.info("[fullres] nút download: ok=%s (%d KB).", ok,
                     dest.stat().st_size // 1024 if dest.exists() else 0)
            return ok
        except Exception as e:  # noqa: BLE001
            log.info("[fullres] nút download không bắt được tải: %s", e)
            return False

    async def _grab_full_image(self, page: Page, src: str, dest: Path) -> bool:
        """Mở ảnh ra rồi lấy bản gốc: ưu tiên nút Tải xuống, sau đó tới URL."""
        if not await self._open_viewer(page, src):
            return False
        try:
            if await self._try_download_button(page, dest) and is_real_art(dest):
                log.debug("Lấy được ảnh qua nút Tải xuống.")
                return True
            for url in (await self._viewer_image_urls(page))[:3]:
                if await self._download(page, url, dest) and is_real_art(dest):
                    log.debug("Lấy được ảnh gốc trong trình xem.")
                    return True
                if dest.exists():
                    dest.unlink()
            return False
        finally:
            await self._close_viewer(page)

    @staticmethod
    def _long_edge(dest: Path) -> int:
        """Cạnh dài (px) của file ảnh; 0 nếu đọc lỗi/không tồn tại."""
        try:
            from PIL import Image
            with Image.open(dest) as im:
                return max(im.size)
        except Exception:  # noqa: BLE001
            return 0

    async def _upgrade_to_fullres(self, page: Page, dest: Path) -> None:
        """Nếu ảnh vừa bắt (bản xem trước) nhỏ, thử lấy BẢN GỐC và giữ bản lớn hơn.

        Ảnh inline trong chat Gemini là bản nén ~800-1024px. Bản gốc (2K+, 3-6MB)
        CHỈ lấy được qua nút 'Tải ảnh kích thước đầy đủ' - đó là đường duy nhất,
        nên ở đây chỉ bấm nút đó (tối đa 2 lần), tải vào file tạm và chỉ THAY THẾ
        khi bản mới lớn hơn thật. Thất bại thì giữ nguyên bản xem trước.
        """
        if self.raw_min_long_edge <= 0:
            return
        cur = self._long_edge(dest)
        if cur >= self.raw_min_long_edge:
            return

        # NGÂN SÁCH THỜI GIAN cho cả bước nâng. Bốn cách dự phòng nối đuôi nhau
        # có thể ngốn hơn 120s -> vượt mốc "treo" của lane, cả ảnh bị bỏ và gen
        # lại từ đầu (~3 phút). Hết ngân sách thì dừng, giữ bản xem trước: ảnh
        # 1024px vẫn dùng được, mất 3 phút thì không.
        deadline = time.monotonic() + FULLRES_BUDGET_SEC

        def _out_of_time(step: str) -> bool:
            if time.monotonic() < deadline:
                return False
            log.info("[fullres] %s: hết ngân sách %ds ở bước %s -> giữ bản %dpx.",
                     dest.name, FULLRES_BUDGET_SEC, step, cur)
            return True

        urls = await self._image_urls(page)
        log.info("[fullres] %s: bản bắt %dpx < %d, thử nâng. %d URL ứng viên.",
                 dest.name, cur, self.raw_min_long_edge, len(urls))
        for u in urls[:3]:
            scheme = u.split(":", 1)[0]
            log.info("[fullres]   URL(%s): %s", scheme, u[:160])
        if not urls:
            return
        tmp = dest.with_suffix(dest.suffix + ".full")

        def _consider(tag: str) -> bool:
            big = self._long_edge(tmp) if tmp.exists() else 0
            if tmp.exists() and is_real_art(tmp) and big > cur:
                tmp.replace(dest)
                log.info("[fullres] %s: NÂNG qua %s -> %dpx (trước %dpx).",
                         dest.name, tag, big, cur)
                return True
            if tmp.exists():
                log.info("[fullres] %s: %s cho %dpx (%d KB), không hơn %dpx -> bỏ.",
                         dest.name, tag, big, tmp.stat().st_size // 1024, cur)
                tmp.unlink()
            else:
                log.info("[fullres] %s: %s không tải được file.", dest.name, tag)
            return False

        # 1) ĐÁNG TIN NHẤT: bấm thẳng nút 'Tải ảnh đầy đủ' trên trang.
        try:
            if await self._download_fullsize_button(page, tmp) and _consider("nút-download"):
                return
        except Exception as e:  # noqa: BLE001
            log.info("[fullres] nút-download lỗi: %s", e)

        # KHÔNG CÒN ĐƯỜNG NÀO KHÁC. Ba nhánh dự phòng cũ (=s0/url, viewer,
        # toolbar) đã bị bỏ: trên log thật, =s0/url chỉ trả về 1024-1184px tức
        # vẫn là bản xem trước, còn viewer/toolbar chưa từng thành công lần nào.
        # Chúng chỉ đốt thời gian sau khi đường thật đã trượt, đẩy job qua mốc
        # treo 120s -> mất cả ảnh, phải gen lại.
        #
        # Vì đây là đường DUY NHẤT, thà thử lại nó lần nữa còn hơn bỏ cuộc:
        # bấm lại rẻ hơn nhiều so với gen lại cả ảnh (~3 phút + quota).
        if not _out_of_time("bấm lại"):
            log.info("[fullres] %s: thử bấm lại nút download (lần 2).", dest.name)
            try:
                if await self._download_fullsize_button(page, tmp) and _consider("nút-download-2"):
                    return
            except Exception as e:  # noqa: BLE001
                log.info("[fullres] nút-download lần 2 lỗi: %s", e)

        log.warning("[fullres] %s: không lấy được bản lớn hơn %dpx, giữ bản xem trước.",
                    dest.name, cur)

    async def _turn_ended_with_text(self, page: Page) -> bool:
        """Model đã kết thúc lượt trả lời và có chữ trong câu trả lời."""
        try:
            stop = page.locator(", ".join(SELECTORS["stop_button"])).first
            if await stop.is_visible():
                return False              # vẫn đang chạy
        except Exception:
            pass
        for sel in SELECTORS["response_container"]:
            try:
                loc = page.locator(sel).last
                if await loc.is_visible():
                    return len((await loc.inner_text()).strip()) > 40
            except Exception:
                continue
        return False

    async def _capture_image(self, page: Page, catcher: ImageCatcher,
                             dest: Path) -> bool:
        """Chờ ảnh đi qua tầng mạng rồi ghi thẳng ra file.

        Trả về True nếu có ảnh đạt chuẩn. Nếu Gemini báo lỗi hoặc trả lời bằng
        chữ thì thoát sớm chứ không chờ hết generation_timeout.

        Chờ thêm 'settle' sau khi bắt được ảnh đầu tiên: Gemini hay tải bản
        nhỏ trước rồi mới tới bản gốc, ta muốn bản to nhất.
        """
        deadline = time.time() + self.timeout
        first_seen_at = None
        text_done_at = None
        settle = 6.0

        while time.time() < deadline:
            if catcher.images:
                if first_seen_at is None:
                    first_seen_at = time.time()
                    log.debug("Thấy ảnh đầu tiên, chờ %.0fs xem có bản to hơn.",
                              settle)
                elif time.time() - first_seen_at >= settle:
                    break
            else:
                # Kiểm tra hạn mức TRƯỚC: thông báo hết hạn mức cũng là một câu
                # trả lời bằng chữ, không tách ra thì bị hiểu nhầm thành
                # NoImageInReply rồi đi nhắc lại một cách vô ích.
                quota = await self._quota_text(page)
                if quota:
                    raise QuotaExhausted(
                        f"Tài khoản đã hết hạn mức tạo ảnh ({quota}).")
                err = await self._page_error_text(page)
                if err:
                    raise RuntimeError(f"Gemini báo lỗi ({err}).")
                # Trả lời xong, có chữ, mà không ảnh nào đi qua mạng -> nó chỉ
                # nói chứ không vẽ. Chốt sớm sau 5 giây thay vì chờ hết 240s.
                if await self._turn_ended_with_text(page):
                    if text_done_at is None:
                        text_done_at = time.time()
                    elif time.time() - text_done_at >= 5:
                        raise NoImageInReply(
                            "Gemini trả lời bằng chữ, không kèm ảnh.")
                else:
                    text_done_at = None
            await asyncio.sleep(1.0)

        best = catcher.best()
        if not best:
            if catcher.images:
                log.debug("Bắt được %d ảnh nhưng đều nhỏ hơn %dpx.",
                          len(catcher.images), MIN_ART_PX)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(best)
        log.debug("Ghi ảnh bắt từ mạng: %d KB", len(best) // 1024)
        return True

    async def _quota_text(self, page: Page) -> str:
        """Bắt thông báo hết hạn mức tạo ảnh."""
        needles = (
            "as soon as your limit resets",
            "check your usage in settings",
            "reached your limit",
            "you've hit your limit",
            "daily limit",
            "hạn mức",
            "giới hạn",
            "kiểm tra mức sử dụng",
        )
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            return ""
        for n in needles:
            if n in body:
                return n
        return ""

    async def _page_error_text(self, page: Page) -> str:
        """Bắt thông báo lỗi của Gemini để dừng sớm thay vì chờ hết giờ."""
        needles = ("encountered an error", "something went wrong",
                   "hard time", "dừng câu trả lời", "đã xảy ra lỗi")
        try:
            body = (await page.inner_text("body")).lower()
        except Exception:
            return ""
        for n in needles:
            if n in body:
                return n
        return ""

    async def _wait_for_image(self, page: Page, before: set[str],
                              timeout: float | None = None) -> list[str]:
        """Chờ tới khi có ảnh THẬT mới xuất hiện, tối đa generation_timeout.

        Trước đây chỉ chờ nút Stop rồi quét một lần: model vẽ lâu hơn vài giây
        là bỏ cuộc, báo 'không thấy ảnh mới' dù thật ra nó vẫn đang vẽ.
        Giờ mốc kết thúc là ẢNH, không phải cái nút.
        """
        limit = self.timeout if timeout is None else timeout
        deadline = time.time() + limit
        while time.time() < deadline:
            new = [u for u in await self._image_urls(page) if u not in before]
            if new:
                return new
            err = await self._page_error_text(page)
            if err:
                raise RuntimeError(f"Gemini báo lỗi ({err}).")
            await page.wait_for_timeout(2_000)
        raise TimeoutError(f"Không thấy ảnh sau {limit:.0f}s.")

    async def _download(self, page: Page, src: str, dest: Path) -> bool:
        dest.parent.mkdir(parents=True, exist_ok=True)

        if src.startswith("data:image"):
            dest.write_bytes(base64.b64decode(src.split(",", 1)[1]))
            return True

        if src.startswith("blob:"):
            b64 = await page.evaluate(
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

        for candidate in (upscale_url(src), src):
            try:
                resp = await page.request.get(candidate, timeout=60_000)
                if not resp.ok:
                    continue
                body = await resp.body()
                if len(body) < 20_000:        # tranh thật không bao giờ bé thế
                    log.debug("Bỏ %s: chỉ %d byte.", candidate, len(body))
                    continue
                dest.write_bytes(body)
                return True
            except Exception as e:  # noqa: BLE001
                log.debug("Tải %s lỗi: %s", candidate, e)
        return False

    # ---------- API công khai ----------

    async def ask_text(self, prompt: str) -> str:
        """Hỏi text bằng tab đầu tiên (dùng để nhờ Gemini nghĩ chủ đề)."""
        page = self.pages[0]
        await self._new_chat(page)
        await self._send_prompt(page, prompt)
        await self._wait_for_generation(page)
        for sel in SELECTORS["response_container"]:
            loc = page.locator(sel).last
            try:
                if await loc.is_visible():
                    return await loc.inner_text()
            except Exception:
                continue
        return ""

    async def _attach_images(self, page: Page, file_paths: list[Path]) -> bool:
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
            async (payloads) => {
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
            pasted = await page.evaluate(js_paste, file_payloads)
            if pasted:
                await page.wait_for_timeout(wait_ms)
                log.info("[tab] Đã dán (Paste) %d ảnh thực tế trực tiếp qua Clipboard (chờ %dms).", len(valid_paths), wait_ms)
                return True
        except Exception as e_paste:
            log.debug("Dán ảnh Clipboard lỗi: %s, chuyển sang set_input_files", e_paste)

        # 2. Thử set_input_files trực tiếp lên input[type="file"]
        str_paths = [str(p) for p in valid_paths]
        try:
            inp = page.locator('input[type="file"]').first
            if await inp.count() > 0:
                await inp.set_input_files(str_paths, timeout=4_000)
                await page.wait_for_timeout(wait_ms)
                log.info("[tab] Đã đính kèm %d ảnh thực tế qua input[type=file] (chờ %dms).", len(valid_paths), wait_ms)
                return True
        except Exception as e1:
            log.debug("Trực tiếp set_input_files thất bại: %s, chuyển sang nút Upload", e1)

        # 3. Dự phòng: Mở menu Upload và chọn Tải tệp lên (Lọc bỏ Google Workspace & Google Drive để không bị dính popup)
        try:
            plus_btn = page.locator(
                'button[aria-label*="tải" i], button[aria-label*="upload" i], '
                'button[aria-label*="thêm" i], button[aria-label*="đính kèm" i], '
                'button[aria-label*="add" i], button.uploader-button'
            ).first
            
            if await plus_btn.is_visible():
                await plus_btn.click()
                await page.wait_for_timeout(500)
                
                upload_item = page.locator(
                    'div[role="menuitem"]:has-text("Tải tệp"), div[role="menuitem"]:has-text("Upload"), '
                    'div[role="menuitem"]:has-text("tải lên"), button:has-text("Tải tệp"), button:has-text("Upload")'
                ).filter(has_not_text="Workspace").filter(has_not_text="Drive").first
                
                if await upload_item.is_visible():
                    async with page.expect_file_chooser(timeout=4_000) as fc_info:
                        await upload_item.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(str_paths)
                    await page.wait_for_timeout(wait_ms)
                    log.info("[tab] Đã đính kèm %d ảnh qua menu Upload (chờ %dms).", len(valid_paths), wait_ms)
                    return True
        except Exception as e2:
            log.debug("Upload qua menu thất bại: %s", e2)

        log.warning("Không tìm thấy nút hoặc ô đính kèm ảnh.")
        return False

    async def _one_job(self, page: Page, idx: int, prompt: str, dest: Path,
                       same_chat: bool = False, attach_files: list[Path] | None = None) -> bool:
        """same_chat=True: gửi tiếp vào cuộc trò chuyện đang mở thay vì mở mới."""
        for attempt in range(1, self.max_retries + 1):
            from bookgen.cancel import check_cancel
            check_cancel()
            await self.throttle.acquire()
            try:
                # Mở chat mới TRƯỚC khi gắn bộ bắt ảnh, để không nhặt nhầm ảnh
                # còn sót của lượt trước. same_chat=True là chuỗi hai bìa - phải
                # ở nguyên trong chat đang mở thì bìa sau mới thấy bìa trước.
                log.info("[tab %d] %s: bắt đầu (lần %d/%d).",
                         idx, dest.stem, attempt, self.max_retries)
                if not same_chat:
                    await self._ensure_fresh_chat(page)

                catcher = self.catchers.get(page)
                if catcher:
                    eff_ignore = None if "preview" in dest.stem else attach_files
                    catcher.arm(ignore_files=eff_ignore)

                if attach_files:
                    await self._attach_images(page, attach_files)
                    await page.wait_for_timeout(1_000)

                # Chụp danh sách ảnh ĐANG có trước khi gửi, để _wait_for_image
                # phân biệt được ảnh mới với ảnh cũ còn trên trang.
                #
                # Biến này vốn bị THIẾU HẲN: dòng dưới gọi _wait_for_image(page,
                # before, ...) nhưng không chỗ nào gán 'before', nên cả đường dự
                # phòng DOM nổ NameError ngay câu đầu và chưa từng chạy được lần
                # nào - kể cả nhánh bấm nút tải xuống để lấy file GỐC.
                before = set(await self._image_urls(page))

                await self._send_prompt(page, prompt)
                log.info("[tab %d] %s: đã gửi, chờ ảnh (tối đa %.0fs).",
                         idx, dest.stem, self.timeout)

                # QUAN TRỌNG: Ngay sau khi gửi prompt, xóa sạch toàn bộ ảnh upload/paste lỡ bị catcher bắt trước đó!
                if catcher:
                    catcher.clear()

                # 0) Đường chính: bắt ảnh ở tầng mạng. Chắc chắn nhất, và cho
                #    luôn bytes gốc nên không cần rê chuột/bấm nút/đoán URL.
                if catcher:
                    try:
                        got = False
                        for nudge in range(self.max_nudges + 1):
                            try:
                                got = await self._capture_image(
                                    page, catcher, dest)
                                break
                            except NoImageInReply:
                                if nudge >= self.max_nudges:
                                    raise
                                # Nó chỉ nói mà không vẽ -> nhắc thẳng, ngay
                                # trong chat đó, giữ nguyên ngữ cảnh bìa trước.
                                log.warning(
                                    "[tab %d] %s: chỉ trả lời bằng chữ, "
                                    "nhắc lại (%d/%d).",
                                    idx, dest.stem, nudge + 1, self.max_nudges)
                                catcher.arm()
                                await self._send_prompt(page, self.nudge_prompt)
                        if got and is_real_art(dest):
                            # Bản bắt ở mạng là ảnh xem trước nhỏ -> thử nâng lên
                            # bản gốc (2K+) trước khi chốt.
                            await self._upgrade_to_fullres(page, dest)
                            await self.throttle.on_ok()
                            log.info("[tab %d] OK -> %s (%dpx)",
                                     idx, dest.name, self._long_edge(dest))
                            return True
                    finally:
                        catcher.disarm()
                    if dest.exists():
                        dest.unlink()
                    log.warning("[tab %d] %s: hết %.0fs chờ mà không bắt được ảnh "
                                "ở tầng mạng -> thử đường DOM.",
                                idx, dest.stem, self.timeout)

                # Tới đây nghĩa là đã chờ hết generation_timeout ở bước bắt
                # mạng rồi. Nếu ảnh có thật thì nó phải đã nằm sẵn trong DOM,
                # nên chỉ cho thêm 20 giây - đừng chờ thêm một lượt 240s nữa.
                new = await self._wait_for_image(page, before, timeout=20)

                # NGUYÊN TẮC: ảnh đã vẽ xong là tài nguyên đắt (tốn quota, tốn
                # cả phút chờ). Lấy được bản nào thì GIỮ bản đó, chỉ coi là
                # thất bại khi không có gì dùng được. Trước đây không mở nổi
                # trình xem là vứt cả ảnh rồi vẽ lại từ đầu - vừa phí quota
                # vừa lâu, mà ảnh thì vẫn nằm sẵn trong chat.
                saved = False

                # 1) NÚT TẢI XUỐNG trên thanh công cụ nổi của ảnh - giống hệt
                #    thao tác tay, và cho ra file gốc. Ưu tiên số một.
                for url in new[:2]:
                    if await self._download_via_toolbar(page, url, dest) \
                            and is_real_art(dest):
                        saved = True
                        break
                    if dest.exists():
                        dest.unlink()

                # 2) Mở ảnh phóng to rồi lấy bản gốc trong trình xem.
                if not saved:
                    for url in new[:2]:
                        if await self._grab_full_image(page, url, dest):
                            saved = True
                            break

                # 3) Dự phòng cuối: bản xem trước trong khung chat.
                if not saved:
                    log.debug("Không mở được trình xem, dùng ảnh trong chat.")
                    for url in new[:4]:
                        if await self._download(page, url, dest) and is_real_art(dest):
                            saved = True
                            log.warning(
                                "[tab %d] %s: chỉ lấy được bản xem trước, "
                                "độ phân giải có thể thấp hơn chuẩn in.",
                                idx, dest.stem)
                            break
                        if dest.exists():
                            dest.unlink()

                if not saved:
                    raise RuntimeError(
                        "Không lấy được ảnh gốc (chỉ thấy bản xem trước/icon)."
                    )

                await self.throttle.on_ok()
                log.info("[tab %d] OK -> %s", idx, dest.name)
                return True

            except QuotaExhausted:
                raise            # hết hạn mức: retry vô nghĩa, để pool dừng
            except Exception as e:  # noqa: BLE001
                log.warning("[tab %d] %s lần %d/%d lỗi: %s",
                            idx, dest.stem, attempt, self.max_retries, e)
                await self.throttle.on_fail()
                try:
                    await page.screenshot(
                        path=str(dest.with_name(f"debug_{dest.stem}_try{attempt}.png"))
                    )
                except Exception:
                    pass
            finally:
                await self.throttle.release()

            if attempt < self.max_retries:
                await asyncio.sleep(random.uniform(15, 35))
        return False

    async def run_jobs(self, jobs: list, on_done=None) -> dict:
        """jobs = danh sách việc. Mỗi phần tử là một trong hai dạng:

            (key, prompt, dest)              - một ảnh, chat riêng
            [(key, prompt, dest), ...]       - CHUỖI: các ảnh vẽ nối tiếp nhau
                                               trong cùng một chat, cùng một tab

        Dạng chuỗi dùng khi các ảnh phải khớp nhau (bìa trước + bìa sau).
        on_done(key, ok) được gọi sau mỗi ảnh.
        """
        # MỖI TAB MỘT HÀNG ĐỢI RIÊNG, chia sẵn theo vòng tròn.
        #
        # Bản trước dùng một hàng đợi chung, tab nào rảnh trước thì bốc tiếp. Cách
        # đó tổng thời gian ngắn hơn, nhưng số ảnh giữa các tab lệch rất xa: tab
        # chạy trơn ăn gần hết việc, tab bị Gemini cho xếp hàng thì làm được vài
        # cái. Chia sẵn thì mỗi tab nhận đúng len(jobs)/N việc, chênh nhau tối đa 1.
        #
        # ĐÁNH ĐỔI phải biết: không còn "tab rảnh làm hộ" nữa. Tab chậm giữ nguyên
        # phần của nó, các tab khác xong sớm sẽ ngồi không chờ. Tổng thời gian
        # chạy bằng thời gian của tab CHẬM NHẤT.
        n_workers = len(self.pages)
        queues: list[asyncio.Queue] = [asyncio.Queue() for _ in range(n_workers)]
        for i, j in enumerate(jobs):
            queues[i % n_workers].put_nowait((j, 0))   # (việc, số lần đã xếp lại)
        results: dict[str, bool] = {}
        done_by_tab: dict[int, int] = {i: 0 for i in range(1, n_workers + 1)}

        def handoff(from_idx: int, extra=None) -> None:
            """Dồn việc của tab hết hạn mức sang các tab thuộc tài khoản khác.

            Chia đều là để cân tải, không phải để đánh mất việc: tài khoản nào
            cạn quota thì phần của nó BẮT BUỘC phải chuyển đi, chấp nhận lệch.
            """
            spare = [i for i in range(1, n_workers + 1)
                     if self.pages[i - 1].context not in self.exhausted_contexts]
            pending = [extra] if extra is not None else []
            q = queues[from_idx - 1]
            while not q.empty():
                pending.append(q.get_nowait())
                q.task_done()
            if not spare or not pending:
                return
            for k, it in enumerate(pending):
                queues[spare[k % len(spare)] - 1].put_nowait(it)
            log.info("Chuyển %d việc của tab %d sang các tab %s.",
                     len(pending), from_idx, spare)

        async def run_steps(idx: int, steps: list) -> None:
            page = self.pages[idx - 1]        # có thể đã bị thay bởi _recycle
            for n, step in enumerate(steps):
                if len(step) == 4:
                    key, prompt, dest, attach = step
                else:
                    key, prompt, dest = step
                    attach = None
                # Bước đầu mở chat mới, các bước sau nối tiếp trong đó.
                ok = await self._one_job(page, idx, prompt, dest,
                                         same_chat=(n > 0), attach_files=attach)
                results[key] = ok
                if on_done:
                    on_done(key, ok)

        busy = 0        # số worker đang làm; cần để biết khi nào thật sự hết việc

        async def worker(idx: int):
            nonlocal busy
            since_recycle = 0
            q = queues[idx - 1]               # hàng đợi RIÊNG của tab này
            while True:
                page_context = self.pages[idx - 1].context
                if page_context in self.exhausted_contexts:
                    return
                if len(self.exhausted_contexts) == len(self.contexts):
                    self.quota_hit = True
                    return

                try:
                    item, tries = q.get_nowait()
                except asyncio.QueueEmpty:
                    # Hàng đợi rỗng CHƯA chắc là xong: một tab khác có thể đang
                    # treo và sắp xếp việc của nó trở lại. Chỉ thoát khi không
                    # còn ai đang làm.
                    if busy == 0:
                        return
                    await asyncio.sleep(1)
                    continue
                busy += 1
                try:
                    steps = item if isinstance(item, list) else [item]
                    names = ", ".join(step[0] for step in steps)
                    stalled = False
                    try:
                        # Đồng hồ canh: quá stall_timeout mà chưa xong thì bỏ
                        # dở, coi như tab hỏng chứ không ngồi chờ tiếp.
                        await asyncio.wait_for(
                            run_steps(idx, steps),
                            timeout=self.stall_timeout * len(steps))
                    except asyncio.TimeoutError:
                        stalled = True
                        log.warning(
                            "[tab %d] %s treo quá %.0fs -> bỏ, mở phiên mới.",
                            idx, names, self.stall_timeout)
                    except QuotaExhausted as e:
                        log.error("[tab %d] %s", idx, e)
                        self.exhausted_contexts.add(page_context)

                        # Tài khoản này cạn -> đẩy CẢ phần còn lại của tab sang
                        # tab khác, không chỉ mỗi việc đang dở.
                        handoff(idx, (item, tries))
                        q.task_done()
                        if len(self.exhausted_contexts) == len(self.contexts):
                            self.quota_hit = True
                            log.error("Tất cả tài khoản đều hết hạn mức. Dừng hệ thống.")
                        else:
                            log.info("Chuyển việc %s cho tài khoản khác.", names)
                        return
                    finally:
                        if page_context not in self.exhausted_contexts:
                            q.task_done()

                    if stalled:
                        # Tab coi như hỏng: đóng hẳn, mở tab mới sạch hoàn toàn.
                        try:
                            await self._recycle(idx)
                            since_recycle = 0
                        except Exception as e:  # noqa: BLE001
                            log.warning("[tab %d] tái tạo lỗi: %s", idx, e)
                        if tries < self.max_requeue:
                            # Xếp lại CUỐI hàng đợi: tab nào rảnh trước thì làm,
                            # không nhất thiết phải là tab vừa treo.
                            q.put_nowait((item, tries + 1))
                            log.info("Xếp lại %s vào hàng đợi (lần %d/%d).",
                                     names, tries + 1, self.max_requeue)
                        else:
                            for step in steps:
                                key = step[0]
                                results.setdefault(key, False)
                                if on_done:
                                    on_done(key, False)
                            log.error("%s treo %d lần, bỏ qua.", names, tries + 1)
                        continue

                    # Chỉ tái tạo SAU khi ảnh đã tải xong, không giữa chừng.
                    #
                    # ĐỪNG chuyển việc cộng/tái tạo này vào trong run_steps: hai
                    # bìa là MỘT việc gồm 2 bước chạy nối tiếp trong cùng một chat
                    # (build_jobs gộp chúng lại) để bìa sau nhìn thấy bìa trước mà
                    # khớp tông màu. Tái tạo giữa hai bước là đóng mất chat đó, bìa
                    # sau sẽ vẽ một mình và lệch hẳn phong cách. Cộng theo cả việc
                    # (len(steps)) nên recycle_tab_every=1 vẫn an toàn cho bìa.
                    done_by_tab[idx] += len(steps)
                    since_recycle += len(steps)
                    if self.recycle_every and since_recycle >= self.recycle_every:
                        since_recycle = 0
                        if not q.empty():
                            try:
                                await self._recycle(idx)
                            except Exception as e:  # noqa: BLE001
                                log.warning("[tab %d] tái tạo lỗi: %s", idx, e)

                    lo, hi = self.delay_range
                    await asyncio.sleep(random.uniform(lo, hi))
                finally:
                    busy -= 1

        await asyncio.gather(*(worker(i) for i in range(1, n_workers + 1)))
        log.info("Số ảnh mỗi tab: %s",
                 " | ".join(f"tab {i}: {c}" for i, c in sorted(done_by_tab.items())))
        return results
