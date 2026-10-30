"""Sprint 4.3 – Atlas: domov, výlety, oblouky po hlavních kružnicích, hexagonová hustota, časová osa
let a přechod do Cesty. JS část ve skutečné stránce mapy (síť blokovaná)."""
import json
import math
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import unittest

import atlas
import database

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage

HOME = (50.08, 14.42)                       # Praha
PLACES = [("Paříž", "Francie", 48.86, 2.35), ("Řím", "Itálie", 41.9, 12.5), ("Tokio", "Japonsko", 35.68, 139.7),
          ("New York", "USA", 40.71, -74.0), ("Reykjavík", "Island", 64.15, -21.94), ("Fidži", "Fidži", -17.7, 178.1),
          ("Šumava", "Česko", 49.0, 13.5), ("Split", "Chorvatsko", 43.5, 16.44)]


def make_world_db(path, photos_per_trip=40, home_photos=600, years=range(2015, 2026), seed=3):
    """Syntetická knihovna: domov s mnoha dny + výlety po světě (pro testy i bench_atlas)."""
    rng = random.Random(seed)
    saved = database.DB_PATH
    database.DB_PATH = path
    try:
        database.init_db()
    finally:
        database.DB_PATH = saved
    conn = sqlite3.connect(path)
    mid = 1
    eid = 1
    for k in range(home_photos):
        y = rng.choice(list(years))
        conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, latitude, longitude, "
                     "place_city, place_country) VALUES (?, ?, ?, 'photo', ?, ?, ?, 'Praha', 'Česko')",
                     (mid, f"C:/h/{mid}.jpg", f"{mid}.jpg", f"{y}:{rng.randint(1, 12):02d}:{rng.randint(1, 28):02d} 12:00:00",
                      HOME[0] + rng.uniform(-0.05, 0.05), HOME[1] + rng.uniform(-0.05, 0.05)))
        mid += 1
    for y in years:
        for city, country, lat, lon in rng.sample(PLACES, 3):
            month = rng.randint(1, 12)
            conn.execute("INSERT INTO events (id, title, location_name, start_date) VALUES (?, ?, ?, ?)",
                         (eid, f"Výlet: {city} ({month:02d}. {y})", city, f"{y}-{month:02d}-01"))
            for k in range(photos_per_trip):
                conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, latitude, longitude, "
                             "place_city, place_country, event_id) VALUES (?, ?, ?, 'photo', ?, ?, ?, ?, ?, ?)",
                             (mid, f"C:/t/{mid}.jpg", f"{mid}.jpg", f"{y}:{month:02d}:{1 + k // 10:02d} {8 + k % 10:02d}:00:00",
                              lat + rng.uniform(-0.2, 0.2), lon + rng.uniform(-0.2, 0.2), city, country, eid))
                mid += 1
            eid += 1
    conn.commit()
    return conn


class TestAtlasData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="atlas_")
        cls.conn = make_world_db(os.path.join(cls.dir, "w.db"))
        cls.data = atlas.build(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.dir, True)

    def test_home_is_most_days_not_most_photos(self):
        self.assertAlmostEqual(self.data["home"]["lat"], HOME[0], delta=0.1)
        self.assertAlmostEqual(self.data["home"]["lon"], HOME[1], delta=0.1)
        rows = [(1, 10.0, 10.0, "2020-01-01 10:00", 1, "", "")] * 900 + \
               [(2, 50.0, 14.0, f"2020-01-{d:02d} 10:00", None, "", "") for d in range(1, 29)]
        h = atlas.find_home(rows)
        self.assertAlmostEqual(h[0], 50.0, msg="28 různých dní > 900 fotek z jednoho dne")

    def test_trips_and_arcs(self):
        trips = self.data["trips"]
        self.assertEqual(len(trips), 33)
        self.assertEqual(trips, sorted(trips, key=lambda t: t["start"]))
        far = [t for t in trips if t["far"]]
        self.assertTrue(all(t["km"] >= atlas.TRIP_MIN_KM for t in far))
        tokyo = next(t for t in trips if t["place"] == "Tokio")
        self.assertGreater(tokyo["km"], 8500)
        self.assertNotIn("(", tokyo["title"], "datum z názvu pryč")
        kinds = {f["properties"]["kind"] for f in self.data["arcs"]["features"]}
        self.assertEqual(kinds, {"home", "thread"})
        self.assertEqual(self.data["stats"]["countries"], 8)
        self.assertEqual(self.data["stats"]["years"], list(range(2015, 2026)))

    def test_great_circle(self):
        pts = atlas.great_circle(HOME, (35.68, 139.7))
        self.assertEqual(len(pts), atlas.ARC_POINTS + 1)
        self.assertAlmostEqual(pts[0][1], HOME[0], places=3)
        self.assertAlmostEqual(pts[-1][0], 139.7, places=3)
        self.assertGreater(max(p[1] for p in pts), 60, "Praha–Tokio vede přes Sibiř")
        fiji = atlas.great_circle((-17.7, 178.1), (21.3, -157.8))     # přes antimeridián
        self.assertLess(max(abs(b[0] - a[0]) for a, b in zip(fiji, fiji[1:])), 10, "bez skoku o 360°")

    def test_hex_counts(self):
        feats = self.data["hex"]["features"]
        for lvl in (r for _a, _b, r in atlas.HEX_LEVELS):
            level = [f["properties"] for f in feats if f["properties"]["lvl"] == lvl]
            self.assertEqual(sum(p["n"] for p in level), self.data["stats"]["photos"])
            self.assertEqual(sum(p["all"] for p in level if p["first"]), self.data["stats"]["photos"])
        ring = feats[0]["geometry"]["coordinates"][0]
        self.assertEqual(len(ring), 7)
        self.assertEqual(ring[0], ring[-1])

    def test_hex_cell_contains_point(self):
        for lat, lon in ((50.08, 14.42), (-33.9, 151.2), (64.1, -21.9)):
            size = 40 / 111.0
            q, r = atlas._hex_lonlat_key(lat, lon, size)
            clat, _clon = atlas._hex_center(q, r, size)
            self.assertLess(abs(clat - lat), size * 1.01)

    def test_empty_library(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE media_items (id INTEGER, latitude REAL, longitude REAL, date_taken TEXT, "
                           "event_id INTEGER, place_city TEXT, place_country TEXT, is_trashed INTEGER, file_path TEXT, "
                           "paired_video_path TEXT, media_type TEXT);")
        self.assertIsNone(atlas.build(conn))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestAtlasJs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="atlas_js_")
        conn = make_world_db(os.path.join(cls.dir, "w.db"), photos_per_trip=10, home_photos=100)
        cls.data = atlas.build(conn)
        conn.close()
        cls.m = ShownMapPage((1400, 900))
        cls.m.wait_style()
        cls.m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
        cls.m.wait_style()

    @classmethod
    def tearDownClass(cls):
        cls.m.js("window.atlasMode(false); 0")
        cls.m.close()
        shutil.rmtree(cls.dir, True)

    def js(self, code):
        return self.m.js(code)

    def setUp(self):
        self.js(f"window.setAtlas({json.dumps(self.data)}); window.atlasMode(true); 0")

    def tearDown(self):
        self.js("window.atlasMode(false); 0")

    def test_layers_and_pins(self):
        st = self.js("getAtlasState()")
        self.assertTrue(st["on"])
        self.assertEqual(st["layers"], 9)
        self.assertEqual(st["arcs"], len(self.data["arcs"]["features"]))
        self.assertTrue(self.js("document.body.classList.contains('atlas')"))
        self.assertEqual(self.js("map.getLayer('pins') ? map.getLayoutProperty('pins', 'visibility') : 'none'"), "none")
        self.assertEqual(self.js("getMapErrors().filter(function (e) { return /a-|atlas|gradient/.test(e); }).length"), 0)
        self.assertIn("výlet", self.js("document.getElementById('astats').textContent"))

    def test_year_filter(self):
        self.assertEqual(self.js("window.atlasYear(2019)"), 2019)
        flt = self.js("JSON.stringify(map.getFilter('a-arc'))")
        self.assertIn("2019", flt)
        self.assertIn("2019", self.js("document.getElementById('atitle').textContent"))
        self.assertEqual(self.js("document.querySelectorAll('#atl button[aria-pressed=\"true\"]').length"), 1)
        self.assertIsNone(self.js("window.atlasYear(null)"))
        self.assertIn("first", self.js("JSON.stringify(map.getFilter('a-hex-0'))"))

    def test_year_buttons(self):
        n = self.js("document.querySelectorAll('#atl button[data-y]').length")
        self.assertEqual(n, len(self.data["years"]) + 1)
        self.assertTrue(self.js("!!document.getElementById('aplay')"))

    def test_play_trip_leaves_atlas(self):
        tid = next(t["id"] for t in self.data["trips"] if t["playable"])
        self.assertTrue(self.js(f"window.atlasPlayTrip({tid})"))
        self.assertFalse(self.js("getAtlasState().on"))
        self.assertEqual(self.js("getAtlasState().layers"), 0)

    def test_journey_closes_atlas(self):
        from test_phase2_film import FILM_TRIP
        self.assertTrue(self.js(f"window.playJourney({json.dumps(FILM_TRIP)})"))
        self.assertFalse(self.js("getAtlasState().on"))
        self.js("window.endJourney(true); 0")

    def test_escape_closes(self):
        self.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'})); 0")
        self.assertFalse(self.js("getAtlasState().on"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
