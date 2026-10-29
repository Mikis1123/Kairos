"""Kairos â€“ stabilizaÄŤnĂ­ sprint: mapa (WebGL piny, grafitovĂˇ zemÄ›koule, terĂ©n, klid = 0 snĂ­mkĹŻ,
tichĂˇ konzole), relevance hledĂˇnĂ­, odezva videa, avatary, kontrolnĂ­ listy, Ĺ™azenĂ­, Cesta.

SpuĹˇtÄ›nĂ­ (ve sloĹľce projektu):   python -m pytest test_sprint_stabilization.py -v
Testy NESAHAJĂŤ na sĂ­ĹĄ ani na tvoje photos.db / fotky.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 â€“ pĹ™ed QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import ai_engine
import database
import face_engine
import journey

if HAS_QT:
    import main
    import viewer
    import web_console
    from ui_controls import KButton, MenuSelect
    from test_patch11_maps import ShownMapPage, wait

HERE = os.path.dirname(os.path.abspath(__file__))


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# HledĂˇnĂ­: prĂˇh relevance CLIP
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestSearchRelevance(unittest.TestCase):

    def setUp(self):
        self.rng = np.random.default_rng(7)

    def noise(self, n=400):
        return self.rng.normal(0.20, 0.012, n).astype(np.float32)        # typickĂ© CLIP pĂˇsmo bez shody

    @staticmethod
    def tiers(s):
        """Pásma pro knihovnu bez banky pojmů: z = z-skóre v knihovně (jako rank_semantic)."""
        s = np.asarray(s, dtype=np.float32)
        z = (s - s.mean()) / s.std()
        return ai_engine.tier_scores(z, z, z, s.size)

    def test_unrelated_library_has_no_confident_match(self):
        """„pes“ v knihovně bez psů: šum smí nanejvýš do „možných“, nikdy mezi jisté."""
        confident, possible = self.tiers(self.noise())
        self.assertEqual(confident, [])
        self.assertLessEqual(len(possible), ai_engine.SEARCH_POSSIBLE_MAX)

    def test_true_matches_are_confident_and_ranked(self):
        s = self.noise()
        s[[3, 50, 77]] = [0.29, 0.285, 0.27]
        confident, _ = self.tiers(s)
        self.assertEqual(confident, [3, 50, 77])

    def test_weak_but_outstanding_matches_are_not_lost(self):
        """Mírně vyčnívající shody (dřív pod hranou = nic) jsou aspoň mezi možnými."""
        s = self.noise()
        s[[10, 11]] = [0.232, 0.229]
        confident, possible = self.tiers(s)
        self.assertTrue({10, 11} <= set(confident + possible))

    def test_search_by_text_applies_cutoff_over_whole_library(self):
        dim = ai_engine.EMBED_DIM
        rng = np.random.default_rng(1)
        q = rng.normal(size=dim).astype(np.float32)
        q /= np.linalg.norm(q)
        emb = {}
        for i in range(60):
            v = rng.normal(size=dim).astype(np.float32)
            v -= v.dot(q) * q                                               # kolmĂ© = bez shody
            v /= np.linalg.norm(v)
            if i in (5, 9):
                v = 0.4 * q + 0.9165 * v                                    # skĂłre 0,4
            emb[i] = ai_engine.encode_embedding(v)
        model = mock.Mock()
        model.encode.return_value = q
        with mock.patch.object(ai_engine, "get_text_model", return_value=model):
            res = ai_engine.search_by_text("pes", emb)
        self.assertEqual(sorted(m for m, _ in res), [5, 9])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# TichĂˇ konzole webovĂ˝ch strĂˇnek
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@unittest.skipUnless(HAS_QT, "Qt nenĂ­ dostupnĂ©")
class TestQuietConsole(unittest.TestCase):

    def test_gpu_noise_dropped_and_repeats_limited(self):
        out = io.StringIO()
        page = web_console.QuietWebPage(tag="Mapa", stream=out)
        self.addCleanup(page.deleteLater)
        Level = page.JavaScriptConsoleMessageLevel
        for _ in range(200):
            page.javaScriptConsoleMessage(Level.WarningMessageLevel,
                                          "[.WebGL-0x1]GL performance warning: READ-usage buffer was written, "
                                          "then fenced, but written again before being read back.", 0, "")
            page.javaScriptConsoleMessage(Level.WarningMessageLevel, "calculateFogMatrix is not supported on "
                                          "globe projection.", 1, "maplibre-gl.js")
            page.javaScriptConsoleMessage(Level.ErrorMessageLevel, "WebGL: too many errors, no more errors will "
                                          "be reported to the console for this context.", 0, "")
            page.javaScriptConsoleMessage(Level.ErrorMessageLevel, "SkuteÄŤnĂˇ chyba", 5, "map_vault.html")
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), web_console.MAX_REPEATS + 1, lines)
        self.assertTrue(all("SkuteÄŤnĂˇ chyba" in l for l in lines))
        self.assertGreater(page.suppressed, 600)

    def test_chromium_native_logging_quiet_and_pages_use_quiet_page(self):
        self.assertIn("--log-level=3", os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        self.assertIn('QuietWebPage(profile, self.map_view, tag="Mapa")', src)
        self.assertIn("QuietWebPage", open(os.path.join(HERE, "viewer.py"), encoding="utf-8").read())


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Video: slouÄŤenĂ© skoky, ĹľĂˇdnĂ© pĹ™ednaÄŤtenĂ­ pĹ™i prĂˇci s videem
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@unittest.skipUnless(HAS_QT, "Qt nenĂ­ dostupnĂ©")
class TestVideoResponsiveness(unittest.TestCase):

    def fake(self, playing=False):
        from PyQt6.QtCore import QTimer
        from PyQt6.QtMultimedia import QMediaPlayer
        player = mock.Mock()
        player.duration.return_value = 60000
        player.position.return_value = 5000
        player.playbackState.return_value = (QMediaPlayer.PlaybackState.PlayingState if playing
                                             else QMediaPlayer.PlaybackState.PausedState)
        v = types.SimpleNamespace(player=player, _seek_target=None, _seek_timer=QTimer(),
                                  _preload_timer=mock.Mock(), _set_timeline=mock.Mock(),
                                  _scrubber=types.SimpleNamespace(active=False), _sync_play_icon=mock.Mock(),
                                  can_preload=lambda: True, media_list=[1, 2], canvas=mock.Mock())
        v._seek_timer.setSingleShot(True)
        v._seek_timer.setInterval(viewer.SEEK_COALESCE_MS)
        v._flush_seek = lambda: viewer.MediaViewerWidget._flush_seek(v)
        v._seek_timer.timeout.connect(v._flush_seek)
        v._schedule_preload = lambda d=viewer.PRELOAD_DELAY_MS: viewer.MediaViewerWidget._schedule_preload(v, d)
        return v

    def test_rapid_skips_coalesce_into_one_seek(self):
        v = self.fake()
        for _ in range(5):
            viewer.MediaViewerWidget.skip_seconds(v, 10000)
        v.player.setPosition.assert_not_called()
        self.assertEqual(v._seek_target, 55000)
        v._set_timeline.assert_called_with(55000, 60000)          # osa ukazuje cĂ­l hned
        wait(viewer.SEEK_COALESCE_MS + 80)
        v.player.setPosition.assert_called_once_with(55000)

    def test_play_flushes_pending_seek_without_new_source(self):
        v = self.fake()
        viewer.MediaViewerWidget.skip_seconds(v, -10000)
        viewer.MediaViewerWidget.toggle_play_pause(v)
        v.player.setPosition.assert_called_once_with(0)
        v.player.play.assert_called_once()
        v.player.setSource.assert_not_called()

    def test_pause_defers_preload_and_scrub_never_preloads(self):
        from PyQt6.QtMultimedia import QMediaPlayer
        v = self.fake()
        viewer.MediaViewerWidget._on_playback_state(v, QMediaPlayer.PlaybackState.PausedState)
        v._preload_timer.start.assert_called_once_with(viewer.PAUSED_PRELOAD_DELAY_MS)
        v._preload_timer.reset_mock()
        v._scrubber.active = True                                    # pauza od taĹľenĂ­ ÄŤasovĂ© osy
        viewer.MediaViewerWidget._on_playback_state(v, QMediaPlayer.PlaybackState.PausedState)
        v._preload_timer.start.assert_not_called()
        viewer.MediaViewerWidget._on_playback_state(v, QMediaPlayer.PlaybackState.PlayingState)
        v._preload_timer.stop.assert_called()
        self.assertGreater(viewer.PAUSED_PRELOAD_DELAY_MS, 2000)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Avatary: protaĹľenĂ© oĹ™ezy z videĂ­
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestVideoFaceCropRepair(unittest.TestCase):

    def setUp(self):
        import cv2
        self.cv2 = cv2
        self.tmp = tempfile.mkdtemp(prefix="omni_sprint_")
        self._saved = (database.DB_PATH, face_engine.FACE_CACHE_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.faces = os.path.join(self.tmp, "faces")
        os.makedirs(self.faces)
        face_engine.FACE_CACHE_DIR = self.faces
        database.init_db()

    def tearDown(self):
        database.DB_PATH, face_engine.FACE_CACHE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def frame(self):
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        yy, xx = np.ogrid[:400, :600]
        img[(xx - 300) ** 2 + (yy - 40) ** 2 <= 30 * 30] = 255             # â€žobliÄŤejâ€ś u hornĂ­ho okraje
        img[(xx - 300) ** 2 + (yy - 200) ** 2 <= 20 * 20] = 255            # obliÄŤej uprostĹ™ed
        return img

    def video(self):
        path = os.path.join(self.tmp, "clip.mp4")
        w = self.cv2.VideoWriter(path, self.cv2.VideoWriter_fourcc(*"mp4v"), 10, (600, 400))
        if not w.isOpened():
            self.skipTest("OpenCV neumĂ­ zapsat mp4")
        for _ in range(30):
            w.write(self.frame())
        w.release()
        return path

    def aspect(self, path):
        arr = face_engine.cv_io.imread(path)[:, :, 0]
        ys, xs = np.nonzero(arr > 128)
        return (xs.max() - xs.min()) / (ys.max() - ys.min())

    def test_old_stretched_video_crop_is_recut_and_correct_one_kept(self):
        if not os.path.exists(face_engine.FFMPEG_PATH):
            self.skipTest("ffmpeg nenĂ­ k dispozici")
        video = self.video()
        img = self.frame()
        edge_bbox, centre_bbox = (270, 10, 330, 70), (280, 180, 320, 220)
        old = os.path.join(self.faces, "edge.jpg")
        good = os.path.join(self.faces, "centre.jpg")
        face_engine.cv_io.imwrite(old, face_engine._legacy_avatar_crop(img, edge_bbox))
        face_engine.cv_io.imwrite(good, face_engine._extract_apple_avatar_crop(img, centre_bbox))
        self.assertGreater(abs(self.aspect(old) - 1.0), 0.12, "starĂ˝ oĹ™ez je opravdu protaĹľenĂ˝")
        before_good = open(good, "rb").read()
        with database.db_transaction() as conn:
            mid = conn.execute("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?, ?, 'video')",
                               (video, "clip.mp4")).lastrowid
            for crop, (x1, y1, x2, y2) in ((old, edge_bbox), (good, centre_bbox)):
                conn.execute("INSERT INTO face_instances (media_id, face_crop_path, embedding, bbox_x, bbox_y, "
                             "bbox_w, bbox_h, crop_version) VALUES (?, ?, ?, ?, ?, ?, ?, 2)",
                             (mid, crop, b"\x00" * 16, x1 / 600, y1 / 400, (x2 - x1) / 600, (y2 - y1) / 400))
        self.assertEqual(face_engine.repair_stretched_face_crops(), 1)
        self.assertAlmostEqual(self.aspect(old), 1.0, delta=0.1, msg="kruh zase kruhem")
        self.assertEqual(open(good, "rb").read(), before_good, "sprĂˇvnĂ˝ oĹ™ez se nepĹ™episuje")
        conn = database.get_connection()
        try:
            versions = {r[0] for r in conn.execute("SELECT crop_version FROM face_instances")}
        finally:
            conn.close()
        self.assertEqual(versions, {face_engine.CROP_VERSION})
        self.assertEqual(face_engine.repair_stretched_face_crops(), 0, "jednorĂˇzovÄ›")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# UI: kontrolnĂ­ listy, Ĺ™azenĂ­
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@unittest.skipUnless(HAS_QT, "Qt nenĂ­ dostupnĂ©")
class TestKairosControls(unittest.TestCase):

    def test_menu_select_is_combo_compatible(self):
        sel = MenuSelect(["NejnovÄ›jĹˇĂ­", "NejstarĹˇĂ­", "NĂˇzev Aâ€“Z", "Velikost"], icon="sort")
        self.addCleanup(sel.deleteLater)
        got = []
        sel.currentIndexChanged.connect(got.append)
        self.assertEqual((sel.count(), sel.currentIndex(), sel.currentText()), (4, 0, "NejnovÄ›jĹˇĂ­"))
        sel.setCurrentIndex(2)
        sel.setCurrentIndex(2)                                   # beze zmÄ›ny = bez signĂˇlu
        self.assertEqual(got, [2])
        self.assertEqual(sel.currentText(), "NĂˇzev Aâ€“Z")
        self.assertEqual(sel.sizeHint().height(), MenuSelect.HEIGHT)
        w = sel.sizeHint().width()
        sel.setCurrentIndex(3)
        self.assertEqual(sel.sizeHint().width(), w, "ĹˇĂ­Ĺ™ka podle nejdelĹˇĂ­ volby â€“ neskĂˇÄŤe")

    def test_review_sheets_use_kairos_buttons(self):
        dup = main.DuplicateReviewView([])
        self.addCleanup(dup.deleteLater)
        for b in (dup.btn_keep_a, dup.btn_keep_b, dup.btn_keep_both, dup.btn_skip):
            self.assertIsInstance(b, KButton)
            self.assertEqual(b.styleSheet(), "", "ĹľĂˇdnĂ© sytĂ© QSS plochy")
        self.assertEqual(dup.btn_keep_a.variant(), "primary")
        with mock.patch.object(main.BorderlineReviewView, "_load_face"):
            bl = main.BorderlineReviewView(1, "Eva", [])
        self.addCleanup(bl.deleteLater)
        self.assertEqual(bl.btn_yes.variant(), "primary")
        self.assertEqual(bl.btn_no.variant(), "danger_outline")
        self.assertEqual(bl.btn_skip.variant(), "ghost")
        src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
        self.assertNotIn("background:#30D158", src)
        self.assertNotIn('self.btn_no.setStyleSheet("background:#FF453A', src)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Cesta: zastĂˇvky nesou pĹ™Ă­jezd a odjezd
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class TestJourneyEntryExit(unittest.TestCase):

    def test_stops_carry_first_and_last_photo_position(self):
        p = lambda i, lat, lon, t: {"id": i, "lat": lat, "lon": lon, "date": t, "city": "", "thumb": ""}
        # (do 0,4 km = jedna zastávka i s nejmenším poloměrem túry)
        stops = journey.build_stops([p(1, 49.240, 13.400, "2026-07-06 08:00"), p(2, 49.242, 13.402, "2026-07-06 09:00"),
                                     p(3, 49.170, 13.580, "2026-07-06 12:00")])
        self.assertEqual(stops[0]["entry"], {"lat": 49.24, "lon": 13.4})
        self.assertEqual(stops[0]["exit"], {"lat": 49.242, "lon": 13.402})
        json.dumps(stops)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# StrĂˇnka mapy (skuteÄŤnĂ˝ MapLibre, sĂ­ĹĄ zablokovanĂˇ)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

TRIP = {"id": 9, "title": "Ĺ umava", "location": "", "start": "2023-07-06 08:00", "end": "2023-07-07 18:00",
        "count": 6, "stops": [
            {"lat": 49.200, "lon": 13.400, "start": "2023-07-06 09:00", "city": "A", "count": 2, "photos": [],
             "entry": {"lat": 49.199, "lon": 13.399}, "exit": {"lat": 49.205, "lon": 13.430}},
            {"lat": 49.230, "lon": 13.520, "start": "2023-07-06 12:00", "city": "B", "count": 2, "photos": [],
             "entry": {"lat": 49.215, "lon": 13.500}, "exit": {"lat": 49.230, "lon": 13.520}},
            {"lat": 48.200, "lon": 16.370, "start": "2023-07-07 12:00", "city": "VĂ­deĹ", "count": 2, "photos": []}]}


@unittest.skipUnless(HAS_QT, "Qt nenĂ­ dostupnĂ©")
class TestMapPage(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1000, 700))
        cls.thumb_dir = tempfile.mkdtemp(prefix="omni_sprint_thumb_")
        from PyQt6.QtGui import QImage, QColor
        img = QImage(40, 30, QImage.Format.Format_RGB32)
        img.fill(QColor("#20A040"))
        cls.thumb = os.path.join(cls.thumb_dir, "t.png")
        img.save(cls.thumb)

    @classmethod
    def tearDownClass(cls):
        cls.m.close()
        shutil.rmtree(cls.thumb_dir, ignore_errors=True)

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def setUp(self):
        self.js("if (journey.active) window.endJourney(true); reduceMotion = false; stopSpin(); 0")
        self.m.reset_view((15.5, 49.8), 1.7)

    def until(self, code, ms=4000):
        for _ in range(ms // 50):
            if self.js(code):
                return True
            wait(50)
        return False

    def test_pins_are_webgl_symbols_with_thumbnail(self):
        url = QUrl.fromLocalFile(self.thumb).toString()
        markers = [{"id": 1, "lat": 49.2, "lon": 13.4, "name": "a", "city": "", "date": "", "thumb": url},
                   {"id": 2, "lat": 50.08, "lon": 14.43, "name": "b", "city": "", "date": "", "thumb": ""},
                   {"id": 3, "lat": 50.081, "lon": 14.431, "name": "c", "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)}); 0")
        self.m.reset_view((14.0, 49.6), 7)
        self.assertEqual(self.js("document.querySelectorAll('.maplibregl-marker').length"), 0, "ĹľĂˇdnĂ© HTML znaÄŤky")
        st = self.js("getPinState()")
        self.assertTrue(st["layer"])
        self.assertEqual(self.js("getPinCount()"), 2)
        self.assertTrue(self.until("getPinState().keys.some(function (k) { return k.indexOf('t.png') >= 0; })"),
                        "nĂˇhled se nakreslil do obrĂˇzku pinu (plĂˇtno nezamÄŤenĂ©)")
        self.assertTrue(any(k.endswith(self.js("accent()")) and "|2|" in k for k in self.js("getPinState().keys")),
                        "shluk dvou fotek: odznak poÄŤtu ve zvĂ˝raznÄ›nĂ­")
        self.assertTrue(self.js("map.hasImage(getPinState().keys[0])"))
        # vĂ˝mÄ›na podkladu (setStyle) piny nesmaĹľe
        self.js("setBaseLayer('outdoor'); 0")
        self.assertTrue(self.m.wait_style())
        self.assertTrue(self.until("getPinState().layer && map.hasImage(getPinState().keys[0])"))
        self.js("setBaseLayer('standard'); 0")
        self.m.wait_style()

    def test_globe_is_dark_graphite_and_landscape_unchanged(self):
        from test_patch11_maps import FIXTURE_STYLE
        self.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)}); 0")
        self.assertTrue(self.m.wait_style())
        layers = self.js("map.getStyle().layers.map(function (l) { return [l.id, l.type, l.minzoom || 0, l.maxzoom || 24]; })")
        by = {l[0]: l for l in layers}
        for gid in ("globe-land", "globe-water", "globe-border"):
            self.assertIn(gid, by)
            self.assertLessEqual(by[gid][3], 6, "tmavĂˇ zemÄ›koule jen do z6")
        self.assertIn("relief", by, "stĂ­novĂˇnĂ­ reliĂ©fu v krajinÄ›")
        self.assertEqual(self.js("map.getPaintProperty('globe-land', 'background-color')"), "#22262D")
        liberty = [l for l in layers if not l[0].startswith(("globe-", "base-", "pins", "relief"))]
        self.assertTrue(all(l[2] >= 5 for l in liberty), "svÄ›tlĂ˝ podklad aĹľ pod tmavou zemÄ›koulĂ­")
        self.js("setBaseLayer('satellite'); 0")
        self.assertTrue(self.m.wait_style())
        self.assertEqual(self.js("map.getStyle().layers[0].id"), "base-bg", "pĂłly bez ÄŤernĂ© dĂ­ry")
        self.js("setBaseLayer('standard'); 0")
        self.m.wait_style()

    def test_terrain_never_with_globe_rendering(self):
        self.m.reset_view((13.5, 49.05), 11)
        self.js("map.fire('moveend'); 0")
        self.assertTrue(self.until("isTerrainOn()"))
        self.js("holdTerrain(); 0")
        self.assertFalse(self.js("isTerrainOn()"), "pĹ™ed dlouhĂ˝m letem bez terĂ©nu")
        self.js("map.fire('moveend'); 0")
        self.assertTrue(self.until("isTerrainOn()"), "v cĂ­li zase terĂ©n")
        self.m.reset_view((13.5, 49.05), 5)
        self.js("map.fire('zoom'); 0")
        self.assertFalse(self.js("isTerrainOn()"))

    def test_idle_map_renders_zero_frames(self):
        self.js("SPIN_MAX_MS = 60000; lastAct = Date.now() + 1e9; stopSpin(); 0")
        self.m.reset_view((15.5, 49.8), 1.7)
        settled = False
        for _ in range(40):                       # globe: mÄ›Ĺ™enĂ­ chyby projekce se ustĂˇlĂ­
            f0 = self.js("renderStats().frames")
            wait(500)
            if self.js("renderStats().frames") == f0:
                settled = True
                break
        self.assertTrue(settled, "neÄŤinnĂˇ zemÄ›koule pĹ™estane kreslit")
        f0 = self.js("renderStats().frames")
        wait(1500)
        self.assertEqual(self.js("renderStats().frames"), f0, "0 snĂ­mkĹŻ za sekundu v klidu")
        self.js("lastAct = Date.now(); 0")

    def test_route_is_smooth_through_real_entry_and_exit(self):
        self.js("reduceMotion = true; 0")
        res = self.js(f"(function () {{ try {{ return playJourney({json.dumps(TRIP)}); }} "
                      f"catch (e) {{ return String(e && e.stack || e); }} }})()")
        self.assertIs(res, True, res)
        leg = self.js("getLegPath(1)")
        self.assertGreater(len(leg), 10, "kĹ™ivka, ne ĂşseÄŤka")

        def near(p, q):
            return abs(p[0] - q[0]) < 1e-6 and abs(p[1] - q[1]) < 1e-6
        self.assertTrue(any(near(p, [13.430, 49.205]) for p in leg), "trasa vede pĹ™es odjezd")
        self.assertTrue(any(near(p, [13.500, 49.215]) for p in leg), "a pĹ™es pĹ™Ă­jezd")
        flight = self.js("getLegPath(2)")
        mid = flight[len(flight) // 2]
        chord = [(13.52 + 16.37) / 2, (49.23 + 48.2) / 2]
        self.assertGreater(len(flight), 3)
        self.assertGreater(abs(mid[1] - chord[1]), 0.005, "dlouhĂ˝ let po hlavnĂ­ kruĹľnici (oblouk)")
        self.assertLess(self.js("hopZoom(300)"), self.js("hopZoom(30)"), "dĂˇl = vĂ˝Ĺˇ")
        self.assertGreater(self.js("prefetchStop(2)"), 0, "dlaĹľdice cĂ­le ve frontÄ› pĹ™ednaÄŤtenĂ­")
        self.js("window.endJourney(true); 0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
