"""Day chapters of a multi-day Journey + zero-hole map fidelity (2026-11-03).

Python: journey.build_days (chapters / calendar days), tile_store priority queues, RAM cache,
_warm / _state endpoints, per-day corridor routing. Page: day pills, single-day films, next
day, seekable timeline, region / arrival-view warm-up keys, terrain during descents.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import database
import journey
import tile_store

try:
    from PyQt6.QtWidgets import QApplication
    from test_patch6 import WindowEnv  # noqa: F401  (QtWebEngine before the QApplication)
    _app = QApplication.instance() or QApplication(sys.argv)
    import main
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False


def _p(date, chapter=None, city="X"):
    return {"id": 0, "date": date, "chapter": chapter, "lat": 49.0, "lon": 13.0, "city": city}


class TestSplitDays(unittest.TestCase):
    def test_calendar_days_with_early_morning_cut_off(self):
        days = journey.split_days([_p("2026-07-06 10:00"), _p("2026-07-06 23:50"), _p("2026-07-07 01:30"),
                                   _p("2026-07-07 09:00"), _p("2026-07-08 12:00")])
        self.assertEqual([len(d) for d in days], [3, 1, 1], "01:30 still belongs to the evening before")
        self.assertEqual(journey.day_of("2026-07-07 03:59"), "2026-07-06")
        self.assertEqual(journey.day_of("2026-07-07 04:00"), "2026-07-07")

    def test_trip_chapters_win_over_the_calendar(self):
        days = journey.split_days([_p("2026-07-06 10:00", 5), _p("2026-07-07 09:00", 5), _p("2026-07-07 18:00", 6)])
        self.assertEqual([len(d) for d in days], [2, 1])

    def test_first_stop_is_where_the_day_begins(self):
        stops = [{"start": "2026-07-06 10:00", "end": "2026-07-06 12:00"},
                 {"start": "2026-07-06 19:00", "end": "2026-07-07 09:00"},     # the night at the cottage
                 {"start": "2026-07-07 11:00", "end": "2026-07-07 12:00"}]
        self.assertEqual(journey.first_stop_at(stops, "2026-07-06 10:00"), 0)
        self.assertEqual(journey.first_stop_at(stops, "2026-07-07 09:00"), 1)
        self.assertEqual(journey.first_stop_at(stops, "2026-07-08 09:00"), 2)


class TestJourneyDaysDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos_days_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.conn = database.get_connection()
        self.conn.execute("INSERT INTO events (id, title, location_name, start_date, end_date) VALUES "
                          "(1, 'Šumava', '', '2026-07-06 08:00', '2026-07-08 22:00')")
        self.n = 0

    def tearDown(self):
        self.conn.close()
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, date, lat, lon, city):
        self.n += 1
        self.conn.execute(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, latitude, longitude, "
            "place_city, event_id, thumbnail_path) VALUES (?, ?, 'photo', ?, ?, ?, ?, 1, ?)",
            (f"C:/x/p{self.n}.jpg", f"p{self.n}.jpg", date, lat, lon, city, f"C:/t/{self.n}.jpg"))

    def test_days_have_their_own_stops_and_a_jump_point(self):
        # day 1: two places; day 2: one place only; day 3: three places
        self.add("2026-07-06 09:00:00", 49.247, 13.399, "Kolinec")
        self.add("2026-07-06 14:00:00", 49.160, 13.520, "Kasperske Hory")
        self.add("2026-07-07 10:00:00", 49.247, 13.399, "Kolinec")
        self.add("2026-07-08 09:00:00", 49.050, 13.230, "Zelezna Ruda")
        self.add("2026-07-08 12:00:00", 49.100, 13.300, "Spicak")
        self.add("2026-07-08 16:00:00", 49.247, 13.399, "Kolinec")
        self.conn.commit()
        data = journey.build_journey(self.conn, 1)
        days = data["days"]
        self.assertEqual([d["n"] for d in days], [1, 2, 3])
        self.assertEqual([len(d["stops"]) for d in days], [2, 0, 3], "a one-place day is listed, not playable")
        self.assertEqual([d["date"] for d in days], ["2026-07-06", "2026-07-07", "2026-07-08"])
        for d in days:
            self.assertLess(d["first"], len(data["stops"]))
            self.assertIn("thumb", d)
        self.assertEqual(days[0]["first"], 0)
        self.assertGreater(days[2]["first"], days[0]["first"])
        json.dumps(data)                              # goes to the page as JSON

    def test_single_day_trip_has_no_days(self):
        self.add("2026-07-06 09:00:00", 49.247, 13.399, "Kolinec")
        self.add("2026-07-06 14:00:00", 49.160, 13.520, "Kasperske Hory")
        self.conn.commit()
        self.assertNotIn("days", journey.build_journey(self.conn, 1))


class _FakeUpstream:
    def __init__(self, delay=0.0):
        self.calls, self.delay, self.lock = [], delay, threading.Lock()

    def fetch(self, host, path, lane="map"):
        with self.lock:
            self.calls.append((host + path, lane))
        time.sleep(self.delay)
        return 200, b"x" * 10, "image/png", None


class TestTileStorePriorities(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos_ts_")
        self.store = tile_store.TileStore(os.path.join(self.tmp, "t.sqlite"), budget_mb=50, hot_mb=1)
        self.srv = tile_store.TileServer(self.store, shards=2, redirect_misses=False)
        self.up = self.srv.upstream = _FakeUpstream()

    def tearDown(self):
        self.srv.shutdown()
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_idle(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            st = self.srv.state()
            if not any(st["queued"].values()) and st["threads"] == 0:
                return st
            time.sleep(0.02)
        self.fail("queues did not drain")

    def test_urgent_runs_before_film_before_background(self):
        with self.srv._bake_lock:                       # queue everything before a worker runs
            bg = [f"tile.opentopomap.org/5/{x}/0.png" for x in range(20)]
            film = [f"tile.opentopomap.org/6/{x}/0.png" for x in range(20)]
            urgent = [f"tile.opentopomap.org/7/{x}/0.png" for x in range(10)]
            with mock.patch.object(tile_store, "BAKE_WORKERS", 1):     # one worker: strict order
                self.srv._queues["background"].extend(reversed(bg))
                self.srv._queues["film"] = list(reversed(film))
                self.srv._queues["urgent"].extend(reversed(urgent))
                self.srv._spawn()
        st = self.wait_idle()
        order = [k for k, _ in self.up.calls]
        self.assertEqual(order, urgent + film + bg)
        lanes = dict(self.up.calls)
        self.assertEqual(lanes[bg[0]], "background")
        self.assertEqual(lanes[urgent[0]], "bake")
        self.assertEqual(lanes[film[0]], "bake")
        self.assertEqual(st["queued"], {"urgent": 0, "film": 0, "background": 0})

    def test_background_never_takes_every_worker(self):
        self.up.delay = 0.2
        self.srv.warm([f"tile.opentopomap.org/5/{x}/1.png" for x in range(40)], "background")
        time.sleep(0.1)
        self.assertLessEqual(self.srv._bg_active, tile_store.BACKGROUND_THREADS)
        t0 = time.time()
        self.srv.warm(["tile.opentopomap.org/9/9/9.png"], "urgent")
        while "tile.opentopomap.org/9/9/9.png" not in [k for k, _ in self.up.calls] and time.time() - t0 < 3:
            time.sleep(0.01)
        self.assertLess(time.time() - t0, 0.15, "an urgent tile starts at once, not after the background")

    def test_stored_keys_are_skipped_and_foreign_hosts_refused(self):
        self.store.put("tile.opentopomap.org/1/0/0.png", b"have", "image/png", None)
        self.store.flush()
        n = self.srv.warm(["tile.opentopomap.org/1/0/0.png", "evil.example.com/1/1/1.png",
                           "tile.opentopomap.org/1/1/0.png"], "background")
        self.assertEqual(n, 2)
        self.wait_idle()
        self.assertEqual([k for k, _ in self.up.calls], ["tile.opentopomap.org/1/1/0.png"])

    def test_warm_and_state_endpoints(self):
        base = self.srv.bases()[0]
        body = json.dumps({"keys": ["tile.opentopomap.org/3/1/1.png"], "priority": "urgent"}).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "_warm", data=body, method="POST"), timeout=5) as r:
            self.assertEqual(json.loads(r.read())["accepted"], 1)
        self.wait_idle()
        with urllib.request.urlopen(base + "_state", timeout=5) as r:
            st = json.loads(r.read())
        self.assertEqual(st["warming"]["urgent"]["done"], 1)
        self.assertEqual(st["ports"], 2)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.srv.bases()[0].replace(self.srv.token, "wrong") + "_state", timeout=5)

    def test_hot_cache_serves_without_sqlite_and_stays_in_budget(self):
        self.store.put("tile.opentopomap.org/2/0/0.png", b"a" * 1000, "image/png", None)
        self.store.flush()
        self.assertIsNotNone(self.store.get("tile.opentopomap.org/2/0/0.png"))   # from SQLite, now in RAM
        hits = self.store.hot_hits
        self.assertEqual(self.store.get("tile.opentopomap.org/2/0/0.png")[0], b"a" * 1000)
        self.assertEqual(self.store.hot_hits, hits + 1)
        for i in range(30):                                  # 1 MB budget, 100 kB tiles
            k = f"tile.opentopomap.org/2/{i}/1.png"
            self.store.put(k, b"b" * 100_000, "image/png", None)
        self.store.flush()
        for i in range(30):
            self.store.get(f"tile.opentopomap.org/2/{i}/1.png")
        self.assertLessEqual(self.store.hot_stats()["bytes"], self.store.hot_budget)
        self.store.clear()
        self.assertEqual(self.store.hot_stats()["tiles"], 0)
        self.assertIsNone(self.store.get("tile.opentopomap.org/2/0/0.png"))

    def test_eight_ports_by_default(self):
        self.assertEqual(tile_store.SHARDS, 8)


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestDayRouting(unittest.TestCase):
    def test_route_journey_routes_the_days_own_stops(self):
        w = mock.MagicMock()
        stops_whole = [{"lat": 49.0 + i * 0.1, "lon": 13.0, "entry": {"lat": 49.0, "lon": 13.0},
                        "exit": {"lat": 49.0, "lon": 13.0}} for i in range(3)]
        stops_day = stops_whole[:2]
        w._journey_data = {"id": 5, "stops": stops_whole,
                           "days": [{"stops": stops_day}, {"stops": []}]}
        w._corridor_router = None
        seen = {}

        class Router:
            def __init__(self, jid, reqs, fetch, parent):
                seen["reqs"] = reqs
                self.leg_ready = mock.MagicMock()
                self.finished = mock.MagicMock()

            def start(self, *a):
                pass

            def deleteLater(self):
                pass

            def stop(self):
                pass

        with mock.patch.object(main, "CorridorRouter", Router), \
             mock.patch.object(main.tile_store, "service", return_value=mock.MagicMock()), \
             mock.patch.object(main.route_corridor, "leg_requests", side_effect=lambda st: [(i, st[i]) for i in range(1, len(st))]):
            self.assertTrue(main.MainWindow.route_journey(w, 5, "https://t/{z}/{x}/{y}.pbf", day=0))
            self.assertEqual(len(seen["reqs"]), 1, "the day's one leg, not the trip's two")
            self.assertFalse(main.MainWindow.route_journey(w, 5, "https://t/{z}/{x}/{y}.pbf", day=1),
                             "a one-place day has nothing to route")
            self.assertTrue(main.MainWindow.route_journey(w, 5, "https://t/{z}/{x}/{y}.pbf"))
            self.assertEqual(len(seen["reqs"]), 2)

    def test_corridor_leg_carries_the_day(self):
        w = mock.MagicMock()
        w._map_loaded = True
        main.MainWindow._on_corridor_leg(w, 5, 2, "[[1,2],[3,4]]", 1)
        js = w.map_view.page().runJavaScript.call_args[0][0]
        self.assertIn("corridorRoute(5, 2, [[1,2],[3,4]], 1)", js)


def _stop(lat, lon, start, end, sid=1):
    return {"lat": lat, "lon": lon, "start": start, "end": end, "city": "X", "count": 1,
            "entry": {"lat": lat, "lon": lon}, "exit": {"lat": lat, "lon": lon},
            "photos": [{"id": sid, "thumb": "", "date": start}],
            "best": {"id": sid, "thumb": "", "date": start, "image": "", "type": "photo"}}


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestDayChaptersPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_patch11_maps import ShownMapPage
        cls.m = ShownMapPage((1000, 700))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def tearDown(self):
        self.js("if (journey.active) window.endJourney(true); reduceMotion = false; 0")

    def trip(self):
        d1 = [_stop(49.25, 13.40, "2026-07-06 09:00", "2026-07-06 10:00", 1),
              _stop(49.16, 13.52, "2026-07-06 14:00", "2026-07-06 15:00", 2)]
        d3 = [_stop(49.05, 13.23, "2026-07-08 09:00", "2026-07-08 10:00", 4),
              _stop(49.10, 13.30, "2026-07-08 12:00", "2026-07-08 13:00", 5),
              _stop(49.25, 13.40, "2026-07-08 16:00", "2026-07-08 17:00", 6)]
        whole = d1 + [_stop(49.25, 13.40, "2026-07-07 10:00", "2026-07-07 11:00", 3)] + d3
        return {"id": 91, "title": "Šumava", "location": "Kolinec", "start": "2026-07-06 09:00",
                "end": "2026-07-08 17:00", "count": 6, "stops": whole,
                "days": [{"n": 1, "date": "2026-07-06", "start": d1[0]["start"], "end": d1[-1]["end"], "count": 2,
                          "city": "Kolinec", "thumb": "", "first": 0, "stops": d1},
                         {"n": 2, "date": "2026-07-07", "start": "2026-07-07 10:00", "end": "2026-07-07 11:00",
                          "count": 1, "city": "Kolinec", "thumb": "", "first": 2, "stops": []},
                         {"n": 3, "date": "2026-07-08", "start": d3[0]["start"], "end": d3[-1]["end"], "count": 3,
                          "city": "Zelezna Ruda", "thumb": "", "first": 3, "stops": d3,
                          "tracks": {"1": [[13.23, 49.05], [13.26, 49.08], [13.30, 49.10]]}}]}

    def play(self, data=None):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(data or self.trip())})"))

    def test_whole_trip_shows_day_pills_and_ticks(self):
        self.play()
        st = self.js("getJourneyDays()")
        self.assertEqual(st["day"], -1)
        self.assertEqual(st["pills"], 4, "whole trip + 3 days")
        self.assertEqual(st["ticks"], 2, "day 2 and day 3 start on the timeline")
        self.assertFalse(self.js("document.getElementById('jdays').hidden"))
        self.assertFalse(self.js("document.getElementById('jdaynext').hidden"))
        self.assertEqual(self.js("journeyState().stops"), 6)

    def test_single_day_plays_its_own_stops(self):
        self.play()
        self.assertTrue(self.js("journeyPlayDay(2)"))
        s = self.js("journeyState()")
        self.assertEqual((s["day"], s["stops"], s["active"]), (2, 3, True))
        self.assertEqual(self.js("getLegPath(1)"), [[13.23, 49.05], [13.26, 49.08], [13.30, 49.10]],
                         "the day's GPX leg (index in the day film)")
        self.assertIn("3", self.js("document.getElementById('jtitle').textContent"))
        self.assertEqual(self.js("document.querySelector('#jdays button[aria-selected=\"true\"]').dataset.day"), "2")
        self.assertLess(self.js("journeyState().total"), self.js(f"getFilmPlan({json.dumps(self.trip())}).total"),
                        "a day is a shorter film than the whole trip")

    def test_one_place_day_jumps_into_the_whole_trip(self):
        self.play()
        self.js("journeyPlayDay(0); 0")
        self.assertEqual(self.js("journeyState().day"), 0)
        self.assertTrue(self.js("journeyPlayDay(1)"))
        s = self.js("journeyState()")
        self.assertEqual((s["day"], s["stops"], s["index"]), (-1, 6, 2))

    def test_next_day(self):
        self.play()
        self.js("journeyStepDay(1); 0")                   # whole trip: to day 2's first stop
        self.assertEqual(self.js("journeyState().index"), 2)
        self.js("journeyStepDay(1); 0")
        self.assertEqual(self.js("journeyState().index"), 3)
        self.js("journeyPlayDay(0); 0")                   # day film: next = the next day's film
        self.js("journeyStepDay(1); 0")
        self.assertEqual(self.js("journeyState().day"), -1, "day 2 has one place: whole trip at its stop")
        self.js("journeyPlayDay(2); 0")
        self.js("journeyStepDay(-1); 0")                  # back from day 3 to day 2 (one place)
        s = self.js("journeyState()")
        self.assertEqual((s["day"], s["index"]), (-1, 2))

    def test_end_card_offers_next_day_and_whole_trip(self):
        self.play()
        self.js("journeyPlayDay(0); 0")
        self.js("journeySeek(journeyState().total); 0")
        self.m.wait_style()
        end = time.time() + 3
        while not self.js("journeyState().ended") and time.time() < end:
            from test_patch11_maps import wait
            wait(50)
        self.assertTrue(self.js("journeyState().ended"))
        self.assertFalse(self.js("document.getElementById('jnextday').hidden"))
        self.assertFalse(self.js("document.getElementById('jwhole').hidden"))
        self.js("document.getElementById('jwhole').click(); 0")
        self.assertEqual(self.js("journeyState().day"), -1)
        self.assertTrue(self.js("document.getElementById('jnextday').hidden"))

    def test_seek_bar_scrubs_the_film(self):
        self.play()
        total = self.js("journeyState().total")
        self.assertTrue(self.js(f"journeySeekTo({total / 2})"))
        self.assertAlmostEqual(self.js("journeyState().t"), total / 2, delta=0.2)
        self.js("journeyPause(); 0")
        self.js(f"journeySeekTo({total * 0.8}); 0")
        s = self.js("journeyState()")
        self.assertTrue(s["paused"], "seeking a paused film keeps it paused")
        self.assertAlmostEqual(s["t"], total * 0.8, delta=0.2)
        self.assertEqual(self.js("document.getElementById('jprogress').getAttribute('role')"), "slider")

    def test_corridor_for_another_day_is_ignored(self):
        trip = self.trip()
        self.play(trip)
        self.js("journeyPlayDay(0); 0")
        road = [[13.40, 49.25], [13.46, 49.20], [13.52, 49.16]]
        self.assertFalse(self.js(f"corridorRoute(91, 1, {json.dumps(road)})"), "whole-trip route, day film")
        self.assertFalse(self.js(f"corridorRoute(91, 1, {json.dumps(road)}, 2)"), "another day")
        self.assertTrue(self.js(f"corridorRoute(91, 1, {json.dumps(road)}, 0)"))

    def test_plain_journey_has_no_day_ui(self):
        trip = self.trip()
        del trip["days"]
        self.play(trip)
        self.assertTrue(self.js("document.getElementById('jdays').hidden"))
        self.assertTrue(self.js("document.getElementById('jdaynext').hidden"))
        self.assertEqual(self.js("journeyState().day"), -1)

    def test_strings_have_defaults(self):
        for key in ("whole-trip", "next-day", "days-a11y", "seek-a11y", "day-one-place"):
            self.assertNotEqual(self.js(f"T({json.dumps(key)})"), key)
        self.assertEqual(self.js("T('day-n', {n: 4})"), "Day 4")


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestFidelityPage(unittest.TestCase):
    STORE = {"bases": ["http://127.0.0.1:9/tok/"], "hosts": sorted(tile_store.TILE_HOSTS)}

    @classmethod
    def setUpClass(cls):
        from test_patch11_maps import ShownMapPage
        cls.m = ShownMapPage((1000, 700))
        cls.m.js(f"setTileStore({json.dumps(cls.STORE)}); 0")

    @classmethod
    def tearDownClass(cls):
        cls.m.js("setTileStore(null); 0")
        cls.m.close()

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def test_region_keys_go_parents_first_and_stay_in_budget(self):
        spec = {"bbox": [13.3, 49.1, 13.5, 49.3], "points": [[13.4, 49.25], [13.35, 49.15]], "zmin": 10, "zmax": 16}
        keys = self.js(f"regionTileKeys({json.dumps(spec)})")
        self.assertTrue(keys, "the vector, DEM and underlay sources yield keys")
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLessEqual(len(keys), self.js("REGION_MAX_KEYS"))
        vec = [k for k in keys if k.startswith("tiles.openfreemap.org/")]
        if vec:
            zs = [int(k.split("/")[-3]) for k in vec]
            self.assertEqual(zs, sorted(zs), "lower zooms (overzoom fallback) before detail")
            self.assertLessEqual(max(zs), 14, "the vector source overzooms past z14: nothing deeper")
        dem = [k for k in keys if k.startswith("elevation-tiles-prod")]
        self.assertTrue(dem, "terrain for the region too")
        self.assertTrue(all(k.split("/")[0] in tile_store.TILE_HOSTS for k in keys))
        self.assertEqual(self.js(f"regionTileKeys({json.dumps(dict(spec, max=25))}).length"), 25)

    def test_huge_region_falls_back_to_points(self):
        spec = {"bbox": [-10, 35, 30, 60], "points": [[13.4, 49.25]], "zmin": 12, "zmax": 13}
        keys = self.js(f"regionTileKeys({json.dumps(spec)})")
        self.assertLessEqual(len(keys), 9 * 6, "3 × 3 around the one place per source and level")

    def test_arrival_view_keys_cover_the_target(self):
        keys = self.js("warmViewKeys({center: [13.4, 49.25], zoom: 16, pitch: 52, bearing: 0})")
        self.assertGreater(len(keys), 4)
        dem = {int(k.split("/")[-3]) for k in keys if k.startswith("elevation-tiles-prod")}
        self.assertIn(14, dem, "terrain at the arrival zoom (DEM max z14)")
        self.assertTrue(dem & {12, 13}, "parent levels for the overzoom fallback")
        # the basemap of this (offline: raster fallback, online: vector) style is in there too
        self.assertTrue(any(not k.startswith("elevation-tiles-prod") for k in keys))

    def test_flight_into_the_landscape_turns_terrain_on_in_the_descent(self):
        self.js("reduceMotion = false; map.jumpTo({center: [13.4, 49.25], zoom: 6, pitch: 0, bearing: 0}); 0")
        self.assertTrue(self.js("flyToPhotos([{lat: 49.25, lon: 13.40}, {lat: 49.251, lon: 13.401}])"))
        st = self.js("({on: descent.on, at: descent.at})")
        self.assertTrue(st["on"])
        self.assertGreaterEqual(st["at"], self.js("TERRAIN_MIN_ZOOM"))
        self.js("map.stop(); 0")
        self.assertFalse(self.js("descent.on"), "moveend ends the descent")

    def test_terrain_ramp_starts_flat(self):
        self.js("descent.rampAt = performance.now(); 0")
        self.assertLess(self.js("terrainRampK()"), 0.1)
        self.js("descent.rampAt = performance.now() - TERRAIN_RAMP_MS - 1; 0")
        self.assertEqual(self.js("terrainRampK()"), 1)
        self.assertEqual(self.js("descent.rampAt"), 0)

    def test_maplibre_limits_raised(self):
        # 32 parallel image requests tripled dropped film frames (measured): MapLibre's 16 stays
        self.assertEqual(self.js("MAX_IMAGE_REQUESTS"), 16)
        self.assertEqual(self.js("map._maxTileCacheZoomLevels || TILE_CACHE_ZOOM_LEVELS"), 7)

if __name__ == "__main__":
    unittest.main()
