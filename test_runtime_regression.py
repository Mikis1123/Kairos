"""Runtime regression 2026-11-04: WebView2 controller disposed on first photo open; Live Photo judder.

1. Qt 6 recreates a top-level window (and every native child HWND) the first time an RHI widget –
   the viewer's video QQuickWidget – joins it (Raster -> Direct3D surface). WebView2 disposes a
   controller whose parent HWND dies: "[Mapa] bounds: CoreWebView2Controller members cannot be
   accessed after the WebView2 control is disposed", blank Map / Journey, the window flashing as if
   the app restarted. Fix: MainWindow reserves the RHI surface before it is first shown
   (_reserve_rhi_surface); map_host guards every controller access and re-creates a dead controller.
2. Qt 6.11's FFmpeg backend holds video back to the audio clock (~12 %): Live motion played at
   ~27 fps with uneven frames. Fix: the motion plays without its audio track, the sound comes from
   an audio-only companion player (media_engine track switches, viewer._live_audio).
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from PyQt6 import QtWebEngineWidgets  # noqa: F401 – before QApplication (import main)
from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtGui import QSurface
from PyQt6.QtWidgets import QApplication, QStackedWidget, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import database  # noqa: E402
import main  # noqa: E402
import map_host  # noqa: E402
import media_engine  # noqa: E402


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(cond, timeout_s=20.0):
    t0 = time.time()
    while not cond() and time.time() - t0 < timeout_s:
        wait(30)
    return cond()


def controller_alive(host):
    try:
        return host._ctl is not None and host._ctl.Bounds is not None
    except Exception:
        return False


DISPOSED_TEXT = ("CoreWebView2Controller members cannot be accessed after the WebView2 control is "
                 "disposed. ---> System.Runtime.InteropServices.COMException: (0x8007139F)")


# ── map_host guards (no real controller) ─────────────────────────────────────

class _FakeCtl:
    def __init__(self, fail=None):
        self.fail = fail
        self.sets = []

    def __setattr__(self, name, value):
        if name in ("fail", "sets"):
            object.__setattr__(self, name, value)
            return
        if self.fail is not None:
            raise self.fail
        self.sets.append((name, value))

    def NotifyParentWindowPositionChanged(self):
        if self.fail is not None:
            raise self.fail


class TestHostGuards(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(map_host.WebView2Host, "_create", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host = map_host.WebView2Host(tag="Test")
        self.addCleanup(self._close)

    def _close(self):
        self.host._ctl = None
        self.host.close_webview()
        self.host.deleteLater()
        app.processEvents()

    def test_disposed_error_is_recognised(self):
        self.assertTrue(map_host.WebView2Host.is_disposed_error(Exception(DISPOSED_TEXT)))
        self.assertTrue(map_host.WebView2Host.is_disposed_error(Exception("HRESULT 0x8007139F")))
        self.assertFalse(map_host.WebView2Host.is_disposed_error(Exception("access denied")))

    def test_disposed_bounds_recovers_instead_of_failing(self):
        if not map_host.available():
            self.skipTest("pythonnet / WebView2 SDK not available")
        self.host._ctl = _FakeCtl(fail=Exception(DISPOSED_TEXT))
        with mock.patch.object(self.host, "_recover") as recover:
            self.host._sync_bounds()
            self.host._sync_visible()
            self.host.window_moved()
        self.assertTrue(recover.called)

    def test_closed_host_leaves_the_controller_alone(self):
        ctl = _FakeCtl()
        self.host._ctl = ctl
        self.host._closed = True
        self.host._sync_bounds()
        self.host._sync_visible()
        self.host.window_moved()
        self.assertEqual(ctl.sets, [])

    def test_recover_drops_controller_and_requests_a_new_one(self):
        self.host._ctl = _FakeCtl()
        self.host._env = object()
        self.host._navigated = True
        with mock.patch.object(self.host, "_request_controller") as req:
            self.host._recover("test")
            self.host._recover("again")              # one recovery at a time
            wait(20)
        self.assertIsNone(self.host._ctl)
        self.assertFalse(self.host._navigated)       # the page is navigated again on the new one
        self.assertEqual(self.host.recoveries, 1)
        self.assertEqual(req.call_count, 1)

    def test_closed_host_never_recovers(self):
        self.host.close_webview()
        self.host._recover("late")
        self.assertEqual(self.host.recoveries, 0)


# ── real WebView2: window surface switch ─────────────────────────────────────

@unittest.skipUnless(map_host.available(), "WebView2 runtime / Windows platform not available")
class TestControllerSurvivesSurfaceSwitch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos_rt_wv2_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _window(self, anchor):
        win = QWidget()
        win.resize(640, 420)
        if anchor:                                   # what MainWindow._reserve_rhi_surface does
            from PyQt6.QtQuickWidgets import QQuickWidget
            a = QQuickWidget(win)
            a.setGeometry(0, 0, 1, 1)
            a.hide()
        lay = QVBoxLayout(win)
        lay.setContentsMargins(0, 0, 0, 0)
        stack = QStackedWidget()
        lay.addWidget(stack)
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(0, 0, 0, 0)
        host = map_host.WebView2Host(user_data_dir=os.path.join(self.tmp, "udf"), tag="Test")
        pl.addWidget(host)
        stack.addWidget(QWidget())                   # the "grid"
        stack.addWidget(page)
        loaded = []
        host.loadFinished.connect(loaded.append)
        html = os.path.join(self.tmp, "p.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write("<html><body style='background:#234'><script>window.MARK = 41;</script></body></html>")
        host.load(map_host.file_url(html))
        win.show()
        self.assertTrue(wait_for(lambda: loaded and loaded[-1]), "page never loaded")

        def cleanup():
            host.close_webview()
            win.close()
            win.deleteLater()
            app.processEvents()
        self.addCleanup(cleanup)
        return win, stack, host, loaded

    def _add_quick_page(self, stack):
        """The first photo open: a page with a QQuickWidget joins the window."""
        from PyQt6.QtQuickWidgets import QQuickWidget
        viewer = QWidget()
        QVBoxLayout(viewer).addWidget(QQuickWidget())
        stack.addWidget(viewer)
        stack.setCurrentWidget(viewer)
        wait(300)

    def _js(self, host, code):
        out = []
        host.run_js(code, out.append)
        wait_for(lambda: out, 10)
        return out[0] if out else None

    def test_without_anchor_the_controller_is_recreated(self):
        win, stack, host, loaded = self._window(anchor=False)
        self.assertEqual(win.windowHandle().surfaceType(), QSurface.SurfaceType.RasterSurface)
        self._add_quick_page(stack)
        self.assertEqual(win.windowHandle().surfaceType(), QSurface.SurfaceType.Direct3DSurface)
        stack.setCurrentIndex(1)                     # back to the map
        self.assertTrue(wait_for(lambda: controller_alive(host) and len(loaded) >= 2),
                        "controller not re-created after the window was rebuilt")
        self.assertEqual(host.recoveries, 1)
        self.assertEqual(self._js(host, "window.MARK + 1"), 42)

    def test_anchor_keeps_the_original_controller(self):
        win, stack, host, loaded = self._window(anchor=True)
        self.assertEqual(win.windowHandle().surfaceType(), QSurface.SurfaceType.Direct3DSurface)
        hwnd, ctl = int(host.winId()), host._ctl
        self._add_quick_page(stack)
        stack.setCurrentIndex(1)
        wait(200)
        self.assertEqual(int(host.winId()), hwnd, "map HWND was re-created")
        self.assertIs(host._ctl, ctl)
        self.assertTrue(controller_alive(host))
        self.assertEqual(host.recoveries, 0)
        self.assertEqual(len(loaded), 1, "no reload needed")
        self.assertEqual(self._js(host, "window.MARK + 1"), 42)


# ── MainWindow end to end ────────────────────────────────────────────────────

class _LibraryEnv(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.tmp = tempfile.mkdtemp(prefix="kairos_rt_main_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(database, "DB_PATH", os.path.join(self.tmp, "photos.db"))
        p.start()
        self.addCleanup(p.stop)
        database.init_db()
        self.photos = []
        for i in range(3):
            path = os.path.join(self.tmp, f"IMG_{i}.jpg")
            Image.new("RGB", (64, 48), (60 * i, 120, 200)).save(path, "JPEG")
            with database.db_transaction() as conn:
                conn.execute("INSERT INTO media_items (file_path, file_name, media_type, date_taken) "
                             "VALUES (?, ?, 'photo', ?)", (path, os.path.basename(path), f"2026-07-0{i + 1} 10:00:00"))
            self.photos.append(path)


@unittest.skipUnless(map_host.available(), "WebView2 runtime / Windows platform not available")
class TestMainWindowPhotoOpen(_LibraryEnv):

    def test_photo_open_keeps_map_controller(self):
        with mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None), \
                mock.patch.object(main.MainWindow, "_smart_import_check", lambda self: None), \
                mock.patch.object(main.MainWindow, "start_deferred_services", lambda self: None):
            w = main.MainWindow()
            try:
                from PyQt6.QtQuickWidgets import QQuickWidget
                self.assertIsInstance(w._rhi_anchor, QQuickWidget)
                self.assertFalse(w._rhi_anchor.isVisible())
                w.show()
                self.assertEqual(w.windowHandle().surfaceType(), QSurface.SurfaceType.Direct3DSurface)
                self.assertTrue(w._ensure_map_view())
                host = w.map_view
                self.assertIsInstance(host, map_host.WebView2Host)
                self.assertTrue(wait_for(lambda: controller_alive(host)), "map controller never came up")
                top, hwnd, ctl = int(w.winId()), int(host.winId()), host._ctl
                records = w._filtered_records or main.fetch_media_records(order_by="m.date_taken, m.id")
                self.assertTrue(records)
                w._open_viewer(records, 0)               # the first photo open
                wait(700)                                # hero open + layout settle
                self.assertEqual(int(w.winId()), top, "the main window was re-created")
                self.assertEqual(int(host.winId()), hwnd, "the map HWND was re-created")
                self.assertIs(host._ctl, ctl)
                self.assertTrue(controller_alive(host))
                self.assertEqual(host.recoveries, 0)
                w._nav_back()
                wait(400)
                w._on_sidebar("map")
                wait(300)
                self.assertTrue(controller_alive(host))
                self.assertTrue(host._ctl.IsVisible, "map not shown after returning to it")
            finally:
                if w._viewer is not None:
                    w._viewer.cleanup_playback()
                w.close()
                w.deleteLater()
                wait(50)


# ── media_engine: per-source track switches ──────────────────────────────────

class _FakePlayer:
    def __init__(self):
        self.status = media_engine._MS.LoadingMedia
        self.audio_track = 0
        self.video_track = 0
        self.calls = []

    def mediaStatus(self):
        return self.status

    def activeAudioTrack(self):
        return self.audio_track

    def activeVideoTrack(self):
        return self.video_track

    def setActiveAudioTrack(self, i):
        self.calls.append(("audio", i))
        self.audio_track = i

    def setActiveVideoTrack(self, i):
        self.calls.append(("video", i))
        self.video_track = i

    def setSource(self, url):
        self.calls.append(("source", url))
        self.status = media_engine._MS.LoadingMedia
        self.audio_track = self.video_track = 0      # what Qt does

    def playbackState(self):
        return media_engine._PS.StoppedState

    def position(self):
        return 0

    def duration(self):
        return 0


class TestTrackSwitches(unittest.TestCase):

    def setUp(self):
        self.core = media_engine._PlayerCore()
        self.core.player = _FakePlayer()

    def test_switch_before_load_is_applied_on_loaded_media(self):
        self.core.run(1, "setTrackEnabled", ("audio", False))
        self.assertEqual(self.core.player.calls, [], "not yet loaded – Qt would overwrite it")
        self.core.player.status = media_engine._MS.LoadedMedia
        self.core._on_status(media_engine._MS.LoadedMedia)
        self.assertEqual(self.core.player.audio_track, -1)
        self.assertEqual(self.core.player.video_track, 0)

    def test_switch_on_loaded_media_applies_at_once(self):
        self.core.player.status = media_engine._MS.LoadedMedia
        self.core.run(1, "setTrackEnabled", ("video", False))
        self.assertEqual(self.core.player.video_track, -1)
        self.core.run(2, "setTrackEnabled", ("video", True))
        self.assertEqual(self.core.player.video_track, 0)

    def test_new_source_restores_all_tracks(self):
        """A Live clip's silent video must not leave the next (normal) video without sound."""
        self.core.player.status = media_engine._MS.LoadedMedia
        self.core.run(1, "setTrackEnabled", ("audio", False))
        self.core.run(2, "setSource", "next.mov")
        self.core.player.status = media_engine._MS.LoadedMedia
        self.core._on_status(media_engine._MS.LoadedMedia)
        self.assertEqual(self.core.player.audio_track, 0)
        self.assertEqual(self.core.tracks_off, set())

    def test_proxy_sends_track_commands(self):
        p = media_engine.ThreadedPlayer()
        try:
            with mock.patch.object(p, "_send") as send:
                p.setAudioTrackEnabled(False)
                p.setVideoTrackEnabled(False)
            send.assert_any_call("setTrackEnabled", ("audio", False), bump=False)
            send.assert_any_call("setTrackEnabled", ("video", False), bump=False)
        finally:
            p.shutdown()
            p.deleteLater()
            app.processEvents()


# ── viewer: Live motion without its audio track + companion sound ────────────

class TestLiveCompanionAudio(unittest.TestCase):

    def setUp(self):
        from PIL import Image
        from viewer import MediaViewerWidget
        self.tmp = tempfile.mkdtemp(prefix="kairos_rt_live_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(database, "DB_PATH", os.path.join(self.tmp, "photos.db"))
        p.start()
        self.addCleanup(p.stop)
        database.init_db()
        still = os.path.join(self.tmp, "IMG_L.jpg")
        Image.new("RGB", (64, 48), (200, 80, 40)).save(still, "JPEG")
        self.motion = os.path.join(self.tmp, "IMG_L.MOV")
        with open(self.motion, "wb") as f:
            f.write(b"\x00" * 128)
        with database.db_transaction() as conn:
            conn.execute("INSERT INTO media_items (file_path, file_name, media_type, paired_video_path, date_taken) "
                         "VALUES (?, ?, 'live_photo', ?, '2026-07-05 10:00:00')", (still, "IMG_L.jpg", self.motion))
        self.records = main.fetch_media_records(order_by="m.date_taken, m.id")
        self.viewer = MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        app.processEvents()

    def _open_live(self):
        with mock.patch.object(media_engine.ThreadedPlayer, "setAudioTrackEnabled") as audio_switch:
            self.viewer.set_media_data(list(self.records), 0)
            self.viewer._start_media()
            app.processEvents()
        return audio_switch

    def test_live_motion_plays_without_its_audio_track(self):
        audio_switch = self._open_live()
        audio_switch.assert_called_with(False)
        companion = self.viewer._live_audio
        self.assertEqual(os.path.normcase(companion.source().toLocalFile()), os.path.normcase(self.motion))

    def test_companion_follows_play_stop_and_level(self):
        self._open_live()
        companion = self.viewer._live_audio
        with mock.patch.object(companion, "play") as play, mock.patch.object(companion, "pause") as pause, \
                mock.patch.object(companion, "playbackState",
                                  return_value=media_engine._PS.PlayingState):
            self.viewer.audio_output.setMuted(True)
            self.viewer.toggle_live_playback()
            play.assert_called_once()
            self.assertTrue(companion.audio.isMuted(), "the viewer's mute applies to Live sound")
            self.viewer.stop_live_smoothly(natural=True)        # motion ended: sound rings out
            pause.assert_not_called()
            self.viewer.toggle_live_playback()
            self.viewer.toggle_live_playback()                  # the user stops it
            pause.assert_called_once()

    def test_leaving_the_item_silences_live_sound(self):
        self._open_live()
        companion = self.viewer._live_audio
        with mock.patch.object(companion, "stop") as stop:
            self.viewer._halt_playback()
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
