"""Regresní testy pro Patch 11 – dávka D: ambientní střídání fotek na kartách událostí.

Spuštění (ve složce projektu):   python -m pytest test_patch11_events.py -v

Pokrývá:
  Výběr     – obálka první, k-center přes čas / GPS (každá část výletu), bez téměř stejných
              snímků (pHash), chybějící soubory vynechány, malá událost = méně snímků.
  Karta     – obálka vždy první, prolnutí 600 ms, ostrá obálka nepřebije zobrazený snímek,
              snímky zmenšené na velikost karty.
  Časovač   – jen viditelné karty, nejvýš dvě prolínání naráz, rozfázované periody, pauza
              mimo stránku / v neaktivním okně, vypnuté animace systému = statické karty,
              další snímek se vyžádá dopředu.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QSize
    from PyQt6.QtGui import QImage, QPixmap, QColor
    from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    import events_view
    from events_view import EventCard, AmbientCycler, BentoLayout, select_ambient_frames


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def spread_hash(i):
    """Navzájem vzdálené pHashe (Fibonacciho hašování) – po sobě jdoucí čísla by byla „duplicity“."""
    return f"{((i + 1) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF:016x}"


def pixmap(color, w=1600, h=1000):
    p = QPixmap(w, h)
    p.fill(QColor(*color))
    return p


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestAmbientSelection(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p11e_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.conn = database.get_connection()
        self.conn.execute("INSERT INTO events (id, title, start_date, end_date) VALUES "
                          "(1, 'Výlet', '2026-07-01 08:00:00', '2026-07-03 20:00:00')")
        self.n = 0

    def tearDown(self):
        self.conn.close()
        database.DB_PATH = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, date, lat=None, lon=None, phash=None, sharp=50.0, exists=True, media_type="photo"):
        self.n += 1
        path = os.path.join(self.tmp, f"p{self.n}.jpg")
        if exists:
            open(path, "wb").close()
        cur = self.conn.execute(
            "INSERT INTO media_items (file_path, file_name, media_type, date_taken, latitude, longitude, "
            "phash, sharpness_score, event_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (path, os.path.basename(path), media_type, date, lat, lon, phash, sharp))
        self.conn.commit()
        return cur.lastrowid

    def test_cover_first_then_spread_over_trip(self):
        days = {}
        for day in (1, 2, 3):
            for hour in (9, 10, 11, 12):
                mid = self.add(f"2026-07-0{day} {hour:02d}:00:00", phash=spread_hash(day * 16 + hour))
                days[mid] = day
        cover_id = next(m for m, d in days.items() if d == 1)
        cover = {"media_id": cover_id, "path": "cover", "thumb": None, "focus": (0.5, 0.5)}
        frames = select_ambient_frames(self.conn, 1, cover, k=3)
        self.assertEqual(len(frames), 3)
        self.assertIs(frames[0], cover, "karta začíná obálkou, kterou už ukazuje")
        self.assertEqual(sorted(days[f["media_id"]] for f in frames), [1, 2, 3], "každý den výletu")

    def test_gps_spread_when_time_is_flat(self):
        here = [self.add("2026-07-01 10:00:00", 49.0, 14.0, phash=spread_hash(i)) for i in range(4)]
        far = self.add("2026-07-01 10:00:00", 49.5, 14.6, phash="00000000ffffffff")
        frames = select_ambient_frames(self.conn, 1, None, k=2)
        ids = [f["media_id"] for f in frames]
        self.assertIn(far, ids, "jiné místo výletu")
        self.assertTrue(set(ids) & set(here))

    def test_near_duplicates_never_both(self):
        a = self.add("2026-07-01 09:00:00", phash="ffff0000ffff0000")
        b = self.add("2026-07-03 19:00:00", phash="ffff0000ffff0001")     # Hamming 1
        c = self.add("2026-07-02 12:00:00", phash="0000ffff0000ffff")
        frames = select_ambient_frames(self.conn, 1, None, k=4)
        ids = {f["media_id"] for f in frames}
        self.assertFalse({a, b} <= ids, "téměř stejný snímek nepatří do střídání")
        self.assertIn(c, ids)

    def test_missing_files_and_videos_skipped(self):
        self.add("2026-07-01 09:00:00", phash="1" * 16)
        gone = self.add("2026-07-02 09:00:00", phash="2" * 16, exists=False)
        clip = self.add("2026-07-03 09:00:00", phash="3" * 16, media_type="video")
        ids = {f["media_id"] for f in select_ambient_frames(self.conn, 1, None, k=4)}
        self.assertNotIn(gone, ids)
        self.assertNotIn(clip, ids)
        self.assertEqual(len(ids), 1, "malá událost = méně snímků (karta zůstane statická)")

    def test_sharp_faces_preferred_among_equals(self):
        self.add("2026-07-01 09:00:00", phash="a" * 16, sharp=100)
        blurry = self.add("2026-07-03 20:00:00", phash="b" * 16, sharp=1)
        sharp = self.add("2026-07-03 20:00:00", phash="c" * 16, sharp=100)
        ids = [f["media_id"] for f in select_ambient_frames(self.conn, 1, None, k=2)]
        self.assertIn(sharp, ids)
        self.assertNotIn(blurry, ids)


def make_card(eid, parent=None, frames=3):
    ev = {"id": eid, "title": f"Událost {eid}", "location": "", "start": "2026-07-01", "end": "2026-07-02",
          "count": 10, "cover": {"media_id": eid * 100, "path": f"/c/{eid}.jpg", "thumb": None, "focus": (0.5, 0.5)}}
    card = EventCard(ev, parent)
    card.set_cover(pixmap((200, 0, 0), 800, 500), full_res=True)
    card.set_frames([{"media_id": eid * 100 + i, "path": f"/f/{eid}_{i}.jpg", "focus": (0.5, 0.5)}
                     for i in range(1, frames)])
    return card


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestAmbientCard(unittest.TestCase):

    def test_cover_always_first(self):
        card = make_card(1)
        self.assertEqual(card.frames[0]["path"], "/c/1.jpg")
        card.set_frames([{"path": "/f/x.jpg"}, {"path": "/c/1.jpg"}])
        self.assertEqual([f["path"] for f in card.frames], ["/c/1.jpg", "/f/x.jpg"])
        self.assertTrue(card.has_frame(0))

    def test_fade_to_next_frame(self):
        card = make_card(1)
        card.resize(400, 250)
        self.assertFalse(card.show_frame(1), "nenačtený snímek se nepřepne")
        card.set_frame_pixmap(1, pixmap((0, 200, 0)))
        self.assertTrue(card.show_frame(1))
        self.assertTrue(card.is_fading())
        card.grab()                              # kreslení během prolnutí nespadne
        wait(events_view.AMBIENT_FADE_MS + 250)
        self.assertFalse(card.is_fading())
        self.assertEqual(card.frame_index, 1)
        self.assertEqual(card.next_frame_index(), 2)

    def test_late_full_res_cover_does_not_replace_shown_frame(self):
        card = make_card(1)
        card.set_frame_pixmap(1, pixmap((0, 200, 0)))
        card.show_frame(1, animate=False)
        shown = card._pixmap.cacheKey()
        card.set_cover(pixmap((0, 0, 200)), full_res=True)
        self.assertEqual(card._pixmap.cacheKey(), shown)
        self.assertTrue(card.has_frame(0))

    def test_frames_scaled_to_card(self):
        card = make_card(1)
        card.resize(400, 250)
        card.set_frame_pixmap(1, pixmap((0, 200, 0), 4000, 3000))
        stored = card._frame_pixmaps[1]
        limit = 1.25 * card.devicePixelRatioF()
        self.assertLessEqual(min(stored.width() / 400, stored.height() / 250), limit + 0.01)
        self.assertGreaterEqual(stored.width(), 400)
        self.assertGreaterEqual(stored.height(), 250)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestAmbientCycler(unittest.TestCase):

    def setUp(self):
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid = QWidget()
        self.layout = BentoLayout(self.grid)
        self.scroll.setWidget(self.grid)
        self.cards = [make_card(i, self.grid) for i in range(1, 13)]
        for c in self.cards:
            self.layout.addWidget(c)
        self.scroll.resize(700, 500)
        self.scroll.show()
        _app.processEvents()
        self.now = [1000.0]
        self.requested = []

        def image_fn(path):
            self.requested.append(path)
            return pixmap((0, 150, 0), 600, 400).toImage()
        self.cycler = AmbientCycler(self.scroll.viewport(), lambda: self.cards, image_fn,
                                    clock=lambda: self.now[0])
        self.cycler.animations = True
        self.addCleanup(self._close)

    def _close(self):
        self.cycler.timer.stop()
        self.scroll.hide()
        self.scroll.deleteLater()
        _app.processEvents()

    def _run(self, advance=0.0):
        with mock.patch.object(self.cycler, "should_run", return_value=True):
            self.cycler.sync()                    # rozfázuje karty od „teď“
            self.now[0] += advance
            self.cycler.tick()

    def test_only_visible_cards_and_at_most_two_fading(self):
        visible = list(self.cycler.visible_cards())
        self.assertTrue(visible)
        self.assertLess(len(visible), len(self.cards), "karty pod okrajem se nestřídají")
        self._run(advance=100.0)                  # všechny karty „na řadě“
        fading = [c for c in self.cards if c.is_fading()]
        self.assertEqual(len(fading), events_view.AMBIENT_MAX_FADING)
        self.assertTrue(all(c in visible for c in fading))
        hidden = [c for c in self.cards if c not in visible]
        self.assertTrue(all(not c.is_fading() for c in hidden))
        self.assertTrue(all(f"/f/{c.event['id']}_1.jpg" not in self.requested for c in hidden),
                        "neviditelné karty nic nedekódují")

    def test_next_frame_requested_ahead_of_switch(self):
        self._run()                               # nikdo není na řadě, ale snímky se chystají
        self.assertFalse(any(c.is_fading() for c in self.cards))
        first = next(self.cycler.visible_cards())
        self.assertTrue(first.has_frame(1))
        self.assertIn(f"/f/{first.event['id']}_1.jpg", self.requested)

    def test_staggered_phases_and_periods(self):
        self.cycler.restart_phases()
        switches = [c.next_switch for c in self.cards]
        self.assertEqual(len(set(round(s, 3) for s in switches)), len(switches), "karty se nestřídají naráz")
        periods = {events_view.ambient_period_ms(c.event["id"]) for c in self.cards}
        self.assertTrue(all(3000 <= p < 4000 for p in periods))
        self.assertGreater(len(periods), 1)

    def test_pauses_when_page_hidden_or_window_inactive(self):
        with mock.patch.object(self.cycler, "should_run", return_value=True):
            self.cycler.sync()
        self.assertTrue(self.cycler.is_running())
        with mock.patch.object(self.cycler, "should_run", return_value=False):
            self.cycler.sync()
        self.assertFalse(self.cycler.is_running())
        self.scroll.hide()
        self.assertFalse(self.cycler.should_run(), "skrytá stránka")
        self.scroll.show()
        win = self.scroll.window()
        with mock.patch.object(win, "isActiveWindow", return_value=False):
            self.assertFalse(self.cycler.should_run(), "neaktivní okno")
        with mock.patch.object(win, "isActiveWindow", return_value=True), \
                mock.patch.object(win, "isMinimized", return_value=True):
            self.assertFalse(self.cycler.should_run(), "minimalizované okno")

    def test_tick_stops_timer_when_conditions_fail(self):
        with mock.patch.object(self.cycler, "should_run", return_value=True):
            self.cycler.sync()
        with mock.patch.object(self.cycler, "should_run", return_value=False):
            self.cycler.tick()
        self.assertFalse(self.cycler.is_running())

    def test_system_animations_off_keeps_cards_static(self):
        self.cycler.animations = False
        self.assertFalse(self.cycler.should_run())
        self.cycler.sync()
        self.assertFalse(self.cycler.is_running())

    def test_windows_animation_setting_is_read(self):
        if sys.platform != "win32":
            self.skipTest("jen Windows")
        self.assertIsInstance(events_view.system_animations_enabled(), bool)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestAmbientWiring(unittest.TestCase):

    def test_stale_frames_are_ignored(self):
        import main
        win = mock.MagicMock()
        win._events_gen = 2
        card = make_card(7)
        win.event_cards = [card]
        main.MainWindow._on_ambient_frames(win, 1, {7: [{"path": "/f/new.jpg"}]})
        self.assertNotIn("/f/new.jpg", [f["path"] for f in card.frames])
        main.MainWindow._on_ambient_frames(win, 2, {7: [{"path": "/f/new.jpg"}]})
        self.assertEqual([f["path"] for f in card.frames], ["/c/7.jpg", "/f/new.jpg"])
        win._ambient.sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
