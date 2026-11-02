"""Kairos dávka 3 – kostra okna: ikony, postranní panel s kolekcemi, prázdné stavy, listy v okně,
toasty z tokenů a Mica (window_effects)."""
import sys
import time
import unittest
from unittest import mock

try:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QApplication, QWidget, QLineEdit, QComboBox
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    from PyQt6.QtTest import QTest
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

if HAS_QT:
    import icons
    import motion
    import tokens
    import ui_toast
    import window_effects
    from ui_controls import EmptyState
    from ui_sheets import InWindowDialog, Sheet
    from test_patch6 import WindowEnv
    import main
else:                      # pragma: no cover
    WindowEnv = unittest.TestCase


def _opaque_pixels(pm):
    img = pm.toImage()
    return sum(1 for x in range(img.width()) for y in range(img.height()) if QColor.fromRgba(img.pixel(x, y)).alpha() > 0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestIcons(unittest.TestCase):

    def test_every_icon_renders(self):
        for name in icons.names():
            self.assertGreater(_opaque_pixels(icons.mask(name, 24, 1.0)), 20, name)

    def test_tint_and_cache(self):
        pm = icons.pixmap("heart_fill", "#FF0000", 24, 1.0)
        img = pm.toImage()
        colors = {QColor.fromRgba(img.pixel(x, y)).name() for x in range(24) for y in range(24)
                  if QColor.fromRgba(img.pixel(x, y)).alpha() == 255}
        self.assertEqual(colors, {"#ff0000"})
        self.assertIs(icons.pixmap("heart_fill", "#FF0000", 24, 1.0), pm)
        self.assertFalse(icons.qicon("search", "#FFFFFF").isNull())

    def test_unknown_icon_is_blank_not_crash(self):
        self.assertEqual(_opaque_pixels(icons.mask("neexistuje", 16, 1.0)), 0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestEmptyStateAndSheets(unittest.TestCase):

    def setUp(self):
        self._saved = (motion._user_reduce, motion._system_cache)
        motion._user_reduce, motion._system_cache = False, True

    def tearDown(self):
        motion._user_reduce, motion._system_cache = self._saved

    def test_empty_state_content_action_and_entrance(self):
        host = QWidget()
        es = EmptyState("trash", "Koš je prázdný", "Text", parent=host)
        self.assertFalse(es.btn_action.isVisibleTo(es))
        es.set_content("search", "Nic", "Zkuste jiné slovo", "Vymazat hledání")
        self.assertEqual(es.title(), "Nic")
        self.assertTrue(es.btn_action.isVisibleTo(es))
        hits = []
        es.action_triggered.connect(lambda: hits.append(1))
        host.resize(500, 400)
        host.show()
        self.assertTrue(es._enter.is_running(), "jednorázový příjezd při zobrazení")
        QTest.qWait(tokens.MOTION["emphasized"][0] + 150)
        self.assertFalse(es._enter.is_running(), "v klidu nic neběží")
        self.assertIsNone(es.graphicsEffect(), "po doběhnutí bez efektu")
        es.btn_action.click()
        self.assertEqual(hits, [1])
        host.deleteLater()

    def _answer(self, fn, delay=250):
        QTimer.singleShot(delay, fn)

    def test_get_text_sheet_returns_value(self):
        win = QWidget()
        win.resize(800, 600)
        win.show()

        def type_and_accept():
            sheet = win.findChild(Sheet)
            self.assertIsNotNone(sheet)
            self.assertFalse(sheet.isWindow(), "list v okně, ne nové okno")
            edit = sheet.findChild(QLineEdit)
            edit.setText("Jana Nová")
            QTest.keyClick(edit, Qt.Key.Key_Return)
        self._answer(type_and_accept)
        value, ok = InWindowDialog.getText(win, "Přejmenovat osobu", "Nové jméno:", text="Jana")
        self.assertEqual((value, ok), ("Jana Nová", True))
        win.deleteLater()

    def test_get_item_and_escape_cancels(self):
        win = QWidget()
        win.resize(800, 600)
        win.show()

        def pick():
            sheet = win.findChild(Sheet)
            sheet.findChild(QComboBox).setCurrentIndex(1)
            sheet.btn_action.click()
        self._answer(pick)
        self.assertEqual(InWindowDialog.getItem(win, "Sloučit", "Které jméno?", ["Jana", "Janička"]),
                         ("Janička", True))

        self._answer(lambda: QTest.keyClick(win.findChild(Sheet), Qt.Key.Key_Escape))
        self.assertFalse(InWindowDialog.confirm(win, "Vysypat koš?", "Text", "Vysypat koš"))
        win.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestToastAndTitleBar(unittest.TestCase):

    def test_toast_uses_vector_glyph_and_tokens(self):
        t = ui_toast.Toast("Hotovo", "success")
        glyph = t.findChild(ui_toast.QLabel, "ToastGlyph")
        self.assertFalse(glyph.pixmap().isNull())
        self.assertEqual(glyph.text(), "")
        self.assertNotIn("#0A84FF", t.styleSheet())
        t.deleteLater()

    def test_sidebar_style_from_tokens(self):
        for mode in ("dark", "light"):
            qss = tokens.build_qss("main", mode)
            self.assertIn(f"QWidget#sidebar {{ background-color: {tokens.palette(mode)['sidebar']}", qss)

    def test_title_bar_colors_on_windows_11_only(self):
        self.assertEqual(window_effects._colorref("#1C1C1E"), 0x1E1C1C)
        w = QWidget()
        calls = []
        with mock.patch.object(window_effects, "_set_attr", side_effect=lambda h, a, v: calls.append(a) or True), \
                mock.patch.object(window_effects.sys, "platform", "win32"):
            with mock.patch.object(window_effects, "windows_build", return_value=19045):
                window_effects.style_title_bar(w, True, "#1C1C1E", "#F5F5F7")
            self.assertEqual(calls, [window_effects.DWMWA_USE_IMMERSIVE_DARK_MODE], "Windows 10: jen tmavý režim")
            calls.clear()
            with mock.patch.object(window_effects, "windows_build", return_value=26200):
                window_effects.style_title_bar(w, True, "#1C1C1E", "#F5F5F7")
            self.assertEqual(calls, [20, window_effects.DWMWA_CAPTION_COLOR, window_effects.DWMWA_TEXT_COLOR])
        w.deleteLater()

    def test_tinted_icon_fills_its_box_on_hidpi(self):
        pm = icons.pixmap("photos", "#FFFFFF", 18, 2.0)
        img = pm.toImage()
        xs = [x for x in range(img.width()) for y in range(img.height())
              if QColor.fromRgba(img.pixel(x, y)).alpha() > 0]
        self.assertGreater(max(xs) - min(xs), img.width() * 0.75, "ikona nesmí být zmenšená na polovinu")


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestShellWindow(WindowEnv):

    def test_sidebar_sections_and_collections(self):
        nav = self.w.sidebar_nav
        self.assertEqual([it.key for it in nav.items],
                         ["photos", "people", "events", "map", "favorites", "videos", "albums", "trash", "hidden", "reels",
                          "echoes"])   # Sprint 2: Alba; Pro vás (Atlas a Konstelace odstraněny)
        self.assertTrue(all(it.icon_name for it in nav.items), "každá položka má vektorovou ikonu")
        nav.item("trash").click()
        self.assertEqual(self.w._current_filter, "trash")
        self.assertEqual(self.w.stack.currentIndex(), 0)
        self.assertEqual(self.w.lbl_photos_title.text(), "Koš")          # Gate 1: Smazané -> Koš
        nav.item("people").click()
        self.assertTrue(self.w._nav_buttons[1].isChecked())
        nav.item("photos").click()
        self.assertEqual(self.w._current_filter, "all", "Fotografie = celá knihovna")
        self.assertTrue(nav.item("photos").isChecked())

    def test_echoes_opens_as_subpage_and_back_restores_sidebar(self):
        nav = self.w.sidebar_nav
        nav.item("photos").click()
        nav.item("echoes").click()
        page = self.w.nav.current()
        self.assertEqual(page.property("sidebar_key"), "echoes")
        self.assertTrue(nav.item("echoes").isChecked())
        self.assertEqual(self.w.stack.indexOf(self.w.page_photos), 0, "indexy stránek stacku beze změny")
        for _ in range(100):                     # hledání běží na pozadí (EchoJob)
            _app.processEvents()
            if self.w._echoes_panel.echoes is not None and not self.w._echoes_panel._job:
                break
            time.sleep(0.02)
        self.w.nav.pop()
        self.assertTrue(nav.item("photos").isChecked(), "Zpět vrátí jezdec na kořenovou stránku")

    def test_programmatic_page_switch_keeps_collection_highlighted(self):
        self.w._open_collection("favorites")
        self.w._switch_page(1)
        self.w._switch_page(0)
        self.assertTrue(self.w.sidebar_nav.item("favorites").isChecked())

    def test_empty_states_follow_filter_and_search(self):
        es = self.w.photos_empty
        self.w._open_collection("trash")
        _app.processEvents()
        self.assertTrue(es.isVisible())
        self.assertEqual(es.title(), "Koš je prázdný")
        self.w._set_filter("all")
        self.assertFalse(es.isVisible(), "knihovna má fotky")
        self.w._search_query = "zcela-neexistující"
        self.w._on_search_results(main.search_engine.SearchResult("zcela-neexistující", [], [], [], True),
                                  "zcela-neexistující")
        self.assertTrue(es.isVisible())
        self.assertIn("zcela-neexistující", es.title())
        with mock.patch.object(self.w, "_clear_search") as clear:
            es.btn_action.click()
        clear.assert_called_once()

    def test_toolbar_has_no_emoji(self):
        texts = [self.w.btn_recluster.text(), self.w.btn_borderline.text(), self.w.btn_duplicates.text(),
                 self.w.btn_slideshow.text(), self.w.search_bar.placeholderText(), self.w.people_search.placeholderText()]
        self.assertFalse([t for t in texts if any(ord(ch) > 0x2600 for ch in t)], texts)
        self.assertEqual(self.w.btn_recluster.iconName(), "refresh")

    def test_events_empty_state_widget(self):
        self.w._render_event_cards([])
        self.w._switch_page(2)
        self.assertIsInstance(self.w.lbl_events_empty, EmptyState)
        self.assertTrue(self.w.lbl_events_empty.isVisible())


if __name__ == "__main__":
    unittest.main()
