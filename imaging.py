"""Kairos native image pipeline – the one door every still image goes through.

Every derived image (grid thumbnail, viewer preview, viewer original, face avatar) is built here
by the same short sequence, and nothing else ever touches its pixels:

    decode   – Pillow (+ pillow_heif), embedded ICC read BEFORE anything can drop it, EXIF
               orientation, 16-bit / palette / CMYK normalised without clipping or washing out
    resample – Lanczos3 with an exact integer box pre-reduction (Pillow `reducing_gap`):
               area-correct for big reductions, Lanczos-sharp for the final step
    sharpen  – only for thumbnails and avatars: a fixed, photographic screen-sharpen on luma only
               (the same delta is added to R, G and B, so chroma is untouched bit-for-bit) with
               overshoot clamping against the 3×3 neighbourhood – crisp, no halos
    colour   – exactly ONE conversion, source ICC -> the display contract (color_manage)
    encode   – cache JPEGs at 4:4:4 (no chroma subsampling: saturated edges stay clean)

There are no scene-dependent tone curves, no saturation tweaks, no gamut "compression".
Untagged images are sRGB and pass through untouched when the display contract is sRGB.

Thread-safe (no Qt widgets; QImage helpers are plain image data).
"""
import io
import os

import numpy as np
from PIL import Image, ImageOps

import color_manage

PIPELINE_VERSION = 1

# Screen sharpening (radius in output px, amount, threshold in 0–255 luma levels). Fixed values,
# deliberately NOT derived from the image: the same mild "sharpen for screen" a photographer
# applies after downscaling. Viewer images and exports are never sharpened.
SHARPEN_THUMB = (0.6, 0.55, 1.5)
SHARPEN_AVATAR = (0.8, 0.75, 1.5)
OVERSHOOT = 6.0                      # luma may exceed the local 3×3 min/max by at most this much

CACHE_JPEG_QUALITY = 92
FLATTEN_RGB = (255, 255, 255)        # transparent PNG -> cache JPEG background

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)   # Rec.709 / sRGB primaries
_codecs_ready = False


def register_codecs():
    global _codecs_ready
    if not _codecs_ready:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:                     # pragma: no cover
            pass
        import raw_decode                       # ProRAW / camera RAW as a Pillow format (raw_decode.py)
        raw_decode.register()
        _codecs_ready = True


# ── decode ────────────────────────────────────────────────────────────────────

def _normalize_mode(img, icc):
    """RGB or RGBA, 8 bit, without clipping. Returns (image, icc) – icc may be consumed
    (CMYK / grey profiles are applied here because LittleCMS needs the native mode)."""
    mode = img.mode
    if mode in ("RGB", "RGBA"):
        return img, icc
    if mode in ("I;16", "I;16L", "I;16B", "I;16N", "I"):
        # 16-bit grey: Pillow's convert("RGB") CLIPS everything above 255 to white
        a = np.asarray(img, dtype=np.float64)
        top = 65535.0 if mode.startswith("I;16") or a.max() > 255 else 255.0
        a = np.clip(np.round(a * (255.0 / top)), 0, 255).astype(np.uint8)
        return Image.fromarray(a, "L").convert("RGB"), None if icc and not _is_rgb_icc(icc) else icc
    if mode == "F":
        a = np.clip(np.round(np.asarray(img, dtype=np.float64) * 255.0), 0, 255).astype(np.uint8)
        return Image.fromarray(a, "L").convert("RGB"), None
    if mode == "CMYK":
        if icc and not _is_rgb_icc(icc):
            out = color_manage.convert_to_srgb(img, icc, "CMYK")
            if out is not None:
                return out, None
        return img.convert("RGB"), None
    if mode in ("L", "LA") and icc and not _is_rgb_icc(icc):
        icc = None                              # grey gamma profile: treat as sRGB grey
    has_alpha = mode in ("LA", "PA", "La", "RGBa") or (mode == "P" and "transparency" in img.info)
    return img.convert("RGBA" if has_alpha else "RGB"), icc


def _is_rgb_icc(icc):
    try:
        return icc[16:20] == b"RGB "
    except Exception:
        return False


class Decoded:
    __slots__ = ("image", "icc", "full_size")

    def __init__(self, image, icc, full_size):
        self.image = image            # PIL RGB / RGBA, oriented
        self.icc = icc                # embedded RGB ICC bytes or None (= sRGB)
        self.full_size = full_size    # oriented size of the original (before any draft)


def open_oriented(path, need_long=None):
    """Decode a still. need_long = longest output edge the caller will produce; JPEG then
    decodes in the DCT at no less than 2× that (exact, and far cheaper than a full decode)."""
    register_codecs()
    with Image.open(path) as im:
        icc = im.info.get("icc_profile") or None
        w, h = im.size
        orientation = 1
        try:
            orientation = int(im.getexif().get(274, 1) or 1)
        except Exception:
            pass
        if orientation in (5, 6, 7, 8):
            w, h = h, w
        if im.info.get("raw_full_size"):         # RAW: upright already; the sensor size is the original
            w, h = im.info["raw_full_size"]
        if need_long and im.format == "JPEG":
            try:
                im.draft("RGB", (int(need_long) * 2, int(need_long) * 2))
            except Exception:
                pass
        im.load()
        img = ImageOps.exif_transpose(im)
        if img is im:
            img = im.copy()
    img, icc = _normalize_mode(img, icc)
    return Decoded(img, icc, (w, h))


# ── geometry + resampling ─────────────────────────────────────────────────────

def fit_box(w, h, box_w, box_h):
    """Size that fits (w, h) inside the box, keeping aspect; never enlarges."""
    w, h = max(1, int(w)), max(1, int(h))
    k = min(1.0, box_w / float(w), box_h / float(h))
    return max(1, round(w * k)), max(1, round(h * k))


def fit_short_edge(w, h, short, long_max=None):
    w, h = max(1, int(w)), max(1, int(h))
    k = min(1.0, short / float(min(w, h)))
    if long_max:
        k = min(k, long_max / float(max(w, h)))
    return max(1, round(w * k)), max(1, round(h * k))


def resample(img, size):
    """Lanczos3 to an exact size (integer box pre-reduction first for big reductions)."""
    size = (max(1, int(size[0])), max(1, int(size[1])))
    if img.size == size:
        return img
    shrinking = size[0] < img.size[0] and size[1] < img.size[1]
    return img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0 if shrinking else None)


# ── luma-only screen sharpening ───────────────────────────────────────────────

def sharpen_luma(img, radius, amount, threshold=0.0):
    """Unsharp mask on luminance only. The identical delta is added to R, G and B, so the
    channel differences (R−G, B−G = chroma) are unchanged wherever no channel clips."""
    import cv2
    if img.mode not in ("RGB", "RGBA") or amount <= 0 or min(img.size) < 3:
        return img
    alpha = img.getchannel("A") if img.mode == "RGBA" else None
    a = np.asarray(img.convert("RGB") if alpha is not None else img, dtype=np.float32)
    y = a @ _LUMA
    blur = cv2.GaussianBlur(y, (0, 0), sigmaX=float(radius), borderType=cv2.BORDER_REFLECT)
    d = y - blur
    if threshold > 0:                         # soft threshold: leave sensor noise / flat skin alone
        mag = np.abs(d)
        d *= np.clip((mag - threshold) / max(threshold, 1e-6), 0.0, 1.0)
    target = y + amount * d
    k = np.ones((3, 3), np.uint8)
    lo = cv2.erode(y, k, borderType=cv2.BORDER_REFLECT) - OVERSHOOT
    hi = cv2.dilate(y, k, borderType=cv2.BORDER_REFLECT) + OVERSHOOT
    delta = np.clip(target, lo, hi) - y
    out = np.clip(np.rint(a + delta[..., None]), 0, 255).astype(np.uint8)
    res = Image.fromarray(out, "RGB")
    if alpha is not None:
        res.putalpha(alpha)
    return res


# ── colour + encode ───────────────────────────────────────────────────────────

def to_display(img, icc):
    """The single colour conversion of the pipeline (source profile -> display contract)."""
    return color_manage.pil_to_output(img, icc)


def flatten(img):
    if img.mode == "RGBA":
        bg = Image.new("RGB", img.size, FLATTEN_RGB)
        bg.paste(img, mask=img.getchannel("A"))
        return bg
    return img if img.mode == "RGB" else img.convert("RGB")


def encode_jpeg(img, quality=CACHE_JPEG_QUALITY):
    """Cache JPEG bytes: 4:4:4 (no chroma subsampling), baseline, no metadata."""
    buf = io.BytesIO()
    flatten(img).save(buf, "JPEG", quality=int(quality), subsampling=0, optimize=False)
    return buf.getvalue()


def write_jpeg(img, path, quality=CACHE_JPEG_QUALITY):
    """Atomic cache write (tmp + replace with retry). True on success."""
    import fs_atomic
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = fs_atomic.unique_tmp(path)
    try:
        with open(tmp, "wb") as f:
            f.write(encode_jpeg(img, quality))
        return fs_atomic.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ── the renders ───────────────────────────────────────────────────────────────

def render_still(path, size_fn=None, sharpen=None, output=True, need_long=None):
    """Decode -> resample (size_fn(w, h) -> (w, h)) -> optional luma sharpen -> display colour.
    Sharpening only runs when the image was actually reduced. Returns PIL RGB/RGBA."""
    dec = open_oriented(path, need_long)
    img = dec.image
    if size_fn is not None:
        target = size_fn(*img.size)
        reduced = target[0] < img.size[0] and target[1] < img.size[1]
        img = resample(img, target)
        if sharpen and reduced:
            img = sharpen_luma(img, *sharpen)
    return to_display(img, dec.icc) if output else img


def render_avatar(crop_rgb, size, icc=None, output=True):
    """Square face crop (PIL RGB, source colour) -> size×size display-ready avatar."""
    img = resample(crop_rgb, (size, size))
    img = sharpen_luma(img, *SHARPEN_AVATAR)
    return to_display(img, icc) if output else img


# ── Qt bridges (pure image data – safe off the GUI thread) ───────────────────

def pil_to_qimage(img):
    from PyQt6.QtGui import QImage
    if img.mode == "RGBA":
        w, h = img.size
        return QImage(img.tobytes("raw", "RGBA"), w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    img = img if img.mode == "RGB" else img.convert("RGB")
    w, h = img.size
    return QImage(img.tobytes("raw", "RGB"), w, h, w * 3, QImage.Format.Format_RGB888).copy()


def qimage_to_pil(qimg):
    from PyQt6.QtGui import QImage
    alpha = qimg.hasAlphaChannel()
    fmt = QImage.Format.Format_RGBA8888 if alpha else QImage.Format.Format_RGB888
    q = qimg.convertToFormat(fmt)
    w, h = q.width(), q.height()
    ptr = q.constBits()
    ptr.setsize(q.sizeInBytes())
    mode = "RGBA" if alpha else "RGB"
    return Image.frombuffer(mode, (w, h), bytes(ptr), "raw", mode, q.bytesPerLine(), 1).copy()


def scale_qimage(qimg, w, h):
    """QImage -> exactly w×h with Lanczos3 (Qt's smooth scaling is bilinear / box). Upscales
    too (still better than bilinear). Returns a new QImage; null in = null out."""
    if qimg is None or qimg.isNull() or w <= 0 or h <= 0:
        return qimg
    if qimg.width() == w and qimg.height() == h:
        return qimg
    return pil_to_qimage(resample(qimage_to_pil(qimg), (w, h)))


def shrink_qimage_short_edge(qimg, short):
    """QImage reduced (Lanczos) so its short edge is `short`; smaller images are returned as is."""
    w, h = qimg.width(), qimg.height()
    if qimg.isNull() or min(w, h) <= short:
        return qimg
    return scale_qimage(qimg, *fit_short_edge(w, h, short))


def fit_qimage(qimg, box_w, box_h):
    """QImage reduced (Lanczos) to fit inside the box; never enlarged."""
    if qimg.isNull() or (qimg.width() <= box_w and qimg.height() <= box_h):
        return qimg
    return scale_qimage(qimg, *fit_box(qimg.width(), qimg.height(), box_w, box_h))


# ── retired caches ────────────────────────────────────────────────────────────

def purge_retired_caches():
    """Remove data of retired pipeline stages (Live tone curves, crop recolour markers).
    Returns the number of removed entries."""
    import shutil
    import paths
    removed = 0
    tone = os.path.join(paths.CACHE_ROOT, "tone")
    if os.path.isdir(tone):
        removed += len(os.listdir(tone))
        shutil.rmtree(tone, ignore_errors=True)
    for name in ("CROP_COLOR", "CROP_COLOR.icc"):
        p = os.path.join(paths.FACE_DIR, name)
        try:
            os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed
