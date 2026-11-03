"""QuickTime / ISO-BMFF clean aperture (`clap`, `tapt/clef`) and one framing for every surface.

iPhone Live Photo motion videos are coded at 1920x1440, and their video track declares a clean
aperture of 1744x1308 (all 125 Live Photos in the library; 80 are rotated 90 deg, 17 are
rotated 180 deg). The two decoders on screen disagree about it:
  * Qt Multimedia plays the full coded frame (measured, Qt 6.11: 1920x1440, full viewport).
  * ffmpeg >= 7.1 applies the clean aperture by default. Scrub strips (viewer scrubbing,
    grid hover) and Journey card clips came out 1744x1308, i.e. zoomed in ~10 %.
Measured (eval/bench_live_framing.py: the still template-matched into the video frame at the
still moment, scale search 0.86-1.0): the still covers 0.98-1.00 of the FULL coded frame, not
the 0.908 clean aperture. So the full frame is the framing that matches the still and what Qt
plays. Every ffmpeg decode that is shown next to the still or the player keeps the full frame
(FULL_FRAME_ARGS: `-apply_cropping codec` keeps the codec's conformance window and skips the
container `clap`). The residual 0-2 % is per-clip stabilisation, which no static metadata
describes.

parse(path) reads only the moov box (a few kB from the start or end of the file).
"""
import math
import os
import struct
from collections import OrderedDict

_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"tapt", b"edts", b"udta"}
_VIDEO_ENTRY_HEADER = 78           # VisualSampleEntry fields after the 8-byte box header
_CACHE = OrderedDict()
_CACHE_MAX = 256
MIN_CROP = 0.002                   # under 0.2 % per side: treat as no crop (rounding in muxers)
FULL_FRAME_ARGS = ["-apply_cropping", "codec"]     # ffmpeg input option, since 7.1
_FFMPEG_OK = {}


class Crop:
    """x, y, w, h: visible rectangle in [0, 1], relative to the displayed (rotated) frame.
    aspect: displayed width / height of the FULL coded frame. rotation: 0/90/180/270."""
    __slots__ = ("x", "y", "w", "h", "aspect", "rotation", "coded", "clean")

    def __init__(self, x, y, w, h, aspect, rotation=0, coded=(0, 0), clean=(0, 0)):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.aspect, self.rotation, self.coded, self.clean = aspect, rotation, coded, clean

    @property
    def is_identity(self):
        return self.w >= 1 - 2 * MIN_CROP and self.h >= 1 - 2 * MIN_CROP

    def as_tuple(self):
        return (round(self.x, 6), round(self.y, 6), round(self.w, 6), round(self.h, 6))

    def __repr__(self):
        return (f"Crop(x={self.x:.4f}, y={self.y:.4f}, w={self.w:.4f}, h={self.h:.4f}, "
                f"aspect={self.aspect:.4f}, rot={self.rotation}, coded={self.coded}, clean={self.clean})")


def _boxes(f, start, end):
    """(type, payload_start, box_end) for each box in [start, end)."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        hdr = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield kind, pos + hdr, pos + size
        pos += size


def _find(f, start, end, kind):
    for k, a, b in _boxes(f, start, end):
        if k == kind:
            return a, b
    return None


def _rotation(matrix):
    a, b = matrix[0] / 65536.0, matrix[1] / 65536.0
    deg = int(round(math.degrees(math.atan2(b, a)))) % 360
    return min((0, 90, 180, 270), key=lambda r: min(abs(deg - r), 360 - abs(deg - r)))


def _parse_trak(f, a, b):
    """{'rotation', 'coded': (w, h), 'clap': (cw, ch, ho, vo) | None, 'clef': (w, h) | None,
    'pasp': (h, v) | None} for a video track, None for anything else."""
    mdia = _find(f, a, b, b"mdia")
    if not mdia:
        return None
    hdlr = _find(f, mdia[0], mdia[1], b"hdlr")
    if not hdlr:
        return None
    f.seek(hdlr[0] + 8)
    if f.read(4) != b"vide":
        return None
    out = {"rotation": 0, "coded": None, "clap": None, "clef": None, "pasp": None}
    tkhd = _find(f, a, b, b"tkhd")
    if tkhd:
        f.seek(tkhd[0])
        version = f.read(1)[0]
        f.seek(tkhd[0] + (40 if version == 0 else 52))     # to the matrix
        m = struct.unpack(">9i", f.read(36))
        out["rotation"] = _rotation(m)
    tapt = _find(f, a, b, b"tapt")
    if tapt:
        clef = _find(f, tapt[0], tapt[1], b"clef")
        if clef:
            f.seek(clef[0] + 4)
            w, h = struct.unpack(">II", f.read(8))
            out["clef"] = (w / 65536.0, h / 65536.0)
    minf = _find(f, mdia[0], mdia[1], b"minf")
    stbl = minf and _find(f, minf[0], minf[1], b"stbl")
    stsd = stbl and _find(f, stbl[0], stbl[1], b"stsd")
    if stsd:
        entry = next(_boxes(f, stsd[0] + 8, stsd[1]), None)   # first sample entry
        if entry:
            _, ea, eb = entry
            f.seek(ea + 24)
            out["coded"] = struct.unpack(">HH", f.read(4))
            for k, ca, cb in _boxes(f, ea + _VIDEO_ENTRY_HEADER, eb):
                f.seek(ca)
                if k == b"clap" and cb - ca >= 32:
                    wn, wd, hn, hd = struct.unpack(">IIII", f.read(16))
                    hon, hod, von, vod = struct.unpack(">iIiI", f.read(16))
                    if wd and hd and hod and vod:
                        out["clap"] = (wn / wd, hn / hd, hon / hod, von / vod)
                elif k == b"pasp" and cb - ca >= 8:
                    hs, vs = struct.unpack(">II", f.read(8))
                    if hs and vs:
                        out["pasp"] = (hs, vs)
    return out


def _from_track(t):
    if not t or not t["coded"] or not all(t["coded"]):
        return None
    W, H = t["coded"]
    if t["clap"]:
        cw, ch, ho, vo = t["clap"]
    elif t["clef"]:
        cw, ch = t["clef"]
        ho = vo = 0.0
        # clef is in display units (pixel aspect applied): back to coded pixels
        if t["pasp"]:
            cw = cw * t["pasp"][1] / t["pasp"][0]
    else:
        cw, ch, ho, vo = W, H, 0.0, 0.0
    cw, ch = min(cw, W), min(ch, H)
    x = (W / 2.0 + ho - cw / 2.0) / W
    y = (H / 2.0 + vo - ch / 2.0) / H
    w, h = cw / W, ch / H
    x, y = max(0.0, min(1.0 - w, x)), max(0.0, min(1.0 - h, y))
    par = (t["pasp"][0] / t["pasp"][1]) if t["pasp"] else 1.0
    aspect = W * par / H
    rot = t["rotation"]
    # coded -> displayed orientation (the player rotates the frame by the track matrix)
    if rot == 90:
        x, y, w, h = 1.0 - (y + h), x, h, w
        aspect = 1.0 / aspect
    elif rot == 180:
        x, y = 1.0 - (x + w), 1.0 - (y + h)
    elif rot == 270:
        x, y, w, h = y, 1.0 - (x + w), h, w
        aspect = 1.0 / aspect
    return Crop(x, y, w, h, aspect, rot, (W, H), (round(cw), round(ch)))


def parse(path):
    """Crop of the first video track, or None (not a QuickTime/MP4 file, no video track)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            moov = _find(f, 0, size, b"moov")
            if not moov:
                return None
            for k, a, b in _boxes(f, moov[0], moov[1]):
                if k == b"trak":
                    t = _parse_trak(f, a, b)
                    if t:
                        return _from_track(t)
    except (OSError, struct.error, IndexError, ValueError):
        return None
    return None


def clean_aperture(path):
    """Cached parse(); None when there is nothing to crop (no clean aperture, or a full one)."""
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.normcase(os.path.abspath(path)), st.st_size, int(st.st_mtime))
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    crop = parse(path)
    if crop is not None and crop.is_identity:
        crop = None
    _CACHE[key] = crop
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return crop


def ffmpeg_full_frame_args(ffmpeg_path):
    """Input options that make ffmpeg decode the full coded frame like Qt does (see module doc).
    [] for an ffmpeg older than 7.1: it never applied the container crop, and it would reject
    the option."""
    if not ffmpeg_path:
        return []
    if ffmpeg_path not in _FFMPEG_OK:
        ok = False
        try:
            import re
            import subprocess
            out = subprocess.run([ffmpeg_path, "-hide_banner", "-version"], capture_output=True, timeout=10,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            m = re.search(rb"ffmpeg version n?(\d+)\.(\d+)", out)
            ok = bool(m) and (int(m.group(1)), int(m.group(2))) >= (7, 1)
        except (OSError, ValueError, Exception):
            ok = False
        _FFMPEG_OK[ffmpeg_path] = ok
    return list(FULL_FRAME_ARGS) if _FFMPEG_OK[ffmpeg_path] else []
