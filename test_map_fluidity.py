"""Map fluidity under CPU pressure (2026-11-04, founder: "map lags and stutters, also on battery").

Measured with eval/bench_map_input.py (real OS input) and an in-page A/B harness at a 4x slower
renderer CPU, Sumava z12 at 50 deg with 3D terrain:
  * RenderPool cascade: one invalidated terrain tile re-drew the whole RTT list (977 steals / 200 fr)
  * every 'style' event wiped all RTT textures (pin fades: 45 wipes per pan; relief shed: 21 per drag)
  * 256 px relief tiles churned the RTT fingerprints at the view edge (288 re-draws per 3 s pan)
  * pin fades re-laid out the pins source on every frame (data-driven paint change)
  * the pacing governor never stepped back up after one spike per gesture
  * Windows may EcoQoS the windowless WebView2 renderer on battery (Chromium leaves it to the OS)
"""
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
    m.js("map.setTerrain(null); setMarkers([]); 0")
    m.close()


def _wait_js(m, cond, ms=4000):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < ms / 1000:
        if m.js(cond):
            return True
        _app.processEvents()
        time.sleep(0.02)
    return False


def _terrain_view(m):
    m.js("setMarkers([{id: 1, lat: 49.05, lon: 13.52, thumb: ''}]); map.stop(); "
         "map.jumpTo({center: [13.52, 49.05], zoom: 10, pitch: 50, bearing: 0}); "
         "map.setTerrain({source: 'dem', exaggeration: 1}); 0")
    assert _wait_js(m, "getRttGuard().active && getRttGuard().pool", 4000)
    _wait_js(m, "map.loaded() && map.areTilesLoaded()", 8000)
    m.js("map.redraw(); map.redraw(); 0")


# ── static guards ─────────────────────────────────────────────────────────────────────────────

def test_relief_uses_the_terrain_tile_grid():
    html = _read("map_vault.html")
    assert "style.sources['dem-shade'] = { type: 'raster-dem', tiles: [DEM_TILES], tileSize: 512" in html


def test_relief_is_not_shed_while_terrain_caches_it():
    html = _read("map_vault.html")
    body = html[html.index("function shedRelief(on)"):]
    assert body.index("if (on && map.getTerrain()) return;") < body.index("reliefIds().forEach")


def test_pin_fades_write_feature_state_not_paint():
    html = _read("map_vault.html")
    assert "var PIN_OPACITY = ['case', ['==', ['get', 'stay'], 1], 1, ['coalesce', ['feature-state', 'op'], 1]];" in html
    assert "pinOpacity(" not in html, "no per-frame data-driven expression"
    assert "map.setPaintProperty('pins', 'icon-opacity'" not in html


def test_main_gives_the_map_processes_interactive_qos():
    src = _read("main.py")
    assert "pool.submit(power.set_web_processes_interactive, high)" in src, (
        "off the GUI thread: the process-table walk held every tab switch ~12 ms")
    sync = src[src.index("def _sync_map_active"):src.index("def _push_map_fps_cap")]
    assert "self._map_web_qos(False)" in sync, "hidden map: back to the system default"
    push = src[src.index("def _push_map_fps_cap"):src.index("def _map_fps_cap")]
    assert "self._map_web_qos(True)" in push, "re-applied on the 5 s power poll while shown"


# ── power.py: process QoS ─────────────────────────────────────────────────────────────────────

def test_web_process_ids_walks_only_this_apps_tree():
    import power
    table = [(100, 1, "python.exe"), (200, 100, "msedgewebview2.exe"), (201, 200, "msedgewebview2.exe"),
             (202, 200, "msedgewebview2.exe"), (300, 100, "exiftool.exe"), (400, 7, "msedgewebview2.exe"),
             (500, 300, "qtwebengineprocess.exe")]
    assert sorted(power.web_process_ids(100, table)) == [200, 201, 202, 500]
    assert power.web_process_ids(999, table) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows power throttling")
def test_interactive_qos_round_trip_on_a_real_web_process(mappage):
    import power
    pids = power.web_process_ids()
    assert pids, "the test page runs in this process's web-engine children"
    assert power.set_web_processes_interactive(True) >= 1
    st = [power.process_qos_state(p) for p in pids]
    on = [s for s in st if s and s[0] & 1 and not s[1] & 1]
    assert on, "execution speed controlled and not throttled (HighQoS)"
    assert power.set_web_processes_interactive(False) >= 1
    off = [power.process_qos_state(p) for p in pids]
    assert all(not (s[0] & 1) for s in off if s), "back to the system's own decision"


# ── live page: terrain RTT cache ──────────────────────────────────────────────────────────────

def test_fp_subset(mappage):
    m = mappage
    assert m.js("fpSubset('a,b#0', 'a,b,c#0')") is True, "a tile left the view: texture still valid"
    assert m.js("fpSubset('a,d#0', 'a,b,c#0')") is False, "a tile came in: re-draw"
    assert m.js("fpSubset('a#1', 'a,b#0')") is False, "source revision changed"
    assert m.js("fpSubset('a#0', '')") is False


def test_one_invalidated_tile_redraws_one_texture_not_the_list(mappage):
    m = mappage
    _terrain_view(m)
    r = m.js("""(function () {
      var tm = map.terrain.tileManager, tiles = tm.getRenderableTiles(), g0 = getRttGuard().draws;
      map.redraw();
      var idle = getRttGuard().draws - g0;
      tiles[0].rtt = [];
      var g1 = getRttGuard().draws;
      map.redraw();
      return { tiles: tiles.length, idle: idle, one: getRttGuard().draws - g1,
               stacks: map.painter.renderToTexture._stacks.length, steals: getRttGuard().steals };
    })()""")
    assert r["tiles"] >= 4
    assert r["idle"] == 0, "nothing changed: every texture comes from the cache"
    assert 1 <= r["one"] <= r["stacks"], "only the invalidated tile is re-drawn (no pool cascade)"


def test_symbol_only_style_change_keeps_the_textures(mappage):
    m = mappage
    _terrain_view(m)
    r = m.js("""(function () {
      map.addSource('kt-fill', { type: 'geojson', data: { type: 'Feature', properties: {},
        geometry: { type: 'Polygon', coordinates: [[[13.4, 49.0], [13.6, 49.0], [13.6, 49.1], [13.4, 49.0]]] } } });
      map.addLayer({ id: 'kt-fill', type: 'fill', source: 'kt-fill', paint: { 'fill-color': '#888' } }, 'pins-halo');
      map.redraw();
      var g0 = getRttGuard();
      map.setPaintProperty('pins', 'icon-opacity', 0.97); map.redraw();        // symbol layer
      var g1 = getRttGuard();
      map.setPaintProperty('kt-fill', 'fill-opacity', 0.5); map.redraw();      // RTT layer
      var g2 = getRttGuard();
      map.setPaintProperty('pins', 'icon-opacity', PIN_OPACITY);
      map.removeLayer('kt-fill'); map.removeSource('kt-fill'); map.redraw();
      return { keptSym: g1.styleKept - g0.styleKept, wipeSym: g1.styleWipes - g0.styleWipes,
               wipeFill: g2.styleWipes - g1.styleWipes };
    })()""")
    assert r["keptSym"] >= 1 and r["wipeSym"] == 0, "a pin/label paint change never touches an RTT texture"
    assert r["wipeFill"] == 1, "a fill paint change still re-draws the textures"


def test_new_pin_image_keeps_the_textures(mappage):
    m = mappage
    _terrain_view(m)
    r = m.js("""(function () {
      var g0 = getRttGuard();
      map.addImage('kairos-test-pin', { width: 2, height: 2, data: new Uint8Array(16) }); map.redraw();
      var g1 = getRttGuard();
      map.removeImage('kairos-test-pin');
      return { kept: g1.styleKept - g0.styleKept, wiped: g1.styleWipes - g0.styleWipes };
    })()""")
    assert r["wiped"] == 0, "an icon no RTT pattern uses doesn't wipe the terrain textures"


def test_relief_stays_visible_when_motion_detail_starts_in_3d(mappage):
    m = mappage
    _terrain_view(m)
    vis = m.js("shedRelief(true); var v = map.getLayoutProperty('relief', 'visibility'); "
               "shedRelief(false); v === undefined ? 'visible' : v")
    assert vis == "visible"


def test_underlay_stops_where_it_is_invisible_and_the_film_lifts_it(mappage):
    html = _read("map_vault.html")
    assert "id: 'underlay', type: 'raster', source: 'underlay', maxzoom: UNDERLAY_REST_MAX_ZOOM," in html
    assert "map.setLayerZoomRange('underlay', 0, on ? 24 : UNDERLAY_REST_MAX_ZOOM);" in html
    m = mappage
    if not m.js("!!map.getLayer('underlay')"):
        return                                       # offline test page: raster style, no underlay
    m.js("underlayFilm(true); 0")
    assert m.js("map.getLayer('underlay').maxzoom") == 24
    m.js("underlayFilm(false); 0")
    assert m.js("map.getLayer('underlay').maxzoom") == 12.5


# ── live page: pacing governor ────────────────────────────────────────────────────────────────

def test_governor_steps_back_up_mid_gesture_despite_a_spike(mappage):
    m = mappage
    k = m.js("""(function () {
      vs.ms = 1000 / 120; resetPace(); pace.k = 3; pace.calmNeed = 2; pace.changedAt = 0;
      pace.moving = false; pace.lastRender = 0;
      var t = 1000;
      paceTick(t, 3);                                  // gesture start
      for (var i = 0; i < 80; i++) { t += paceMs(); paceTick(t, i === 5 ? 40 : 3); }   // one 40 ms spike
      var k = pace.k;
      paceSettle(); resetPace();
      return k;
    })()""")
    assert k < 3, "cheap frames (p90 3 ms) bring the cadence back up although one frame spiked"


def test_governor_holds_when_frames_really_are_expensive(mappage):
    m = mappage
    k = m.js("""(function () {
      vs.ms = 1000 / 120; resetPace(); pace.k = 3; pace.calmNeed = 2; pace.changedAt = 0;
      pace.moving = false; pace.lastRender = 0;
      var t = 1000;
      paceTick(t, 14);
      for (var i = 0; i < 80; i++) { t += paceMs(); paceTick(t, 14); }
      var k = pace.k;
      paceSettle(); resetPace();
      return k;
    })()""")
    assert k == 3, "14 ms frames can't hold 60 fps at 120 Hz: stays at an even 40"
