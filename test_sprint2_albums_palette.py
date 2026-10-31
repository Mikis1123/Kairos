"""Sprint 2 – alba (ruční / chytrá) a paleta příkazů (Ctrl + K)."""
import sqlite3
import sys
import unittest

import albums
import command_palette as cp

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, thumbnail_path TEXT,
                    is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0)""")
    conn.executemany("INSERT INTO media_items VALUES (?, ?, ?, 0, 0)",
                     [(i, f"2024-01-0{i} 10:00:00", f"t{i}.jpg") for i in range(1, 6)])
    albums.ensure_tables(conn)
    return conn


class TestAlbums(unittest.TestCase):
    def test_manual_album_lifecycle(self):
        conn = make_db()
        aid = albums.create_album(conn, "  Léto   u moře ", [3, 1, 1])
        self.assertEqual(albums.get_album(conn, aid)["name"], "Léto u moře")
        self.assertEqual(albums.item_ids(conn, aid), [1, 3], "podle data, bez duplicit")
        self.assertEqual(albums.add_items(conn, aid, [3, 4]), 1, "už přítomné se nepočítá")
        conn.execute("UPDATE media_items SET is_trashed = 1 WHERE id = 4")
        self.assertEqual(albums.item_ids(conn, aid), [1, 3], "koš se v albu nezobrazuje")
        listed = albums.list_albums(conn)[0]
        self.assertEqual(listed["count"], 2)
        self.assertEqual(listed["cover"], "t3.jpg", "obálka = nejnovější viditelná")
        albums.set_cover(conn, aid, 1)
        self.assertEqual(albums.list_albums(conn)[0]["cover"], "t1.jpg")
        albums.remove_items(conn, aid, [1])
        self.assertEqual(albums.list_albums(conn)[0]["cover"], "t3.jpg", "odebraná obálka se zahodí")
        albums.rename_album(conn, aid, "Moře")
        albums.delete_album(conn, aid)
        self.assertEqual(albums.list_albums(conn), [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM album_items").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0], 5, "fotky zůstávají")

    def test_smart_album(self):
        conn = make_db()
        aid = albums.create_smart_album(conn, "Západy", "  západ   slunce ", "favorites")
        a = albums.get_album(conn, aid)
        self.assertEqual((a["kind"], a["query"], a["filter"]), ("smart", "západ slunce", "favorites"))
        self.assertIsNone(albums.list_albums(conn)[0]["count"])
        self.assertEqual(albums.get_album(conn, albums.create_smart_album(conn, "X", "les", "bogus"))["filter"], "all")
        with self.assertRaises(ValueError):
            albums.create_smart_album(conn, "Prázdné", "   ")
        with self.assertRaises(ValueError):
            albums.create_album(conn, "  ")

    def test_init_db_creates_tables(self):
        import os
        import tempfile
        import database
        d = tempfile.mkdtemp()
        old = database.DB_PATH
        database.DB_PATH = os.path.join(d, "t.db")
        try:
            database.init_db()
            conn = database.get_connection()
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            cols = {r[1] for r in conn.execute("PRAGMA table_info(media_items)")}
            conn.close()
        finally:
            database.DB_PATH = old
        self.assertTrue({"albums", "album_items"} <= names)
        self.assertIn("gps_source", cols)


def cmds():
    noop = lambda: None  # noqa: E731
    return [cp.Command("Fotografie", noop, "page"), cp.Command("Osoby", noop, "page", keywords="tváře lidé"),
            cp.Command("Mapa", noop, "page"), cp.Command("Jan Novák", noop, "person"),
            cp.Command("Žluťoučký kůň", noop, "album"), cp.Command("Ostrava", noop, "event"),
            cp.Command("Předvolby", noop, "action", keywords="nastavení")]


class TestPaletteRanking(unittest.TestCase):
    def test_fold(self):
        self.assertEqual(cp.fold("Žluťoučký Kůň"), "zlutoucky kun")

    def test_diacritics_and_case_insensitive(self):
        self.assertEqual(cp.rank("zlutoucky", cmds())[0].title, "Žluťoučký kůň")
        self.assertEqual(cp.rank("PREDV", cmds())[0].title, "Předvolby")

    def test_word_prefixes_and_abbreviation(self):
        self.assertEqual(cp.rank("jan nov", cmds())[0].title, "Jan Novák")
        self.assertEqual(cp.rank("jn nk", cmds())[0].title, "Jan Novák")

    def test_keywords(self):
        self.assertEqual(cp.rank("nastavení", cmds())[0].title, "Předvolby")
        self.assertEqual(cp.rank("tváře", cmds())[0].title, "Osoby")

    def test_prefix_beats_substring(self):
        self.assertGreater(cp.score("ost", "Ostrava"), cp.score("ost", "Kostel"))

    def test_no_match_and_empty_query(self):
        self.assertEqual(cp.rank("qqqq", cmds()), [])
        self.assertTrue(all(c.kind in ("page", "action") for c in cp.rank("", cmds())))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPaletteWidget(unittest.TestCase):
    def setUp(self):
        self.win = QWidget()
        self.win.resize(900, 700)
        self.win.show()
        self.ran = []
        noop = lambda: None  # noqa: E731
        items = [cp.Command("Mapa", lambda: self.ran.append("map"), "page"),
                 cp.Command("Jan Novák", lambda: self.ran.append("jan"), "person"), cp.Command("Osoby", noop, "page")]
        self.pal = cp.install(self.win, lambda: items,
                              lambda q: cp.Command(f"Hledat „{q}“", lambda: self.ran.append(("search", q)), "search"))

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()

    def test_shortcut_opens_and_enter_runs_top_match(self):
        # okno testu nemusí být aktivní (zkratka by nedorazila) – ověřit vazbu a spustit ji
        self.assertEqual(self.pal._shortcut.key().toString(), "Ctrl+K")
        self.pal._shortcut.activated.emit()
        self.assertTrue(self.pal.isVisible())
        QTest.keyClicks(self.pal.edit, "jan")
        titles = [c.title for c in self.pal.visible_commands()]
        self.assertEqual(titles[0], "Jan Novák")
        self.assertEqual(titles[-1], "Hledat „jan“", "hledání v knihovně je vždy poslední")
        QTest.keyClick(self.pal.edit, Qt.Key.Key_Return)
        self.assertEqual(self.ran, ["jan"])

    def test_arrows_and_escape(self):
        self.pal.open()
        QTest.keyClicks(self.pal.edit, "zzz")
        self.assertEqual([c.kind for c in self.pal.visible_commands()], ["search"])
        QTest.keyClick(self.pal.edit, Qt.Key.Key_Down)
        QTest.keyClick(self.pal.edit, Qt.Key.Key_Return)
        self.assertEqual(self.ran, [("search", "zzz")])
        self.pal.open("ma")
        QTest.keyClick(self.pal.edit, Qt.Key.Key_Escape)
        QTest.qWait(400)
        self.assertFalse(self.pal.isVisible())
        self.assertEqual(len(self.ran), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
