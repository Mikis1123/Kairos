"""Sprint 4.0 – přesné hledání SigLIP 2 (volitelné): stažení, index vedle CLIP, směrování dotazu."""
import hashlib
import io
import os
import shutil
import tempfile
import unittest

import numpy as np

import ai_engine
import database
import siglip_engine


class FakeResponse(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(data)
        self.status = status


def fake_files(payloads):
    """FILES pro test: (vzdálená, místní, velikost, sha) ze skutečného obsahu."""
    return [(f"r/{name}", name, len(data), hashlib.sha256(data).hexdigest()) for name, data in payloads.items()]


class TestDownload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="siglip_dl_")
        self.saved = siglip_engine.FILES
        self.payloads = {"a.onnx": os.urandom(3 * siglip_engine.CHUNK + 17), "tok.json": b'{"x": 1}'}
        siglip_engine.FILES = fake_files(self.payloads)
        self.requests = []

    def tearDown(self):
        siglip_engine.FILES = self.saved
        shutil.rmtree(self.dir, True)

    def opener(self, req, timeout=0):
        name = req.full_url.rsplit("/", 1)[1]
        data = self.payloads[name]
        rng = req.get_header("Range")
        self.requests.append((name, rng))
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return FakeResponse(data[start:], 206)
        return FakeResponse(data)

    def test_full_download_and_installed(self):
        seen = []
        self.assertFalse(siglip_engine.installed(self.dir))
        ok = siglip_engine.download(lambda got, total: seen.append((got, total)), folder=self.dir,
                                    base_url="https://x/", opener=self.opener)
        self.assertTrue(ok)
        self.assertTrue(siglip_engine.installed(self.dir))
        self.assertEqual(seen[-1][0], seen[-1][1], "průběh skončí na 100 %")
        self.assertFalse(any(n.endswith(".part") for n in os.listdir(self.dir)))

    def test_resume_partial_file(self):
        data = self.payloads["a.onnx"]
        with open(os.path.join(self.dir, "a.onnx.part"), "wb") as f:
            f.write(data[:1000])
        self.assertTrue(siglip_engine.download(folder=self.dir, base_url="https://x/", opener=self.opener))
        self.assertIn(("a.onnx", "bytes=1000-"), self.requests)
        with open(os.path.join(self.dir, "a.onnx"), "rb") as f:
            self.assertEqual(f.read(), data)

    def test_checksum_mismatch_is_rejected(self):
        siglip_engine.FILES = [(r, n, s, "0" * 64) for r, n, s, _ in siglip_engine.FILES]
        with self.assertRaises(siglip_engine.DownloadError):
            siglip_engine.download(folder=self.dir, base_url="https://x/", opener=self.opener)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.onnx")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.onnx.part")), "vadný soubor pryč")

    def test_cancel(self):
        self.assertFalse(siglip_engine.download(folder=self.dir, base_url="https://x/", opener=self.opener,
                                                should_stop=lambda: True))
        self.assertFalse(siglip_engine.installed(self.dir))

    def test_pinned_real_files(self):
        self.assertEqual(len(self.saved), 3)
        for _remote, _local, size, sha in self.saved:
            self.assertGreater(size, 1_000_000)
            self.assertEqual(len(sha), 64)


class TestPreprocess(unittest.TestCase):
    def test_shape_and_range(self):
        from PIL import Image
        x = siglip_engine.preprocess(Image.new("RGB", (640, 360), (255, 0, 0)))
        self.assertEqual(x.shape, (3, 256, 256))
        self.assertAlmostEqual(float(x[0].mean()), 1.0, places=5)
        self.assertAlmostEqual(float(x[1].mean()), -1.0, places=5)


class TestEngineRouting(unittest.TestCase):
    def test_embeddings_of_both_dims(self):
        for dim in (512, 768):
            raw = ai_engine.encode_embedding(np.ones(dim))
            self.assertEqual(len(raw), dim * 4)
            self.assertEqual(ai_engine.decode_embedding(raw).size, dim)
        with self.assertRaises(ValueError):
            ai_engine.encode_embedding(np.ones(300))
        self.assertIsNone(ai_engine.decode_embedding(b"\0" * 1200))

    def test_index_stays_one_model(self):
        clip = ai_engine.encode_embedding(np.ones(512))
        sig = ai_engine.encode_embedding(np.ones(768))
        ai_engine._rebuild_vector_index({1: sig, 2: clip, 3: sig})
        st = ai_engine._cached_index_state
        self.assertEqual(st["matrix"].shape, (2, 768))
        self.assertEqual(st["media_ids"], [1, 3])

    def test_text_model_follows_index_dim(self):
        saved = ai_engine._text_model, ai_engine._siglip_text
        try:
            ai_engine._text_model, ai_engine._siglip_text = "clip", "siglip"
            self.assertEqual(ai_engine.text_model_for_dim(512), "clip")
            self.assertEqual(ai_engine.text_model_for_dim(768), "siglip")
        finally:
            ai_engine._text_model, ai_engine._siglip_text = saved

    def test_z_profile(self):
        self.assertEqual(ai_engine.z_profile(512)["confident"], ai_engine.SEARCH_Z_CONFIDENT)
        self.assertGreater(ai_engine.z_profile(768)["confident"], ai_engine.SEARCH_Z_CONFIDENT)
        zi = np.array([3.0, 2.0, 0.9])
        conf, poss = ai_engine.tier_scores(zi, None, zi, 100)
        self.assertEqual(conf, [0])
        conf, poss = ai_engine.tier_scores(zi, None, zi, 100, ai_engine.z_profile(768))
        self.assertEqual(conf, [], "SigLIP: z 3,0 ještě není jistá shoda")
        self.assertEqual(poss, [0, 1, 2])


class TestSiglipTable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="siglip_db_")
        self.saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.dir, "t.db")
        database.init_db()
        with database.db_transaction() as c:
            for i in (1, 2, 3):
                c.execute("INSERT INTO media_items (id, file_path, file_name, media_type, thumbnail_path) "
                          "VALUES (?, ?, ?, 'photo', '')", (i, f"C:/f/{i}.jpg", f"{i}.jpg"))
        for i, sig in database.get_connection().execute(
                f"SELECT m.id, {database.EMBEDDING_SIG_SQL} FROM media_items m").fetchall():
            database.save_image_embedding(i, ai_engine.encode_embedding(np.ones(512)), sig)

    def tearDown(self):
        database.DB_PATH = self.saved
        shutil.rmtree(self.dir, True)

    def test_pending_save_coverage(self):
        pending = database.get_media_needing_siglip()
        self.assertEqual(len(pending), 3)
        self.assertEqual(database.siglip_coverage(), (0, 3))
        database.save_siglip_embeddings([(mid, ai_engine.encode_embedding(np.ones(768)), sig)
                                         for mid, _p, _t, _m, sig in pending[:2]])
        self.assertEqual(database.siglip_coverage(), (2, 3))
        self.assertEqual(len(database.get_media_needing_siglip()), 1)
        vecs = database.get_siglip_vectors()
        self.assertEqual(len(vecs), 2)
        self.assertTrue(all(len(v) == 768 * 4 for v in vecs.values()))
        self.assertEqual(len(database.get_siglip_vectors([pending[0][0]])), 1)
        self.assertEqual(len(database.get_search_vectors()), 3, "CLIP index nedotčený")


@unittest.skipUnless(siglip_engine.installed(), "model SigLIP 2 není stažený")
class TestSiglipModel(unittest.TestCase):
    def test_text_image_agreement(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (256, 256), (30, 90, 200))
        ImageDraw.Draw(img).ellipse((60, 60, 196, 196), fill=(250, 220, 40))
        iv = siglip_engine.ImageEncoder().encode([img])
        tv = siglip_engine.TextEncoder().encode(["žlutý kruh na modrém pozadí", "hustý zelený les"])
        self.assertEqual(iv.shape, (1, 768))
        iv = iv / np.linalg.norm(iv, axis=1, keepdims=True)
        tv = tv / np.linalg.norm(tv, axis=1, keepdims=True)
        sims = (tv @ iv.T).ravel()
        self.assertGreater(sims[0], sims[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
