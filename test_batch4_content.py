"""Kairos dávka 4 – Graphite Night, mřížka (zoom, výběr), Roky/Měsíce/Dny, karty, Info panel, Předvolby."""
import sys
import unittest
from unittest import mock

try:
    from PyQt6.QtCore import Qt, QRect, QPoint, QPointF
    from PyQt6.QtGui import QColor, QImage, QPainter, QWheelEvent
    from PyQt6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    from PyQt6.QtTest import QTest
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

if HAS_QT:
    import motion
    import theme
    import tokens
    import moments_view
    import viewer
    from ui_controls import AccentPicker, smooth_rect_path
    from test_patch6 import WindowEnv
    import main
else:                      # pragma: no cover
    WindowEnv = unittest.TestCase


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestGraphiteAndHelpers(unittest.TestCase):

    def test_graphite_palette(self):
        self.assertEqual(theme.color("bg", "dark"), "#0E0E10")
        self.assertEqual(theme.color("surface", "dark"), "#1A1A1E")
        self.assertEqual(set(theme.TOKENS["dark"]), set(theme.TOKENS["light"]))

    def test_legacy_greys_become_graphite_only_in_dark(self):
        qss = "QWidget { background: #1C1C1E; color: #8E8E93; border: 1px solid #3A3A3C; }"
        self.assertEqual(tokens.retint(qss, "dark"),
                         "QWidget { background: #0E0E10; color: #A1A1AA; border: 1px solid #2A2A30; }")
        light = theme.remap_qss(qss, "light")
        self.assertEqual(tokens.retint(light, "light"), light, "světlý režim beze změny")

    def test_smooth_rect_path_stays_inside(self):
        from PyQt6.QtCore import QRectF
        r = QRectF(0, 0, 100, 60)
        path = smooth_rect_path(r, 12)
        self.assertTrue(r.adjusted(-0.01, -0.01, 0.01, 0.01).contains(path.boundingRect()))
        self.assertFalse(path.contains(QPointF(0.5, 0.5)), "roh je zaoblený")
        self.assertTrue(path.contains(QPointF(50, 30)))

    def test_moments_grouping_and_czech_dates(self):
        recs = [(1, "a", "", "photo", None, None, "2025-06-14 10:00:00", 0),
                (2, "b", "", "photo", None, None, "2025-06-01 09:00:00", 0),
                (3, "c", "", "photo", None, None, "2024-12-24 18:00:00", 0),
                (4, "d", "", "photo", None, None, None, 0)]
        years = moments_view.group_records(recs, "years")
        self.assertEqual([(g["title"], g["count"]) for g in years], [("2025", 2), ("2024", 1), ("Bez data", 1)])
        months = moments_view.group_records(recs, "months")
        self.assertEqual([g["title"] for g in months], ["Červen 2025", "Prosinec 2024", "Bez data"])
        self.assertEqual(months[0]["subtitle"], "2 položky")
        self.assertEqual(moments_view.czech_day("2025-06-14 10:00"), "Sobota 14. června 2025")
        self.assertEqual(moments_view.czech_day(None), "")

    def test_info_panel_formatting(self):
        self.assertEqual(viewer._exposure(0.004), "1/250 s")
        self.assertEqual(viewer._exposure("1/60"), "1/60 s")
        self.assertEqual(viewer._exposure(2), "2 s")
        self.assertEqual(viewer._fnum(1.8), "ƒ/1.8")
        self.assertEqual(viewer._iso_date("2025:06:14 10:22:01"), "2025-06-14")
        self.assertEqual(viewer._clock("2025-06-14 10:22:01"), "10:22")
        self.assertEqual(viewer._fnum(None), "")

    def test_accent_picker(self):
        picker = AccentPicker("ember")
        self.assertEqual(picker.value(), ("ember", ""))
        got = []
        picker.changed.connect(lambda k, h: got.append((k, h)))
        picker.swatches["moss"].click()
        self.assertEqual(got, [("moss", "")])
        picker.set_value("custom", "#E86AA6")
        self.assertEqual(picker.value(), ("custom", "#E86AA6"))
        picker.grab()
        picker.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestDelegateSelection(unittest.TestCase):

    def test_selected_tile_has_accent_ring(self):
        model = main.ApplePhotoModel(10)
        model.set_records([(1, "x.jpg", "", "photo", None, None, "2025-01-01", 0)])
        delegate = main.ApplePhotoDelegate()
        img = QImage(180, 180, QImage.Format.Format_ARGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 180, 180)
        opt.state |= QStyle.StateFlag.State_Selected
        delegate.paint(p, opt, model.index(0, 0))
        p.end()
        accent = QColor(tokens.palette()["accent"])
        near = sum(1 for x in range(0, 180, 2) for y in range(0, 180, 2)
                   if abs(QColor(img.pixel(x, y)).red() - accent.red()) < 20
                   and abs(QColor(img.pixel(x, y)).blue() - accent.blue()) < 20)
        self.assertGreater(near, 40)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestGridAndTimeViews(WindowEnv):
    N_PHOTOS = 12

    def setUp(self):
        super().setUp()
        self._saved = (motion._user_reduce, motion._system_cache)
        motion._user_reduce, motion._system_cache = True, True        # okamžitě, bez čekání na animace
        self.addCleanup(lambda: setattr(motion, "_user_reduce", self._saved[0]))

    def _ctrl_wheel(self, delta):
        g = self.w.grid_view
        pos = QPointF(g.viewport().rect().center())
        ev = QWheelEvent(pos, g.viewport().mapToGlobal(pos), QPoint(0, 0), QPoint(0, delta),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.ControlModifier,
                         Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(g.viewport(), ev)          # kolečko dostává viewport (jako ve skutečnosti)

    def test_toolbar_has_no_zoom_slider(self):
        self.assertFalse(hasattr(self.w, "zoom_slider"))

    def test_ctrl_wheel_zooms_by_columns_and_remembers(self):
        g = self.w.grid_view
        _app.processEvents()
        before = g.columns()
        with mock.patch.object(main, "save_config") as save:
            self._ctrl_wheel(120)
        self.assertEqual(g.columns(), max(main.GRID_MIN_COLUMNS, before - 1), "přiblížení = o sloupec méně")
        save.assert_called()
        self.assertEqual(self.w.config["default_thumbnail_size"], g.cell_size)
        with mock.patch.object(main, "save_config"):
            self._ctrl_wheel(-120)
        self.assertEqual(g.columns(), before)

    def test_rows_fill_the_width(self):
        g = self.w.grid_view
        _app.processEvents()
        cols = g.columns()
        self.assertLessEqual(cols * g.cell_size, g.viewport().width())
        self.assertGreater((cols + 1) * g.cell_size, g.viewport().width() - 2, "bez prázdného pruhu vpravo")

    def test_years_months_days_all(self):
        w = self.w
        w.time_switch.set_current("years")
        w._set_time_view("years")
        self.assertIs(w.photo_views.currentWidget(), w.moments_view)
        self.assertTrue(w.moments_view.keys())
        year = w.moments_view.keys()[0]
        w._on_moment_clicked("years", year)
        self.assertEqual(w._time_view, "months")
        self.assertEqual(w.time_switch.current_key(), "months")
        month = w.moments_view.keys()[0]
        w._on_moment_clicked("months", month)
        self.assertEqual(w._time_view, "all")
        self.assertIs(w.photo_views.currentWidget(), w.grid_view)
        w.time_switch.set_current("days")
        w._set_time_view("days")
        _app.processEvents()
        self.assertEqual(w.grid_view.columns(), 4)
        w._set_time_view("all")
        self.assertNotEqual(w._time_view, "days")

    def test_hover_progress_moves_between_tiles(self):
        g = self.w.grid_view
        g._set_hover_row(0)
        self.assertEqual(g.hover_progress(0), 1.0)
        g._set_hover_row(1)
        self.assertEqual(g.hover_progress(1), 1.0)
        self.assertEqual(g.hover_progress(0), 0.0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSettingsRedesign(unittest.TestCase):

    def make(self, cfg=None):
        import settings_dialog
        stats = {"unconfirmed": None, "confirmed": None, "trash_plan": None}
        with mock.patch.object(settings_dialog.SettingsDialog, "_read_library_stats", return_value=stats), \
                mock.patch.object(settings_dialog, "load_config", return_value=dict(cfg or {})):
            return settings_dialog.SettingsDialog()

    def test_grouped_rows_and_controls(self):
        from PyQt6.QtWidgets import QFrame
        dlg = self.make()
        groups = [f for f in dlg.findChildren(QFrame) if f.objectName() == "group"]
        self.assertGreaterEqual(len(groups), 6)
        self.assertFalse(hasattr(dlg, "slider_default_size"), "velikost dlaždic jen zoomem v mřížce")
        self.assertFalse(hasattr(dlg, "combo_theme"))
        dlg.deleteLater()

    def test_accent_and_theme_are_saved(self):
        import settings_dialog
        dlg = self.make({"accent": "iris"})
        dlg.theme_switch.set_current("light")
        dlg.accent_picker.swatches["ember"].click()
        with mock.patch.object(settings_dialog, "save_config") as save:
            dlg.save_and_close()
        saved = save.call_args[0][0]
        self.assertEqual((saved["theme"], saved["accent"], saved["accent_custom"]), ("light", "ember", ""))
        dlg.deleteLater()

    def test_reset_restores_default_accent(self):
        dlg = self.make({"accent": "moss"})
        self.assertEqual(dlg.accent_picker.value()[0], "moss")
        dlg.reset_to_defaults()
        self.assertEqual(dlg.accent_picker.value()[0], tokens.DEFAULT_ACCENT)
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
