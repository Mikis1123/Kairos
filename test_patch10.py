"""Regresní testy pro Patch 10 – plynulost prohlížeče, portréty osob, rychlé duplicity,
mapové podklady a Fáze 5 (události jako Bento mřížka).

Spuštění (ve složce projektu):   python -m pytest test_patch10.py -v

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db, config.json ani fotky se nepoužijí.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
from PIL import Image

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QUrl, QSize, QRect
    from PyQt6.QtGui import QColor, QPixmap, QImage
    from PyQt6.QtWidgets import QApplication, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import face_engine
import face_cluster

if HAS_QT:
    import main
    import viewer
    import viewer_hud
    import events_view

HERE = os.path.dirname(os.path.abspath(__file__))


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class TempEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p10_test_")
        self._saved = (database.DB_PATH, face_engine.FACE_CACHE_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.faces = os.path.join(self.tmp, "faces")
        os.makedirs(self.faces)
        face_engine.FACE_CACHE_DIR = self.faces
        database.init_db()
        face_cluster.invalidate_centroid_cache()

    def tearDown(self):
        database.DB_PATH, face_engine.FACE_CACHE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def x(self, sql, params=()):
        with database.db_transaction() as conn:
            return conn.execute(sql, params).lastrowid

    def jpg(self, name, size=(640, 480), color=(200, 90, 40)):
        path = os.path.join(self.tmp, name)
        Image.new("RGB", size, color).save(path, "JPEG", quality=90)
        return path

    def blob(self, name):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 128)
        return path

    def add(self, path, media_type="photo", thumb=None, date="2026-07-07 10:00:00", paired=None,
            event=None, sharp=0.0, city=None, trashed=0):
        return self.x("INSERT INTO media_items (file_path, file_name, media_type, thumbnail_path, date_taken, "
                      "paired_video_path, event_id, sharpness_score, place_city, is_trashed) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (path, os.path.basename(path), media_type, thumb, date, paired, event, sharp, city, trashed))


# ═════════════════════════════════════════════════════════════════════════════
# 1. Plynulost prohlížeče
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestImagePipeline(TempEnv):

    def test_decodes_off_thread_and_caches(self):
        path = self.jpg("a.jpg", size=(1200, 900))
        pipe = viewer_hud.ImagePipeline(lambda: QSize(600, 600), capacity=2)
        got = []
        pipe.loaded.connect(lambda p, img: got.append((p, img.width())))
        self.assertIsNone(pipe.request(path), "první požadavek nevrací nic hned – dekóduje se na pozadí")
        self.assertTrue(pipe.is_pending(path))
        pipe.wait()
        self.assertEqual(got, [(path, 600)], "zmenšeno na cílovou velikost")
        self.assertEqual(pipe.request(path).width(), 600, "podruhé z cache")

    def test_lru_eviction_and_dedup(self):
        paths = [self.jpg(f"p{i}.jpg", size=(100, 100)) for i in range(3)]
        pipe = viewer_hud.ImagePipeline(lambda: QSize(50, 50), capacity=2)
        queued = []
        with mock.patch.object(pipe.pool, "start", side_effect=queued.append):
            pipe.prefetch(paths + paths)              # ještě se dekóduje -> podruhé se nespouští
        self.assertEqual(len(queued), 3)
        for job in queued:                            # dokončení v pořadí p0, p1, p2
            job.run()
        _app.processEvents()
        cached = [p for p in paths if pipe.get(p) is not None]
        self.assertEqual(len(cached), 2, "kapacita 2 – jeden snímek vypadl")
        self.assertIsNone(pipe.get(paths[0]), "nejstarší (první dokončený) vypadl")
        self.assertIsNone(pipe.request(os.path.join(self.tmp, "neexistuje.jpg")))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestViewerResponsiveness(TempEnv):

    def setUp(self):
        super().setUp()
        self.big = self.jpg("BIG.jpg", size=(2400, 1800), color=(20, 160, 60))
        thumb = self.jpg("thumb_big.jpg", size=(320, 240), color=(20, 160, 60))
        self.p1 = self.add(self.big, thumb=thumb, date="2026-07-01 10:00:00")
        motion = self.blob("LIVE.MOV")
        self.live = self.add(self.jpg("LIVE.jpg"), "live_photo", paired=motion, date="2026-07-02 10:00:00")
        self.add(motion, "video", date="2026-07-02 10:00:00")
        self.p3 = self.add(self.jpg("P3.jpg"), date="2026-07-03 10:00:00")
        self.records = main.fetch_media_records(order_by="m.date_taken, m.id")
        self.motion = motion
        self.v = viewer.MediaViewerWidget()
        self.v.resize(900, 600)
        self.v.show()
        self.addCleanup(self._close)

    def _close(self):
        self.v.cleanup_playback()
        self.v.hide()
        self.v.deleteLater()

    def test_photo_shows_thumbnail_instantly_then_sharp(self):
        self.v.set_media_data(self.records, 0)
        self.assertEqual(self.v.canvas.current_pixmap().width(), 320, "hned náhled, žádné dekódování v GUI vlákně")
        self.v._images.wait()
        self.assertGreater(self.v.canvas.current_pixmap().width(), 320, "ostrý snímek z pozadí")

    def test_neighbors_are_prefetched(self):
        self.v.set_media_data(self.records, 0)
        self.v._images.wait()
        nxt = self.records[1][2]
        self.assertIsNotNone(self.v._images.get(nxt), "další fotka připravená předem")

    def test_navigation_defers_media_loading(self):
        self.v.set_media_data(self.records, 0)
        self.v.show_next()                       # na Live Photo
        self.assertTrue(self.v._is_live)
        self.assertEqual(self.v.player.source().toLocalFile(), "", "zdroj se při listování nemění hned")
        wait(viewer.MEDIA_LOAD_DELAY_MS + 120)
        self.assertEqual(os.path.normcase(self.v.player.source().toLocalFile()), os.path.normcase(self.motion))

    def test_fast_navigation_skips_intermediate_media(self):
        self.v.set_media_data(self.records, 0)
        with mock.patch.object(self.v.player, "setSource") as set_source:
            self.v.show_next()
            self.v.show_next()                   # Live Photo jen „proletělo“
            wait(viewer.MEDIA_LOAD_DELAY_MS + 120)
        set_source.assert_not_called()

    def test_halt_is_cheap_cleanup_releases(self):
        self.v.set_media_data(self.records, 1)
        with mock.patch.object(self.v.player, "setSource") as set_source:
            self.v._halt_playback()
            set_source.assert_not_called()       # výměna zdroje stojí ~100 ms – při přepnutí ne
            self.v.cleanup_playback()
            set_source.assert_called_once()      # při odchodu z prohlížeče ano

    def test_mini_map_is_created_lazily(self):
        self.assertIsNone(self.v.mini_map, "QWebEngine stránka (~1 s) až když je potřeba")
        self.x("UPDATE media_items SET latitude = 49.2, longitude = 13.5 WHERE id = ?", (self.p1,))
        self.v.set_media_data(main.fetch_media_records(order_by="m.date_taken, m.id"), 0)
        self.v.btn_info.setChecked(True)
        self.v.toggle_info_panel()
        self.assertIsNotNone(self.v.mini_map)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPrewarm(TempEnv):

    def setUp(self):
        super().setUp()
        self.add(self.blob("clip.MOV"), "video")
        patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prewarm_creates_viewer_and_schedules_warmup(self):
        w = main.MainWindow()
        self.addCleanup(lambda: (w.close(), w.deleteLater()))
        w.show()
        with mock.patch.object(main.QTimer, "singleShot") as later:
            w._prewarm_viewer()
        self.assertIsNotNone(w._viewer, "prohlížeč připravený dřív, než ho uživatel otevře")
        later.assert_called_once()

    def test_hidden_window_does_not_prewarm(self):
        w = main.MainWindow()
        self.addCleanup(w.deleteLater)
        w._prewarm_viewer()
        w._start_media_warmup(os.path.join(self.tmp, "clip.MOV"))
        self.assertIsNone(w._viewer)
        self.assertIsNone(w._media_warmup)

    def test_warmup_thread_class(self):
        t = viewer_hud.MediaWarmUp("x.mov")
        self.assertGreater(t.WARM_MS, 0)
        self.assertTrue(isinstance(t, main.QThread) or hasattr(t, "start"))


# ═════════════════════════════════════════════════════════════════════════════
# 2. Reprezentativní portrét osoby
# ═════════════════════════════════════════════════════════════════════════════

def _face_obj(cx, cy, size, nose_dx=0.0, det=0.9):
    """Falešný obličej InsightFace: bbox + 5 bodů (oči, nos, koutky úst)."""
    half = size / 2
    eye = size * 0.18
    kps = np.array([[cx - eye, cy - size * 0.1], [cx + eye, cy - size * 0.1],
                    [cx + nose_dx * 2 * eye, cy + size * 0.05],
                    [cx - eye * 0.8 + nose_dx * 2 * eye, cy + size * 0.25],
                    [cx + eye * 0.8 + nose_dx * 2 * eye, cy + size * 0.25]])
    return types.SimpleNamespace(bbox=np.array([cx - half, cy - half, cx + half, cy + half]), kps=kps, det_score=det)


class TestFrontality(unittest.TestCase):

    def test_frontal_vs_profile(self):
        self.assertGreater(face_engine.frontality_from_kps(_face_obj(100, 100, 80).kps), 0.95)
        self.assertLess(face_engine.frontality_from_kps(_face_obj(100, 100, 80, nose_dx=0.45).kps), 0.2)
        self.assertIsNone(face_engine.frontality_from_kps(None))

    def test_crowded_detection(self):
        main_face = _face_obj(100, 100, 60)
        friend = _face_obj(160, 100, 50)
        tiny = _face_obj(30, 30, 10)
        window = (40, 40, 180)
        self.assertEqual(face_engine._crowded(main_face, [main_face, friend], window), 1)
        self.assertEqual(face_engine._crowded(main_face, [main_face, tiny], window), 0, "malý obličej v pozadí nevadí")
        self.assertEqual(face_engine._crowded(main_face, [main_face], window), 0)


class TestCoverSelection(TempEnv):

    def _crop(self, name):
        path = os.path.join(self.faces, name)
        Image.new("RGB", (64, 64), (128, 128, 128)).save(path)
        return path

    def _vec(self, seed, base=None, noise=0.05):
        rng = np.random.default_rng(seed)
        v = (base if base is not None else rng.normal(size=128)) + noise * rng.normal(size=128)
        return (v / np.linalg.norm(v)).astype(np.float32)

    def test_frontal_beats_sharper_profile_and_crowded(self):
        base = np.random.default_rng(1).normal(size=128)
        faces = [
            (1, self._crop("profile.jpg"), self._vec(2, base), 100.0, 0.3, 0.3, 0.05, 0),   # nejostřejší profil
            (2, self._crop("frontal.jpg"), self._vec(3, base), 70.0, 0.25, 0.25, 0.95, 0),
            (3, self._crop("group.jpg"), self._vec(4, base), 90.0, 0.3, 0.3, 0.95, 1),     # čelně, ale dva lidé
        ]
        self.assertEqual(face_cluster.select_cover_face(faces)[0], 2)

    def test_outlier_identity_is_not_chosen(self):
        """Čelní, ostrý, ale špatně zařazený obličej jiného člověka nesmí vyhrát."""
        base = np.random.default_rng(5).normal(size=128)
        faces = [(i, self._crop(f"m{i}.jpg"), self._vec(10 + i, base), 50.0, 0.2, 0.2, 0.7, 0) for i in range(8)]
        faces.append((99, self._crop("other.jpg"), self._vec(99), 100.0, 0.4, 0.4, 1.0, 0))
        self.assertNotEqual(face_cluster.select_cover_face(faces)[0], 99)

    def test_missing_crop_is_skipped(self):
        base = np.random.default_rng(7).normal(size=128)
        faces = [(1, os.path.join(self.faces, "gone.jpg"), self._vec(1, base), 99.0, 0.3, 0.3, 1.0, 0),
                 (2, self._crop("ok.jpg"), self._vec(2, base), 10.0, 0.1, 0.1, 0.5, 0)]
        self.assertEqual(face_cluster.select_cover_face(faces)[0], 2)

    def _person_with_faces(self, locked=0):
        pid = self.x("INSERT INTO people (name, cover_locked) VALUES ('Eva', ?)", (locked,))
        base = np.random.default_rng(3).normal(size=128)
        ids = []
        for i, front in enumerate((0.1, 0.9, 0.2)):
            mid = self.add(self.jpg(f"e{pid}_{i}.jpg"))
            ids.append(self.x("INSERT INTO face_instances (media_id, person_id, face_crop_path, embedding, "
                              "quality_score, bbox_w, bbox_h, frontality, crowded) VALUES (?, ?, ?, ?, 50, 0.2, 0.2, ?, 0)",
                              (mid, pid, self._crop(f"c{pid}_{i}.jpg"), self._vec(i, base).tobytes(), front)))
        return pid, ids

    def test_refresh_updates_cover_but_respects_lock(self):
        pid, ids = self._person_with_faces()
        self.assertEqual(face_cluster.refresh_person_covers(), 1)
        self.assertEqual(self.q("SELECT key_face_id FROM people WHERE id=?", (pid,))[0][0], ids[1])
        locked, _ = self._person_with_faces(locked=1)
        face_cluster.refresh_person_covers()
        self.assertIsNone(self.q("SELECT key_face_id FROM people WHERE id=?", (locked,))[0][0], "zamčený se nemění")

    def test_forced_cover_locks(self):
        pid, ids = self._person_with_faces()
        face_engine.update_person_key_photo(pid, forced_face_id=ids[0])
        self.assertEqual(self.q("SELECT key_face_id, cover_locked FROM people WHERE id=?", (pid,))[0], (ids[0], 1))
        face_cluster.refresh_person_covers()
        self.assertEqual(self.q("SELECT key_face_id FROM people WHERE id=?", (pid,))[0][0], ids[0])

    def test_analyze_face_crops_backfills(self):
        mid = self.add(self.jpg("x.jpg"))
        front = self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                       (mid, self._crop("f.jpg"), b"\x00" * 16))
        empty = self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                       (mid, self._crop("back.jpg"), b"\x00" * 16))
        fake_app = mock.Mock()
        fake_app.get.side_effect = [[_face_obj(32, 32, 40), _face_obj(58, 32, 30)], []]
        with mock.patch.object(face_engine, "_get_app", return_value=fake_app):
            self.assertEqual(face_engine.count_faces_without_frontality(), 2)
            self.assertEqual(face_engine.analyze_face_crops(), 2)
        f, c = self.q("SELECT frontality, crowded FROM face_instances WHERE id=?", (front,))[0]
        self.assertGreater(f, 0.9)
        self.assertEqual(c, 1, "druhý výrazný obličej v ořezu")
        self.assertEqual(self.q("SELECT frontality FROM face_instances WHERE id=?", (empty,))[0][0], 0.0,
                         "bez obličeje v ořezu (týl hlavy) = 0")
        self.assertEqual(face_engine.count_faces_without_frontality(), 0)

    def test_new_faces_store_frontality(self):
        mid = self.add(self.jpg("n.jpg"))
        database.save_faces_for_media(mid, [{"crop_path": None, "embedding": b"\x00" * 16,
                                             "frontality": 0.8, "crowded": 1}])
        self.assertEqual(self.q("SELECT frontality, crowded FROM face_instances")[0], (0.8, 1))


# ═════════════════════════════════════════════════════════════════════════════
# 3. Duplicity – přednačtení dalších párů
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestDuplicatePrefetch(TempEnv):

    def test_next_pairs_are_sharp_immediately(self):
        ids, paths = [], []
        for i in range(6):
            p = self.jpg(f"d{i}.jpg", size=(2200, 1600), color=(10 * i, 100, 200))
            paths.append(p)
            ids.append(self.add(p, thumb=self.jpg(f"t{i}.jpg", size=(300, 220))))
        pairs = [(ids[0], paths[0], ids[1], paths[1], 1), (ids[2], paths[2], ids[3], paths[3], 1),
                 (ids[4], paths[4], ids[5], paths[5], 1)]
        view = main.DuplicateReviewView(pairs)
        self.addCleanup(view.deleteLater)
        view.wait_for_images()
        for p in paths[2:]:
            self.assertIsNotNone(view._images.get(p), f"přednačteno: {os.path.basename(p)}")
        view._next()
        self.assertTrue(view.lbl_a.full_res and view.lbl_b.full_res, "další pár hned ostrý (z cache)")


# ═════════════════════════════════════════════════════════════════════════════
# 4. Mapové podklady
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapStyles(unittest.TestCase):

    def test_hosts(self):
        hosts = main.ALLOWED_WEB_HOSTS
        self.assertIn("tile.waymarkedtrails.org", hosts)
        self.assertIn("server.arcgisonline.com", hosts)
        self.assertFalse(any("cartocdn" in h for h in hosts), "CARTO vyžaduje API klíč – nepoužívá se")
        self.assertFalse(main.is_allowed_web_url("https://tile.waymarkedtrails.org.evil.com/hiking/1/1/1.png"))

    def test_layer_definitions(self):
        html = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
        for needle in ("World_Street_Map/MapServer", "World_Imagery/MapServer",
                       "Reference/World_Transportation/MapServer", "Reference/World_Boundaries_and_Places/MapServer",
                       "tile.opentopomap.org", "tile.waymarkedtrails.org/hiking", "tile.waymarkedtrails.org/cycling",
                       "waymarkedtrails.org</a> (CC-BY-SA)"):
            self.assertIn(needle, html)
        self.assertNotIn("basemaps.cartocdn.com", html)
        self.assertNotIn("apikey", html.lower())

    def test_legacy_config_values(self):
        self.assertEqual(main.LEGACY_MAP_LAYERS, {"dark": "standard", "topo": "outdoor"})
        self.assertEqual(main.MAP_LAYERS, ("standard", "satellite", "outdoor"))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapPageHybrid(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineUrlRequestInterceptor

        class Block(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                if info.requestUrl().scheme() not in ("file", "qrc", "data"):
                    info.block(True)
        cls.block = Block()
        cls.profile = QWebEngineProfile()
        cls.profile.setUrlRequestInterceptor(cls.block)
        cls.page = QWebEnginePage(cls.profile)
        loop = QEventLoop()
        cls.page.loadFinished.connect(lambda ok: loop.quit())
        QTimer.singleShot(15000, loop.quit)
        cls.page.load(QUrl.fromLocalFile(os.path.join(HERE, "map_vault.html")))
        loop.exec()

    @classmethod
    def tearDownClass(cls):
        cls.page.deleteLater()

    @classmethod
    def js(cls, code):
        loop, out = QEventLoop(), {}
        cls.page.runJavaScript(code, lambda r: (out.__setitem__("r", r), loop.quit()))
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        return out.get("r")

    def test_satellite_is_hybrid(self):
        self.js("setBaseLayer('satellite')")
        urls = self.js("getBaseLayerUrls()")
        self.assertEqual(len(urls), 3)
        self.assertTrue(any("World_Transportation" in u for u in urls), "silnice přes satelit")
        self.assertTrue(any("World_Boundaries_and_Places" in u for u in urls), "hranice a názvy obcí")

    def test_outdoor_has_trails_overlay(self):
        self.js("setBaseLayer('outdoor')")
        urls = self.js("getBaseLayerUrls()")
        self.assertTrue(urls[0].startswith("https://tile.opentopomap.org"))
        self.assertEqual(sum("waymarkedtrails" in u for u in urls), 2, "pěší i cyklo trasy")
        self.assertEqual(self.js("BASE_LAYERS.outdoor.maxZoom"), 17)

    def test_standard_is_light_without_filters(self):
        self.assertEqual(self.js("setBaseLayer('standard')"), "standard")
        self.assertIn("World_Street_Map", self.js("getBaseLayerUrls()")[0])
        self.assertEqual(self.js("getComputedStyle(document.getElementById('map')).filter"), "none")


# ═════════════════════════════════════════════════════════════════════════════
# 5. Události – Bento mřížka
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestEventData(TempEnv):

    def test_date_and_title_formatting(self):
        f = events_view.format_event_dates
        self.assertEqual(f("2026-07-06 10:00", "2026-07-12 18:00"), "6.–12. 7. 2026")
        self.assertEqual(f("2026-07-06", "2026-07-06 23:00"), "6. 7. 2026")
        self.assertEqual(f("2026-06-28", "2026-07-03"), "28. 6. – 3. 7. 2026")
        self.assertEqual(f("2025-12-30", "2026-01-02"), "30. 12. 2025 – 2. 1. 2026")
        self.assertEqual(f(None, None), "")
        s = events_view.split_event_title
        self.assertEqual(s("Výlet: Frauenau a okolí (09. 07. – 12. 07. 2026)"), ("Výlet", "Frauenau a okolí"))
        self.assertEqual(s("Dovolená"), ("", "Dovolená"))
        self.assertEqual(s("Narozeniny (Jana)"), ("", "Narozeniny (Jana)"), "závorka bez čísel zůstává")

    def test_cover_is_sharpest_photo_with_faces(self):
        ev = self.x("INSERT INTO events (title, start_date, end_date) VALUES ('Výlet', '2026-07-01', '2026-07-02')")
        self.add(self.jpg("sharp_landscape.jpg"), event=ev, sharp=100)
        faces = self.add(self.jpg("group.jpg"), event=ev, sharp=80)
        self.add(self.blob("clip.MOV"), "video", event=ev, sharp=500)
        for bx in (0.2, 0.6):
            self.x("INSERT INTO face_instances (media_id, embedding, bbox_x, bbox_y, bbox_w, bbox_h, person_id) "
                   "VALUES (?, ?, ?, 0.3, 0.1, 0.1, NULL)", (faces, b"\x00" * 16, bx))
        conn = database.get_connection()
        try:
            cover = events_view.select_event_cover(conn, ev)
        finally:
            conn.close()
        self.assertEqual(cover["media_id"], faces)
        self.assertAlmostEqual(cover["focus"][0], 0.45, places=2, msg="střed obou obličejů")

    def test_fetch_cards(self):
        old = self.x("INSERT INTO events (title, start_date, end_date) VALUES ('Starý', '2025-05-01', '2025-05-02')")
        new = self.x("INSERT INTO events (title, start_date, end_date) VALUES ('Nový', '2026-07-01', '2026-07-03')")
        empty = self.x("INSERT INTO events (title, start_date, end_date) VALUES ('Koš', '2026-08-01', '2026-08-01')")
        for i, city in enumerate(("Sušice", "Sušice", "Klatovy")):
            self.add(self.jpg(f"n{i}.jpg"), event=new, city=city)
        self.add(self.jpg("o.jpg"), event=old)
        self.add(self.jpg("t.jpg"), event=empty, trashed=1)
        conn = database.get_connection()
        try:
            cards = events_view.fetch_event_cards(conn)
        finally:
            conn.close()
        self.assertEqual([c["id"] for c in cards], [new, old], "nejnovější první, bez prázdných / jen v koši")
        self.assertEqual(cards[0]["location"], "Sušice", "nejčastější místo")
        self.assertEqual(cards[0]["count"], 3)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestBentoLayout(unittest.TestCase):

    def _grid(self, n, width):
        host = QWidget()
        layout = events_view.BentoLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        cards = []
        for i in range(n):
            c = QWidget(host)
            c.span = (1, 1)
            layout.addWidget(c)
            cards.append(c)
        layout.setGeometry(QRect(0, 0, width, layout.heightForWidth(width)))
        self.addCleanup(host.deleteLater)
        return layout, cards

    def test_hero_and_wide_cards_without_overlap(self):
        layout, cards = self._grid(9, 1200)
        self.assertEqual(layout.columns_for(1200), 4)
        self.assertEqual(cards[0].span, (2, 2), "nejnovější = velká karta")
        self.assertEqual(cards[3].span, (2, 1), "rytmus: široká karta")
        geos = layout.placements
        for i in range(len(geos)):
            for j in range(i + 1, len(geos)):
                self.assertFalse(geos[i].intersects(geos[j]), f"karty {i} a {j} se překrývají")
        self.assertGreater(layout.heightForWidth(1200), 0)

    def test_narrow_window_single_column(self):
        layout, cards = self._grid(4, 400)
        self.assertEqual(layout.columns_for(400), 1)
        self.assertTrue(all(c.span == (1, 1) for c in cards))

    def test_focus_crop(self):
        r = events_view.focus_crop(4000, 3000, 800, 400, (0.9, 0.1))
        self.assertAlmostEqual(r.width() / r.height(), 2.0, delta=0.01)
        self.assertEqual(r.y(), 0, "široký výřez se posune nahoru k obličejům")
        r = events_view.focus_crop(4000, 3000, 400, 600, (0.9, 0.5))     # užší karta než fotka
        self.assertAlmostEqual(r.width() / r.height(), 400 / 600, delta=0.01)
        self.assertLessEqual(r.x() + r.width(), 4000)
        self.assertGreater(r.x(), 1000, "posunuto k obličejům vpravo")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestEventsPage(TempEnv):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window(self):
        w = main.MainWindow()
        w.resize(1300, 850)
        w.show()
        self.addCleanup(lambda: (w.close(), w.deleteLater()))
        return w

    def test_empty_state(self):
        w = self._window()
        w._switch_page(2)
        self.assertTrue(w.lbl_events_empty.isVisible())
        self.assertEqual(w.event_cards, [])

    def test_cards_open_collection(self):
        ev = self.x("INSERT INTO events (title, start_date, end_date) VALUES "
                    "('Výlet: Šumava (01. 07. – 03. 07. 2026)', '2026-07-01 08:00', '2026-07-03 20:00')")
        big = self.jpg("cover.jpg", size=(2400, 1600))
        self.add(big, event=ev, thumb=self.jpg("thumb_c.jpg", size=(320, 213)), city="Sušice", sharp=50)
        self.add(self.jpg("b.jpg"), event=ev, city="Sušice")
        w = self._window()
        w._switch_page(2)
        self.assertEqual(len(w.event_cards), 1)
        card = w.event_cards[0]
        self.assertEqual((card.kicker, card.display_title), ("Výlet", "Šumava"))
        self.assertEqual(card.subtitle(), "Sušice · 1.–3. 7. 2026")
        w._event_images.wait()
        self.assertTrue(card.full_res, "obálka v plném rozlišení (náhled hned, ostrá z pozadí)")
        card.clicked.emit(ev)
        page = w.stack.currentWidget()
        self.assertIsInstance(page, main.DetailPage)
        self.assertEqual(page.lbl_title.text(), "Šumava")
        self.assertIn("Sušice", page.lbl_subtitle.text())
        grid = page.findChild(main.AppleGridView)
        self.assertEqual(grid.photo_model.rowCount(), 2)
        self.assertFalse(hasattr(w, "events_list"), "textový seznam nahrazen mřížkou")


if __name__ == "__main__":
    unittest.main(verbosity=2)
