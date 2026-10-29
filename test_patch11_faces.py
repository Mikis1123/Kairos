"""Patch 11 / dávka A – vlastní zarovnání obličejů, anatomická brána (karanténa), prototypy osob.

Spuštění:   python -m pytest test_patch11_faces.py -v
Testy pracují VÝHRADNĚ v dočasných složkách.
"""
import os
import shutil
import tempfile
import types
import unittest
import warnings
from unittest import mock

import numpy as np
from PIL import Image

import database
import face_engine
import face_cluster


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def jitter(v, seed, amount=0.02):
    rng = np.random.default_rng(10_000 + seed)
    return unit(v + amount * rng.normal(size=v.size))


def kps_face(cx=100, cy=100, eye=30.0):
    """5 bodů čelního obličeje (oči, nos, koutky úst)."""
    return np.array([[cx - eye, cy - 20], [cx + eye, cy - 20], [cx, cy + 8], [cx - 22, cy + 38], [cx + 22, cy + 38]],
                    dtype=np.float64)


class TempEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p11a_")
        self._saved = (database.DB_PATH, face_engine.FACE_CACHE_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        face_engine.FACE_CACHE_DIR = os.path.join(self.tmp, "faces")
        os.makedirs(face_engine.FACE_CACHE_DIR)
        database.init_db()
        self._n = 0
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

    def face(self, vec, person=None, valid=None, scale=20.0):
        self._n += 1
        mid = self.x("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?, ?, 'photo')",
                     (os.path.join(self.tmp, f"m{self._n}.jpg"), f"m{self._n}.jpg"))
        return self.x("INSERT INTO face_instances (media_id, person_id, embedding, quality_score, bbox_x, bbox_y, "
                      "bbox_w, bbox_h, is_valid) VALUES (?, ?, ?, 30, 0.1, 0.1, 0.2, 0.3, ?)",
                      (mid, person, (np.asarray(vec, np.float32) * scale).tobytes(), valid))


# ── vlastní zarovnání ─────────────────────────────────────────────────────────

class TestOwnAlignment(unittest.TestCase):

    def test_matches_skimage_reference(self):
        try:
            from skimage import transform as trans
        except ImportError:
            self.skipTest("scikit-image není nainstalované")
        rng = np.random.default_rng(1)
        for _ in range(200):
            lmk = face_engine.ARCFACE_DST * rng.uniform(0.5, 3) + rng.normal(0, 5, (5, 2)) + rng.uniform(0, 200, 2)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                t = trans.SimilarityTransform()
                t.estimate(lmk, face_engine.ARCFACE_DST * 2)
            self.assertLess(np.abs(t.params[:2] - face_engine.umeyama_similarity(lmk, face_engine.ARCFACE_DST * 2)).max(), 1e-3)

    def test_identity_and_size_variants(self):
        M = face_engine.estimate_norm(face_engine.ARCFACE_DST, 112)
        np.testing.assert_allclose(M, [[1, 0, 0], [0, 1, 0]], atol=1e-9)
        M224 = face_engine.estimate_norm(face_engine.ARCFACE_DST, 224)
        self.assertAlmostEqual(M224[0, 0], 2.0, places=6)
        M128 = face_engine.estimate_norm(face_engine.ARCFACE_DST, 128)
        self.assertAlmostEqual(M128[0, 2], 8.0 * 128 / 128, places=6)

    def test_installed_into_insightface_without_future_warning(self):
        fake = types.SimpleNamespace(estimate_norm=None, norm_crop=None)
        face_engine._install_own_alignment(fake)
        img = np.zeros((300, 300, 3), np.uint8)
        with warnings.catch_warnings():
            warnings.simplefilter("error")          # jakékoli varování = chyba
            out = fake.norm_crop(img, landmark=face_engine.ARCFACE_DST * 2, image_size=112)
        self.assertEqual(out.shape, (112, 112, 3))
        self.assertIs(fake.estimate_norm, face_engine.estimate_norm)


# ── anatomická brána ──────────────────────────────────────────────────────────

class TestFaceGate(unittest.TestCase):

    def test_plausible_geometry(self):
        self.assertEqual(face_engine.landmark_plausibility(kps_face(), [40, 40, 160, 180]), 1.0)
        upside = kps_face().copy()
        upside[[3, 4], 1] = 50                    # ústa nad očima
        self.assertLess(face_engine.landmark_plausibility(upside), 0.75)
        self.assertEqual(face_engine.landmark_plausibility(np.zeros((5, 2))), 0.0)

    def test_scores(self):
        T = face_engine.FACE_VALID_THRESHOLD
        good, _ = face_engine.face_validity(23.0, 0.85, kps_face(), [40, 40, 160, 180])
        rock, why = face_engine.face_validity(11.0, 0.52, kps_face(), [40, 40, 160, 180])
        back_of_head, _ = face_engine.face_validity(10.0, None, None, None, redetected=False)
        closeup, _ = face_engine.face_validity(22.0, None, None, None, redetected=False)
        dim_light, _ = face_engine.face_validity(18.0, 0.62, kps_face(), [40, 40, 160, 180])
        self.assertGreater(good, T)
        self.assertLess(rock, T)
        self.assertIn("slabý obličejový vzor (embedding)", why)
        self.assertLess(back_of_head, T)
        self.assertGreater(closeup, T, "detail obličeje, který re-detekce nenajde, se nezahazuje")
        self.assertGreater(dim_light, T, "skutečný obličej ve špatném světle projde")


class TestGateStorage(TempEnv):

    def test_ingestion_columns(self):
        mid = self.x("INSERT INTO media_items (file_path, media_type) VALUES ('a.jpg', 'photo')")
        database.save_faces_for_media(mid, [{"crop_path": None, "embedding": b"\x00" * 16, "is_valid": 0,
                                             "face_score": 0.2, "det_score": 0.5,
                                             "gate_version": face_engine.GATE_VERSION}])
        self.assertEqual(self.q("SELECT is_valid, face_score, det_score, gate_version FROM face_instances")[0],
                         (0, 0.2, 0.5, face_engine.GATE_VERSION))

    def test_backfill_uses_embedding_norm(self):
        crop = os.path.join(self.tmp, "c.jpg")
        Image.new("RGB", (64, 64), (90, 90, 90)).save(crop)
        u = unit(np.random.default_rng(3).normal(size=128))
        mid = self.x("INSERT INTO media_items (file_path, media_type) VALUES ('b.jpg', 'photo')")
        strong = self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                        (mid, crop, (u * 23).astype(np.float32).tobytes()))
        weak = self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding) VALUES (?, ?, ?)",
                      (mid, crop, (u * 9).astype(np.float32).tobytes()))
        app = mock.Mock()
        app.get.return_value = []                # těsný ořez – detektor nic nenajde
        with mock.patch.object(face_engine, "_get_app", return_value=app):
            self.assertEqual(face_engine.analyze_face_crops(), 2)
        self.assertEqual(self.q("SELECT is_valid FROM face_instances WHERE id=?", (strong,))[0][0], 1)
        self.assertEqual(self.q("SELECT is_valid FROM face_instances WHERE id=?", (weak,))[0][0], 0)
        self.assertEqual(face_engine.count_faces_without_frontality(), 0, "brána běží jen jednou (gate_version)")


# ── karanténa ve shlukování ───────────────────────────────────────────────────

class TestQuarantineInClustering(TempEnv):

    def test_quarantined_faces_never_form_people(self):
        u = unit(np.random.default_rng(5).normal(size=128))
        rock = unit(np.random.default_rng(6).normal(size=128))
        for i in range(3):
            self.face(jitter(u, i))
        for i in range(3):
            self.face(jitter(rock, 10 + i), valid=0)
        stats = face_cluster.strict_recluster_faces()
        sizes = [r[0] for r in self.q("SELECT COUNT(*) FROM face_instances WHERE person_id IS NOT NULL GROUP BY person_id")]
        self.assertEqual(sizes, [3])
        self.assertEqual(stats["faces"], 3)

    def test_quarantined_face_is_detached_even_from_named_person(self):
        u = unit(np.random.default_rng(7).normal(size=128))
        jana = self.x("INSERT INTO people (name) VALUES ('Jana')")
        keep = [self.face(jitter(u, i), person=jana) for i in range(2)]
        bad = self.face(jitter(u, 9), person=jana, valid=0)
        stats = face_cluster.strict_recluster_faces()
        self.assertEqual(stats["quarantined_detached"], 1)
        self.assertIsNone(self.q("SELECT person_id FROM face_instances WHERE id=?", (bad,))[0][0])
        self.assertEqual({r[0] for r in self.q(f"SELECT person_id FROM face_instances WHERE id IN ({keep[0]}, {keep[1]})")},
                         {jana})
        self.assertEqual(face_cluster.count_quarantined_assigned(), 0)

    def test_unchecked_faces_count_as_valid(self):
        u = unit(np.random.default_rng(8).normal(size=128))
        for i in range(3):
            self.face(jitter(u, i), valid=None)
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT COUNT(*) FROM people")[0][0], 1)

    def test_suggestions_and_covers_ignore_quarantine(self):
        u = unit(np.random.default_rng(9).normal(size=128))
        a, b = self.x("INSERT INTO people (name) VALUES ('A')"), self.x("INSERT INTO people (name) VALUES ('B')")
        self.face(jitter(u, 1), person=a)
        self.face(jitter(u, 2), person=b, valid=0)
        self.assertEqual(face_cluster.suggest_person_merges(), [], "osoba B nemá platný obličej")


# ── prototypy osob ────────────────────────────────────────────────────────────

class TestPrototypes(TempEnv):

    def _two_looks(self, seed=11, n=6):
        rng = np.random.default_rng(seed)
        cap, nocap = unit(rng.normal(size=128)), unit(rng.normal(size=128))
        mix = unit(0.55 * cap + 0.45 * nocap)              # stejná osoba, dvě podoby
        look_a = [jitter(unit(0.75 * mix + 0.25 * cap), i) for i in range(n)]
        look_b = [jitter(unit(0.75 * mix + 0.25 * nocap), 50 + i) for i in range(n)]
        return look_a, look_b, cap, nocap

    def test_prototypes_cover_both_looks(self):
        a, b, cap, nocap = self._two_looks()
        P = face_cluster.person_prototypes(np.vstack(a + b))
        self.assertGreaterEqual(len(P), 2)
        self.assertLessEqual(len(P), face_cluster.PROTOTYPE_MAX)
        self.assertGreater((P @ a[0]).max(), 0.95)
        self.assertGreater((P @ b[0]).max(), 0.95)

    def test_outlier_does_not_become_prototype(self):
        a, b, _, _ = self._two_looks(n=8)
        stranger = unit(np.random.default_rng(99).normal(size=128))
        P = face_cluster.person_prototypes(np.vstack(a + b + [stranger]))
        self.assertLess((P @ stranger).max(), 0.5, "cizí obličej v osobě nevytvoří vlastní prototyp")

    def test_small_person_single_prototype(self):
        u = unit(np.random.default_rng(12).normal(size=128))
        self.assertEqual(len(face_cluster.person_prototypes(np.vstack([jitter(u, i) for i in range(2)]))), 1)

    def test_named_person_absorbs_new_variation(self):
        """Nový obličej podobný jen jedné podobě (ne průměru) se přidá k pojmenované osobě."""
        rng = np.random.default_rng(21)
        looks = [unit(rng.normal(size=128)) for _ in range(3)]      # kšiltovka / brýle / profil
        mean_dir = unit(sum(looks))
        petr = self.x("INSERT INTO people (name) VALUES ('Petr')")
        for j, look in enumerate(looks):
            for i in range(4):
                self.face(jitter(look, 20 * j + i), person=petr)
        newcomer = self.face(jitter(looks[2], 77))
        self.assertLess(float(jitter(looks[2], 77) @ mean_dir), face_cluster.SIMILARITY_THRESHOLD,
                        "s jediným průměrem by se nepřidal")
        face_cluster.strict_recluster_faces(merge_threshold=1.0)
        self.assertEqual(self.q("SELECT person_id FROM face_instances WHERE id=?", (newcomer,))[0][0], petr)

    def test_borderline_uses_prototypes_and_skips_quarantine(self):
        rng = np.random.default_rng(31)
        look1, look2 = unit(rng.normal(size=128)), unit(rng.normal(size=128))
        eva = self.x("INSERT INTO people (name) VALUES ('Eva')")
        for i in range(3):
            self.face(jitter(look1, i), person=eva)
            self.face(jitter(look2, 10 + i), person=eva)
        crop = os.path.join(self.tmp, "crop.jpg")
        Image.new("RGB", (8, 8)).save(crop)
        near = unit(0.42 * look2 + np.sqrt(1 - 0.42 ** 2) * unit(rng.normal(size=128)))
        valid = self.face(near)
        hidden = self.face(near, valid=0)
        self.x("UPDATE face_instances SET face_crop_path = ? WHERE id IN (?, ?)", (crop, valid, hidden))
        ids = [f["face_id"] for f in face_cluster.get_borderline_faces_for_person(eva, 0.3, 0.6)]
        self.assertIn(valid, ids)
        self.assertNotIn(hidden, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
