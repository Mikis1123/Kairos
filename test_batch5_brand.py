"""Kairos dávka 5 – značka: logo Iris, ikona .ico, úvodní obrazovka, indikátor práce na pozadí,
přejmenování a odolné hledání."""
import os
import sys
import tempfile
import unittest
from unittest import mock

try:
    from PyQt6.QtCore import QByteArray, QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter
    from PyQt6.QtWidgets import QApplication, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

if HAS_QT:
    import brand
    import motion
    import splash
    import tokens
    from ui_controls import ActivityStatus, parse_progress
    from test_patch6 import WindowEnv
else:                      # pragma: no cover
    WindowEnv = unittest.TestCase

HERE = os.path.dirname(os.path.abspath(__file__))


def _drawn_pixels(draw, size=96):
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    draw(p)
    p.end()
    return sum(1 for x in range(size) for y in range(size) if QColor.fromRgba(img.pixel(x, y)).alpha() > 0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestBrandMark(unittest.TestCase):

    def test_gap_sits_at_half_past_one(self):
        """Mezera prstence je v půl druhé (45°) a jiskra leží v ní, vně prstence."""
        self.assertAlmostEqual((brand.RING_START + brand.RING_SPAN) % 360, brand.GAP_AT - brand.GAP / 2)
        self.assertEqual(brand.RING_START, brand.GAP_AT + brand.GAP / 2)
        c = brand.spark_center()
        self.assertGreater(c.x(), brand.CENTER.x())
        self.assertLess(c.y(), brand.CENTER.y())
        self.assertGreater(brand.SPARK_R, brand.RING_R)

    def test_ring_progress_draws_more(self):
        part = _drawn_pixels(lambda p: brand.paint_mark(p, QRectF(0, 0, 96, 96), ring=0.3, spark=0))
        full = _drawn_pixels(lambda p: brand.paint_mark(p, QRectF(0, 0, 96, 96), ring=1.0, spark=0))
        none = _drawn_pixels(lambda p: brand.paint_mark(p, QRectF(0, 0, 96, 96), ring=0.0, spark=0))
        self.assertEqual(none, 0)
        self.assertLess(part, full)

    def test_svg_is_valid_and_matches_geometry(self):
        from PyQt6.QtSvg import QSvgRenderer
        for tile in (False, True):
            svg = brand.mark_svg(tile=tile)
            self.assertTrue(QSvgRenderer(QByteArray(svg.encode())).isValid())
            self.assertIn(brand.IRIS_LIGHT, svg)
            self.assertIn(brand.SPARK, svg)

    def test_ico_has_every_size_rendered_separately(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            path = brand.build_ico(os.path.join(d, "k.ico"))
            with Image.open(path) as ico:
                self.assertEqual(sorted(ico.info["sizes"]), [(s, s) for s in brand.ICO_SIZES])
                ico.size = (16, 16)
                small = ico.convert("RGBA")
        # 16 px = vlastní tučná varianta, ne zmenšenina 256 px: obsahuje zřetelně světlé pixely prstence
        import numpy as np
        px = np.asarray(small)
        bright = int(((px[..., 3] > 200) & (px[..., 2] > 180)).sum())
        self.assertGreater(bright, 20)

    def test_shipped_assets_exist(self):
        for name in brand.ASSETS:
            self.assertTrue(os.path.exists(os.path.join(HERE, "assets", name)), name)

    def test_app_icon_sizes(self):
        sizes = {(s.width(), s.height()) for s in brand.app_icon().availableSizes()}
        for s in (16, 32, 48, 256):
            self.assertIn((s, s), sizes)

    def test_wordmark_ring_is_the_o(self):
        """Prstenec sedí mezi „Kair“ a „s“ na účaří ve výšce malých písmen."""
        width, _h, _xk, x_ring, x_s, side, fm = brand.wordmark_layout(60)
        r = brand.ring_rect_in_wordmark(QPointF(0, 100), 60)
        outer = side * (2 * brand.RING_R + brand.RING_STROKE) / brand.GRID
        self.assertAlmostEqual(r.center().x(), x_ring + outer / 2, places=3)
        self.assertLess(x_ring, x_s)
        self.assertAlmostEqual(outer, fm.xHeight() * 1.18, places=3)
        self.assertGreater(_drawn_pixels(lambda p: brand.Wordmark(20).render(p), 200), 0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSplash(unittest.TestCase):

    def tearDown(self):
        motion.set_reduce_motion(False)

    def test_reduced_motion_shows_final_mark_at_once(self):
        motion.set_reduce_motion(True)
        s = splash.KairosSplash("dark")
        s.play()
        self.assertTrue(s.intro_done())
        s.wait_until(lambda: True, timeout_ms=2000)        # vrátí se hned
        s.finish(None)
        _app.processEvents()

    def test_intro_runs_then_waits_for_loader(self):
        s = splash.KairosSplash("light")
        s.show()
        s.play()
        ready = {"v": False}
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(splash.INTRO_MS + 300, lambda: ready.update(v=True))
        s.wait_until(lambda: ready["v"], timeout_ms=10000)
        self.assertTrue(s.intro_done())
        self.assertTrue(ready["v"])
        s.finish(None)
        for _ in range(40):
            _app.processEvents()
        s.close()

    def test_every_frame_paints(self):
        s = splash.KairosSplash("dark")
        for t in (0.0, 0.2, 0.5, 0.6, 0.75, 0.9, 1.0):
            s._t = t
            self.assertFalse(s.grab().isNull())

    def test_boot_hook_runs_before_heavy_imports(self):
        """main.py ukáže splash dřív, než načte PyQt widgety, AI moduly a databázi."""
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        hook = src.index("splash.boot()")
        self.assertLess(hook, src.index("from PyQt6.QtWidgets import"))
        self.assertLess(hook, src.index("from ai_engine import"))
        self.assertLess(src.index('os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]'), hook)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestActivityStatus(unittest.TestCase):

    def test_parse_progress(self):
        self.assertAlmostEqual(parse_progress("Počítám otisky (12/40)..."), 0.3)
        self.assertAlmostEqual(parse_progress("Náhledy 45 %"), 0.45)
        self.assertIsNone(parse_progress("Shlukuji výlety..."))
        self.assertIsNone(parse_progress("(3/0)"))
        self.assertIsNone(parse_progress(None))

    def test_label_compatible_and_tasks_independent(self):
        w = ActivityStatus()
        w.setText("Hledám duplicity...")
        self.assertEqual(w.text(), "Hledám duplicity...")
        w.set_task("search", "Hledám „pes“…")
        self.assertEqual(w.current_text(), "Hledám „pes“…", "poslední změněná úloha")
        w.setText("")
        self.assertEqual(w.text(), "")
        self.assertEqual(w.tasks(), {"search": "Hledám „pes“…"}, "vymazání stavu nesmaže hledání")

    def test_short_task_never_shows_and_long_one_fades_in(self):
        from PyQt6.QtTest import QTest
        host = QWidget()
        w = ActivityStatus(host)
        host.show()
        w.set_task("search", "Hledám…")
        w.set_task("search", "")
        QTest.qWait(ActivityStatus.SHOW_DELAY_MS + 80)
        self.assertTrue(w.isHidden(), "krátká úloha neblikne")
        w.setText("Shlukuji výlety...")
        QTest.qWait(ActivityStatus.SHOW_DELAY_MS + 80)
        self.assertTrue(w.isVisible())
        self.assertEqual(w.height(), ActivityStatus.HEIGHT)
        self.assertFalse(w.grab().isNull())
        w.setText("")
        QTest.qWait(tokens.MOTION["standard"][0] + 150)
        self.assertTrue(w.isHidden())
        self.assertEqual(w.height(), 0)
        host.close()

    def test_reduced_motion_paints_static(self):
        motion.set_reduce_motion(True)
        try:
            w = ActivityStatus()
            w.setText("Počítám otisky (1/2)...")
            w._reveal()
            self.assertNotEqual(w._spin.state(), w._spin.State.Running)
            self.assertFalse(w.grab().isNull())
        finally:
            motion.set_reduce_motion(False)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestRenamedWindow(WindowEnv):
    N_PHOTOS = 2

    def test_title_and_sidebar_brand(self):
        self.assertEqual(self.w.windowTitle(), "Kairos")
        self.assertTrue(self.w.findChildren(brand.BrandMark))
        self.assertIsInstance(self.w.lbl_status, ActivityStatus)

    def test_search_shows_and_clears_activity(self):
        import ai_engine
        self.w._embeddings_cache = {1: b"neplatny"}       # search_by_text vrátí [] bez modelu
        self.w._search_query = "les"
        with mock.patch.object(ai_engine, "_text_model", object()):
            self.w._run_ai_search()
            self.assertIn("search", self.w.lbl_status.tasks())
            for wk in list(self.w._search_workers):
                wk.wait(5000)
            for _ in range(50):
                _app.processEvents()
                if not self.w._search_workers:
                    break
        self.assertNotIn("search", self.w.lbl_status.tasks())

    def test_no_old_name_left_in_ui_sources(self):
        for name in ("main.py", "settings_dialog.py", "map_vault.html", "app.spec"):
            src = open(os.path.join(HERE, name), encoding="utf-8").read()
            self.assertNotIn("OmniPhoto Vault", src, name)
            self.assertNotIn("name='OmniPhotoVault'", src, name)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSearchRobustness(unittest.TestCase):

    def test_bad_inputs_return_empty(self):
        import ai_engine
        self.assertEqual(ai_engine.search_by_text(None, {1: b"x"}), [])
        self.assertEqual(ai_engine.search_by_text("   ", {1: b"x"}), [])
        self.assertEqual(ai_engine.search_by_text("pes", {}), [])
        self.assertEqual(ai_engine.search_by_text("pes", {1: None, 2: b"short"}), [])

    def test_reloaded_embeddings_same_count_rebuild_index(self):
        """Nový slovník se stejným počtem embeddingů (jiné fotky) nesmí použít starou matici."""
        import numpy as np
        import ai_engine
        rng = np.random.default_rng(3)
        q = rng.normal(size=ai_engine.EMBED_DIM).astype(np.float32)
        q /= np.linalg.norm(q)

        def library(hit_id):
            emb = {}
            for i in range(1, 41):
                v = rng.normal(size=ai_engine.EMBED_DIM).astype(np.float32)
                v -= v.dot(q) * q
                v /= np.linalg.norm(v)
                emb[i] = ai_engine.encode_embedding(0.6 * q + 0.8 * v if i == hit_id else v)
            return emb
        model = mock.Mock()
        model.encode.return_value = q
        with mock.patch.object(ai_engine, "get_text_model", return_value=model):
            first = ai_engine.search_by_text("pes", library(7))
            second = ai_engine.search_by_text("pes", library(9))
        self.assertEqual(first[0][0], 7)
        self.assertEqual(second[0][0], 9)

    def test_mismatched_or_nan_query_vector(self):
        import numpy as np
        import ai_engine
        vec = np.ones(ai_engine.EMBED_DIM, dtype=np.float32)
        emb = {1: ai_engine.encode_embedding(vec)}
        for bad in (np.ones(10), np.full(ai_engine.EMBED_DIM, np.nan)):
            model = mock.Mock()
            model.encode.return_value = bad
            with mock.patch.object(ai_engine, "get_text_model", return_value=model):
                self.assertEqual(ai_engine.search_by_text("pes", emb), [])


if __name__ == "__main__":
    unittest.main()
