"""Chuyển bản export printsyde -> CSV nhập sản phẩm crayonahub.

Hai định dạng KHÁC HẲN nhau, không phải đổi tên cột là xong:

  printsyde  : bản dump DB, 40 cột, MỖI DÒNG 1 VARIANT và mọi field cấp sản
               phẩm bị lặp lại y nguyên trên từng dòng.
  crayonahub : file import, 25 cột, field cấp sản phẩm CHỈ nằm ở dòng đầu của
               mỗi Handle; ảnh gallery thứ 2 trở đi phải tách thành dòng riêng
               chỉ có Handle + Image Src.

Mỗi sản phẩm printsyde 6 biến thể + 5 ảnh -> 10 dòng crayonahub
(6 dòng biến thể + 4 dòng ảnh).

Ngoài việc đổi khuôn, module còn xử 4 nhóm việc, tất cả đều rút ra từ việc soi
9291 sản phẩm thật chứ không phải phòng xa:

  1. SỬA DỮ LIỆU HỎNG  - mojibake 'â€¢', entity '&#34;', Markdown '**Design:**',
                         repr Python lọt vào mô tả, nhãn bọc nháy '"Quote:"'.
  2. DỰNG LẠI MÔ TẢ    - bóc nhãn nội bộ, chuẩn hoá nhãn đồng nghĩa, thêm khối
                         chọn kích thước, gắn emoji làm dấu đầu dòng.
  3. ĐỔI TÊN THUỘC TÍNH- 'Size'/'Paper' -> 'Roll format'/'Paper format' để
                         storefront dựng hàng NÚT thay vì dropdown (xem ATTR_RENAME).
  4. VÁ Ô CÒN TRỐNG    - Variant Image lấy tạm ảnh sản phẩm đầu tiên.
"""
from __future__ import annotations

import ast
import csv
import html
import io
import logging
import re

from bookgen.crayonahub_export import COLUMNS

log = logging.getLogger(__name__)

# Danh mục bên crayonahub. Printsyde ghi "Wrapping Paper" ở category_names,
# nhưng crayonahub gọi dòng sản phẩm này là "Gift Wrap" -> dùng tên nhà mới.
# Đổi được bằng ô nhập trên dashboard hoặc cờ -c của CLI.
DEFAULT_CATEGORY = "Gift Wrap"

# --------------------------------------------------------------- tên thuộc tính
#
# Storefront crayonahub dựng ô chọn thành HÀNG NÚT hay DROPDOWN tuỳ đúng một
# điều kiện, hard-code trong ProductCard của họ:
#
#     const Da = ["page", "format"];
#     function er(t) { return Da.some(s => t.name.toLowerCase().includes(s)); }
#
# Tên thuộc tính chứa chuỗi con "page" hoặc "format" -> hàng nút; còn lại rơi
# xuống nhánh mặc định là dropdown. Đã kiểm bằng cách import thử: cùng một sản
# phẩm, cùng danh mục, cùng dữ liệu, chỉ đổi mỗi tên thuộc tính thì kết quả lật
# hẳn. Danh mục, số biến thể, số giá trị, ảnh biến thể đều KHÔNG ảnh hưởng.
#
# Nên đổi tên khi xuất. Giữ từ khoá gốc (Roll, Paper) để khách vẫn hiểu đang
# chọn gì, chỉ thêm chữ "format" cho qua điều kiện trên.
#
# LƯU Ý: đây là LÁCH, không phải cách đúng. Code của họ đã có sẵn nhánh
# type === "radiobox" dựng ra nút, chỉ thiếu cột trong file import để đặt giá
# trị đó. Sàn đổi mảng ["page","format"] lúc nào là toàn bộ quay lại dropdown.
ATTR_RENAME = {
    "size": "Roll format",
    "paper": "Paper format",
}

# Đừng để 'print', 'digital', 'pdf' lọt vào GIÁ TRỊ thuộc tính: storefront bắt
# các chuỗi đó để bật ưu đãi printed/digital và chèn dòng phụ đề của sách.
_ATTR_OK = ("page", "format")


def rename_attr(name: str) -> str:
    """Tên thuộc tính -> tên khiến storefront dựng nút."""
    name = (name or "").strip()
    if not name:
        return ""
    low = name.lower()
    if low in ATTR_RENAME:
        return ATTR_RENAME[low]
    if any(k in low for k in _ATTR_OK):
        return name                      # đã hợp lệ sẵn
    return f"{name} format"              # tên lạ -> vẫn cho ra nút


# --------------------------------------------------------------- sửa chữ hỏng

def fix_text(s: str) -> str:
    """Sửa mojibake + giải mã entity HTML."""
    if not s:
        return ""
    # 'â€¢' -> '•'. Phải mã hoá ngược bằng CP1252 chứ KHÔNG phải latin-1: ký tự
    # như € (U+20AC) nằm trong CP1252 mà latin-1 không có, dùng latin-1 sẽ ném
    # UnicodeEncodeError và chuỗi giữ nguyên lỗi.
    if "â€" in s or "Ã" in s:
        for enc in ("cp1252", "latin-1"):
            try:
                s = s.encode(enc).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return html.unescape(s)


def strip_markdown(s: str) -> str:
    """Bỏ cú pháp in đậm Markdown lẫn trong nội dung.

    1108/9291 sản phẩm viết nhãn kiểu '**Design:** ...'. Dấu ** không có nghĩa
    trong HTML, để nguyên thì khách nhìn thấy đúng hai dấu sao.
    """
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    return s.replace("**", "").replace("__", "")


def unwrap_repr(text: str) -> str | None:
    """Bóc repr Python bị dump thẳng vào mô tả.

    6/9291 sản phẩm có khâu sinh nội dung bên printsyde serialize nhầm dict vào
    field thay vì render ra chữ, nên khách đọc được nguyên văn:

        {'paragraph': 'Wrap your special gifts...', 'bullets': ['Quote: "Love
        is patient..."', 'Design: Features a repeating pattern...']}

    Cấu trúc còn nguyên nên literal_eval bóc lại được. Không bóc được thì trả
    None để giữ nguyên - thà để nguyên còn hơn cắt bừa mất nội dung.
    """
    m = re.search(r"\{['\"]paragraph['\"]\s*:.*\}", text, re.S)
    if not m:
        return None
    try:
        d = ast.literal_eval(m.group(0))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(d, dict):
        return None

    lines = [str(d.get("paragraph") or "").strip()]
    for x in d.get("bullets") or []:
        if isinstance(x, dict):          # dạng {'label': 'Quote:', 'value': 'N/A'}
            lines.append(f"{x.get('label', '')} {x.get('value', '')}".strip())
        else:
            lines.append(str(x).strip())
    return "\n".join(ln for ln in lines if ln)


# --------------------------------------------------------------- nhãn trong mô tả
#
# Phần mô tả do một template sinh nội dung đẻ ra, và nó ĐỂ LỌT NHÃN NỘI BỘ ra
# mặt khách: 'Quote: None', 'Theme: Canine, Pet Lover', 'Key Features:'...
# Không phải nhãn nào cũng là rác - 'Design:', 'Color Palette:' đi kèm nội dung
# dùng được. Nên: giữ hết, chỉ xoá nhóm JUNK.
#
# KHÔNG dùng danh sách trắng: khảo sát 9291 sản phẩm thấy hơn 25 nhãn khác nhau
# (Color Palette 1356 lần, Ideal For 1253, Perfect For 1151, Versatile Use,
# Background, Material, Art Style...). Liệt kê tay kiểu gì cũng sót.

_BULLET = r"^[\s•\*\-·●▪>]+"

JUNK_LABELS = ("quote", "theme", "keywords", "tags", "key features",
               "key feature", "features")

_EMPTY_VALUES = {"", "none", "n/a", "na", "null", "-"}

# Cả catalog đang mỗi sản phẩm gọi một kiểu cho cùng một ý. Gom lại để đọc cả
# catalog thấy một giọng.
CANON = {
    "design": "Design", "pattern": "Design",
    "style": "Style", "art style": "Style", "aesthetic": "Style",
    "color palette": "Colors", "color scheme": "Colors", "colors": "Colors",
    "color": "Colors", "colours": "Colors", "background": "Colors",
    "background color": "Colors",
    "occasion": "Perfect for", "occasions": "Perfect for",
    "ideal for": "Perfect for", "perfect for": "Perfect for",
    "versatile": "Perfect for", "versatile use": "Perfect for",
    "versatile style": "Perfect for", "versatility": "Perfect for",
    "usage": "Perfect for",
}

# Nhãn trùng nội dung với bảng thông số bên dưới -> bỏ, khỏi nói hai lần.
DROP_LABELS = {"material", "finish", "quality", "high-quality", "high quality",
               "premium quality", "high-quality print", "print", "paper"}

LABEL_ORDER = ["Design", "Colors", "Style", "Perfect for"]

# Emoji làm DẤU ĐẦU DÒNG.
#
# Vì sao không dùng <ul><li>: theme crayonahub đặt list-style none và bỏ thụt
# lề ở trang gift wrap, nên <li> hiện ra y hệt dòng văn thường - dựng danh sách
# bao nhiêu cũng vô hình. Emoji là KÝ TỰ nên không CSS nào tắt được, và <p> cho
# khoảng cách giữa các dòng thật sự.
#
# Tránh emoji cờ (🇺🇸): Windows không dựng được, hiện ra hai chữ cái rời.
ICON = {"Design": "🎨", "Colors": "🌈", "Style": "✨", "Perfect for": "🎁"}
ICON_OTHER = "🔸"

SPEC_ICON = [("Wide Rolls", "📏"), ("High-Quality Material", "📄"),
             ("Tear-Resistant", "💪"), ("Stunning Single-Side Print", "🖨️"),
             ("Eco-Conscious Sourcing", "🌱"), ("Printed in the USA", "⭐")]

# Chữ hướng dẫn chọn cuộn - CHÍNH TAY viết, cố ý chung chung, không hứa hẹn gì
# về hoa văn. Đây và tiêu đề mục là chỗ DUY NHẤT code tự đặt chữ.
SIZE_HINT = {
    36: "a couple of small gifts",
    72: "a birthday's worth of boxes",
    180: "enough for the whole season",
}

# Khối chào hàng + thông số dùng CHUNG cho cả dòng sản phẩm, lấy nguyên văn từ
# 9284/9291 sản phẩm. 7 sản phẩm còn lại (nhóm lọt repr) không hề có khối này
# trong nguồn -> vá vào cho đồng bộ. Thông số là của cả dòng gift wrap nên áp
# cho chúng là đúng, không phải bịa.
BOILERPLATE_LINES = [
    ("📏", "Wide Rolls", 'Premium 30" wide rolls available in 3 lengths'),
    ("📄", "High-Quality Material",
     "Crafted from 90 GSM fine art paper, ensuring both elegance and durability."),
    ("💪", "Tear-Resistant",
     "Thick, robust construction makes our wrapping paper resistant to tears."),
    ("🖨️", "Stunning Single-Side Print",
     "Features vivid, high-definition prints on one side for eye-catching gift "
     "presentations."),
    ("🌱", "Eco-Conscious Sourcing", "Made using sustainably sourced paper."),
    ("⭐", "Printed in the USA",
     "Proudly designed and printed in the United States for supreme quality "
     "assurance."),
]
BLURB = ("Set your gifts apart with our beautifully crafted wrapping paper. "
         "Made to impress, our paper features exclusive designs and unmatched "
         "quality that ensure your presents are as delightful to wrap as they "
         "are to unwrap.")


def enrich_line(line: str) -> tuple[str, str]:
    """Một dòng thô -> (nhãn hiển thị, nội dung).

    Nhãn rỗng = câu văn thường. Nội dung rỗng = bỏ hẳn dòng đó.
    """
    txt = strip_markdown(re.sub(_BULLET, "", line)).strip()
    if not txt:
        return "", ""
    # 2/9291 sản phẩm bọc nhãn trong nháy kép: '"Quote:" N/A'.
    txt = re.sub(r'^"([^"]{1,28}:)"\s*', r"\1 ", txt)

    # Nhãn: tối đa ~4 từ, đứng đầu dòng, kết bằng dấu hai chấm. Chặn 'http' để
    # URL trong câu không bị hiểu là nhãn.
    m = re.match(r"([A-Za-z][A-Za-z'&/-]*(?:[ ][A-Za-z'&/-]+){0,3})\s*:\s*(.*)$",
                 txt)
    if not m or txt.lower().startswith("http"):
        return "", txt
    label, value = m.group(1).strip(), m.group(2).strip()
    low = label.lower()

    if not value:                        # 'Key Features:' đứng một mình
        return ("", "") if low in JUNK_LABELS else ("", txt)
    if value.strip(" .").lower() in _EMPTY_VALUES:   # 'Quote: N/A'
        return "", ""
    if low in JUNK_LABELS or low in DROP_LABELS:
        return "", ""
    return CANON.get(low, label), value


def _split_head_tail(body: str) -> tuple[str, str]:
    """Tách mô tả riêng của sản phẩm khỏi khối chào hàng + thông số chung.

    KHÔNG cắt ở "thẻ khối đầu tiên": 38/9291 sản phẩm viết mô tả bằng <ul><li>
    chứ không phải text trần, cắt kiểu đó thì cả phần mô tả rơi vào tail.
    Cũng KHÔNG dùng '90 GSM' làm mốc: cụm đó nằm cả trong câu mô tả của vài
    sản phẩm, khớp vào là cắt mất mô tả.
    """
    pos = -1
    for marker in ("Set your gifts apart", "Wide Rolls"):
        pos = body.find(marker)
        if pos >= 0:
            break
    if pos < 0:
        return body, ""
    starts = [m.start() for m in re.finditer(r"<(p|div|ul|ol)\b", body, re.I)
              if m.start() <= pos]
    cut = starts[-1] if starts else pos
    return body[:cut], body[cut:]


def _head_lines(head: str) -> list[str]:
    """Phần mô tả -> danh sách dòng, dù nguồn viết text trần hay <p>/<li>."""
    s = re.sub(r"</(p|li|div|h[1-6])\s*>", "\n", head, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return (unwrap_repr(s) or s).split("\n")


def _size_block(variants: list[dict]) -> str:
    """Khối chọn kích thước, dựng từ chính các dòng variant của sản phẩm.

    Mô tả gốc không hề nhắc tới kích thước - khách phải tự hiểu '30 inch x 180
    inch' là bao nhiêu. Quy ra feet và nói rõ cuộn nào gói được bao nhiêu quà.
    """
    seen: dict[int, float] = {}
    for v in variants:
        for n in (1, 2, 3):
            val = (v.get(f"option{n}_value") or "").strip()
            m = re.search(r"x\s*(\d+)\s*inch", val, re.I)
            if m:
                try:
                    seen[int(m.group(1))] = float(v.get("price_usd") or 0)
                except ValueError:
                    pass
    if len(seen) < 2:
        return ""

    # Cuộn rẻ nhất tính theo giá mỗi inch - tính riêng từng sản phẩm, vì có sản
    # phẩm bảng giá bị đảo.
    per_inch = {k: (p / k if k and p else float("inf")) for k, p in seen.items()}
    best = min(per_inch, key=per_inch.get)

    out = "<p><strong>🎀 Choose your roll</strong></p>"
    for inch in sorted(seen):
        hint = SIZE_HINT.get(inch, "")
        if inch == best:
            hint = f"{hint} · <strong>best value per foot</strong>" if hint \
                else "<strong>best value per foot</strong>"
        label = f'30" × {inch}" ({inch // 12} ft)'
        out += (f"<p>▸ <strong>{label}</strong> — {hint}</p>" if hint
                else f"<p>▸ <strong>{label}</strong></p>")
    return out + ("<p>Every length comes in <strong>Glossy</strong> for a bright "
                  "sheen, or <strong>Matte</strong> for a soft, non-reflective "
                  "finish.</p>")


def _spec_block(tail: str) -> str:
    """Khối chào hàng + thông số: bỏ <ul>/<li>, mỗi dòng một <p> + emoji."""
    if "Wide Rolls" not in tail:
        lines = BOILERPLATE_LINES
    else:
        lines = []
        for li in re.findall(r"<li>(.*?)</li>", tail, re.S | re.I):
            txt = re.sub(r"\s+", " ", li).strip()
            plain = re.sub(r"<[^>]+>", "", txt).strip()
            # Bỏ <li> thực chất chỉ là chữ tiêu đề (di chứng xử lý trước).
            if plain.lower() in ("product details", "choose your roll"):
                continue
            m = re.match(r"<strong>\s*(.*?)\s*</strong>\s*(.*)$", txt, re.S | re.I)
            if not m:
                if plain:
                    lines.append(("▸", "", plain))
                continue
            label = m.group(1).rstrip(":").strip()
            icon = next((ic for k, ic in SPEC_ICON if k.lower() in label.lower()),
                        "▸")
            lines.append((icon, label, m.group(2).strip()))

    out = f"<p>{BLURB}</p><p><strong>📋 Product details</strong></p>"
    for icon, label, text in lines:
        out += (f"<p>{icon} <strong>{label}:</strong> {text}</p>" if label
                else f"<p>{icon} {text}</p>")
    return out


def build_body(raw_body: str, variants: list[dict]) -> str:
    """body_html của printsyde -> Body (HTML) hoàn chỉnh cho crayonahub.

    Bố cục:
        <p>câu dẫn</p>
        <p>🎨 <strong>Design</strong> — ...</p>      (Design/Colors/Style/Perfect for)
        <p><strong>🎀 Choose your roll</strong></p>
        <p>▸ <strong>30" × 36" (3 ft)</strong> — ...</p>
        <p>đoạn chào hàng chung</p>
        <p><strong>📋 Product details</strong></p>
        <p>📏 <strong>Wide Rolls:</strong> ...</p>
    """
    body = fix_text(raw_body or "").strip()
    head, tail = _split_head_tail(body)

    items = [enrich_line(ln) for ln in _head_lines(head)]
    items = [(lb, tx) for lb, tx in items if tx]

    # Gộp nhãn đồng nghĩa (giữ lần xuất hiện đầu) rồi xếp theo thứ tự cố định.
    keep: dict[str, str] = {}
    extra: list[str] = []
    for label, text in items:
        if not label:
            extra.append(text)
        elif label not in keep:
            keep[label] = text
    ordered = [(k, keep[k]) for k in LABEL_ORDER if k in keep]
    ordered += [(k, v) for k, v in keep.items() if k not in LABEL_ORDER]

    # Câu đầu tiên không nhãn làm câu dẫn, phần còn lại xuống dòng đều nhau.
    lead = extra.pop(0) if extra else ""
    out = f"<p>{lead}</p>" if lead else ""
    for label, text in ordered:
        out += f"<p>{ICON.get(label, ICON_OTHER)} <strong>{label}</strong> — {text}</p>"
    for text in extra:
        out += f"<p>{ICON_OTHER} {text}</p>"

    out += _size_block(variants) + _spec_block(tail)

    # Khối thông số của nguồn ghi "2 lengths" ở 9290/9291 sản phẩm trong khi
    # sản phẩm nào cũng bán ĐỦ 3 độ dài. Sai sẵn trong dữ liệu gốc, nhưng đặt
    # ngay dưới bảng 3 cuộn thì mâu thuẫn lộ ra.
    out = re.sub(r"available in 2 lengths", "available in 3 lengths", out,
                 flags=re.I)
    # Gộp về MỘT dòng vật lý: nhiều bộ import đọc CSV theo từng dòng, body có
    # xuống dòng thật thì 1 bản ghi bị xé thành nhiều dòng và cả file hỏng.
    out = re.sub(r"\s+", " ", out).strip()
    return out.replace('30""', '30"')


# --------------------------------------------------------------- chuyển đổi

def _split_urls(cell: str) -> list[str]:
    return [u.strip() for u in (cell or "").split("|") if u.strip()]


def _bool(cell: str) -> str:
    """printsyde dùng 't'/'f'; crayonahub dùng TRUE/FALSE."""
    return "TRUE" if str(cell or "").strip().lower() in ("t", "true", "1") else "FALSE"


def convert(rows: list[dict], category: str = DEFAULT_CATEGORY) -> list[dict]:
    """Nhóm dòng theo sản phẩm rồi dựng các dòng crayonahub."""
    def blank() -> dict:
        return {c: "" for c in COLUMNS}

    # Gom theo product_id, GIỮ NGUYÊN thứ tự xuất hiện (variant_sort đã đúng).
    products: dict[str, list[dict]] = {}
    for r in rows:
        products.setdefault(r.get("product_id") or r.get("handle"), []).append(r)

    out: list[dict] = []
    for pid, vrows in products.items():
        head = vrows[0]
        handle = head.get("handle") or pid
        images = _split_urls(head.get("product_image_urls", ""))

        for i, r in enumerate(vrows):
            row = blank()
            row["Handle"] = handle
            for n in (1, 2, 3):
                row[f"Option{n} Value"] = (r.get(f"option{n}_value") or "").strip()
            row.update({
                "Variant SKU": r.get("variant_sku", ""),
                "Variant Price": r.get("price_usd", ""),
                "Variant Compare At Price": r.get("regular_price_usd", ""),
                "Variant Price GBP": r.get("price_gbp", ""),
                "Variant Compare At Price GBP": r.get("regular_price_gbp", ""),
                "Variant Price CAD": r.get("price_cad", ""),
                "Variant Compare At Price CAD": r.get("regular_price_cad", ""),
            })
            # Ảnh biến thể: printsyde để trống ở TOÀN BỘ sản phẩm. Lấy ảnh sản
            # phẩm đầu tiên - các cuộn chỉ khác độ dài và độ bóng chứ hoa văn y
            # hệt nhau, gán ảnh khác nhau cho từng cuộn là đánh lừa mắt khách.
            vi = _split_urls(r.get("variant_image_urls", ""))
            row["Variant Image"] = vi[0] if vi else (images[0] if images else "")
            row["Variant File"] = "|".join(_split_urls(r.get("variant_file_urls", "")))
            row["Variant Design"] = "|".join(
                _split_urls(r.get("variant_design_urls", "")))

            if i == 0:
                row.update({
                    "Title": fix_text(head.get("title", "")),
                    "Body (HTML)": build_body(head.get("body_html", ""), vrows),
                    "Product Category": category,
                    "Type": head.get("product_type", ""),
                    "Tags": fix_text(head.get("tags", "")),
                    # Nhãn cấp sản phẩm, KHÔNG phải cờ quyết định biến thể nào
                    # là digital - cột đó là Variant File.
                    "Is Digital": _bool(head.get("is_digital")),
                    "Is Trademark": _bool(head.get("is_trademark")),
                    "Image Src": images[0] if images else "",
                })
                for n in (1, 2, 3):
                    row[f"Option{n} Name"] = rename_attr(
                        head.get(f"option{n}_name") or "")
            out.append(row)

        # Ảnh gallery còn lại: mỗi ảnh 1 dòng chỉ có Handle + Image Src. Cách
        # này đã được sàn chấp nhận (import xong image_urls có đủ 5 ảnh).
        for u in images[1:]:
            row = blank()
            row.update({"Handle": handle, "Image Src": u})
            out.append(row)
    return out


def read_export(text: str) -> list[dict]:
    """Đọc bản export printsyde. Tự đoán dấu phân cách (tab hay phẩy)."""
    sample = text[:8192]
    delim = "\t" if sample.count("\t") > sample.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def to_csv(rows: list[dict], category: str = DEFAULT_CATEGORY) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(convert(rows, category))
    return buf.getvalue()


# --------------------------------------------------------------- chạy dòng lệnh

def _main(argv: list[str]) -> int:
    """python -m bookgen.printsyde_import <export.csv> [-o out.csv] [-c "Danh mục"]"""
    import argparse
    import sys
    from pathlib import Path

    # Console Windows mặc định cp1252, in tiếng Việt là UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - stream bị chuyển hướng thì bỏ qua
            pass

    ap = argparse.ArgumentParser(
        description="Chuyển bản export printsyde -> CSV nhập crayonahub.")
    ap.add_argument("src", help="file export printsyde (.csv hoặc .tsv)")
    ap.add_argument("-o", "--out", default="crayonahub-import.csv")
    ap.add_argument("-c", "--category", default=DEFAULT_CATEGORY,
                    help=f'Product Category (mặc định "{DEFAULT_CATEGORY}")')
    a = ap.parse_args(argv)

    # utf-8-sig: bản export có BOM thì cột đầu dính BOM vào tên -> hỏng việc
    # tra product_id.
    rows = read_export(Path(a.src).read_text(encoding="utf-8-sig"))
    out_rows = convert(rows, a.category)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(out_rows)

    # newline="" BẮT BUỘC: module csv đã tự ghi "\r\n" cuối dòng; để Python
    # dịch newline lần nữa thì trên Windows thành "\r\r\n" -> sau mỗi bản ghi
    # dôi ra một dòng trống. BOM ở đầu file để Excel đọc đúng UTF-8.
    with Path(a.out).open("w", newline="", encoding="utf-8") as fh:
        fh.write("﻿" + buf.getvalue())

    n_prod = len({r.get("product_id") or r.get("handle") for r in rows})
    no_design = sum(1 for r in rows
                    if not (r.get("variant_design_urls") or "").strip())
    attrs = sorted({rename_attr(r.get(f"option{n}_name") or "")
                    for r in rows for n in (1, 2, 3)} - {""})

    print(f"{a.src}: {len(rows)} biến thể / {n_prod} sản phẩm")
    print(f"-> {a.out}: {len(out_rows)} dòng, danh mục '{a.category}'")
    print(f"   Tên thuộc tính sau khi đổi: {', '.join(attrs)}")
    if no_design:
        print(f"   CẢNH BÁO: {no_design}/{len(rows)} biến thể KHÔNG có "
              f"variant_design_urls -> lên sàn bán được nhưng không có file "
              f"gửi xưởng in.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
