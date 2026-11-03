"""Golden master – the native image pipeline (imaging.py + color_manage display contract).

Invariant guards that must never regress:
  1. One colour conversion, never two; untagged / sRGB pass through bit-exact; cache files are
     never converted again; neutral greys keep their exact level (= no tone curve anywhere).
  2. No chroma stripping: Display P3 saturates fully in the Windows contract, skin tones are
     colorimetrically exact, ramps keep every level, 16-bit sources do not wash out.
  3. Crispness: avatars and thumbnails are measurably sharper than the old INTER_AREA/bilinear
     path, sharpening is luma-only (chroma bit-exact) and halo-bounded.
  4. Retired stages stay retired: no Live tone module, cache versions bumped, 4:4:4 caches,
     face crops re-rendered from originals (never re-converted) when the contract changes.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageCms, ImageDraw, JpegImagePlugin

import color_manage
import database
import face_engine
import imaging
import paths
import preview_cache
import scanner

try:
    from PyQt6 import QtWebEngineWidgets  # noqa: F401 – before QApplication (tests import main)
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QImage, QImageReader
    from PyQt6.QtWidgets import QApplication
    import main                               # before the QApplication exists (WebEngine attrs)
    import viewer_hud
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False

P3_XY = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290))
P3_ICC = color_manage.build_matrix_profile(P3_XY, "Display P3")
SRGB_ICC = ImageCms.ImageCmsProfile(color_manage._SRGB).tobytes()

# golden patches in Display P3 code values
PATCHES = {
    "p3_red": (255, 0, 0),
    "p3_green": (0, 255, 0),
    "skin_light": (226, 178, 150),
    "skin_dark": (141, 95, 72),
    "lake_blue": (40, 96, 150),
    "foliage": (70, 120, 50),
    "grey_18": (118, 118, 118),
    "white": (255, 255, 255),
}
PATCH = 160                                    # big flat patches: centres unaffected by resampling

_APP = []


def app():
    if not _APP:
        _APP.append(QApplication.instance() or QApplication([]))
    return _APP[0]


def patch_chart(scale=1, cols=None):
    """Flat-patch chart (cols per row, default one row)."""
    cols = cols or len(PATCHES)
    rows = -(-len(PATCHES) // cols)
    p = PATCH * scale
    img = Image.new("RGB", (p * cols, p * rows))
    d = ImageDraw.Draw(img)
    for i, rgb in enumerate(PATCHES.values()):
        x, y = i % cols, i // cols
        d.rectangle((x * p, y * p, (x + 1) * p - 1, (y + 1) * p - 1), fill=rgb)
    return img


def centres(img, cols=None):
    cols = cols or len(PATCHES)
    rows = -(-len(PATCHES) // cols)
    w, h = img.size
    rgb = img.convert("RGB")
    return {k: rgb.getpixel((int((i % cols + .5) * w / cols), int((i // cols + .5) * h / rows)))
            for i, k in enumerate(PATCHES)}


def pixels(img):
    return [tuple(int(c) for c in p) for p in np.asarray(img.convert("RGB")).reshape(-1, 3)]


def single_conversion(rgb_values, icc=P3_ICC):
    """Reference: exactly one LittleCMS conversion P3 -> sRGB (relative colorimetric)."""
    src = Image.new("RGB", (len(rgb_values), 1))
    src.putdata(list(rgb_values))
    tr = ImageCms.buildTransform(ImageCms.ImageCmsProfile(io.BytesIO(icc)), color_manage._SRGB,
                                 "RGB", "RGB", renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
    return pixels(ImageCms.applyTransform(src, tr))


def sat(rgb):
    a = np.asarray(rgb, dtype=np.float64).reshape(-1, 3) / 255.0
    mx, mn = a.max(1), a.min(1)
    return float(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-9), 0).mean())


def to_lab(rgb_values, icc):
    src = Image.new("RGB", (len(rgb_values), 1))
    src.putdata(list(rgb_values))
    prof = ImageCms.ImageCmsProfile(io.BytesIO(icc)) if icc else color_manage._SRGB
    tr = ImageCms.buildTransform(prof, ImageCms.createProfile("LAB"), "RGB", "LAB")
    lab = np.asarray(ImageCms.applyTransform(src, tr), dtype=np.float64).reshape(-1, 3)
    return np.stack([lab[:, 0] * 100 / 255, lab[:, 1] - 128, lab[:, 2] - 128], 1)


def grad_energy(arr):
    g = np.asarray(arr, dtype=np.float64)
    if g.ndim == 3:
        g = g @ [0.2126, 0.7152, 0.0722]
    return float(np.mean(np.abs(np.diff(g, axis=0))) + np.mean(np.abs(np.diff(g, axis=1))))


def detail_face(size=1200):
    """High-frequency stand-in for a face: fine hair-like lines, an iris ring, skin speckle."""
    rng = np.random.default_rng(7)
    img = Image.new("RGB", (size, size), (205, 160, 135))
    d = ImageDraw.Draw(img)
    for i in range(0, size, 9):                                # hair strands
        d.line((i, 0, i + 140, size // 3), fill=(60, 40, 30), width=2)
    for cx in (size * .35, size * .65):                        # eyes
        d.ellipse((cx - 60, size * .45 - 60, cx + 60, size * .45 + 60), fill=(245, 245, 240))
        d.ellipse((cx - 30, size * .45 - 30, cx + 30, size * .45 + 30), fill=(70, 110, 60))
        d.ellipse((cx - 12, size * .45 - 12, cx + 12, size * .45 + 12), fill=(10, 10, 10))
    a = np.asarray(img, dtype=np.int16) + rng.integers(-6, 7, (size, size, 1))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


class ContractMixin:
    def setUp(self):
        super().setUp()
        self._saved_target = dict(color_manage._target)
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=False), \
                mock.patch.object(color_manage, "_assigned_icc", return_value=None):
            color_manage.configure(color_manage.MODE_SYSTEM)
        self.tmp = tempfile.mkdtemp(prefix="kairos_fidelity_")

    def tearDown(self):
        color_manage._target.clear()
        color_manage._target.update(self._saved_target)
        color_manage._transforms.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def save(self, img, name, icc=None, **kw):
        path = os.path.join(self.tmp, name)
        if icc:
            kw["icc_profile"] = icc
        img.save(path, **kw)
        return path


# ═════════════════════════════════════════════════════════════════════════════
# 0. Display contract
# ═════════════════════════════════════════════════════════════════════════════

class TestDisplayContract(ContractMixin, unittest.TestCase):

    def _wide_edid(self):
        edid = bytearray(128)
        edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        prim = ((0.684, 0.316), (0.240, 0.714), (0.140, 0.044), (0.3125, 0.329))
        vals = [round(c * 1024) for xy in prim for c in xy]
        edid[25] = ((vals[0] & 3) << 6) | ((vals[1] & 3) << 4) | ((vals[2] & 3) << 2) | (vals[3] & 3)
        edid[26] = ((vals[4] & 3) << 6) | ((vals[5] & 3) << 4) | ((vals[6] & 3) << 2) | (vals[7] & 3)
        for i, v in enumerate(vals):
            edid[27 + i] = v >> 2
        return bytes(edid)

    def test_default_is_windows_srgb_even_on_a_wide_gamut_panel(self):
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=False), \
                mock.patch.object(color_manage, "_assigned_icc", return_value=None), \
                mock.patch.object(color_manage, "_internal_display_edid", return_value=self._wide_edid()):
            tgt = color_manage.configure("auto")                      # legacy config value
        self.assertEqual((tgt["mode"], tgt["icc"], tgt["fp"]), ("system", None, "srgb"))

    def test_legacy_and_new_mode_values(self):
        for v, want in (("auto", "system"), ("srgb", "system"), (None, "system"), ("", "system"),
                        ("system", "system"), ("panel", "panel"), ("PANEL", "panel")):
            self.assertEqual(color_manage.normalize_mode(v), want, v)

    def test_assigned_windows_profile_wins_in_every_mode(self):
        prof = color_manage.build_matrix_profile(P3_XY, "Calibrated P3 panel")
        for mode in ("system", "panel"):
            with mock.patch.object(color_manage, "_advanced_color_active", return_value=False), \
                    mock.patch.object(color_manage, "_assigned_icc", return_value=prof):
                self.assertEqual(color_manage.detect_output(mode)[0], prof, mode)

    def test_windows_acm_means_srgb_even_in_panel_mode(self):
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=True), \
                mock.patch.object(color_manage, "_internal_display_edid", return_value=self._wide_edid()):
            self.assertIsNone(color_manage.detect_output("panel")[0])

    def test_panel_mode_is_opt_in_edid(self):
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=False), \
                mock.patch.object(color_manage, "_assigned_icc", return_value=None), \
                mock.patch.object(color_manage, "_internal_display_edid", return_value=self._wide_edid()):
            icc, name, _ = color_manage.detect_output("panel")
        self.assertIsNotNone(icc)
        self.assertIn("EDID", name)


# ═════════════════════════════════════════════════════════════════════════════
# 1. Zero double conversion
# ═════════════════════════════════════════════════════════════════════════════

class TestZeroDoubleConversion(ContractMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.ref = dict(zip(PATCHES, single_conversion(PATCHES.values())))
        self.twice = dict(zip(PATCHES, single_conversion(single_conversion(PATCHES.values()))))
        self.p3_png = self.save(patch_chart(), "chart_p3.png", P3_ICC)

    def assertMatches(self, got, tol, where):
        for k, want in self.ref.items():
            diff = max(abs(int(a) - int(b)) for a, b in zip(got[k], want))
            self.assertLessEqual(diff, tol, f"{where}: {k} {got[k]} != single conversion {want}")

    def test_reference_is_sensitive_to_double_conversion(self):
        """Guard for the guard: a second conversion is clearly visible on these patches."""
        self.assertGreater(abs(self.twice["foliage"][1] - self.ref["foliage"][1])
                           + abs(self.twice["skin_light"][0] - self.ref["skin_light"][0]), 6)

    def test_pipeline_full_size_equals_one_conversion(self):
        self.assertMatches(centres(imaging.render_still(self.p3_png)), 1, "imaging.render_still")

    def test_jpeg_source_with_profile(self):
        path = self.save(patch_chart(4), "chart_p3.jpg", P3_ICC, quality=98, subsampling=0)
        self.assertMatches(centres(imaging.render_still(path)), 3, "JPEG + ICC")

    def test_thumbnail_equals_one_conversion(self):
        src = self.save(patch_chart(8, cols=4), "big_p3.png", P3_ICC)   # 5120×2560 -> 1680×840
        out = os.path.join(self.tmp, "thumb.jpg")
        self.assertTrue(scanner.make_thumbnail(src, out, "photo", None))
        with Image.open(out) as im:
            self.assertEqual(min(im.size), scanner.THUMB_SHORT_EDGE)
            self.assertNotIn("icc_profile", im.info)
            got = centres(im, cols=4)
        self.assertMatches(got, 3, "scanner thumbnail")

    def test_preview_equals_one_conversion(self):
        src = self.save(patch_chart(4), "prev_p3.png", P3_ICC)
        out = preview_cache.build_preview(src, "photo")
        self.assertTrue(out and os.path.exists(out))
        with Image.open(out) as im:
            self.assertMatches(centres(im), 3, "preview cache")

    @unittest.skipUnless(HAS_QT, "Qt not available")
    def test_viewer_original_equals_one_conversion(self):
        app()
        q = viewer_hud.load_image_file(self.p3_png, use_preview=False)
        self.assertFalse(q.isNull())
        self.assertMatches(centres(imaging.qimage_to_pil(q)), 1, "viewer load_image_file")

    @unittest.skipUnless(HAS_QT, "Qt not available")
    def test_qimage_fallback_equals_one_conversion_and_keeps_alpha(self):
        app()
        rgba = patch_chart().convert("RGBA")
        rgba.putalpha(200)
        path = self.save(rgba, "chart_alpha.png", P3_ICC)
        q = QImageReader(path).read()
        out = color_manage.qimage_to_output(q)
        self.assertTrue(out.hasAlphaChannel(), "alpha must survive the colour conversion")
        pil = imaging.qimage_to_pil(out)
        self.assertEqual(pil.getpixel((5, 5))[3], 200)
        self.assertMatches(centres(pil.convert("RGB")), 1, "qimage_to_output")

    def test_untagged_and_srgb_pass_through_bit_exact(self):
        chart = patch_chart()
        for name, icc in (("untagged.png", None), ("srgb.png", SRGB_ICC)):
            path = self.save(chart, name, icc)
            out = imaging.render_still(path)
            self.assertEqual(out.tobytes(), chart.tobytes(), f"{name}: no transform at all")

    @unittest.skipUnless(HAS_QT, "Qt not available")
    def test_cache_files_are_never_converted_again(self):
        app()
        cache_root = os.path.join(self.tmp, "cache")
        os.makedirs(cache_root)
        path = os.path.join(cache_root, "already_display.png")
        patch_chart().save(path, icc_profile=P3_ICC)                  # tagged, but it is a cache file
        with mock.patch.object(paths, "CACHE_ROOT", cache_root):
            q = viewer_hud.load_image_file(path, use_preview=False)
        self.assertEqual(centres(imaging.qimage_to_pil(q)), dict(PATCHES), "cache = display space as is")

    def test_neutral_greys_keep_their_level_no_tone_curve(self):
        """P3 and sRGB share TRC and white: a neutral ramp must come out unchanged – any tone
        curve, contrast tweak or brightness lift anywhere in the pipeline breaks this."""
        ramp = Image.fromarray(np.repeat(np.arange(256, dtype=np.uint8)[None, :, None], 3, 2).repeat(8, 0))
        out = np.asarray(imaging.render_still(self.save(ramp, "ramp_p3.png", P3_ICC)), dtype=int)
        self.assertLessEqual(int(np.abs(out - np.asarray(ramp, dtype=int)).max()), 1)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Chroma, contrast, bit depth
# ═════════════════════════════════════════════════════════════════════════════

class TestChromaAndContrast(ContractMixin, unittest.TestCase):

    def test_p3_red_is_full_saturation_not_pale(self):
        path = self.save(patch_chart(), "red.png", P3_ICC)
        r, g, b = centres(imaging.render_still(path))["p3_red"]
        self.assertGreaterEqual(r, 250)
        self.assertLessEqual(max(g, b), 12)

    def test_windows_contract_never_desaturates_p3_content(self):
        chart = patch_chart()
        out = imaging.render_still(self.save(chart, "c.png", P3_ICC))
        self.assertGreater(sat(list(centres(out).values())), sat(list(PATCHES.values())) * 1.03)

    def test_skin_tones_are_colorimetrically_exact(self):
        out = centres(imaging.render_still(self.save(patch_chart(), "skin.png", P3_ICC)))
        for k in ("skin_light", "skin_dark"):
            lab_in = to_lab([PATCHES[k]], P3_ICC)[0]
            lab_out = to_lab([out[k]], None)[0]
            self.assertLess(float(np.linalg.norm(lab_in - lab_out)), 1.5, f"{k} ΔE")

    def test_lake_scene_contrast_and_luminance_preserved(self):
        """Dark water + bright sky + foliage: thumbnail luminance and contrast match the source."""
        rng = np.random.default_rng(3)
        h, w = 1500, 2000
        a = np.zeros((h, w, 3), dtype=np.float64)
        a[: h // 2] = (150, 190, 235)                                   # sky
        a[h // 2: h * 3 // 4] = (60, 105, 55)                           # shore foliage
        a[h * 3 // 4:] = (25, 55, 80)                                   # dark lake
        a += rng.normal(0, 4, (h, w, 1))
        img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
        src = self.save(img, "lake.png", P3_ICC)
        out = os.path.join(self.tmp, "lake_t.jpg")
        self.assertTrue(scanner.make_thumbnail(src, out, "photo", None))
        ref = Image.new("RGB", img.size)
        ref.putdata(single_conversion(pixels(img)))
        ref = np.asarray(ref.resize((1120, 840), Image.Resampling.LANCZOS), dtype=np.float64) @ [.2126, .7152, .0722]
        with Image.open(out) as im:
            got = np.asarray(im.convert("RGB"), dtype=np.float64) @ [.2126, .7152, .0722]
        self.assertAlmostEqual(got.mean(), ref.mean(), delta=1.0, msg="no brightness lift / crush")
        self.assertAlmostEqual(got.std() / ref.std(), 1.0, delta=0.02, msg="contrast preserved")

    def test_every_level_of_a_ramp_survives(self):
        ramp = Image.fromarray(np.repeat(np.arange(256, dtype=np.uint8)[None, :, None], 3, 2).repeat(4, 0))
        out = np.asarray(imaging.render_still(self.save(ramp, "ramp.png")))
        self.assertEqual(len(np.unique(out[..., 1])), 256)

    def test_16bit_grey_png_is_scaled_not_clipped(self):
        a = np.array([[0, 16384, 32768, 65535]], dtype=np.uint16).repeat(4, 0)
        path = os.path.join(self.tmp, "g16.png")
        Image.fromarray(a).save(path)                                  # uint16 -> mode I;16
        out = np.asarray(imaging.render_still(path))[0, :, 0]
        self.assertEqual(list(out), [0, 64, 128, 255], "old convert('RGB') clipped all of this to white")

    def test_panel_contract_converts_once_to_the_panel(self):
        panel = color_manage.build_matrix_profile(((0.684, 0.316), (0.24, 0.714), (0.14, 0.044),
                                                   (0.3125, 0.329)), "panel")
        color_manage._target.update(icc=panel, name="panel", reason="t", fp="panel", mode="panel")
        color_manage._transforms.clear()
        out = centres(imaging.render_still(self.save(patch_chart(), "pp.png", P3_ICC)))
        ref = dict(zip(PATCHES, single_conversion(PATCHES.values(), P3_ICC)))
        tr = ImageCms.buildTransform(ImageCms.ImageCmsProfile(io.BytesIO(P3_ICC)),
                                     ImageCms.ImageCmsProfile(io.BytesIO(panel)), "RGB", "RGB",
                                     renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
        src = Image.new("RGB", (len(PATCHES), 1))
        src.putdata(list(PATCHES.values()))
        want = dict(zip(PATCHES, pixels(ImageCms.applyTransform(src, tr))))
        for k in PATCHES:
            self.assertLessEqual(max(abs(a - b) for a, b in zip(out[k], want[k])), 1, k)
        self.assertNotEqual(out["foliage"], ref["foliage"], "panel target really differs from sRGB")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Crispness
# ═════════════════════════════════════════════════════════════════════════════

class TestCrispness(ContractMixin, unittest.TestCase):

    def test_luma_sharpen_leaves_chroma_bit_exact(self):
        rng = np.random.default_rng(11)
        a = rng.integers(20, 236, (200, 200, 3)).astype(np.uint8)
        a = np.asarray(Image.fromarray(a).resize((400, 400), Image.Resampling.NEAREST))
        out = np.asarray(imaging.sharpen_luma(Image.fromarray(a), 0.8, 1.0, 0.0), dtype=int)
        src = a.astype(int)
        unclipped = (out > 0).all(2) & (out < 255).all(2)
        for c in (0, 2):
            d_src = src[..., c] - src[..., 1]
            d_out = out[..., c] - out[..., 1]
            exact = (d_src == d_out)[unclipped].mean()
            self.assertGreater(exact, 0.995, "chroma (R−G, B−G) must not change")
            self.assertLessEqual(int(np.abs(d_src - d_out)[unclipped].max()), 1)
        self.assertGreater(np.abs(out - src).max(), 5, "and it really sharpened")

    def test_sharpen_has_no_halos_on_a_hard_edge(self):
        a = np.full((64, 64, 3), 40, np.uint8)
        a[:, 32:] = 200
        out = np.asarray(imaging.sharpen_luma(Image.fromarray(a), *imaging.SHARPEN_AVATAR), dtype=int)
        self.assertGreaterEqual(out.min(), 40 - imaging.OVERSHOOT - 1)
        self.assertLessEqual(out.max(), 200 + imaging.OVERSHOOT + 1)

    def test_sharpen_ignores_flat_skin_noise(self):
        rng = np.random.default_rng(5)
        a = np.clip(np.full((64, 64, 3), (205, 160, 135)) + rng.normal(0, 0.4, (64, 64, 1)), 0, 255).astype(np.uint8)
        out = np.asarray(imaging.sharpen_luma(Image.fromarray(a), *imaging.SHARPEN_THUMB), dtype=int)
        self.assertLessEqual(int(np.abs(out - a.astype(int)).max()), 1, "threshold protects smooth skin")

    def test_face_avatar_is_crisper_than_the_old_path(self):
        face = detail_face()
        bgr = np.asarray(face)[:, :, ::-1].copy()
        bbox = (400, 400, 800, 800)                                    # 400 px face -> 800 px window
        new = face_engine._extract_apple_avatar_crop(bgr, bbox)
        self.assertEqual(new.shape[:2], (face_engine.AVATAR_PX, face_engine.AVATAR_PX))
        import cv2
        x, y, side = face_engine.square_crop_window(1200, 1200, bbox)
        old = cv2.resize(bgr[y:y + side, x:x + side], (256, 256), interpolation=cv2.INTER_AREA)
        display = 275                                                 # people card at DPR 2
        new_disp = np.asarray(Image.fromarray(new[:, :, ::-1]).resize((display, display), Image.Resampling.LANCZOS))
        old_disp = np.asarray(Image.fromarray(old[:, :, ::-1]).resize((display, display), Image.Resampling.BILINEAR))
        self.assertGreater(grad_energy(new_disp), grad_energy(old_disp) * 1.15)
        plain = np.asarray(Image.fromarray(np.ascontiguousarray(bgr[y:y + side, x:x + side, ::-1])).resize(
            (face_engine.AVATAR_PX,) * 2, Image.Resampling.LANCZOS))
        self.assertGreater(grad_energy(new[:, :, ::-1]), grad_energy(plain) * 1.10, "avatar sharpening applied")

    def test_thumbnail_crisper_than_plain_resample_but_same_tone(self):
        face = detail_face(2400)
        src = self.save(face, "detail.png")
        out = os.path.join(self.tmp, "d.jpg")
        self.assertTrue(scanner.make_thumbnail(src, out, "photo", None))
        with Image.open(out) as im:
            got = np.asarray(im.convert("RGB"))
            self.assertEqual(JpegImagePlugin.get_sampling(im), 0, "cache JPEG must be 4:4:4")
        plain = np.asarray(face.resize((840, 840), Image.Resampling.LANCZOS))   # same resampler, no sharpen
        self.assertGreater(grad_energy(got), grad_energy(plain) * 1.08, "screen sharpening applied")
        self.assertAlmostEqual(got.mean(), plain.mean(), delta=1.0)

    @unittest.skipUnless(HAS_QT, "Qt not available")
    def test_avatar_loader_renders_at_requested_physical_size(self):
        app()
        path = os.path.join(self.tmp, "av.jpg")
        imaging.write_jpeg(detail_face(384), path)
        img = main._load_avatar_image(path, 275)
        self.assertEqual((img.width(), img.height()), (275, 275))

    @unittest.skipUnless(HAS_QT, "Qt not available")
    def test_lanczos_qimage_scaler_beats_qt_bilinear(self):
        app()
        from PyQt6.QtCore import Qt
        q = imaging.pil_to_qimage(detail_face(768))
        ours = imaging.qimage_to_pil(imaging.scale_qimage(q, 300, 300))
        qt = imaging.qimage_to_pil(q.scaled(300, 300, Qt.AspectRatioMode.IgnoreAspectRatio,
                                            Qt.TransformationMode.SmoothTransformation))
        self.assertGreater(grad_energy(np.asarray(ours)), grad_energy(np.asarray(qt)) * 1.03)


# ═════════════════════════════════════════════════════════════════════════════
# 4. Retired stages, versions, self-healing caches
# ═════════════════════════════════════════════════════════════════════════════

class TestInvariants(unittest.TestCase):

    def test_live_tone_is_gone_for_good(self):
        root = os.path.dirname(os.path.abspath(__file__))
        self.assertFalse(os.path.exists(os.path.join(root, "live_tone.py")))
        for name in os.listdir(root):
            if name.endswith(".py") and name != os.path.basename(__file__):
                with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                    src = f.read()
                self.assertNotIn("import live_tone", src, name)
        self.assertNotIn("live_tone", sys.modules)

    def test_cache_versions_bumped_for_the_new_pipeline(self):
        self.assertGreaterEqual(scanner.THUMB_VERSION, 3)
        self.assertGreaterEqual(preview_cache.PREVIEW_VERSION, 4)
        self.assertGreaterEqual(face_engine.FACE_RENDER_VERSION, 1)
        self.assertGreaterEqual(face_engine.AVATAR_PX, 384)

    def test_cache_jpeg_is_444(self):
        data = imaging.encode_jpeg(Image.new("RGB", (64, 64), (200, 30, 30)))
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(JpegImagePlugin.get_sampling(im), 0)

    def test_retired_caches_are_purged(self):
        tmp = tempfile.mkdtemp(prefix="kairos_purge_")
        try:
            tone = os.path.join(tmp, "tone")
            faces = os.path.join(tmp, "faces")
            os.makedirs(tone)
            os.makedirs(faces)
            open(os.path.join(tone, "a.json"), "w").close()
            open(os.path.join(faces, "CROP_COLOR"), "w").close()
            with mock.patch.object(paths, "CACHE_ROOT", tmp), mock.patch.object(paths, "FACE_DIR", faces):
                self.assertEqual(imaging.purge_retired_caches(), 2)
            self.assertFalse(os.path.exists(tone))
            self.assertFalse(os.path.exists(os.path.join(faces, "CROP_COLOR")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFaceRerender(ContractMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self._saved_db = (database.DB_PATH, face_engine.FACE_CACHE_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.faces = os.path.join(self.tmp, "faces")
        os.makedirs(self.faces)
        face_engine.FACE_CACHE_DIR = self.faces
        database.init_db()

    def tearDown(self):
        database.DB_PATH, face_engine.FACE_CACHE_DIR = self._saved_db
        super().tearDown()

    def x(self, sql, params=()):
        with database.db_transaction() as conn:
            return conn.execute(sql, params).lastrowid

    def test_rerender_from_original_once_per_contract_never_reconverts(self):
        face = detail_face(1200)
        photo = self.save(face, "portrait.png", P3_ICC)
        mid = self.x("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?, 'p', 'photo')", (photo,))
        crop = os.path.join(self.faces, "face_1_old.jpg")
        Image.new("RGB", (112, 112), (128, 128, 128)).save(crop, quality=70)   # legacy muddy 112 px crop
        self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)", (mid, crop, b"\0" * 16, 400 / 1200, 400 / 1200, 400 / 1200, 400 / 1200))

        self.assertEqual(face_engine.rerender_face_crops(), 1)
        with Image.open(crop) as im:
            self.assertEqual(im.size, (face_engine.AVATAR_PX, face_engine.AVATAR_PX))
            self.assertEqual(JpegImagePlugin.get_sampling(im), 0)
            first = np.asarray(im.convert("RGB"), dtype=int)
        self.assertEqual(face_engine.rerender_face_crops(), 0, "idempotent for the same contract")

        # contract change -> re-render FROM THE ORIGINAL (a conversion of the crop would drift)
        panel = color_manage.build_matrix_profile(((0.684, 0.316), (0.24, 0.714), (0.14, 0.044),
                                                   (0.3125, 0.329)), "panel")
        color_manage._target.update(icc=panel, fp="panel01", mode="panel")
        color_manage._transforms.clear()
        self.assertEqual(face_engine.rerender_face_crops(), 1)
        color_manage._target.update(icc=None, fp="srgb", mode="system")
        color_manage._transforms.clear()
        self.assertEqual(face_engine.rerender_face_crops(), 1)
        with Image.open(crop) as im:
            back = np.asarray(im.convert("RGB"), dtype=int)
        self.assertLessEqual(int(np.abs(back - first).max()), 2, "round trip = identical, no generational loss")

    def test_orphan_face_files_purged_but_referenced_and_fresh_kept(self):
        mid = self.x("INSERT INTO media_items (file_path, file_name, media_type) VALUES ('x.jpg', 'x', 'photo')")
        keep = os.path.join(self.faces, "face_1_keep.jpg")
        cover = os.path.join(self.faces, "face_2_cover.jpg")
        orphan = os.path.join(self.faces, "face_3_orphan.jpg")
        fresh = os.path.join(self.faces, "face_4_fresh.jpg")
        other = os.path.join(self.faces, "FACE_RENDER")
        for p in (keep, cover, orphan, fresh, other):
            open(p, "wb").close()
        old = 1_600_000_000
        for p in (keep, cover, orphan, other):
            os.utime(p, (old, old))
        self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
               (mid, r"C:\old\location\face_1_keep.jpg", b"\0"))          # stale absolute path, same name
        self.x("INSERT INTO people (name, cover_face_path) VALUES ('A', ?)", (cover,))
        self.assertEqual(face_engine.purge_orphan_face_files(), 1)
        self.assertEqual(sorted(os.listdir(self.faces)),
                         sorted(os.path.basename(p) for p in (keep, cover, fresh, other)))

    def test_purge_refuses_when_db_does_not_own_the_folder(self):
        """Temp / empty / foreign DB against a real folder must never delete anything."""
        old = 1_600_000_000
        files = [os.path.join(self.faces, f"face_{i}_x.jpg") for i in range(5)]
        for p in files:
            open(p, "wb").close()
            os.utime(p, (old, old))
        self.assertEqual(face_engine.purge_orphan_face_files(), 0, "no references at all")
        mid = self.x("INSERT INTO media_items (file_path, file_name, media_type) VALUES ('x.jpg', 'x', 'photo')")
        for i in range(3):                                            # refs to crops that are elsewhere
            self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                   (mid, os.path.join(r"D:\other", f"face_9{i}_y.jpg"), b"\x00"))
        self.assertEqual(face_engine.purge_orphan_face_files(), 0, "DB belongs to another folder")
        self.assertEqual(sorted(os.listdir(self.faces)), sorted(os.path.basename(p) for p in files))


class TestHarnessIsolation(unittest.TestCase):

    def test_tests_never_resolve_the_real_cache_folders(self):
        real = os.path.normcase(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Kairos"))
        for p in (scanner.CACHE_DIR, face_engine.FACE_CACHE_DIR, paths.CACHE_ROOT, paths.THUMB_DIR,
                  paths.FACE_DIR, paths.PREVIEW_DIR):
            self.assertFalse(os.path.normcase(os.path.abspath(p)).startswith(real), p)


if __name__ == "__main__":
    unittest.main()
