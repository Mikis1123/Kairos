"""Gate 2 A: pixel-smooth kinetic scrolling for the photo grid.

QAbstractScrollArea moves in fixed jumps: one wheel notch is one jump. This module replaces
that with two physical models. They calibrate themselves from the input stream, so there is
nothing to configure:

* **Wheel notch** (angleDelta a multiple of 120, no pixelDelta): the target moves by a step
  derived from the Windows "lines per notch" setting. A critically damped spring then glides
  toward it, so new notches keep the velocity continuous instead of restarting a jump. Fast
  notch bursts accelerate up to 3x.
* **Precision touchpad / high-res wheel** (pixelDelta, or angleDelta finer than 120): the
  content follows the fingers 1:1 and the velocity is tracked. When the fingers lift
  (ScrollEnd, or a short gap with no events), momentum decays exponentially with
  tau = 325 ms (the iOS deceleration curve). If the OS already sends its own inertia
  (ScrollMomentum phase, or deltas that decay to nothing), no second inertia is added.

The position is kept as a float and only rounded when it is written to the scrollbar, so
slow glides never stall on integer truncation and long flings never drift.

`KineticPhysics` is pure Python (tested with a synthetic clock). `KineticScroller` drives it
from the Qt animation timer (one step per frame) and reports velocity, which the grid uses to
defer full-quality thumbnail decodes during fast flings.
"""
import math
import sys

try:
    from PyQt6.QtCore import QElapsedTimer, QObject, Qt, QTimer, pyqtSignal
    HAS_QT = True
except ImportError:          # pragma: no cover - the physics core works without Qt
    HAS_QT = False

MOMENTUM_TAU_S = 0.325       # iOS-like exponential deceleration time constant
SPRING_OMEGA = 30.0          # rad/s, critically damped: 90 % of a notch in ~130 ms, settled by ~290 ms
NOTCH_PX_PER_LINE = 40.0     # px per Windows "wheel line" (default 3 lines -> 120 px/notch)
NOTCH_BURST_S = 0.09         # notches closer than this accelerate the step
NOTCH_ACCEL_MAX = 3.0
FINGER_LIFT_S = 0.05         # no touchpad event for this long = fingers lifted
MIN_FLING_PX_S = 350.0       # slower releases just stop (no drift after a careful scroll)
STOP_PX_S = 12.0             # momentum ends below this
SETTLE_PX = 0.35             # spring is done within this distance and STOP_PX_S
VELOCITY_WINDOW_S = 0.08     # touchpad release velocity = average over the last 80 ms


class KineticPhysics:
    """Scroll position state machine. Time is passed in explicitly (seconds)."""

    IDLE, SPRING, FINGERS, MOMENTUM = "idle", "spring", "fingers", "momentum"

    def __init__(self, lines_per_notch=3):
        self.pos = 0.0
        self.target = 0.0
        self.vel = 0.0               # px/s, + = down
        self.lo, self.hi = 0.0, 0.0
        self.mode = self.IDLE
        self.lines = max(1, int(lines_per_notch or 3))
        self._last_notch_t = None
        self._last_dir = 0
        self._accel = 1.0
        self._samples = []           # touchpad: [(t, dy)] within VELOCITY_WINDOW_S
        self._last_finger_t = None
        self._os_momentum = False

    # ── bounds / sync ──
    def set_range(self, lo, hi):
        self.lo, self.hi = float(lo), float(max(lo, hi))
        self.pos = self._clamp(self.pos)
        self.target = self._clamp(self.target)

    def _clamp(self, v):
        return min(self.hi, max(self.lo, v))

    def sync(self, value):
        """Something else moved the scrollbar (drag, keyboard, scrollTo): adopt it and stop."""
        self.pos = self.target = self._clamp(float(value))
        self.vel = 0.0
        self.mode = self.IDLE
        self._samples.clear()

    def active(self):
        return self.mode != self.IDLE

    # ── input ──
    def notch(self, notches, t):
        """Classic wheel: notches > 0 = scroll content down (wheel pulled toward the user)."""
        direction = 1 if notches > 0 else -1
        if self._last_notch_t is not None and t - self._last_notch_t < NOTCH_BURST_S \
                and self.mode == self.SPRING and direction == self._last_dir:
            self._accel = min(NOTCH_ACCEL_MAX, self._accel * 1.35)
        else:
            self._accel = 1.0
        self._last_notch_t, self._last_dir = t, direction
        step = notches * self.lines * NOTCH_PX_PER_LINE * self._accel
        base = self.target if self.mode == self.SPRING else self.pos
        self.target = self._clamp(base + step)
        if self.mode != self.SPRING:
            self.vel = self.vel if self.mode == self.MOMENTUM else 0.0
        self.mode = self.SPRING

    def finger(self, dy, t, momentum_phase=False, end=False):
        """Touchpad pixels (+ = content down). momentum_phase = the OS is sending its own inertia."""
        if momentum_phase:
            self._os_momentum = True
        if self.mode in (self.SPRING, self.MOMENTUM) and not momentum_phase:
            self.vel = 0.0                         # fingers caught the glide
        if dy:
            self.pos = self._clamp(self.pos + dy)
            self.target = self.pos
            self._samples.append((t, float(dy)))
        self._samples = [(st, d) for st, d in self._samples if t - st <= VELOCITY_WINDOW_S]
        self._last_finger_t = t
        self.mode = self.FINGERS
        if end:
            self._release(t)

    def _release_velocity(self, t):
        pts = [(st, d) for st, d in self._samples if t - st <= VELOCITY_WINDOW_S + FINGER_LIFT_S]
        if len(pts) < 2:
            return 0.0
        span = max(1e-3, pts[-1][0] - pts[0][0])
        total = sum(d for _, d in pts[1:])
        v = total / span
        # OS inertia that already decayed: the last deltas are small compared with the
        # average, so the stream ended on its own; adding momentum would double it
        if len(pts) >= 4:
            recent = abs(pts[-1][1]) + abs(pts[-2][1])
            early = abs(pts[0][1]) + abs(pts[1][1])
            if early > 0 and recent < 0.35 * early:
                return 0.0
        return v

    def _release(self, t):
        v = 0.0 if self._os_momentum else self._release_velocity(t)
        self._samples.clear()
        self._os_momentum = False
        if abs(v) >= MIN_FLING_PX_S:
            self.vel = v
            self.mode = self.MOMENTUM
        else:
            self.vel = 0.0
            self.mode = self.IDLE

    # ── time ──
    def step(self, t, dt):
        """Advances the simulation. Returns True while still moving."""
        dt = max(0.0, min(0.05, float(dt)))        # a stalled frame never teleports the content
        if self.mode == self.FINGERS:
            if self._last_finger_t is not None and t - self._last_finger_t >= FINGER_LIFT_S:
                self._release(t)
            return self.mode != self.IDLE
        if self.mode == self.SPRING:
            w = SPRING_OMEGA
            x = self.pos - self.target
            # exact critically damped step: x(t) = (x0 + (v0 + w x0) t) e^{-wt}
            e = math.exp(-w * dt)
            c = self.vel + w * x
            nx = (x + c * dt) * e
            nv = (c - w * (x + c * dt)) * e
            self.pos, self.vel = self.target + nx, nv
            if self.pos <= self.lo or self.pos >= self.hi:
                self.pos = self._clamp(self.pos)
            if abs(nx) < SETTLE_PX and abs(nv) < STOP_PX_S * 4:
                self.pos, self.vel, self.mode = self.target, 0.0, self.IDLE
            return self.mode != self.IDLE
        if self.mode == self.MOMENTUM:
            decay = math.exp(-dt / MOMENTUM_TAU_S)
            # integral of v0 e^{-t/tau} over dt
            self.pos += self.vel * MOMENTUM_TAU_S * (1.0 - decay)
            self.vel *= decay
            if self.pos <= self.lo or self.pos >= self.hi:
                self.pos = self._clamp(self.pos)
                self.vel = 0.0
            if abs(self.vel) < STOP_PX_S:
                self.vel, self.mode = 0.0, self.IDLE
            self.target = self.pos
            return self.mode != self.IDLE
        return False

    @staticmethod
    def momentum_distance(v0):
        """How far a release at v0 px/s coasts (for tests and anchoring)."""
        return v0 * MOMENTUM_TAU_S


def classify_wheel(angle_dy, pixel_dy):
    """('notch', notches) for a classic wheel, ('fine', px) for touchpads and high-res wheels.

    Sign convention: positive = content moves down (Qt delta is negative when scrolling down).
    """
    if pixel_dy:
        return "fine", -float(pixel_dy)
    if angle_dy and angle_dy % 120 == 0:
        return "notch", -angle_dy / 120.0
    return "fine", None              # caller converts angle units to px


if HAS_QT:
    _period = {"held": False}

    def _timer_resolution(fine):
        """1 ms system timer resolution while a glide runs, released when it settles.
        Measured: without it the 8 ms precise timer fired every ~16 ms (Windows' default
        15.6 ms tick), and the opaque grid that now paints a frame in ~6 ms still glided at
        62 fps. Chromium raises it the same way for its animations."""
        if fine == _period["held"] or sys.platform != "win32":
            return
        try:
            import ctypes
            winmm = ctypes.WinDLL("winmm")
            (winmm.timeBeginPeriod if fine else winmm.timeEndPeriod)(1)
            _period["held"] = fine
        except (OSError, AttributeError):
            pass

    class _FrameTicker(QObject):
        """Per-frame callback at the panel's refresh rate.

        Qt's unified animation timer (QAbstractAnimation) ticks every 16 ms on Windows. Measured
        (eval/bench_massive_scale.py): the grid glide ran at 59-62 ticks/s on a 120 Hz panel,
        so it was capped at 60 fps whatever the paint cost. This one is a precise timer at the
        refresh interval of the window's screen, read at each start (battery: 60 Hz). The physics
        integrates real elapsed time, so timer jitter changes the sampling, never the motion."""

        def __init__(self, cb, area, parent=None):
            super().__init__(parent)
            self._cb = cb
            self._area = area
            self._timer = QTimer(self)
            self._timer.setTimerType(Qt.TimerType.PreciseTimer)
            self._timer.timeout.connect(self._cb)

        def interval_ms(self):
            hz = 60.0
            try:
                scr = self._area.screen() if hasattr(self._area, "screen") else None
                hz = float(scr.refreshRate()) if scr is not None else 60.0
            except Exception:
                pass
            return max(4, int(1000.0 / max(30.0, min(hz, 240.0))))

        def start(self):
            ms = self.interval_ms()
            _timer_resolution(ms < 15)
            self._timer.start(ms)

        def stop(self):
            self._timer.stop()
            _timer_resolution(False)

        def running(self):
            return self._timer.isActive()

    class _VBlank:
        """Blocking wait for the display's vertical blank (D3DKMTWaitForVerticalBlankEvent on
        the primary output, as Chromium paces its animations). None when unavailable."""

        @staticmethod
        def open():
            if sys.platform != "win32":
                return None
            try:
                import ctypes
                from ctypes import wintypes

                class _Luid(ctypes.Structure):
                    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

                class _Open(ctypes.Structure):
                    _fields_ = [("hDc", wintypes.HDC), ("hAdapter", ctypes.c_uint), ("AdapterLuid", _Luid),
                                ("VidPnSourceId", ctypes.c_uint)]

                class _Wait(ctypes.Structure):
                    _fields_ = [("hAdapter", ctypes.c_uint), ("hDevice", ctypes.c_uint),
                                ("VidPnSourceId", ctypes.c_uint)]

                gdi32, user32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("user32")
                user32.GetDC.restype = wintypes.HDC
                user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
                hdc = user32.GetDC(None)
                o = _Open(hDc=hdc)
                ok = gdi32.D3DKMTOpenAdapterFromHdc(ctypes.byref(o)) == 0
                user32.ReleaseDC(None, hdc)
                if not ok:
                    return None
                w = _Wait(hAdapter=o.hAdapter, hDevice=0, VidPnSourceId=o.VidPnSourceId)
                ref = ctypes.byref(w)
                fn = gdi32.D3DKMTWaitForVerticalBlankEvent
                return lambda: fn(ref) == 0          # ctypes releases the GIL while it blocks
            except (OSError, AttributeError, ValueError):
                return None

    class _VsyncTicker(QObject):
        """Per-frame callback on the display's real vblank.

        Measured (eval/bench_massive_scale.py, 20 000 items): the 8 ms precise QTimer was
        quantised to Windows' 15.6 ms tick once the GUI event queue was busy, even with a 1 ms
        timer period, so the glide stayed at 62 fps although a frame cost ~6 ms. A thread
        waits for each vblank (8.3 ms at 120 Hz, 16.7 ms at 60 Hz: the refresh is followed
        automatically) and posts one tick to the GUI thread. Ticks coalesce: a busy GUI never
        builds a backlog. The thread lives only while a glide runs (0 wakeups at rest)."""

        _tick_signal = pyqtSignal()

        def __init__(self, cb, wait, parent=None):
            super().__init__(parent)
            import threading
            self._cb = cb
            self._wait = wait
            self._run = False
            self._pending = threading.Event()
            self._thread = None
            self._tick_signal.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

        def _loop(self):
            while self._run:
                if not self._wait():
                    self._run = False
                    break
                if self._run and not self._pending.is_set():
                    self._pending.set()
                    self._tick_signal.emit()

        def _deliver(self):
            self._pending.clear()
            if self._run:
                self._cb()

        def start(self):
            import threading
            if self._run and self._thread is not None and self._thread.is_alive():
                return
            self._run = True
            self._pending.clear()
            self._thread = threading.Thread(target=self._loop, name="kairos-vsync", daemon=True)
            self._thread.start()

        def stop(self):
            self._run = False

        def running(self):
            return self._run

        def interval_ms(self):
            return 0                                 # paced by the display, not a timer

    def make_ticker(cb, area, parent=None):
        """The vblank ticker where the platform offers it, else the precise-timer ticker."""
        import os
        wait = None if os.environ.get("KAIROS_TICKER") == "timer" else _VBlank.open()
        if wait is not None:
            return _VsyncTicker(cb, wait, parent)
        return _FrameTicker(cb, area, parent)

    class KineticScroller(QObject):
        """Drives a QAbstractScrollArea's vertical scrollbar with KineticPhysics."""

        velocity_changed = pyqtSignal(float)        # |px/s|, emitted per frame while moving
        settled = pyqtSignal()

        def __init__(self, area, reduced=lambda: False, parent=None):
            super().__init__(parent or area)
            from PyQt6.QtWidgets import QApplication
            self.area = area
            self.bar = area.verticalScrollBar()
            self.reduced = reduced
            self.phys = KineticPhysics(QApplication.wheelScrollLines())
            self._clock = QElapsedTimer()
            self._clock.start()
            self._last_t = None
            self._writing = False
            self._ticker = make_ticker(self._tick, area, self)
            self.bar.valueChanged.connect(self._on_value)
            self.bar.rangeChanged.connect(self._on_range)
            self._on_range(self.bar.minimum(), self.bar.maximum())
            self.phys.sync(self.bar.value())

        def now(self):
            return self._clock.nsecsElapsed() / 1e9

        def velocity(self):
            return abs(self.phys.vel)

        def is_moving(self):
            return self.phys.active()

        def stop(self):
            self.phys.sync(self.bar.value())
            self._ticker.stop()
            self.velocity_changed.emit(0.0)
            self.settled.emit()

        def _on_range(self, lo, hi):
            self.phys.set_range(lo, hi)

        def _on_value(self, value):
            if self._writing:
                return
            if abs(value - round(self.phys.pos)) > 1:   # scrollbar drag / keys / scrollTo
                was = self.phys.active()
                self.phys.sync(value)
                if was:
                    self._ticker.stop()
                    self.velocity_changed.emit(0.0)
                    self.settled.emit()

        def wheel(self, event):
            """Handles a plain (no Ctrl) vertical wheel event. Returns False when not handled."""
            angle = event.angleDelta()
            pixel = event.pixelDelta()
            if abs(angle.x()) > abs(angle.y()) and not pixel.y():
                return False
            if self.reduced():
                return False                        # Reduce motion: Qt's plain steps
            t = self.now()
            phase = event.phase()
            kind, amount = classify_wheel(angle.y(), pixel.y())
            if self.bar.value() != round(self.phys.pos) and not self.phys.active():
                self.phys.sync(self.bar.value())
            if kind == "notch":
                self.phys.notch(amount, t)
            else:
                if amount is None:
                    amount = -angle.y() / 120.0 * self.phys.lines * NOTCH_PX_PER_LINE
                self.phys.finger(amount, t,
                                 momentum_phase=(phase == Qt.ScrollPhase.ScrollMomentum),
                                 end=(phase == Qt.ScrollPhase.ScrollEnd))
                self._write()
            self._ensure_running()
            return True

        def _ensure_running(self):
            if self.phys.active() and not self._ticker.running():
                self._last_t = self.now()
                self._ticker.start()

        def _tick(self):
            t = self.now()
            dt = t - (self._last_t if self._last_t is not None else t)
            self._last_t = t
            moving = self.phys.step(t, dt)
            self._write()
            self.velocity_changed.emit(abs(self.phys.vel))
            if not moving:
                self._ticker.stop()
                self.settled.emit()

        def _write(self):
            v = int(round(self.phys.pos))
            if v != self.bar.value():
                self._writing = True
                try:
                    self.bar.setValue(v)
                finally:
                    self._writing = False
