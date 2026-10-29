"""Kairos Batch 6 – mapa: zeměkoule MapLibre 5, terén, klidové otáčení, přelety a Cesta.

Spuštění (ve složce projektu):   python -m pytest test_batch6_map.py -v

Pokrývá:
  journey.py  – zastávky z událostí (časové pořadí, návrat = nová zastávka, slučování na
                MAX_STOPS, jen viditelné fotky, data EXIF), seznam přehratelných cest.
  Most        – requestJourney / play_journey, viditelnost záložky, zvýraznění.
  Stránka     – zeměkoule + atmosféra, terén jen v krajině, otáčení (start/stop/skrytá záložka),
                přelet do shluku s náklonem, návrat na zeměkouli, výběr Cesty, přehrávání:
                kapsle, trasa, obloha podle času, polaroidy -> filmový pás, konec a úklid.
Testy NESAHAJÍ na síť.
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import journey

if HAS_QT:
    import main
    from test_patch11_maps import ShownMapPage, wait


def _p(i, lat, lon, date, city=""):
    return {"id": i, "lat": lat, "lon": lon, "date": date, "city": city, "thumb": ""}


class TestStopGrouping(unittest.TestCase):

    def test_consecutive_photos_form_stops_and_return_is_new_stop(self):
        photos = [_p(1, 49.24, 13.40, "2026-07-06 08:00"), _p(2, 49.241, 13.401, "2026-07-06 08:20"),
                  _p(3, 49.17, 13.58, "2026-07-06 12:00"),
                  _p(4, 49.24, 13.40, "2026-07-06 19:00")]          # návrat do Kolince
        stops = journey.group_stops(photos, 1.5)
        self.assertEqual([[p["id"] for p in s] for s in stops], [[1, 2], [3], [4]])

    def test_pick_spread_covers_whole_stop(self):
        self.assertEqual(journey.pick_spread(list(range(10)), 4), [0, 3, 6, 9])
        self.assertEqual(journey.pick_spread([1, 2], 4), [1, 2])

    def test_too_many_stops_merge_with_larger_radius(self):
        # 60 míst po ~3 km na přímce: s 1,5 km by bylo 60 zastávek, výsledek musí být ≤ MAX_STOPS
        photos = [_p(i, 49.0 + i * 0.027, 13.5, f"2026-07-06 {8 + i // 6:02d}:{i % 6 * 10:02d}") for i in range(60)]
        stops = journey.build_stops(photos)
        self.assertLessEqual(len(stops), journey.MAX_STOPS)
        self.assertGreaterEqual(len(stops), journey.MIN_STOPS)
        self.assertEqual(sum(s["count"] for s in stops), 60, "žádná fotka se neztratí")
        self.assertTrue(all(len(s["photos"]) <= journey.POLAROIDS_PER_STOP for s in stops))

    def test_haversine(self):
        self.assertAlmostEqual(journey.haversine_km((50.0755, 14.4378), (49.1951, 16.6068)), 185, delta=3)


class TestJourneyDb(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_b6_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.conn = database.get_connection()
        self.conn.execute("INSERT INTO events (id, title, location_name, start_date, end_date) VALUES "
                          "(1, 'Šumava', '', '2023-07-06 08:00:00', '2023-07-08 22:00:00'), "
                          "(2, 'Zahrada', '', '2024-05-01 10:00:00', '2024-05-01 12:00:00')")
        self.n = 0

    def tearDown(self):
        self.conn.close()
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, event, date, lat, lon, city="", trashed=0):
        self.n += 1
        self.conn.execute(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, latitude, longitude, "
            "place_city, event_id, thumbnail_path, is_trashed) VALUES (?, ?, 'photo', ?, ?, ?, ?, ?, ?, ?)",
            (f"C:/x/p{self.n}.jpg", f"p{self.n}.jpg", date, lat, lon, city, event, f"C:/t/{self.n}.jpg", trashed))
        self.conn.commit()

    def test_build_journey(self):
        self.add(1, "2023:07:06 08:10:00", 49.247, 13.399, "Kolinec")      # formát EXIF
        self.add(1, "2023-07-06 08:40:00", 49.248, 13.400, "Kolinec")
        self.add(1, "2023-07-06 19:40:00", 49.241, 13.521, "Sušice")
        self.add(1, "2023-07-07 10:00:00", 0, 0)                            # Null Island = bez GPS
        self.add(1, "2023-07-08 21:40:00", 49.167, 13.576, "Kašperské Hory", trashed=1)
        data = journey.build_journey(self.conn, 1, lambda p: "file:///" + p)
        self.assertEqual(data["title"], "Šumava")
        self.assertEqual(data["location"], "Kolinec", "bez názvu místa události: první město")
        self.assertEqual([s["city"] for s in data["stops"]], ["Kolinec", "Sušice"], "koš se nepřehrává")
        self.assertEqual(data["start"], "2023-07-06 08:10", "datum EXIF převedené na ISO")
        self.assertEqual(data["stops"][0]["count"], 2)
        self.assertEqual(data["count"], 3)
        self.assertTrue(data["stops"][0]["photos"][0]["thumb"].startswith("file:///C:/t/"))
        json.dumps(data)                                                    # jde rovnou do JS

    def test_single_place_is_not_a_journey(self):
        self.add(2, "2024-05-01 10:00:00", 50.0, 14.0)
        self.add(2, "2024-05-01 11:00:00", 50.0001, 14.0001)
        self.assertIsNone(journey.build_journey(self.conn, 2))
        self.assertIsNone(journey.build_journey(self.conn, 999))
        self.add(1, "2023-07-06 08:10:00", 49.247, 13.399)
        self.add(1, "2023-07-06 19:40:00", 49.241, 13.521)
        trips = journey.list_journeys(self.conn)
        self.assertEqual([t["id"] for t in trips], [1], "zahrada na jednom místě v nabídce není")
        self.assertEqual(trips[0]["count"], 2)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestJourneyPlumbing(unittest.TestCase):

    def test_bridge_requests_journey(self):
        fake = mock.Mock()
        main.MapBridge(fake).requestJourney(7)
        wait(30)
        fake.play_journey.assert_called_once_with(7)

    def test_play_journey_sends_data_and_switches_to_map(self):
        calls = []
        page = types.SimpleNamespace(runJavaScript=lambda code, *a: calls.append(code))
        page_map = object()
        stack = types.SimpleNamespace(currentWidget=lambda: None)
        win = types.SimpleNamespace(map_view=types.SimpleNamespace(page=lambda: page), _map_loaded=True,
                                    stack=stack, page_map=page_map, _switch_page=mock.Mock())
        data = {"id": 3, "stops": [{}, {}]}
        with mock.patch.object(main, "get_connection"), \
                mock.patch.object(main.journey, "build_journey", return_value=data):
            self.assertTrue(main.MainWindow.play_journey(win, 3))
        win._switch_page.assert_called_once_with(3)
        self.assertEqual(calls, [f"window.playJourney && window.playJourney({json.dumps(data)});"])
        with mock.patch.object(main, "get_connection"), \
                mock.patch.object(main.journey, "build_journey", return_value=None):
            self.assertFalse(main.MainWindow.play_journey(win, 4), "událost bez cesty = nic")

    def test_terrain_host_allowed(self):
        self.assertIn("elevation-tiles-prod.s3.amazonaws.com", main.ALLOWED_WEB_HOSTS)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowMap(unittest.TestCase):
    """Skutečné hlavní okno (dočasná DB): mapa dostane zvýraznění, viditelnost a seznam Cest."""

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

    def test_load_sends_accent_visibility_and_journeys(self):
        calls = []
        with mock.patch.object(self.w.map_view.page(), "runJavaScript", side_effect=lambda code, *a: calls.append(code)):
            self.w._on_map_loaded(True)
            self.assertTrue(any("setAccent(\"#" in c for c in calls), calls)
            self.assertTrue(any("setMapActive(false)" in c for c in calls), "mapa není aktuální stránka")
            self.assertTrue(any("setJourneys([])" in c for c in calls), calls)
            calls.clear()
            self.w._switch_page(3)
            self.assertTrue(any("setMapActive(true)" in c for c in calls), calls)
            calls.clear()
            self.w._switch_page(0)
            self.assertTrue(any("setMapActive(false)" in c for c in calls), calls)


TRIP = {"id": 1, "title": "Šumava <b>2023</b>", "location": "Šumava", "start": "2023-07-06 08:10",
        "end": "2023-07-06 21:50", "count": 5, "stops": [
            {"lat": 49.247, "lon": 13.399, "start": "2023-07-06 12:10", "end": "", "city": "Kolinec", "count": 2,
             "photos": [{"id": 11, "thumb": "", "date": "2023-07-06 12:10"},
                        {"id": 12, "thumb": "", "date": "2023-07-06 12:40"}]},
            {"lat": 49.167, "lon": 13.576, "start": "2023-07-06 23:30", "end": "", "city": "Kašperské Hory", "count": 3,
             "photos": [{"id": 13, "thumb": "", "date": "2023-07-06 23:30"}]}]}


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestGlobePage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1000, 700))
        cls.thumb_dir = tempfile.mkdtemp(prefix="omni_b6_thumb_")
        from PyQt6.QtGui import QImage, QColor
        img = QImage(32, 24, QImage.Format.Format_RGB32)
        img.fill(QColor("#C04020"))
        cls.thumb = os.path.join(cls.thumb_dir, "t.png")
        img.save(cls.thumb)

    @classmethod
    def tearDownClass(cls):
        cls.m.close()
        shutil.rmtree(cls.thumb_dir, ignore_errors=True)

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def setUp(self):
        self.js("if (journey.active) window.endJourney(); reduceMotion = false; mapActive = true; stopSpin(); 0")
        self.m.reset_view((15.5, 49.8), 1.7)

    def wait_idle(self, ms=6000):
        for _ in range(ms // 100):
            if not self.js("map.isMoving()"):
                return
            wait(100)

    def test_globe_projection_and_atmosphere(self):
        self.assertEqual(self.js("map.style.projection.name"), "globe")
        # do roviny dřív, než se zapne terén (terén ani mlha MapLibre na zeměkouli nefungují)
        self.assertEqual(self.js("map.getProjection().type"), self.js("GLOBE_PROJECTION"))
        self.assertLess(self.js("GLOBE_FLAT_AT"), self.js("TERRAIN_MIN_ZOOM"))
        self.assertTrue(self.js("map.style.projection.useGlobeRendering"), "na z1.7 zeměkoule")
        self.assertIsNotNone(self.js("map.getSky()"), "atmosféra / obloha")
        self.assertEqual(self.js("map.getSky()['sky-color']"), "#6FA6EE", "výchozí denní obloha")

    def test_terrain_only_in_landscape(self):
        self.assertFalse(self.js("isTerrainOn()"), "na zeměkouli bez výškových dlaždic")
        self.m.reset_view((13.5, 49.05), 11)
        self.js("map.fire('zoomend'); 0")
        for _ in range(30):                     # skok ze zeměkoule: terén až po překlopení projekce
            if self.js("isTerrainOn()"):
                break
            wait(50)
        self.assertTrue(self.js("isTerrainOn()"))
        self.assertEqual(self.js("map.getTerrain().source"), "dem")
        wait(300)
        self.assertFalse(self.js("map.style.projection.useGlobeRendering"), "terén jen v rovině")
        self.assertLessEqual(self.js("map.getSource('dem').maxzoom"), 14,
                             "DEM ne hlouběji než vektory (cannot calculate elevation)")
        # výměna podkladu se zapnutým terénem: dřív padalo „reading 'shaderPreludeCode'“
        errs = []
        self.js("window.__errs = []; window.addEventListener('error', function (e) { __errs.push(String(e.message)); }); 0")
        self.js("setBaseLayer('outdoor'); 0")
        self.assertTrue(self.m.wait_style())
        self.js("map.fire('zoomend'); 0")
        wait(500)
        errs = self.js("__errs") or []
        self.assertEqual([e for e in errs if "shaderPreludeCode" in e], [], errs)
        self.assertTrue(self.js("isTerrainOn()"), "terén po výměně stylu zpět")
        self.js("setBaseLayer('standard'); 0")
        self.assertTrue(self.m.wait_style())
        self.m.reset_view((15.5, 49.8), 3)
        self.js("map.fire('zoomend'); 0")
        self.assertFalse(self.js("isTerrainOn()"))

    def test_idle_spin_starts_stops_and_respects_hidden_tab(self):
        self.assertTrue(self.js("startSpinNow()"))
        lng0 = self.js("map.getCenter().lng")
        wait(1500)
        self.assertTrue(self.js("isSpinning()"))
        lng1 = self.js("map.getCenter().lng")
        self.assertLess((lng1 - lng0 + 540) % 360 - 180, -0.1, "otáčí se od západu k východu")
        self.js("map.getCanvas().dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); 0")
        wait(100)
        self.assertFalse(self.js("isSpinning()"), "zásah uživatele otáčení zastaví")
        self.js("setMapActive(false); 0")
        self.assertFalse(self.js("startSpinNow()"), "skrytá záložka se neotáčí")
        self.js("setMapActive(true); 0")
        self.m.reset_view((13.5, 49.05), 10)
        self.assertFalse(self.js("startSpinNow()"), "v krajině se neotáčí")
        self.js("reduceMotion = true; 0")
        self.m.reset_view((15.5, 49.8), 1.7)
        self.assertFalse(self.js("startSpinNow()"), "omezený pohyb = bez otáčení")

    def test_idle_spin_rests_after_limit(self):
        """Nečinná mapa nesmí kreslit donekonečna (GPU, readback zeměkoule) – otáčení dobrzdí."""
        self.addCleanup(lambda: self.js("SPIN_MAX_MS = 60000; 0"))
        self.js("SPIN_MAX_MS = 300; 0")
        self.assertTrue(self.js("startSpinNow()"))
        wait(1500)
        self.assertFalse(self.js("isSpinning()"), "po limitu dobrzdí")
        self.js("lastAct = 0; 0")
        wait(1300)
        self.assertFalse(self.js("isSpinning()"), "bez zásahu uživatele se znovu nerozjede")
        self.js("SPIN_MAX_MS = 60000; "
                "map.getCanvas().dispatchEvent(new MouseEvent('mousedown', {bubbles: true})); lastAct = 0; 0")
        wait(1300)
        self.assertTrue(self.js("isSpinning()"), "po zásahu a nečinnosti zase ano")

    def test_cluster_fly_in_tilts_and_pull_back_returns_to_globe(self):
        markers = [{"id": i, "lat": 49.03 + i * 0.002, "lon": 13.49 + i * 0.003, "name": f"IMG_{i}",
                    "city": "Modrava", "date": "", "thumb": ""} for i in range(6)]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.js("render(); 0")
        self.assertEqual(self.js("getClusterCount()"), 1)
        self.js("clickCluster(0); 0")
        wait(300)
        self.assertTrue(self.js("map.isMoving()"), "plynulý přelet, ne skok")
        self.wait_idle()
        self.assertGreater(self.js("map.getZoom()"), 12)
        self.assertGreater(self.js("map.getPitch()"), 40, "v krajině nakloněná kamera")
        self.js("window.pullBack(); 0")
        self.wait_idle()
        self.assertAlmostEqual(self.js("map.getZoom()"), 1.7, places=1)
        self.assertLess(self.js("map.getPitch()"), 1)

    def test_hover_card_is_escaped(self):
        markers = [{"id": 5, "lat": 49.0, "lon": 13.0, "name": "<img src=x onerror=alert(1)>",
                    "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.assertTrue(self.js("hoverCluster(0)"))
        html = self.js("document.getElementById('card').innerHTML")
        self.assertIn("&lt;img", html)
        self.assertEqual(self.js("document.querySelectorAll('#card img').length"), 0)
        self.js("hideCard(); 0")

    def test_journey_picker_requests_journey(self):
        trips = [{"id": 4, "title": "Alpy", "location": "Tyrolsko", "start": "2024-08-01 09:00",
                  "end": "2024-08-05 18:00", "count": 120}]
        self.assertEqual(self.js(f"setJourneys({json.dumps(trips)})"), 1)
        self.assertFalse(self.js("document.getElementById('btn-journeys').hidden"))
        self.js("document.getElementById('btn-journeys').click(); 0")
        self.assertTrue(self.js("document.getElementById('jlist').classList.contains('open')"))
        self.assertIn("1. 8. 2024 – 5. 8. 2024", self.js("document.getElementById('jlist').textContent"))
        self.js("document.querySelector('#jlist button').click(); 0")
        for _ in range(30):
            if self.m.fake.journeys:
                break
            wait(50)
        self.assertEqual(self.m.fake.journeys, [4])
        self.js("setJourneys([]); 0")
        self.assertTrue(self.js("document.getElementById('btn-journeys').hidden"))

    def test_invalid_journey_rejected(self):
        self.assertFalse(self.js("playJourney(null)"))
        self.assertFalse(self.js("playJourney({stops: [{lat: 1, lon: 2}]})"), "jedna zastávka není cesta")
        self.assertFalse(self.js("playJourney({stops: [{lat: 1, lon: 2}, {lat: 99, lon: 2}]})"))
        self.assertFalse(self.js("journeyState().active"))

    def test_sun_and_time_capsule(self):
        noon = self.js("sunPosition(50.08, 14.43, '2023-06-21 13:00')")
        self.assertAlmostEqual(noon["el"], 63, delta=3, msg="poledne v létě v Praze")
        night = self.js("sunPosition(50.08, 14.43, '2023-06-21 23:30')")
        self.assertLess(night["el"], -10)
        dusk = self.js("sunPosition(50.08, 14.43, '2023-12-21 16:30')")
        self.assertTrue(-9 < dusk["el"] < 1, dusk)
        self.assertIsNone(self.js("sunPosition(50, 14, '2023-06-21')"), "bez času = bez oblohy")
        self.assertEqual(self.js("yearsAgo('2023-07-06 10:00', new Date(2026, 9, 28))"), "Před 3 lety")
        self.assertEqual(self.js("yearsAgo('2025-07-06 10:00', new Date(2026, 9, 28))"), "Před rokem")
        self.assertEqual(self.js("yearsAgo('2026-11-06 10:00', new Date(2026, 9, 28))"), "")

    def test_accent(self):
        self.assertTrue(self.js("setAccent('#E0703A')"))
        self.assertFalse(self.js("setAccent('red; background: url(x)')"))
        self.assertEqual(self.js("accent()"), "#E0703A")
        self.js("setAccent('#8B7CF6'); 0")

    def test_journey_playback(self):
        """Celý film zrychleně: titulek -> okamžiky zastávek -> kapitoly -> konec (Cesta 2.0)."""
        trip = json.loads(json.dumps(TRIP))
        url = QUrl.fromLocalFile(self.thumb).toString()
        for s in trip["stops"]:
            for p in s["photos"]:
                p["thumb"] = url
        self.js("FILM_SPEED = 12; 0")
        markers = [{"id": 1, "lat": 49.2, "lon": 13.4, "name": "a", "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.assertTrue(self.js(f"playJourney({json.dumps(trip)})"))
        st = self.js("journeyState()")
        self.assertTrue(st["active"])
        self.assertTrue(st["route"], "trasa na mapě")
        self.assertEqual(self.js("document.getElementById('jtitle').textContent"), "Šumava <b>2023</b>",
                         "název jako text, ne HTML")
        self.assertTrue(self.js("document.getElementById('jcap').classList.contains('show')"))
        self.assertIn("2 zastávky", self.js("document.getElementById('jmeta').textContent"))
        self.assertFalse(self.js("getPinState().visible"), "piny se během Cesty schovají")
        self.assertEqual(self.js("journeyState().frames"), 2, "kapitola za každou zastávku hned od začátku")
        self.js("journeySkip(1); 0")                              # střih rovnou na okamžik první zastávky
        st = self.js("journeyState()")
        self.assertEqual(st["index"], 0)
        self.assertEqual(st["segment"], "dwell")
        self.assertFalse(self.js("document.getElementById('jcap').classList.contains('show')"))
        self.assertEqual(st["place"], "Kolinec")
        self.assertEqual(st["sky"], "#6FA6EE", "poledne = denní obloha")
        self.assertIn("Den", self.js("document.getElementById('jwhen').textContent"))
        # okamžik zastávky: jediný nejlepší snímek (best, jinak první fotka) ve velké kartě
        self.assertEqual(st["polaroids"], 1, "okamžik zastávky je vidět")
        self.assertIn(os.path.basename(self.thumb),
                      self.js("document.querySelector('#jmoment .moment.show .ph').style.backgroundImage"))
        self.assertIn("Kolinec", self.js("document.querySelector('#jmoment .moment.show .place').textContent"))
        self.assertTrue(self.js("document.querySelector('#jstrip .chap').classList.contains('now')"))
        self.m.fake.opened.clear()
        self.js("document.querySelector('#jmoment .moment.show .shot').click(); 0")
        for _ in range(30):                                       # QWebChannel je asynchronní
            if self.m.fake.opened:
                break
            wait(50)
        self.assertEqual(self.m.fake.opened, [11], "snímek okamžiku otevře fotku")
        self.assertTrue(self.js("journeyState().paused"), "otevření fotky film pozastaví")
        self.js("journeyPause(); 0")
        for _ in range(80):
            if self.js("journeyState().index") == 1:
                break
            wait(50)
        st = self.js("journeyState()")
        self.assertEqual(st["index"], 1, "po okamžiku přesun k další zastávce")
        self.assertEqual(st["place"], "Kašperské Hory")
        for _ in range(80):
            if self.js("journeyState().segment") == "dwell":
                break
            wait(50)
        self.assertEqual(self.js("Math.round(skyNow.el) < -10"), True, "23:30 = noc")
        self.assertGreater(self.js("+document.getElementById('tint').style.opacity"), 0.5, "noční tónování")
        for _ in range(120):
            if self.js("journeyState().ended && journeyState().t >= journeyState().total"):
                break
            wait(50)
        st = self.js("journeyState()")
        self.assertTrue(st["ended"])
        self.assertTrue(self.js("document.getElementById('jcap').classList.contains('end')"), "závěrečná karta")
        self.assertEqual(self.js("document.querySelectorAll('#jstrip .chap.seen').length"), 2, "vše viděno")
        self.js("document.querySelectorAll('#jstrip .chap')[1].click(); 0")
        st = self.js("journeyState()")
        self.assertEqual((st["index"], st["segment"], st["ended"]), (1, "dwell", False), "kapitola = skok na zastávku")
        self.assertTrue(self.js("window.endJourney()"))
        st = self.js("journeyState()")
        self.assertFalse(st["active"])
        self.assertFalse(st["route"], "trasa z mapy pryč")
        self.assertTrue(self.js("getPinState().visible"), "piny zpět po konci Cesty")
        for _ in range(60):                                       # obloha se vrací plynule (1,5 s)
            if self.js("+document.getElementById('tint').style.opacity") == 0:
                break
            wait(50)
        self.assertEqual(self.js("+document.getElementById('tint').style.opacity"), 0, "zpět den")

    def test_journey_pause_and_keyboard(self):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRIP)})"))
        self.js("journeySkip(1); 0")
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: ' '})); 0")
        self.assertTrue(self.js("journeyState().paused"))
        self.assertTrue(self.js("document.body.classList.contains('paused')"), "pauza zastaví i Ken Burns")
        self.assertEqual(self.js("document.getElementById('jplay').getAttribute('aria-label')"), "Přehrát")
        t = self.js("journeyState().t")
        wait(200)
        self.assertEqual(self.js("journeyState().t"), t, "čas filmu v pauze stojí")
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight'})); 0")
        st = self.js("journeyState()")
        self.assertEqual(st["index"], 1)
        self.assertFalse(st["paused"])
        self.assertEqual(st["polaroids"], 1, "přeskočená zastávka rovnou ukáže svůj okamžik")
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowLeft'})); 0")
        self.assertEqual(self.js("journeyState().index"), 0)
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); 0")
        self.assertFalse(self.js("journeyState().active"))
        self.js("reduceMotion = false; 0")

    def test_hidden_tab_pauses_journey(self):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRIP)})"))
        self.js("journeySkip(1); setMapActive(false); 0")
        self.assertTrue(self.js("journeyState().paused"), "odchod ze záložky Cestu pozastaví")
        self.js("setMapActive(true); window.endJourney(); 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
