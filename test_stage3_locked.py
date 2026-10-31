"""Stage 3: složka Skryté 🔒 (úroveň 1) – hranice viditelnosti, šifrování miniatur, odemčení,
auto-zamčení a ochrana obrazovky.

Spuštění (ve složce projektu):   python -m pytest test_stage3_locked.py -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date
from unittest import mock

import numpy as np
from PIL import Image

import database
import locked_folder
import paths
import preview_cache

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEvent, QPointF
    from PyQt6.QtWidgets import QApplication, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    import main                                          # před vytvořením QApplication
    _APP = [QApplication.instance() or QApplication(sys.argv)]
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False


def _vec(seed, dim=512):
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return (v / np.linalg.norm(v)).tobytes()


class Env(unittest.TestCase):
    """Dočasná DB + složky; knihovna: viditelná fotka A, fotka B (ke skrytí), video V (ke skrytí),
    fotka C v koši. Všechny mají GPS, vektory, OCR, tváře a jsou v jedné události a albu."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_s3_")
        self._saved = database.DB_PATH, paths.THUMB_DIR, paths.PREVIEW_DIR, paths.PROXY_DIR, paths.HIGHLIGHT_DIR
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        paths.THUMB_DIR = os.path.join(self.tmp, "thumbs")
        paths.PREVIEW_DIR = os.path.join(self.tmp, "previews")
        paths.PROXY_DIR = os.path.join(self.tmp, "proxies")
        paths.HIGHLIGHT_DIR = os.path.join(self.tmp, "highlights")
        for d in (paths.THUMB_DIR, paths.PREVIEW_DIR, paths.PROXY_DIR, paths.HIGHLIGHT_DIR):
            os.makedirs(d, exist_ok=True)
        database.init_db()
        locked_folder.set_hidden_paths([])
        self.vault = locked_folder.Vault(os.path.join(self.tmp, "vault"))
        self.ids = {}
        with database.db_transaction() as conn:
            conn.execute("INSERT INTO events (id, title, location_name, start_date, end_date) "
                         "VALUES (1, 'Výlet', 'Praha', '2023-10-31', '2023-10-31')")
            conn.execute("INSERT INTO people (id, name) VALUES (1, 'Anna')")
            for i, key in enumerate(("A", "B", "V", "C")):
                path = self._jpg(f"IMG_{key}.jpg", (40 * i, 100, 200))
                thumb = self._jpg(f"thumbs/thumb_{key}.jpg", (40 * i, 100, 200), (96, 72))
                mtype = "video" if key == "V" else "photo"
                mid = conn.execute(
                    "INSERT INTO media_items (file_path, file_name, media_type, thumbnail_path, date_taken, "
                    "latitude, longitude, place_city, event_id, phash, faces_scanned, is_trashed) "
                    "VALUES (?, ?, ?, ?, ?, 50.08, 14.42, 'Praha', 1, 'ffff0000ffff0000', 1, ?)",
                    (path, os.path.basename(path), mtype, thumb, f"2023-10-31 1{i}:00:00", int(key == "C"))).lastrowid
                self.ids[key] = mid
                conn.execute("INSERT INTO image_embeddings (media_id, embedding, source_sig) VALUES (?, ?, 'x')",
                             (mid, _vec(mid)))
                conn.execute("INSERT INTO siglip_embeddings (media_id, embedding, source_sig) VALUES (?, ?, 'x')",
                             (mid, _vec(mid, 768)))
                conn.execute("INSERT INTO media_ocr (media_id, text, source_sig) VALUES (?, ?, 'x')",
                             (mid, f"tajny{key.lower()} dokument"))
                crop = self._jpg(f"face_{key}.jpg", (1, 2, 3), (16, 16))
                fid = conn.execute("INSERT INTO face_instances (media_id, face_crop_path, embedding, person_id, "
                                   "bbox_x, bbox_y, bbox_w, bbox_h, is_valid) VALUES (?, ?, ?, 1, .1, .1, .3, .3, 1)",
                                   (mid, crop, _vec(mid + 100))).lastrowid
                if key == "V":
                    conn.execute("INSERT INTO video_frame_embeddings (media_id, frame_ms, embedding, source_sig) "
                                 "VALUES (?, 500, ?, 'x')", (mid, _vec(mid + 7)))
                if key == "B":
                    conn.execute("UPDATE people SET cover_face_path = ?, key_face_id = ? WHERE id = 1", (crop, fid))
            database._sync_geo(conn)
        import albums
        conn = database.get_connection()
        try:
            albums.ensure_tables(conn)
            self.album = albums.create_album(conn, "Vše", [self.ids[k] for k in "ABV"])
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        (database.DB_PATH, paths.THUMB_DIR, paths.PREVIEW_DIR, paths.PROXY_DIR, paths.HIGHLIGHT_DIR) = self._saved
        locked_folder.set_hidden_paths([])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jpg(self, name, color, size=(320, 240)):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Image.new("RGB", size, color).save(path, "JPEG")
        return path

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def thumb_of(self, key):
        return self.q("SELECT thumbnail_path FROM media_items WHERE id = ?", (self.ids[key],))[0][0]

    def hide(self, *keys):
        self.vault.setup("123456") if not self.vault.is_configured() else None
        return locked_folder.hide_items([self.ids[k] for k in keys], self.vault)


# ═════════════════════════════════════════════════════════════════════════════
# Hranice viditelnosti v DB
# ═════════════════════════════════════════════════════════════════════════════

class TestVisibilityBoundary(Env):

    def setUp(self):
        super().setUp()
        self.thumb_b = self.thumb_of("B")
        self.assertEqual(self.hide("B", "V"), 2)
        self.B, self.V, self.A = self.ids["B"], self.ids["V"], self.ids["A"]
        self.hidden = {self.B, self.V}

    def assertNoHidden(self, ids, where):
        leaked = self.hidden & {int(i) for i in ids}
        self.assertFalse(leaked, f"{where}: skryté položky {leaked} unikly")

    def test_flags_set(self):
        rows = dict(self.q("SELECT id, is_hidden FROM media_items"))
        self.assertEqual(rows[self.B], 1)
        self.assertEqual(rows[self.A], 0)
        self.assertIsNotNone(self.q("SELECT hidden_at FROM media_items WHERE id = ?", (self.B,))[0][0])

    def test_derived_data_purged(self):
        for table in ("image_embeddings", "siglip_embeddings", "video_frame_embeddings", "media_ocr",
                      "face_instances", "media_geo_index"):
            col = "id" if table == "media_geo_index" else "media_id"
            self.assertNoHidden([r[0] for r in self.q(f"SELECT {col} FROM {table}")], table)
        dup = self.q("SELECT is_duplicate, duplicate_group_id, faces_scanned FROM media_items WHERE id = ?", (self.B,))
        self.assertEqual(dup[0], (0, None, 0))

    def test_library_and_collections(self):
        self.assertNoHidden([r[0] for r in main.fetch_media_records()] if HAS_QT else [], "knihovna")
        if HAS_QT:
            self.assertNoHidden([r[0] for r in main.fetch_media_records(trashed=True)], "koš")
            self.assertEqual({r[0] for r in main.fetch_media_records(hidden=True)}, self.hidden)
            self.assertNoHidden([r[0] for r in main.fetch_media_records("m.event_id = 1")], "událost")
            self.assertNoHidden([p for p, _ in main.preview_build_order()
                                 for p in [self.q("SELECT id FROM media_items WHERE file_path = ?", (p,))[0][0]]],
                                "pořadí náhledů")

    def test_ai_queues_exclude_hidden(self):
        # vektory chybí (smazané) – fronty je přesto nesmí znovu počítat
        self.assertNoHidden([r[0] for r in database.get_media_needing_embedding()], "fronta CLIP")
        self.assertNoHidden([r[0] for r in database.get_media_needing_siglip()], "fronta SigLIP")
        self.assertNoHidden([r[0] for r in database.get_media_needing_ocr()], "fronta OCR")
        self.assertNoHidden([r[0] for r in database.get_videos_needing_frames()], "fronta snímků videí")
        self.assertNoHidden([r[0] for r in database.get_media_pending_face_scan()], "fronta tváří")

    def test_search_sources_exclude_hidden(self):
        import search_engine
        self.assertNoHidden(database.get_all_embeddings(), "vektory")
        self.assertNoHidden(database.get_search_vectors(), "vektory hledání")
        self.assertNoHidden(database.get_search_vectors([self.A, self.B, self.V]), "vektory hledání (ids)")
        self.assertNoHidden(database.get_siglip_vectors(), "SigLIP")
        self.assertNoHidden(database.get_siglip_vectors([self.A, self.B]), "SigLIP (ids)")
        vocab = search_engine.load_vocab()
        self.assertNoHidden(vocab.items, "slovník hledání")
        self.assertNoHidden(vocab.ocr, "OCR v hledání")
        have, need = database.siglip_coverage()
        self.assertLessEqual(have, 2)

    def test_map_memories_reels_echoes_journey(self):
        import echoes
        import journey
        import memories
        import reels
        self.assertNoHidden([r[0] for r in database.query_geo_bounds(-90, 90, -180, 180)], "mapa")
        conn = database.get_connection()
        try:
            mem = memories.find_memory(conn, today=date(2026, 10, 31), min_photos=1)
            self.assertNoHidden((mem or {}).get("ids", []), "vzpomínky")
            for s in reels.suggestions(conn, today=date(2026, 10, 31)):
                self.assertNoHidden([s.get("cover_id")], "návrh sestřihu")
                self.assertNoHidden([c["id"] for c in reels.candidates(conn, s["key"], today=date(2026, 10, 31))],
                                    f"sestřih {s['key']}")
            items, _ = echoes.load_candidates(conn)
            self.assertNoHidden([it["id"] if isinstance(it, dict) else it[0] for it in items], "ozvěny")
            data = journey.build_journey(conn, 1) or {}
            ids = [p.get("id") for stop in data.get("stops", []) for p in stop.get("photos", [])]
            self.assertNoHidden(ids, "Cesta")
        finally:
            conn.close()

    def test_albums_events_people_location(self):
        import albums
        import events_view
        import location_borrow
        conn = database.get_connection()
        try:
            self.assertEqual(albums.item_ids(conn, self.album), [self.A])
            self.assertEqual([a["count"] for a in albums.list_albums(conn)], [1])
            cards = events_view.fetch_event_cards(conn)
            self.assertEqual(len(cards), 1)
            self.assertNotIn(cards[0].get("cover", {}).get("id"), self.hidden)
            conn.execute("UPDATE media_items SET latitude = NULL, longitude = NULL WHERE id = ?", (self.B,))
            self.assertNoHidden(location_borrow.borrow(conn), "vypůjčená poloha")
            conn.rollback()
        finally:
            conn.close()
        cover = self.q("SELECT cover_face_path, key_face_id FROM people WHERE id = 1")[0]
        self.assertNotIn("face_B", cover[0] or "", "portrét osoby nesmí zůstat ze skryté fotky")

    def test_duplicates_exclude_hidden(self):
        import ai_engine
        conn = database.get_connection()
        try:
            pairs = ai_engine.get_duplicate_pairs(conn, max_hamming=64, max_time_diff_sec=10 ** 9)
        finally:
            conn.close()
        self.assertNoHidden([i for p in pairs for i in (p[0], p[2])], "duplicity")

    def test_write_guards_reject_hidden(self):
        """Démon počítal položku, mezitím ji uživatel skryl: výsledek se neuloží."""
        database.save_image_embedding(self.B, _vec(1), "sig")
        database.save_siglip_embeddings([(self.B, _vec(1, 768), "sig"), (self.A, _vec(2, 768), "sig")])
        database.save_ocr_results([(self.B, "tajne", "sig")])
        database.save_video_frame_embeddings(self.V, [(100, _vec(3))], "sig")
        crop = self._jpg("late_face.jpg", (5, 5, 5), (16, 16))
        database.save_faces_for_media(self.B, [{"crop_path": crop, "embedding": _vec(4)}])
        for table in ("image_embeddings", "siglip_embeddings", "media_ocr", "video_frame_embeddings", "face_instances"):
            self.assertNoHidden([r[0] for r in self.q(f"SELECT media_id FROM {table}")], f"zápis do {table}")
        self.assertFalse(os.path.exists(crop), "ořez tváře skryté fotky smazán")
        self.assertEqual(self.q("SELECT COUNT(*) FROM siglip_embeddings WHERE media_id = ?", (self.A,))[0][0], 1)

    def test_hidden_cannot_be_trashed_or_rehidden(self):
        self.assertEqual(database.trash_media([self.B]), 0)
        self.assertEqual(database.hide_media([self.B])["rows"], [])
        self.assertEqual(database.hide_media([self.ids["C"]])["rows"], [], "položka v koši se neskrývá")

    def test_unhide_restores_visibility(self):
        self.vault.unlock_with_passcode("123456")
        self.assertEqual(locked_folder.unhide_items([self.B], self.vault), 1)
        self.assertIn(self.B, [r[0] for r in database.get_media_needing_embedding()], "vektor se dopočítá")
        self.assertIn(self.B, [r[0] for r in database.get_media_pending_face_scan()], "tváře se dopočítají")
        self.assertIn(self.B, [r[0] for r in self.q("SELECT id FROM media_geo_index")], "zpět na mapě")
        thumb = self.thumb_of("B")
        self.assertTrue(thumb and os.path.exists(thumb), "miniatura dešifrována zpět")
        self.assertFalse(os.path.exists(self.vault.thumb_path(self.B)))
        self.assertFalse(locked_folder.is_hidden_path(self.q("SELECT file_path FROM media_items WHERE id = ?",
                                                             (self.B,))[0][0]))


# ═════════════════════════════════════════════════════════════════════════════
# Soubory: šifrované miniatury, úklid nešifrovaných stop
# ═════════════════════════════════════════════════════════════════════════════

class TestHideFiles(Env):

    def test_thumbnail_sealed_and_plaintext_shredded(self):
        thumb = self.thumb_of("B")
        with open(thumb, "rb") as f:
            original = f.read()
        self.hide("B")
        self.assertFalse(os.path.exists(thumb), "nešifrovaná miniatura smazána")
        self.assertIsNone(self.thumb_of("B"))
        with open(self.vault.thumb_path(self.ids["B"]), "rb") as f:
            blob = f.read()
        self.assertTrue(blob.startswith(locked_folder.THUMB_MAGIC))
        self.assertNotIn(original[:64], blob, "obsah miniatury není na disku čitelný")
        self.assertNotIn(b"\xff\xd8\xff", blob[len(locked_folder.THUMB_MAGIC) + locked_folder.NONCE_LEN:][:3])
        self.vault.lock()
        with self.assertRaises(locked_folder.VaultLocked):
            self.vault.open_thumbnail(self.ids["B"])
        self.assertTrue(self.vault.unlock_with_passcode("123456"))
        self.assertEqual(self.vault.open_thumbnail(self.ids["B"]), original)

    def test_caches_purged_and_guarded(self):
        src = self.q("SELECT file_path FROM media_items WHERE id = ?", (self.ids["B"],))[0][0]
        if HAS_QT:
            from PyQt6.QtGui import QImage
            img = QImage(src)
            self.assertIsNotNone(preview_cache.save_preview(src, img))
        strip = preview_cache.base_path(src) + ".scrub"
        open(strip, "wb").write(b"x" * 10)
        import video_proxy
        open(video_proxy.proxy_path(src), "wb").write(b"x")
        self.hide("B")
        self.assertEqual(locked_folder.cache_files_for(src), [], "náhledy, pás i proxy smazány")
        self.assertTrue(locked_folder.is_hidden_path(src))
        if HAS_QT:
            self.assertIsNone(preview_cache.save_preview(src, img), "nový náhled skrytého se neuloží")
            self.assertIsNone(preview_cache.build_preview(src, "photo"))
        self.assertIsNone(video_proxy.build_proxy(src))
        import card_media
        self.assertIsNone(card_media.build({"source": src}))
        import scrub_strip
        self.assertIsNone(scrub_strip.ScrubStrip(src).save())
        self.assertFalse(os.path.exists(strip))

    def test_face_crops_shredded(self):
        crop = self.q("SELECT face_crop_path FROM face_instances WHERE media_id = ?", (self.ids["B"],))[0][0]
        self.assertTrue(os.path.exists(crop))
        self.hide("B")
        self.assertFalse(os.path.exists(crop))

    def test_hide_needs_configured_vault(self):
        with self.assertRaises(locked_folder.VaultLocked):
            locked_folder.hide_items([self.ids["B"]], self.vault)
        self.assertEqual(self.q("SELECT is_hidden FROM media_items WHERE id = ?", (self.ids["B"],))[0][0], 0)

    def test_hide_while_locked_uses_os_key(self):
        """Skrytí nic neodhaluje: jde i zamčené (klíč tiše z DPAPI), zobrazení až po odemčení."""
        self.vault.setup("123456")
        self.vault.lock()
        self.assertEqual(locked_folder.hide_items([self.ids["B"]], self.vault), 1)
        self.assertFalse(self.vault.is_unlocked, "skrytí složku neodemkne")
        self.vault.unlock_with_os()
        self.assertIsNotNone(self.vault.open_thumbnail(self.ids["B"]))

    def test_render_sealed_thumbnail_never_touches_disk_plain(self):
        self.hide("B")
        self.vault.delete_thumbnail(self.ids["B"])
        before = set(os.listdir(paths.THUMB_DIR))
        src = self.q("SELECT file_path FROM media_items WHERE id = ?", (self.ids["B"],))[0][0]
        data = locked_folder.render_sealed_thumbnail(self.ids["B"], src, "photo", None, self.vault)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertEqual(set(os.listdir(paths.THUMB_DIR)), before)
        self.assertTrue(os.path.exists(self.vault.thumb_path(self.ids["B"])))

    def test_startup_sweep(self):
        self.hide("B")
        mid = self.ids["B"]
        leftover = os.path.join(paths.THUMB_DIR, f"thumb_{mid}.jpg")
        open(leftover, "wb").write(b"\xff\xd8 plain")
        orphan = self.vault.thumb_path(self.ids["A"])          # A není skrytá
        open(orphan, "wb").write(b"KVT1 orphan")
        locked_folder.set_hidden_paths([])
        locked_folder.startup_sweep(self.vault)
        self.assertFalse(os.path.exists(leftover))
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(self.vault.thumb_path(mid)))
        self.assertTrue(locked_folder.is_hidden_path(self.q("SELECT file_path FROM media_items WHERE id = ?",
                                                            (mid,))[0][0]))


# ═════════════════════════════════════════════════════════════════════════════
# Trezor: klíč, heslo, DPAPI, zámek
# ═════════════════════════════════════════════════════════════════════════════

class TestVault(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_s3v_")
        self.now = [1000.0]
        self.vault = locked_folder.Vault(self.tmp, clock=lambda: self.now[0])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_setup_unlock_lock(self):
        self.assertFalse(self.vault.is_configured())
        with self.assertRaises(locked_folder.VaultError):
            self.vault.setup("123")                       # krátké heslo
        self.vault.setup("tajne-heslo")
        self.assertTrue(self.vault.is_unlocked)
        key = self.vault._key
        events = []
        self.vault.add_lock_listener(lambda: events.append(1))
        self.assertTrue(self.vault.lock())
        self.assertEqual(bytes(key), b"\0" * locked_folder.KEY_LEN, "klíč přepsán nulami")
        self.assertEqual(events, [1])
        self.assertFalse(self.vault.lock(), "druhé zamčení nic nedělá")
        self.assertFalse(self.vault.unlock_with_passcode("spatne-heslo"))
        self.assertTrue(self.vault.unlock_with_passcode("tajne-heslo"))

    def test_vault_file_holds_no_plain_key(self):
        self.vault.setup("tajne-heslo")
        key = bytes(self.vault._key)
        with open(self.vault.meta_path, "rb") as f:
            raw = f.read()
        import base64
        self.assertNotIn(key, raw)
        self.assertNotIn(base64.b64encode(key), raw)
        self.assertNotIn(b"tajne-heslo", raw)

    def test_lockout_after_failures_persists(self):
        self.vault.setup("tajne-heslo")
        self.vault.lock()
        for _ in range(locked_folder.FREE_ATTEMPTS - 1):
            self.assertFalse(self.vault.unlock_with_passcode("x" * 6))
        self.assertEqual(self.vault.lockout_remaining(), 0)
        self.assertFalse(self.vault.unlock_with_passcode("x" * 6))
        self.assertEqual(self.vault.lockout_remaining(), locked_folder.LOCKOUT_BASE_S)
        again = locked_folder.Vault(self.tmp, clock=lambda: self.now[0])     # restart aplikace
        with self.assertRaises(locked_folder.LockedOut):
            again.unlock_with_passcode("tajne-heslo")
        self.now[0] += locked_folder.LOCKOUT_BASE_S + 1
        self.assertFalse(again.unlock_with_passcode("y" * 6))
        self.assertEqual(again.lockout_remaining(), locked_folder.LOCKOUT_BASE_S * 2, "prodleva se zdvojuje")
        self.now[0] += 1000
        self.assertTrue(again.unlock_with_passcode("tajne-heslo"))
        self.assertEqual(again._read_meta()["failures"], 0)

    def test_dpapi_roundtrip_and_os_unlock(self):
        blob = locked_folder.dpapi_protect(b"abc")
        self.assertNotIn(b"abc", blob)
        self.assertEqual(locked_folder.dpapi_unprotect(blob), b"abc")
        self.vault.setup("tajne-heslo")
        key = bytes(self.vault._key)
        self.vault.lock()
        self.vault.unlock_with_os()
        self.assertEqual(bytes(self.vault._key), key)

    def test_thumbnail_bound_to_id_and_tamper_evident(self):
        self.vault.setup("tajne-heslo")
        self.vault.seal_thumbnail(7, b"\xff\xd8 jpeg 7")
        self.assertEqual(self.vault.open_thumbnail(7), b"\xff\xd8 jpeg 7")
        shutil.copy(self.vault.thumb_path(7), self.vault.thumb_path(8))     # podstrčení jiné položce
        self.assertIsNone(self.vault.open_thumbnail(8))
        with open(self.vault.thumb_path(7), "r+b") as f:
            f.seek(-1, os.SEEK_END)
            last = f.read(1)
            f.seek(-1, os.SEEK_END)
            f.write(bytes([last[0] ^ 1]))
        self.assertIsNone(self.vault.open_thumbnail(7), "upravený soubor neprojde tagem")

    def test_change_passcode(self):
        self.vault.setup("prvni-heslo")
        self.vault.seal_thumbnail(1, b"data")
        self.assertFalse(self.vault.change_passcode("spatne", "druhe-heslo"))
        self.assertTrue(self.vault.change_passcode("prvni-heslo", "druhe-heslo"))
        self.vault.lock()
        self.assertFalse(self.vault.unlock_with_passcode("prvni-heslo"))
        self.assertTrue(self.vault.unlock_with_passcode("druhe-heslo"))
        self.assertEqual(self.vault.open_thumbnail(1), b"data", "klíč se nemění, jen obal")

    def test_hello_probe_does_not_raise(self):
        self.assertIsInstance(locked_folder.hello_available(), bool)


# ═════════════════════════════════════════════════════════════════════════════
# Qt: auto-zamčení, ochrana obrazovky, mřížka
# ═════════════════════════════════════════════════════════════════════════════

def _pump(ms=50):
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestLockMechanics(unittest.TestCase):

    def setUp(self):
        import locked_ui
        self.ui = locked_ui
        self.tmp = tempfile.mkdtemp(prefix="omni_s3q_")
        self.vault = locked_folder.Vault(self.tmp)
        self.vault.setup("tajne-heslo")
        self.vault.lock()
        self.win = QWidget()
        self.win.resize(300, 200)
        self.win.show()
        _pump(80)
        self.ctl = locked_ui.LockedFolderController(self.win, self.vault, idle_s=0.3)
        self.reasons = []
        self.ctl.locked.connect(self.reasons.append)

    def tearDown(self):
        self.ctl.shutdown()
        self.ctl._probe.wait(3000)
        self.win.close()
        self.win.deleteLater()
        _pump(20)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def unlock(self):
        self.assertTrue(self.vault.unlock_with_passcode("tajne-heslo"))
        self.assertTrue(self.ctl.request_unlock())      # odemčeno: jen zapne štít a odpočet

    def test_capture_shield_follows_lock_state(self):
        hwnd = int(self.win.winId())
        self.assertEqual(self.ui.get_capture_affinity(hwnd), 0)
        self.unlock()
        self.assertEqual(self.ui.get_capture_affinity(hwnd), self.ui.WDA_EXCLUDEFROMCAPTURE,
                         "odemčeno: okno mimo snímky, sdílení i Alt+Tab")
        self.ctl.lock("manual")
        self.assertEqual(self.ui.get_capture_affinity(hwnd), 0)
        self.assertEqual(self.reasons, ["manual"])
        self.assertFalse(self.vault.is_unlocked)

    def test_idle_auto_lock(self):
        self.unlock()
        _pump(150)
        self.assertTrue(self.vault.is_unlocked)
        _pump(450)
        self.assertFalse(self.vault.is_unlocked, "po nečinnosti zamčeno")
        self.assertEqual(self.reasons, ["idle"])

    def test_activity_postpones_idle_lock(self):
        self.unlock()
        for _ in range(4):
            _pump(150)
            QApplication.sendEvent(self.win, _key_event())
        self.assertTrue(self.vault.is_unlocked, "vstup posouvá odpočet")
        _pump(500)
        self.assertFalse(self.vault.is_unlocked)

    def test_session_lock_messages(self):
        S = self.ui.SessionWatcher
        self.assertTrue(S.is_lock_message(self.ui.WM_WTSSESSION_CHANGE, self.ui.WTS_SESSION_LOCK))
        self.assertTrue(S.is_lock_message(self.ui.WM_POWERBROADCAST, self.ui.PBT_APMSUSPEND))
        self.assertFalse(S.is_lock_message(self.ui.WM_WTSSESSION_CHANGE, 0x8))     # odemčení relace
        self.unlock()
        self.ctl.session._on_lock("session")
        self.assertEqual(self.reasons, ["session"])
        self.assertTrue(self.ctl._session_registered, "WTS oznámení zaregistrováno")

    def test_unlock_sheet_passcode_and_lockout(self):
        sheet = self.ui.UnlockSheet(self.vault, "unlock", hello=False, parent=self.win)
        sheet.edit_old.setText("spatne-heslo")
        sheet._submit()
        self.assertFalse(self.vault.is_unlocked)
        self.assertIn("Špatné", sheet.lbl_error.text())
        sheet.edit_old.setText("tajne-heslo")
        sheet._submit()
        self.assertTrue(self.vault.is_unlocked)
        self.assertEqual(sheet.method, "passcode")
        self.assertEqual(sheet.edit_old.text(), "", "heslo z widgetu smazáno")

    def test_setup_sheet_mismatch(self):
        v = locked_folder.Vault(os.path.join(self.tmp, "new"))
        sheet = self.ui.UnlockSheet(v, "setup", parent=self.win)
        sheet.edit_new.setText("abcdef")
        sheet.edit_repeat.setText("abcdeg")
        sheet._submit()
        self.assertFalse(v.is_configured())
        sheet.edit_repeat.setText("abcdef")
        sheet._submit()
        self.assertTrue(v.is_configured() and v.is_unlocked)

    def test_hello_result_unlocks_via_os_key(self):
        sheet = self.ui.UnlockSheet(self.vault, "unlock", hello=False, parent=self.win)
        sheet._on_hello(locked_folder.HELLO_CANCELED)
        self.assertFalse(self.vault.is_unlocked)
        sheet._on_hello(locked_folder.HELLO_VERIFIED)
        self.assertTrue(self.vault.is_unlocked)
        self.assertEqual(sheet.method, "hello")


def _key_event():
    from PyQt6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowHidden(Env):
    """Celé okno: sidebar, odemčení, mřížka z trezoru, zamčení při minimalizaci / odchodu."""

    def setUp(self):
        super().setUp()
        self.hide("B")
        self.vault.lock()
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config"),
                  mock.patch.object(locked_folder, "_vault", self.vault)):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w.resize(1200, 800)
        self.w.show()
        _pump(100)
        self.addCleanup(self._close)
        self.model = self.w.grid_view.photo_model

    def _close(self):
        self.w.nav.reset()
        self.w.close()
        self.w.locked._probe.wait(3000)
        self.w.deleteLater()
        _pump(30)

    def open_hidden(self, unlock=True):
        def fake_unlock(parent=None):
            if unlock:
                self.vault.unlock_with_passcode("123456")
                self.w.locked._on_unlocked()
            return unlock
        with mock.patch.object(self.w.locked, "request_unlock", side_effect=fake_unlock):
            self.w._on_sidebar("hidden")

    def wait_thumbs(self):
        for _ in range(100):
            for r in range(self.model.rowCount()):
                self.model.data(self.model.index(r, 0), main.ApplePhotoModel.ThumbRole)
            if all(self.model.ram_cache.get(int(r[0])) is not None for r in self.model.records):
                return
            _pump(30)

    def test_sidebar_entry_and_preference(self):
        item = self.w.sidebar_nav.item("hidden")
        self.assertIsNotNone(item)
        self.assertEqual(item.text() if hasattr(item, "text") else "", "Skryté 🔒")
        self.assertFalse(item.isHidden())
        self.w.config["show_hidden_folder"] = False
        self.w._sync_hidden_entry()
        self.assertTrue(item.isHidden())

    def test_library_never_shows_hidden(self):
        ids = {int(r[0]) for r in self.w._filtered_records}
        self.assertNotIn(self.ids["B"], ids)
        self.assertIn(self.ids["A"], ids)

    def test_cancelled_unlock_stays_out(self):
        self.open_hidden(unlock=False)
        self.assertNotEqual(self.w._current_filter, "hidden")
        self.assertEqual(self.w._hidden_records, [])

    def test_unlock_shows_decrypted_grid_then_lock_on_navigation(self):
        self.open_hidden()
        self.assertEqual(self.w._current_filter, "hidden")
        self.assertEqual([int(r[0]) for r in self.w._filtered_records], [self.ids["B"]])
        self.wait_thumbs()
        self.assertIsNotNone(self.model.ram_cache.get(self.ids["B"]), "miniatura dešifrována do RAM")
        self.assertEqual(self.w.locked.shield_affinity, 0x11)
        self.w._on_sidebar("photos")
        self.assertFalse(self.vault.is_unlocked, "odchod ze Skrytých zamkne")
        self.assertIsNone(self.model.ram_cache.get(self.ids["B"]), "RAM cache vyčištěna")
        self.assertEqual(self.w._hidden_records, [])
        self.assertEqual(self.w.locked.shield_affinity, 0)

    def test_viewer_from_hidden_keeps_unlocked_then_lock_closes_it(self):
        self.open_hidden()
        self.w._on_photo_activated(0)
        _pump(80)
        self.assertTrue(self.vault.is_unlocked, "prohlížeč skryté fotky = stále ve Skrytých")
        self.w.locked.lock("idle")
        _pump(30)
        self.assertFalse(self.w.nav.contains(self.w._viewer), "zamčení zavře prohlížeč")
        self.assertEqual(self.w._viewer.media_list, [])
        self.assertEqual(self.w._filtered_records, [])

    def test_entering_from_subpage_does_not_self_lock(self):
        """Vstup do Skrytých z otevřené podstránky (nav.reset ukáže na okamžik jinou stránku)."""
        self.w._switch_page(1)
        page = main.DetailPage("Test")
        self.w.nav.push(page)
        _pump(30)
        self.open_hidden()
        _pump(60)
        self.assertTrue(self.vault.is_unlocked)
        self.assertEqual(self.w._current_filter, "hidden")

    def test_slideshow_from_hidden_stays_unlocked_and_lock_closes_it(self):
        self.open_hidden()
        self.w._on_photo_activated(0)
        _pump(50)
        view = self.w._start_slideshow(self.w._filtered_records, 0, self.model)
        _pump(80)
        self.assertTrue(self.vault.is_unlocked, "prezentace Skrytých = stále ve Skrytých")
        self.w.locked.lock("idle")
        _pump(30)
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertFalse(self.w.nav.contains(view))

    def test_minimize_locks(self):
        self.open_hidden()
        self.w.showMinimized()
        _pump(150)
        self.assertFalse(self.vault.is_unlocked)

    def test_hide_and_unhide_from_window(self):
        self.vault.unlock_with_passcode("123456")
        self.vault.lock()
        n = self.w.hide_media_ids([self.ids["A"]])
        self.assertEqual(n, 1)
        self.assertNotIn(self.ids["A"], {int(r[0]) for r in self.w._all_records})
        self.open_hidden()
        self.assertEqual({int(r[0]) for r in self.w._filtered_records}, {self.ids["A"], self.ids["B"]})
        self.assertEqual(self.w.unhide_media_ids([self.ids["A"]]), 1)
        self.assertIn(self.ids["A"], {int(r[0]) for r in self.w._all_records})
        self.assertEqual([int(r[0]) for r in self.w._filtered_records], [self.ids["B"]])

    def test_search_in_library_skips_hidden(self):
        import search_engine
        vocab = search_engine.load_vocab()
        universe = list(self.ids.values())             # i kdyby se skryté id dostalo do kolekce
        res = search_engine.run_search("tajnyb dokument", universe, database.get_search_vectors(), vocab=vocab,
                                       semantic_fn=lambda *a, **k: ([], []))
        self.assertNotIn(self.ids["B"], vocab.ocr, "text skryté fotky není v indexu OCR")
        self.assertNotIn(self.ids["B"], list(res.confident) + list(res.possible))


if __name__ == "__main__":
    unittest.main()
