"""Kairos Polish P4 – Live Photo při najetí a kontextový odstín (Přizpůsobit obsahu).

Spuštění (ve složce projektu):   python -m pytest test_polish_p4.py -v

Pokrývá:
  live_hover.py – jen Live Photo, prodleva, pás z StripManager (i rozpracovaný), postupné dekódování
                  (jeden snímek za tik), prolnutí, zastavení (odjetí / posun / skrytí), LRU 4 fotek,
                  delegát kreslí snímek v dlaždici.
  tint.py       – převládající odstín v OKLCH, šedá = bez odstínu, výpočet na pozadí s uložením
                  do events.tint a přepočet po změně obálky.
  tokens        – context_glow: AA kontrast, vypnutí, QSS zvýraznění se nemění.
  použití       – záře karty, hlavička události, Cesta na mapě v odstínu a návrat, Předvolby.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QBuffer, QByteArray, QIODevice
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database

if HAS_QT:
    import json
    from PyQt6.QtGui import QColor, QImage
    import live_hover
    import main
    import scrub_strip
    import tint
    import tokens
    from events_view import EventCard
    from test_batch6_map import TRIP
    from test_patch11_maps import ShownMapPage, wait
    from ui_nav import DetailPage


def _img(color, w=64, h=48):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def _jpeg(color):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    _img(color, 96, 72).save(buf, "JPG")
    return bytes(ba)


def _hue(hx):
    return tokens.hex_to_oklch(hx)[2]


def _hue_dist(a, b):
    return abs(((a - b + 540) % 360) - 180)


# ── odstín ────────────────────────────────────────────────────────────────────

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestDominantTint(unittest.TestCase):

    def test_hue_of_colorful_images(self):
        for color in ("#C83C28", "#2860C8", "#3C9A4A"):
            t = tint.dominant_tint(_img(color))
            self.assertTrue(t, color)
            self.assertLess(_hue_dist(_hue(t), _hue(color)), 12, (color, t))

    def test_grey_and_empty_have_no_tint(self):
        self.assertEqual(tint.dominant_tint(_img("#808080")), "")
        self.assertEqual(tint.dominant_tint(_img("#050505")), "")
        self.assertEqual(tint.dominant_tint(QImage()), "")

    def test_dominant_wins_over_small_patch(self):
        img = _img("#2860C8", 48, 48)
        for y in range(10):
            for x in range(10):
                img.setPixelColor(x, y, QColor("#C83C28"))
        self.assertLess(_hue_dist(_hue(tint.dominant_tint(img)), _hue("#2860C8")), 12)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestContextGlow(unittest.TestCase):

    def setUp(self):
        self.addCleanup(tokens.set_adaptive, tokens.adaptive())
        tokens.set_adaptive(True)

    def test_blend_keeps_contrast_and_qss_accent(self):
        base = tokens.palette("dark")["accent"]
        warm = tokens.context_glow("#C8783C", "dark")
        self.assertNotEqual(warm, base)
        self.assertGreaterEqual(tokens._min_contrast(warm, "dark"), 4.5, "AA proti tmavému pozadí")
        h_base, h_warm, h_tint = _hue(base), _hue(warm), _hue("#C8783C")
        self.assertLess(_hue_dist(h_warm, h_base), _hue_dist(h_tint, h_base), "jen posun, ne převzetí")
        self.assertEqual(tokens.palette("dark")["accent"], base, "QSS zvýraznění beze změny")
        self.assertGreaterEqual(tokens._min_contrast(tokens.context_glow("#C8783C", "light"), "light"), 4.5)

    def test_off_or_no_tint_returns_user_accent(self):
        base = tokens.accent("dark")["accent"]
        self.assertEqual(tokens.context_glow("", "dark"), base)
        self.assertEqual(tokens.context_glow("nesmysl", "dark"), base)
        tokens.set_adaptive(False)
        self.assertEqual(tokens.context_glow("#C8783C", "dark"), base)

    def test_configure_reads_toggle(self):
        tokens.configure({"accent": tokens.accent_key(), "adaptive_accent": False})
        self.assertFalse(tokens.adaptive())


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestTintStorage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p4_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.addCleanup(setattr, database, "DB_PATH", self._saved)
        database.init_db()
        conn = database.get_connection()
        conn.execute("INSERT INTO events (id, title) VALUES (1, 'Moře'), (2, 'Mlha')")
        conn.commit()
        conn.close()
        self.red, self.grey = os.path.join(self.tmp, "red.png"), os.path.join(self.tmp, "grey.png")
        _img("#C83C28").save(self.red)
        _img("#7A7A7A").save(self.grey)

    def cards(self, red=None):
        return [{"id": 1, "cover": {"thumb": red or self.red}}, {"id": 2, "cover": {"thumb": self.grey}}]

    def test_compute_once_store_and_recompute_on_new_cover(self):
        conn = database.get_connection()
        cards = self.cards()
        todo = tint.stale_items(conn, cards)
        self.assertEqual(sorted(e for e, _ in todo), [1, 2])
        got = []
        job = tint.TintJob(todo, database.get_connection)
        job.signals.done.connect(got.append)
        job.run()
        self.assertTrue(got[0][1].startswith("#"))
        self.assertEqual(got[0][2], "", "šedá obálka = bez odstínu, ale spočítáno")
        cards = self.cards()
        self.assertEqual(tint.stale_items(conn, cards), [], "podruhé se nepočítá")
        self.assertEqual(cards[0]["tint"], got[0][1])
        self.assertEqual(cards[1]["tint"], "")
        blue = os.path.join(self.tmp, "blue.png")
        _img("#2860C8").save(blue)
        self.assertEqual(tint.stale_items(conn, self.cards(blue)), [(1, blue)], "jiná obálka = přepočet")
        conn.close()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestTintedSurfaces(unittest.TestCase):

    def setUp(self):
        self.addCleanup(tokens.set_adaptive, tokens.adaptive())
        tokens.set_adaptive(True)

    def test_event_card_glow(self):
        card = EventCard({"id": 3, "title": "Moře", "location": "", "start": None, "end": None,
                          "count": 4, "cover": None, "tint": "#C8783C"})
        self.addCleanup(card.deleteLater)
        self.assertIsNotNone(card.glow_color())
        card.event["tint"] = ""
        self.assertIsNone(card.glow_color(), "šedá obálka = bílý lem")
        card.event["tint"] = "#C8783C"
        tokens.set_adaptive(False)
        self.assertIsNone(card.glow_color())

    def test_detail_header_ambient(self):
        page = DetailPage("Moře")
        self.addCleanup(page.deleteLater)
        page.resize(800, 300)
        page.show()
        wait(20)
        plain = page.header.grab().toImage().pixelColor(4, 10)
        page.set_ambient(tokens.context_glow("#C8783C", "dark"))
        wait(20)
        tinted = page.header.grab().toImage().pixelColor(4, 10)
        self.assertNotEqual(plain, tinted)
        page.set_ambient(None)
        self.assertIsNone(page.header.ambient)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMainWindowTint(unittest.TestCase):

    def setUp(self):
        from test_patch9 import TempEnv
        self.env = TempEnv("setUp")
        self.env.setUp()
        self.addCleanup(self.env.tearDown)
        self.addCleanup(tokens.set_adaptive, tokens.adaptive())
        for p in (mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                  mock.patch.object(main, "save_config")):
            p.start()
            self.addCleanup(p.stop)
        self.w = main.MainWindow()
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))
        tokens.set_adaptive(True)

    def test_journey_gets_event_tint_and_qss_untouched(self):
        self.env.x("INSERT INTO events (id, title, tint) VALUES (5, 'Moře', '#C8783C')")
        calls = []
        self.w._map_loaded = True
        data = {"id": 5, "stops": [{}, {}]}
        with mock.patch.object(self.w.map_view.page(), "runJavaScript", side_effect=lambda c, *a: calls.append(c)), \
                mock.patch.object(main.journey, "build_journey", return_value=dict(data)), \
                mock.patch.object(main.theme, "apply") as reapply:
            self.assertTrue(self.w.play_journey(5))
        sent = json.loads(calls[-1][calls[-1].index("(", 30) + 1:-2])
        self.assertEqual(sent["accent"], tokens.context_glow("#C8783C", "dark"))
        reapply.assert_not_called()
        tokens.set_adaptive(False)
        calls.clear()
        with mock.patch.object(self.w.map_view.page(), "runJavaScript", side_effect=lambda c, *a: calls.append(c)), \
                mock.patch.object(main.journey, "build_journey", return_value=dict(data)):
            self.w.play_journey(5)
        self.assertNotIn("accent", calls[-1], "Přizpůsobit obsahu vypnuto = zvýraznění uživatele")

    def test_event_tints_arrive_without_restyle(self):
        self.w._event_cards = [{"id": 9, "title": "X", "location": "", "start": None, "end": None, "count": 1,
                                "cover": None}]
        self.w._render_event_cards(self.w._event_cards)
        with mock.patch.object(main.theme, "apply") as reapply:
            self.w._on_event_tints(self.w._events_gen, {9: "#2860C8"})
        self.assertEqual(self.w.event_cards[0].event["tint"], "#2860C8")
        self.assertIsNotNone(self.w.event_cards[0].glow_color())
        reapply.assert_not_called()


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapJourneyTint(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((900, 600))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code):
        return self.m.js(code, 5000)

    def test_tint_during_journey_then_base_accent(self):
        self.js("reduceMotion = true; setAccent('#8B7CF6'); 0")
        trip = dict(TRIP, accent="#E0703A")
        self.assertTrue(self.js(f"playJourney({json.dumps(trip)})"))
        self.assertEqual(self.js("accent()"), "#E0703A")
        self.assertEqual(self.js("map.getPaintProperty('j-done', 'line-color')"), "#E0703A")
        self.assertTrue(self.js("setAccent('#3FA36B')"), "změna v Předvolbách během Cesty")
        self.assertEqual(self.js("accent()"), "#E0703A", "běžící Cesta drží svůj odstín")
        self.js("window.endJourney(); 0")
        self.assertEqual(self.js("accent()"), "#3FA36B", "po konci zvýraznění uživatele")
        self.assertTrue(self.js(f"playJourney({json.dumps(dict(TRIP, accent='red; x'))})"))
        self.assertEqual(self.js("accent()"), "#3FA36B", "neplatný odstín se ignoruje")
        self.js("window.endJourney(); setAccent('#8B7CF6'); 0")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestSettingsAdaptive(unittest.TestCase):

    def test_toggle_saved(self):
        from test_patch11_settings import TempLibrary
        import config as cfg
        import settings_dialog
        lib = TempLibrary()
        self.addCleanup(lib.close)
        dlg = settings_dialog.SettingsDialog()
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(dlg.chk_adaptive.isChecked())
        self.assertEqual(dlg.chk_adaptive.text(), "Přizpůsobit obsahu")
        dlg.chk_adaptive.setChecked(False)
        dlg.save_and_close()
        self.assertFalse(cfg.load_config()["adaptive_accent"])


# ── Live Photo při najetí ─────────────────────────────────────────────────────

class _Strips:
    """Jako scrub_strip.StripManager: request()/get() vrací připravený (nebo rozpracovaný) pás."""

    def __init__(self):
        self.strips = {}
        self.requests = []

    def request(self, source):
        self.requests.append(source)
        return self.strips.get(source)

    def get(self, source):
        return self.strips.get(source)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestLiveHover(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p4_live_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.host = main.QWidget()
        self.host.resize(700, 500)
        self.addCleanup(self.host.deleteLater)
        self.grid = main.AppleGridView(mock.Mock(), self.host)
        self.grid.resize(700, 500)
        self.grid.set_cell_size(160)
        self.strips = _Strips()
        self.live = self.grid.live_preview
        self.live._strips = self.strips
        self.live._allowed = lambda: True
        recs = []
        for i in range(6):
            mov = os.path.join(self.tmp, f"IMG_{i}.MOV")
            open(mov, "wb").close()
            strip = scrub_strip.ScrubStrip(mov, 3000)
            for k in range(20):
                strip.add(k * 83, _jpeg("#E03020"))
            strip.complete = True
            self.strips.strips[mov] = strip
            recs.append((i + 1, f"IMG_{i}.HEIC", os.path.join(self.tmp, f"IMG_{i}.HEIC"), "live_photo", mov,
                         "", "2024-01-01 10:00:00", 0))
        recs.append((99, "a.jpg", os.path.join(self.tmp, "a.jpg"), "photo", None, "", "2024-01-01", 0))
        self.grid.photo_model.set_records(recs)
        self.host.show()
        wait(30)

    def test_pick_indices(self):
        self.assertEqual(live_hover.pick_indices(5), [0, 1, 2, 3, 4])
        self.assertEqual(len(live_hover.pick_indices(100)), live_hover.MAX_FRAMES)

    def test_only_live_photos_after_delay(self):
        self.live.hover(6)                                   # obyčejná fotka
        self.assertFalse(self.live.is_active())
        self.live.hover(0)
        self.assertTrue(self.live._delay.isActive())
        self.assertEqual(self.live._delay.interval(), 350)
        self.assertEqual(self.strips.requests, [], "pás až po prodlevě")
        with mock.patch.object(live_hover.motion, "reduced", return_value=True):
            self.live.hover(1)
        self.assertFalse(self.live.is_active(), "omezené animace = bez náhledu")

    def test_plays_decodes_progressively_and_stops(self):
        self.live.hover(0)
        self.live._begin()
        self.assertEqual(self.live.playing_row, 0)
        pm, opacity = self.live.frame_for(0)
        self.assertLess(opacity, 1.0, "prolnutí ze statického snímku")
        self.assertEqual(self.live.cached_frames(), 1, "jeden dekód za tik")
        for _ in range(4):
            self.live._advance()
        self.assertEqual(self.live.cached_frames(), 5)
        self.assertEqual(self.live.frame_for(0)[1], 1.0)
        self.assertIsNone(self.live.frame_for(1))
        cell = self.grid.visualRect(self.grid.photo_model.index(0, 0))
        im = self.grid.viewport().grab().toImage()
        dpr = im.devicePixelRatio()                          # snímek je ve fyzických pixelech
        color = im.pixelColor(round(cell.center().x() * dpr), round(cell.center().y() * dpr))
        self.assertGreater(color.red(), 150, "snímek Live Photo v dlaždici")
        self.assertLess(color.blue(), 90)
        self.grid.verticalScrollBar().valueChanged.emit(10)  # posun mřížky
        self.assertFalse(self.live.is_active())
        self.assertIsNone(self.live.frame_for(0))

    def test_leave_and_hide_stop(self):
        self.grid._set_hover_row(0)                          # entered -> mřížka předá řádek náhledu
        self.assertTrue(self.live._delay.isActive())
        self.live._begin()
        self.assertEqual(self.live.playing_row, 0)
        self.grid.leaveEvent(main.QEvent(main.QEvent.Type.Leave))
        self.assertEqual(self.live.playing_row, -1)
        self.live.hover(1)
        self.live._begin()
        self.grid.hide()
        self.assertFalse(self.live.is_active())

    def test_waits_for_strip_in_progress(self):
        mov = self.grid.photo_model.records[2][4]
        building = scrub_strip.ScrubStrip(mov, 3000)
        self.strips.strips[mov] = building
        self.live.hover(2)
        self.live._begin()
        self.assertTrue(self.live._poll.isActive(), "pás se teprve vytváří")
        self.assertEqual(self.live.playing_row, -1)
        for k in range(10):
            building.add(k * 83, _jpeg("#E03020"))
        building.complete = True
        self.live._check_strip()
        self.assertEqual(self.live.playing_row, 2)
        self.assertFalse(self.live._poll.isActive())

    def test_lru_keeps_four_photos(self):
        for row in range(6):
            self.live.hover(row)
            self.live._begin()
            self.live._advance()
        self.assertEqual(len(self.live.cache), live_hover.CACHE_ENTRIES)
        newest = {k[0] for k in self.live.cache}
        self.assertIn(self.grid.photo_model.records[5][4], newest)
        self.assertNotIn(self.grid.photo_model.records[0][4], newest)


if __name__ == "__main__":
    unittest.main()
