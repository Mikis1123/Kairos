"""Automaticke testy pro Patch 2 (OmniPhoto Vault).

Spusteni (ve slozce projektu):   python test_patch2.py -v

Testuje:
  1. Lazy rekonstrukce orezku oblicheje (reconstruct_face_crop / reconstruct_best_crop_for_person)
  2. Shlukování s centroidy a kotvami (anchors)
  3. Cannot-link pravidlo (dva oblicheje ze stejne fotky se nesmi sloucit)
  4. Quality gate (prilis male / rozmazane oblicheje jsou vyrazeny)
  5. Decode-once pipeline (preloaded_image parametr v detect_and_extract_faces)

Testy pracuji VYHRADNE v docasnych slockach; tvoje photos.db, .cache ani fotky se
nepouziji.
"""
import importlib
import os
import shutil
import sys
import tempfile
import types
import unittest

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---------------------------------------------------------------------------
# Stuby pro PyQt6 / reverse_geocoder (pokud nejsou nainstalovane)
# ---------------------------------------------------------------------------
try:
    import PyQt6.QtCore  # noqa: F401
except ImportError:
    class _Sig:
        def __init__(self): self.calls = []
        def emit(self, *a): self.calls.append(a)
        def connect(self, *a): pass

    class pyqtSignal:
        def __init__(self, *a): pass
        def __set_name__(self, owner, name): self.name = name
        def __get__(self, obj, objtype=None):
            if obj is None: return self
            return obj.__dict__.setdefault("_sig_" + self.name, _Sig())

    class QThread:
        def __init__(self, *a, **k): pass
        def isRunning(self): return False

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

import database       # noqa: E402
import face_cluster   # noqa: E402
import face_engine    # noqa: E402
import cv_io          # noqa: E402


# ---------------------------------------------------------------------------
# Pomocna zakladni trida s izolovanym prostredim
# ---------------------------------------------------------------------------
class TempEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p2_")
        self.db = os.path.join(self.tmp, "photos.db")
        self.faces_dir = os.path.join(self.tmp, ".cache", "faces")
        os.makedirs(self.faces_dir)
        self._saved_db = database.DB_PATH
        self._saved_fe_dir = face_engine.FACE_CACHE_DIR
        database.DB_PATH = self.db
        face_engine.FACE_CACHE_DIR = self.faces_dir
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._saved_db
        face_engine.FACE_CACHE_DIR = self._saved_fe_dir
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
        mid = self.q("SELECT id FROM media_items WHERE file_path=?", (path,))
        if mid:
            return mid[0][0]
        return self.x("INSERT INTO media_items (file_path, media_type) VALUES (?, 'photo')", (path,))

    def _add_face(self, media_path, vec, person=None, q_score=20.0,
                  crop=None, bx=0.1, by=0.1, bw=0.2, bh=0.3):
        mid = self._add_media(media_path)
        return self.x(
            "INSERT INTO face_instances "
            "(media_id, person_id, face_crop_path, embedding, quality_score, "
            " bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, person, crop or "/neexistuje/crop.jpg",
             vec.tobytes(), q_score, bx, by, bw, bh)
        )

    def _unit(self, rng, base, noise=0.10):
        v = base + noise * rng.normal(size=base.size).astype(np.float32)
        return (v / np.linalg.norm(v)).astype(np.float32)


# ===========================================================================
# 1. LAZY REKONSTRUKCE OREZKU (Patch 2.1)
# ===========================================================================
class TestLazyReconstruction(TempEnv):

    def _make_photo(self, name, size=(200, 150), color=(80, 120, 200)):
        p = os.path.join(self.tmp, name)
        Image.new("RGB", size, color).save(p, "JPEG")
        return p

    def test_reconstruct_creates_file_from_original(self):
        """reconstruct_face_crop() vyrizne oblicej z originalni fotky a ulozi ho."""
        photo = self._make_photo("foto.jpg", size=(400, 300))
        mid = self._add_media(photo)
        fi_id = self.x(
            "INSERT INTO face_instances "
            "(media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, NULL, ?, 0.25, 0.20, 0.50, 0.60)",
            (mid, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_face_crop(fi_id)
        self.assertIsNotNone(result, "rekonstrukce musi vratit cestu")
        self.assertTrue(os.path.exists(result), "soubor orezku musi existovat na disku")
        img = cv_io.imread(result)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape[:2], (face_engine.AVATAR_PX, face_engine.AVATAR_PX), "orez musi mit cilovou velikost AVATAR_PX")

    def test_reconstruct_returns_existing_without_rewrite(self):
        """Pokud orez existuje, reconstruct_face_crop() ho vrati bez prepisu."""
        photo = self._make_photo("foto2.jpg")
        mid = self._add_media(photo)
        existing = os.path.join(self.faces_dir, "existing.jpg")
        Image.new("RGB", (64, 64), (255, 0, 0)).save(existing, "JPEG")
        fi_id = self.x(
            "INSERT INTO face_instances "
            "(media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, ?, ?, 0.1, 0.1, 0.3, 0.4)",
            (mid, existing, b"\x00" * 2048)
        )
        mtime_before = os.path.getmtime(existing)
        result = face_engine.reconstruct_face_crop(fi_id)
        self.assertEqual(result, existing)
        self.assertAlmostEqual(os.path.getmtime(existing), mtime_before, places=1,
                               msg="existujici soubor se nesmi prepsat")

    def test_reconstruct_returns_none_for_missing_coords(self):
        """Bez bbox souradnic nelze rekonstruovat -> vrati None."""
        photo = self._make_photo("foto3.jpg")
        mid = self._add_media(photo)
        fi_id = self.x(
            "INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, NULL, ?)",
            (mid, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_face_crop(fi_id)
        self.assertIsNone(result, "bez souradnic musi vratit None")

    def test_reconstruct_returns_none_for_missing_source(self):
        """Pokud originalni fotka neexistuje, vrati None."""
        mid = self._add_media("/neexistuje/foto.jpg")
        fi_id = self.x(
            "INSERT INTO face_instances "
            "(media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, NULL, ?, 0.1, 0.1, 0.3, 0.4)",
            (mid, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_face_crop(fi_id)
        self.assertIsNone(result)

    def test_reconstruct_best_crop_finds_best_quality(self):
        """reconstruct_best_crop_for_person() vrati orez nejkvalitnejsiho oblicheje."""
        photo = self._make_photo("foto4.jpg", size=(300, 200))
        mid = self._add_media(photo)
        pid = self.x("INSERT INTO people (name) VALUES ('Testovaci osoba')")
        existing_crop = os.path.join(self.faces_dir, "low_q.jpg")
        Image.new("RGB", (64, 64), (0, 255, 0)).save(existing_crop, "JPEG")
        # Vyssi kvalita, bez orezku na disku -> bude rekonstruovan
        self.x(
            "INSERT INTO face_instances "
            "(media_id, person_id, face_crop_path, embedding, quality_score, "
            " bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, ?, NULL, ?, 50.0, 0.1, 0.1, 0.4, 0.5)",
            (mid, pid, b"\x00" * 2048)
        )
        # Nizsi kvalita, s existujicim orezkem
        self.x(
            "INSERT INTO face_instances "
            "(media_id, person_id, face_crop_path, embedding, quality_score, "
            " bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, ?, ?, ?, 10.0, 0.1, 0.1, 0.4, 0.5)",
            (mid, pid, existing_crop, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_best_crop_for_person(pid)
        self.assertIsNotNone(result)
        self.assertTrue(os.path.exists(result))

    def test_reconstruct_best_crop_returns_none_when_no_source(self):
        """Pokud zadny oblicej nelze rekonstruovat, vrati None."""
        pid = self.x("INSERT INTO people (name) VALUES ('Bez fotek')")
        mid = self._add_media("/neexistuje/foto.jpg")
        self.x(
            "INSERT INTO face_instances "
            "(media_id, person_id, face_crop_path, embedding) VALUES (?, ?, NULL, ?)",
            (mid, pid, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_best_crop_for_person(pid)
        self.assertIsNone(result)

    def test_reconstruct_updates_db_path(self):
        """Po rekonstrukci se cesta ulozi do face_instances.face_crop_path."""
        photo = self._make_photo("foto5.jpg", size=(300, 200))
        mid = self._add_media(photo)
        fi_id = self.x(
            "INSERT INTO face_instances "
            "(media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h) "
            "VALUES (?, NULL, ?, 0.1, 0.1, 0.4, 0.5)",
            (mid, b"\x00" * 2048)
        )
        result = face_engine.reconstruct_face_crop(fi_id)
        self.assertIsNotNone(result)
        stored = self.q("SELECT face_crop_path FROM face_instances WHERE id=?", (fi_id,))[0][0]
        self.assertEqual(stored, result, "cesta musi byt ulozena do DB")


# ===========================================================================
# 2. SHLUKOVÁNÍ S CENTROIDY A KOTVAMI (Patch 2.2)
# ===========================================================================
class TestClusteringCentroidsAnchors(TempEnv):

    def test_anchors_preserved_and_new_faces_attach(self):
        """Pojmenovana osoba je kotva – nove podobne oblicheje se k ni priradi."""
        rng = np.random.default_rng(42)
        A = rng.normal(size=512).astype(np.float32)
        honza = self.x("INSERT INTO people (name, cover_face_path) VALUES ('Honza', 'cover.jpg')")
        for i in range(3):
            self._add_face(f"honza_{i}.jpg", self._unit(rng, A), person=honza)
        for i in range(3):
            self._add_face(f"honza_new_{i}.jpg", self._unit(rng, A), person=None)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertEqual(stats["anchors"], 1)
        people = {r[0]: r[1] for r in self.q("SELECT id, name FROM people")}
        self.assertIn(honza, people)
        self.assertEqual(people[honza], "Honza")
        owners = {r[0] for r in self.q(
            "SELECT fi.person_id FROM face_instances fi "
            "JOIN media_items m ON m.id=fi.media_id "
            "WHERE m.file_path LIKE 'honza_new_%'"
        )}
        self.assertEqual(owners, {honza}, "nove oblicheje Honzy se priradi ke kotve")

    def test_favorite_person_is_anchor(self):
        """Oblibena osoba (is_favorite=1) je kotva i bez jmena."""
        rng = np.random.default_rng(99)
        C = rng.normal(size=512).astype(np.float32)
        fav = self.x("INSERT INTO people (name, is_favorite) VALUES (NULL, 1)")
        for i in range(3):
            self._add_face(f"fav_{i}.jpg", self._unit(rng, C), person=fav)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertIn(fav, {r[0] for r in self.q("SELECT id FROM people")})

    def test_auto_clusters_rebuilt_from_scratch(self):
        """Automaticke shluky ('Neznama osoba') se smazu a prestavuji."""
        rng = np.random.default_rng(11)
        B = rng.normal(size=512).astype(np.float32)
        # Pouzijeme konstantu z modulu (obsahuje diakritiku: "Neznámá osoba")
        stale = self.x(
            "INSERT INTO people (name) VALUES (?)", (face_cluster.UNKNOWN_NAME,)
        )
        for i in range(6):
            self._add_face(f"b_{i}.jpg", self._unit(rng, B),
                           person=stale if i < 2 else None)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertNotIn(stale, {r[0] for r in self.q("SELECT id FROM people")},
                         "stary automaticky shluk se prestavuje")
        auto = [r for r in self.q("SELECT id, name FROM people")
                if r[1] == face_cluster.UNKNOWN_NAME]
        self.assertEqual(len(auto), 1, "identita B ma vytvorit prave jednu novou osobu")

    def test_centroid_groups_similar_faces(self):
        """Centroidove shlukování spravne seskupi podobne oblicheje do dvou skupin."""
        rng = np.random.default_rng(77)
        # Pouzijeme nahodne vektory z rng a normalizujeme je – pak pridame male perturbace.
        # _unit() pouziva: v = base + noise * rng.normal(size=512)
        # Aby perturbace byla mala vuci base, musime pouzit velmi male noise.
        # Alternativa: pouzijeme pevne normalizovane vektory a pridame perturbaci rucne.
        base_A = rng.normal(size=512).astype(np.float32)
        base_A /= np.linalg.norm(base_A)
        base_B = rng.normal(size=512).astype(np.float32)
        # Ortogonalizace Gram-Schmidt
        base_B -= np.dot(base_B, base_A) * base_A
        base_B /= np.linalg.norm(base_B)

        def perturb(base, noise=0.02):
            """Mala perturbace: noise << 1 zarucuje vysokou podobnost uvnitr skupiny."""
            v = base + noise * rng.normal(size=base.size).astype(np.float32)
            return (v / np.linalg.norm(v)).astype(np.float32)

        for i in range(4):
            self._add_face(f"a_{i}.jpg", perturb(base_A))
        for i in range(4):
            self._add_face(f"b_{i}.jpg", perturb(base_B))
        face_cluster.strict_recluster_faces()

        def owner_set(prefix):
            return {r[0] for r in self.q(
                "SELECT fi.person_id FROM face_instances fi "
                "JOIN media_items m ON m.id=fi.media_id "
                "WHERE m.file_path LIKE ?", (prefix + "%",)
            )}

        owners_a = owner_set("a_")
        owners_b = owner_set("b_")
        self.assertEqual(len(owners_a), 1, "vsechny oblicheje A patri jedne osobe")
        self.assertEqual(len(owners_b), 1, "vsechny oblicheje B patri jedne osobe")
        self.assertNotIn(None, owners_a, "oblicheje A musi byt prirazeny k osobe")
        self.assertNotIn(None, owners_b, "oblicheje B musi byt prirazeny k osobe")
        self.assertNotEqual(owners_a, owners_b, "A a B jsou ruzne identity")


# ===========================================================================
# 3. CANNOT-LINK PRAVIDLO (Patch 2.2)
# ===========================================================================
class TestCannotLink(TempEnv):

    def test_two_faces_same_photo_never_same_person(self):
        """Dva oblicheje ze stejne fotky se nesmi sloucit do jedne identity."""
        rng = np.random.default_rng(5)
        A = rng.normal(size=512).astype(np.float32)
        v = self._unit(rng, A, noise=0.01)
        self._add_face("group.jpg", v)
        self._add_face("group.jpg", v)
        for i in range(2):
            self._add_face(f"solo_{i}.jpg", self._unit(rng, A, noise=0.01))
        face_cluster.strict_recluster_faces()
        same_photo = self.q(
            "SELECT fi.person_id FROM face_instances fi "
            "JOIN media_items m ON m.id=fi.media_id "
            "WHERE m.file_path='group.jpg'"
        )
        pids = [r[0] for r in same_photo if r[0] is not None]
        self.assertEqual(len(pids), len(set(pids)),
                         "dva oblicheje z jedne fotky nesmi patrit teze osobe")

    def test_cannot_link_with_anchor(self):
        """Oblicej ze stejne fotky jako kotva se nesmi pridat ke kotve."""
        rng = np.random.default_rng(13)
        A = rng.normal(size=512).astype(np.float32)
        honza = self.x("INSERT INTO people (name) VALUES ('Honza')")
        self._add_face("group.jpg", self._unit(rng, A, noise=0.01), person=honza)
        self._add_face("group.jpg", self._unit(rng, A, noise=0.01), person=None)
        face_cluster.strict_recluster_faces()
        pids_in_group = {r[0] for r in self.q(
            "SELECT fi.person_id FROM face_instances fi "
            "JOIN media_items m ON m.id=fi.media_id "
            "WHERE m.file_path='group.jpg'"
        )}
        self.assertNotEqual(len(pids_in_group), 1,
                            "oblicheje ze stejne fotky nesmi sdilet person_id")

    def test_three_faces_same_photo_all_different(self):
        """Tri oblicheje ze stejne fotky musi mit tri ruzne person_id (nebo None)."""
        rng = np.random.default_rng(17)
        A = rng.normal(size=512).astype(np.float32)
        v = self._unit(rng, A, noise=0.01)
        for _ in range(3):
            self._add_face("triple.jpg", v)
        for i in range(3):
            self._add_face(f"support_{i}.jpg", self._unit(rng, A, noise=0.01))
        face_cluster.strict_recluster_faces()
        pids = [r[0] for r in self.q(
            "SELECT fi.person_id FROM face_instances fi "
            "JOIN media_items m ON m.id=fi.media_id "
            "WHERE m.file_path='triple.jpg'"
        ) if r[0] is not None]
        self.assertEqual(len(pids), len(set(pids)),
                         "tri oblicheje z jedne fotky musi mit ruzne person_id")


# ===========================================================================
# 4. QUALITY GATE (Patch 2.2)
# ===========================================================================
class TestQualityGate(TempEnv):

    def test_low_quality_excluded_from_clustering(self):
        """Oblicheje s quality_score pod prahem se nezapocitaji do shlukování."""
        rng = np.random.default_rng(21)
        A = rng.normal(size=512).astype(np.float32)
        for i in range(4):
            self._add_face(f"good_{i}.jpg", self._unit(rng, A), q_score=20.0)
        for i in range(10):
            self._add_face(f"bad_{i}.jpg", self._unit(rng, A), q_score=1.0)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertGreater(stats.get("skipped_quality", 0), 0,
                           "nekvalitni oblicheje musi byt zaznamenany v statistice")
        auto = self.q("SELECT id FROM people WHERE name=?", (face_cluster.UNKNOWN_NAME,))
        self.assertEqual(len(auto), 1, "kvalitni oblicheje musi tvorit shluk")

    def test_tiny_bbox_excluded_when_set(self):
        """Oblicheje s prilis malym bboxem (< 2 % snimku) jsou vyrazeny."""
        rng = np.random.default_rng(33)
        A = rng.normal(size=512).astype(np.float32)
        for i in range(4):
            self._add_face(f"normal_{i}.jpg", self._unit(rng, A),
                           q_score=20.0, bw=0.15, bh=0.20)
        for i in range(5):
            self._add_face(f"tiny_{i}.jpg", self._unit(rng, A),
                           q_score=20.0, bw=0.005, bh=0.005)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertGreater(stats.get("skipped_quality", 0), 0,
                           "miniaturni oblicheje musi byt zaznamenany jako preskocene")

    def test_anchor_faces_bypass_quality_gate(self):
        """Oblicheje kotvy projdou quality gate i s nizkym skore."""
        rng = np.random.default_rng(55)
        A = rng.normal(size=512).astype(np.float32)
        honza = self.x("INSERT INTO people (name) VALUES ('Honza')")
        for i in range(3):
            self._add_face(f"honza_{i}.jpg", self._unit(rng, A),
                           person=honza, q_score=0.5)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertIn(honza, {r[0] for r in self.q("SELECT id FROM people")})
        count = self.q("SELECT COUNT(*) FROM face_instances WHERE person_id=?", (honza,))[0][0]
        self.assertEqual(count, 3,
                         "oblicheje kotvy musi zustat prirazeny i s nizkym quality_score")

    def test_zero_bbox_not_filtered(self):
        """Oblicheje s bbox_w=0 (starsi zaznamy bez bbox) se nevyrazuji."""
        rng = np.random.default_rng(66)
        A = rng.normal(size=512).astype(np.float32)
        for i in range(4):
            self._add_face(f"nobbox_{i}.jpg", self._unit(rng, A),
                           q_score=20.0, bx=0.0, by=0.0, bw=0.0, bh=0.0)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertEqual(stats.get("skipped_quality", 0), 0,
                         "oblicheje bez bbox (bw=0) se nesmi vyradit")
        auto = self.q("SELECT id FROM people WHERE name=?", (face_cluster.UNKNOWN_NAME,))
        self.assertEqual(len(auto), 1, "oblicheje bez bbox musi tvorit shluk")

    def test_skipped_quality_in_stats(self):
        """Statistika skipped_quality musi byt v navratovem slovniku."""
        rng = np.random.default_rng(88)
        A = rng.normal(size=512).astype(np.float32)
        self._add_face("x.jpg", self._unit(rng, A), q_score=20.0)
        stats = face_cluster.strict_recluster_faces()
        self.assertIsNotNone(stats)
        self.assertIn("skipped_quality", stats,
                      "navratovy slovnik musi obsahovat klic 'skipped_quality'")


# ===========================================================================
# 5. DECODE-ONCE PIPELINE (Patch 2.3)
# ===========================================================================
class TestDecodeOnce(TempEnv):
    """Testuje, ze detect_and_extract_faces() prijme predem dekodovany obrazek."""

    class _Face:
        def __init__(self, x_off, emb):
            self.bbox = np.array([x_off, 60, x_off + 120, 200], dtype=np.float32)
            self.kps = np.array([
                [x_off + 30, 100], [x_off + 90, 100], [x_off + 60, 130],
                [x_off + 40, 160], [x_off + 80, 160]
            ], dtype=np.float32)
            self.det_score = 0.9
            self.embedding = emb

    class _Align:
        @staticmethod
        def norm_crop(img, kps, image_size=224):
            import cv2
            return cv2.resize(img[40:220, 20:260], (image_size, image_size))

    def setUp(self):
        super().setUp()
        self.fe = importlib.import_module("face_engine")
        self._saved_app = self.fe._app
        self._saved_align = self.fe._face_align
        self.fe._face_align = self._Align
        self.img_path = os.path.join(self.tmp, "foto.jpg")
        Image.new("RGB", (400, 300), (100, 150, 200)).save(self.img_path, "JPEG")

    def tearDown(self):
        self.fe._app = self._saved_app
        self.fe._face_align = self._saved_align
        super().tearDown()

    def test_preloaded_image_used_instead_of_disk(self):
        """Pokud je predan preloaded_image, detektor ho pouzije misto cteni z disku."""
        import cv2
        rng = np.random.default_rng(7)
        emb = rng.normal(size=512).astype(np.float32)
        face = self._Face(20, emb)
        received_images = []

        def fake_get(img):
            received_images.append(img.copy())
            return [face]

        self.fe._app = types.SimpleNamespace(get=fake_get)

        # Predame predem dekodovany obrazek (BGR: kanal 0 = 200, kanal 1 = 100, kanal 2 = 50)
        # Soubor na disku ma RGB(100,150,200) -> BGR(200,150,100): kanal 0 = 200, kanal 2 = 100
        # Preloaded ma BGR(200,100,50): kanal 1 = 100 (soubor ma kanal 1 = 150)
        preloaded = np.full((300, 400, 3), 0, dtype=np.uint8)
        preloaded[:, :, 0] = 200   # B kanal = 200 (soubor ma 200 take)
        preloaded[:, :, 1] = 42    # G kanal = 42  (soubor ma 150 -> jasne odlisne)
        preloaded[:, :, 2] = 50    # R kanal = 50  (soubor ma 100)
        self.fe.detect_and_extract_faces(1, self.img_path, preloaded_image=preloaded)

        self.assertEqual(len(received_images), 1,
                         "detektor musi byt volan prave jednou")
        # Overime, ze detektor dostal preloaded (G kanal = 42), ne soubor (G kanal = 150)
        center_pixel = received_images[0][150, 200]
        self.assertLess(int(center_pixel[1]), 100,
                        "detektor dostal predlozeny obrazek (G=42), ne soubor z disku (G=150)")

    def test_none_preloaded_falls_back_to_disk(self):
        """Pokud preloaded_image=None, soubor se nacte z disku standardne."""
        rng = np.random.default_rng(8)
        emb = rng.normal(size=512).astype(np.float32)
        face = self._Face(20, emb)
        received_images = []

        def fake_get(img):
            received_images.append(img.copy())
            return [face]

        self.fe._app = types.SimpleNamespace(get=fake_get)
        self.fe.detect_and_extract_faces(1, self.img_path, preloaded_image=None)

        self.assertEqual(len(received_images), 1,
                         "bez preloaded_image se soubor nacte z disku")
        # Soubor ma barvu (100,150,200) v BGR = (200,150,100)
        center_pixel = received_images[0][150, 200]
        self.assertGreater(int(center_pixel[0]), 150,
                           "detektor dostal obrazek nacteny z disku")

    def test_invalid_preloaded_falls_back_to_disk(self):
        """Prazdne nebo neplatne preloaded_image -> fallback na disk."""
        rng = np.random.default_rng(9)
        emb = rng.normal(size=512).astype(np.float32)
        face = self._Face(20, emb)
        received_images = []

        def fake_get(img):
            received_images.append(img.copy())
            return [face]

        self.fe._app = types.SimpleNamespace(get=fake_get)
        # Prazdne pole -> fallback
        empty = np.array([], dtype=np.uint8)
        self.fe.detect_and_extract_faces(1, self.img_path, preloaded_image=empty)
        self.assertEqual(len(received_images), 1,
                         "prazdne preloaded_image -> fallback na disk")


if __name__ == "__main__":
    unittest.main(verbosity=2)