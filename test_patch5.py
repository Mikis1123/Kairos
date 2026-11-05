"""Regresní testy pro Patch 5 – druhá dávka kritických oprav z auditu (OmniPhoto Vault).

Spuštění (ve složce projektu):   python -m pytest test_patch5.py -v

Pokrývá:
  C5 – Mapa: skutečné fotky s GPS z SQLite (R*Tree / query_geo_bounds), správné id a
       miniatura v pinech, most pin -> open_viewer_by_id, shlukování, žádné nepovolené
       vzdálené požadavky (MapLibre přibalený lokálně i v minimapě, jen povolené mapové servery).
  C7 – Duplicity: pHash + ostrost pro nové importy, přepočet zastaralých otisků,
       vyloučení pohybových videí Live Photos, akce dialogu (koš místo odebrání srdíčka),
       "Ponechat obě" se pamatuje, Zpět.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import inspect
import json
import os
import random
import re
import shutil
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageFilter

# ── Qt inicializace PŘED WebEngine importy (vzor z test_patch3.py) ────────────
HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import scanner
import cache_tools
import ai_engine

if HAS_QT:
    import main

HERE = os.path.dirname(os.path.abspath(__file__))


def _scene(path, seed=0, blur=0.0, size=(360, 270)):
    """Deterministická "fotka" z tvarů; stejný seed = stejná scéna."""
    rng = random.Random(seed)
    im = Image.new("RGB", size, tuple(rng.randint(0, 255) for _ in range(3)))
    d = ImageDraw.Draw(im)
    for _ in range(14):
        x0, y0 = rng.randint(0, size[0]), rng.randint(0, size[1])
        x1, y1 = x0 + rng.randint(20, 160), y0 + rng.randint(20, 120)
        fill = tuple(rng.randint(0, 255) for _ in range(3))
        (d.ellipse if rng.random() < 0.5 else d.rectangle)([x0, y0, x1, y1], fill=fill)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    im.save(path, "JPEG", quality=92)
    return path


class TempEnv(unittest.TestCase):
    """Izolované prostředí: vlastní DB, miniatury a média v dočasné složce."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p5_test_")
        self.thumbs = os.path.join(self.tmp, ".cache", "thumbnails")
        os.makedirs(self.thumbs)
        self._saved = (database.DB_PATH, scanner.CACHE_DIR, cache_tools.THUMB_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        scanner.CACHE_DIR = self.thumbs
        cache_tools.THUMB_DIR = self.thumbs
        database.init_db()

    def tearDown(self):
        database.DB_PATH, scanner.CACHE_DIR, cache_tools.THUMB_DIR = self._saved
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

    def _add(self, path, media_type="photo", date="2026-07-07 10:00:00", paired=None,
             lat=None, lon=None, thumb=None, fav=0, size=None):
        return self.x(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, paired_video_path, "
            "latitude, longitude, thumbnail_path, is_favorite, file_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, os.path.basename(path), media_type, date, paired, lat, lon, thumb, fav, size))

    def _thumb_for(self, media_id, seed):
        path = os.path.join(self.thumbs, f"thumb_{media_id}.jpg")
        _scene(path, seed)
        self.x("UPDATE media_items SET thumbnail_path=? WHERE id=?", (path, media_id))
        return path

    def _photo(self, name, seed, blur=0.0, **kw):
        return self._add(_scene(os.path.join(self.tmp, name), seed, blur), **kw)

    def _video(self, name, seed, **kw):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"\x00" * 64)
        media_id = self._add(path, media_type="video", **kw)
        self._thumb_for(media_id, seed)
        return media_id

    def _pairs(self):
        conn = database.get_connection()
        try:
            return ai_engine.get_duplicate_pairs(conn)
        finally:
            conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# C5 – MAPA
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapMarkers(TempEnv):
    """C5 – Data pinů pocházejí z R*Tree indexu a nesou správné id + miniaturu."""

    def test_build_markers_maps_columns(self):
        thumb = _scene(os.path.join(self.tmp, "t.jpg"), 1)
        rows = [(7, 49.25, 13.40, "IMG_7.HEIC", "Sušice", "2026-07-06 12:18:14", thumb, "photo")]
        m = main.build_map_markers(rows)[0]
        self.assertEqual((m["id"], m["lat"], m["lon"]), (7, 49.25, 13.40))
        self.assertEqual((m["name"], m["city"], m["date"]), ("IMG_7.HEIC", "Sušice", "2026-07-06 12:18"))
        self.assertTrue(m["thumb"].startswith("file:///"), "miniatura = file URL, ne název souboru")
        self.assertEqual(os.path.normcase(QUrl(m["thumb"]).toLocalFile()), os.path.normcase(thumb))

    def test_invalid_coordinates_skipped(self):
        rows = [(1, None, 13.0, "a", "", "", None, "photo"),
                (2, 95.0, 13.0, "b", "", "", None, "photo"),
                (3, 0.0, 0.0, "c", "", "", None, "photo"),
                (4, "x", 1.0, "d", "", "", None, "photo"),
                (5, float("nan"), 1.0, "e", "", "", None, "photo"),
                (6, 50.1, 14.4, "f", None, None, "/neexistuje.jpg", "photo")]
        markers = main.build_map_markers(rows)
        self.assertEqual([m["id"] for m in markers], [6])
        self.assertEqual(markers[0]["thumb"], "", "chybějící miniatura = prázdný řetězec")

    def test_geo_pipeline_excludes_motion_clips_and_trash(self):
        """DB -> sync_geo_index -> query_geo_bounds -> piny: bez pohybových videí a koše."""
        motion = os.path.join(self.tmp, "IMG_2.MOV")
        photo = self._photo("IMG_1.jpg", 1, lat=49.24, lon=13.52)
        live = self._photo("IMG_2.jpg", 2, lat=49.16, lon=13.57, paired=motion)
        self._add(motion, "video", lat=49.16, lon=13.57)
        trashed = self._photo("IMG_3.jpg", 3, lat=49.0, lon=14.0)
        self._photo("IMG_4.jpg", 4)                     # bez GPS
        thumb = self._thumb_for(photo, 1)
        database.trash_media([trashed])                 # synchronizuje R*Tree
        markers = main.build_map_markers(database.query_geo_bounds(-90, 90, -180, 180))
        self.assertEqual(sorted(m["id"] for m in markers), sorted([photo, live]))
        by_id = {m["id"]: m for m in markers}
        self.assertEqual(os.path.normcase(QUrl(by_id[photo]["thumb"]).toLocalFile()),
                         os.path.normcase(thumb))
        database.restore_media([trashed])
        ids = {m["id"] for m in main.build_map_markers(database.query_geo_bounds(-90, 90, -180, 180))}
        self.assertIn(trashed, ids, "obnovená fotka se vrátí na mapu")


class _FakeMainWindow:
    def __init__(self):
        self.opened, self.opened_many = [], []

    def open_viewer_by_id(self, item_id):
        self.opened.append(item_id)

    def open_viewer_for_ids(self, ids):
        self.opened_many.append(ids)


def _wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapBridge(unittest.TestCase):
    """C5 – Most JS -> Python předá správné id do open_viewer_by_id."""

    def test_open_photo_passes_id(self):
        fake = _FakeMainWindow()
        main.MapBridge(fake).openPhotoFromMap(42)
        _wait(120)
        self.assertEqual(fake.opened, [42])

    def test_open_cluster_passes_ids(self):
        fake = _FakeMainWindow()
        bridge = main.MapBridge(fake)
        bridge.openPhotosFromMap("[5, 3, 9]")
        bridge.openPhotosFromMap("nesmysl")
        bridge.openPhotosFromMap("17")
        _wait(120)
        self.assertEqual(fake.opened_many, [[5, 3, 9]])


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestWebPrivacy(unittest.TestCase):
    """C5 – Poloha fotek nesmí odcházet na nepovolené servery."""

    def test_url_policy(self):
        allowed = ["file:///C:/OPV/map_vault.html", "qrc:///qtwebchannel/qwebchannel.js",
                   "data:image/png;base64,AAAA", "https://tiles.openfreemap.org/styles/dark",
                   "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/1/1/1"]
        blocked = ["https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
                   "https://mt0.google.com/vt/lyrs=y&x=1&y=2&z=3",
                   "https://tile.openstreetmap.org/1/1/1.png",      # minimapa už OSM dlaždice nebere
                   "http://tiles.openfreemap.org/styles/dark",
                   "https://tiles.openfreemap.org.evil.com/styles/dark",
                   "https://www.google.com/maps/search/?api=1&query=49.2,13.4"]
        for url in allowed:
            self.assertTrue(main.is_allowed_web_url(url), url)
        for url in blocked:
            self.assertFalse(main.is_allowed_web_url(url), url)

    def test_map_page_has_no_external_assets(self):
        html = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
        self.assertNotIn("unpkg.com", html)
        self.assertNotIn("google", html.lower())
        # Batch 6: mapa = přibalený MapLibre GL
        self.assertIn('src="vendor/maplibre/maplibre-gl.js"', html)
        self.assertIn('href="vendor/maplibre/maplibre-gl.css"', html)
        self.assertIn("qrc:///qtwebchannel/qwebchannel.js", html)
        for needle in ("window.setMarkers", "openPhotoFromMap", "openPhotosFromMap"):
            self.assertIn(needle, html)

    def test_leaflet_is_gone_maplibre_is_the_only_map_stack(self):
        """Fix 1B: Leaflet není přibalený ani bundlovaný; minimapa = stejný MapLibre jako mapa."""
        self.assertFalse(os.path.exists(os.path.join(HERE, "vendor", "leaflet")))
        spec = open(os.path.join(HERE, "app.spec"), encoding="utf-8").read()
        self.assertNotIn("leaflet", spec.lower())
        self.assertNotIn("mini_map.html", spec, "Sprint 3: the mini-map is native (mini_map.py)")
        import viewer
        src = inspect.getsource(viewer.MediaViewerWidget)
        self.assertNotIn("leaflet", src.lower())
        self.assertNotIn("tile.openstreetmap.org", src)

    def test_viewer_minimap_is_native_and_uses_allowed_tiles(self):
        """Sprint 3: no second web page – Qt composites tiles of an allowed host."""
        self.assertFalse(os.path.exists(os.path.join(HERE, "mini_map.html")))
        src = open(os.path.join(HERE, "mini_map.py"), encoding="utf-8").read()
        for bad in ("leaflet", "unpkg.com", "tile.openstreetmap.org", "import QWebEngine", "PyQt6.QtWebEngine"):
            self.assertNotIn(bad, src, bad)
        import mini_map
        self.assertIn(mini_map.TILE_HOST, main.ALLOWED_WEB_HOSTS)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapPageEndToEnd(unittest.TestCase):
    """C5 – Skutečná stránka v QWebEngine: piny, shluky a klik -> správné id."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWebEngineCore import (QWebEngineProfile, QWebEnginePage,
                                           QWebEngineUrlRequestInterceptor, QWebEngineSettings)
        from PyQt6.QtWebChannel import QWebChannel

        class Recorder(QWebEngineUrlRequestInterceptor):
            """Zaznamená vše, co stránka chce načíst; síť v testu zakáže."""
            def __init__(self):
                super().__init__()
                self.urls = []

            def interceptRequest(self, info):
                url = info.requestUrl()
                self.urls.append(url.toString())
                if url.scheme() not in ("file", "qrc", "data"):
                    info.block(True)

        cls.recorder = Recorder()
        cls.profile = QWebEngineProfile()          # off-the-record, nesdílí nic s aplikací
        cls.profile.setUrlRequestInterceptor(cls.recorder)
        cls.page = QWebEnginePage(cls.profile)
        cls.page.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        cls.fake = _FakeMainWindow()
        cls.bridge = main.MapBridge(cls.fake)
        cls.channel = QWebChannel()
        cls.channel.registerObject("bridge", cls.bridge)
        cls.page.setWebChannel(cls.channel)

        loop = QEventLoop()
        cls.loaded = []
        cls.page.loadFinished.connect(lambda ok: (cls.loaded.append(ok), loop.quit()))
        QTimer.singleShot(15000, loop.quit)
        cls.page.load(QUrl.fromLocalFile(os.path.join(HERE, "map_vault.html")))
        loop.exec()
        for _ in range(50):                      # QWebChannel se inicializuje asynchronně
            if cls.js("bridge !== null"):
                break
            _wait(100)

    @classmethod
    def tearDownClass(cls):
        cls.page.deleteLater()
        _wait(50)

    @classmethod
    def js(cls, code, timeout=5000):
        loop, out = QEventLoop(), {}

        def done(result):
            out["r"] = result
            loop.quit()
        cls.page.runJavaScript(code, done)
        QTimer.singleShot(timeout, loop.quit)
        loop.exec()
        return out.get("r")

    def test_page_loads_with_local_maplibre(self):
        self.assertEqual(self.loaded, [True])
        self.assertEqual(self.js("typeof maplibregl"), "object", "MapLibre se musí načíst z vendor/")
        self.assertTrue(self.js("bridge !== null"), "QWebChannel most musí být připojen")

    def test_only_local_and_osm_requests(self):
        markers = [{"id": 1, "lat": 49.2, "lon": 13.4, "name": "a", "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        _wait(300)
        remote = [u for u in self.recorder.urls if not u.startswith(("file:", "qrc:", "data:"))]
        self.assertTrue(all(main.is_allowed_web_url(u) for u in remote),
                        f"nepovolené požadavky: {[u for u in remote if not main.is_allowed_web_url(u)]}")

    def test_markers_render_and_cluster(self):
        near = [{"id": i, "lat": 49.2 + i * 1e-5, "lon": 13.4, "name": f"IMG_{i}",
                 "city": "Sušice", "date": "", "thumb": ""} for i in range(1, 6)]
        far = [{"id": 99, "lat": 50.08, "lon": 14.43, "name": "IMG_99", "city": "Praha",
                "date": "", "thumb": ""}]
        self.assertEqual(self.js(f"setMarkers({json.dumps(near + far)})"), 6)
        self.assertEqual(self.js("computeClusters(7).length"), 2, "5 blízkých fotek = 1 shluk, Praha zvlášť")
        self.assertEqual(self.js("setMarkers([])"), 0)
        self.assertEqual(self.js("document.getElementById('empty').style.display"), "flex",
                         "prázdná mapa ukáže prázdný stav")

    def test_hidden_tab_defers_fit(self):
        """Stránka bez velikosti (skrytá záložka): piny se vykreslí, přiblížení se odloží."""
        self.assertFalse(self.js("hasSize()"), "testovací stránka nemá view = nulová velikost")
        self.assertFalse(self.js("isMapCreated()"), "skrytá stránka nevytváří WebGL mapu")
        markers = [{"id": 1, "lat": 49.2, "lon": 13.4, "name": "a", "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.assertFalse(self.js("isFitted()"))
        self.assertEqual(self.js("getClusterCount()"), 1)

    def test_pin_click_opens_correct_id(self):
        self.fake.opened.clear()
        markers = [{"id": 2563, "lat": 49.24, "lon": 13.52, "name": "IMG_1543.HEIC",
                    "city": "Sušice", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.js("clickCluster(0)")
        for _ in range(30):
            if self.fake.opened:
                break
            _wait(50)
        self.assertEqual(self.fake.opened, [2563])

    def test_names_are_escaped(self):
        """Název souboru se do HTML pinu nevkládá jako kód."""
        markers = [{"id": 5, "lat": 49.0, "lon": 13.0, "name": "<img src=x onerror=alert(1)>",
                    "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        tip = self.js("getClusters()[0].card")
        self.assertNotIn("<img", tip)
        self.assertIn("&lt;img", tip)


# ═════════════════════════════════════════════════════════════════════════════
# C7 – DUPLICITY
# ═════════════════════════════════════════════════════════════════════════════

class TestPhashPipeline(TempEnv):
    """C7 – Otisky se spočítají a spolehlivě uloží; zastaralé se přepočítají."""

    def test_new_items_get_phash_and_sharpness(self):
        a = self._photo("a.jpg", 1)
        v = self._video("v.MOV", 2)
        self.assertEqual(ai_engine.refresh_phashes(), 2)
        for media_id in (a, v):
            phash, sharp, ver = self.q("SELECT phash, sharpness_score, phash_version FROM media_items "
                                       "WHERE id=?", (media_id,))[0]
            self.assertRegex(phash, r"^[0-9a-f]{16}$")
            self.assertGreater(sharp, 0)
            self.assertEqual(ver, ai_engine.PHASH_VERSION)
        self.assertEqual(ai_engine.refresh_phashes(), 0, "druhý průchod nemá co dělat")

    def test_stale_legacy_hash_is_recomputed(self):
        """Starý otisk bez verze (příčina kolizí) se nahradí otiskem skutečného obrázku."""
        a = self._photo("a.jpg", 1)
        b = self._photo("b.jpg", 2)
        self.x("UPDATE media_items SET phash='e7a0d893a3fa3c09', sharpness_score=2737.42, "
               "phash_version=NULL")
        ai_engine.refresh_phashes()
        ha, hb = (self.q("SELECT phash FROM media_items WHERE id=?", (i,))[0][0] for i in (a, b))
        self.assertNotEqual(ha, hb)
        self.assertNotEqual(ha, "e7a0d893a3fa3c09")
        self.assertEqual(ha, ai_engine.compute_phash_and_sharpness(os.path.join(self.tmp, "a.jpg"))[0])
        self.assertEqual(self._pairs(), [], "různé fotky už nejsou duplicity")

    def test_foreign_thumbnail_is_not_used(self):
        """Miniatura s cizím id (stará chyba přečíslování) se pro otisk nepoužije."""
        a = self._photo("a.jpg", 1)
        foreign = _scene(os.path.join(self.thumbs, "thumb_999.jpg"), 7)
        original = os.path.join(self.tmp, "a.jpg")
        self.assertEqual(ai_engine._phash_source(a, original, foreign, "photo"), original)
        self.assertIsNone(ai_engine._phash_source(a, "/neni.MOV", foreign, "video"))

    def test_changed_file_invalidates_hash(self):
        a = self._photo("a.jpg", 1, size=100)
        ai_engine.refresh_phashes()
        path = os.path.join(self.tmp, "a.jpg")
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), {"file_path": path, "file_name": "a.jpg",
                                                       "file_size": 555, "media_type": "photo"})
        self.assertIsNone(self.q("SELECT phash_version FROM media_items WHERE id=?", (a,))[0][0])
        self.assertEqual(ai_engine.refresh_phashes(), 1)

    def test_unchanged_file_keeps_hash(self):
        a = self._photo("a.jpg", 1, size=100)
        ai_engine.refresh_phashes()
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), {"file_path": os.path.join(self.tmp, "a.jpg"),
                                                       "file_name": "a.jpg", "file_size": 100,
                                                       "media_type": "photo"})
        self.assertEqual(self.q("SELECT phash_version FROM media_items WHERE id=?", (a,))[0][0],
                         ai_engine.PHASH_VERSION)

    def test_import_pipeline_computes_hashes(self):
        """Nový import (MediaScannerThread) zapíše otisky bez restartu aplikace."""
        lib = os.path.join(self.tmp, "Fotky")
        os.makedirs(lib)
        for i in range(3):
            _scene(os.path.join(lib, f"IMG_{i}.jpg"), i)
        s = scanner.MediaScannerThread(lib)
        s.exiftool_path = os.path.join(self.tmp, "chybi_exiftool.exe")   # hermeticky bez ExifTool
        s._run_pipeline()
        rows = self.q("SELECT phash, phash_version FROM media_items")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(p and v == ai_engine.PHASH_VERSION for p, v in rows), rows)


class TestDuplicateDetection(TempEnv):
    """C7 – Detekce: skutečné duplicity ano, pohybová videa / koš / foto-vs-video ne."""

    def test_true_duplicate_detected_sharpest_first(self):
        blurry = self._photo("IMG_1_blur.jpg", 5, blur=1.2, date="2026-07-07 10:00:00")
        sharp = self._photo("IMG_1.jpg", 5, date="2026-07-07 10:00:05")
        self._photo("IMG_2.jpg", 6, date="2026-07-07 10:00:10")
        ai_engine.refresh_phashes()
        pairs = self._pairs()
        self.assertEqual(len(pairs), 1)
        id_a, _, id_b, _, hamming = pairs[0]
        self.assertEqual((id_a, id_b), (sharp, blurry), "A = ostřejší snímek (doporučeno ponechat)")
        self.assertLessEqual(hamming, 3)

    def test_motion_clips_excluded(self):
        """Pohybová videa Live Photos se do duplicit nedostanou (dřív 5 z 9 skupin)."""
        m1, m2 = (os.path.join(self.tmp, n) for n in ("IMG_10.MOV", "IMG_11.MOV"))
        self._photo("IMG_10.jpg", 1, media_type="live_photo", paired=m1, date="2026-07-07 10:00:00")
        self._photo("IMG_11.jpg", 2, media_type="live_photo", paired=m2, date="2026-07-07 10:00:04")
        c1 = self._video("IMG_10.MOV", 9, date="2026-07-07 10:00:00")
        c2 = self._video("IMG_11.MOV", 9, date="2026-07-07 10:00:04")   # stejný obraz
        ai_engine.refresh_phashes()
        self.assertEqual(self._pairs(), [])
        flagged = {r[0] for r in self.q("SELECT id FROM media_items WHERE is_duplicate=1")}
        self.assertFalse(flagged & {c1, c2})
        hashed = {r[0] for r in self.q("SELECT id FROM media_items WHERE phash_version IS NOT NULL")}
        self.assertFalse(hashed & {c1, c2}, "pohybová videa se ani nehashují")

    def test_photo_and_video_not_compared(self):
        self._photo("IMG_1.jpg", 3)
        self._video("IMG_2.MOV", 3)          # miniatura videa = stejný obraz jako fotka
        ai_engine.refresh_phashes()
        self.assertEqual(self._pairs(), [])

    def test_standalone_video_duplicates_still_found(self):
        self._video("A.MOV", 4, date="2026-07-07 10:00:00")
        self._video("B.MOV", 4, date="2026-07-07 10:00:30")
        ai_engine.refresh_phashes()
        self.assertEqual(len(self._pairs()), 1)

    def test_trashed_and_dismissed_excluded(self):
        a = self._photo("a.jpg", 5)
        b = self._photo("b.jpg", 5, date="2026-07-07 10:00:01")
        c = self._photo("c.jpg", 5, date="2026-07-07 10:00:02")
        ai_engine.refresh_phashes()
        self.assertEqual(len(self._pairs()), 2)
        database.trash_media([c])
        self.assertEqual(len(self._pairs()), 1)
        database.dismiss_duplicate_pair(b, a)            # pořadí id nehraje roli
        self.assertEqual(self._pairs(), [])
        database.undismiss_duplicate_pair(a, b)
        self.assertEqual(len(self._pairs()), 1)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestDuplicateDialogActions(TempEnv):
    """C7 – Tlačítka dialogu: koš místo odebrání srdíčka, Ponechat obě, Zpět."""

    def _dialog(self, n_extra=0):
        ids = [self._photo(f"p{i}.jpg", i, fav=1) for i in range(2 + n_extra)]
        pairs = [(ids[0], "", ids[1], "", 1)] + [(ids[0], "", ids[1 + k], "", 2)
                                                 for k in range(1, n_extra + 1)]
        dlg = main.DuplicateReviewDialog(pairs)
        self.addCleanup(dlg.close)
        return dlg, ids

    def _state(self, media_id):
        return self.q("SELECT is_trashed, is_favorite FROM media_items WHERE id=?", (media_id,))[0]

    def test_keep_left_trashes_right_and_keeps_favorites(self):
        dlg, (a, b) = self._dialog()
        dlg._action("keep_a")
        self.assertEqual(self._state(b), (1, 1), "pravá do koše, srdíčko zůstává")
        self.assertEqual(self._state(a), (0, 1))
        self.assertTrue(dlg.changed)
        self.assertNotIn(b, {r[0] for r in main.fetch_media_records()})
        self.assertIn(b, {r[0] for r in main.fetch_media_records(trashed=True)})

    def test_keep_right_trashes_left(self):
        dlg, (a, b) = self._dialog()
        dlg._action("keep_b")
        self.assertEqual(self._state(a), (1, 1))
        self.assertEqual(self._state(b), (0, 1))

    def test_keep_both_is_remembered(self):
        dlg, (a, b) = self._dialog()
        dlg._action("keep_both")
        self.assertEqual(self.q("SELECT id_a, id_b FROM duplicate_dismissed"), [(min(a, b), max(a, b))])
        self.assertEqual(self._state(a)[0] + self._state(b)[0], 0, "nic se nemaže")

    def test_skip_changes_nothing(self):
        dlg, (a, b) = self._dialog()
        dlg._action("skip")
        self.assertFalse(dlg.changed)
        self.assertEqual(self._state(a)[0] + self._state(b)[0], 0)

    def test_undo_restores(self):
        dlg, (a, b) = self._dialog()
        dlg._action("keep_a")
        self.assertTrue(dlg.btn_undo.isEnabled())
        dlg._undo()
        self.assertEqual(self._state(b), (0, 1))
        self.assertEqual(dlg.current_idx, 0)
        dlg._action("keep_both")
        dlg._undo()
        self.assertEqual(self.q("SELECT COUNT(*) FROM duplicate_dismissed")[0][0], 0)
        self.assertFalse(dlg.btn_undo.isEnabled())

    def test_pairs_with_trashed_item_are_skipped(self):
        """Skupina A,B,C: po "Ponechat pravé" (A do koše) se pár (A,C) přeskočí."""
        ids = [self._photo(f"p{i}.jpg", i) for i in range(5)]
        pairs = [(ids[0], "", ids[1], "", 1), (ids[0], "", ids[2], "", 1), (ids[3], "", ids[4], "", 1)]
        dlg = main.DuplicateReviewDialog(pairs)
        self.addCleanup(dlg.close)
        dlg._action("keep_b")
        self.assertEqual(dlg.current_idx, 2)
        # A je v koši -> vyřešené jsou (A,B) i (A,C): zpět už není kam
        self.assertFalse(dlg.btn_prev.isEnabled())
        dlg._prev()
        self.assertEqual(dlg.current_idx, 2)
        dlg._undo()
        self.assertEqual(dlg.current_idx, 0, "Zpět vrátí A z koše i k jeho páru")
        self.assertTrue(dlg.btn_next.isEnabled())


class TestTrash(TempEnv):
    """Koš (měkké smazání) – podklad pro akce dialogu duplicit."""

    def test_trash_and_restore_roundtrip(self):
        a = self._photo("a.jpg", 1)
        self.assertEqual(database.trash_media([a]), 1)
        self.assertEqual(database.trash_media([a]), 0, "podruhé už není co měnit")
        self.assertIsNotNone(self.q("SELECT trashed_at FROM media_items WHERE id=?", (a,))[0][0])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "a.jpg")), "soubor na disku zůstává")
        self.assertEqual(database.restore_media([a]), 1)
        self.assertEqual(self.q("SELECT is_trashed, trashed_at FROM media_items WHERE id=?", (a,))[0],
                         (0, None))

    def test_schema_migration_adds_columns(self):
        cols = {r[1] for r in self.q("PRAGMA table_info(media_items)")}
        self.assertTrue({"phash_version", "is_trashed", "trashed_at"} <= cols)
        tables = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("duplicate_dismissed", tables)


if __name__ == "__main__":
    unittest.main(verbosity=2)
