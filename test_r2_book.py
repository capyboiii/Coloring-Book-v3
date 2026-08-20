"""Test đẩy NGUYÊN MỘT CUỐN lên R2 rồi lấy về (HƯỚNG B: URL công khai, không presign).

Chạy:
    python test_r2_book.py                     # tự chọn cuốn đã có PDF
    python test_r2_book.py space-adventure     # chỉ định cuốn
    python test_r2_book.py space-adventure --cleanup   # test xong xoá sạch

Kiểm 4 việc:
  ① UPLOAD  : đẩy PDF (mọi biến thể, key SHA256) + ảnh preview. Manifest ghi CỤC BỘ.
  ② MANIFEST: đọc manifest.json cục bộ -> lấy interior_url (URL website sẽ dùng).
  ③ PDF     : tải interior_url TRỰC TIẾP qua cdn (không presign) -> so SHA.
  ④ PREVIEW : tải ảnh preview qua cdn -> so SHA.

Bucket dùng chung với website. --cleanup để xoá mọi thứ vừa đẩy sau khi test.
Không có cờ này thì GIỮ LẠI (đẩy thật).
"""

from __future__ import annotations

import sys
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bookgen.storage as storage

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def pick_book() -> str | None:
    for pdf in sorted((ROOT / "output" / "books").glob("*/03_pdf/interior.pdf")):
        return pdf.parent.parent.name
    return None


def fetch_public(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cleanup(manifest: dict) -> None:
    """Xoá PDF + ảnh khỏi R2, và manifest cục bộ."""
    cl = storage.client()
    keys: list[str] = []
    for v in manifest["variants"]:
        keys += [v["interior_key"], v["cover_key"]]
    imgs = manifest.get("images", {})
    base = storage.cfg()["public_base"]
    urls = ([imgs["cover_front"]] if imgs.get("cover_front") else []) + imgs.get("previews", [])
    for u in urls:
        if base and u.startswith(base + "/"):
            keys.append(u[len(base) + 1:])
    for k in keys:
        try:
            cl.delete_object(Bucket=storage.bucket_for(k), Key=k)
            print("   xoá R2 :", k)
        except Exception as e:  # noqa: BLE001
            print("   KHÔNG xoá được:", k, "->", e)
    mp = Path(manifest.get("manifest_path", ""))
    if mp.exists():
        mp.unlink()
        print("   xoá cục bộ:", mp)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_cleanup = "--cleanup" in sys.argv

    slug = args[0] if args else pick_book()
    if not slug:
        print("Không tìm thấy cuốn nào có PDF. Chạy build trước, hoặc truyền slug.")
        return 1

    c = storage.cfg()
    if not c["public_base"]:
        print("Chưa cấu hình R2_PUBLIC_BASE -> hướng B cần URL công khai. Dừng.")
        return 1

    print(f"Cuốn      : {slug}")
    print(f"Bucket    : {c['bucket']}   (PDF key SHA256 công khai - hướng B)")
    print(f"Chế độ    : {'TEST rồi xoá' if do_cleanup else 'ĐẨY THẬT (giữ lại)'}")
    print("=" * 64)

    base = ROOT / "output" / "books" / slug / "03_pdf"

    # ① UPLOAD
    try:
        manifest = storage.upload_book(slug)
        print("① UPLOAD                 : OK")
    except Exception as e:  # noqa: BLE001
        print("① UPLOAD                 : LỖI ->", e)
        return 1

    ok = True

    # ② MANIFEST cục bộ
    try:
        remote = storage.get_manifest(slug)
        good = remote.get("slug") == slug and remote["variants"]
        ok &= bool(good)
        print(f"② MANIFEST cục bộ        : {'OK' if good else 'SAI'}"
              f"  ({len(remote['variants'])} biến thể)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print("② MANIFEST cục bộ        : LỖI ->", e)

    # ③ PDF: tải interior_url TRỰC TIẾP (không presign) -> so SHA
    dl = ROOT / "r2-test-download" / slug
    for v in manifest["variants"]:
        sub = "" if v["id"] == "full" else "24-trang"
        local_pdf = base / sub / "interior.pdf"
        url = v.get("interior_url")
        try:
            data = fetch_public(url)
            good = sha_bytes(data) == storage.sha256_hex(local_pdf)
            ok &= good
            dest = dl / v["id"] / "interior.pdf"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            print(f"③ PDF [{v['id']:4}] tải TRỰC TIẾP : {'OK' if good else 'SAI'}"
                  f"  ({len(data)/1e6:.0f} MB, SHA {'khớp' if good else 'LỆCH'})")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"③ PDF [{v['id']:4}]            : LỖI -> {e}\n   URL: {url}")

    # ④ PREVIEW qua CDN
    prev_urls = manifest.get("images", {}).get("previews", [])
    if prev_urls:
        raw = ROOT / "output" / "books" / slug / "04_previews"
        u = prev_urls[0]
        localp = raw / Path(u).name
        try:
            data = fetch_public(u)
            good = (not localp.exists()) or sha_bytes(data) == storage.sha256_hex(localp)
            ok &= good
            print(f"④ PREVIEW qua CDN        : {'OK' if good else 'SAI'}  ({u})")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"④ PREVIEW qua CDN        : LỖI -> {e}")
    else:
        print("④ PREVIEW qua CDN        : bỏ qua (cuốn chưa có ảnh preview)")

    print("=" * 64)
    print("KẾT QUẢ:", "TẤT CẢ ĐẠT ✓" if ok else "CÓ BƯỚC SAI ✗")
    if do_cleanup:
        print("\nDọn dẹp (--cleanup):")
        cleanup(manifest)
    else:
        print(f"\nĐã đẩy thật. Manifest cục bộ: {manifest.get('manifest_path')}")
        print("URL website sẽ dùng (đưa cho họ nhập vào sản phẩm):")
        for v in manifest["variants"]:
            print(f"  [{v['id']}] {v.get('interior_url')}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
