"""Gate 2: kinetic grid scrolling, adaptive trips, journey routing/GPX, terrain, music."""
import math
import sys
import time
import unittest
from unittest import mock

import kinetic_scroll as ks

try:
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    from test_patch6 import WindowEnv           # sets up QtWebEngine before the QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    import main
    import motion
    HAS_QT = True
except ImportError:          # pragma: no cover
    HAS_QT = False
    WindowEnv = unittest.TestCase


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        _app.processEvents()
        time.sleep(0.004)


def run(phys, t0, seconds, fps=120):
    """Steps the physics like the frame timer; returns (t, positions)."""
    dt = 1.0 / fps
    t = t0
    seen = []
    for _ in range(int(seconds * fps)):
        t += dt
        phys.step(t, dt)
        seen.append(phys.pos)
    return t, seen


class TestKineticPhysics(unittest.TestCase):
    def setUp(self):
        self.p = ks.KineticPhysics(lines_per_notch=3)
        self.p.set_range(0, 100_000)
        self.p.sync(5000)

    def test_notch_glides_smoothly_to_the_step(self):
        self.p.notch(1, 0.0)
        t, seen = run(self.p, 0.0, 0.5)
        self.assertAlmostEqual(self.p.pos, 5000 + 3 * ks.NOTCH_PX_PER_LINE, places=3)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)
        # a glide, not a jump: many distinct intermediate frames, monotonic, no overshoot
        moving = [v for v in seen if 5000 < v < 5120]
        self.assertGreater(len(moving), 8)
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(seen, seen[1:])))
        self.assertLessEqual(max(seen), 5120 + 1e-6)

    def test_notch_settles_within_300ms(self):
        self.p.notch(1, 0.0)
        t, seen = run(self.p, 0.0, 0.3)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_fast_notches_accelerate_and_stay_continuous(self):
        slow = ks.KineticPhysics(3)
        slow.set_range(0, 100_000)
        slow.sync(0)
        for i in range(5):
            slow.notch(1, i * 0.5)
            run(slow, i * 0.5, 0.5)
        fast = ks.KineticPhysics(3)
        fast.set_range(0, 100_000)
        fast.sync(0)
        t = 0.0
        for i in range(5):
            fast.notch(1, t)
            t, _ = run(fast, t, 0.04)
        run(fast, t, 0.6)
        self.assertGreater(fast.pos, slow.pos * 1.3, "a burst of notches covers more ground")

    def test_reversing_direction_resets_acceleration(self):
        self.p.notch(1, 0.0)
        self.p.notch(1, 0.03)
        self.p.notch(-1, 0.06)
        self.assertEqual(self.p._accel, 1.0)

    def test_touchpad_follows_fingers_one_to_one(self):
        self.p.finger(7.5, 0.0)
        self.p.finger(2.25, 0.008)
        self.assertAlmostEqual(self.p.pos, 5009.75)
        self.assertEqual(self.p.mode, ks.KineticPhysics.FINGERS)

    def test_flick_release_coasts_with_325ms_decay(self):
        t = 0.0
        for _ in range(8):                     # 2000 px/s flick
            self.p.finger(16.0, t)
            t += 0.008
        self.p.finger(0, t, end=True)
        self.assertEqual(self.p.mode, ks.KineticPhysics.MOMENTUM)
        v0 = self.p.vel
        self.assertGreater(v0, 1500)
        start = self.p.pos
        # after one time constant velocity is ~1/e of the release
        run(self.p, t, ks.MOMENTUM_TAU_S, fps=240)
        self.assertAlmostEqual(self.p.vel / v0, math.exp(-1), delta=0.02)
        run(self.p, t, 3.0)
        coasted = self.p.pos - start
        self.assertAlmostEqual(coasted, ks.KineticPhysics.momentum_distance(v0), delta=v0 * 0.02)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_finger_lift_detected_without_scroll_end(self):
        t = 0.0
        for _ in range(8):
            self.p.finger(16.0, t)
            t += 0.008
        self.p.step(t + ks.FINGER_LIFT_S + 0.001, 0.008)
        self.assertEqual(self.p.mode, ks.KineticPhysics.MOMENTUM)

    def test_slow_careful_scroll_does_not_drift(self):
        t = 0.0
        for _ in range(10):                    # 125 px/s
            self.p.finger(1.0, t)
            t += 0.008
        self.p.finger(0, t, end=True)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_os_inertia_is_not_doubled(self):
        t = 0.0
        self.p.finger(16.0, t)
        for _ in range(6):
            t += 0.008
            self.p.finger(12.0, t, momentum_phase=True)
        self.p.finger(0, t, end=True)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_decayed_stream_counts_as_os_inertia(self):
        t = 0.0
        for d in (20, 18, 12, 6, 3, 1.5, 0.6):  # inertia the OS already applied
            self.p.finger(d, t)
            t += 0.008
        self.p.finger(0, t, end=True)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_fingers_catch_a_glide(self):
        self.p.notch(3, 0.0)
        run(self.p, 0.0, 0.03)
        self.p.finger(1.0, 0.04)
        self.assertEqual(self.p.vel, 0.0)
        self.assertEqual(self.p.mode, ks.KineticPhysics.FINGERS)

    def test_bounds_stop_momentum(self):
        self.p.set_range(0, 5100)
        t = 0.0
        for _ in range(8):
            self.p.finger(30.0, t)
            t += 0.008
        self.p.finger(0, t, end=True)
        run(self.p, t, 1.0)
        self.assertEqual(self.p.pos, 5100)
        self.assertEqual(self.p.mode, ks.KineticPhysics.IDLE)

    def test_stalled_frame_never_teleports(self):
        self.p.notch(10, 0.0)
        self.p.step(1.0, 1.0)                  # a 1 s hitch is treated as 50 ms
        self.assertLess(self.p.pos, 5000 + 10 * 120)

    def test_external_sync_stops_motion(self):
        self.p.notch(5, 0.0)
        self.p.sync(42)
        self.assertEqual((self.p.pos, self.p.mode), (42, ks.KineticPhysics.IDLE))

    def test_classify_wheel(self):
        self.assertEqual(ks.classify_wheel(-120, 0), ("notch", 1.0))
        self.assertEqual(ks.classify_wheel(240, 0), ("notch", -2.0))
        self.assertEqual(ks.classify_wheel(-30, 0), ("fine", None))
        self.assertEqual(ks.classify_wheel(-120, -14), ("fine", 14.0))


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestGridKineticScroll(WindowEnv):
    N_PHOTOS = 200

    def setUp(self):
        super().setUp()
        saved = (motion._user_reduce, motion._system_cache)
        motion._user_reduce, motion._system_cache = False, True       # animations on
        self.addCleanup(lambda: (setattr(motion, "_user_reduce", saved[0]),
                                 setattr(motion, "_system_cache", saved[1])))
        self.g = self.w.grid_view
        _app.processEvents()
        self.bar = self.g.verticalScrollBar()
        self.assertGreater(self.bar.maximum(), 1000)

    def _wheel(self, angle=0, pixel=0, phase=Qt.ScrollPhase.NoScrollPhase):
        pos = QPointF(self.g.viewport().rect().center())
        ev = QWheelEvent(pos, QPointF(self.g.viewport().mapToGlobal(pos.toPoint())), QPoint(0, pixel),
                         QPoint(0, angle), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                         phase, False)
        QApplication.sendEvent(self.g.viewport(), ev)

    def test_wheel_notch_glides_instead_of_jumping(self):
        seen = []
        self.bar.valueChanged.connect(seen.append)
        self._wheel(angle=-120)
        self.assertLess(self.bar.value(), 60, "no instant jump on the notch")
        pump(450)
        step = QApplication.wheelScrollLines() * ks.NOTCH_PX_PER_LINE
        self.assertEqual(self.bar.value(), round(step))
        self.assertGreater(len(set(seen)), 4, "several intermediate frames")
        self.assertFalse(self.g._kinetic.is_moving())

    def test_touchpad_flick_coasts(self):
        for _ in range(6):
            self._wheel(pixel=-18, phase=Qt.ScrollPhase.ScrollUpdate)
            pump(8)
        lifted = self.bar.value()
        self._wheel(phase=Qt.ScrollPhase.ScrollEnd)
        pump(700)
        self.assertGreater(self.bar.value(), lifted + 40, "momentum after the fingers lift")

    def test_reduce_motion_keeps_plain_steps(self):
        motion._user_reduce = True
        self._wheel(angle=-120)
        self.assertGreater(self.bar.value(), 0, "Qt step applied immediately")
        self.assertFalse(self.g._kinetic.is_moving())

    def test_scrollbar_drag_cancels_glide(self):
        self._wheel(angle=-120 * 5)
        pump(30)
        self.bar.setValue(900)
        pump(300)
        self.assertEqual(self.bar.value(), 900)

    def test_fling_defers_full_quality_decodes(self):
        model = self.g.photo_model
        self.g._on_scroll_speed(main.FLING_ROWS_PER_S * self.g.cell_size * 2)
        self.assertTrue(model.fast)
        started = []
        with mock.patch.object(model.thread_pool, "start", lambda w: started.append(w.tier)):
            model.data(model.index(150, 0), model.ThumbRole)
        self.assertEqual(started, [min(model.tier, main.THUMB_TIERS[0])])
        with mock.patch.object(model.thread_pool, "clear") as clear:
            self.g._on_scroll_speed(0.0)
        self.assertFalse(model.fast)
        clear.assert_called_once()
        self.assertNotIn(int(model.records[150][0]), model.loading_ids,
                         "queued decode for a row that flew past is cancelled")

    def test_fast_mode_skips_upgrades(self):
        from PyQt6.QtGui import QPixmap
        model = self.g.photo_model
        item = int(model.records[10][0])
        model.ram_cache.put(item, QPixmap(64, 64))
        model.loading_ids.discard(item)
        model._src_short.pop(item, None)          # source is big enough for a sharper tier
        model.set_fast(True)
        with mock.patch.object(model, "_start_worker") as sw:
            model.data(model.index(10, 0), model.ThumbRole)
        sw.assert_not_called()
        model.set_fast(False)
        with mock.patch.object(model, "_start_worker") as sw:
            model.data(model.index(10, 0), model.ThumbRole)
        sw.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# B: adaptive trips
# ═════════════════════════════════════════════════════════════════════════════
import sqlite3
from datetime import datetime as _dt, timedelta as _td

import trips

HOME = (50.08, 14.43)            # Prague
KUTNA_HORA = (49.95, 15.27)      # ~62 km east
KARLOVY_VARY = (50.23, 12.87)    # ~110 km west
VIENNA = (48.20, 16.37)          # ~250 km


class TripLib:
    """Synthetic library: photos at home most evenings plus scripted trips."""

    def __init__(self):
        self.items, self._id = [], 0

    def add(self, when, place, city="X"):
        self._id += 1
        self.items.append({"id": self._id, "dt": when, "lat": place[0] if place else None,
                           "lon": place[1] if place else None, "city": city})
        return self._id

    def home_evenings(self, start, days, skip=()):
        for i in range(days):
            d = start + _td(days=i)
            if d.date() in skip:
                continue
            self.add(d.replace(hour=21, minute=10), HOME, "Praha")


def _db(lib):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, location_name TEXT, "
                 "start_date TEXT, end_date TEXT, item_count INTEGER DEFAULT 0, cover_media_id INTEGER, "
                 "tint TEXT, tint_source TEXT)")
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, latitude REAL, "
                 "longitude REAL, place_city TEXT, is_hidden INTEGER DEFAULT 0, event_id INTEGER, "
                 "FOREIGN KEY (event_id) REFERENCES events (id))")
    _sync(conn, lib)
    return conn


def _sync(conn, lib):
    for it in lib.items:
        conn.execute("INSERT OR IGNORE INTO media_items (id, date_taken, latitude, longitude, place_city) "
                     "VALUES (?, ?, ?, ?, ?)", (it["id"], it["dt"].strftime("%Y-%m-%d %H:%M:%S"),
                                                it["lat"], it["lon"], it["city"]))


class TestHomeInference(unittest.TestCase):
    def test_home_from_night_photos(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 5, 1), 20)
        home = trips.HomeModel.fit(lib.items).home_at(_dt(2026, 5, 10).date())
        self.assertIsNotNone(home)
        self.assertLess(trips.haversine_km(home[0], home[1], *HOME), 1.0)
        self.assertGreaterEqual(home[2], trips.AWAY_KM_MIN)

    def test_home_follows_a_move(self):
        lib = TripLib()
        lib.home_evenings(_dt(2024, 1, 1), 60)
        brno = (49.19, 16.61)
        for i in range(60):
            lib.add(_dt(2025, 6, 1, 22, 0) + _td(days=i), brno, "Brno")
        model = trips.HomeModel.fit(lib.items)
        early = model.home_at(_dt(2024, 1, 20).date())
        late = model.home_at(_dt(2025, 7, 1).date())
        self.assertLess(trips.haversine_km(early[0], early[1], *HOME), 1.0)
        self.assertLess(trips.haversine_km(late[0], late[1], *brno), 1.0)

    def test_no_night_data_means_no_home(self):
        lib = TripLib()
        for i in range(10):
            lib.add(_dt(2026, 5, 1, 12) + _td(days=i), None)
        self.assertFalse(trips.HomeModel.fit(lib.items).has_home())


class TestTripSegmentation(unittest.TestCase):
    def kinds(self, lib):
        return [c.kind for c in trips.segment(lib.items) if c.kind != trips.KIND_MOMENT]

    def test_day_trip_returning_home_at_night(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 6, 1), 14)
        for h in (10, 12, 15):
            lib.add(_dt(2026, 6, 6, h), KUTNA_HORA, "Kutná Hora")
        trips_ = [c for c in trips.segment(lib.items) if c.kind != trips.KIND_MOMENT]
        self.assertEqual([c.kind for c in trips_], [trips.KIND_DAY_TRIP])
        self.assertEqual(len(trips_[0].items), 3, "the evening at home is not part of the day trip")

    def test_multi_day_journey_has_daily_chapters(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 7, 1), 20, skip={_dt(2026, 7, 9 + i).date() for i in range(4)})
        for day in (9, 10, 11):
            for h in (9, 13, 18):
                lib.add(_dt(2026, 7, day, h), VIENNA, "Wien")
        lib.add(_dt(2026, 7, 12, 10), (48.85, 16.05), "Znojmo")     # drive home via Znojmo
        js = [c for c in trips.segment(lib.items) if c.kind == trips.KIND_JOURNEY]
        self.assertEqual(len(js), 1)
        self.assertEqual(len(js[0].children), 4)
        self.assertTrue(all(ch.kind == trips.KIND_CHAPTER for ch in js[0].children))
        self.assertEqual(len(js[0].items), 10)

    def test_weekend_of_two_day_trips_is_not_one_journey(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 6, 1), 14, skip={_dt(2026, 6, 6).date(), _dt(2026, 6, 7).date()})
        for h in (11, 14, 17):
            lib.add(_dt(2026, 6, 6, h), KUTNA_HORA, "Kutná Hora")
        for h in (11, 14, 17):
            lib.add(_dt(2026, 6, 7, h), KARLOVY_VARY, "Karlovy Vary")
        self.assertEqual(self.kinds(lib), [trips.KIND_DAY_TRIP, trips.KIND_DAY_TRIP])

    def test_hotel_night_photo_links_days(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 6, 1), 14, skip={_dt(2026, 6, 6).date()})
        lib.add(_dt(2026, 6, 6, 11), KUTNA_HORA, "Kutná Hora")
        lib.add(_dt(2026, 6, 6, 22, 30), KUTNA_HORA, "Kutná Hora")    # hotel dinner
        lib.add(_dt(2026, 6, 7, 11), (49.6, 15.6), "Havlíčkův Brod")
        self.assertEqual(self.kinds(lib), [trips.KIND_JOURNEY])

    def test_photo_less_day_inside_journey_is_bridged(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 8, 1), 20, skip={_dt(2026, 8, d).date() for d in (5, 6, 7)})
        lib.add(_dt(2026, 8, 5, 21), VIENNA, "Wien")
        lib.add(_dt(2026, 8, 6, 12), None, "")                       # no GPS that day
        lib.add(_dt(2026, 8, 7, 9), VIENNA, "Wien")
        js = [c for c in trips.segment(lib.items) if c.kind == trips.KIND_JOURNEY]
        self.assertEqual(len(js), 1)
        self.assertEqual(len(js[0].items), 3)

    def test_without_home_falls_back_to_classic_clusters(self):
        lib = TripLib()
        for i in range(3):
            lib.add(_dt(2026, 1, 1, 12) + _td(days=i * 5), None)
        cs = trips.segment(lib.items)
        self.assertEqual([c.kind for c in cs], [trips.KIND_MOMENT] * 3)


class TestStableEventIds(unittest.TestCase):
    def setUp(self):
        self.lib = TripLib()
        self.lib.home_evenings(_dt(2026, 7, 1), 30, skip={_dt(2026, 7, d).date() for d in (9, 10, 11)})
        self.vienna = [self.lib.add(_dt(2026, 7, day, h), VIENNA, "Wien") for day in (9, 10, 11) for h in (9, 15)]
        self.conn = _db(self.lib)

    def _journey(self):
        return self.conn.execute("SELECT id, cover_media_id, tint FROM events WHERE kind = 'journey'").fetchone()

    def _chapters(self, jid):
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM events WHERE parent_id = ? ORDER BY start_date", (jid,))]

    def test_recluster_keeps_ids_cover_and_tint(self):
        trips.recluster(self.conn)
        jid = self._journey()[0]
        chapters = self._chapters(jid)
        self.assertEqual(len(chapters), 3)
        self.conn.execute("UPDATE events SET cover_media_id = ?, tint = 'warm' WHERE id = ?", (self.vienna[3], jid))
        all_before = {r[0] for r in self.conn.execute("SELECT id FROM events")}
        # new photos arrive (another evening in Vienna, more evenings at home) -> re-cluster
        self.lib.add(_dt(2026, 7, 10, 20, 30), VIENNA, "Wien")
        self.lib.home_evenings(_dt(2026, 8, 1), 5)
        _sync(self.conn, self.lib)
        stats = trips.recluster(self.conn)
        jid2, cover, tint = self._journey()
        self.assertEqual(jid2, jid, "journey keeps its id")
        self.assertEqual((cover, tint), (self.vienna[3], "warm"), "chosen cover and tint survive")
        self.assertEqual(self._chapters(jid), chapters, "chapters keep their ids")
        self.assertEqual(stats["deleted"], 0)
        self.assertTrue(all_before <= {r[0] for r in self.conn.execute("SELECT id FROM events")})

    def test_media_point_to_journey_and_chapter(self):
        trips.recluster(self.conn)
        jid = self._journey()[0]
        eid, chid = self.conn.execute("SELECT event_id, chapter_id FROM media_items WHERE id = ?",
                                      (self.vienna[2],)).fetchone()
        self.assertEqual(eid, jid)
        self.assertEqual(self.conn.execute("SELECT parent_id FROM events WHERE id = ?", (chid,)).fetchone()[0], jid)

    def test_cover_dropped_only_when_it_left_the_event(self):
        trips.recluster(self.conn)
        jid = self._journey()[0]
        home_photo = self.lib.items[0]["id"]
        self.conn.execute("UPDATE events SET cover_media_id = ? WHERE id = ?", (home_photo, jid))
        trips.recluster(self.conn)
        self.assertIsNone(self._journey()[1])

    def test_unclaimed_events_are_removed_safely(self):
        trips.recluster(self.conn)
        self.conn.execute("INSERT INTO events (title) VALUES ('stale')")
        stats = trips.recluster(self.conn)
        self.assertEqual(stats["deleted"], 1)
        self.assertIsNone(self.conn.execute("SELECT id FROM events WHERE title = 'stale'").fetchone())
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_empty_library_keeps_events(self):
        conn = _db(TripLib())
        conn.execute("INSERT INTO events (title) VALUES ('keep')")
        trips.recluster(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_legacy_events_are_adopted_in_place(self):
        """Events from the old clustering (no kind) are matched and upgraded, not replaced."""
        for c in trips.classic_clusters(sorted(self.lib.items, key=lambda i: i["dt"])):
            cur = self.conn.execute("INSERT INTO events (title) VALUES ('legacy')")
            self.conn.executemany("UPDATE media_items SET event_id = ? WHERE id = ?",
                                  [(cur.lastrowid, it["id"]) for it in c])
        legacy_of_vienna = self.conn.execute("SELECT event_id FROM media_items WHERE id = ?",
                                             (self.vienna[0],)).fetchone()[0]
        trips.recluster(self.conn)
        self.assertEqual(self._journey()[0], legacy_of_vienna)


# ═════════════════════════════════════════════════════════════════════════════
# C: corridor routing + GPX
# ═════════════════════════════════════════════════════════════════════════════
import gzip
import json as _json

import gpx_tracks
import route_corridor as rc


def _v(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _ld(field, payload):
    return _v((field << 3) | 2) + _v(len(payload)) + payload


def _zz(n):
    return (n << 1) ^ (n >> 31)


def mvt_tile(lines, layer="transportation", extent=4096):
    """[(class, [(px, py), ...])] in tile pixels -> MVT bytes."""
    keys = _ld(3, b"class")
    classes = sorted({c for c, _ in lines})
    values = b"".join(_ld(4, _ld(1, c.encode())) for c in classes)
    feats = b""
    for cls, pts in lines:
        geom = [9, _zz(pts[0][0]), _zz(pts[0][1]), 2 | ((len(pts) - 1) << 3)]
        cx, cy = pts[0]
        for x, y in pts[1:]:
            geom += [_zz(x - cx), _zz(y - cy)]
            cx, cy = x, y
        tags = b"".join(_v(t) for t in (0, classes.index(cls)))
        body = _ld(2, tags) + _v((3 << 3) | 0) + _v(2) + _ld(4, b"".join(_v(g) for g in geom))
        feats += _ld(2, body)
    lay = _ld(1, layer.encode()) + feats + keys + values + _v((5 << 3) | 0) + _v(extent) + _v((15 << 3) | 0) + _v(2)
    return _ld(3, lay)


def px_to_ll(z, x, y, px, py, extent=4096):
    n = float(1 << z)
    wx, wy = (x + px / extent) / n, (y + py / extent) / n
    return (wx * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy)))))


class TestCorridorRouting(unittest.TestCase):
    Z = 14

    def setUp(self):
        tx, ty = rc.lonlat_to_tile(14.43, 50.08, self.Z)
        self.t = (self.Z, int(tx), int(ty))
        self.tile = mvt_tile([("path", [(200, 3000), (3000, 3000), (3000, 400)]),
                              ("motorway", [(200, 3000), (3000, 400)])])        # diagonal = motorway

    def fetch(self, z, x, y):
        return self.tile if (z, x, y) == self.t else None

    def test_decoder_roundtrip(self):
        roads = rc.decode_roads(gzip.compress(self.tile), *self.t)
        self.assertEqual(sorted(c for c, _ in roads), ["motorway", "path"])
        path = dict(roads)["path"][0]
        self.assertEqual(len(path), 3)
        a = px_to_ll(*self.t, 3000, 3000)
        self.assertAlmostEqual(path[1][0], a[0], places=6)
        self.assertAlmostEqual(path[1][1], a[1], places=6)

    def test_walk_follows_the_path_not_the_motorway(self):
        a, b = px_to_ll(*self.t, 210, 2990), px_to_ll(*self.t, 2990, 410)
        path = rc.route_leg([a, b], "foot", self.fetch)
        self.assertIsNotNone(path)
        corner = px_to_ll(*self.t, 3000, 3000)
        self.assertTrue(any(rc.haversine_km(p, corner) < 0.01 for p in path), "goes round the corner")
        self.assertGreater(rc.path_km(path), 1.25 * rc.haversine_km(a, b))

    def test_drive_takes_the_motorway(self):
        a, b = px_to_ll(*self.t, 210, 2990), px_to_ll(*self.t, 2990, 410)
        path = rc.route_leg([a, b], "car", self.fetch)
        self.assertIsNotNone(path)
        self.assertLess(rc.path_km(path), 1.1 * rc.haversine_km(a, b))

    def test_photo_far_from_any_road_gives_up(self):
        a, b = px_to_ll(*self.t, 210, 2990), px_to_ll(*self.t, 600, 600)
        self.assertIsNone(rc.route_leg([a, b], "foot", self.fetch))

    def test_no_tiles_no_route(self):
        self.assertIsNone(rc.route_leg([(14.4, 50.0), (14.5, 50.1)], "car", lambda *t: None))

    def test_mode_from_pace(self):
        self.assertEqual(rc.travel_mode(4.0, 1.5), "foot")
        self.assertEqual(rc.travel_mode(120.0, 1.6), "car")
        self.assertEqual(rc.travel_mode(1200.0, 2.0), "flight")
        self.assertEqual(rc.travel_mode(600.0, 1.0), "flight")
        self.assertEqual(rc.travel_mode(300.0, 4.0), "car", "a long drive is no longer cut at 15 km")

    def test_long_legs_use_coarse_zoom_within_tile_budget(self):
        prague, vienna = (14.43, 50.08), (16.37, 48.20)
        z, tiles = rc.plan_tiles([prague, vienna], rc.haversine_km(prague, vienna), "car")
        self.assertLessEqual(z, 10)
        self.assertLessEqual(len(tiles), rc.MAX_TILES)
        z2, _ = rc.plan_tiles([(14.43, 50.08), (14.45, 50.09)], 1.7, "foot")
        self.assertEqual(z2, 14)

    def test_leg_requests_skip_flights_and_keep_order(self):
        stops = [{"lat": 50.08, "lon": 14.43, "start": "2026-07-01 08:00", "end": "2026-07-01 09:00"},
                 {"lat": 48.20, "lon": 16.37, "start": "2026-07-01 13:00", "end": "2026-07-02 09:00"},
                 {"lat": 40.64, "lon": -73.78, "start": "2026-07-02 18:00", "end": "2026-07-02 19:00"}]
        reqs = rc.leg_requests(stops)
        self.assertEqual([(i, m) for i, _w, m in reqs], [(1, "car")])

    def test_store_key_from_local_or_upstream_template(self):
        up = "https://tiles.openfreemap.org/planet/20251029_001001_pt/{z}/{x}/{y}.pbf"
        local = "http://127.0.0.1:5555/tok/tiles.openfreemap.org/planet/20251029_001001_pt/{z}/{x}/{y}.pbf"
        want = "tiles.openfreemap.org/planet/20251029_001001_pt/9/1/2.pbf"
        self.assertEqual(rc.tile_key(up, 9, 1, 2), want)
        self.assertEqual(rc.tile_key(local, 9, 1, 2), want)
        seen = []
        fetch = rc.store_fetcher(up, lambda key, lane="map": (seen.append((key, lane)) or (200, b"x", "", None, "store")))
        self.assertEqual(fetch(9, 1, 2), b"x")
        self.assertEqual(seen, [(want, "bake")])


def gpx_doc(points, name="Hike"):
    pts = "".join(f'<trkpt lat="{la}" lon="{lo}"><time>{t}</time></trkpt>' for la, lo, t in points)
    return (f'<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
            f'<metadata><name>{name}</name></metadata><trk><trkseg>{pts}</trkseg></trk></gpx>').encode()


def _track(hours=6, start="2026-07-09T06:00:00Z"):
    """A walk east from Vienna, one point a minute (UTC)."""
    t0 = _dt.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
    return [(48.20, 16.37 + 0.002 * m, (t0 + _td(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for m in range(hours * 60)]


class TestGpx(unittest.TestCase):
    def test_parse_namespaced_gpx(self):
        name, pts = gpx_tracks.parse_gpx(gpx_doc(_track(1)))
        self.assertEqual(name, "Hike")
        self.assertEqual(len(pts), 60)
        self.assertLess(pts[0][2], pts[-1][2])

    def test_untimed_or_broken_files_are_rejected(self):
        with self.assertRaises(gpx_tracks.GpxError):
            gpx_tracks.parse_gpx(b"<gpx><trk><trkseg><trkpt lat='1' lon='2'/></trkseg></trk></gpx>")
        with self.assertRaises(gpx_tracks.GpxError):
            gpx_tracks.parse_gpx(b"not xml")

    def test_offset_is_calibrated_from_the_photos(self):
        _name, pts = gpx_tracks.parse_gpx(gpx_doc(_track()))
        # photos on the track, local clock = UTC+2 (CEST)
        photos = []
        for m in (30, 90, 150, 240):
            la, lo, _t = pts[m]
            local = _dt(2026, 7, 9, 8, 0) + _td(minutes=m)
            photos.append((local.strftime("%Y-%m-%d %H:%M:%S"), la, lo))
        off, n, med = gpx_tracks.estimate_offset_min(pts, photos)
        self.assertEqual(off, 120)
        self.assertEqual(n, 4)
        self.assertLess(med, 0.2)

    def test_offset_falls_back_to_system_zone_without_photos(self):
        _name, pts = gpx_tracks.parse_gpx(gpx_doc(_track(1)))
        off, n, _ = gpx_tracks.estimate_offset_min(pts, [])
        self.assertEqual(n, 0)
        self.assertEqual(off, gpx_tracks.system_offset_min(pts[0][2]))

    def _conn_with_trip(self):
        lib = TripLib()
        lib.home_evenings(_dt(2026, 7, 1), 20, skip={_dt(2026, 7, 9).date(), _dt(2026, 7, 10).date()})
        _n, pts = gpx_tracks.parse_gpx(gpx_doc(_track()))
        for m in (10, 120, 200, 330):
            la, lo, _t = pts[m]
            lib.add(_dt(2026, 7, 9, 8, 0) + _td(minutes=m), (la, lo), "Wien")
        lib.add(_dt(2026, 7, 10, 9, 0), (48.21, 16.40), "Wien")
        conn = _db(lib)
        trips.recluster(conn)
        return conn, lib

    def test_import_binds_to_the_overlapping_trip(self):
        conn, _lib = self._conn_with_trip()
        res = gpx_tracks.import_gpx(conn, gpx_doc(_track()), "walk.gpx")
        jid = conn.execute("SELECT id FROM events WHERE kind = 'journey'").fetchone()[0]
        self.assertEqual(res["event_id"], jid)
        self.assertEqual(res["offset_min"], 120)
        again = gpx_tracks.import_gpx(conn, gpx_doc(_track()), "walk.gpx")
        self.assertTrue(again["duplicate"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM gpx_tracks").fetchone()[0], 1)

    def test_leg_slices_follow_the_recorded_line(self):
        conn, _lib = self._conn_with_trip()
        gpx_tracks.import_gpx(conn, gpx_doc(_track()), "walk.gpx")
        stops = [{"lat": 48.20, "lon": 16.39, "start": "2026-07-09 08:10", "end": "2026-07-09 08:10",
                  "exit": {"lat": 48.20, "lon": 16.39}},
                 {"lat": 48.20, "lon": 16.61, "start": "2026-07-09 10:00", "end": "2026-07-09 10:00",
                  "entry": {"lat": 48.20, "lon": 16.61}}]
        legs = gpx_tracks.leg_tracks(conn, stops)
        self.assertIn(1, legs)
        line = legs[1]
        self.assertGreater(len(line), 100, "one point per recorded minute")
        lons = [p[0] for p in line]
        self.assertTrue(all(b >= a - 1e-9 for a, b in zip(lons, lons[1:])), "in travel order")
        self.assertEqual(line[0], [16.39, 48.2])
        self.assertEqual(line[-1], [16.61, 48.2])

    def test_no_table_no_tracks(self):
        conn = sqlite3.connect(":memory:")
        self.assertEqual(gpx_tracks.leg_tracks(conn, [{"start": "2026-01-01 10:00"}, {"start": "2026-01-01 11:00"}]), {})


def _stop(lat, lon, start, end):
    return {"lat": lat, "lon": lon, "start": start, "end": end, "city": "X", "count": 1,
            "entry": {"lat": lat, "lon": lon}, "exit": {"lat": lat, "lon": lon},
            "photos": [{"id": 1, "thumb": "", "date": start}], "best": {"id": 1, "thumb": "", "date": start,
                                                                       "image": "", "type": "photo"}}


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestMapPageGate2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_patch11_maps import ShownMapPage
        cls.m = ShownMapPage((900, 600))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def tearDown(self):
        self.js("if (journey.active) window.endJourney(); reduceMotion = false; 0")

    def trip(self):
        return {"id": 77, "title": "T", "location": "", "start": "2026-07-09 08:00", "end": "2026-07-09 16:00",
                "count": 3, "stops": [_stop(48.20, 16.37, "2026-07-09 08:00", "2026-07-09 09:00"),
                                      _stop(48.30, 16.60, "2026-07-09 11:00", "2026-07-09 12:00"),
                                      _stop(48.10, 16.90, "2026-07-09 15:00", "2026-07-09 16:00")],
                "tracks": {"1": [[16.37, 48.20], [16.45, 48.27], [16.60, 48.30]]}}

    def test_relief_eases_to_true_scale_up_close(self):
        far = self.js("terrainExaggeration(9)")
        self.assertAlmostEqual(far, self.js("TERRAIN_EXAGGERATION"), places=6)
        self.assertAlmostEqual(self.js("terrainExaggeration(13)"), 1.0, places=6)
        self.assertAlmostEqual(self.js("terrainExaggeration(16)"), 1.0, places=6)
        mid = self.js("terrainExaggeration(11.5)")
        self.assertTrue(1.0 < mid < far)

    def test_pitch_is_capped_at_60(self):
        self.assertEqual(self.js("MAX_PITCH"), 60)
        self.assertEqual(self.js("map.getMaxPitch()"), 60)
        self.js("map.jumpTo({pitch: 85}); 0")
        self.assertLessEqual(self.js("map.getPitch()"), 60)

    def test_hidpi_raster_only_on_capable_machines(self):
        self.js("window.__dpr = window.devicePixelRatio; 0")
        self.assertEqual(self.js("PERF.map_hidpi_raster = false; rasterTileSize()"), 256)
        dpr = self.js("window.devicePixelRatio")
        self.assertEqual(self.js("PERF.map_hidpi_raster = true; rasterTileSize()"), 128 if dpr >= 1.5 else 256)
        self.js("PERF.map_hidpi_raster = false; 0")

    def test_gpx_leg_is_the_recorded_line_and_wins_over_routing(self):
        self.js("reduceMotion = true; 0")
        trip = self.trip()
        self.assertTrue(self.js(f"playJourney({_json.dumps(trip)})"))
        self.assertEqual(self.js("getLegPath(1)"), trip["tracks"]["1"])
        self.assertFalse(self.js("corridorRoute(77, 1, [[16.37, 48.2], [16.6, 48.3]])"), "GPX line is kept")

    def test_corridor_route_replaces_the_curve(self):
        self.js("reduceMotion = true; 0")
        trip = self.trip()
        del trip["tracks"]
        self.assertTrue(self.js(f"playJourney({_json.dumps(trip)})"))
        road = [[16.60, 48.30], [16.75, 48.25], [16.80, 48.15], [16.90, 48.10]]
        self.assertFalse(self.js(f"corridorRoute(78, 2, {_json.dumps(road)})"), "another Journey's leg is ignored")
        self.assertTrue(self.js(f"corridorRoute(77, 2, {_json.dumps(road)})"))
        self.assertEqual(self.js("getLegPath(2)"), road)
        self.assertFalse(self.js(f"corridorRoute(77, 2, {_json.dumps(road)})"), "a routed leg is not replaced")


class TestCorridorPlumbing(unittest.TestCase):
    def test_router_skips_gpx_legs_and_flights(self):
        stops = [_stop(48.20, 16.37, "2026-07-09 08:00", "2026-07-09 09:00"),
                 _stop(48.30, 16.60, "2026-07-09 11:00", "2026-07-09 12:00"),
                 _stop(48.10, 16.90, "2026-07-09 15:00", "2026-07-09 16:00"),
                 _stop(40.64, -73.78, "2026-07-09 20:00", "2026-07-09 21:00")]
        reqs = rc.leg_requests(stops)
        self.assertEqual([i for i, _w, _m in reqs], [1, 2], "the transatlantic leg stays a flight arc")


# ═════════════════════════════════════════════════════════════════════════════
# D: music & mood
# ═════════════════════════════════════════════════════════════════════════════
import os
import shutil
import tempfile

import ambient_music
import mood

_ORIG_USER_MUSIC_DIR = ambient_music.USER_MUSIC_DIR


def _mood_db(dates, smiles=(), videos=0, embeddings=None):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, date_taken TEXT, media_type TEXT, "
                 "event_id INTEGER, is_hidden INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE face_instances (id INTEGER PRIMARY KEY, media_id INTEGER, smile REAL)")
    conn.execute("CREATE TABLE image_embeddings (media_id INTEGER PRIMARY KEY, embedding BLOB)")
    for i, d in enumerate(dates, 1):
        conn.execute("INSERT INTO media_items (id, date_taken, media_type, event_id) VALUES (?, ?, ?, 1)",
                     (i, d, "video" if i <= videos else "photo"))
    for i, s in enumerate(smiles, 1):
        conn.execute("INSERT INTO face_instances (media_id, smile) VALUES (?, ?)", (i, s))
    if embeddings is not None:
        from ai_engine import encode_embedding
        for i, v in enumerate(embeddings, 1):
            conn.execute("INSERT INTO image_embeddings VALUES (?, ?)", (i, encode_embedding(v)))
    return conn


class FakeText:
    """Text model whose prompts land on fixed axes (dimension 0 = energy, 1 = brightness)."""
    DIM = 512

    def encode(self, texts):
        import numpy as np
        out = []
        for t in texts:
            v = np.zeros(self.DIM, np.float32)
            v[5] = 1.0
            if t in mood.ENERGY_PROMPTS:
                v[0] = 1.0
            elif t in mood.CALM_PROMPTS:
                v[0] = -1.0
            elif t in mood.BRIGHT_PROMPTS:
                v[1] = 1.0
            elif t in mood.DARK_PROMPTS:
                v[1] = -1.0
            out.append(v)
        return np.stack(out)


class TestMood(unittest.TestCase):
    def test_party_reads_lively_and_bright_dusk_walk_reads_calm(self):
        party = [f"2026-07-09 {13 + m // 60:02d}:{m % 60:02d}" for m in range(0, 120, 2)]   # 60 photos in 2 h
        dusk = ["2026-07-09 20:10", "2026-07-09 20:55", "2026-07-09 21:40", "2026-07-09 22:30"]
        lively = mood.trip_mood(_mood_db(party, smiles=[0.9, 0.95, 0.8], videos=8), 1, bank={})
        calm = mood.trip_mood(_mood_db(dusk, smiles=[0.1, 0.2]), 1, bank={})
        self.assertGreater(lively["energy"], calm["energy"] + 0.3)
        self.assertGreater(lively["brightness"], calm["brightness"] + 0.3)
        self.assertIn("smiles", lively["signals"])

    def test_vibe_from_embeddings_and_prompt_bank(self):
        import numpy as np
        bank = {"clip": mood.build_bank_entry(FakeText())}
        def vec(e):
            v = np.zeros(512, np.float32)
            v[0], v[7] = e, 1.0
            return v
        noon = ["2026-07-09 12:00", "2026-07-09 12:40", "2026-07-09 13:20"]
        hi = mood.trip_mood(_mood_db(noon, embeddings=[vec(1.0)] * 3), 1, bank=bank)
        lo = mood.trip_mood(_mood_db(noon, embeddings=[vec(-1.0)] * 3), 1, bank=bank)
        self.assertGreater(hi["signals"]["vibe_energy"], 0.8)
        self.assertLess(lo["signals"]["vibe_energy"], 0.2)
        self.assertGreater(hi["energy"], lo["energy"])

    def test_no_signals_is_neutral(self):
        m = mood.trip_mood(_mood_db([]), 1, bank={})
        self.assertEqual((m["energy"], m["brightness"]), (0.5, 0.5))

    def test_bundled_tracks_span_calm_to_lively(self):
        prof = {t["file"]: mood.spec_profile(t) for t in ambient_music.TRACKS}
        self.assertLess(prof["kairos_dusk.opus"]["energy"], prof["kairos_north.opus"]["energy"])
        tracks = [{"file": f, "profile": p} for f, p in prof.items()]
        calm = mood.rank_tracks(tracks, {"energy": 0.2, "brightness": 0.2})
        lively = mood.rank_tracks(tracks, {"energy": 0.9, "brightness": 0.7})
        self.assertEqual(calm[0]["file"], "kairos_dusk.opus")
        self.assertNotEqual(lively[0]["file"], "kairos_dusk.opus")

    def test_rank_is_deterministic(self):
        tracks = [{"file": "a", "profile": {"energy": 0.5, "brightness": 0.5}},
                  {"file": "b", "profile": {"energy": 0.5, "brightness": 0.5}}]
        m = {"energy": 0.5, "brightness": 0.5}
        self.assertEqual(mood.rank_tracks(tracks, m, salt=3)[0]["file"], mood.rank_tracks(tracks, m, salt=3)[0]["file"])

    def test_measured_audio_energy(self):
        import numpy as np
        sr = 11025
        t = np.arange(sr * 20) / sr
        drone = (0.05 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
        clicks = np.zeros_like(t)
        for k in range(0, t.size, sr // 4):                       # 240 bpm noise hits
            n = min(400, t.size - k)
            clicks[k:k + n] = np.random.default_rng(k).standard_normal(n) * np.exp(-np.arange(n) / 80.0)
        busy = (0.5 * clicks + 0.05 * np.sin(2 * np.pi * 2000 * t)).astype(np.float32)
        calm = mood.audio_profile(drone, sr, {"period": 1.0, "strength": 0.1})
        lively = mood.audio_profile(busy, sr, {"period": 0.4, "strength": 0.9})
        self.assertGreater(lively["energy"], calm["energy"] + 0.25)
        self.assertGreater(lively["brightness"], calm["brightness"])


class TestUserMusic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos_music_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(ambient_music, "USER_MUSIC_DIR", os.path.join(self.tmp, "music"))
        p.start()
        self.addCleanup(p.stop)
        self.src = os.path.join(self.tmp, "Summer Song.mp3")
        with open(self.src, "wb") as f:
            f.write(b"ID3" + b"\0" * 2000)

    def test_user_folder_is_in_app_data(self):
        import paths
        self.assertEqual(os.path.normcase(_ORIG_USER_MUSIC_DIR), os.path.normcase(os.path.join(paths.DATA_DIR, "music")))
        self.assertNotEqual(os.path.normcase(_ORIG_USER_MUSIC_DIR), os.path.normcase(ambient_music.MUSIC_DIR),
                            "not the read-only bundled folder")

    def test_add_lists_and_removes_a_track(self):
        added = ambient_music.add_user_tracks([self.src, os.path.join(self.tmp, "notes.txt")])
        self.assertEqual([t["file"] for t in added], ["Summer Song.mp3"])
        self.assertTrue(os.path.exists(self.src), "the original stays")
        user = ambient_music.user_tracks()
        self.assertEqual([(t["title"], t["user"]) for t in user], [("Summer Song", True)])
        self.assertTrue(all(not t["user"] for t in ambient_music.tracks()[:-1]))
        self.assertEqual(ambient_music.add_user_tracks([self.src]), [], "the same file is not added twice")
        self.assertTrue(ambient_music.remove_user_track("Summer Song.mp3"))
        self.assertEqual(ambient_music.user_tracks(), [])

    def test_name_clash_gets_a_suffix(self):
        ambient_music.add_user_tracks([self.src])
        other = os.path.join(self.tmp, "x", "Summer Song.mp3")
        os.makedirs(os.path.dirname(other))
        with open(other, "wb") as f:
            f.write(b"ID3" + b"\1" * 500)
        added = ambient_music.add_user_tracks([other])
        self.assertEqual(added[0]["file"], "Summer Song (2).mp3")

    def test_profiles_and_beats_never_decode_on_demand_when_told_not_to(self):
        ambient_music.add_user_tracks([self.src])
        t = ambient_music.user_tracks()[0]
        with mock.patch.object(ambient_music, "decode_mono", side_effect=AssertionError("decoded")):
            self.assertIsNone(ambient_music.track_profile(t, compute=False))
            self.assertIsNone(ambient_music.beat_map(t, compute=False))

    def test_bundled_profile_needs_no_audio(self):
        bundled = [t for t in ambient_music.tracks() if not t["user"]]
        if bundled:
            with mock.patch.object(ambient_music, "decode_mono", side_effect=AssertionError("decoded")):
                self.assertIsNotNone(ambient_music.track_profile(bundled[0]))


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestMusicPick(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_patch11_maps import ShownMapPage
        cls.m = ShownMapPage((600, 400))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def test_page_plays_the_track_the_app_picked(self):
        cfg = {"enabled": False, "tracks": [{"title": "A", "url": "file:///a.opus", "file": "a.opus"},
                                            {"title": "B", "url": "file:///b.opus", "file": "b.opus"},
                                            {"title": "C", "url": "file:///c.opus", "file": "c.opus"}]}
        self.m.js(f"setJourneyMusic({_json.dumps(cfg)}); 0")
        self.assertEqual(self.m.js("musicIndexFor({id: 3, music: {file: 'b.opus'}})"), 1)
        self.assertEqual(self.m.js("musicIndexFor({id: 3, music: {file: 'gone.opus'}})"), 0, "falls back to the id")
        self.assertEqual(self.m.js("musicIndexFor({id: 4})"), 1)


@unittest.skipUnless(HAS_QT, "PyQt6 not available")
class TestWindowGate2(WindowEnv):
    N_PHOTOS = 6

    def test_events_page_offers_gpx_import(self):
        self.assertTrue(hasattr(self.w, "btn_import_gpx"))
        path = os.path.join(tempfile.mkdtemp(prefix="kairos_gpx_"), "walk.gpx")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        with open(path, "wb") as f:
            f.write(gpx_doc(_track(1)))
        bad = path.replace("walk.gpx", "broken.gpx")
        with open(bad, "wb") as f:
            f.write(b"<nope/>")
        res = self.w._import_gpx([path, bad])
        self.assertEqual(len(res), 1)
        texts = " | ".join(self.toast_texts())
        self.assertIn("broken.gpx", texts)

    def test_route_journey_needs_matching_data_and_tile_store(self):
        self.w._journey_data = {"id": 5, "stops": [_stop(48.2, 16.37, "2026-07-09 08:00", "2026-07-09 09:00"),
                                                   _stop(48.3, 16.6, "2026-07-09 11:00", "2026-07-09 12:00")]}
        self.assertFalse(self.w.route_journey(6, "https://tiles.openfreemap.org/planet/x/{z}/{x}/{y}.pbf"))
        self.assertFalse(self.w.route_journey(5, ""))

        class Srv:
            def resolve(self, key, lane="map"):
                return 404, b"", "", None, "net"
        with mock.patch.object(main.tile_store, "service", return_value=Srv()):
            self.assertTrue(self.w.route_journey(5, "https://tiles.openfreemap.org/planet/x/{z}/{x}/{y}.pbf"))
            router = self.w._corridor_router
            self.assertIsNotNone(router)
            router.wait(5000)
        self.w._journey_data["tracks"] = {"1": [[16.37, 48.2], [16.6, 48.3]]}
        with mock.patch.object(main.tile_store, "service", return_value=Srv()):
            self.assertFalse(self.w.route_journey(5, "https://tiles.openfreemap.org/planet/x/{z}/{x}/{y}.pbf"),
                             "the only leg has a GPX line")

    def test_settings_lists_user_music(self):
        import settings_dialog
        tmp = tempfile.mkdtemp(prefix="kairos_music_")
        self.addCleanup(shutil.rmtree, tmp, True)
        src = os.path.join(tmp, "Song.ogg")
        with open(src, "wb") as f:
            f.write(b"OggS" + b"\0" * 100)
        with mock.patch.object(ambient_music, "USER_MUSIC_DIR", os.path.join(tmp, "music")):
            dlg = settings_dialog.SettingsDialog(self.w)
            self.addCleanup(dlg.deleteLater)
            self.assertEqual(dlg.list_music.count(), 0)
            dlg.add_music([src])
            self.assertEqual(dlg.list_music.count(), 1)
            dlg.list_music.item(0).setSelected(True)
            dlg.remove_music()
            self.assertEqual(dlg.list_music.count(), 0)


if __name__ == "__main__":
    unittest.main()
