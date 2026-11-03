"""Map engine: orbit frame pipeline (terrain RTT guard, orbit session, unchanged-pin skip) and the
spring-driven cluster split / merge. Measured with eval/bench_map_orbit.py."""
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
    _app = QApplication.instance() or QApplication(sys.argv)   # Chromium potřebuje argv[0]
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
    m.js("reduceMotion = false; setMarkers([]); 0")
    m.close()


def _wait_js(m, cond, ms=4000):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000:
        if m.js(cond):
            return True
        _app.processEvents()
        time.sleep(0.02)
    return False


# ── static guards: the mechanisms stay wired ──────────────────────────────────────────────────

def test_terrain_rtt_guard_is_wired():
    html = _read("map_vault.html")
    assert "map.on('terrain', guardTerrainRtt)" in html
    assert "map._terrainDataCallback = guarded" in html
    # only flat layer types that MapLibre draws into the terrain textures may free them
    assert "background: 1, fill: 1, line: 1, raster: 1, hillshade: 1, 'color-relief': 1" in html


def test_orbit_bench_exists_and_measures_the_rtt_pool():
    src = _read(os.path.join("eval", "bench_map_orbit.py"))
    for k in ("getOrCreateFreeObject", "texImage2D", "_render", "ORBIT_TUNING_OFF", "frames_with_rtt_pct"):
        assert k in src


# ── live page ─────────────────────────────────────────────────────────────────────────────────

def test_rtt_guard_drops_tile_events_of_sources_that_never_draw_into_terrain(mappage):
    m = mappage
    m.js("setMarkers([{id: 1, lat: 49.05, lon: 13.52, thumb: ''}]); 0")
    m.js("map.stop(); map.jumpTo({center: [13.52, 49.05], zoom: 10, pitch: 50, bearing: 0}); "
         "map.setTerrain({source: 'dem', exaggeration: 1}); 0")
    assert _wait_js(m, "getRttGuard().active", 3000), "guard installed on the 'terrain' event"
    res = m.js("""(function () {
      var g0 = getRttGuard(), tiles = map.terrain.tileManager, cleared = 0;
      var keep = tiles.freeRtt; tiles.freeRtt = function () { cleared++; };
      var fake = { tileID: { canonical: { z: 3, x: 4, y: 2 }, key: 'x', equals: function () { return false; },
                             isChildOf: function () { return false; } } };
      map._terrainDataCallback({ dataType: 'source', sourceId: 'pins', tile: fake });          // symbol/circle only
      var afterPins = cleared;
      var flat = map.getStyle().layers.filter(function (l) {
        return l.source && l.source !== 'pins' && ['fill', 'line', 'raster', 'hillshade'].indexOf(l.type) >= 0 &&
               sourceDrawsIntoTerrain(l.source); })[0];
      map._terrainDataCallback({ dataType: 'source', sourceId: flat.source, tile: fake });      // draws into terrain
      var afterVec = cleared;
      map._terrainDataCallback({ dataType: 'style' });                                          // paint change
      tiles.freeRtt = keep;
      var g1 = getRttGuard();
      return { pins: afterPins, vec: afterVec, style: cleared, skipped: g1.skipped - g0.skipped };
    })()""")
    assert res["pins"] == 0, "pins (symbol + circle) never invalidate terrain textures"
    assert res["vec"] == 1, "a vector tile with fill/line layers still frees its terrain tiles"
    assert res["style"] == 2, "style changes still free everything"
    assert res["skipped"] == 1
    m.js("map.setTerrain(null); 0")


def test_orbit_session_rests_once_and_unchanged_pins_skip_setdata(mappage):
    m = mappage
    m.js("reduceMotion = false; map.setTerrain(null); "
         "setMarkers([{id: 1, lat: 49.25, lon: 13.40, thumb: ''}, {id: 2, lat: 49.40, lon: 13.70, thumb: ''}]); 0")
    m.js("map.stop(); map.jumpTo({center: [13.5, 49.3], zoom: 9, pitch: 30, bearing: 0}); 0")
    _wait_js(m, "!map.isMoving()", 1000)
    r0 = m.js("getPinRenders()")
    m.js("window.__rest = 0; window.__origRest = cameraRested;"
         " cameraRested = function () { window.__rest++; return window.__origRest(); }; 0")
    try:
        for _ in range(12):
            m.js("orbitBy(4, 1); 0")
        assert m.js("getOrbitState().on"), "orbit steps form one session"
        assert m.js("window.__rest") == 0, "no rest path per orbit step (jumpTo fires moveend)"
        assert _wait_js(m, "!getOrbitState().on", 1500)
        assert m.js("window.__rest") == 1, "exactly one rest when the orbit stops"
        r1 = m.js("getPinRenders()")
        assert r1["applied"] == r0["applied"], "same zoom, same clusters: no setData"
        assert r1["skipped"] >= r0["skipped"] + 1
    finally:
        m.js("cameraRested = window.__origRest; map.jumpTo({bearing: 0, pitch: 0}); 0")


def test_detail_logic_treats_an_orbit_session_as_motion(mappage):
    m = mappage
    m.js("orbitBy(2, 0); 0")
    assert m.js("detailMoving()")
    m.js("endOrbitSession(); 0")
    assert not m.js("detailMoving()")


def _spread_markers():
    # four photos ~1-2 km apart: one cluster at z8, four pins at z15
    pts = [(49.250, 13.400), (49.262, 13.418), (49.244, 13.430), (49.257, 13.385)]
    return "[" + ",".join("{id: %d, lat: %f, lon: %f, thumb: ''}" % (i + 1, a, b) for i, (a, b) in enumerate(pts)) + "]"


def test_zoom_in_children_spring_out_of_the_parent(mappage):
    m = mappage
    m.js("reduceMotion = false; map.setTerrain(null); map.stop(); "
         "map.jumpTo({center: [13.41, 49.252], zoom: 8, pitch: 0, bearing: 0}); 0")
    m.js(f"setMarkers({_spread_markers()}); map.jumpTo({{center: [13.41, 49.252], zoom: 8}}); 0")
    _wait_js(m, "!getPinSpring().active", 2500)
    assert m.js("getClusterCount()") == 1
    m.js("map.jumpTo({center: [13.41, 49.252], zoom: 14.5}); 0")       # zoomend -> render()
    st = m.js("getPinSpring()")
    assert st["active"], "split runs as a spring"
    assert st["split"] == 4 and st["merge"] == 0 and st["fade"] == 1, "children burst out, the parent dissolves"
    hidden = m.js("JSON.stringify(map.getPaintProperty('pins', 'icon-opacity'))")
    assert '"anim"' in hidden, "GL pins under the sprites stay hidden until they land"
    assert m.js("document.querySelectorAll('#pinSprites .pin-sprite').length") == 5
    assert _wait_js(m, "!getPinSpring().active", 2500), "settles"
    last = m.js("getPinSpring().last")
    assert 0 < last["overshoot"] < 0.25, "a slight bounce past the target, not a wobble"
    assert last["ms"] < 1400
    assert m.js("map.getPaintProperty('pins', 'icon-opacity')") == 1, "GL pins back at full opacity"
    assert m.js("document.querySelectorAll('#pinSprites .pin-sprite').length") == 0


def test_zoom_out_children_collapse_into_the_parent(mappage):
    m = mappage
    m.js("map.jumpTo({center: [13.41, 49.252], zoom: 14.5}); 0")
    _wait_js(m, "!getPinSpring().active", 2500)
    assert m.js("getClusterCount()") == 4
    m.js("map.jumpTo({center: [13.41, 49.252], zoom: 8}); 0")
    st = m.js("getPinSpring()")
    # the children fly into the new centroid; one already within SPRING_MIN_PX of it just fades
    assert st["active"] and st["split"] == 0 and st["merge"] >= 2 and st["merge"] + st["fade"] == 4
    assert _wait_js(m, "!getPinSpring().active", 2500)
    assert m.js("getClusterCount()") == 1
    assert m.js("map.getPaintProperty('pins', 'icon-opacity')") == 1


def test_reduced_motion_keeps_the_instant_path(mappage):
    m = mappage
    m.js("reduceMotion = true; map.jumpTo({center: [13.41, 49.252], zoom: 14.5}); 0")
    try:
        assert not m.js("getPinSpring().active")
        assert m.js("document.querySelectorAll('#pinSprites .pin-sprite').length") == 0
    finally:
        m.js("reduceMotion = false; 0")


def test_spring_math_is_stable_and_damped(mappage):
    m = mappage
    res = m.js("""(function () {
      var out = {};
      [1 / 30, 1 / 60, 1 / 120, 1 / 240].forEach(function (dt) {
        var s = { u: 0, v: 0 }, peak = 0, t = 0;
        while (t < 1.4) { springStep(s, dt); peak = Math.max(peak, s.u); t += dt; }
        out[Math.round(1 / dt)] = { peak: +peak.toFixed(3), end: +s.u.toFixed(4) };
      });
      return out;
    })()""")
    for hz, r in res.items():
        assert 1.03 < r["peak"] < 1.2, f"{hz} Hz: overshoot in the Apple range"
        assert abs(r["end"] - 1) < 0.005, f"{hz} Hz: settled"
    peaks = [r["peak"] for r in res.values()]
    assert max(peaks) - min(peaks) < 0.01, "same motion at every refresh rate (fixed sub-steps)"
