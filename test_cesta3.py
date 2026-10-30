"""Cesta 3.0 – spojitá C2 kamera (C1), slunce v reliéfu a kometa (C4), Ken Burns ke tvářím
a postupná typografie (C5), přednačítání podle zorného pole, předtočení koridoru a reliéf
z výšek ≤ z11 (C7a–c).

Testy NESAHAJÍ na síť ani na photos.db (stránka s podstrčeným vektorovým stylem, síť blokovaná).
"""
import json
import math
import sqlite3
import sys
import unittest

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

import journey

if HAS_QT:
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage, wait
    from test_phase2_film import FILM_TRIP


class TestFaceFocus(unittest.TestCase):

    def test_area_weighted_centre(self):
        f = journey.face_focus([(0.1, 0.1, 0.1, 0.1), (0.6, 0.5, 0.3, 0.3)])   # malá vlevo nahoře, velká vpravo
        self.assertGreater(f[0], 0.6)
        self.assertGreater(f[1], 0.55)

    def test_no_faces_or_garbage(self):
        self.assertIsNone(journey.face_focus([]))
        self.assertIsNone(journey.face_focus([(None, 0.1, 0.1, 0.1), (0.2, 0.2, 0, 0.1), (5, 5, 0.1, 0.1)]))

    def test_journey_photos_carry_focus(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE face_instances (media_id INTEGER, bbox_x REAL, bbox_y REAL, bbox_w REAL, bbox_h REAL);
            CREATE TABLE media_items (id INTEGER PRIMARY KEY, event_id INTEGER);
            INSERT INTO media_items VALUES (1, 7), (2, 7), (3, 8);
            INSERT INTO face_instances VALUES (1, 0.7, 0.2, 0.2, 0.2), (3, 0.1, 0.1, 0.1, 0.1);
        """)
        focus = journey._event_focus(conn, 7)
        self.assertEqual(set(focus), {1}, "jen fotky této události s tvářemi")
        self.assertEqual(focus[1], [0.8, 0.3])
        self.assertEqual(journey._public({"id": 1, "thumb": "", "date": "", "focus": [0.8, 0.3]})["focus"], [0.8, 0.3])
        self.assertNotIn("focus", journey._public({"id": 2, "thumb": "", "date": "", "focus": None}))

    def test_missing_face_table_is_harmless(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(journey._event_focus(conn, 1), {})


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestCesta3(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1400, 900))
        cls.m.wait_style()
        cls.m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
        cls.m.wait_style()

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code)

    def setUp(self):
        self.js("FILM_SPEED = 1; reduceMotion = false; if (journey.active) window.endJourney(true); 0")

    def tearDown(self):
        self.js("FILM_SPEED = 1; reduceMotion = false; window.endJourney(true); 0")

    def play(self, trip=None):
        self.assertTrue(self.js(f"playJourney({json.dumps(trip or FILM_TRIP)})"))
        self.js("journeyPause(); 0")

    # ── C1 ──
    def _track(self, step=1 / 60):
        return self.js(f"""(function () {{ var out = [];
            for (var t = 0; t <= film.total; t += {step}) {{
              var p = getFilmPose(t);
              out.push([t, mercX(p.center[0]), mercY(p.center[1]), p.zoom, p.bearing, p.pitch]);
            }} return out; }})()""")

    def test_camera_never_stops_at_a_stop(self):
        self.play()
        dwells = self.js("film.dwells.map(function (d) { return d.t0; })")
        for t0 in dwells:
            a, b = self.js(f"[getFilmPose({t0} - 0.05), getFilmPose({t0} + 0.05)]")
            dz = abs(a["zoom"] - b["zoom"]) / 0.1
            db = abs(((a["bearing"] - b["bearing"] + 540) % 360) - 180) / 0.1
            self.assertGreater(dz * 10 + db, 0.5, f"kamera v zastávce ({t0:.1f} s) nezastaví: z' {dz:.3f}/s, b' {db:.2f}°/s")

    def test_motion_is_c2_smooth(self):
        self.play()
        tr = self._track()
        dt = 1 / 60
        worst = 0.0
        for ch, scale in ((3, 1.0), (4, 1 / 30.0)):            # zoom (z/s³), směr (°/s³ ÷ 30)
            v = [tr[k + 1][ch] - tr[k][ch] for k in range(len(tr) - 1)]
            if ch == 4:
                v = [((x + 540) % 360) - 180 for x in v]
            acc = [(v[k + 1] - v[k]) / dt / dt for k in range(len(v) - 1)]
            jerk = [abs(acc[k + 1] - acc[k]) / dt * scale for k in range(len(acc) - 1)]
            worst = max(worst, max(jerk))
        self.assertLess(worst, 60, f"žádné skoky zrychlení (ryv {worst:.1f})")

    def test_geometry_is_followed(self):
        self.play()
        for t in self.js("film.dwells.map(function (d) { return d.t0 + d.dur / 2; })"):
            g, p = self.js(f"[getFilmGeom({t}), getFilmPose({t})]")
            self.assertLess(abs(g["center"][0] - p["center"][0]) + abs(g["center"][1] - p["center"][1]), 0.002,
                            "uprostřed okamžiku je zastávka v ohnisku")
            self.assertLess(abs(g["zoom"] - p["zoom"]), 0.05)

    def test_late_trail_resamples_only_its_window(self):
        self.play()
        g = self.js("travelSegOf(2)")
        before = self.js(f"[getFilmPose({g['t0']} - 2.5), getFilmPose({g['t0']} + {g['dur']} / 2)]")
        self.js(f"""(function () {{ var p = legPath(2).map(function (q) {{ return [q[0] + 0.05, q[1] + 0.05]; }});
                    journey.trails = journey.trails || {{}}; journey.trailPending = {{2: 99}};
                    onTrailComputed({{ data: {{ seq: 99, leg: 2, path: [legPath(2)[0]].concat(p.slice(1, -1), [legPath(2).slice(-1)[0]]) }} }});
                    return 0; }})()""")
        after = self.js(f"[getFilmPose({g['t0']} - 2.5), getFilmPose({g['t0']} + {g['dur']} / 2)]")
        self.assertAlmostEqual(before[0]["center"][0], after[0]["center"][0], places=4, msg="minulost se nehne")
        self.assertNotAlmostEqual(before[1]["center"][0], after[1]["center"][0], places=4, msg="úsek přepočten")

    def test_late_trail_never_lands_inside_the_margin(self):
        self.play()
        g = self.js("travelSegOf(2)")
        self.js(f"journeySeek({g['t0']} - 0.5); journeyPause(); 0")
        self.assertTrue(self.js("legStarted(2)"), "1,6 s před rozjezdem už se dráha nemění")

    # ── C4 ──
    def test_relief_lit_by_the_sun_of_the_stop(self):
        self.play()
        self.js("journeySeek(film.dwells[1].t0 + 0.4); journeyPause(); 0")
        lit = self.js("getReliefLight()")
        sun = self.js("sunPosition(journey.data.stops[1].lat, journey.data.stops[1].lon, journey.data.stops[1].start)")
        self.assertIsNotNone(lit)
        self.assertLessEqual(abs(((lit["dir"] - sun["az"] + 540) % 360) - 180), 4)
        self.assertEqual(self.js("map.getPaintProperty('relief-film', 'hillshade-illumination-anchor')"), "map")
        self.js("window.endJourney(true); 0")
        self.assertEqual(self.js("map.getPaintProperty('relief', 'hillshade-illumination-anchor')"), "viewport")
        self.assertIsNone(self.js("getReliefLight()"))

    def test_comet_head_rides_the_route(self):
        self.play()
        hop = self.js("travelSegOf(2)")
        self.js(f"journeySeek({hop['t0']} + {hop['dur']} * 0.5); journeyPause(); 0")
        c = self.js("getComet()")
        self.assertIsNotNone(c)
        self.assertTrue(c["moving"])
        grad = self.js("map.getPaintProperty('j-cur', 'line-gradient')")
        self.assertEqual(grad[0], "interpolate", "ocas -> hlava")
        self.js(f"journeySeek(film.dwells[2].t0 + 0.3); journeyPause(); 0")
        c = self.js("getComet()")
        self.assertFalse(c["moving"])
        self.assertAlmostEqual(c["lng"], FILM_TRIP["stops"][2]["lon"], places=3)
        self.js("window.endJourney(true); 0")
        self.assertIsNone(self.js("getComet()"))

    # ── C5 ──
    def test_ken_burns_targets_faces(self):
        trip = json.loads(json.dumps(FILM_TRIP))
        trip["stops"][1]["best"]["focus"] = [0.8, 0.25]
        self.play(trip)
        self.js("journeySeek(film.dwells[1].t0 + 0.1); journeyPause(); 0")
        st = self.js("""(function () { var ph = document.querySelector('#jmoment .moment.show .ph.on');
            return [ph.style.backgroundPosition, ph.style.getPropertyValue('--kox'), ph.style.getPropertyValue('--kbs'),
                    ph.dataset.focus || '']; })()""")
        self.assertEqual(st, ["80% 25%", "80%", "1.14", "1"])

    def test_typography_staggers_in(self):
        self.play()
        delays = self.js("""(function () { var m = document.createElement('div'); m.className = 'moment show';
            m.innerHTML = momentHtml(FILM_DATA.stops[0], 0); document.getElementById('jmoment').appendChild(m);
            var out = Array.prototype.map.call(m.querySelectorAll('.meta > *'), function (e) {
              return parseFloat(getComputedStyle(e).transitionDelay); });
            m.remove(); return out; })()""".replace("FILM_DATA", json.dumps(FILM_TRIP)))
        self.assertEqual(len(delays), 3)
        self.assertTrue(delays[0] < delays[1] < delays[2], delays)

    # ── C7 ──
    def test_film_relief_uses_capped_dem(self):
        self.assertEqual(self.js("map.getSource('dem-film').maxzoom"), 11)
        self.play()
        self.assertEqual(self.js("map.getLayoutProperty('relief-film', 'visibility')"), "visible")
        self.assertEqual(self.js("map.getLayoutProperty('relief', 'visibility')"), "none")
        self.js("window.endJourney(true); 0")
        self.assertEqual(self.js("map.getLayoutProperty('relief', 'visibility')"), "visible")
        self.assertEqual(self.js("map.getLayoutProperty('relief-film', 'visibility')"), "none")

    def test_tile_decoding_runs_on_a_worker_pool(self):
        self.assertEqual(self.js("[tileWorkerCount(16), tileWorkerCount(8), tileWorkerCount(4), tileWorkerCount(0)]"),
                         [4, 2, 2, 2])
        self.assertGreaterEqual(self.js("getTileWorkers()"), 2, "dřív 1 worker = fronta dekódování za letu")

    def test_frustum_prefetch_covers_the_pitched_view(self):
        self.play()
        res = self.js("""(function () {
            var d = film.dwells[1], r = filmPoseAt(d.t0 + 0.5), pts = [];
            prefetchTr = map.transform.clone();
            var ok = frustumTiles(r.pose, film.pad, pts);
            prefetchTr = null;
            var zs = pts.map(function (p) { return p.z; });
            var far = pts.reduce(function (m, p) { return Math.max(m, haversineKm(p.ll, r.pose.c)); }, 0);
            var dem = [];
            prefetchSeen = {};
            prefetchPoints(pts);
            Object.keys(prefetchSeen).forEach(function (u) { if (/elevation|terrarium/.test(u)) dem.push(u); });
            return { ok: ok, n: pts.length, zmin: Math.min.apply(null, zs), zmax: Math.max.apply(null, zs), far: far,
                     dem: dem, pitch: r.pose.p, zoom: r.pose.z }; })()""")
        self.assertTrue(res["ok"])
        self.assertGreater(res["n"], 40)
        self.assertLess(res["zmin"], res["zmax"] - 0.5, "v dálce hrubší dlaždice (náklon)")
        self.assertGreater(res["far"], 2.0, "pruh u obzoru (dál než čtverec kolem středu)")
        dem_z = {int(u.split("/")[-3]) for u in res["dem"]}
        self.assertTrue(dem_z, "reliéf se přednačítá")
        self.assertLessEqual(max(dem_z), 11, "výšky ve filmu nejvýš z11")

    def test_dwell_prefetches_the_whole_next_leg(self):
        self.play()
        h = self.js("(function () { var d = film.dwells[0]; return [prefetchHorizon(d.t0 + 0.2), film.dwells[1].t0 + film.dwells[1].dur, d.t0 + 0.2]; })()")
        self.assertGreaterEqual(h[0] + 1e-6, min(h[1], h[2] + self.js("PREFETCH_DWELL_LOOKAHEAD_S")) - 1e-6)

    def test_preroll_holds_the_intro_briefly(self):
        self.play()
        r = self.js("""(function () { director.t = 0; film.preroll = 0; prefetchQueue.push('https://x.invalid/a.pbf');
            var n = 0; while (holdForTiles(0.25) && n < 100) n++;
            prefetchQueue.length = 0; return [n, film.preroll]; })()""")
        self.assertGreater(r[0], 0, "úvod chvíli počká na dlaždice")
        self.assertLess(r[0], 100, "nejvýš PREROLL_MAX_S")
        self.assertLessEqual(r[1], self.js("PREROLL_MAX_S") + 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
