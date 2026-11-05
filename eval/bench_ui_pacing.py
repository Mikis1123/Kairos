"""120 Hz UI pacing: real widgets, real event loop, paint timestamps.

    python eval/bench_ui_pacing.py [--json out.json] [--reps 6]

Every scenario counts the PAINT EVENTS of the animated widget, not animation callbacks. A
callback that doesn't reach the screen isn't a frame. Per scenario it reports frames, fps,
and the paint interval p50/p95/p99 plus the share over 1.5x the panel's vsync (a missed
frame). It runs the same code paths the app uses:

  raw_qt / raw_vsync   600 ms QVariantAnimation vs frame_clock.VsyncAnimation on a widget
  hero_open            viewer_motion.HeroOverlay: tile -> full photo (emphasized), 2880 px image
  sheet_open           ui_sheets.Sheet open (spring Tween)
  toast                ui_toast.show_toast fade-in
  zoom_wheel           viewer_hud.MediaCanvas: 4 wheel notches (zoom animation)
  zoom_glide           MediaCanvas: drag + release (inertial pan)

There is no screen capture: frames are timed inside paintEvent. The script also runs on HEAD
(scenarios whose API is missing are skipped), so it serves for A/B.
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QEvent, QEventLoop, QObject, QPoint, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap, QWheelEvent
from PyQt6.QtWidgets import QApplication, QWidget


PAINT_MS = {}


def time_paint(cls, key):
    """Wrap cls.paintEvent to record its CPU cost (ms) under PAINT_MS[key]."""
    orig = cls.paintEvent
    if getattr(orig, "_timed", False):
        return

    def timed(self, ev):
        t = time.perf_counter()
        orig(self, ev)
        PAINT_MS.setdefault(key, []).append((time.perf_counter() - t) * 1000.0)
    timed._timed = True
    cls.paintEvent = timed


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec()


class PaintClock(QObject):
    """Timestamps of QEvent.Paint on one widget (event filter: no subclassing needed)."""

    def __init__(self, widget):
        super().__init__(widget)
        self.stamps = []
        self.on = False
        widget.installEventFilter(self)

    def eventFilter(self, obj, ev):
        if self.on and ev.type() == QEvent.Type.Paint:
            self.stamps.append(time.perf_counter())
        return False

    def intervals(self):
        s = self.stamps
        return [(b - a) * 1000.0 for a, b in zip(s, s[1:])]


RHI = {"on": False}


def host_window(w=1440, h=900):
    """A top-level window like MainWindow: born with an RHI surface when --rhi (main.py
    _reserve_rhi_surface puts a hidden 1x1 QQuickWidget in it before it is shown)."""
    host = QWidget()
    if RHI["on"]:
        from PyQt6.QtQuickWidgets import QQuickWidget
        anchor = QQuickWidget(host)
        anchor.setGeometry(0, 0, 1, 1)
        anchor.hide()
        host._anchor = anchor
    host.resize(w, h)
    return host


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def summary(name, runs, vsync_ms):
    iv = [x for r in runs for x in r["iv"]]
    dur = sum(r["dur"] for r in runs)
    frames = sum(len(r["iv"]) + 1 for r in runs)
    out = {"scenario": name, "runs": len(runs), "frames": frames,
           "fps": round(sum(len(r["iv"]) for r in runs) / max(1e-9, dur), 1),
           "p50_ms": round(pct(iv, 0.50), 2), "p95_ms": round(pct(iv, 0.95), 2),
           "p99_ms": round(pct(iv, 0.99), 2),
           "stdev_ms": round(statistics.pstdev(iv), 2) if len(iv) > 1 else 0.0,
           "missed_share": round(sum(1 for x in iv if x > 1.5 * vsync_ms) / max(1, len(iv)), 3)}
    pm = PAINT_MS.get(name)
    if pm:
        out["paint_p50_ms"], out["paint_p95_ms"] = round(pct(pm, 0.5), 2), round(pct(pm, 0.95), 2)
    print(f"{name:12s} frames {out['frames']:4d}  fps {out['fps']:6.1f}  p50 {out['p50_ms']:5.2f}  "
          f"p95 {out['p95_ms']:5.2f}  p99 {out['p99_ms']:5.2f}  sd {out['stdev_ms']:4.2f}  "
          f"missed {out['missed_share'] * 100:4.1f} %"
          + (f"  paint p50 {out['paint_p50_ms']:.2f} p95 {out['paint_p95_ms']:.2f}" if pm else ""))
    return out


def run_once(clock, start, settle_ms, is_done=None):
    clock.stamps.clear()
    clock.on = True
    t0 = time.perf_counter()
    start()
    deadline = t0 + settle_ms / 1000.0 + 1.0
    pump(settle_ms)
    while is_done is not None and not is_done() and time.perf_counter() < deadline:
        pump(10)
    clock.on = False
    s = clock.stamps
    return {"iv": clock.intervals(), "dur": (s[-1] - s[0]) if len(s) > 1 else 0.0}


def photo(w=2880, h=1920):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#2b5876"))
    g.setColorAt(1, QColor("#d9a35f"))
    p.fillRect(0, 0, w, h, g)
    for i in range(0, w, 64):
        p.fillRect(i, 0, 2, h, QColor(255, 255, 255, 40))
    p.end()
    return QPixmap.fromImage(img)


class Box(QWidget):
    def __init__(self):
        super().__init__()
        self.v = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101014"))
        p.fillRect(QRectF(self.v * (self.width() - 200), 100, 200, 200), QColor("#e0b060"))
        p.end()


def scenario_raw(kind, reps):
    from PyQt6.QtCore import QVariantAnimation
    try:
        from frame_clock import VsyncAnimation
    except ImportError:
        if kind == "vsync":
            return None
    host = host_window(1200, 500)
    w = Box()
    w.setParent(host)
    w.resize(1200, 500)
    host.show()
    pump(200)
    clock = PaintClock(w)
    runs = []
    for _ in range(reps):
        a = QVariantAnimation(w) if kind == "qt" else VsyncAnimation(w)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(600)
        a.valueChanged.connect(lambda v: (setattr(w, "v", float(v)), w.update()))
        runs.append(run_once(clock, a.start, 620, lambda: a.state() != a.State.Running))
        pump(80)
    host.close()
    return runs


def scenario_hero(reps):
    import motion
    import viewer_motion
    host = host_window()
    host.setStyleSheet("background:#000")
    host.show()
    pump(200)
    time_paint(viewer_motion.HeroOverlay, "hero_open")
    ov = viewer_motion.HeroOverlay(host)
    dpr = host.devicePixelRatioF()
    img = photo(840, 1120)                  # the tile thumbnail, as main._hero_source passes it
    back = photo(round(host.width() * dpr), round(host.height() * dpr))   # same size as the viewer
    back.setDevicePixelRatio(dpr)           # page.grab() is device-resolution
    clock = PaintClock(ov)
    runs = []
    for _ in range(reps):
        def go():
            ov.begin(img, back)
            ov.set_state(QRectF(120, 140, 180, 180), 12.0, 1.0, 1.0)
            ov.animate_to((viewer_motion.fit_rect(img.size(), QRectF(host.rect())), 0.0, 0.0, 0.0),
                          motion.duration("emphasized"), motion.easing("emphasized"), "opened")
        runs.append(run_once(clock, go, motion.duration("emphasized") + 30, lambda: not ov.is_animating()))
        ov.release()
        pump(80)
    host.close()
    return runs


def scenario_sheet(reps):
    import motion
    import ui_sheets
    host = host_window()
    host.show()
    pump(200)
    runs = []
    for _ in range(reps):
        sh = ui_sheets.Sheet(host, "Smazat 12 položek?", "Položky se přesunou do koše.")
        clock = PaintClock(sh)
        runs.append(run_once(clock, sh.open, motion.duration("spring") + 30,
                             lambda: not sh._progress.is_running()))
        sh._close(False)
        pump(motion.duration("standard") + 80)
    host.close()
    return runs


def scenario_toast(reps):
    import motion
    import ui_toast
    host = host_window()
    host.toasts = ui_toast.ToastManager(host)
    host.show()
    pump(200)
    runs = []
    for i in range(reps):
        t = ui_toast.show_toast(host, f"Exportováno {i + 3} fotek", timeout_ms=None)
        if t is None:
            return None
        clock = PaintClock(t)
        runs.append(run_once(clock, lambda: None, motion.duration("emphasized") + 30,
                             lambda: not t._fade.state() == t._fade.State.Running))
        t.dismiss()
        pump(motion.duration("standard") + 120)
    host.close()
    return runs


def _canvas():
    import viewer_hud
    time_paint(viewer_hud.MediaCanvas, "zoom_wheel")
    canvas = viewer_hud.MediaCanvas(with_video=False)
    canvas.resize(1440, 900)
    canvas.show()
    pump(150)
    img = photo(5712, 4284)                 # 24 Mpx source
    canvas.zoom.set_photo(None, (img.width(), img.height()))
    canvas.zoom.set_enabled(True)
    vp = canvas.viewport()
    dpr = canvas.devicePixelRatioF()
    fit = img.scaled(round(vp.width() * dpr), round(vp.height() * dpr), Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    canvas.set_image(fit)
    pump(50)
    import viewer_zoom
    canvas.zoom.set_full_levels(canvas.zoom.token, viewer_zoom.build_pyramid(img.toImage()))
    return canvas


def _wheel(canvas, pos, dy):
    vp = canvas.viewport()
    ev = QWheelEvent(QPointF(pos), QPointF(vp.mapToGlobal(pos)), QPoint(0, 0), QPoint(0, dy),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(vp, ev)


def _zoom_busy(canvas):
    z = canvas.zoom
    t = getattr(z, "_ticker", None)
    if t is not None and hasattr(t, "isActive"):
        return t.isActive()
    return z.is_moving() if hasattr(z, "is_moving") else False


def scenario_zoom_wheel(reps):
    canvas = _canvas()
    vp = canvas.viewport()
    clock = PaintClock(vp)
    c = QPoint(vp.width() // 2, vp.height() // 2)
    runs = []
    for _ in range(reps):
        canvas.zoom.reset(animate=False)
        pump(60)

        def go():
            for i in range(3):
                QTimer.singleShot(i * 70, lambda: _wheel(canvas, c, 120))
        runs.append(run_once(clock, go, 3 * 70 + 260, lambda: not _zoom_busy(canvas)))
    canvas.close()
    return runs


def scenario_zoom_glide(reps):
    canvas = _canvas()
    vp = canvas.viewport()
    clock = PaintClock(vp)
    c = QPointF(vp.width() / 2, vp.height() / 2)
    runs = []
    for _ in range(reps):
        canvas.zoom.zoom_to(canvas.zoom.one_to_one_w(), c, animate=False)
        pump(60)
        canvas.zoom.press(c)
        for i in range(8):
            pump(8)
            canvas.zoom.move(QPointF(c.x() - 28 * (i + 1), c.y() - 10 * (i + 1)))
        runs.append(run_once(clock, lambda: canvas.zoom.release(QPointF(c.x() - 224, c.y() - 80)), 1400,
                             lambda: not _zoom_busy(canvas)))
    canvas.close()
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--only")
    ap.add_argument("--rhi", action="store_true", help="windows born with an RHI surface, as MainWindow")
    args = ap.parse_args()
    RHI["on"] = args.rhi
    app = QApplication.instance() or QApplication(sys.argv)
    import motion
    motion.set_reduce_motion(False)
    hz = app.primaryScreen().refreshRate()
    vsync = 1000.0 / hz
    print(f"rhi surface: {RHI['on']}")
    print(f"panel {hz:.1f} Hz (vsync {vsync:.2f} ms), DPR {app.primaryScreen().devicePixelRatio()}")
    try:
        import frame_clock
        print(f"frame clock: {frame_clock.clock().source}")
    except ImportError:
        print("frame clock: n/a (HEAD)")
    table = [("raw_qt", lambda: scenario_raw("qt", args.reps)),
             ("raw_vsync", lambda: scenario_raw("vsync", args.reps)),
             ("hero_open", lambda: scenario_hero(args.reps)),
             ("sheet_open", lambda: scenario_sheet(args.reps)),
             ("toast", lambda: scenario_toast(args.reps)),
             ("zoom_wheel", lambda: scenario_zoom_wheel(args.reps)),
             ("zoom_glide", lambda: scenario_zoom_glide(args.reps))]
    results = []
    for name, fn in table:
        if args.only and name not in args.only.split(","):
            continue
        runs = fn()
        if not runs:
            print(f"{name:12s} skipped")
            continue
        results.append(summary(name, runs, vsync))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"hz": hz, "results": results}, f, indent=1)


if __name__ == "__main__":
    main()
