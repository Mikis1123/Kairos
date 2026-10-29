"""Kairos Polish P5 – fyzický prohlížeč: stažení dolů do dlaždice a hero otevření z mřížky.

Spuštění (ve složce projektu):   python -m pytest test_polish_p5.py -v

Pokrývá:
  viewer_motion.py – fyzika tahu (postup, zmenšení, odpor nahoru), rychlost z posledních 80 ms,
                     rozhodnutí zavřít / vrátit, délka doletu podle rychlosti, výřez „výplní“.
  prohlížeč        – hero otevření z dlaždice (start v dlaždici, konec přes plochu, úklid snímků),
                     švih dolů = doletí do dlaždice a zavře, pomalý krátký tah = pružinou zpět,
                     vodorovný tah a video gesto nespouští, omezené animace = okamžitě.
"""
import sys
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEvent, QPointF, QRectF, QSize, QSizeF
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from PyQt6.QtGui import QMouseEvent
    import main
    import viewer_motion as vm
    from test_patch7 import TempEnv
    from test_patch11_maps import wait
else:                                    # bez Qt se třídy jen přeskočí
    TempEnv = unittest.TestCase


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPhysics(unittest.TestCase):

    def test_progress_and_rect(self):
        self.assertEqual(vm.drag_progress(-50, 800), 0.0)
        self.assertAlmostEqual(vm.drag_progress(240, 800), 0.5)
        self.assertEqual(vm.drag_progress(5000, 800), 1.0)
        fit = QRectF(100, 50, 800, 600)
        r = vm.drag_rect(fit, 20, 240, 0.5)
        self.assertAlmostEqual(r.width(), 800 * (1 - vm.SHRINK * 0.5))
        self.assertAlmostEqual(r.center().x(), fit.center().x() + 20)
        self.assertAlmostEqual(r.center().y(), fit.center().y() + 240)
        up = vm.drag_rect(fit, 0, -100, 0.0)
        self.assertAlmostEqual(up.center().y(), fit.center().y() - 25, msg="nahoru s odporem")

    def test_velocity_uses_recent_samples(self):
        self.assertEqual(vm.velocity([(0, 0)]), 0.0)
        samples = [(0, 0), (500, 10), (540, 40), (580, 100)]
        self.assertAlmostEqual(vm.velocity(samples), (100 - 10) / 80 * 1000)

    def test_dismiss_decision(self):
        self.assertTrue(vm.should_dismiss(0.3, 0))
        self.assertTrue(vm.should_dismiss(0.05, 1200), "rychlý švih stačí i na krátký tah")
        self.assertFalse(vm.should_dismiss(0.1, 200))
        self.assertFalse(vm.should_dismiss(0.6, -500), "švih zpět nahoru zruší")

    def test_settle_duration(self):
        fast, slow = vm.settle_ms(400, 3000), vm.settle_ms(400, 600)
        self.assertLess(fast, slow, "setrvačnost: rychlejší švih = kratší dolet")
        for ms in (fast, slow, vm.settle_ms(400, 0)):
            self.assertTrue(vm.MIN_SETTLE_MS <= ms <= vm.MAX_SETTLE_MS, ms)

    def test_cover_and_fit(self):
        crop = vm.cover_source(QSize(400, 200), QRectF(0, 0, 100, 100))
        self.assertEqual((crop.width(), crop.height()), (200, 200))
        self.assertEqual(crop.x(), 100)
        fit = vm.fit_rect(QSizeF(400, 200), QRectF(0, 0, 800, 800))
        self.assertEqual((fit.width(), fit.height(), fit.y()), (800, 400, 200))


def _mouse(kind, pos, buttons=Qt.MouseButton.LeftButton):
    button = Qt.MouseButton.LeftButton
    return QMouseEvent(kind, QPointF(pos), QPointF(pos), button, buttons, Qt.KeyboardModifier.NoModifier)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPhysicalViewer(TempEnv):

    def setUp(self):
        super().setUp()
        self.ids = {f"p{i}": self._add(self._jpg(f"IMG_{i}.jpg", (40 * i + 60, 120, 200), size=(320, 240)),
                                       date=f"2026-07-0{i + 1} 10:00:00") for i in range(3)}
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config")):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w.resize(1280, 820)
        self.w.show()
        _app.processEvents()
        self.addCleanup(self._close)
        self.model = self.w.grid_view.photo_model
        for _ in range(60):                    # náhledy dlaždic (vlákna) – hero potřebuje obrázek
            if all(self.model.ram_cache.get(i) is not None for i in self.ids.values()):
                break
            for r in range(self.model.rowCount()):
                self.model.data(self.model.index(r, 0), main.ApplePhotoModel.ThumbRole)
            wait(50)

    def _close(self):
        self.w.nav.reset()
        self.w.close()
        self.w.deleteLater()
        _app.processEvents()

    def row(self, key):
        return next(i for i, r in enumerate(self.w._filtered_records) if r[0] == self.ids[key])

    def open(self, key="p1"):
        self.w._on_photo_activated(self.row(key))
        return self.w._viewer

    def settle(self, viewer, ms=900):
        for _ in range(ms // 30):
            if not viewer.dismiss.overlay.is_animating():
                break
            wait(30)
        wait(30)

    def drag(self, viewer, steps, t0=1000.0):
        """steps [(dy, ms)] – posun od středu plochy v čase (řízené hodiny gesta)."""
        surface = viewer.canvas.viewport()
        c = QPointF(surface.width() / 2, surface.height() / 2)
        clock = {"t": t0}
        with mock.patch.object(vm.time, "monotonic", side_effect=lambda: clock["t"] / 1000.0):
            viewer.dismiss.eventFilter(surface, _mouse(QEvent.Type.MouseButtonPress, c))
            consumed = []
            for dy, ms in steps:
                clock["t"] += ms
                consumed.append(viewer.dismiss.eventFilter(
                    surface, _mouse(QEvent.Type.MouseMove, c + QPointF(dy[0], dy[1]) if isinstance(dy, tuple)
                                    else c + QPointF(0, dy))))
            last = steps[-1][0]
            end = c + (QPointF(*last) if isinstance(last, tuple) else QPointF(0, last))
            viewer.dismiss.eventFilter(surface, _mouse(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton))
        return consumed

    def test_hero_open_from_cell(self):
        v = self.open("p1")
        ov = v.dismiss.overlay
        self.assertTrue(ov.isVisible(), "fotka vylétá z dlaždice")
        self.assertTrue(ov.is_animating())
        start_rect = ov._from[0]
        cell, _ = self.w._grid_cell(self.w.grid_view, self.row("p1"), self.w.page_photos, False)
        self.assertLess(abs(start_rect.center().x() - cell.center().x()), 2)
        self.assertLess(abs(start_rect.width() - cell.width()), 2)
        self.assertEqual(ov._from[3], 1.0, "start = výřez jako v dlaždici")
        end_rect = ov._to[0]
        area = v.photo_area()
        self.assertTrue(area.adjusted(-1, -1, 1, 1).contains(end_rect), "konec = fotka přes plochu")
        self.settle(v)
        self.assertFalse(ov.isVisible())
        self.assertIsNone(ov.image)
        self.assertIsNone(ov.backdrop, "snímek mřížky se pustí")

    def test_flick_down_flies_into_cell_and_closes(self):
        v = self.open("p2")
        self.settle(v)
        wait(200)                                          # obraz v prohlížeči
        self.assertTrue(v.can_drag_dismiss())
        consumed = self.drag(v, [(20, 16), (60, 16), (120, 16)])
        self.assertTrue(consumed[-1], "tah dolů patří gestu")
        ov = v.dismiss.overlay
        self.assertEqual(ov._outcome, "dismissed")
        target = ov._to[0]
        cell, _ = self.w._grid_cell(self.w.grid_view, self.row("p2"), self.w.page_photos, True)
        self.assertLess(abs(target.center().y() - cell.center().y()), 2, "cíl = dlaždice v mřížce")
        self.assertIsNotNone(ov.backdrop)
        self.settle(v)
        wait(100)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_photos)
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertEqual(self.w.grid_view.currentIndex().row(), self.row("p2"))
        self.assertIsNone(ov.image)

    def test_slow_short_drag_springs_back(self):
        v = self.open("p0")
        self.settle(v)
        wait(200)
        self.drag(v, [(15, 120), (30, 120), (45, 120)])
        ov = v.dismiss.overlay
        self.assertEqual(ov._to[0], v.dismiss._fit, "zpět na celou plochu")
        self.settle(v)
        self.assertIs(self.w.stack.currentWidget(), v, "prohlížeč zůstal otevřený")
        self.assertFalse(ov.isVisible())

    def test_horizontal_and_video_do_not_start(self):
        v = self.open("p0")
        self.settle(v)
        wait(200)
        consumed = self.drag(v, [((40, 5), 16), ((90, 8), 16)])
        self.assertEqual(consumed, [False, False], "vodorovný tah = listování / nic")
        self.assertIs(self.w.stack.currentWidget(), v)
        with mock.patch.object(v.canvas, "is_video_shown", return_value=True):
            self.assertFalse(v.can_drag_dismiss())
            self.assertEqual(self.drag(v, [(40, 16), (120, 16)]), [False, False])

    def test_reduced_motion_is_instant(self):
        with mock.patch.object(main.motion, "reduced", return_value=True), \
                mock.patch.object(vm.motion, "reduced", return_value=True):
            v = self.open("p1")
            self.assertFalse(v.dismiss.overlay.isVisible(), "bez hero animace")
            wait(200)
            self.drag(v, [(40, 16), (140, 16)])
            _app.processEvents()
            self.assertIs(self.w.stack.currentWidget(), self.w.page_photos, "zavřeno hned")
            self.assertFalse(v.dismiss.overlay.isVisible())


if __name__ == "__main__":
    unittest.main()
