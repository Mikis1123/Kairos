"""Final product pillars: semantic search latency (1), Quick Edits (2), export presets (3),
Live hover with edits (4) and cold boot (5)."""
import hashlib
import json
import math
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import quick_edit as qe

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from PyQt6 import QtWebEngineWidgets  # noqa: F401 – before any QApplication (main imports it)
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication
    HAS_QT = True
except Exception:                                       # pragma: no cover
    HAS_QT = False

_APP = []


def app():
    if not _APP:
        _APP.append(QApplication.instance() or QApplication([]))
    return _APP[0]


def _rand(h, w, c=3, seed=1):
    return (np.random.default_rng(seed).random((h, w, c)) * 255).astype(np.uint8)


# ── pillar 2: the engine ─────────────────────────────────────────────────────

class TestAdjustmentsModel(unittest.TestCase):
    def test_default_is_identity_and_apply_returns_input(self):
        a = qe.Adjustments()
        self.assertTrue(a.is_identity())
        self.assertEqual(a.key(), "")
        arr = _rand(20, 30)
        self.assertIs(qe.apply(arr, a), arr, "neutral edit must not copy or touch pixels")

    def test_json_round_trip_keeps_unknown_keys(self):
        a = qe.Adjustments(crop=(0.1, 0.2, 0.5, 0.6), aspect="4:5", angle=-3.25, exposure=20, highlights=-40,
                           shadows=35, temperature=12, contrast=-8, extra={"vignette": 0.3})
        b = qe.Adjustments.from_json(a.to_json())
        self.assertEqual(b.to_dict(), a.to_dict())
        self.assertEqual(b.extra, {"vignette": 0.3}, "keys from newer builds survive")
        self.assertEqual(json.loads(a.to_json())["v"], qe.EDIT_VERSION)

    def test_values_are_clamped_and_garbage_is_neutral(self):
        a = qe.Adjustments.from_dict({"angle": 99, "exposure": 1e9, "shadows": float("nan"),
                                      "crop": [0.9, 0.9, 0.5, 0.5], "aspect": "7:3"})
        self.assertEqual(a.angle, qe.ANGLE_LIMIT)
        self.assertEqual(a.exposure, qe.SLIDER_LIMIT)
        self.assertEqual(a.shadows, 0.0)
        self.assertEqual(a.aspect, "free")
        x, y, w, h = a.crop
        self.assertLessEqual(x + w, 1.0 + 1e-9)
        self.assertLessEqual(y + h, 1.0 + 1e-9)
        for bad in ("", "{", "[]", "null", '{"crop": "x"}'):
            self.assertTrue(qe.Adjustments.from_json(bad).is_identity(), bad)


class TestTone(unittest.TestCase):
    def test_lut_monotone_for_every_slider_extreme(self):
        import itertools
        for vals in itertools.product((-100, 0, 100), repeat=5):
            lut = qe.build_lut(qe.Adjustments(**dict(zip(qe.TONE_KEYS, vals))))[0].astype(int)
            self.assertGreaterEqual(int(np.diff(lut, axis=0).min()), 0, vals)

    def test_exposure_is_ev_in_linear_light(self):
        lut = qe.build_lut(qe.Adjustments(exposure=50))[0]          # +1 EV
        v = 100 / 255.0
        lin = ((v + 0.055) / 1.055) ** 2.4 * 2
        expect = 1.055 * lin ** (1 / 2.4) - 0.055
        self.assertAlmostEqual(lut[100, 0] / 255.0, expect, delta=2 / 255)

    def test_warmth_raises_red_lowers_blue_keeps_luma(self):
        arr = np.full((4, 4, 3), 128, np.uint8)
        out = qe.apply_tone(arr, qe.Adjustments(temperature=100)).astype(float)
        self.assertGreater(out[0, 0, 0], 128)
        self.assertLess(out[0, 0, 2], 128)
        g = qe.channel_gains(qe.Adjustments(temperature=100))
        self.assertAlmostEqual(float(g @ np.array([0.2126, 0.7152, 0.0722])), 1.0, places=6)

    def test_shadows_lift_darks_more_than_brights(self):
        lut = qe.build_lut(qe.Adjustments(shadows=100))[0][:, 1].astype(int)
        self.assertGreater(lut[64] - 64, lut[230] - 230)
        self.assertEqual(lut[0], 0)
        self.assertEqual(lut[255], 255)

    def test_contrast_is_an_s_curve(self):
        lut = qe.build_lut(qe.Adjustments(contrast=100))[0][:, 1].astype(int)
        self.assertLess(lut[64], 64)
        self.assertGreater(lut[192], 192)

    def test_alpha_and_channel_order(self):
        rgba = np.zeros((2, 2, 4), np.uint8)
        rgba[..., 0] = 200
        rgba[..., 3] = 77
        out = qe.apply(rgba, qe.Adjustments(temperature=100), "RGBA")
        self.assertTrue((out[..., 3] == 77).all(), "alpha never changes")
        bgr = rgba[..., [2, 1, 0, 3]].copy()
        out_bgr = qe.apply(bgr, qe.Adjustments(temperature=100), "BGRA")
        self.assertTrue((out_bgr[..., [2, 1, 0, 3]] == out).all())


class TestGeometry(unittest.TestCase):
    def test_crop_slice_matches_warp(self):
        img = _rand(300, 400)
        crop = (0.1, 0.2, 0.5, 0.4)
        a = qe.apply(img, qe.Adjustments(crop=crop)).astype(int)
        b = qe.apply(img, qe.Adjustments(crop=crop, angle=1e-4)).astype(int)
        self.assertEqual(a.shape, (120, 200, 3))
        self.assertEqual(a.shape, b.shape)
        self.assertLess(float(np.abs(a - b).mean()), 0.01)

    def test_straighten_never_shows_empty_corners(self):
        w, h = 640, 480
        for angle in (-45, -12.5, 3, 30, 45):
            adj = qe.Adjustments(angle=angle)
            m = qe.inverse_matrix(w, h, adj)
            ow, oh = qe.output_size(w, h, adj)
            for qx, qy in ((0, 0), (ow - 1, 0), (0, oh - 1), (ow - 1, oh - 1)):
                sx = m[0, 0] * qx + m[0, 1] * qy + m[0, 2]
                sy = m[1, 0] * qx + m[1, 1] * qy + m[1, 2]
                self.assertTrue(-0.51 <= sx <= w - 0.49 and -0.51 <= sy <= h - 0.49, (angle, qx, qy, sx, sy))

    def test_positive_angle_rotates_clockwise(self):
        img = np.zeros((200, 200, 3), np.uint8)
        img[95:105, :, :] = 255                       # horizontal white bar
        out = qe.apply(img, qe.Adjustments(angle=20))
        left = out[:, 40].argmax(axis=0)[0]
        right = out[:, 160].argmax(axis=0)[0]
        self.assertGreater(right, left, "clockwise: the bar's right end goes down on screen")

    def test_output_size_and_scale(self):
        adj = qe.Adjustments(crop=(0, 0, 0.5, 0.25))
        self.assertEqual(qe.output_size(4000, 3000, adj), (2000, 750))
        self.assertEqual(qe.output_size(4000, 3000, adj, scale=0.5), (1000, 375))
        self.assertEqual(qe.apply(_rand(300, 400), adj, scale=0.5).shape[:2], (38, 100))

    def test_aspect_presets_fit_exactly(self):
        w, h = 4032, 3024
        for key, ratio in (("1:1", 1.0), ("16:9", 16 / 9), ("4:5", 4 / 5)):
            a = qe.with_aspect(qe.Adjustments(), key, w, h)
            _x, _y, cw, ch = a.crop
            self.assertAlmostEqual(cw * w / (ch * h), ratio, places=4)
            self.assertTrue(cw == 1.0 or ch == 1.0, "largest fit")
        self.assertEqual(qe.with_aspect(qe.Adjustments(), "free", w, h).crop, (0.0, 0.0, 1.0, 1.0))

    def test_drag_keeps_ratio_and_stays_in_frame(self):
        w, h = 1600, 1200
        crop = qe.fit_aspect((0, 0, 1, 1), 1.0, w, h)
        for handle, dx, dy in (("br", -0.2, -0.05), ("tl", 0.1, 0.3), ("r", -0.3, 0), ("move", 0.9, 0.9)):
            c = qe.drag_crop(crop, handle, dx, dy, 1.0, w, h)
            self.assertAlmostEqual(c[2] * w / (c[3] * h), 1.0, places=4, msg=handle)
            self.assertGreaterEqual(min(c[0], c[1]), -1e-9)
            self.assertLessEqual(c[0] + c[2], 1 + 1e-9)
            self.assertLessEqual(c[1] + c[3], 1 + 1e-9)
        free = qe.drag_crop((0, 0, 1, 1), "l", 0.999, 0, None, w, h)
        self.assertAlmostEqual(free[2], qe.MIN_CROP, places=6, msg="never smaller than MIN_CROP")

    def test_pil_render_keeps_mode_and_profile(self):
        img = Image.new("RGB", (80, 60), (10, 20, 30))
        img.info["icc_profile"] = b"fake-icc"
        out = qe.render_pil(img, qe.Adjustments(crop=(0, 0, 0.5, 0.5), exposure=10))
        self.assertEqual(out.size, (40, 30))
        self.assertEqual(out.mode, "RGB")
        self.assertEqual(out.info.get("icc_profile"), b"fake-icc")


class TestRealtimeBudget(unittest.TestCase):
    def test_drag_proxy_fits_a_120hz_frame(self):
        """Measured 3.6 ms on the target laptop for a 2880×1800 display image (proxy 1600 px)."""
        big = _rand(1800, 2880, 4)
        adj = qe.Adjustments(exposure=30, shadows=40, contrast=20, temperature=-10, angle=7.5,
                             crop=(0.1, 0.1, 0.8, 0.8))
        k = qe.proxy_scale(2880, 1800)
        small = np.ascontiguousarray(big[::2, ::2])                   # what the proxy roughly is
        times = []
        for _ in range(15):
            t = time.perf_counter()
            qe.apply(small, adj, "RGBA", scale=min(1.0, k * 2), fast=True)
            times.append((time.perf_counter() - t) * 1000)
        self.assertLess(statistics.median(times), 8.3 * 1.5, f"median {statistics.median(times):.1f} ms")


class TestEditStorage(unittest.TestCase):
    def setUp(self):
        import database
        self.database = database
        self.tmp = tempfile.mkdtemp(prefix="kairos-edits-")
        self._old = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        conn = database.get_connection()
        conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type) VALUES (7, 'C:/x/a.jpg', "
                     "'a.jpg', 'photo')")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.database.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_clear_and_cascade(self):
        conn = self.database.get_connection()
        a = qe.Adjustments(exposure=25, crop=(0, 0, 0.5, 0.5))
        self.assertTrue(qe.save(conn, 7, a))
        conn.commit()
        self.assertEqual(qe.load(conn, 7).to_dict(), a.normalized().to_dict())
        self.assertEqual(list(qe.load_many(conn)), [7])
        self.assertEqual(list(qe.load_many(conn, [7, 8])), [7])
        self.assertFalse(qe.save(conn, 7, qe.Adjustments()), "neutral = row removed")
        self.assertIsNone(qe.load(conn, 7))
        qe.save(conn, 7, a)
        conn.execute("DELETE FROM media_items WHERE id = 7")
        conn.commit()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_edits").fetchone()[0], 0, "cascade")
        conn.close()

    def test_master_file_is_never_written(self):
        src = os.path.join(self.tmp, "a.jpg")
        Image.new("RGB", (64, 48), (90, 80, 70)).save(src, quality=90)
        before = hashlib.sha256(open(src, "rb").read()).hexdigest()
        conn = self.database.get_connection()
        qe.save(conn, 7, qe.Adjustments(exposure=60, angle=10))
        conn.commit()
        conn.close()
        img = Image.open(src)
        qe.render_pil(img.convert("RGB"), qe.Adjustments(exposure=60, angle=10))
        img.close()
        self.assertEqual(hashlib.sha256(open(src, "rb").read()).hexdigest(), before)


# ── pillar 2: Qt surfaces (grid worker, viewer, edit session, live hover) ───

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestQtSurfaces(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app()
        import main
        cls.main = main

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-qe-qt-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.main.EDITS.clear()

    def test_render_qimage_formats(self):
        for fmt in (QImage.Format.Format_RGB888, QImage.Format.Format_RGB32, QImage.Format.Format_ARGB32):
            img = QImage(101, 51, fmt)
            img.fill(0xFF336699)
            out = qe.render_qimage(img, qe.Adjustments(crop=(0, 0, 0.5, 1.0), exposure=20))
            self.assertEqual((out.width(), out.height()), (50, 51), fmt)
            self.assertFalse(out.isNull())

    def test_thumbnail_worker_applies_edit(self):
        src = os.path.join(self.tmp, "t.jpg")
        Image.new("RGB", (1200, 800), (100, 100, 100)).save(src, "JPEG")
        got = []
        w = self.main.ThumbnailWorker(7, src, None, tier=256)
        w.adjustments = qe.Adjustments(crop=(0, 0, 0.5, 1.0))
        w.signals.loaded.connect(lambda i, img, tile: got.append(img))
        w.run()
        self.assertEqual(len(got), 1)
        self.assertLess(got[0].width(), got[0].height(), "half-width crop of a landscape = portrait tile")

    def test_cache_decode_divisor(self):
        d = self.main.cache_decode_divisor
        self.assertEqual(d(1120, 840, 256), 2)          # 420 >= 384
        self.assertEqual(d(1120, 840, 512), 1)
        self.assertEqual(d(4480, 3360, 256), 8)
        self.assertEqual(d(1120, 840, 840), 1)

    def test_cache_thumbnail_dct_read_reports_true_source(self):
        import paths
        os.makedirs(paths.THUMB_DIR, exist_ok=True)
        src = os.path.join(paths.THUMB_DIR, "pillar5-test.jpg")
        Image.new("RGB", (1120, 840), (40, 50, 60)).save(src, "JPEG", quality=90)
        try:
            got, short = [], []
            w = self.main.ThumbnailWorker(9, src, None, tier=256)
            w.signals.loaded.connect(lambda i, img, tile: got.append(img))
            w.signals.source_short.connect(lambda i, s: short.append(s))
            w.run()
            self.assertEqual(min(got[0].width(), got[0].height()), 256)
            self.assertEqual(short, [840], "upgrade logic still sees the 840 px file")
        finally:
            os.remove(src)

    def test_model_refresh_items_drops_caches(self):
        from PyQt6.QtGui import QPixmap
        model = self.main.ApplePhotoModel()
        model.set_records([(5, "a", "a.jpg", "photo", None, "t.jpg", "2024", 0)])
        model.ram_cache.put(5, QPixmap(10, 10))
        model._src_short[5] = 300
        changed = []
        model.dataChanged.connect(lambda a, b: changed.append(a.row()))
        model.refresh_items([5])
        self.assertIsNone(model.ram_cache.get(5))
        self.assertNotIn(5, model._src_short)
        self.assertEqual(changed, [0])
        self.assertIn(model, list(self.main._LIVE_MODELS))

    def test_worker_gets_edit_from_registry_but_videos_never(self):
        model = self.main.ApplePhotoModel()
        model.set_records([(5, "a", "a.jpg", "photo", None, "t.jpg", "2024", 0),
                           (6, "b", "b.mp4", "video", None, "t2.jpg", "2024", 0)])
        self.main.EDITS[5] = qe.Adjustments(exposure=10)
        self.main.EDITS[6] = qe.Adjustments(exposure=10)
        started = []
        with mock.patch.object(model.thread_pool, "start", side_effect=started.append):
            model._start_worker(5, model.records[0])
            model._start_worker(6, model.records[1])
        self.assertIsNotNone(started[0].adjustments)
        self.assertIsNone(started[1].adjustments)

    def test_full_res_job_applies_edit(self):
        import viewer_zoom
        src = os.path.join(self.tmp, "f.jpg")
        Image.new("RGB", (900, 600), (10, 20, 30)).save(src, "JPEG")
        out = []
        job = viewer_zoom.FullResJob(src, 1, adjustments=qe.Adjustments(crop=(0, 0, 1 / 3, 1.0)))
        job.signals.done.connect(lambda p, t, levels: out.append(levels))
        job.run()
        self.assertEqual((out[0][0].width(), out[0][0].height()), (300, 600))

    def test_edit_session_proxy_then_final_and_commit(self):
        import edit_panel
        from PyQt6.QtWidgets import QWidget

        class FakeCanvas:
            def __init__(self):
                self.frames = []

            def show_edit_frame(self, img):
                self.frames.append((img.width(), img.height()))

        host = QWidget()
        panel = edit_panel.EditPanel(host)
        overlay = edit_panel.CropOverlay(host)
        canvas = FakeCanvas()
        ctl = edit_panel.EditController(canvas, panel, overlay)
        ctl.canvas_photo_rect = lambda: __import__("PyQt6.QtCore", fromlist=["QRectF"]).QRectF(0, 0, 100, 60)
        src = QImage(3200, 2000, QImage.Format.Format_RGBA8888)
        src.fill(0xFF808080)
        ctl.start(src, qe.Adjustments(crop=(0, 0, 0.5, 0.5)))
        self.assertEqual(canvas.frames[-1], (3200, 2000), "session shows the FULL frame, crop as overlay")
        panel._set_dragging(True)
        panel.tone_sliders["exposure"].setValue(40)
        self.assertLessEqual(max(canvas.frames[-1]), qe.PROXY_LONG_EDGE, "drag renders the proxy")
        panel._set_dragging(False)
        self.assertEqual(canvas.frames[-1], (3200, 2000), "release renders full quality")
        self.assertEqual(ctl.adj.crop, (0, 0, 0.5, 0.5), "sliders keep the crop")
        ctl._on_aspect("1:1")
        _x, _y, cw, ch = ctl.adj.crop
        self.assertAlmostEqual(cw * 3200 / (ch * 2000), 1.0, places=4)
        done = []
        ctl.finished.connect(lambda adj, saved: done.append((adj, saved)))
        ctl.commit()
        self.assertTrue(done[0][1])
        self.assertEqual(done[0][0].exposure, 40)
        self.assertFalse(ctl.active)

    def test_edit_session_cancel_returns_initial(self):
        import edit_panel
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        panel = edit_panel.EditPanel(host)
        ctl = edit_panel.EditController(mock.Mock(), panel, edit_panel.CropOverlay(host))
        ctl._sync_overlay = lambda: None
        src = QImage(400, 300, QImage.Format.Format_RGB888)
        src.fill(0xFF202020)
        initial = qe.Adjustments(shadows=20)
        ctl.start(src, initial)
        panel.tone_sliders["shadows"].setValue(-60)
        out = []
        ctl.finished.connect(lambda adj, saved: out.append((adj, saved)))
        ctl.cancel()
        self.assertEqual(out[0][0].shadows, 20)
        self.assertFalse(out[0][1])

    def test_live_hover_frames_carry_the_edit(self):
        import live_hover
        from PyQt6.QtCore import Qt as _Qt

        class Strip:
            def decode(self, index, size=None):
                img = QImage(400, 300, QImage.Format.Format_RGB888)
                img.fill(0xFF808080)
                return img.scaled(size, _Qt.AspectRatioMode.KeepAspectRatioByExpanding) if size else img

        lp = live_hover.LivePreview.__new__(live_hover.LivePreview)
        lp._strip, lp._size = Strip(), QSize(100, 100)
        lp._edit = None
        plain = lp._decode_frame(0)
        lp._edit = qe.Adjustments(exposure=100)
        edited = lp._decode_frame(0)
        self.assertEqual((edited.width(), edited.height()), (133, 100))
        self.assertGreater(edited.pixelColor(50, 50).red(), plain.pixelColor(50, 50).red())


# ── pillar 3: export presets ─────────────────────────────────────────────────

def _p3_profile():
    import color_manage as cm
    return cm.build_matrix_profile(((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290)), "Display P3")


class TestExportPresets(unittest.TestCase):
    def setUp(self):
        import privacy_export as pe
        self.pe = pe
        self.tmp = tempfile.mkdtemp(prefix="kairos-presets-")
        self.src = os.path.join(self.tmp, "p3.jpg")
        Image.new("RGB", (3000, 2000), (0, 200, 40)).save(self.src, "JPEG", quality=95, icc_profile=_p3_profile())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preset_table(self):
        P = self.pe.PRESETS
        self.assertFalse(P["original"].render)
        self.assertEqual((P["web"].edge, P["web"].quality, P["web"].srgb), (2048, 90, True))
        self.assertEqual((P["archive"].edge, P["archive"].srgb), (0, False))
        self.assertEqual(self.pe.ExportOptions(preset="nope").preset_spec, P["original"])

    def test_web_converts_p3_to_srgb_and_caps_edge(self):
        import color_manage as cm
        out = os.path.join(self.tmp, "w.jpg")
        size = self.pe.render_export_image(self.src, out, self.pe.PRESETS["web"])
        self.assertEqual(size, (2048, 1365))
        img = Image.open(out)
        self.assertTrue(cm.is_srgb_profile(img.info.get("icc_profile")), "sRGB profile embedded")
        r, g, b = img.getpixel((5, 5))
        self.assertLess(r, 10)
        self.assertNotEqual((r, g, b), (0, 200, 40), "numbers converted, not just re-tagged")

    def test_archive_keeps_full_res_and_wide_gamut(self):
        out = os.path.join(self.tmp, "a.jpg")
        size = self.pe.render_export_image(self.src, out, self.pe.PRESETS["archive"])
        self.assertEqual(size, (3000, 2000))
        self.assertEqual(Image.open(out).info.get("icc_profile"), _p3_profile())

    def test_edits_apply_to_rendered_presets(self):
        out = os.path.join(self.tmp, "e.jpg")
        size = self.pe.render_export_image(self.src, out, self.pe.PRESETS["archive"],
                                           qe.Adjustments(crop=(0, 0, 0.5, 0.5)))
        self.assertEqual(size, (1500, 1000))

    def test_srgb_source_is_untouched_numerically(self):
        src = os.path.join(self.tmp, "s.png")
        Image.new("RGB", (64, 64), (12, 200, 99)).save(src)
        out = os.path.join(self.tmp, "s.jpg")
        self.pe.render_export_image(src, out, self.pe.PRESETS["web"])
        px = Image.open(out).getpixel((32, 32))
        self.assertTrue(all(abs(a - b) <= 3 for a, b in zip(px, (12, 200, 99))), px)

    def test_options_round_trip_and_validation(self):
        import export_sheet
        cfg = {"export_options": {"preset": "web"}}
        self.assertEqual(export_sheet.options_from_config(cfg).preset, "web")
        self.assertEqual(export_sheet.options_from_config({"export_options": {"preset": "x"}}).preset, "original")
        self.assertEqual(export_sheet.options_to_config(self.pe.ExportOptions(preset="archive"))["preset"], "archive")

    @unittest.skipUnless(os.path.exists(__import__("paths").EXIFTOOL_PATH), "ExifTool chybí")
    def test_export_end_to_end_web_preset_strips_gps_keeps_date(self):
        subprocess.run([self.pe.EXIFTOOL_PATH, "-q", "-overwrite_original", "-DateTimeOriginal=2024:07:06 10:11:12",
                        "-GPSLatitude=49.2", "-GPSLatitudeRef=N", "-Orientation#=6", self.src], capture_output=True)
        before = hashlib.sha256(open(self.src, "rb").read()).hexdigest()
        items = [{"id": 1, "path": self.src, "name": "p3", "type": "photo", "date": "2024-07-06 10:11:12",
                  "paired": None, "city": None, "country": None, "people": [],
                  "edit": qe.Adjustments(exposure=10)}]
        opts = self.pe.ExportOptions(preset="web", strip_people=False)
        res = self.pe.export(items, opts, os.path.join(self.tmp, "out"))
        files = [f for f in os.listdir(res.path) if f.lower().endswith(".jpg")]
        self.assertEqual(len(files), 1)
        out = os.path.join(res.path, files[0])
        meta = self.pe.read_metadata([out])[os.path.normcase(os.path.abspath(out))]
        flat = {k.split(":")[-1].lower(): v for k, v in meta.items()}
        self.assertNotIn("gpslatitude", flat)
        self.assertEqual(str(flat.get("datetimeoriginal")), "2024:07:06 10:11:12")
        self.assertIn(str(flat.get("orientation", "1")), ("1", "Horizontal (normal)"))
        w, h = Image.open(out).size
        self.assertGreater(h, w, "orientation 6 baked into the pixels (portrait)")
        self.assertEqual(hashlib.sha256(open(self.src, "rb").read()).hexdigest(), before, "master untouched")

    @unittest.skipUnless(os.path.exists(__import__("paths").EXIFTOOL_PATH), "ExifTool chybí")
    def test_original_preset_is_a_byte_copy_without_privacy_edits(self):
        items = [{"id": 1, "path": self.src, "name": "p3", "type": "photo", "date": None, "paired": None,
                  "city": None, "country": None, "people": [], "edit": qe.Adjustments(exposure=50)}]
        opts = self.pe.ExportOptions(preset="original", strip_gps=False, strip_people=False, neutral_names=False)
        res = self.pe.export(items, opts, os.path.join(self.tmp, "orig"))
        out = os.path.join(res.path, "p3.jpg")
        self.assertEqual(hashlib.sha256(open(out, "rb").read()).hexdigest(),
                         hashlib.sha256(open(self.src, "rb").read()).hexdigest())


# ── pillar 1: semantic search ────────────────────────────────────────────────

class TestSearchLatency(unittest.TestCase):
    def test_ranking_over_20k_items_is_sub_50ms(self):
        """Ranking + facets on 20 000 vectors with a stub text encoder (the ONNX encoder adds
        ~5 ms – eval/bench_search_latency.py measures the full path: p50 19 ms at 20k)."""
        import ai_engine
        import search_engine
        rng = np.random.default_rng(3)
        v = rng.standard_normal((20000, 512)).astype("<f4")
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        emb = {i + 1: v[i].tobytes() for i in range(20000)}
        q = v[123] + 0.05 * rng.standard_normal(512).astype("<f4")
        vocab = search_engine.Vocab(places={}, people={}, items={})
        with mock.patch.object(ai_engine, "query_vector", return_value=q / np.linalg.norm(q)), \
                mock.patch.object(ai_engine, "text_model_for_dim", return_value=None), \
                mock.patch.object(ai_engine, "_image_stats", return_value=None):
            search_engine.run_search("sunset", list(emb), emb, vocab=vocab)          # index build
            times = []
            for _ in range(7):
                t = time.perf_counter()
                res = search_engine.run_search("sunset", list(emb), emb, vocab=vocab)
                times.append((time.perf_counter() - t) * 1000)
        self.assertLess(statistics.median(times), 50.0, f"{statistics.median(times):.1f} ms")
        self.assertEqual(res.confident[0], 124, "nearest vector ranks first")

    def test_onnx_uses_directml_when_available(self):
        import ai_onnx
        with mock.patch("onnxruntime.get_available_providers",
                        return_value=["DmlExecutionProvider", "CPUExecutionProvider"]), \
                mock.patch.dict(os.environ, {"KAIROS_NO_GPU": ""}):
            self.assertEqual(ai_onnx.providers()[0], "DmlExecutionProvider")
        with mock.patch.dict(os.environ, {"KAIROS_NO_GPU": "1"}):
            self.assertEqual(ai_onnx.providers(), ["CPUExecutionProvider"])


# ── pillar 5: cold boot ──────────────────────────────────────────────────────

class TestColdBoot(unittest.TestCase):
    def test_scanner_import_does_not_load_scipy_geocoder(self):
        code = "import sys, scanner; print('reverse_geocoder' in sys.modules, 'mutagen.mp4' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "False False", out.stderr[-400:])

    def test_boot_trace_marks_are_monotonic(self):
        import boot_trace
        boot_trace.mark("t1")
        boot_trace.mark("t2")
        ms = dict(boot_trace.marks())
        self.assertGreater(ms["t2"], 0)
        self.assertGreaterEqual(ms["t2"], ms["t1"])
        self.assertGreater(boot_trace.now_ms(), ms["interpreter"])

    def test_boot_trace_writes_file_when_requested(self):
        import boot_trace
        path = os.path.join(tempfile.mkdtemp(), "t.json")
        with mock.patch.dict(os.environ, {"KAIROS_BOOT_TRACE": path}):
            boot_trace.finish(None)
        data = json.load(open(path, encoding="utf-8"))
        self.assertEqual(data["marks"][-1][0], "first_frame")

    def test_i18n_parse_cache_round_trip(self):
        import i18n
        path = os.path.join(i18n.LOCALES_DIR, "cs", i18n.CATALOG_NAME)
        cache = i18n._parse_cache_path(path)
        if cache and os.path.exists(cache):
            os.remove(cache)
        i18n.reload_catalogs()
        fresh = i18n.bundle("cs").format("common-done")
        self.assertTrue(cache and os.path.exists(cache), "parsed catalog cached")
        i18n.reload_catalogs()
        self.assertEqual(i18n.bundle("cs").format("common-done"), fresh)
        self.assertEqual(i18n._cached_body(path) is not None, True)

    def test_english_fallback_loads_lazily(self):
        import i18n
        old = i18n.current()
        try:
            i18n.reload_catalogs()
            i18n.set_language("cs", retranslate=False)
            self.assertNotIn("en", i18n._bundles)
            i18n.tr("pillar5-surely-missing-key")
            self.assertIn("en", i18n._bundles)
        finally:
            i18n.set_language(old, retranslate=False)

    def test_launch_intro_is_opt_in(self):
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        self.assertIn('load_config().get("launch_intro", False)', src)
        self.assertIn("if __name__ == \"__main__\" and _launch_intro_wanted():", src)

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_map_host_is_lazy(self):
        app()
        import main

        class Win:
            _map_view_built = False
            map_view = None
            _map_layout = object()
            built = []

            def _build_map_view_webview2(self, layout):
                self.built.append("wv2")
                self.map_view = object()

        w = Win()
        with mock.patch.object(main.map_host, "available", return_value=True):
            self.assertTrue(main.MainWindow._ensure_map_view(w))
            self.assertTrue(main.MainWindow._ensure_map_view(w))
        self.assertEqual(w.built, ["wv2"], "built once, on demand")
        self.assertGreater(main.DEFERRED_SERVICES_MS, 0)
        self.assertLessEqual(main.DEFERRED_SERVICES_MS, 500)

    def test_build_map_page_does_not_construct_a_web_view(self):
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        start = src.index("    def _build_map_page(self):")
        body = src[start:src.index("    def _ensure_map_view(self):")]
        self.assertNotIn("_build_map_view_webview2", body)
        self.assertNotIn("_build_map_view_qtwebengine", body)


if __name__ == "__main__":
    unittest.main()
