"""Automatické testy pro Patch 1 (OmniPhoto Vault).

Spuštění (ve složce projektu):   python test_patch1.py -v

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db, .cache ani fotky se
nepoužijí. Pokud není nainstalované PyQt6 / reverse_geocoder, použijí se atrapy.
"""
import io
import os
import pickle
import shutil
import sqlite3
import sys
import tempfile
import threading
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _install_stubs():
    try:
        import PyQt6.QtCore  # noqa: F401
    except ImportError:
        class _Sig:
            def __init__(self):
                self.calls = []
            def emit(self, *a):
                self.calls.append(a)
            def connect(self, *a):
                pass

        class pyqtSignal:
            def __init__(self, *a):
                pass
            def __set_name__(self, owner, name):
                self.name = name
            def __get__(self, obj, objtype=None):
                if obj is None:
                    return self
                return obj.__dict__.setdefault("_sig_" + self.name, _Sig())

        class QThread:
            def __init__(self, *a, **k):
                pass
            def isRunning(self):
                return False

        qt, core = types.ModuleType("PyQt6"), types.ModuleType("PyQt6.QtCore")
        core.QThread, core.pyqtSignal = QThread, pyqtSignal
        qt.QtCore = core
        sys.modules["PyQt6"], sys.modules["PyQt6.QtCore"] = qt, core
    try:
        import reverse_geocoder  # noqa: F401
    except ImportError:
        rg = types.ModuleType("reverse_geocoder")
        rg.search = lambda coords, mode=1, verbose=False: [{"name": "Praha", "cc": "CZ"} for _ in coords]
        sys.modules["reverse_geocoder"] = rg


_install_stubs()

import numpy as np                      # noqa: E402
from PIL import Image                   # noqa: E402

import database                         # noqa: E402
import scanner                          # noqa: E402
import cache_tools                      # noqa: E402
import cv_io                            # noqa: E402
import ai_engine                        # noqa: E402
import face_cluster                     # noqa: E402


class TempEnv(unittest.TestCase):
    """Izolované prostředí: vlastní DB a cache v dočasné složce."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_test_")
        self.db = os.path.join(self.tmp, "photos.db")
        self.thumbs = os.path.join(self.tmp, ".cache", "thumbnails")
        self.faces = os.path.join(self.tmp, ".cache", "faces")
        os.makedirs(self.thumbs)
        os.makedirs(self.faces)
        self._saved = (database.DB_PATH, scanner.CACHE_DIR, cache_tools.THUMB_DIR)
        database.DB_PATH = self.db
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
            cur = conn.execute(sql, params)
            return cur.lastrowid


def _meta(path, **kw):
    d = {"file_path": path, "file_name": os.path.basename(path), "file_size": 100, "file_mtime": 1000.0,
         "file_ext": ".jpg", "media_type": "photo", "date_taken": "2024-05-01 10:00:00",
         "latitude": 50.1, "longitude": 14.4}
    d.update(kw)
    return d


class TestUpsert(TempEnv):
    def test_upsert_keeps_id_favorite_faces_embeddings(self):
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), _meta("a.jpg"))
        mid = self.q("SELECT id FROM media_items WHERE file_path='a.jpg'")[0][0]
        self.x("UPDATE media_items SET is_favorite=1, thumbnail_path='t.jpg', event_id=NULL, "
               "place_city='Brno', media_type='live_photo', paired_video_path='a.mov' WHERE id=?", (mid,))
        self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, ?)", (mid, b"x" * 2048))
        self.x("INSERT INTO image_embeddings (media_id, embedding) VALUES (?, ?)", (mid, b"y" * 2048))

        # znovu stejný soubor (stejná velikost, jiné datum) -> aktualizace, ne smazání
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), _meta("a.jpg", date_taken="2025-01-01 00:00:00"))

        row = self.q("SELECT id, is_favorite, thumbnail_path, place_city, media_type, paired_video_path, date_taken "
                     "FROM media_items")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0][0], mid, "ID se nesmí změnit")
        self.assertEqual(row[0][1:], (1, "t.jpg", "Brno", "live_photo", "a.mov", "2025-01-01 00:00:00"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances")[0][0], 1, "obličeje nesmí zmizet")
        self.assertEqual(self.q("SELECT COUNT(*) FROM image_embeddings")[0][0], 1, "embeddingy nesmí zmizet")

    def test_size_change_resets_thumbnail_and_location_change_resets_city(self):
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), _meta("a.jpg"))
        self.x("UPDATE media_items SET thumbnail_path='t.jpg', place_city='Praha'")
        with database.db_transaction() as conn:
            database.upsert_media_item(conn.cursor(), _meta("a.jpg", file_size=999, latitude=49.0))
        r = self.q("SELECT thumbnail_path, place_city FROM media_items")[0]
        self.assertEqual(r, (None, None))


class TestMigration(unittest.TestCase):
    def test_old_schema_is_upgraded_and_faces_backfilled(self):
        tmp = tempfile.mkdtemp()
        saved = database.DB_PATH
        try:
            database.DB_PATH = os.path.join(tmp, "old.db")
            c = sqlite3.connect(database.DB_PATH)
            c.executescript("""
                CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT);
                CREATE TABLE media_items (id INTEGER PRIMARY KEY AUTOINCREMENT, file_path TEXT UNIQUE,
                    file_name TEXT, file_size INTEGER, media_type TEXT, date_taken TEXT, latitude REAL, longitude REAL,
                    paired_video_path TEXT, content_identifier TEXT, event_id INTEGER);
                CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
                CREATE TABLE face_instances (id INTEGER PRIMARY KEY AUTOINCREMENT, media_id INTEGER NOT NULL,
                    person_id INTEGER, embedding BLOB NOT NULL);
                INSERT INTO media_items (file_path, media_type) VALUES ('a.jpg','photo'), ('b.jpg','photo');
                INSERT INTO face_instances (media_id, embedding) VALUES (1, x'00');
            """)
            c.commit(); c.close()
            database.init_db()
            conn = database.get_connection()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
            self.assertTrue({"file_mtime", "faces_scanned", "phash"} <= cols)
            scanned = dict(conn.execute("SELECT file_path, faces_scanned FROM media_items").fetchall())
            conn.close()
            self.assertEqual(scanned, {"a.jpg": 1, "b.jpg": 0})
            database.init_db()   # druhé spuštění musí být idempotentní
        finally:
            database.DB_PATH = saved
            shutil.rmtree(tmp, ignore_errors=True)


class TestFavoriteAndFaces(TempEnv):
    def test_toggle_favorite_is_atomic_under_threads(self):
        mid = self.x("INSERT INTO media_items (file_path) VALUES ('a.jpg')")
        errors = []

        def work():
            try:
                for _ in range(40):
                    database.toggle_favorite(mid)
            except Exception as e:      # pragma: no cover
                errors.append(e)

        ts = [threading.Thread(target=work) for _ in range(3)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(errors, [])
        # 120 přepnutí (sudý počet) -> musí skončit na 0; neatomický toggle by ztrácel kroky
        self.assertEqual(self.q("SELECT is_favorite FROM media_items")[0][0], 0)

    def test_save_faces_persists_quality_and_marks_scanned_even_without_faces(self):
        a = self.x("INSERT INTO media_items (file_path, media_type) VALUES ('a.jpg','photo')")
        b = self.x("INSERT INTO media_items (file_path, media_type) VALUES ('b.jpg','photo')")
        self.assertEqual({r[0] for r in database.get_media_pending_face_scan()}, {a, b})
        database.save_faces_for_media(a, [{
            "crop_path": "c.jpg", "embedding": b"e" * 2048, "bbox": "1,2,3,4",
            "bbox_x": .1, "bbox_y": .2, "bbox_w": .3, "bbox_h": .4, "quality_score": 55.5}])
        database.save_faces_for_media(b, [])          # fotka bez tváře
        self.assertEqual(database.get_media_pending_face_scan(), [])
        r = self.q("SELECT quality_score, bbox_x, bbox FROM face_instances")[0]
        self.assertEqual(r, (55.5, 0.1, "1,2,3,4"))


class TestCvIo(unittest.TestCase):
    def test_unicode_path_roundtrip(self):
        import cv2
        tmp = tempfile.mkdtemp()
        try:
            d = os.path.join(tmp, "Příliš žluťoučký kůň", "Jiří")
            os.makedirs(d)
            p = os.path.join(d, "řím_ořez.jpg")
            img = np.full((40, 60, 3), (10, 120, 240), dtype=np.uint8)
            self.assertTrue(cv_io.imwrite(p, img, [int(cv2.IMWRITE_JPEG_QUALITY), 92]))
            self.assertTrue(os.path.exists(p))
            back = cv_io.imread(p)
            self.assertEqual(back.shape, (40, 60, 3))
            self.assertIsNone(cv_io.imread(os.path.join(d, "neexistuje.jpg")))
            self.assertFalse(os.path.exists(p + ".part"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestEmbeddings(TempEnv):
    def setUp(self):
        super().setUp()
        rng = np.random.default_rng(1)
        v = rng.normal(size=ai_engine.EMBED_DIM).astype(np.float32)
        self.vec = v / np.linalg.norm(v)

    def test_roundtrip_is_raw_float32(self):
        raw = ai_engine.encode_embedding(self.vec)
        self.assertEqual(len(raw), 2048)
        np.testing.assert_allclose(ai_engine.decode_embedding(raw), self.vec, atol=1e-6)

    def test_legacy_pickle_still_readable_for_all_protocols(self):
        # Původní kód volal pickle.dumps() s výchozím protokolem (Python 3.8+ = 4); 3-5 pokrývá vše reálné.
        for proto in range(3, pickle.HIGHEST_PROTOCOL + 1):
            raw = pickle.dumps(self.vec, protocol=proto)
            out = ai_engine.decode_embedding(raw)
            self.assertIsNotNone(out, f"protokol {proto}")
            np.testing.assert_allclose(out, self.vec, atol=1e-6)

    def test_malicious_pickle_is_rejected_and_not_executed(self):
        marker = os.path.join(self.tmp, "PWNED.txt")

        class Evil:
            def __reduce__(self):
                return (open, (marker, "w"))

        self.assertIsNone(ai_engine.decode_embedding(pickle.dumps(Evil())))
        self.assertFalse(os.path.exists(marker), "škodlivý pickle se NESMÍ provést")

    def test_migrate_converts_legacy_and_drops_garbage(self):
        good = self.x("INSERT INTO media_items (file_path) VALUES ('g.jpg')")
        bad = self.x("INSERT INTO media_items (file_path) VALUES ('b.jpg')")
        new = self.x("INSERT INTO media_items (file_path) VALUES ('n.jpg')")
        emb_sql = "INSERT INTO image_embeddings (media_id, embedding) VALUES (?, ?)"   # + source_sig (Fáze 1)
        self.x(emb_sql, (good, pickle.dumps(self.vec)))
        self.x(emb_sql, (bad, b"\x80\x04nesmysl"))
        self.x(emb_sql, (new, ai_engine.encode_embedding(self.vec)))
        self.assertEqual(ai_engine.migrate_legacy_embeddings(), (1, 1))
        rows = dict(self.q("SELECT media_id, length(embedding) FROM image_embeddings"))
        self.assertEqual(rows, {good: 2048, new: 2048})
        self.assertEqual(ai_engine.migrate_legacy_embeddings(), (0, 0))   # idempotentní


class TestThumbnails(TempEnv):
    def test_exif_orientation_is_applied(self):
        p = os.path.join(self.tmp, "port.jpg")
        im = Image.new("RGB", (400, 200), (200, 30, 30))      # uloženo naležato...
        ex = Image.Exif(); ex[274] = 6                         # ...ale s orientací "otočit o 90°"
        im.save(p, "JPEG", exif=ex)
        out = os.path.join(self.thumbs, "t.jpg")
        self.assertTrue(scanner.make_thumbnail(p, out, "photo", None))
        with Image.open(out) as im_out:
            w, h = im_out.size
        self.assertGreater(h, w, "po aplikaci EXIF orientace má být miniatura na výšku")
        self.assertEqual((w, h), (200, 400), "menší originál se nezvětšuje (Stage 2: kratší strana 840 px)")

    def test_cache_clear_keeps_faces_and_repair_regenerates(self):
        src = os.path.join(self.tmp, "x.jpg")
        Image.new("RGB", (800, 600), (1, 2, 3)).save(src, "JPEG")
        mid = self.x("INSERT INTO media_items (file_path, media_type) VALUES (?, 'photo')", (src,))
        self.assertEqual(scanner.repair_missing_thumbnails(), 1)
        thumb = self.q("SELECT thumbnail_path FROM media_items")[0][0]
        self.assertTrue(os.path.exists(thumb))

        crop = os.path.join(self.faces, "face_1.jpg")
        with open(crop, "wb") as fh:
            fh.write(b"avatar")
        fid = self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                     (mid, crop, b"e" * 2048))
        pid = self.x("INSERT INTO people (name, cover_face_path) VALUES ('Honza', ?)", (crop,))
        self.x("UPDATE face_instances SET person_id=? WHERE id=?", (pid, fid))

        ok, msg = cache_tools.clear_thumbnail_cache()
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(thumb), "miniatura má být smazána")
        self.assertTrue(os.path.exists(crop), "ořez obličeje se NESMÍ smazat")
        self.assertEqual(self.q("SELECT COUNT(*) FROM people")[0][0], 1)
        self.assertEqual(self.q("SELECT person_id FROM face_instances")[0][0], pid)
        self.assertIsNone(self.q("SELECT thumbnail_path FROM media_items")[0][0])

        self.assertEqual(scanner.repair_missing_thumbnails(), 1, "po vyčištění se miniatury dogenerují")
        self.assertTrue(os.path.exists(self.q("SELECT thumbnail_path FROM media_items")[0][0]))
        # druhé spuštění nemá co dělat
        self.assertEqual(scanner.repair_missing_thumbnails(), 0)


class TestScannerEndToEnd(TempEnv):
    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.tmp, "Fotky", "Příliš žluťoučký")
        os.makedirs(self.lib)
        self.exif_calls = []
        self.fake_exiftool = os.path.join(self.tmp, "exiftool.exe")
        open(self.fake_exiftool, "w").close()
        self.dates = {}
        self.skip = set()
        self._orig_run = scanner.subprocess.run
        scanner.subprocess.run = self._fake_run

    def tearDown(self):
        scanner.subprocess.run = self._orig_run
        super().tearDown()

    def _fake_run(self, cmd, *a, **k):
        if cmd and cmd[0] == self.fake_exiftool:
            import json as _json
            files = [c for c in cmd[1:] if not c.startswith("-")]
            self.exif_calls.append(files)
            out = []
            for f in files:
                if os.path.basename(f) in self.skip:
                    continue            # ExifTool soubor "nezvládl" (nevrátí pro něj záznam)
                out.append({"SourceFile": f.replace("\\", "/"),
                            "DateTimeOriginal": self.dates.get(os.path.basename(f), "")})
            return types.SimpleNamespace(stdout=_json.dumps(out), stderr="", returncode=0)
        return self._orig_run(cmd, *a, **k)

    def _make(self, name, size=(640, 480), color=(10, 20, 30)):
        p = os.path.join(self.lib, name)
        Image.new("RGB", size, color).save(p, "JPEG")
        return p

    def _scan(self):
        s = scanner.MediaScannerThread(os.path.join(self.tmp, "Fotky"), exiftool_path=self.fake_exiftool)
        s.run()
        return s

    def test_metadata_is_matched_by_sourcefile_not_by_order(self):
        for n, d in (("a.jpg", "2024:01:01 10:00:00"), ("b.jpg", "2024:02:02 10:00:00"),
                     ("c.jpg", "2024:03:03 10:00:00")):
            self._make(n); self.dates[n] = d
        self.skip = {"b.jpg"}
        self._scan()
        got = {os.path.basename(p): d for p, d in self.q("SELECT file_path, date_taken FROM media_items")}
        self.assertEqual(got["a.jpg"], "2024-01-01 10:00:00")
        self.assertEqual(got["c.jpg"], "2024-03-03 10:00:00", "c.jpg nesmí dostat metadata jiného souboru")
        self.assertNotIn(got["b.jpg"][:10], ("2024-01-01", "2024-02-02", "2024-03-03"))  # fallback na mtime
        self.assertEqual(len(got), 3)

    def test_rescan_is_incremental_and_never_destroys_user_data(self):
        a, b = self._make("a.jpg"), self._make("b.jpg", color=(90, 90, 90))
        self.dates = {"a.jpg": "2024:05:01 10:00:00", "b.jpg": "2024:05:01 11:00:00"}
        self._scan()
        self.assertEqual(self.q("SELECT COUNT(*) FROM media_items")[0][0], 2)
        ev1 = self.q("SELECT COUNT(*) FROM events")[0][0]
        self.assertGreaterEqual(ev1, 1)
        ids = dict(self.q("SELECT file_name, id FROM media_items"))
        self.x("UPDATE media_items SET is_favorite=1 WHERE id=?", (ids["a.jpg"],))
        self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, ?)", (ids["a.jpg"], b"e" * 2048))
        self.x("INSERT INTO image_embeddings (media_id, embedding) VALUES (?, ?)", (ids["a.jpg"], b"y" * 2048))
        for (p,) in self.q("SELECT thumbnail_path FROM media_items"):
            self.assertTrue(p and os.path.exists(p))

        # --- 2. sken beze změn: ExifTool se nesmí vůbec spustit, nic se neztratí ---
        self.exif_calls.clear()
        self._scan()
        self.assertEqual(self.exif_calls, [], "beze změn se ExifTool spouštět nemá")
        self.assertEqual(dict(self.q("SELECT file_name, id FROM media_items")), ids)
        self.assertEqual(self.q("SELECT is_favorite FROM media_items WHERE id=?", (ids["a.jpg"],))[0][0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances")[0][0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM image_embeddings")[0][0], 1)

        # --- 3. změní se jen b.jpg (jiná velikost) + přibude c.jpg ---
        thumb_a = self.q("SELECT thumbnail_path FROM media_items WHERE id=?", (ids["a.jpg"],))[0][0]
        self._make("b.jpg", size=(1200, 900), color=(5, 5, 5))
        os.utime(b, (os.path.getmtime(b) + 100, os.path.getmtime(b) + 100))
        self._make("c.jpg"); self.dates["c.jpg"] = "2024:05:02 09:00:00"
        self.exif_calls.clear()
        self._scan()
        called = {os.path.basename(f) for batch in self.exif_calls for f in batch}
        self.assertEqual(called, {"b.jpg", "c.jpg"}, "ExifTool jen pro změněné a nové soubory")
        self.assertEqual(self.q("SELECT id FROM media_items WHERE file_name='a.jpg'")[0][0], ids["a.jpg"])
        self.assertEqual(self.q("SELECT is_favorite FROM media_items WHERE file_name='a.jpg'")[0][0], 1)
        self.assertEqual(self.q("SELECT thumbnail_path FROM media_items WHERE file_name='a.jpg'")[0][0], thumb_a)
        self.assertEqual(self.q("SELECT COUNT(*) FROM media_items")[0][0], 3)
        self.assertEqual(self.q("SELECT id FROM media_items WHERE file_name='b.jpg'")[0][0], ids["b.jpg"],
                         "změněný soubor zachová své ID")
        # události se musí přeskupit i při druhém a třetím importu (cizí klíč!)
        self.assertEqual(self.q("SELECT COUNT(*) FROM media_items WHERE event_id IS NULL")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM events")[0][0] >= 1, True)
        for (p,) in self.q("SELECT thumbnail_path FROM media_items"):
            self.assertTrue(p and os.path.exists(p))

    def test_finished_signal_emitted_on_exception_but_not_on_cancel(self):
        self._make("a.jpg")

        # PyQt6 může být nainstalováno (reálný pyqtBoundSignal nemá .calls).
        # Nahradíme finished_signal na instanci jednoduchým sledovačem.
        class _SigSpy:
            def __init__(self):
                self.calls = []
            def emit(self, *a):
                self.calls.append(a)
            def connect(self, *a):
                pass

        s = scanner.MediaScannerThread(os.path.join(self.tmp, "Fotky"), exiftool_path=self.fake_exiftool)
        s.finished_signal = _SigSpy()
        s._run_pipeline = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        s.run()
        self.assertEqual(len(s.finished_signal.calls), 1, "po výjimce se UI musí odblokovat")

        s2 = scanner.MediaScannerThread(os.path.join(self.tmp, "Fotky"), exiftool_path=self.fake_exiftool)
        s2.finished_signal = _SigSpy()
        s2.stop(); s2.run()
        self.assertEqual(len(s2.finished_signal.calls), 0, "při zrušení se finished nevysílá")


class TestFaceCluster(TempEnv):
    def _unit(self, rng, base, noise=0.12):
        v = base + noise * rng.normal(size=base.size).astype(np.float32)
        return (v / np.linalg.norm(v)).astype(np.float32)

    def _add_face(self, media_path, vec, person=None, q=10.0, crop=None):
        mid = self.q("SELECT id FROM media_items WHERE file_path=?", (media_path,))
        mid = mid[0][0] if mid else self.x("INSERT INTO media_items (file_path) VALUES (?)", (media_path,))
        return self.x("INSERT INTO face_instances (media_id, person_id, face_crop_path, embedding, quality_score) "
                      "VALUES (?, ?, ?, ?, ?)", (mid, person, crop or "/neexistuje/crop.jpg", vec.tobytes(), q))

    def test_named_people_are_anchors_new_faces_attach_and_unknown_clusters_rebuilt(self):
        rng = np.random.default_rng(7)
        A, B, C = (rng.normal(size=512).astype(np.float32) for _ in range(3))
        honza = self.x("INSERT INTO people (name, cover_face_path) VALUES ('Honza', 'cover.jpg')")
        fav = self.x("INSERT INTO people (name, is_favorite) VALUES (NULL, 1)")
        stale = self.x("INSERT INTO people (name) VALUES ('Neznámá osoba')")
        for i in range(3):
            self._add_face(f"honza_{i}.jpg", self._unit(rng, A), person=honza)
        for i in range(3):
            self._add_face(f"honza_new_{i}.jpg", self._unit(rng, A), person=None)       # mají se přidat k Honzovi
        for i in range(3):
            self._add_face(f"fav_{i}.jpg", self._unit(rng, C), person=fav)
        for i in range(6):
            self._add_face(f"b_{i}.jpg", self._unit(rng, B), person=stale if i < 2 else None)   # nový automatický shluk
        self._add_face("lonely.jpg", self._unit(rng, rng.normal(size=512).astype(np.float32)))  # osamělý obličej

        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)

        people = {r[0]: r[1:] for r in self.q("SELECT id, name, is_favorite, cover_face_path FROM people")}
        self.assertEqual(people[honza][0], "Honza")
        self.assertEqual(people[honza][2], "cover.jpg", "kotva se nesmí přepsat")
        self.assertIn(fav, people)
        self.assertNotIn(stale, people, "starý automatický shluk se přestavuje")
        auto = [p for p, v in people.items() if v[0] == "Neznámá osoba"]
        self.assertEqual(len(auto), 1, "identita B má vytvořit právě jednu novou osobu")

        def owner(prefix):
            return {r[0] for r in self.q("SELECT fi.person_id FROM face_instances fi JOIN media_items m "
                                         "ON m.id=fi.media_id WHERE m.file_path LIKE ?", (prefix + "%",))}
        self.assertEqual(owner("honza_"), {honza}, "nové obličeje Honzy se přiřadí ke kotvě")
        self.assertEqual(owner("fav_"), {fav})
        self.assertEqual(owner("b_"), {auto[0]})
        self.assertEqual(owner("lonely"), {None})

    def test_missing_crop_files_do_not_wipe_people(self):
        """Scénář smazané mezipaměti: ořezy na disku nejsou -> osoby musí přežít."""
        rng = np.random.default_rng(3)
        A = rng.normal(size=512).astype(np.float32)
        honza = self.x("INSERT INTO people (name) VALUES ('Honza')")
        for i in range(4):
            self._add_face(f"h{i}.jpg", self._unit(rng, A), person=honza, crop=f"/smazano/{i}.jpg")
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT name FROM people")[0][0], "Honza")
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances WHERE person_id=?", (honza,))[0][0], 4)

    def test_cannot_link_same_photo_never_same_person(self):
        rng = np.random.default_rng(5)
        A = rng.normal(size=512).astype(np.float32)
        v = self._unit(rng, A, noise=0.01)
        # dva (téměř identické) obličeje ve STEJNÉ fotce + po 2 dalších fotkách s toutéž osobou
        self._add_face("group.jpg", v)
        self._add_face("group.jpg", v)
        for i in range(2):
            self._add_face(f"solo_{i}.jpg", self._unit(rng, A, noise=0.01))
        face_cluster.strict_recluster_faces()
        same_photo = self.q("SELECT fi.person_id FROM face_instances fi JOIN media_items m ON m.id=fi.media_id "
                            "WHERE m.file_path='group.jpg'")
        pids = [r[0] for r in same_photo if r[0] is not None]
        self.assertEqual(len(pids), len(set(pids)), "dva obličeje z jedné fotky nesmí patřit téže osobě")


class TestFaceEngineLogic(TempEnv):
    """Logika kolem detekce (bez InsightFace - používá se atrapa modelu)."""

    class _Face:
        def __init__(self, x, emb):
            self.bbox = np.array([x, 60, x + 120, 200], dtype=np.float32)
            self.kps = np.array([[x + 30, 100], [x + 90, 100], [x + 60, 130], [x + 40, 160], [x + 80, 160]], dtype=np.float32)
            self.det_score = 0.9
            self.embedding = emb

    class _Align:
        @staticmethod
        def norm_crop(img, kps, image_size=224):
            import cv2
            return cv2.resize(img[40:220, 20:260], (image_size, image_size))

    def setUp(self):
        super().setUp()
        import importlib
        self.fe = importlib.import_module("face_engine")
        self._saved_fe = (self.fe._app, self.fe._face_align, self.fe.FACE_CACHE_DIR)
        # cesta s diakritikou, jako u uživatele "Jiří"
        self.fe.FACE_CACHE_DIR = os.path.join(self.tmp, "Jiří", ".cache", "faces")
        os.makedirs(self.fe.FACE_CACHE_DIR)
        self.fe._face_align = self._Align
        rng = np.random.default_rng(0)
        self.img_path = os.path.join(self.tmp, "foto.png")
        Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)).save(self.img_path)

    def tearDown(self):
        self.fe._app, self.fe._face_align, self.fe.FACE_CACHE_DIR = self._saved_fe
        super().tearDown()

    def test_import_is_lazy(self):
        self.assertNotIn("insightface", sys.modules, "InsightFace se nesmí načítat při importu modulu")

    def test_unicode_crops_written_and_duplicate_crops_discarded(self):
        rng = np.random.default_rng(1)
        e = rng.normal(size=512).astype(np.float32)
        e2 = e + 0.02 * rng.normal(size=512).astype(np.float32)          # téměř stejná osoba -> dedup
        other = rng.normal(size=512).astype(np.float32)                   # jiná osoba
        faces = [self._Face(20, e), self._Face(150, e2), self._Face(270, other)]
        self.fe._app = types.SimpleNamespace(get=lambda img: faces)

        res = self.fe.detect_and_extract_faces(1, self.img_path)
        self.assertEqual(len(res), 2, "dva obličeje stejné osoby se sloučí, třetí zůstane")
        on_disk = set(os.listdir(self.fe.FACE_CACHE_DIR))
        self.assertEqual(on_disk, {os.path.basename(r["crop_path"]) for r in res},
                         "na disku nesmí zůstat ořez zahozené detekce (sirotek)")
        for r in res:
            self.assertGreater(r["quality_score"], 0)
            self.assertIsNotNone(cv_io.imread(r["crop_path"]))

    def test_detection_failure_is_not_reported_as_no_faces(self):
        def boom(img):
            raise RuntimeError("DirectML selhal")
        self.fe._app = types.SimpleNamespace(get=boom)
        with self.assertRaises(RuntimeError):
            self.fe.detect_and_extract_faces(1, self.img_path)

    def test_no_faces_returns_empty_list(self):
        self.fe._app = types.SimpleNamespace(get=lambda img: [])
        self.assertEqual(self.fe.detect_and_extract_faces(1, self.img_path), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)