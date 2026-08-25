"""Làm sạch ảnh line-art từ Gemini để đủ chuẩn in 300 DPI."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None


def _hf_std(im: Image.Image) -> float:
    """Do "do chi tiet" cua anh = lech chuan cua thanh phan tan so cao.

    Dung lam thuoc do khach quan cho grain: hat cang nhieu -> so nay cang lon.
    """
    import numpy as np
    g = im.convert("L")
    a = np.asarray(g, np.float32)
    b = np.asarray(g.filter(ImageFilter.GaussianBlur(1.2)), np.float32)
    return float((a - b).std())


def auto_grain_params(path: Path, ratio: float = 0.35) -> tuple[float, float]:
    """Tu chon (amount, size) sao cho HAT HOA VAO ANH, khong lan at anh.

    Y tuong: grain nen ti le voi luong chi tiet san co cua anh.
      * Anh nhieu net (nhieu chi tiet)  -> chiu duoc hat manh hon.
      * Anh phang, mang mau lon         -> hat nhe, neu khong se lo ro nhu bui.
    ratio = ti le do hat so voi chi tiet goc (0.35 -> hat bang ~35% chi tiet).

    Size hat scale theo do phan giai de khi IN ra hat luon trong cung mot co,
    khong phu thuoc anh 1000px hay 3000px.
    """
    import tempfile

    im = Image.open(path).convert("RGB")
    long_edge = max(im.size)
    size = max(1.0, round(long_edge / 1600.0, 2))

    # Do & hieu chinh tren mot O CAT 640px o giua anh (CAT chu khong THU NHO:
    # thu nho lam sac net gia tao, thang do se sai hoan toan).
    cx, cy = im.size[0] // 2, im.size[1] // 2
    r = min(320, cx, cy)
    sm = im.crop((cx - r, cy - r, cx + r, cy + r))
    base = _hf_std(sm) or 1.0
    target = min(7.0, max(1.5, ratio * base))        # luong hat muon them vao

    amount = 0.08
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "probe.png"
        for _ in range(5):
            sm.save(probe)
            add_grain(probe, amount=amount, mode="tone", size=size)
            got = _hf_std(Image.open(probe)) ** 2 - base ** 2
            got = got ** 0.5 if got > 0.01 else 0.01
            if abs(got - target) / target < 0.05:
                break
            # mu 0.8: mode "tone" phan hoi duoi tuyen tinh -> tranh vot qua.
            amount = max(0.01, min(0.6, amount * (target / got) ** 0.8))
    return round(amount, 3), size


def add_grain(path: Path, amount: float = 0.05, mono: bool = True,
              mode: str = "overlay", size: float = 1.0) -> Path:
    """Phu mot lop grain len anh, ghi de tai cho.

    amount = do dam (0..0.3). mode:
      * "overlay" (mac dinh) -> lop hat xam blend overlay, giong VAN GIAY IN:
        dam ma MIN, khong san; hat theo tong anh nen tu nhien.
      * "add" -> noise cong truc tiep (co trong so midtone), de sang salt-pepper
        khi amount cao.
      * "auto" -> TU CHON do dam + co hat cho hoa voi anh; luc nay `amount`
        la RATIO (0.35 = hat bang ~35% luong chi tiet san co cua anh).
    mono=True -> grain kieu phim/giay (cung mot nhieu cho 3 kenh).
    """
    import numpy as np

    if mode == "auto":
        # amount luc nay dong vai tro RATIO (do hai hoa), khong phai do dam.
        amount, size = auto_grain_params(path, ratio=float(amount) or 0.35)
        mode = "tone"

    amount = max(0.0, min(0.6, float(amount)))
    if amount <= 0:
        return path
    im = Image.open(path).convert("RGB")
    arr = np.asarray(im).astype(np.float32)
    h, w = arr.shape[:2]
    sigma = amount * 255.0
    ch = 1 if mono else 3
    # size = kich thuoc HAT (px). >1 -> tao noise o do phan giai thap roi phong
    # to len -> moi hat phu nhieu pixel (hat to hon).
    size = max(1.0, float(size))
    if size > 1.0:
        # NOISE DA TANG (fractal): tron nhieu co hat -> co cum to, cum nho,
        # khong deu tam tap -> tu nhien nhu grain phim/giay that. Noi suy BICUBIC
        # cho bien hat muot, khong bi vuong/kim cuong nhu bilinear.
        # Ít tầng "cụm to" (loang) hơn, nặng tầng mịn -> grain đều, min hon.
        octaves = [(size * 1.5, 0.25), (size, 1.0), (size / 2.0, 0.7)]
        noise = np.zeros((h, w, ch), np.float32)
        wsum = 0.0
        for sc, wgt in octaves:
            sc = max(1.0, sc)
            nh, nw = max(1, int(h / sc)), max(1, int(w / sc))
            small = np.random.normal(0.0, 255.0, (nh, nw, ch)).astype(np.float32)
            for c in range(ch):
                layer = Image.fromarray(
                    np.clip(128.0 + small[:, :, c], 0, 255).astype("uint8"))
                layer = layer.resize((w, h), Image.BICUBIC)
                noise[:, :, c] += (np.asarray(layer).astype(np.float32)
                                   - 128.0) * wgt
            wsum += wgt
        # chuan hoa ve dung do dam mong muon (sigma)
        std = noise.std() or 1.0
        noise = noise / std * sigma
    else:
        noise = np.random.normal(0.0, sigma, (h, w, ch)).astype(np.float32)

    if mode == "tone":
        # Grain CUNG MAU NEN, chi sang/toi hon 1 bac: NHAN he so (giu nguyen
        # hue + do bao hoa) thay vi cong (cong se day ve trang/den, mat mau).
        # Huong theo tong: nen sang -> hat sang hon, nen toi -> hat toi hon ->
        # tuong phan thap, hoa vao nen du nen mau gi.
        nim = Image.fromarray(
            np.clip(128.0 + noise[:, :, 0], 0, 255).astype("uint8"))
        nim = nim.filter(ImageFilter.GaussianBlur(1.1))
        mag = np.abs(np.asarray(nim).astype(np.float32) - 128.0) / 255.0
        luma = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
                + 0.114 * arr[:, :, 2]) / 255.0
        direction = 2.0 * luma - 1.0                 # +1 sang, -1 toi, 0 giua
        factor = 1.0 + mag * direction               # nhan -> doi SHADE cung hue
        out = arr * factor[:, :, None]
    elif mode == "soft":
        # Grain MEM: lam mo hat thanh mang huu co (nhu van giay/canvas), roi
        # modulate do sang RAT NHE quanh 1.0 va GIU NGUYEN MAU (nhan deu 3 kenh).
        # Khong phai cham sac -> nhin nhu texture giay, khong bui ban.
        nim = Image.fromarray(
            np.clip(128.0 + noise[:, :, 0], 0, 255).astype("uint8"))
        nim = nim.filter(ImageFilter.GaussianBlur(1.3))
        soft = (np.asarray(nim).astype(np.float32) - 128.0)   # (h,w) mem
        factor = 1.0 + (soft / 255.0)                          # quanh 1.0
        out = arr * factor[:, :, None]
    elif mode == "ink":
        # Grain MOT CHIEU (chi lam TOI), kieu van muc in. Khong bao gio tao cham
        # trang tren nen den -> khong lap lanh/bui. |noise| nhan voi anh: dot hoi
        # toi deu, dam o vung sang, gan nhu vo hinh o vung da toi.
        d = np.abs(noise[:, :, :1])                # luong lam toi (>=0)
        out = arr * (1.0 - d / 255.0)
    elif mode == "overlay":
        # Lop hat xam quanh 128, blend OVERLAY. Day CA 2 CHIEU -> tren nen sang
        # ra cham den, nen den ra cham trang (tuong phan voi nen).
        b = arr / 255.0
        t = np.clip(128.0 + noise, 0, 255) / 255.0            # lop hat
        out = np.where(t < 0.5, 2.0 * b * t,
                       1.0 - 2.0 * (1.0 - b) * (1.0 - t)) * 255.0
    else:
        luma = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
                + 0.114 * arr[:, :, 2]) / 255.0
        weight = np.clip(1.0 - (2.0 * luma - 1.0) ** 2, 0.0, 1.0)
        weight = 0.15 + 0.85 * weight
        out = arr + noise * weight[:, :, None]

    Image.fromarray(np.clip(out, 0, 255).astype("uint8")).save(path)
    return path


def remove_watermark(path: Path,
                     center: tuple[float, float] = (0.870, 0.902),
                     size: tuple[float, float] = (0.034, 0.041)) -> Path:
    """Xoa dau sao cua Gemini o GOC PHAI-DUOI anh mau, ghi de tai cho.

    Gemini web luon dong dau sao ban trong suot o MOT vi tri co dinh. Trang to
    mau tu sach nho buoc threshold 1-bit; anh preview MAU khong qua buoc do.

    Cach lam: mask hinh sao 4 canh tai vi tri co dinh `center`, roi inpaint bang
    xphoto SHIFTMAP - thuat toan COPY van that tu vung xung quanh (patch-based),
    nen va xong van con van go/da/giay, KHONG de lai mang "qua min" nhu Telea.
    Chay tren mot cua so nho quanh dau sao cho nhanh (~1s thay vi quet ca anh).

    center,size theo TI LE anh -> dung cho moi kich thuoc. Thieu opencv-contrib
    (khong co xphoto) thi tu lui ve Telea.
    """
    import cv2
    import numpy as np

    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as e:  # noqa: BLE001
        log.warning("Khong doc duoc %s de xoa watermark: %s", path.name, e)
        return path
    if img is None:
        return path

    H, W = img.shape[:2]

    def star_mask(h, w, cx, cy, rx, ry):
        outer = [(0, -1), (1, 0), (0, 1), (-1, 0)]
        pts = []
        for i in range(4):
            ox, oy = outer[i]
            pts.append((cx + ox * rx, cy + oy * ry))
            nx, ny = outer[(i + 1) % 4]
            pts.append((cx + (ox + nx) * rx * 0.34, cy + (oy + ny) * ry * 0.34))
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.array(pts, np.int32)], 255)
        return cv2.dilate(m, np.ones((3, 3), np.uint8))

    # Cua so nho quanh dau sao (SHIFTMAP quet ca anh thi rat cham).
    wx0, wy0 = int(W * 0.72), int(H * 0.78)
    win = img[wy0:H, wx0:W].copy()
    wh, ww = win.shape[:2]
    cx, cy = center[0] * W - wx0, center[1] * H - wy0
    rx, ry = size[0] * W, size[1] * H
    m = star_mask(wh, ww, cx, cy, rx, ry)

    has_xphoto = hasattr(cv2, "xphoto")
    if has_xphoto:
        # SHIFTMAP: mask non-zero = pixel HOP LE (giu), zero = can lap -> 255-m.
        dst = win.copy()
        cv2.xphoto.inpaint(win, 255 - m, dst, cv2.xphoto.INPAINT_SHIFTMAP)
    else:
        dst = cv2.inpaint(win, m, 3, cv2.INPAINT_TELEA)

    a = np.maximum(m.astype(np.float32) / 255.0,
                   cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), 1.5))[..., None]
    blended = (win.astype(np.float32) * (1 - a) + dst.astype(np.float32) * a
               ).clip(0, 255).astype(np.uint8)
    out = img.copy()
    out[wy0:H, wx0:W] = blended

    ok, enc = cv2.imencode(path.suffix or ".png", out)
    if ok:
        enc.tofile(str(path))
        log.info("Da xoa watermark (%s): %s",
                 "SHIFTMAP" if has_xphoto else "Telea", path.name)
    return path


def upscale_to_min(path: Path, min_long_edge: int = 2000) -> Path:
    """Phóng ảnh lên cho cạnh dài đạt tối thiểu min_long_edge, ghi đè tại chỗ.

    Dùng cho ảnh preview marketing: Gemini trả về khoảng 800px, trong khi sàn
    thương mại điện tử cần cạnh dài từ 1600px trở lên thì mới bật được chức năng
    phóng to khi khách rê chuột.

    NÓI RÕ GIỚI HẠN: phóng to KHÔNG tạo thêm chi tiết. Việc này chỉ giúp ảnh đạt
    ngưỡng kỹ thuật của sàn và trông đỡ vỡ khi hiển thị lớn, chứ không làm ảnh
    nét hơn bản gốc. Nguồn gốc vấn đề vẫn là Gemini trả ảnh nhỏ.

    Ảnh đã đủ lớn thì giữ nguyên, không đụng vào.
    """
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            long_edge = max(w, h)
            if long_edge >= min_long_edge:
                return path

            scale = min_long_edge / long_edge
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            big = im.resize(new_size, Image.LANCZOS)
            # LANCZOS phóng xong hay bị mềm; unsharp nhẹ lấy lại cảm giác sắc nét.
            big = big.filter(ImageFilter.UnsharpMask(radius=2, percent=90,
                                                     threshold=3))
        big.save(path, "PNG", dpi=(300, 300), optimize=True)
        log.info("Nâng %s: %dx%d -> %dx%d (phóng %.1f lần).",
                 path.name, w, h, new_size[0], new_size[1], scale)
    except Exception as e:  # noqa: BLE001
        log.warning("Không nâng được độ phân giải %s: %s", path.name, e)
    return path


def square_upscale(path: Path, size_px: int = 2000) -> Path:
    """Ép ảnh preview về tỉ lệ 1:1 (crop giữa) rồi phóng lên size_px x size_px.

    Gemini trả ảnh preview quanh 800px và tỉ lệ không cố định. Sàn TMĐT muốn ảnh
    vuông (1:1) cỡ lớn thì mới bật được chức năng zoom khi khách rê chuột. Ở đây:
      1) crop giữa về đúng 1:1 (không bóp méo hình);
      2) resize LANCZOS lên đúng size_px x size_px;
      3) unsharp nhẹ lấy lại cảm giác sắc nét sau khi phóng.

    NÓI RÕ: phóng to KHÔNG tạo thêm chi tiết, chỉ đạt ngưỡng kỹ thuật của sàn.
    """
    try:
        with Image.open(path) as im:
            im.load()
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            w, h = im.size

            # 1) crop giữa về vuông 1:1
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            sq = im.crop((left, top, left + side, top + side))

            # 2) phóng/thu về đúng size_px x size_px
            if sq.size != (size_px, size_px):
                sq = sq.resize((size_px, size_px), Image.LANCZOS)

            # 3) LANCZOS phóng xong hay mềm; unsharp nhẹ khi có phóng to
            if side < size_px:
                sq = sq.filter(ImageFilter.UnsharpMask(radius=2, percent=90,
                                                       threshold=3))
        sq.save(path, "PNG", dpi=(300, 300), optimize=True)
        log.info("Vuông hoá %s: %dx%d -> %dx%d px.",
                 path.name, w, h, size_px, size_px)
    except Exception as e:  # noqa: BLE001
        log.warning("Không vuông hoá được %s: %s", path.name, e)
    return path


def crop_white_margins(img: Image.Image, padding_px: int = 10) -> Image.Image:
    """Tự động xén bỏ các lề trắng thừa xung quanh hình vẽ nét để nét vẽ mở rộng chạm sát viền."""
    try:
        gray = img.convert("L")
        bw = gray.point(lambda p: 255 if p < 245 else 0)
        bbox = bw.getbbox()
        if bbox:
            left, top, right, bottom = bbox
            left = max(0, left - padding_px)
            top = max(0, top - padding_px)
            right = min(img.width, right + padding_px)
            bottom = min(img.height, bottom + padding_px)
            return img.crop((left, top, right, bottom))
    except Exception as e:
        log.debug("Lỗi crop_white_margins: %s", e)
    return img


def _upscale_steps(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Phóng theo từng chặng x2 rồi mới về đúng khổ (C).

    Nhảy thẳng 800->2625 (một lần LANCZOS ~3.3x) làm nét mềm và răng cưa không
    đều. Phóng nhiều chặng nhỏ (800->1600->2625) cho nét liền và đều hơn vì mỗi
    lần nội suy chỉ phải "đoán" ít.
    """
    w, h = img.size
    while w * 2 <= tw and h * 2 <= th:
        w, h = w * 2, h * 2
        img = img.resize((w, h), Image.LANCZOS)
    if (img.width, img.height) != (tw, th):
        img = img.resize((tw, th), Image.LANCZOS)
    return img


def _levels(img: Image.Image, black: int = 12, white: int = 244) -> Image.Image:
    """Kéo levels NHẸ (B): nền về trắng sạch, nét về đen đặc hơn.

    Khoảng [black, white] hẹp vừa phải để KHÔNG nuốt nét xám mảnh (chỉ dịch nhẹ),
    nhưng đủ để bỏ nền hơi ngả xám và làm nét đen dứt khoát -> nhìn crisp hơn.
    """
    scale = 255.0 / max(1, (white - black))
    lut = [0 if i <= black else 255 if i >= white
           else int(round((i - black) * scale)) for i in range(256)]
    return img.point(lut)


def process_lineart(
    src: Path,
    dest: Path,
    target_w_px: int,
    target_h_px: int,
    threshold: int = 165,
    pure_bw: bool = False,
    sharpen: bool = True,
) -> Path:
    img = Image.open(src)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("L")

    src_w = img.width
    # cutoff nhỏ thôi: cắt mạnh sẽ nuốt mất các nét xám nhạt
    img = ImageOps.autocontrast(img, cutoff=(0, 1))

    # 0) xén bỏ lề trắng thừa xung quanh để nét vẽ mở rộng chạm viền
    img = crop_white_margins(img, padding_px=10)

    # 1) cắt về đúng tỉ lệ TRƯỚC khi phóng to
    img = _fit_to_ratio(img, target_w_px, target_h_px)

    scale = target_w_px / max(1, src_w)
    up = (img.width, img.height) != (target_w_px, target_h_px)

    # 2) (C) phóng NHIỀU CHẶNG (800->1600->2625) cho nét đều rồi về đúng khổ.
    #    Ảnh Gemini vốn sạch nên KHÔNG khử nhiễu (median chỉ bào mòn nét mảnh).
    if up:
        img = _upscale_steps(img, target_w_px, target_h_px)

    # 3) (C) unsharp lấy lại độ sắc sau khi phóng
    if sharpen and scale > 1.2:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=120,
                                                 threshold=2))

    # 4) (B) kéo levels: nền về trắng sạch, lõi nét về đen đặc -> nhìn crisp,
    #    khoảng vẫn đủ rộng để giữ các nét xám mảnh (vết nứt, hoa văn nhạt).
    img = _levels(img, black=28, white=246)

    # 5) threshold MỘT lần duy nhất, ở kích thước cuối
    if pure_bw:
        img = img.point(lambda p: 255 if p >= threshold else 0, mode="L")
        img = img.convert("1")

    if scale > 2.0:
        log.warning("%s: ảnh gốc chỉ %dpx, phải phóng %.1f lần lên %dpx. "
                    "Phóng to không tạo thêm chi tiết - nét sẽ mềm. "
                    "Nên xin Gemini ảnh độ phân giải cao hơn.",
                    src.name, src_w, scale, target_w_px)

    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", dpi=(300, 300), optimize=True)
    log.info("Xử lý xong %s -> %dx%d px (%s)", src.name, img.width, img.height,
             "1-bit đen trắng" if pure_bw else "xám khử răng cưa")
    return dest


def _fit_to_ratio(img: Image.Image, tw: int, th: int) -> Image.Image:
    """Crop giữa về đúng tỉ lệ tw:th (không bóp méo hình)."""
    want = tw / th
    have = img.width / img.height
    if abs(want - have) < 0.01:
        return img
    if have > want:  # ảnh quá rộng -> cắt hai bên
        new_w = int(img.height * want)
        left = (img.width - new_w) // 2
        return img.crop((left, 0, left + new_w, img.height))
    new_h = int(img.width / want)  # ảnh quá cao -> cắt trên dưới
    top = (img.height - new_h) // 2
    return img.crop((0, top, img.width, top + new_h))


def check_dpi(path: Path, w_in: float, h_in: float, min_dpi: int = 300) -> tuple[bool, float]:
    """Trả về (đạt_chuẩn, dpi_thực_tế) khi in ảnh này lên khổ w_in x h_in."""
    with Image.open(path) as im:
        dpi = min(im.width / w_in, im.height / h_in)
    return dpi >= min_dpi - 1, dpi


def _fill_blur(img: Image.Image, W: int, H: int) -> Image.Image:
    """Nền cho dải viền: phóng chính ảnh lấp kín khung rồi làm mờ mạnh.

    Kéo giãn một hàng pixel ra 0.8 inch tạo thành vệt sọc rất lộ. Nền mờ thì
    không sọc, không nhân đôi chi tiết, màu vẫn đúng, và mắt đọc nó như bóng đổ
    chứ không như tranh - hỏng cũng chỉ hỏng thành một vùng màu vô hại.
    """
    scale = max(W / img.width, H / img.height)
    big = img.resize((max(1, round(img.width * scale)),
                      max(1, round(img.height * scale))), Image.LANCZOS)
    left = (big.width - W) // 2
    top = (big.height - H) // 2
    bg = big.crop((left, top, left + W, top + H))
    return bg.filter(ImageFilter.GaussianBlur(radius=max(8, W // 60)))


def _inset_with_edge_fill(img: Image.Image, W: int, H: int, insets: dict,
                          mode: str = "blur") -> Image.Image:
    """Thu ảnh vào giữa rồi lấp viền bằng hàng pixel ngoài cùng kéo giãn ra.

    Vì sao kéo giãn chứ không để trắng hay soi gương: phần viền này chính là chỗ
    bị máy xén cắt bỏ (bìa mềm) hoặc gập vào mặt trong tấm bìa các-tông (bìa
    cứng) - gần như không ai nhìn thấy. Kéo giãn thì liền mạch với tranh, không
    đẻ ra chi tiết lạ như soi gương, và không để lộ khung trắng.
    """
    t = max(0, round(H * insets.get("top", 0)))
    b = max(0, round(H * insets.get("bottom", 0)))
    l = max(0, round(W * insets.get("left", 0)))
    r = max(0, round(W * insets.get("right", 0)))
    iw, ih = W - l - r, H - t - b
    if iw <= 0 or ih <= 0:                     # thu quá tay -> thà giữ nguyên
        return img.resize((W, H), Image.LANCZOS)

    art = img.resize((iw, ih), Image.LANCZOS)

    if mode == "blur":
        out = _fill_blur(img, W, H)
        out.paste(art, (l, t))
        return out

    out = Image.new("RGB", (W, H))
    out.paste(art, (l, t))
    if mode == "mirror":
        if t:
            out.paste(art.crop((0, 0, iw, t)).transpose(Image.FLIP_TOP_BOTTOM), (l, 0))
        if b:
            out.paste(art.crop((0, ih - b, iw, ih)).transpose(Image.FLIP_TOP_BOTTOM), (l, H - b))
        if l:
            out.paste(out.crop((l, 0, l + l, H)).transpose(Image.FLIP_LEFT_RIGHT), (0, 0))
        if r:
            out.paste(out.crop((W - r - r, 0, W - r, H)).transpose(Image.FLIP_LEFT_RIGHT), (W - r, 0))
        return out

    # mode == "stretch": kéo giãn hàng pixel ngoài cùng.
    # Trên/dưới trước, rồi trái/phải kéo trọn chiều cao -> bốn góc tự lấp đúng.
    if t:
        out.paste(art.crop((0, 0, iw, 1)).resize((iw, t), Image.NEAREST), (l, 0))
    if b:
        out.paste(art.crop((0, ih - 1, iw, ih)).resize((iw, b), Image.NEAREST), (l, H - b))
    if l:
        out.paste(out.crop((l, 0, l + 1, H)).resize((l, H), Image.NEAREST), (0, 0))
    if r:
        out.paste(out.crop((W - r - 1, 0, W - r, H)).resize((r, H), Image.NEAREST), (W - r, 0))
    return out


def prepare_cover_art(src: Path, dest: Path, w_px: int, h_px: int,
                      insets: dict | None = None, fill: str = "blur") -> Path:
    """Bìa: giữ màu, resize/crop, ép RGB 300 DPI.

    insets = tỉ lệ mỗi cạnh cần thu vào (pdf_builder.cover_art_insets). Có thì
    tranh được đẩy trọn vào vùng an toàn, không còn phụ thuộc việc model có chịu
    chừa lề hay không.
    """
    img = Image.open(src).convert("RGB")
    img = _fit_to_ratio(img, w_px, h_px)
    if insets:
        img = _inset_with_edge_fill(img, w_px, h_px, insets, fill)
        log.info("Thu bìa %s (%s): trên/dưới %.1f%%, trái %.1f%%, phải %.1f%%.",
                 src.name, fill, insets.get("top", 0) * 100,
                 insets.get("left", 0) * 100, insets.get("right", 0) * 100)
    else:
        img = img.resize((w_px, h_px), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "PNG", dpi=(300, 300))
    return dest


def render_titled_cover(src_cover: Path, dest_cover: Path, title: str = "", subtitle: str = "") -> Path:
    """Ảnh bìa đã được Gemini tự vẽ sẵn chữ tiêu đề trong prompt. Hàm này chỉ sao chép file tới dest_cover."""
    if not src_cover.exists():
        return src_cover
    try:
        if src_cover.resolve() != dest_cover.resolve():
            import shutil
            dest_cover.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_cover, dest_cover)
            return dest_cover
    except Exception as e:
        log.debug("Lỗi copy cover: %s", e)
    return src_cover
