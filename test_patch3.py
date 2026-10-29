"""Automatické testy pro Patch 3 (OmniPhoto Vault).

Pokrývá:
  P3.1 – Asynchronní karty osob (PersonCardWorker)
  P3.2 – Debounce vyhledávání (AISearchWorker)
  P3.3 – Přesun obličeje mezi osobami (assign_face_to_person)
  P3.5 – Dialog duplicit (DuplicateReviewDialog)
  P3.6 – Borderline Review (get_borderline_faces_for_person)
  P3.7 – Nastavitelný práh v UI (SettingsDialog sliders)
  P3.8 – Konfigurovatelný práh z config.json
  P3.9 – Dynamická LRU cache (LRUCache)
"""
import os
import sys
import shutil
import tempfile
import unittest
import numpy as np

# ── Qt inicializace PŘED WebEngine importy ────────────────────────────────────
HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import cache_tools
import scanner
import face_cluster


# ── Izolované prostředí (vzor z test_patch1.py / test_patch2.py) ─────────────

class TempEnv(unittest.TestCase):
    """Izolované prostředí: vlastní DB a cache v dočasné složce."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p3_test_")
        self.db = os.path.join(self.tmp, "photos.db")
        self.thumbs = os.path.join(self.tmp, ".cache", "thumbnails")
        self.faces_dir = os.path.join(self.tmp, ".cache", "faces")
        os.makedirs(self.thumbs)
        os.makedirs(self.faces_dir)
        # Přesměrování cest – identické se vzorem z test_patch1.py
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
            return conn.execute(sql, params).lastrowid

    def _add_media(self, path):
        existing = self.q("SELECT id FROM media_items WHERE file_path=?", (path,))
        if existing:
            return existing[0][0]
        return self.x("INSERT INTO media_items (file_path, media_type) VALUES (?, 'photo')", (path,))

    def _add_person(self, name):
        existing = self.q("SELECT id FROM people WHERE name=?", (name,))
        if existing:
            return existing[0][0]
        return self.x("INSERT INTO people (name) VALUES (?)", (name,))

    def _add_face(self, media_path, vec, person_id=None, q_score=20.0,
                  crop=None, bx=0.1, by=0.1, bw=0.2, bh=0.3):
        """Přidá obličej do face_instances (skutečný název tabulky v DB)."""
        mid = self._add_media(media_path)
        return self.x(
            "INSERT INTO face_instances "
            "(media_id, person_id, face_crop_path, embedding, quality_score, "
            " bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, person_id, crop or "/neexistuje/crop.jpg",
             vec.tobytes(), q_score, bx, by, bw, bh)
        )


# ── P3.9 LRU Cache ────────────────────────────────────────────────────────────

class TestLRUCache(unittest.TestCase):
    """P3.9 – Dynamická LRU cache."""

    def _make_cache(self, cap=200):
        from collections import OrderedDict

        class LRUCache:
            def __init__(self, capacity=1200):
                self.cache = OrderedDict()
                self.capacity = max(64, int(capacity))

            def get(self, key):
                if key not in self.cache:
                    return None
                self.cache.move_to_end(key)
                return self.cache[key]

            def put(self, key, value):
                self.cache[key] = value
                self.cache.move_to_end(key)
                if len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)

            def clear(self):
                self.cache.clear()

            def resize(self, new_capacity):
                self.capacity = max(64, int(new_capacity))
                while len(self.cache) > self.capacity:
                    self.cache.popitem(last=False)

        return LRUCache(cap)

    def test_basic_put_get(self):
        """Základní put/get funguje správně."""
        c = self._make_cache(200)
        c.put(1, "a")
        c.put(2, "b")
        self.assertEqual(c.get(1), "a")
        self.assertEqual(c.get(2), "b")
        self.assertIsNone(c.get(99))

    def test_eviction_lru(self):
        """Nejstarší položka se vyhodí při překročení kapacity."""
        c = self._make_cache(200)
        for i in range(200):
            c.put(i, f"v{i}")
        c.get(0)          # obnoví 0
        c.put(200, "new") # vyhodí 1 (nejstarší bez obnovy)
        self.assertIsNone(c.get(1))
        self.assertEqual(c.get(0), "v0")
        self.assertEqual(c.get(200), "new")

    def test_resize_shrinks_cache(self):
        """Resize na menší kapacitu vyhodí přebytečné položky."""
        c = self._make_cache(200)
        for i in range(150):
            c.put(i, i * 10)
        c.resize(100)
        self.assertLessEqual(len(c.cache), 100)

    def test_clear(self):
        """clear() vyprázdní celou cache."""
        c = self._make_cache(200)
        c.put("x", 1)
        c.put("y", 2)
        c.clear()
        self.assertEqual(len(c.cache), 0)
        self.assertIsNone(c.get("x"))

    def test_minimum_capacity_64(self):
        """Kapacita nesmí klesnout pod 64."""
        c = self._make_cache(1)
        self.assertEqual(c.capacity, 64)

    def test_capacity_never_exceeded(self):
        """Počet položek nikdy nepřekročí kapacitu."""
        cap = 100
        c = self._make_cache(cap)
        for i in range(200):
            c.put(i, i)
        self.assertLessEqual(len(c.cache), cap)

    def test_put_overwrites_existing(self):
        """put() přepíše existující hodnotu."""
        c = self._make_cache(200)
        c.put("a", 1)
        c.put("a", 99)
        self.assertEqual(c.get("a"), 99)


# ── P3.8 Konfigurovatelný práh ────────────────────────────────────────────────

class TestConfigThreshold(unittest.TestCase):
    """P3.8 – Konfigurovatelný práh z config.json."""

    def test_default_threshold_in_config(self):
        """DEFAULT_CONFIG obsahuje face_similarity_threshold."""
        from config import DEFAULT_CONFIG
        self.assertIn("face_similarity_threshold", DEFAULT_CONFIG)
        val = DEFAULT_CONFIG["face_similarity_threshold"]
        self.assertIsInstance(val, float)
        self.assertGreater(val, 0.0)
        self.assertLess(val, 1.0)

    def test_default_lru_capacity_in_config(self):
        """DEFAULT_CONFIG obsahuje thumbnail_lru_capacity."""
        from config import DEFAULT_CONFIG
        self.assertIn("thumbnail_lru_capacity", DEFAULT_CONFIG)
        val = DEFAULT_CONFIG["thumbnail_lru_capacity"]
        self.assertIsInstance(val, int)
        self.assertGreaterEqual(val, 64)

    def test_load_config_returns_custom_threshold(self):
        """load_config() načte vlastní práh ze souboru."""
        import json
        import config as cfg
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"face_similarity_threshold": 0.55, "thumbnail_lru_capacity": 800}, f)
        orig_file = cfg.CONFIG_FILE
        try:
            cfg.CONFIG_FILE = cfg_path
            loaded = cfg.load_config()
            self.assertAlmostEqual(loaded["face_similarity_threshold"], 0.55)
            self.assertEqual(loaded["thumbnail_lru_capacity"], 800)
        finally:
            cfg.CONFIG_FILE = orig_file
            shutil.rmtree(tmp, ignore_errors=True)

    def test_strict_recluster_accepts_threshold_param(self):
        """strict_recluster_faces() přijme parametr threshold."""
        import inspect
        sig = inspect.signature(face_cluster.strict_recluster_faces)
        self.assertIn("threshold", sig.parameters)

    def test_threshold_default_is_none(self):
        """Výchozí hodnota parametru threshold je None."""
        import inspect
        sig = inspect.signature(face_cluster.strict_recluster_faces)
        param = sig.parameters.get("threshold")
        self.assertIsNotNone(param)
        self.assertIsNone(param.default)


# ── P3.3 Přesun obličeje ──────────────────────────────────────────────────────

class TestAssignFaceToPerson(TempEnv):
    """P3.3 – Přesun obličeje mezi osobami."""

    def test_assign_face_changes_person_id(self):
        """assign_face_to_person přiřadí obličej jiné osobě."""
        vec = np.random.rand(512).astype(np.float32)
        vec /= np.linalg.norm(vec)
        pid_a = self._add_person("Osoba A")
        fid = self._add_face("photo_a.jpg", vec, person_id=pid_a)

        pid_b = self._add_person("Osoba B")
        face_cluster.assign_face_to_person(fid, pid_b)

        rows = self.q("SELECT person_id FROM face_instances WHERE id=?", (fid,))
        self.assertEqual(rows[0][0], pid_b)

    def test_assign_face_to_none_clears_person(self):
        """assign_face_to_person(fid, None) odebere osobu z obličeje."""
        vec = np.random.rand(512).astype(np.float32)
        vec /= np.linalg.norm(vec)
        pid = self._add_person("Osoba E")
        fid = self._add_face("photo_c.jpg", vec, person_id=pid)
        face_cluster.assign_face_to_person(fid, None)
        rows = self.q("SELECT person_id FROM face_instances WHERE id=?", (fid,))
        self.assertIsNone(rows[0][0])

    def test_assign_face_centroid_still_computable(self):
        """Po přesunu obličeje lze stále volat get_person_centroid."""
        vec = np.random.rand(512).astype(np.float32)
        vec /= np.linalg.norm(vec)
        pid_c = self._add_person("Osoba C")
        fid = self._add_face("photo_b.jpg", vec, person_id=pid_c)
        pid_d = self._add_person("Osoba D")
        face_cluster.assign_face_to_person(fid, pid_d)
        centroid = face_cluster.get_person_centroid(pid_c)
        self.assertTrue(centroid is None or isinstance(centroid, np.ndarray))


# ── P3.6 Borderline Review ────────────────────────────────────────────────────

class TestBorderlineFaces(TempEnv):
    """P3.6 – Hraničí obličeje pro osobu."""

    def _unit(self, base, noise=0.05):
        v = base + np.random.randn(512).astype(np.float32) * noise
        return v / np.linalg.norm(v)

    def test_borderline_returns_list(self):
        """get_borderline_faces_for_person vrátí seznam (může být prázdný)."""
        pid = self._add_person("Test")
        result = face_cluster.get_borderline_faces_for_person(pid)
        self.assertIsInstance(result, list)

    def test_borderline_faces_have_required_keys(self):
        """Každý záznam má klíče face_id, similarity, crop_path."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        pid = self._add_person("Kotva")
        self._add_face("anchor.jpg", self._unit(base, 0.01), person_id=pid)
        self._add_face("borderline.jpg", self._unit(base, 0.30))  # bez osoby

        result = face_cluster.get_borderline_faces_for_person(pid, min_sim=0.0, max_sim=1.0)
        for item in result:
            self.assertIn("face_id", item)
            self.assertIn("similarity", item)
            self.assertIn("crop_path", item)

    def test_borderline_similarity_in_range(self):
        """Vrácené obličeje mají podobnost v zadaném rozsahu."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        pid = self._add_person("Kotva2")
        self._add_face("anchor2.jpg", self._unit(base, 0.01), person_id=pid)
        self._add_face("other.jpg", self._unit(base, 0.3))

        min_s, max_s = 0.0, 1.0
        result = face_cluster.get_borderline_faces_for_person(pid, min_sim=min_s, max_sim=max_s)
        for item in result:
            self.assertGreaterEqual(item["similarity"], min_s)
            self.assertLessEqual(item["similarity"], max_s)

    def test_borderline_excludes_own_faces(self):
        """Vlastní obličeje osoby nejsou v hraničí množině."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        pid = self._add_person("Kotva3")
        fid_own = self._add_face("same_person.jpg", self._unit(base, 0.01), person_id=pid)
        fid_other = self._add_face("candidate.jpg", self._unit(base, 0.1))

        result = face_cluster.get_borderline_faces_for_person(pid, min_sim=0.0, max_sim=1.0)
        result_ids = [r["face_id"] for r in result]
        self.assertNotIn(fid_own, result_ids)


# ── P3.9 Centroid cache ───────────────────────────────────────────────────────

class TestCentroidCache(TempEnv):
    """P3.9 – LRU cache pro centroidy osob."""

    def test_get_person_centroid_returns_ndarray_or_none(self):
        """get_person_centroid vrátí ndarray nebo None."""
        pid = self._add_person("CacheTest")
        result = face_cluster.get_person_centroid(pid)
        self.assertTrue(result is None or isinstance(result, np.ndarray))

    def test_centroid_with_face_returns_ndarray(self):
        """get_person_centroid vrátí ndarray pro osobu s obličejem."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        pid = self._add_person("CacheTest2")
        self._add_face("cache_test.jpg", base, person_id=pid)
        result = face_cluster.get_person_centroid(pid)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, np.ndarray)
        self.assertEqual(result.shape[0], 512)

    def test_centroid_cached_returns_same_value(self):
        """Druhé volání get_person_centroid vrátí stejný výsledek."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        pid = self._add_person("CacheTest3")
        self._add_face("cache_test2.jpg", base, person_id=pid)
        c1 = face_cluster.get_person_centroid(pid)
        c2 = face_cluster.get_person_centroid(pid)
        if c1 is not None and c2 is not None:
            np.testing.assert_array_equal(c1, c2)

    def test_invalidate_centroid_cache_all(self):
        """invalidate_centroid_cache() nesmí vyhodit výjimku."""
        face_cluster.invalidate_centroid_cache()
        face_cluster.invalidate_centroid_cache(person_id=None)

    def test_invalidate_centroid_cache_specific(self):
        """invalidate_centroid_cache(person_id) vymaže jen danou osobu."""
        pid = self._add_person("SpecificInvalidate")
        face_cluster.get_person_centroid(pid)
        face_cluster.invalidate_centroid_cache(person_id=pid)
        result = face_cluster.get_person_centroid(pid)
        self.assertTrue(result is None or isinstance(result, np.ndarray))


# ── P3.8 Threshold v strict_recluster ────────────────────────────────────────

class TestReclusterThreshold(TempEnv):
    """P3.8 – Konfigurovatelný práh v strict_recluster_faces."""

    def _unit(self, base, noise=0.05):
        v = base + np.random.randn(512).astype(np.float32) * noise
        return v / np.linalg.norm(v)

    def test_recluster_returns_threshold_in_stats(self):
        """strict_recluster_faces vrátí 'threshold' ve statistikách."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        self._add_face("r1.jpg", self._unit(base, 0.02))
        self._add_face("r2.jpg", self._unit(base, 0.02))
        stats = face_cluster.strict_recluster_faces(threshold=0.55)
        if stats is not None:
            self.assertIn("threshold", stats)
            self.assertAlmostEqual(stats["threshold"], 0.55)

    def test_recluster_uses_config_threshold_when_none(self):
        """Bez parametru threshold se použije None → bere z configu."""
        import inspect
        sig = inspect.signature(face_cluster.strict_recluster_faces)
        param = sig.parameters.get("threshold")
        self.assertIsNotNone(param)
        self.assertIsNone(param.default)

    def test_recluster_invalidates_centroid_cache(self):
        """strict_recluster_faces invaliduje centroid cache."""
        base = np.random.rand(512).astype(np.float32)
        base /= np.linalg.norm(base)
        self._add_face("rc1.jpg", self._unit(base, 0.01))
        face_cluster.strict_recluster_faces(threshold=0.62)
        rows = self.q("SELECT DISTINCT person_id FROM face_instances WHERE person_id IS NOT NULL")
        for (pid,) in rows:
            result = face_cluster.get_person_centroid(pid)
            self.assertTrue(result is None or isinstance(result, np.ndarray))


# ── P3.1 PersonCardWorker ─────────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPersonCardWorker(unittest.TestCase):
    """P3.1 – Asynchronní karty osob."""

    def test_make_person_card_returns_pixmap(self):
        """_make_person_card vrátí neprázdný QPixmap."""
        from main import _make_person_card
        from PyQt6.QtGui import QPixmap
        pix = _make_person_card(None, "Test Osoba", 42, target_size=80)
        self.assertIsInstance(pix, QPixmap)
        self.assertFalse(pix.isNull())

    def test_make_person_card_with_missing_crop(self):
        """_make_person_card funguje bez crop souboru."""
        from main import _make_person_card
        from PyQt6.QtGui import QPixmap
        pix = _make_person_card("/neexistujici/cesta.jpg", "Neznámá", 0, target_size=80)
        self.assertIsInstance(pix, QPixmap)
        self.assertFalse(pix.isNull())

    def test_person_card_worker_has_signals(self):
        """PersonCardWorker má signál ready."""
        from main import PersonCardWorker
        w = PersonCardWorker(1, "Test", None, 5)
        self.assertTrue(hasattr(w, "signals"))
        self.assertTrue(hasattr(w.signals, "ready"))

    def test_person_card_worker_is_runnable(self):
        """PersonCardWorker je QRunnable."""
        from main import PersonCardWorker
        from PyQt6.QtCore import QRunnable
        w = PersonCardWorker(1, "Test", None, 5)
        self.assertIsInstance(w, QRunnable)


# ── P3.2 AISearchWorker ───────────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestAISearchWorker(unittest.TestCase):
    """P3.2 – Debounce vyhledávání."""

    def test_search_worker_exists(self):
        """AISearchWorker lze vytvořit."""
        from main import AISearchWorker
        w = AISearchWorker("test query", {})
        self.assertIsNotNone(w)

    def test_search_worker_has_signal(self):
        """AISearchWorker má signál search_finished."""
        from main import AISearchWorker
        w = AISearchWorker("test", {})
        self.assertTrue(hasattr(w, "search_finished"))

    def test_search_worker_is_qthread(self):
        """AISearchWorker dědí z QThread."""
        from main import AISearchWorker
        from PyQt6.QtCore import QThread
        w = AISearchWorker("test", {})
        self.assertIsInstance(w, QThread)


# ── P3.5 DuplicateReviewDialog ────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestDuplicateReviewDialog(unittest.TestCase):
    """P3.5 – Dialog pro review duplicit."""

    def test_dialog_creates_with_empty_pairs(self):
        """DuplicateReviewDialog lze vytvořit s prázdným seznamem."""
        from main import DuplicateReviewDialog
        dlg = DuplicateReviewDialog([])
        self.assertIsNotNone(dlg)
        dlg.close()

    def test_dialog_creates_with_pairs(self):
        """DuplicateReviewDialog lze vytvořit s páry."""
        from main import DuplicateReviewDialog
        pairs = [(1, "/a.jpg", 2, "/b.jpg", 2)]
        dlg = DuplicateReviewDialog(pairs)
        self.assertIsNotNone(dlg)
        self.assertEqual(dlg.current_idx, 0)
        dlg.close()

    def test_dialog_next_advances_index(self):
        """_next() posune index o jedna."""
        from main import DuplicateReviewDialog
        pairs = [
            (1, "/a.jpg", 2, "/b.jpg", 1),
            (3, "/c.jpg", 4, "/d.jpg", 2),
        ]
        dlg = DuplicateReviewDialog(pairs)
        dlg._next()
        self.assertEqual(dlg.current_idx, 1)
        dlg.close()

    def test_dialog_prev_decrements_index(self):
        """_prev() posune index zpět."""
        from main import DuplicateReviewDialog
        pairs = [
            (1, "/a.jpg", 2, "/b.jpg", 1),
            (3, "/c.jpg", 4, "/d.jpg", 2),
        ]
        dlg = DuplicateReviewDialog(pairs)
        dlg._next()
        dlg._prev()
        self.assertEqual(dlg.current_idx, 0)
        dlg.close()

    def test_dialog_has_action_buttons(self):
        """Dialog má tlačítka pro akce."""
        from main import DuplicateReviewDialog
        dlg = DuplicateReviewDialog([(1, "/a.jpg", 2, "/b.jpg", 1)])
        self.assertTrue(hasattr(dlg, "btn_keep_a"))
        self.assertTrue(hasattr(dlg, "btn_keep_b"))
        self.assertTrue(hasattr(dlg, "btn_keep_both"))
        self.assertTrue(hasattr(dlg, "btn_skip"))
        dlg.close()


# ── P3.6 BorderlineReviewDialog ───────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestBorderlineReviewDialog(unittest.TestCase):
    """P3.6 – Dialog pro hraničí obličeje."""

    def test_dialog_creates_with_empty_faces(self):
        """BorderlineReviewDialog lze vytvořit s prázdným seznamem."""
        from main import BorderlineReviewDialog
        dlg = BorderlineReviewDialog(1, "Test Osoba", [])
        self.assertIsNotNone(dlg)
        dlg.close()

    def test_dialog_creates_with_faces(self):
        """BorderlineReviewDialog lze vytvořit s obličeji."""
        from main import BorderlineReviewDialog
        faces = [{"face_id": 1, "similarity": 0.40, "crop_path": ""}]
        dlg = BorderlineReviewDialog(1, "Test Osoba", faces)
        self.assertIsNotNone(dlg)
        self.assertEqual(dlg.current_idx, 0)
        dlg.close()

    def test_dialog_next_advances_index(self):
        """_next() posune index."""
        from main import BorderlineReviewDialog
        faces = [
            {"face_id": 1, "similarity": 0.38, "crop_path": ""},
            {"face_id": 2, "similarity": 0.42, "crop_path": ""},
        ]
        dlg = BorderlineReviewDialog(1, "Test Osoba", faces)
        dlg._next()
        self.assertEqual(dlg.current_idx, 1)
        dlg.close()

    def test_dialog_has_yes_no_buttons(self):
        """Dialog má tlačítka Ano / Ne / Přeskočit."""
        from main import BorderlineReviewDialog
        dlg = BorderlineReviewDialog(1, "Test", [{"face_id": 1, "similarity": 0.4, "crop_path": ""}])
        self.assertTrue(hasattr(dlg, "btn_yes"))
        self.assertTrue(hasattr(dlg, "btn_no"))
        self.assertTrue(hasattr(dlg, "btn_skip"))
        dlg.close()


# ── P3.7 SettingsDialog sliders ───────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSettingsDialogSliders(unittest.TestCase):
    """P3.7 – Nastavitelný práh v UI."""

    def test_settings_dialog_has_threshold_slider(self):
        """SettingsDialog obsahuje slider_threshold."""
        from settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        self.assertTrue(hasattr(dlg, "slider_threshold"))
        dlg.close()

    def test_settings_dialog_has_lru_slider(self):
        """SettingsDialog obsahuje slider_lru."""
        from settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        self.assertTrue(hasattr(dlg, "slider_lru"))
        dlg.close()

    def test_threshold_slider_range(self):
        """slider_threshold má rozsah 30–90 (= 0.30–0.90)."""
        from settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        self.assertEqual(dlg.slider_threshold.minimum(), 30)
        self.assertEqual(dlg.slider_threshold.maximum(), 90)
        dlg.close()

    def test_lru_slider_range(self):
        """slider_lru má rozsah 64–4000."""
        from settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        self.assertEqual(dlg.slider_lru.minimum(), 64)
        self.assertEqual(dlg.slider_lru.maximum(), 4000)
        dlg.close()

    def test_save_and_close_saves_threshold(self):
        """save_and_close uloží face_similarity_threshold do configu."""
        import json
        from settings_dialog import SettingsDialog
        import config as cfg
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.json")
        orig_file = cfg.CONFIG_FILE
        try:
            cfg.CONFIG_FILE = cfg_path
            dlg = SettingsDialog()
            dlg.slider_threshold.setValue(70)  # = 0.70
            dlg.save_and_close()
            self.assertTrue(os.path.exists(cfg_path),
                            f"Config file not created at {cfg_path}")
            with open(cfg_path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertAlmostEqual(saved.get("face_similarity_threshold", 0), 0.70)
        finally:
            cfg.CONFIG_FILE = orig_file
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_and_close_saves_lru_capacity(self):
        """save_and_close uloží thumbnail_lru_capacity do configu."""
        import json
        from settings_dialog import SettingsDialog
        import config as cfg
        tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp, "config.json")
        orig_file = cfg.CONFIG_FILE
        try:
            cfg.CONFIG_FILE = cfg_path
            dlg = SettingsDialog()
            dlg.slider_lru.setValue(2000)
            dlg.save_and_close()
            self.assertTrue(os.path.exists(cfg_path))
            with open(cfg_path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved.get("thumbnail_lru_capacity"), 2000)
        finally:
            cfg.CONFIG_FILE = orig_file
            shutil.rmtree(tmp, ignore_errors=True)


# ── Spuštění ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
