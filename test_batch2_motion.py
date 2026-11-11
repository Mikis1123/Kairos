"""Kairos dávka 2 – pohybový engine (motion.py) a kreslené ovládací prvky (ui_controls.py)."""
import sys
import unittest
from unittest import mock

try:
    from PyQt6.QtCore import Qt, QRect, QRectF, QPoint
    from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QPushButton
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    from PyQt6.QtTest import QTest
    HAS_QT = True
except Exception:          # pragma: no cover
    HAS_QT = False

if HAS_QT:
    import motion
    import tokens
    from ui_controls import KButton, SidebarNav, SegmentedControl, ToggleSwitch


class _MotionOn:
    """Animace zapnuté bez ohledu na nastavení Windows stroje, na kterém testy běží."""

    def setUp(self):
        self._saved = (motion._user_reduce, motion._system_cache)
        motion._user_reduce, motion._system_cache = False, True

    def tearDown(self):
        motion._user_reduce, motion._system_cache = self._saved


def all_tweens(widget):
    return [t for t in widget.findChildren(motion.Tween)]


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestMotion(_MotionOn, unittest.TestCase):

    def test_durations_follow_tokens_and_reduced_motion(self):
        for kind, (ms, _) in tokens.MOTION.items():
            self.assertEqual(motion.duration(kind), ms)
        motion.set_reduce_motion(True)
        self.assertTrue(motion.reduced())
        self.assertEqual(motion.duration("emphasized"), 0)
        motion.set_reduce_motion(False)
        motion._system_cache = False                 # Windows: Animační efekty vypnuté
        self.assertTrue(motion.reduced())

    def test_config_switch(self):
        motion.configure({"reduce_motion": True})
        self.assertTrue(motion.reduced())
        motion.configure({})
        self.assertFalse(motion.reduced())

    def test_spring_overshoots(self):
        curve = motion.easing("spring")
        self.assertGreater(max(curve.valueForProgress(i / 50) for i in range(51)), 1.0)
        self.assertLessEqual(max(motion.easing("emphasized").valueForProgress(i / 50) for i in range(51)), 1.0)

    def test_tween_animates_then_stops(self):
        host = QWidget()
        host.show()
        seen = []
        tw = motion.Tween(0.0, seen.append, host)
        tw.to(1.0, "micro")
        self.assertTrue(tw.is_running())
        QTest.qWait(tokens.MOTION["micro"][0] + 120)
        self.assertFalse(tw.is_running(), "po doběhnutí žádné tikání")
        self.assertAlmostEqual(tw.value, 1.0)
        self.assertGreater(len(seen), 1, "průběžné kroky")
        host.deleteLater()

    def test_tween_is_instant_when_reduced_or_hidden(self):
        hidden = QWidget()
        tw = motion.Tween(0.0, lambda v: None, hidden)
        tw.to(1.0, "scene")
        self.assertFalse(tw.is_running())
        self.assertEqual(tw.value, 1.0)
        shown = QWidget()
        shown.show()
        motion.set_reduce_motion(True)
        tw2 = motion.Tween(0.0, lambda v: None, shown)
        tw2.to(1.0, "scene")
        self.assertFalse(tw2.is_running())
        self.assertEqual(tw2.value, 1.0)
        hidden.deleteLater()
        shown.deleteLater()

    def test_widget_deleted_mid_animation_is_silent(self):
        """Regrese: Tween rušený uprostřed animace (karty osob se přestaví při najetí) nesmí
        vyhodit výjimku ze slotu – PyQt6 by kvůli ní zavolal qFatal a celý proces spadl."""
        caught = []
        old_hook = sys.excepthook
        sys.excepthook = lambda *exc: caught.append(exc)
        try:
            for _ in range(20):
                host = QWidget()
                host.show()
                tw = motion.Tween(0.0, lambda v, h=host: h.update(), host)
                tw.finished.connect(lambda: None)
                tw.to(1.0, "scene")
                QTest.qWait(5)
                host.deleteLater()
                QTest.qWait(5)
            QTest.qWait(tokens.MOTION["scene"][0] + 50)
        finally:
            sys.excepthook = old_hook
        self.assertEqual(caught, [])

    def test_retarget_continues_from_current_value(self):
        host = QWidget()
        host.show()
        tw = motion.Tween(0.0, lambda v: None, host)
        tw.to(1.0, "scene")
        QTest.qWait(150)
        mid = tw.value
        self.assertTrue(0.0 < mid < 1.0)
        tw.to(0.0, "standard")
        self.assertEqual(tw._anim.startValue(), mid, "bez skoku na začátek")
        host.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestControls(_MotionOn, unittest.TestCase):

    def test_no_animation_runs_while_idle(self):
        root = QWidget()
        lay = QVBoxLayout(root)
        nav = SidebarNav(["A", "B", "C"])
        seg = SegmentedControl([("Vše", "all"), ("Videa", "video")])
        sw = ToggleSwitch("Chytrý import")
        btn = KButton("OK", variant="primary")
        for w in (nav, seg, sw, btn):
            lay.addWidget(w)
        nav.items[0].setChecked(True)
        root.show()
        QTest.qWait(50)
        self.assertFalse(any(t.is_running() for t in all_tweens(root)), "klid = žádná běžící animace")
        root.deleteLater()

    def test_kbutton_variant_and_press(self):
        b = KButton("Uložit")
        b.setObjectName("btnSave")
        self.assertEqual(b.variant(), "primary")
        b.setObjectName("btnReset")
        self.assertEqual(b.variant(), "danger_outline")
        self.assertEqual(KButton("x", variant="ghost").variant(), "ghost")
        self.assertIsInstance(b, QPushButton)
        b.show()
        clicks = []
        b.clicked.connect(lambda: clicks.append(1))
        QTest.mousePress(b, Qt.MouseButton.LeftButton)
        self.assertEqual(b._press.target, 1.0)
        QTest.mouseRelease(b, Qt.MouseButton.LeftButton)
        self.assertEqual(clicks, [1])
        self.assertEqual(b._press.target, 0.0)
        self.assertEqual(b.sizeHint().height(), 32)
        self.assertEqual(KButton("x", compact=True).sizeHint().height(), 28)
        b.grab()                      # vykreslení bez chyby
        b.deleteLater()

    def test_sidebar_pill_slides_to_checked_item(self):
        nav = SidebarNav(["Fotografie", "Osoby", "Události", "Mapa"])
        nav.resize(180, 160)
        nav.items[0].setChecked(True)
        nav.show()
        QTest.qWait(30)
        self.assertEqual(nav._pill.geometry(), nav.items[0].geometry())
        got = []
        nav.current_changed.connect(got.append)
        QTest.mouseClick(nav.items[2], Qt.MouseButton.LeftButton)
        self.assertEqual(got, [2])
        self.assertTrue(nav._rect.is_running(), "jezdec přejíždí")
        QTest.qWait(tokens.MOTION["emphasized"][0] + 150)
        self.assertEqual(nav._pill.geometry(), nav.items[2].geometry())
        self.assertFalse(nav.items[0].isChecked())
        nav.deleteLater()

    def test_segmented_control(self):
        seg = SegmentedControl([("Vše", "all"), ("Oblíbené", "favorites"), ("Smazané", "trash")], current="all")
        seg.show()
        QTest.qWait(30)
        got = []
        seg.changed.connect(got.append)
        seg.buttons()[-1].click()                    # jako main._filter_group.buttons()[-1].click()
        self.assertEqual(got, ["trash"])
        self.assertEqual(seg.current_key(), "trash")
        QTest.qWait(tokens.MOTION["emphasized"][0] + 150)
        self.assertEqual(seg._thumb.value, QRectF(seg.segments[2].geometry()))
        seg.segments[2].setFocus()
        QTest.keyClick(seg.segments[2], Qt.Key.Key_Right)  # šipky dokola
        self.assertEqual(got[-1], "all")
        seg.set_current("favorites")
        self.assertEqual(seg.current_key(), "favorites")
        self.assertEqual(got[-1], "all", "programová změna nevysílá changed")
        seg.deleteLater()

    def test_toggle_switch(self):
        sw = ToggleSwitch("Omezit animace")
        sw.resize(300, 28)
        sw.show()
        toggled = []
        sw.toggled.connect(toggled.append)
        QTest.mouseClick(sw, Qt.MouseButton.LeftButton, pos=QPoint(10, 14))     # klik na text = přepnutí
        self.assertTrue(sw.isChecked())
        self.assertEqual(toggled, [True])
        self.assertEqual(sw._pos.target, 1.0)
        QTest.qWait(tokens.MOTION["spring"][0] + 150)
        self.assertAlmostEqual(sw._pos.value, 1.0, places=3)
        motion.set_reduce_motion(True)
        sw.setChecked(False)
        self.assertEqual(sw._pos.value, 0.0, "omezený pohyb = okamžitě")
        sw.grab()
        sw.deleteLater()


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestSettingsControls(unittest.TestCase):

    def make(self, cfg=None):
        import settings_dialog
        stats = {"unconfirmed": None, "confirmed": None, "trash_plan": None}
        with mock.patch.object(settings_dialog.SettingsDialog, "_read_library_stats", return_value=stats), \
                mock.patch.object(settings_dialog, "load_config", return_value=dict(cfg or {})):
            return settings_dialog.SettingsDialog()

    def test_category_rail_switches_pages(self):
        dlg = self.make()
        self.assertEqual([t for _b, t in dlg._nav],
                         ["Knihovna", "Vzhled", "Hudba", "Rozpoznávání", "Úložiště a mezipaměť", "Mapa offline",
                          "Soukromí", "O aplikaci"])
        self.assertEqual(dlg.pages.count(), 8)
        dlg._nav[4][0].click()
        self.assertEqual(dlg.pages.currentIndex(), 4)
        dlg.pages.setCurrentIndex(2)
        self.assertTrue(dlg._nav[2][0].isChecked())
        dlg.deleteLater()

    def test_switches_replace_checkboxes_and_save(self):
        import settings_dialog
        dlg = self.make({"reduce_motion": True, "auto_scan_on_startup": False})
        self.assertIsInstance(dlg.chk_autoscan, ToggleSwitch)
        self.assertTrue(dlg.chk_reduce_motion.isChecked())
        self.assertFalse(dlg.chk_autoscan.isChecked())
        dlg.chk_reduce_motion.setChecked(False)
        with mock.patch.object(settings_dialog, "save_config") as save:
            dlg.save_and_close()
        self.assertFalse(save.call_args[0][0]["reduce_motion"])
        dlg.deleteLater()

    def test_all_dialog_buttons_are_animated(self):
        dlg = self.make()
        buttons = dlg.findChildren(QPushButton)
        self.assertTrue(buttons)
        self.assertTrue(all(isinstance(b, KButton) for b in buttons))
        dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
