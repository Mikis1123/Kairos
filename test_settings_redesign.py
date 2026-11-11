"""Settings redesign: category rail + search, card menus for dangerous actions, auto-height lists,
Cancel/Save-only footer and WCAG AA text contrast (>= 4.5 : 1) in both themes."""
import sys
import unittest
from unittest import mock

try:
    from PyQt6.QtGui import QAction
    from PyQt6.QtWidgets import QApplication, QFrame, QListWidgetItem, QPushButton
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

import tokens

AA = 4.5
# (foreground token, background tokens it is drawn on inside the Settings dialog)
SETTINGS_TEXT = (
    ("text", ("bg", "bg_sunken", "surface", "surface_2")),
    ("text_secondary", ("bg", "bg_sunken", "surface", "surface_2")),
    ("warning_text", ("bg", "bg_sunken", "surface")),
    ("danger_text", ("bg", "bg_sunken", "surface")),
    ("accent", ("bg", "bg_sunken", "surface")),
)


class TestSettingsContrast(unittest.TestCase):

    def test_text_tokens_meet_aa_on_every_settings_surface(self):
        for mode in ("dark", "light"):
            p = tokens.palette(mode)
            for fg, bgs in SETTINGS_TEXT:
                for bg in bgs:
                    ratio = tokens.contrast(p[fg], p[bg])
                    self.assertGreaterEqual(ratio, AA, f"{mode}: {fg} {p[fg]} on {bg} {p[bg]} = {ratio:.2f}")

    def test_filled_buttons_meet_aa(self):
        for mode in ("dark", "light"):
            p = tokens.palette(mode)
            self.assertGreaterEqual(tokens.contrast(p["fill"], p["on_accent"]), AA, f"{mode} primary")
            self.assertGreaterEqual(tokens.contrast(p["danger_fill"], "#FFFFFF"), AA, f"{mode} danger")

    def test_settings_qss_has_no_tertiary_text_on_labels(self):
        """Informative labels use text / text_secondary; text_tertiary is only for disabled states."""
        for mode in ("dark", "light"):
            qss = tokens.build_qss("settings", mode)
            tertiary = tokens.palette(mode)["text_tertiary"]
            for line in qss.splitlines():
                if tertiary in line and "color:" in line:
                    self.assertIn("disabled", line, f"{mode}: {line.strip()}")


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSettingsLayout(unittest.TestCase):

    def make(self, cfg=None):
        import settings_dialog
        stats = {"unconfirmed": None, "confirmed": None, "trash_plan": None}
        with mock.patch.object(settings_dialog.SettingsDialog, "_read_library_stats", return_value=stats), \
                mock.patch.object(settings_dialog, "load_config", return_value=dict(cfg or {})):
            dlg = settings_dialog.SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_footer_has_only_cancel_and_save(self):
        from ui_controls import KButton
        dlg = self.make()
        names = {b.objectName() for b in dlg.findChildren(QPushButton)}
        self.assertNotIn("btnReset", names, "reset buttons live in card menus")
        self.assertNotIn("btnCleanCache", names, "destructive cache buttons live in card menus")
        footer = [b for b in dlg.findChildren(KButton) if b.text() in ("Zrušit", "Uložit změny")]
        self.assertEqual(sorted(b.text() for b in footer), ["Uložit změny", "Zrušit"])

    def test_dangerous_actions_are_card_menu_items(self):
        dlg = self.make()
        for attr in ("btn_empty_trash", "btn_factory_reset", "btn_clear_previews", "btn_clear_proxies",
                     "act_delete_thumbs", "btn_map_clear", "btn_map_remove", "act_reset_form"):
            self.assertIsInstance(getattr(dlg, attr), QAction, attr)
        menus = [b for b in dlg.findChildren(QPushButton) if b.objectName() == "cardMenu"]
        self.assertGreaterEqual(len(menus), 4)
        dlg.chk_reduce_motion.setChecked(True)
        dlg.act_reset_form.trigger()
        self.assertFalse(dlg.chk_reduce_motion.isChecked())

    def test_cards_and_rail(self):
        dlg = self.make()
        groups = [f for f in dlg.findChildren(QFrame) if f.objectName() == "group"]
        self.assertGreaterEqual(len(groups), 12)
        self.assertEqual(len(dlg._nav), dlg.pages.count())
        self.assertEqual(dlg.rail.width(), 214)

    def test_search_filters_and_jumps_to_first_page_with_hits(self):
        dlg = self.make()
        dlg.show()
        dlg.pages.setCurrentIndex(0)
        dlg.search.setText("mapa offline")
        map_idx = dlg.PAGE_KEYS.index("map")
        self.assertEqual(dlg.pages.currentIndex(), map_idx)
        self.assertIn("Mapa offline", dlg._nav[map_idx][0].text())
        self.assertTrue(dlg._nav[map_idx][0].text().rstrip()[-1].isdigit(), "match count on the rail")
        self.assertFalse(dlg.lbl_search_empty.isVisible())
        dlg.search.setText("")
        self.assertEqual([b.text() for b, _t in dlg._nav], [t for _b, t in dlg._nav], "titles restored")
        dlg.close()

    def test_search_ignores_diacritics_and_reports_no_hits(self):
        dlg = self.make()
        dlg.show()
        dlg.search.setText("ulozi")                       # "Úložiště a mezipaměť"
        self.assertEqual(dlg.pages.currentIndex(), dlg.PAGE_KEYS.index("storage"))
        dlg.search.setText("zzzqqq")
        self.assertTrue(dlg.lbl_search_empty.isVisible())
        self.assertIn("zzzqqq", dlg.lbl_search_empty.text())
        self.assertFalse(dlg.pages.isVisible())
        dlg.search.setText("")
        self.assertFalse(dlg.lbl_search_empty.isVisible())
        self.assertTrue(dlg.pages.isVisible())
        dlg.close()

    def test_search_never_reveals_rows_hidden_by_page_logic(self):
        dlg = self.make()
        dlg.show()
        dlg._set_row_visible(dlg._sync_forget_row, False)       # no paired phone
        dlg.search.setText("iphone")
        dlg.search.setText("")
        self.assertTrue(dlg._sync_forget_row.isHidden())
        dlg.close()

    def test_escape_clears_search_before_closing(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        dlg = self.make()
        dlg.show()
        dlg.search.setText("hudba")
        QTest.keyClick(dlg.search, Qt.Key.Key_Escape)
        self.assertEqual(dlg.search.text(), "")
        self.assertTrue(dlg.isVisible())
        dlg.close()

    def test_lists_grow_with_their_items(self):
        import settings_dialog
        lst = settings_dialog._AutoList(max_rows=4, empty_text="nothing")
        lst.show()
        empty_h = lst.height()
        lst.addItem(QListWidgetItem("a"))
        one = lst.height()
        for i in range(10):
            lst.addItem(QListWidgetItem(str(i)))
        capped = lst.height()
        self.assertGreaterEqual(one, empty_h - 10)
        self.assertGreater(capped, one)
        lst.clear()
        self.assertEqual(lst.height(), empty_h)
        lst.close()

    def test_add_folder_action_is_visible_and_adds(self):
        dlg = self.make()
        add = [b for b in dlg.findChildren(QPushButton) if b.objectName() == "btnAddFolder"]
        self.assertEqual(len(add), 1)
        self.assertEqual(add[0].text(), "+ Přidat složku")
        with mock.patch("settings_dialog.QFileDialog.getExistingDirectory", return_value="C:/tmp/photos-x"):
            add[0].click()
        self.assertEqual(dlg.list_folders.item(dlg.list_folders.count() - 1).text(), "C:/tmp/photos-x")


if __name__ == "__main__":
    unittest.main()
