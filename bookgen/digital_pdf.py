"""Dựng bản DIGITAL (khách tải về) từ interior.pdf đã dựng cho nhà in.

Vì sao không bán thẳng interior.pdf: nó là file gửi xưởng in, mang hai thứ chỉ
có nghĩa với nhà in mà khách tải về sẽ thấy như lỗi:

  * Trang trắng xen kẽ. blank_verso chèn 1 trang trắng sau MỖI hình để bút lông
    không thấm sang mặt sau khi in hai mặt. Bản digital in một mặt -> khách bấm
    Print ra một nửa là giấy trắng. Sách 48 hình = 51 tờ trắng.
  * Khổ 8.75 x 11.25 (đã cộng bleed 0.125 mỗi cạnh). Máy in gia đình không in
    tràn lề, khổ này ép Acrobat co lại còn 97% hoặc xén mép.

Bố cục bản digital:
    trang 1      : bìa trước (ảnh màu)
    trang 2 -> N : các trang tô màu, KHÔNG trang trắng
    trang cuối   : bìa sau (ảnh màu)

Mọi trang dùng chung của bản in đều bị BỎ (belongs to, color test, thank you):
khách mua file PDF chỉ cần tranh để in, hai bìa màu đóng khung hai đầu là đủ.

Cách làm: cắt lại từ interior.pdf chứ không dựng lại từ ảnh. Nhờ vậy chạy được
cả với sách CŨ (chỉ cần còn interior.pdf) và không đụng vào pdf_builder.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

log = logging.getLogger(__name__)

PT = 72.0


def _has_image(page) -> bool:
    """Trang có ảnh không. Trang trắng do blank_page() vẽ chỉ có 1 hình chữ
    nhật trắng, không nhúng XObject nào -> đây là dấu hiệu đáng tin nhất."""
    res = page.get("/Resources")
    if res is None:
        return False
    if hasattr(res, "get_object"):
        res = res.get_object()
    xo = res.get("/XObject")
    if xo is None:
        return False
    if hasattr(xo, "get_object"):
        xo = xo.get_object()
    return len(xo) > 0


def _cover_page(cover_img: Path, trim_w: float, trim_h: float) -> PdfReader:
    """Dựng 1 trang PDF khổ trim, ảnh bìa phủ kín (cover-fit, xén phần thừa)."""
    buf = io.BytesIO()
    pw, ph = trim_w * PT, trim_h * PT
    c = canvas.Canvas(buf, pagesize=(pw, ph))
    from PIL import Image
    # Bìa là ảnh MÀU: nhúng thẳng PNG thì reportlab nén Flate không ăn thua,
    # một trang bìa phình lên hơn 20 MB - to hơn cả phần ruột. Ép sang JPEG.
    # Chỉ áp cho BÌA; trang tô màu tuyệt đối không JPEG vì nhiễu quanh nét vẽ
    # làm công cụ đổ màu (tô trên iPad) bị lem.
    with Image.open(cover_img) as im:
        im = im.convert("RGB")
        iw, ih = im.size
        jpg = io.BytesIO()
        im.save(jpg, "JPEG", quality=85, optimize=True, progressive=True)
    jpg.seek(0)
    scale = max(pw / iw, ph / ih)          # phủ kín, thừa đâu xén đó
    w, h = iw * scale, ih * scale
    c.drawImage(ImageReader(jpg), (pw - w) / 2, (ph - h) / 2,
                width=w, height=h, preserveAspectRatio=False)
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


def build_digital(
    interior_pdf: Path,
    cover_img: Path | None,
    out_pdf: Path,
    cfg: dict,
    drop_positions: set[int] | None = None,
    back_cover_img: Path | None = None,
) -> tuple[Path, int]:
    """Trả về (đường dẫn, số trang).

    drop_positions: vị trí (đếm từ 0) trong DÃY TRANG CÓ HÌNH cần bỏ đi - dùng
    để loại trang color test. Người gọi tính giúp vì chỉ main.py mới biết thứ
    tự các trang dùng chung.
    """
    p = cfg["print"]
    trim_w = float(p.get("trim_width", 8.5))
    trim_h = float(p.get("trim_height", 11.0))
    bleed = float(p.get("bleed", 0.125))
    drop = drop_positions or set()

    reader = PdfReader(str(interior_pdf))
    writer = PdfWriter()

    if cover_img and Path(cover_img).exists():
        writer.add_page(_cover_page(Path(cover_img), trim_w, trim_h).pages[0])
    else:
        log.warning("Không có ảnh bìa trước -> bản digital thiếu trang 1.")

    kept = blanks = 0
    for page in reader.pages:
        if not _has_image(page):
            blanks += 1
            continue
        pos = kept          # vị trí trong dãy trang CÓ HÌNH, đếm từ 0
        kept += 1
        if pos in drop:
            continue

        # Xén bleed: đưa mediabox về đúng khổ trim, canh giữa. Tranh đã nằm
        # trong safety margin 0.6" nên xén 0.125" mỗi cạnh không chạm nét vẽ.
        box = page.mediabox
        x0 = float(box.left) + bleed * PT
        y0 = float(box.bottom) + bleed * PT
        page.mediabox = RectangleObject((x0, y0, x0 + trim_w * PT,
                                         y0 + trim_h * PT))
        page.cropbox = RectangleObject((x0, y0, x0 + trim_w * PT,
                                        y0 + trim_h * PT))
        writer.add_page(page)

    if back_cover_img and Path(back_cover_img).exists():
        writer.add_page(_cover_page(Path(back_cover_img), trim_w, trim_h).pages[0])
    else:
        log.warning("Không có ảnh bìa sau -> bản digital thiếu trang cuối.")

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with out_pdf.open("wb") as f:
        writer.write(f)

    n = len(writer.pages)
    log.info("Digital: %s (%d trang, %.2f x %.2f in) - bỏ %d trang trắng"
             "%s", out_pdf.name, n, trim_w, trim_h, blanks,
             f", bỏ {len(drop)} trang dùng chung" if drop else "")
    return out_pdf, n
