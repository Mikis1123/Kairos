"""Trips v3: basecamp / hub model without home assumptions, lodging != home, the
algorithm-version migration, and the clicked(bool) file-dialog buttons (GPX, music)."""
import sqlite3
import sys
import unittest
from datetime import datetime as _dt, timedelta as _td
from unittest import mock

import trips

try:
    from PyQt6.QtWidgets import QApplication
    from test_patch6 import WindowEnv           # sets up QtWebEngine before the QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    import main
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False
    WindowEnv = unittest.TestCase

# Šumava, as in the user's library: a cottage in Kolinec, day trips around it
KOLINEC = (49.229, 13.378)
SUSICE = (49.241, 13.521)
KASPERSKE = (49.166, 13.570)
HORAZDOVICE = (49.321, 13.709)
FRAUENAU = (49.064, 13.395)
ZELEZNA_RUDA = (49.172, 13.196)
PRAHA = (50.08, 14.43)
VIENNA = (48.20, 16.37)
SALZBURG = (47.80, 13.04)


class Lib:
    def __init__(self):
        self.items, self._id = [], 0

    def add(self, when, place, city="X"):
        self._id += 1
        self.items.append({"id": self._id, "dt": when, "lat": place[0] if place else None,
                           "lon": place[1] if place else None, "city": city})
        return self._id

    def sumava_week(self, year=2026):
        """Mirrors the real library: no home photos at all, 4 of 6 nights evidenced at the cottage,
        one day trip whose last photo is at 18:20 somewhere else (a guess, not a night)."""
        d = lambda day, h, m=0: _dt(year, 7, day, h, m)
        for h in (12, 14, 16):
            self.add(d(6, h), SUSICE, "Susice")
        self.add(d(6, 19), KOLINEC, "Kolinec")                 # evening at the cottage
        self.add(d(7, 9, 30), KOLINEC, "Kolinec")              # morning at the cottage
        for h in (11, 13, 15):
            self.add(d(7, h), KASPERSKE, "Kasperske Hory")
        self.add(d(7, 19, 45), KOLINEC, "Kolinec")
        self.add(d(8, 14, 30), HORAZDOVICE, "Horazd'ovice")    # nothing in the evening
        self.add(d(9, 21, 10), KOLINEC, "Kolinec")             # night photo
        for h in (13, 15):
            self.add(d(10, h), FRAUENAU, "Frauenau")
        self.add(d(10, 18, 20), FRAUENAU, "Frauenau")          # weak evening guess elsewhere
        for h in (14, 16):
            self.add(d(11, h), ZELEZNA_RUDA, "Zelezna Ruda")
        self.add(_dt(year, 7, 12, 0, 47), KOLINEC, "Kolinec")   # 00:47 = the night of the 11th

    def home_evenings(self, start, days, skip=()):
        for i in range(days):
            day = start + _td(days=i)
            if day.date() not in skip:
                self.add(day.replace(hour=21, minute=10), PRAHA, "Praha")


def _db(lib):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, location_name TEXT, "
                 "start_date TEXT, end_date TEXT, item_count INTEGER DEFAULT 0, cover_media_id INTEGER, "
                 "tint TEXT, tint_source TEXT)")
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, latitude REAL, "
                 "longitude REAL, place_city TEXT, is_hidden INTEGER DEFAULT 0, event_id INTEGER)")
    for it in lib.items:
        conn.execute("INSERT INTO media_items (id, date_taken, latitude, longitude, place_city) VALUES (?, ?, ?, ?, ?)",
                     (it["id"], it["dt"].strftime("%Y-%m-%d %H:%M:%S"), it["lat"], it["lon"], it["city"]))
    return conn


class TestNightAnchors(unittest.TestCase):
    def test_night_of(self):
        self.assertEqual(trips.night_of(_dt(2026, 7, 12, 0, 47)), _dt(2026, 7, 11).date())
        self.assertEqual(trips.night_of(_dt(2026, 7, 11, 21)), _dt(2026, 7, 11).date())

    def test_evening_and_morning_photos_are_evidence(self):
        lib = Lib()
        lib.add(_dt(2026, 7, 6, 12), SUSICE)
        lib.add(_dt(2026, 7, 6, 19), KOLINEC)
        lib.add(_dt(2026, 7, 7, 9, 30), KOLINEC)
        lib.add(_dt(2026, 7, 7, 15), KASPERSKE)              # last photo too early: no evening anchor
        nights = sorted((n, w) for n, _a, _b, w in trips.night_anchors(lib.items))
        day6 = _dt(2026, 7, 6).date()
        self.assertEqual(nights, [(day6, trips.W_MORNING), (day6, trips.W_EVENING)])


class TestLodgingIsNotHome(unittest.TestCase):
    def test_cottage_week_is_not_home(self):
        lib = Lib()
        lib.sumava_week()
        model = trips.HomeModel.fit(lib.items)
        self.assertFalse(model.has_home(), "4 nights in one block are a lodging")

    def test_real_home_still_found(self):
        lib = Lib()
        lib.home_evenings(_dt(2026, 5, 1), 20)
        self.assertTrue(trips.HomeModel.fit(lib.items).has_home())


class TestBasecamps(unittest.TestCase):
    def test_sumava_week_without_home_photos_is_one_journey(self):
        lib = Lib()
        lib.sumava_week()
        cs = trips.segment(lib.items)
        self.assertEqual([c.kind for c in cs], [trips.KIND_JOURNEY])
        j = cs[0]
        self.assertEqual(len(j.items), len(lib.items))
        self.assertEqual(len(j.children), 6, "one chapter per day")
        self.assertEqual(trips.title_for(j.items, j.hub)[1], "Kolinec", "named after the basecamp")
        chapter_places = [trips.title_for(ch.items)[1] for ch in j.children]
        self.assertEqual(chapter_places[:2], ["Susice", "Kasperske Hory"])
        self.assertEqual(chapter_places[-1], "Zelezna Ruda")

    def test_basecamp_far_from_a_known_home(self):
        lib = Lib()
        lib.home_evenings(_dt(2026, 6, 1), 60, skip={_dt(2026, 7, d).date() for d in range(5, 13)})
        lib.sumava_week()
        js = [c for c in trips.segment(lib.items) if c.kind == trips.KIND_JOURNEY]
        self.assertEqual(len(js), 1)
        self.assertEqual(trips.title_for(js[0].items, js[0].hub)[1], "Kolinec")
        self.assertEqual(len(js[0].children), 6)

    def test_two_separate_cottage_weeks(self):
        lib = Lib()
        lib.sumava_week(2025)
        lib.sumava_week(2026)
        self.assertEqual([c.kind for c in trips.segment(lib.items)], [trips.KIND_JOURNEY] * 2)

    def test_night_photo_elsewhere_ends_a_stay_but_road_trip_stays_one_journey(self):
        lib = Lib()
        for day in (1, 2):
            lib.add(_dt(2026, 8, day, 12), VIENNA, "Wien")
            lib.add(_dt(2026, 8, day, 22), VIENNA, "Wien")
        for day in (3, 4):
            lib.add(_dt(2026, 8, day, 12), SALZBURG, "Salzburg")
            lib.add(_dt(2026, 8, day, 22), SALZBURG, "Salzburg")
        model = trips.HomeModel.fit(lib.items)
        self.assertEqual(len(trips.find_stays(model)), 2)
        cs = trips.segment(lib.items)
        self.assertEqual([c.kind for c in cs], [trips.KIND_JOURNEY])
        self.assertIsNone(cs[0].hub, "two basecamps: titled by the dominant place, not one hub")

    def test_no_evidence_still_falls_back_to_classic(self):
        lib = Lib()
        for i in range(3):
            lib.add(_dt(2026, 1, 1, 12) + _td(days=i * 5), KOLINEC)
        self.assertEqual([c.kind for c in trips.segment(lib.items)], [trips.KIND_MOMENT] * 3)


class TestMigration(unittest.TestCase):
    def test_ensure_current_runs_once_and_upgrades_legacy_events(self):
        lib = Lib()
        lib.sumava_week()
        conn = _db(lib)
        # the old (v2) result in the user's DB: two legacy events with NULL kind
        for title, ids in (("a", [it["id"] for it in lib.items[:9]]), ("b", [it["id"] for it in lib.items[9:]])):
            eid = conn.execute("INSERT INTO events (title, cover_media_id) VALUES (?, ?)", (title, ids[-1])).lastrowid
            conn.executemany("UPDATE media_items SET event_id = ? WHERE id = ?", [(eid, i) for i in ids])
        stats = trips.ensure_current(conn)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["kinds"], {trips.KIND_JOURNEY: 1})
        rows = conn.execute("SELECT id, kind, location_name FROM events WHERE parent_id IS NULL").fetchall()
        self.assertEqual(len(rows), 1)
        # ALGO_VERSION 4: a trip across a landscape is named after its region (test_regions.py)
        self.assertEqual(rows[0][1:], (trips.KIND_JOURNEY, "Šumava"))
        self.assertIn(rows[0][0], (1, 2), "the journey adopted a legacy id")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind = 'chapter'").fetchone()[0], 6)
        self.assertIsNone(trips.ensure_current(conn), "second start: nothing to do")
        conn.execute("UPDATE app_meta SET value = '2' WHERE key = 'trips_algo'")
        self.assertIsNotNone(trips.ensure_current(conn), "older version -> re-cluster again")


class TestFreshLibraryStamp(unittest.TestCase):
    def test_new_library_starts_current_existing_one_migrates(self):
        import os
        import shutil
        import tempfile
        import database
        tmp = tempfile.mkdtemp(prefix="kairos_meta_")
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(database, "DB_PATH", os.path.join(tmp, "fresh.db")):
            database.init_db()
            conn = database.get_connection()
            self.assertIsNone(trips.ensure_current(conn), "empty new library: nothing to migrate")
            conn.close()
        with mock.patch.object(database, "DB_PATH", os.path.join(tmp, "old.db")):
            database.init_db()
            conn = database.get_connection()        # a pre-v3 library: has media, no stamp
            conn.execute("INSERT INTO media_items (file_path, file_name) VALUES ('a.jpg', 'a.jpg')")
            conn.execute("DROP TABLE app_meta")
            conn.commit()
            conn.close()
            database.init_db()
            conn = database.get_connection()
            self.assertIsNone(conn.execute("SELECT value FROM app_meta WHERE key = 'trips_algo'").fetchone())
            conn.close()


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestFileDialogButtons(WindowEnv):
    """clicked(bool) used to arrive as files=False, so the handlers returned before any dialog."""
    N_PHOTOS = 2

    def test_gpx_button_opens_the_file_dialog(self):
        with mock.patch.object(main.QFileDialog, "getOpenFileNames", return_value=([], "")) as dlg:
            self.w.btn_import_gpx.click()
        dlg.assert_called_once()

    def test_add_music_button_opens_the_file_dialog(self):
        import settings_dialog
        d = settings_dialog.SettingsDialog(self.w)
        self.addCleanup(d.deleteLater)
        with mock.patch.object(settings_dialog.QFileDialog, "getOpenFileNames", return_value=([], "")) as dlg:
            d.btn_add_music.click()
        dlg.assert_called_once()

    def test_bool_is_never_a_file_list(self):
        with mock.patch.object(main.QFileDialog, "getOpenFileNames", return_value=([], "")) as dlg:
            self.assertEqual(self.w._import_gpx(False), [])
        dlg.assert_called_once()


if __name__ == "__main__":
    unittest.main()
