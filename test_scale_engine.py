"""Scale fixes found by eval/bench_massive_scale.py: the grid glide ticks at the panel's refresh
rate, the thumbnail model connects signals once, and fewer decoders run while the grid glides."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QListView
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:                                    # pragma: no cover
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="Qt není dostupné")


def test_kinetic_ticker_follows_the_screen_refresh():
    import kinetic_scroll
    view = QListView()
    k = kinetic_scroll.KineticScroller(view)
    t = k._ticker
    assert isinstance(t, kinetic_scroll._FrameTicker)
    hz = view.screen().refreshRate()
    assert t.interval_ms() == max(4, int(1000.0 / max(30.0, min(hz, 240.0))))
    assert t._timer.timerType() == Qt.TimerType.PreciseTimer
    # 120 Hz -> 8 ms, 60 Hz -> 16 ms (not Qt's fixed 16 ms animation timer)

    class Scr:
        def __init__(self, hz):
            self.hz = hz

        def refreshRate(self):
            return self.hz

    for hz, ms in ((120.0, 8), (60.0, 16), (144.0, 6), (1000.0, 4)):
        t._area = type("A", (), {"screen": lambda self, _h=hz: Scr(_h)})()
        assert t.interval_ms() == ms
    view.deleteLater()


def test_glide_runs_and_settles_on_the_frame_ticker():
    import time
    import kinetic_scroll
    from PyQt6.QtCore import QStringListModel
    view = QListView()
    view.setModel(QStringListModel([f"row {i}" for i in range(4000)]))
    view.resize(300, 400)
    view.show()
    _app.processEvents()
    k = kinetic_scroll.KineticScroller(view)
    k.phys.notch(3, k.now())
    k._ensure_running()
    assert k._ticker.running()
    t0 = time.perf_counter()
    while k._ticker.running() and time.perf_counter() - t0 < 3:
        _app.processEvents()
        time.sleep(0.002)
    assert not k._ticker.running(), "the glide settles and the timer stops (0 idle wakeups)"
    assert view.verticalScrollBar().value() > 0
    view.deleteLater()


def test_thumbnail_workers_share_one_signals_object():
    import main
    model = main.ApplePhotoModel()
    started = []
    model.thread_pool.start = lambda w, *a: started.append(w)
    rec = (1, "a.jpg", "C:/x/a.jpg", "photo", None, "C:/x/t.jpg", "2026-01-01 10:00:00", 0)
    model._start_worker(1, rec)
    model._start_worker(2, (2,) + rec[1:])
    assert started[0].signals is model._signals and started[1].signals is model._signals
    assert model._signals.receivers(model._signals.loaded) == 1, "connected once, not per tile"


def test_fewer_decoders_while_the_grid_glides():
    import main
    model = main.ApplePhotoModel()
    assert model.thread_pool.maxThreadCount() == main.DECODE_THREADS_REST
    model.set_fast(True)
    assert model.thread_pool.maxThreadCount() == main.DECODE_THREADS_FLING < main.DECODE_THREADS_REST
    model.set_fast(False)
    assert model.thread_pool.maxThreadCount() == main.DECODE_THREADS_REST


def test_scale_bench_is_sandboxed():
    src = open(os.path.join(HERE, "eval", "bench_massive_scale.py"), encoding="utf-8").read()
    i = src.index("def setup_sandbox")
    body = src[i:src.index("\n\n\n", i)]
    for k in ("THUMB_DIR", "FACE_DIR", "CACHE_ROOT", "TILE_STORE_PATH", "VAULT_DIR", "TEST_GUARD_ENV",
              "CONFIG_FILE", "HIGHLIGHT_DIR", "PROXY_DIR", "BACKUP_DIR"):
        assert k in body, k
    # the guard captures the real DB path first: database is imported before DB_PATH moves
    assert body.index("import database") < body.index("database.DB_PATH = paths.DB_PATH")
