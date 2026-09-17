"""Xuất sách -> CSV nhập sản phẩm crayonahub (schema hybrid in + digital).

KHÁC shopify_export.py: đây là schema riêng của crayonahub, 25 cột, và mỗi
cuốn bán DƯỚI DẠNG HYBRID - một sản phẩm gánh cả bản in lẫn bản PDF:

    Option1 Pages          : 24 / 48 Coloring Pages
    Option2 Choose format  : Printed / Printable
    -> 4 biến thể trên 1 trang bán hàng

Sàn phân biệt in hay digital KHÔNG bằng cột "Is Digital" (đó chỉ là nhãn cấp
sản phẩm) mà bằng CỘT NÀO ĐƯỢC ĐIỀN:

    Variant Design = cover.pdf|interior.pdf   -> file gửi xưởng in
    Variant File   = digital.pdf              -> file giao cho khách mua PDF

Mọi URL lấy từ manifest.json của cuốn (storage.get_manifest). KHÔNG tự ghép URL
từ slug: key trên R2 chứa sha256 nội dung file nên không đoán được, và sàn
FETCH THẬT các URL này lúc import - sai một ký tự là hỏng cả dòng.
"""
from __future__ import annotations

import csv
import io
import logging

from bookgen.shopify_export import body_html, gen_sku, slugify

log = logging.getLogger(__name__)

# Thứ tự cột PHẢI khớp template import của crayonahub.
COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Product Category", "Type", "Tags",
    "Is Digital", "Is Trademark",
    "Option1 Name", "Option1 Value", "Option2 Name", "Option2 Value",
    "Option3 Name", "Option3 Value",
    "Variant SKU", "Variant Price", "Variant Compare At Price",
    "Variant Price GBP", "Variant Compare At Price GBP",
    "Variant Price CAD", "Variant Compare At Price CAD",
    "Image Src", "Variant Image", "Variant File", "Variant Design",
]

OPT1_NAME = "Pages"
OPT2_NAME = "Choose your format"
FMT_PRINT = "Printed"
FMT_DIGITAL = "Printable"

# Danh mục: chuỗi NÀY đã được sàn chấp nhận thật (import xong sản phẩm trả về
# category_paths = ["Coloring Books & Pads"]). Đừng đổi sang chuỗi taxonomy
# kiểu "Media > Books" nếu chưa thử - sai danh mục thì sản phẩm kẹt ở draft.
PRODUCT_CATEGORY = "Coloring Books & Pads"
PRODUCT_TYPE = "Coloring Book"

# Bảng giá theo biến thể trong manifest. Bản in giữ nguyên giá đang bán; bản
# digital rẻ hơn vì không tốn in ấn lẫn vận chuyển.
PRICES = {
    "full": {
        FMT_PRINT: dict(sku="-48P", price="24.95", cmp="29.95",
                        gbp="18.46", gbp_cmp="22.16",
                        cad="34.93", cad_cmp="41.93"),
        FMT_DIGITAL: dict(sku="-48D", price="9.95", cmp="14.95",
                          gbp="7.95", gbp_cmp="11.95",
                          cad="13.95", cad_cmp="20.95"),
    },
    "24p": {
        FMT_PRINT: dict(sku="-24P", price="19.95", cmp="24.95",
                        gbp="14.76", gbp_cmp="18.46",
                        cad="27.93", cad_cmp="34.93"),
        FMT_DIGITAL: dict(sku="-24D", price="7.95", cmp="12.95",
                          gbp="5.95", gbp_cmp="9.95",
                          cad="10.95", cad_cmp="17.95"),
    },
}

# Thứ tự biến thể trên trang bán: bản 24 trang trước (giá thấp làm mồi).
VARIANT_ORDER = ["24p", "full"]


# Bảng cột Master Manifest (quản trị nội bộ / thông số in ấn Lulu POD).
# Liên kết 1-1 với products.csv qua "Handle" và "Variant SKU".
MANIFEST_COLUMNS = [
    "Handle", "Slug", "Variant SKU", "Title", "Cover Title",
    "Option1 Value", "Option2 Value",
    "Cover PDF URL", "Interior PDF URL", "Digital PDF URL",
    "Trim Size", "Spine (in)", "Cover Width (in)", "Cover Height (in)",
    "Total PDF Pages", "Lulu Package ID", "Audience", "Cover Style",
    "Cover Front Image", "Preview 1", "Preview 2", "Preview 3", "Preview 4", "Preview 5",
]


def _pages_label(vid: str, page_count: int | None, num_images: int | None) -> str:
    """Nhãn Option1: số TRANG TÔ MÀU, không phải số trang PDF (PDF còn trang
    trắng mặt sau nên gấp đôi)."""
    if vid == "24p":
        return "24 Coloring Pages"
    n = num_images or ((page_count or 96) // 2)
    return f"{n} Coloring Pages"


def _book_data(slug: str, book_main, storage) -> dict:
    """Gom dữ liệu 1 cuốn: manifest (URL) + state (tiêu đề, đối tượng)."""
    man = storage.get_manifest(slug)      # ném lỗi nếu cuốn chưa lên R2

    cfg = book_main.load_cfg(book_main.ROOT / "config.yaml")
    cfg["_book"] = slug
    book_main.set_current_book(slug)
    P = book_main.paths_of(cfg)
    state = book_main.load_state(P["state_file"])
    book = state.get("book", {}) or {}

    seo_title = (state.get("title") or book.get("title")
                 or man.get("title") or slug)
    cfg["book"]["title"] = seo_title
    if book.get("cover_title"):
        cfg["book"]["cover_title"] = book["cover_title"]

    return {
        "slug": slug,
        "manifest": man,
        "seo_title": seo_title,
        "cover_title": book_main.cover_title(cfg).title(),
        "audience": state.get("audience") or book.get("audience") or "kids",
        "cover_style": state.get("cover_style") or book.get("cover_style") or cfg.get("cover", {}).get("style") or "glossy",
        "num_images": book.get("num_images") or state.get("num_images"),
        "tags": book.get("tags") or "coloring book",
    }



def _rows_for(d: dict) -> list[dict]:
    man = d["manifest"]
    handle = slugify(d["seo_title"]) or d["slug"]
    base_sku = gen_sku(d["cover_title"], d["audience"], d["slug"])
    previews = (man.get("images") or {}).get("previews") or []

    by_id = {v["id"]: v for v in man.get("variants", [])}
    ordered = [by_id[v] for v in VARIANT_ORDER if v in by_id]
    if not ordered:
        raise ValueError(f"{d['slug']}: manifest không có biến thể nào.")

    def blank() -> dict:
        return {c: "" for c in COLUMNS}

    rows: list[dict] = []
    for v in ordered:
        vid = v["id"]
        # Ảnh đại diện MỌI biến thể = preview_1 (ảnh bìa): cùng một cuốn, chỉ
        # khác số trang -> ảnh giống nhau và preview_1 luôn dẫn đầu.
        vimg = previews[0] if previews else ""

        for fmt in (FMT_PRINT, FMT_DIGITAL):
            if fmt == FMT_DIGITAL and not v.get("digital_url"):
                # Sách dựng trước khi có build_digital: chỉ bán được bản in.
                log.warning("%s/%s: thiếu digital.pdf -> bỏ biến thể '%s'",
                            d["slug"], vid, fmt)
                continue
            spec = PRICES[vid][fmt]
            r = blank()
            r.update({
                "Handle": handle,
                "Option1 Value": _pages_label(vid, v.get("page_count"),
                                              d["num_images"]),
                "Option2 Value": fmt,
                "Variant SKU": f"{base_sku}{spec['sku']}",
                "Variant Price": spec["price"],
                "Variant Compare At Price": spec["cmp"],
                "Variant Price GBP": spec["gbp"],
                "Variant Compare At Price GBP": spec["gbp_cmp"],
                "Variant Price CAD": spec["cad"],
                "Variant Compare At Price CAD": spec["cad_cmp"],
                "Variant Image": vimg,
            })
            if fmt == FMT_PRINT:
                r["Variant Design"] = "|".join(
                    x for x in (v.get("cover_url"), v.get("interior_url")) if x)
            else:
                r["Variant File"] = v["digital_url"]
            rows.append(r)

    # Dòng đầu gánh toàn bộ field cấp sản phẩm + ảnh gallery thứ nhất.
    pages_pdf = ordered[-1].get("page_count") or 0
    rows[0].update({
        "Title": d["seo_title"],
        "Body (HTML)": body_html(d["num_images"] or max(pages_pdf // 2, 1)),
        "Product Category": PRODUCT_CATEGORY,
        "Type": PRODUCT_TYPE,
        "Tags": d["tags"],
        # Nhãn cấp sản phẩm, KHÔNG phải cờ quyết định biến thể nào là digital.
        "Is Digital": "FALSE",
        "Is Trademark": "FALSE",
        "Option1 Name": OPT1_NAME,
        "Option2 Name": OPT2_NAME,
        "Image Src": previews[0] if previews else "",
    })

    # Ảnh gallery còn lại: mỗi ảnh 1 dòng chỉ có Handle + Image Src. Cách này
    # đã được sàn chấp nhận (import xong image_urls có đủ 5 ảnh).
    for u in previews[1:]:
        r = blank()
        r.update({"Handle": handle, "Image Src": u})
        rows.append(r)
    return rows


def export_csv(slugs: list[str], book_main, storage) -> str:
    """Trả về nội dung CSV (string) cho danh sách slug."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for slug in slugs:
        try:
            for row in _rows_for(_book_data(slug, book_main, storage)):
                w.writerow(row)
        except Exception as e:  # noqa: BLE001 - 1 cuốn lỗi không chặn cả file
            log.warning("Bỏ qua %s khi export CSV crayonahub: %s", slug, e)
    return buf.getvalue()


def _manifest_rows_for(d: dict) -> list[dict]:
    """Tạo các dòng Master Manifest cho 1 cuốn sách.
    Khớp 1-1 với products.csv qua Handle và Variant SKU."""
    man = d["manifest"]
    handle = slugify(d["seo_title"]) or d["slug"]
    base_sku = gen_sku(d["cover_title"], d["audience"], d["slug"])
    previews = (man.get("images") or {}).get("previews") or []
    cover_front = (man.get("images") or {}).get("cover_front") or ""
    trim = f"{man.get('trim_width', 8.5)}x{man.get('trim_height', 11)} in"
    pkg_id = man.get("pod_package_id") or ""

    by_id = {v["id"]: v for v in man.get("variants", [])}
    ordered = [by_id[v] for v in VARIANT_ORDER if v in by_id]
    if not ordered:
        raise ValueError(f"{d['slug']}: manifest không có biến thể nào.")

    rows: list[dict] = []
    for v in ordered:
        vid = v["id"]
        for fmt in (FMT_PRINT, FMT_DIGITAL):
            if fmt == FMT_DIGITAL and not v.get("digital_url"):
                continue
            spec = PRICES[vid][fmt]
            sku = f"{base_sku}{spec['sku']}"
            opt1 = _pages_label(vid, v.get("page_count"), d["num_images"])
            is_print = (fmt == FMT_PRINT)

            rows.append({
                "Handle": handle,
                "Slug": d["slug"],
                "Variant SKU": sku,
                "Title": d["seo_title"],
                "Cover Title": d["cover_title"],
                "Option1 Value": opt1,
                "Option2 Value": fmt,
                "Cover PDF URL": v.get("cover_url", "") if is_print else "",
                "Interior PDF URL": v.get("interior_url", "") if is_print else "",
                "Digital PDF URL": v.get("digital_url", "") if not is_print else "",
                "Trim Size": trim,
                "Spine (in)": str(v.get("spine_in", "")) if is_print else "",
                "Cover Width (in)": str(v.get("cover_width_in", "")) if is_print else "",
                "Cover Height (in)": str(v.get("cover_height_in", "")) if is_print else "",
                "Total PDF Pages": str(v.get("page_count", "")) if is_print else str(v.get("digital_pages", "")),
                "Lulu Package ID": pkg_id if is_print else "",
                "Audience": d["audience"],
                "Cover Style": d.get("cover_style", "glossy"),
                "Cover Front Image": cover_front,
                "Preview 1": previews[0] if len(previews) > 0 else "",
                "Preview 2": previews[1] if len(previews) > 1 else "",
                "Preview 3": previews[2] if len(previews) > 2 else "",
                "Preview 4": previews[3] if len(previews) > 3 else "",
                "Preview 5": previews[4] if len(previews) > 4 else "",
            })
    return rows


def export_manifest_csv(slugs: list[str], book_main, storage) -> str:
    """Trả về nội dung CSV Master Manifest (string) cho danh sách slug."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for slug in slugs:
        try:
            for row in _manifest_rows_for(_book_data(slug, book_main, storage)):
                w.writerow(row)
        except Exception as e:  # noqa: BLE001 - 1 cuốn lỗi không chặn cả file
            log.warning("Bỏ qua %s khi export manifest CSV: %s", slug, e)
    return buf.getvalue()

