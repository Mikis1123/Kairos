"""Regresní testy pro Patch 4 – první dávka kritických oprav z auditu (OmniPhoto Vault).

Spuštění (ve složce projektu):   python -m pytest test_patch4.py -v

Pokrývá:
  C1 – Prohlížeč se vytvoří se správným API (dřív TypeError: multiple values for 'parent')
  C2 – Záznamy médií mají sloupce, které čte prohlížeč (file_name, file_path, paired_video_path)
  C3 – Výsledky AI hledání [(id, skóre)] se správně převedou na položky mřížky
  C4 – Zrušení importu volá MediaScannerThread.stop() (metoda cancel() neexistuje)
  C6 – Pohybová videa Live Photos nejsou v mřížce jako samostatné položky
  + Vlákna: rychle po sobě spuštěná hledání nezničí běžící QThread

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import inspect
import os
import shutil
import sys
import tempfile
import types
import unittest

from PIL import Image

# ── Qt inicializace PŘED WebEngine importy (vzor z test_patch3.py) ────────────
HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    import main
    from main import MainWindow


class TempEnv(unittest.TestCase):
    """Izolované prostředí: vlastní DB a soubory médií v dočasné složce."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p4_test_")
        self._saved_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self._saved_db
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

    def _file(self, name, image=True):
        path = os.path.join(self.tmp, name)
        if image:
            Image.new("RGB", (64, 48), (90, 140, 200)).save(path, "JPEG")
        else:
            with open(path, "wb") as f:
                f.write(b"\x00" * 32)
        return path

    def _add(self, path, media_type, date="2026-07-07 10:00:00", paired=None, event_id=None):
        return self.x(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, "
            "paired_video_path, event_id) VALUES (?, ?, ?, ?, ?, ?)",
            (path, os.path.basename(path), media_type, date, paired, event_id))

    def _library(self):
        """Fotka, Live Photo (+ jeho pohybové video) a samostatné video."""
        photo = self._file("IMG_0001.jpg")
        live = self._file("IMG_0002.jpg")
        motion = self._file("IMG_0002.MOV", image=False)
        video = self._file("IMG_0003.MOV", image=False)
        ids = {
            "photo": self._add(photo, "photo", "2026-07-07 10:00:00"),
            "live": self._add(live, "live_photo", "2026-07-07 11:00:00", paired=motion),
            "motion": self._add(motion, "video", "2026-07-07 11:00:00"),
            "video": self._add(video, "video", "2026-07-07 12:00:00"),
        }
        paths = {"photo": photo, "live": live, "motion": motion, "video": video}
        return ids, paths


# ── C2 – sloupce záznamů ─────────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMediaRecordColumns(TempEnv):
    """C2 – Záznamy mají tvar MEDIA_COLUMNS, který čte model i prohlížeč."""

    def test_record_layout_matches_viewer_contract(self):
        """[1] = název souboru, [2] = cesta, [3] = typ, [4] = párové video."""
        ids, paths = self._library()
        rows = {r[0]: r for r in main.fetch_media_records()}
        live = rows[ids["live"]]
        self.assertEqual(live[1], "IMG_0002.jpg")
        self.assertEqual(live[2], paths["live"])
        self.assertEqual(live[3], "live_photo")
        self.assertEqual(live[4], paths["motion"], "Live Photo musí nést cestu k pohybovému videu")
        self.assertEqual(len(live), 8)

    def test_grid_meta_name_is_file_name(self):
        """Model mřížky zobrazuje název souboru, ne celou cestu."""
        self._library()
        model = main.ApplePhotoModel(64)
        model.set_records(main.fetch_media_records())
        meta = model.data(model.index(0, 0), main.ApplePhotoModel.MetaRole)
        self.assertNotIn(os.sep, meta["name"])

    def test_sort_newest_first(self):
        """Výchozí řazení: nejnovější nahoře."""
        ids, _ = self._library()
        rows = main.fetch_media_records()
        self.assertEqual(rows[0][0], ids["video"])
        self.assertEqual(rows[-1][0], ids["photo"])


# ── C6 – pohybová videa Live Photos ──────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMotionClipFilter(TempEnv):
    """C6 – Pohybové video Live Photo není samostatná položka."""

    def test_motion_clip_excluded_from_grid(self):
        ids, _ = self._library()
        got = {r[0] for r in main.fetch_media_records()}
        self.assertNotIn(ids["motion"], got)
        self.assertEqual(got, {ids["photo"], ids["live"], ids["video"]},
                         "fotka, Live Photo i samostatné video musí zůstat")

    def test_motion_clip_excluded_from_event(self):
        """Ani v událostech se pohybové video nezobrazí samostatně."""
        ev = self.x("INSERT INTO events (title) VALUES ('Výlet')")
        live = self._file("IMG_0010.jpg")
        motion = self._file("IMG_0010.MOV", image=False)
        self._add(live, "live_photo", paired=motion, event_id=ev)
        motion_id = self._add(motion, "video", event_id=ev)
        got = {r[0] for r in main.fetch_media_records("m.event_id = ?", (ev,))}
        self.assertNotIn(motion_id, got)
        self.assertEqual(len(got), 1)

    def test_person_photos_listed_once(self):
        """Dva obličeje téže osoby na jedné fotce = fotka v seznamu jen jednou."""
        ids, _ = self._library()
        pid = self.x("INSERT INTO people (name) VALUES ('Jana')")
        for _ in range(2):
            self.x("INSERT INTO face_instances (media_id, person_id, embedding) VALUES (?, ?, ?)",
                   (ids["photo"], pid, b"\x00" * 2048))
        rows = main.fetch_media_records(
            "m.id IN (SELECT media_id FROM face_instances WHERE person_id = ?)", (pid,))
        self.assertEqual([r[0] for r in rows], [ids["photo"]])


# ── C1 – vytvoření prohlížeče ────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestViewerCreation(TempEnv):
    """C1 – Prohlížeč se otevře nad seznamem záznamů bez TypeError."""

    def _viewer(self, key):
        ids, paths = self._library()
        records = main.fetch_media_records()
        index = next(i for i, r in enumerate(records) if r[0] == ids[key])
        viewer = main.create_media_viewer(records, index)
        self.addCleanup(viewer.close)
        return viewer, records, index, paths

    def test_viewer_constructs_with_records(self):
        """create_media_viewer nevyhodí TypeError a nastaví aktuální položku."""
        viewer, records, index, _ = self._viewer("photo")
        self.assertIsInstance(viewer, main.MediaViewerWidget)
        self.assertEqual(viewer.current_index, index)
        self.assertEqual(len(viewer.media_list), len(records))
        self.assertEqual(viewer.lbl_title.text(), "IMG_0001.jpg")

    def test_viewer_is_embeddable_widget(self):
        """Fáze 1: prohlížeč není samostatné okno – MainWindow ho vkládá do zásobníku stránek.
        (Dříve test ověřoval opak; požadavek se změnil na navigaci uvnitř okna, viz test_patch6.)"""
        parent = main.QWidget()
        self.addCleanup(parent.deleteLater)
        self._library()
        records = main.fetch_media_records()
        viewer = main.create_media_viewer(records, 0, parent)
        self.assertFalse(viewer.isWindow())
        self.assertFalse(viewer.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose),
                         "jediný prohlížeč se znovu používá, nesmí se zničit při zavření")

    def test_live_photo_gets_paired_video(self):
        """Live Photo: tlačítko LIVE je k dispozici a přehrávač má pohybové video."""
        viewer, _, _, paths = self._viewer("live")
        self.assertFalse(viewer.btn_live.isHidden())
        self.assertEqual(os.path.normcase(viewer.player.source().toLocalFile()),
                         os.path.normcase(paths["motion"]))

    def test_old_constructor_signature_is_gone(self):
        """main.py už nevolá MediaViewerWidget(file_path, media_type, thumb, parent=...)."""
        src = inspect.getsource(main)
        self.assertNotIn("MediaViewerWidget(file_path", src)

    def test_favorite_toggle_updates_viewer_list(self):
        """Po přepnutí oblíbené zná nový stav i seznam prohlížeče (návrat šipkou)."""
        viewer, _, index, _ = self._viewer("photo")
        received = []
        viewer.favorite_changed_signal.connect(lambda mid, fav: received.append((mid, fav)))
        viewer.handle_toggle_favorite()
        media_id = viewer.media_list[index][0]
        self.assertEqual(viewer.media_list[index][7], 1)
        self.assertEqual(received, [(media_id, 1)])
        self.assertEqual(self.q("SELECT is_favorite FROM media_items WHERE id=?", (media_id,))[0][0], 1)

    def test_model_set_favorite_keeps_rows(self):
        """ApplePhotoModel.set_favorite změní jen srdíčko, bez resetu modelu."""
        self._library()
        model = main.ApplePhotoModel(64)
        records = main.fetch_media_records()
        model.set_records(records)
        resets = []
        model.modelReset.connect(lambda: resets.append(1))
        model.set_favorite(records[0][0], 1)
        meta = model.data(model.index(0, 0), main.ApplePhotoModel.MetaRole)
        self.assertTrue(meta["is_fav"])
        self.assertEqual(resets, [])
        self.assertEqual(model.rowCount(), len(records))


# ── C3 – AI hledání ──────────────────────────────────────────────────────────

def _fake_search_window(records, query):
    """Okno bez Qt widgetů: skutečné metody filtrování/hledání MainWindow nad falešnou mřížkou."""
    shown = []
    fake = types.SimpleNamespace(
        _search_query=query, _search_result=None, _search_sort_override=False,
        _current_filter="all", _current_sort="date_desc",
        _all_records=list(records), _trash_records=[],
        _filtered_records=list(records),
        grid_view=types.SimpleNamespace(photo_model=types.SimpleNamespace(
            set_records=lambda recs, possible=(): shown.append((list(recs), set(possible))))),
        lbl_photo_count=types.SimpleNamespace(setText=lambda t: shown.append(t)),
        _sync_memory_card=lambda: None,
    )
    for name in ("_collection_records", "_sorted", "_apply_filter_and_sort", "_count_text"):
        setattr(fake, name, getattr(MainWindow, name).__get__(fake))
    return fake, shown


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSearchResults(unittest.TestCase):
    """C3 – search_by_text vrací (id, skóre); dřív se porovnávaly n-tice s int -> 0 výsledků."""

    RECORDS = [(i, f"IMG_{i}.jpg", f"/f/IMG_{i}.jpg", "photo", None, None, "2026-07-07", 0)
               for i in (1, 2, 3, 4)]

    def test_parse_tuple_results(self):
        self.assertEqual(main.parse_search_results([(5, 0.91), (3, 0.50)]), [5, 3])

    def test_parse_plain_ids_and_garbage(self):
        self.assertEqual(main.parse_search_results([7, "x", None, (8, 0.3), ()]), [7, 8])
        self.assertEqual(main.parse_search_results(None), [])

    def test_order_by_relevance(self):
        got = main.order_records_by_ids(self.RECORDS, [3, 1, 3])
        self.assertEqual([r[0] for r in got], [3, 1])

    def test_on_search_results_shows_tiers(self):
        """Celý slot: jisté shody, pak možné – v pořadí relevance, možné označené pro závoj."""
        fake, shown = _fake_search_window(self.RECORDS, "pes")
        MainWindow._on_search_results(fake, main.search_engine.SearchResult("pes", [4, 2], [1], [], True), "pes")
        self.assertEqual([r[0] for r in fake._filtered_records], [4, 2, 1])
        self.assertEqual(shown[0][1], {1})
        self.assertIn("2 shody · 1 možných", shown[1])

    def test_legacy_tuple_results_still_accepted(self):
        fake, _ = _fake_search_window(self.RECORDS, "pes")
        MainWindow._on_search_results(fake, [(4, 0.8), (2, 0.6)], "pes")
        self.assertEqual([r[0] for r in fake._filtered_records], [4, 2])

    def test_stale_results_ignored(self):
        fake, shown = _fake_search_window(self.RECORDS, "kočka")
        MainWindow._on_search_results(fake, main.search_engine.SearchResult("pes", [4], [], [], True), "pes")
        self.assertEqual(shown, [])

    def test_empty_results_clear_filtered_records(self):
        """Prázdný výsledek nesmí nechat staré záznamy (dvojklik by otevřel jinou fotku)."""
        fake, shown = _fake_search_window(self.RECORDS, "pes")
        MainWindow._on_search_results(fake, main.search_engine.SearchResult("pes", [], [], [], True), "pes")
        self.assertEqual(fake._filtered_records, [])
        self.assertEqual(shown[-1], "Nic neodpovídá „pes“")

    def test_sort_change_keeps_search_results(self):
        """Dřív změna řazení během hledání přefiltrovala mřížku podle NÁZVU SOUBORU -> prázdno."""
        fake, _ = _fake_search_window(self.RECORDS, "pes")
        MainWindow._on_search_results(fake, main.search_engine.SearchResult("pes", [4, 2], [], [], True), "pes")
        fake._current_sort = "date_asc"
        fake._search_sort_override = True
        fake._apply_filter_and_sort()
        self.assertEqual(sorted(r[0] for r in fake._filtered_records), [2, 4])

    def test_results_respect_active_collection(self):
        """Hledání v Oblíbených nesmí ukázat neoblíbené fotky z knihovny."""
        recs = [r[:7] + ((1,) if r[0] == 2 else (0,)) for r in self.RECORDS]
        fake, _ = _fake_search_window(recs, "pes")
        fake._current_filter = "favorites"
        MainWindow._on_search_results(fake, main.search_engine.SearchResult("pes", [4, 2], [], [], True), "pes")
        self.assertEqual([r[0] for r in fake._filtered_records], [2])


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSearchWorkerLifetime(unittest.TestCase):
    """Vlákna – další hledání nepřepíše referenci na běžící QThread."""

    def test_overlapping_searches_keep_workers_alive(self):
        fake = types.SimpleNamespace(
            _search_query="les", _search_workers=set(),
            _embeddings_cache={1: b"neplatny"}, _embeddings_loaded=True,     # nevalidní -> obraz nic nevrátí, model netřeba
            _apply_filter_and_sort=lambda: None,
            _collection_records=lambda: [],
        )
        results = []
        fake._on_search_results = lambda res, q: results.append((res, q))
        fake._release_search_worker = lambda w: MainWindow._release_search_worker(fake, w)
        fake._drop_search_worker = lambda w: MainWindow._drop_search_worker(fake, w)

        MainWindow._run_ai_search(fake)
        MainWindow._run_ai_search(fake)
        self.assertEqual(len(fake._search_workers), 2, "obě vlákna musí mít živou referenci")
        for w in list(fake._search_workers):
            w.wait(5000)
        for _ in range(50):
            _app.processEvents()
            if not fake._search_workers:
                break
        self.assertEqual(fake._search_workers, set(), "doběhlá vlákna se uvolní")
        self.assertEqual(len(results), 2)

    def test_worker_exception_yields_empty_results(self):
        """Výjimka v hledání (model, síť) nesmí uniknout z QThread.run – to by byl qFatal."""
        import main
        from unittest import mock
        w = main.AISearchWorker("pes", {1: b"x"})
        got = []
        w.search_finished.connect(lambda res, q: got.append((res, q)),
                                  Qt.ConnectionType.DirectConnection)
        with mock.patch.object(main.search_engine, "run_search", side_effect=OSError("HF Hub offline")):
            w.run()                                         # přímo, ať je výjimka vidět v testu
        self.assertEqual(len(got), 1)
        result, query = got[0]
        self.assertEqual(query, "pes")
        self.assertTrue(result.is_empty())


# ── C4 – zrušení importu ─────────────────────────────────────────────────────

class _FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class _ScannerWithoutCancel:
    """Jako MediaScannerThread: má stop(), nemá cancel()."""

    def __init__(self):
        self.stopped = False
        self.finished = _FakeSignal()

    def isRunning(self):
        return True

    def stop(self):
        self.stopped = True


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestImportCancel(unittest.TestCase):
    """C4 – Zrušení importu."""

    def test_scanner_api_has_stop_not_cancel(self):
        import scanner
        t = scanner.MediaScannerThread(tempfile.gettempdir())
        self.assertFalse(hasattr(t, "cancel"))
        t.stop()
        self.assertFalse(t._is_running)

    def test_cancel_import_calls_stop(self):
        """_cancel_import nevyhodí AttributeError, zastaví sken a po doběhnutí obnoví data."""
        pages = []
        fake = types.SimpleNamespace(
            _scanner=_ScannerWithoutCancel(),
            stack=types.SimpleNamespace(setCurrentIndex=pages.append),
            _load_photos=lambda: None, _load_events=lambda: None,
        )
        MainWindow._cancel_import(fake)
        self.assertTrue(fake._scanner.stopped)
        self.assertEqual(pages, [0])
        self.assertIn(fake._load_photos, fake._scanner.finished.slots)
        self.assertIn(fake._load_events, fake._scanner.finished.slots)

    def test_close_event_does_not_call_cancel(self):
        src = inspect.getsource(MainWindow.closeEvent)
        self.assertNotIn(".cancel(", src)
        # Sprint R: sken a démon se zastavují ve společné smyčce (stop + wait, jinak úschova)
        self.assertRegex(src, r"for thread in \([^)]*self\._scanner[^)]*\):")
        self.assertIn("thread.stop()", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
