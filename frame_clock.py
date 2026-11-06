"""One display-paced frame clock for every animation in Kairos.

Qt's QVariantAnimation / QPropertyAnimation run on QUnifiedTimer. On Windows that timer ticks
every ~16 ms, so on a 120 Hz panel every hero open, sheet, toast and HUD fade advanced at
~60 Hz (eval/bench_ui_pacing.py measures it). PyQt6 has no QAnimationDriver, so the driver
can't be swapped. Animations use VsyncAnimation instead: a small drop-in for the
QVariantAnimation API used here (start/end value, duration, easing, loop count, state,
valueChanged, finished).

* FrameClock is one shared thread. It blocks in D3DKMTWaitForVerticalBlankEvent (the vblank
  wait from kinetic_scroll, the same one Chromium paces with) and posts one coalesced tick per
  vblank to the GUI thread. It parks on an Event when nothing animates, so it costs 0 wakeups
  at rest, and it follows whatever refresh the display runs (120 Hz, or 60 Hz on battery).
* Animation time is the vblank timestamp, not the moment the GUI thread got round to the tick.
  A late slot changes when a frame is computed, never where the motion is, so the motion
  stays uniform against the panel.
* Without a vblank (another OS, or KAIROS_TICKER=timer) a precise QTimer at the screen's
  refresh interval with a 1 ms system timer period takes over.
"""
import collections
import math
import os
import sys
import threading
import time
import traceback

from PyQt6.QtCore import (QAbstractAnimation, QEasingCurve, QObject, QPoint, QPointF, QRect,
                          QRectF, QSize, QSizeF, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import QColor


# ── spring easing (closed form, normalised to the duration) ──────────────────

def spring_curve(damping=0.82, settle=6.9):
    """Easing t -> x for a damped spring released from rest. `settle` = ζω·T, so
    e^(-settle) ≈ 0.1 % residual at t = 1. ζ 0.82 overshoots ~1.3 %, a soft landing
    like the SwiftUI .smooth/.snappy family, not a bounce. The last frame is exactly 1."""
    z = max(0.05, min(damping, 0.999))
    w = settle / z
    wd = w * math.sqrt(1.0 - z * z)
    k = z * w / wd

    def f(t):
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        return 1.0 - math.exp(-z * w * t) * (math.cos(wd * t) + k * math.sin(wd * t))
    return f


def _easer(curve):
    """QEasingCurve, its Type, or a callable -> a callable progress -> eased progress."""
    if curve is None:
        return lambda t: t
    if callable(curve) and not isinstance(curve, QEasingCurve):
        return curve
    if not isinstance(curve, QEasingCurve):
        curve = QEasingCurve(curve)
    return curve.valueForProgress


# ── interpolation (same types and results as QVariantAnimation) ──────────────

def interpolate(a, b, t):
    if isinstance(a, bool) or isinstance(b, bool):
        return b if t >= 1.0 else a
    if isinstance(a, int) and isinstance(b, int):
        return int(round(a + (b - a) * t))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * t
    if isinstance(a, QColor):
        return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF(), b.getRgbF())))
    if isinstance(a, QRectF):
        return QRectF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t,
                      a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)
    if isinstance(a, QRect):
        return QRect(*(int(round(x + (y - x) * t)) for x, y in
                       ((a.x(), b.x()), (a.y(), b.y()), (a.width(), b.width()), (a.height(), b.height()))))
    if isinstance(a, QPointF):
        return QPointF(a.x() + (b.x() - a.x()) * t, a.y() + (b.y() - a.y()) * t)
    if isinstance(a, QPoint):
        return QPoint(int(round(a.x() + (b.x() - a.x()) * t)), int(round(a.y() + (b.y() - a.y()) * t)))
    if isinstance(a, QSizeF):
        return QSizeF(a.width() + (b.width() - a.width()) * t, a.height() + (b.height() - a.height()) * t)
    if isinstance(a, QSize):
        return QSize(int(round(a.width() + (b.width() - a.width()) * t)),
                     int(round(a.height() + (b.height() - a.height()) * t)))
    return b if t >= 1.0 else a


# ── the clock ────────────────────────────────────────────────────────────────

def _open_vblank():
    if os.environ.get("KAIROS_TICKER") == "timer":
        return None
    try:
        import kinetic_scroll
        return kinetic_scroll._VBlank.open() if kinetic_scroll.HAS_QT else None
    except Exception:
        return None


def _timer_period(fine):
    try:
        import kinetic_scroll
        kinetic_scroll._timer_resolution(fine)
    except Exception:
        pass


REOPEN_S = 2.0


class FrameClock(QObject):
    """Shared per-frame pulse. subscribe(key, cb) -> cb(frame_time_s) once per display frame."""

    _tick_signal = pyqtSignal()
    _instance = None

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, wait="auto"):
        super().__init__()
        self._subs = collections.OrderedDict()
        self._wait = _open_vblank() if wait == "auto" else wait
        # vblank lost (display asleep, dock / undock, mode change, driver reset): the clock falls
        # back to the timer and re-opens the vblank at most every REOPEN_S when an animation
        # starts. Before, one failure meant timer pacing (15.6 ms quanta) until the app restarted.
        self._can_reopen = wait == "auto" and self._wait is not None
        self._reopen_at = 0.0
        self._active = threading.Event()
        self._pending = threading.Event()
        self._stamp = time.perf_counter()        # vblank time of the newest tick
        self._thread = None
        self._timer = None
        self.ticks = 0
        self.recording = None                     # deque of (vblank_s, delivered_s) for benches
        self._dead = False
        self._tick_signal.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        import atexit
        atexit.register(self.shutdown)
        try:
            from PyQt6.QtCore import QCoreApplication
            app = QCoreApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.shutdown)
        except Exception:
            pass
        if self._wait is None:
            self._make_timer()

    def _make_timer(self):
        """GUI thread only (QTimer affinity)."""
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._timer_tick)

    # ── public ──
    @property
    def source(self):
        return "vblank" if self._wait is not None else "timer"

    def now(self):
        """Time of the frame being built (the vblank that woke this tick) while one runs."""
        return self._stamp

    def subscribe(self, key, cb):
        self._subs[key] = cb
        self._ensure_running()

    def unsubscribe(self, key):
        self._subs.pop(key, None)
        if not self._subs:
            self._park()

    def is_running(self):
        return bool(self._subs)

    def shutdown(self):
        """App exit: end the vblank thread (it must never emit on a deleted QObject)."""
        self._dead = True
        self._subs.clear()
        self._active.set()                        # wake a parked thread so it can return
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(0.1)                           # at most one vblank

    def record(self, on=True, maxlen=8192):
        self.recording = collections.deque(maxlen=maxlen) if on else None

    # ── internals ──
    def _ensure_running(self):
        if self._dead:
            return
        if self._wait is None and self._can_reopen and time.monotonic() >= self._reopen_at:
            self._reopen_at = time.monotonic() + REOPEN_S
            w = _open_vblank()
            if w is not None and (self._thread is None or not self._thread.is_alive()):
                if self._timer is not None and self._timer.isActive():
                    self._timer.stop()
                    _timer_period(False)
                self._wait = w                    # a new failure falls back again (same path)
        if self._wait is not None:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="kairos-frame-clock", daemon=True)
                self._thread.start()
            self._active.set()
        else:
            if self._timer is None:
                # the vblank thread just gave up (_wait = None) and its queued tick has not been
                # delivered yet: an animation started in that gap used to crash here
                # ('NoneType' object has no attribute 'isActive')
                self._make_timer()
            if self._timer.isActive():
                return
            hz = 60.0
            try:
                from PyQt6.QtGui import QGuiApplication
                scr = QGuiApplication.primaryScreen()
                hz = float(scr.refreshRate()) if scr is not None else 60.0
            except Exception:
                pass
            ms = max(4, int(1000.0 / max(30.0, min(hz, 240.0))))
            _timer_period(ms < 15)
            self._timer.start(ms)

    def _park(self):
        self._active.clear()
        if self._timer is not None and self._timer.isActive():
            self._timer.stop()
            _timer_period(False)

    def _loop(self):
        try:
            while not self._dead:
                self._active.wait()               # parked: no wakeups at rest
                if self._dead:
                    return
                if not self._wait():
                    self._wait = None             # the vblank wait failed: fall back to the timer
                    self._tick_signal.emit()
                    return
                self._stamp_pending = time.perf_counter()
                if self._active.is_set() and not self._pending.is_set() and not self._dead:
                    self._pending.set()
                    self._tick_signal.emit()
        except RuntimeError:                      # the QObject went away during interpreter exit
            return

    def _timer_tick(self):
        self._stamp_pending = time.perf_counter()
        self._pending.set()
        self._deliver()

    def _deliver(self):
        if self._wait is None and (self._timer is None or not self._timer.isActive()):
            if self._timer is None:                              # vblank died mid-run
                self._make_timer()
            if self._subs:                                       # (again, after a failed re-open)
                self._ensure_running()
        if not self._pending.is_set():
            return
        self._pending.clear()
        stamp = getattr(self, "_stamp_pending", time.perf_counter())
        self._stamp = stamp
        self.ticks += 1
        if self.recording is not None:
            self.recording.append((stamp, time.perf_counter()))
        for key, cb in list(self._subs.items()):
            try:
                cb(stamp)
            except RuntimeError:                  # the owner was deleted mid-animation
                self._subs.pop(key, None)
            except Exception:                     # never let a slot exception reach qFatal
                traceback.print_exc()
                self._subs.pop(key, None)
        if not self._subs:
            self._park()


def clock():
    return FrameClock.instance()


def release(key):
    """Unsubscribe without ever creating the clock (safe from destroyed() during teardown)."""
    inst = FrameClock._instance
    if inst is not None:
        inst._subs.pop(key, None)
        if not inst._subs:
            inst._active.clear()


# ── VsyncAnimation: QVariantAnimation on the frame clock ─────────────────────

class VsyncAnimation(QObject):
    """Drop-in for the QVariantAnimation calls Kairos makes, driven by FrameClock."""

    State = QAbstractAnimation.State
    valueChanged = pyqtSignal(object)
    finished = pyqtSignal()
    stateChanged = pyqtSignal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._start = None
        self._end = None
        self._duration = 250
        self._ease = lambda t: t
        self._curve = QEasingCurve(QEasingCurve.Type.Linear)
        self._loops = 1
        self._state = QAbstractAnimation.State.Stopped
        self._t0 = 0.0
        self._value = None
        self._key = object()
        self.frames = 0                            # ticks applied in the current run (benches)
        # No destroyed() hook: a Python slot run from a QObject's destructor during interpreter
        # teardown crashes PyQt. A deleted animation's next tick raises RuntimeError instead,
        # and the clock drops it.

    # ── QVariantAnimation API ──
    def setStartValue(self, v):
        self._start = v

    def startValue(self):
        return self._start

    def setEndValue(self, v):
        self._end = v

    def endValue(self):
        return self._end

    def setDuration(self, ms):
        self._duration = max(0, int(ms))

    def duration(self):
        return self._duration

    def setEasingCurve(self, curve):
        self._curve = curve
        self._ease = _easer(curve)

    def easingCurve(self):
        return self._curve

    def setLoopCount(self, n):
        self._loops = int(n)

    def loopCount(self):
        return self._loops

    def state(self):
        return self._state

    def currentValue(self):
        return self._value

    def start(self):
        if self._state == QAbstractAnimation.State.Running:
            return
        if self._start is None:
            self._start = self._initial_value()
        self._set_state(QAbstractAnimation.State.Running)
        self.frames = 0
        self._t0 = time.perf_counter()
        if self._duration <= 0:
            self._emit_value(self._end)
            self._finish()
            return
        self._emit_value(self._start)
        if self._state == QAbstractAnimation.State.Running:    # a slot may have stopped it
            FrameClock.instance().subscribe(self._key, self._tick)

    def stop(self):
        if self._state == QAbstractAnimation.State.Stopped:
            return
        FrameClock.instance().unsubscribe(self._key)
        self._set_state(QAbstractAnimation.State.Stopped)

    # ── internals ──
    def _initial_value(self):
        return self._end

    def _apply(self, value):
        pass

    def _set_state(self, new):
        old, self._state = self._state, new
        if old != new:
            self.stateChanged.emit(new, old)

    def _emit_value(self, v):
        self._value = v
        self._apply(v)
        self.valueChanged.emit(v)

    def _tick(self, now):
        if self._state != QAbstractAnimation.State.Running:
            FrameClock.instance().unsubscribe(self._key)
            return
        elapsed = max(0.0, now - self._t0) * 1000.0
        d = float(self._duration)
        loop = int(elapsed // d)
        done = self._loops >= 0 and loop >= self._loops
        p = 1.0 if done else (elapsed - loop * d) / d
        self.frames += 1
        self._emit_value(interpolate(self._start, self._end, 1.0 if done else self._ease(p)))
        if done:
            self._finish()

    def _finish(self):
        FrameClock.instance().unsubscribe(self._key)
        self._set_state(QAbstractAnimation.State.Stopped)
        self.finished.emit()


class VsyncPropertyAnimation(VsyncAnimation):
    """QPropertyAnimation(target, b"prop") on the frame clock."""

    def __init__(self, target, prop, parent=None):
        super().__init__(parent)
        self._target = target
        self._prop = bytes(prop).decode() if not isinstance(prop, str) else prop
        setter = "set" + self._prop[:1].upper() + self._prop[1:]
        self._setter = getattr(target, setter, None)

    def _initial_value(self):
        return self._target.property(self._prop)

    def _apply(self, value):
        if self._setter is not None:
            self._setter(value)
        else:
            self._target.setProperty(self._prop, value)


# Field kill switch: KAIROS_ANIM=qt puts every animation back on Qt's own (~60 Hz) timer.
if os.environ.get("KAIROS_ANIM") == "qt":                       # pragma: no cover
    from PyQt6.QtCore import QPropertyAnimation as _QPA, QVariantAnimation as _QVA
    VsyncAnimation, VsyncPropertyAnimation = _QVA, _QPA          # noqa: F811
    spring_curve = lambda *a, **k: QEasingCurve(QEasingCurve.Type.OutBack)  # noqa: E731,F811
