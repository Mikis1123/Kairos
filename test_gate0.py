"""Gate 0: data v AppData + migrace, odolná identita médií, zástupné soubory cloudu, napájení.

Vše v dočasných složkách – skutečná knihovna (AppData i stará složka aplikace) se nedotkne.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import cloud_files
import data_migration
import database
import governor
import media_identity
import paths
import power


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class DbEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kairos_g0_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.addCleanup(setattr, database, "DB_PATH", self._db)
        database.init_db()

    def p(self, *parts):
        return os.path.normpath(os.path.join(self.tmp, *parts))

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def x(self, sql, params=()):
        with database.db_transaction() as conn:
            return conn.execute(sql, params).lastrowid


# ═════════════════════════════════════════════════════════════════════════════
# 1. Data mimo složku aplikace + migrace
# ═════════════════════════════════════════════════════════════════════════════

class TestLayout(unittest.TestCase):
    def test_user_data_is_not_next_to_the_app(self):
        if paths.PORTABLE or os.environ.get("KAIROS_DATA_DIR"):
            self.skipTest("přenosný režim")
        appdata = os.path.normcase(os.environ.get("APPDATA", ""))
        local = os.path.normcase(os.environ.get("LOCALAPPDATA", ""))
        self.assertTrue(os.path.normcase(database.REAL_DB_PATH).startswith(appdata), "DB v %APPDATA%")
        self.assertTrue(os.path.normcase(paths.CONFIG_PATH).startswith(appdata))
        self.assertTrue(os.path.normcase(paths.DATA_DIR).endswith(os.path.normcase(r"\Kairos")))
        self.assertTrue(os.path.normcase(paths.DEFAULT_CACHE_ROOT).startswith(local), "mezipaměť v %LOCALAPPDATA%")
        self.assertFalse(os.path.normcase(paths.MODELS_DIR).startswith(os.path.normcase(paths.INSTALL_DIR)))

    def test_cache_override_read_from_config(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"cache_dir": r"D:\KairosCache"}, f)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KAIROS_CACHE_DIR", None)
            self.assertEqual(paths._read_cache_override(d), r"D:\KairosCache")
        self.assertIsNone(paths._read_cache_override(os.path.join(d, "nic")))

    def test_rebase(self):
        self.assertEqual(data_migration.rebase(r"C:\App\.cache\thumbnails\t_1.jpg", r"C:\App\.cache", r"E:\C"),
                         r"E:\C\thumbnails\t_1.jpg")
        self.assertEqual(data_migration.rebase(r"c:\app\.CACHE\x.jpg", r"C:\App\.cache", r"E:\C"), r"E:\C\x.jpg",
                         "Windows: velikost písmen nerozhoduje")
        self.assertIsNone(data_migration.rebase(r"C:\App\.cache2\x.jpg", r"C:\App\.cache", r"E:\C"))
        self.assertIsNone(data_migration.rebase(r"D:\Fotky\x.jpg", r"C:\App\.cache", r"E:\C"))


class TestMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kairos_mig_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.legacy = os.path.join(self.tmp, "app")
        self.data = os.path.join(self.tmp, "Roaming", "Kairos")
        self.local = os.path.join(self.tmp, "Local", "Kairos")
        self.cache = os.path.join(self.local, "cache")
        self.faces = os.path.join(self.local, "faces")
        self.models = os.path.join(self.local, "models")
        lc = os.path.join(self.legacy, ".cache")
        self.thumb = _write(os.path.join(lc, "thumbnails", "thumb_1.jpg"), b"thumb")
        self.face = _write(os.path.join(lc, "faces", "face_1_0.jpg"), b"face")
        _write(os.path.join(lc, "previews", "p.jpg"))
        _write(os.path.join(self.legacy, "vault", "vault.json"), b"{}")
        _write(os.path.join(self.legacy, "backups", "photos-2026-11-01.db"), b"bak")
        _write(os.path.join(self.legacy, "config.json"), json.dumps({"theme": "light"}).encode())
        db = os.path.join(self.legacy, "photos.db")
        saved = database.DB_PATH
        database.DB_PATH = db
        try:
            database.init_db()
            with database.db_transaction() as conn:
                for i in range(1, 51):
                    conn.execute("INSERT INTO media_items (file_path, file_name, thumbnail_path, is_favorite) "
                                 "VALUES (?, ?, ?, ?)", (rf"D:\Fotky\IMG_{i}.JPG", f"IMG_{i}.JPG",
                                                         self.thumb if i == 1 else None, i % 2))
                conn.execute("INSERT INTO people (name, cover_face_path) VALUES ('Eva', ?)", (self.face,))
                conn.execute("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (1, ?, x'00')",
                             (self.face,))
        finally:
            database.DB_PATH = saved

    def run_migration(self):
        return data_migration.migrate_legacy_layout(self.legacy, self.data, self.cache, self.faces, self.models)

    def test_full_migration_keeps_library_and_rewrites_cache_paths(self):
        stats = self.run_migration()
        self.assertEqual(stats["items"], 50)
        new_db = os.path.join(self.data, "photos.db")
        conn = sqlite3.connect(new_db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*), SUM(is_favorite) FROM media_items").fetchone(), (50, 25))
            thumb = conn.execute("SELECT thumbnail_path FROM media_items WHERE id = 1").fetchone()[0]
            crop = conn.execute("SELECT face_crop_path FROM face_instances").fetchone()[0]
            cover = conn.execute("SELECT cover_face_path FROM people").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(os.path.normcase(thumb), os.path.normcase(os.path.join(self.cache, "thumbnails", "thumb_1.jpg")))
        self.assertTrue(os.path.exists(thumb), "miniatura se přesunula s cestou v DB")
        self.assertTrue(os.path.exists(crop) and os.path.exists(cover))
        self.assertTrue(os.path.exists(os.path.join(self.cache, "previews", "p.jpg")))
        for name in ("config.json", os.path.join("vault", "vault.json"), os.path.join("backups", "photos-2026-11-01.db")):
            self.assertTrue(os.path.exists(os.path.join(self.data, name)), name)
            self.assertTrue(os.path.exists(os.path.join(self.legacy, name)), f"{name}: originál zůstává jako záloha")
        self.assertTrue(os.path.exists(os.path.join(self.legacy, "photos.db")), "stará DB se nemaže")
        self.assertTrue(os.path.exists(os.path.join(self.legacy, data_migration.MARKER_NAME)))
        self.assertIsNone(self.run_migration(), "jednorázová: podruhé nic")

    def test_failed_copy_leaves_no_half_library(self):
        with open(os.path.join(self.legacy, "photos.db"), "r+b") as f:
            f.seek(100)
            f.write(b"\xff" * 4000)                    # poškozená stránka
        with self.assertRaises(Exception):
            self.run_migration()
        self.assertFalse(os.path.exists(os.path.join(self.data, "photos.db")),
                         "bez platné kopie nesmí vzniknout nová knihovna (příště se zkusí znovu)")
        self.assertTrue(data_migration.needs_migration(self.legacy, self.data))

    def test_relocate_cache_moves_and_rebases(self):
        self.run_migration()
        cfg_path = os.path.join(self.data, "config.json")
        target = os.path.join(self.tmp, "E", "KairosCache")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.update(cache_dir=target, cache_dir_pending_from=self.cache)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        stats = data_migration.relocate_cache(cfg_path, os.path.join(self.data, "photos.db"))
        self.assertEqual(stats["rebased"], 1)
        conn = sqlite3.connect(os.path.join(self.data, "photos.db"))
        thumb = conn.execute("SELECT thumbnail_path FROM media_items WHERE id = 1").fetchone()[0]
        conn.close()
        self.assertTrue(thumb.startswith(target) and os.path.exists(thumb))
        with open(cfg_path, encoding="utf-8") as f:
            self.assertNotIn("cache_dir_pending_from", json.load(f))
        self.assertIsNone(data_migration.relocate_cache(cfg_path, os.path.join(self.data, "photos.db")))


# ═════════════════════════════════════════════════════════════════════════════
# 2. Odolná identita
# ═════════════════════════════════════════════════════════════════════════════

class TestFingerprint(unittest.TestCase):
    def test_fingerprint_reads_only_the_head(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        a = _write(os.path.join(d, "a.jpg"), b"A" * 100_000)
        b = _write(os.path.join(d, "b.jpg"), b"A" * 100_000)
        c = _write(os.path.join(d, "c.jpg"), b"A" * 99_999 + b"B")      # změna až za 64 kB
        e = _write(os.path.join(d, "e.jpg"), b"B" + b"A" * 99_999)
        fa = media_identity.fingerprint(a)
        self.assertTrue(fa.startswith("100000:"))
        self.assertEqual(fa, media_identity.fingerprint(b), "stejný obsah = stejný otisk (jiný název)")
        self.assertEqual(fa, media_identity.fingerprint(c), "jen prvních 64 kB – datum EXIF rozhodne")
        self.assertNotEqual(fa, media_identity.fingerprint(e))
        self.assertIsNone(media_identity.fingerprint(os.path.join(d, "nic.jpg")))
        with mock.patch.object(cloud_files, "stat_is_placeholder", return_value=True):
            self.assertIsNone(media_identity.fingerprint(a), "zástupný soubor cloudu se neotevírá")

    def test_identity_of_local_file(self):
        guid, rel = media_identity.identity_of(os.path.abspath(__file__))
        if guid is None:
            self.skipTest("svazek bez GUID")
        self.assertTrue(guid.startswith("\\\\?\\volume{"))
        self.assertTrue(os.path.abspath(__file__).lower().endswith(rel.lower()))
        self.assertIn(guid, media_identity.mounted_volumes())


class TestRekeyAndReconnect(DbEnv):
    def add(self, path, date="2024-05-01 10:00:00", fp=None, paired=None, mtype="photo"):
        return self.x("INSERT INTO media_items (file_path, file_name, media_type, date_taken, fingerprint, "
                      "file_size, paired_video_path) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (path, os.path.basename(path), mtype, date, fp, 10, paired))

    def test_rekey_preserves_everything_tied_to_the_id(self):
        old_p, old_v = self.p("old", "IMG_1.HEIC"), self.p("old", "IMG_1.MOV")
        mid = self.add(old_p, paired=old_v)
        vid = self.add(old_v, mtype="video")
        self.x("UPDATE media_items SET is_favorite = 1 WHERE id = ?", (mid,))
        self.x("INSERT INTO image_embeddings (media_id, embedding, source_sig) VALUES (?, x'00', ?)",
               (mid, old_p + "|10|123"))
        self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, x'00')", (mid,))
        new_p, new_v = self.p("new", "IMG_1.HEIC"), self.p("new", "IMG_1.MOV")
        with database.db_transaction() as conn:
            self.assertTrue(media_identity.rekey(conn.cursor(), mid, new_p, fp="10:abc"))
            self.assertTrue(media_identity.rekey(conn.cursor(), vid, new_v))
        row = self.q("SELECT file_path, file_name, is_favorite, paired_video_path, fingerprint "
                     "FROM media_items WHERE id = ?", (mid,))[0]
        self.assertEqual(row, (new_p, "IMG_1.HEIC", 1, new_v, "10:abc"))
        self.assertEqual(self.q("SELECT source_sig FROM image_embeddings")[0][0], new_p + "|10|123",
                         "vektory se po přesunu nepřepočítávají")
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances WHERE media_id = ?", (mid,))[0][0], 1)

    def test_rekey_refuses_to_merge_into_another_item(self):
        a = self.add(self.p("a.jpg"))
        self.add(self.p("b.jpg"))
        with database.db_transaction() as conn:
            self.assertFalse(media_identity.rekey(conn.cursor(), a, self.p("b.jpg")))
        self.assertEqual(self.q("SELECT file_path FROM media_items WHERE id = ?", (a,))[0][0], self.p("a.jpg"))

    def test_missing_index_matches_fingerprint_and_exif_date(self):
        here = _write(self.p("present.jpg"))
        rows = [(1, self.p("gone1.jpg"), "gone1.jpg", 10, "10:aa", "2024:05:01 10:00:00"),
                (2, self.p("gone2.jpg"), "gone2.jpg", 10, "10:aa", "2024-06-01 10:00:00"),
                (3, here, "present.jpg", 10, "10:aa", "2024-05-01 10:00:00"),
                (4, self.p("legacy.jpg"), "legacy.jpg", 77, None, "2020-01-01 00:00:00")]
        idx = media_identity.MissingIndex(rows)
        cands = idx.candidates(self.p("x", "renamed.jpg"), 10, "10:aa")
        self.assertEqual(sorted(c[0] for c in cands), [1, 2], "existující soubor není kandidát")
        self.assertEqual(idx.choose(cands, "2024-05-01 10:00:00"), 1, "rozhodne datum pořízení")
        self.assertEqual(idx.candidates(self.p("x", "r.jpg"), 10, "10:aa"), [(2, "2024-06-01 10:00:00")],
                         "už připojená položka se nenabízí znovu")
        self.assertIsNone(idx.choose([(2, "2024-06-01 10:00:00")], "2030-01-01 00:00:00"),
                          "jiné datum = nová položka, ne omyl")
        legacy = idx.candidates(self.p("y", "LEGACY.JPG"), 77, "77:zz")
        self.assertEqual([c[0] for c in legacy], [4], "starší položka bez otisku: název + velikost")

    def test_scanner_reconnects_moved_file_instead_of_duplicating(self):
        import scanner
        src = _write(self.p("Fotky", "2024", "IMG_9.JPG"), b"\xff\xd8" + os.urandom(70_000))
        s = scanner.MediaScannerThread(None, files=[src])
        meta = {"SourceFile": src, "DateTimeOriginal": "2024:05:01 10:00:00"}
        with database.db_transaction() as conn:
            s._insert_item(conn.cursor(), meta, src, os.path.getsize(src), os.path.getmtime(src))
        mid, fp = self.q("SELECT id, fingerprint FROM media_items")[0]
        self.assertTrue(fp)
        self.x("UPDATE media_items SET is_favorite = 1 WHERE id = ?", (mid,))
        self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, x'00')", (mid,))
        import albums
        with database.db_transaction() as conn:
            albums.ensure_tables(conn)
            albums.create_album(conn, "Léto", [mid])
        dst = self.p("Přejmenováno", "léto-01.jpg")              # přesun + přejmenování v Průzkumníku
        os.makedirs(os.path.dirname(dst))
        os.replace(src, dst)
        conn = database.get_connection()
        missing = media_identity.MissingIndex.load(conn)
        conn.close()
        to_process, _, _ = s._classify([dst], {}, missing)
        real, dbp, size, mtime, cands, fp2 = to_process[0]
        self.assertEqual(fp2, fp)
        with database.db_transaction() as conn:
            s._insert_item(conn.cursor(), {"SourceFile": dst, "DateTimeOriginal": "2024:05:01 10:00:00"},
                           dbp, size, mtime, cands=cands, fp=fp2, missing=missing)
        rows = self.q("SELECT id, file_path, is_favorite, availability FROM media_items")
        self.assertEqual(rows, [(mid, dst, 1, None)], "jedna položka, stejné id, oblíbená zůstala")
        self.assertEqual(s.reconnected, 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances WHERE media_id = ?", (mid,))[0][0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM album_items WHERE media_id = ?", (mid,))[0][0], 1)

    def test_reconcile_marks_offline_and_remaps_drive_letter(self):
        guid_ext, guid_gone = "\\\\?\\volume{ext}\\", "\\\\?\\volume{gone}\\"
        moved = _write(self.p("F", "Fotky", "a.jpg"))           # disk dřív E:, teď „F:“ (self.tmp\F)
        a = self.add(r"Q:\Fotky\a.jpg")
        self.x("UPDATE media_items SET volume_guid = ?, vol_rel_path = ? WHERE id = ?",
               (guid_ext, r"Fotky\a.jpg", a))
        b = self.add(r"Q:\Archiv\b.jpg")
        self.x("UPDATE media_items SET volume_guid = ?, vol_rel_path = ? WHERE id = ?",
               (guid_gone, r"Archiv\b.jpg", b))
        here = _write(self.p("local.jpg"))
        c = self.add(here)
        d = self.add(self.p("smazaný.jpg"))
        volumes = {guid_ext: [self.p("F") + "\\"], "\\\\?\\volume{sys}\\": ["C:\\"]}
        with database.db_transaction() as conn:
            stats = media_identity.reconcile(conn, volumes=volumes)
        state = dict(self.q("SELECT id, availability FROM media_items"))
        self.assertEqual(self.q("SELECT file_path FROM media_items WHERE id = ?", (a,))[0][0], moved)
        self.assertEqual(state, {a: None, b: "offline", c: None, d: "missing"})
        self.assertEqual((stats["remapped"], stats["offline"], stats["missing"]), (1, 1, 1))
        self.assertEqual(self.q("SELECT COUNT(*) FROM media_items")[0][0], 4, "nic se nemaže")
        os.makedirs(self.p("G", "Archiv"))
        _write(self.p("G", "Archiv", "b.jpg"))
        volumes[guid_gone] = [self.p("G") + "\\"]
        with database.db_transaction() as conn:
            media_identity.reconcile(conn, volumes=volumes)
        self.assertIsNone(dict(self.q("SELECT id, availability FROM media_items"))[b], "disk zpět = online")

    def test_reconcile_probes_a_dead_drive_only_once(self):
        for i in range(200):
            self.add(rf"Q:\Síť\{i}.jpg")
        calls = []

        def exists(p):
            calls.append(p)
            return False
        conn = database.get_connection()
        try:
            stats = media_identity.reconcile(conn, exists=exists, volumes={})
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(stats["offline"], 200)
        self.assertEqual(len(calls), 1, "jen kořen Q:\\ – mrtvý síťový disk nesmí stát sekundy na soubor")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Zástupné soubory cloudu
# ═════════════════════════════════════════════════════════════════════════════

class TestCloudPlaceholders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos_cloud_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.local = _write(os.path.join(self.tmp, "OneDrive", "local.jpg"))
        self.online = _write(os.path.join(self.tmp, "OneDrive", "online.jpg"), b"y" * 10)
        real_stat = os.stat

        class _St:
            def __init__(self, st, attrs):
                self._st, self.st_file_attributes = st, attrs

            def __getattr__(self, k):
                return getattr(self._st, k)

        def fake_stat(p, *a, **kw):
            st = real_stat(p, *a, **kw)
            attrs = cloud_files.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if "online" in str(p) else 0
            return _St(st, attrs)
        self.fake_stat = fake_stat

    def test_attribute_mask(self):
        self.assertTrue(cloud_files.attrs_are_placeholder(0x400000 | 0x20))
        self.assertTrue(cloud_files.attrs_are_placeholder(0x1000))
        self.assertTrue(cloud_files.attrs_are_placeholder(0x40000))
        self.assertFalse(cloud_files.attrs_are_placeholder(0x20 | cloud_files.FILE_ATTRIBUTE_PINNED))
        self.assertTrue(cloud_files.local_ok(self.local))
        self.assertFalse(cloud_files.local_ok(os.path.join(self.tmp, "nic.jpg")))

    def test_scanner_skips_cloud_only_files_without_consent(self):
        import scanner
        with mock.patch("os.stat", self.fake_stat):
            s = scanner.MediaScannerThread(os.path.join(self.tmp, "OneDrive"))
            files = s._collect_files()
            self.assertEqual(files, [self.local])
            self.assertEqual([p for p, _ in s.cloud_files], [self.online])
            self.assertFalse(cloud_files.local_ok(self.online), "démon AI ho neotevře")
            consent = scanner.MediaScannerThread(None, files=[self.online], allow_cloud=True)
            self.assertEqual(consent._collect_files(), [self.online], "po souhlasu se importuje")
            dropped = scanner.MediaScannerThread(None, files=[self.online, self.local])
            self.assertEqual(dropped._collect_files(), [self.local])


# ═════════════════════════════════════════════════════════════════════════════
# 4. Napájení
# ═════════════════════════════════════════════════════════════════════════════

class TestPower(unittest.TestCase):
    def tearDown(self):
        power.get().override = power.PowerState()
        power.get().set_policy(power.DEFAULT_POLICY)

    def test_policy_matrix(self):
        ac = power.PowerState(on_battery=False, percent=50, has_battery=True)
        bat = power.PowerState(on_battery=True, percent=60, has_battery=True)
        low = power.PowerState(on_battery=True, percent=15, has_battery=True)
        saver = power.PowerState(on_battery=True, percent=80, saver=True, has_battery=True)
        d = power.decide
        self.assertEqual(d(ac, "pause", True), power.RUN)
        self.assertEqual(d(bat, "throttle", True), power.THROTTLE)
        self.assertEqual(d(bat, "throttle", False), power.THROTTLE)
        self.assertEqual(d(low, "throttle", True), power.PAUSE)
        self.assertEqual(d(low, "throttle", False), power.THROTTLE, "lehká práce nikdy nestojí")
        self.assertEqual(d(saver, "throttle", True), power.PAUSE)
        self.assertEqual(d(bat, "pause", True), power.PAUSE)
        self.assertEqual(d(low, "full", True), power.RUN)

    def test_read_system_is_sane(self):
        st = power.read_system()
        self.assertIsInstance(st.on_battery, bool)
        self.assertTrue(st.percent is None or 0 <= st.percent <= 100)

    def test_governor_throttles_heavy_work_on_battery(self):
        g = governor.Governor(quiet_s=0)
        power.get().override = power.PowerState(on_battery=True, percent=70, has_battery=True)
        with mock.patch.object(power, "HEAVY_PAUSE_S", 0.05):
            t0 = time.perf_counter()
            for _ in range(4):
                self.assertFalse(g.checkpoint(heavy=True))
            self.assertGreaterEqual(time.perf_counter() - t0, 0.2)
        self.assertEqual(g.throttled_items, 4)

    def test_governor_pauses_until_plugged_in(self):
        g = governor.Governor(quiet_s=0)
        power.get().override = power.PowerState(on_battery=True, percent=10, has_battery=True)
        done = threading.Event()
        with mock.patch.object(governor, "POWER_POLL_S", 0.02), mock.patch.object(power, "HEAVY_PAUSE_S", 0):
            th = threading.Thread(target=lambda: (g.checkpoint(heavy=True), done.set()))
            th.start()
            time.sleep(0.15)
            self.assertFalse(done.is_set(), "těžká práce na slabé baterii stojí")
            self.assertTrue(g.power_paused)
            power.get().override = power.PowerState(on_battery=False, percent=11, has_battery=True)
            th.join(2)
        self.assertTrue(done.is_set(), "po připojení k síti pokračuje")
        self.assertFalse(g.power_paused)

    def test_pause_honours_stop(self):
        g = governor.Governor(quiet_s=0)
        power.get().override = power.PowerState(on_battery=True, percent=5, has_battery=True)
        with mock.patch.object(governor, "POWER_POLL_S", 0.01):
            self.assertTrue(g.checkpoint(lambda: True, heavy=True), "ukončení aplikace nečeká na síť")

    def test_eco_qos_toggle(self):
        self.assertTrue(power.set_thread_eco(True))
        self.assertTrue(power.set_thread_eco(False))


if __name__ == "__main__":
    unittest.main()
