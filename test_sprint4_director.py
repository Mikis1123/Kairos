"""Sprint 4.1 – sdílený Director (director.py + director.js): časová osa, střihy na doby, Ken Burns,
mapa dob hudby a Cesta, která přijíždí na zastávky na dobu.

JS část běží ve skutečné stránce mapy (síť blokovaná, podstrčený vektorový styl) a porovnává
director.js s director.py na stejných vstupech – přehrávač i export musí stříhat stejně.
"""
import json
import random
import sys
import unittest

import numpy as np

import ambient_music
import director as D

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

if HAS_QT:
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage
    from test_phase2_film import FILM_TRIP

BEATS = {"period": 2.25, "bar": 4, "offset": 0.0, "length": 150.0}


def random_segs(seed, n=14):
    rng = random.Random(seed)
    kinds = ["intro"] + [k for _ in range(n) for k in ("hop", "dwell")] + ["outro"]
    return [{"kind": k, "dur": round(rng.uniform(1.5, 7.0), 3)} for k in kinds]


def is_cut(g):
    return g["kind"] == "dwell"


def is_flex(g):
    return g["kind"] in ("hop", "intro")


class TestTimeline(unittest.TestCase):
    def test_layout_and_index(self):
        segs = [{"dur": 2.0}, {"dur": 3.0}, {"dur": 1.0}]
        self.assertEqual(D.layout(segs), 6.0)
        self.assertEqual([g["t0"] for g in segs], [0.0, 2.0, 5.0])
        self.assertEqual([D.index_at(segs, t) for t in (0, 1.99, 2.0, 4.9, 5.5, 99)], [0, 0, 1, 1, 2, 2])

    def test_compress_keeps_fixed_and_floor(self):
        segs = [{"k": "a", "dur": 4.0}, {"k": "x", "dur": 10.0}, {"k": "x", "dur": 1.0}, {"k": "a", "dur": 4.0}]
        total = D.compress(segs, 12.0, lambda g: g["k"] == "a", lambda g: 0.8)
        self.assertEqual(segs[0]["dur"], 4.0)
        self.assertEqual(segs[2]["dur"], 0.8, "podlaha")
        self.assertAlmostEqual(segs[1]["dur"], 10.0 * 4 / 11, places=6)
        self.assertAlmostEqual(total, sum(g["dur"] for g in segs))


class TestBeats(unittest.TestCase):
    def test_grid_loops_with_track(self):
        g = D.beat_grid({"period": 2.0, "bar": 4, "offset": 0.0, "length": 9.0}, 20)
        self.assertEqual(g["beats"][:6], [0.0, 2.0, 4.0, 6.0, 8.0, 9.0], "smyčka začíná mřížku znovu")
        self.assertEqual(g["downbeats"][:3], [0.0, 8.0, 9.0])
        self.assertEqual(D.beat_grid({"period": 0}, 10)["beats"], [])

    def test_snap_cuts_lands_on_beats(self):
        for seed in range(6):
            segs = random_segs(seed)
            D.layout(segs)
            grid = D.beat_grid(BEATS, 400)
            n = D.snap_cuts(segs, grid, is_cut, is_flex)
            self.assertGreater(n, 10)
            beats = set(round(b, 6) for b in grid["beats"])
            on = [round(g["t0"], 6) in beats for g in segs if is_cut(g)]
            self.assertGreaterEqual(sum(on), n)
            for g in segs:
                self.assertGreater(g["dur"], 0)

    def test_snap_prefers_downbeat(self):
        segs = [{"kind": "hop", "dur": 8.4}, {"kind": "dwell", "dur": 3}]
        D.snap_cuts(segs, D.beat_grid({"period": 2.25, "bar": 4}, 60), is_cut, is_flex)
        self.assertAlmostEqual(segs[1]["t0"], 9.0, msg="těžká doba 9,0 místo bližší doby 9,0/6,75")
        segs = [{"kind": "hop", "dur": 5.5}, {"kind": "dwell", "dur": 3}]
        D.snap_cuts(segs, D.beat_grid({"period": 2.25, "bar": 4}, 60), is_cut, is_flex)
        self.assertAlmostEqual(segs[1]["t0"], 4.5, msg="těžká doba 9,0 je moc daleko -> nejbližší doba")

    def test_track_beat_maps(self):
        for t in ambient_music.tracks():
            m = ambient_music.beat_map(t)
            spec = next((s for s in ambient_music.TRACKS if s["file"] == t["file"]), None)
            if spec:
                self.assertAlmostEqual(m["period"] * m["bar"], spec["chord_s"], msg="takt = akord")

    def test_detect_beats_synthetic(self):
        sr = ambient_music.DETECT_SR
        for bpm, off in ((100, 0.3), (72, 1.1), (128, 0.05)):
            pcm = np.random.default_rng(1).normal(0, 0.02, int(sr * 40))
            per = 60 / bpm
            for k in range(int((40 - off - 0.1) / per)):
                b, n = int((off + k * per) * sr), int(0.08 * sr)
                pcm[b:b + n] += (1.0 if k % 4 == 0 else 0.5) * np.sin(2 * np.pi * 180 * np.arange(n) / sr) \
                    * np.exp(-np.arange(n) / sr * 40)
            r = ambient_music.detect_beats(None, pcm=pcm.astype(np.float32))
            self.assertAlmostEqual(r["period"], per, delta=0.004)
            err = (r["offset"] - off + 2 * per) % (4 * per) - 2 * per
            self.assertLess(abs(err), 0.03, f"{bpm} BPM: těžká doba {r['offset']} místo {off}")

    def test_no_rhythm(self):
        pcm = np.random.default_rng(2).normal(0, 0.1, ambient_music.DETECT_SR * 20).astype(np.float32)
        self.assertEqual(ambient_music.detect_beats(None, pcm=pcm)["period"], 0.0)


class TestKenBurns(unittest.TestCase):
    def test_rect_stays_inside_photo(self):
        for aspect in (0.66, 1.0, 1.5, 3.0):
            for seed in range(8):
                for focus in (None, [0.05, 0.95], [0.9, 0.1]):
                    kb = D.ken_burns(focus, seed, aspect_in=aspect)
                    for cx, cy, s in (kb["from"], kb["to"], D.kb_at(kb, 0.5)):
                        w = s * (16 / 9) / aspect
                        self.assertLessEqual(s, 1.0 + 1e-6)
                        if w < 1:
                            self.assertGreaterEqual(cx - w / 2, -1e-4)
                            self.assertLessEqual(cx + w / 2, 1 + 1e-4)
                        self.assertGreaterEqual(cy - s / 2, -1e-4)

    def test_push_towards_faces(self):
        kb = D.ken_burns([0.8, 0.3], 0, aspect_in=1.5)
        self.assertGreater(kb["to"][0], kb["from"][0])
        self.assertLess(kb["to"][2], kb["from"][2], "přítah = menší výřez")
        self.assertEqual(D.kb_at(kb, 0), kb["from"])


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestDirectorJs(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1400, 900))
        cls.m.wait_style()
        cls.m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
        cls.m.wait_style()

    @classmethod
    def tearDownClass(cls):
        cls.m.js("window.setJourneyMusic({enabled: false, tracks: []}); 0")
        cls.m.close()

    def js(self, code):
        return self.m.js(code)

    def test_engine_loaded(self):
        self.assertTrue(self.js("!!window.KairosDirector && typeof KairosDirector.snapCuts === 'function'"))
        self.assertTrue(self.js("smootherstep === KairosDirector.smootherstep"), "Cesta používá Director")

    def test_snap_parity_with_python(self):
        for seed in range(5):
            segs = random_segs(seed)
            js = json.loads(self.js(
                f"(function () {{ var s = {json.dumps(segs)}, D = KairosDirector; D.layout(s);"
                f" var n = D.snapCuts(s, D.beatGrid({json.dumps(BEATS)}, 400), {{cut: function (g) {{ return g.kind === 'dwell'; }},"
                f" stretch: function (g) {{ return g.kind === 'hop' || g.kind === 'intro'; }}}});"
                f" return JSON.stringify({{n: n, t0: s.map(function (g) {{ return g.t0; }})}}); }})()"))
            D.layout(segs)
            n = D.snap_cuts(segs, D.beat_grid(BEATS, 400), is_cut, is_flex)
            self.assertEqual(js["n"], n)
            for a, b in zip(js["t0"], [g["t0"] for g in segs]):
                self.assertAlmostEqual(a, b, places=6)

    def test_grid_and_kb_parity(self):
        g = json.loads(self.js("JSON.stringify(KairosDirector.beatGrid({period: 2.5, bar: 4, offset: 0.4, length: 150}, 320))"))
        p = D.beat_grid({"period": 2.5, "bar": 4, "offset": 0.4, "length": 150}, 320)
        self.assertEqual(len(g["beats"]), len(p["beats"]))
        self.assertAlmostEqual(max(abs(a - b) for a, b in zip(g["beats"], p["beats"])), 0, places=9)
        self.assertEqual(len(g["downbeats"]), len(p["downbeats"]))
        h = self.js("[0, 0.3, 0.7, 1].map(function (u) { return KairosDirector.hermite(u, 0.6, 0.6); })")
        self.assertEqual([round(x, 9) for x in h], [round(D.hermite(u, 0.6, 0.6), 9) for u in (0, 0.3, 0.7, 1)])

    def test_camera_spline_is_c1_and_pinned(self):
        r = json.loads(self.js("""(function () {
            var sp = new KairosDirector.CameraSpline({dt: 0.2, keys: ['x'], tau: {x: 0.3},
                sample: function (t) { return {x: t < 2 ? 0 : 1}; }}).build(4);
            var a = sp.at(0).x, b = sp.at(4).x, jump = 0;
            for (var t = 0.01; t < 4; t += 0.01) jump = Math.max(jump, Math.abs(sp.at(t).x - sp.at(t - 0.01).x));
            return JSON.stringify({a: a, b: b, jump: jump}); })()"""))
        self.assertAlmostEqual(r["a"], 0, places=3)
        self.assertAlmostEqual(r["b"], 1, places=3)
        self.assertLess(r["jump"], 0.03, "skok vstupu se rozloží do hladké křivky")

    def test_beat_clock(self):
        r = self.js("""(function () { var c = new KairosDirector.BeatClock({offset: 0});
            return [c.rate(10, 10), c.rate(10, 10.5), c.rate(10, 9.5), c.rate(10, 30)]; })()""")
        self.assertAlmostEqual(r[0], 1)
        self.assertGreater(r[1], 1)
        self.assertLess(r[2], 1)
        self.assertLessEqual(r[1], 1.12 + 1e-9)

    def test_cesta_arrives_on_beats_with_music(self):
        tracks = [{"title": "T", "url": "file:///nic.opus", "beats": BEATS}]
        self.js(f"window.setJourneyMusic({json.dumps({'enabled': True, 'tracks': tracks})}); 0")
        try:
            plan = json.loads(self.js(f"(function () {{ var f = getFilmPlan({json.dumps(FILM_TRIP)});"
                                      " return JSON.stringify({beats: f.beats, cuts: f.dwells.map(function (g) { return g.t0; }),"
                                      " grid: f.grid.beats}); })()"))
        finally:
            self.js("window.setJourneyMusic({enabled: false}); 0")
        self.assertGreaterEqual(plan["beats"], len(plan["cuts"]) - 1)
        grid = set(round(b, 5) for b in plan["grid"])
        on = sum(round(t, 5) in grid for t in plan["cuts"])
        self.assertGreaterEqual(on, len(plan["cuts"]) - 1, "příjezdy na zastávky na doby")
        free = json.loads(self.js(f"JSON.stringify(getFilmPlan({json.dumps(FILM_TRIP)}).dwells.map(function (g) {{ return g.t0; }}))"))
        self.assertNotEqual([round(t, 3) for t in free], [round(t, 3) for t in plan["cuts"]], "bez hudby beze změny")


if __name__ == "__main__":
    unittest.main(verbosity=2)
