"""Test upload ẢNH lên R2 rồi lấy về, kiểm tra toàn vẹn.

Chạy:
    python test_r2_image.py                          # tự chọn 1 ảnh có sẵn
    python test_r2_image.py "đường/dẫn/ảnh.png"      # chỉ định ảnh

Kiểm 3 việc:
  1. Upload lên prefix ảnh (public) trên R2.
  2. Lấy về QUA API (dùng khoá) -> so SHA, chắc chắn không hỏng byte nào.
  3. Lấy về QUA CDN công khai cdn.crayonahub.com (UA trình duyệt) -> so SHA,
     chứng minh khách xem được ảnh y hệt.

Đây là bucket DÙNG CHUNG với website, nên object test:
  - nằm trong prefix con "_selftest/" để không lẫn ảnh sản phẩm,
  - bị XOÁ trên R2 ngay sau khi test xong (không để rác trong bucket họ).
Bản tải về vẫn giữ lại trong ./r2-test-download/ để bạn mở xem tận mắt.
"""

from __future__ import annotations

import sys
import uuid
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bookgen.storage as storage

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def pick_default_image() -> Path | None:
    """Lấy đại một ảnh thật trong output/books để test."""
    for pat in ("*/01_raw/cover_front.png", "*/04_previews/preview_*.png",
                "*/01_raw/page_001.png"):
        found = sorted((ROOT / "output" / "books").glob(pat))
        if found:
            return found[0]
    return None


def fetch_public(url: str) -> bytes:
    """Tải qua CDN với UA trình duyệt (tránh bot-protection trả 403 giả)."""
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main() -> int:
    # 1) chọn ảnh
    if len(sys.argv) > 1:
        img = Path(sys.argv[1])
    else:
        img = pick_default_image()
    if not img or not img.exists():
        print("Không tìm thấy ảnh để test. Truyền đường dẫn: "
              "python test_r2_image.py <ảnh.png>")
        return 1

    c = storage.cfg()
    local_sha = storage.sha256_hex(img)
    size_kb = img.stat().st_size / 1024
    key = f"{c['prefix_img']}/_selftest/{uuid.uuid4().hex}{img.suffix.lower()}"

    print(f"Ảnh nguồn : {img}")
    print(f"  {size_kb:.0f} KB | SHA {local_sha[:16]}...")
    print(f"Bucket    : {c['bucket']}")
    print(f"Key test  : {key}")
    print(f"CDN base  : {c['public_base'] or '(chưa cấu hình)'}")
    print("-" * 60)

    cl = storage.client()
    ok = True

    # ------------------------------------------------ 2) UPLOAD
    try:
        storage.upload(img, key, meta={"purpose": "selftest"},
                       skip_if_exists=False)
        print("① UPLOAD lên R2                 : OK")
    except Exception as e:  # noqa: BLE001
        print("① UPLOAD lên R2                 : LỖI ->", e)
        return 1

    # ------------------------------------------------ 3) LẤY VỀ QUA API
    dl_dir = ROOT / "r2-test-download"
    dest = dl_dir / Path(key).name
    try:
        storage.download(key, dest)
        api_sha = storage.sha256_hex(dest)
        good = api_sha == local_sha
        ok &= good
        print(f"② LẤY VỀ qua API (khoá)         : {'OK' if good else 'SAI'}"
              f"  (SHA {'khớp' if good else 'LỆCH'})")
        print(f"   -> đã lưu: {dest}")
    except Exception as e:  # noqa: BLE001
        ok = False
        print("② LẤY VỀ qua API                : LỖI ->", e)

    # ------------------------------------------------ 4) LẤY VỀ QUA CDN CÔNG KHAI
    if c["public_base"]:
        url = storage.public_url(key)
        try:
            data = fetch_public(url)
            cdn_sha = _sha_bytes(data)
            good = cdn_sha == local_sha
            ok &= good
            print(f"③ LẤY VỀ qua CDN công khai      : {'OK' if good else 'SAI'}"
                  f"  (SHA {'khớp' if good else 'LỆCH'})")
            print(f"   -> mở thử trên trình duyệt: {url}")
        except urllib.error.HTTPError as e:
            ok = False
            print(f"③ LẤY VỀ qua CDN công khai      : HTTP {e.code}")
            print(f"   URL: {url}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print("③ LẤY VỀ qua CDN                : LỖI ->", e)
    else:
        print("③ LẤY VỀ qua CDN                : bỏ qua (chưa có R2_PUBLIC_BASE)")

    # ------------------------------------------------ 5) DỌN RÁC trên R2
    try:
        cl.delete_object(Bucket=c["bucket"], Key=key)
        print("④ XOÁ object test trên R2       : OK (không để rác trong bucket)")
    except Exception as e:  # noqa: BLE001
        print("④ XOÁ object test               : LỖI ->", e,
              "\n   >>> XOÁ TAY key này:", key)

    print("-" * 60)
    print("KẾT QUẢ:", "TẤT CẢ ĐẠT ✓" if ok else "CÓ BƯỚC SAI ✗")
    return 0 if ok else 1


def _sha_bytes(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
