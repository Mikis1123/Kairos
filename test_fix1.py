"""Fix 1B + 1C – minimapa prohlížeče na MapLibre a návrat z Cesty na událost.

Spuštění (ve složce projektu):   python -m pytest test_fix1.py -v

Pokrývá:
  (mini_map.html / viewer minimap tests moved to test_sprint3_map.py: the minimap is native now)
  main.py       – Cesta z detailu / přehledu událostí se vloží nad ně; konec Cesty, Zpět i
                  postranní panel vrátí zpět (Cesta skončí tiše); Cesta spuštěná v mapě nikam nevrací.
"""
import json
import os
import sys
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QUrl
    from PyQt6.QtWidgets import QApplication, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from PyQt6 import sip
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
    import main
    import viewer
    from test_patch10 import TempEnv
    from test_patch11_maps import FIXTURE_STYLE, _offline_interceptor, _js, wait

HERE = os.path.dirname(os.path.abspath(__file__))


def _until(pred, tries=80, step=100):
    for _ in range(tries):
        if pred():
            return True
        wait(step)
    return False


def _offline_profile():
    profile = QWebEngineProfile()                    # off-the-record: prázdné localStorage
    profile._offline = _offline_interceptor()
    profile.setUrlRequestInterceptor(profile._offline)
    return profile


# Sprint 3: the viewer's mini-map is native (mini_map.py) – its tests live in test_sprint3_map.py


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestJourneyReturn(unittest.TestCase):

    def setUp(self):
        from test_patch9 import TempEnv as Env9
        self.env = Env9("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config")):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w._ensure_map_view()                 # pillar 5: the map host is built lazily
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        self.w._map_loaded = True
        self.calls = []
        p = mock.patch.object(self.w.map_view.page(), "runJavaScript",
                              side_effect=lambda code, *a: self.calls.append(code))
        p.start()
        self.addCleanup(p.stop)
        self.ev = self.env.x("INSERT INTO events (title, start_date, end_date) "
                             "VALUES ('Výlet', '2026-07-01', '2026-07-03')")
        for i in range(2):
            self.env.x("INSERT INTO media_items (file_path, file_name, media_type, date_taken, event_id) "
                       "VALUES (?, ?, 'photo', '2026-07-01 10:00:00', ?)", (f"C:/x/p{i}.jpg", f"p{i}.jpg", self.ev))
        with mock.patch.object(main.journey, "list_journeys", return_value=[{"id": self.ev}]):
            self.w._switch_page(2)
        self.data = {"id": self.ev, "stops": [{}, {}]}

    def request(self):
        with mock.patch.object(main.journey, "build_journey", return_value=dict(self.data)):
            self.w._request_journey(self.ev)
            wait(30)

    def open_detail(self):
        self.w._open_event(self.ev)
        detail = self.w.stack.currentWidget()
        self.assertIsInstance(detail, main.DetailPage)
        return detail

    def ended_silently(self):
        return "window.endJourney && window.endJourney(true);" in self.calls

    def test_journey_from_detail_returns_to_detail(self):
        detail = self.open_detail()
        self.request()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)
        self.assertTrue(any("playJourney(" in c for c in self.calls))
        self.assertEqual(self.w.sidebar_nav.current_key(), "map")
        self.assertFalse(sip.isdeleted(detail), "detail události se nezničil")
        self.assertEqual(self.w.nav.depth(), 2)
        main.MapBridge(self.w).journeyClosed()           # ×, Hotovo nebo Esc v mapě
        wait(30)
        self.assertIs(self.w.stack.currentWidget(), detail)
        self.assertEqual(self.w.nav.depth(), 1)
        self.assertEqual(self.w.sidebar_nav.current_key(), "events", "podstránka = kořen pod ní")
        self.assertTrue(self.ended_silently())
        self.assertFalse(self.w._journey_return)

    def test_back_shortcut_returns_and_ends_journey(self):
        detail = self.open_detail()
        self.request()
        self.w._nav_back()                                # Alt+← během přehrávání
        self.assertIs(self.w.stack.currentWidget(), detail)
        self.assertTrue(self.ended_silently(), "Cesta neběží dál na skryté mapě")
        self.calls.clear()
        self.w.journey_closed()                           # hlášení z tichého konce už nic nedělá
        self.assertIs(self.w.stack.currentWidget(), detail)

    def test_journey_from_event_card_returns_to_overview(self):
        self.request()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)
        self.w.journey_closed()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_events)
        self.assertEqual(self.w.sidebar_nav.current_key(), "events")

    def test_viewer_over_journey_is_not_closed(self):
        self.open_detail()
        self.request()
        photo = QWidget()                                 # prohlížeč fotky otevřený z Cesty
        self.w.nav.push(photo)
        self.w.journey_closed()
        self.assertIs(self.w.stack.currentWidget(), photo)
        self.w.nav.pop()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)

    def test_sidebar_switch_clears_context(self):
        self.open_detail()
        self.request()
        self.w._switch_page(0)
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertFalse(self.w._journey_return)
        self.assertTrue(self.ended_silently())

    def test_journey_started_in_map_stays_on_map(self):
        self.w._switch_page(3)
        with mock.patch.object(main.journey, "build_journey", return_value=dict(self.data)):
            self.assertTrue(self.w.play_journey(self.ev))
        self.w.journey_closed()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)
        self.assertEqual(self.w.nav.depth(), 0)



@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapPageJourneyClosed(unittest.TestCase):
    """Skutečná stránka: uživatelský konec Cesty se hlásí aplikaci, tichý (Zpět, opakování) ne."""

    @classmethod
    def setUpClass(cls):
        from test_patch11_maps import ShownMapPage
        from test_batch6_map import TRIP
        cls.m, cls.trip = ShownMapPage((900, 600)), TRIP

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code, 5000)

    def test_user_exit_reports_silent_does_not(self):
        fake = self.m.fake
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(self.trip)})"))
        self.assertTrue(self.js("window.endJourney(true)"))
        wait(200)
        self.assertEqual(fake.journeys_closed, 0, "tichý konec se nehlásí")
        self.assertTrue(self.js(f"playJourney({json.dumps(self.trip)})"))
        self.js("document.getElementById('jclose').click(); 0")
        self.assertTrue(_until(lambda: fake.journeys_closed == 1, 30, 50), "× = návrat")
        self.assertFalse(self.js("journeyState().active"))


if __name__ == "__main__":
    unittest.main()
