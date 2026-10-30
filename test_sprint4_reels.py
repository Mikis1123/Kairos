"""Sprint 4.2 – Throwback Reels: návrhy, kurátor (úsměv, ostrost, oblíbené, duplikáty), plán střihu
na doby (Director), renderer, export H.264 MP4 a přehrávač. Dočasná knihovna, žádná photos.db."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date

import numpy as np

import ai_engine
import database
import director
import paths
import reels
import smile

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

BEATS = {"period": 0.5, "bar": 4, "offset": 0.0, "length": 0.0}


def vec(seed, dim=512):
    v = np.random.default_rng(seed).normal(size=dim).astype(np.float32)
    return v / np.linalg.norm(v)


class Library:
    """Dočasná knihovna: dvě události, různé dny, miniatury jako skutečné soubory."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="reels_lib_")
        self.saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.dir, "t.db")
        database.init_db()
        from PIL import Image
        self.ids = []
        with database.db_transaction() as c:
            c.execute("INSERT INTO events (id, title, location_name, start_date, end_date) VALUES "
                      "(1, 'Výlet: Šumava (06. 07. – 08. 07. 2024)', 'Šumava', '2024-07-06', '2024-07-08')")
            c.execute("INSERT INTO events (id, title, start_date) VALUES (2, 'Malá', '2025-01-01')")
            for i in range(1, 25):
                day = 6 + (i - 1) // 8
                path = os.path.join(self.dir, f"p{i}.jpg")
                color = ((i * 37) % 255, (i * 91) % 255, (i * 53) % 255)
                Image.new("RGB", (360, 240) if i % 5 else (240, 360), color).save(path)
                ev = 1 if i <= 20 else 2
                c.execute("""INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, thumbnail_path,
                             is_favorite, sharpness_score, phash, event_id) VALUES (?, ?, ?, 'photo', ?, ?, ?, ?, ?, ?)""",
                          (i, path, f"p{i}.jpg", f"2024:07:{day:02d} 1{i % 10}:{i:02d}:00", path, int(i == 7),
                           50.0 + i * 3, f"{(1 << (i % 60)) | (i * 2654435761) & 0xFFFFFFFF:016x}", ev))
                c.execute("INSERT INTO image_embeddings (media_id, embedding) VALUES (?, ?)",
                          (i, ai_engine.encode_embedding(vec(i))))
                self.ids.append(i)
            # 3 = téměř stejný záběr jako 2 (série): stejný pHash
            c.execute("UPDATE media_items SET phash = (SELECT phash FROM media_items WHERE id = 2) WHERE id = 3")
            c.execute("INSERT INTO face_instances (media_id, embedding, bbox_x, bbox_y, bbox_w, bbox_h, smile) "
                      "VALUES (5, x'00', 0.6, 0.2, 0.2, 0.3, 0.95), (6, x'00', 0.1, 0.1, 0.2, 0.3, 0.05)")

    def close(self):
        database.DB_PATH = self.saved
        shutil.rmtree(self.dir, True)


class TestCurator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def conn(self):
        return database.get_connection()

    def test_suggestions(self):
        s = reels.suggestions(self.conn(), today=date(2026, 7, 7))
        keys = [x["key"] for x in s]
        self.assertEqual(keys[0], "throwback", "tento týden před 2 lety")
        self.assertIn("event:1", keys)
        self.assertNotIn("event:2", keys, "4 fotky – na sestřih málo")
        self.assertIn("year:2024", keys)
        ev = next(x for x in s if x["key"] == "event:1")
        self.assertEqual(ev["title"], "Výlet: Šumava", "datum z názvu pryč (je v podtitulku)")

    def test_candidates_carry_faces_and_aspect(self):
        items = {m["id"]: m for m in reels.candidates(self.conn(), "event:1")}
        self.assertEqual(len(items), 20)
        self.assertAlmostEqual(items[5]["smile"], 0.95)
        self.assertEqual(items[5]["faces"], 1)
        self.assertIsNotNone(items[5]["focus"])
        self.assertEqual((items[5]["w"], items[5]["h"]), (240, 360), "na výšku")
        self.assertEqual(items[4]["emb"].size, 512)

    def test_score_prefers_smiles_and_favorites(self):
        items = {m["id"]: m for m in reels.score(reels.candidates(self.conn(), "event:1"))}
        self.assertGreater(items[5]["score"], items[6]["score"], "úsměv > vážná tvář")
        self.assertGreater(items[7]["score"], items[8]["score"], "oblíbená")

    def test_curate_dedupes_and_spreads(self):
        items = reels.score(reels.candidates(self.conn(), "event:1"))
        chosen = reels.curate(items, n=10)
        ids = [m["id"] for m in chosen]
        self.assertEqual(len(chosen), 10)
        self.assertFalse(2 in ids and 3 in ids, "série = jeden záběr")
        self.assertEqual(ids, sorted(ids, key=lambda i: next(m["date"] for m in items if m["id"] == i)))
        days = {m["date"][:10] for m in chosen}
        self.assertEqual(len(days), 3, "rozprostřeno přes všechny dny")

    def test_same_scene_rule(self):
        a = {"phash": "0" * 16, "emb": vec(1), "date": "2024-07-06 10:00"}
        b = {"phash": "f" * 16, "emb": (vec(1) * 0.9 + vec(2) * 0.45), "date": "2024-07-06 10:02"}
        cos = reels._cos(a["emb"], b["emb"])
        self.assertTrue(reels.SAME_SCENE_COS <= cos < reels.NEAR_DUP_COS)
        self.assertTrue(reels._same_shot(a, b))
        self.assertFalse(reels._same_shot(a, dict(b, date="2024-07-06 12:00")), "jindy = jiná chvíle")

    def test_plan_cuts_on_beats(self):
        items = reels.curate(reels.score(reels.candidates(self.conn(), "event:1")), n=8)
        beats = {"period": 0.6, "bar": 4, "offset": 0.25, "length": 0.0}
        p = reels.plan(items, beats, "T", "S", "event:1")
        grid = director.beat_grid(beats, p["total"] + 1)
        on = set(round(b, 6) for b in grid["beats"])
        for g in p["segs"][1:]:
            self.assertIn(round(g["t0"], 6), on, f"střih {g['kind']} {g['t0']} na době")
            self.assertEqual(g["fade"], reels.DOWNBEAT_FADE_S if round(g["t0"], 6) in
                             set(round(b, 6) for b in grid["downbeats"]) else reels.CUT_FADE_S)
        shots = [g for g in p["segs"] if g["kind"] == "shot"]
        self.assertEqual(sum(1 for g in shots if g.get("caption")), 3, "popisek na prvním záběru dne")
        self.assertTrue(any(g["hero"] for g in shots))
        self.assertTrue(all(g["fit"] in ("fill", "contain") for g in shots))
        self.assertEqual(p, reels.plan(items, beats, "T", "S", "event:1"), "deterministické")

    def test_build_without_music(self):
        p = reels.build(self.conn(), {"key": "event:1", "title": "Šumava"}, None, text_model=FakeText())
        self.assertGreaterEqual(p["shots"], reels.MIN_SHOTS)
        self.assertAlmostEqual(p["beats"]["period"], reels.DEFAULT_BEATS["period"])
        self.assertEqual(p["range"][0][:7], "2024-07")


class FakeText:
    """Textový model pro testy: popisy -> pevné náhodné vektory (512)."""

    def encode(self, texts, **kw):
        single = isinstance(texts, str)
        out = np.stack([vec(abs(hash(t)) % 10_000) for t in ([texts] if single else texts)])
        return out[0] if single else out


class FakeImage:
    def encode(self, images, **kw):
        # „úsměv“ = světlý ořez: vektor blízko průměru pozitivních popisů
        pos, neg = smile.prompt_vectors(FakeText())
        return np.stack([pos if np.asarray(im).mean() > 128 else neg for im in images])


class TestSmile(unittest.TestCase):
    def test_probs_order(self):
        tm = FakeText()
        smile._prompt_cache.update(model=None)
        pos, neg = smile.prompt_vectors(tm)
        p = smile.smile_probs(np.stack([pos, neg]), tm)
        self.assertGreater(p[0], 0.9)
        self.assertLess(p[1], 0.1)

    def test_score_faces_writes_column(self):
        lib = Library()
        try:
            from PIL import Image
            crops = []
            for k, shade in enumerate((240, 20)):
                path = os.path.join(lib.dir, f"face{k}.jpg")
                Image.new("RGB", (64, 64), (shade,) * 3).save(path)
                crops.append(path)
            with database.db_transaction() as c:
                c.execute("UPDATE face_instances SET smile = NULL, face_crop_path = ? WHERE media_id = 5", (crops[0],))
                c.execute("UPDATE face_instances SET smile = NULL, face_crop_path = ? WHERE media_id = 6", (crops[1],))
                c.execute("INSERT INTO face_instances (media_id, embedding, face_crop_path) VALUES (7, x'00', 'nic.jpg')")
            conn = database.get_connection()
            smile._prompt_cache.update(model=None)
            n = smile.score_faces(conn, image_model=FakeImage(), text_model=FakeText())
            self.assertEqual(n, 3)
            got = dict(conn.execute("SELECT media_id, smile FROM face_instances").fetchall())
            self.assertGreater(got[5], 0.9)
            self.assertLess(got[6], 0.1)
            self.assertEqual(got[7], 0.0, "nečitelný ořez = 0, ne pořád dokola")
            self.assertEqual(smile.pending_faces(conn), [])
        finally:
            lib.close()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestRenderExportPlayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        conn = database.get_connection()
        items = reels.curate(reels.score(reels.candidates(conn, "event:1")), n=6)
        cls.plan = reels.plan(items, BEATS, "Šumava", "test", "event:1")
        cls.plan["range"] = ("2024-07-06", "2024-07-08")

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def renderer(self):
        import reel_render
        r = reel_render.ReelRenderer(self.plan, max_edge=640)
        self.assertTrue(r.load_all())
        return r

    def test_frames_differ_and_fade_blends(self):
        r = self.renderer()
        segs = self.plan["segs"]
        shot = segs[2]
        a = r.frame(shot["t0"] - 0.01, 320, 180)
        b = r.frame(shot["t0"] + shot["dur"] * 0.6, 320, 180)
        mid = r.frame(shot["t0"] + shot["fade"] / 2, 320, 180)
        pa, pb, pm = (QColorAt(x) for x in (a, b, mid))
        self.assertNotEqual(pa, pb)
        self.assertTrue(min(pa[0], pb[0]) - 2 <= pm[0] <= max(pa[0], pb[0]) + 2, "prolnutí = mezi oběma záběry")
        title = r.frame(0.5, 320, 180)
        self.assertFalse(title.isNull())

    def test_export_mp4_with_music(self):
        import reel_export
        if reel_export.video_encoder() is None:
            self.skipTest("ffmpeg bez H.264")
        out = os.path.join(self.lib.dir, "reel.mp4")
        music = os.path.join(self.lib.dir, "tone.wav")
        subprocess.run([paths.FFMPEG_PATH, "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=3",
                        music], check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        seen = []
        stats = reel_export.export(self.renderer(), out, music=music, fps=10, size=(320, 180), progress=seen.append)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(stats["frames"], int(round(self.plan["total"] * 10)))
        self.assertAlmostEqual(seen[-1], 1.0)
        probe = subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-i", out], capture_output=True, text=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stderr
        self.assertIn("h264", probe)
        self.assertIn("aac", probe, "hudba ve videu (skladba ve smyčce až do konce)")
        self.assertIn("yuv420p", probe)

    def test_export_cancel_cleans_up(self):
        import reel_export
        if reel_export.video_encoder() is None:
            self.skipTest("ffmpeg bez H.264")
        out = os.path.join(self.lib.dir, "cancel.mp4")
        with self.assertRaises(reel_export.ExportCancelled):
            reel_export.export(self.renderer(), out, fps=10, size=(320, 180), should_stop=lambda: True)
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.exists(out + ".part.mp4"))

    def test_player_follows_clock(self):
        import reels_view
        p = reels_view.ReelPlayer(self.renderer(), audio=False)
        p.resize(640, 360)
        p.show()
        p.play()
        for _ in range(10):
            p._last -= 0.05                    # 50 ms na tik
            p._tick()
        self.assertAlmostEqual(p.t, 0.5, delta=0.05)
        p.step(1)
        self.assertAlmostEqual(p.t, self.plan["segs"][1]["t0"])
        p.seek(self.plan["total"] + 5)
        self.assertEqual(p.t, self.plan["total"])
        p.repaint()
        p.stop()
        p.close()


def QColorAt(img):
    c = img.pixelColor(img.width() // 2, img.height() // 2)
    return c.red(), c.green(), c.blue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
