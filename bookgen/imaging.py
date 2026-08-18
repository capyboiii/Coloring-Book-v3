"""Làm sạch ảnh line-art từ Gemini để đủ chuẩn in 300 DPI."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None


def upscale_to_min(path: Path, min_long_edge: int = 2000) -> Path:
    """Phóng ảnh lên cho cạnh dài đạt tối thiểu min_long_edge, ghi đè tại chỗ.

    Dùng cho ảnh preview marketing: Gemini trả về khoảng 800px, trong khi sàn
    thương mại điện tử cần cạnh dài từ 1600px trở lên thì mới bật được chức năng
    phóng to khi khách rê chuột.

    NÓI RÕ GIỚI HẠN: phóng to KHÔNG tạo thêm chi tiết. Việc này chỉ giúp ảnh đạt
    ngưỡng kỹ thuật của sàn và trông đỡ vỡ khi hiển thị lớn, chứ không làm ảnh
    nét hơn bản gốc. Nguồn gốc vấn đề vẫn là Gemini trả ảnh nhỏ.

    Ảnh đã đủ lớn thì giữ nguyên, không đụng vào.
    """
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            long_edge = max(w, h)
            if long_edge >= min_long_edge:
                return path

            scale = min_long_edge / long_edge
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            big = im.resize(new_size, Image.LANCZOS)
            # LANCZOS phóng xong hay bị mềm; unsharp nhẹ lấy lại cảm giác sắc nét.
            big = big.filter(ImageFilter.UnsharpMask(radius=2, percent=90,
                                                     threshold=3))
        big.save(path, "PNG", dpi=(300, 300), optimize=True)
        log.info("Nâng %s: %dx%d -> %dx%d (phóng %.1f lần).",
                 path.name, w, h, new_size[0], new_size[1], scale)
    except Exception as e:  # noqa: BLE001
        log.warning("Không nâng được độ phân giải %s: %s", path.name, e)
    return path


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


def _fill_blur(img: Image.Image, W: int, H: int) -> Image.Image:
    """Nền cho dải viền: phóng chính ảnh lấp kín khung rồi làm mờ mạnh.

    Kéo giãn một hàng pixel ra 0.8 inch tạo thành vệt sọc rất lộ. Nền mờ thì
    không sọc, không nhân đôi chi tiết, màu vẫn đúng, và mắt đọc nó như bóng đổ
    chứ không như tranh - hỏng cũng chỉ hỏng thành một vùng màu vô hại.
    """
    scale = max(W / img.width, H / img.height)
    big = img.resize((max(1, round(img.width * scale)),
                      max(1, round(img.height * scale))), Image.LANCZOS)
    left = (big.width - W) // 2
    top = (big.height - H) // 2
    bg = big.crop((left, top, left + W, top + H))
    return bg.filter(ImageFilter.GaussianBlur(radius=max(8, W // 60)))


def _inset_with_edge_fill(img: Image.Image, W: int, H: int, insets: dict,
                          mode: str = "blur") -> Image.Image:
    """Thu ảnh vào giữa rồi lấp viền bằng hàng pixel ngoài cùng kéo giãn ra.

    Vì sao kéo giãn chứ không để trắng hay soi gương: phần viền này chính là chỗ
    bị máy xén cắt bỏ (bìa mềm) hoặc gập vào mặt trong tấm bìa các-tông (bìa
    cứng) - gần như không ai nhìn thấy. Kéo giãn thì liền mạch với tranh, không
    đẻ ra chi tiết lạ như soi gương, và không để lộ khung trắng.
    """
    t = max(0, round(H * insets.get("top", 0)))
    b = max(0, round(H * insets.get("bottom", 0)))
    l = max(0, round(W * insets.get("left", 0)))
    r = max(0, round(W * insets.get("right", 0)))
    iw, ih = W - l - r, H - t - b
    if iw <= 0 or ih <= 0:                     # thu quá tay -> thà giữ nguyên
        return img.resize((W, H), Image.LANCZOS)

    art = img.resize((iw, ih), Image.LANCZOS)

    if mode == "blur":
        out = _fill_blur(img, W, H)
        out.paste(art, (l, t))
        return out

    out = Image.new("RGB", (W, H))
    out.paste(art, (l, t))
    if mode == "mirror":
        if t:
            out.paste(art.crop((0, 0, iw, t)).transpose(Image.FLIP_TOP_BOTTOM), (l, 0))
        if b:
            out.paste(art.crop((0, ih - b, iw, ih)).transpose(Image.FLIP_TOP_BOTTOM), (l, H - b))
        if l:
            out.paste(out.crop((l, 0, l + l, H)).transpose(Image.FLIP_LEFT_RIGHT), (0, 0))
        if r:
            out.paste(out.crop((W - r - r, 0, W - r, H)).transpose(Image.FLIP_LEFT_RIGHT), (W - r, 0))
        return out

    # mode == "stretch": kéo giãn hàng pixel ngoài cùng.
    # Trên/dưới trước, rồi trái/phải kéo trọn chiều cao -> bốn góc tự lấp đúng.
    if t:
        out.paste(art.crop((0, 0, iw, 1)).resize((iw, t), Image.NEAREST), (l, 0))
    if b:
        out.paste(art.crop((0, ih - 1, iw, ih)).resize((iw, b), Image.NEAREST), (l, H - b))
    if l:
        out.paste(out.crop((l, 0, l + 1, H)).resize((l, H), Image.NEAREST), (0, 0))
    if r:
        out.paste(out.crop((W - r - 1, 0, W - r, H)).resize((r, H), Image.NEAREST), (W - r, 0))
    return out


def prepare_cover_art(src: Path, dest: Path, w_px: int, h_px: int,
                      insets: dict | None = None, fill: str = "blur") -> Path:
    """Bìa: giữ màu, resize/crop, ép RGB 300 DPI.

    insets = tỉ lệ mỗi cạnh cần thu vào (pdf_builder.cover_art_insets). Có thì
    tranh được đẩy trọn vào vùng an toàn, không còn phụ thuộc việc model có chịu
    chừa lề hay không.
    """
    img = Image.open(src).convert("RGB")
    img = _fit_to_ratio(img, w_px, h_px)
    if insets:
        img = _inset_with_edge_fill(img, w_px, h_px, insets, fill)
        log.info("Thu bìa %s (%s): trên/dưới %.1f%%, trái %.1f%%, phải %.1f%%.",
                 src.name, fill, insets.get("top", 0) * 100,
                 insets.get("left", 0) * 100, insets.get("right", 0) * 100)
    else:
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
