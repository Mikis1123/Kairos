"""Výkon mřížky: klidové dlaždice se kreslí z mezipaměti hotových pixmap (posouvání 60 FPS).

Spuštění (ve složce projektu):   python -m pytest test_perf_grid.py -v
"""
import sys
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from PyQt6.QtGui import QColor, QImage, QPixmap
    import main


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestGridTileCache(unittest.TestCase):

    def setUp(self):
        self.host = main.QWidget()
        self.host.resize(800, 600)
        self.addCleanup(self.host.deleteLater)
        self.grid = main.AppleGridView(mock.Mock(), self.host)
        self.grid.setGeometry(0, 0, 800, 600)
        recs = [(i, f"p{i}.jpg", f"C:/x/p{i}.jpg", "live_photo" if i == 1 else "photo", None, None,
                 "2024-01-01 10:00", 1 if i == 2 else 0) for i in range(40)]
        self.grid.photo_model.set_records(recs)
        for i in range(40):
            img = QImage(360, 270, QImage.Format.Format_RGB32)
            img.fill(QColor(i * 5, 80, 120))
            self.grid.photo_model.ram_cache.put(i, QPixmap.fromImage(img))
        self.host.show()
        _app.processEvents()
        self.d = self.grid.photo_delegate

    def paint(self):
        # grab() kreslí i bez vystavení okna (v celé sadě testů nemusí být okno ještě namapované)
        self.grid.doItemsLayout()
        self.grid.viewport().grab()

    def test_scrolling_reuses_finished_tiles(self):
        self.paint()
        n = len(self.d._tiles)
        self.assertGreater(n, 0)
        with mock.patch.object(QPixmap, "scaled", side_effect=AssertionError("škálování při posunu")):
            sb = self.grid.verticalScrollBar()
            sb.setValue(sb.value() + 1)
            self.paint()
        self.assertEqual(len(self.d._tiles), n, "stejné dlaždice, žádné nové")

    def test_badge_shade_is_part_of_the_cache_key(self):
        self.paint()
        shades = {k[4] for k in self.d._tiles}
        self.assertIn((False, False), shades)
        self.assertIn((True, False), shades, "Live Photo: ztmavení pod odznakem")
        self.assertIn((False, True), shades, "oblíbená: ztmavení pod srdcem")

    def test_hovered_tile_bypasses_cache(self):
        self.paint()
        n = len(self.d._tiles)
        self.grid.hover_progress = lambda row: 0.5 if row == 0 else 0.0
        self.paint()
        self.assertEqual(len(self.d._tiles), n, "animovaná dlaždice se do mezipaměti neukládá")

    def test_budget_evicts_oldest(self):
        self.d._tiles.clear()
        self.d._tiles_bytes = 0
        with mock.patch.object(main, "TILE_CACHE_BYTES", 1):
            self.paint()
        self.assertEqual(len(self.d._tiles), 1)
        tile = next(iter(self.d._tiles.values()))
        self.assertEqual(self.d._tiles_bytes, tile.width() * tile.height() * 4)


if __name__ == "__main__":
    unittest.main()
