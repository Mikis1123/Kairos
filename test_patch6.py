"""Regresní testy pro Patch 6 – Fáze 0 (úklid, pojistky) a Fáze 1 (navigace v okně).

Spuštění (ve složce projektu):   python -m pytest test_patch6.py -v

Pokrývá:
  Fáze 0 – osiřelé testovací řádky (černé karty) se odstraní, chybějící soubory s absolutní
           cestou zůstávají; pojistka proti testům na skutečné photos.db; náhradní karty
           v mřížce (načítání / soubor nedostupný / náhled nedostupný); mapa bez menu Chromia.
  Fáze 1 – NavigationController (push/pop, obnova posunu a fokusu), prohlížeč uvnitř okna
           (žádné nové okno, návrat na stejné místo, Esc, přehrávání se zastaví), osoba /
           událost / duplicity / hraniční obličeje jako podstránky, toasty místo QMessageBoxů.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import inspect
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QRect, QModelIndex
    from PyQt6.QtGui import QImage, QPainter, QColor, QKeyEvent, QPixmap
    from PyQt6.QtCore import QEvent
    from PyQt6.QtWidgets import (QApplication, QWidget, QStackedWidget, QScrollArea,
                                 QVBoxLayout, QStyleOptionViewItem, QLabel)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    import main
    import ui_nav
    import ui_toast


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def press(widget, key, modifiers=Qt.KeyboardModifier.NoModifier):
    ev = QKeyEvent(QEvent.Type.KeyPress, key, modifiers)
    QApplication.sendEvent(widget, ev)
    _app.processEvents()


class TempEnv(unittest.TestCase):
    """Izolované prostředí: vlastní DB a média v dočasné složce."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p6_test_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._saved
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

    def _jpg(self, name, color=(200, 80, 40), size=(48, 36)):
        path = os.path.join(self.tmp, name)
        Image.new("RGB", size, color).save(path, "JPEG")
        return path

    def _add(self, path, media_type="photo", date="2026-07-07 10:00:00", name=None, **cols):
        fields = {"file_path": path, "file_name": name if name is not None else os.path.basename(path),
                  "media_type": media_type, "date_taken": date, **cols}
        keys = ", ".join(fields)
        marks = ", ".join("?" * len(fields))
        return self.x(f"INSERT INTO media_items ({keys}) VALUES ({marks})", tuple(fields.values()))

    def _library(self, n=3):
        return [self._add(self._jpg(f"IMG_{i:04d}.jpg", (i * 4 % 255, 90, 160)),
                          date=f"2026-07-{1 + i % 28:02d} 10:{i % 60:02d}:00") for i in range(n)]


# ═════════════════════════════════════════════════════════════════════════════
# FÁZE 0
# ═════════════════════════════════════════════════════════════════════════════

class TestOrphanPurge(TempEnv):
    """Černé karty = řádky z dřívějšího běhu testů s relativní cestou bez souboru."""

    def test_relative_missing_rows_are_removed_with_dependents(self):
        keep = self._add(self._jpg("ok.jpg"))
        orphan = self._add("photo_a.jpg", name=None)
        self.x("UPDATE media_items SET file_name = NULL WHERE id = ?", (orphan,))
        self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, ?)", (orphan, b"\x00" * 16))
        self.x("INSERT INTO image_embeddings (media_id, embedding) VALUES (?, ?)", (orphan, b"\x00" * 16))
        database.dismiss_duplicate_pair(keep, orphan)

        self.assertEqual(database.purge_orphan_test_rows(), 1)
        self.assertEqual([r[0] for r in self.q("SELECT id FROM media_items")], [keep])
        self.assertEqual(self.q("SELECT COUNT(*) FROM face_instances")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM image_embeddings")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM duplicate_dismissed")[0][0], 0)
        self.assertEqual(database.purge_orphan_test_rows(), 0, "podruhé už není co mazat")

    def test_missing_absolute_path_is_kept(self):
        """Odpojený externí disk: soubor chybí, ale záznam (a obličeje, oblíbené) se nesmí ztratit."""
        offline = self._add(os.path.join(self.tmp, "E_drive", "IMG_9999.jpg"))
        self.assertEqual(database.purge_orphan_test_rows(), 0)
        self.assertEqual(self.q("SELECT id FROM media_items")[0][0], offline)

    def test_startup_runs_purge(self):
        src = inspect.getsource(main.MainWindow._init_db_and_load)
        self.assertIn("purge_orphan_test_rows()", src)


class TestRealDbGuard(unittest.TestCase):
    """Pojistka: test, který zapomene přesměrovat DB_PATH, spadne místo zápisu do knihovny."""

    def test_guard_is_active_under_pytest(self):
        if "pytest" not in sys.modules:
            self.skipTest("pojistku zapíná conftest.py (pytest)")
        self.assertEqual(os.environ.get(database.TEST_GUARD_ENV), "1")

    def test_real_db_connection_is_refused(self):
        saved = database.DB_PATH
        try:
            with mock.patch.dict(os.environ, {database.TEST_GUARD_ENV: "1"}):
                database.DB_PATH = database.REAL_DB_PATH
                with mock.patch("sqlite3.connect") as connect:
                    with self.assertRaises(database.RealDatabaseAccessError):
                        database.get_connection()
                    connect.assert_not_called()      # skutečný soubor se ani neotevře
        finally:
            database.DB_PATH = saved

    def test_temp_db_is_allowed(self):
        tmp = tempfile.mkdtemp(prefix="omni_p6_guard_")
        saved = database.DB_PATH
        try:
            with mock.patch.dict(os.environ, {database.TEST_GUARD_ENV: "1"}):
                database.DB_PATH = os.path.join(tmp, "photos.db")
                database.get_connection().close()
        finally:
            database.DB_PATH = saved
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestThumbnailPlaceholders(TempEnv):
    """Mřížka: místo černých karet stav načítání / chyby s ikonou a názvem."""

    def _model_with(self, records):
        model = main.ApplePhotoModel(64)
        model.set_records(records)
        return model

    def _load(self, model, row=0):
        idx = model.index(row, 0)
        model.data(idx, main.ApplePhotoModel.ThumbRole)
        model.thread_pool.waitForDone(5000)
        for _ in range(20):
            _app.processEvents()
        return idx

    def test_states(self):
        ok = self._add(self._jpg("ok.jpg"))
        corrupt_path = os.path.join(self.tmp, "broken.jpg")
        with open(corrupt_path, "wb") as f:
            f.write(b"not a jpeg")
        corrupt = self._add(corrupt_path)
        missing = self._add(os.path.join(self.tmp, "gone.jpg"))
        model = self._model_with(main.fetch_media_records(order_by="m.id"))
        self.assertEqual(model.data(model.index(0, 0), main.ApplePhotoModel.StateRole), main.THUMB_LOADING)
        for row in range(3):
            self._load(model, row)
        states = {int(r[0]): model.data(model.index(i, 0), main.ApplePhotoModel.StateRole)
                  for i, r in enumerate(model.records)}
        self.assertEqual(states, {ok: main.THUMB_OK, corrupt: main.THUMB_UNAVAILABLE,
                                  missing: main.THUMB_MISSING_FILE})
        self.assertIsInstance(model.data(model.index(0, 0), main.ApplePhotoModel.ThumbRole), QPixmap)

    def test_failed_thumbnail_is_not_retried_on_every_paint(self):
        self._add(os.path.join(self.tmp, "gone.jpg"))
        model = self._model_with(main.fetch_media_records())
        idx = self._load(model)
        with mock.patch.object(model.thread_pool, "start") as start:
            for _ in range(5):
                self.assertIsNone(model.data(idx, main.ApplePhotoModel.ThumbRole))
            start.assert_not_called()

    def test_worker_emits_qimage_not_qpixmap(self):
        """QPixmap mimo GUI vlákno není bezpečný – worker posílá QImage."""
        got = []
        w = main.ThumbnailWorker(1, self._jpg("a.jpg"), None)
        w.signals.loaded.connect(lambda i, img: got.append(img))
        w.run()
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], QImage)

    def test_meta_name_falls_back_to_file_name(self):
        mid = self._add(os.path.join(self.tmp, "gone.jpg"))
        self.x("UPDATE media_items SET file_name = NULL WHERE id = ?", (mid,))
        model = self._model_with(main.fetch_media_records())
        self.assertEqual(model.data(model.index(0, 0), main.ApplePhotoModel.MetaRole)["name"], "gone.jpg")

    def test_missing_card_is_not_a_black_void(self):
        self._add(os.path.join(self.tmp, "gone.jpg"))
        model = self._model_with(main.fetch_media_records())
        idx = self._load(model)
        delegate = main.ApplePhotoDelegate()
        img = QImage(180, 180, QImage.Format.Format_ARGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 180, 180)
        delegate.paint(p, opt, idx)
        p.end()
        self.assertEqual(QColor(img.pixel(20, 160)).name(), main.theme.color("surface").lower(), "šedá karta, ne černá")
        colors = {QColor(img.pixel(x, y)).name() for x in range(40, 140, 3) for y in range(40, 140, 3)}
        self.assertTrue({"#8e8e93", "#ff453a"} & colors or len(colors) > 3, "ikona / text je vykreslen")


# ═════════════════════════════════════════════════════════════════════════════
# FÁZE 1 – stavební kameny
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestToasts(unittest.TestCase):

    def setUp(self):
        self.host = QWidget()
        self.host.resize(800, 600)
        self.host.show()
        self.host.toasts = ui_toast.ToastManager(self.host)
        self.addCleanup(self.host.deleteLater)

    def test_info_toast_auto_dismisses(self):
        t = ui_toast.show_toast(self.host, "Hotovo", "success", timeout_ms=60)
        self.assertTrue(t.isVisible())
        self.assertFalse(t.isWindow(), "toast je překryv v okně, ne nové okno")
        wait(500)
        self.assertEqual(self.host.toasts.toasts, [])

    def test_default_timeout_is_three_seconds_and_errors_stay(self):
        info = self.host.toasts.show("Informace")
        err = self.host.toasts.show("Chyba zápisu", "error")
        self.assertEqual(info.timeout_ms, 3000)
        self.assertIsNone(err.timeout_ms, "chyba nesmí zmizet sama")
        self.assertFalse(err.timer.isActive())

    def test_action_runs_callback_once(self):
        calls = []
        t = self.host.toasts.show("Přesunuto do koše", action_text="Vrátit", on_action=lambda: calls.append(1))
        t.btn_action.click()
        t.btn_action.click() if t.btn_action is not None else None
        wait(400)
        self.assertEqual(calls, [1])

    def test_same_message_is_not_stacked_and_max_three(self):
        a = self.host.toasts.show("Stejné")
        self.assertIs(self.host.toasts.show("Stejné"), a)
        for i in range(5):
            self.host.toasts.show(f"Zpráva {i}")
        self.assertLessEqual(len(self.host.toasts.toasts), ui_toast.TOAST_MAX_VISIBLE)

    def test_toasts_stack_bottom_center(self):
        a = self.host.toasts.show("První")
        b = self.host.toasts.show("Druhý")
        self.assertLess(a.y(), b.y(), "novější je níž, starší se posouvá nahoru")
        self.assertLessEqual(b.geometry().bottom(), self.host.height() - ui_toast.TOAST_MARGIN + 1)
        self.assertAlmostEqual(b.geometry().center().x(), self.host.width() // 2, delta=2)

    def test_without_manager_only_prints(self):
        lonely = QWidget()
        self.addCleanup(lonely.deleteLater)
        self.assertIsNone(ui_toast.show_toast(lonely, "nic"))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestNavigationController(unittest.TestCase):

    def setUp(self):
        self.stack = QStackedWidget()
        self.stack.resize(600, 400)
        self.root = QScrollArea()
        content = QWidget()
        content.setMinimumHeight(3000)
        self.root.setWidget(content)
        self.stack.addWidget(self.root)
        self.stack.show()
        _app.processEvents()
        self.nav = ui_nav.NavigationController(self.stack)
        self.addCleanup(self.stack.deleteLater)

    def test_push_pop_restores_page_and_scroll(self):
        self.root.verticalScrollBar().setValue(1234)
        page = ui_nav.DetailPage("Detail")
        self.nav.push(page)
        self.assertIs(self.stack.currentWidget(), page)
        self.root.verticalScrollBar().setValue(0)        # např. reset modelu na pozadí
        self.nav.pop()
        self.assertIs(self.stack.currentWidget(), self.root)
        self.assertEqual(self.root.verticalScrollBar().value(), 1234)
        self.assertEqual(self.nav.depth(), 0)

    def test_dispose_and_reuse(self):
        temp, keep = ui_nav.DetailPage("A"), QWidget()
        self.nav.push(temp)
        self.nav.push(keep, dispose=False)
        self.nav.pop()
        self.assertGreaterEqual(self.stack.indexOf(keep), 0, "znovupoužitelná stránka zůstává")
        self.nav.pop()
        self.assertEqual(self.stack.indexOf(temp), -1, "dočasná stránka je pryč")

    def test_on_pop_and_reset(self):
        calls = []
        self.nav.push(ui_nav.DetailPage("A"), on_pop=lambda: calls.append("A"))
        self.nav.push(ui_nav.DetailPage("B"), on_pop=lambda: calls.append("B"))
        self.nav.reset()
        self.assertEqual(calls, ["B", "A"])
        self.assertIs(self.stack.currentWidget(), self.root)

    def test_back_button_and_escape(self):
        page = ui_nav.DetailPage("A")
        self.nav.push(page)
        page.btn_back.click()
        self.assertEqual(self.nav.depth(), 0)
        page2 = ui_nav.DetailPage("B")
        self.nav.push(page2)
        press(page2, Qt.Key.Key_Escape)
        self.assertEqual(self.nav.depth(), 0)

    def test_pushing_page_already_in_stack_returns_to_it(self):
        a, b = QWidget(), ui_nav.DetailPage("B")
        self.nav.push(a, dispose=False)
        self.nav.push(b)
        self.nav.push(a)
        self.assertEqual(self.nav.depth(), 1)
        self.assertIs(self.stack.currentWidget(), a)

    def test_date_range_format(self):
        self.assertEqual(main._format_date_range("2024-07-01 10:00", "2024-07-03 18:00"),
                         "1. 7. 2024 – 3. 7. 2024")
        self.assertEqual(main._format_date_range("2024-07-01", "2024-07-01 23:00"), "1. 7. 2024")
        self.assertEqual(main._format_date_range(None, None), "")


# ═════════════════════════════════════════════════════════════════════════════
# FÁZE 1 – hlavní okno
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class WindowEnv(TempEnv):
    N_PHOTOS = 3

    def setUp(self):
        super().setUp()
        self.ids = self._library(self.N_PHOTOS)
        patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = main.MainWindow()
        self.w.resize(1280, 820)
        self.w.show()
        _app.processEvents()
        self.addCleanup(self._close_window)

    def _close_window(self):
        self.w.close()
        self.w.deleteLater()
        _app.processEvents()

    def _visible_windows(self):
        return {w for w in QApplication.topLevelWidgets() if w.isVisible()}

    def toast_texts(self):
        return [t.text for t in self.w.toasts.toasts]


class TestInAppViewer(WindowEnv):
    N_PHOTOS = 60

    def test_photo_opens_inside_main_window(self):
        before = self._visible_windows()
        self.w._on_photo_activated(0)
        _app.processEvents()
        viewer = self.w._viewer
        self.assertIs(self.w.stack.currentWidget(), viewer)
        self.assertFalse(viewer.isWindow())
        self.assertEqual(self._visible_windows(), before, "žádné nové okno")
        self.assertEqual(viewer.lbl_title.text(), self.w._filtered_records[0][1])

    def test_back_returns_to_same_scroll_position_and_marks_last_photo(self):
        grid = self.w.grid_view
        bar = grid.verticalScrollBar()
        self.assertGreater(bar.maximum(), 300, "mřížka musí být posuvná")
        bar.setValue(300)
        row = grid.indexAt(grid.viewport().rect().center()).row()
        self.w._on_photo_activated(row)
        self.w._viewer.show_next()
        self.w._viewer.btn_back.click()
        wait(50)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_photos)
        self.assertEqual(bar.value(), 300, "stejné místo v mřížce")
        self.assertEqual(grid.currentIndex().row(), row + 1, "označena naposledy prohlížená fotka")

    def test_viewer_is_reused(self):
        self.w._on_photo_activated(0)
        first = self.w._viewer
        press(first, Qt.Key.Key_Escape)
        self.w._on_photo_activated(1)
        self.assertIs(self.w._viewer, first)
        self.assertEqual(self.w.nav.depth(), 1)

    def test_escape_closes_info_panel_first(self):
        self.w._on_photo_activated(0)
        viewer = self.w._viewer
        press(viewer, Qt.Key.Key_I)
        self.assertTrue(viewer.info_panel.isVisible())
        press(viewer, Qt.Key.Key_Escape)
        self.assertFalse(viewer.info_panel.isVisible())
        self.assertIs(self.w.stack.currentWidget(), viewer)
        press(viewer, Qt.Key.Key_Escape)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_photos)

    def test_alt_left_goes_back(self):
        self.w._on_photo_activated(0)
        self.w._nav_back()
        self.assertEqual(self.w.nav.depth(), 0)

    def test_playback_stops_when_leaving(self):
        self.w._on_photo_activated(0)
        with mock.patch.object(self.w._viewer, "cleanup_playback") as cleanup:
            self.w._switch_page(1)           # postranní panel během prohlížení
        cleanup.assert_called()
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_people)
        self.assertTrue(self.w._nav_buttons[1].isChecked())

    def test_map_pin_returns_to_map(self):
        self.w._switch_page(3)
        self.w.open_viewer_by_id(self.ids[5])
        self.assertIs(self.w.stack.currentWidget(), self.w._viewer)
        self.w._viewer.request_close()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_map)

    def test_favorite_in_viewer_updates_grid(self):
        self.w._on_photo_activated(0)
        media_id = int(self.w._filtered_records[0][0])
        self.w._viewer.handle_toggle_favorite()
        meta = self.w.grid_view.photo_model.data(self.w.grid_view.photo_model.index(0, 0),
                                                 main.ApplePhotoModel.MetaRole)
        self.assertTrue(meta["is_fav"])
        self.assertEqual(self.q("SELECT is_favorite FROM media_items WHERE id=?", (media_id,))[0][0], 1)

    def test_file_info_opens_viewer_info_panel(self):
        self.w._show_file_info(2)
        self.assertIs(self.w.stack.currentWidget(), self.w._viewer)
        self.assertTrue(self.w._viewer.info_panel.isVisible())


class TestSubPages(WindowEnv):

    def _person(self, name="Jana"):
        pid = self.x("INSERT INTO people (name) VALUES (?)", (name,))
        for mid in self.ids[:2]:
            self.x("INSERT INTO face_instances (media_id, person_id, embedding) VALUES (?, ?, ?)",
                   (mid, pid, b"\x00" * 16))
        return pid

    def test_person_photos_page(self):
        pid = self._person()
        self.w._switch_page(1)
        before = self._visible_windows()
        self.w._on_person_card_clicked(pid, "Jana")
        page = self.w.stack.currentWidget()
        self.assertIsInstance(page, ui_nav.DetailPage)
        self.assertEqual(self._visible_windows(), before)
        self.assertEqual(page.lbl_title.text(), "Jana")
        self.assertEqual(page.lbl_subtitle.text(), "2 fotky")
        grid = page.findChild(main.AppleGridView)
        self.assertEqual(sorted(int(r[0]) for r in grid.photo_model.records), sorted(self.ids[:2]))
        # fotka z osoby -> prohlížeč -> zpět na osobu -> Esc zpět na Osoby
        grid.item_activated.emit(1)
        self.assertIs(self.w.stack.currentWidget(), self.w._viewer)
        self.w._viewer.request_close()
        self.assertIs(self.w.stack.currentWidget(), page)
        press(grid, Qt.Key.Key_Escape)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_people)

    def test_escape_in_grid_first_clears_selection(self):
        pid = self._person()
        self.w._on_person_card_clicked(pid, "Jana")
        page = self.w.stack.currentWidget()
        grid = page.findChild(main.AppleGridView)
        grid.selectAll()
        press(grid, Qt.Key.Key_Escape)
        self.assertIs(self.w.stack.currentWidget(), page, "první Esc jen zruší výběr")
        self.assertFalse(grid.selectionModel().hasSelection())

    def test_rename_updates_page_title(self):
        pid = self._person()
        self.w._on_person_card_clicked(pid, "Jana")
        page = self.w.stack.currentWidget()
        with mock.patch.object(main.InWindowDialog, "getText", return_value=("Jana Nová", True)):
            self.w._rename_person(pid, "Jana", page)
        self.assertEqual(page.lbl_title.text(), "Jana Nová")
        self.assertEqual(self.q("SELECT name FROM people WHERE id=?", (pid,))[0][0], "Jana Nová")

    def test_event_page(self):
        ev = self.x("INSERT INTO events (title, start_date, end_date) VALUES (?, ?, ?)",
                    ("Dovolená", "2026-07-01 08:00:00", "2026-07-03 20:00:00"))
        self.x("UPDATE media_items SET event_id = ? WHERE id IN (?, ?)", (ev, self.ids[0], self.ids[1]))
        self.w._switch_page(2)
        self.w.event_cards[0].clicked.emit(ev)      # Patch 10: Bento karta místo položky seznamu
        page = self.w.stack.currentWidget()
        self.assertIsInstance(page, ui_nav.DetailPage)
        self.assertEqual(page.lbl_title.text(), "Dovolená")
        self.assertEqual(page.lbl_subtitle.text(), "1. 7. 2026 – 3. 7. 2026 · 2 položky")
        page.btn_back.click()
        self.assertIs(self.w.stack.currentWidget(), self.w.page_events)

    def test_duplicate_review_is_in_window_page(self):
        a, b = self.ids[0], self.ids[1]
        before = self._visible_windows()
        view = self.w._push_duplicate_review([(a, "", b, "", 1)])
        self.assertIsInstance(view, main.DuplicateReviewView)
        self.assertFalse(view.isWindow())
        self.assertEqual(self._visible_windows(), before)
        self.assertIs(self.w.stack.currentWidget(), view.parentWidget())
        view._action("keep_a")                  # poslední pár -> průvodce skončí -> návrat
        _app.processEvents()
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertNotIn(b, {int(r[0]) for r in self.w._all_records}, "knihovna obnovena")
        self.assertIn("Přesunuto do koše: 1 položka", self.toast_texts())
        toast = self.w.toasts.toasts[-1]
        toast.btn_action.click()                # „Zobrazit koš“
        wait(300)
        self.assertEqual(self.w._current_filter, "trash")
        self.assertEqual([int(r[0]) for r in self.w._filtered_records], [b])

    def test_duplicate_review_without_pairs_is_a_toast(self):
        with mock.patch.object(main, "refresh_phashes", lambda **kw: 0), \
                mock.patch("ai_engine.get_duplicate_pairs", lambda conn: []):
            self.w._open_duplicate_review()
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertIn("Žádné duplicitní fotografie nenalezeny", self.toast_texts())

    def test_duplicate_shortcuts(self):
        ids = self.ids
        view = self.w._push_duplicate_review([(ids[0], "", ids[1], "", 1), (ids[0], "", ids[2], "", 2)])
        press(view, Qt.Key.Key_Right)
        self.assertEqual(view.current_idx, 1)
        press(view, Qt.Key.Key_Left)
        self.assertEqual(view.current_idx, 0)
        view._action("keep_both")
        press(view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.q("SELECT COUNT(*) FROM duplicate_dismissed")[0][0], 0)
        press(view, Qt.Key.Key_Escape)
        self.assertEqual(self.w.nav.depth(), 0)

    def test_borderline_review_page(self):
        pid = self.x("INSERT INTO people (name) VALUES ('Petr')")
        fid = self.x("INSERT INTO face_instances (media_id, embedding) VALUES (?, ?)", (self.ids[0], b"\x00" * 16))
        with mock.patch.object(main, "assign_face_to_person") as assign:
            view = self.w._push_borderline_review(pid, "Petr", [{"face_id": fid, "similarity": 0.4, "crop_path": ""}])
            self.assertIsInstance(view, main.BorderlineReviewView)
            self.assertFalse(view.isWindow())
            view._assign_yes()
        assign.assert_called_once_with(fid, pid)
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertIn("Petr: potvrzeno obličejů – 1", self.toast_texts())

    def test_borderline_without_named_people_is_a_toast(self):
        self.w._open_borderline_review()
        self.assertEqual(self.w.nav.depth(), 0)
        self.assertTrue(any("pojmenujte" in t for t in self.toast_texts()))

    def test_recluster_reports_real_stats(self):
        stats = {"anchors": 2, "new_people": 11, "faces": 486, "threshold": 0.62}
        with mock.patch.object(main, "strict_recluster_faces", return_value=stats):
            self.w._recluster_faces()
        self.assertIn("Přeshlukování dokončeno – 13 osob, 486 obličejů", self.toast_texts())

    def test_trash_from_grid_offers_undo(self):
        target = int(self.w._filtered_records[0][0])
        self.w._set_trashed_for_rows([0], True)
        self.assertIn(target, {int(r[0]) for r in self.w._trash_records})
        toast = self.w.toasts.toasts[-1]
        self.assertEqual(toast.btn_action.text(), "Vrátit")
        toast.btn_action.click()
        self.assertNotIn(target, {int(r[0]) for r in self.w._trash_records})

    def test_map_has_no_chromium_context_menu(self):
        self.assertEqual(self.w.map_view.contextMenuPolicy(), Qt.ContextMenuPolicy.NoContextMenu)

    def test_no_blocking_message_boxes_left(self):
        """Jediný modální QMessageBox: nefunkční databáze při startu (aplikace nemůže pokračovat)."""
        src = inspect.getsource(main)
        calls = [line.strip() for line in src.splitlines() if "QMessageBox." in line]
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("QMessageBox.critical", calls[0])
        self.assertNotIn("dlg.exec()", inspect.getsource(main.MainWindow._open_duplicate_review))


if __name__ == "__main__":
    unittest.main(verbosity=2)
