"""Sprint 4: low-spec hardware profile (GPU class, lite map, Windows 10 title bar) and the
global offline map (world-base pack, every country / continent as a region pack)."""
import json
import os
import shutil
import subprocess
import sys
import time
import types
from unittest import mock

import pytest

try:                                    # before any QApplication exists (the live page test)
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:                       # pragma: no cover
    pass

import map_precache
import perf_profile
import tile_store
import window_effects
import world_regions
from test_offline_map import FakeGov, finished, make_jobs, server, tmpdir_  # noqa: F401 (fixtures)


@pytest.fixture(scope="module")
def qapp():
    # sys.argv, never []: an empty argv hard-crashes QtWebEngine (exit 127, no output)
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)

HERE = os.path.dirname(os.path.abspath(__file__))


def gpu(name, vendor, vram=128, software=False):
    return {"name": name, "vendor": vendor, "device": 0, "vram_mb": vram, "shared_mb": 8000, "software": software}


INTEL, AMD, NV, MS, QC = (perf_profile.VENDOR_INTEL, perf_profile.VENDOR_AMD, perf_profile.VENDOR_NVIDIA,
                          perf_profile.VENDOR_MICROSOFT, perf_profile.VENDOR_QUALCOMM)


# ── GPU classification ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("g, want", [
    (gpu("Intel(R) UHD Graphics 620", INTEL), "integrated-low"),
    (gpu("Intel(R) HD Graphics 4000", INTEL), "integrated-low"),
    (gpu("Intel(R) Iris(R) Plus Graphics 655", INTEL), "integrated-low"),
    (gpu("Intel(R) UHD Graphics", INTEL, 128), "integrated-low"),
    (gpu("Intel(R) Iris(R) Xe Graphics", INTEL), "integrated"),
    (gpu("Intel(R) Arc(TM) 140V GPU (16GB)", INTEL, 128), "integrated"),
    (gpu("Intel(R) Arc(TM) A770 Graphics", INTEL, 16000), "discrete"),
    (gpu("AMD Radeon(TM) Graphics", AMD, 512), "integrated-low"),
    (gpu("AMD Radeon(TM) Vega 8 Graphics", AMD, 512), "integrated-low"),
    (gpu("AMD Radeon(TM) 780M", AMD, 2048), "integrated"),
    (gpu("AMD Radeon(TM) 860M Graphics", AMD, 512), "integrated"),         # this dev machine
    (gpu("AMD Radeon RX 7800 XT", AMD, 16000), "discrete"),
    (gpu("NVIDIA GeForce RTX 4060 Laptop GPU", NV, 8000), "discrete"),
    (gpu("Microsoft Basic Render Driver", MS, 0, True), "software"),
    (gpu("Qualcomm(R) Adreno(TM) X1-85 GPU", QC, 0), "integrated"),
    (None, "unknown"),
])
def test_classify_gpu(g, want):
    assert perf_profile.classify_gpu(g) == want


def test_primary_gpu_skips_the_software_adapter():
    gpus = [gpu("Microsoft Basic Render Driver", MS, 0, True), gpu("Intel(R) UHD Graphics 620", INTEL)]
    assert perf_profile.primary_gpu(gpus)["name"].startswith("Intel")
    assert perf_profile.primary_gpu([gpus[0]])["software"]                  # nothing else: still reported
    assert perf_profile.primary_gpu([]) is None


@pytest.mark.skipif(sys.platform != "win32", reason="DXGI")
def test_dxgi_reads_real_adapters_fast():
    t = time.perf_counter()
    gpus = perf_profile.read_gpus()
    ms = (time.perf_counter() - t) * 1000
    assert gpus and all(g["name"] and isinstance(g["vendor"], int) for g in gpus), gpus
    assert any(g["software"] for g in gpus) or len(gpus) >= 1
    assert ms < 500, ms                                                       # no WMI / PowerShell


# ── lite map profile ────────────────────────────────────────────────────────────
UHD = [gpu("Intel(R) UHD Graphics 620", INTEL)]
RTX = [gpu("NVIDIA GeForce RTX 4060", NV, 8000)]


def profile_for(gpus, hz=60.0, tier="high", mode="auto"):
    with mock.patch.object(perf_profile, "screen_refresh_hz", return_value=hz), \
            mock.patch.dict(os.environ, {"KAIROS_TIER": tier, "KAIROS_GPU_PROFILE": ""}):
        return perf_profile.detect(gpu_mode=mode, gpus=gpus)


def test_low_spec_gpu_degrades_the_map_only():
    full = profile_for(RTX, 120.0)
    lite = profile_for(UHD, 120.0)
    assert not full.gpu_lite and full.map_buildings_3d and full.map_hillshade and full.map_fps_cap == 0
    assert lite.gpu_lite and lite.gpu_class == "integrated-low"
    assert lite.map_buildings_3d is False and lite.map_lite_ui is True and lite.map_hillshade is False
    assert lite.map_fps_cap == 60                                    # 120 Hz panel -> steady 60
    assert 0 < lite.map_pixel_ratio_max <= perf_profile.LITE_PIXEL_RATIO_MAX and not lite.map_hidpi_raster
    assert lite.map_lod_levels <= 4
    # CPU-side knobs stay with the CPU tier (video, background work)
    assert (lite.tier, lite.bg_threads, lite.proxy_playback) == (full.tier, full.bg_threads, full.proxy_playback)
    m = lite.for_map()
    assert m["map_buildings_3d"] is False and m["map_lite_ui"] is True and m["gpu"] == "integrated-low"
    perf_profile.detect("auto")


def test_lite_on_a_60hz_panel_needs_no_cap_and_never_raises_quality():
    lite60 = profile_for(UHD, 60.0, tier="baseline")
    assert lite60.map_fps_cap == 0                                   # vsync already is 60
    base = perf_profile.build("baseline", 4, 8, 60.0)
    assert lite60.map_pixel_ratio_max <= base.map_pixel_ratio_max
    assert lite60.map_pixel_ratio_min <= base.map_pixel_ratio_min
    perf_profile.detect("auto")


def test_gpu_profile_override():
    assert profile_for(UHD, mode="full").gpu_lite is False
    assert profile_for(RTX, mode="lite").gpu_lite is True
    with mock.patch.dict(os.environ, {"KAIROS_GPU_PROFILE": "lite"}), \
            mock.patch.object(perf_profile, "screen_refresh_hz", return_value=60.0):
        assert perf_profile.detect(gpus=RTX).gpu_lite is True
    perf_profile.detect("auto")


def test_main_map_fps_cap_takes_the_profile_floor():
    import main
    import power
    win = types.SimpleNamespace(config={}, perf=types.SimpleNamespace(map_fps_cap=60))
    with mock.patch.object(power.get(), "override", power.PowerState(on_battery=False)):
        assert main.MainWindow._map_fps_cap(win) == 60
        win.config = {"map_fps_cap": 90}
        assert main.MainWindow._map_fps_cap(win) == 90                  # explicit setting wins
        win.config = {"battery_policy": "full"}
        assert main.MainWindow._map_fps_cap(win) == 60                  # "full" lifts only the battery cap
        win.perf = types.SimpleNamespace(map_fps_cap=0)
        win.config = {}
        assert main.MainWindow._map_fps_cap(win) == 0
    with mock.patch.object(power.get(), "override", power.PowerState(on_battery=True, saver=True)):
        assert main.MainWindow._map_fps_cap(win) == 60


def test_main_injects_the_profile_before_the_page_scripts():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    assert 'window.KAIROS_PERF_BOOT = {json.dumps(self.perf.for_map())}' in src
    assert 'perf_profile.detect(self.config.get("performance_tier"), self.config.get("gpu_profile"))' in src
    page = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
    assert "antialias: !PERF.map_lite_ui" in page
    assert "window.KAIROS_PERF_BOOT" in page and "body.lite-gpu .glass" in page
    assert "PERF.map_buildings_3d === false ? MS.flatBuildings(s) : s" in page


NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="node.js")
def test_flat_buildings_turns_extrusions_into_one_fill_pass():
    style = {"version": 8, "sources": {"openmaptiles": {}}, "layers": [
        {"id": "building", "type": "fill", "source": "openmaptiles", "source-layer": "building", "maxzoom": 14,
         "paint": {"fill-color": "#ddd"}},
        {"id": "building-3d", "type": "fill-extrusion", "source": "openmaptiles", "source-layer": "building",
         "minzoom": 14, "filter": ["!=", ["get", "hide_3d"], True],
         "paint": {"fill-extrusion-color": ["interpolate", ["linear"], ["get", "render_height"], 0, "#ddd", 200, "#aaa"],
                   "fill-extrusion-height": ["get", "render_height"], "fill-extrusion-opacity": 0.6}}]}
    js = ("global.window = global; require(%s); var s = KairosMapStyle.flatBuildings(%s);"
          "process.stdout.write(JSON.stringify(s.layers));") % (
        json.dumps(os.path.join(HERE, "map_style.js")), json.dumps(style))
    layers = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30).stdout)
    assert [l["type"] for l in layers] == ["fill", "fill"]
    flat = layers[1]
    assert flat["id"] == "building-3d" and flat["minzoom"] == 14 and flat["source-layer"] == "building"
    assert flat["filter"] == style["layers"][1]["filter"]
    assert flat["paint"]["fill-color"] == style["layers"][1]["paint"]["fill-extrusion-color"]
    assert flat["paint"]["fill-opacity"] == 0.6 and not any(k.startswith("fill-extrusion") for k in flat["paint"])


# ── Windows 10 title bar ────────────────────────────────────────────────────────
def _title_calls(build):
    from PyQt6.QtWidgets import QWidget
    calls = []
    w = QWidget()
    real_supported = window_effects.attr_supported
    with mock.patch.object(window_effects, "_dwm_set", side_effect=lambda h, a, v: calls.append(a) or True), \
            mock.patch.object(window_effects, "_refresh_frame"), \
            mock.patch.object(window_effects.sys, "platform", "win32"), \
            mock.patch.object(window_effects, "windows_build", return_value=build):
        ok = window_effects.style_title_bar(w, True, "#1C1C1E", "#F5F5F7")
        assert all(real_supported(a, build) for a in calls)
    w.deleteLater()
    return ok, calls


@pytest.mark.parametrize("build, want", [
    (17134, []),                                         # Windows 10 1803: no dark title API
    (18363, [19]),                                       # 1909: the old undocumented attribute
    (19045, [20]),                                       # 22H2: dark mode only, no caption colour
    (22000, [20, 35, 36]),                               # Windows 11: + caption / text colour
    (26200, [20, 35, 36]),
])
def test_title_bar_sends_only_attributes_the_build_knows(qapp, build, want):  # noqa: F811
    ok, calls = _title_calls(build)
    assert calls == want and ok == bool(want)
    assert window_effects.DWMWA_SYSTEMBACKDROP_TYPE not in calls


def test_win11_only_attributes_are_gated():
    for attr in (window_effects.DWMWA_SYSTEMBACKDROP_TYPE, window_effects.DWMWA_CAPTION_COLOR,
                 window_effects.DWMWA_WINDOW_CORNER_PREFERENCE):
        assert not window_effects.attr_supported(attr, 19045)
    assert window_effects.attr_supported(window_effects.DWMWA_SYSTEMBACKDROP_TYPE, 22621)
    with mock.patch.object(window_effects, "windows_build", return_value=19045), \
            mock.patch.object(window_effects, "_dwm_set") as dwm:
        assert window_effects._set_attr(1, window_effects.DWMWA_SYSTEMBACKDROP_TYPE, 2) is False
        dwm.assert_not_called()
    # nothing in the app passes the backdrop attribute (Mica / Acrylic) to DWM at all
    for name in os.listdir(HERE):
        if name.endswith(".py") and not name.startswith("test_") and name != "window_effects.py":
            src = open(os.path.join(HERE, name), encoding="utf-8", errors="ignore").read()
            assert "SYSTEMBACKDROP" not in src and "DwmEnableBlurBehindWindow" not in src, name


# ── world catalogue ─────────────────────────────────────────────────────────────
def contains(region, lon, lat):
    return any(w <= lon <= e and s <= lat <= n for w, s, e, n in tile_store.region_boxes(region))


def test_every_country_and_continent_is_a_region():
    conts, countries = tile_store.region_ids()
    assert len(countries) >= 220 and len(conts) == 6
    assert {"cz", "sk", "at", "de", "pl", "ch", "hu", "si", "hr", "us", "jp", "br", "au", "za", "nz"} <= set(countries)
    for rid, boxes in tile_store.REGIONS.items():
        assert boxes, rid
        for w, s, e, n in tile_store.region_boxes(boxes):
            assert -180 <= w < e <= 180 and -85.1 <= s < n <= 85.1, (rid, (w, s, e, n))


@pytest.mark.parametrize("rid, lon, lat", [
    ("cz", 14.42, 50.09), ("cz", 18.29, 49.83), ("us", -77.04, 38.91), ("us", -157.86, 21.31),
    ("us", -149.90, 61.22), ("jp", 139.69, 35.69), ("jp", 141.35, 43.06), ("au", 149.13, -35.28),
    ("br", -47.88, -15.79), ("ru", 37.62, 55.76), ("ru", 131.89, 43.12), ("ru", 20.51, 54.71),
    ("is", -21.94, 64.15), ("nz", 174.78, -41.29), ("cl", -70.67, -33.45), ("es", -15.43, 28.12),
    ("pt", -25.67, 37.74), ("no", 18.96, 69.65), ("ca", -79.38, 43.65), ("za", 18.42, -33.92),
    ("fr", 2.35, 48.86), ("fr", 9.15, 42.15), ("gb", -3.19, 55.95), ("in", 77.21, 28.61),
])
def test_country_boxes_cover_real_places(rid, lon, lat):
    assert contains(tile_store.REGIONS[rid], lon, lat)


def test_continents_hold_their_countries_and_names_are_localized():
    assert contains(tile_store.REGIONS["continent-europe"], 14.42, 50.09)
    assert contains(tile_store.REGIONS["continent-north-america"], -99.13, 19.43)    # Mexico City
    assert not contains(tile_store.REGIONS["continent-south-america"], 14.42, 50.09)
    assert tile_store.region_name("cz", "cs") == "Česko"
    assert tile_store.region_name("de", "de") == "Deutschland"
    assert tile_store.region_name("jp", "ja") == "日本"
    assert tile_store.region_name("continent-europe", "fr") == "Europe"
    assert tile_store.region_name("cz", "xx") == "Czechia"
    for cc, (_cont, _boxes, names) in world_regions.COUNTRIES.items():
        assert set(names) == {"cs", "de", "en", "es", "fr", "ja", "zh-CN"} and all(names.values()), cc


def test_multi_box_tiles_are_unique_parents_first_and_estimated_exactly():
    boxes = [(14.40, 50.08, 14.43, 50.10), (14.42, 50.09, 14.45, 50.11), (-157.9, 21.3, -157.8, 21.4)]
    tiles = list(tile_store.region_tiles(boxes, 0, 12))
    assert len(tiles) == len(set(tiles))
    assert [z for z, _x, _y in tiles] == sorted(z for z, _x, _y in tiles)
    assert tile_store.estimate_region(boxes, 12)[0] == len(tiles)
    assert tile_store.region_bounds(boxes) == (-157.9, 21.3, 14.45, 50.11)
    assert list(tile_store.region_tiles((14.40, 50.08, 14.43, 50.10), 0, 3)) == \
        list(tile_store.region_tiles([(14.40, 50.08, 14.43, 50.10)], 0, 3))           # one box = old API


def test_tile_cap_picks_the_deepest_detail_that_fits():
    assert tile_store.region_fits(tile_store.REGIONS["cz"], 14)
    assert tile_store.region_max_detail(tile_store.REGIONS["cz"]) == 14
    assert not tile_store.region_fits(tile_store.REGIONS["continent-europe"], 14)
    assert tile_store.region_max_detail(tile_store.REGIONS["continent-europe"]) in (8, 11)
    assert tile_store.region_max_detail(tile_store.REGIONS["us"]) in (8, 11)
    t = time.perf_counter()
    for rid in tile_store.REGIONS:                       # the settings call this on the GUI thread
        tile_store.region_max_detail(tile_store.REGIONS[rid])
    assert time.perf_counter() - t < 3.0


def test_start_region_refuses_a_pack_above_the_cap(server):  # noqa: F811
    mj = make_jobs(server)
    assert not mj.start_region("continent-europe", zmax=14)
    assert not mj.running()


# ── world-base pack ─────────────────────────────────────────────────────────────
def test_world_base_pack_is_built_once_then_photos_follow(server, monkeypatch):  # noqa: F811
    srv = server
    monkeypatch.setattr(tile_store, "WORLD_ZMAX", 2)
    mj = make_jobs(srv)
    places = [(14.42, 50.09, 30)]
    assert not map_precache.world_base_installed(srv)
    assert mj.start_precache(places=places, manual=True, world=True)
    st = finished(mj)
    assert st["phase"] == map_precache.DONE and st["stage"] == "photos" and st["done"] > 0, st
    assert map_precache.world_base_installed(srv)
    pack = [p for p in srv.packs.list() if p["name"] == tile_store.WORLD_PACK][0]
    assert (pack["minzoom"], pack["maxzoom"]) == (0, 2)
    # every z0-2 tile of the planet is served from the pack with the network gone
    srv.upstream.fail = True
    for z, x, y in tile_store.region_tiles(tile_store.WORLD_BOX, 0, 2):
        key = f"tiles.openfreemap.org/planet/20261020_001001_pt/{z}/{x}/{y}.pbf"
        assert srv.packs.lookup(key) is not None, (z, x, y)
    srv.upstream.fail = False
    calls = len(srv.upstream.calls)
    mj.start_precache(places=places, manual=True, world=True)
    st = finished(mj)
    assert st.get("stage") != "world"                                          # not rebuilt
    world_calls = [c for c, _lane in srv.upstream.calls[calls:] if "/0/0/0.pbf" in c]
    assert world_calls == []


def test_world_base_waits_on_battery_for_automatic_runs(server, monkeypatch):  # noqa: F811
    from test_offline_map import FakePower
    monkeypatch.setattr(tile_store, "WORLD_ZMAX", 1)
    pwr = FakePower(on_battery=True)
    mj = make_jobs(server, pwr=pwr)
    mj.start_precache(places=[(14.42, 50.09, 3)], manual=False, world=True, policy="throttle")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and mj.state()["phase"] != map_precache.WAITING:
        time.sleep(0.02)
    assert mj.state()["phase"] == map_precache.WAITING and mj.state()["reason"] == "battery"
    assert not map_precache.world_base_installed(server)
    mj.cancel(wait_s=5)


def test_main_asks_for_the_world_base():
    import main
    win = types.SimpleNamespace(config={}, devicePixelRatioF=lambda: 1.0,
                                perf=types.SimpleNamespace(for_map=lambda: {}))
    assert main.MainWindow.precache_options(win)["world"] is True
    win.config = {"map_world_base": False}
    assert main.MainWindow.precache_options(win)["world"] is False


# ── settings ────────────────────────────────────────────────────────────────────
@pytest.fixture
def settings(qapp, monkeypatch):  # noqa: F811
    from test_offline_map import StubJobs
    stub = StubJobs()
    monkeypatch.setattr(map_precache, "jobs", lambda: stub)
    monkeypatch.setattr(tile_store, "_service", None)
    import settings_dialog
    dlg = settings_dialog.SettingsDialog()
    yield dlg, stub
    dlg._map_timer.stop()
    dlg.deleteLater()


def test_settings_offers_every_region_and_caps_the_detail(settings):
    import settings_dialog
    dlg, stub = settings
    combo = dlg.combo_map_region
    ids = [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]
    assert set(ids) == set(tile_store.REGIONS) and ids[:6] == [i for i in ids if i.startswith("continent-")]
    assert dlg.combo_map_detail.count() == len(settings_dialog.MAP_DETAILS)
    combo.setCurrentIndex(combo.findData("continent-europe"))
    assert dlg.combo_map_detail.currentData() < 14                              # dropped to what fits
    dlg.download_region_pack()
    assert stub.calls[-1][:2] == ("region", "continent-europe")
    assert stub.calls[-1][2]["zmax"] == dlg.combo_map_detail.currentData()
    dlg.combo_map_detail.setCurrentIndex(dlg.combo_map_detail.findData(14))    # user forces streets
    assert "settings-map" not in dlg.lbl_map_region.text() and not dlg.btn_map_region.isEnabled()
    n = len(stub.calls)
    dlg.download_region_pack()
    assert len(stub.calls) == n                                                 # refused, no job
    combo.setCurrentIndex(combo.findData("cz"))
    assert dlg.btn_map_region.isEnabled() and "MB" in dlg.lbl_map_region.text()


def test_settings_names_the_world_pack_and_its_progress(settings):
    dlg, _stub = settings
    assert dlg._pack_title(tile_store.WORLD_PACK, "World") != "World" or dlg._pack_title("x", "x") == "x"
    text = dlg._map_job_text({"kind": "precache", "phase": map_precache.RUNNING, "stage": "world",
                              "done": 5, "skipped": 0, "failed": 0, "total": 1365, "bytes": 2 ** 20})
    assert text and "settings-map" not in text and "1" in text


# ── live page: lite GPU boot ────────────────────────────────────────────────────
def test_live_page_boots_lite_without_msaa_and_with_flat_buildings(qapp):  # noqa: F811
    pytest.importorskip("PyQt6.QtWebEngineWidgets")
    import main  # noqa: F401
    import test_patch11_maps as t11
    from PyQt6.QtWebEngineCore import QWebEngineScript
    boot = perf_profile.apply_gpu(perf_profile.build("high", 8, 16, 120.0), UHD[0]).for_map()
    orig = t11._load

    def load_with_boot(page):
        s = QWebEngineScript()
        s.setName("kairos-perf-boot")
        s.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        s.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        s.setSourceCode(f"window.KAIROS_PERF_BOOT = {json.dumps(boot)};")
        page.scripts().insert(s)
        orig(page)

    with mock.patch.object(t11, "_load", load_with_boot):
        page = t11.ShownMapPage((800, 500))
    try:
        assert page.js("isMapCreated()")
        assert page.js("map.painter.context.gl.getContextAttributes().antialias") is False
        assert page.js("document.body.classList.contains('lite-gpu')") is True
        assert page.js("getComputedStyle(document.querySelector('.maplibregl-ctrl-group') || document.body)"
                       ".backdropFilter") in ("none", "", None)
        style = {"version": 8, "sources": {"openmaptiles": {"type": "vector", "tiles": ["https://x/{z}/{x}/{y}.pbf"]}},
                 "layers": [{"id": "building-3d", "type": "fill-extrusion", "source": "openmaptiles",
                             "source-layer": "building", "minzoom": 14,
                             "paint": {"fill-extrusion-color": "#ccc", "fill-extrusion-height": 10}}]}
        page.js(f"window.setVectorStyle({json.dumps(style)}); 0")
        types_ = page.js("buildVectorStyle('standard').layers.map(function (l) { return l.type; })")
        assert "fill-extrusion" not in types_ and "fill" in types_
        page.js("setPerfProfile({map_buildings_3d: true, map_lite_ui: false}); 0")
        assert "fill-extrusion" in page.js("buildVectorStyle('standard').layers.map(function (l) { return l.type; })")
        assert page.js("document.body.classList.contains('lite-gpu')") is False
    finally:
        page.close()


# ── founder decision 2026-11-06: world z0-6 ────────────────────────────────────
def test_world_zmax_is_six_and_fits_the_cap():
    assert tile_store.WORLD_ZMAX == 6
    tiles, size = tile_store.estimate_region(tile_store.WORLD_BOX, tile_store.WORLD_ZMAX)
    assert tiles == sum(4 ** z for z in range(7)) == 5461
    assert tile_store.region_fits([tile_store.WORLD_BOX], tile_store.WORLD_ZMAX)


def test_shallower_world_pack_is_rebuilt_at_the_new_depth(server, monkeypatch):  # noqa: F811
    srv = server
    monkeypatch.setattr(tile_store, "WORLD_ZMAX", 1)
    mj = make_jobs(srv)
    assert mj.start_precache(places=[(14.42, 50.09, 3)], manual=True, world=True)
    finished(mj)
    assert map_precache.world_base_installed(srv)
    monkeypatch.setattr(tile_store, "WORLD_ZMAX", 2)        # the app now ships a deeper world
    assert not map_precache.world_base_installed(srv)
    mj.start_precache(places=[(14.42, 50.09, 3)], manual=True, world=True)
    finished(mj)
    pack = [p for p in srv.packs.list() if p["name"] == tile_store.WORLD_PACK][0]
    assert pack["maxzoom"] == 2 and map_precache.world_base_installed(srv)
