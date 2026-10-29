"""Kairos Polish P2 – Vzpomínky („Před 3 lety“) a Tipy a průvodce (Kairos Spark).

Spuštění (ve složce projektu):   python -m pytest test_polish_p2.py -v

Pokrývá:
  memories.py – okno ±3 dny (přelom roku, 29. 2.), české tvary, výběr roku a místa, záložní
                „Tento týden“, koš / letošek / videa mimo, index idx_media_md v plánu dotazu,
                karta (✕ vs. otevření, prolínání jen viditelné).
  main.py     – karta jen ve Vše bez hledání, skrytí na den, vypnutí v Předvolbách.
  guide.py    – kapitoly a tipy, kotvy existují, zkratky odpovídají obsluze kláves v kódu,
                F1 / postranní panel / Předvolby, Ukázat = SpotlightOverlay (a zavření).
"""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEvent, QPointF
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    from PyQt6.QtGui import QImage, QColor, QKeyEvent, QMouseEvent
    import guide
    import main
    import memories
    from test_patch11_maps import wait

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = date(2026, 10, 28)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMemoryDates(unittest.TestCase):

    def test_window_today_first_and_leap_day(self):
        mds = memories.window_month_days(TODAY)
        self.assertEqual(mds[0], "10-28")
        self.assertEqual(sorted(mds), ["10-25", "10-26", "10-27", "10-28", "10-29", "10-30", "10-31"])
        self.assertIn("02-29", memories.window_month_days(date(2026, 3, 1)))
        self.assertIn("01-01", memories.window_month_days(date(2026, 12, 30)))

    def test_anniversary(self):
        self.assertEqual(memories.anniversary(date(2023, 10, 28), TODAY), (3, 0))
        self.assertEqual(memories.anniversary(date(2023, 10, 26), TODAY), (3, -2))
        self.assertIsNone(memories.anniversary(date(2023, 10, 20), TODAY))
        self.assertIsNone(memories.anniversary(date(2026, 10, 27), TODAY), "letošek není vzpomínka")
        self.assertEqual(memories.anniversary(date(2025, 1, 1), date(2026, 12, 30)), (2, 2), "přes přelom roku")
        self.assertEqual(memories.anniversary(date(2024, 2, 29), date(2026, 2, 28)), (2, 0), "29. 2.")

    def test_czech_forms(self):
        self.assertEqual(memories.years_ago_text(1), "Před rokem")
        self.assertEqual(memories.years_ago_text(3), "Před 3 lety")
        self.assertEqual(memories.years_ago_text(5, week=True), "Tento týden před 5 lety")
        self.assertEqual(memories.years_ago_text(1, week=True), "Tento týden před rokem")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestFindMemory(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p2_mem_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.conn = database.get_connection()
        self.conn.execute("INSERT INTO events (id, title) VALUES (1, 'Výlet: Šumava (26. 10. – 29. 10. 2023)')")
        self.n = 0

    def tearDown(self):
        self.conn.close()
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, taken, event=None, city=None, kind="photo", trashed=0, fav=0):
        self.n += 1
        self.conn.execute(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, event_id, place_city, "
            "thumbnail_path, is_trashed, is_favorite) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"C:/x/p{self.n}.jpg", f"p{self.n}.jpg", kind, taken, event, city, f"C:/t/{self.n}.jpg", trashed, fav))
        self.conn.commit()
        return self.n

    def test_exact_day_with_event_title(self):
        ids = [self.add("2023:10:28 09:00:00", 1), self.add("2023-10-28 12:00:00", 1),
               self.add("2023-10-28 18:00:00", 1, fav=1)]
        self.add("2023-10-28 19:00:00", 1, trashed=1)
        self.add("2023-10-28 20:00:00", 1, kind="video")
        self.add("2026-10-28 10:00:00", 1)                     # letošek
        m = memories.find_memory(self.conn, TODAY)
        self.assertEqual(m["kind"], "day")
        self.assertEqual(m["title"], "Před 3 lety · Šumava")
        self.assertEqual(m["ids"], ids, "podle data, bez koše, videí a letoška")
        self.assertIn("3 fotky", m["subtitle"])
        self.assertEqual(m["frames"][0], f"C:/t/{ids[2]}.jpg", "oblíbená je obálka")

    def test_week_fallback_and_nearest_year(self):
        for d in ("2021-10-28", "2021-10-28"):                 # jen 2 přesně z dneška = málo
            self.add(d + " 10:00:00", city="Praha")
        for d in ("2022-10-26", "2022-10-27", "2022-10-31"):
            self.add(d + " 10:00:00", city="Brno")
        for d in ("2019-10-25", "2019-10-26", "2019-10-30"):
            self.add(d + " 10:00:00", city="Ostrava")
        m = memories.find_memory(self.conn, TODAY)
        self.assertEqual(m["kind"], "week")
        self.assertEqual(m["title"], "Tento týden před 4 lety · Brno", "nejbližší rok s ≥ 3 fotkami")

    def test_none_without_enough_photos(self):
        self.add("2023-10-28 10:00:00")
        self.add("2023-10-20 10:00:00")
        self.assertIsNone(memories.find_memory(self.conn, TODAY))

    def test_query_uses_month_day_index(self):
        plan = " ".join(str(r) for r in self.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT m.id FROM media_items m WHERE {database.MONTH_DAY_SQL} IN (?, ?) "
            f"AND m.media_type IN ('photo', 'live_photo') AND {database.visible_media_sql('m')}",
            ("10-28", "10-29")).fetchall())
        self.assertIn("idx_media_md", plan)


def _release(w, x, y):
    pos = QPointF(x, y)
    w.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, pos, w.mapToGlobal(pos),
                                    Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier))


def _img(color):
    img = QImage(64, 40, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


MEMORY = {"kind": "day", "years": 3, "place": "Šumava", "title": "Před 3 lety · Šumava",
          "subtitle": "28. 10. 2023 · 3 fotky", "ids": [4, 5, 6], "frames": []}


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMemoryCard(unittest.TestCase):

    def setUp(self):
        self.card = memories.MemoryCard()
        self.card.resize(800, memories.CARD_HEIGHT)
        self.addCleanup(self.card.deleteLater)
        self.hits = {"open": [], "dismiss": 0}
        self.card.opened.connect(self.hits["open"].append)
        self.card.dismissed.connect(lambda: self.hits.__setitem__("dismiss", self.hits["dismiss"] + 1))

    def test_close_vs_open(self):
        self.card.set_memory(MEMORY, [_img("#C04020")])
        c = self.card.close_rect().center()
        _release(self.card, c.x(), c.y())
        self.assertEqual(self.hits, {"open": [], "dismiss": 1})
        _release(self.card, 100, 100)
        self.assertEqual(self.hits["open"], [[4, 5, 6]])
        self.assertFalse(self.card.grab().toImage().isNull())

    def test_frames_cycle_only_when_visible(self):
        self.card.set_memory(MEMORY, [_img("#C04020"), _img("#2040C0")])
        self.assertFalse(self.card._timer.isActive(), "skrytá karta netiká")
        self.assertTrue(self.card.next_frame())
        self.assertEqual(self.card.index, 1)
        self.card.set_memory(MEMORY, [_img("#C04020")])
        self.assertFalse(self.card.next_frame(), "jeden snímek se nestřídá")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestGuideContent(unittest.TestCase):

    def test_chapters(self):
        self.assertEqual([c.id for c in guide.CHAPTERS], ["shortcuts", "map", "faces", "search"])
        ids = [t.id for t in guide.all_tips()]
        self.assertEqual(len(ids), len(set(ids)), "id tipů jsou jedinečná")
        for t in guide.all_tips():
            self.assertTrue(t.title and t.body, t.id)
            if t.anchor:
                self.assertIsNotNone(t.page, f"{t.id}: kotva potřebuje stránku")

    def test_shortcuts_match_key_handlers(self):
        """Zkratka v průvodci = obsluha klávesy v kódu (jinak by průvodce lhal)."""
        for tip in guide.all_tips():
            if not tip.source:
                continue
            fname, keys = tip.source
            with open(os.path.join(HERE, fname), encoding="utf-8") as f:
                text = f.read()
            for k in keys:
                needle = f"Qt.Key.{k}" if fname.endswith(".py") else f"'{k}'"
                self.assertIn(needle, text, f"{tip.id}: {needle} v {fname}")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowP2(unittest.TestCase):

    def setUp(self):
        from test_patch9 import TempEnv
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.saved = []
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config", side_effect=lambda c: self.saved.append(dict(c)))):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.w.config["memories_dismissed"] = ""
        self.w.config["show_memories"] = True
        self.w.resize(1280, 800)
        self.w.show()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        wait(30)

    # ── Vzpomínky ──
    def test_card_visibility_rules(self):
        self.w._on_memory_ready((dict(MEMORY), [_img("#C04020")]))
        self.assertTrue(self.w.memory_box.isVisible())
        self.w._set_filter("favorites")
        self.assertFalse(self.w.memory_box.isVisible(), "jen ve Vše")
        self.w._set_filter("all")
        self.w._search_query = "pes"
        self.w._apply_filter_and_sort()
        self.assertFalse(self.w.memory_box.isVisible(), "ne při hledání")
        self.w._search_query = ""
        self.w._apply_filter_and_sort()
        self.assertTrue(self.w.memory_box.isVisible())

    def test_dismiss_for_today(self):
        self.w._on_memory_ready((dict(MEMORY), []))
        with mock.patch.object(main, "show_toast"):
            self.w.memory_card.dismissed.emit()
        self.assertFalse(self.w.memory_box.isVisible())
        self.assertEqual(self.saved[-1]["memories_dismissed"], date.today().isoformat())
        self.w.config["memories_dismissed"] = "2001-01-01"      # jiný den -> zase vidět
        self.w._sync_memory_card()
        self.assertTrue(self.w.memory_box.isVisible())

    def test_disabled_in_settings(self):
        self.w.config["show_memories"] = False
        with mock.patch.object(main.memories, "MemoryJob") as job:
            self.w._refresh_memory()
        job.assert_not_called()
        self.assertFalse(self.w.memory_box.isVisible())
        self.assertTrue(self.w._memory_midnight.isActive(), "po půlnoci se přepočítá")

    def test_card_opens_viewer(self):
        with mock.patch.object(self.w, "open_viewer_for_ids") as opener:
            self.w.memory_card.opened.disconnect()
            self.w.memory_card.opened.connect(self.w.open_viewer_for_ids)
            self.w._on_memory_ready((dict(MEMORY), []))
            self.w.memory_card.opened.emit([4, 5, 6])
        opener.assert_called_once_with([4, 5, 6])

    # ── Průvodce ──
    def test_every_anchor_exists(self):
        for tip in guide.all_tips():
            if tip.anchor:
                self.assertIsNotNone(guide.find_anchor(self.w, tip.anchor, visible_only=False), tip.id)

    def test_open_guide_once_and_switch_chapter(self):
        page = self.w.open_guide()
        self.assertIs(self.w.stack.currentWidget(), page)
        self.assertEqual(page.current_chapter(), "shortcuts")
        self.assertEqual(len(page.cards), len(guide.chapter("shortcuts").tips))
        again = self.w.open_guide("faces")
        self.assertIs(again, page, "druhá stránka nevznikne")
        self.assertEqual(page.current_chapter(), "faces")
        self.assertEqual(self.w.nav.depth(), 1)

    def test_f1_and_sidebar_open_guide(self):
        self.w._help_shortcut.activated.emit()
        self.assertIsInstance(self.w.stack.currentWidget(), guide.GuidePage)
        self.w.nav.reset()
        self.w.btn_guide.click()
        self.assertIsInstance(self.w.stack.currentWidget(), guide.GuidePage)

    def test_show_tip_spotlights_anchor_and_closes(self):
        page = self.w.open_guide("search")
        card = next(c for c in page.cards if c.tip.id == "search_text")
        card.btn_show.click()
        wait(60)
        self.assertIs(self.w.stack.currentWidget(), self.w.page_photos)
        spot = self.w._spotlight
        self.assertIsNotNone(spot)
        self.assertIs(spot.target, self.w.search_bar)
        hole = spot.hole()
        self.assertTrue(hole.contains(QPointF(self.w.search_bar.mapTo(self.w.centralWidget(),
                                                                       self.w.search_bar.rect().center()))))
        self.assertFalse(spot.callout_rect().intersects(hole), "bublina nezakrývá cíl")
        spot.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        wait(400)
        self.assertIsNone(self.w._spotlight)

    def test_hidden_anchor_falls_back_to_toast(self):
        tip = next(t for t in guide.all_tips() if t.id == "search_memories")
        self.w._memory = None
        self.w._sync_memory_card()
        with mock.patch.object(main, "show_toast") as toast:
            self.assertIsNone(self.w._spotlight_tip(tip))
        toast.assert_called_once()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSettingsGuideEntry(unittest.TestCase):

    def test_settings_button_requests_guide(self):
        from test_patch11_settings import TempLibrary
        import settings_dialog
        lib = TempLibrary()
        self.addCleanup(lib.close)
        dlg = settings_dialog.SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        self.assertFalse(dlg.guide_requested)
        self.assertTrue(dlg.chk_memories.isChecked())
        dlg.btn_guide.click()
        self.assertTrue(dlg.guide_requested)
        self.assertEqual(dlg.result(), 0, "Předvolby se zavřou bez uložení")


if __name__ == "__main__":
    unittest.main()
