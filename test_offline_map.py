"""Sprint 3 item 3: offline map – photo-area pre-caching (map_precache.py), region packs
(tile_store.PackManager / RegionPack / write_pack), offline serving with the network severed,
yielding to the user / metered / battery, worker-thread-only DB + network, Settings section."""
import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

import pytest

import map_precache
import mini_map
import paths
import tile_store

OFM_TEMPLATE = "https://tiles.openfreemap.org/planet/20261101_080013_pt/{z}/{x}/{y}.pbf"
OFM_KEY = "tiles.openfreemap.org/planet/20261101_080013_pt/{z}/{x}/{y}.pbf"
PRAGUE = (14.4208, 50.0875)
BRNO = (16.6068, 49.1951)


# ── fakes ──────────────────────────────────────────────────────────────────────
class FakeUpstream:
    """Answers every tile; .fail = True severs the network (OSError like a pulled cable)."""

    def __init__(self, delay=0.0):
        self.calls, self.threads = [], set()
        self.delay, self.fail = delay, False
        self.lock = threading.Lock()

    def fetch(self, host, path, lane="map"):
        with self.lock:
            self.calls.append((host + path, lane))
            self.threads.add(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("network unreachable")
        if path == map_precache.OFM_TILEJSON_PATH:
            return 200, json.dumps({"tiles": [OFM_TEMPLATE], "minzoom": 0, "maxzoom": 14}).encode(), \
                "application/json", None
        if path.endswith(".pbf"):
            return 200, gzip.compress(b"vector:" + path.encode()), "application/x-protobuf", "gzip"
        return 200, b"\x89PNG" + path.encode(), "image/png", None


class FakeGov:
    def __init__(self):
        self.is_busy = False

    def busy(self):
        return self.is_busy


class FakeNet:
    def __init__(self, metered=False):
        self.is_metered = metered

    def metered(self):
        return self.is_metered


class FakePower:
    def __init__(self, on_battery=False):
        self.on_battery = on_battery

    def state(self):
        return types.SimpleNamespace(on_battery=self.on_battery, saver=False, percent=80)


def http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", dict(e.headers)


def wait_for(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def tmpdir_():
    d = tempfile.mkdtemp(prefix="offline_map_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def server(tmpdir_):
    """A real TileServer (HTTP on 127.0.0.1) over a temp store + packs folder, fake network."""
    store = tile_store.TileStore(os.path.join(tmpdir_, "tiles.sqlite"), budget_mb=64)
    packs = tile_store.PackManager(os.path.join(tmpdir_, "packs"))
    srv = tile_store.TileServer(store, shards=2, packs=packs)
    srv.upstream = FakeUpstream()
    store.ready.wait(10)
    yield srv
    srv.shutdown()
    store.close()
    packs.close()


def make_jobs(srv, gov=None, net=None, pwr=None, connect=None):
    return map_precache.MapJobs(service_factory=lambda _mb: srv, connect=connect, gov=gov or FakeGov(),
                                net=net or FakeNet(), power_monitor=pwr or FakePower())


def finished(mj, timeout=30.0):
    assert wait_for(lambda: not mj.running(), timeout), mj.state()
    return mj.state()


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def vector_pack(path, tiles, bounds=None, maxzoom=14, title="Test"):
    """tiles: [(z, x, y)] -> pack with gzip'd payloads naming the tile."""
    return tile_store.write_pack(path, "openmaptiles", "pbf",
                                 ((z, x, y, gzip.compress(f"pack:{z}/{x}/{y}".encode())) for z, x, y in tiles),
                                 title=title, bounds=bounds, maxzoom=maxzoom)


# ── tile math / key parsing ──────────────────────────────────────────────────────
def test_parse_key_every_source():
    assert tile_store.parse_key("tiles.openfreemap.org/planet/20250101_x/14/8846/5550.pbf") == \
        ("openmaptiles", 14, 8846, 5550)
    # Esri is z/y/x in the URL
    assert tile_store.parse_key("server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/5/10/17") == \
        ("esri:World_Imagery", 5, 17, 10)
    assert tile_store.parse_key(mini_map.tile_key(15, 17700, 11101)) == ("esri:World_Street_Map", 15, 17700, 11101)
    assert tile_store.parse_key("tile.opentopomap.org/7/68/44.png") == ("opentopomap", 7, 68, 44)
    assert tile_store.parse_key("elevation-tiles-prod.s3.amazonaws.com/terrarium/9/277/173.png") == ("terrarium", 9, 277, 173)
    assert tile_store.parse_key("tiles.openfreemap.org/styles/liberty") is None
    assert tile_store.parse_key("evil.example/1/2/3.png") is None


def test_tile_math_matches_mini_map_projection():
    for lon, lat in (PRAGUE, BRNO, (-122.42, 37.77), (151.2, -33.87)):
        for z in (0, 5, 12, 14, 16):
            px, py = mini_map.world_px(lat, lon, z)
            assert tile_store.lon2tile(lon, z) == int(px // 256)
            assert tile_store.lat2tile(lat, z) == int(py // 256)
    assert tile_store.lon2tile(180.0, 3) == 7 and tile_store.lat2tile(89.9, 3) == 0   # clamped


def test_region_estimate_is_pure_math_and_plausible():
    tiles, size = tile_store.estimate_region(tile_store.REGIONS["cz"])
    assert 60000 < tiles < 90000                  # z0-14 over Czechia
    assert 0.7 * 2 ** 30 < size < 1.5 * 2 ** 30   # ~1 GB at measured OpenFreeMap tile sizes
    assert set(tile_store.REGIONS) >= {"cz", "de", "at", "sk", "pl"}


# ── planner ────────────────────────────────────────────────────────────────────
def vec_source():
    return map_precache.Source("openmaptiles", OFM_KEY, 0, 14)


def test_plan_is_parents_first_deduplicated_and_capped():
    places = [(PRAGUE[0], PRAGUE[1], 50), (BRNO[0], BRNO[1], 5), (PRAGUE[0] + 0.001, PRAGUE[1], 3)]
    keys = map_precache.plan_keys(places, [vec_source()], panel=False, max_keys=100000)
    assert len(keys) == len(set(keys))
    zooms = [tile_store.parse_key(k)[1] for k in keys]
    assert zooms == sorted(zooms)                              # parents always before children
    assert max(zooms) == 14                                    # vector tiles stop at 14 (view 15 overzooms)
    for lon, lat, _n in places:                                # each photo's own tile at every zoom
        for z in range(0, 15):
            assert OFM_KEY.format(z=z, x=tile_store.lon2tile(lon, z), y=tile_store.lat2tile(lat, z)) in keys
    capped = map_precache.plan_keys(places, [vec_source()], panel=False, max_keys=50)
    assert capped == keys[:50]                                 # the cap drops the deepest levels


def test_plan_busiest_place_first_within_a_level():
    places = [(BRNO[0], BRNO[1], 1), (PRAGUE[0], PRAGUE[1], 99)]
    keys = map_precache.plan_keys(places, [vec_source()], panel=False, max_keys=100000)
    z14 = [k for k in keys if tile_store.parse_key(k)[1] == 14]
    first = tile_store.parse_key(z14[0])
    assert (tile_store.lon2tile(PRAGUE[0], 12), tile_store.lat2tile(PRAGUE[1], 12)) == (first[2] >> 2, first[3] >> 2)


def test_plan_includes_info_panel_mini_map_tiles():
    places = [(PRAGUE[0], PRAGUE[1], 3)]
    keys = map_precache.plan_keys(places, [vec_source()], dpr=2.0, panel=True, max_keys=100000)
    z, _size, tiles = mini_map.layout_tiles(PRAGUE[1], PRAGUE[0], 300, mini_map.HEIGHT, 2.0)
    for x, y, _l, _t in tiles:                 # every tile the panel draws at this place
        assert mini_map.tile_key(z, x, y) in keys
    oz = z - mini_map.OVERVIEW_DROP
    cx, cy = mini_map.world_px(PRAGUE[1], PRAGUE[0], oz)
    assert mini_map.tile_key(oz, int(cx // 256), int(cy // 256)) in keys   # the LQIP overview too


def test_plan_satellite_only_when_chosen_and_offset_follows_tile_size():
    places = [(PRAGUE[0], PRAGUE[1], 3)]
    sat = map_precache.tile_store_source_imagery(map_precache.raster_offset(2.0, True))
    keys = map_precache.plan_keys(places, [vec_source(), sat], panel=False, max_keys=100000)
    sat_z = {tile_store.parse_key(k)[1] for k in keys if "World_Imagery" in k}
    assert max(sat_z) == 17                    # view 15 + 2 (128 px HiDPI raster tiles)
    assert map_precache.raster_offset(1.0, True) == 1 and map_precache.raster_offset(2.0, False) == 1
    plain = map_precache.plan_keys(places, [vec_source()], panel=False, max_keys=100000)
    assert not any("World_Imagery" in k for k in plain)


def test_plan_never_bulk_fetches_policy_restricted_sources():
    """OpenTopoMap / waymarkedtrails forbid bulk downloads: no source for them exists here."""
    src = open(os.path.join(os.path.dirname(map_precache.__file__), "map_precache.py"), encoding="utf-8").read()
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "tile.opentopomap.org" not in code and "tile.waymarkedtrails.org" not in code


def media_db(rows):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("""CREATE TABLE media_items (id INTEGER PRIMARY KEY, file_path TEXT, latitude REAL,
                    longitude REAL, is_trashed INTEGER, is_hidden INTEGER, paired_video_path TEXT)""")
    conn.executemany("INSERT INTO media_items (file_path, latitude, longitude, is_trashed, is_hidden) "
                     "VALUES (?, ?, ?, ?, ?)", rows)
    return conn


def test_read_places_excludes_hidden_trashed_and_null_island():
    conn = media_db([("a.jpg", PRAGUE[1], PRAGUE[0], 0, 0), ("b.jpg", PRAGUE[1], PRAGUE[0], 0, 0),
                     ("secret.jpg", BRNO[1], BRNO[0], 0, 1), ("bin.jpg", 48.0, 11.0, 1, 0),
                     ("zero.jpg", 0.0, 0.0, 0, 0), ("nogps.jpg", None, None, 0, 0)])
    places = map_precache.read_places(conn)
    assert places == [(round(PRAGUE[0], 4), round(PRAGUE[1], 4), 2)]   # Hidden-folder places never leave


# ── metered connection ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("cost, metered", [
    (0x0, False), (0x1, False), (0x2, True), (0x4, True), (0x1 | 0x40000, True),
    (0x1 | 0x80000, True), (0x1 | 0x10000, True), (0x1 | 0x20000, False)])
def test_metered_cost_flags(cost, metered):
    assert map_precache.is_metered_cost(cost) is metered


def test_network_cost_reader_runs_and_is_cached():
    cost = map_precache.read_network_cost()          # real INetworkCostManager on Windows
    assert isinstance(cost, int) and cost >= 0
    calls = []
    nc = map_precache.NetworkCost(reader=lambda: calls.append(1) or 0x2, recheck_s=60)
    assert nc.metered() and nc.metered() and len(calls) == 1
    nc.override = False
    assert nc.metered() is False


# ── region packs ─────────────────────────────────────────────────────────────────
def test_write_pack_round_trip_tms_and_bounds(tmpdir_):
    path = os.path.join(tmpdir_, "cz.mbtiles")
    z, x, y = 14, tile_store.lon2tile(PRAGUE[0], 14), tile_store.lat2tile(PRAGUE[1], 14)
    assert vector_pack(path, [(0, 0, 0), (z, x, y)], bounds=tile_store.REGIONS["cz"]) == 2
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT tile_row FROM tiles WHERE zoom_level = 14").fetchone()[0] == 2 ** 14 - 1 - y   # TMS
    raw.close()
    pack = tile_store.RegionPack(path)
    data, ctype, enc = pack.get(z, x, y)
    assert gzip.decompress(data) == f"pack:{z}/{x}/{y}".encode() and enc == "gzip"
    assert ctype == "application/x-protobuf" and pack.source == "openmaptiles"
    assert pack.get(z, tile_store.lon2tile(BRNO[0] + 10, z), y) is None        # outside the bounds
    pack.close()


def test_write_pack_stopped_leaves_nothing(tmpdir_):
    path = os.path.join(tmpdir_, "x.mbtiles")
    with pytest.raises(InterruptedError):
        tile_store.write_pack(path, "openmaptiles", "pbf", ((0, 0, 0, b"a") for _ in range(10)),
                              should_stop=lambda: True)
    assert os.listdir(tmpdir_) == []


def test_pack_manager_serves_every_planet_version_and_removes_on_windows(tmpdir_):
    d = os.path.join(tmpdir_, "packs")
    os.makedirs(d)
    z, x, y = 13, tile_store.lon2tile(PRAGUE[0], 13), tile_store.lat2tile(PRAGUE[1], 13)
    vector_pack(os.path.join(d, "region-cz.mbtiles"), [(z, x, y)])
    with open(os.path.join(d, "junk.mbtiles"), "wb") as f:
        f.write(b"not sqlite at all")
    pm = tile_store.PackManager(d)
    infos = pm.scan()
    assert [p["name"] for p in infos] == ["region-cz"] and "junk.mbtiles" in pm.errors
    for version in ("20261101_080013_pt", "20270101_000000_pt"):
        got = pm.lookup(f"tiles.openfreemap.org/planet/{version}/{z}/{x}/{y}.pbf")
        assert got is not None and gzip.decompress(got[0]) == f"pack:{z}/{x}/{y}".encode()
    assert pm.lookup(mini_map.tile_key(z, x, y)) is None             # other source: not this pack
    threading.Thread(target=lambda: pm.lookup(f"tiles.openfreemap.org/planet/v/{z}/{x}/{y}.pbf")).start()
    time.sleep(0.2)                                                     # a connection on another thread
    assert pm.remove("region-cz")                                       # closes every connection first
    assert not os.path.exists(os.path.join(d, "region-cz.mbtiles")) and pm.list() == []
    pm.close()


def test_pack_install_validates_and_copies(tmpdir_):
    d = os.path.join(tmpdir_, "packs")
    pm = tile_store.PackManager(d)
    src = os.path.join(tmpdir_, "My Region (CZ).mbtiles")
    vector_pack(src, [(0, 0, 0)])
    info = pm.install(src)
    assert info["name"] == "My_Region_CZ_" and os.path.exists(os.path.join(d, "My_Region_CZ_.mbtiles"))
    assert os.path.exists(src)                                          # the original stays
    bad = os.path.join(tmpdir_, "raster.mbtiles")
    tile_store.write_pack(bad, "", "png", [(0, 0, 0, b"\x89PNG")])      # raster without a source id
    with pytest.raises(ValueError):
        pm.install(bad)
    txt = os.path.join(tmpdir_, "notes.mbtiles")
    open(txt, "w").write("hello")
    with pytest.raises(ValueError):
        pm.install(txt)
    assert [p["name"] for p in pm.list()] == ["My_Region_CZ_"]
    pm.close()


# ── offline serving: the network severed ─────────────────────────────────────────
def test_network_severed_store_and_pack_tiles_still_served_over_http(server):
    srv = server
    stored = OFM_KEY.format(z=14, x=8846, y=5550)
    packed_z, packed_x, packed_y = 14, 8847, 5550
    srv.store.put(stored, gzip.compress(b"stored"), "application/x-protobuf", "gzip")
    srv.store.flush()
    vector_pack(os.path.join(srv.packs.dir, "cz.mbtiles"), [(packed_z, packed_x, packed_y)])
    srv.packs.scan()
    srv.upstream.fail = True                                            # pull the cable
    base = srv.bases()[0]
    st, body, hdr = http_get(base + stored)
    assert st == 200 and hdr["X-Tile-Source"] == "store" and gzip.decompress(body) == b"stored"
    packed = OFM_KEY.format(z=packed_z, x=packed_x, y=packed_y)
    st, body, hdr = http_get(srv.bases()[tile_store.shard_of(packed, len(srv.ports))] + packed)
    assert st == 200 and hdr["X-Tile-Source"] == "pack" and gzip.decompress(body) == b"pack:14/8847/5550"
    assert srv.upstream.calls == []                                     # neither touched the network
    missing = OFM_KEY.format(z=14, x=1, y=1)
    assert srv.resolve(missing)[0] == 502                               # a real miss is an error, not a hang
    srv.online = False
    st, _b, _h = http_get(base + missing)
    assert st == 504


def test_network_severed_stale_raster_beats_a_hole(server):
    srv = server
    key = mini_map.tile_key(15, 17700, 11101)
    srv.store.put(key, b"\x89PNGold", "image/png", None)
    srv.store.flush()
    srv.store._hot.clear()
    srv.store._hot_bytes = 0
    srv.store.flush()
    conn = sqlite3.connect(srv.store.path)
    conn.execute("UPDATE tiles SET fetched = 0")                        # older than RASTER_TTL_S
    conn.commit()
    conn.close()
    srv.upstream.fail = True
    srv.redirect_misses = False
    status, data, _c, _e, source = srv.resolve(key)
    assert status == 200 and data == b"\x89PNGold" and source == "stale"


def test_mini_map_fetch_uses_packs_offline(server):
    srv = server
    key = mini_map.tile_key(15, 17700, 11101)
    tile_store.write_pack(os.path.join(srv.packs.dir, "street.mbtiles"), "esri:World_Street_Map", "png",
                          [(15, 17700, 11101, b"\x89PNGpack")])
    srv.packs.scan()
    srv.upstream.fail = True
    assert mini_map.fetch_tile(key, srv) == b"\x89PNGpack"


# ── precache job end to end ──────────────────────────────────────────────────────
def test_precache_then_offline_every_planned_tile_is_served(server):
    srv = server
    places = [(PRAGUE[0], PRAGUE[1], 20), (BRNO[0], BRNO[1], 4)]
    mj = make_jobs(srv)
    assert mj.start_precache(places=places, dpr=1.0)
    st = finished(mj)
    assert st["phase"] == map_precache.DONE, st
    keys = map_precache.plan_keys(places, [map_precache.vector_source(
        {"tiles": [OFM_TEMPLATE], "maxzoom": 14})], dpr=1.0, panel=True)
    assert st["total"] == len(keys) and st["done"] == len(keys) and st["failed"] == 0
    assert all(lane == "background" for _k, lane in srv.upstream.calls)    # polite lane only
    srv.store.flush()
    srv.upstream.fail = True                                               # now offline
    srv.upstream.calls.clear()
    for key in keys:
        status, _d, _c, _e, source = srv.resolve(key)
        assert status == 200 and source == "store", key
    assert srv.upstream.calls == []
    # a second run downloads nothing: everything is already offline
    mj2 = make_jobs(srv)
    srv.upstream.fail = False
    mj2.start_precache(places=places)
    st2 = finished(mj2)
    assert st2["done"] == 0 and st2["skipped"] == len(keys)


def test_precache_skips_tiles_a_pack_already_has(server):
    srv = server
    places = [(PRAGUE[0], PRAGUE[1], 1)]
    keys = map_precache.plan_keys(places, [vec_source_real()], panel=False)
    vector_pack(os.path.join(srv.packs.dir, "cz.mbtiles"), [tile_store.parse_key(k)[1:] for k in keys])
    srv.packs.scan()
    mj = make_jobs(srv)
    mj.start_precache(places=places)
    st = finished(mj)
    assert st["skipped"] >= len(keys)
    fetched = {k for k, _lane in srv.upstream.calls}
    assert not any(k.endswith(".pbf") for k in fetched)


def vec_source_real():
    return map_precache.vector_source({"tiles": [OFM_TEMPLATE], "maxzoom": 14})


def test_precache_waits_while_the_user_interacts(server):
    srv = server
    gov = FakeGov()
    gov.is_busy = True
    mj = make_jobs(srv, gov=gov)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)])
    assert wait_for(lambda: mj.state()["phase"] == map_precache.WAITING, 5)
    assert mj.state()["reason"] == "busy" and mj.state()["done"] == 0
    assert not any(k.endswith(".pbf") for k, _l in srv.upstream.calls)
    gov.is_busy = False
    st = finished(mj)
    assert st["phase"] == map_precache.DONE and st["done"] > 0


def test_precache_yields_mid_run_within_one_poll(server):
    srv = server
    srv.upstream.delay = 0.01
    gov = FakeGov()
    mj = make_jobs(srv, gov=gov)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 5), (BRNO[0], BRNO[1], 5)], manual=True)
    assert wait_for(lambda: mj.state().get("done", 0) > 20, 10)
    gov.is_busy = True
    time.sleep(map_precache.WAIT_POLL_S + 0.05)            # in-flight downloads finish...
    frozen = len(srv.upstream.calls)
    time.sleep(0.4)
    assert len(srv.upstream.calls) == frozen               # ...and no new one starts while busy
    gov.is_busy = False
    assert finished(mj)["phase"] == map_precache.DONE


def test_precache_waits_on_metered_connection(server):
    net = FakeNet(metered=True)
    mj = make_jobs(server, net=net)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)], manual=True)    # even a manual run waits
    assert wait_for(lambda: mj.state().get("reason") == "metered", 5)
    assert mj.state()["phase"] == map_precache.WAITING and mj.state()["done"] == 0
    net.is_metered = False
    assert finished(mj)["phase"] == map_precache.DONE


def test_precache_battery_waits_when_automatic_but_not_when_manual(server):
    pwr = FakePower(on_battery=True)
    mj = make_jobs(server, pwr=pwr)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)], manual=False, policy="throttle")
    assert wait_for(lambda: mj.state().get("reason") == "battery", 5)
    mj.cancel()
    assert finished(mj)["phase"] == map_precache.CANCELLED
    mj2 = make_jobs(server, pwr=pwr)
    mj2.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)], manual=True)
    assert finished(mj2)["phase"] == map_precache.DONE
    mj3 = make_jobs(server, pwr=pwr)                       # battery policy "full" = no limits
    mj3.start_precache(places=[(BRNO[0], BRNO[1], 1)], manual=False, policy="full")
    assert finished(mj3)["phase"] == map_precache.DONE


def test_precache_stops_at_the_budget_share(server):
    srv = server
    srv.store.budget = 1024                                 # 1 KB: full after a few tiles
    mj = make_jobs(srv)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)])
    st = finished(mj)
    assert st["phase"] == map_precache.BUDGET and st["done"] < st["total"]


def test_precache_offline_stops_and_reports(server):
    srv = server
    srv.upstream.fail = True
    mj = make_jobs(srv)
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 1)])
    st = finished(mj)
    assert st["phase"] == map_precache.OFFLINE             # TileJSON failed: nothing to plan against


def test_precache_network_drops_mid_run(server):
    srv = server
    srv.redirect_misses = False
    mj = make_jobs(srv)
    orig = srv.upstream.fetch
    n = [0]

    def flaky(host, path, lane="map"):
        n[0] += 1
        if n[0] > 30:
            raise OSError("cable pulled")
        return orig(host, path, lane)
    srv.upstream.fetch = flaky
    mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 5), (BRNO[0], BRNO[1], 5)])
    st = finished(mj)
    assert st["phase"] == map_precache.OFFLINE and st["failed"] >= map_precache.OFFLINE_AFTER_FAILS


def test_one_job_at_a_time_and_cancel(server):
    srv = server
    srv.upstream.delay = 0.02
    mj = make_jobs(srv)
    assert mj.start_precache(places=[(PRAGUE[0], PRAGUE[1], 5)], manual=True)
    assert not mj.start_clear()                             # busy
    mj.cancel()
    assert finished(mj)["phase"] == map_precache.CANCELLED


def test_db_and_network_never_on_the_calling_thread(server):
    """The GUI thread only starts the job: the places query and every download run on job threads."""
    srv = server
    seen = {}

    def connect():
        seen["db"] = threading.current_thread().name
        return media_db([("a.jpg", PRAGUE[1], PRAGUE[0], 0, 0)])
    mj = make_jobs(srv, connect=connect)
    caller = threading.current_thread().name
    t0 = time.perf_counter()
    assert mj.start_precache()
    start_ms = (time.perf_counter() - t0) * 1000
    st = finished(mj)
    assert st["phase"] == map_precache.DONE and st["places"] == 1
    assert seen["db"] != caller and seen["db"].startswith("map-")
    assert caller not in srv.upstream.threads and srv.upstream.threads
    assert start_ms < 20, start_ms                          # starting is instant


def test_gui_event_loop_stays_smooth_during_precache(server, qapp):
    """A 4 ms QTimer on the GUI thread keeps ticking while a precache downloads (fake network)."""
    from PyQt6.QtCore import QElapsedTimer, QEventLoop, QTimer
    srv = server
    srv.upstream.delay = 0.002
    places = [(PRAGUE[0] + i * 0.03, PRAGUE[1] + (i % 7) * 0.02, 1) for i in range(40)]
    mj = make_jobs(srv)
    gaps, clock = [], QElapsedTimer()
    clock.start()
    last = [clock.nsecsElapsed()]

    def tick():
        now = clock.nsecsElapsed()
        gaps.append((now - last[0]) / 1e6)
        last[0] = now
        if not mj.running() and len(gaps) > 50:
            loop.quit()
    loop = QEventLoop()
    timer = QTimer()
    timer.setInterval(4)
    timer.timeout.connect(tick)
    timer.start()
    mj.start_precache(places=places, manual=True)
    QTimer.singleShot(60000, loop.quit)
    loop.exec()
    timer.stop()
    st = mj.state()
    assert st["phase"] == map_precache.DONE and st["done"] > 500, st
    gaps.sort()
    p99 = gaps[int(len(gaps) * 0.99) - 1]
    assert p99 < 16.7, (p99, gaps[-5:])                    # never a lost 60 Hz frame at p99
    print(f"\n[bench] {st['done']} tiles, GUI timer p50 {gaps[len(gaps) // 2]:.1f} ms "
          f"p99 {p99:.1f} ms max {gaps[-1]:.1f} ms")


# ── region pack download / import / clear ────────────────────────────────────────
def test_region_pack_download_builds_a_servable_pack(server, monkeypatch):
    srv = server
    monkeypatch.setitem(tile_store.REGIONS, "tiny", (14.40, 50.08, 14.43, 50.10))
    mj = make_jobs(srv)
    assert mj.start_region("tiny", zmax=13, title="Tiny")
    st = finished(mj)
    assert st["phase"] == map_precache.DONE and st["pack"] == "region-tiny.mbtiles"
    assert st["done"] == sum(1 for _ in tile_store.region_tiles(tile_store.REGIONS["tiny"], 0, 13))
    assert not srv.store.has(OFM_KEY.format(z=13, x=tile_store.lon2tile(14.41, 13), y=tile_store.lat2tile(50.09, 13)))
    assert [p["title"] for p in srv.packs.list()] == ["Tiny"]    # outside the LRU store
    srv.upstream.fail = True
    key = OFM_KEY.format(z=13, x=tile_store.lon2tile(14.41, 13), y=tile_store.lat2tile(50.09, 13))
    assert srv.resolve(key)[4] == "pack"


def test_region_cancel_leaves_no_pack_and_redownload_replaces_an_open_pack(server, monkeypatch):
    srv = server
    monkeypatch.setitem(tile_store.REGIONS, "tiny", (14.40, 50.08, 14.43, 50.10))
    srv.upstream.delay = 0.01
    mj = make_jobs(srv)
    mj.start_region("tiny", zmax=14)
    assert wait_for(lambda: mj.state().get("done", 0) > 10, 10)
    mj.cancel()
    assert finished(mj)["phase"] == map_precache.CANCELLED
    assert srv.packs.list() == [] and not [n for n in os.listdir(srv.packs.dir) if not n.endswith(".part")]
    srv.upstream.delay = 0.0
    mj.start_region("tiny", zmax=12)
    assert finished(mj)["phase"] == map_precache.DONE
    key = OFM_KEY.format(z=12, x=tile_store.lon2tile(14.41, 12), y=tile_store.lat2tile(50.09, 12))
    assert srv.packs.lookup(key) is not None                           # opens a reading connection
    mj.start_region("tiny", zmax=13)                                    # replaces the open file
    assert finished(mj)["phase"] == map_precache.DONE
    assert [p["maxzoom"] for p in srv.packs.list()] == [13]


def test_region_waits_while_the_user_interacts(server, monkeypatch):
    srv = server
    monkeypatch.setitem(tile_store.REGIONS, "tiny", (14.40, 50.08, 14.43, 50.10))
    gov = FakeGov()
    gov.is_busy = True
    mj = make_jobs(srv, gov=gov)
    mj.start_region("tiny", zmax=12)
    assert wait_for(lambda: mj.state()["phase"] == map_precache.WAITING, 5)
    assert mj.state()["done"] == 0
    gov.is_busy = False
    assert finished(mj)["phase"] == map_precache.DONE


def test_region_unknown_is_refused(server):
    assert not make_jobs(server).start_region("atlantis")


def test_import_job_and_failure_reason(server, tmpdir_):
    src = os.path.join(tmpdir_, "alps.mbtiles")
    vector_pack(src, [(0, 0, 0)], title="Alps")
    mj = make_jobs(server)
    mj.start_import(src)
    st = finished(mj)
    assert st["phase"] == map_precache.DONE and st["pack"] == "alps"
    bad = os.path.join(tmpdir_, "bad.mbtiles")
    open(bad, "wb").write(b"zz")
    mj.start_import(bad)
    st = finished(mj)
    assert st["phase"] == map_precache.FAILED and st["reason"]


def test_clear_job_empties_store_shrinks_file_and_keeps_packs(server):
    srv = server
    for i in range(300):
        srv.store.put(OFM_KEY.format(z=14, x=i, y=1), os.urandom(4000), "application/x-protobuf", None)
    srv.store.flush()
    vector_pack(os.path.join(srv.packs.dir, "cz.mbtiles"), [(0, 0, 0)])
    srv.packs.scan()
    before = srv.store.disk_bytes()
    mj = make_jobs(srv)
    assert mj.start_clear()
    assert finished(mj)["phase"] == map_precache.DONE
    assert srv.store.bytes == 0 and srv.store.disk_bytes() < before / 4
    assert len(srv.packs.list()) == 1


def test_background_gate_holds_the_server_queue_until_idle(server):
    srv = server
    open_ = [False]
    srv.background_gate = lambda: open_[0]
    keys = [OFM_KEY.format(z=14, x=i, y=7) for i in range(20)]
    srv.warm(keys, "background")
    time.sleep(0.4)
    assert not any(".pbf" in k for k, _l in srv.upstream.calls)          # user busy: nothing fetched
    open_[0] = True                                                     # idle: parked workers resume
    assert wait_for(lambda: all(srv.store.has(k) for k in keys) or srv.store.flush() and False, 10) or \
        all(srv.store.has(k) for k in keys)
    srv.warm([mini_map.tile_key(15, 1, 1)], "urgent")                   # urgent never waits for the gate
    open_[0] = False
    assert wait_for(lambda: any("tile/15/1/1" in k for k, _l in srv.upstream.calls), 5)


def test_service_installs_the_user_idle_gate():
    srv = tile_store.start(64)
    try:
        assert srv is not None and srv.background_gate is tile_store._user_idle
        assert srv.packs is not None and os.path.normcase(srv.packs.dir).startswith(
            os.path.normcase(paths.CACHE_ROOT))                         # conftest isolation
    finally:
        tile_store.stop()


def test_usage_without_service_is_a_stat_only(monkeypatch, tmpdir_):
    monkeypatch.setattr(paths, "TILE_STORE_PATH", os.path.join(tmpdir_, "t.sqlite"))
    open(paths.TILE_STORE_PATH, "wb").write(b"x" * 5000)
    monkeypatch.setattr(tile_store, "_service", None)
    u = map_precache.usage()
    assert u["bytes"] == 5000 and u["live"] is False


# ── Settings › Advanced › Offline map ────────────────────────────────────────────
class StubJobs:
    def __init__(self):
        self.calls, self._running = [], False
        self._state = {"kind": None, "phase": map_precache.IDLE}

    def state(self):
        return dict(self._state)

    def running(self):
        return self._running

    def start_precache(self, **kw):
        self.calls.append(("precache", kw))
        return True

    def start_region(self, region, **kw):
        self.calls.append(("region", region, kw))
        return True

    def start_import(self, path, **kw):
        self.calls.append(("import", path))
        return True

    def start_clear(self, **kw):
        self.calls.append(("clear", kw))
        return True

    def remove_pack(self, name):
        self.calls.append(("remove", name))
        return True

    def cancel(self, wait_s=0.0):
        self.calls.append(("cancel",))


@pytest.fixture
def settings(qapp, monkeypatch):
    stub = StubJobs()
    monkeypatch.setattr(map_precache, "jobs", lambda: stub)
    monkeypatch.setattr(tile_store, "_service", None)
    import settings_dialog
    dlg = settings_dialog.SettingsDialog()
    dlg._confirm = lambda *a, **k: (True, {})
    yield dlg, stub
    dlg._map_timer.stop()
    dlg.deleteLater()


def test_settings_section_shows_usage_and_limit(settings):
    dlg, _stub = settings
    assert dlg.slider_map_mb.value() == int(dlg.config.get("tile_store_mb", 1024))
    assert "/" in dlg.lbl_map_usage.text() and "GB" in dlg.lbl_map_usage.text()
    assert 0 <= dlg.bar_map_usage.value() <= 1000
    dlg.slider_map_mb.setValue(2048)
    assert "2" in dlg.lbl_map_mb.text()
    assert dlg.lbl_map_region.text()                      # size estimate for the chosen region
    assert dlg.combo_map_region.count() == len(dlg_regions())


def dlg_regions():
    import settings_dialog
    return settings_dialog.MAP_REGIONS


def test_settings_actions_start_background_jobs(settings):
    dlg, stub = settings
    dlg.toggle_map_precache()
    assert stub.calls[-1][0] == "precache" and stub.calls[-1][1]["manual"] is True
    dlg.combo_map_region.setCurrentIndex(dlg.combo_map_region.findData("de"))
    dlg.download_region_pack()
    assert stub.calls[-1][:2] == ("region", "de")
    assert dlg.import_map_pack("C:/x/alps.mbtiles") and stub.calls[-1] == ("import", "C:/x/alps.mbtiles")
    assert dlg.clear_map_cache() and stub.calls[-1][0] == "clear"
    stub._running = True
    stub._state = {"kind": "precache", "phase": map_precache.RUNNING, "done": 10, "skipped": 5, "failed": 0,
                   "total": 100, "bytes": 3 * 2 ** 20, "started": 1}
    dlg.refresh_map_offline()
    assert "15" in dlg.lbl_map_job.text() and "100" in dlg.lbl_map_job.text()
    assert not dlg.btn_map_clear.isEnabled() and dlg.btn_map_precache.isEnabled()
    dlg.toggle_map_precache()
    assert stub.calls[-1] == ("cancel",)


@pytest.mark.parametrize("phase, reason", [
    (map_precache.WAITING, "busy"), (map_precache.WAITING, "metered"), (map_precache.WAITING, "battery"),
    (map_precache.DONE, ""), (map_precache.OFFLINE, ""), (map_precache.BUDGET, ""),
    (map_precache.CANCELLED, ""), (map_precache.FAILED, "boom"), (map_precache.PREPARING, "")])
def test_settings_job_texts_are_translated(settings, phase, reason):
    dlg, _stub = settings
    text = dlg._map_job_text({"kind": "precache", "phase": phase, "reason": reason, "done": 3, "skipped": 2,
                              "failed": 0, "total": 9, "bytes": 0})
    assert text and "settings-map" not in text


def test_settings_save_writes_limit_and_auto(settings, monkeypatch):
    dlg, _stub = settings
    import settings_dialog
    saved = {}
    monkeypatch.setattr(settings_dialog, "save_config", lambda c: saved.update(c))
    dlg.slider_map_mb.setValue(3000)
    dlg.chk_map_prefetch.setChecked(False)
    dlg.save_and_close()
    assert saved["tile_store_mb"] == 3072 and saved["map_prefetch"] is False


def test_settings_lists_installed_packs_without_sqlite(settings, tmpdir_, monkeypatch):
    dlg, stub = settings
    d = os.path.join(tmpdir_, "packs")
    os.makedirs(d)
    open(os.path.join(d, "region-cz.mbtiles"), "wb").write(b"0" * 2 ** 20)   # not even a valid DB
    monkeypatch.setattr(paths, "MAP_PACKS_DIR", d)
    dlg._map_packs_shown = None
    dlg.refresh_map_offline()
    assert dlg.list_map_packs.count() == 1 and "region-cz" in dlg.list_map_packs.item(0).text()
    dlg.list_map_packs.setCurrentRow(0)
    assert dlg.remove_map_pack() and stub.calls[-1] == ("remove", "region-cz")


# ── main window wiring ───────────────────────────────────────────────────────────
def test_main_auto_precache_respects_the_setting(monkeypatch):
    import main
    stub = StubJobs()
    monkeypatch.setattr(map_precache, "jobs", lambda: stub)
    win = types.SimpleNamespace(config={"map_prefetch": False}, _shutting_down=False,
                                precache_options=lambda: {"layer": "standard"})
    assert main.MainWindow._start_photo_precache(win) is False and stub.calls == []
    assert main.MainWindow._start_photo_precache(win, manual=True) is True
    win.config["map_prefetch"] = True
    assert main.MainWindow._start_photo_precache(win) is True
    assert [c[1]["manual"] for c in stub.calls] == [True, False]
    win._shutting_down = True
    assert main.MainWindow._start_photo_precache(win) is False


def test_main_schedules_precache_and_cancels_on_close():
    src = open(os.path.join(os.path.dirname(map_precache.__file__), "main.py"), encoding="utf-8").read()
    deferred = src[src.index("def _run_deferred_services"):src.index("def precache_options")]
    assert "PHOTO_PRECACHE_DELAY_MS, self._start_photo_precache" in deferred
    close = src[src.index("def closeEvent"):src.index("def closeEvent") + 600]
    assert "map_precache.jobs().cancel()" in close
