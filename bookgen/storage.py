"""Đẩy sách lên Cloudflare R2 và lấy lại.

Ranh giới trách nhiệm (xem các trao đổi trước):
  * PDF in ấn  -> để PRIVATE. Chỉ tạo presigned URL khi có đơn, hạn 7 ngày.
                  Key có <sha8> nội dung nên dựng lại sách KHÔNG đè lên file cũ
                  mà đơn hàng trước đang trỏ tới.
  * Ảnh preview -> PUBLIC qua R2_PUBLIC_BASE (cdn.crayonahub.com). Vốn để cho
                  người xem, cần link ổn định, được CDN cache miễn phí.
  * manifest.json -> điểm vào CỐ ĐỊNH cho mỗi cuốn. Bên web chỉ cần nhớ đúng
                  một key: books/<slug>/manifest.json, đọc mọi thứ từ trong đó.

Sách 48 trang tô màu còn dựng kèm bản 24 trang (thư mục 03_pdf/24-trang/). Đây
là HAI SẢN PHẨM khác gáy, nên lên R2 thành hai biến thể, mỗi biến thể một bộ
file + page_count + spine riêng. page_count LUÔN đọc từ PDF thật, không tính tay.

Dùng nhanh:
    python -m bookgen.storage selftest              # kiểm tra kết nối R2
    python -m bookgen.storage upload cute-dragons   # đẩy 1 cuốn + sinh manifest
    python -m bookgen.storage manifest cute-dragons # in manifest (không cần mạng)
    python -m bookgen.storage presign cute-dragons full   # link Lulu tải, 7 ngày
    python -m bookgen.storage pull cute-dragons ./tai-ve  # tải PDF về máy
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("bookgen.storage")

# 03_pdf/<subdir> ứng với từng biến thể. "" = bản đầy đủ.
VARIANTS = [("full", ""), ("24p", "24-trang")]

_MAX_PRESIGN_SEC = 7 * 24 * 3600  # trần cứng của SigV4


# --------------------------------------------------------------- cấu hình

def _load_env() -> None:
    """Nạp .env giống server.py, để CLI chạy được khi server chưa bật."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            # Cắt chú thích cùng dòng (" # ...") - giá trị R2 không chứa " #".
            # Không cắt thì prefix nuốt cả câu chú thích -> key R2 sai bét.
            if " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            os.environ.setdefault(k.strip(), v.strip('"').strip("'"))


def cfg() -> dict:
    _load_env()
    account = os.environ.get("R2_ACCOUNT_ID", "")
    endpoint = os.environ.get("R2_ENDPOINT") or (
        f"https://{account}.r2.cloudflarestorage.com" if account else "")
    main_bucket = os.environ.get("R2_BUCKET", "")
    return {
        "endpoint": endpoint,
        "bucket": main_bucket,
        # PDF nên nằm bucket PRIVATE riêng (bucket public để lộ cả PDF qua cdn).
        # Chưa có thì tạm dùng bucket chính -> vẫn test được, chỉ kém an toàn.
        "bucket_pdf": os.environ.get("R2_BUCKET_PDF") or main_bucket,
        "key_id": os.environ.get("R2_ACCESS_KEY_ID", ""),
        "secret": os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        "public_base": os.environ.get("R2_PUBLIC_BASE", "").rstrip("/"),
        # PDF in: prefix riêng, mặc định "books". Ảnh preview: R2_FOLDER (images).
        "prefix_pdf": os.environ.get("R2_PREFIX_PDF", "books").strip("/"),
        "prefix_img": os.environ.get(
            "R2_PREFIX_PREVIEW", os.environ.get("R2_FOLDER", "images")).strip("/"),
        "presign_days": int(os.environ.get("R2_PRESIGN_DAYS", "7")),
    }


_client = None


def client():
    """Trả client S3 trỏ vào R2. Lười khởi tạo để import không cần credentials."""
    global _client
    if _client is not None:
        return _client
    import boto3
    from botocore.config import Config

    c = cfg()
    missing = [k for k in ("endpoint", "bucket", "key_id", "secret") if not c[k]]
    if missing:
        raise RuntimeError(
            "Thiếu cấu hình R2 trong .env: "
            + ", ".join({"key_id": "R2_ACCESS_KEY_ID",
                         "secret": "R2_SECRET_ACCESS_KEY",
                         "endpoint": "R2_ENDPOINT/R2_ACCOUNT_ID",
                         "bucket": "R2_BUCKET"}[k] for k in missing))
    _client = boto3.client(
        "s3",
        endpoint_url=c["endpoint"],
        aws_access_key_id=c["key_id"],
        aws_secret_access_key=c["secret"],
        region_name="auto",                       # R2 luôn là "auto", KHÔNG us-east-1
        config=Config(signature_version="s3v4",   # R2 chỉ nhận SigV4
                      retries={"max_attempts": 5, "mode": "standard"}),
    )
    return _client


# --------------------------------------------------------------- thao tác cơ bản

def bucket_for(key: str) -> str:
    """HƯỚNG B: mọi thứ vào bucket public (PDF phải qua cdn được để website tải).

    PDF được bảo vệ bằng key SHA256 không đoán nổi, không phải bằng bucket riêng.
    Vẫn tôn trọng R2_BUCKET_PDF nếu ai đó cố tình đặt (dùng cho hướng A sau này).
    """
    c = cfg()
    if key.startswith(c["prefix_img"] + "/"):
        return c["bucket"]
    return c["bucket_pdf"]  # = bucket chính nếu không đặt R2_BUCKET_PDF


def sha8(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "application/pdf"           # ép, tránh máy đoán ra octet-stream
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        client().head_object(Bucket=bucket_for(key), Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload(path: Path, key: str, meta: dict | None = None,
           skip_if_exists: bool = True) -> str:
    """Đẩy 1 file. Bỏ qua nếu key đã tồn tại (key PDF có sha nên trùng = giống hệt).

    Dùng upload_file -> tự multipart, rớt mạng giữa chừng file 60 MB không phải
    làm lại từ đầu.
    """
    path = Path(path)
    if skip_if_exists and exists(key):
        log.info("bỏ qua (đã có trên R2): %s", key)
        return key
    extra: dict[str, Any] = {"ContentType": _content_type(path)}
    if meta:
        extra["Metadata"] = {k: str(v) for k, v in meta.items()}
    mb = path.stat().st_size / 1e6
    log.info("upload %.1f MB -> %s", mb, key)
    client().upload_file(str(path), bucket_for(key), key, ExtraArgs=extra)
    return key


def presign(key: str, days: int | None = None) -> str:
    c = cfg()
    sec = min(_MAX_PRESIGN_SEC, (days or c["presign_days"]) * 24 * 3600)
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": bucket_for(key), "Key": key}, ExpiresIn=sec)


def public_url(key: str) -> str:
    base = cfg()["public_base"]
    if not base:
        raise RuntimeError("Chưa cấu hình R2_PUBLIC_BASE.")
    return f"{base}/{key}"


def download(key: str, dest: Path) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    client().download_file(bucket_for(key), key, str(dest))
    return dest


def list_keys(prefix: str) -> list[str]:
    out: list[str] = []
    tok = None
    while True:
        kw = {"Bucket": bucket_for(prefix), "Prefix": prefix}
        if tok:
            kw["ContinuationToken"] = tok
        r = client().list_objects_v2(**kw)
        out += [o["Key"] for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            return out
        tok = r["NextContinuationToken"]


# --------------------------------------------------------------- đo PDF

def _measure(interior_pdf: Path, cover_pdf: Path, print_cfg: dict) -> dict:
    """Số liệu bàn giao, đọc TỪ CHÍNH FILE ĐÃ DỰNG (không tính lại từ config)."""
    from pypdf import PdfReader
    from bookgen import pdf_builder

    pages = len(PdfReader(str(interior_pdf)).pages)
    box = PdfReader(str(cover_pdf)).pages[0].mediabox
    cover_w, cover_h = float(box.width) / 72, float(box.height) / 72

    # Gáy = chiều ngang bìa trừ hai nửa bìa và hai mép bao -> đúng con số đã in.
    spec = pdf_builder.binding_spec(print_cfg.get("binding", "perfect"))
    trim_w, _ = pdf_builder.cover_trim(print_cfg)
    spine = round(cover_w - 2 * trim_w - 2 * float(spec["wrap"]), 4)
    return {
        "page_count": pages,
        "cover_width_in": round(cover_w, 3),
        "cover_height_in": round(cover_h, 3),
        "spine_in": max(0.0, spine),
    }


# --------------------------------------------------------------- đẩy cả cuốn

def _book_cfg(slug: str) -> dict:
    """Đọc config + state của một cuốn để lấy trim/binding/title. Không đụng mạng."""
    import main as book_main
    c = book_main.load_cfg(ROOT / "config.yaml")
    c["_book"] = slug
    P = book_main.paths_of(c)
    state = book_main.load_state(P["state_file"])
    c.setdefault("book", {})
    for k in ("title", "subtitle", "num_images", "blank_verso"):
        if state.get(k) is not None:
            c["book"][k] = state[k]
    c["_paths"] = {k: str(v) for k, v in P.items()}
    c["_state"] = state
    return c


def build_manifest(slug: str) -> dict:
    """Dựng manifest từ file trên đĩa. KHÔNG cần R2, test được ngoại tuyến."""
    import main as book_main

    c = _book_cfg(slug)
    P = book_main.paths_of(c)
    pdf_dir = P["pdf_dir"]
    pfx_pdf = cfg()["prefix_pdf"]
    pfx_img = cfg()["prefix_img"]
    base = cfg()["public_base"]

    variants = []
    for vid, sub in VARIANTS:
        idir = pdf_dir / sub / "interior.pdf"
        cvr = pdf_dir / sub / "cover.pdf"
        if not idir.exists() or not cvr.exists():
            continue
        # HƯỚNG B: key PDF dùng SHA256 ĐẦY ĐỦ (64 hex) làm đoạn không đoán được.
        # Website đọc thẳng interior_url dưới đây đưa cho Lulu - KHÔNG presign,
        # không cần khoá bí mật. Người lạ không đoán ra (2^256) và không liệt kê
        # được bucket, nên URL công khai này coi như riêng tư.
        isha = sha256_hex(idir)
        csha = sha256_hex(cvr)
        ikey = f"{pfx_pdf}/{slug}/{isha}/interior.pdf"
        ckey = f"{pfx_pdf}/{slug}/{csha}/cover.pdf"
        m = _measure(idir, cvr, c["print"])
        variants.append({
            "id": vid,
            **m,
            "interior_key": ikey,
            "cover_key": ckey,
            "interior_url": f"{base}/{ikey}" if base else None,
            "cover_url": f"{base}/{ckey}" if base else None,
            "interior_sha256": isha,
            "cover_sha256": csha,
            "interior_bytes": idir.stat().st_size,
        })

    if not variants:
        raise FileNotFoundError(
            f"Cuốn '{slug}' chưa có PDF trong {pdf_dir}. Chạy build trước.")

    # Ảnh marketing: dùng CHUNG cho mọi biến thể (khác gáy nhưng cùng bìa).
    images: dict[str, Any] = {}
    raw = P["raw_dir"]
    prev = P.get("preview_dir") or (pdf_dir.parent / "04_previews")
    if (raw / "cover_front.png").exists() and base:
        images["cover_front"] = f"{base}/{pfx_img}/{slug}/cover_front.png"
    if base:
        pv = [f"{base}/{pfx_img}/{slug}/{p.name}"
              for p in sorted(prev.glob("preview_*.png"))]
        if pv:
            images["previews"] = pv

    bk = c["book"]
    return {
        "schema": 1,
        "slug": slug,
        "title": bk.get("title", slug),
        "trim_width": c["print"]["trim_width"],
        "trim_height": c["print"]["trim_height"],
        "bleed": c["print"]["bleed"],
        "binding": c["print"].get("binding", "perfect"),
        "interior_color": "bw" if not c.get("process", {}).get("color") else "color",
        # Mã Lulu cấp cuốn: giống nhau cho mọi biến thể (mã hoá giấy/khổ/đóng,
        # KHÔNG mã hoá số trang). page_count riêng từng biến thể lo phần gáy.
        "pod_package_id": c["print"].get("pod_package_id"),
        "variants": variants,
        "images": images,
        "source": {
            "num_images": bk.get("num_images"),
            "blank_verso": bk.get("blank_verso", True),
            "backend": c.get("backend", "web"),
        },
    }


def manifest_path(slug: str) -> Path:
    """Nơi lưu manifest CỤC BỘ (không đẩy lên R2 - xem upload_book)."""
    import main as book_main
    return book_main.BOOKS_DIR / slug / "manifest.json"


def upload_book(slug: str) -> dict:
    """Đẩy PDF + ảnh của một cuốn lên R2, ghi manifest CỤC BỘ.

      PDF   -> {prefix_pdf}/{slug}/{sha256}/...   (public nhưng key không đoán nổi)
      ảnh   -> {prefix_img}/{slug}/...            (public, marketing)
      mani  -> output/books/{slug}/manifest.json  (CỤC BỘ, KHÔNG lên R2)

    Vì sao manifest KHÔNG lên R2: nó chứa interior_url (URL PDF bí mật). Nếu đặt
    ở đường dẫn đoán được như books/<slug>/manifest.json trên bucket public thì
    ai biết slug là đọc được manifest -> lấy được URL PDF -> tải chùa. Giữ cục bộ,
    em tự đưa URL cho website (nhập tay/CSV), không phơi ra chỗ đoán được.
    """
    import main as book_main

    manifest = build_manifest(slug)
    c = _book_cfg(slug)
    P = book_main.paths_of(c)
    pdf_dir = P["pdf_dir"]
    pfx_img = cfg()["prefix_img"]

    # 1) PDF từng biến thể (key có sha256 nên trùng = giống hệt -> bỏ qua được)
    for v, (vid, sub) in zip(manifest["variants"], VARIANTS):
        idir = pdf_dir / sub / "interior.pdf"
        cvr = pdf_dir / sub / "cover.pdf"
        meta = {"slug": slug, "variant": vid, "page-count": v["page_count"]}
        upload(idir, v["interior_key"], meta)
        upload(cvr, v["cover_key"], meta)

    # 2) Ảnh marketing (public). Bỏ qua debug_*.png.
    raw = P["raw_dir"]
    prev = P.get("preview_dir") or (pdf_dir.parent / "04_previews")
    cf = raw / "cover_front.png"
    if cf.exists():
        upload(cf, f"{pfx_img}/{slug}/cover_front.png", skip_if_exists=False)
    for p in sorted(prev.glob("preview_*.png")):
        upload(p, f"{pfx_img}/{slug}/{p.name}", skip_if_exists=False)

    # 3) manifest.json - GHI CỤC BỘ, không đẩy lên R2.
    mp = manifest_path(slug)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    manifest["manifest_path"] = str(mp)
    log.info("[R2] Xong '%s': %d biến thể. Manifest cục bộ -> %s",
             slug, len(manifest["variants"]), mp)
    return manifest


# --------------------------------------------------------------- lấy về

def get_manifest(slug: str) -> dict:
    """Đọc manifest cục bộ (nguồn sự thật cho URL của cuốn)."""
    mp = manifest_path(slug)
    if not mp.exists():
        raise FileNotFoundError(
            f"Chưa có manifest cục bộ cho '{slug}'. Chạy upload_book trước.")
    return json.loads(mp.read_text(encoding="utf-8"))


def presign_variant(slug: str, variant_id: str = "full",
                    days: int | None = None) -> dict:
    """Link để LULU tải một biến thể. Đây là thứ bên web gọi lúc có đơn."""
    man = get_manifest(slug)
    for v in man["variants"]:
        if v["id"] == variant_id:
            return {
                "slug": slug, "variant": variant_id,
                "page_count": v["page_count"], "spine_in": v["spine_in"],
                "interior_url": presign(v["interior_key"], days),
                "cover_url": presign(v["cover_key"], days),
                "expires_in_days": days or cfg()["presign_days"],
            }
    raise KeyError(f"Cuốn '{slug}' không có biến thể '{variant_id}'. "
                   f"Có: {[v['id'] for v in man['variants']]}")


def pull_book(slug: str, dest_dir: Path) -> list[Path]:
    """Tải mọi PDF của một cuốn về máy (dùng API key, không qua presign)."""
    man = get_manifest(slug)
    dest_dir = Path(dest_dir)
    out = []
    for v in man["variants"]:
        for kind in ("interior_key", "cover_key"):
            key = v[kind]
            dest = dest_dir / slug / v["id"] / Path(key).name
            out.append(download(key, dest))
            log.info("tải về: %s", dest)
    return out


def selftest() -> bool:
    """Đẩy 1 file 12 byte lên rồi tải lại, xác nhận credentials + bucket chạy."""
    import io as _io
    key = "_selftest/ping.txt"
    payload = b"crayonahub-ok"
    client().put_object(Bucket=cfg()["bucket"], Key=key, Body=payload,
                        ContentType="text/plain")
    obj = client().get_object(Bucket=cfg()["bucket"], Key=key)
    got = obj["Body"].read()
    client().delete_object(Bucket=cfg()["bucket"], Key=key)
    ok = got == payload
    c = cfg()
    print(f"  endpoint   : {c['endpoint']}")
    print(f"  bucket     : {c['bucket']}")
    print(f"  public_base: {c['public_base'] or '(chưa đặt)'}")
    print(f"  ghi+đọc+xoá: {'ĐẠT ✓' if ok else 'SAI ✗'}")
    # Kiểm tra domain public đã gắn chưa. Đây là bucket có thể DÙNG CHUNG với
    # website nên phải tự dọn: ghi vào _diag/, tự đọc bằng UA trình duyệt (tránh
    # bot-protection của Cloudflare trả 403 giả), rồi XOÁ ngay.
    if c["public_base"]:
        import urllib.request
        dkey = f"_diag/selftest-{uuid.uuid4().hex}.txt"
        client().put_object(Bucket=c["bucket"], Key=dkey,
                            Body=b"ok", ContentType="text/plain")
        url = f"{c['public_base']}/{dkey}"
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            code = urllib.request.urlopen(req, timeout=20).status
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", f"lỗi {e}")
        client().delete_object(Bucket=c["bucket"], Key=dkey)
        print(f"  domain đọc : HTTP {code}  "
              f"({'CÔNG KHAI, cdn trỏ đúng bucket' if code == 200 else 'chưa public / chưa gắn'})")
    return ok


# --------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "selftest":
            return 0 if selftest() else 1
        if cmd == "manifest":
            print(json.dumps(build_manifest(rest[0]), ensure_ascii=False, indent=2))
            return 0
        if cmd == "upload":
            man = upload_book(rest[0])
            print(json.dumps(man, ensure_ascii=False, indent=2))
            return 0
        if cmd == "presign":
            vid = rest[1] if len(rest) > 1 else "full"
            print(json.dumps(presign_variant(rest[0], vid), ensure_ascii=False, indent=2))
            return 0
        if cmd == "pull":
            dest = rest[1] if len(rest) > 1 else "./r2-pull"
            pull_book(rest[0], Path(dest))
            return 0
        print(f"Lệnh lạ: {cmd}")
        print(__doc__)
        return 2
    except Exception as e:  # noqa: BLE001
        log.error("LỖI: %s", e)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(_main(sys.argv[1:]))
