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

# --------------------------------------------------------------- loại đóng sách
#
# LỀ BAO CỦA BÌA KHÔNG PHẢI BLEED CỦA RUỘT. Bìa mềm chỉ cần 0.125" tràn lề, còn
# bìa cứng phải chừa 0.875" để gập vào mặt trong tấm bìa các-tông. Lấy nhầm là
# nhà in trả file ngay.
#
#   wrap      : lề bao FILE BÌA tính từ mép trim, mỗi cạnh
#   spine     : "calc"   = page_count * paper_thickness
#               "none"   = không có gáy
#               "manual" = nhà in cấp số (bìa cứng còn tính cả độ dày bìa các-tông,
#                          không suy ra được từ số trang) -> lấy print.spine_width
BINDINGS: dict[str, dict] = {
    "perfect":   {"label": "Bìa mềm, gáy keo (Perfect Bound)",
                  "wrap": 0.125, "spine": "calc",   "min_pages": 32},
    "coil":      {"label": "Bìa mềm, gáy xoắn (Coil)",
                  "wrap": 0.125, "spine": "none",   "min_pages": 2},
    "saddle":    {"label": "Bìa mềm, đóng ghim (Saddle Stitch)",
                  "wrap": 0.125, "spine": "none",   "min_pages": 4},
    "hardcover": {"label": "Bìa cứng (Case Wrap)",
                  "wrap": 0.875, "spine": "manual", "min_pages": 24},
    "linen":     {"label": "Bìa cứng, bìa vải lanh (Linen)",
                  "wrap": 0.875, "spine": "manual", "min_pages": 24},
}


def binding_spec(binding: str) -> dict:
    return BINDINGS.get((binding or "perfect").lower(), BINDINGS["perfect"])


def cover_text_safe_pct(p: dict) -> int:
    """% mép ảnh bìa mà CHỮ tuyệt đối không được lấn vào, tính theo loại đóng.

    Ảnh Gemini bị kéo lấp đầy cả ô trim + lề bao, nên phần bị nuốt phụ thuộc lề
    bao - mà lề bao lại khác nhau một trời một vực:

        bìa mềm  wrap 0.125" -> chữ phải cách mép ngoài  7.2%
        bìa cứng wrap 0.875" -> chữ phải cách mép ngoài 14.7%

    Viết chết 10% trong prompt là đúng cho bìa mềm nhưng THIẾU cho bìa cứng: tiêu
    đề nằm lọt vào phần gập và mất chữ. Vì vậy con số này phải do code tính rồi
    nhét vào prompt, không để người dùng tự nhớ.

    Lấy cạnh ngặt nhất, làm tròn lên, cộng 2 điểm đệm vì model chỉ ước lượng chứ
    không đo được chính xác.
    """
    import math

    spec = binding_spec(p.get("binding", "perfect"))
    wrap = float(spec["wrap"])
    safety = float(p.get("safety_margin", 0.5))
    trim_w = float(p["trim_width"])
    trim_h = float(p["trim_height"])

    panel_w = trim_w + wrap
    panel_h = trim_h + wrap * 2
    worst = max((wrap + safety) / panel_h, (wrap + safety) / panel_w) * 100
    return int(math.ceil(worst)) + 2


def cover_geometry(p: dict, page_count: int) -> dict:
    """Kích thước file bìa theo ĐÚNG loại đóng sách đã chọn.

    Kiểm chứng bằng chính trang tải lên của Lulu: trim 8.5x11, bìa vải lanh,
    gáy 0.5 -> đòi 19.25 x 12.75. Với wrap 0.875 thì
        ngang = 8.5*2 + 0.5 + 0.875*2 = 19.25
        dọc   = 11    +       0.875*2 = 12.75
    """
    spec = binding_spec(p.get("binding", "perfect"))
    trim_w = float(p["trim_width"])
    trim_h = float(p["trim_height"])
    wrap = float(spec["wrap"])

    mode = spec["spine"]
    manual = p.get("spine_width")
    if mode == "none":
        spine = 0.0
    elif mode == "manual":
        spine = float(manual) if manual else 0.0
        if not spine:
            # DỪNG HẲN chứ không đoán. Bản trước tạm tính theo số trang rồi chỉ ghi
            # cảnh báo - kết quả là file vẫn dựng ra nhưng LUÔN bị nhà in trả về, và
            # cảnh báo thì trôi mất giữa hàng trăm dòng log. Đo thật: 8.5x11 bìa cứng,
            # gáy tự tính 0.222" -> file rộng 18.972" trong khi Lulu đòi 19.25".
            guess = spine_width(page_count, float(p.get("paper_thickness", 0.002252)))
            raise ValueError(
                f"Bìa '{spec['label']}' bắt buộc phải có độ rộng gáy do nhà in cấp, "
                f"nhưng print.spine_width đang để trống.\n"
                f"Gáy bìa cứng KHÔNG suy ra được từ số trang vì còn cộng độ dày bìa "
                f"các-tông và bản lề (tính theo số trang chỉ ra {guess:.4f}\").\n"
                f"Hãy mở trang tải lên của nhà in, chép đúng con số 'Độ rộng gáy' và "
                f"nhập vào ô 'Độ rộng gáy do nhà in cấp' trên dashboard.")
    else:
        spine = (float(manual) if manual not in (None, "")
                 else spine_width(page_count, float(p.get("paper_thickness", 0.002252))))

    return {
        "spec": spec,
        "wrap": wrap,
        "spine": spine,
        "total_w": trim_w * 2 + spine + wrap * 2,
        "total_h": trim_h + wrap * 2,
    }


def spine_width(page_count: int, thickness: float = 0.002252) -> float:
    """Độ rộng gáy (inch). Chuẩn Lulu Paperback: page_count * thickness."""
    if page_count <= 0:
        return 0.0
    return page_count * thickness


def get_lulu_gutter(page_count: int, binding: str = "perfect") -> float:
    """Độ rộng lề Gutter cộng thêm phía gáy theo bảng chuẩn Lulu Creation Guide (Trang 9)."""
    if binding in ["coil", "saddle"]:
        return 0.0
    if page_count <= 60:
        return 0.0
    elif page_count <= 150:
        return 0.125
    elif page_count <= 400:
        return 0.5
    elif page_count <= 600:
        return 0.625
    else:
        return 0.75


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
            c.setFont(font, 15)
            c.drawCentredString(page_w / 2, page_h * 0.54, b["subtitle"])
        c.setFont(font, 12)
        c.drawCentredString(page_w / 2, page_h * 0.44, b["author"])
        c.showPage()
        pages += 1
    for _ in range(max(0, fm - 1)):
        blank_page()

    # ---- các trang hình ----
    safety_margin = float(p.get("safety_margin", 0.5))
    full_bleed = bool(p.get("full_bleed_interior", False))

    for img in images:
        c.setFillColor(white)
        c.rect(0, 0, page_w, page_h, stroke=0, fill=1)
        try:
            with Image.open(img) as im:
                iw, ih = im.size

            if full_bleed or safety_margin <= 0:
                # Tràn lề full-bleed: phủ kín cả trang + bleed (cover-fit)
                scale = max(page_w / iw, page_h / ih)
                w, h = iw * scale, ih * scale
            else:
                # Cách lề an toàn (margin-contain fit): tranh nằm gọn bên trong lề an toàn
                margin_pt = (bleed + safety_margin) * PT
                avail_w = max(0, page_w - 2 * margin_pt)
                avail_h = max(0, page_h - 2 * margin_pt)
                scale = min(avail_w / iw, avail_h / ih)
                w, h = iw * scale, ih * scale

            img_x = (page_w - w) / 2
            img_y = (page_h - h) / 2
            c.drawImage(
                ImageReader(str(img)),
                img_x,
                img_y,
                width=w,
                height=h,
                preserveAspectRatio=False,
                mask="auto",
            )

            # Vẽ viền đen nhỏ bo khung ảnh trang ruột nếu được bật
            draw_border = bool(p.get("interior_border", not full_bleed and safety_margin > 0))
            if draw_border:
                border_w = float(p.get("interior_border_width", 1.5))
                c.setStrokeColor(black)
                c.setLineWidth(border_w)
                c.rect(img_x, img_y, w, h, stroke=1, fill=0)
        except Exception as e:  # noqa: BLE001
            log.error("Không đặt được ảnh %s: %s", img, e)
        c.showPage()
        pages += 1

        if b.get("blank_verso", True):
            blank_page()

    # ---- đệm cho đủ bội số 2 và số trang tối thiểu của nhà in ----
    # Lulu bìa keo (perfect bound) yêu cầu tối thiểu 32 trang. Sách ít hình sẽ
    # bị đệm rất nhiều trang trắng ở cuối - đó KHÔNG phải bug, nhưng phải nói
    # rõ cho người dùng biết thay vì lặng lẽ nhét vào.
    default_min = binding_spec(p.get("binding", "perfect"))["min_pages"]
    min_pages = int(p.get("min_pages", default_min))

    content_pages = pages
    while pages < min_pages or pages % 2 != 0:
        blank_page()
    padded = pages - content_pages

    c.save()

    fm_blank = max(0, fm - 1)
    verso = len(images) if b.get("blank_verso", True) else 0
    total_blank = fm_blank + verso + padded

    log.info("Interior: %s (%d trang, %.2f x %.2f in)", out_pdf.name, pages,
             page_w / PT, page_h / PT)
    log.info("  Chi tiết: %d trang hình + %d trang trắng "
             "(%d mặt sau hình, %d front matter, %d đệm cho đủ %d trang)",
             len(images), total_blank, verso, fm_blank, padded, min_pages)

    if padded >= 4:
        need = (min_pages - content_pages + (2 if b.get("blank_verso", True)
                                             else 1) - 1) // (
            2 if b.get("blank_verso", True) else 1)
        log.warning("  Có %d trang trắng ĐỆM ở cuối vì sách chưa đủ %d trang. "
                    "Thêm khoảng %d hình nữa là hết. Hoặc đặt "
                    "print.min_pages thấp hơn / đổi binding nếu nhà in cho phép.",
                    padded, min_pages, max(1, need))
    if verso and len(images) >= 4:
        log.info("  %d trang trắng mặt sau là CỐ Ý (chống lộ màu). "
                 "Tắt bằng book.blank_verso: false -> sách còn %d trang.",
                 verso, pages - verso if pages - verso >= min_pages else min_pages)

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

    trim_w, trim_h = p["trim_width"], p["trim_height"]

    # Mọi kích thước bìa tra từ BINDINGS theo loại đóng sách - xem chú thích ở
    # đầu file. print.bleed KHÔNG dùng ở đây: đó là bleed của ruột.
    geo = cover_geometry(p, page_count)
    wrap, spine = geo["wrap"], geo["spine"]

    total_w = geo["total_w"] * PT
    total_h = geo["total_h"] * PT
    log.info("Bìa [%s]: %.3f x %.3f in (trim %.2fx%.2f, gáy %.4f, lề bao %.3f).",
             geo["spec"]["label"], geo["total_w"], geo["total_h"],
             trim_w, trim_h, spine, wrap)

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(out_pdf), pagesize=(total_w, total_h))
    c.setTitle(f"{b['title']} - Cover")
    font = _register_font()

    bl = wrap * PT
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

    # ---- gáy sách (Spine Column) ----
    if sp > 0:
        spine_color = HexColor(ct.get("spine_color", "#1E293B"))
        c.setFillColor(spine_color)
        c.rect(spine_x, 0, sp, total_h, stroke=0, fill=1)
        
        # Vẽ 2 đường biên nếp gấp gáy mỏng hai bên để gáy hiển thị rõ nét trên PDF
        c.setStrokeColor(HexColor("#475569"))
        c.setLineWidth(0.6)
        c.line(spine_x, 0, spine_x, total_h)
        c.line(spine_x + sp, 0, spine_x + sp, total_h)

        # Lulu: chỉ in chữ lên gáy khi gáy đủ rộng >= 0.25" (tương đương >=110 trang)
        if spine >= 0.25:
            c.saveState()
            c.translate(spine_x + sp / 2, total_h / 2)
            c.rotate(-90)
            c.setFillColor(white)
            c.setFont(font, min(14, sp * 0.55))
            c.drawCentredString(0, -min(14, sp * 0.55) * 0.35, b["title"])
            c.restoreState()

    # Chữ tiêu đề đã nằm sẵn 100% trong hình vẽ nghệ thuật của Gemini, không vẽ chèn thêm chữ bằng code Python nữa.

    # ---- chữ bìa sau ----
    bx = bl + tw / 2
    c.setFillColor(black)
    c.setFont(font, 14)
    y = total_h - bl - th * 0.30
    for line in ct.get("back_blurb", "").strip().splitlines():
        c.drawCentredString(bx, y, line.strip())
        y -= 20

    # ô trắng chừa chỗ barcode (Chuẩn Lulu Template: 3.622" x 1.26", cách mép 0.5")
    if ct.get("barcode", True):
        bw, bh = 3.622 * PT, 1.26 * PT
        margin = 0.5 * PT
        bxx = (bl + tw) - margin - bw
        # Đo từ mép TRIM chứ không từ mép tài liệu: với bìa cứng, wrap dày 0.875"
        # nên byy = margin sẽ đặt mã vạch nằm lọt trong phần gập và bị mất hẳn.
        byy = bl + margin
        c.setFillColor(white)
        c.setStrokeColor(HexColor("#CCCCCC"))
        c.rect(bxx, byy, bw, bh, stroke=1, fill=1)
        c.setFillColor(HexColor("#999999"))
        c.setFont(font, 9)
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
