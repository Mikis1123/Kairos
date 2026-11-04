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


def test_kinetic_ticker_is_vblank_paced_with_a_timer_fallback(monkeypatch):
    import kinetic_scroll
    view = QListView()
    k = kinetic_scroll.KineticScroller(view)
    if kinetic_scroll._VBlank.open() is not None:
        assert isinstance(k._ticker, kinetic_scroll._VsyncTicker), "real vblank where the platform has it"
    monkeypatch.setenv("KAIROS_TICKER", "timer")
    assert isinstance(kinetic_scroll.make_ticker(lambda: None, view), kinetic_scroll._FrameTicker)
    view.deleteLater()


def test_vblank_ticks_at_the_display_rate():
    import time
    import kinetic_scroll
    wait = kinetic_scroll._VBlank.open()
    if wait is None:
        pytest.skip("no D3DKMT vblank on this platform")
    ts = []
    for _ in range(30):
        assert wait()
        ts.append(time.perf_counter())
    iv = sorted(b - a for a, b in zip(ts, ts[1:]))
    hz = QApplication.primaryScreen().refreshRate()
    assert abs(iv[len(iv) // 2] * 1000 - 1000 / hz) < 2.5, "one wait per refresh"


def test_vsync_ticker_coalesces_and_stops():
    import threading
    import time
    import kinetic_scroll
    calls = []
    gate = threading.Event()

    def wait():                       # a fast fake vblank: 1 ms
        time.sleep(0.001)
        return True
    t = kinetic_scroll._VsyncTicker(lambda: calls.append(1), wait)
    t.start()
    time.sleep(0.05)                  # GUI thread busy: no events processed
    _app.processEvents()
    assert len(calls) == 1, "ticks coalesce while the GUI thread is busy (no backlog)"
    t.stop()
    time.sleep(0.01)
    assert not t._thread.is_alive(), "thread ends when the glide settles"


def test_frame_ticker_follows_the_screen_refresh():
    import kinetic_scroll
    view = QListView()
    t = kinetic_scroll._FrameTicker(lambda: None, view)
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


def test_grid_viewport_is_opaque_and_paints_the_page_background():
    import main
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert "self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)" in src
    i = src.index("class AppleGridView")
    j = src.index("    def paintEvent(self, event):", i)
    assert 'fillRect(event.rect(), theme.qcolor("bg"))' in src[j:j + 400]


def _sample_image(w=640, h=480):
    from PyQt6.QtGui import QImage, QPainter, QColor, QLinearGradient
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor(200, 40, 40))
    g.setColorAt(1, QColor(30, 90, 220))
    p.fillRect(img.rect(), g)
    p.end()
    return img


def test_worker_tile_matches_the_delegate_tile():
    """The pre-rendered tile is pixel-equivalent to what the delegate draws itself."""
    import main
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QPixmap
    view = QListView()
    deleg = main.ApplePhotoDelegate(view)
    src = _sample_image()
    for meta in ({"id": 1, "type": "photo", "is_fav": False}, {"id": 2, "type": "video", "is_fav": True}):
        side, dpr = 195, 2.0
        radius = max(6.0, min(14.0, side * 0.07))
        ref = deleg._tile(QPixmap.fromImage(src), QSize(side, side), radius, meta, dpr).toImage()
        pre = main.compose_tile_image(src, round(side * dpr), round(side * dpr), radius, dpr, main.tile_shade(meta))
        assert (pre.width(), pre.height()) == (ref.width(), ref.height())
        a = ref.convertToFormat(pre.format())
        diff = 0
        for y in range(0, pre.height(), 7):
            for x in range(0, pre.width(), 7):
                ca, cb = a.pixelColor(x, y), pre.pixelColor(x, y)
                diff = max(diff, abs(ca.red() - cb.red()), abs(ca.green() - cb.green()), abs(ca.blue() - cb.blue()),
                           abs(ca.alpha() - cb.alpha()))
        # the delegate tile also carries the badge icons (top right / bottom left): skip them
        if meta["type"] == "photo":
            assert diff <= 3, f"photo tile differs by {diff}"
    view.deleteLater()


def test_model_keeps_and_serves_pretiles():
    import main
    model = main.ApplePhotoModel()
    rec = (7, "a.jpg", "C:/x/a.jpg", "photo", None, "C:/x/t.jpg", "2026-01-01 10:00:00", 0)
    model.set_records([rec])
    model.set_tile_geom(390, 390, 13.65, 2.0)
    tile = main.compose_tile_image(_sample_image(), 390, 390, 13.65, 2.0, (False, False))
    model._on_thumb_loaded(7, _sample_image(), tile)
    pm = model.pretile(7, 390, 390, 13.65, 2.0, (False, False))
    assert pm is not None and pm.width() == 390
    assert model.pretile(7, 390, 390, 13.65, 2.0, (False, True)) is None, "favourite shade changed: re-render"
    model.set_tile_geom(300, 300, 10.5, 2.0)
    assert model.pretile(7, 390, 390, 13.65, 2.0, (False, False)) is None, "new cell size drops old tiles"


def test_loading_tiles_blit_a_cached_placeholder():
    import main
    from PyQt6.QtCore import QSize
    view = QListView()
    deleg = main.ApplePhotoDelegate(view)
    a = deleg._placeholder_tile(QSize(195, 195), 13.65, 2.0)
    b = deleg._placeholder_tile(QSize(195, 195), 13.65, 2.0)
    assert a is b and a.width() == 390
    view.deleteLater()
