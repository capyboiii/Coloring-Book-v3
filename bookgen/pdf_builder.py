"""
Dựng PDF interior và cover full-wrap theo đúng đặc tả Lulu.

Đặc tả Lulu (paperback, perfect bound):
  INTERIOR
    - Nếu ảnh tràn lề (full bleed): page size = trim + 0.125" bleed MỖI CẠNH
      => 8.5 x 11  ->  8.75 x 11.25 in
    - Tổng số trang phải là bội số của 2; perfect bound tối thiểu 32 trang.
    - Chữ/chi tiết quan trọng phải nằm trong safety margin 0.5" tính từ trim.
  COVER (1 file duy nhất, trải phẳng: bìa sau | gáy | bìa trước)
    - width  = trim_w * 2 + spine + bleed * 2
    - height = trim_h + bleed * 2
    - spine  = số trang * độ dày giấy
    - Không để chữ trong vùng 0.0625" hai bên gáy nếu spine < 0.25"
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image
from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

log = logging.getLogger(__name__)

PT = 72.0  # 1 inch = 72 points


# ------------------------------------------------------------------ helpers

def spine_width(page_count: int, thickness: float) -> float:
    """Độ rộng gáy (inch). Lulu: trang < 32 thì không có gáy in được."""
    return page_count * thickness


def round_up_even(n: int) -> int:
    return n if n % 2 == 0 else n + 1


def _register_font() -> str:
    """Dùng font TTF nếu tìm thấy, không thì fallback Helvetica."""
    for name, path in [
        ("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("Arial-Bold", "C:/Windows/Fonts/arialbd.ttf"),
    ]:
        p = Path(path)
        if p.exists():
            try:
                pdfmetrics.registerFont(TTFont(name, str(p)))
                return name
            except Exception:
                continue
    return "Helvetica-Bold"


# ------------------------------------------------------------------ interior

def build_interior(
    images: list[Path],
    out_pdf: Path,
    cfg: dict,
) -> tuple[Path, int]:
    """Dựng PDF ruột sách. Trả về (đường dẫn, tổng số trang)."""
    p = cfg["print"]
    b = cfg["book"]

    trim_w, trim_h = p["trim_width"], p["trim_height"]
    bleed = p["bleed"]
    page_w = (trim_w + 2 * bleed) * PT
    page_h = (trim_h + 2 * bleed) * PT

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_pdf), pagesize=(page_w, page_h))
    c.setTitle(b["title"])
    c.setAuthor(b["author"])
    font = _register_font()

    pages = 0

    def blank_page():
        nonlocal pages
        c.setFillColor(white)
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
        c.showPage()
        pages += 1

    # ---- front matter: title page + copyright ----
    fm = b.get("front_matter_pages", 2)
    if fm >= 1:
        c.setFillColor(white)
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
        c.setFillColor(black)
        c.setFont(font, 30)
        c.drawCentredString(page_w / 2, page_h * 0.60, b["title"])
        if b.get("subtitle"):
            c.setFont("Helvetica", 15)
            c.drawCentredString(page_w / 2, page_h * 0.54, b["subtitle"])
        c.setFont("Helvetica", 12)
        c.drawCentredString(page_w / 2, page_h * 0.44, b["author"])
        c.showPage()
        pages += 1
    for _ in range(max(0, fm - 1)):
        blank_page()

    # ---- các trang hình ----
    for img in images:
        c.setFillColor(white)
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
        try:
            with Image.open(img) as im:
                iw, ih = im.size
            # phủ kín cả trang + bleed, giữ tỉ lệ (cover-fit)
            scale = max(page_w / iw, page_h / ih)
            w, h = iw * scale, ih * scale
            c.drawImage(
                ImageReader(str(img)),
                (page_w - w) / 2,
                (page_h - h) / 2,
                width=w,
                height=h,
                preserveAspectRatio=False,
                mask="auto",
            )
        except Exception as e:  # noqa: BLE001
            log.error("Không đặt được ảnh %s: %s", img, e)
        c.showPage()
        pages += 1

        if b.get("blank_verso", True):
            blank_page()

    # ---- đệm cho đủ bội số 2 và tối thiểu 32 trang (perfect bound) ----
    min_pages = 32 if p.get("binding", "perfect") == "perfect" else 2
    while pages < min_pages or pages % 2 != 0:
        blank_page()

    c.save()
    log.info("Interior: %s (%d trang, %.2f x %.2f in)", out_pdf.name, pages,
             page_w / PT, page_h / PT)
    return out_pdf, pages


# ------------------------------------------------------------------ cover

def build_cover(
    front_img: Path,
    back_img: Path,
    out_pdf: Path,
    cfg: dict,
    page_count: int,
) -> Path:
    """Dựng PDF bìa full-wrap: [bìa sau | gáy | bìa trước] + bleed."""
    p = cfg["print"]
    b = cfg["book"]
    ct = cfg.get("cover_text", {})

    trim_w, trim_h, bleed = p["trim_width"], p["trim_height"], p["bleed"]
    spine = spine_width(page_count, p["paper_thickness"])

    total_w = (trim_w * 2 + spine + bleed * 2) * PT
    total_h = (trim_h + bleed * 2) * PT

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_pdf), pagesize=(total_w, total_h))
    c.setTitle(f"{b['title']} - Cover")
    font = _register_font()

    bl = bleed * PT
    tw = trim_w * PT
    th = trim_h * PT
    sp = spine * PT

    # vùng bìa sau bắt đầu ở x=0 (gồm bleed), bìa trước kết thúc ở total_w
    back_x = 0.0
    spine_x = bl + tw
    front_x = spine_x + sp

    # ---- ảnh nền ----
    def place(img: Path, x: float, w: float):
        if not img or not Path(img).exists():
            c.setFillColor(HexColor("#F3E9D2"))
            c.rect(x, 0, w, total_h, stroke=0, fill=1)
            return
        c.drawImage(ImageReader(str(img)), x, 0, width=w, height=total_h,
                    preserveAspectRatio=False, mask="auto")

    place(back_img, back_x, bl + tw)             # bìa sau + bleed trái
    place(front_img, front_x, tw + bl)           # bìa trước + bleed phải

    # ---- gáy ----
    c.setFillColor(HexColor("#2B2B2B"))
    c.rect(spine_x, 0, sp, total_h, stroke=0, fill=1)
    # Lulu: chỉ in chữ lên gáy khi đủ ~0.25" (tương đương >=110 trang giấy 60#)
    if spine >= 0.25:
        c.saveState()
        c.translate(spine_x + sp / 2, total_h / 2)
        c.rotate(-90)
        c.setFillColor(white)
        c.setFont(font, min(14, sp * 0.55))
        c.drawCentredString(0, -min(14, sp * 0.55) * 0.35, b["title"])
        c.restoreState()

    # ---- chữ bìa trước ----
    cx = front_x + tw / 2
    c.setFillColor(HexColor(ct.get("title_color", "#FFFFFF")))
    c.setStrokeColor(HexColor(ct.get("stroke_color", "#2B2B2B")))
    c.setLineWidth(2.2)
    size = ct.get("title_font_size", 62)
    c.setFont(font, size)
    text_obj_y = total_h - bl - th * 0.16
    for line in _wrap(b["title"], 18):
        c.setFont(font, size)
        # vẽ viền + fill để chữ nổi trên ảnh
        c.saveState()
        from reportlab.pdfgen.textobject import PDFTextObject  # noqa: F401
        t = c.beginText()
        t.setTextRenderMode(2)  # fill + stroke
        w = c.stringWidth(line, font, size)
        t.setTextOrigin(cx - w / 2, text_obj_y)
        t.textOut(line)
        c.drawText(t)
        c.restoreState()
        text_obj_y -= size * 1.12

    if b.get("subtitle"):
        c.setFillColor(HexColor(ct.get("title_color", "#FFFFFF")))
        s = ct.get("subtitle_font_size", 26)
        c.setFont(font, s)
        t = c.beginText()
        t.setTextRenderMode(2)
        w = c.stringWidth(b["subtitle"], font, s)
        t.setTextOrigin(cx - w / 2, text_obj_y - 10)
        t.textOut(b["subtitle"])
        c.drawText(t)

    a = ct.get("author_font_size", 20)
    c.setFont(font, a)
    t = c.beginText()
    t.setTextRenderMode(2)
    w = c.stringWidth(b["author"], font, a)
    t.setTextOrigin(cx - w / 2, bl + th * 0.07)
    t.textOut(b["author"])
    c.drawText(t)

    # ---- chữ bìa sau ----
    bx = bl + tw / 2
    c.setFillColor(black)
    c.setFont("Helvetica", 14)
    y = total_h - bl - th * 0.30
    for line in ct.get("back_blurb", "").strip().splitlines():
        c.drawCentredString(bx, y, line.strip())
        y -= 20

    # ô trắng chừa chỗ barcode (Lulu yêu cầu 2.0 x 1.2 in, cách mép 0.25")
    if ct.get("barcode", True):
        bw, bh = 2.0 * PT, 1.2 * PT
        bxx = bl + tw - bw - 0.35 * PT
        byy = bl + 0.35 * PT
        c.setFillColor(white)
        c.setStrokeColor(HexColor("#CCCCCC"))
        c.rect(bxx, byy, bw, bh, stroke=1, fill=1)
        c.setFillColor(HexColor("#999999"))
        c.setFont("Helvetica", 8)
        c.drawCentredString(bxx + bw / 2, byy + bh / 2 - 3, "BARCODE AREA")

    c.showPage()
    c.save()
    log.info(
        "Cover: %s (%.3f x %.3f in, gáy %.4f in cho %d trang)",
        out_pdf.name, total_w / PT, total_h / PT, spine, page_count,
    )
    return out_pdf


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= width:
            cur = f"{cur} {w}".strip()
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines
