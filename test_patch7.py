"""Regresní testy pro Patch 7 – Fáze 2: prohlížeč s plovoucím HUD a prezentace.

Spuštění (ve složce projektu):   python -m pytest test_patch7.py -v

Pokrývá:
  Video ve scéně – QGraphicsVideoItem v QGraphicsView (žádný nativní QVideoWidget),
                   plakát do prvního snímku, prolínání fotek.
  Skleněný HUD   – kapsle s rozostřeným pozadím; po 2 s nečinnosti zmizí HUD i kurzor,
                   zůstane při najetí myší / tažení posuvníku; klávesy fungují i se skrytým HUD.
  Celá obrazovka – F11 / tlačítko / dvojklik, bez postranního panelu, Esc vrací.
  Prezentace     – S nebo tlačítko, černé pozadí bez ovládání, prolínání, mezerník = pauza,
                   šipky ručně, Esc zpět na stejný snímek; orientace z EXIFu.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QEvent, QPointF, QSize
    from PyQt6.QtGui import QKeyEvent, QMouseEvent, QPixmap, QColor
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    from PyQt6.QtMultimediaWidgets import QVideoWidget, QGraphicsVideoItem
    import main
    import viewer_hud
    from viewer import MediaViewerWidget


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def press(widget, key, modifiers=Qt.KeyboardModifier.NoModifier):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))
    _app.processEvents()


class TempEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p7_test_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def x(self, sql, params=()):
        with database.db_transaction() as conn:
            return conn.execute(sql, params).lastrowid

    def _jpg(self, name, color=(200, 80, 40), size=(64, 48), exif_orientation=None):
        path = os.path.join(self.tmp, name)
        img = Image.new("RGB", size, color)
        if exif_orientation:
            exif = Image.Exif()
            exif[274] = exif_orientation
            img.save(path, "JPEG", exif=exif.tobytes())
        else:
            img.save(path, "JPEG")
        return path

    def _blob(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 128)
        return path

    def _add(self, path, media_type="photo", paired=None, thumb=None, date="2026-07-07 10:00:00"):
        return self.x("INSERT INTO media_items (file_path, file_name, media_type, paired_video_path, "
                      "thumbnail_path, date_taken) VALUES (?, ?, ?, ?, ?, ?)",
                      (path, os.path.basename(path), media_type, paired, thumb, date))

    def _library(self):
        """3 fotky, Live Photo (+ pohybové video) a video s náhledem."""
        ids = {}
        for i in range(3):
            ids[f"p{i}"] = self._add(self._jpg(f"IMG_{i}.jpg", (40 * i, 120, 200)),
                                     date=f"2026-07-0{i + 1} 10:00:00")
        motion = self._blob("IMG_L.MOV")
        ids["live"] = self._add(self._jpg("IMG_L.jpg"), "live_photo", paired=motion, date="2026-07-05 10:00:00")
        self._add(motion, "video", date="2026-07-05 10:00:00")
        ids["video"] = self._add(self._blob("IMG_V.MOV"), "video", thumb=self._jpg("thumb_v.jpg", (10, 10, 10)),
                                 date="2026-07-06 10:00:00")
        return ids

    def _records(self):
        return main.fetch_media_records(order_by="m.date_taken, m.id")


# ═════════════════════════════════════════════════════════════════════════════
# Plátno: video ve scéně
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMediaCanvas(TempEnv):

    def setUp(self):
        super().setUp()
        self.canvas = viewer_hud.MediaCanvas()
        self.canvas.resize(800, 600)
        self.canvas.show()
        _app.processEvents()
        self.addCleanup(self.canvas.deleteLater)

    def test_video_layer_is_not_a_native_window(self):
        """Patch 8: video přes QQuickWidget (GPU), záložně QGraphicsVideoItem – nikdy nativní
        povrch, takže HUD nad ním zůstává průsvitný (původně Phase 2 test na QGraphicsVideoItem)."""
        c = self.canvas
        if c.video_backend == "quick":
            from PyQt6.QtQuickWidgets import QQuickWidget
            self.assertIsInstance(c.video_surface, QQuickWidget)
            self.assertFalse(c.video_surface.isWindow())
            self.assertFalse(c.video_surface.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
            self.assertIs(c.video_surface.parentWidget(), c)
            self.assertEqual(c.video_output.objectName(), "video")
        else:
            self.assertIsInstance(c.video_item, QGraphicsVideoItem)
            self.assertIn(c.video_item, c.scene().items())

    def test_image_fits_and_is_centered(self):
        self.canvas.set_image(QPixmap(400, 100))
        item = self.canvas.photo_item
        rect = item.sceneBoundingRect()
        vw, vh = self.canvas.viewport().width(), self.canvas.viewport().height()
        self.assertAlmostEqual(rect.width(), vw, delta=1)
        self.assertAlmostEqual(rect.center().y(), vh / 2, delta=1)

    def test_crossfade(self):
        a, b = QPixmap(200, 100), QPixmap(100, 200)
        a.fill(QColor("red"))
        b.fill(QColor("blue"))
        self.canvas.set_image(a)
        self.canvas.set_image(b, fade_ms=120)
        self.assertTrue(self.canvas.is_fading())
        self.assertTrue(self.canvas.back_item.isVisible(), "odcházející snímek je vidět")
        self.assertLess(self.canvas.photo_item.opacity(), 1.0)
        for _ in range(40):          # první snímky OpenGL viewportu mohou chvíli trvat
            if not self.canvas.is_fading():
                break
            wait(50)
        self.assertFalse(self.canvas.is_fading())
        self.assertFalse(self.canvas.back_item.isVisible())
        self.assertEqual(self.canvas.photo_item.opacity(), 1.0)
        self.assertEqual(self.canvas.current_pixmap().size(), b.size())

    def test_show_video_switches_layers(self):
        self.canvas.set_image(QPixmap(50, 50))
        self.canvas.show_video(True)
        self.assertTrue(self.canvas.is_video_shown())
        self.assertFalse(self.canvas.photo_item.isVisible())
        self.canvas.show_video(False)
        self.assertTrue(self.canvas.photo_item.isVisible())


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class ViewerEnv(TempEnv):

    def setUp(self):
        super().setUp()
        self.ids = self._library()
        self.records = self._records()
        self.viewer = MediaViewerWidget()
        self.viewer.resize(1100, 720)
        self.viewer.show()
        _app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def open(self, key):
        index = next(i for i, r in enumerate(self.records) if r[0] == self.ids[key])
        self.viewer.set_media_data(list(self.records), index)
        _app.processEvents()
        return index


class TestViewerVideoOutput(ViewerEnv):

    def test_no_native_video_widget(self):
        self.assertEqual(self.viewer.findChildren(QVideoWidget), [], "žádný nativní povrch (airspace)")
        self.assertIs(self.viewer.player.videoOutput(), self.viewer.canvas.video_output)

    def _first_frame(self):
        """Patch 9: video se odkryje s PRVNÍM PLATNÝM SNÍMKEM ze sinku (dřív odhad podle
        pozice >= 55 ms, kvůli kterému se mezi videi ukazovala černá)."""
        from PyQt6.QtMultimedia import QVideoFrame, QVideoFrameFormat
        from PyQt6.QtCore import QSize
        frame = QVideoFrame(QVideoFrameFormat(QSize(8, 8), QVideoFrameFormat.PixelFormat.Format_BGRA8888))
        self.viewer.on_position_changed(80)        # samotná pozice už video neodkrývá
        self.assertFalse(self.viewer.canvas.is_video_shown())
        self.viewer._on_video_frame(frame)
        wait(80)

    def test_video_shows_poster_until_first_frame(self):
        self.open("video")
        self.assertFalse(self.viewer.canvas.is_video_shown(), "plakát do 1. snímku")
        self.assertFalse(self.viewer.canvas.current_pixmap().isNull())
        self._first_frame()
        self.assertTrue(self.viewer.canvas.is_video_shown())
        self.viewer.cleanup_playback()
        self.assertFalse(self.viewer.canvas.is_video_shown())

    def test_live_photo_returns_to_still(self):
        self.open("live")
        self.viewer.toggle_live_playback()
        self._first_frame()
        self.assertTrue(self.viewer.canvas.is_video_shown())
        self.viewer.stop_live_smoothly()
        self.assertFalse(self.viewer.canvas.is_video_shown())


# ═════════════════════════════════════════════════════════════════════════════
# Skleněný HUD + automatické skrývání
# ═════════════════════════════════════════════════════════════════════════════

class TestGlassHud(ViewerEnv):

    def test_hud_panels_float_over_canvas(self):
        v = self.viewer
        for panel in (v.top_hud, v.scrubber_panel, v.prev_hud, v.next_hud):
            self.assertIsInstance(panel, viewer_hud.GlassPanel)
            self.assertIs(panel.parentWidget(), v.canvas, "překryv nad plátnem, ne pruh v layoutu")
        self.assertGreater(v.top_hud.radius, 0)
        self.assertGreater(viewer_hud.GlassPanel.SHADOW, 0, "měkký stín")

    def test_frosted_backdrop_is_a_blurred_copy(self):
        self.open("p0")
        self.viewer.hud.poke()
        wait(300)
        self.viewer.top_hud.refresh_backdrop()
        self.assertTrue(self.viewer.top_hud.has_backdrop(), "sklo má rozostřený výřez obrazu pod sebou")

    def test_required_controls(self):
        v = self.viewer
        for name in ("btn_play_pause", "timeline_slider", "lbl_time_cur", "lbl_time_total", "btn_mute",
                     "volume_slider", "btn_live", "btn_fullscreen", "btn_video_fullscreen",
                     "btn_slideshow", "btn_info", "btn_fav", "btn_back"):
            self.assertTrue(hasattr(v, name), name)
        self.assertIn(v.timeline_slider, v.scrubber_panel.findChildren(type(v.timeline_slider)))

    def test_photo_vs_video_panel_sets(self):
        self.open("p1")
        self.assertNotIn(self.viewer.scrubber_panel, self.viewer._wanted_panels())
        self.assertIn(self.viewer.prev_hud, self.viewer._wanted_panels())
        self.open("video")
        self.assertIn(self.viewer.scrubber_panel, self.viewer._wanted_panels())
        self.open("p0")
        self.assertNotIn(self.viewer.prev_hud, self.viewer._wanted_panels(), "první položka nemá Předchozí")

    def test_icons_follow_state(self):
        self.open("p0")
        self.viewer.handle_toggle_favorite()
        self.assertEqual(self.viewer.btn_fav.icon_name(), "heart_fill")
        self.open("video")
        self.viewer.toggle_mute()
        self.assertEqual(self.viewer.btn_mute.icon_name(), "mute")


class TestAutoHide(ViewerEnv):

    def setUp(self):
        super().setUp()
        self.open("video")
        self.hud = self.viewer.hud
        self.hud.timer.setInterval(80)

    def test_default_delay_is_two_seconds(self):
        self.assertEqual(viewer_hud.HUD_HIDE_DELAY_MS, 2000)
        fresh = viewer_hud.HudAutoHider(self.viewer.canvas.viewport(), lambda: [])
        self.assertEqual(fresh.timer.interval(), 2000)

    def test_hides_hud_and_cursor_after_inactivity(self):
        # Patch 10: během čekání ignorovat vstup z OS – v plné sadě (330 testů) se pod skutečným
        # kurzorem otvírají a zavírají okna a jejich Enter / MouseMove jsou oprávněně „aktivita“.
        import time, traceback
        calls = []
        real_poke = self.hud.poke
        with mock.patch.object(self.hud, "is_interacting", return_value=False):
            self.hud.poke()
            self.assertTrue(self.viewer.top_hud.is_shown())
            t0 = time.perf_counter()
            stamp = lambda: round((time.perf_counter() - t0) * 1000)
            on_timeout = lambda: calls.append(("timeout", stamp(), self.hud.enabled, self.hud.timer.interval()))
            self.hud.timer.timeout.connect(on_timeout)
            with mock.patch.object(self.hud, "eventFilter", return_value=False), \
                    mock.patch.object(self.hud, "poke", side_effect=lambda: (
                        calls.append(("poke", stamp(), "".join(traceback.format_stack(limit=6)[:-1]))),
                        real_poke())):
                wait(600)
            self.hud.timer.timeout.disconnect(on_timeout)
            calls.append(("end", stamp(), self.hud.timer.isActive(), self.hud.timer.interval()))
        self.assertFalse(self.hud.visible, f"diagnostika: {calls}")
        self.assertFalse(self.viewer.top_hud.isVisible())
        self.assertFalse(self.viewer.scrubber_panel.isVisible())
        self.assertEqual(self.viewer.canvas.viewport().cursor().shape(), Qt.CursorShape.BlankCursor)

    def test_stays_while_hovered_or_dragging(self):
        with mock.patch.object(self.hud, "is_interacting", return_value=True):
            self.hud.poke()
            wait(400)
        self.assertTrue(self.hud.visible)
        self.assertTrue(self.viewer.scrubber_panel.is_shown())

    def test_slider_drag_counts_as_interaction(self):
        self.hud.poke()
        wait(50)
        self.viewer.timeline_slider.setSliderDown(True)
        try:
            self.assertTrue(self.hud.is_interacting())
        finally:
            self.viewer.timeline_slider.setSliderDown(False)

    def test_mouse_move_brings_hud_back(self):
        with mock.patch.object(self.hud, "is_interacting", return_value=False):
            self.hud.hide_all()
            wait(400)
            vp = self.viewer.canvas.viewport()
            ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(200, 200), QPointF(vp.mapToGlobal(vp.rect().center())),
                             Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(vp, ev)
            _app.processEvents()
        self.assertTrue(self.hud.visible)
        self.assertTrue(self.viewer.top_hud.is_shown())
        self.assertNotEqual(vp.cursor().shape(), Qt.CursorShape.BlankCursor)

    def test_keys_work_with_hidden_hud(self):
        """Dřív klávesy videa závisely na scrubber_panel.isVisible() – se skrytým HUD by nefungovaly."""
        self.hud.hide_all()
        wait(400)
        with mock.patch.object(self.viewer, "skip_seconds") as skip:
            press(self.viewer, Qt.Key.Key_L)
        skip.assert_called_once_with(10000)
        self.open("live")
        self.hud.hide_all()
        wait(400)
        press(self.viewer, Qt.Key.Key_Space)
        self.assertTrue(self.viewer.is_live_playing)

    def test_leaving_viewer_restores_cursor(self):
        self.hud.hide_all()
        self.viewer.hide()
        self.assertNotEqual(self.viewer.canvas.viewport().cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertFalse(self.hud.timer.isActive())


# ═════════════════════════════════════════════════════════════════════════════
# Prezentace
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSlideshowView(TempEnv):

    def setUp(self):
        super().setUp()
        self.ids = self._library()
        self.records = [r for r in self._records()]
        self.view = viewer_hud.SlideshowView(self.records, 0, interval_ms=5000, fade_ms=120)
        self.view.resize(900, 600)
        self.view.show()
        self.view.start()
        _app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.view.stop()
        self.view.deleteLater()
        _app.processEvents()

    def test_clean_black_presentation(self):
        self.assertEqual(self.view.findChildren(viewer_hud.GlassPanel), [], "žádné ovládání")
        self.assertEqual(self.view.cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertEqual(self.view.canvas.viewport().cursor().shape(), Qt.CursorShape.BlankCursor)
        self.assertIn("#000000", self.view.styleSheet())
        self.assertFalse(self.view.canvas.current_pixmap().isNull())
        self.assertTrue(self.view.timer.isActive())

    def test_auto_advance_crossfades(self):
        self.view.timer.timeout.emit()
        self.assertEqual(self.view.index, 1)
        self.assertTrue(self.view.canvas.is_fading())
        wait(400)
        self.assertFalse(self.view.canvas.is_fading())

    def test_space_pauses_and_resumes(self):
        press(self.view, Qt.Key.Key_Space)
        self.assertTrue(self.view.paused)
        self.assertFalse(self.view.timer.isActive())
        press(self.view, Qt.Key.Key_Space)
        self.assertFalse(self.view.paused)
        self.assertTrue(self.view.timer.isActive())

    def test_arrows_navigate_and_wrap(self):
        press(self.view, Qt.Key.Key_Right)
        self.assertEqual(self.view.index, 1)
        self.assertGreater(self.view.timer.remainingTime(), 4000, "ruční posun = plný interval")
        press(self.view, Qt.Key.Key_Left)
        press(self.view, Qt.Key.Key_Left)
        self.assertEqual(self.view.index, len(self.records) - 1, "zleva na konec (smyčka)")

    def test_paused_manual_navigation_stays_paused(self):
        press(self.view, Qt.Key.Key_Space)
        press(self.view, Qt.Key.Key_Right)
        self.assertFalse(self.view.timer.isActive())

    def test_escape_exits_cleanly(self):
        exits = []
        self.view.back_requested.connect(lambda: exits.append(1))
        press(self.view, Qt.Key.Key_Escape)
        self.assertEqual(exits, [1])
        self.assertFalse(self.view.running)
        self.assertFalse(self.view.timer.isActive())

    def test_next_slide_is_prefetched(self):
        self.view.pool.waitForDone(5000)
        _app.processEvents()
        self.assertIn(1, self.view._cache, "další snímek je načtený předem (bez zaseknutí při přechodu)")

    def test_video_uses_thumbnail_and_exif_orientation(self):
        video = next(r for r in self.records if r[0] == self.ids["video"])
        img = viewer_hud.load_slide_image(video)
        self.assertEqual(QColor(img.pixel(1, 1)).red(), 10, "náhled videa, ne neplatný soubor")
        rotated = self._jpg("rot.jpg", size=(60, 40), exif_orientation=6)
        rec = (0, "rot.jpg", rotated, "photo", None, None, None, 0)
        img = viewer_hud.load_slide_image(rec)
        self.assertEqual((img.width(), img.height()), (40, 60), "orientace z EXIFu")
        big = viewer_hud.load_slide_image(rec, QSize(20, 20))
        self.assertLessEqual(max(big.width(), big.height()), 20)


# ═════════════════════════════════════════════════════════════════════════════
# Hlavní okno: celá obrazovka a prezentace
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestWindowIntegration(TempEnv):

    def setUp(self):
        super().setUp()
        self.ids = self._library()
        patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = main.MainWindow()
        self.w.resize(1280, 820)
        self.w.show()
        _app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.w.nav.reset()
        self.w.close()
        self.w.deleteLater()
        _app.processEvents()

    def _row(self, key):
        return next(i for i, r in enumerate(self.w._filtered_records) if r[0] == self.ids[key])

    def test_viewer_fullscreen_hides_sidebar_and_escape_restores(self):
        self.w._on_photo_activated(self._row("p1"))
        v = self.w._viewer
        v.btn_fullscreen.click()
        wait(300)
        self.assertTrue(self.w.isFullScreen())
        self.assertFalse(self.w.sidebar.isVisible())
        self.assertEqual(v.btn_fullscreen.icon_name(), "fullscreen_exit")
        press(v, Qt.Key.Key_Escape)
        wait(300)
        self.assertFalse(self.w.isFullScreen())
        self.assertTrue(self.w.sidebar.isVisible())
        self.assertIs(self.w.stack.currentWidget(), v, "první Esc jen opustí celou obrazovku")

    def test_f11_and_leaving_viewer_exit_fullscreen(self):
        self.w._on_photo_activated(self._row("p1"))
        v = self.w._viewer
        press(v, Qt.Key.Key_F11)
        self.assertTrue(self.w.is_immersive())
        v.request_close()
        wait(300)
        self.assertFalse(self.w.is_immersive())
        self.assertFalse(self.w.isFullScreen())

    def test_slideshow_from_grid_with_s(self):
        grid = self.w.grid_view
        grid.setCurrentIndex(grid.photo_model.index(2, 0))
        grid.setFocus()
        press(grid, Qt.Key.Key_S)
        view = self.w.stack.currentWidget()
        self.assertIsInstance(view, viewer_hud.SlideshowView)
        self.assertEqual(view.index, 2, "od vybrané fotky")
        wait(300)
        self.assertTrue(self.w.isFullScreen())
        self.assertFalse(self.w.sidebar.isVisible())
        press(view, Qt.Key.Key_Right)
        press(view, Qt.Key.Key_Escape)
        wait(300)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_photos)
        self.assertFalse(self.w.isFullScreen())
        self.assertTrue(self.w.sidebar.isVisible())
        self.assertEqual(grid.currentIndex().row(), 3, "v mřížce označen poslední snímek")

    def test_slideshow_from_viewer_returns_to_same_slide(self):
        self.w._on_photo_activated(0)
        v = self.w._viewer
        v.btn_slideshow.click()
        view = self.w.stack.currentWidget()
        self.assertIsInstance(view, viewer_hud.SlideshowView)
        self.assertEqual(self.w.nav.depth(), 2)
        press(view, Qt.Key.Key_Right)
        press(view, Qt.Key.Key_Right)
        press(view, Qt.Key.Key_Escape)
        wait(200)
        self.assertIs(self.w.stack.currentWidget(), v)
        self.assertEqual(v.current_index, 2)
        self.assertFalse(self.w.is_immersive())

    def test_slideshow_buttons_exist(self):
        self.assertTrue(self.w.btn_slideshow.isVisible())
        ev = self.x("INSERT INTO events (title, start_date, end_date) VALUES ('Výlet', '2026-07-01', '2026-07-03')")
        self.x("UPDATE media_items SET event_id = ?", (ev,))
        self.w._load_events()
        self.w._open_event(ev)                      # Patch 10: Bento karta místo položky seznamu
        page = self.w.stack.currentWidget()
        buttons = [b.text() for b in page.findChildren(main.QPushButton)]
        self.assertTrue(any("Prezentace" in t for t in buttons), buttons)

    def test_sidebar_navigation_during_slideshow_cleans_up(self):
        view = self.w._start_slideshow(self.w._filtered_records, 0)
        self.w._switch_page(1)
        wait(200)
        self.assertFalse(view.running)
        self.assertFalse(self.w.is_immersive())
        self.assertTrue(self.w.sidebar.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)
