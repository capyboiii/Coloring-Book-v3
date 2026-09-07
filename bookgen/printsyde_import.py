"""Chuyển bản export printsyde -> CSV nhập sản phẩm crayonahub.

Hai định dạng KHÁC HẲN nhau, không phải đổi tên cột là xong:

  printsyde  : bản dump DB, 40 cột, MỖI DÒNG 1 VARIANT và mọi field cấp sản
               phẩm bị lặp lại y nguyên trên từng dòng.
  crayonahub : file import, 25 cột, field cấp sản phẩm CHỈ nằm ở dòng đầu của
               mỗi Handle; ảnh gallery thứ 2 trở đi phải tách thành dòng riêng
               chỉ có Handle + Image Src.

Mỗi sản phẩm printsyde 6 biến thể + 5 ảnh -> 10 dòng crayonahub
(6 dòng biến thể + 4 dòng ảnh).

Hai thứ PHẢI xử lý chứ không bê thẳng:
  * body_html dính mojibake: dấu • bị lưu thành 'â€¢' (UTF-8 đọc nhầm sang
    latin-1), kèm entity chưa giải mã như &#34;. Bê nguyên là mang rác sang
    nhà mới, sau đó phải sửa tay từng sản phẩm.
  * phần đầu body_html là text trần xuống dòng, không bọc thẻ -> sang HTML sẽ
    dính thành một khối chữ liền.
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
# nhưng crayonahub gọi dòng sản phẩm này là "Gift Wrap" -> dùng tên của nhà
# mới. Đổi được bằng ô nhập trên dashboard hoặc cờ -c của CLI, không cần sửa
# code (sai danh mục = sản phẩm kẹt draft hàng loạt).
DEFAULT_CATEGORY = "Gift Wrap"


def fix_text(s: str) -> str:
    """Sửa mojibake + giải mã entity HTML."""
    if not s:
        return ""
    # 'â€¢' -> '•'. Phải mã hoá ngược bằng CP1252 chứ KHÔNG phải latin-1: các
    # ký tự như € (U+20AC), ' (U+2019) nằm trong CP1252 mà latin-1 không có,
    # dùng latin-1 sẽ ném UnicodeEncodeError và chuỗi giữ nguyên lỗi. Thử
    # cp1252 trước, rớt về latin-1 cho phần còn lại.
    if "â€" in s or "Ã" in s or "â€™" in s:
        for enc in ("cp1252", "latin-1"):
            try:
                s = s.encode(enc).decode("utf-8")
                break
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return html.unescape(s)


# ------------------------------------------------------------------ enrich body
#
# Phần đầu body_html do một template sinh nội dung đẻ ra, và nó ĐỂ LỌT NHÃN
# NỘI BỘ ra mặt khách:
#
#     • Quote: None
#     * Theme: Canine, Pet Lover, Farm Dog
#     Key Features:
#     * Design: A repeating, watercolor-style portrait of a Border Collie...
#
# Không phải dòng nào cũng là rác. Chia hai nhóm:
#   * JUNK_LABELS : field trống / nhãn phân loại nội bộ -> XOÁ cả dòng.
#   * KEEP_LABELS : nhãn dán trước nội dung dùng được   -> BỎ NHÃN, giữ nội dung.
#
# Làm bằng regex chứ không gọi mô hình: chạy được cho cả nghìn sản phẩm, không
# tốn token, và quan trọng nhất là KHÔNG BỊA - chỉ cắt đi, không thêm chữ nào.

# Ký tự đầu dòng kiểu bullet mà template hay chèn.
_BULLET = r"^[\s•\*\-·●▪>]+"

# Nhãn xoá cả dòng: giá trị của chúng không bao giờ là câu bán hàng.
JUNK_LABELS = ("quote", "theme", "keywords", "tags", "key features",
               "key feature", "features")

# KHÔNG dùng danh sách trắng cho nhãn giữ lại. Khảo sát 9291 sản phẩm thấy có
# hơn 25 nhãn khác nhau - Color Palette (1356 lần), Ideal For (1253),
# Perfect For (1151), Versatile Use, Background, Material, Finish, Art Style...
# Liệt kê tay kiểu gì cũng sót. Nên: MỌI nhãn đều được giữ và in đậm, chỉ
# những nhãn trong JUNK_LABELS mới bị xoá.

# Giá trị rỗng mà template điền khi không có dữ liệu.
_EMPTY_VALUES = {"", "none", "n/a", "na", "null", "-"}


def strip_markdown(s: str) -> str:
    """Bỏ cú pháp in đậm Markdown lẫn trong nội dung.

    1108/9291 sản phẩm viết nhãn kiểu '**Design:** ...'. Dấu ** không có nghĩa
    gì trong HTML, để nguyên thì khách nhìn thấy đúng hai dấu sao.
    """
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    return s.replace("**", "").replace("__", "")


def enrich_line(line: str) -> tuple[str, str]:
    """Một dòng thô -> (nhãn hiển thị, nội dung).

    Nhãn rỗng = câu văn thường. Nội dung rỗng = bỏ hẳn dòng đó.
    Nhãn giữ NGUYÊN chữ hoa thường của nguồn: corpus đã viết sẵn 'Color
    Palette', 'Ideal For'... nên không phải chế lại, và cũng không sinh ra
    cảnh lắp bắp kiểu 'Perfect for — Perfect for birthdays'.
    """
    txt = strip_markdown(re.sub(_BULLET, "", line)).strip()
    if not txt:
        return "", ""
    # 2/9291 sản phẩm bọc nhãn trong nháy kép: '"Quote:" N/A'. Gỡ nháy để
    # bước nhận nhãn bên dưới bắt được.
    txt = re.sub(r'^"([^"]{1,28}:)"\s*', r"\1 ", txt)

    # Nhãn: tối đa ~4 từ, đứng đầu dòng, kết bằng dấu hai chấm. Chặn 'http'
    # để URL trong câu không bị hiểu là nhãn.
    m = re.match(r"([A-Za-z][A-Za-z'&/-]*(?:[ ][A-Za-z'&/-]+){0,3})\s*:\s*(.*)$",
                 txt)
    if not m or txt.lower().startswith(("http", "https")):
        return "", txt                  # câu thường, giữ nguyên
    label, value = m.group(1).strip(), m.group(2).strip()
    low = label.lower()

    # 'Key Features:' đứng một mình -> tiêu đề cụt, bỏ.
    if not value:
        return ("", "") if low in JUNK_LABELS else ("", txt)
    # Field trống kiểu 'Quote: N/A' -> bỏ, bất kể nhãn nào.
    if value.strip(" .").lower() in _EMPTY_VALUES:
        return "", ""
    if low in JUNK_LABELS:
        return "", ""
    return label, value


def _split_head_tail(body: str) -> tuple[str, str]:
    """Tách phần mô tả riêng của sản phẩm khỏi khối chào hàng + thông số chung.

    KHÔNG cắt ở "thẻ khối đầu tiên": 38/9291 sản phẩm viết phần mô tả bằng
    <ul><li> chứ không phải text trần, cắt kiểu đó thì cả phần mô tả rơi vào
    tail và không được xử lý - tệ hơn nữa là tiêu đề "Product details" bị chèn
    nhầm trước danh sách MÔ TẢ thay vì danh sách THÔNG SỐ.

    Neo vào chính khối chung: nó bắt đầu bằng <div>Set your gifts apart... ở
    9290/9291 sản phẩm. Không thấy thì tìm <ul> chứa thông số in ấn.
    """
    m = re.search(r"<div\b", body, re.I)
    if not m:
        m = re.search(r"<ul\b(?=[^<]*(?:<[^u/][^>]*>[^<]*)*Wide Rolls)", body, re.I)
    if not m:
        m = re.search(r"<(ul|ol|table|h[1-6])\b", body, re.I)
    return (body[:m.start()], body[m.start():]) if m else (body, "")


def _unwrap_repr(text: str) -> str | None:
    """Bóc repr Python bị dump thẳng vào mô tả.

    6/9291 sản phẩm có khâu sinh nội dung bên printsyde serialize nhầm dict vào
    field thay vì render ra chữ, nên khách đọc được nguyên văn:

        {'paragraph': 'Wrap your special gifts...', 'bullets': ['Quote: "Love
        is patient..."', 'Design: Features a repeating pattern...']}

    Cấu trúc còn nguyên nên literal_eval bóc lại được -> trả về text nhiều
    dòng cho _head_lines xử lý tiếp như mọi sản phẩm khác. Không bóc được thì
    trả None để giữ nguyên, thà để nguyên còn hơn cắt bừa mất nội dung.
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
        if isinstance(x, dict):         # dạng {'label': 'Quote:', 'value': 'N/A'}
            lines.append(f"{x.get('label', '')} {x.get('value', '')}".strip())
        else:
            lines.append(str(x).strip())
    return "\n".join(ln for ln in lines if ln)


def _head_lines(head: str) -> list[str]:
    """Phần mô tả -> danh sách dòng, dù nguồn viết bằng text trần hay <p>/<li>."""
    s = re.sub(r"</(p|li|div|h[1-6])\s*>", "\n", head, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)       # bỏ thẻ còn lại; enrich_line tự in đậm lại
    return (_unwrap_repr(s) or s).split("\n")


def _polish_tail(tail: str) -> str:
    """Làm đẹp phần HTML có sẵn: đoạn chào hàng chung + danh sách thông số.

    Hai việc, đều thuần trình bày, không đổi một chữ nội dung nào:
      * <div> -> <p>. Nhiều theme không đặt margin cho <div> trần nên đoạn văn
        đó dính sát vào danh sách bên dưới.
      * chèn tiêu đề nhỏ trước <ul> thông số, để khối kỹ thuật tách khỏi phần
        mô tả hoa văn thay vì trôi thành một mạch.
    """
    tail = re.sub(r"<div>(.*?)</div>", r"<p>\1</p>", tail, flags=re.S | re.I)
    # Dùng <p><strong> chứ KHÔNG dùng <h3>: cỡ chữ thẻ heading do theme của sàn
    # quyết định, có theme phóng to lấn át cả tiêu đề sản phẩm.
    tail = re.sub(r"<ul\b", "<p><strong>Product details</strong></p><ul",
                  tail, count=1, flags=re.I)
    return tail


def clean_body(body: str) -> str:
    """Chuẩn hoá + trình bày lại body_html.

    Bố cục ra:
        <p>câu dẫn</p>
        <ul><li><strong>Design</strong> — ...</li>
            <li><strong>Style</strong> — ...</li></ul>
        <p>đoạn chào hàng chung</p>
        <p><strong>Product details</strong></p>
        <ul>...thông số...</ul>

    Vì sao gom các dòng có nhãn thành danh sách: nguồn vốn đã có nhãn
    (Design/Style/Occasion), bỏ nhãn đi thì thành 4 đoạn văn ngang nhau, mắt
    không biết bám vào đâu. In đậm nhãn rồi xuống dòng thì quét mắt được ngay,
    và khớp với cách khối thông số bên dưới đang trình bày.
    """
    body = fix_text(body).strip()
    if not body:
        return ""
    head, tail = _split_head_tail(body)

    items = [enrich_line(ln) for ln in _head_lines(head)]
    items = [(lb, tx) for lb, tx in items if tx]

    # Đoạn ĐẦU làm câu dẫn, mọi đoạn sau gom vào MỘT danh sách - kể cả đoạn
    # không có nhãn. Trộn nửa đoạn văn nửa gạch đầu dòng trông lộn xộn hơn là
    # cho tất cả xuống dòng đều nhau, và cách này giữ đúng thứ tự nguồn.
    lead = items[0][1] if items and not items[0][0] else ""
    rest = items[1:] if lead else items

    out = f"<p>{lead}</p>" if lead else ""
    if rest:
        out += "<ul>" + "".join(
            (f"<li><strong>{lb}</strong> — {tx}</li>" if lb else f"<li>{tx}</li>")
            for lb, tx in rest
        ) + "</ul>"
    out += _polish_tail(tail)

    # Gộp về MỘT dòng vật lý. HTML không quan tâm khoảng trắng, nhưng nhiều bộ
    # import đọc CSV theo từng dòng: body có xuống dòng thật thì 1 bản ghi bị
    # xé thành nhiều dòng và cả file hỏng.
    return re.sub(r"\s*\n\s*", " ", out).strip()


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
            vi = _split_urls(r.get("variant_image_urls", ""))
            row["Variant Image"] = vi[0] if vi else ""
            row["Variant File"] = "|".join(_split_urls(r.get("variant_file_urls", "")))
            row["Variant Design"] = "|".join(
                _split_urls(r.get("variant_design_urls", "")))

            if i == 0:
                row.update({
                    "Title": fix_text(head.get("title", "")),
                    "Body (HTML)": clean_body(head.get("body_html", "")),
                    "Product Category": category,
                    "Type": head.get("product_type", ""),
                    "Tags": fix_text(head.get("tags", "")),
                    "Is Digital": _bool(head.get("is_digital")),
                    "Is Trademark": _bool(head.get("is_trademark")),
                    "Image Src": images[0] if images else "",
                })
                for n in (1, 2, 3):
                    row[f"Option{n} Name"] = (head.get(f"option{n}_name") or "").strip()
            out.append(row)

        # Ảnh gallery còn lại: mỗi ảnh 1 dòng chỉ có Handle + Image Src.
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

    from pathlib import Path
    # utf-8-sig: bản export có BOM thì cột đầu sẽ dính BOM vào tên -> hỏng
    # toàn bộ việc tra product_id.
    rows = read_export(Path(a.src).read_text(encoding="utf-8-sig"))
    out_rows = convert(rows, a.category)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    w.writerows(out_rows)
    out = buf.getvalue()

    n_prod = len({r.get("product_id") or r.get("handle") for r in rows})
    no_design = sum(1 for r in rows
                    if not (r.get("variant_design_urls") or "").strip())
    # BOM ở đầu file để Excel đọc đúng UTF-8.
    # newline="" B\u1eaeT BU\u1ed8C: module csv \u0111\u00e3 t\u1ef1 ghi "\r\n" cu\u1ed1i d\u00f2ng; \u0111\u1ec3 Python
    # d\u1ecbch newline l\u1ea7n n\u1eefa th\u00ec tr\u00ean Windows th\u00e0nh "\r\r\n" -> sau m\u1ed7i b\u1ea3n ghi
    # d\u00f4i ra m\u1ed9t d\u00f2ng tr\u1ed1ng, parser kh\u00f3 t\u00ednh \u0111\u1ecdc th\u00e0nh g\u1ea5p \u0111\u00f4i s\u1ed1 d\u00f2ng.
    with Path(a.out).open("w", newline="", encoding="utf-8") as fh:
        fh.write("\ufeff" + out)

    print(f"{a.src}: {len(rows)} biến thể / {n_prod} sản phẩm")
    print(f"-> {a.out}: {len(out_rows)} dòng, danh mục '{a.category}'")
    if no_design:
        print(f"CẢNH BÁO: {no_design}/{len(rows)} biến thể KHÔNG có "
              f"variant_design_urls -> lên sàn bán được nhưng không có file "
              f"gửi xưởng in.")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
