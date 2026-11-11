"""Regresní testy pro Patch 11 – dávka E: předvolby, světlý režim, koš, chytrý import.

Spuštění (ve složce projektu):   python -m pytest test_patch11_settings.py -v

Pokrývá:
  Motiv       – tokeny obou režimů, překlad stylů podle vlastnosti (pozadí / okraj / písmo),
                bílé písmo na barevném tlačítku zůstává, návrat do tmavého, nové widgety,
                prohlížeč a toasty zůstávají tmavé, „podle systému“.
  Koš         – rozdělení (pevný disk / vyjímatelný a síťový / bez souboru), Koš Windows,
                vyjímatelné se ve výchozím stavu přeskočí, trvalé smazání jen výslovně,
                neúspěch nechá položku v koši, kaskáda obličejů.
  Import      – nové soubory ve sledovaných složkách, video Live Photo se nepočítá zvlášť,
                toast „Importovat na pozadí · Přeskočit“.
  Obnovení    – jen config.json (+ volitelně mezipaměti), DB a originály zůstanou.
  Předvolby   – Standardní / Pokročilé, uložení motivu a rozpočtu náhledů, dopad pod prvky,
                potvrzovací listy jen u nevratných akcí.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db, config.json ani fotky se nepoužijí.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QPushButton
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import config as cfg
import database

if HAS_QT:
    import theme
    import library_tools
    import ui_toast
    from settings_dialog import SettingsDialog, ConfirmSheet


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class TempLibrary:
    """Dočasná DB + config.json pro jeden test."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p11s_")
        self._saved = (database.DB_PATH, cfg.CONFIG_FILE, cfg.LEGACY_CONFIG_FILE)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        cfg.CONFIG_FILE = os.path.join(self.tmp, "config.json")
        cfg.LEGACY_CONFIG_FILE = os.path.join(self.tmp, "legacy-config.json")
        database.init_db()

    def close(self):
        database.DB_PATH, cfg.CONFIG_FILE, cfg.LEGACY_CONFIG_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, name, content=b"x"):
        path = os.path.normpath(os.path.join(self.tmp, name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def media(self, path, trashed=0, paired=None):
        conn = database.get_connection()
        try:
            cur = conn.execute("INSERT INTO media_items (file_path, file_name, media_type, is_trashed, "
                               "paired_video_path) VALUES (?, ?, 'photo', ?, ?)",
                               (path, os.path.basename(path), trashed, paired))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Motiv
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestTheme(unittest.TestCase):

    def tearDown(self):
        theme.apply("dark")

    def test_tokens_complete_in_both_modes(self):
        self.assertEqual(set(theme.TOKENS["dark"]), set(theme.TOKENS["light"]))
        self.assertEqual(theme.color("bg", "dark"), "#0E0E10")          # Kairos Graphite Night
        self.assertNotEqual(theme.color("text", "light"), theme.color("text", "dark"))

    def test_remap_by_property_kind(self):
        qss = "QLabel { color: #F5F5F7; background: #1C1C1E; border: 1px solid #3A3A3C; }"
        out = theme.remap_qss(qss, "light")
        self.assertIn("color: #1D1D1F", out)
        self.assertIn("background: #F5F5F7", out, "tmavé pozadí -> světlé")
        self.assertIn("solid #D1D1D6", out, "okraj -> světlý oddělovač (ne bílá)")
        self.assertEqual(theme.remap_qss(qss, "dark"), qss)

    def test_white_text_kept_on_accent_button(self):
        qss = ("QPushButton#save { background-color: #0A84FF; color: #FFFFFF; } "
               "QLabel { color: #FFFFFF; }")
        out = theme.remap_qss(qss, "light")
        self.assertIn("background-color: #007AFF; color: #FFFFFF", out)
        self.assertIn("QLabel { color: #1D1D1F", out)

    def test_white_overlays_become_dark_overlays(self):
        out = theme.remap_qss("QPushButton:hover { background: rgba(255, 255, 255, 0.08); }", "light")
        self.assertIn("rgba(0, 0, 0, 0.08)", out)

    def test_apply_light_and_back_restores_original(self):
        root = QWidget()
        lbl = QLabel("x", root)
        dark = "QLabel { color: #F5F5F7; background: #2C2C2E; }"
        lbl.setStyleSheet(dark)
        theme.apply("light", root)
        self.assertIn("#1D1D1F", lbl.styleSheet())
        from PyQt6.QtGui import QPalette
        self.assertEqual(QApplication.palette().color(QPalette.ColorRole.Window).name().upper(), "#F5F5F7")
        theme.apply("dark", root)
        # zpět v tmavém: původní styl (Apple šedé převede Kairos na Graphite Night)
        self.assertEqual(lbl.property(theme._ORIG_PROPERTY), dark)
        self.assertEqual(lbl.styleSheet(), "QLabel { color: #F2F2F4; background: #1A1A1E; }")
        root.deleteLater()

    def test_widgets_created_or_restyled_later_follow_theme(self):
        theme.apply("light")
        root = QWidget()
        lbl = QLabel("nový", root)
        lbl.setStyleSheet("color: #8E8E93;")
        root.show()
        _app.processEvents()
        self.assertIn("#6E6E73", lbl.styleSheet(), "filtr přestyluje nový widget")
        lbl.setStyleSheet("color: #F5F5F7;")      # kód nastaví nový (tmavý) styl
        _app.processEvents()
        self.assertIn("#1D1D1F", lbl.styleSheet())
        theme.apply("dark", root)
        self.assertEqual(lbl.property(theme._ORIG_PROPERTY), "color: #F5F5F7;", "pamatuje si nejnovější tmavý styl")
        self.assertEqual(lbl.styleSheet(), "color: #F2F2F4;")
        root.hide()
        root.deleteLater()

    def test_viewer_and_toasts_stay_dark(self):
        root = QWidget()
        viewer = QWidget(root)
        viewer.setProperty(theme.FIXED_DARK_PROPERTY, True)
        inner = QLabel("v prohlížeči", viewer)
        inner.setStyleSheet("color: #F5F5F7;")
        toast = ui_toast.Toast("Hotovo", parent=root)
        theme.apply("light", root)
        self.assertEqual(inner.styleSheet(), "color: #F2F2F4;", "prohlížeč tmavý (Graphite), ne světlý")
        self.assertIn("#F2F2F4", toast.styleSheet())
        self.assertEqual(viewer.palette().color(viewer.backgroundRole()).name().upper(), "#0E0E10")
        root.deleteLater()

    def test_system_follows_windows_setting(self):
        with mock.patch.object(theme, "system_prefers_light", return_value=True):
            self.assertEqual(theme.resolve("system"), "light")
        with mock.patch.object(theme, "system_prefers_light", return_value=False):
            self.assertEqual(theme.resolve("system"), "dark")
        self.assertEqual(theme.resolve("nesmysl"), "dark")

    def test_painted_tokens_follow_mode(self):
        import main
        theme.apply("light")
        self.assertEqual(main.ApplePhotoDelegate().placeholder_color.name().upper(),
                         theme.color("placeholder", "light"))
        theme.apply("dark")
        self.assertEqual(main.ApplePhotoDelegate().placeholder_color.name().upper(), theme.color("placeholder", "dark"))


# ═════════════════════════════════════════════════════════════════════════════
# Koš
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestEmptyTrash(unittest.TestCase):

    def setUp(self):
        self.lib = TempLibrary()
        self.addCleanup(self.lib.close)

    def _kinds(self, removable_paths):
        real = library_tools.drive_kind
        return mock.patch.object(library_tools, "drive_kind",
                                 side_effect=lambda p: library_tools.DRIVE_REMOVABLE if p in removable_paths else real(p))

    def test_drive_kinds(self):
        self.assertEqual(library_tools.drive_kind(r"\\nas\fotky\a.jpg"), library_tools.DRIVE_NETWORK)
        self.assertEqual(library_tools.drive_kind(""), library_tools.DRIVE_MISSING)
        if sys.platform == "win32":
            self.assertEqual(library_tools.drive_kind(self.lib.tmp), library_tools.DRIVE_FIXED)
            with mock.patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=2):
                self.assertEqual(library_tools.drive_kind(r"E:\DCIM\a.jpg"), library_tools.DRIVE_REMOVABLE)
            with mock.patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4):
                self.assertEqual(library_tools.drive_kind(r"Z:\a.jpg"), library_tools.DRIVE_NETWORK)

    def test_plan_buckets(self):
        local = self.lib.file("a.jpg")
        usb = self.lib.file("usb.jpg")
        kept = self.lib.file("kept.jpg")
        self.lib.media(local, trashed=1)
        self.lib.media(usb, trashed=1)
        self.lib.media(os.path.join(self.lib.tmp, "gone.jpg"), trashed=1)
        self.lib.media(kept, trashed=0)
        conn = database.get_connection()
        try:
            with self._kinds({usb}):
                plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        self.assertEqual(library_tools.plan_summary(plan), {"recycle": 1, "removable": 1, "missing": 1})
        self.assertEqual(plan["recycle"][0][1], [local])

    def test_live_photo_video_goes_too(self):
        still = self.lib.file("IMG_1.HEIC")
        motion = self.lib.file("IMG_1.MOV")
        self.lib.media(still, trashed=1, paired=motion)
        conn = database.get_connection()
        try:
            plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        recycled = []
        library_tools.empty_trash(plan, recycle=lambda p: recycled.append(p) or True)
        self.assertEqual(sorted(recycled), sorted([still, motion]))

    def test_empty_recycles_local_skips_removable_by_default(self):
        local = self.lib.file("a.jpg")
        usb = self.lib.file("usb.jpg")
        mid_local = self.lib.media(local, trashed=1)
        mid_usb = self.lib.media(usb, trashed=1)
        conn = database.get_connection()
        try:
            conn.execute("INSERT INTO people (id, name) VALUES (1, 'Anna')")
            conn.execute("INSERT INTO face_instances (media_id, person_id, embedding) VALUES (?, 1, ?)",
                         (mid_local, b"\0" * 8))
            conn.commit()
            with self._kinds({usb}):
                plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        recycled = []
        stats = library_tools.empty_trash(plan, recycle=lambda p: recycled.append(p) or True)
        self.assertEqual(recycled, [local], "do Koše Windows jen soubor z pevného disku")
        self.assertEqual((stats["recycled"], stats["skipped"], stats["deleted"]), (1, 1, 0))
        self.assertTrue(os.path.exists(usb), "vyjímatelný disk bez výslovného souhlasu nedotčen")
        ids = {r[0] for r in self.lib.q("SELECT id FROM media_items")}
        self.assertEqual(ids, {mid_usb}, "přeskočená položka zůstává v koši aplikace")
        self.assertEqual(self.lib.q("SELECT COUNT(*) FROM face_instances")[0][0], 0, "kaskáda obličejů")
        self.assertEqual(self.lib.q("SELECT COUNT(*) FROM people")[0][0], 1, "osoby se nemažou")

    def test_permanent_delete_only_when_explicit(self):
        usb = self.lib.file("usb.jpg")
        self.lib.media(usb, trashed=1)
        conn = database.get_connection()
        try:
            with self._kinds({usb}):
                plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        with self._kinds({usb}):
            stats = library_tools.empty_trash(plan, delete_removable_permanently=True, recycle=lambda p: True)
        self.assertEqual(stats["deleted"], 1)
        self.assertFalse(os.path.exists(usb))
        self.assertEqual(self.lib.q("SELECT COUNT(*) FROM media_items")[0][0], 0)

    def test_failed_recycle_keeps_item_in_trash(self):
        local = self.lib.file("a.jpg")
        self.lib.media(local, trashed=1)
        conn = database.get_connection()
        try:
            plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        stats = library_tools.empty_trash(plan, recycle=lambda p: False)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(self.lib.q("SELECT is_trashed FROM media_items")[0][0], 1)
        self.assertTrue(os.path.exists(local))

    def test_trash_and_restore_update_map_index_incrementally(self):
        """Koš / obnovení mění R*Tree jen u svých položek – výsledek stejný jako plná přestavba."""
        conn = database.get_connection()
        try:
            ids = []
            for i in range(5):
                cur = conn.execute("INSERT INTO media_items (file_path, file_name, media_type, latitude, longitude) "
                                   "VALUES (?, ?, 'photo', ?, ?)", (f"C:/geo/{i}.jpg", f"{i}.jpg", 49.0 + i, 13.0 + i))
                ids.append(cur.lastrowid)
            conn.commit()
        finally:
            conn.close()
        database.sync_geo_index()
        geo = lambda: sorted(r[0] for r in self.lib.q("SELECT id FROM media_geo_index"))
        self.assertEqual(geo(), sorted(ids))
        with mock.patch.object(database, "sync_geo_index", side_effect=AssertionError("plná přestavba")):
            database.trash_media(ids[:2])
            self.assertEqual(geo(), sorted(ids[2:]), "v koši = mimo mapu")
            database.restore_media([ids[0]])
            self.assertEqual(geo(), sorted([ids[0]] + ids[2:]))
        expected = lambda: sorted(r[0] for r in self.lib.q(
            f"SELECT m.id FROM media_items m WHERE m.latitude IS NOT NULL AND m.longitude IS NOT NULL "
            f"AND {database.visible_media_sql('m')}"))
        self.assertEqual(geo(), expected(), "stejné jako plná přestavba (definice indexu)")
        # nezaindexovaná položka (vložená mimo sken) a přesunutá fotka (jiné GPS)
        conn = database.get_connection()
        try:
            new_id = conn.execute("INSERT INTO media_items (file_path, file_name, media_type, latitude, longitude) "
                                  "VALUES ('C:/geo/new.jpg', 'new.jpg', 'photo', 50.1, 14.4)").lastrowid
            conn.execute("UPDATE media_items SET latitude = 48.5, longitude = 16.0 WHERE id = ?", (ids[3],))
            conn.commit()
        finally:
            conn.close()
        database.trash_media([ids[4]])
        self.assertEqual(geo(), expected())
        self.assertIn(new_id, geo(), "koš srovná celý index, ne jen svou položku")
        moved = self.lib.q("SELECT min_lat, min_lon FROM media_geo_index WHERE id = ?", (ids[3],))[0]
        self.assertAlmostEqual(moved[0], 48.5, places=4)
        self.assertAlmostEqual(moved[1], 16.0, places=4)

    def test_missing_files_are_forgotten(self):
        self.lib.media(os.path.join(self.lib.tmp, "gone.jpg"), trashed=1)
        conn = database.get_connection()
        try:
            plan = library_tools.plan_empty_trash(conn)
        finally:
            conn.close()
        stats = library_tools.empty_trash(plan, recycle=lambda p: self.fail("nic k přesunu"))
        self.assertEqual(stats["forgotten"], 1)
        self.assertEqual(self.lib.q("SELECT COUNT(*) FROM media_items")[0][0], 0)


# ═════════════════════════════════════════════════════════════════════════════
# Chytrý import + tovární nastavení
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSmartImportAndReset(unittest.TestCase):

    def setUp(self):
        self.lib = TempLibrary()
        self.addCleanup(self.lib.close)

    def test_find_new_media(self):
        known = self.lib.file("lib/known.jpg")
        new = self.lib.file("lib/sub/new.png")
        still = self.lib.file("lib/IMG_2.HEIC")
        self.lib.file("lib/IMG_2.MOV")                 # pohybová část Live Photo
        clip = self.lib.file("lib/clip.mp4")
        self.lib.file("lib/notes.txt")
        self.lib.media(known)
        conn = database.get_connection()
        try:
            keys = library_tools.known_media_keys(conn)
        finally:
            conn.close()
        found = library_tools.find_new_media([os.path.join(self.lib.tmp, "lib"), "Z:\\neexistuje"], keys)
        self.assertEqual(sorted(found), sorted([new, still, clip]))

    def test_probe_emits_count_and_folders(self):
        folder = os.path.join(self.lib.tmp, "lib")
        self.lib.file("lib/a.jpg")
        self.lib.file("lib/b.jpg")
        empty = os.path.join(self.lib.tmp, "empty")
        os.makedirs(empty)
        probe = library_tools.NewMediaProbe([folder, empty])
        got = []
        probe.found.connect(lambda n, roots: got.append((n, roots)))
        probe.start()
        probe.wait(5000)
        _app.processEvents()
        self.assertEqual(got, [(2, [folder])])

    def test_new_media_toast_offers_background_import(self):
        import main
        win = mock.MagicMock()
        with mock.patch.object(main, "show_toast") as toast:
            main.MainWindow._on_new_media_found(win, 12, ["D:\\Fotky"])
        args, kwargs = toast.call_args
        self.assertEqual(args[1], "Nalezeno 12 nových fotek")
        self.assertEqual(kwargs["action_text"], "Importovat na pozadí")
        self.assertEqual(kwargs["dismiss_text"], "Přeskočit")
        self.assertIsNone(kwargs["timeout_ms"], "rozhodnutí nezmizí samo")
        kwargs["on_action"]()
        win._import_in_background.assert_called_once_with(["D:\\Fotky"])
        with mock.patch.object(main, "show_toast") as toast:
            main.MainWindow._on_new_media_found(win, 0, [])
        toast.assert_not_called()

    def test_smart_import_respects_setting(self):
        import main
        win = mock.MagicMock()
        win.isVisible.return_value = True
        win._import_probe = None
        win._scanner = None
        win.config = {"library_folders": [self.lib.tmp], "auto_scan_on_startup": False}
        with mock.patch.object(library_tools, "NewMediaProbe") as probe:
            main.MainWindow._smart_import_check(win)
            probe.assert_not_called()
            win.config["auto_scan_on_startup"] = True
            main.MainWindow._smart_import_check(win)
            probe.assert_called_once()

    def test_toast_dismiss_text(self):
        t = ui_toast.Toast("Nalezeno 3 nové fotky", action_text="Importovat na pozadí", on_action=lambda: None,
                           dismiss_text="Přeskočit")
        self.assertEqual(t.btn_close.text(), "Přeskočit")
        plain = ui_toast.Toast("x")
        self.assertEqual(plain.btn_close.text(), "✕")

    def test_factory_reset_only_config(self):
        db_before = os.path.getsize(database.DB_PATH)
        cfg.save_config(dict(cfg.DEFAULT_CONFIG, library_folders=["D:\\Fotky"], theme="light",
                             face_similarity_threshold=0.7, preview_cache_mb=4096))
        self.assertEqual(library_tools.changed_settings(cfg.load_config()),
                         ["face_similarity_threshold", "library_folders", "preview_cache_mb", "theme"])
        with mock.patch("cache_tools.clear_thumbnail_cache") as thumbs, \
                mock.patch("preview_cache.clear_cache") as previews:
            fresh = library_tools.factory_reset(keep_folders=True)
            thumbs.assert_not_called()
            previews.assert_not_called()
        self.assertEqual(fresh["library_folders"], ["D:\\Fotky"])
        self.assertEqual(fresh["theme"], "dark")
        with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["face_similarity_threshold"], 0.62)
        self.assertTrue(os.path.exists(database.DB_PATH))
        self.assertEqual(os.path.getsize(database.DB_PATH), db_before, "databáze nedotčena")
        with mock.patch("cache_tools.clear_thumbnail_cache") as thumbs, \
                mock.patch("preview_cache.clear_cache") as previews:
            fresh = library_tools.factory_reset(keep_folders=False, clear_thumbnails=True, clear_previews=True)
            thumbs.assert_called_once()
            previews.assert_called_once()
        self.assertEqual(fresh["library_folders"], [])


# ═════════════════════════════════════════════════════════════════════════════
# Okno předvoleb
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSettingsDialog(unittest.TestCase):

    def setUp(self):
        self.lib = TempLibrary()
        self.addCleanup(self.lib.close)

    def dialog(self):
        dlg = SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_category_pages(self):
        dlg = self.dialog()
        names = [t for _b, t in dlg._nav]
        self.assertEqual(names[:7], ["Knihovna", "Vzhled", "Hudba", "Rozpoznávání", "Úložiště a mezipaměť",
                                     "Mapa offline", "Soukromí"])
        self.assertEqual(names[-1], "O aplikaci")
        self.assertEqual(dlg.slider_threshold.minimum(), 30)
        self.assertEqual(dlg.slider_lru.maximum(), 4000)

    def test_save_theme_and_preview_budget(self):
        dlg = self.dialog()
        dlg.theme_switch.set_current("light")          # Kairos: segmentový přepínač motivu
        dlg.slider_preview_mb.setValue(3000)
        dlg.save_and_close()
        with open(cfg.CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["theme"], "light")
        self.assertEqual(saved["preview_cache_mb"], 3072, "zaokrouhleno na 256 MB")
        self.assertFalse(dlg.recluster_requested)

    def test_threshold_change_shows_impact_and_requests_recluster(self):
        conn = database.get_connection()
        conn.execute("INSERT INTO people (name, is_confirmed) VALUES ('A', 0), ('B', 0), ('Anna', 1)")
        conn.commit()
        conn.close()
        dlg = self.dialog()
        self.assertEqual(dlg.lbl_threshold_impact.text(), "")
        dlg.slider_threshold.setValue(dlg.slider_threshold.value() + 8)
        text = dlg.lbl_threshold_impact.text()
        self.assertIn("2 nepotvrzené osoby", text)
        self.assertIn("potvrzené zůstanou beze změny (1)", text)
        self.assertIn("rozdělí", text)
        dlg.save_and_close()
        self.assertTrue(dlg.recluster_requested)

    def test_trash_label_and_empty_flow(self):
        local = self.lib.file("a.jpg")
        self.lib.media(local, trashed=1)
        dlg = self.dialog()
        self.assertIn("1 položka", dlg.lbl_trash.text())
        self.assertTrue(dlg.btn_empty_trash.isEnabled())
        with mock.patch.object(dlg, "_confirm", return_value=(False, {})):
            dlg.empty_trash()
        self.assertFalse(dlg.trash_emptied, "zrušeno = nic se nestalo")
        self.assertTrue(os.path.exists(local))
        with mock.patch.object(dlg, "_confirm", return_value=(True, {})) as confirm, \
                mock.patch.object(library_tools, "send_to_recycle_bin", return_value=True) as recycle:
            dlg.empty_trash()
        self.assertIn("Koše Windows", " ".join(confirm.call_args[0][2]))
        recycle.assert_called_once_with(local)
        self.assertTrue(dlg.trash_emptied)
        self.assertEqual(self.lib.q("SELECT COUNT(*) FROM media_items")[0][0], 0)
        self.assertFalse(dlg.btn_empty_trash.isEnabled())

    def test_removable_needs_second_confirmation(self):
        usb = self.lib.file("usb.jpg")
        self.lib.media(usb, trashed=1)
        with mock.patch.object(library_tools, "drive_kind", return_value=library_tools.DRIVE_REMOVABLE):
            dlg = self.dialog()
            answers = [(True, {"permanent": True}), (False, {})]     # druhé potvrzení odmítnuto
            with mock.patch.object(dlg, "_confirm", side_effect=answers) as confirm:
                dlg.empty_trash()
        self.assertEqual(confirm.call_count, 2)
        first_options = confirm.call_args_list[0][0][3]
        self.assertEqual(first_options[0][0], "permanent")
        self.assertFalse(first_options[0][2], "trvalé smazání není předvolené")
        self.assertTrue(os.path.exists(usb))
        self.assertEqual(self.lib.q("SELECT is_trashed FROM media_items")[0][0], 1)

    def test_factory_reset_flow(self):
        cfg.save_config(dict(cfg.DEFAULT_CONFIG, library_folders=["D:\\Fotky"], theme="light"))
        dlg = self.dialog()
        self.assertIn("Změněno 2 nastavení", dlg.lbl_factory.text())
        with mock.patch.object(dlg, "_confirm", return_value=(True, {"keep_folders": True,
                                                                    "clear_thumbnails": False,
                                                                    "clear_previews": False})):
            dlg.factory_reset()
        self.assertTrue(dlg.factory_reset_done)
        saved = cfg.load_config()
        self.assertEqual(saved["theme"], "dark")
        self.assertEqual(saved["library_folders"], ["D:\\Fotky"])

    def test_reset_form_has_no_confirmation(self):
        dlg = self.dialog()
        dlg.theme_switch.set_current("light")
        with mock.patch.object(dlg, "_confirm") as confirm:
            dlg.reset_to_defaults()
        confirm.assert_not_called()
        self.assertEqual(dlg.selected_theme(), "dark")

    def test_clear_previews_inline(self):
        import preview_cache
        dlg = self.dialog()
        with mock.patch.object(preview_cache, "clear_cache") as clear, \
                mock.patch.object(dlg, "_confirm") as confirm:
            dlg.clear_previews()
        clear.assert_called_once()
        confirm.assert_not_called()
        self.assertTrue(dlg.previews_cleared)

    def test_confirm_sheet_defaults_to_cancel(self):
        sheet = ConfirmSheet("Vysypat koš?", "text", ["dopad"], [("permanent", "trvale", False)], "Vysypat koš")
        self.addCleanup(sheet.deleteLater)
        self.assertEqual(sheet.values(), {"permanent": False})
        cancel = [b for b in sheet.findChildren(QPushButton) if b.text() == "Zrušit"][0]
        self.assertTrue(cancel.isDefault(), "Enter = bezpečná volba")
        self.assertEqual(sheet.btn_action.text(), "Vysypat koš")


class TestPackaging(unittest.TestCase):
    """Release: čerstvý PyInstaller build musí najít nástroje a nesmí nést osobní předvolby."""

    HERE = os.path.dirname(os.path.abspath(__file__))

    def test_tool_found_next_to_exe_then_in_internal(self):
        import paths
        tmp = tempfile.mkdtemp(prefix="omni_pkg_")
        self.addCleanup(shutil.rmtree, tmp, True)
        exe_dir, internal = os.path.join(tmp, "app"), os.path.join(tmp, "app", "_internal")
        os.makedirs(internal)
        with mock.patch.object(paths, "BASE_DIR", exe_dir), mock.patch.object(paths, "RESOURCE_DIR", internal):
            self.assertEqual(paths._tool_path("ffmpeg.exe"), os.path.join(exe_dir, "ffmpeg.exe"),
                             "nikde -> očekávané místo vedle .exe (chybová hláška dává smysl)")
            open(os.path.join(internal, "ffmpeg.exe"), "wb").close()
            self.assertEqual(paths._tool_path("ffmpeg.exe"), os.path.join(internal, "ffmpeg.exe"),
                             "čerstvý build: nástroj z _internal bez ručního kopírování")
            open(os.path.join(exe_dir, "ffmpeg.exe"), "wb").close()
            self.assertEqual(paths._tool_path("ffmpeg.exe"), os.path.join(exe_dir, "ffmpeg.exe"),
                             "verze vedle .exe má přednost (uživatel ji může vyměnit)")

    def test_exiftool_needs_its_runtime_and_own_folder(self):
        import paths
        tmp = tempfile.mkdtemp(prefix="omni_pkg_")
        self.addCleanup(shutil.rmtree, tmp, True)
        exe_dir, internal = os.path.join(tmp, "app"), os.path.join(tmp, "app", "_internal")
        tool_dir = os.path.join(internal, "tools", "exiftool")
        os.makedirs(os.path.join(tool_dir, "exiftool_files"))
        open(os.path.join(tool_dir, "exiftool.exe"), "wb").close()
        open(os.path.join(exe_dir, "exiftool.exe"), "wb").close()     # holá kopie (starý ruční build)
        kw = dict(subdir=os.path.join("tools", "exiftool"), needs="exiftool_files")
        with mock.patch.object(paths, "BASE_DIR", exe_dir), mock.patch.object(paths, "RESOURCE_DIR", internal):
            self.assertEqual(paths._tool_path("exiftool.exe", **kw), os.path.join(tool_dir, "exiftool.exe"),
                             "kopie bez exiftool_files se přeskočí (tiše by selhala)")
            os.makedirs(os.path.join(exe_dir, "exiftool_files"))
            self.assertEqual(paths._tool_path("exiftool.exe", **kw), os.path.join(exe_dir, "exiftool.exe"),
                             "úplná kopie vedle .exe má přednost")

    def test_spec_bundles_exiftool_runtime_and_no_personal_config(self):
        with open(os.path.join(self.HERE, "app.spec"), encoding="utf-8") as f:
            spec = f.read()
        self.assertIn("EXIFTOOL_DIR = os.path.join('tools', 'exiftool')", spec, "exiftool ve vlastní složce")
        self.assertIn("datas.append(('exiftool.exe', EXIFTOOL_DIR))", spec)
        self.assertIn("datas.append(('exiftool_files', os.path.join(EXIFTOOL_DIR, 'exiftool_files')))", spec,
                      "exiftool 13.x bez exiftool_files nespustí")
        self.assertNotIn("'config.json',", spec, "osobní config.json se nepřibaluje")
        for needle in ("'ffmpeg.exe'", "vendor', 'maplibre'", "video_output.qml", "map_vault.html"):
            self.assertIn(needle, spec)

    def test_exiftool_runs_from_project(self):
        import paths
        if not os.path.exists(paths.EXIFTOOL_PATH):
            self.skipTest("exiftool není k dispozici")
        out = subprocess.run([paths.EXIFTOOL_PATH, "-ver"], capture_output=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(out.returncode, 0, out.stderr[:200])
        self.assertRegex(out.stdout.decode().strip(), r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
