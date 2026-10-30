"""Zarovnání obličejů a modely OpenCV (YuNet / SFace) – samostatné, deterministické testy.

Dříve to byl ruční skript, který při importu stahoval model a četl pevně zadanou
miniaturu .cache/thumbnails/thumb_15.jpg (v čisté instalaci neexistuje → pytest padal
už při sběru testů). Teď nic nestahuje a nepotřebuje knihovnu fotek.
"""
import os
import unittest

import cv2
import numpy as np

import face_engine

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
YUNET_PATH = os.path.join(MODEL_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_PATH = os.path.join(MODEL_DIR, "face_recognition_sface_2021dec.onnx")


class TestAlignment(unittest.TestCase):
    def test_umeyama_recovers_known_similarity(self):
        rng = np.random.default_rng(7)
        src = rng.uniform(0, 100, (5, 2))
        angle, scale, t = 0.3, 1.7, np.array([12.0, -4.0])
        R = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        dst = (scale * (R @ src.T)).T + t
        M = face_engine.umeyama_similarity(src, dst)
        np.testing.assert_allclose(M[:, :2], scale * R, atol=1e-9)
        np.testing.assert_allclose(M[:, 2], t, atol=1e-9)

    def test_arcface_landmarks_map_to_identity(self):
        M = face_engine.estimate_norm(face_engine.ARCFACE_DST, 112)
        np.testing.assert_allclose(M, [[1, 0, 0], [0, 1, 0]], atol=1e-9)

    def test_norm_crop_shape(self):
        img = np.full((300, 400, 3), 128, np.uint8)
        crop = face_engine.norm_crop(img, face_engine.ARCFACE_DST * 2 + 50, 112)
        self.assertEqual(crop.shape, (112, 112, 3))


@unittest.skipUnless(os.path.exists(YUNET_PATH) and os.path.exists(SFACE_PATH), "chybí modely v models/")
class TestOpenCvModels(unittest.TestCase):
    def test_detector_runs_on_blank_image(self):
        img = np.zeros((240, 320, 3), np.uint8)
        det = cv2.FaceDetectorYN.create(YUNET_PATH, "", (320, 240), 0.55, 0.3)
        _, faces = det.detect(img)
        self.assertTrue(faces is None or len(faces) == 0)

    def test_sface_feature_is_128d_and_self_match_is_one(self):
        rec = cv2.FaceRecognizerSF.create(SFACE_PATH, "")
        rng = np.random.default_rng(3)
        crop = rng.integers(0, 255, (112, 112, 3), dtype=np.uint8)
        feat = rec.feature(crop)
        self.assertEqual(feat.shape, (1, 128))
        score = rec.match(feat, feat, cv2.FaceRecognizerSF_FR_COSINE)
        self.assertAlmostEqual(score, 1.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
