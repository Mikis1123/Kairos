"""Stage 4 – Alba v2: složky (strom), ruční pořadí alb i položek, vlastní obálka, přetahování
v panelu Alb a v mřížce alba, list exportu."""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

import albums

HAS_QT = False
try:
    import main  # noqa: F401  – musí být před QApplication (WebEngine)
    from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt
    from PyQt6.QtGui import QDropEvent, QImage, QColor
    from PyQt6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception as e:                                  # pragma: no cover
    print(f"Qt nedostupné: {e}")


def make_db(n=6):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, thumbnail_path TEXT,
                    is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0)""")
    conn.executemany("INSERT INTO media_items VALUES (?, ?, ?, 0, 0)",
                     [(i, f"2024-01-{i:02d} 10:00:00", f"t{i}.jpg") for i in range(1, n + 1)])
    albums.ensure_tables(conn)
    return conn


class TestTree(unittest.TestCase):
    def test_folders_nesting_and_paths(self):
        conn = make_db()
        trips = albums.create_folder(conn, "Cesty")
        italy = albums.create_folder(conn, "Itálie", trips)
        rome = albums.create_album(conn, "Řím", [1, 2], italy)
        root_album = albums.create_album(conn, "Rodina", [3])
        self.assertEqual([n["name"] for n in albums.children(conn)], ["Cesty", "Rodina"])
        self.assertEqual([n["name"] for n in albums.children(conn, trips)], ["Itálie"])
        self.assertEqual(albums.ancestors(conn, rome), [(trips, "Cesty"), (italy, "Itálie"), (rome, "Řím")])
        listed = {a["name"]: a for a in albums.list_albums(conn)}
        self.assertEqual(set(listed), {"Řím", "Rodina"}, "složky nejsou v nabídkách alb")
        self.assertEqual(listed["Řím"]["path"], "Cesty › Itálie › Řím")
        folder = albums.children(conn)[0]
        self.assertEqual((folder["kind"], folder["count"]), ("folder", 1))
        self.assertEqual(folder["covers"], ["t2.jpg"], "obálka složky = obálky alb uvnitř (i hlouběji)")
        self.assertEqual(albums.list_folders(conn), [(trips, "Cesty"), (italy, "Cesty › Itálie")])
        with self.assertRaises(ValueError):
            albums.create_album(conn, "X", (), root_album)          # rodič musí být složka

    def test_move_reorder_and_cycle_guard(self):
        conn = make_db()
        a, b, c = (albums.create_album(conn, n) for n in "ABC")
        f = albums.create_folder(conn, "F")
        albums.move_node(conn, c, None, a)
        self.assertEqual([n["name"] for n in albums.children(conn)], ["C", "A", "B", "F"])
        albums.move_node(conn, a, f)
        self.assertEqual([n["name"] for n in albums.children(conn, f)], ["A"])
        self.assertEqual([n["name"] for n in albums.children(conn)], ["C", "B", "F"])
        g = albums.create_folder(conn, "G", f)
        with self.assertRaises(ValueError):
            albums.move_node(conn, f, g)                        # do vlastního potomka ne
        with self.assertRaises(ValueError):
            albums.move_node(conn, f, f)

    def test_delete_folder_lifts_children(self):
        conn = make_db()
        f = albums.create_folder(conn, "F")
        inner = albums.create_folder(conn, "Inner", f)
        a = albums.create_album(conn, "A", [1], inner)
        albums.delete_album(conn, inner)
        self.assertEqual([n["id"] for n in albums.children(conn, f)], [a])
        albums.delete_album(conn, f)
        self.assertEqual([n["id"] for n in albums.children(conn)], [a])
        self.assertEqual(albums.item_ids(conn, a), [1], "fotky alba nedotčené")

    def test_migration_from_v1_schema(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, thumbnail_path TEXT,"
                     " is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0)")
        conn.execute("""CREATE TABLE albums (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                        kind TEXT NOT NULL DEFAULT 'manual', query TEXT, filter TEXT, created_at TEXT,
                        cover_media_id INTEGER)""")
        conn.execute("""CREATE TABLE album_items (album_id INTEGER NOT NULL, media_id INTEGER NOT NULL,
                        added_at TEXT, PRIMARY KEY (album_id, media_id))""")
        conn.executemany("INSERT INTO albums (name) VALUES (?)", [("zima",), ("Jaro",), ("Léto",)])
        albums.ensure_tables(conn)
        albums.ensure_tables(conn)                              # podruhé nic nerozbije
        self.assertEqual([n["name"] for n in albums.children(conn)], ["Jaro", "Léto", "zima"])


class TestItemsOrderAndCover(unittest.TestCase):
    def test_custom_order_drag(self):
        conn = make_db()
        a = albums.create_album(conn, "A", [4, 1, 3, 2])
        self.assertEqual(albums.item_ids(conn, a), [1, 2, 3, 4], "výchozí = podle data")
        self.assertEqual(albums.move_items(conn, a, [4, 3], 1), [4, 3, 1, 2])
        self.assertEqual(albums.get_album(conn, a)["sort_mode"], "custom")
        self.assertEqual(albums.item_ids(conn, a), [4, 3, 1, 2])
        albums.add_items(conn, a, [5])
        self.assertEqual(albums.item_ids(conn, a)[-1], 5, "nová položka na konec vlastního pořadí")
        albums.move_items(conn, a, [4], None)
        self.assertEqual(albums.item_ids(conn, a), [3, 1, 2, 5, 4])
        albums.set_sort_mode(conn, a, "date")
        self.assertEqual(albums.item_ids(conn, a), [1, 2, 3, 4, 5])
        albums.set_sort_mode(conn, a, "custom")
        self.assertEqual(albums.item_ids(conn, a), [1, 2, 3, 4, 5], "vlastní začíná od toho, co je vidět")
        conn.execute("UPDATE media_items SET is_hidden = 1 WHERE id = 3")
        self.assertEqual(albums.move_items(conn, a, [5], 1)[:3], [5, 1, 2], "skrytá položka se nepřesouvá")
        self.assertNotIn(3, albums.item_ids(conn, a))

    def test_cover_choice_and_reset(self):
        conn = make_db()
        a = albums.create_album(conn, "A", [1, 2])
        albums.set_cover(conn, a, 1)
        self.assertEqual(albums.children(conn)[0]["cover"], "t1.jpg")
        with self.assertRaises(ValueError):
            albums.set_cover(conn, a, 5)                         # jen položka alba
        albums.set_cover(conn, a, None)
        self.assertEqual(albums.children(conn)[0]["cover"], "t2.jpg", "automatická = nejnovější")
        conn.execute("UPDATE media_items SET is_hidden = 1 WHERE id = 2")
        albums.set_cover(conn, a, 2)
        self.assertEqual(albums.children(conn)[0]["cover"], "t1.jpg", "skrytá obálka se nezobrazí")

    def test_export_ids_for_folder_deduplicated(self):
        conn = make_db()
        f = albums.create_folder(conn, "F")
        albums.create_album(conn, "A", [1, 2], f)
        sub = albums.create_folder(conn, "S", f)
        albums.create_album(conn, "B", [2, 3], sub)
        albums.create_smart_album(conn, "Chytré", "moře", parent_id=f)
        self.assertEqual(sorted(albums.export_item_ids(conn, f)), [1, 2, 3])
        conn.execute("UPDATE media_items SET is_trashed = 1 WHERE id = 1")
        self.assertEqual(sorted(albums.export_item_ids(conn, f)), [2, 3])


@unittest.skipUnless(HAS_QT, "Qt nedostupné")
class TestAlbumsPanelDnD(unittest.TestCase):
    def setUp(self):
        import albums_view
        self.av = albums_view
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "a.db")
        conn = sqlite3.connect(self.db)
        conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, thumbnail_path TEXT,
                        is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0)""")
        for i in range(1, 5):
            p = os.path.join(self.tmp, f"t{i}.png")
            img = QImage(40, 30, QImage.Format.Format_RGB32)
            img.fill(QColor(40 * i, 90, 160))
            img.save(p)
            conn.execute("INSERT INTO media_items VALUES (?, ?, ?, 0, 0)", (i, f"2024-01-0{i}", p))
        albums.ensure_tables(conn)
        self.f = albums.create_folder(conn, "Cesty")
        self.a = albums.create_album(conn, "A", [1])
        self.b = albums.create_album(conn, "B", [2])
        albums.create_album(conn, "Uvnitř 1", [3], self.f)
        albums.create_album(conn, "Uvnitř 2", [4], self.f)
        conn.commit()
        conn.close()
        self.panel = self.av.AlbumsPanel(lambda: sqlite3.connect(self.db))
        self.panel.resize(900, 600)
        self.panel.show()
        self.panel.load()
        _app.processEvents()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        _app.processEvents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _drop(self, panel, node_id, card, x_frac):
        mime = QMimeData()
        mime.setData(self.av.NODE_MIME, str(node_id).encode("ascii"))
        local = QPoint(int(card.width() * x_frac), 60)
        pos = QPointF(card.mapTo(panel, local))
        ev = QDropEvent(pos, Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        panel.dropEvent(ev)
        _app.processEvents()

    def _names(self, folder=None):
        conn = sqlite3.connect(self.db)
        try:
            return [n["name"] for n in albums.children(conn, folder)]
        finally:
            conn.close()

    def _card(self, name, panel=None):
        return next(c for c in (panel or self.panel).cards if c.album["name"] == name)

    def test_drop_action_zones(self):
        self.assertEqual(self.av.drop_action("folder", 240, 120), "into")
        self.assertEqual(self.av.drop_action("folder", 240, 10), "before")
        self.assertEqual(self.av.drop_action("manual", 240, 120), "after")

    def test_folder_card_shows_mosaic_and_summary(self):
        card = self._card("Cesty")
        self.assertEqual(self.av.subtitle(card.album), "2 alba")
        self.assertEqual(len(card.album["covers"]), 2)
        card.grab()                                          # vykreslení mozaiky bez pádu
        self.assertFalse(card._img.isNull())

    def test_drop_into_folder_and_reorder(self):
        self._drop(self.panel, self.b, self._card("Cesty"), 0.5)
        self.assertEqual(self._names(), ["Cesty", "A"])
        self.assertEqual(self._names(self.f), ["Uvnitř 1", "Uvnitř 2", "B"])
        self._drop(self.panel, self.a, self._card("Cesty"), 0.05)
        self.assertEqual(self._names(), ["A", "Cesty"])

    def test_subfolder_up_target(self):
        sub = self.av.AlbumsPanel(lambda: sqlite3.connect(self.db), folder_id=self.f)
        sub.resize(900, 600)
        sub.show()
        sub.load()
        _app.processEvents()
        inner = self._card("Uvnitř 2", sub)
        self._drop(sub, inner.album["id"], self._card("Uvnitř 1", sub), 0.1)
        self.assertEqual(self._names(self.f), ["Uvnitř 2", "Uvnitř 1"])
        self.assertTrue(sub.move(inner.album["id"], sub._parent_id, None))
        self.assertEqual(self._names(), ["Cesty", "A", "B", "Uvnitř 2"])
        sub.close()
        sub.deleteLater()

    def test_move_menu_excludes_self_and_descendants(self):
        from PyQt6.QtWidgets import QMenu
        conn = sqlite3.connect(self.db)
        inner = albums.create_folder(conn, "Hluboko", self.f)
        conn.commit()
        conn.close()
        menu = QMenu()
        targets = self.panel._move_menu(menu, self._card("Cesty").album)
        self.assertNotIn(self.f, targets.values())
        self.assertNotIn(inner, targets.values())
        targets = self.panel._move_menu(QMenu(), self._card("A").album)
        self.assertIn(self.f, targets.values())
        self.assertNotIn(None, targets.values(), "už v kořeni – kořen se nenabízí")


@unittest.skipUnless(HAS_QT, "Qt nedostupné")
class TestGridReorder(unittest.TestCase):
    def test_insertion_row(self):
        import album_reorder
        self.assertEqual(album_reorder.insertion_row(50, 10, 3, 9), 3)
        self.assertEqual(album_reorder.insertion_row(50, 90, 3, 9), 4)
        self.assertEqual(album_reorder.insertion_row(50, 90, -1, 9), 9)

    def test_drop_calls_move_with_next_unmoved_item(self):
        import album_reorder
        grid = main.AppleGridView(None)
        grid.resize(800, 600)
        grid.set_cell_size(150)
        grid.photo_model.set_records([(i, f"{i}.jpg", f"C:/x/{i}.jpg", "photo", None, None, "2024", 0)
                                      for i in range(1, 7)])
        grid.show()
        _app.processEvents()
        calls = []
        ro = album_reorder.GridReorder(grid, lambda ids, before: calls.append((ids, before)))
        # syntetický Drop bez skutečného tahu Qt nedoručí – rovnou do filtru viewportu
        r = grid.visualRect(grid.photo_model.index(1, 0))     # druhá dlaždice, levá polovina = před ni
        mime = QMimeData()
        mime.setData(album_reorder.MIME, b"5,6")
        ev = QDropEvent(QPointF(r.left() + 5, r.center().y()), Qt.DropAction.MoveAction, mime,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.assertTrue(ro.eventFilter(grid.viewport(), ev))
        self.assertEqual(calls, [([5, 6], 2)])
        r = grid.visualRect(grid.photo_model.index(4, 0))     # pravá polovina dlaždice 5 (přesouvá se) → konec
        ev = QDropEvent(QPointF(r.right() - 5, r.center().y()), Qt.DropAction.MoveAction, mime,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.assertTrue(ro.eventFilter(grid.viewport(), ev))
        self.assertEqual(calls[-1], ([5, 6], None))
        grid.close()
        grid.deleteLater()


@unittest.skipUnless(HAS_QT, "Qt nedostupné")
class TestExportSheet(unittest.TestCase):
    def test_options_roundtrip_and_privacy_defaults(self):
        import export_sheet
        o = export_sheet.options_from_config({}, "Léto")
        self.assertTrue(o.strip_gps and o.strip_people and o.neutral_names)
        self.assertEqual((o.layout, o.package, o.title), ("files", "folder", "Léto"))
        o.layout, o.package, o.strip_people = "web", "zip", False
        cfg = {"export_options": export_sheet.options_to_config(o)}
        back = export_sheet.options_from_config(cfg)
        self.assertEqual((back.layout, back.package, back.strip_people), ("web", "zip", False))
        bad = export_sheet.options_from_config({"export_options": {"layout": "pdf", "strip_gps": "ano"}})
        self.assertEqual(bad.layout, "files")
        self.assertTrue(bad.strip_gps)

    def test_sheet_reflects_controls_and_blocks_without_exiftool(self):
        import export_sheet
        from PyQt6.QtWidgets import QWidget
        host = QWidget()
        host.resize(800, 600)
        sheet = export_sheet.ExportSheet(host, 3, export_sheet.options_from_config({}), exiftool_ok=False)
        self.assertFalse(sheet.btn_action.isEnabled(), "bez ExifToolu se soukromý export nespustí")
        for t in (sheet.t_gps, sheet.t_people, sheet.t_camera):
            t.setChecked(False)
        self.assertTrue(sheet.btn_action.isEnabled())
        sheet.seg_layout.set_current("web")
        sheet._sync()
        self.assertFalse(sheet.t_live.isEnabled())
        o = sheet.chosen()
        self.assertEqual((o.strip_gps, o.layout), (False, "web"))
        sheet.deleteLater()
        host.deleteLater()


@unittest.skipUnless(HAS_QT, "Qt nedostupné")
class TestMainWindowAlbumsV2(unittest.TestCase):
    """Celý tok v okně: kořen Alb → složka → album (řazení, přetažení, obálka) → export."""

    def setUp(self):
        from unittest import mock
        from test_patch9 import TempEnv
        from test_patch11_maps import wait
        self.wait = wait
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.ids = [self.env.add_media(self.env.jpg(f"p{i}.jpg"), date=f"2024-05-0{i} 10:00:00") for i in range(1, 5)]
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config", lambda c: None)):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w.resize(1280, 800)
        self.w.show()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        wait(30)

    def _edit(self, fn):
        return self.w._album_edit(fn)

    def test_folder_navigation_reorder_cover_and_export(self):
        from unittest import mock
        f = self._edit(lambda c: albums.create_folder(c, "Cesty"))
        a = self._edit(lambda c: albums.create_album(c, "Řím", self.ids, f))
        self.w.open_albums()
        root_panel = self.w._albums_panels[-1][0]
        self.assertEqual([n["name"] for n in root_panel.albums], ["Cesty"])
        self.w._open_album(root_panel.albums[0])                  # složka = další úroveň
        sub_panel, sub_page = self.w._albums_panels[-1]
        self.assertEqual(sub_panel.folder_id, f)
        self.assertEqual(sub_page.lbl_title.text(), "Cesty")
        self.w._open_album(sub_panel.albums[0])                   # ruční album
        self.wait(30)
        grid = next(g for g in self.w.findChildren(main.AppleGridView)
                    if g.isVisible() and g is not self.w.grid_view)
        self.assertEqual([int(r[0]) for r in grid.photo_model.records], self.ids, "podle data")
        grid._reorder.on_move([self.ids[3]], self.ids[0])
        self.assertEqual([int(r[0]) for r in grid.photo_model.records][0], self.ids[3])
        conn = database_conn()
        try:
            self.assertEqual(albums.get_album(conn, a)["sort_mode"], "custom")
        finally:
            conn.close()
        with mock.patch.object(main, "show_toast"):
            self.w._begin_cover_pick({"id": a, "name": "Řím"}, grid)
            grid.item_activated.emit(2)                            # aktivace = obálka, ne prohlížeč
        conn = database_conn()
        try:
            self.assertEqual(albums.get_album(conn, a)["cover_id"], int(grid.photo_model.records[2][0]))
        finally:
            conn.close()
        self.assertIsNone(getattr(grid, "_cover_pick_cb", None))
        # export alba: list potvrzen (mock), cíl zvolen (mock), job se spustí s položkami alba
        import export_sheet
        started = {}

        class FakeJob:
            def __init__(self, items, opts, dest, names, parent=None):
                started.update(items=items, opts=opts, dest=dest)
                self.progress = self.finished_ok = self.failed = self.cancelled = mock.MagicMock()

            def start(self):
                started["running"] = True

            def isRunning(self):
                return False

            def stop(self):
                pass
        with mock.patch.object(export_sheet, "ask_options", lambda host, n, o, ok: o), \
                mock.patch.object(export_sheet, "PrivacyExportJob", FakeJob), \
                mock.patch.object(main.QFileDialog, "getExistingDirectory", lambda *a, **k: self.env.tmp), \
                mock.patch.object(main, "show_toast"):
            self.w._export_album({"id": f, "name": "Cesty"})
        self.assertTrue(started.get("running"))
        self.assertEqual(sorted(it["id"] for it in started["items"]), sorted(self.ids))
        self.assertTrue(started["dest"].startswith(os.path.join(self.env.tmp, "Cesty")))
        self.assertTrue(started["opts"].strip_gps and started["opts"].strip_people)


def database_conn():
    import database
    return database.get_connection()


if __name__ == "__main__":
    unittest.main()
