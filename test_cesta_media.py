"""Cesta 3.0 – pohyb v kartách (Live Photo / 3s video, card_media.py) a hudební podkres (C6,
ambient_music.py).

Testy NESAHAJÍ na síť ani na photos.db: klip se vyrobí z umělého videa (ffmpeg testsrc),
stránka mapy má síť zablokovanou.
"""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

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

import numpy as np

import ambient_music
import card_media
import journey
import paths
import video_proxy

if HAS_QT:
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage, wait
    from test_phase2_film import FILM_TRIP

HAS_FFMPEG = os.path.exists(paths.FFMPEG_PATH)


def make_video(path, seconds=6, size="1280x720"):
    subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size={size}:rate=30:duration={seconds}", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", path], check=True, creationflags=video_proxy._no_window())


class TestHighlightPlanning(unittest.TestCase):

    def test_start_keeps_window_inside_video(self):
        self.assertEqual(card_media.highlight_start(2500), 0)                 # Live Photo: celý klip
        self.assertEqual(card_media.highlight_start(20000, 10000), 8500)      # 3 s kolem středu
        self.assertEqual(card_media.highlight_start(20000, 500), 0)
        self.assertEqual(card_media.highlight_start(20000, 19900), 17000)
        self.assertEqual(card_media.highlight_start(20000), 4500)             # bez snímků: 30 % délky

    def test_representative_frame_is_closest_to_mean(self):
        a, b = np.array([1, 0, 0], np.float32), np.array([0, 1, 0], np.float32)
        mid = np.array([0.7, 0.7, 0], np.float32)
        self.assertEqual(card_media.representative_ms([(100, a), (900, mid), (1700, b)]), 900)
        self.assertIsNone(card_media.representative_ms([]))

    def test_hero_carries_media_type(self):
        hero = journey._hero({"id": 5, "thumb": "t", "date": "d", "type": "live_photo"})
        self.assertEqual(hero["type"], "live_photo")
        self.assertEqual(journey._hero({"id": 6, "thumb": "t", "date": "d"})["type"], "photo")

    def test_plan_picks_live_pair_and_video_highlight(self):
        tmp = tempfile.mkdtemp(prefix="cards_")
        self.addCleanup(shutil.rmtree, tmp, True)
        live_mov, vid = os.path.join(tmp, "IMG_1.MOV"), os.path.join(tmp, "clip.mp4")
        for p in (live_mov, vid):
            open(p, "wb").close()
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE media_items (id INTEGER PRIMARY KEY, file_path TEXT, paired_video_path TEXT);
            CREATE TABLE video_frame_embeddings (media_id INTEGER, frame_ms INTEGER, embedding BLOB);
        """)
        conn.execute("INSERT INTO media_items VALUES (1, ?, ?), (2, ?, NULL), (3, 'x.jpg', NULL)",
                     (os.path.join(tmp, "IMG_1.HEIC"), live_mov, vid))
        frames = [(1000, [1, 0]), (8000, [0.7, 0.7]), (15000, [0, 1])]
        conn.executemany("INSERT INTO video_frame_embeddings VALUES (2, ?, ?)",
                         [(ms, np.array(v, np.float32).tobytes()) for ms, v in frames])
        data = {"stops": [{"best": {"id": 1, "type": "live_photo"}}, {"best": {"id": 3, "type": "photo"}},
                          {"best": {"id": 2, "type": "video"}}]}
        plan = card_media.plan(conn, data)
        self.assertEqual([(c["stop"], c["kind"], c["source"], c["centre_ms"]) for c in plan],
                         [(0, "live_photo", live_mov, None), (2, "video", vid, 8000)])   # nejbližší průměru
        self.assertFalse(any(c["ready"] for c in plan))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg není k dispozici")
class TestHighlightBuild(unittest.TestCase):

    def test_builds_short_muted_vp9_webm(self):
        tmp = tempfile.mkdtemp(prefix="cards_build_")
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "src.mp4")
        make_video(src)
        item = {"source": src, "kind": "video", "centre_ms": 3000}
        out = card_media.build(item)
        self.assertTrue(out and out.endswith(".webm") and os.path.getsize(out) > 0)
        self.assertEqual(card_media.get_highlight(src), out)                    # druhé volání z cache
        info = subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-i", out], capture_output=True,
                              creationflags=video_proxy._no_window()).stderr.decode("utf-8", "replace")
        self.assertIn("vp9", info)
        self.assertNotIn("Audio:", info)
        dur = video_proxy.probe(out)["duration_ms"]
        self.assertTrue(2700 <= dur <= 3300, dur)
        self.assertLessEqual(max(video_proxy.probe(out)["width"], video_proxy.probe(out)["height"]),
                             card_media.HIGHLIGHT_EDGE)

    def test_clip_worker_reports_each_ready_clip(self):
        tmp = tempfile.mkdtemp(prefix="cards_worker_")
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "a.mp4")
        make_video(src, seconds=2)
        got = []
        w = card_media.ClipWorker([{"stop": 4, "source": src, "kind": "live_photo", "ready": False},
                                   {"stop": 5, "source": src, "kind": "live_photo", "ready": True}],
                                  lambda item, path: got.append((item["stop"], path)))
        w.start()
        w.join(60)
        self.assertEqual([g[0] for g in got], [4])                              # hotový se nestaví znovu


class TestAmbientMusic(unittest.TestCase):

    def test_bundled_tracks_exist_and_are_opus(self):
        tracks = ambient_music.tracks()
        self.assertGreaterEqual(len(tracks), 2)
        self.assertLessEqual(len(tracks), 4)
        for t in tracks:
            self.assertTrue(t["title"])
            self.assertGreater(os.path.getsize(t["path"]), 500_000)
            if HAS_FFMPEG:
                info = subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-i", t["path"]], capture_output=True,
                                      creationflags=video_proxy._no_window()).stderr.decode("utf-8", "replace")
                self.assertIn("opus", info)
                self.assertIsNotNone(video_proxy._DURATION.search(info))

    def test_music_is_opt_in_and_packaged(self):
        import config
        self.assertIs(config.DEFAULT_CONFIG["cesta_music"], False)
        spec = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.spec"), encoding="utf-8").read()
        self.assertIn("os.path.join('assets', 'music')", spec)


@unittest.skipUnless(HAS_QT and HAS_FFMPEG, "Qt / ffmpeg není dostupné")
class TestCardsAndMusicInPage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWebEngineCore import QWebEngineSettings
        cls.tmp = tempfile.mkdtemp(prefix="cards_page_")
        src = os.path.join(cls.tmp, "live.mp4")
        make_video(src, seconds=3)
        cls.clip = QUrl.fromLocalFile(card_media.build({"source": src, "kind": "live_photo"})).toString()
        cls.m = ShownMapPage((1400, 900))
        cls.m.page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        cls.m.wait_style()
        cls.m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
        cls.m.wait_style()
        cls.m.js("setPerfProfile({tier: 'high'}); 0")

    @classmethod
    def tearDownClass(cls):
        cls.m.close()
        shutil.rmtree(cls.tmp, True)

    def js(self, code):
        return self.m.js(code)

    def setUp(self):
        self.js("FILM_SPEED = 1; reduceMotion = false; setPerfProfile({tier: 'high'}); "
                "if (journey.active) window.endJourney(true); 0")

    def tearDown(self):
        self.js("window.endJourney(true); setJourneyMusic({enabled: false}); 0")

    def trip(self, clip=True):
        t = copy.deepcopy(FILM_TRIP)
        t["stops"][1]["best"]["type"] = "live_photo"
        if clip:
            t["stops"][1]["best"]["clip"] = self.clip
        return t

    def seek_dwell(self, i, u=0.2):
        self.js(f"journeySeek(film.dwells[{i}].t0 + film.dwells[{i}].dur * {u}); 0")

    def wait_for(self, expr, ms=4000):
        for _ in range(ms // 100):
            if self.js(expr):
                return True
            wait(100)
        return False

    def test_clip_plays_on_arrival_then_returns_to_photo(self):
        self.assertTrue(self.js(f"playJourney({json.dumps(self.trip())})"))
        self.seek_dwell(1, 0.0)
        wait(150)
        self.assertTrue(self.wait_for("(getClipState().card || {}).on === true"), self.js("getClipState()"))
        self.assertTrue(self.wait_for("(getClipState().card || {}).done === true", 6000), self.js("getClipState()"))
        self.assertFalse(self.js("getClipState().card.on"), "po doběhnutí zpět fotka (Ken Burns)")

    def test_pause_freezes_clip(self):
        self.js(f"playJourney({json.dumps(self.trip())}); 0")
        self.seek_dwell(1, 0.0)
        self.assertTrue(self.wait_for("(getClipState().card || {}).on === true"))
        self.js("togglePause(); 0")
        wait(100)
        self.assertTrue(self.js("getClipState().card.paused"))
        self.js("togglePause(); 0")
        self.assertTrue(self.wait_for("getClipState().card.paused === false || getClipState().card.done"))

    def test_clip_arriving_mid_film_attaches_to_open_card(self):
        self.js(f"playJourney({json.dumps(self.trip(clip=False))}); 0")
        self.seek_dwell(1, 0.0)
        wait(100)
        self.assertIsNone(self.js("getClipState().card"))
        self.assertTrue(self.js(f"journeyClip(1, 1, {json.dumps(self.clip)})"))
        self.assertFalse(self.js(f"journeyClip(999, 1, {json.dumps(self.clip)})"), "cizí Cesta se ignoruje")
        self.assertTrue(self.wait_for("(getClipState().card || {}).on === true"))

    def test_no_clips_on_lower_tier_or_reduced_motion(self):
        self.js("setPerfProfile({tier: 'standard'}); 0")
        self.js(f"playJourney({json.dumps(self.trip())}); 0")
        self.seek_dwell(1, 0.0)
        wait(150)
        self.assertIsNone(self.js("getClipState().card"))
        self.js("window.endJourney(true); setPerfProfile({tier: 'high'}); reduceMotion = true; 0")
        self.js(f"playJourney({json.dumps(self.trip())}); 0")
        self.seek_dwell(1, 0.0)
        wait(150)
        self.assertIsNone(self.js("getClipState().card"))
        self.js("reduceMotion = false; 0")

    def test_music_opt_in_fades_with_journey(self):
        track = QUrl.fromLocalFile(ambient_music.tracks()[0]["path"]).toString()
        self.assertEqual(self.js(f"setJourneyMusic({{enabled: false, tracks: [{{title: 'A', url: {json.dumps(track)}}}]}})"), 1)
        self.assertFalse(self.js("document.getElementById('jmusic').hidden"))
        self.js(f"playJourney({json.dumps(self.trip())}); 0")
        wait(300)
        self.assertFalse(self.js("getMusicState().playing"), "výchozí stav: bez hudby")
        self.js("document.getElementById('jmusic').click(); 0")                 # zapnout tlačítkem
        self.assertTrue(self.wait_for("getMusicState().playing && getMusicState().volume > 0.05"),
                        self.js("getMusicState()"))
        self.assertEqual(self.js("getMusicState().target"), 0.5)
        self.js("togglePause(); 0")
        self.assertEqual(self.js("getMusicState().target"), 0)
        self.assertTrue(self.wait_for("!getMusicState().playing", 3000), "pauza = doznění a stop")
        self.js("togglePause(); 0")
        self.assertTrue(self.wait_for("getMusicState().playing"))
        self.js("window.endJourney(true); 0")
        self.assertEqual(self.js("getMusicState().target"), 0)
        self.assertTrue(self.wait_for("!getMusicState().playing", 3000))


if __name__ == "__main__":
    unittest.main()
