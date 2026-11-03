"""Gate 2: the map in WebView2 (map_host.py) – bridge, allowlist, airspace, keys, pacing, LOD."""
import json
import sys
import os
import re
import tempfile
import time
import unittest
from unittest import mock

from PyQt6 import QtWebEngineWidgets  # noqa: F401 – before QApplication (import main)
from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

app = QApplication.instance() or QApplication(sys.argv)

import main  # noqa: E402
import map_host  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


class TestAllowlistCsp(unittest.TestCase):
    def test_csp_hosts_match_allowed_web_hosts(self):
        html = read("map_vault.html")
        m = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
        self.assertIsNotNone(m, "map_vault.html carries the network allowlist as a CSP")
        csp = dict((d.strip().split(" ", 1) + [""])[:2] for d in m.group(1).split(";") if d.strip())
        for directive in ("img-src", "connect-src"):
            hosts = set(re.findall(r"https://([^\s;]+)", csp[directive]))
            self.assertEqual(hosts, set(main.ALLOWED_WEB_HOSTS), directive)
            self.assertIn("http://127.0.0.1:*", csp[directive], "tile store (localhost) stays reachable")
        self.assertEqual(csp["default-src"], "'none'")
        self.assertNotIn("https:", csp["script-src"], "no remote scripts")

    def test_bridge_picks_webview2_or_qwebchannel(self):
        html = read("map_vault.html")
        self.assertIn("if (window.__kairosBridge) {", html)
        self.assertIn("new QWebChannel(qt.webChannelTransport", html)


class TestMessageBridge(unittest.TestCase):
    def test_slot_names_are_the_nine_bridge_methods(self):
        self.assertEqual(map_host.bridge_slot_names(main.MapBridge),
                         sorted(["openPhotoFromMap", "setInteraction", "setMapLayer", "openPhotosFromMap",
                                 "requestJourney", "routeJourney", "routeJourneyDay", "journeyClosed",
                                 "setJourneyMusic"]))
        self.assertNotIn("release_all", map_host.bridge_slot_names(main.MapBridge), "not callable from the page")

    def test_dispatch(self):
        target = mock.MagicMock()
        b = map_host.WebMessageBridge(target, ["openPhotoFromMap", "setInteraction"])
        self.assertTrue(b.dispatch(json.dumps({"m": "openPhotoFromMap", "a": [42]})))
        target.openPhotoFromMap.assert_called_once_with(42)
        self.assertTrue(b.dispatch('{"m": "setInteraction", "a": ["map", true]}'))
        target.setInteraction.assert_called_once_with("map", True)
        self.assertFalse(b.dispatch('{"m": "release_all", "a": []}'), "only whitelisted names")
        self.assertFalse(b.dispatch("not json"))
        self.assertFalse(b.dispatch('{"m": "openPhotoFromMap", "a": "x"}'))
        target.openPhotoFromMap.side_effect = ValueError("boom")
        self.assertFalse(b.dispatch('{"m": "openPhotoFromMap", "a": [1]}'), "a failing slot never raises")
        self.assertEqual(b.dispatched, 2)

    def test_bridge_js_lists_names_and_drop(self):
        js = map_host.BRIDGE_JS % json.dumps(["a", "b"])
        self.assertIn('["a", "b"]', js)
        self.assertIn("postMessageWithAdditionalObjects('kairos-drop'", js)


class TestKeys(unittest.TestCase):
    def test_vk_mapping(self):
        self.assertEqual(map_host.vk_to_qt_key(0x4B), Qt.Key.Key_K)
        self.assertEqual(map_host.vk_to_qt_key(0x31), Qt.Key.Key_1)
        self.assertEqual(map_host.vk_to_qt_key(0x7B), Qt.Key.Key_F12)
        self.assertEqual(map_host.vk_to_qt_key(0x1B), Qt.Key.Key_Escape)
        self.assertIsNone(map_host.vk_to_qt_key(0xFF))

    def test_page_keeps_plain_keys(self):
        none = Qt.KeyboardModifier.NoModifier
        self.assertFalse(map_host.is_qt_accelerator(Qt.Key.Key_Escape, none), "Esc ends a Journey in the page")
        self.assertFalse(map_host.is_qt_accelerator(Qt.Key.Key_Left, none))
        self.assertTrue(map_host.is_qt_accelerator(Qt.Key.Key_F1, none))
        self.assertTrue(map_host.is_qt_accelerator(Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier))

    def test_find_shortcut(self):
        w = QWidget()
        sc = QShortcut(QKeySequence("Ctrl+Shift+F"), w)
        found = map_host.find_shortcut(w, QKeySequence("Ctrl+Shift+F"))
        self.assertIs(found, sc)
        self.assertIsNone(map_host.find_shortcut(w, QKeySequence("Ctrl+Q")))
        sc.setEnabled(False)
        self.assertIsNone(map_host.find_shortcut(w, QKeySequence("Ctrl+Shift+F")))


class TestUrls(unittest.TestCase):
    def test_same_page_ignores_encoding_case_and_fragment(self):
        a = map_host.file_url(r"C:\Program Files\Kairos\_internal\map_vault.html")
        self.assertIn("Program%20Files", a, "encoded like WebView2 reports it")
        self.assertTrue(map_host.same_page(a, "file:///C:/Program Files/Kairos/_internal/map_vault.html"))
        self.assertTrue(map_host.same_page(a + "#x", "file:///c:/program%20files/kairos/_internal/MAP_VAULT.html"))
        self.assertFalse(map_host.same_page(a, "https://example.com/"))
        self.assertFalse(map_host.same_page(a, map_host.file_url(r"C:\other.html")))

    def test_key_sequence(self):
        seq = map_host.key_sequence(Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(seq, QKeySequence("Ctrl+Shift+F"))
        self.assertTrue(map_host.key_sequence(None, Qt.KeyboardModifier.NoModifier).isEmpty())


class _FakeHost(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.frozen = []

    def freeze(self, on):
        self.frozen.append(bool(on))

    def window_moved(self):
        pass


class TestAirspace(unittest.TestCase):
    def setUp(self):
        self.win = QWidget()
        self.win.resize(800, 600)
        lay = QVBoxLayout(self.win)
        lay.setContentsMargins(0, 0, 0, 0)
        self.top = QLabel("toolbar")
        lay.addWidget(self.top)
        self.host = _FakeHost(self.win)
        lay.addWidget(self.host, 1)
        self.guard = map_host.AirspaceGuard(self.host)
        self.win.show()
        wait(50)
        self.guard.attach()

    def tearDown(self):
        from PyQt6.QtCore import QEvent
        self.guard.detach()
        self.win.close()
        self.win.deleteLater()          # deterministic teardown (sip exit-order crash otherwise)
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    def test_classify(self):
        sheet = QWidget(self.win)
        self.assertEqual(self.guard.classify(sheet), "freeze", "a floating child of the window = modal overlay")
        self.assertIsNone(self.guard.classify(self.top), "laid-out sibling sits beside the map")
        pulse = QWidget(self.win)
        pulse.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.assertIsNone(self.guard.classify(pulse), "decorative pass-through overlays are ignored")
        toast = QWidget(self.win)
        toast.setProperty(map_host.AIRSPACE_PROP, "native")
        self.assertEqual(self.guard.classify(toast), "native")
        inner = QWidget(self.host)
        self.assertIsNone(self.guard.classify(inner), "the map's own children are not overlays")

    def test_modal_overlay_freezes_until_last_one_hides(self):
        a, b = QWidget(self.win), QWidget(self.win)
        for w in (a, b):
            w.setGeometry(self.win.rect())
        a.show()
        b.show()
        wait(20)
        self.assertEqual(self.host.frozen, [True, True])
        a.hide()
        self.assertEqual(self.host.frozen[-1], True, "still one overlay up")
        b.hide()
        self.assertEqual(self.host.frozen[-1], False)

    def test_native_overlay_gets_own_hwnd_with_round_mask(self):
        toast = QWidget(self.win)
        toast.setProperty(map_host.AIRSPACE_PROP, "native")
        toast.setProperty("kairos_airspace_radius", 14)
        toast.setGeometry(100, 400, 300, 48)
        toast.show()
        wait(20)
        self.assertTrue(toast.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        self.assertFalse(toast.mask().isEmpty(), "rounded HWND region")
        self.assertFalse(toast.mask().contains(toast.rect().topLeft()), "corner is cut")
        self.assertEqual(self.host.frozen, [], "a toast never freezes the map")

    def test_toast_and_frame_meter_are_native_overlays(self):
        import ui_toast
        t = ui_toast.Toast("x", parent=self.win)
        self.assertEqual(t.property("kairos_airspace"), "native")
        o = main.FrameStatsOverlay(self.win)
        self.assertEqual(o.property(map_host.AIRSPACE_PROP), "native")


class TestFrameCapPolicy(unittest.TestCase):
    def _fake(self, **cfg):
        return mock.MagicMock(config=cfg)

    def test_cap(self):
        on_batt = mock.MagicMock(on_battery=True)
        on_ac = mock.MagicMock(on_battery=False)
        with mock.patch.object(main.power, "get") as get:
            get.return_value.state.return_value = on_batt
            self.assertEqual(main.MainWindow._map_fps_cap(self._fake()), 60)
            self.assertEqual(main.MainWindow._map_fps_cap(self._fake(battery_policy="full")), 0)
            self.assertEqual(main.MainWindow._map_fps_cap(self._fake(map_fps_cap=90)), 90)
            get.return_value.state.return_value = on_ac
            self.assertEqual(main.MainWindow._map_fps_cap(self._fake()), 0)


class TestPageSource(unittest.TestCase):
    def setUp(self):
        self.html = read("map_vault.html")

    def test_pacing_and_detail_api(self):
        for name in ("window.setFrameCap", "window.getFrameCap", "window.getDetailState",
                     "window.setDetailMax", "function motionDetailTick", "function settleDetail"):
            self.assertIn(name, self.html)
        self.assertIn("motionDetailTick(now);", self.html, "runs from the render handler while moving")
        self.assertIn("settleDetail();", self.html, "full detail back on moveend")
        self.assertIn("settleDetail(true);", self.html, "filmLook starts from full detail")

    def test_rejected_levels_stay_out(self):
        body = self.html[self.html.index("function setDetail(level)"):self.html.index("function detailMoving()")]
        self.assertNotIn("setSourceTileLodParams", body, "LOD swaps mid-motion re-request tiles (measured worse)")
        self.assertNotIn("holdTerrain", body, "terrain must not collapse while moving")

    def test_main_wires_frame_cap(self):
        src = read("main.py")
        self.assertIn('window.setFrameCap && window.setFrameCap({self._map_fps_cap()})', src)


@unittest.skipUnless(map_host.available(), "WebView2 runtime / Windows platform not available")
class TestLiveWebView2(unittest.TestCase):
    """Real WebView2 controller inside a Qt window."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kairos_wv2_test_")
        cls.win = QWidget()
        cls.win.resize(640, 420)
        lay = QVBoxLayout(cls.win)
        lay.setContentsMargins(0, 0, 0, 0)
        cls.host = map_host.WebView2Host(user_data_dir=os.path.join(cls.tmp, "udf"), tag="Test")
        lay.addWidget(cls.host)
        cls.msgs = []
        cls.host.message.connect(cls.msgs.append)
        cls.host.add_init_script(map_host.BRIDGE_JS % json.dumps(["openPhotoFromMap"]))
        cls.host.add_init_script("window.INJECTED = 7;")
        page = os.path.join(cls.tmp, "p.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write("<html><body style='background:#234'><script>"
                    "window.early = window.INJECTED;"
                    "setTimeout(function(){ __kairosBridge.openPhotoFromMap(5); }, 50);"
                    "</script></body></html>")
        cls.loaded = []
        cls.host.loadFinished.connect(cls.loaded.append)
        cls.host.load(map_host.file_url(page))
        cls.win.show()
        t0 = time.time()
        while not cls.loaded and time.time() - t0 < 20:
            wait(50)

    @classmethod
    def tearDownClass(cls):
        cls.host.close_webview()
        cls.win.close()

    def js(self, code):
        box = {}
        self.host.page().runJavaScript(code, lambda v: box.setdefault("v", v))
        t0 = time.time()
        while "v" not in box and time.time() - t0 < 5:
            wait(10)
        return box.get("v")

    def test_load_and_init_scripts(self):
        self.assertEqual(self.loaded, [True])
        self.assertEqual(self.js("window.early"), 7, "init scripts run before page scripts")
        self.assertEqual(self.js("window.__kairosHost"), "webview2")

    def test_run_js_converts_values(self):
        self.assertEqual(self.js("({a: 1, b: [true, 'x']})"), {"a": 1, "b": [True, "x"]})
        self.assertIsNone(self.js("undefined"))

    def test_bridge_message(self):
        t0 = time.time()
        while not self.msgs and time.time() - t0 < 5:
            wait(20)
        self.assertIn('{"m":"openPhotoFromMap","a":[5]}', self.msgs)

    def test_freeze_shows_still_frame(self):
        self.host.freeze(True)
        t0 = time.time()
        while self.host._snapshot is None and time.time() - t0 < 5:
            wait(20)
        self.assertIsNotNone(self.host._snapshot, "still frame captured")
        self.assertFalse(self.host._ctl.IsVisible, "live view hidden under Qt overlays")
        self.host.freeze(False)
        self.assertTrue(self.host._ctl.IsVisible)
        self.assertIsNone(self.host._snapshot)

    def test_page_in_folder_with_spaces_and_diacritics_loads(self):
        folder = os.path.join(self.tmp, "Program Files ž")
        os.makedirs(folder, exist_ok=True)
        page = os.path.join(folder, "m.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write("<html><body><script>window.OK = 1;</script></body></html>")
        host = map_host.WebView2Host(user_data_dir=os.path.join(self.tmp, "udf3"), tag="Test")
        win = QWidget()
        QVBoxLayout(win).addWidget(host)
        done = []
        host.loadFinished.connect(done.append)
        host.load(map_host.QUrl.fromLocalFile(page))
        win.show()
        t0 = time.time()
        while not done and time.time() - t0 < 20:
            wait(50)
        try:
            self.assertEqual(done, [True], "the guard must not cancel the map page's own encoded URL")
        finally:
            host.close_webview()
            win.close()

    def test_keyboard_focus_returns_to_qt(self):
        """Map gets Win32 focus -> Qt focus follows; a Qt field taking focus asks for it back.
        (The real SetFocus only succeeds for the foreground window – checked in the live app.)"""
        from PyQt6.QtWidgets import QLineEdit
        edit = QLineEdit(self.win)
        self.win.layout().addWidget(edit)
        edit.show()
        edit.setFocus()
        wait(50)
        self.host.setFocus()                     # -> MoveFocus into WebView2 -> GotFocus
        t0 = time.time()
        while not self.host._wv_focus and time.time() - t0 < 3:
            wait(20)
        self.assertTrue(self.host._wv_focus, "WebView2 reported GotFocus")
        self.assertTrue(self.host.hasFocus(), "Qt focus follows the map")
        with mock.patch.object(map_host, "reclaim_keyboard_focus") as reclaim:
            edit.setFocus()
            wait(30)
        reclaim.assert_called_once_with(self.win)
        self.assertFalse(self.host._wv_focus)

    def test_navigation_away_is_refused(self):
        self.js("location.href = 'https://example.com/'; 0")
        href, t0 = None, time.time()
        while time.time() - t0 < 3:              # the cancelled navigation settles asynchronously
            wait(200)
            href = self.js("location.href")
            if href:
                break
        self.assertTrue(str(href).startswith("file:"), f"the map page is never replaced ({href})")

    def test_frame_cap_in_map_page(self):
        """The real map page: rAF wrapper paces every other vsync, cancel works."""
        host = map_host.WebView2Host(user_data_dir=os.path.join(self.tmp, "udf2"), tag="Test")
        win = QWidget()
        lay = QVBoxLayout(win)
        lay.addWidget(host)
        done = []
        host.loadFinished.connect(done.append)
        host.load(map_host.file_url(os.path.join(HERE, "map_vault.html")))
        win.resize(400, 300)
        win.show()
        t0 = time.time()
        while not done and time.time() - t0 < 20:
            wait(50)
        box = {}

        def js(code):
            box.pop("v", None)
            host.page().runJavaScript(code, lambda v: box.setdefault("v", v))
            t1 = time.time()
            while "v" not in box and time.time() - t1 < 5:
                wait(10)
            return box.get("v")
        try:
            self.assertEqual(js("setPerfProfile({refresh_hz: 120}); setFrameCap(60)"), 2)
            self.assertEqual(js("getFrameCap()"), {"fps": 60, "div": 2})
            js("""window.__n = 0; window.__c = 0;
                  var id = requestAnimationFrame(function () { window.__c++; }); cancelAnimationFrame(id);
                  (function f() { if (++window.__n < 20) requestAnimationFrame(f); })(); 0""")
            wait(1000)
            self.assertEqual(js("window.__n"), 20, "paced callbacks keep running")
            self.assertEqual(js("window.__c"), 0, "a cancelled callback never runs")
            self.assertEqual(js("setFrameCap(0)"), 1)
            self.assertIn("levelTime", js("getDetailState()"))
        finally:
            host.close_webview()
            win.close()


class TestMainWindowHost(unittest.TestCase):
    def test_backend_override(self):
        with mock.patch.dict(os.environ, {"KAIROS_MAP_HOST": "qtwebengine"}):
            self.assertFalse(map_host.available())

    def test_failure_falls_back_to_qtwebengine(self):
        """WebView2 'failed' -> the map page swaps to QtWebEngine in place."""
        if not map_host.available():
            self.skipTest("WebView2 not available")
        import database
        tmp = tempfile.mkdtemp(prefix="kairos_wv2_main_")
        with mock.patch.object(database, "DB_PATH", os.path.join(tmp, "photos.db")), \
                mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None), \
                mock.patch.object(main.MainWindow, "_smart_import_check", lambda self: None):
            w = main.MainWindow()
            try:
                self.assertEqual(w.map_host_kind, "webview2")
                self.assertIsInstance(w.map_view, map_host.WebView2Host)
                w.map_view.failed.emit("test")
                self.assertEqual(w.map_host_kind, "qtwebengine")
                from PyQt6.QtWebEngineWidgets import QWebEngineView
                self.assertIsInstance(w.map_view, QWebEngineView)
            finally:
                w.close()
                w.deleteLater()
                wait(50)


if __name__ == "__main__":
    unittest.main()
