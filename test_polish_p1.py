"""Kairos Polish P1 – zvýraznění živě do mapy a Cesta přímo z Událostí.

Spuštění (ve složce projektu):   python -m pytest test_polish_p1.py -v

Pokrývá:
  _sync_accent  – změna zvýraznění v Předvolbách pošle setAccent do načtené mapy (bez obnovení);
                  stránka mapy přebarví i běžící trasu a kroužek aktuální zastávky.
  Cesta         – kapsle na kartě události (hit-test, klávesa J, jen události s ≥ 2 místy),
                  tlačítko v hlavičce detailu, _pending_journey pro ještě nenačtenou mapu.
Testy NESAHAJÍ na síť.
"""
import json
import sys
import types
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QPointF, QEvent
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from PyQt6.QtGui import QKeyEvent, QMouseEvent
    import main
    import tokens
    from events_view import EventCard
    from test_batch6_map import TRIP
    from test_patch11_maps import ShownMapPage, wait


def _event(eid, journey=False):
    return {"id": eid, "title": "Šumava", "location": "Kolinec", "start": "2023-07-06 08:00:00",
            "end": "2023-07-08 20:00:00", "count": 12, "cover": None, "journey": journey}


def _release(card, x, y):
    pos = QPointF(x, y)
    ev = QMouseEvent(QEvent.Type.MouseButtonRelease, pos, card.mapToGlobal(pos), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    card.mouseReleaseEvent(ev)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestEventCardJourneyChip(unittest.TestCase):

    def make(self, journey):
        card = EventCard(_event(5, journey))
        card.resize(360, 220)
        self.addCleanup(card.deleteLater)
        hits = {"open": [], "journey": []}
        card.clicked.connect(hits["open"].append)
        card.journey_requested.connect(hits["journey"].append)
        return card, hits

    def test_chip_click_requests_journey_elsewhere_opens_event(self):
        card, hits = self.make(True)
        c = card.journey_rect().center()
        _release(card, c.x(), c.y())
        self.assertEqual(hits, {"open": [], "journey": [5]})
        _release(card, 180, 150)
        self.assertEqual(hits, {"open": [5], "journey": [5]})

    def test_without_journey_chip_area_opens_event(self):
        card, hits = self.make(False)
        c = card.journey_rect().center()
        _release(card, c.x(), c.y())
        self.assertEqual(hits, {"open": [5], "journey": []})
        card.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_J, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(hits["journey"], [], "J bez cesty nic nedělá")

    def test_key_j_requests_journey(self):
        card, hits = self.make(True)
        card.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_J, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(hits["journey"], [5])
        self.assertIn("J", card.toolTip())

    def test_chip_paints_only_on_hover(self):
        card, _ = self.make(True)
        c = card.journey_rect().center().toPoint()
        rest = card.grab().toImage().pixelColor(c)
        card._hover.set(1.0)
        hover = card.grab().toImage().pixelColor(c)
        self.assertNotEqual(rest, hover, "kapsle se vynoří při najetí")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowP1(unittest.TestCase):

    def setUp(self):
        from test_patch9 import TempEnv
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config")):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        self.calls = []
        p = mock.patch.object(self.w.map_view.page(), "runJavaScript",
                              side_effect=lambda code, *a: self.calls.append(code))
        p.start()
        self.addCleanup(p.stop)

    def journey_calls(self):
        return [c for c in self.calls if "playJourney(" in c]

    # ── zvýraznění ──
    def test_settings_accent_change_reaches_map(self):
        self.w._map_loaded = True
        self.addCleanup(tokens.configure, dict(self.w.config))
        dialog = mock.Mock(trash_emptied=False, recluster_requested=False)
        dialog.exec.return_value = 1
        changed = dict(self.w.config, accent="ember", accent_custom="")
        with mock.patch.object(main, "SettingsDialog", return_value=dialog), \
                mock.patch.object(main, "load_config", return_value=changed):
            self.w._open_settings()
        expected = tokens.accent("dark")["accent"]
        self.assertEqual(tokens.accent_key(), "ember")
        self.assertIn(f"window.setAccent && window.setAccent({json.dumps(expected)});", self.calls)

    def test_sync_accent_waits_for_map(self):
        self.w._map_loaded = False
        self.w._sync_accent()
        self.assertEqual(self.calls, [], "nenačtená mapa dostane barvu až v _on_map_loaded")
        self.w._on_map_loaded(True)
        self.assertTrue(any("setAccent(\"#" in c for c in self.calls), self.calls)

    # ── Cesta ──
    def test_pending_journey_plays_once_after_map_load(self):
        self.w._map_loaded = False
        data = {"id": 3, "stops": [{}, {}]}
        with mock.patch.object(main.journey, "build_journey", return_value=data):
            self.assertTrue(self.w.play_journey(3))
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)
        self.assertEqual(self.w._pending_journey, data)
        self.assertEqual(self.journey_calls(), [])
        self.w._on_map_loaded(True)
        self.assertEqual(self.journey_calls(), [f"window.playJourney && window.playJourney({json.dumps(data)});"])
        self.assertIsNone(self.w._pending_journey)
        self.w._on_map_loaded(True)
        self.assertEqual(len(self.journey_calls()), 1, "Cesta se přehraje jen jednou")

    def test_failed_map_load_drops_pending_journey(self):
        self.w._pending_journey = {"id": 1}
        self.w._on_map_loaded(False)
        self.assertIsNone(self.w._pending_journey)

    def test_event_without_journey_shows_toast(self):
        with mock.patch.object(main.journey, "build_journey", return_value=None), \
                mock.patch.object(main, "show_toast") as toast:
            self.w._request_journey(4)
            wait(30)
        toast.assert_called_once()
        self.assertEqual(self.journey_calls(), [])

    def add_event(self):
        ev = self.env.x("INSERT INTO events (title, start_date, end_date) VALUES ('Výlet', '2026-07-01', '2026-07-03')")
        for i in range(2):
            self.env.x("INSERT INTO media_items (file_path, file_name, media_type, date_taken, event_id) "
                       "VALUES (?, ?, 'photo', '2026-07-01 10:00:00', ?)", (f"C:/x/p{i}.jpg", f"p{i}.jpg", ev))
        return ev

    def test_cards_get_journey_flag_and_signal(self):
        ev = self.add_event()
        with mock.patch.object(main.journey, "list_journeys", return_value=[{"id": ev}]):
            self.w._load_events()
        card = self.w.event_cards[0]
        self.assertTrue(card.has_journey())
        with mock.patch.object(self.w, "play_journey", return_value=True) as play:
            card.journey_requested.emit(ev)
            wait(30)
        play.assert_called_once_with(ev, return_here=True)
        with mock.patch.object(main.journey, "list_journeys", return_value=[]):
            self.w._load_events()
        self.assertFalse(self.w.event_cards[0].has_journey())

    def test_detail_header_has_journey_button_only_with_route(self):
        ev = self.add_event()
        for trips, expected in (([{"id": ev}], True), ([], False)):
            with mock.patch.object(main.journey, "list_journeys", return_value=trips):
                self.w._load_events()
            self.w._open_event(ev)
            page = self.w.stack.currentWidget()
            buttons = {b.text(): b for b in page.findChildren(main.QPushButton)}
            self.assertEqual("Cesta" in buttons, expected, list(buttons))
            if expected:
                with mock.patch.object(self.w, "play_journey", return_value=True) as play:
                    buttons["Cesta"].click()
                    wait(30)
                play.assert_called_once_with(ev, return_here=True)
            self.w.nav.reset()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapAccentLive(unittest.TestCase):
    """Skutečná stránka: setAccent přebarví běžící Cestu bez setStyle."""

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((900, 600))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code, 5000)

    def test_running_journey_recolors_in_place(self):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRIP)})"))
        self.addCleanup(lambda: self.js("if (journey.active) window.endJourney(); setAccent('#8B7CF6'); 0"))
        self.assertTrue(self.js("journeyState().route"))
        style_before = self.js("map.getStyle().sources ? Object.keys(map.getStyle().sources).length : 0")
        self.assertTrue(self.js("setAccent('#E0703A')"))
        for layer in ("j-all", "j-done", "j-glow"):
            self.assertEqual(self.js(f"map.getPaintProperty('{layer}', 'line-color')"), "#E0703A", layer)
        stroke = self.js("JSON.stringify(map.getPaintProperty('j-stops', 'circle-stroke-color'))")
        self.assertIn("#E0703A", stroke, "kroužek aktuální zastávky")
        self.assertEqual(self.js("Object.keys(map.getStyle().sources).length"), style_before,
                         "žádné setStyle – zdroje dlaždic zůstaly")


if __name__ == "__main__":
    unittest.main()
