"""Kairos Polish P3 – přetažení do okna a sledované složky.

Spuštění (ve složce projektu):   python -m pytest test_polish_p3.py -v

Pokrývá:
  drop_import.py  – třídění přetažených cest (vnořené složky, nepodporované soubory), popisek,
                    směrování tažení z libovolného podřízeného widgetu (i mapy), překryv, cizí obsah.
  scanner.py      – přetažené soubory se indexují na místě (+ partner Live Photo), bez kopie.
  folder_watch.py – strom do MAX_DEPTH, strop cest (mělké přednost), odložené sloučené
                    přeskenování, nová / smazaná vnořená složka, kontrola mtime po návratu do okna.
  main.py         – fronta importu na pozadí, nabídka „Sledovat složku“, sledování v Předvolbách.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEvent, QMimeData, QPoint, QPointF, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

if HAS_QT:
    from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    import drop_import
    import folder_watch
    import main
    import scanner
    from test_patch11_maps import wait


def _touch(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="omni_p3_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def p(self, *parts):
        return os.path.normpath(os.path.join(self.tmp, *parts))


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestClassify(TmpDir):

    def test_folders_and_supported_files_only(self):
        a = self.p("Dovolená")
        _touch(self.p("Dovolená", "sub", "x.jpg"))
        inner = _touch(self.p("Dovolená", "y.jpg"))
        loose = _touch(self.p("IMG_1.HEIC"))
        txt = _touch(self.p("pozn.txt"))
        folders, files = drop_import.classify_paths(
            [a, self.p("Dovolená", "sub"), inner, loose, txt, self.p("neexistuje.jpg"), ""])
        self.assertEqual(folders, [a], "podsložka přetažené složky se neskenuje dvakrát")
        self.assertEqual(files, [loose], "soubor uvnitř složky i nepodporovaný soubor ven")

    def test_describe(self):
        self.assertEqual(drop_import.describe([self.p("Dovolená")], []), "„Dovolená“")
        self.assertEqual(drop_import.describe([self.p("a"), self.p("b")], [self.p("c.jpg")] * 5),
                         "2 složky · 5 souborů")
        self.assertEqual(drop_import.describe([], [self.p("c.jpg")]), "1 soubor")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestScannerFiles(TmpDir):

    def test_live_partner_and_in_place_paths(self):
        heic = _touch(self.p("iphone", "IMG_7.HEIC"))
        mov = _touch(self.p("iphone", "IMG_7.MOV"))
        _touch(self.p("iphone", "IMG_7.AAE"))
        _touch(self.p("iphone", "IMG_8.HEIC"))
        files = scanner.with_live_partners([heic])
        self.assertEqual(sorted(files), sorted([heic, mov]))
        s = scanner.MediaScannerThread(None, files=[heic])
        self.assertEqual(sorted(s._collect_files()), sorted([heic, mov]), "složka se neprochází")
        to_process, _, _ = s._classify(s._collect_files(), {})
        self.assertEqual(sorted(db for _, db, _, _ in to_process), sorted([heic, mov]),
                         "do DB jde původní cesta – nic se nekopíruje")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestFolderWatcher(TmpDir):

    def make(self, **kw):
        w = folder_watch.FolderWatcher(debounce_ms=kw.pop("debounce_ms", 150), **kw)
        self.addCleanup(w.deleteLater)
        self.events = []
        w.changed.connect(self.events.append)
        return w

    def watched_keys(self, w):
        return {os.path.normcase(os.path.normpath(d)) for d in w.watched()}

    def test_nested_tree_down_to_max_depth(self):
        album = ("lib", "2026", "Léto", "Chorvatsko", "Den1")
        os.makedirs(self.p(*album))
        os.makedirs(self.p("lib", ".hidden", "x"))
        os.makedirs(self.p("lib", *[f"d{i}" for i in range(folder_watch.MAX_DEPTH + 1)]))
        w = self.make()
        w.set_roots([self.p("lib"), self.p("neexistuje")])
        watched = self.watched_keys(w)
        for i in range(1, len(album) + 1):
            self.assertIn(os.path.normcase(self.p(*album[:i])), watched)
        self.assertNotIn(os.path.normcase(self.p("lib", ".hidden")), watched)
        chain = ["lib"] + [f"d{i}" for i in range(folder_watch.MAX_DEPTH + 1)]
        self.assertIn(os.path.normcase(self.p(*chain[:folder_watch.MAX_DEPTH + 1])), watched)
        self.assertNotIn(os.path.normcase(self.p(*chain)), watched, "hlouběji než MAX_DEPTH ne")
        self.assertEqual(w.roots(), [self.p("lib")])

    def test_path_budget_prefers_shallow_folders(self):
        for i in range(4):
            os.makedirs(self.p("lib", f"s{i}", "deep"))
        with mock.patch.object(folder_watch, "MAX_PATHS", 5):
            w = self.make()
            w.set_roots([self.p("lib")])
            self.assertEqual(self.watched_keys(w),
                             {os.path.normcase(self.p("lib"))} |
                             {os.path.normcase(self.p("lib", f"s{i}")) for i in range(4)})

    def test_deep_change_triggers_scan(self):
        os.makedirs(self.p("lib", "2026", "Léto", "Chorvatsko", "Den1"))
        w = self.make()
        w.set_roots([self.p("lib")])
        _touch(self.p("lib", "2026", "Léto", "Chorvatsko", "Den1", "a.jpg"))
        for _ in range(40):
            if self.events:
                break
            wait(50)
        self.assertEqual(self.events, [[self.p("lib")]])

    def test_changes_are_debounced_into_one_scan(self):
        os.makedirs(self.p("lib", "2024"))
        w = self.make()
        w.set_roots([self.p("lib")])
        for i in range(3):
            _touch(self.p("lib", "2024", f"n{i}.jpg"))
            wait(40)
        for _ in range(40):
            if self.events:
                break
            wait(50)
        wait(250)
        self.assertEqual(self.events, [[self.p("lib")]], "tři změny = jeden sken")

    def test_new_nested_subfolder_becomes_watched(self):
        os.makedirs(self.p("lib", "2026"))
        w = self.make()
        w.set_roots([self.p("lib")])
        os.makedirs(self.p("lib", "2026", "nová", "Den1"))
        for _ in range(40):
            if self.events:
                break
            wait(50)
        watched = self.watched_keys(w)
        self.assertIn(os.path.normcase(self.p("lib", "2026", "nová")), watched)
        self.assertIn(os.path.normcase(self.p("lib", "2026", "nová", "Den1")), watched)
        shutil.rmtree(self.p("lib", "2026", "nová"))
        self.events.clear()
        for _ in range(40):
            if self.events:
                break
            wait(50)
        self.assertNotIn(os.path.normcase(self.p("lib", "2026", "nová")), self.watched_keys(w),
                         "smazaná složka se přestane hlídat")

    def test_window_activation_checks_mtimes_immediately(self):
        os.makedirs(self.p("lib", "2026", "Den1"))
        # NTFS nezmění mtime složky, když soubor vznikne ve stejném okamžiku jako ona -> starší složka
        os.utime(self.p("lib", "2026", "Den1"), (1_700_000_000, 1_700_000_000))
        w = self.make(debounce_ms=60000)       # watcher by skenoval až za minutu
        w.set_roots([self.p("lib")])
        w.window_activated()
        self.assertEqual(self.events, [], "beze změny = žádný sken")
        w._fs.blockSignals(True)               # změna, kterou watcher propásl (spánek, síťový disk)
        _touch(self.p("lib", "2026", "Den1", "a.jpg"))
        wait(100)
        w.window_activated()
        self.assertEqual(self.events, [], "rychlé opakování aktivace se nekontroluje")
        w._last_check = None
        w.window_activated()
        self.assertEqual(self.events, [[self.p("lib")]], "návrat do okna = sken hned")
        w._last_check = None
        w.window_activated()
        self.assertEqual(len(self.events), 1, "stav se zapamatoval")


class _FakeScanner:
    """Jako MediaScannerThread pro frontu: signály s connect(), start(), wait(), isRunning()."""
    started = []

    class _Sig:
        def __init__(self):
            self.slots = []

        def connect(self, slot):
            self.slots.append(slot)

        def emit(self, *a):
            for s in list(self.slots):
                s(*a)

    def __init__(self, folder, files=None):
        self.folder, self.files = folder, files
        self.status_signal, self.finished_signal, self.finished = self._Sig(), self._Sig(), self._Sig()
        self.running = False

    def start(self):
        self.running = True
        _FakeScanner.started.append(self)

    def isRunning(self):
        return self.running

    def wait(self, *_):
        return True

    def stop(self):
        self.running = False

    def finish(self, n):
        self.running = False
        self.finished_signal.emit(n)
        self.finished.emit()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowDrop(TmpDir):

    def setUp(self):
        super().setUp()
        from test_patch9 import TempEnv
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.saved = []
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config", side_effect=lambda c: self.saved.append(json.loads(json.dumps(c))))):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w.config["library_folders"], self.w.config["watched_folders"] = [], []
        self.w.resize(1200, 760)
        self.w.show()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        wait(30)

    def mime(self, *paths):
        m = QMimeData()
        m.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return m

    def drag(self, target, mime):
        pos = target.rect().center()
        enter = QDragEnterEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(target, enter)
        return enter

    def test_drag_over_grid_shows_overlay_and_drop_emits(self):
        folder = self.p("Dovolená")
        _touch(self.p("Dovolená", "a.jpg"))
        got = []
        self.w.drop_router.dropped.disconnect()
        self.w.drop_router.dropped.connect(lambda f, s: got.append((f, s)))
        mime = self.mime(folder)
        enter = self.drag(self.w.grid_view.viewport(), mime)
        self.assertTrue(enter.isAccepted())
        overlay = self.w.drop_router.overlay
        self.assertTrue(overlay.isVisible())
        self.assertIn("Dovolená", overlay.detail)
        self.assertTrue(overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        wait(200)
        self.assertFalse(overlay.grab().toImage().isNull(), "lem a kapsle se vykreslí")
        pos = QPointF(self.w.grid_view.viewport().rect().center())
        move = QDragMoveEvent(pos.toPoint(), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.w.grid_view.viewport(), move)
        self.assertTrue(move.isAccepted())
        drop = QDropEvent(pos, Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.w.grid_view.viewport(), drop)
        self.assertEqual(got, [([folder], [])])
        wait(400)
        self.assertFalse(overlay.isVisible(), "po puštění překryv zmizí")

    def test_map_view_does_not_swallow_drop(self):
        folder = self.p("Fotky")
        os.makedirs(folder)
        enter = self.drag(self.w.map_view, self.mime(folder))
        self.assertTrue(enter.isAccepted(), "i nad mapou (QWebEngineView)")

    def test_foreign_content_ignored(self):
        txt = _touch(self.p("pozn.txt"))
        m = QMimeData()
        m.setText("ahoj")
        self.drag(self.w.grid_view.viewport(), m)
        self.drag(self.w.grid_view.viewport(), self.mime(txt))
        self.assertIsNone(self.w.drop_router.overlay, "nepodporovaný obsah = žádný překryv")

    def test_drop_queues_import_and_offers_watch(self):
        folder = self.p("Dovolená")
        os.makedirs(folder)
        loose = _touch(self.p("IMG_1.jpg"))
        _FakeScanner.started = []
        toasts = []
        with mock.patch.object(main, "MediaScannerThread", _FakeScanner), \
                mock.patch.object(main, "show_toast", side_effect=lambda *a, **k: toasts.append((a, k))):
            self.w.import_dropped([folder], [loose])
            self.assertEqual([s.folder for s in _FakeScanner.started], [folder])
            self.assertEqual(self.w._background_import, [("files", [loose])])
            self.assertEqual(toasts[0][1]["action_text"], "Sledovat složku")
            self.w.import_dropped([folder], [])                  # stejná složka znovu = nic navíc
            self.assertEqual(len(self.w._background_import), 1)
            _FakeScanner.started[0].finish(4)
            self.assertEqual(_FakeScanner.started[1].files, [loose], "soubory = jeden sken na místě")
            _FakeScanner.started[1].finish(1)
            self.assertEqual(self.w._background_import, [])
            self.assertIn("Import dokončen: 5", toasts[-1][0][1])
            toasts[0][1]["on_action"]()                          # „Sledovat složku“
        self.assertEqual(self.saved[-1]["library_folders"], [folder])
        self.assertEqual(self.saved[-1]["watched_folders"], [folder])
        self.assertEqual(self.w.folder_watcher.roots(), [folder])
        self.assertTrue(self.w._is_watched(folder))

    def test_watched_change_rescans_in_background(self):
        folder = self.p("Sledovaná")
        os.makedirs(folder)
        self.w.config["library_folders"] = [folder]
        self.w.config["watched_folders"] = [folder, self.p("mimo_knihovnu")]
        self.assertEqual(self.w._watched_roots(), [folder], "sleduje se jen složka knihovny")
        _FakeScanner.started = []
        with mock.patch.object(main, "MediaScannerThread", _FakeScanner), mock.patch.object(main, "show_toast"):
            self.w._on_watched_changed([folder])
            self.assertEqual([s.folder for s in _FakeScanner.started], [folder])
            _FakeScanner.started[0].finish(0)
            self.assertEqual(self.w._background_import, [])

    def test_window_activation_reaches_watcher(self):
        with mock.patch.object(self.w.folder_watcher, "window_activated") as activated, \
                mock.patch.object(self.w, "isActiveWindow", return_value=True):
            self.w.changeEvent(QEvent(QEvent.Type.ActivationChange))
        activated.assert_called_once()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSettingsWatched(TmpDir):

    def test_checkbox_per_library_folder(self):
        from test_patch11_settings import TempLibrary
        import config as cfg
        import settings_dialog
        lib = TempLibrary()
        self.addCleanup(lib.close)
        a, b = self.p("A"), self.p("B")
        cfg.save_config(dict(cfg.DEFAULT_CONFIG, library_folders=[a, b], watched_folders=[a]))
        dlg = settings_dialog.SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        items = [dlg.list_folders.item(i) for i in range(dlg.list_folders.count())]
        self.assertEqual([it.checkState() == Qt.CheckState.Checked for it in items], [True, False])
        items[0].setCheckState(Qt.CheckState.Unchecked)
        items[1].setCheckState(Qt.CheckState.Checked)
        dlg.save_and_close()
        saved = cfg.load_config()
        self.assertEqual(saved["library_folders"], [a, b])
        self.assertEqual(saved["watched_folders"], [b])


if __name__ == "__main__":
    unittest.main()
