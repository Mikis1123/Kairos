"""Kairos dávka 1 – tokeny, zvýraznění, generované styly a jejich napojení na theme.py."""
import os
import sys
import unittest

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QPushButton
    # jako ostatní testy: QtWebEngine (mapa, minimapa) vyžaduje sdílené GL kontexty před vznikem aplikace
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False

if HAS_QT:
    _app = QApplication.instance() or QApplication(sys.argv)
    import theme
    import tokens
    import config


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestAccentContrast(unittest.TestCase):
    SAMPLES = ["#E86AA6", "#FFD60A", "#30D158", "#64D2FF", "#BF5AF2", "#222222", "#FFFFFF", "#000000"]

    def tearDown(self):
        tokens.set_accent(tokens.DEFAULT_ACCENT)

    def assert_aa(self, label):
        for mode in ("dark", "light"):
            a = tokens.accent(mode)
            for bg in tokens.CONTRAST_BACKGROUNDS[mode]:
                self.assertGreaterEqual(tokens.contrast(a["accent"], bg), 4.5, f"{label} {mode} text na {bg}")
            self.assertGreaterEqual(tokens.contrast(a["fill"], a["on_accent"]), 4.5, f"{label} {mode} tlačítko")

    def test_presets_pass_aa_in_both_modes(self):
        for key in ACCENT_KEYS_WITHOUT_LEGACY():
            tokens.set_accent(key)
            self.assert_aa(key)

    def test_any_custom_color_is_tuned_to_aa(self):
        for hx in self.SAMPLES:
            self.assertEqual(tokens.set_accent("custom", hx), "custom")
            self.assert_aa(hx)

    def test_gray_custom_stays_gray(self):
        tokens.set_accent("custom", "#808080")
        r, g, b = tokens.hex_to_rgb(tokens.accent("dark")["accent"])
        self.assertLess(max(r, g, b) - min(r, g, b), 12)

    def test_unknown_or_invalid_falls_back_to_default(self):
        self.assertEqual(tokens.set_accent("nesmysl"), tokens.DEFAULT_ACCENT)
        self.assertEqual(tokens.set_accent("custom", "modrá"), tokens.DEFAULT_ACCENT)
        self.assertEqual(tokens.configure({}), tokens.DEFAULT_ACCENT)
        self.assertEqual(tokens.configure({"accent": "ember"}), "ember")

    def test_oklch_round_trip(self):
        for hx in ("#5457D6", "#C2491B", "#3F7D22", "#808080"):
            self.assertEqual(tokens.oklch_to_hex(*tokens.hex_to_oklch(hx)), hx)


def ACCENT_KEYS_WITHOUT_LEGACY():
    return [k for k in tokens.ACCENTS if k != tokens.LEGACY_ACCENT]


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestRecolor(unittest.TestCase):
    LEGACY = ("QPushButton#x { background-color: #0A84FF; color: #FFFFFF; border: 1px solid #0A84FF; } "
              "QPushButton#x:hover { background-color: #409CFF; } QLabel { color: #0A84FF; }")

    def tearDown(self):
        tokens.set_accent(tokens.DEFAULT_ACCENT)
        theme.apply("dark")

    def test_classic_is_identity(self):
        tokens.set_accent("classic")
        self.assertEqual(tokens.recolor_accent(self.LEGACY, "dark"), self.LEGACY, "modrá zůstává")
        self.assertEqual(tokens.retint("QLabel { color: #0A84FF; background: #2C2C2E; }", "dark"),
                         "QLabel { color: #0A84FF; background: #1A1A1E; }", "šedé -> Graphite i u klasické")

    def test_dark_recolor_by_property(self):
        tokens.set_accent("iris")
        a = tokens.accent("dark")
        out = tokens.recolor_accent(self.LEGACY, "dark")
        self.assertIn(f"background-color: {a['fill']}; color: #FFFFFF; border: 1px solid {a['accent']}", out)
        self.assertIn(f"background-color: {a['fill_hover']}", out)
        self.assertIn(f"QLabel {{ color: {a['accent']}", out)
        self.assertNotIn("#0A84FF", out)

    def test_light_recolor_runs_after_remap_and_keeps_white_text(self):
        tokens.set_accent("ember")
        a = tokens.accent("light")
        out = tokens.recolor_accent(theme.remap_qss(self.LEGACY, "light"), "light")
        self.assertIn(f"background-color: {a['fill']}; color: #FFFFFF", out)
        self.assertIn(f"background-color: {a['fill_hover']}", out)
        self.assertIn(f"QLabel {{ color: {a['accent']}", out)

    def test_legacy_widget_follows_accent_and_restores(self):
        tokens.set_accent("moss")
        root = QWidget()
        btn = QPushButton("x", root)
        btn.setStyleSheet(self.LEGACY)
        theme.apply("dark", root)
        self.assertIn(tokens.accent("dark")["fill"], btn.styleSheet())
        theme.apply("light", root)
        self.assertIn(tokens.accent("light")["fill"], btn.styleSheet())
        tokens.set_accent("classic")
        theme.apply("dark", root)
        self.assertEqual(btn.styleSheet(), self.LEGACY, "klasická modrá = původní styl")
        root.deleteLater()

    def test_fixed_dark_widgets_get_dark_accent_only(self):
        tokens.set_accent("iris")
        root = QWidget()
        viewer = QWidget(root)
        viewer.setProperty(theme.FIXED_DARK_PROPERTY, True)
        lbl = QLabel("v", viewer)
        lbl.setStyleSheet("QLabel { color: #F5F5F7; background: #0A84FF; }")
        theme.apply("light", root)
        self.assertIn("color: #F2F2F4", lbl.styleSheet(), "prohlížeč zůstává tmavý (Graphite)")
        self.assertIn(tokens.accent("dark")["fill"], lbl.styleSheet())
        root.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestGeneratedStylesheets(unittest.TestCase):

    def tearDown(self):
        tokens.set_accent(tokens.DEFAULT_ACCENT)
        theme.apply("dark")

    def test_both_kinds_build_in_both_modes(self):
        for kind in ("main", "settings"):
            dark, light = tokens.build_qss(kind, "dark"), tokens.build_qss(kind, "light")
            self.assertNotEqual(dark, light)
            self.assertIn(theme.color("bg_sunken" if kind == "settings" else "bg", "dark"), dark)
            self.assertIn(tokens.accent("light")["fill"], light)

    def test_checkbox_has_checkmark_image(self):
        qss = tokens.build_qss("settings", "dark")
        rule = qss[qss.index("QCheckBox::indicator:checked"):]
        rule = rule[:rule.index("}")]
        self.assertIn("image: url(", rule)
        path = tokens.check_image()
        self.assertTrue(path and os.path.exists(path.replace("/", os.sep)))
        self.assertTrue(os.path.exists(path.replace("/", os.sep).replace(".png", "@2x.png")))

    def test_token_widget_regenerates_on_theme_switch(self):
        w = QWidget()
        tokens.apply_stylesheet(w, "main")
        self.assertEqual(w.styleSheet(), tokens.build_qss("main", "dark"))
        theme.apply("light", w)
        self.assertEqual(w.styleSheet(), tokens.build_qss("main", "light"))
        self.assertNotIn("#1C1C1E", w.styleSheet().split("QLabel")[0], "světlé pozadí, ne přeložené tmavé")
        theme.apply("dark", w)
        self.assertEqual(w.styleSheet(), tokens.build_qss("main", "dark"))
        w.deleteLater()

    def test_accent_change_rebuilds_cached_stylesheet(self):
        tokens.set_accent("iris")
        iris = tokens.build_qss("main", "dark")
        tokens.set_accent("ember")
        self.assertNotEqual(iris, tokens.build_qss("main", "dark"))
        self.assertIn(tokens.accent("dark")["fill"], tokens.build_qss("main", "dark"))

    def test_palette_highlight_uses_accent_fill(self):
        from PyQt6.QtGui import QPalette
        tokens.set_accent("moss")
        pal = theme.build_palette("dark")
        self.assertEqual(pal.color(QPalette.ColorRole.Highlight).name().upper(), tokens.accent("dark")["fill"])
        self.assertEqual(pal.color(QPalette.ColorRole.Window).name().upper(), theme.color("bg", "dark"))

    def test_settings_dialog_uses_generated_style(self):
        import settings_dialog
        from unittest import mock
        with mock.patch.object(settings_dialog.SettingsDialog, "_read_library_stats",
                               return_value={"unconfirmed": None, "confirmed": None, "trash_plan": None}):
            dlg = settings_dialog.SettingsDialog()
        self.assertEqual(dlg.property(theme.TOKEN_QSS_PROPERTY), "settings")
        self.assertEqual(dlg.styleSheet(), tokens.build_qss("settings", "dark"))
        self.assertNotIn("-apple-system", dlg.styleSheet())
        dlg.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestTypeAndConfig(unittest.TestCase):

    def test_type_scale_fonts(self):
        for role, (px, weight, _) in tokens.TYPE_SCALE.items():
            f = tokens.font(role)
            self.assertEqual(f.pixelSize(), px, role)
            self.assertEqual(int(f.weight()), weight, role)
        self.assertIn("font-size: 15px", tokens.qss_font("headline"))

    def test_default_config_has_accent(self):
        self.assertEqual(config.DEFAULT_CONFIG["accent"], tokens.DEFAULT_ACCENT)
        self.assertIn("accent_custom", config.DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()
