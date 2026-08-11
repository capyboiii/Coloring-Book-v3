"""
Sinh ảnh bằng Gemini API chính thức (generativelanguage.googleapis.com).

Vì sao có file này:
    Giao diện web gemini.google.com trả về "I seem to be encountering an error"
    cho MỌI yêu cầu sinh ảnh trên một số tài khoản (text vẫn chạy bình thường).
    Đó là giới hạn phía Google, không phải bug selector/nút Send.
    Đường API ổn định hơn nhiều và không cần Playwright/Chrome.

Lấy API key miễn phí: https://aistudio.google.com/apikey
Đặt biến môi trường trước khi chạy:
    Windows PowerShell:  $env:GEMINI_API_KEY = "AIza..."
    Windows CMD:         set GEMINI_API_KEY=AIza...
    Linux/macOS:         export GEMINI_API_KEY=AIza...

Driver này có cùng bộ API công khai với GeminiDriver:
    with GeminiApiDriver(cfg) as g:
        g.generate_image(prompt, dest)
        g.ask_text(prompt)
        g._sleep_jitter()
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiApiError(RuntimeError):
    pass


class GeminiApiDriver:
    """Gọi thẳng REST API. Không cần trình duyệt."""

    def __init__(self, cfg: dict):
        api = cfg.get("api") or {}
        self.key = os.environ.get(api.get("key_env", "GEMINI_API_KEY"), "").strip()
        if not self.key:
            raise GeminiApiError(
                "Chưa có API key. Lấy key ở https://aistudio.google.com/apikey "
                "rồi đặt biến môi trường GEMINI_API_KEY."
            )
        self.image_model = api.get("image_model", "gemini-2.5-flash-image")
        self.text_model = api.get("text_model", "gemini-2.5-flash")
        self.timeout = int(api.get("timeout", 180))
        self.max_retries = int(cfg.get("browser", {}).get("max_retries", 3))
        self.delay_range = api.get("delay_between_prompts", [2, 5])

    # ---------- lifecycle (giữ cùng interface với GeminiDriver) ----------

    def __enter__(self) -> "GeminiApiDriver":
        log.info("Dùng Gemini API, model ảnh: %s", self.image_model)
        return self

    def __exit__(self, *exc):
        return False

    def new_chat(self) -> None:
        """API không có ngữ cảnh giữa các lần gọi -> không cần làm gì."""

    def _sleep_jitter(self) -> None:
        lo, hi = self.delay_range
        t = random.uniform(lo, hi)
        log.info("Nghỉ %.1fs trước prompt tiếp theo...", t)
        time.sleep(t)

    # ---------- lõi ----------

    def _post(self, model: str, payload: dict) -> dict:
        url = f"{BASE}/{model}:generateContent"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            raise GeminiApiError(f"HTTP {e.code}: {body}") from e

    @staticmethod
    def _parts(resp: dict) -> list[dict]:
        try:
            return resp["candidates"][0]["content"]["parts"]
        except (KeyError, IndexError):
            fb = resp.get("promptFeedback") or resp.get("error") or resp
            raise GeminiApiError(f"Câu trả lời không có nội dung: {fb}") from None

    # ---------- API chính ----------

    def generate_image(self, prompt: str, dest: Path) -> Path | None:
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._post(self.image_model, payload)
                for part in self._parts(resp):
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(base64.b64decode(inline["data"]))
                        log.info("OK -> %s (%d KB)", dest.name,
                                 dest.stat().st_size // 1024)
                        return dest
                raise GeminiApiError("Câu trả lời không chứa ảnh.")
            except Exception as e:  # noqa: BLE001
                log.warning("Lần %d/%d thất bại: %s", attempt, self.max_retries, e)
                if attempt < self.max_retries:
                    time.sleep(random.uniform(5, 15))
        return None

    def ask_text(self, prompt: str) -> str:
        resp = self._post(self.text_model, {"contents": [{"parts": [{"text": prompt}]}]})
        return "".join(p.get("text", "") for p in self._parts(resp))
