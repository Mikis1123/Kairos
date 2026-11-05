"""Founder field audit 2026-11-04 (real app, real library): globe easing guard, WebView2 busy
retry, thumbnail enum race. The tile store's off-thread size count is in test_tile_store.py."""
import os
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    import sys
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:                                               # pragma: no cover
    HAS_QT = False


@pytest.fixture(scope="module")
def mappage():
    if not HAS_QT:
        pytest.skip("Qt není dostupné")
    import main  # noqa: F401  (MapBridge)
    from test_patch11_maps import ShownMapPage
    m = ShownMapPage((1000, 700))
    yield m
    m.close()


# ── globe: easeTo({around}) never reaches MapLibre ──

def test_globe_around_guard_is_installed_on_the_map():
    html = _read("map_vault.html")
    assert "installGlobeAroundGuard();" in html
    assert "if (opts && opts.around && globeRendering())" in html


def test_globe_ease_options_turn_the_anchor_into_a_center(mappage):
    # zoom in by 1 towards an anchor: the new center is halfway between center and anchor
    o = mappage.js("JSON.stringify(globeEaseOptions({zoom: 4, around: [20, 10], duration: 300}, 3, {lng: 10, lat: 0}))")
    import json
    o = json.loads(o)
    assert "around" not in o and o["duration"] == 300
    assert o["center"] == pytest.approx([15, 5])
    # zoom out by 1: the anchor stays put, the center moves away from it
    o = json.loads(mappage.js("JSON.stringify(globeEaseOptions({zoom: 2, around: [20, 10]}, 3, {lng: 10, lat: 0}))"))
    assert o["center"] == pytest.approx([0, -10])
    # across the antimeridian: the short way round
    o = json.loads(mappage.js("JSON.stringify(globeEaseOptions({zoom: 4, around: [-170, 0]}, 3, {lng: 170, lat: 0}))"))
    assert o["center"][0] == pytest.approx(-180) or o["center"][0] == pytest.approx(180)
    # bearing inertia without zoom: only the anchor is dropped
    o = json.loads(mappage.js("JSON.stringify(globeEaseOptions({bearing: 30, around: [1, 2]}, 3, {lng: 0, lat: 0}))"))
    assert o == {"bearing": 30}


def test_double_click_on_the_globe_logs_no_easing_warning(mappage):
    m = mappage
    m.js("window.__warns = []; var w0 = console.warn; console.warn = function () {"
         " window.__warns.push(Array.prototype.join.call(arguments, ' ')); return w0.apply(console, arguments); };"
         " map.jumpTo({center: [15, 50], zoom: 2, pitch: 0}); 0")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 2 and not m.js("map.transform.isGlobeRendering"):
        _app.processEvents()
        time.sleep(0.02)
    if not m.js("map.transform.isGlobeRendering"):
        pytest.skip("globe projection not active in this page")
    z0 = m.js("map.getZoom()")
    m.js("map.easeTo({zoom: map.getZoom() + 1, around: map.unproject([700, 250]), duration: 50}); 0")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 1.5:
        _app.processEvents()
        time.sleep(0.02)
    assert m.js("map.getZoom()") == pytest.approx(z0 + 1, abs=0.05)
    assert not [w for w in m.js("window.__warns") if "around a point" in w]


# ── WebView2: a busy user data folder is retried, not dropped to QtWebEngine (60 fps) ──

def test_busy_controller_error_is_recognised_and_retried():
    import map_host
    assert map_host.WebView2Host.is_busy_error(Exception("... (HRESULT: 0x800700AA) ..."))
    assert not map_host.WebView2Host.is_busy_error(Exception("0x8007139F disposed"))
    assert sum(map_host.BUSY_RETRY_MS) >= 5000
    src = _read("map_host.py")
    assert "self.is_busy_error(task.Exception) and self._busy_retries < len(BUSY_RETRY_MS)" in src


# ── thumbnails: the conversion format comes from sip's own enum class ──

def test_qimage_to_pil_uses_the_enum_class_sip_returns():
    if not HAS_QT:
        pytest.skip("Qt není dostupné")
    from PyQt6.QtGui import QImage
    import imaging
    q = QImage(30, 20, QImage.Format.Format_ARGB32)
    q.fill(0)
    fmt = imaging._qt_format(q, True)
    assert type(fmt) is type(q.format())
    assert imaging.qimage_to_pil(q).size == (30, 20)
    q2 = QImage(30, 20, QImage.Format.Format_RGB32)
    q2.fill(0)
    assert imaging.qimage_to_pil(q2).mode == "RGB"
