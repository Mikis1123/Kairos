"""Ozvěny (tehdy a teď): hledání návratů na stejné místo se stejnou kompozicí, srovnání záběrů
a zobrazení. Syntetická knihovna (žádné photos.db): vektory scén = náhodné jednotkové vektory,
varianty stejného záběru = malý šum kolem nich.
"""
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

import numpy as np

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication (main.py)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import echoes

DIM = 512
SCHEMA = """
CREATE TABLE media_items (id INTEGER PRIMARY KEY, latitude REAL, longitude REAL, date_taken TEXT,
    thumbnail_path TEXT, file_path TEXT, place_city TEXT, phash TEXT, is_favorite INTEGER DEFAULT 0,
    media_type TEXT DEFAULT 'photo', is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0, paired_video_path TEXT);
CREATE TABLE image_embeddings (media_id INTEGER PRIMARY KEY, embedding BLOB NOT NULL);
CREATE INDEX idx_media_paired ON media_items(paired_video_path);   -- jako database.init_db (visible_media_sql)
"""


class Library:
    def __init__(self, path=":memory:", seed=3):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.rng = np.random.default_rng(seed)
        self.scenes = {}
        self.next_id = 1

    def scene(self, name):
        if name not in self.scenes:
            v = self.rng.standard_normal(DIM).astype(np.float32)
            self.scenes[name] = v / np.linalg.norm(v)
        return self.scenes[name]

    def add(self, scene, lat, lon, date, city="", noise=0.012, **extra):
        v = self.scene(scene) + self.rng.standard_normal(DIM).astype(np.float32) * noise
        mid = self.next_id
        self.next_id += 1
        cols = {"id": mid, "latitude": lat, "longitude": lon, "date_taken": date, "thumbnail_path": f"t{mid}.jpg",
                "file_path": f"f{mid}.jpg", "place_city": city, "phash": extra.pop("phash", None)}
        cols.update(extra)
        self.conn.execute(f"INSERT INTO media_items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          tuple(cols.values()))
        self.conn.execute("INSERT INTO image_embeddings VALUES (?, ?)", (mid, (v / np.linalg.norm(v)).tobytes()))
        return mid


def offset(lat, lon, north_m=0.0, east_m=0.0):
    return lat + north_m / 111320.0, lon + east_m / (111320.0 * math.cos(math.radians(lat)))


class TestFindEchoes(unittest.TestCase):

    def setUp(self):
        L = self.lib = Library()
        # A: vyhlídka – stejný záběr 2019 (série 3), 2022, 2026; navíc jiný záběr 2026 na stejném místě
        self.a19 = [L.add("view", 49.10, 13.50, f"2019:08:1{i} 10:00:00", city="Kvilda") for i in range(3)]
        self.a22 = L.add("view", *offset(49.10, 13.50, 40, -30), "2022-08-12 10:00:00", city="Kvilda")
        self.a26 = L.add("view", *offset(49.10, 13.50, -20, 25), "2026-07-07 10:00", city="Kvilda", is_favorite=1)
        L.add("selfie", 49.10, 13.50, "2026-07-07 10:05:00", city="Kvilda")
        # B: stejný záběr, ale jen o 5 dní později (silvestrovský výlet přes dva roky) -> ne
        L.add("bridge", 50.0, 14.4, "2020-12-29 12:00:00")
        L.add("bridge", 50.0, 14.4, "2021-01-03 12:00:00")
        # C: stejný záběr po letech, ale 400 m daleko -> ne
        L.add("tower", 48.8, 14.3, "2018-06-01 12:00:00")
        L.add("tower", *offset(48.8, 14.3, 400, 0), "2025-06-01 12:00:00")
        # D: stejné místo po letech, jiná kompozice -> ne
        L.add("lake", 49.5, 15.0, "2015-07-01 12:00:00")
        L.add("forest", 49.5, 15.0, "2024-07-01 12:00:00")
        # E: návrat, ale novější snímek je v koši -> ne
        L.add("gate", 49.7, 15.2, "2016-01-01 12:00:00")
        L.add("gate", 49.7, 15.2, "2023-01-01 12:00:00", is_trashed=1)

    def test_finds_only_true_return_with_one_shot_per_year(self):
        found = echoes.find_echoes(self.lib.conn)
        self.assertEqual(len(found), 1, [e["key"] for e in found])
        e = found[0]
        self.assertEqual([it["year"] for it in e["items"]], [2019, 2022, 2026])
        self.assertIn(e["items"][0]["id"], self.a19)                  # ze série jeden snímek
        self.assertEqual(e["items"][1]["id"], self.a22)
        self.assertEqual(e["items"][2]["id"], self.a26)
        self.assertEqual(e["place"], "Kvilda")
        self.assertEqual(e["span_years"], 7)
        self.assertGreater(e["score"], 0.9)
        self.assertEqual(echoes.subtitle(e), "2019 – 2026 · 7 let mezi snímky · 3 návraty")

    def test_thresholds_are_parameters(self):
        self.assertEqual(len(echoes.find_echoes(self.lib.conn, radius_m=600)), 2)       # i věž 400 m
        self.assertEqual(len(echoes.find_echoes(self.lib.conn, min_gap_days=3)), 2)     # i most po 5 dnech

    def test_favorite_wins_within_year(self):
        L = self.lib
        fav = L.add("view", 49.10, 13.50, "2019:08:20 10:00:00", city="Kvilda", is_favorite=1)
        e = echoes.find_echoes(L.conn)[0]
        self.assertEqual(e["items"][0]["id"], fav)

    def test_empty_and_tiny_libraries(self):
        empty = Library()
        self.assertEqual(echoes.find_echoes(empty.conn), [])
        one = Library()
        one.add("x", 49, 13, "2020-01-01")
        self.assertEqual(echoes.find_echoes(one.conn), [])

    def test_bad_rows_are_skipped(self):
        L = self.lib
        L.add("view", None, None, "2010-01-01")
        L.add("view", 0, 0, "2011-01-01")
        L.add("view", 49.10, 13.50, "neznámé datum")
        self.assertEqual(len(echoes.find_echoes(L.conn)), 1)

    def test_phash_agreement_lifts_borderline_composition(self):
        from datetime import datetime
        rng = np.random.default_rng(4)
        v1 = rng.standard_normal(DIM).astype(np.float32)
        v1 /= np.linalg.norm(v1)
        o = rng.standard_normal(DIM).astype(np.float32)
        o -= (o @ v1) * v1
        o /= np.linalg.norm(o)
        cos = echoes.MIN_SIMILARITY - 0.01                                   # těsně pod prahem
        v2 = cos * v1 + math.sqrt(1 - cos ** 2) * o
        base = {"lat": 49.0, "lon": 14.0, "city": "", "fav": False, "thumb": "", "path": ""}
        then = dict(base, id=1, when=datetime(2018, 5, 1), phash="ffff0000ffff0000")
        near = dict(base, id=2, when=datetime(2024, 5, 1), phash="ffff0000ffff0001")    # 1 bit
        far = dict(base, id=3, when=datetime(2024, 5, 1), phash="0000ffff0000ffff")     # 64 bitů
        vecs = np.stack([v1, v2, v2]).astype(np.float32)
        pairs = echoes.candidate_pairs([then, near, far], vecs)
        self.assertEqual([(a, b) for a, b, _ in pairs], [(0, 1)], "stejné rozložení scény = ozvěna")
        self.assertAlmostEqual(pairs[0][2], cos + echoes.PHASH_BONUS, places=4)

    def test_years_text(self):
        self.assertEqual([echoes.years_text(n) for n in (1, 2, 4, 5, 12)], ["rok", "2 roky", "4 roky", "5 let", "12 let"])

    def test_scales_to_large_library(self):
        L = Library(seed=9)
        rng = np.random.default_rng(1)
        homes = [(49 + rng.uniform(-1, 1), 14 + rng.uniform(-1, 1)) for _ in range(40)]
        L.conn.execute("BEGIN")
        for k in range(12000):                       # 40 míst × hodně fotek, různé scény i roky
            lat, lon = homes[k % 40]
            north, east = rng.uniform(-60, 60, 2)
            L.add(f"s{k % 40}-{k % 7}", *offset(lat, lon, north, east),
                  f"{2014 + k % 12}-0{1 + k % 9}-1{k % 9} 12:00:00")
        L.conn.commit()
        t0 = time.perf_counter()
        found = echoes.find_echoes(L.conn)
        took = time.perf_counter() - t0
        self.assertTrue(found)
        self.assertLessEqual(len(found), echoes.MAX_ECHOES)
        self.assertLess(took, 3.0, f"{took:.1f} s")
        print(f"\n[Echoes] 12 000 fotek: {len(found)} ozvěn za {took:.2f} s")


def _texture(seed, size=(900, 1200)):
    import cv2
    rng = np.random.default_rng(seed)
    img = np.full((size[0], size[1], 3), 90, np.uint8)
    for _ in range(140):                            # obdélníky a kruhy = rohy pro ORB
        c = tuple(int(x) for x in rng.integers(0, 255, 3))
        x, y = int(rng.integers(0, size[1])), int(rng.integers(0, size[0]))
        if rng.random() < 0.5:
            cv2.rectangle(img, (x, y), (x + int(rng.integers(20, 120)), y + int(rng.integers(20, 120))), c, -1)
        else:
            cv2.circle(img, (x, y), int(rng.integers(10, 60)), c, -1)
    return img


class TestAlign(unittest.TestCase):

    def test_recovers_known_similarity_transform(self):
        import cv2
        then = _texture(1)
        true = cv2.getRotationMatrix2D((600, 450), 4.0, 1.12)
        true[:, 2] += (35, -20)
        now = cv2.warpAffine(then, true, (1200, 900), borderValue=(90, 90, 90))
        m = echoes.align_pair(then, now)
        self.assertIsNotNone(m)
        for pt in ((300, 200), (900, 700), (600, 450)):
            exp = true @ np.array([pt[0], pt[1], 1.0])
            got = m @ np.array([pt[0], pt[1], 1.0])
            self.assertLess(np.hypot(*(exp - got)), 4.0)

    def test_unrelated_scenes_are_not_aligned(self):
        self.assertIsNone(echoes.align_pair(_texture(1), _texture(2)))

    def test_flat_images_are_not_aligned(self):
        flat = np.full((400, 600, 3), 128, np.uint8)
        self.assertIsNone(echoes.align_pair(flat, flat))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestEchoesView(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import cv2
        cls.tmp = tempfile.mkdtemp(prefix="echoes_view_")
        then = _texture(5)
        true = cv2.getRotationMatrix2D((600, 450), -3.0, 1.08)
        now = cv2.warpAffine(then, true, (1200, 900), borderValue=(90, 90, 90))
        cls.then_path, cls.now_path = os.path.join(cls.tmp, "then.png"), os.path.join(cls.tmp, "now.png")
        cv2.imwrite(cls.then_path, cv2.cvtColor(then, cv2.COLOR_RGB2BGR))
        cv2.imwrite(cls.now_path, cv2.cvtColor(now, cv2.COLOR_RGB2BGR))
        cls.echo = {"key": "1-2", "place": "Kvilda", "span_years": 7, "score": 0.95, "lat": 49.1, "lon": 13.5,
                    "items": [{"id": 1, "year": 2019, "date": "2019-08-10", "thumb": cls.then_path, "path": cls.then_path},
                              {"id": 2, "year": 2026, "date": "2026-07-07", "thumb": cls.now_path, "path": cls.now_path}]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def pump(self, until, ms=8000):
        from PyQt6.QtCore import QEventLoop, QTimer
        end = time.time() + ms / 1000
        while time.time() < end and not until():
            loop = QEventLoop()
            QTimer.singleShot(30, loop.quit)
            loop.exec()
        return until()

    def test_then_now_view_aligns_and_drags(self):
        import echoes_view
        v = echoes_view.ThenNowView(self.echo)
        v.resize(1000, 700)
        v.show()
        self.assertTrue(self.pump(lambda: not v.images[1].isNull()))
        self.assertIsNotNone(v.matrix, "stejná scéna se má srovnat")
        self.assertFalse(v.grab().isNull())
        r = v.image_rect()
        v._set_from_x(r.left() + r.width() * 0.25)
        self.assertAlmostEqual(v.split.value, 0.25, places=2)
        from PyQt6.QtTest import QTest
        QTest.keyClick(v, Qt.Key.Key_Right)
        self.assertAlmostEqual(v.split.value, 0.30, places=2)
        opened = []
        v.open_photo.connect(opened.append)
        QTest.mouseDClick(v, Qt.MouseButton.LeftButton, pos=r.center().toPoint())
        self.assertEqual(opened, [2], "dvojklik vpravo od čáry = snímek „teď“")
        v.close()

    def test_panel_empty_state_and_cards(self):
        import echoes_view
        db = os.path.join(self.tmp, "lib.db")
        L = Library(db)
        L.add("view", 49.1, 13.5, "2019-08-10 10:00:00", city="Kvilda", thumbnail_path=self.then_path, file_path=self.then_path)
        L.add("view", 49.1, 13.5, "2026-07-07 10:00:00", city="Kvilda", thumbnail_path=self.now_path, file_path=self.now_path)
        L.conn.commit()
        panel = echoes_view.EchoesPanel(lambda: sqlite3.connect(db))
        panel.resize(900, 600)
        panel.show()
        counts = []
        panel.loaded.connect(counts.append)
        panel.load()
        self.assertTrue(self.pump(lambda: counts))
        self.assertEqual(counts, [1])
        self.assertEqual(len(panel.cards), 1)
        self.assertFalse(panel.cards[0].grab().isNull())
        got = []
        panel.opened.connect(got.append)
        from PyQt6.QtTest import QTest
        QTest.keyClick(panel.cards[0], Qt.Key.Key_Return)
        self.assertEqual(got[0]["items"][1]["year"], 2026)

        empty_db = os.path.join(self.tmp, "empty.db")
        Library(empty_db).conn.commit()
        empty = echoes_view.EchoesPanel(lambda: sqlite3.connect(empty_db))
        empty.show()
        done = []
        empty.loaded.connect(done.append)
        empty.load()
        self.assertTrue(self.pump(lambda: done))
        self.assertEqual(done, [0])
        self.assertTrue(empty.empty.isVisible())
        panel.close()
        empty.close()

    def test_sidebar_has_echoes_entry(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), encoding="utf-8").read()
        self.assertIn('("echoes", "Ozvěny", "echo")', src)
        import icons
        self.assertIn("echo", icons.ICONS)


if __name__ == "__main__":
    unittest.main()
