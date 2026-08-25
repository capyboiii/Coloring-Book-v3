"""Xuất sách -> CSV nhập sản phẩm Shopify.

Một sách = 2 dòng variant (24 & 48 Coloring Pages) + N dòng ảnh preview,
gom bằng Handle. Dữ liệu lấy từ state.json của cuốn + URL ảnh public trên R2.

Quy tắc field (theo yêu cầu):
  * Handle          = slug(cover_title) + đuôi random
  * Title           = seo_title
  * Body (HTML)     = template thông số sách (khổ, số trang, đóng, giấy, bìa)
  * Product Category= Coloring Books & Pads (phải khớp danh mục hệ thống)
  * SEO Description = state.book.seo_description (Gemini viết) nếu có
  * Giá: theo VARIANTS_SPEC (USD/CAD/GBP + Compare At), cột variant khác để trống
  * SKU: gen_sku() + hậu tố -24P / -48P
"""
from __future__ import annotations

import csv
import glob
import hashlib
import io
import os
import re
import secrets
from pathlib import Path

# Thứ tự cột PHẢI khớp template import của Shopify.
COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Product Category", "Type",
    "Tags", "Published", "Option1 Name", "Option1 Value", "Option1 Linked To",
    "Option2 Name", "Option2 Value", "Option2 Linked To", "Option3 Name",
    "Option3 Value", "Option3 Linked To", "Variant SKU", "Variant Grams",
    "Variant Inventory Tracker", "Variant Inventory Qty",
    "Variant Inventory Policy", "Variant Fulfillment Service", "Variant Price",
    "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
    "Variant Barcode", "Image Src", "Image Position", "Image Alt Text",
    "Gift Card", "SEO Title", "SEO Description", "Variant Image",
    "Variant Weight Unit", "Variant Tax Code", "Cost per item", "Status",
    "Variant Price CAD", "Variant Price GBP", "Variant Compare At Price CAD",
    "Variant Compare At Price GBP", "Is Trademark",
]

# 2 biến thể mỗi cuốn: 24 & 48 trang, kèm bảng giá USD/CAD/GBP.
VARIANTS_SPEC = [
    {"name": "24 Coloring Pages", "sku_suffix": "-24P",
     "price": "19.95", "compare_at": "24.95",
     "price_cad": "27.93", "compare_at_cad": "34.93",
     "price_gbp": "14.76", "compare_at_gbp": "18.46"},
    {"name": "48 Coloring Pages", "sku_suffix": "-48P",
     "price": "24.95", "compare_at": "29.95",
     "price_cad": "34.93", "compare_at_cad": "41.93",
     "price_gbp": "18.46", "compare_at_gbp": "22.16"},
]

VENDOR = os.environ.get("SHOP_VENDOR", "Crayona Hub")

# Danh mục sản phẩm PHẢI khớp danh mục có sẵn trên hệ thống import,
# nếu không sản phẩm sẽ kẹt ở trạng thái draft (không publish được).
# Mỗi cuốn bốc ngẫu nhiên 1 trong các danh mục dưới đây.
PRODUCT_CATEGORIES = ["Coloring Books & Pads", "Arts & Crafts"]

_STOP = {"the", "a", "an", "and", "of", "for", "to", "coloring", "colouring",
         "book", "pages", "page"}


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "book"


def gen_handle(cover_title: str) -> str:
    """Handle = slug(cover_title) + đuôi random (tránh trùng khi re-import)."""
    return f"{slugify(cover_title)}-{secrets.token_hex(3)}"


def gen_sku(cover_title: str, audience: str, slug: str) -> str:
    """Cơ chế SKU: CB-<K/A>-<viết tắt tên>-<5 ký tự hash slug>.

    - CB          : coloring book (cố định).
    - K / A       : Kids / Adults.
    - viết tắt    : chữ cái đầu của tối đa 4 từ có nghĩa trong cover_title.
    - hash 5      : 5 ký tự đầu SHA1(slug) -> ỔN ĐỊNH (re-export ra cùng SKU),
                    đảm bảo không trùng giữa các cuốn.
    Ví dụ: "Cozy Christmas Animals" (kids) -> CB-K-CCA-3f9a1
    """
    aud = "A" if str(audience).lower().startswith("adult") else "K"
    words = [w for w in re.findall(r"[A-Za-z0-9]+", cover_title or "")
             if w.lower() not in _STOP]
    abbr = "".join(w[0] for w in words[:4]).upper() or "XX"
    h5 = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:5]
    return f"CB-{aud}-{abbr}-{h5}"


def body_html(page_count: int) -> str:
    """Mô tả sản phẩm cố định (thông số sách), chỉ số trang là động."""
    lines = [
        "<strong>Size:</strong> US Letter (8.5 x 11 in)",
        f"<strong>Page Count:</strong> {page_count} Coloring pages",
        "<strong>Binding:</strong> 📚 Perfect Bound – durable and sleek for everyday use",
        "<strong>Interior:</strong> 🖤 Standard Black &amp; White – clean, crisp printing for puzzles and activities",
        "<strong>Paper Type:</strong> ✉️ 60# White – Uncoated – smooth texture that works great with pencil, pen, or markers",
        "<strong>Cover Finish:</strong> ✨ Glossy – bright and protective, resists smudges and wear",
    ]
    return "".join(f"<p>{ln}</p>" for ln in lines)


# --------------------------------------------------------------- gom dữ liệu 1 cuốn
def _book_data(slug: str, book_main, storage) -> dict:
    root = Path(__file__).resolve().parent.parent
    cfg = book_main.load_cfg(root / "config.yaml")
    cfg["_book"] = slug
    book_main.set_current_book(slug)
    P = book_main.paths_of(cfg)
    state = book_main.load_state(P["state_file"])
    book = state.get("book", {}) or {}

    seo_title = state.get("title") or book.get("title") or cfg["book"]["title"]
    cfg["book"]["title"] = seo_title
    # cover_title: ưu tiên đã lưu, không thì để cover_title() tự cắt từ title.
    if book.get("cover_title"):
        cfg["book"]["cover_title"] = book["cover_title"]
    cover_title = book_main.cover_title(cfg).title()

    audience = state.get("audience") or book.get("audience") or "kids"
    seo_desc = book.get("seo_description", "")

    pages = len(glob.glob(P["raw_dir"].as_posix() + "/page_*.png")) \
        or int(state.get("num_images") or cfg["book"].get("num_images") or 48)

    # URL ảnh public trên R2 (chỉ có nếu đã upload / public_base đặt).
    base = storage.cfg()["public_base"]
    pfx = storage.cfg()["prefix_img"]
    cover_url = f"{base}/{pfx}/{slug}/cover_front.png" if base else ""
    prev_urls = []
    if base:
        prev_dir = P.get("preview_dir") or (P["raw_dir"].parent / "04_previews")
        # Preview đẩy R2 dưới dạng .webp -> URL trong CSV cũng .webp.
        prev_urls = [f"{base}/{pfx}/{slug}/" + Path(p).stem + ".webp"
                     for p in sorted(glob.glob(prev_dir.as_posix() + "/preview_*.png"))]

    return {
        "slug": slug, "cover_title": cover_title, "seo_title": seo_title,
        "audience": audience, "seo_description": seo_desc, "pages": pages,
        "cover_url": cover_url, "prev_urls": prev_urls,
    }


def _price_cells(spec: dict) -> dict:
    """Map cấu hình giá của 1 biến thể -> các cột giá trong CSV."""
    return {
        "Variant Price": spec["price"],
        "Variant Compare At Price": spec["compare_at"],
        "Variant Price CAD": spec["price_cad"],
        "Variant Price GBP": spec["price_gbp"],
        "Variant Compare At Price CAD": spec["compare_at_cad"],
        "Variant Compare At Price GBP": spec["compare_at_gbp"],
    }


def _rows_for(d: dict) -> list[dict]:
    """1 cuốn = 2 dòng variant (24 & 48 trang) + các dòng ảnh preview còn lại."""
    handle = slugify(d["seo_title"]) or gen_handle(d["cover_title"])
    base_sku = gen_sku(d["cover_title"], d["audience"], d["slug"])

    def blank() -> dict:
        return {c: "" for c in COLUMNS}

    # Ảnh sản phẩm: CHỈ 5 ảnh preview (không dùng cover_front).
    imgs = d["prev_urls"]
    # Ảnh đại diện từng biến thể: 24 trang -> preview_2, 48 trang -> preview_1.
    variant_imgs = [
        imgs[1] if len(imgs) > 1 else (imgs[0] if imgs else ""),
        imgs[0] if imgs else "",
    ]

    # Dòng 1: biến thể 24 trang, mang toàn bộ thông tin sản phẩm + ảnh vị trí 1.
    v1 = blank()
    v1.update({
        "Handle": handle,
        "Title": d["seo_title"],
        "Body (HTML)": body_html(d["pages"]),
        "Vendor": VENDOR,
        "Product Category": secrets.choice(PRODUCT_CATEGORIES),
        "Type": "",
        "Tags": "",
        "Published": "TRUE",
        "Option1 Name": "Pages",
        "Option1 Value": VARIANTS_SPEC[0]["name"],
        "Variant SKU": f"{base_sku}{VARIANTS_SPEC[0]['sku_suffix']}",
        "Image Src": imgs[0] if imgs else "",
        "Image Position": "1" if imgs else "",
        "Image Alt Text": f"{d['cover_title']} preview 1" if imgs else "",
        "Gift Card": "FALSE",
        "SEO Title": d["seo_title"],
        "SEO Description": d["seo_description"],
        "Variant Image": variant_imgs[0],
        "Status": "active",
        "Is Trademark": "FALSE",
    })
    v1.update(_price_cells(VARIANTS_SPEC[0]))

    # Dòng 2: biến thể 48 trang, chỉ Handle + Option + SKU + giá (+ ảnh vị trí 2).
    v2 = blank()
    v2.update({
        "Handle": handle,
        "Option1 Name": "Pages",
        "Option1 Value": VARIANTS_SPEC[1]["name"],
        "Variant SKU": f"{base_sku}{VARIANTS_SPEC[1]['sku_suffix']}",
        "Variant Image": variant_imgs[1],
    })
    v2.update(_price_cells(VARIANTS_SPEC[1]))
    if len(imgs) > 1:
        v2.update({
            "Image Src": imgs[1],
            "Image Position": "2",
            "Image Alt Text": f"{d['cover_title']} preview 2",
        })

    rows = [v1, v2]
    # Các ảnh preview còn lại: mỗi ảnh 1 dòng, chỉ Handle + Image*.
    for i, u in enumerate(imgs[2:], start=3):
        r = blank()
        r.update({"Handle": handle, "Image Src": u, "Image Position": str(i),
                  "Image Alt Text": f"{d['cover_title']} preview {i}"})
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
            import logging
            logging.getLogger("bookgen.shopify_export").warning(
                "Bỏ qua %s khi export CSV: %s", slug, e)
    return buf.getvalue()
