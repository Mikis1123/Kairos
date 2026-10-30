"""Sprint 4.4 – Konstelace: síť lidí z face_instances, rozvržení, společný příběh, zobrazení."""
import math
import os
import shutil
import sys
import tempfile
import unittest

import constellations as C
import database

HAS_QT = False
try:
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False


class Library:
    """Ana a Ben spolu na 10 fotkách (2 události + den bez události), Ana a Cyril na 2, Dana na 1 (mimo síť)."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="const_")
        self.saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.dir, "t.db")
        database.init_db()
        with database.db_transaction() as c:
            for pid, name in ((1, "Ana"), (2, "Ben"), (3, "Neznámá osoba"), (4, "Dana"), (5, "Eva")):
                c.execute("INSERT INTO people (id, name) VALUES (?, ?)", (pid, name))
            c.execute("INSERT INTO events (id, title, start_date) VALUES (1, 'Výlet: Alpy (06. 2023)', '2023-06-01')")
            c.execute("INSERT INTO events (id, title, start_date) VALUES (2, 'Svatba', '2024-05-01')")
            mid = 0

            def photo(people, date, event=None, trashed=0):
                nonlocal mid
                mid += 1
                c.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, event_id, is_trashed) "
                          "VALUES (?, ?, ?, 'photo', ?, ?, ?)", (mid, f"C:/x/{mid}.jpg", f"{mid}.jpg", date, event, trashed))
                for p in people:
                    c.execute("INSERT INTO face_instances (media_id, person_id, embedding) VALUES (?, ?, x'00')", (mid, p))
            for k in range(6):
                photo([1, 2], f"2023:06:0{1 + k % 3} 10:00:00", 1)
            for k in range(3):
                photo([1, 2], f"2024:05:01 1{k}:00:00", 2)
            photo([1, 2], "2025:01:10 09:00:00")
            photo([1, 2], "2025:01:11 09:00:00", trashed=1)            # koš se nepočítá
            photo([1, 3], "2024:05:01 15:00:00", 2)
            photo([1, 3], "2024:05:01 16:00:00", 2)
            for k in range(3):
                photo([3], f"2022:01:0{k + 1} 10:00:00")
                photo([5], f"2022:02:0{k + 1} 10:00:00")
            photo([4], "2022:03:01 10:00:00")
            photo([1, 5], "2022:04:01 10:00:00")                          # jedna společná = náhoda

    def close(self):
        database.DB_PATH = self.saved
        shutil.rmtree(self.dir, True)


class TestGraph(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        cls.g = C.graph(database.get_connection())

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def test_nodes_and_edges(self):
        ids = {n["id"] for n in self.g["nodes"]}
        self.assertEqual(ids, {1, 2, 3, 5}, "Dana má jen 1 fotku")
        pairs = {(e["a"], e["b"]): e for e in self.g["edges"]}
        self.assertEqual(set(pairs), {(1, 2), (1, 3)}, "Ana–Eva: jediná společná fotka není pouto")
        self.assertEqual(pairs[(1, 2)]["n"], 10, "fotka v koši se nepočítá")
        self.assertEqual(pairs[(1, 2)]["w"], 1.0, "Ben je na všech fotkách s Anou")
        self.assertAlmostEqual(pairs[(1, 3)]["w"], 2 / 5)

    def test_labels(self):
        by = {n["id"]: n for n in self.g["nodes"]}
        self.assertEqual(by[1]["label"], "Ana")
        self.assertEqual(by[3]["label"], "Osoba 1", "nepojmenovaná osoba dostane číslo")

    def test_layout_deterministic_and_strong_ties_close(self):
        again = C.graph(database.get_connection())
        self.assertEqual([(n["x"], n["y"]) for n in again["nodes"]], [(n["x"], n["y"]) for n in self.g["nodes"]])
        by = {n["id"]: n for n in self.g["nodes"]}
        for n in self.g["nodes"]:
            self.assertLessEqual(max(abs(n["x"]), abs(n["y"])), 1.0 + 1e-9)
        d = lambda a, b: math.dist((by[a]["x"], by[a]["y"]), (by[b]["x"], by[b]["y"]))  # noqa: E731
        self.assertLess(d(1, 2), d(2, 5), "silné pouto blízko, bez pouta daleko")

    def test_companions(self):
        self.assertEqual(C.companions(self.g, 1), [(2, 10), (3, 2)])
        self.assertEqual(C.companions(self.g, 5), [])

    def test_story(self):
        s = C.story(database.get_connection(), 1, 2)
        self.assertEqual(s["together"], 10)
        self.assertEqual([m["count"] for m in s["moments"]], [6, 3, 1])
        self.assertEqual(s["moments"][0]["title"], "Výlet: Alpy")
        self.assertEqual(s["moments"][2]["key"], "d2025-01-10", "bez události = po dnech")
        self.assertEqual(s["first"][:10], "2023-06-01")
        self.assertEqual(sorted(s["moments"][1]["ids"]), s["moments"][1]["ids"])
        self.assertEqual(C.story(database.get_connection(), 2, 5)["moments"], [])

    def test_layout_edge_cases(self):
        self.assertEqual(C.layout([], []), {})
        self.assertEqual(C.layout([7], []), {7: (0.0, 0.0)})


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = Library()
        cls.g = C.graph(database.get_connection())

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def test_hit_select_and_story_signal(self):
        import constellations_view as V
        v = V.ConstellationView(self.g, {})
        v.resize(900, 600)
        v.show()
        ana = v.nodes[1]
        self.assertEqual(v.hit(v.pos(ana)), ("node", 1))
        a, b = v.pos(v.nodes[1]), v.pos(v.nodes[2])
        mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
        if v.hit(mid) and v.hit(mid)[0] == "edge":
            self.assertEqual(v.hit(mid), ("edge", (1, 2)))
        self.assertIsNone(v.hit(QPointF(2, 2)))
        got = []
        v.story_requested.connect(lambda x, y: got.append((x, y)))
        v.select(1)
        self.assertEqual(v._related(1), {2, 3})
        v.repaint()
        v.close()

    def test_panel_and_story_view(self):
        import constellations_view as V
        side = V.SidePanel(self.g)
        got = []
        side.story_requested.connect(lambda a, b: got.append((a, b)))
        rows = [side.lay.itemAt(i).widget() for i in range(side.lay.count()) if isinstance(side.lay.itemAt(i).widget(), V._PairRow)]
        self.assertEqual(len(rows), 2)
        rows[0].clicked.emit()
        self.assertEqual(got, [(1, 2)])
        side.show_for(3)
        story = C.story(database.get_connection(), 1, 2)
        sv = V.StoryView(story)
        self.assertEqual(len(sv.rows), 3)
        opened = []
        sv.open_moment.connect(opened.append)
        sv.rows[0].clicked.emit()
        self.assertEqual(len(opened[0]), 6)
        self.assertIn("10 společných fotek", V.story_subtitle(story))
        sv.resize(800, 500)
        sv.show()
        sv.grab()
        sv.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
