"""Làm sạch ảnh line-art từ Gemini để đủ chuẩn in 300 DPI."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None


def crop_white_margins(img: Image.Image, padding_px: int = 10) -> Image.Image:
    """Tự động xén bỏ các lề trắng thừa xung quanh hình vẽ nét để nét vẽ mở rộng chạm sát viền."""
    try:
        gray = img.convert("L")
        bw = gray.point(lambda p: 255 if p < 245 else 0)
        bbox = bw.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            left = max(0, left - padding_px)
            top = max(0, top - padding_px)
            right = min(img.width, right + padding_px)
            bottom = min(img.height, bottom + padding_px)
            return img.crop((left, top, right, bottom))
    except Exception as e:
        log.debug("Lỗi crop_white_margins: %s", e)
    return img


def process_lineart(
    src: Path,
    dest: Path,
    target_w_px: int,
    target_h_px: int,
    threshold: int = 165,
    pure_bw: bool = False,
    sharpen: bool = True,
) -> Path:
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

    # 0) xén bỏ lề trắng thừa xung quanh để nét vẽ mở rộng chạm viền
    img = crop_white_margins(img, padding_px=10)

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


def render_titled_cover(src_cover: Path, dest_cover: Path, title: str = "", subtitle: str = "", author: str = "") -> Path:
    """Ảnh bìa đã được Gemini tự vẽ sẵn chữ tiêu đề trong prompt. Hàm này chỉ sao chép file tới dest_cover."""
    if not src_cover.exists():
        return src_cover
    try:
        if src_cover.resolve() != dest_cover.resolve():
            import shutil
            dest_cover.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_cover, dest_cover)
            return dest_cover
    except Exception as e:
        log.debug("Lỗi copy cover: %s", e)
    return src_cover
