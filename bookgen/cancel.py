"""
Quản lý trạng thái dừng (Cancel/Stop) chương trình.
"""

import threading

_cancel_event = threading.Event()


def request_cancel() -> None:
    """Gửi tín hiệu dừng tới tất cả các tiến trình đang chạy."""
    _cancel_event.set()


def reset_cancel() -> None:
    """Xóa tín hiệu dừng trước khi bắt đầu công việc mới."""
    _cancel_event.clear()


def is_canceled() -> bool:
    """Kiểm tra xem người dùng có yêu cầu dừng không."""
    return _cancel_event.is_set()


def check_cancel() -> None:
    """Kiểm tra và văng ngoại lệ InterruptedError nếu đã nhận lệnh dừng."""
    if _cancel_event.is_set():
        raise InterruptedError("⛔ ĐÃ DỪNG CHƯƠNG TRÌNH THEO YÊU CẦU CỦA NGƯỜI DÙNG!")
