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
        self.armed = False

    def arm(self) -> None:
        """Bật bắt, xoá kết quả cũ. Gọi ngay trước khi gửi prompt."""
        self.images.clear()
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

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
        
        self.recycle_every = max(0, int(b.get("recycle_tab_every", 5)))
        self.stall_timeout = float(b.get("stall_timeout", 120))
        self.max_requeue = int(b.get("max_requeue", 2))
        self.max_nudges = int(b.get("max_nudges", 2))
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
        
        for p_dir in self.profile_dirs:
            p_dir.mkdir(parents=True, exist_ok=True)
            ctx = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(p_dir),
                headless=self.headless,
                channel="chrome",
                viewport={"width": 1440, "height": 960},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--disable-extensions",
                    "--disable-dev-shm-usage",
                    "--js-flags=--max-old-space-size=512",
                ],
            )
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
            return
        except PWTimeout:
            pass
        if self.headless:
            raise RuntimeError(
                "Chưa đăng nhập Google và đang chạy headless. "
                "Đặt browser.headless: false, đăng nhập một lần rồi chạy lại."
            )
        print("\n" + "=" * 62)
        print("  Hãy ĐĂNG NHẬP Google trong cửa sổ Chrome vừa mở.")
        print("  Xong thì quay lại đây và nhấn Enter...")
        print("=" * 62 + "\n")
        await asyncio.get_running_loop().run_in_executor(None, input)
        await self._find(page, SELECTORS["prompt_box"], timeout=60_000)

    # ---------- helper ----------

    async def _find(self, page: Page, selectors: list[str], timeout: int = 10_000):
        """Chờ MỘT lần trên tất cả selector gộp lại.

        Bản cũ chờ lần lượt từng selector và chia nhỏ timeout, nên selector nào
        không khớp cũng ngốn trọn phần thời gian của nó. Gộp bằng dấu phẩy thì
        Playwright kiểm tra song song, khớp cái nào xong ngay cái đó.
        """
        loc = page.locator(", ".join(selectors)).first
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
        await page.wait_for_timeout(1_500)
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
            log.debug("Không bấm được nút chat mới (%s), quay về goto.", e)
        await page.goto(self.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2_500)

    async def _send_prompt(self, page: Page, text: str) -> None:
        box = await self._find(page, SELECTORS["prompt_box"], timeout=30_000)
        await box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        # Gõ bằng phím thật, một dòng duy nhất - xem ghi chú trong gemini_driver.py.
        # Prompt dài ~430 ký tự: delay 8ms/ký tự = 3.4 giây, delay 1ms = 0.4 giây.
        # Vẫn là sự kiện bàn phím thật nên Angular vẫn bật nút Send.
        await page.keyboard.type(flatten(text), delay=self.typing_delay)
        await page.wait_for_timeout(400)
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
        stop = page.locator(", ".join(SELECTORS["stop_button"])).first

        async def started() -> bool:
            """Gửi thành công = nút Stop hiện ra HOẶC ô nhập trống đi."""
            for _ in range(16):                      # tối đa ~8 giây
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

        send = page.locator(", ".join(SELECTORS["send_button"]))
        deadline = time.time() + 12
        while time.time() < deadline:
            for b in await send.all():
                try:
                    if not (await b.is_visible() and await b.is_enabled()):
                        continue
                    aria = ((await b.get_attribute("aria-label")) or "").lower()
                    if "stop" in aria or "dừng" in aria:
                        continue                     # tuyệt đối không bấm
                    await b.click(timeout=5_000)
                    return await started()           # bấm một lần rồi thôi
                except Exception:
                    continue
            await page.wait_for_timeout(300)

        try:                                   # dự phòng: gửi bằng phím Enter
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

    async def _one_job(self, page: Page, idx: int, prompt: str, dest: Path,
                       same_chat: bool = False) -> bool:
        """same_chat=True: gửi tiếp vào cuộc trò chuyện đang mở thay vì mở mới.

        Dùng cho các ảnh cần thấy nhau, ví dụ bìa sau phải khớp tông với bìa
        trước - tả lại bằng chữ không bao giờ khớp bằng việc cho model nhìn
        thẳng vào ảnh nó vừa vẽ.
        """
        for attempt in range(1, self.max_retries + 1):
            await self.throttle.acquire()
            try:
                if not same_chat:
                    await self._new_chat(page)
                before = set(await self._image_urls(page))

                catcher = self.catchers.get(page)
                if catcher:
                    catcher.arm()          # bật bắt NGAY TRƯỚC khi gửi
                await self._send_prompt(page, prompt)

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
                            await self.throttle.on_ok()
                            log.info("[tab %d] OK -> %s", idx, dest.name)
                            return True
                    finally:
                        catcher.disarm()
                    if dest.exists():
                        dest.unlink()
                    log.debug("Không bắt được ảnh qua mạng, thử đường DOM.")

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
        queue: asyncio.Queue = asyncio.Queue()
        for j in jobs:
            queue.put_nowait((j, 0))          # (việc, số lần đã xếp lại)
        results: dict[str, bool] = {}

        async def run_steps(idx: int, steps: list) -> None:
            page = self.pages[idx - 1]        # có thể đã bị thay bởi _recycle
            for n, (key, prompt, dest) in enumerate(steps):
                # Bước đầu mở chat mới, các bước sau nối tiếp trong đó.
                ok = await self._one_job(page, idx, prompt, dest,
                                         same_chat=(n > 0))
                results[key] = ok
                if on_done:
                    on_done(key, ok)

        busy = 0        # số worker đang làm; cần để biết khi nào thật sự hết việc

        async def worker(idx: int):
            nonlocal busy
            since_recycle = 0
            while True:
                page_context = self.pages[idx - 1].context
                if page_context in self.exhausted_contexts:
                    return
                if len(self.exhausted_contexts) == len(self.contexts):
                    self.quota_hit = True
                    return

                try:
                    item, tries = queue.get_nowait()
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
                    names = ", ".join(k for k, _, _ in steps)
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
                        
                        queue.put_nowait((item, tries))
                        queue.task_done()
                        if len(self.exhausted_contexts) == len(self.contexts):
                            self.quota_hit = True
                            log.error("Tất cả tài khoản đều hết hạn mức. Dừng hệ thống.")
                        else:
                            log.info("Chuyển việc %s cho tài khoản khác.", names)
                        return
                    finally:
                        if page_context not in self.exhausted_contexts:
                            queue.task_done()

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
                            queue.put_nowait((item, tries + 1))
                            log.info("Xếp lại %s vào hàng đợi (lần %d/%d).",
                                     names, tries + 1, self.max_requeue)
                        else:
                            for key, _, _ in steps:
                                results.setdefault(key, False)
                                if on_done:
                                    on_done(key, False)
                            log.error("%s treo %d lần, bỏ qua.", names, tries + 1)
                        continue

                    # Chỉ tái tạo SAU khi ảnh đã tải xong, không giữa chừng.
                    since_recycle += len(steps)
                    if self.recycle_every and since_recycle >= self.recycle_every:
                        since_recycle = 0
                        if not queue.empty():
                            try:
                                await self._recycle(idx)
                            except Exception as e:  # noqa: BLE001
                                log.warning("[tab %d] tái tạo lỗi: %s", idx, e)

                    lo, hi = self.delay_range
                    await asyncio.sleep(random.uniform(lo, hi))
                finally:
                    busy -= 1

        await asyncio.gather(*(worker(i) for i in range(1, len(self.pages) + 1)))
        return results
