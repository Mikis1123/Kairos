"""Kairos – refaktor enginu: video mimo GUI vlákno, posouvání mřížky, kalibrace hledání CLIP,
mapa (obrys pobřeží bez přetečení segmentu, LOD, 2D/3D), Cesta po stezkách + nejlepší snímek,
puls Kairos Spark.

Spuštění (ve složce projektu):   python -m pytest test_engine_refactor.py -v
Testy NESAHAJÍ na síť ani na tvoje photos.db / fotky.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QUrl, QTimer, QElapsedTimer
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import ai_engine
import journey

if HAS_QT:
    from PyQt6.QtGui import QColor, QImage, QPixmap
    from PyQt6.QtMultimedia import QMediaPlayer
    import brand
    import main
    import media_engine
    import motion
    import viewer
    from test_patch11_maps import ShownMapPage, wait, FIXTURE_STYLE
    from test_patch11_media import make_video, media_record, wait_until

PS = QMediaPlayer.PlaybackState if HAS_QT else None
MS = QMediaPlayer.MediaStatus if HAS_QT else None


class StallWatch:
    """Nejdelší mezera mezi tiky 4ms časovače = nejdelší zablokování GUI vlákna."""

    def __init__(self):
        self.clock = QElapsedTimer()
        self.clock.start()
        self.last = 0
        self.worst = 0
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(4)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def _tick(self):
        now = self.clock.elapsed()
        self.worst = max(self.worst, now - self.last)
        self.last = now

    def reset(self):
        self.last = self.clock.elapsed()
        self.worst = 0

    def stop(self):
        self.timer.stop()


# ═════════════════════════════════════════════════════════════════════════════
# 1. Video engine: přehrávač ve vlastním vlákně
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestThreadedPlayerState(unittest.TestCase):
    """Optický stav proxy: příkaz platí hned, zastaralá hlášení z vlákna ho nepřepíšou."""

    def setUp(self):
        self.p = media_engine.ThreadedPlayer()
        self.p._send = mock.Mock(side_effect=lambda *a, **k: setattr(self.p, "_seq", self.p._seq + 1)
                                 if k.get("bump", True) else None)
        self.addCleanup(self.p.shutdown)

    def test_commands_return_immediately_with_intended_state(self):
        self.p.setSource(QUrl.fromLocalFile("C:/x/a.mov"))
        self.assertEqual(self.p.mediaStatus(), MS.LoadingMedia)
        self.p.play()
        self.assertEqual(self.p.playbackState(), PS.PlayingState)
        self.p.pause()
        self.assertEqual(self.p.playbackState(), PS.PausedState, "rychlé přepnutí čte záměr, ne zpožděný stav")

    def test_stale_report_is_ignored(self):
        self.p.setSource(QUrl.fromLocalFile("C:/x/a.mov"))
        self.p.play()
        seq = self.p._seq
        self.p._on_report(seq - 1, PS.StoppedState.value, MS.LoadedMedia.value, 0, 5000)
        self.assertEqual(self.p.playbackState(), PS.PlayingState, "hlášení z doby před novějším příkazem")
        self.assertEqual(self.p.duration(), 0)

    def test_loading_does_not_flicker_to_stopped(self):
        self.p.setSource(QUrl.fromLocalFile("C:/x/a.mov"))
        self.p.play()
        states = []
        self.p.playbackStateChanged.connect(states.append)
        seq = self.p._seq
        self.p._on_report(seq, PS.StoppedState.value, MS.LoadingMedia.value, 0, 0)   # Qt odloží play()
        self.assertEqual(self.p.playbackState(), PS.PlayingState)
        self.p._on_report(seq, PS.PlayingState.value, MS.BufferedMedia.value, 40, 3000)
        self.assertEqual(states, [], "žádné Playing -> Stopped -> Playing")
        self.assertEqual(self.p.duration(), 3000)
        self.p._on_report(seq, PS.StoppedState.value, MS.EndOfMedia.value, 3000, 3000)
        self.assertEqual(self.p.playbackState(), PS.StoppedState, "konec média platí")

    def test_invalid_media_releases_intent(self):
        self.p.setSource(QUrl.fromLocalFile("C:/x/broken.mov"))
        self.p.play()
        self.p._on_report(self.p._seq, PS.StoppedState.value, MS.InvalidMedia.value, 0, 0)
        self.assertEqual(self.p.playbackState(), PS.StoppedState)

    def test_audio_proxy(self):
        self.p.audio.setVolume(0.35)
        self.p.audio.setMuted(True)
        self.assertAlmostEqual(self.p.audio.volume(), 0.35)
        self.assertTrue(self.p.audio.isMuted())


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestVideoEngineNoGuiStall(unittest.TestCase):
    """Skutečné video: otevření, přepnutí i rychlé play/pause bez zamrznutí GUI vlákna."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_engine_")
        cls.a = make_video(os.path.join(cls.tmp, "a.mp4"), seconds=4)
        cls.b = make_video(os.path.join(cls.tmp, "b.mp4"), seconds=3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not (self.a and self.b):
            self.skipTest("ffmpeg není k dispozici")
        self.v = viewer.MediaViewerWidget()
        self.v.resize(900, 600)
        self.v.show()
        self.watch = StallWatch()
        self.addCleanup(self._close)

    def _close(self):
        self.watch.stop()
        self.v.cleanup_playback()
        self.v.hide()
        self.v.deleteLater()
        _app.processEvents()

    def test_players_live_in_worker_threads(self):
        for p in self.v._players:
            self.assertIsInstance(p, media_engine.ThreadedPlayer)
            thread, core = media_engine._live[p._key]
            self.assertIsNot(thread, _app.thread())
            self.assertTrue(thread.isRunning())
        self.assertIs(self.v.player.videoOutput(), self.v.canvas.video_output)

    def test_open_switch_and_toggle_without_freeze(self):
        v = self.v
        v.media_list = [media_record(1, self.a, "video"), media_record(2, self.b, "video")]
        v.current_index = 0
        wait(300)                                       # QML / první snímek okna
        self.watch.reset()
        t0 = time.perf_counter()
        v.load_current_item()
        self.assertLess((time.perf_counter() - t0) * 1000, 60, "otevření se vrací hned")
        self.assertTrue(wait_until(lambda: v.player.duration() > 3000, 6000))
        self.assertTrue(wait_until(lambda: v.player.position() > 200, 4000), "video opravdu hraje")
        for _ in range(8):                              # rychlé přepínání mezerníkem
            v.toggle_play_pause()
            wait(30)
        self.assertEqual(v.player.playbackState(), PS.PlayingState, "sudý počet přepnutí = hraje dál")
        v.show_next()                                   # jiné video: ukončení staré relace ve vlákně
        self.assertTrue(wait_until(lambda: v.player.duration() > 2000 and v.player.duration() < 3600, 6000))
        wait(300)
        self.assertLess(self.watch.worst, 150, f"GUI vlákno zamrzlo na {self.watch.worst} ms")

    def test_shutdown_all_stops_threads(self):
        p = media_engine.ThreadedPlayer()
        key = p._key
        self.assertIn(key, media_engine._live)
        p.shutdown()
        self.assertNotIn(key, media_engine._live)


# ═════════════════════════════════════════════════════════════════════════════
# 2. Mřížka: během posouvání čisté kopírování hotových dlaždic
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestGridScrollFastPath(unittest.TestCase):

    def setUp(self):
        self.host = main.QWidget()
        self.host.resize(900, 640)
        self.addCleanup(self.host.deleteLater)
        self.grid = main.AppleGridView(mock.Mock(), self.host)
        self.grid.setGeometry(0, 0, 900, 640)
        types = ["photo", "live_photo", "video", "photo"]
        recs = [(i, f"p{i}.jpg", f"C:/x/p{i}.jpg", types[i % 4], None, None, "2024-01-01 10:00",
                 1 if i % 5 == 0 else 0) for i in range(200)]
        self.grid.photo_model.set_records(recs)
        for i in range(200):
            img = QImage(360, 270, QImage.Format.Format_RGB32)
            img.fill(QColor(i, 90, 140))
            self.grid.photo_model.ram_cache.put(i, QPixmap.fromImage(img))
        self.host.show()
        _app.processEvents()
        self.d = self.grid.photo_delegate

    def paint(self):
        self.grid.doItemsLayout()
        self.grid.viewport().grab()

    def test_resting_tiles_are_single_blits_with_badges_baked(self):
        self.paint()
        # výjimka uvnitř paint() delegáta = qFatal -> jen počítat volání (wraps), ověřit až potom
        with mock.patch.object(self.d, "_paint_photo", wraps=self.d._paint_photo) as photo, \
                mock.patch.object(self.d, "_paint_badges", wraps=self.d._paint_badges) as badges, \
                mock.patch.object(QPixmap, "scaled", side_effect=lambda *a, **k: QPixmap()) as scaled:
            sb = self.grid.verticalScrollBar()
            sb.setValue(sb.value() + 1)              # stejné dlaždice o pixel jinde
            self.paint()
        self.assertEqual(photo.call_count, 0, "klidová dlaždice se nekreslí znovu")
        self.assertEqual(badges.call_count, 0, "odznaky jsou zapečené v dlaždici")
        self.assertEqual(scaled.call_count, 0)
        self.assertIn("video", {k[6] for k in self.d._tiles}, "druh média je v klíči dlaždice")

    def test_scrolling_suppresses_hover_and_live(self):
        g = self.grid
        g._set_hover_row(3)
        self.assertEqual(g._hover_in_row, 3)
        with mock.patch.object(g.live_preview, "stop") as stop:
            g.verticalScrollBar().setValue(g.verticalScrollBar().value() + 30)
        stop.assert_called()
        self.assertTrue(g.is_scrolling())
        self.assertEqual(g._hover_in_row, -1, "zvednutá dlaždice okamžitě dolů")
        self.assertEqual(g.hover_progress(3), 0.0)
        with mock.patch.object(g.live_preview, "hover") as hover:
            g._set_hover_row(5)                           # entered během posunu
        hover.assert_not_called()
        self.assertEqual(g._hover_in_row, -1)
        from PyQt6.QtCore import QEvent, QPointF
        from PyQt6.QtGui import QHoverEvent
        ev = QHoverEvent(QEvent.Type.HoverMove, QPointF(50, 50), QPointF(50, 50), QPointF(40, 40))
        self.assertTrue(g.viewportEvent(ev), "pohyb myši během posunu se spolkne")

    def test_hover_returns_after_scroll_settles(self):
        g = self.grid
        g.verticalScrollBar().setValue(30)
        self.assertTrue(g.is_scrolling())
        self.assertTrue(wait_until(lambda: not g.is_scrolling(), 1000))
        g._set_hover_row(7)
        self.assertEqual(g._hover_in_row, 7)

    def test_meta_is_cached_and_favorite_invalidates(self):
        m = self.grid.photo_model
        idx = m.index(1, 0)
        first = m.data(idx, m.MetaRole)
        self.assertIs(m.data(idx, m.MetaRole), first, "slovník se nestaví při každém snímku")
        m.set_favorite(1, 1)
        self.assertTrue(m.data(idx, m.MetaRole)["is_fav"])


# ═════════════════════════════════════════════════════════════════════════════
# 3. Hledání: kalibrace na obraz (hubness) – nepřítomný pojem = prázdný výsledek
# ═════════════════════════════════════════════════════════════════════════════

class _FakeTextModel:
    """Deterministické vektory: banka pojmů = náhodné směry, dotazy podle mapy."""

    def __init__(self, dim, queries):
        self.dim, self.queries = dim, queries
        self.rng = np.random.default_rng(3)
        self.cache = {}

    def _vec(self, text):
        if text in self.queries:
            return self.queries[text]
        if text not in self.cache:
            v = self.rng.normal(size=self.dim).astype(np.float32)
            self.cache[text] = v / np.linalg.norm(v)
        return self.cache[text]

    def encode(self, text, **_kw):
        if isinstance(text, (list, tuple)):
            return np.vstack([self._vec(t) for t in text])
        return self._vec(text)


class TestCalibratedSearch(unittest.TestCase):

    def setUp(self):
        ai_engine._bank_cache.update(model=None, vectors=None)
        self.addCleanup(lambda: ai_engine._bank_cache.update(model=None, vectors=None))

    @staticmethod
    def _unit(v):
        return (v / np.linalg.norm(v)).astype(np.float32)

    def library(self, n=80, dim=ai_engine.EMBED_DIM, seed=5):
        """Jako skutečný CLIP: všechny texty sdílí společný směr (mezera modalit). „Hub“ na něm
        leží výrazně -> vysoké skóre ke KAŽDÉMU dotazu včetně „pes“, ale psa neobsahuje."""
        rng = np.random.default_rng(seed)
        unit, rnd = self._unit, lambda: rng.normal(size=dim)
        common = unit(rnd())
        self.dog_part = unit(rnd())
        model = _FakeTextModel(dim, {"pes": unit(0.8 * common + 0.6 * self.dog_part)})
        model.cache = {w: unit(0.8 * common + 0.6 * unit(rnd())) for w in ai_engine.CONCEPT_BANK}
        dog_part = self.dog_part

        def no_dog(v):                                               # fotka bez psa = nulová složka psa
            return v - np.dot(v, dog_part) * dog_part
        imgs = [unit(0.25 * common + unit(no_dog(rnd()))) for _ in range(n)]
        imgs[0] = unit(0.9 * common + 0.45 * unit(no_dog(rnd())))  # hub: podobný všemu
        self.common = common
        return model, self.dog_part, imgs

    def search(self, model, imgs, query):
        emb = {i + 1: ai_engine.encode_embedding(v) for i, v in enumerate(imgs)}
        with mock.patch.object(ai_engine, "get_text_model", return_value=model):
            return [m for m, _ in ai_engine.search_by_text(query, emb)]

    def test_absent_concept_returns_nothing_even_with_hub(self):
        model, dog, imgs = self.library()
        scores = np.array([float(np.dot(v, model.encode("pes"))) for v in imgs])
        self.assertEqual(int(np.argmax(scores)), 0, "hub má nejvyšší surové skóre (starý výběr by ho vrátil)")
        self.assertEqual(self.search(model, imgs, "pes"), [], "hub ani šum nejsou psi")

    def test_true_matches_are_found_and_ranked(self):
        model, dog, imgs = self.library()
        for i, w in ((10, 0.45), (20, 0.6), (30, 0.75)):
            imgs[i] = self._unit(0.25 * self.common + w * dog + 0.7 * imgs[i])
        emb = {i + 1: ai_engine.encode_embedding(v) for i, v in enumerate(imgs)}
        with mock.patch.object(ai_engine, "get_text_model", return_value=model):
            confident, possible = ai_engine.rank_semantic("pes", None, emb)
        self.assertEqual(confident, [31, 21], "silní psi jistě, nejjistější první")
        self.assertEqual((confident + possible)[:3], [31, 21, 11], "slabý pes vede možné shody")
        self.assertNotIn(1, confident + possible, "hub (podobný všemu) ani mezi možnými")

    def test_no_cliff_between_something_and_nothing(self):
        """Dřív: nejlepší z 2,61 pod hranou 2,7 = NIC („voda“). Teď pásma: nad SHOW se něco ukáže."""
        zi = np.array([2.61, 2.0, 0.3], dtype=np.float32)
        zl = np.array([2.0, 1.5, -0.5], dtype=np.float32)
        confident, possible = ai_engine.tier_scores(zi, zl, zi + zl, 152)
        self.assertEqual(confident, [0])
        self.assertEqual(possible, [1])
        nothing = ai_engine.tier_scores(np.array([1.5, 1.0], np.float32), None, np.array([1.5, 1.0]), 152)
        self.assertEqual(nothing, ([], []), "nic nevyčnívá nad SEARCH_Z_SHOW = prázdno")

    def test_degenerate_bank_falls_back_to_library_threshold(self):
        self.assertIsNone(ai_engine.image_concept_stats(np.ones((3, 4), np.float32), np.ones((5, 4), np.float32)))


# ═════════════════════════════════════════════════════════════════════════════
# 5a. Cesta – data: jemné zastávky túry, nejlepší snímek
# ═════════════════════════════════════════════════════════════════════════════

def _shot(i, sharp=100.0, phash=None, fav=False, mtype="photo", lat=49.0, lon=13.0, date="2026-07-06 10:00"):
    return {"id": i, "lat": lat, "lon": lon, "date": date, "city": "", "thumb": f"t{i}",
            "sharpness": sharp, "phash": phash, "fav": fav, "type": mtype}


class TestJourneyCuration(unittest.TestCase):

    def test_sharpest_wins(self):
        self.assertEqual(journey.best_shot([_shot(1, 40), _shot(2, 900), _shot(3, 300)])["id"], 2)

    def test_favorite_beats_sharpness(self):
        self.assertEqual(journey.best_shot([_shot(1, 900), _shot(2, 60, fav=True)])["id"], 2)

    def test_burst_signals_intent_and_sharpest_of_burst_wins(self):
        burst = [_shot(1, 300, "ffff0000ffff0000"), _shot(2, 420, "ffff0000ffff0001"),
                 _shot(3, 380, "ffff0000ffff0003")]
        lone = _shot(4, 500, "0000ffff0000ffff")
        self.assertEqual(journey.best_shot(burst + [lone])["id"], 2, "série 3 záběrů > o kus ostřejší solitér")

    def test_video_loses_to_photo(self):
        self.assertEqual(journey.best_shot([_shot(1, 700, mtype="video"), _shot(2, 500)])["id"], 2)

    def test_hike_gets_stop_per_viewpoint_with_best(self):
        hike = [_shot(i, 100 + i, lat=49.0 + i * 0.006, date=f"2026-07-06 {9 + i // 2:02d}:{i % 2 * 30:02d}")
                for i in range(8)]                    # ~670 m mezi fotkami, ~4,7 km celkem
        stops = journey.build_stops(hike)
        self.assertEqual(len(stops), 8, "túra: zastávka u každé vyhlídky (0,4 km)")
        self.assertTrue(all("best" in s and s["best"]["thumb"] for s in stops))
        self.assertEqual(set(stops[0]["best"]), {"id", "thumb", "date", "image"}, "do JS jen veřejná pole")
        self.assertEqual(stops[0]["best"]["image"], stops[0]["best"]["thumb"], "bez náhledu = miniatura")
        json.dumps(stops)


# ═════════════════════════════════════════════════════════════════════════════
# 4 + 5b. Mapa: pobřeží, LOD, 2D/3D, Cesta po stezkách, nejlepší snímek
# ═════════════════════════════════════════════════════════════════════════════

def _road(coords, cls="path"):
    return {"type": "Feature", "properties": {"class": cls},
            "geometry": {"type": "LineString", "coordinates": coords}}


# Túra: stezka (path) jde oklikou na sever, přímou čáru blokuje jen nic (žádná cesta)
TRAIL_TRIP = {"id": 5, "title": "Túra", "location": "", "start": "2026-07-06 09:00", "end": "2026-07-06 12:00",
              "count": 2, "stops": [
                  {"lat": 49.000, "lon": 13.000, "start": "2026-07-06 09:00", "end": "2026-07-06 09:10",
                   "city": "Start", "count": 1, "photos": [{"id": 1, "thumb": "", "date": "2026-07-06 09:00"}]},
                  {"lat": 49.000, "lon": 13.030, "start": "2026-07-06 10:30", "end": "2026-07-06 10:40",
                   "city": "Vyhlídka", "count": 3,
                   "photos": [{"id": 2, "thumb": "", "date": "2026-07-06 10:30"},
                              {"id": 3, "thumb": "", "date": "2026-07-06 10:35"}],
                   "best": {"id": 3, "thumb": "", "date": "2026-07-06 10:35"}}]}
TRAIL_ROADS = [
    _road([[13.0002, 49.0001], [13.0005, 49.004], [13.015, 49.008]]),
    _road([[13.015, 49.008], [13.025, 49.004], [13.0298, 49.0002]]),
    _road([[12.99, 49.02], [13.04, 49.02]], "motorway"),         # pěšky zakázaná
]


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapEngine(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1000, 700))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def setUp(self):
        self.js("if (journey.active) window.endJourney(true); reduceMotion = false; stopSpin(); 0")
        self.m.reset_view((15.5, 49.8), 1.7)

    def test_coast_is_fill_outline_not_line_over_polygons(self):
        self.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)}); 0")
        self.assertTrue(self.m.wait_style())
        ids = self.js("map.getStyle().layers.map(function (l) { return l.id; })")
        self.assertNotIn("globe-coast", ids, "čára nad polygony vody přetékala 16bit segment")
        self.assertTrue(self.js("map.getPaintProperty('globe-water', 'fill-antialias')"))
        self.assertTrue(self.js("!!map.getPaintProperty('globe-water', 'fill-outline-color')"))
        line_on_water = self.js("map.getStyle().layers.filter(function (l) { return l.type === 'line' && "
                                "l['source-layer'] === 'water' && l.id.indexOf('globe') === 0; }).length")
        self.assertEqual(line_on_water, 0)

    def test_tile_lod_applied_after_style_change(self):
        self.assertEqual(self.js("getTileLod()"), [6, 3])
        self.js("setBaseLayer('outdoor'); 0")
        self.assertTrue(self.m.wait_style())
        self.assertEqual(self.js("getTileLod()"), [6, 3], "nové zdroje po setStyle mají stejný LOD")
        self.js("setBaseLayer('standard'); 0")
        self.m.wait_style()

    def test_tilt_toggle_restores_0_and_52(self):
        self.m.reset_view((13.5, 49.05), 10)
        self.js("reduceMotion = true; 0")
        self.assertEqual(self.js("document.getElementById('btn-tilt').textContent"), "3D")
        self.assertIs(self.js("toggleTilt()"), True)
        self.assertTrue(self.until("Math.abs(map.getPitch() - 52) < 0.5"))
        self.assertTrue(self.until("document.getElementById('btn-tilt').textContent === '2D'"))
        self.js("map.easeTo({bearing: 40, duration: 0}); 0")
        self.assertIs(self.js("toggleTilt()"), False)
        self.assertTrue(self.until("map.getPitch() < 0.5 && Math.abs(map.getBearing()) < 0.5"),
                        "2D = kolmý pohled, sever nahoře")
        self.js("map.easeTo({pitch: 30, duration: 2000}); 0")          # rozjetý pohyb nepřebije přepnutí
        wait(60)
        self.assertIs(self.js("toggleTilt()"), True)
        self.assertTrue(self.until("Math.abs(map.getPitch() - 52) < 0.5"))
        self.js("toggleTilt(); 0")
        self.assertTrue(self.until("map.getPitch() < 0.5"))

    def until(self, code, ms=4000):
        for _ in range(ms // 50):
            if self.js(code):
                return True
            wait(50)
        return False

    def test_long_route_is_split_below_segment_limit(self):
        n = self.js("(function () { var c = []; for (var i = 0; i < 9000; i++) c.push([13 + i * 1e-5, 49]);"
                    " var f = lineFeature(c); return f.geometry.type === 'MultiLineString' ? "
                    "f.geometry.coordinates.map(function (p) { return p.length; }) : [c.length]; })()")
        self.assertGreater(len(n), 1)
        self.assertTrue(all(k * 10 <= 65535 for k in n), "10 vrcholů na bod ≤ 65 535 na kus")

    def test_trail_leg_follows_paths(self):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRAIL_TRIP)})"))
        self.assertEqual(self.js("trailMode(1)"), "foot", "3 km za 80 min = pěšky")
        self.assertTrue(self.js(f"routeTrail(1, {json.dumps(TRAIL_ROADS)})"))
        leg = self.js("getLegPath(1)")
        self.assertEqual(leg[0], [13.0, 49.0], "začíná ve středu zastávky")
        self.assertEqual(leg[-1], [13.03, 49.0])
        self.assertTrue(any(abs(p[0] - 13.015) < 1e-6 and abs(p[1] - 49.008) < 1e-6 for p in leg),
                        "vede přes vrchol stezky, ne pravítkem")
        self.assertFalse(any(abs(p[1] - 49.02) < 1e-6 for p in leg), "dálnice pěšky ne")
        all_coords = self.js("allRouteCoords().length")
        self.assertGreaterEqual(all_coords, len(leg), "celá trasa na mapě přepočtená po stezce")
        self.js("window.endJourney(true); 0")

    def test_trail_falls_back_when_photos_are_far_from_roads(self):
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRAIL_TRIP)})"))
        far = [_road([[13.2, 49.2], [13.3, 49.3]])]
        self.assertFalse(self.js(f"routeTrail(1, {json.dumps(far)})"))
        self.assertFalse(self.js("routeTrail(1, [])"), "bez dlaždic (offline / satelit) = křivka")
        self.assertGreater(len(self.js("getLegPath(1)")), 10, "záloha: hladká křivka")
        self.js("window.endJourney(true); 0")

    def test_fast_leg_is_car_mode(self):
        trip = json.loads(json.dumps(TRAIL_TRIP))
        trip["stops"][1]["start"] = "2026-07-06 09:13"            # 3 km za 3 min
        self.js("reduceMotion = true; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(trip)})"))
        self.assertEqual(self.js("trailMode(1)"), "car")
        self.js("window.endJourney(true); 0")

    def test_stop_shows_single_best_shot(self):
        self.js("POP_MS = 120; HOLD_MS = 2000; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRAIL_TRIP)})"))
        self.js("reduceMotion = true; journeySkip(1); 0")         # zastávka 0
        self.js("reduceMotion = true; journeySkip(1); 0")         # zastávka 1 (3 fotky, best = 3)
        self.assertTrue(self.until("journeyState().polaroids === 1"))
        self.assertEqual(self.js("document.querySelector('#jpol .polaroid').dataset.id"), "3")
        self.assertTrue(self.js("document.querySelector('#jpol .polaroid').classList.contains('hero')"))
        self.js("HOLD_MS = 1700; POP_MS = 700; window.endJourney(true); 0")


# ═════════════════════════════════════════════════════════════════════════════
# 6. Kairos Spark
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSparkPulse(unittest.TestCase):

    def setUp(self):
        from PyQt6.QtWidgets import QVBoxLayout, QWidget
        from ui_controls import SidebarNav
        self.side = QWidget()
        self.side.resize(212, 400)
        lay = QVBoxLayout(self.side)
        self.mark = brand.BrandMark()
        lay.addWidget(self.mark)
        self.nav = SidebarNav([("photos", "Fotografie", "photos"), ("map", "Mapa", "map")])
        lay.addWidget(self.nav)
        lay.addStretch()
        self.pulse = brand.SparkPulse(self.side, self.mark)
        self.side.show()
        _app.processEvents()
        self.addCleanup(self.side.deleteLater)
        self._reduce = motion._user_reduce
        motion.set_reduce_motion(False)
        self.addCleanup(lambda: motion.set_reduce_motion(self._reduce))

    def test_pulse_runs_from_spark_to_tab_and_hides(self):
        if motion.reduced():
            self.skipTest("Windows má vypnuté animace")
        target = self.nav.item("map")
        self.assertTrue(self.pulse.fire(target))
        self.assertTrue(self.pulse.isVisible() and self.pulse.is_running())
        self.assertTrue(self.pulse.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents),
                        "kliknutí projde na záložky")
        spark = self.mark.mapTo(self.side, self.mark.spark_pos().toPoint())
        self.assertLess(abs(self.pulse._a.x() - spark.x()) + abs(self.pulse._a.y() - spark.y()), 2)
        tab = target.mapTo(self.side, target.rect().center())
        self.assertLess(abs(self.pulse._b.y() - tab.y()), 2, "dopadne na řádek záložky")
        self.assertLessEqual(self.pulse.PULSE_MS, 200)
        self.assertTrue(wait_until(lambda: not self.pulse.isVisible(), 800), "po pulsu skrytá (0 kreslení)")
        self.pulse.grab()                                 # kreslení mimo animaci nespadne

    def test_reduced_motion_skips_pulse(self):
        motion.set_reduce_motion(True)
        self.assertFalse(self.pulse.fire(self.nav.item("map")))
        self.assertFalse(self.pulse.isVisible())

    def test_paint_frames(self):
        self.pulse.setGeometry(self.side.rect())
        from PyQt6.QtCore import QPointF
        self.pulse._a, self.pulse._b = self.mark.spark_pos(), QPointF(30, 160)
        for t in (0.0, 0.2, 0.5, 0.8, 1.0):
            self.pulse._t = t
            self.pulse.grab()


if __name__ == "__main__":
    unittest.main(verbosity=2)
