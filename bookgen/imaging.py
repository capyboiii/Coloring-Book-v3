"""Làm sạch ảnh line-art từ Gemini để đủ chuẩn in 300 DPI."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None


def process_lineart(
    src: Path,
    dest: Path,
    target_w_px: int,
    target_h_px: int,
    threshold: int = 165,
    pure_bw: bool = False,
    sharpen: bool = True,
) -> Path:
    """
    Chuyển ảnh Gemini thành line-art sạch, đúng tỉ lệ trang, đủ pixel cho in.

    THỨ TỰ LÀ THỨ QUAN TRỌNG NHẤT Ở ĐÂY.

    Bản cũ làm sai thứ tự và đó là lý do nét bị vỡ, chỗ dày chỗ mỏng:
        median blur -> threshold về đen/trắng -> RỒI MỚI phóng to -> threshold lần nữa
    Phóng to một ảnh đã chỉ còn đen và trắng thì không còn thông tin nào để nội
    suy: LANCZOS tạo ra viền xám răng cưa, rồi lần threshold thứ hai cắt phăng
    thành bậc thang. Nét mảnh (lông chó, cọng cỏ) đứt hẳn, nét đậm thì phình.
    MedianFilter còn ăn mất nét mảnh trước cả khi tới bước đó.

    Thứ tự đúng:
        1. Grayscale, ghép nền trắng nếu có alpha
        2. Crop về đúng tỉ lệ trang (vẫn đang là ảnh xám, còn đủ thông tin)
        3. PHÓNG TO ở dạng xám bằng LANCZOS - nội suy mượt trên dữ liệu thật
        4. Unsharp mask để lấy lại độ sắc bị mềm đi khi phóng to
        5. CHỈ threshold MỘT LẦN, ở kích thước cuối (nếu thật sự cần bitonal)

    pure_bw=False (mặc định mới): giữ ảnh xám 8-bit có khử răng cưa. Ảnh nguồn
    từ Gemini chỉ khoảng 1024px mà trang in cần 2625px, tức phóng 2.5 lần -
    ở mức đó, giữ xám cho nét mượt hơn hẳn so với ép về 1-bit.
    """
    img = Image.open(src)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("L")

    src_w = img.width
    # cutoff nhỏ thôi: cắt mạnh sẽ nuốt mất các nét xám nhạt
    img = ImageOps.autocontrast(img, cutoff=(0, 1))

    # 1) cắt về đúng tỉ lệ TRƯỚC khi phóng to
    img = _fit_to_ratio(img, target_w_px, target_h_px)

    # 2) phóng/thu về đúng kích thước, vẫn ở dạng xám
    if (img.width, img.height) != (target_w_px, target_h_px):
        img = img.resize((target_w_px, target_h_px), Image.LANCZOS)

    # 3) lấy lại độ sắc đã mất khi phóng to
    scale = target_w_px / max(1, src_w)
    if sharpen and scale > 1.2:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=110,
                                                 threshold=3))

    # 4) threshold MỘT lần duy nhất, ở kích thước cuối
    if pure_bw:
        img = img.point(lambda p: 255 if p >= threshold else 0, mode="L")
        img = img.convert("1")

    if scale > 2.0:
        log.warning("%s: ảnh gốc chỉ %dpx, phải phóng %.1f lần lên %dpx. "
                    "Phóng to không tạo thêm chi tiết - nét sẽ mềm. "
                    "Nên xin Gemini ảnh độ phân giải cao hơn.",
                    src.name, src_w, scale, target_w_px)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", dpi=(300, 300), optimize=True)
    log.info("Xử lý xong %s -> %dx%d px (%s)", src.name, img.width, img.height,
             "1-bit đen trắng" if pure_bw else "xám khử răng cưa")
    return dest


def _fit_to_ratio(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Crop giữa về đúng tỉ lệ tw:th (không bóp méo hình)."""
    want = tw / th
    have = img.width / img.height
    if abs(want - have) < 0.01:
        return img
    if have > want:  # ảnh quá rộng -> cắt hai bên
        new_w = int(img.height * want)
        left = (img.width - new_w) // 2
        return img.crop((left, 0, left + new_w, img.height))
    new_h = int(img.width / want)  # ảnh quá cao -> cắt trên dưới
    top = (img.height - new_h) // 2
    return img.crop((0, top, img.width, top + new_h))


def check_dpi(path: Path, w_in: float, h_in: float, min_dpi: int = 300) -> tuple[bool, float]:
    """Trả về (đạt_chuẩn, dpi_thực_tế) khi in ảnh này lên khổ w_in x h_in."""
    with Image.open(path) as im:
        dpi = min(im.width / w_in, im.height / h_in)
    return dpi >= min_dpi - 1, dpi


def prepare_cover_art(src: Path, dest: Path, w_px: int, h_px: int) -> Path:
    """Bìa: giữ màu, chỉ resize/crop và ép RGB 300 DPI."""
    img = Image.open(src).convert("RGB")
    img = _fit_to_ratio(img, w_px, h_px)
    img = img.resize((w_px, h_px), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", dpi=(300, 300))
    return dest
