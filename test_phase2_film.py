"""Fáze 2 (zpětná vazba z praxe): mřížka ≥ 3 sloupce, ostrý živý náhled Live Photo, průběh
přípravy videí, vícesnímkový index videí v hledání a Cesta 2.0 (film na jedné časové ose)."""
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

try:
    from PyQt6.QtCore import Qt, QPoint, QPointF
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401  (před QApplication)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

import ai_engine
import scrub_strip
import video_frames
import video_proxy

if HAS_QT:
    import motion
    import main
    from test_patch6 import WindowEnv
    from test_patch11_maps import ShownMapPage, wait
else:                      # pragma: no cover
    WindowEnv = unittest.TestCase


# ── mřížka ──────────────────────────────────────────────────────────────────
@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestGridMinimumColumns(WindowEnv):
    N_PHOTOS = 12

    def setUp(self):
        super().setUp()
        saved = (motion._user_reduce, motion._system_cache)
        motion._user_reduce, motion._system_cache = True, True
        self.addCleanup(lambda: setattr(motion, "_user_reduce", saved[0]))

    def test_zoom_never_below_three_columns(self):
        self.assertEqual(main.GRID_MIN_COLUMNS, 3)
        g = self.w.grid_view
        _app.processEvents()
        with mock.patch.object(main, "save_config"):
            for _ in range(20):
                pos = QPointF(g.viewport().rect().center())
                ev = QWheelEvent(pos, g.viewport().mapToGlobal(pos), QPoint(0, 0), QPoint(0, 120),
                                 Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                                 Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(g.viewport(), ev)
        self.assertEqual(g.columns(), 3)
        self.assertLessEqual(3 * g.cell_size, g.viewport().width(), "tři dlaždice se vejdou do řádku")

    def test_remembered_big_cell_is_clamped_to_three_columns(self):
        g = self.w.grid_view
        _app.processEvents()
        g.set_cell_size(10_000)                       # uložená velikost ze starší verze (2 sloupce)
        self.assertLessEqual(g.cell_size, g._max_cell())
        self.assertGreaterEqual(g.viewport().width() // g.cell_size, 3)

    def test_pinch_rubber_bands_at_three_columns(self):
        g = self.w.grid_view
        _app.processEvents()
        g._pinch_begin(QPointF(10, 10))
        base = g._pinch_base.rect(0).width()
        want = g._max_cell() * 2.0                    # roztažení prsty za tři sloupce
        g._pinch_update(want / base)
        top = g._max_cell()
        self.assertAlmostEqual(base * g._pinch_scale, top + (want - top) * 0.3, delta=1.0,
                               msg="za třemi sloupci jen gumový přesah")
        g._pinch_end(animate=False)
        self.assertEqual(g.columns(), 3)


# ── živý náhled Live Photo ───────────────────────────────────────────────────
class TestSharpShortStrips(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "IMG_1.MOV")
        with open(self.src, "wb") as f:
            f.write(b"x")

    def _write(self, header):
        path = os.path.join(self.tmp, "s.scrub")
        h = json.dumps(header).encode()
        with open(path, "wb") as f:
            f.write(scrub_strip.STRIP_MAGIC + bytes([scrub_strip.STRIP_VERSION]) + struct.pack("<I", len(h)) + h)
        os.utime(path, (os.path.getmtime(self.src) + 10,) * 2)
        return path

    def test_short_clips_get_hidpi_edge(self):
        self.assertEqual(scrub_strip.edge_for(3000), scrub_strip.SHORT_EDGE)
        self.assertGreaterEqual(scrub_strip.SHORT_EDGE, 1000, "dlaždice ~900 px na HiDPI bez roztažení")
        self.assertEqual(scrub_strip.edge_for(60000), scrub_strip.STRIP_EDGE, "dlouhá videa: malý pás na tažení")
        self.assertEqual(scrub_strip.edge_for(0), scrub_strip.STRIP_EDGE)

    def test_sharp_command_uses_area_scaling_and_better_jpeg(self):
        cmd = scrub_strip._ffmpeg_cmd("a.mov", False, False, 1, edge=scrub_strip.SHORT_EDGE)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("flags=area", vf)
        self.assertIn(str(scrub_strip.SHORT_EDGE), vf)
        self.assertEqual(cmd[cmd.index("-q:v") + 1], str(scrub_strip.SHORT_QUALITY))
        self.assertIn("fast_bilinear", " ".join(scrub_strip._ffmpeg_cmd("a.mov", True, False, 1)))

    def test_old_blurry_strip_of_short_clip_is_rebuilt(self):
        old = self._write({"duration_ms": 3000, "dense": True, "frames": []})          # bez „edge“ = 480 px
        self.assertFalse(scrub_strip.has_valid_strip(self.src, old))
        self.assertIsNone(scrub_strip.ScrubStrip.load(self.src, old))
        new = self._write({"duration_ms": 3000, "dense": True, "edge": scrub_strip.SHORT_EDGE, "frames": []})
        self.assertTrue(scrub_strip.has_valid_strip(self.src, new))
        self.assertEqual(scrub_strip.ScrubStrip.load(self.src, new).edge, scrub_strip.SHORT_EDGE)
        long_old = self._write({"duration_ms": 60000, "dense": True, "frames": []})
        self.assertTrue(scrub_strip.has_valid_strip(self.src, long_old), "dlouhá videa se nepřestavují")


# ── průběh přípravy videí ────────────────────────────────────────────────────
class TestVideoPrepProgress(unittest.TestCase):

    def test_weighted_by_duration_and_monotonic(self):
        clock = [0.0]
        p = video_proxy.PrepProgress([1000, 9000], clock=lambda: clock[0])
        p.update(0, 1.0)
        self.assertAlmostEqual(p.fraction, 0.1, msg="3s klip je 10 % práce, ne polovina")
        p.update(1, 0.5)
        self.assertAlmostEqual(p.fraction, 0.55)
        p.update(1, 0.2)
        self.assertAlmostEqual(p.fraction, 0.55, msg="průběh necouvá")

    def test_text_has_percent_counter_and_eta_after_warmup(self):
        clock = [0.0]
        p = video_proxy.PrepProgress([60000, 60000], clock=lambda: clock[0])
        text = p.update(0, 0.0)
        self.assertIn("1 z 2", text)
        self.assertIn("0 %", text)
        self.assertNotIn("zbývá", text, "odhad až po zahřátí")
        clock[0] = 30.0
        text = p.update(0, 0.5)                       # 25 % za 30 s -> ještě ~90 s
        self.assertIn("25 %", text)
        self.assertIn("zbývá asi 2 min", text)
        from ui_controls import parse_progress
        self.assertAlmostEqual(parse_progress(text), 0.25, msg="stavový řádek kreslí průběh z textu")

    def test_paused_time_does_not_count_and_is_said_plainly(self):
        clock = [0.0]
        p = video_proxy.PrepProgress([60000], clock=lambda: clock[0])
        p.update(0, 0.0)
        clock[0] = 300.0
        text = p.update(0, 0.1, paused=True)          # 5 minut přehrávání videa
        self.assertIn("pozastavena", text)
        self.assertEqual(p.active_s, 0.0)
        self.assertNotIn("zbývá", text)

    def test_unknown_durations_use_the_average(self):
        p = video_proxy.PrepProgress([0, 4000])
        self.assertEqual(p.weights, [4000, 4000])

    def test_proxy_reports_ffmpeg_progress(self):
        done = [0]
        video_proxy._read_progress(iter([b"frame=10\n", b"out_time_us=1500000\n", b"progress=continue\n"]), done)
        self.assertEqual(done[0], 1500000)


# ── hledání ve videích ───────────────────────────────────────────────────────
def _vec(*hot):
    v = np.zeros(ai_engine.EMBED_DIM, dtype=np.float32)
    for i in hot:
        v[i] = 1.0
    return ai_engine.encode_embedding(v)


class _Model:
    """Textový model: dotaz = osa 7; banka pojmů = osy 100+ (každá fotka k ní stejně)."""
    def encode(self, texts, **kw):
        out = []
        for t in texts:
            v = np.zeros(ai_engine.EMBED_DIM, dtype=np.float32)
            if t in ai_engine.CONCEPT_BANK:
                v[100 + ai_engine.CONCEPT_BANK.index(t) % 300] = 1.0
                v[400] = 0.3
            else:
                v[7] = 1.0
            out.append(v)
        return np.asarray(out)


class TestMultiFrameSearch(unittest.TestCase):

    def setUp(self):
        ai_engine._cached_index_state.update({"count": -1, "matrix": None, "source": None})
        p = mock.patch.object(ai_engine, "get_text_model", return_value=_Model())
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch("search_lexicon.english_for", return_value="")
        q.start()
        self.addCleanup(q.stop)

    def _library(self, video_value):
        lib = {i: _vec(1, 400) for i in range(1, 40)}         # běžné fotky bez dotazovaného pojmu
        lib[50] = _vec(7, 400)                                  # fotka s pojmem
        lib[60] = video_value
        return lib

    def test_frames_rank_video_by_its_best_moment(self):
        poster_only = self._library(_vec(1, 400))
        conf, poss = ai_engine.rank_semantic("duha", None, poster_only)
        self.assertNotIn(60, conf + poss, "plakát pojem nemá")
        frames = self._library((_vec(1, 400), _vec(2, 400), _vec(7, 400), _vec(3, 400)))
        ai_engine._cached_index_state.update({"count": -1, "matrix": None, "source": None})
        conf, poss = ai_engine.rank_semantic("duha", None, frames)
        self.assertIn(60, conf, "duha uprostřed klipu = jistá shoda")
        self.assertIn(50, conf)

    def test_penalty_grows_with_frame_count(self):
        pen = ai_engine.frame_penalty([1, 2, 8])
        self.assertEqual(float(pen[0]), 0.0)
        self.assertGreater(float(pen[2]), float(pen[1]))
        self.assertGreater(ai_engine.SEARCH_FRAME_Z_PENALTY, 0, "kalibrováno proti nepřítomným pojmům")

    def test_index_keeps_rows_of_one_video_together(self):
        lib = {1: _vec(1), 2: (_vec(2), _vec(3), _vec(4)), 3: _vec(5)}
        ai_engine._rebuild_vector_index(lib)
        st = ai_engine._cached_index_state
        self.assertEqual(st["media_ids"], [1, 2, 3])
        self.assertEqual(list(st["starts"]), [0, 1, 4])
        self.assertEqual(list(st["rows_per_item"]), [1, 3, 1])
        self.assertEqual(st["matrix"].shape[0], 5)

    def test_frame_times_skip_shaky_edges(self):
        t = video_frames.frame_times(10000)
        self.assertEqual(len(t), video_frames.FRAMES_MAX)
        self.assertGreaterEqual(t[0], 10000 * video_frames.EDGE_TRIM - 1)
        self.assertLessEqual(t[-1], 10000 * (1 - video_frames.EDGE_TRIM))
        self.assertEqual(len(video_frames.frame_times(1500)), video_frames.FRAMES_SHORT)
        self.assertEqual(video_frames.frame_times(0), [0])

    def test_hdr_original_is_tonemapped_but_proxy_is_not(self):
        hdr = {"hdr": True, "transfer": "arib-std-b67"}
        self.assertIn("tonemap", video_frames._vf(hdr, from_proxy=False))
        self.assertNotIn("tonemap", video_frames._vf(hdr, from_proxy=True), "proxy už je SDR")


class TestVideoFrameStore(unittest.TestCase):

    def setUp(self):
        import database
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "t.db")
        p = mock.patch.object(database, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)
        database.init_db()
        self.database = database
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO media_items (id, file_path, file_name, media_type, file_size, file_mtime) "
                  "VALUES (1, 'a.mov', 'a.mov', 'video', 10, 5), (2, 'b.jpg', 'b.jpg', 'photo', 10, 5)")
        c.execute("INSERT INTO image_embeddings (media_id, embedding) VALUES (1, ?), (2, ?)", (_vec(1), _vec(2)))
        c.commit()
        c.close()

    def test_frames_merge_into_search_vectors(self):
        d = self.database
        todo = d.get_videos_needing_frames()
        self.assertEqual([r[0] for r in todo], [1], "jen videa")
        d.save_video_frame_embeddings(1, [(500, _vec(3)), (900, _vec(4))], todo[0][2])
        self.assertEqual(d.get_videos_needing_frames(), [], "hotové video se nepřepočítává")
        vecs = d.get_search_vectors()
        self.assertIsInstance(vecs[2], bytes)
        self.assertEqual(len(vecs[1]), 3, "plakát + 2 snímky")
        self.assertEqual(vecs[1][0], _vec(1), "plakát první")

    def test_unreadable_video_is_marked_not_retried(self):
        d = self.database
        sig = d.get_videos_needing_frames()[0][2]
        d.save_video_frame_embeddings(1, [], sig)
        self.assertEqual(d.get_videos_needing_frames(), [])
        self.assertIsInstance(d.get_search_vectors()[1], bytes, "značka bez vektoru se do hledání nedostane")

    def test_changed_file_needs_new_frames(self):
        d = self.database
        d.save_video_frame_embeddings(1, [(500, _vec(3))], d.get_videos_needing_frames()[0][2])
        c = sqlite3.connect(self.db)
        c.execute("UPDATE media_items SET file_size = 11 WHERE id = 1")
        c.commit()
        c.close()
        self.assertEqual([r[0] for r in d.get_videos_needing_frames()], [1])


# ── Cesta 2.0 ────────────────────────────────────────────────────────────────
FILM_TRIP = {"id": 1, "title": "Šumava", "location": "", "start": "2026-07-06 09:00", "end": "2026-07-07 18:00",
             "count": 40, "stops": [
                 {"lat": 49.200, "lon": 13.400, "start": "2026-07-06 09:00", "city": "A", "count": 1,
                  "photos": [{"id": 1, "thumb": "", "date": "2026-07-06 09:00"}]},
                 {"lat": 49.212, "lon": 13.430, "start": "2026-07-06 10:30", "city": "B", "count": 40,
                  "photos": [{"id": 2, "thumb": "", "date": "2026-07-06 10:30"},
                             {"id": 3, "thumb": "", "date": "2026-07-06 10:40"}],
                  "best": {"id": 3, "thumb": "", "image": "", "date": "2026-07-06 10:40"}},
                 {"lat": 48.200, "lon": 16.370, "start": "2026-07-07 12:00", "city": "C", "count": 4, "photos": []}]}


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestJourneyFilm(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1400, 900))
        cls.m.wait_style()

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code)

    def tearDown(self):
        self.js("FILM_SPEED = 1; reduceMotion = false; window.endJourney(true); 0")

    def test_plan_is_one_continuous_timeline(self):
        # JSON: převod QtWebEngine vrací None u objektů s undefined (úvod nemá zastávku)
        raw = self.js(f"(function () {{ try {{ return JSON.stringify(getFilmPlan({json.dumps(FILM_TRIP)}).segs.map("
                      "function (s) { return {k: s.kind, i: s.i, t0: s.t0, dur: s.dur, fz: s.from.z, tz: s.to.z,"
                      " fc: s.from.c, tc: s.to.c}; })); } catch (e) { return 'ERR ' + (e.stack || e); } })()")
        self.assertFalse(raw.startswith("ERR"), raw)
        plan = json.loads(raw)
        kinds = [s["k"] for s in plan]
        self.assertEqual(kinds, ["intro", "hop", "dwell", "glide", "dwell", "hop", "dwell", "outro"],
                         "krátký úsek klouže po trase, dlouhý letí obloukem")
        for a, b in zip(plan, plan[1:]):
            self.assertAlmostEqual(a["t0"] + a["dur"], b["t0"], places=6, msg="bez mezer v čase")
            self.assertAlmostEqual(a["tz"], b["fz"], places=6, msg="kamera navazuje (zoom)")
            self.assertAlmostEqual(a["tc"][0], b["fc"][0], places=6, msg="kamera navazuje (místo)")
        dwell = {s["i"]: s["dur"] for s in plan if s["k"] == "dwell"}
        self.assertGreater(dwell[1], dwell[0], "zastávka se 40 fotkami se ukazuje déle než s jednou")

    def test_long_trips_are_compressed(self):
        stops = [{"lat": 49 + k * 0.05, "lon": 13 + (k % 2) * 0.05, "start": "2026-07-06 %02d:00" % (k % 24),
                  "city": str(k), "count": 30, "photos": []} for k in range(24)]
        trip = dict(FILM_TRIP, stops=stops)
        total = self.js(f"getFilmPlan({json.dumps(trip)}).total")
        self.assertLessEqual(total, self.js("FILM_MAX_S") + 1)

    def test_camera_pose_is_continuous_across_segments(self):
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); 0")
        bounds = self.js("film.segs.map(function (s) { return s.t0; })")[1:]
        for t in bounds:
            a, b = self.js(f"[getFilmPose({t} - 0.002), getFilmPose({t} + 0.002)]")
            self.assertLess(abs(a["zoom"] - b["zoom"]), 0.05, (t, a, b))
            self.assertLess(abs(a["center"][0] - b["center"][0]) + abs(a["center"][1] - b["center"][1]), 0.01, (t, a, b))
            self.assertLess(abs(((a["bearing"] - b["bearing"] + 540) % 360) - 180), 1.0, (t, a, b))

    def test_film_look_drops_terrain_and_restores_it(self):
        self.js("map.jumpTo({center: [13.4, 49.2], zoom: 12}); syncTerrain(); 0")
        self.assertEqual(self.js(f"playJourney({json.dumps(FILM_TRIP)}) && map.getPadding().left"), 0,
                         "odsazení najíždí plynule od nuly")
        self.assertFalse(self.js("isTerrainOn()"), "film bez 3D terénu (~20 ms za snímek)")
        self.assertFalse(self.js("terrainAllowed()"))
        self.js("journeySeek(film.dwells[0].t0 + 0.5); 0")
        self.assertGreater(self.js("map.getPadding().left"), 100, "ohnisko vpravo od karty okamžiku")
        self.js("window.endJourney(true); 0")
        self.assertEqual(self.js("map.getPadding().left"), 0)
        self.assertTrue(self.js("terrainAllowed()"))

    def test_card_crossfades_between_stops(self):
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); journeySeek(film.dwells[1].t0 + 0.1); journeyPause(); 0")
        self.assertEqual(self.js("journeyState().card"), 1)
        self.assertEqual(self.js("document.querySelector('#jmoment .moment.show .ph.on').dataset.id"), "3", "best")
        self.js("journeySeek(film.dwells[1].t0 + film.dwells[1].dur * 0.8); journeyPause(); 0")
        self.assertEqual(self.js("document.querySelector('#jmoment .moment.show .ph.on').dataset.id"), "2",
                         "dlouhý okamžik prolne na druhý snímek")
        seg = self.js("(function (s) { return s.t0 + s.dur * 0.3; })"
                      "(film.segs.filter(function (s) { return s.kind === 'hop' && s.i === 2; })[0])")
        self.js(f"journeySeek({seg}); journeyPause(); 0")
        st = self.js("journeyState()")
        self.assertIsNone(st["card"], "během přeletu žádná karta")
        self.assertTrue(self.js("document.getElementById('jcaption').classList.contains('show')"), "kam se letí")
        self.assertEqual(st["place"], "C")

    def test_no_blur_over_the_moving_map(self):
        css = self.js("Array.prototype.map.call(document.querySelectorAll('#jx *'), function (e) {"
                      " return getComputedStyle(e).backdropFilter; }).filter(function (v) { return v && v !== 'none'; })")
        self.assertEqual(css, [], "backdrop-filter nad plátnem WebGL = rozostření celé plochy za snímek")

    def test_resolution_changes_only_while_dwelling(self):
        self.assertTrue(self.js(f"playJourney({json.dumps(FILM_TRIP)})"))
        self.js("journeyPause(); journeySeek(film.segs[1].t0 + 0.5); journeyPause();"
                " res.lastFrame = 1; adaptResolution(100); 0")
        self.assertEqual(self.js("res.lastFrame"), 0, "během přeletu se rozlišení neměří ani nemění")


if __name__ == "__main__":
    unittest.main(verbosity=2)
