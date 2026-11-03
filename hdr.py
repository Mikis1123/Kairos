"""HDR readiness: is the display in HDR, does the photo carry a gain map, and the gain-map maths.

The pieces an HDR still path needs, measured on this machine and library:
  * display_state(): Windows Advanced Color (HDR) per active display via the DisplayConfig API
    (on/off, supported), the SDR white level the user set in Settings, and the panel's peak
    luminance from DXGI (IDXGIOutput6::GetDesc1). headroom = peak / SDR white, the factor
    a gain map may expand highlights to.
  * gain_map_info(path): which gain map a file carries:
      apple    iPhone HEIC auxiliary image urn:com:apple:photo:2020:aux:hdrgainmap with
               XMP HDRGainMap:HDRGainMapHeadroom (the whole library: iPhone 15, headroom ~6.8)
      iso      ISO 21496-1 'tmap' item (HEIC/AVIF) or the ISO URN in a JPEG (Ultra HDR v2)
      ultrahdr JPEG with Adobe/Google hdrgm XMP (Android Ultra HDR v1)
  * apply_apple() / apply_iso(): base image + gain map -> linear scRGB (1.0 = SDR white),
    weighted by the display's headroom (no headroom: exactly the SDR image).

What it does NOT do yet: present the result. Qt Widgets compose an 8-bit sRGB surface, so HDR
pixels need a native FP16 scRGB swap chain (a D3D11/DComp child window like the WebView2 map
host) for the viewer's image layer. The maths here is what that surface will be fed, and
test_hdr_pipeline.py pins it.
"""
import ctypes
import mmap
import os
import re
from ctypes import wintypes

import numpy as np

APPLE_GAIN_MAP_URN = "urn:com:apple:photo:2020:aux:hdrgainmap"
ISO_URN = b"urn:iso:std:iso:ts:21496:-1"
DEFAULT_SDR_WHITE_NITS = 80.0            # Windows reference (SDRWhiteLevel 1000)


# ── display ───────────────────────────────────────────────────────────────────

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _PathInfo(ctypes.Structure):          # DISPLAYCONFIG_PATH_INFO (72 bytes)
    _fields_ = [("src_adapter", _LUID), ("src_id", ctypes.c_uint32), ("src_mode", ctypes.c_uint32),
                ("src_flags", ctypes.c_uint32),
                ("tgt_adapter", _LUID), ("tgt_id", ctypes.c_uint32), ("tgt_mode", ctypes.c_uint32),
                ("tech", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refresh_num", ctypes.c_uint32), ("refresh_den", ctypes.c_uint32),
                ("scanline", ctypes.c_uint32), ("available", wintypes.BOOL), ("tgt_flags", ctypes.c_uint32),
                ("flags", ctypes.c_uint32)]


class _ModeInfo(ctypes.Structure):          # DISPLAYCONFIG_MODE_INFO (64 bytes)
    _fields_ = [("type", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapter", _LUID),
                ("union", ctypes.c_byte * 48)]


class _Header(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapter", _LUID),
                ("id", ctypes.c_uint32)]


class _AdvancedColor(ctypes.Structure):     # DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO (type 9)
    _fields_ = [("header", _Header), ("value", ctypes.c_uint32), ("encoding", ctypes.c_uint32),
                ("bits", ctypes.c_uint32)]


class _SdrWhite(ctypes.Structure):          # DISPLAYCONFIG_SDR_WHITE_LEVEL (type 11)
    _fields_ = [("header", _Header), ("level", ctypes.c_ulong)]


class _TargetName(ctypes.Structure):        # DISPLAYCONFIG_TARGET_DEVICE_NAME (type 2)
    _fields_ = [("header", _Header), ("flags", ctypes.c_uint32), ("tech", ctypes.c_uint32),
                ("edid_mfg", ctypes.c_uint16), ("edid_product", ctypes.c_uint16),
                ("connector", ctypes.c_uint32), ("name", ctypes.c_wchar * 64), ("path", ctypes.c_wchar * 128)]


def _displayconfig():
    user32 = ctypes.windll.user32
    n_paths, n_modes = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(n_paths), ctypes.byref(n_modes)) != 0:
        return []
    paths = (_PathInfo * n_paths.value)()
    modes = (_ModeInfo * n_modes.value)()
    if user32.QueryDisplayConfig(2, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None) != 0:
        return []
    out = []
    for p in paths[:n_paths.value]:
        def query(struct, kind):
            s = struct()
            s.header.type, s.header.size = kind, ctypes.sizeof(struct)
            s.header.adapter, s.header.id = p.tgt_adapter, p.tgt_id
            return s if user32.DisplayConfigGetDeviceInfo(ctypes.byref(s)) == 0 else None
        ac = query(_AdvancedColor, 9)
        white = query(_SdrWhite, 11)
        name = query(_TargetName, 2)
        out.append({
            "name": name.name if name else "",
            "hdr_supported": bool(ac and ac.value & 1),
            "hdr_enabled": bool(ac and ac.value & 2),
            "bits_per_channel": int(ac.bits) if ac else 0,
            "sdr_white_nits": (white.level / 1000.0 * 80.0) if white and white.level else DEFAULT_SDR_WHITE_NITS,
        })
    return out


_GUID = ctypes.c_byte * 16


def _guid(s):
    import uuid
    return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


def _vcall(obj, index, restype, *args):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    types = [type(a) if isinstance(a, ctypes._SimpleCData) else ctypes.c_void_p for a in args]   # byref -> pointer
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *types)(vtbl[index])
    return fn(obj, *args)


class _OutputDesc1(ctypes.Structure):       # DXGI_OUTPUT_DESC1
    _fields_ = [("name", ctypes.c_wchar * 32), ("rect", wintypes.RECT), ("attached", wintypes.BOOL),
                ("rotation", ctypes.c_uint32), ("monitor", ctypes.c_void_p), ("bits", ctypes.c_uint32),
                ("colorspace", ctypes.c_uint32), ("primaries", ctypes.c_float * 8),
                ("min_nits", ctypes.c_float), ("max_nits", ctypes.c_float), ("max_full_nits", ctypes.c_float)]


def _dxgi_outputs():
    """[{name, max_nits, min_nits, max_full_nits, hdr_colorspace}] from IDXGIOutput6::GetDesc1."""
    out = []
    try:
        dxgi = ctypes.windll.dxgi
    except OSError:
        return out
    factory = ctypes.c_void_p()
    if dxgi.CreateDXGIFactory1(ctypes.byref(_guid("770aae78-f26f-4dba-a829-253c83d1b387")),
                               ctypes.byref(factory)) != 0:
        return out
    iid6 = _guid("068346e8-aaec-4b84-add7-137f513f77a1")
    try:
        a = 0
        while True:
            adapter = ctypes.c_void_p()
            if _vcall(factory, 12, ctypes.c_long, ctypes.c_uint(a), ctypes.byref(adapter)) != 0:
                break                                       # DXGI_ERROR_NOT_FOUND
            o = 0
            while True:
                output = ctypes.c_void_p()
                if _vcall(adapter, 7, ctypes.c_long, ctypes.c_uint(o), ctypes.byref(output)) != 0:
                    break
                out6 = ctypes.c_void_p()
                if _vcall(output, 0, ctypes.c_long, ctypes.byref(iid6), ctypes.byref(out6)) == 0:
                    d = _OutputDesc1()
                    if _vcall(out6, 27, ctypes.c_long, ctypes.byref(d)) == 0:
                        out.append({"name": d.name, "max_nits": float(d.max_nits), "min_nits": float(d.min_nits),
                                    "max_full_nits": float(d.max_full_nits),
                                    "hdr_colorspace": d.colorspace == 12})   # RGB_FULL_G2084_NONE_P2020
                    _vcall(out6, 2, ctypes.c_ulong)
                _vcall(output, 2, ctypes.c_ulong)
                o += 1
            _vcall(adapter, 2, ctypes.c_ulong)
            a += 1
    finally:
        _vcall(factory, 2, ctypes.c_ulong)
    return out


def display_state():
    """[{name, hdr_supported, hdr_enabled, sdr_white_nits, max_nits, headroom}] per active display.
    headroom = max_nits / sdr_white_nits when HDR is on, else 1.0 (SDR: nothing to expand)."""
    try:
        displays = _displayconfig()
    except Exception:
        displays = []
    try:
        dx = _dxgi_outputs()
    except Exception:
        dx = []
    for i, d in enumerate(displays):
        m = dx[i] if i < len(dx) else {}
        d["name"] = d["name"] or m.get("name", "")
        d["max_nits"] = m.get("max_nits", 0.0)
        d["max_full_nits"] = m.get("max_full_nits", 0.0)
        on = d["hdr_enabled"] or m.get("hdr_colorspace", False)
        d["hdr_enabled"] = bool(on)
        d["headroom"] = max(1.0, d["max_nits"] / d["sdr_white_nits"]) if on and d["max_nits"] > 0 else 1.0
    return displays


# ── gain map detection ────────────────────────────────────────────────────────

_HEADROOM_RE = re.compile(rb"HDRGainMapHeadroom(?:>|=\")\s*([0-9]*\.?[0-9]+)")


def _scan(path, needles, limit=64 << 20):
    """{needle: offset or -1}, searched over the whole file (mmap, no copy)."""
    found = {n: -1 for n in needles}
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size == 0:
            return found, b""
        with mmap.mmap(f.fileno(), min(size, limit), access=mmap.ACCESS_READ) as mm:
            for n in needles:
                found[n] = mm.find(n)
            m = _HEADROOM_RE.search(mm)
            return found, (m.group(1) if m else b"")


def gain_map_info(path):
    """{'kind': 'apple' | 'iso' | 'ultrahdr', 'headroom': linear or None, 'size': (w, h) or None},
    or None when the file has no gain map."""
    ext = os.path.splitext(str(path))[1].lower()
    try:
        if ext in (".heic", ".heif", ".avif", ".hif"):
            found, headroom = _scan(path, (APPLE_GAIN_MAP_URN.encode(), b"tmap"))
            if found[APPLE_GAIN_MAP_URN.encode()] >= 0:
                size = None
                try:
                    import pillow_heif
                    h = pillow_heif.open_heif(path)
                    ids = (h.info.get("aux") or {}).get(APPLE_GAIN_MAP_URN) or []
                    if ids:
                        size = h.get_aux_image(ids[0]).size
                except Exception:
                    pass
                return {"kind": "apple", "headroom": float(headroom) if headroom else None, "size": size}
            if found[b"tmap"] >= 0 and _has_tmap_item(path):
                return {"kind": "iso", "headroom": None, "size": None}
            return None
        if ext in (".jpg", ".jpeg"):
            found, _ = _scan(path, (ISO_URN, b"hdrgm:Version", b"hdrgm:GainMapMax"))
            if found[ISO_URN] >= 0:
                return {"kind": "iso", "headroom": None, "size": None}
            if found[b"hdrgm:Version"] >= 0 or found[b"hdrgm:GainMapMax"] >= 0:
                return {"kind": "ultrahdr", "headroom": None, "size": None}
    except (OSError, ValueError):
        return None
    return None


def _has_tmap_item(path):
    """An ISO 21496-1 tone-map item: an 'infe' entry of item type 'tmap' in the meta box."""
    with open(path, "rb") as f:
        head = f.read(1 << 16)
    i = head.find(b"infe")
    while i >= 0:
        # infe v2/v3: version(1) flags(3) item_ID(2|4) protection(2) item_type(4)
        v = head[i + 4] if i + 4 < len(head) else 0
        t = head[i + 12:i + 16] if v == 2 else head[i + 14:i + 18]
        if t == b"tmap":
            return True
        i = head.find(b"infe", i + 4)
    return False


# ── the maths ─────────────────────────────────────────────────────────────────

def srgb_to_linear(a):
    a = np.asarray(a, dtype=np.float32)
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def weight(display_headroom, map_headroom):
    """How much of the gain map to apply: log2(display) / log2(map), clamped to [0, 1]."""
    if display_headroom <= 1.0 or map_headroom <= 1.0:
        return 0.0
    return float(min(1.0, max(0.0, np.log2(display_headroom) / np.log2(map_headroom))))


def apply_apple(sdr_u8, gain_u8, map_headroom, display_headroom):
    """Apple HDR gain map. sdr_u8: HxWx3 sRGB uint8; gain_u8: hxw uint8 (any size, resized here).
    linear HDR = linear SDR * (1 + (H - 1) * g), with g the linearised gain map and H the
    headroom actually used (map headroom scaled by the display's capacity). Returns float32
    scRGB-linear with 1.0 = SDR white."""
    sdr = srgb_to_linear(np.asarray(sdr_u8, dtype=np.float32) / 255.0)
    g = _fit_gain(gain_u8, sdr.shape[:2])
    g = srgb_to_linear(g)                                   # Apple stores the map sRGB-encoded
    w = weight(display_headroom, map_headroom)
    used = 2.0 ** (np.log2(max(1.0, map_headroom)) * w)
    return sdr * (1.0 + (used - 1.0) * g)[..., None]


def apply_iso(sdr_u8, gain_u8, gain_min_log2, gain_max_log2, cap_min_log2, cap_max_log2,
              display_headroom, gamma=1.0, offset_sdr=1 / 64, offset_hdr=1 / 64):
    """ISO 21496-1 / Ultra HDR: HDR = (SDR + o_sdr) * 2^(lerp(min, max, g^(1/gamma)) * W) - o_hdr,
    where W = clamp((log2(display) - cap_min) / (cap_max - cap_min), 0, 1)."""
    sdr = srgb_to_linear(np.asarray(sdr_u8, dtype=np.float32) / 255.0)
    g = _fit_gain(gain_u8, sdr.shape[:2]) ** (1.0 / gamma)
    span = max(1e-6, cap_max_log2 - cap_min_log2)
    w = float(min(1.0, max(0.0, (np.log2(max(1.0, display_headroom)) - cap_min_log2) / span)))
    boost = gain_min_log2 * (1.0 - g) + gain_max_log2 * g
    if g.ndim == 2:
        boost = boost[..., None]
    return (sdr + offset_sdr) * np.exp2(boost * w) - offset_hdr


def _fit_gain(gain_u8, shape):
    from PIL import Image
    g = np.asarray(gain_u8)
    if g.shape[:2] != tuple(shape):
        img = Image.fromarray(g.astype(np.uint8))
        g = np.asarray(img.resize((shape[1], shape[0]), Image.BILINEAR))
    return g.astype(np.float32) / 255.0


def load_apple_gain_map(path):
    """(sdr_u8 HxWx3, gain_u8 hxw, headroom) for an iPhone HEIC with a gain map, else None."""
    info = gain_map_info(path)
    if not info or info["kind"] != "apple":
        return None
    import pillow_heif
    h = pillow_heif.open_heif(path)
    ids = (h.info.get("aux") or {}).get(APPLE_GAIN_MAP_URN) or []
    if not ids:
        return None
    aux = h.get_aux_image(ids[0])
    gain = np.asarray(aux.to_pillow() if hasattr(aux, "to_pillow") else aux)
    sdr = np.asarray(h.to_pillow().convert("RGB"))
    return sdr, gain, info["headroom"] or 1.0
