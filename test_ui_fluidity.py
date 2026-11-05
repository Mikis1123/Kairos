"""120 Hz unified fluidity & visual fidelity pass (2026-11-05).

frame_clock (vblank-paced VsyncAnimation), viewer_zoom (spring zoom, rubber-band edges,
1:1 device-grid snap, 400 % cap), bg_jobs (zero-stall GUI reads), hero overlay paint
budget, device-pixel alignment of the fitted photo and of icons.
"""
import math
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QEventLoop, QPoint, QPointF, QRect, QRectF, QTimer, Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

_app = QApplication.instance() or QApplication(sys.argv)

import bg_jobs  # noqa: E402
import frame_clock  # noqa: E402
import motion  # noqa: E402
import viewer_zoom as vz  # noqa: E402


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec()


class _MotionOn:
    def setUp(self):
        self._rm = motion.reduced()
        motion.set_reduce_motion(False)

    def tearDown(self):
        motion.set_reduce_motion(self._rm)


# ── frame clock ──────────────────────────────────────────────────────────────

class TestFrameClock(unittest.TestCase):

    def test_ticks_follow_the_wait_and_park_without_subscribers(self):
        waits = []

        def fake_vblank():
            waits.append(time.perf_counter())
            time.sleep(0.008)
            return True
        clk = frame_clock.FrameClock(wait=fake_vblank)
        got = []
        clk.subscribe("a", got.append)
        pump(120)
        clk.unsubscribe("a")
        n = len(got)
        self.assertGreater(n, 5, "ticks delivered on the GUI thread")
        self.assertTrue(all(isinstance(t, float) for t in got), "callbacks get the vblank time")
        pump(60)
        parked = len(waits)
        pump(80)
        self.assertLessEqual(len(waits) - parked, 1, "no subscribers = the thread parks (0 wakeups)")
        self.assertEqual(len(got), n)
        clk.shutdown()

    def test_ticks_coalesce_when_the_gui_is_busy(self):
        clk = frame_clock.FrameClock(wait=lambda: (time.sleep(0.002), True)[1])
        got = []
        clk.subscribe("a", got.append)
        time.sleep(0.1)                       # GUI blocked: ~50 vblanks, at most one tick queued
        _app.processEvents()
        self.assertLessEqual(len(got), 1)
        clk.shutdown()

    def test_timer_fallback_without_vblank(self):
        clk = frame_clock.FrameClock(wait=None)
        self.assertEqual(clk.source, "timer")
        got = []
        clk.subscribe("a", got.append)
        pump(100)
        clk.unsubscribe("a")
        self.assertGreater(len(got), 3)
        self.assertFalse(clk._timer.isActive())

    def test_a_raising_subscriber_is_dropped_not_fatal(self):
        clk = frame_clock.FrameClock(wait=None)

        def boom(_t):
            raise RuntimeError("wrapped C/C++ object has been deleted")
        clk.subscribe("x", boom)
        pump(60)
        self.assertFalse(clk.is_running())

    def test_shutdown_ends_the_thread(self):
        clk = frame_clock.FrameClock(wait=lambda: (time.sleep(0.004), True)[1])
        clk.subscribe("a", lambda t: None)
        pump(30)
        clk.shutdown()
        time.sleep(0.05)
        self.assertFalse(clk._thread.is_alive())


class TestVsyncAnimation(_MotionOn, unittest.TestCase):

    def run_anim(self, a, ms):
        done = []
        a.finished.connect(lambda: done.append(1))
        a.start()
        t0 = time.perf_counter()
        while not done and time.perf_counter() - t0 < 3:
            pump(10)
        return done

    def test_api_of_qvariantanimation(self):
        a = frame_clock.VsyncAnimation()
        vals = []
        a.valueChanged.connect(vals.append)
        a.setStartValue(0.0)
        a.setEndValue(10.0)
        a.setDuration(120)
        a.setEasingCurve(motion.easing("standard"))
        self.assertEqual(a.state(), a.State.Stopped)
        done = self.run_anim(a, 120)
        self.assertEqual(done, [1])
        self.assertEqual(vals[0], 0.0, "start value emitted synchronously, like Qt")
        self.assertEqual(vals[-1], 10.0, "ends exactly on the end value")
        self.assertEqual(a.state(), a.State.Stopped)
        self.assertEqual(a.startValue(), 0.0)
        self.assertGreater(len(vals), 4)

    def test_interpolates_qt_types(self):
        it = frame_clock.interpolate
        self.assertEqual(it(QRectF(0, 0, 10, 10), QRectF(10, 10, 20, 30), 0.5), QRectF(5, 5, 15, 20))
        self.assertEqual(it(QPointF(0, 0), QPointF(4, 8), 0.25), QPointF(1, 2))
        self.assertEqual(it(QRect(0, 0, 10, 10), QRect(10, 0, 10, 20), 0.5), QRect(5, 0, 10, 15))
        self.assertEqual(it(0, 10, 0.26), 3)
        c = it(QColor(0, 0, 0), QColor(255, 255, 255), 0.5)
        self.assertAlmostEqual(c.redF(), 0.5, places=2)

    def test_stop_does_not_finish_and_zero_duration_finishes_at_once(self):
        a = frame_clock.VsyncAnimation()
        fin = []
        a.finished.connect(lambda: fin.append(1))
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(400)
        a.start()
        pump(30)
        a.stop()
        pump(30)
        self.assertEqual(fin, [])
        b = frame_clock.VsyncAnimation()
        vals = []
        b.valueChanged.connect(vals.append)
        b.finished.connect(lambda: fin.append(2))
        b.setStartValue(0.0)
        b.setEndValue(1.0)
        b.setDuration(0)
        b.start()
        self.assertEqual((vals, fin), ([1.0], [2]))

    def test_infinite_loop_keeps_running_until_stopped(self):
        a = frame_clock.VsyncAnimation()
        a.setStartValue(0.0)
        a.setEndValue(360.0)
        a.setDuration(50)
        a.setLoopCount(-1)
        a.start()
        pump(160)
        self.assertEqual(a.state(), a.State.Running)
        a.stop()
        self.assertEqual(a.state(), a.State.Stopped)

    def test_property_animation_reads_the_current_value(self):
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        w = QWidget()
        eff = QGraphicsOpacityEffect(w)
        eff.setOpacity(0.3)
        a = frame_clock.VsyncPropertyAnimation(eff, b"opacity", w)
        a.setEndValue(1.0)
        a.setDuration(60)
        self.run_anim(a, 60)
        self.assertAlmostEqual(a.startValue(), 0.3, places=5)
        self.assertAlmostEqual(eff.opacity(), 1.0, places=5)

    def test_spring_curve_soft_landing(self):
        f = frame_clock.spring_curve()
        xs = [f(i / 200) for i in range(201)]
        self.assertEqual((xs[0], xs[-1]), (0.0, 1.0))
        self.assertLess(max(xs), 1.02, "≤ 2 % overshoot: a soft landing, not a bounce")
        self.assertGreater(max(xs), 1.0)
        self.assertTrue(all(b >= a for a, b in zip(xs[:100], xs[1:101])), "rises monotonically")

    def test_tween_runs_on_the_frame_clock(self):
        host = QWidget()
        host.show()
        tw = motion.Tween(0.0, lambda v: None, host)
        self.assertIsInstance(tw._anim, frame_clock.VsyncAnimation)
        tw.to(1.0, "spring")
        self.assertIs(tw._anim._ease, motion._SPRING)
        host.deleteLater()

    @unittest.skipUnless(frame_clock.clock().source == "vblank", "needs a real vblank")
    def test_runs_at_the_panel_refresh(self):
        hz = _app.primaryScreen().refreshRate()
        a = frame_clock.VsyncAnimation()
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(400)
        self.run_anim(a, 400)
        fps = a.frames / 0.4
        self.assertGreater(fps, 0.75 * hz, f"{fps:.0f} fps on a {hz:.0f} Hz panel (Qt's timer: ~62)")


# ── zoom physics ─────────────────────────────────────────────────────────────

class TestZoomPhysics(unittest.TestCase):

    def test_rubber_band_bounded_monotone_and_invertible(self):
        d = 900.0
        ys = [vz.rubber_band(x, d) for x in range(0, 5000, 50)]
        self.assertTrue(all(b > a for a, b in zip(ys, ys[1:])))
        self.assertLess(ys[-1], d)
        self.assertLess(vz.rubber_band(100, d), 100, "resistance: less than the finger moved")
        for x in (-700.0, -3.0, 12.0, 400.0):
            self.assertAlmostEqual(vz.rubber_inverse(vz.rubber_band(x, d), d), x, places=6)

    def test_pan_axis_unwinds_overscroll_first(self):
        size, view = 3000.0, 1000.0
        half = view / (2 * size)
        c, raw = vz.pan_axis(half, 120.0, size, view)          # at the left edge, drag right
        self.assertAlmostEqual(c, half)
        self.assertAlmostEqual(raw, 120.0)
        c, raw = vz.pan_axis(c, -50.0, size, view, raw)        # back: only the overscroll shrinks
        self.assertAlmostEqual(c, half)
        self.assertAlmostEqual(raw, 70.0)
        c, raw = vz.pan_axis(c, -100.0, size, view, raw)       # past zero: the image moves again
        self.assertEqual(raw, 0.0)
        self.assertAlmostEqual(c, half + 30.0 / size)
        self.assertEqual(vz.pan_axis(0.5, 50.0, 800.0, view, 0.0), (0.5, 0.0), "fits: no rubber")

    def test_spring_step_is_exact_and_settles(self):
        x, v = 100.0, 0.0
        for _ in range(240):                                   # 2 s at 120 Hz
            x, v = vz.spring_step(x, v, vz.BOUNCE_OMEGA, 1 / 120)
        self.assertLess(abs(x), 0.01)
        a = vz.spring_step(50.0, 300.0, 13.0, 0.1)
        b = vz.spring_step(*vz.spring_step(50.0, 300.0, 13.0, 0.05), 13.0, 0.05)
        self.assertAlmostEqual(a[0], b[0], places=9)           # step size independent (closed form)


class TestCanvasZoomFluidity(_MotionOn, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import viewer_hud
        self.canvas = viewer_hud.MediaCanvas(with_video=False)
        self.canvas.resize(1200, 800)
        self.canvas.show()
        _app.processEvents()
        self.zoom = self.canvas.zoom
        self.zoom.set_photo("x.jpg", (8000, 6000))
        self.zoom.set_enabled(True)
        img = QImage(2400, 1800, QImage.Format.Format_RGB32)
        img.fill(QColor(90, 120, 150))
        self.canvas.set_image(QPixmap.fromImage(img))

    def tearDown(self):
        self.zoom._stop_motion()
        self.canvas.close()
        super().tearDown()

    def settle(self, limit=3.0):
        t0 = time.perf_counter()
        while self.zoom.is_moving() and time.perf_counter() - t0 < limit:
            pump(10)

    def test_zoom_is_vsync_driven_and_keeps_the_point_under_the_cursor(self):
        vp = self.canvas.viewport()
        ax, ay = 900.0, 250.0
        fit = self.zoom.fit_w()
        self.zoom.zoom_to(fit * 2.0, animate=False)       # larger than the view: no edge clamp
        w0 = self.zoom.w
        pu = 0.5 + (ax - vp.width() / 2) / w0
        self.zoom.zoom_by(2.0, QPointF(ax, ay))
        self.assertTrue(self.zoom.is_moving())
        self.assertFalse(hasattr(self.zoom, "_ticker"), "no free-running QTimer any more")
        drift = []
        t0 = time.perf_counter()
        while self.zoom.is_moving() and time.perf_counter() - t0 < 3:
            pump(4)
            if self.zoom.w:
                drift.append(abs((self.zoom.u + (ax - vp.width() / 2) / self.zoom.w) - pu) * self.zoom.w)
        self.assertAlmostEqual(self.zoom.w, fit * 4.0, delta=1)
        self.assertTrue(drift and max(drift) < 0.5, f"glued within 0.5 px on every frame: {max(drift):.3f}")

    def test_new_notch_keeps_zoom_velocity_continuous(self):
        self.zoom.zoom_by(1.25, QPointF(600, 400))
        pump(40)
        v_before = self.zoom._s_vel
        self.zoom.zoom_by(1.25, QPointF(600, 400))
        self.assertEqual(self.zoom._s_vel, v_before, "retarget keeps the spring's velocity")
        self.assertGreater(v_before, 0.0)

    def test_drag_past_edge_rubber_bands_and_springs_back(self):
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        self.zoom.pan_by(1e6, 0)                              # to the left edge
        c = QPointF(600, 400)
        self.zoom.press(c)
        for i in range(1, 11):
            self.zoom.move(QPointF(600 + 30 * i, 400))
        over = self.zoom.overscroll()[0]
        self.assertGreater(over, 0.0)
        self.assertLess(over, 300.0, "resistance: less than the 300 px the finger moved")
        x_item = self.zoom.item.pos().x()
        self.assertAlmostEqual(x_item, over, delta=0.6, msg="the image shows the overscroll")
        pump(30)                                              # pause: no fling
        self.zoom.release(QPointF(900, 400))
        self.settle()
        self.assertEqual(self.zoom.overscroll(), (0.0, 0.0))
        self.assertAlmostEqual(self.zoom.item.pos().x(), 0.0, delta=0.6)

    def test_fling_into_edge_bounces(self):
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        vp = self.canvas.viewport()
        self.zoom.u = vp.width() / (2 * self.zoom.w) + 40 / self.zoom.w     # 40 px from the edge
        self.zoom._vel = (2500.0, 0.0)
        self.zoom._start_clock()
        peak = 0.0
        t0 = time.perf_counter()
        while self.zoom.is_moving() and time.perf_counter() - t0 < 3:
            pump(4)
            peak = max(peak, self.zoom.overscroll()[0])
        self.assertGreater(peak, 5.0, "overshoots past the edge")
        self.assertLess(peak, vz.MAX_BOUNCE_SPEED / (vz.BOUNCE_OMEGA * math.e) + 2)
        self.assertEqual(self.zoom.overscroll(), (0.0, 0.0), "and returns to rest")

    def test_one_to_one_lands_on_the_device_grid_and_cap_is_400(self):
        dpr = self.canvas.devicePixelRatioF()
        self.zoom.set_full_levels(self.zoom.token, [QImage(8000, 6000, QImage.Format.Format_RGB32)])
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        self.zoom.pan_by(13.37, 7.21)
        p = self.zoom.item.pos()
        self.assertAlmostEqual(p.x() * dpr, round(p.x() * dpr), places=6)
        self.assertAlmostEqual(p.y() * dpr, round(p.y() * dpr), places=6)
        self.assertTrue(self.zoom.item.smooth)
        self.zoom.zoom_to(1e9, animate=False)
        self.assertAlmostEqual(self.zoom.percent(self.zoom.w), 400.0, delta=0.5)
        self.assertFalse(self.zoom.item.smooth, "pixel peeping: crisp pixels, not bilinear mush")

    def test_reduced_motion_has_no_rubber(self):
        motion.set_reduce_motion(True)
        self.zoom.zoom_to(self.zoom.one_to_one_w(), animate=False)
        self.zoom.pan_by(1e6, 0)
        self.zoom.press(QPointF(600, 400))
        self.zoom.move(QPointF(800, 400))
        self.assertEqual(self.zoom.overscroll(), (0.0, 0.0))
        self.zoom.release(QPointF(800, 400))


# ── background loads ─────────────────────────────────────────────────────────

class TestBackgroundLoads(unittest.TestCase):

    def setUp(self):
        self._sync = bg_jobs.SYNC
        bg_jobs.SYNC = False

    def tearDown(self):
        bg_jobs.SYNC = self._sync

    def wait_for(self, cond, limit=3.0):
        t0 = time.perf_counter()
        while not cond() and time.perf_counter() - t0 < limit:
            pump(5)

    def test_runs_off_the_gui_thread_and_delivers_on_it(self):
        bg = bg_jobs.BackgroundLoads()
        seen = {}
        gui = threading.get_ident()
        bg.run("k", lambda: seen.setdefault("worker", threading.get_ident()) and 42,
               lambda r: seen.update(result=r, deliver=threading.get_ident()))
        self.wait_for(lambda: "result" in seen)
        self.assertNotEqual(seen["worker"], gui)
        self.assertEqual(seen["deliver"], gui)
        self.assertFalse(bg.pending("k"))

    def test_latest_wins_and_cancel(self):
        bg = bg_jobs.BackgroundLoads()
        got = []
        gate = threading.Event()
        bg.run("k", lambda: (gate.wait(2), "old")[1], got.append)
        bg.run("k", lambda: "new", got.append)
        gate.set()
        self.wait_for(lambda: got)
        pump(50)
        self.assertEqual(got, ["new"], "the superseded result is dropped")
        bg.run("c", lambda: "x", got.append)
        bg.cancel("c")
        pump(80)
        self.assertEqual(got, ["new"])

    def test_errors_go_to_on_error_never_raise(self):
        bg = bg_jobs.BackgroundLoads()
        errs = []
        with mock.patch("traceback.print_exc"):
            bg.run("e", lambda: 1 / 0, lambda r: None, errs.append)
            self.wait_for(lambda: errs)
        self.assertIsInstance(errs[0], ZeroDivisionError)

    def test_eager_mode_is_synchronous(self):
        bg_jobs.SYNC = True
        bg = bg_jobs.BackgroundLoads()
        got = []
        bg.run("s", lambda: 7, got.append)
        self.assertEqual(got, [7])


class TestDeferredServicesOffGuiThread(unittest.TestCase):

    def fake(self):
        import main
        f = mock.MagicMock()
        f._shutting_down = False
        f._embeddings_loaded = False
        f._run_deferred_services = main.MainWindow._run_deferred_services.__get__(f)
        return f

    def test_deferred_services_use_the_async_loaders(self):
        f = self.fake()
        with mock.patch("main.QTimer"):
            f._run_deferred_services()
        f._load_people_async.assert_called_once()
        f._load_events_async.assert_called_once()
        f._load_embeddings_async.assert_called_once()
        f._load_people.assert_not_called()
        f._load_events.assert_not_called()
        f._load_embeddings.assert_not_called()

    def test_search_before_vectors_waits_for_the_background_read(self):
        import main
        f = mock.MagicMock()
        f._search_query = "les"
        f._embeddings_loaded = False
        f._embeddings_cache = {}
        f.lbl_status = None
        f._bg_loads.return_value.pending.return_value = False
        main.MainWindow._run_ai_search(f)
        f._load_embeddings_async.assert_called_once()
        self.assertTrue(f._search_after_embeddings)
        f._collection_records.assert_not_called()          # no search worker yet
        f._search_after_embeddings = True
        main.MainWindow._after_embeddings_loaded(f)
        f._run_ai_search.assert_called_once()

    def test_query_functions_open_their_own_connection(self):
        import inspect
        import main
        for fn in (main.query_people, main.query_events, main.query_library, main.load_search_state):
            src = inspect.getsource(fn)
            self.assertTrue("get_connection()" in src or "get_search_vectors" in src, fn.__name__)
            self.assertNotIn("self", inspect.signature(fn).parameters)


# ── hero overlay paint budget ────────────────────────────────────────────────

class TestHeroOverlay(_MotionOn, unittest.TestCase):

    def setUp(self):
        super().setUp()
        import viewer_motion
        self.vm = viewer_motion
        self.host = QWidget()
        self.host.resize(1000, 700)
        self.host.show()
        self.ov = viewer_motion.HeroOverlay(self.host)
        img = QPixmap(400, 300)
        img.fill(QColor(200, 50, 50))
        back = QPixmap(1000, 700)
        back.fill(QColor(10, 200, 10))
        self.ov.begin(img, back)

    def tearDown(self):
        self.ov.release()
        self.host.close()
        super().tearDown()

    def test_opaque_hole_stays_inside_the_rounded_photo(self):
        self.ov.set_state(QRectF(100.4, 80.6, 300.2, 200.7), 14.0, 0.5)
        hole = self.ov._opaque_hole()
        r = self.ov.rect_now
        self.assertTrue(r.contains(QRectF(hole)))
        reach = 14.0 * 1.6
        # the hole's corner lies inside the squircle's corner arc (circle bound)
        dx, dy = hole.left() - r.left(), hole.top() - r.top()
        self.assertGreaterEqual(min(dx, dy), reach * (1 - 1 / math.sqrt(2)) - 1e-6)
        self.ov.set_state(QRectF(10, 10, 6, 6), 3.0, 0.5)
        self.assertTrue(self.ov._opaque_hole().isEmpty())

    def test_backdrop_floor_is_continuous_and_reaches_black_early(self):
        lv = lambda b: self.ov._backdrop_level(b)[1]           # noqa: E731
        self.assertEqual(lv(1.0), 1.0)
        self.assertEqual(lv(self.vm.BACKDROP_FLOOR), 0.0)
        self.assertEqual(lv(0.01), 0.0)
        self.assertLess(abs(lv(self.vm.BACKDROP_FLOOR + 1e-4)), 1e-3)
        self.assertAlmostEqual(lv(0.5), (0.5 - self.vm.BACKDROP_FLOOR) / (1 - self.vm.BACKDROP_FLOOR), places=4)

    def test_black_background_repaints_only_the_photo_bounds(self):
        self.ov.set_state(QRectF(100, 100, 200, 150), 0.0, 0.0)
        calls = []
        with mock.patch.object(self.ov, "update", side_effect=lambda *a: calls.append(a)):
            self.ov.set_state(QRectF(90, 95, 240, 170), 0.0, 0.0)
            self.ov.set_state(QRectF(90, 95, 240, 170), 0.0, 0.5)     # backdrop changes: full
        self.assertEqual(len(calls[0]), 1)
        self.assertTrue(QRect(calls[0][0]).contains(QRect(88, 93, 244, 174)))
        self.assertEqual(calls[1], ())

    def test_paint_matches_the_reference_composite(self):
        self.ov.set_state(QRectF(300, 200, 400, 300), 0.0, 0.5, 0.0)
        img = self.ov.grab().toImage()
        mid_photo = img.pixelColor(img.width() // 2, img.height() // 2)
        self.assertEqual((mid_photo.red(), mid_photo.green()), (200, 50))
        b = (0.5 - self.vm.BACKDROP_FLOOR) / (1 - self.vm.BACKDROP_FLOOR)
        corner = img.pixelColor(5, 5)
        self.assertAlmostEqual(corner.green(), 200 * b, delta=3)


# ── 3K / fractional DPI sharpness ────────────────────────────────────────────

class TestDevicePixelAlignment(unittest.TestCase):

    def test_fitted_photo_lands_on_whole_device_pixels_at_125_and_150(self):
        import viewer_hud
        canvas = viewer_hud.MediaCanvas(with_video=False)
        canvas.resize(1201, 801)
        canvas.show()
        _app.processEvents()
        src = QPixmap(4000, 2999)
        src.fill(QColor(1, 2, 3))
        try:
            for dpr in (1.25, 1.5, 2.0):
                with mock.patch.object(canvas, "devicePixelRatioF", return_value=dpr):
                    canvas._sources[canvas.photo_item] = src
                    canvas._fit(canvas.photo_item)
                pm = canvas.photo_item.pixmap()
                pos = canvas.photo_item.pos()
                vp = canvas.viewport()
                self.assertAlmostEqual(pos.x() * dpr, round(pos.x() * dpr), places=6, msg=dpr)
                self.assertAlmostEqual(pos.y() * dpr, round(pos.y() * dpr), places=6, msg=dpr)
                self.assertLessEqual(pm.width(), round(vp.width() * dpr))
                self.assertLessEqual(pm.height(), round(vp.height() * dpr))
                self.assertTrue(pm.width() == round(vp.width() * dpr) or pm.height() == round(vp.height() * dpr))
        finally:
            canvas.close()

    def test_icons_snap_to_the_device_grid(self):
        import icons
        from PyQt6.QtGui import QTransform

        class P:
            def __init__(self, dpr):
                self.d = mock.Mock()
                self.d.devicePixelRatioF.return_value = dpr
                self.at = None

            def device(self):
                return self.d

            def transform(self):
                return QTransform().translate(3.3, 0.0)

            def drawPixmap(self, pt, pm):
                self.at = pt
        for dpr in (1.25, 1.5):
            p = P(dpr)
            icons.paint(p, "heart_fill", QRectF(7.3, 4.1, 16, 16), QColor("white"))
            self.assertAlmostEqual(((p.at.x() + 3.3) * dpr) % 1.0, 0.0, places=6)
            self.assertAlmostEqual((p.at.y() * dpr) % 1.0, 0.0, places=6)

    def test_qicon_has_the_screen_scale(self):
        import icons
        dprs = icons._icon_dprs()
        self.assertIn(1.0, dprs)
        self.assertIn(2.0, dprs)
        self.assertIn(round(_app.primaryScreen().devicePixelRatio(), 3), dprs)


if __name__ == "__main__":
    unittest.main()
