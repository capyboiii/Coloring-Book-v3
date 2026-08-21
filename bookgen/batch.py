"""Chạy hàng loạt: nhập một danh sách chủ đề, tool tự dựng ra từng cuốn sách.

Kiến trúc 2 lane để pool Gemini không phải nằm chờ:

    lane GEMINI (1 luồng)   cuốn 1 gen ảnh -> cuốn 2 gen ảnh -> cuốn 3 ...
                                  |               |
                                  v               v
    lane CPU    (1 luồng)     process+build   process+build

Xong ảnh là bàn giao ngay sang lane CPU rồi quay lại gen cuốn kế tiếp, KHÔNG
chờ PDF dựng xong. Hai lane dùng hai tài nguyên khác nhau (quota Gemini vs CPU)
nên chạy đồng thời không cuốn nào chặn cuốn nào.

Config dùng CHUNG cho cả batch: chụp một bản config.yaml lúc bấm chạy, mỗi cuốn
chỉ khác title/slug/subjects. Đổi config.yaml giữa chừng không ảnh hưởng batch
đang chạy.

XỬ LÝ LỖI - module này KHÔNG sửa gì trong luồng gen đang chạy tốt, nó chỉ đứng
ngoài đếm file và quyết định:

  * Thiếu ảnh    -> KHÔNG dựng PDF. cmd_generate luôn trả về None dù hỏng bao
                    nhiêu ảnh, nên nếu cứ thế build thì cuốn thiếu 20/24 ảnh vẫn
                    ra PDF 32 trang toàn trang trắng và bị đánh dấu HOÀN TẤT.
  * Thiếu lẻ tẻ  -> chạy lại cmd_generate (nó tự resume, chỉ vẽ ảnh còn thiếu).
  * Chạy lại mà KHÔNG thêm được ảnh nào -> hỏng hệ thống (hết hạn mức, hết phiên
                    đăng nhập, mất mạng). Tạm dừng CẢ batch, giữ nguyên các cuốn
                    chưa làm để mai chạy tiếp - thay vì đốt hết 50 cuốn trong
                    vài giây, cuốn nào cũng rỗng.
  * Treo quá lâu -> đồng hồ canh giờ bắn cờ dừng, cuốn đó bị bỏ, batch đi tiếp.
"""

from __future__ import annotations

import copy
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("bookgen.batch")

# Trạng thái của một cuốn
QUEUED = "QUEUED"          # chờ tới lượt vào lane Gemini
GENERATING = "GENERATING"  # đang gen ảnh AI (bước 1)
BUILDING = "BUILDING"      # đang ở lane CPU: process + build (bước 2 + 3)
DONE = "DONE"
INCOMPLETE = "INCOMPLETE"  # thiếu ảnh -> CHƯA dựng PDF, chạy tiếp được
FAILED = "FAILED"          # lỗi thật khi xử lý/dựng PDF
STOPPED = "STOPPED"        # bị dừng trước khi tới lượt

# Cuốn còn làm tiếp được khi bấm "chạy tiếp"
RESUMABLE = (QUEUED, INCOMPLETE, STOPPED, GENERATING, BUILDING)

DEFAULT_TIMEOUT_MIN = 240  # trần thời gian gen một cuốn, chống treo vĩnh viễn


# --------------------------------------------------------------- log per-book

class _BookLogRouter(logging.Handler):
    """Ghi log của mỗi cuốn vào output/books/<slug>/run.log.

    Hai lane chạy song song nên log của chúng trộn vào nhau trên console. Handler
    này bám theo thread: mỗi lane khai báo mình đang làm cuốn nào, record phát ra
    từ thread đó được ghi vào đúng file cuốn ấy.
    """

    def __init__(self) -> None:
        super().__init__()
        self._ctx = threading.local()
        self._files: dict[str, Any] = {}
        self._lock = threading.Lock()
        self.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))

    def set_book(self, log_path: Path | None) -> None:
        self._ctx.path = log_path

    def emit(self, record: logging.LogRecord) -> None:
        path = getattr(self._ctx, "path", None)
        if path is None:
            return  # record không thuộc batch -> handler khác lo
        try:
            with self._lock:
                f = self._files.get(str(path))
                if f is None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    f = open(path, "a", encoding="utf-8")
                    self._files[str(path)] = f
                f.write(self.format(record) + "\n")
                f.flush()
        except Exception:
            pass  # log hỏng không được phép làm chết batch

    def close_all(self) -> None:
        with self._lock:
            for f in self._files.values():
                try:
                    f.close()
                except Exception:
                    pass
            self._files.clear()


_router = _BookLogRouter()


class _book_log:
    """with _book_log(path): ... -> mọi log trong khối này thuộc về cuốn đó."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "_book_log":
        _router.set_book(self.path)
        return self

    def __exit__(self, *exc: Any) -> None:
        _router.set_book(None)


# --------------------------------------------------------------- runner

class BatchRunner:
    """Một batch tại một thời điểm."""

    def __init__(self, root: Path, book_main: Any) -> None:
        self.root = root
        self.bm = book_main
        self.state_file = root / "output" / "batch.json"
        self._lock = threading.Lock()
        self._books: list[dict] = []
        self._running = False
        self._paused_reason: str | None = None
        self._stop = threading.Event()
        self._cpu_q: queue.Queue = queue.Queue()
        self._base_cfg: dict = {}
        self._started_at: float | None = None
        self._installed = False

    # ---------------------------------------------------------- trạng thái

    def _save(self) -> None:
        """Ghi batch.json. Gọi trong lock."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({
                "running": self._running,
                "started_at": self._started_at,
                "paused_reason": self._paused_reason,
                "config": self._base_cfg,   # để bấm "chạy tiếp" dùng đúng config cũ
                "books": self._books,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.warning("Không ghi được batch.json: %s", e)

    def _set(self, slug: str, **fields: Any) -> None:
        with self._lock:
            for b in self._books:
                if b["slug"] == slug:
                    b.update(fields)
                    break
            self._save()

    def status(self) -> dict:
        with self._lock:
            books = copy.deepcopy(self._books)
            running, started = self._running, self._started_at
            paused = self._paused_reason
        counts: dict[str, int] = {}
        for b in books:
            counts[b["status"]] = counts.get(b["status"], 0) + 1
        return {
            "running": running,
            "paused_reason": paused,
            "started_at": started,
            "books": books,
            "counts": counts,
            "total": len(books),
            "done": counts.get(DONE, 0),
            "failed": counts.get(FAILED, 0) + counts.get(INCOMPLETE, 0),
            "resumable": sum(1 for b in books if b["status"] in RESUMABLE),
        }

    def load_from_disk(self) -> None:
        """Nạp batch cũ để UI hiển thị sau khi restart server (không tự chạy)."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        with self._lock:
            if self._running:
                return
            self._books = data.get("books", [])
            self._started_at = data.get("started_at")
            self._paused_reason = data.get("paused_reason")
            self._base_cfg = data.get("config") or {}
            # Batch cũ chắc chắn không còn chạy: server đã restart. Cuốn đang dở
            # để là STOPPED - vẫn bấm "chạy tiếp" được, ảnh cũ giữ nguyên.
            for b in self._books:
                if b["status"] in (GENERATING, BUILDING, QUEUED):
                    b["status"] = STOPPED
            self._running = False

    # ---------------------------------------------------------- khởi động

    def start(self, titles: list[str], num_images: int | None = None) -> dict:
        with self._lock:
            if self._running:
                raise RuntimeError("Đang có batch chạy dở. Dừng nó trước đã.")

        titles = [t.strip() for t in titles if t and t.strip()]
        if not titles:
            raise ValueError("Danh sách chủ đề rỗng.")

        # Chụp config MỘT LẦN, dùng chung cho cả batch.
        base_cfg = self.bm.load_cfg(self.root / "config.yaml")
        if num_images:
            base_cfg.setdefault("book", {})["num_images"] = int(num_images)

        books = []
        used: set[str] = set()
        for title in titles:
            slug = self._unique_slug(title, used)
            used.add(slug)
            self._prepare_book(base_cfg, slug, title)
            books.append({
                "slug": slug, "title": title, "status": QUEUED,
                "error": None, "started_at": None, "finished_at": None,
                "images": 0, "expected": base_cfg["book"]["num_images"] + 2,
            })

        with self._lock:
            self._books = books
            self._base_cfg = base_cfg
            self._started_at = time.time()
            self._paused_reason = None

        log.info("[BATCH] Bắt đầu %d cuốn, %d ảnh/cuốn.",
                 len(books), base_cfg["book"]["num_images"])
        return self._launch()

    def resume(self) -> dict:
        """Chạy tiếp các cuốn chưa xong, dùng lại đúng config và slug cũ.

        Dùng sau khi hết hạn mức (chờ reset), hoặc sau khi restart server. Ảnh đã
        vẽ được giữ nguyên: cmd_generate tự bỏ qua ảnh đã có.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("Batch đang chạy.")
            if not self._books:
                raise ValueError("Chưa có batch nào để chạy tiếp.")
            if not self._base_cfg:
                # Batch tạo bởi bản cũ (chưa lưu snapshot config), hoặc batch.json
                # hỏng. Lấy tạm config.yaml hiện tại còn hơn từ chối chạy tiếp và
                # bắt người dùng vứt đi mấy chục cuốn đã gen dở.
                self._base_cfg = self.bm.load_cfg(self.root / "config.yaml")
                log.warning("[BATCH] Batch cũ không có snapshot config -> dùng "
                            "config.yaml hiện tại. Kiểm tra số ảnh/cuốn cho khớp.")
            todo = [b for b in self._books if b["status"] in RESUMABLE]
            if not todo:
                raise ValueError("Mọi cuốn đã xong, không còn gì để chạy tiếp.")
            for b in todo:
                b["status"] = QUEUED
                b["error"] = None
            self._paused_reason = None

        log.info("[BATCH] Chạy tiếp %d cuốn còn dở.", len(todo))
        return self._launch()

    def _launch(self) -> dict:
        with self._lock:
            self._running = True
            self._stop = threading.Event()
            self._cpu_q = queue.Queue()
            self._save()

        if not self._installed:
            logging.getLogger().addHandler(_router)
            self._installed = True

        for target, name in ((self._gemini_lane, "batch-gemini"),
                             (self._cpu_lane, "batch-cpu")):
            threading.Thread(target=target, name=name, daemon=True).start()
        return self.status()

    def stop(self) -> dict:
        """Dừng batch: không nhận cuốn mới, huỷ luôn cuốn đang gen ảnh.

        Lane CPU KHÔNG bị huỷ giữa chừng - process/build không đọc cờ cancel,
        nên cuốn nào đã đủ ảnh vẫn được dựng PDF cho trọn, mất thêm ~1 phút.
        """
        from bookgen.cancel import request_cancel

        self._stop.set()
        request_cancel()
        with self._lock:
            for b in self._books:
                if b["status"] == QUEUED:
                    b["status"] = STOPPED
            self._save()
        log.info("[BATCH] Đã yêu cầu dừng.")
        return self.status()

    # ---------------------------------------------------------- chuẩn bị cuốn

    def _unique_slug(self, title: str, used: set[str]) -> str:
        base = self.bm.slugify(title) or "untitled"
        slug, i = base, 2
        while slug in used or (self.bm.BOOKS_DIR / slug).exists():
            slug = f"{base}-{i}"
            i += 1
        return slug

    def _prepare_book(self, base_cfg: dict, slug: str, title: str) -> None:
        """Tạo thư mục + state.json cho một cuốn. Không đụng config.yaml."""
        cfg = self._cfg_for(base_cfg, slug, title)
        P = self.bm.paths_of(cfg)
        P["raw_dir"].mkdir(parents=True, exist_ok=True)

        state = self.bm.load_state(P["state_file"])
        state["title"] = title
        state["subtitle"] = ""
        state["num_images"] = cfg["book"]["num_images"]
        state["blank_verso"] = cfg["book"].get("blank_verso", True)
        state["book"] = cfg["book"]
        # Chủ đề để rỗng: cmd_generate sẽ tự nhờ Gemini nghĩ ra cho đúng title này.
        state.setdefault("subjects", [])
        state.setdefault("done", [])
        self.bm.save_state(P["state_file"], state)

    def _cfg_for(self, base_cfg: dict, slug: str, title: str) -> dict:
        cfg = copy.deepcopy(base_cfg)
        cfg["_book"] = slug
        cfg.setdefault("book", {})
        cfg["book"]["title"] = title
        cfg["book"]["subtitle"] = ""
        # QUAN TRỌNG: config.yaml còn giữ subjects của cuốn làm gần nhất. Không
        # xoá thì cmd_generate sẽ lấy lại đúng list đó cho MỌI cuốn trong batch.
        cfg["subjects"] = []
        return cfg

    def _profile_dirs(self) -> list[Path]:
        """Các profile Chrome gốc mà pool sẽ dùng (từ config.yaml)."""
        b = self._base_cfg.get("browser", {})
        profiles = b.get("profiles") or (
            [b["user_data_dir"]] if "user_data_dir" in b else [])
        return [Path(p).resolve() for p in profiles]

    def _wait_profiles_free(self, timeout: float = 90.0) -> None:
        """Đợi Chrome của cuốn trước nhả khoá profile trước khi phóng cuốn sau.

        Bản chất lỗi 'forest' hôm nay: cuốn kế khởi động chỉ vài giây sau khi
        cuốn trước gen xong, lúc Chrome (Windows) CHƯA nhả 'lockfile'. Pool thấy
        khoá -> tạo bản sao .chrome-account1-p1 KHÔNG mang phiên đăng nhập ->
        'Chưa đăng nhập Google' -> cả cuốn chết oan dù tài khoản vẫn tốt.

        Chờ tới khi mọi profile gốc rảnh, rồi cuốn sau chạy thẳng trên profile
        đã đăng nhập. Hết giờ chờ thì vẫn đi tiếp (pool tự nhân bản như cũ).
        """
        from bookgen.gemini_driver import is_profile_locked

        dirs = self._profile_dirs()
        if not dirs:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            locked = [d for d in dirs if d.exists() and is_profile_locked(d)]
            if not locked:
                return
            if self._stop.is_set():
                return
            log.info("[BATCH] Đợi Chrome nhả profile: %s",
                     ", ".join(d.name for d in locked))
            time.sleep(2.0)
        log.warning("[BATCH] Quá %.0fs profile vẫn bị giữ -> chạy tiếp, pool sẽ "
                    "tự nhân bản (có thể mất phiên đăng nhập).", timeout)

    # ---------------------------------------------------------- kiểm ảnh

    def _missing(self, cfg: dict) -> list[str]:
        """Ảnh còn thiếu của một cuốn. Đếm file thật, không tin state.json."""
        raw = self.bm.paths_of(cfg)["raw_dir"]
        n = int(cfg["book"]["num_images"])
        need = [f"page_{i:03d}" for i in range(1, n + 1)]
        need += ["cover_front", "cover_back"]
        return [k for k in need
                if not (raw / f"{k}.png").exists()
                or (raw / f"{k}.png").stat().st_size < 1024]

    def _timeout_sec(self) -> float:
        mins = (self._base_cfg.get("batch") or {}).get(
            "book_timeout_min", DEFAULT_TIMEOUT_MIN)
        return max(60.0, float(mins) * 60.0)

    def _generate_book(self, slug: str, cfg: dict, logp: Path) -> str:
        """Gen ảnh một cuốn, thử tối đa 2 lượt.

        Trả về: "complete" | "incomplete" | "systemic" | "stopped"
        """
        from bookgen.cancel import request_cancel, reset_cancel

        expected = len(self._missing(cfg)) or 1
        gained_total = 0

        for attempt in (1, 2):
            if self._stop.is_set():
                return "stopped"
            before = len(self._missing(cfg))
            reset_cancel()

            # Đợi Chrome của cuốn/lượt trước nhả profile đã đăng nhập.
            self._wait_profiles_free()
            if self._stop.is_set():
                return "stopped"

            # Đồng hồ canh giờ: cuốn nào treo quá lâu thì bắn cờ dừng để
            # cmd_generate tự văng ra, batch không đứng hình vĩnh viễn.
            timed_out = threading.Event()

            def _fire() -> None:
                timed_out.set()
                log.error("[BATCH] (%s) Quá %.0f phút -> cắt.",
                          slug, self._timeout_sec() / 60)
                request_cancel()

            watchdog = threading.Timer(self._timeout_sec(), _fire)
            watchdog.daemon = True
            watchdog.start()
            try:
                with _book_log(logp):
                    self.bm.cmd_generate(cfg)
            except InterruptedError:
                if not timed_out.is_set():
                    return "stopped"          # người dùng bấm Dừng
                log.warning("[BATCH] (%s) Bị cắt vì quá giờ.", slug)
            except Exception as e:  # noqa: BLE001
                log.exception("[BATCH] (%s) Lượt %d lỗi: %s", slug, attempt, e)
            finally:
                watchdog.cancel()

            missing = self._missing(cfg)
            gained = before - len(missing)
            gained_total += max(0, gained)
            self._set(slug, images=expected - len(missing), expected=expected)

            if not missing:
                return "complete"
            if self._stop.is_set():
                return "stopped"

            log.warning("[BATCH] (%s) Lượt %d: còn thiếu %d ảnh (vẽ thêm được %d).",
                        slug, attempt, len(missing), gained)

        # Hai lượt liền không vẽ thêm được ảnh nào -> không phải lỗi của cuốn này.
        # Hết hạn mức, hết phiên đăng nhập hay mất mạng đều rơi vào đây, và cả ba
        # đều có chung cách xử lý đúng: dừng lại, đừng đốt các cuốn còn lại.
        return "systemic" if gained_total == 0 else "incomplete"

    # ---------------------------------------------------------- lane GEMINI

    def _gemini_lane(self) -> None:
        try:
            for book in list(self._books):
                if self._stop.is_set():
                    break
                if book["status"] != QUEUED:
                    continue

                slug, title = book["slug"], book["title"]
                cfg = self._cfg_for(self._base_cfg, slug, title)
                logp = self.bm.BOOKS_DIR / slug / "run.log"

                self._set(slug, status=GENERATING, started_at=time.time())
                log.info("[BATCH] (%s) Bước 1: gen ảnh AI...", slug)

                outcome = self._generate_book(slug, cfg, logp)

                if outcome == "complete":
                    self._cpu_q.put((slug, cfg))
                    log.info("[BATCH] (%s) Đủ ảnh -> đẩy sang lane CPU.", slug)
                    continue

                if outcome == "stopped":
                    self._set(slug, status=STOPPED, finished_at=time.time())
                    log.info("[BATCH] (%s) Đã dừng.", slug)
                    break

                missing = len(self._missing(cfg))
                self._set(slug, status=INCOMPLETE, finished_at=time.time(),
                          error=f"thiếu {missing} ảnh, chưa dựng PDF")

                if outcome == "systemic":
                    # Giữ nguyên các cuốn còn lại ở QUEUED để bấm "chạy tiếp".
                    reason = (f"Dừng ở '{title}': chạy 2 lượt không vẽ thêm được "
                              f"ảnh nào (hết hạn mức / mất phiên đăng nhập / mất "
                              f"mạng). Các cuốn còn lại vẫn giữ nguyên.")
                    with self._lock:
                        self._paused_reason = reason
                        self._save()
                    log.error("[BATCH] %s", reason)
                    break

                log.warning("[BATCH] (%s) Thiếu ảnh -> BỎ QUA bước dựng PDF, "
                            "đi tiếp cuốn sau.", slug)
        except Exception:
            log.exception("[BATCH] Lane Gemini chết bất ngờ.")
        finally:
            self._cpu_q.put(None)  # báo lane CPU dừng sau khi làm hết hàng đợi

    # ---------------------------------------------------------- lane CPU

    def _cpu_lane(self) -> None:
        while True:
            item = self._cpu_q.get()
            if item is None:
                break
            slug, cfg = item
            logp = self.bm.BOOKS_DIR / slug / "run.log"
            self._set(slug, status=BUILDING)
            try:
                with _book_log(logp):
                    log.info("[BATCH] (%s) Bước 2: xử lý 300 DPI...", slug)
                    self.bm.cmd_process(cfg)
                    log.info("[BATCH] (%s) Bước 3: dựng PDF Lulu...", slug)
                    self.bm.cmd_build(cfg)   # đã gồm check ở đuôi
                self._set(slug, status=DONE, finished_at=time.time())
                log.info("[BATCH] (%s) HOÀN TẤT.", slug)
            except Exception as e:
                self._set(slug, status=FAILED, error=f"dựng PDF: {e}",
                          finished_at=time.time())
                log.exception("[BATCH] (%s) Dựng PDF lỗi.", slug)

        with self._lock:
            self._running = False
            self._save()
        _router.close_all()
        log.info("[BATCH] Kết thúc. %s", self.status()["counts"])
