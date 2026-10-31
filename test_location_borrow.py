"""Sprint 2 – vypůjčená poloha z telefonu pro fotky bez GPS."""
import sqlite3
import unittest

import location_borrow as lb


def db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, event_id INTEGER, date_taken TEXT,
                    latitude REAL, longitude REAL, is_trashed INTEGER DEFAULT 0, is_hidden INTEGER DEFAULT 0,
                    place_city TEXT, place_country TEXT)""")
    conn.executemany("INSERT INTO media_items (id, event_id, date_taken, latitude, longitude) VALUES (?,?,?,?,?)", rows)
    return conn


def pos(conn, mid):
    return conn.execute("SELECT latitude, longitude, gps_source FROM media_items WHERE id = ?", (mid,)).fetchone()


class TestEstimate(unittest.TestCase):
    def test_interpolates_between_close_donors(self):
        lat, lon = lb.estimate(150, [(100, 50.0, 14.0), (200, 50.01, 14.02)])
        self.assertAlmostEqual(lat, 50.005)
        self.assertAlmostEqual(lon, 14.01)

    def test_far_apart_donors_use_nearest(self):
        # 100 km mezi snímky = jízda autem; fotka 2 min po prvním snímku patří k němu
        self.assertEqual(lb.estimate(220, [(100, 50.0, 14.0), (1500, 50.9, 14.0)]), (50.0, 14.0))

    def test_single_donor_only_when_close_in_time(self):
        self.assertEqual(lb.estimate(100 + 9 * 60, [(100, 1.0, 2.0)]), (1.0, 2.0))
        self.assertIsNone(lb.estimate(100 + 11 * 60, [(100, 1.0, 2.0)]))

    def test_no_donors(self):
        self.assertIsNone(lb.estimate(0, []))


class TestBorrow(unittest.TestCase):
    def test_same_event_only_and_marks_source(self):
        conn = db([
            (1, 7, "2024-06-01 10:00:00", 50.0, 14.0),      # telefon
            (2, 7, "2024-06-01 10:03:00", None, None),      # zrcadlovka – stejná akce
            (3, 8, "2024-06-01 10:03:00", None, None),      # jiná událost
            (4, 7, "2024-06-01 13:00:00", None, None),      # o 3 hodiny později
        ])
        got = lb.borrow(conn)
        self.assertEqual(got, [2])
        self.assertEqual(pos(conn, 2), (50.0, 14.0, "borrowed"))
        self.assertEqual(pos(conn, 3), (None, None, None))
        self.assertEqual(pos(conn, 4), (None, None, None))
        self.assertEqual(pos(conn, 1), (50.0, 14.0, None), "původní GPS se nemění")

    def test_borrowed_positions_are_not_donors(self):
        conn = db([
            (1, 7, "2024-06-01 10:00:00", 50.0, 14.0),
            (2, 7, "2024-06-01 10:09:00", None, None),
            (3, 7, "2024-06-01 10:18:00", None, None),      # 18 min od dárce, 9 min od vypůjčené
        ])
        self.assertEqual(lb.borrow(conn), [2])
        self.assertIsNone(pos(conn, 3)[0], "žádné řetězení chyb přes vypůjčené polohy")

    def test_restricted_to_given_ids_and_clear(self):
        conn = db([
            (1, 7, "2024-06-01 10:00:00", 50.0, 14.0),
            (2, 7, "2024-06-01 10:01:00", None, None),
            (3, 7, "2024-06-01 10:02:00", None, None),
        ])
        self.assertEqual(lb.borrow(conn, [3]), [3])
        self.assertIsNone(pos(conn, 2)[0])
        self.assertEqual(lb.clear_borrowed(conn), 1)
        self.assertEqual(pos(conn, 3), (None, None, None))

    def test_trashed_items_ignored(self):
        conn = db([(1, 7, "2024-06-01 10:00:00", 50.0, 14.0), (2, 7, "2024-06-01 10:01:00", None, None)])
        conn.execute("UPDATE media_items SET is_trashed = 1 WHERE id = 2")
        self.assertEqual(lb.borrow(conn), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
