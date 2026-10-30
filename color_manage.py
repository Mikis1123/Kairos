"""Správa barev (Stage 2): fotky z iPhonu jsou v Display P3 a kreslit se mají v barvách displeje.

Dřív aplikace žádný profil neuplatnila – P3 hodnoty šly na displej tak, jak byly. Teď každý
obraz, který aplikace zobrazí nebo uloží do cache (miniatura, náhled, prohlížeč, ořez tváře),
projde převodem z vloženého profilu do VÝSTUPNÍHO profilu:

* Windows barvy spravuje sám (Advanced Color: automatická správa barev / HDR) -> sRGB,
  o zbytek se postará systém.
* Displej má přiřazený vlastní profil ICC (kalibrace) -> ten.
* Jinak displej se širokým gamutem (OLED, EDID primárky výrazně za sRGB) -> profil sestavený
  z EDID (jako macOS). Systém sRGB obsah na takovém panelu NEpřevádí, takže převod do sRGB
  by fotky přesytil.
* Jinak sRGB.

Cache (miniatury, náhledy) se renderují do výstupního profilu jednou; jeho otisk
(`target_fingerprint`) je součástí verze cache – jiný displej / nastavení = přegenerování.
Videa (a obrázky z nich: plakáty, pás časové osy) se NEpřevádějí – přehrávač Qt profil
neuplatňuje a plakát musí barevně navazovat na první snímek videa.

Bezpečné mimo GUI vlákno (po `configure()` z GUI vlákna při startu).
"""
import hashlib
import io
import math
import struct
import threading

from PIL import Image, ImageCms

MODE_AUTO = "auto"
MODE_SRGB = "srgb"

_SRGB = ImageCms.createProfile("sRGB")
_transforms = {}
_lock = threading.Lock()
_target = {"icc": None, "name": "sRGB", "reason": "výchozí", "fp": "srgb"}   # icc None = sRGB

# ── ICC profil z primárek (EDID) ──────────────────────────────────────────────

_D50 = (0.9642, 1.0, 0.8249)
_SRGB_XY = ((0.640, 0.330), (0.300, 0.600), (0.150, 0.060), (0.3127, 0.3290))
_BRADFORD = ((0.8951, 0.2664, -0.1614), (-0.7502, 1.7135, 0.0367), (0.0389, -0.0685, 1.0296))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _mat_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def _mat_inv(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    return ((( e * i - f * h) / det, -(b * i - c * h) / det, ( b * f - c * e) / det),
            (-(d * i - f * g) / det, ( a * i - c * g) / det, -(a * f - c * d) / det),
            (( d * h - e * g) / det, -(a * h - b * g) / det, ( a * e - b * d) / det))


def _xy_to_xyz(x, y):
    return (x / y, 1.0, (1.0 - x - y) / y)


def _rgb_to_xyz_d50(primaries):
    """Matice RGB -> XYZ (D50, Bradford) z chromatičností (r, g, b, bílá)."""
    (rx, ry), (gx, gy), (bx, by), (wx, wy) = primaries
    cols = [_xy_to_xyz(rx, ry), _xy_to_xyz(gx, gy), _xy_to_xyz(bx, by)]
    m = tuple(tuple(cols[j][i] for j in range(3)) for i in range(3))
    white = _xy_to_xyz(wx, wy)
    s = _mat_vec(_mat_inv(m), white)
    m = tuple(tuple(m[i][j] * s[j] for j in range(3)) for i in range(3))
    src = _mat_vec(_BRADFORD, white)
    dst = _mat_vec(_BRADFORD, _D50)
    scale = ((dst[0] / src[0], 0, 0), (0, dst[1] / src[1], 0), (0, 0, dst[2] / src[2]))
    adapt = _mat_mul(_mat_inv(_BRADFORD), _mat_mul(scale, _BRADFORD))
    return _mat_mul(adapt, m)


def _s15(v):
    return struct.pack(">i", int(round(v * 65536.0)))


def _xyz_tag(xyz):
    return b"XYZ " + b"\0" * 4 + b"".join(_s15(c) for c in xyz)


def _desc_tag(text):
    a = text.encode("ascii", "replace") + b"\0"
    return b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + b"\0" * 4 + b"\0" * 4 + b"\0" * 2 + b"\0" + b"\0" * 67


def _text_tag(text):
    return b"text" + b"\0" * 4 + text.encode("ascii", "replace") + b"\0"


def _srgb_trc_tag():
    # parametrická křivka typu 3 = přenosová funkce sRGB
    g, a, b, c, d = 2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045
    return b"para" + b"\0" * 4 + struct.pack(">HH", 3, 0) + b"".join(_s15(v) for v in (g, a, b, c, d))


def build_matrix_profile(primaries, description):
    """Minimální ICC v2.4 profil displeje (matice + křivka sRGB) z chromatičností."""
    m = _rgb_to_xyz_d50(primaries)
    tags = [(b"desc", _desc_tag(description)), (b"cprt", _text_tag("No copyright, use freely")),
            (b"wtpt", _xyz_tag(_D50)),
            (b"rXYZ", _xyz_tag((m[0][0], m[1][0], m[2][0]))),
            (b"gXYZ", _xyz_tag((m[0][1], m[1][1], m[2][1]))),
            (b"bXYZ", _xyz_tag((m[0][2], m[1][2], m[2][2])))]
    trc = _srgb_trc_tag()
    table, data = [], b""
    offset = 128 + 4 + 12 * (len(tags) + 3)
    for sig, body in tags:
        while len(body) % 4:
            body += b"\0"
        table.append((sig, offset + len(data), len(body)))
        data += body
    trc_off = offset + len(data)
    for sig in (b"rTRC", b"gTRC", b"bTRC"):             # sdílená data jedné křivky
        table.append((sig, trc_off, len(trc)))
    data += trc
    size = offset + len(data)
    header = (struct.pack(">I", size) + b"lcms" + struct.pack(">I", 0x02400000) + b"mntrRGB XYZ "
              + struct.pack(">6H", 2026, 1, 1, 0, 0, 0) + b"acspMSFT" + b"\0" * 4 + b"\0" * 8 + b"\0" * 8
              + struct.pack(">I", 0) + b"".join(_s15(c) for c in _D50) + b"\0" * 4 + b"\0" * 16 + b"\0" * 28)
    assert len(header) == 128
    tt = struct.pack(">I", len(table)) + b"".join(sig + struct.pack(">II", o, n) for sig, o, n in table)
    return header + tt + data


def gamut_area_ratio(primaries):
    """Plocha trojúhelníku primárek v xy proti sRGB (1.0 = sRGB, Display P3 ~1.36)."""
    def area(p):
        (x1, y1), (x2, y2), (x3, y3) = p[:3]
        return abs(x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2)) / 2.0
    return area(primaries) / area(_SRGB_XY)


# ── zjištění displeje (Windows) ───────────────────────────────────────────────

def _edid_primaries(edid):
    lo1, lo2 = edid[25], edid[26]
    b = edid[27:35]

    def v(hi, lo):
        return ((hi << 2) | lo) / 1024.0
    return ((v(b[0], lo1 >> 6 & 3), v(b[1], lo1 >> 4 & 3)), (v(b[2], lo1 >> 2 & 3), v(b[3], lo1 & 3)),
            (v(b[4], lo2 >> 6 & 3), v(b[5], lo2 >> 4 & 3)), (v(b[6], lo2 >> 2 & 3), v(b[7], lo2 & 3)))


def _internal_display_edid():
    """EDID vestavěného (nebo jediného aktivního) displeje z registru, nebo None."""
    try:
        import winreg
    except ImportError:
        return None
    base = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
    found = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return None
    i = 0
    while True:
        try:
            mon = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            mk = winreg.OpenKey(root, mon)
        except OSError:
            continue
        j = 0
        while True:
            try:
                inst = winreg.EnumKey(mk, j)
            except OSError:
                break
            j += 1
            try:
                k = winreg.OpenKey(mk, inst + r"\Device Parameters")
                edid = bytes(winreg.QueryValueEx(k, "EDID")[0])
            except OSError:
                continue
            if len(edid) >= 128 and edid[:8] == b"\x00\xff\xff\xff\xff\xff\xff\x00":
                found.append(edid)
    if not found:
        return None
    # notebook: vestavěný panel má ve výčtu jen jednu instanci; bez dalších informací první
    return found[0] if len(found) == 1 else None


def _advanced_color_active():
    """True = Windows barvy spravuje sám (automatická správa barev nebo HDR) na některém
    aktivním displeji; None = nelze zjistit."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        QDC_ONLY_ACTIVE_PATHS = 2
        n_paths, n_modes = wintypes.UINT(0), wintypes.UINT(0)
        if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)):
            return None
        paths = (ctypes.c_byte * (72 * n_paths.value))()
        modes = (ctypes.c_byte * (64 * n_modes.value))()
        if user32.QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths,
                                     ctypes.byref(n_modes), modes, None):
            return None
        raw = bytes(paths)
        active = False
        for p in range(n_paths.value):
            low, high, target_id = struct.unpack_from("<Iil", raw, p * 72 + 20)
            for info_type, size in ((15, 40), (9, 32)):      # GET_ADVANCED_COLOR_INFO_2, pak _INFO
                buf = (ctypes.c_byte * size)()
                struct.pack_into("<IIIiI", buf, 0, info_type, size, low, high, target_id & 0xFFFFFFFF)
                if user32.DisplayConfigGetDeviceInfo(buf) == 0:
                    vals = struct.unpack_from("<I", bytes(buf), 20)[0]
                    if info_type == 15:
                        mode = struct.unpack_from("<I", bytes(buf), 36)[0]
                        active = active or mode in (1, 2)      # WCG (ACM) / HDR
                    else:
                        active = active or bool(vals & 0x2)    # advancedColorEnabled
                    break
        return active
    except Exception:
        return None


def _assigned_icc():
    """Profil ICC přiřazený displeji ve Windows (bajty) – None, když je to obecný sRGB."""
    try:
        import ctypes
        from ctypes import wintypes
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            n = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not ctypes.windll.gdi32.GetICMProfileW(hdc, ctypes.byref(n), buf):
                return None
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        with open(buf.value, "rb") as f:
            icc = f.read()
        return None if is_srgb_profile(icc) else icc
    except Exception:
        return None


def detect_output(mode=MODE_AUTO):
    """(icc_bytes | None = sRGB, popis, důvod) pro výstup na hlavní displej."""
    if mode == MODE_SRGB:
        return None, "sRGB", "nastaveno ručně"
    acm = _advanced_color_active()
    if acm:
        return None, "sRGB", "barvy spravuje Windows (automatická správa barev / HDR)"
    icc = _assigned_icc()
    if icc:
        return icc, _describe(icc), "profil přiřazený displeji ve Windows"
    edid = _internal_display_edid()
    if edid:
        prim = _edid_primaries(edid)
        ratio = gamut_area_ratio(prim)
        if ratio > 1.15:
            name = f"Displej (EDID, {ratio * 100:.0f} % sRGB)"
            return build_matrix_profile(prim, name), name, "displej se širokým gamutem bez správy barev"
    return None, "sRGB", "displej v rozsahu sRGB"


def configure(mode=MODE_AUTO):
    """Zvolí výstupní profil (volat z GUI vlákna při startu a po změně nastavení).
    Vrací slovník s popisem cíle."""
    icc, name, reason = detect_output(mode)
    fp = "srgb" if icc is None else hashlib.sha1(icc).hexdigest()[:8]
    with _lock:
        _target.update(icc=icc, name=name, reason=reason, fp=fp)
        _transforms.clear()
    return dict(_target)


def target():
    return dict(_target)


def target_fingerprint():
    """Krátký otisk výstupního profilu – součást verzí miniatur a náhledů."""
    return _target["fp"]


# ── převody ───────────────────────────────────────────────────────────────────

def _describe(icc):
    try:
        return ImageCms.getProfileDescription(ImageCms.ImageCmsProfile(io.BytesIO(icc))).strip() or "ICC"
    except Exception:
        return "ICC"


def is_srgb_profile(icc):
    """True = profil chybí nebo popisuje sRGB."""
    if not icc:
        return True
    desc = _describe(icc)
    return "srgb" in desc.lower().replace(" ", "").replace("-", "")


def _transform_for(src_icc, mode):
    """LittleCMS transformace zdroj -> výstup (cache podle profilů), None = beze změny."""
    dst_icc = _target["icc"]
    src_is_srgb = is_srgb_profile(src_icc)
    if dst_icc is None and src_is_srgb:
        return None
    key = (hashlib.sha1(src_icc).hexdigest() if not src_is_srgb else "srgb", _target["fp"], mode)
    with _lock:
        if key in _transforms:
            return _transforms[key]
        try:
            src = _SRGB if src_is_srgb else ImageCms.ImageCmsProfile(io.BytesIO(src_icc))
            dst = _SRGB if dst_icc is None else ImageCms.ImageCmsProfile(io.BytesIO(dst_icc))
            tr = ImageCms.buildTransform(src, dst, mode, mode,
                                         renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
        except Exception:
            tr = None                    # vadný profil: zapamatovat, ať se to nezkouší pořád
        _transforms[key] = tr
        return tr


def source_profile(img):
    """Vložený ICC profil obrázku Pillow (bajty) nebo None. Číst HNED po otevření – některé
    operace (exif_transpose, convert) info nepřenášejí."""
    try:
        return img.info.get("icc_profile") or None
    except Exception:
        return None


def pil_to_output(img, icc=None):
    """Obrázek Pillow (RGB/RGBA) z vloženého profilu (bez profilu = sRGB) do výstupního."""
    icc = icc if icc is not None else source_profile(img)
    if img.mode not in ("RGB", "RGBA"):
        return img
    tr = _transform_for(icc, img.mode)
    if tr is None:
        return img
    try:
        out = ImageCms.applyTransform(img, tr)
    except Exception:
        return img
    out.info.pop("icc_profile", None)
    return out


def qimage_to_output(img):
    """QImage (profil z JPEG/PNG přes QImageReader, bez profilu = sRGB) do výstupního profilu."""
    try:
        if img.isNull():
            return img
        cs = img.colorSpace()
        icc = bytes(cs.iccProfile()) if cs.isValid() else b""
        if not icc and cs.isValid():
            from PyQt6.QtGui import QColorSpace
            if cs != QColorSpace(QColorSpace.NamedColorSpace.SRgb):
                return img.convertedToColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb)) \
                    if _target["icc"] is None else img
        tr = _transform_for(icc or None, "RGB")
        if tr is None:
            return img
        from PyQt6.QtGui import QImage
        rgb = img.convertToFormat(QImage.Format.Format_RGB888)
        w, h = rgb.width(), rgb.height()
        ptr = rgb.constBits()
        ptr.setsize(rgb.sizeInBytes())
        pil = Image.frombuffer("RGB", (w, h), bytes(ptr), "raw", "RGB", rgb.bytesPerLine(), 1)
        out = ImageCms.applyTransform(pil, tr)
        res = QImage(out.tobytes("raw", "RGB"), w, h, w * 3, QImage.Format.Format_RGB888).copy()
        return res
    except Exception:
        return img


def open_rgb_output(path, draft=None):
    """Otevře soubor Pillowem, otočí podle EXIF a vrátí RGB ve výstupním profilu."""
    from PIL import ImageOps
    with Image.open(path) as img:
        icc = source_profile(img)
        if draft:
            try:
                img.draft("RGB", draft)
            except Exception:
                pass
        oriented = ImageOps.exif_transpose(img)
        return pil_to_output(oriented.convert("RGB"), icc)



def is_cache_file(path):
    """Soubor z cache aplikace (miniatura, náhled, ořez) je už ve výstupním profilu."""
    try:
        import os
        import paths
        root = os.path.normcase(os.path.abspath(paths.CACHE_ROOT))
        return os.path.normcase(os.path.abspath(path)).startswith(root + os.sep)
    except Exception:
        return False
