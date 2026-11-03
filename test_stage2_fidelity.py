"""Stage 2 (Fidelity): správa barev, miniatury 840 px s úrovněmi v RAM, náhledy podle displeje,
verze cache a hluboké přiblížení v prohlížeči."""
import io
import math
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageCms

import color_manage
import database
import scanner
import cache_tools

try:
    from PyQt6 import QtWebEngineWidgets  # noqa: F401 – před QApplication (testy importují main)
    from PyQt6.QtCore import QPointF, QPoint, QSize, Qt
    from PyQt6.QtGui import QImage, QPixmap, QWheelEvent, QColor
    from PyQt6.QtWidgets import QApplication
    import main                               # před vytvořením QApplication (atributy WebEngine)
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False

P3_XY = ((0.680, 0.320), (0.265, 0.690), (0.150, 0.060), (0.3127, 0.3290))


def p3_icc():
    return color_manage.build_matrix_profile(P3_XY, "Display P3")


_APP = []


def app():
    """QApplication držená v modulu – bez reference by ji Python hned uvolnil (QPixmap pak = pád)."""
    if not _APP:
        _APP.append(QApplication.instance() or QApplication([]))
    return _APP[0]


class ColorTargetMixin:
    def setUp(self):
        super().setUp()
        self._saved_target = dict(color_manage._target)
        color_manage.configure(color_manage.MODE_SRGB)

    def tearDown(self):
        color_manage._target.update(self._saved_target)
        color_manage._transforms.clear()
        super().tearDown()


class TestColorManage(ColorTargetMixin, unittest.TestCase):

    def test_matrix_profile_from_srgb_primaries_is_identity(self):
        icc = color_manage.build_matrix_profile(color_manage._SRGB_XY, "test")
        prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        img = Image.fromarray((np.random.RandomState(1).rand(64, 64, 3) * 255).astype("uint8"))
        tr = ImageCms.buildTransform(color_manage._SRGB, prof, "RGB", "RGB")
        diff = np.abs(np.asarray(ImageCms.applyTransform(img, tr), int) - np.asarray(img, int))
        self.assertLessEqual(diff.max(), 1)

    def test_gamut_ratio(self):
        self.assertAlmostEqual(color_manage.gamut_area_ratio(color_manage._SRGB_XY), 1.0, places=3)
        self.assertAlmostEqual(color_manage.gamut_area_ratio(P3_XY), 1.36, delta=0.02)

    def test_p3_photo_converted_for_srgb_target(self):
        """Sytá P3 červená je mimo sRGB -> po převodu se liší od surových hodnot."""
        img = Image.new("RGB", (8, 8), (220, 40, 40))
        out = color_manage.pil_to_output(img, p3_icc())
        self.assertIsNot(out, img)
        r, g, b = out.getpixel((0, 0))
        self.assertGreater(r, 220, "P3 červená je v sRGB sytější (vyšší R)")
        self.assertNotIn("icc_profile", out.info)

    def test_srgb_source_to_srgb_target_is_noop(self):
        img = Image.new("RGB", (4, 4), (10, 20, 30))
        self.assertIs(color_manage.pil_to_output(img, None), img)
        srgb_icc = ImageCms.ImageCmsProfile(color_manage._SRGB).tobytes()
        self.assertIs(color_manage.pil_to_output(img, srgb_icc), img)

    def test_wide_panel_target_desaturates_srgb_content(self):
        """Na panelu se širokým gamutem (bez správy barev Windows) se sRGB obsah převádí do
        profilu panelu – jinak by byl přesycený."""
        panel = color_manage.build_matrix_profile(((0.684, 0.316), (0.24, 0.714), (0.14, 0.044),
                                                   (0.312, 0.329)), "panel")
        color_manage._target.update(icc=panel, name="panel", reason="test", fp="panel")
        color_manage._transforms.clear()
        img = Image.new("RGB", (4, 4), (200, 60, 60))
        out = color_manage.pil_to_output(img, None)
        r, g, b = out.getpixel((0, 0))
        self.assertLess(r - g, 200 - 60, "méně sytá na širokém panelu")

    def test_detect_output_respects_manual_srgb_and_windows_management(self):
        self.assertEqual(color_manage.detect_output(color_manage.MODE_SRGB)[0], None)
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=True):
            icc, name, reason = color_manage.detect_output()
        self.assertIsNone(icc)
        self.assertIn("Windows", reason)

    def test_detect_output_uses_wide_edid_only_in_panel_mode(self):
        edid = bytearray(128)
        edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
        # chromatičnosti P3 do bajtů 25–34
        vals = [round(c * 1024) for xy in P3_XY for c in xy]
        lo1 = ((vals[0] & 3) << 6) | ((vals[1] & 3) << 4) | ((vals[2] & 3) << 2) | (vals[3] & 3)
        lo2 = ((vals[4] & 3) << 6) | ((vals[5] & 3) << 4) | ((vals[6] & 3) << 2) | (vals[7] & 3)
        edid[25], edid[26] = lo1, lo2
        for i, v in enumerate(vals):
            edid[27 + i] = v >> 2
        with mock.patch.object(color_manage, "_advanced_color_active", return_value=False), \
                mock.patch.object(color_manage, "_assigned_icc", return_value=None), \
                mock.patch.object(color_manage, "_internal_display_edid", return_value=bytes(edid)):
            sys_icc, _n, _r = color_manage.detect_output()                     # kontrakt Windows
            icc, name, reason = color_manage.detect_output(color_manage.MODE_PANEL)
        self.assertIsNone(sys_icc, "výchozí kontrakt = sRGB jako zbytek Windows, ne EDID")
        self.assertIsNotNone(icc)
        self.assertIn("EDID", name)
        prim = color_manage._edid_primaries(bytes(edid))
        self.assertAlmostEqual(prim[0][0], 0.680, places=2)

    def test_fingerprint_follows_target(self):
        self.assertEqual(color_manage.target_fingerprint(), "srgb")
        with mock.patch.object(color_manage, "detect_output", return_value=(p3_icc(), "P3", "test")):
            color_manage.configure()
        self.assertNotEqual(color_manage.target_fingerprint(), "srgb")

    @unittest.skipUnless(HAS_QT, "PyQt6")
    def test_qimage_untagged_srgb_target_unchanged(self):
        app()
        img = QImage(4, 4, QImage.Format.Format_RGB32)
        img.fill(QColor(10, 200, 30))
        out = color_manage.qimage_to_output(img)
        self.assertEqual(out.pixelColor(0, 0), QColor(10, 200, 30))


class _Tmp(ColorTargetMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="omni_s2_")
        self.thumbs = os.path.join(self.tmp, ".cache", "thumbnails")
        os.makedirs(self.thumbs)
        self._saved = (database.DB_PATH, scanner.CACHE_DIR, cache_tools.THUMB_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        scanner.CACHE_DIR = self.thumbs
        cache_tools.THUMB_DIR = self.thumbs
        database.init_db()

    def tearDown(self):
        database.DB_PATH, scanner.CACHE_DIR, cache_tools.THUMB_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def add(self, path, mtype="photo"):
        with database.db_transaction() as conn:
            return conn.execute("INSERT INTO media_items (file_path, media_type) VALUES (?, ?)",
                                (path, mtype)).lastrowid

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class TestThumbnails(_Tmp):

    def test_thumb_box_short_edge_and_caps(self):
        self.assertEqual(min(scanner._thumb_box(5712, 4284)), 840)
        self.assertEqual(scanner._thumb_box(400, 200), (400, 200), "menší se nezvětšuje")
        w, h = scanner._thumb_box(12000, 2000)                 # panorama: delší strana strop
        self.assertLessEqual(max(w, h), scanner.THUMB_LONG_MAX)

    def test_make_thumbnail_short_edge_840_and_color_managed(self):
        src = os.path.join(self.tmp, "big.jpg")
        Image.new("RGB", (4000, 3000), (220, 40, 40)).save(src, "JPEG", quality=95, icc_profile=p3_icc())
        out = os.path.join(self.thumbs, "t.jpg")
        self.assertTrue(scanner.make_thumbnail(src, out, "photo", None))
        with Image.open(out) as im:
            self.assertEqual(min(im.size), 840)
            self.assertEqual(max(im.size), 1120)
            self.assertNotIn("icc_profile", im.info)
            r, g, b = im.convert("RGB").getpixel((10, 10))
        self.assertGreater(r, 228, "P3 -> sRGB (výstup testu) – sytější červená")

    def test_version_upgrade_regenerates_old_thumbs_then_idles(self):
        src = os.path.join(self.tmp, "a.jpg")
        Image.new("RGB", (3000, 2000), (10, 120, 200)).save(src, "JPEG")
        mid = self.add(src)
        old = os.path.join(self.thumbs, f"thumb_{mid}.jpg")
        Image.new("RGB", (360, 240), (10, 120, 200)).save(old, "JPEG")
        os.utime(old, (time.time() - 100, time.time() - 100))
        with database.db_transaction() as conn:
            conn.execute("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", (old, mid))
        self.assertEqual(scanner.repair_missing_thumbnails(), 1, "stará verze se převede")
        with Image.open(old) as im:
            self.assertEqual(min(im.size), 840)
        self.assertEqual(scanner.thumb_version_state(), (scanner.thumb_version_tag(), None))
        self.assertEqual(scanner.repair_missing_thumbnails(), 0)

    def test_interrupted_upgrade_resumes_without_redoing(self):
        ids = []
        for i in range(3):
            src = os.path.join(self.tmp, f"s{i}.jpg")
            Image.new("RGB", (2000, 1500), (i * 40, 80, 80)).save(src, "JPEG")
            mid = self.add(src)
            th = os.path.join(self.thumbs, f"thumb_{mid}.jpg")
            Image.new("RGB", (360, 270)).save(th, "JPEG")
            os.utime(th, (time.time() - 100, time.time() - 100))
            with database.db_transaction() as conn:
                conn.execute("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", (th, mid))
            ids.append(mid)
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > 1                         # po první miniatuře přerušit
        self.assertEqual(scanner.repair_missing_thumbnails(should_stop=stop), 1)
        ver, since = scanner.thumb_version_state()
        self.assertIsNotNone(since, "převod rozpracovaný")
        self.assertEqual(scanner.repair_missing_thumbnails(), 2, "zbytek, ne znovu všechny")
        self.assertEqual(scanner.repair_missing_thumbnails(), 0)

    def test_failed_upgrade_keeps_old_thumbnail(self):
        src = os.path.join(self.tmp, "broken.jpg")
        with open(src, "wb") as f:
            f.write(b"not an image")
        mid = self.add(src)
        th = os.path.join(self.thumbs, f"thumb_{mid}.jpg")
        Image.new("RGB", (360, 270)).save(th, "JPEG")
        os.utime(th, (time.time() - 100, time.time() - 100))
        with database.db_transaction() as conn:
            conn.execute("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", (th, mid))
        with mock.patch.object(scanner, "_hidden_startupinfo", return_value=None), \
                mock.patch("os.path.exists", side_effect=lambda p, _e=os.path.exists: _e(p) and "ffmpeg" not in p.lower()):
            scanner.repair_missing_thumbnails()
        self.assertEqual(self.q("SELECT thumbnail_path FROM media_items")[0][0], th, "stará miniatura zůstává")

    def test_video_thumb_filter_tonemaps_hdr(self):
        with mock.patch("video_proxy.probe", return_value={"hdr": True, "transfer": "arib-std-b67"}):
            vf = scanner._video_thumb_filter("x.mov", "ffmpeg.exe")
        self.assertIn("tonemap", vf)
        self.assertIn("840", vf)
        with mock.patch("video_proxy.probe", return_value={"hdr": False}):
            self.assertNotIn("tonemap", scanner._video_thumb_filter("x.mov", "ffmpeg.exe"))


@unittest.skipUnless(HAS_QT, "PyQt6")
class TestGridTiers(_Tmp):

    def setUp(self):
        super().setUp()
        app()
        self.main = main

    def test_tier_mapping(self):
        t = self.main.thumb_tier
        self.assertEqual(t(180), 256)
        self.assertEqual(t(400), 512)
        self.assertEqual(t(800), 840)
        self.assertEqual(t(2000), 840)

    def test_worker_shrinks_to_tier_and_reports_source(self):
        src = os.path.join(self.tmp, "t.jpg")
        Image.new("RGB", (1120, 840), (1, 2, 3)).save(src, "JPEG")
        got, short = [], []
        w = self.main.ThumbnailWorker(7, src, None, tier=256)
        w.signals.loaded.connect(lambda i, img: got.append(img))
        w.signals.source_short.connect(lambda i, s: short.append(s))
        w.run()
        self.assertEqual(min(got[0].width(), got[0].height()), 256)
        self.assertEqual(short, [840])

    def test_model_upgrades_lazily_and_stops_at_source(self):
        model = self.main.ApplePhotoModel()
        rec = (5, "a", "a.jpg", "photo", None, "t.jpg", "2024", 0)
        model.set_records([rec])
        idx = model.index(0, 0)
        small = QPixmap(341, 256)
        model.ram_cache.put(5, small)
        model.set_tier(840)
        with mock.patch.object(model.thread_pool, "start") as start:
            self.assertIs(model.data(idx, model.ThumbRole), small, "dosavadní se kreslí dál")
            self.assertEqual(start.call_count, 1, "ostřejší úroveň na pozadí")
        model.loading_ids.clear()
        model._on_source_short(5, 256)                      # zdroj sám víc nemá
        with mock.patch.object(model.thread_pool, "start") as start:
            model.data(idx, model.ThumbRole)
            start.assert_not_called()

    def test_smaller_result_does_not_replace_sharper(self):
        model = self.main.ApplePhotoModel()
        model.set_records([(5, "a", "a.jpg", "photo", None, "t.jpg", "2024", 0)])
        big = QPixmap(1120, 840)
        model.ram_cache.put(5, big)
        img = QImage(341, 256, QImage.Format.Format_RGB32)
        model._on_thumb_loaded(5, img)
        self.assertIs(model.ram_cache.get(5), big)


class TestPreviewCache(ColorTargetMixin, unittest.TestCase):

    def test_path_is_versioned_by_profile(self):
        import preview_cache
        p = preview_cache.preview_path("C:/x/a.jpg")
        self.assertTrue(p.endswith(f".p{preview_cache.PREVIEW_VERSION}srgb.jpg"))
        with mock.patch.object(color_manage, "target_fingerprint", return_value="abcd1234"):
            self.assertNotEqual(preview_cache.preview_path("C:/x/a.jpg"), p)

    def test_sweep_legacy_keeps_current_and_strips(self):
        import preview_cache
        d = preview_cache.preview_dir()
        os.makedirs(d, exist_ok=True)
        cur = preview_cache.preview_path("C:/x/b.jpg")
        legacy = os.path.join(d, "0123456789abcdef0123.jpg")
        strip = preview_cache.base_path("C:/x/b.mov") + ".scrub"
        for f in (cur, legacy, strip):
            with open(f, "wb") as fh:
                fh.write(b"x")
        try:
            self.assertGreaterEqual(preview_cache.sweep_legacy(), 1)
            self.assertTrue(os.path.exists(cur))
            self.assertTrue(os.path.exists(strip))
            self.assertFalse(os.path.exists(legacy))
        finally:
            for f in (cur, strip):
                if os.path.exists(f):
                    os.remove(f)

    def test_configure_for_display_clamps(self):
        import preview_cache
        saved = preview_cache.PREVIEW_EDGE
        try:
            self.assertEqual(preview_cache.configure_for_display(2880), 2880)
            self.assertEqual(preview_cache.configure_for_display(1366), 2048)
            self.assertEqual(preview_cache.configure_for_display(7680), 3840)
        finally:
            preview_cache.PREVIEW_EDGE = saved


class TestPhashNormalized(unittest.TestCase):

    def test_sharpness_independent_of_thumb_size(self):
        import ai_engine
        tmp = tempfile.mkdtemp()
        try:
            rng = np.random.RandomState(3)
            base = Image.fromarray((rng.rand(840, 1120) * 255).astype("uint8")).convert("RGB")
            big = os.path.join(tmp, "big.jpg")
            small = os.path.join(tmp, "small.jpg")
            base.save(big, quality=95)
            base.resize((360, 270), Image.Resampling.LANCZOS).save(small, quality=95)
            h1, s1 = ai_engine.compute_phash_and_sharpness(big)
            h2, s2 = ai_engine.compute_phash_and_sharpness(small)
            self.assertLess(abs(s1 - s2) / max(s1, s2), 0.25)
            bits = bin(int(h1, 16) ^ int(h2, 16)).count("1")
            self.assertLessEqual(bits, 6)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestZoomGeometry(unittest.TestCase):

    def test_fit_width(self):
        import viewer_zoom as z
        self.assertEqual(z.fit_width(0.75, 1440, 900), 1200)       # 4:3 na výšku omezené
        self.assertEqual(z.fit_width(4 / 3, 1440, 900), 675)        # na výšku

    def test_clamp_center(self):
        import viewer_zoom as z
        self.assertEqual(z.clamp_center(0.1, 0.9, 1000, 0.75, 1440, 900), (0.5, 0.5))   # menší než plocha
        u, v = z.clamp_center(0.0, 1.0, 4000, 0.75, 1440, 900)
        self.assertAlmostEqual(u, 1440 / 8000)
        self.assertAlmostEqual(v, 1 - 900 / 6000)

    def test_zoom_about_keeps_point_under_cursor(self):
        import viewer_zoom as z
        vw, vh, aspect = 1440, 900, 0.75
        u, v, w = 0.5, 0.5, 3000.0
        ax, ay = 1000.0, 300.0
        pu = u + (ax - vw / 2) / w
        nu, nv = z.zoom_about(u, v, w, 6000.0, ax, ay, vw, vh, aspect)
        self.assertAlmostEqual(nu + (ax - vw / 2) / 6000.0, pu, places=6)

    def test_pick_level(self):
        import viewer_zoom as z
        widths = [5712, 2856, 1428]
        self.assertEqual(z.pick_level(widths, 5000), 0)
        self.assertEqual(z.pick_level(widths, 2000), 1)
        self.assertEqual(z.pick_level(widths, 900), 2)
        self.assertEqual(z.pick_level(widths, 9000), 0)

    def test_pyramid(self):
        import viewer_zoom as z
        app()
        img = QImage(4000, 3000, QImage.Format.Format_RGB888)
        lv = z.build_pyramid(img)
        self.assertEqual([i.width() for i in lv], [4000, 2000, 1000])
        self.assertTrue(all(i.format() == QImage.Format.Format_RGB32 for i in lv))


@unittest.skipUnless(HAS_QT, "PyQt6")
class TestCanvasZoom(unittest.TestCase):

    def setUp(self):
        app()
        import viewer_hud
        import motion
        self._rm = motion.reduced()
        motion.set_reduce_motion(True)                     # bez animací: stav hned
        self.canvas = viewer_hud.MediaCanvas(with_video=False)
        self.canvas.resize(1200, 800)
        self.canvas.show()
        QApplication.processEvents()
        img = QImage(2400, 1800, QImage.Format.Format_RGB32)
        img.fill(QColor(90, 120, 150))
        self.zoom = self.canvas.zoom
        self.zoom.set_photo("x.jpg", (8000, 6000))
        self.zoom.set_enabled(True)
        self.canvas.set_image(QPixmap.fromImage(img))

    def tearDown(self):
        import motion
        motion.set_reduce_motion(self._rm)
        self.canvas.close()

    def wheel(self, dy, mods=Qt.KeyboardModifier.NoModifier, pos=QPointF(600, 400)):
        vp = self.canvas.viewport()
        ev = QWheelEvent(pos, QPointF(vp.mapToGlobal(pos.toPoint())), QPoint(0, 0), QPoint(0, dy),
                         Qt.MouseButton.NoButton, mods, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(vp, ev)

    def test_wheel_zooms_and_requests_original(self):
        wanted = []
        self.zoom.full_res_wanted.connect(lambda: wanted.append(1))
        self.wheel(120)
        self.assertTrue(self.zoom.is_zoomed())
        self.assertTrue(self.zoom.item.isVisible())
        self.assertFalse(self.canvas.photo_item.isVisible())
        self.assertEqual(wanted, [1])
        self.wheel(-120)
        self.assertFalse(self.zoom.is_zoomed(), "zpět na vešlé")
        self.assertTrue(self.canvas.photo_item.isVisible())

    def test_full_levels_replace_source_and_old_token_ignored(self):
        self.wheel(120 * 8)
        lv = [QImage(8000, 6000, QImage.Format.Format_RGB32)]
        self.zoom.set_full_levels(self.zoom.token - 1, lv)
        self.assertNotEqual(self.zoom.item.source().width(), 8000)
        self.zoom.set_full_levels(self.zoom.token, lv)
        self.assertEqual(self.zoom.item.source().width(), 8000)

    def test_one_to_one_and_cap(self):
        dpr = self.canvas.devicePixelRatioF()
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        self.assertAlmostEqual(self.zoom.percent(self.zoom.w), 100.0, delta=0.5)
        self.zoom.zoom_to(1e9, animate=False)
        self.assertAlmostEqual(self.zoom.w, 8000 * 2.0 / dpr, delta=1)

    def test_pan_clamps_to_edges(self):
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        self.zoom.pan_by(1e6, 1e6)
        vp = self.canvas.viewport()
        self.assertAlmostEqual(self.zoom.u, vp.width() / (2 * self.zoom.w))
        self.assertAlmostEqual(self.zoom.item.pos().x(), 0.0, delta=0.5)

    def test_double_click_toggles_one_to_one_photo_but_fullscreen_for_video(self):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QEvent
        emitted = []
        self.canvas.double_clicked.connect(lambda: emitted.append(1))
        vp = self.canvas.viewport()

        def dbl():
            ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(300, 300), QPointF(vp.mapToGlobal(QPoint(300, 300))),
                             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(vp, ev)
        dbl()
        self.assertTrue(self.zoom.is_zoomed())
        self.assertEqual(emitted, [])
        dbl()
        self.assertFalse(self.zoom.is_zoomed())
        self.zoom.set_enabled(False)                        # video
        dbl()
        self.assertEqual(emitted, [1])

    def test_new_photo_resets(self):
        self.wheel(240)
        self.zoom.set_photo("y.jpg", (4000, 3000))
        self.assertFalse(self.zoom.is_zoomed())
        self.assertFalse(self.zoom.item.isVisible())

    def test_touchpad_scroll_pans_only_when_zoomed(self):
        self.assertFalse(self.zoom.wheel(_FakeWheel(dy=17)))          # vešlé: nespotřebuje
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        u = self.zoom.u
        self.assertTrue(self.zoom.wheel(_FakeWheel(dx=-40, dy=0, pixel=QPoint(-40, 0))))
        self.assertGreater(self.zoom.u, u)


class TestZoomDivisionGuards(unittest.TestCase):
    """ZeroDivisionError v zoom_about: cíl animace „vešlé“ = w 0.0 se stal základem dalšího zubu."""

    def test_zoom_about_invalid_sizes_return_input(self):
        import viewer_zoom as z
        for args in [(0.0, 3000.0, 1440, 900, 0.75), (3000.0, 0.0, 1440, 900, 0.75),
                     (-5.0, 3000.0, 1440, 900, 0.75), (3000.0, -1.0, 1440, 900, 0.75),
                     (3000.0, 6000.0, 0, 900, 0.75), (3000.0, 6000.0, 1440, 0, 0.75),
                     (3000.0, 6000.0, -10, -10, 0.75), (3000.0, 6000.0, 1440, 900, 0.0),
                     (1e-9, 3000.0, 1440, 900, 0.75), (float("nan"), 3000.0, 1440, 900, 0.75)]:
            w, new_w, vw, vh, aspect = args
            self.assertEqual(z.zoom_about(0.3, 0.7, w, new_w, 100.0, 100.0, vw, vh, aspect), (0.3, 0.7), args)

    def test_clamp_center_invalid_sizes_centre(self):
        import viewer_zoom as z
        self.assertEqual(z.clamp_center(0.1, 0.9, 0.0, 0.75, 1440, 900), (0.5, 0.5))
        self.assertEqual(z.clamp_center(0.1, 0.9, 4000, 0.0, 1440, 900), (0.5, 0.5))
        self.assertEqual(z.clamp_center(0.1, 0.9, 4000, 0.75, 0, 0), (0.5, 0.5))
        self.assertEqual(z.fit_width(0.75, 0, 900), 0.0)
        self.assertEqual(z.fit_width(0.0, 1440, 900), 0.0)


@unittest.skipUnless(HAS_QT, "PyQt6")
class TestCanvasZoomAnimated(unittest.TestCase):
    """S animacemi (dosavadní testy běží s omezeným pohybem a cestu přes _anim_to nepokryjí)."""

    def setUp(self):
        app()
        import viewer_hud
        import motion
        self._rm = motion.reduced()
        motion.set_reduce_motion(False)
        self.canvas = viewer_hud.MediaCanvas(with_video=False)
        self.canvas.resize(1200, 800)
        self.canvas.show()
        QApplication.processEvents()
        self.zoom = self.canvas.zoom
        self.zoom.set_photo("x.jpg", (8000, 6000))
        self.zoom.set_enabled(True)
        img = QImage(2400, 1800, QImage.Format.Format_RGB32)
        img.fill(QColor(90, 120, 150))
        self.canvas.set_image(QPixmap.fromImage(img))

    def tearDown(self):
        import motion
        self.zoom._stop_motion()
        motion.set_reduce_motion(self._rm)
        self.canvas.close()

    def notch(self, dy):
        self.assertTrue(self.zoom.wheel(_FakeWheel(dy=dy)))

    def finish(self):
        self.zoom._anim_t0 -= 10.0                          # animace hotová při dalším tiku
        self.zoom._tick()

    def test_notch_after_zoom_out_to_fit_during_animation(self):
        self.notch(120)                                     # dovnitř (animuje)
        self.notch(-120 * 3)                                # ven za vešlé -> cíl w = 0.0
        self.assertEqual(self.zoom._anim_to[0], 0.0)
        self.notch(-120)                                    # dřív ZeroDivisionError
        self.notch(120)
        self.finish()
        self.assertTrue(self.zoom.is_zoomed())

    def test_rapid_notches_to_both_limits(self):
        for _ in range(60):
            self.notch(120)
            self.zoom._tick()
        self.finish()
        self.assertAlmostEqual(self.zoom.w, self.zoom.max_w(), delta=1)
        for _ in range(60):
            self.notch(-120)
            self.zoom._tick()
        self.finish()
        self.assertFalse(self.zoom.is_zoomed())
        for dy in (120, -120) * 40:                         # střídání bez tiků (zběsilé kolečko)
            self.notch(dy)
        self.finish()
        self.assertTrue(math.isfinite(self.zoom.u) and math.isfinite(self.zoom.v))

    def test_extreme_factors_ignored_or_clamped(self):
        for f in (0.0, -1.0, float("nan"), float("inf"), 1e-300, 1e300):
            self.zoom.zoom_by(f, animate=False)
        self.assertTrue(math.isfinite(self.zoom.w))
        self.assertLessEqual(self.zoom.w, self.zoom.max_w() + 1e-6)

    def test_viewport_collapses_mid_animation(self):
        self.notch(120 * 4)
        real_fit = self.zoom.fit_w
        with mock.patch.object(self.zoom, "fit_w", return_value=0.0):
            self.zoom._anim_from = (0.0, 0.5, 0.5)          # start „vešlé“ při nulové ploše
            self.zoom._tick()                                # dřív math.log(0) ValueError
            self.notch(-120)
            self.zoom.resized()
        self.assertTrue(math.isfinite(self.zoom.w))
        self.assertGreater(real_fit(), 0)

    def test_wheel_before_layout(self):
        import viewer_hud
        c = viewer_hud.MediaCanvas(with_video=False)       # nezobrazené, bez obrazu
        try:
            zm = c.zoom
            zm.set_photo("x.jpg", (8000, 6000))
            zm.set_enabled(True)
            self.assertTrue(zm.wheel(_FakeWheel(dy=120)))   # aspect 0: nic, žádná výjimka
            self.assertFalse(zm.is_zoomed())
            img = QImage(2400, 1800, QImage.Format.Format_RGB32)
            c.set_image(QPixmap.fromImage(img))
            vp = mock.MagicMock()
            vp.width.return_value = 0
            vp.height.return_value = 0
            with mock.patch.object(c, "viewport", return_value=vp):
                self.assertTrue(zm.wheel(_FakeWheel(dy=120)))
                self.assertTrue(zm.wheel(_FakeWheel(dy=120, mods=Qt.KeyboardModifier.ControlModifier)))
                zm.gesture_zoom(0.3, QPointF(0, 0))
                zm.toggle_one_to_one()
                zm.pan_by(10, 10)
            self.assertFalse(zm.is_zoomed())
        finally:
            c.close()


class _FakeWheel:
    def __init__(self, dx=0, dy=0, pixel=None, mods=None):
        self._a = QPoint(dx, dy)
        self._p = pixel or QPoint(0, 0)
        self._m = mods or Qt.KeyboardModifier.NoModifier

    def angleDelta(self):
        return self._a

    def pixelDelta(self):
        return self._p

    def modifiers(self):
        return self._m

    def position(self):
        return QPointF(600, 400)


if __name__ == "__main__":
    unittest.main()
