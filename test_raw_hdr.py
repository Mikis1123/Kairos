"""ProRAW / camera RAW decode (raw_decode.py) and HDR gain-map readiness (hdr.py)."""
import io
import os
import sqlite3
import struct

import numpy as np
import pytest
from PIL import Image

import hdr
import imaging
import raw_decode

rawpy = pytest.importorskip("rawpy")


def _dng(path, flip=6, baseline_ev=None):
    """A real Bayer DNG (RGGB, 12 bit): left half warm, right half cool."""
    tifffile = pytest.importorskip("tifffile")
    H, W = 64, 96
    rgb = np.zeros((H, W, 3), np.float32)
    rgb[:, :W // 2] = (0.6, 0.25, 0.1)
    rgb[:, W // 2:] = (0.1, 0.25, 0.6)
    cfa = np.zeros((H, W), np.uint16)
    cfa[0::2, 0::2] = rgb[0::2, 0::2, 0] * 4000
    cfa[0::2, 1::2] = rgb[0::2, 1::2, 1] * 4000
    cfa[1::2, 0::2] = rgb[1::2, 0::2, 1] * 4000
    cfa[1::2, 1::2] = rgb[1::2, 1::2, 2] * 4000
    ident = []
    for v in (1, 0, 0, 0, 1, 0, 0, 0, 1):
        ident += [v * 10000, 10000]
    tags = [(33421, "H", 2, (2, 2), True), (33422, "B", 4, (0, 1, 1, 2), True), (50706, "B", 4, (1, 4, 0, 0), True),
            (50708, "s", 0, "Kairos Synthetic", True), (50717, "I", 1, 4095, True), (50714, "I", 1, 0, True),
            (50721, "2i", 9, ident, True), (50778, "H", 1, 21, True),
            (50728, "2I", 3, [10000] * 6, True), (274, "H", 1, flip, True), (271, "s", 0, "Apple", True)]
    if baseline_ev is not None:
        tags.append((50730, "2i", 1, [int(baseline_ev * 100), 100], True))
    tifffile.imwrite(str(path), cfa, photometric=32803, extratags=tags, subfiletype=0)
    return str(path)


def test_dng_opens_upright_through_every_image_open(tmp_path):
    p = _dng(tmp_path / "IMG_0001.DNG", flip=6)
    imaging.register_codecs()
    with Image.open(p) as im:
        assert im.format == raw_decode.FORMAT
        assert im.size == (64, 96), "flip 6 (90 deg CW) applied: portrait"
        assert im.info["raw_full_size"] == (64, 96)
        im.load()
        a = np.asarray(im.convert("RGB"), dtype=np.int32)
    top, bottom = a[5, 32], a[90, 32]
    assert top[0] > top[2] and bottom[2] > bottom[0], "the warm half is on top after the turn"


def test_imaging_pipeline_renders_a_dng(tmp_path):
    p = _dng(tmp_path / "x.dng", flip=1)
    dec = imaging.open_oriented(p)
    assert dec.full_size == (96, 64) and dec.image.mode == "RGB" and dec.icc is None
    thumb = imaging.render_still(p, size_fn=lambda w, h: imaging.fit_box(w, h, 48, 48))
    assert thumb.size == (48, 32)


def test_baseline_exposure_is_read_and_applied(tmp_path):
    flat = _dng(tmp_path / "a.dng", flip=1)
    lifted = _dng(tmp_path / "b.dng", flip=1, baseline_ev=1.0)
    assert raw_decode._baseline_exposure(flat) == 0.0
    assert raw_decode._baseline_exposure(lifted) == pytest.approx(1.0)
    m0 = np.asarray(raw_decode.decode(flat)[0], dtype=np.float32).mean()
    m1 = np.asarray(raw_decode.decode(lifted)[0], dtype=np.float32).mean()
    assert m1 > m0 * 1.3, "+1 EV as the camera asks, no auto-brightness"


class _Thumb:
    def __init__(self, data):
        self.format, self.data = rawpy.ThumbFormat.JPEG, data


class _FakeRaw:
    def __init__(self, thumb_size, flip, sensor=(4032, 3024)):
        buf = io.BytesIO()
        img = Image.new("RGB", thumb_size, (200, 30, 30))
        img.paste((30, 30, 200), (0, 0, thumb_size[0] // 2, thumb_size[1]))     # left half blue
        img.save(buf, "JPEG", quality=95)
        self._jpeg = buf.getvalue()

        class S:
            pass
        self.sizes = S()
        self.sizes.flip, self.sizes.width, self.sizes.height = flip, sensor[0], sensor[1]

    def extract_thumb(self):
        return _Thumb(self._jpeg)

    def postprocess(self, **kw):                     # must not be reached on the preview path
        raise AssertionError("demosaic used although the preview was big enough")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_prores_preview_is_the_fast_path_and_turns_upright(monkeypatch, tmp_path):
    p = tmp_path / "IMG_9000.DNG"
    p.write_bytes(b"II*\x00" + b"\0" * 64)
    monkeypatch.setattr(rawpy, "imread", lambda path: _FakeRaw((4032, 3024), flip=6))
    img, icc, full, used = raw_decode.decode(str(p))
    assert used == "preview" and full == (3024, 4032)
    assert img.size == (3024, 4032), "sensor-orientation preview turned by the RAW flip"
    top = img.getpixel((1500, 10))
    assert top[2] > top[0], "left (blue) half of the landscape preview is on top after 90 deg CW"


def test_small_preview_falls_back_to_demosaic(monkeypatch, tmp_path):
    p = tmp_path / "small.DNG"
    p.write_bytes(b"II*\x00" + b"\0" * 64)
    calls = []

    class R(_FakeRaw):
        def postprocess(self, **kw):
            calls.append(kw)
            return np.zeros((3024, 4032, 3), np.uint8)
    monkeypatch.setattr(rawpy, "imread", lambda path: R((320, 240), flip=0))
    img, icc, full, used = raw_decode.decode(str(p))
    assert used == "demosaic" and img.size == (4032, 3024)
    kw = calls[0]
    assert kw["use_camera_wb"] and kw["no_auto_bright"], "camera rendering intent, no scene-dependent brightening"


def test_non_raw_files_are_untouched():
    imaging.register_codecs()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "TIFF")
    buf.seek(0)
    assert Image.open(buf).format == "TIFF"
    assert not raw_decode.is_raw("a.tif") and raw_decode.is_raw("B.DNG") and raw_decode.is_raw("c.cr3")


# ── HDR ─────────────────────────────────────────────────────────────────────────

def test_display_state_reports_every_active_display():
    for d in hdr.display_state():
        assert {"hdr_supported", "hdr_enabled", "sdr_white_nits", "max_nits", "headroom"} <= set(d)
        assert d["sdr_white_nits"] > 0
        assert d["headroom"] >= 1.0
        if not d["hdr_enabled"]:
            assert d["headroom"] == 1.0, "SDR desktop: no highlight expansion"


def test_gain_map_weight_and_apple_math():
    assert hdr.weight(1.0, 6.8) == 0.0
    assert hdr.weight(6.8, 6.8) == 1.0 and hdr.weight(20, 6.8) == 1.0
    assert hdr.weight(2.0, 4.0) == pytest.approx(0.5)
    sdr = np.full((4, 4, 3), 255, np.uint8)
    gain = np.array([[0, 255], [128, 255]], np.uint8)
    out = hdr.apply_apple(sdr, gain, map_headroom=4.0, display_headroom=1.0)
    assert np.allclose(out, 1.0), "no headroom: exactly the SDR image"
    out = hdr.apply_apple(sdr, gain, map_headroom=4.0, display_headroom=4.0)
    assert out.max() == pytest.approx(4.0, rel=1e-4), "full gain = the map's headroom"
    assert out[0, 0, 0] == pytest.approx(1.0), "zero gain leaves SDR white at 1.0"
    half = hdr.apply_apple(sdr, gain, map_headroom=4.0, display_headroom=2.0)
    assert half.max() == pytest.approx(2.0, rel=1e-4), "a 2x display gets half the stops"


def test_iso_21496_math():
    sdr = np.full((2, 2, 3), 128, np.uint8)
    gain = np.full((2, 2), 255, np.uint8)
    base = hdr.apply_iso(sdr, gain, 0.0, 2.0, 0.0, 2.0, display_headroom=1.0)
    ref = hdr.srgb_to_linear(128 / 255.0)
    assert np.allclose(base, ref, atol=1e-5), "no headroom: SDR"
    full = hdr.apply_iso(sdr, gain, 0.0, 2.0, 0.0, 2.0, display_headroom=4.0)
    assert np.allclose(full, (ref + 1 / 64) * 4 - 1 / 64, atol=1e-4), "+2 stops at full weight"


def test_gain_map_detection_from_bytes(tmp_path):
    jpg = tmp_path / "uhdr.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe1" + b"<x hdrgm:Version=\"1.0\"/>" + b"\0" * 100)
    assert hdr.gain_map_info(str(jpg))["kind"] == "ultrahdr"
    iso = tmp_path / "iso.jpg"
    iso.write_bytes(b"\xff\xd8" + hdr.ISO_URN + b"\0" * 100)
    assert hdr.gain_map_info(str(iso))["kind"] == "iso"
    plain = tmp_path / "p.jpg"
    Image.new("RGB", (4, 4)).save(plain, "JPEG")
    assert hdr.gain_map_info(str(plain)) is None


def _library_heic():
    try:
        import paths
        conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
        for (p,) in conn.execute("SELECT file_path FROM media_items WHERE lower(file_ext) = '.heic' LIMIT 20"):
            if os.path.exists(p):
                return p
    except Exception:
        return None
    return None


def test_iphone_heic_gain_map_is_found():
    p = _library_heic()
    if not p:
        pytest.skip("no iPhone HEIC in the library")
    info = hdr.gain_map_info(p)
    assert info and info["kind"] == "apple"
    assert info["headroom"] and 1.0 < info["headroom"] < 16.0
    assert info["size"] and info["size"][0] > 100
