"""Sprint 3 – hledání v ONNX Runtime bez torch."""
import os
import subprocess
import sys
import unittest

import numpy as np
from PIL import Image

import ai_onnx

MODELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")


class TestPreprocess(unittest.TestCase):
    def test_shape_and_normalization(self):
        x = ai_onnx.preprocess(Image.new("RGB", (640, 360), (255, 255, 255)))
        self.assertEqual(x.shape, (3, 224, 224))
        np.testing.assert_allclose(x[:, 0, 0], (1 - ai_onnx.MEAN) / ai_onnx.STD, rtol=1e-5)

    def test_long_side_floors_like_transformers(self):
        # 360×270: delší strana int(224·360/270) = 298 (zaokrouhlení dávalo 299 a kosinus 0,991)
        img = Image.fromarray(np.tile(np.arange(360, dtype=np.uint8)[None, :, None], (270, 1, 3)))
        a = ai_onnx.preprocess(img)
        b = ai_onnx.preprocess(img.transpose(Image.Transpose.TRANSPOSE))
        self.assertEqual(a.shape, b.shape)

    def test_grayscale_and_rgba_accepted(self):
        self.assertEqual(ai_onnx.preprocess(Image.new("L", (300, 500))).shape, (3, 224, 224))
        self.assertEqual(ai_onnx.preprocess(Image.new("RGBA", (500, 300))).shape, (3, 224, 224))


@unittest.skipUnless(ai_onnx.available(MODELS), "modely ONNX nejsou exportované (tools/export_onnx.py)")
class TestEncoders(unittest.TestCase):
    def test_image_batch_matches_single(self):
        enc = ai_onnx.ImageEncoder(MODELS, prefer_gpu=False)
        rng = np.random.default_rng(0)
        imgs = [Image.fromarray(rng.integers(0, 255, (200, 300, 3), dtype=np.uint8)) for _ in range(3)]
        batch = enc.encode(imgs)
        self.assertEqual(batch.shape, (3, 512))
        np.testing.assert_allclose(enc.encode(imgs[1]), batch[1], rtol=1e-3, atol=1e-4)

    def test_text_padding_does_not_change_vector(self):
        enc = ai_onnx.TextEncoder(MODELS, quantized=False)
        alone = enc.encode(["les"])[0]
        padded = enc.encode(["les", "západ slunce nad horami u jezera"])[0]
        cos = alone @ padded / np.linalg.norm(alone) / np.linalg.norm(padded)
        self.assertGreater(cos, 0.9999, "maska musí vyloučit výplň z průměru")

    def test_multilingual_alignment(self):
        enc = ai_onnx.TextEncoder(MODELS, quantized=True)
        v = enc.encode(["pes", "dog", "auto"])
        v = v / np.linalg.norm(v, axis=1, keepdims=True)
        self.assertGreater(v[0] @ v[1], v[0] @ v[2])

    def test_search_path_does_not_import_torch(self):
        code = ("import os, sys; os.environ.pop('KAIROS_TORCH', None); import ai_engine; "
                "ai_engine.get_text_model(); ai_engine.get_image_model(); "
                "print('torch' in sys.modules, 'transformers' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "False False", out.stderr[-400:])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSoftAnd(unittest.TestCase):
    def test_query_parts(self):
        import ai_engine
        self.assertEqual(ai_engine.query_parts("les"), [])
        self.assertEqual(ai_engine.query_parts("západ slunce a oheň"), ["západ", "slunce", "oheň"])
        self.assertEqual(ai_engine.query_parts("lidi u jezera"), ["lidi", "jezera"])
        self.assertEqual(ai_engine.query_parts("a b c d e f"), [])

    def test_soft_min_prefers_items_with_all_parts(self):
        import ai_engine
        whole = np.zeros(3, np.float32)
        comps = {"ohen": np.array([3.0, 3.0, 0.0]), "zapad": np.array([3.0, 0.0, 3.0])}
        out = ai_engine.soft_and("ohen zapad", whole, comps.get, lambda z: z, weight=0.5)
        self.assertEqual(int(np.argmax(out)), 0, "obojí > jen jedno")
        self.assertAlmostEqual(float(out[1]), float(out[2]), places=5)

    def test_absent_part_is_dropped(self):
        import ai_engine
        whole = np.array([1.0, 2.0], np.float32)
        comps = {"ohen": np.array([3.0, 0.0]), "smich": np.array([0.1, 0.2])}   # smích není vidět
        np.testing.assert_array_equal(ai_engine.soft_and("ohen smich", whole, comps.get, lambda z: z, 0.5), whole)

    def test_disabled_by_default(self):
        import ai_engine
        self.assertEqual(ai_engine.SOFTAND_WEIGHT, 0.0)
