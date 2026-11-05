"""Sprint 3 – native mini-map (mini_map.py) and its hand-off to the Map tab.

Run:   python -m pytest test_sprint3_map.py -v

  * tile maths: Web Mercator position, the tiles covering the panel (nearest first, antimeridian)
  * tiles from the SQLite tile store on worker threads, offline: stored / gzip / missing
  * the widget paints stored tiles, falls back to a cached ancestor, draws the pin, emits clicks
  * the viewer uses the native widget (no QWebEngineView, no mini_map.html) and forwards clicks
  * the main window opens the Map tab and flies there (focusPlace), also before the page loaded
The base-map cross-fade (map_vault.html) is covered in test_patch11_maps.py (test_14).
"""
import gzip
import inspect
import os
import sys
import tempfile
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
    from PyQt6.QtGui import QColor, QImage, QMouseEvent
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    import mini_map
    import tile_store
    from test_patch11_maps import wait

HERE = os.path.dirname(os.path.abspath(__file__))


def _until(pred, tries=80, step=50):
    for _ in range(tries):
        if pred():
            return True
        wait(step)
    return False


def _png(color):
    img = QImage(256, 256, QImage.Format.Format_RGB32)
    img.fill(QColor(*color))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def _offline_server(tmp):
    store = tile_store.TileStore(path=os.path.join(tmp, "tiles.sqlite"), hot_mb=0)
    return tile_store.TileServer(store, online=False, shards=1)


class TestTileMaths(unittest.TestCase):

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_world_px(self):
        self.assertEqual(mini_map.world_px(0, 0, 0), (128.0, 128.0))
        x, y = mini_map.world_px(90, 180, 1)
        self.assertEqual(x, 512.0)
        self.assertAlmostEqual(y, 0.0, places=3)            # clamped to the Mercator limit

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_layout_covers_panel_nearest_first(self):
        z, size, tiles = mini_map.layout_tiles(49.2, 16.6, 320, 180, 1.0)
        self.assertEqual((z, size), (mini_map.VIEW_ZOOM, 256.0))
        lefts = [t[2] for t in tiles]
        tops = [t[3] for t in tiles]
        self.assertLessEqual(min(lefts), 0)
        self.assertGreaterEqual(max(lefts) + size, 320)
        self.assertLessEqual(min(tops), 0)
        self.assertGreaterEqual(max(tops) + size, 180)
        cx, cy = mini_map.world_px(49.2, 16.6, z)
        self.assertEqual(tiles[0][:2], (int(cx // 256), int(cy // 256)), "the photo's own tile first")

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_hidpi_one_level_deeper_at_device_pixels(self):
        z, size, tiles = mini_map.layout_tiles(49.2, 16.6, 320, 180, 2.0)
        self.assertEqual((z, size), (mini_map.VIEW_ZOOM + 1, 128.0))
        self.assertGreaterEqual(len(tiles), 6)

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_antimeridian_wraps(self):
        z, _size, tiles = mini_map.layout_tiles(0.0, 179.999, 320, 180, 1.0)
        n = 2 ** z
        xs = {t[0] for t in tiles}
        self.assertIn(0, xs, "east of 180° continues at x = 0")
        self.assertIn(n - 1, xs)
        self.assertTrue(all(0 <= x < n for x in xs))

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_key_is_the_main_maps_standard_raster(self):
        key = mini_map.tile_key(15, 17000, 11000)
        self.assertEqual(key, "server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/15/11000/17000")
        self.assertIn(mini_map.TILE_HOST, tile_store.TILE_HOSTS)
        import main
        self.assertTrue(main.is_allowed_web_url("https://" + key))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestFetch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.srv = _offline_server(self.tmp.name)
        self.addCleanup(self._close)

    def _close(self):
        self.srv.shutdown()
        self.srv.store.close()
        self.tmp.cleanup()

    def test_stored_gzip_and_missing(self):
        a, b = mini_map.tile_key(15, 1, 1), mini_map.tile_key(15, 2, 1)
        png = _png((255, 0, 0))
        self.srv.store.put(a, png, "image/png", None)
        self.srv.store.put(b, gzip.compress(png), "image/png", "gzip")
        self.srv.store.flush()
        self.assertEqual(mini_map.fetch_tile(a, self.srv), png)
        self.assertEqual(mini_map.fetch_tile(b, self.srv), png, "gzip stored = decompressed")
        self.assertIsNone(mini_map.fetch_tile(mini_map.tile_key(15, 3, 1), self.srv), "offline + missing")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestWidget(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.srv = _offline_server(self.tmp.name)
        self.w = mini_map.MiniMapWidget(accent="#5E64E8")
        self.w.service_provider = lambda: self.srv
        self.w.resize(320, mini_map.HEIGHT)
        self.w.show()
        self.addCleanup(self._close)

    def _close(self):
        self.w.hide()
        self.w.deleteLater()
        wait(30)
        self.srv.shutdown()
        self.srv.store.close()
        self.tmp.cleanup()

    def _fill(self, keys, color):
        png = _png(color)
        for k in keys:
            self.srv.store.put(k, png, "image/png", None)
        self.srv.store.flush()

    def _pixel(self, x, y):
        img = self.w.grab().toImage()
        dpr = img.devicePixelRatio() or 1.0
        return img.pixelColor(int(x * dpr), int(y * dpr))

    def test_rejects_bad_places(self):
        self.assertFalse(self.w.show_place("x", 2))
        self.assertFalse(self.w.show_place(91, 2))
        self.assertFalse(self.w.show_place(float("nan"), 2))
        self.assertIsNone(self.w.place)

    def test_paints_stored_tiles_and_the_pin(self):
        self.w.place = (49.2, 16.6)
        oz = mini_map.tile_zoom(self.w._dpr()) - mini_map.OVERVIEW_DROP
        cx, cy = mini_map.world_px(49.2, 16.6, oz)
        self._fill(self.w.visible_keys() + [mini_map.tile_key(oz, int(cx // 256), int(cy // 256))], (255, 0, 0))
        self.assertTrue(self.w.show_place(49.2, 16.6))
        self.assertTrue(_until(self.w.loaded), "tiles decoded on workers, delivered to the GUI")
        corner = self._pixel(30, 30)
        self.assertGreater(corner.red(), 200)
        self.assertLess(corner.green(), 60, "the stored tile is on screen")
        core = self._pixel(160, 90)
        self.assertGreater(core.blue(), 180, "accent core of the position dot")
        self.assertLess(core.red(), 140)
        ring = self._pixel(160 + 7.5, 90)
        self.assertGreater(min(ring.red(), ring.green(), ring.blue()), 200, "white ring")
        n = self.w.requests
        self.w.show_place(49.2, 16.6)
        self.assertEqual(self.w.requests, n, "cached tiles aren't fetched again")

    def test_missing_tiles_fall_back_to_the_overview(self):
        self.w.place = (49.2, 16.6)
        z = mini_map.tile_zoom(self.w._dpr())
        oz = z - mini_map.OVERVIEW_DROP
        cx, cy = mini_map.world_px(49.2, 16.6, oz)
        self._fill([mini_map.tile_key(oz, int(cx // 256), int(cy // 256))], (0, 200, 0))
        self.w.show_place(49.2, 16.6)
        self.assertTrue(_until(self.w.loaded), "detail tiles reported missing (offline)")
        self.assertTrue(_until(lambda: self._pixel(30, 30).green() > 150), "upscaled overview, not a hole")
        self.assertLess(self._pixel(30, 30).red(), 60)

    def test_offline_without_tiles_shows_background_and_pin(self):
        self.w.show_place(10.0, 10.0)
        self.assertTrue(_until(self.w.loaded))
        bg = QColor(mini_map.BACKGROUND)
        px = self._pixel(30, 30)
        self.assertLessEqual(abs(px.red() - bg.red()) + abs(px.green() - bg.green()) + abs(px.blue() - bg.blue()), 6)
        self.assertGreater(self._pixel(160, 90).blue(), 180)

    def test_click_emits_place(self):
        got = []
        self.w.clicked.connect(lambda la, lo: got.append((la, lo)))
        self.w.show_place(45.1, 13.6)
        pos = QPointF(50, 50)
        for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
            ev = QMouseEvent(kind, pos, self.w.mapToGlobal(pos), Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(self.w, ev)
        self.assertEqual(got, [(45.1, 13.6)])
        self.assertEqual(self.w.cursor().shape(), Qt.CursorShape.PointingHandCursor)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestViewerNativeMiniMap(unittest.TestCase):

    def setUp(self):
        from test_patch10 import TempEnv
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        import main
        import viewer
        self.main, self.viewer = main, viewer
        self.env.add(self.env.jpg("A.jpg"), date="2026-07-01 10:00:00")
        self.env.add(self.env.jpg("B.jpg"), date="2026-07-02 10:00:00")
        self.env.x("UPDATE media_items SET latitude = 45.1, longitude = 13.6 WHERE file_name = 'A.jpg'")
        self.env.x("UPDATE media_items SET latitude = 50.08, longitude = 14.43 WHERE file_name = 'B.jpg'")
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.srv = _offline_server(self.tmp.name)
        self.v = viewer.MediaViewerWidget()
        self.v.tile_service = lambda: self.srv
        self.v.resize(1100, 700)
        self.v.show()
        self.addCleanup(self._close)

    def _close(self):
        self.v.cleanup_playback()
        self.v.hide()
        self.v.deleteLater()
        wait(50)
        self.srv.shutdown()
        self.srv.store.close()
        self.tmp.cleanup()

    def test_native_widget_moves_with_photos_and_forwards_clicks(self):
        self.assertIsNone(self.v.mini_map, "nothing built before a place is shown")
        self.v.set_media_data(self.main.fetch_media_records(order_by="m.date_taken, m.id"), 0)
        self.v.btn_info.setChecked(True)
        self.v.toggle_info_panel()
        m = self.v.mini_map
        self.assertIsInstance(m, mini_map.MiniMapWidget)
        self.assertEqual(m.place, (45.1, 13.6))
        self.assertIs(m.service_provider(), self.srv, "tile store from main.create_media_viewer")
        self.v.show_next()
        self.assertIs(self.v.mini_map, m, "one widget, the place moves")
        self.assertEqual(m.place, (50.08, 14.43))
        got = []
        self.v.show_on_map_requested.connect(lambda la, lo: got.append((la, lo)))
        m.clicked.emit(50.08, 14.43)
        self.assertEqual(got, [(50.08, 14.43)])

    def test_viewer_has_no_web_engine(self):
        src = inspect.getsource(self.viewer)
        self.assertNotIn("QWebEngine", src)
        self.assertNotIn("mini_map.html", src)
        self.assertFalse(os.path.exists(os.path.join(HERE, "mini_map.html")))
        spec = open(os.path.join(HERE, "app.spec"), encoding="utf-8").read()
        self.assertNotIn("mini_map.html", spec)
        v = self.main.create_media_viewer([], 0)
        self.addCleanup(v.deleteLater)
        self.assertIs(v.tile_service, self.main.mini_map_tile_service)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestShowPlaceOnMap(unittest.TestCase):

    def setUp(self):
        from test_patch9 import TempEnv as Env9
        import main
        self.main = main
        self.env = Env9("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config")):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w._ensure_map_view()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        self.calls = []
        p = mock.patch.object(self.w.map_view.page(), "runJavaScript",
                              side_effect=lambda code, *a: self.calls.append(code))
        p.start()
        self.addCleanup(p.stop)

    def test_opens_map_tab_and_flies_there(self):
        self.w._map_loaded = True
        self.assertTrue(self.w.show_place_on_map(49.2, 16.6))
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)
        self.assertTrue(any("focusPlace(49.2, 16.6)" in c for c in self.calls), self.calls)

    def test_waits_for_the_map_page(self):
        self.w._map_loaded = False
        self.assertTrue(self.w.show_place_on_map(45.1, 13.6))
        self.assertFalse(any("focusPlace" in c for c in self.calls))
        self.assertEqual(self.w._pending_place, (45.1, 13.6))
        self.w._on_map_loaded(True)
        self.assertTrue(any("focusPlace(45.1, 13.6)" in c for c in self.calls), self.calls)
        self.assertIsNone(self.w._pending_place)

    def test_viewer_click_is_wired(self):
        v = self.w._ensure_viewer()
        self.calls.clear()
        self.w._map_loaded = True
        v.show_on_map_requested.emit(48.2, 16.37)
        self.assertTrue(any("focusPlace(48.2, 16.37)" in c for c in self.calls), self.calls)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)


if __name__ == "__main__":
    unittest.main()
