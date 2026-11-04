"""Non-destructive Quick Edits (pillar 2): crop, straighten and five tone sliders.

The master file is never touched. An edit is a small `Adjustments` record stored as JSON in
`media_edits` (database.py) and re-applied wherever pixels are produced:
  * viewer – the display-sized image and the deep-zoom pyramid,
  * grid  – the thumbnail before it becomes a tile,
  * export – the "Web / Social" and "High-Res Archive" presets ("Original Master" stays a byte copy).

Pixel model (one implementation for all three):
  * Geometry first, as ONE affine warp (cv2.warpAffine, inverse map): straighten rotates the image
    about its centre and scales it just enough that no empty corner enters the frame (the Photos
    behaviour), then the normalised crop rectangle – expressed in that straightened frame – selects
    the output. Without rotation the crop is a plain slice (bit-exact).
  * Tone second, as ONE fused 256-entry LUT per channel (cv2.LUT, ~1 ms per megapixel):
      exposure + temperature act in linear light (sRGB EOTF), then highlights / shadows / contrast
      shape a monotone Fritsch–Carlson curve in the encoded domain. Monotone by construction, so no
      slider combination can solarise. Alpha is never changed.
  The viewer drags on a ≤ PROXY_LONG_EDGE proxy and refines at full display size when the slider
  rests, which keeps the per-frame cost far below the 8.3 ms budget of a 120 Hz panel.

Colour: tone runs on whatever encoding the caller holds (display contract in the viewer / grid,
source encoding in export, followed by export's own single colour conversion). On the sRGB system
contract both are the same numbers.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

import numpy as np

EDIT_VERSION = 1
ANGLE_LIMIT = 45.0
SLIDER_LIMIT = 100.0
EXPOSURE_EV = 2.0                  # slider ±100 = ±2 EV
TEMP_GAIN = 0.18                   # slider ±100 = R×(1±0.18), B×(1∓0.18) in linear light (luma kept)
SHADOW_LIFT = 0.15                 # curve control point shift at 25 % / 75 % for ±100
HIGHLIGHT_LIFT = 0.15
MID_COUPLING = 0.04                # shadows / highlights nudge the mid-tone a little (natural roll)
CONTRAST_SPAN = 0.08               # S-curve: 25 % / 75 % points move ∓/± this much for ±100
PROXY_LONG_EDGE = 1600             # viewer drags on this; full display size when the slider rests
MIN_CROP = 0.02                    # smallest crop side (fraction of the frame)

ASPECTS = {"free": None, "original": "original", "1:1": 1.0, "16:9": 16 / 9, "4:5": 4 / 5}
ASPECT_ORDER = ("free", "1:1", "16:9", "4:5")
TONE_KEYS = ("exposure", "highlights", "shadows", "temperature", "contrast")
_LUMA = np.array([0.2126, 0.7152, 0.0722])


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return max(lo, min(hi, v))


@dataclass
class Adjustments:
    crop: tuple = (0.0, 0.0, 1.0, 1.0)          # x, y, w, h – fractions of the straightened frame
    aspect: str = "free"
    angle: float = 0.0                          # degrees, + = clockwise on screen
    exposure: float = 0.0
    highlights: float = 0.0
    shadows: float = 0.0
    temperature: float = 0.0                    # + = warmer
    contrast: float = 0.0
    extra: dict = field(default_factory=dict)   # keys from newer builds survive a round trip

    # ── validation ──
    def normalized(self):
        try:
            x, y, w, h = tuple(self.crop)
        except (TypeError, ValueError):
            x, y, w, h = 0.0, 0.0, 1.0, 1.0
        w = _clamp(w, MIN_CROP, 1.0)
        h = _clamp(h, MIN_CROP, 1.0)
        x = _clamp(x, 0.0, 1.0 - w)
        y = _clamp(y, 0.0, 1.0 - h)
        return Adjustments(
            crop=(x, y, w, h),
            aspect=self.aspect if self.aspect in ASPECTS else "free",
            angle=_clamp(self.angle, -ANGLE_LIMIT, ANGLE_LIMIT),
            **{k: _clamp(getattr(self, k), -SLIDER_LIMIT, SLIDER_LIMIT) for k in TONE_KEYS},
            extra=dict(self.extra or {}))

    def tone_identity(self):
        return all(abs(getattr(self, k)) < 1e-6 for k in TONE_KEYS)

    def geometry_identity(self):
        x, y, w, h = self.crop
        return abs(self.angle) < 1e-6 and x <= 1e-6 and y <= 1e-6 and w >= 1 - 1e-6 and h >= 1 - 1e-6

    def is_identity(self):
        return self.tone_identity() and self.geometry_identity()

    # ── persistence ──
    def to_dict(self):
        d = dict(self.extra or {})
        d.update({"v": EDIT_VERSION, "crop": [round(c, 6) for c in self.crop], "aspect": self.aspect,
                  "angle": round(self.angle, 4)})
        d.update({k: round(getattr(self, k), 3) for k in TONE_KEYS})
        return d

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            return cls()
        known = {"v", "crop", "aspect", "angle", *TONE_KEYS}
        crop = d.get("crop")
        if not (isinstance(crop, (list, tuple)) and len(crop) == 4):
            crop = (0.0, 0.0, 1.0, 1.0)
        a = cls(crop=tuple(crop), aspect=str(d.get("aspect", "free")), angle=d.get("angle", 0.0),
                **{k: d.get(k, 0.0) for k in TONE_KEYS},
                extra={k: v for k, v in d.items() if k not in known})
        return a.normalized()

    @classmethod
    def from_json(cls, text):
        if not text:
            return cls()
        try:
            return cls.from_dict(json.loads(text))
        except (ValueError, TypeError):
            return cls()

    def key(self):
        """Short stable fingerprint (cache keys, tile invalidation)."""
        if self.is_identity():
            return ""
        return hashlib.sha1(self.to_json().encode("utf-8")).hexdigest()[:12]


# ── tone ─────────────────────────────────────────────────────────────────────

def _srgb_to_linear(v):
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)


def _monotone_curve(xs, ys, t):
    """Fritsch–Carlson monotone cubic through strictly increasing (xs, ys), evaluated at t."""
    xs, ys = np.asarray(xs, float), np.asarray(ys, float)
    h = np.diff(xs)
    delta = np.diff(ys) / h
    m = np.empty_like(ys)
    m[0], m[-1] = delta[0], delta[-1]
    m[1:-1] = (delta[:-1] + delta[1:]) / 2
    for i in range(len(delta)):
        if delta[i] == 0:
            m[i] = m[i + 1] = 0.0
            continue
        a, b = m[i] / delta[i], m[i + 1] / delta[i]
        s = a * a + b * b
        if s > 9:
            k = 3 / math.sqrt(s)
            m[i], m[i + 1] = k * a * delta[i], k * b * delta[i]
    t = np.clip(np.asarray(t, float), xs[0], xs[-1])
    i = np.clip(np.searchsorted(xs, t, side="right") - 1, 0, len(h) - 1)
    u = (t - xs[i]) / h[i]
    h00 = (1 + 2 * u) * (1 - u) ** 2
    h10 = u * (1 - u) ** 2
    h01 = u * u * (3 - 2 * u)
    h11 = u * u * (u - 1)
    return h00 * ys[i] + h10 * h[i] * m[i] + h01 * ys[i + 1] + h11 * h[i] * m[i + 1]


def curve_points(adj):
    """Control points of the encoded-domain curve (shadows / highlights / contrast)."""
    s, hl, c = adj.shadows / 100.0, adj.highlights / 100.0, adj.contrast / 100.0
    xs = (0.0, 0.25, 0.5, 0.75, 1.0)
    ys = (0.0,
          0.25 + SHADOW_LIFT * s - CONTRAST_SPAN * c,
          0.5 + MID_COUPLING * (s + hl),
          0.75 + HIGHLIGHT_LIFT * hl + CONTRAST_SPAN * c,
          1.0)
    return xs, ys


def channel_gains(adj):
    """Linear-light gains (R, G, B): exposure × white balance, luminance-neutral WB."""
    t = adj.temperature / 100.0
    wb = np.array([1.0 + TEMP_GAIN * t, 1.0, 1.0 - TEMP_GAIN * t])
    wb /= float(wb @ _LUMA)                      # warming must not brighten
    return wb * (2.0 ** (EXPOSURE_EV * adj.exposure / 100.0))


def build_lut(adj, channels="RGB"):
    """uint8 LUT shaped (1, 256, n) for cv2.LUT; `channels` names the memory order ("RGB",
    "BGR", "RGBA", "BGRA"). Alpha entries are the identity."""
    v = np.arange(256, dtype=np.float64) / 255.0
    lin = _srgb_to_linear(v)
    gains = dict(zip("RGB", channel_gains(adj)))
    xs, ys = curve_points(adj)
    out = []
    for ch in channels:
        if ch == "A":
            out.append(np.arange(256, dtype=np.float64))
            continue
        enc = _linear_to_srgb(lin * gains[ch])
        out.append(_monotone_curve(xs, ys, enc) * 255.0)
    lut = np.clip(np.rint(np.stack(out, axis=-1)), 0, 255).astype(np.uint8)
    return lut.reshape(1, 256, len(channels))


def apply_tone(arr, adj, channels="RGB"):
    """uint8 H×W×n array -> toned copy (or the same array when the tone is neutral)."""
    import cv2
    if adj.tone_identity():
        return arr
    lut = build_lut(adj, channels)
    if arr.ndim == 2:
        return cv2.LUT(arr, lut[..., 1].reshape(1, 256))     # greyscale: the green (luma-heavy) curve
    return cv2.LUT(np.ascontiguousarray(arr), lut)


# ── geometry ─────────────────────────────────────────────────────────────────

def straighten_scale(w, h, angle_deg):
    """Zoom that keeps the rotated image covering the whole w×h frame (no empty corners)."""
    th = math.radians(abs(angle_deg))
    c, s = math.cos(th), math.sin(th)
    return max((w * c + h * s) / float(w), (w * s + h * c) / float(h))


def output_size(w, h, adj, scale=1.0):
    _x, _y, cw, ch = adj.crop
    return max(1, int(round(cw * w * scale))), max(1, int(round(ch * h * scale)))


def inverse_matrix(w, h, adj, scale=1.0):
    """2×3 affine mapping OUTPUT pixel coords -> SOURCE pixel coords (cv2 WARP_INVERSE_MAP).
    Pixel centres: cv2 samples output pixel q at its integer coordinate, mapped as given."""
    x0, y0, _cw, _ch = adj.crop
    th = math.radians(adj.angle)
    s = straighten_scale(w, h, adj.angle)
    cx, cy = w / 2.0, h / 2.0
    c, sn = math.cos(th), math.sin(th)
    # frame p = crop_origin + q/scale ; src = C + R(-θ)(p - C)/s
    ox, oy = x0 * w - cx, y0 * h - cy
    k = 1.0 / (s * scale)
    a, b = c * k, sn * k                        # R(-θ) = [[c, s], [-s, c]] (y-down screen coords)
    m00, m01 = a, b
    m10, m11 = -b, a
    tx = cx + (c * ox + sn * oy) / s
    ty = cy + (-sn * ox + c * oy) / s
    # half-pixel centre convention: cv2 maps integer coords; shift so pixel centres line up
    tx += (m00 + m01) * 0.5 - 0.5
    ty += (m10 + m11) * 0.5 - 0.5
    return np.array([[m00, m01, tx], [m10, m11, ty]], dtype=np.float64)


def apply_geometry(arr, adj, scale=1.0, fast=False):
    """uint8 H×W×n array -> cropped / straightened (and optionally scaled) array."""
    import cv2
    h, w = arr.shape[:2]
    if adj.geometry_identity() and abs(scale - 1.0) < 1e-9:
        return arr
    ow, oh = output_size(w, h, adj, scale)
    if abs(adj.angle) < 1e-6:
        x0, y0, cw, ch = adj.crop
        l, t = int(round(x0 * w)), int(round(y0 * h))
        r, b = min(w, l + max(1, int(round(cw * w)))), min(h, t + max(1, int(round(ch * h))))
        part = arr[t:b, l:r]
        if part.shape[1] == ow and part.shape[0] == oh:
            return np.ascontiguousarray(part)
        interp = cv2.INTER_AREA if (ow < part.shape[1] and not fast) else cv2.INTER_LINEAR
        return cv2.resize(part, (ow, oh), interpolation=interp)
    m = inverse_matrix(w, h, adj, scale)
    src = arr
    k = scale / 1.0
    if not fast and k < 0.5:                     # big reduction: pre-shrink so the warp doesn't alias
        pre = max(1e-3, min(1.0, k * 2))
        src = cv2.resize(arr, (max(1, int(w * pre)), max(1, int(h * pre))), interpolation=cv2.INTER_AREA)
        fx, fy = src.shape[1] / float(w), src.shape[0] / float(h)
        m = m.copy()
        m[0, :2] *= fx
        m[1, :2] *= fy
        m[0, 2] = (m[0, 2] + 0.5) * fx - 0.5
        m[1, 2] = (m[1, 2] + 0.5) * fy - 0.5
    flags = (cv2.INTER_LINEAR if fast else cv2.INTER_CUBIC) | cv2.WARP_INVERSE_MAP
    return cv2.warpAffine(src, m, (ow, oh), flags=flags, borderMode=cv2.BORDER_REPLICATE)


def apply(arr, adj, channels="RGB", scale=1.0, fast=False):
    """Geometry then tone. Returns a uint8 array (the input itself when nothing changes)."""
    if adj is None or (adj.is_identity() and abs(scale - 1.0) < 1e-9):
        return arr
    out = apply_geometry(arr, adj, scale, fast)
    if out.ndim == 3 and out.shape[2] == 4 and channels in ("RGB", "BGR"):
        channels = channels + "A"
    return apply_tone(out, adj, channels)


# ── crop helpers (UI) ────────────────────────────────────────────────────────

def aspect_ratio(aspect, w, h):
    """Pixel ratio (width / height) for a preset, or None for free."""
    r = ASPECTS.get(aspect)
    if r == "original":
        return w / float(max(1, h))
    return r


def fit_aspect(crop, ratio, w, h):
    """Largest rectangle of pixel ratio `ratio` inside `crop` (normalised), centred on it."""
    if not ratio:
        return tuple(crop)
    x, y, cw, ch = crop
    pw, ph = cw * w, ch * h
    if pw / ph > ratio:
        nw = ph * ratio / w
        return (x + (cw - nw) / 2, y, nw, ch)
    nh = pw / ratio / h
    return (x, y + (ch - nh) / 2, cw, nh)


def with_aspect(adj, aspect, w, h):
    """New Adjustments with an aspect preset applied to the current crop (largest fit, centred)."""
    a = Adjustments(**{**adj.__dict__, "aspect": aspect})
    ratio = aspect_ratio(aspect, w, h)
    if ratio:
        a.crop = fit_aspect((0.0, 0.0, 1.0, 1.0), ratio, w, h) if adj.geometry_identity() \
            else fit_aspect(adj.crop, ratio, w, h)
    return a.normalized()


def drag_crop(crop, handle, dx, dy, ratio, w, h):
    """Crop after dragging `handle` ("move", "tl", "tr", "bl", "br", "l", "r", "t", "b") by
    (dx, dy) normalised units, keeping `ratio` (pixel w/h) when given. Stays inside the frame."""
    x, y, cw, ch = crop
    if handle == "move":
        return (min(max(0.0, x + dx), 1 - cw), min(max(0.0, y + dy), 1 - ch), cw, ch)
    l, t, r, b = x, y, x + cw, y + ch
    if "l" in handle:
        l = min(max(0.0, l + dx), r - MIN_CROP)
    if "r" in handle:
        r = max(min(1.0, r + dx), l + MIN_CROP)
    if "t" in handle:
        t = min(max(0.0, t + dy), b - MIN_CROP)
    if "b" in handle:
        b = max(min(1.0, b + dy), t + MIN_CROP)
    nw, nh = r - l, b - t
    if ratio:
        # keep the corner opposite the handle fixed; shrink the free side to honour the ratio
        if (nw * w) / (nh * h) > ratio:
            want = nh * h * ratio / w
            if "l" in handle:
                l = r - want
            else:
                r = l + want
        else:
            want = nw * w / ratio / h
            if "t" in handle:
                t = b - want
            else:
                b = t + want
        if l < 0 or r > 1 or t < 0 or b > 1:
            return tuple(crop)
    return (l, t, r - l, b - t)


# ── PIL / QImage front doors ─────────────────────────────────────────────────

def render_pil(img, adj, scale=1.0):
    """PIL RGB/RGBA/L -> edited PIL image (same mode)."""
    from PIL import Image
    if adj is None or (adj.is_identity() and abs(scale - 1.0) < 1e-9):
        return img
    mode = img.mode if img.mode in ("RGB", "RGBA", "L") else "RGB"
    src = img if img.mode == mode else img.convert(mode)
    out = apply(np.asarray(src), adj, "RGBA" if mode == "RGBA" else "RGB", scale)
    res = Image.fromarray(np.ascontiguousarray(out), mode)
    icc = img.info.get("icc_profile")
    if icc:
        res.info["icc_profile"] = icc
    return res


def render_qimage(qimg, adj, scale=1.0, fast=False):
    """QImage -> edited QImage (Format_RGBA8888 unless the input was opaque RGB888/RGB32)."""
    from PyQt6.QtGui import QImage
    if qimg is None or qimg.isNull() or adj is None or (adj.is_identity() and abs(scale - 1.0) < 1e-9):
        return qimg
    fmt = qimg.format()
    if fmt == QImage.Format.Format_RGB888:
        src, n, chans, out_fmt = qimg, 3, "RGB", QImage.Format.Format_RGB888
    else:
        src = qimg if fmt == QImage.Format.Format_RGBA8888 else qimg.convertToFormat(QImage.Format.Format_RGBA8888)
        n, chans, out_fmt = 4, "RGBA", QImage.Format.Format_RGBA8888
    w, h = src.width(), src.height()
    bpl = src.bytesPerLine()
    buf = np.frombuffer(src.constBits().asstring(bpl * h), dtype=np.uint8).reshape(h, bpl)[:, :w * n]
    arr = buf.reshape(h, w, n)
    out = np.ascontiguousarray(apply(arr, adj, chans, scale, fast))
    oh, ow = out.shape[:2]
    res = QImage(out.data, ow, oh, ow * n, out_fmt).copy()
    res.setDevicePixelRatio(qimg.devicePixelRatio())
    cs = qimg.colorSpace()
    if cs.isValid():
        res.setColorSpace(cs)
    return res


def proxy_scale(w, h, long_edge=PROXY_LONG_EDGE):
    """Scale factor that brings w×h down to the drag proxy (never enlarges)."""
    return min(1.0, long_edge / float(max(1, w, h)))


# ── storage helpers (database.media_edits) ──────────────────────────────────

def load(conn, media_id):
    row = conn.execute("SELECT params FROM media_edits WHERE media_id = ?", (int(media_id),)).fetchone()
    return Adjustments.from_json(row[0]) if row else None


def load_many(conn, ids=None):
    """{media_id: Adjustments} for edited items (all, or the given ids)."""
    if ids is None:
        rows = conn.execute("SELECT media_id, params FROM media_edits").fetchall()
    else:
        ids = [int(i) for i in ids]
        rows = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows += conn.execute(f"SELECT media_id, params FROM media_edits WHERE media_id IN "
                                 f"({','.join('?' * len(chunk))})", chunk).fetchall()
    out = {}
    for mid, params in rows:
        a = Adjustments.from_json(params)
        if not a.is_identity():
            out[int(mid)] = a
    return out


def save(conn, media_id, adj):
    """Store (or clear, when neutral) the edit of one item. The caller commits."""
    if adj is None or adj.normalized().is_identity():
        conn.execute("DELETE FROM media_edits WHERE media_id = ?", (int(media_id),))
        return False
    conn.execute("INSERT INTO media_edits (media_id, params, updated_at) VALUES (?, ?, datetime('now')) "
                 "ON CONFLICT(media_id) DO UPDATE SET params = excluded.params, updated_at = excluded.updated_at",
                 (int(media_id), adj.normalized().to_json()))
    return True
