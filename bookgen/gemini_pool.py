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

from bookgen.gemini_driver import SELECTORS, flatten

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
        # nếu pool đang trong thời gian hạ nhiệt thì chờ hết
        while (wait := self.cooldown_until - time.time()) > 0:
            await asyncio.sleep(min(wait, 5))

    async def release(self) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    async def on_fail(self) -> None:
        """Hai lần fail liên tiếp -> bớt 1 tab và hạ nhiệt cả pool."""
        async with self._cond:
            self._oks = 0
            self._fails += 1
            if self._fails >= 2 and self.limit > self.min:
                self.limit -= 1
                self._fails = 0
                log.warning("Nhiều lỗi liên tiếp -> giảm còn %d phiên song song.",
                            self.limit)
            self.cooldown_until = max(
                self.cooldown_until, time.time() + random.uniform(20, 45)
            )
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

class GeminiPool:
    """N tab Gemini cùng rút việc từ một hàng đợi."""

    def __init__(self, cfg: dict):
        b = cfg["browser"]
        self.url = b["gemini_url"]
        self.user_data_dir = Path(b["user_data_dir"]).resolve()
        self.headless = b.get("headless", False)
        self.timeout = b.get("generation_timeout", 240)
        self.delay_range = b.get("delay_between_prompts", [8, 20])
        self.max_retries = b.get("max_retries", 3)
        self.workers = max(1, int(b.get("concurrency", 1)))
        # Đóng và mở lại tab sau mỗi N ảnh để trả RAM về hệ điều hành.
        # 0 = không tái tạo.
        self.recycle_every = max(0, int(b.get("recycle_tab_every", 5)))
        self._pw = None
        self._ctx = None
        self.pages: list[Page] = []
        self.throttle = Throttle(self.workers)

    # ---------- lifecycle ----------

    async def __aenter__(self) -> "GeminiPool":
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            channel="chrome",
            viewport={"width": 1440, "height": 960},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
                "--no-first-run",
                # ---- tiết kiệm RAM ----
                "--disable-extensions",
                "--disable-dev-shm-usage",
                "--js-flags=--max-old-space-size=512",  # chặn trần heap mỗi renderer
            ],
        )
        first = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.pages = [first]
        for _ in range(self.workers - 1):
            self.pages.append(await self._ctx.new_page())

        for p in self.pages:
            await self._prepare(p)

        await self._ensure_logged_in(self.pages[0])
        log.info("Đã mở %d phiên Gemini.", len(self.pages))
        return self

    async def __aexit__(self, *exc):
        try:
            if self._ctx:
                await self._ctx.close()
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
        last = None
        per = max(1500, timeout // max(1, len(selectors)))
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=per)
                return loc
            except PWTimeout as e:
                last = e
        raise PWTimeout(f"Không tìm thấy element nào khớp: {selectors}") from last

    async def _prepare(self, page: Page) -> Page:
        """Cài init script và mở sẵn trang Gemini cho một tab mới."""
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
        new = await self._ctx.new_page()      # mở trước, đóng sau -> context luôn còn tab
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
            btn = await self._find(page, SELECTORS["new_chat"], timeout=5_000)
            await btn.click()
            await page.wait_for_timeout(1_500)
            await self._find(page, SELECTORS["prompt_box"], timeout=10_000)
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
        # Gõ bằng phím thật, một dòng duy nhất - xem ghi chú trong gemini_driver.py
        await page.keyboard.type(flatten(text), delay=random.randint(3, 10))
        await page.wait_for_timeout(1_200)
        if not await self._click_send(page, box):
            raise RuntimeError("Không bấm được nút Send.")

    async def _click_send(self, page: Page, box) -> bool:
        async def sent() -> bool:
            await page.wait_for_timeout(1_500)
            try:
                return len((await box.inner_text()).strip()) < 10
            except Exception:
                return True

        deadline = time.time() + 12
        while time.time() < deadline:
            for sel in SELECTORS["send_button"]:
                for b in await page.locator(sel).all():
                    try:
                        if await b.is_visible() and await b.is_enabled():
                            await b.click(timeout=5_000)
                            if await sent():
                                return True
                    except Exception:
                        continue
            await page.wait_for_timeout(500)

        try:
            await box.click()
            await page.keyboard.press("End")
            await page.keyboard.press("Enter")
            return await sent()
        except Exception:
            return False

    async def _wait_for_generation(self, page: Page) -> None:
        deadline = time.time() + self.timeout
        for sel in SELECTORS["stop_button"]:
            try:
                await page.locator(sel).first.wait_for(state="visible", timeout=15_000)
                break
            except PWTimeout:
                continue
        while time.time() < deadline:
            visible = False
            for sel in SELECTORS["stop_button"]:
                try:
                    if await page.locator(sel).first.is_visible():
                        visible = True
                        break
                except Exception:
                    pass
            if not visible:
                await page.wait_for_timeout(2_500)
                return
            await page.wait_for_timeout(1_500)
        raise TimeoutError(f"Gemini không trả lời xong trong {self.timeout}s.")

    async def _image_urls(self, page: Page) -> list[str]:
        urls: list[str] = []
        for sel in SELECTORS["generated_image"]:
            for el in await page.locator(sel).all():
                try:
                    src = await el.get_attribute("src") or ""
                except Exception:
                    continue
                if not src or src in urls:
                    continue
                try:
                    bb = await el.bounding_box()
                    if bb and (bb["width"] < 120 or bb["height"] < 120):
                        continue
                except Exception:
                    pass
                urls.append(src)
        return urls

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

        hi = re.sub(r"=[swh]\d+(-[a-z0-9-]+)?$", "=s0", src)
        for candidate in (hi, src):
            try:
                resp = await page.request.get(candidate, timeout=60_000)
                if resp.ok and len(await resp.body()) > 5_000:
                    dest.write_bytes(await resp.body())
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

    async def _one_job(self, page: Page, idx: int, prompt: str, dest: Path) -> bool:
        for attempt in range(1, self.max_retries + 1):
            await self.throttle.acquire()
            try:
                await self._new_chat(page)
                before = set(await self._image_urls(page))
                await self._send_prompt(page, prompt)
                await self._wait_for_generation(page)

                new = [u for u in await self._image_urls(page) if u not in before]
                if not new:
                    raise RuntimeError("Không thấy ảnh mới trong câu trả lời.")
                if not await self._download(page, new[-1], dest):
                    raise RuntimeError("Tải ảnh thất bại.")

                await self.throttle.on_ok()
                log.info("[tab %d] OK -> %s", idx, dest.name)
                return True

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

    async def run_jobs(self, jobs: list[tuple[str, str, Path]], on_done=None) -> dict:
        """jobs = [(key, prompt, dest)]. on_done(key, ok) gọi sau mỗi ảnh."""
        queue: asyncio.Queue = asyncio.Queue()
        for j in jobs:
            queue.put_nowait(j)
        results: dict[str, bool] = {}

        async def worker(idx: int):
            since_recycle = 0
            while True:
                try:
                    key, prompt, dest = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                page = self.pages[idx - 1]     # có thể đã bị thay bởi _recycle
                try:
                    ok = await self._one_job(page, idx, prompt, dest)
                    results[key] = ok
                    if on_done:
                        on_done(key, ok)
                finally:
                    queue.task_done()

                # Chỉ tái tạo SAU khi ảnh đã tải xong, không bao giờ giữa chừng.
                since_recycle += 1
                if self.recycle_every and since_recycle >= self.recycle_every:
                    since_recycle = 0
                    if not queue.empty():
                        try:
                            await self._recycle(idx)
                        except Exception as e:  # noqa: BLE001
                            log.warning("[tab %d] tái tạo lỗi: %s", idx, e)

                lo, hi = self.delay_range
                await asyncio.sleep(random.uniform(lo, hi))

        await asyncio.gather(*(worker(i) for i in range(1, len(self.pages) + 1)))
        return results
