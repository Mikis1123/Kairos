"""Camera RAW (Apple ProRAW .DNG and common RAW formats) as a Pillow plugin.

Once register() has run, every Image.open() in the app (imaging pipeline, faces, AI, deep zoom,
export) opens a RAW file like any other still:

  * Fast path: the camera's own rendering, i.e. the largest embedded JPEG preview (LibRaw
    extract_thumb). Apple ProRAW embeds a full-resolution preview rendered by the iPhone
    pipeline, so the image matches what the phone shows, and decoding costs a JPEG decode
    instead of a demosaic (measured: eval/bench_massive_scale.py is for scale; the raw tests
    pin the path choice).
  * Demosaic only when no preview reaches PREVIEW_MIN_LONG: LibRaw with the camera white
    balance, sRGB output, no auto-brightness. The DNG BaselineExposure is applied as the
    camera intends; there is no scene-dependent tone curve (the imaging.py contract).
  * Orientation: the image comes out upright (LibRaw flip / the preview's own EXIF), so callers
    see no EXIF orientation. info["raw_full_size"] = the sensor image size, upright.
  * Colour: the preview's embedded ICC (Display P3 on iPhone) travels in info["icc_profile"].
    The demosaic output is sRGB (no ICC).

Without rawpy, the plugin declines and Pillow falls back to its TIFF reader (DNG IFD0 thumbnail).
"""
import io
import os
import struct

from PIL import Image, ImageFile, ImageOps

RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".pef", ".srw"}
PREVIEW_MIN_LONG = 1600          # an embedded preview at least this long is used as the image
FORMAT = "KAIROS_RAW"
_registered = False
stats = {"preview": 0, "demosaic": 0}


def is_raw(path):
    return bool(path) and os.path.splitext(str(path))[1].lower() in RAW_EXTS


def _baseline_exposure(path):
    """DNG BaselineExposure (tag 50730, SRATIONAL, EV) from IFD0, 0.0 when absent / not a DNG."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:2] not in (b"II", b"MM"):
                return 0.0
            e = "<" if head[:2] == b"II" else ">"
            off = struct.unpack(e + "I", head[4:8])[0]
            f.seek(off)
            n = struct.unpack(e + "H", f.read(2))[0]
            for _ in range(min(n, 512)):
                tag, typ, cnt, val = struct.unpack(e + "HHI4s", f.read(12))
                if tag == 50730 and typ == 10 and cnt == 1:
                    vo = struct.unpack(e + "I", val)[0]
                    f.seek(vo)
                    num, den = struct.unpack(e + "ii", f.read(8))
                    return num / den if den else 0.0
    except (OSError, struct.error):
        pass
    return 0.0


_FLIP_TO_TRANSPOSE = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90, 6: Image.Transpose.ROTATE_270}


def decode(path, min_long=PREVIEW_MIN_LONG):
    """(PIL image RGB, icc bytes or None, upright full size (w, h), 'preview' | 'demosaic')."""
    import numpy as np
    import rawpy
    with rawpy.imread(str(path)) as raw:
        flip = int(raw.sizes.flip or 0)
        w, h = int(raw.sizes.width), int(raw.sizes.height)
        full = (h, w) if flip in (5, 6) else (w, h)
        preview = None
        try:
            th = raw.extract_thumb()
            if th.format == rawpy.ThumbFormat.JPEG:
                preview = Image.open(io.BytesIO(th.data))
                preview.load()
            elif th.format == rawpy.ThumbFormat.BITMAP:
                preview = Image.fromarray(th.data)
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError, OSError, ValueError):
            preview = None
        except Exception:                                  # any other LibRaw thumbnail failure
            preview = None
        if preview is not None and max(preview.size) >= min(min_long, max(full)):
            icc = preview.info.get("icc_profile") or None
            orient = 1
            try:
                orient = int(preview.getexif().get(274, 1) or 1)
            except Exception:
                pass
            if orient != 1:
                preview = ImageOps.exif_transpose(preview)
            elif flip in _FLIP_TO_TRANSPOSE and ((preview.width >= preview.height) != (full[0] >= full[1])
                                                 or flip == 3):
                # stored in sensor orientation: the RAW's flip turns it upright
                preview = preview.transpose(_FLIP_TO_TRANSPOSE[flip])
            stats["preview"] += 1
            return preview.convert("RGB"), icc, full, "preview"
        ev = _baseline_exposure(path) if str(path).lower().endswith(".dng") else 0.0
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=8,
                              output_color=rawpy.ColorSpace.sRGB,
                              exp_shift=float(max(0.25, min(8.0, 2.0 ** ev))), exp_preserve_highlights=1.0)
        stats["demosaic"] += 1
        return Image.fromarray(np.ascontiguousarray(rgb), "RGB"), None, full, "demosaic"


class RawImageFile(ImageFile.ImageFile):
    format = FORMAT
    format_description = "Camera RAW (LibRaw)"

    def _open(self):
        name = getattr(self.fp, "name", None) or getattr(self, "filename", None)
        if not is_raw(name):
            raise SyntaxError("not a camera RAW file")
        try:
            img, icc, full, path_used = decode(name)
        except ImportError:
            raise SyntaxError("rawpy is not available")
        except Exception as e:                             # LibRaw could not read it
            raise SyntaxError(f"LibRaw: {e}")
        self._decoded = img
        self._mode = img.mode
        self._size = img.size
        self.tile = []
        self.info["raw_full_size"] = full
        self.info["raw_path"] = path_used
        if icc:
            self.info["icc_profile"] = icc

    def load(self):
        if getattr(self, "_decoded", None) is not None:
            self.im = self._decoded.im
            self._decoded = None
        return Image.Image.load(self)


def register():
    """Register the plugin ahead of TIFF (a DNG is a TIFF): idempotent."""
    global _registered
    if _registered:
        return
    try:
        import rawpy  # noqa: F401
    except ImportError:
        _registered = True
        return
    Image.register_open(FORMAT, RawImageFile, lambda prefix: True)
    if FORMAT in Image.ID:
        Image.ID.remove(FORMAT)
    Image.ID.insert(0, FORMAT)
    for ext in RAW_EXTS:
        Image.register_extension(FORMAT, ext)
    _registered = True
