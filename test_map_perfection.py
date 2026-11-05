"""Map perfection roadmap (2026-11-05): predictive prefetch, exponential momentum, fast LOD
recovery + HiDPI underlay, label thinning during a 3D orbit.

Focused guards; live checks run on the real map_vault.html in QtWebEngine (network blocked).
"""
import math
import os
import re
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _const(html, name):
    m = re.search(r"\b" + name + r"\s*=\s*([0-9.]+)", html)
    assert m, name
    return float(m.group(1))


try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    import sys
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:                                               # pragma: no cover
    HAS_QT = False


@pytest.fixture(scope="module")
def m():
    if not HAS_QT:
        pytest.skip("Qt není dostupné")
    import main  # noqa: F401
    from test_patch11_maps import ShownMapPage
    page = ShownMapPage((1000, 700))
    yield page
    page.js("map.setTerrain(null); 0")
    page.close()


def _wait_js(m, cond, ms=4000):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000:
        if m.js(cond):
            return True
        _app.processEvents()
        time.sleep(0.02)
    return False


# ── static guards ─────────────────────────────────────────────────────────────────────────────

def test_full_ratio_returns_within_250_ms_of_rest():
    html = _read("map_vault.html")
    assert _const(html, "MD_SETTLE_MS") + _const(html, "RATIO_REST_MS") <= 250
    assert _const(html, "RES_REST_MS") <= 250


def test_momentum_and_prediction_are_wired_into_the_map():
    html = _read("map_vault.html")
    assert "installGlobeAroundGuard();\n    installMomentum();" in html
    render = html[html.index("map.on('render', function () {"):]
    render = render[:render.index("});")]
    assert "predictMotion(now);" in render and "thinTick();" in render
    rest = html[html.index("function cameraRested()"):]
    rest = rest[:rest.index("\n  }\n")]
    assert "thinLabels(false);" in rest and "scheduleIdleBuffer();" in rest


def test_thinning_never_reloads_the_vector_source():
    # setLayoutProperty / setLayerZoomRange -> Style._updateLayer -> whole source re-parsed
    html = _read("map_vault.html")
    body = html[html.index("function thinLabels(on)"):]
    body = body[:body.index("\n  }\n")]
    assert "setLayoutProperty" not in body and "setLayerZoomRange" not in body
    assert "l.minzoom = THIN_ZOOM;" in body


def test_thinning_keeps_places_and_pins():
    html = _read("map_vault.html")
    rx = re.compile(re.search(r"var THIN_LAYERS = /(.+)/;", html).group(1))
    for hidden in ("poi_r1", "poi_transit", "highway-name-major", "highway-shield-non-us", "road_shield_us",
                   "waterway_line_label", "water_name_line_label", "label_village", "label_other", "airport"):
        assert rx.search(hidden), hidden
    for kept in ("label_city", "label_city_capital", "label_town", "label_state", "label_country_1",
                 "water_name_point_label", "pins", "pins-halo"):
        assert not rx.search(kept), kept


# ── momentum (live: MapLibre's real inertia handler) ─────────────────────────────────────────

def test_momentum_plan_is_continuous_and_settles_subpixel(m):
    r = m.js("""(function () {
        var p = momentumPlan(2000), du = 1e-4;
        var v0 = p.amount * p.easing(du) / (du * p.duration) * 1000;            // px/s at release
        var vEnd = p.amount * (1 - p.easing(1 - du)) / (du * p.duration) * 1000;
        var mono = true, prev = 0;
        for (var i = 1; i <= 200; i++) { var e = p.easing(i / 200); if (e < prev) mono = false; prev = e; }
        return [v0, vEnd, mono, p.duration, p.amount, p.easing(0), p.easing(1)];
    })()""")
    v0, v_end, mono, duration, amount, e0, e1 = r
    assert abs(v0 - 2000) / 2000 < 0.01, "first coast frame must move at the release speed"
    assert v_end < 10, "the coast must die out, not stop abruptly"
    assert mono and e0 == 0 and e1 == 1
    assert 300 < amount < 900 and duration <= 2400


def _fling(m, samples, pause_ms=0):
    """Feed MapLibre's inertia buffer with drag samples [(age_ms, dx, dy)] and end the move."""
    js = "(function () { var inr = map.handlers._inertia, now = performance.now(), P = maplibregl.Point;" \
         " inr._inertiaBuffer = [" + ",".join(
             "{time: now - %d, settings: {panDelta: new P(%s, %s)}}" % (age + pause_ms, dx, dy)
             for age, dx, dy in samples) + "];" \
         " var r = inr._onMoveEnd(map.dragPan._inertiaOptions);" \
         " return r ? {d: r.duration, e: r.easing(0.001), off: [r.offset.x, r.offset.y]} : null; })()"
    return m.js(js)


def test_fling_coasts_exponentially_at_release_speed(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 11, pitch: 0, bearing: 0}); 0")
    assert m.js("getMomentumState().installed")
    # 8 samples, 12 ms apart, 24 px each -> 2000 px/s to the right
    r = _fling(m, [(84 - 12 * i, 24, 0) for i in range(8)])
    assert r is not None
    st = m.js("getMomentumState().last")
    assert st["coast"] and abs(st["speed"] - 2000) < 60
    off = math.hypot(*r["off"])
    v0 = off * r["e"] / (0.001 * r["d"]) * 1000
    assert abs(v0 - 2000) / 2000 < 0.03, v0
    assert r["off"][0] > 0 and abs(r["off"][1]) < 1


def test_release_after_a_pause_does_not_drift(m):
    m.js("map.stop(); 0")
    assert _fling(m, [(84 - 12 * i, 24, 0) for i in range(8)], pause_ms=70) is None
    assert m.js("getMomentumState().last.coast") is False


def test_pinch_fling_keeps_maplibres_curve(m):
    m.js("map.stop(); 0")
    r = m.js("""(function () { var inr = map.handlers._inertia, now = performance.now(), P = maplibregl.Point;
        inr._inertiaBuffer = [0, 1, 2, 3].map(function (i) {
            return {time: now - 40 + i * 12, settings: {panDelta: new P(10, 0), zoomDelta: 0.05}}; });
        var r = inr._onMoveEnd(map.dragPan._inertiaOptions);
        return [!!r, getMomentumState().last]; })()""")
    assert r[0] and r[1] is None


# ── prediction ───────────────────────────────────────────────────────────────────────────────

def test_wheel_target_keeps_the_focal_point(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 10, pitch: 0, bearing: 0}); 0")
    same = m.js("wheelTargetPose(11, null).c")
    assert abs(same[0] - 14.4) < 1e-6 and abs(same[1] - 50.08) < 1e-6
    r = m.js("""(function () { var a = map.unproject(new maplibregl.Point(900, 600)), c = map.getCenter();
        var p = wheelTargetPose(11, new maplibregl.Point(900, 600));
        return [mercX(p.c[0]), mercX(c.lng), mercX(a.lng), mercY(p.c[1]), mercY(c.lat), mercY(a.lat)]; })()""")
    px, cx, ax, py, cy, ay = r
    tol = 2 / (512 * 2 ** 10)          # 2 CSS px at z10: analytic offset vs MapLibre's unproject
    assert abs(px - (ax + (cx - ax) / 2)) < tol and abs(py - (ay + (cy - ay) / 2)) < tol


def test_drag_velocity_predicts_the_view_ahead(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 11, pitch: 0, bearing: 0}); pred.ring = []; pred.key = ''; pred.at = 0; 0")
    r = m.js("""(function () { var t = performance.now() + 10000, before = getPredictState().poses;
        for (var i = 0; i < 4; i++) { map.jumpTo({center: [14.4 + i * 0.01, 50.08]}); predictMotion(t + i * 40); }
        flushPredict(); var s = getPredictState(); return [s.poses - before, s.lastPoses[0].c[0], map.getCenter().lng]; })()""")
    n, predicted_lng, now_lng = r
    assert n >= 1
    # 0.01 deg per 40 ms = 0.25 deg/s -> ~0.11 deg ahead after PREDICT_LOOKAHEAD_S (0.45 s)
    assert 0.08 < predicted_lng - now_lng < 0.14, (predicted_lng, now_lng)


def test_zooming_prefetches_the_next_level(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 10.2}); pred.ring = []; pred.key = ''; pred.at = 0; 0")
    z = m.js("""(function () { var t = performance.now() + 20000;
        for (var i = 0; i < 4; i++) { map.jumpTo({zoom: 10.2 + i * 0.03}); predictMotion(t + i * 40); }
        flushPredict(); return getPredictState().lastPoses[0].z; })()""")
    assert z >= 11


def test_landing_and_idle_buffer_cover_more_than_the_view(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 11, pitch: 0, bearing: 0}); 0")
    n = m.js("""(function () { var before = getPredictState().poses;
        var queued = predictLanding({offset: new maplibregl.Point(300, 0), duration: 900});
        var pending = getPredictState().jobs; flushPredict();
        if (queued !== 2 || pending < 1) return ['not deferred', queued, pending];
        var s = getPredictState(); return [s.poses - before, s.lastPoses.length]; })()""")
    # content dragged right -> the camera lands to the west; 300 px at z11 = 300 / 2^20 of the world
    west = m.js("(function () { predictLanding({offset: new maplibregl.Point(300, 0)}); flushPredict();"
                " var s = getPredictState(); return [s.lastPoses[1].c[0], s.lastPoses[0].c[0], map.getCenter().lng]; })()")
    assert west[0] < west[2] - 0.05 and abs((west[2] - west[1]) - 300 / 2 ** 20 * 360) < 0.005, west
    assert n == [2, 2], "landing + midpoint, worked off later (idle), not in the release handler"
    lng = m.js("""(function () { var c = map.getCenter(); return [false, true].map(function (wide) {
        var pts = []; prefetchTr = map.transform.clone();
        frustumTiles({c: [c.lng, c.lat], z: 11, p: 0, b: 0, ext: wide ? IDLE_BUFFER_PX : 0}, null, pts, 9, 7);
        prefetchTr = null;
        var xs = pts.map(function (p) { return p.ll[0]; }); return [Math.min.apply(null, xs), Math.max.apply(null, xs)]; }); })()""")
    narrow, wide = lng
    assert wide[0] < narrow[0] and wide[1] > narrow[1]


def test_prediction_goes_to_the_front_of_the_queue(m):
    r = m.js("""(function () { var saved = PREFETCH_PARALLEL; PREFETCH_PARALLEL = 0;
        var q0 = prefetchQueue.slice(); prefetchQueue = ['tail-a', 'tail-b'];
        var src = tileSourceIds.map(function (id) { return map.getSource(id); })
                               .filter(function (s) { return s && Array.isArray(s.tiles) && s.tiles.length; })[0];
        var n = prefetchPoints([{ll: [-120.5, 37.2], z: 9.3}], true);
        var out = [n, prefetchQueue[0], prefetchQueue[prefetchQueue.length - 1]];
        prefetchQueue = q0; PREFETCH_PARALLEL = saved; return out; })()""")
    n, first, last = r
    if n:
        assert first not in ("tail-a", "tail-b") and last == "tail-b"


# ── label thinning ───────────────────────────────────────────────────────────────────────────

def test_orbit_thins_minor_labels_without_a_style_reload(m):
    if not m.js("getThinState().supported"):
        pytest.skip("MapLibre version not the one thinning is written against")
    m.js("""map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 12, pitch: 50, bearing: 0});
        if (!map.getSource('thin-test')) {
          map.addSource('thin-test', {type: 'geojson', data: {type: 'FeatureCollection', features: [
            {type: 'Feature', properties: {n: 'x'}, geometry: {type: 'Point', coordinates: [14.4, 50.08]}}]}});
          map.addLayer({id: 'poi_thin_test', type: 'symbol', source: 'thin-test', minzoom: 3, layout: {'text-field': ['get', 'n']}});
          map.addLayer({id: 'label_city_thin_test', type: 'symbol', source: 'thin-test', layout: {'text-field': ['get', 'n']}});
        }
        window.__styledata = 0; map.on('styledata', function () { window.__styledata++; }); 0""")
    _wait_js(m, "!map.isMoving()", 1000)
    time.sleep(0.2); _app.processEvents()
    m.js("window.__styledata = 0; thinLabelsForTest(true); thinLabelsForTest(false); map.redraw(); 0")
    time.sleep(0.15); _app.processEvents()
    assert m.js("window.__styledata") == 0, "thinning must not touch the style (source reload)"
    m.js("orbitBy(4, 0); orbitBy(4, 0); 0")
    st = m.js("getThinState()")
    assert st["on"] and st["count"] >= 1
    assert m.js("map.style._layers['poi_thin_test'].minzoom") == 24.5
    assert m.js("map.style._layers['label_city_thin_test'].minzoom") in (None, 0)
    assert _wait_js(m, "!getOrbitState().on", 1500)
    assert not m.js("getThinState().on")
    assert m.js("map.style._layers['poi_thin_test'].minzoom") == 3
    m.js("map.removeLayer('poi_thin_test'); map.removeLayer('label_city_thin_test'); map.removeSource('thin-test'); 0")


def test_flat_orbit_and_plain_pans_keep_labels(m):
    m.js("map.stop(); map.jumpTo({center: [14.4, 50.08], zoom: 12, pitch: 10, bearing: 0}); 0")
    m.js("orbitBy(4, 0); 0")
    assert not m.js("getThinState().on")
    _wait_js(m, "!getOrbitState().on", 1500)


# ── underlay ─────────────────────────────────────────────────────────────────────────────────

def test_underlay_resolution_follows_the_panel(m):
    r = m.js("""(function () { var saved = PERF.map_underlay; PERF.map_underlay = true;
        var st = {sources: {}, layers: [{id: 'background', type: 'background'}]};
        addUnderlay(st); PERF.map_underlay = saved;
        return [st.sources.underlay.tileSize, st.sources.underlay.maxzoom, window.devicePixelRatio >= 1.5]; })()""")
    assert r[:2] == ([256, 10] if r[2] else [512, 9])
    # and the HiDPI rule itself, independent of this test window's DPR
    assert m.js("""(function () { var d = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio');
        Object.defineProperty(window, 'devicePixelRatio', {value: 2, configurable: true});
        var hi = underlayHiDpi(); if (d) Object.defineProperty(window, 'devicePixelRatio', d); else delete window.devicePixelRatio;
        return hi; })()""")


# ── pacing: back to 120 Hz when the frame fits ───────────────────────────────────────────────

def _pace_run(m, k0, cost):
    return m.js("""(function () {
      vs.ms = 1000 / 120; resetPace(); pace.k = %d; pace.calmNeed = 2; pace.changedAt = 0;
      pace.moving = false; pace.lastRender = 0;
      var t = 1000; paceTick(t, %s);
      for (var i = 0; i < 80; i++) { t += paceMs(); paceTick(t, %s); }
      var k = pace.k; paceSettle(); resetPace(); return k; })()""" % (k0, cost, cost))


def test_pacer_returns_to_full_rate_when_the_orbit_fits(m):
    assert _pace_run(m, 2, 6.5) == 1, "6.5 ms p90 fits a 120 Hz frame (measured z14 orbit, buildings off)"


def test_pacer_holds_half_rate_when_it_does_not(m):
    assert _pace_run(m, 2, 7.5) == 2
