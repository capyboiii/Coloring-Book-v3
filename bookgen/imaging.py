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
    threshold: int = 200,
    pure_bw: bool = True,
) -> Path:
    """
    Chuyển ảnh Gemini thành line-art sạch, đúng tỉ lệ trang, đủ pixel cho 300 DPI.

    Các bước:
      1. Grayscale + tăng tương phản (bỏ nền xám nhạt)
      2. Threshold -> chỉ còn đen/trắng tuyệt đối (mực in sắc nét, file nhẹ)
      3. Upscale LANCZOS nếu ảnh nhỏ hơn yêu cầu
      4. Crop/pad về đúng tỉ lệ trang, nền trắng
    """
    img = Image.open(src)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("L")

    img = ImageOps.autocontrast(img, cutoff=(0, 2))

    if pure_bw:
        # làm mượt nhẹ trước khi threshold để viền bớt răng cưa
        img = img.filter(ImageFilter.MedianFilter(size=3))
        img = img.point(lambda p: 255 if p >= threshold else 0, mode="L")

    img = _fit_to_ratio(img, target_w_px, target_h_px)

    if img.width < target_w_px:
        img = img.resize((target_w_px, target_h_px), Image.LANCZOS)
        if pure_bw:
            img = img.point(lambda p: 255 if p >= 128 else 0, mode="L")

    dest.parent.mkdir(parents=True, exist_ok=True)
    img = img.convert("1" if pure_bw else "L")
    img.save(dest, "PNG", dpi=(300, 300), optimize=True)
    log.info("Xử lý xong %s -> %dx%d px", src.name, img.width, img.height)
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
