"""Offline map, Sprint 3 item 3: photo-area pre-caching and region-pack jobs.

What it does
  * PhotoAreaPlan: every place with photos (visible library only, never the Hidden folder) is
    clustered on the z12 grid (~10 km cells, ~6 km at 50° N) and by z8 region bounding boxes.
    Tile keys go out parents first:
      - view zoom <= 11: the padded bounding box of each photo region
      - z12 cells with photos plus their 8 neighbours, z13 the cells themselves
      - z14 cell descendants (16 tiles a cell), deeper levels 3 x 3 tiles around each place
    for the OpenFreeMap vector planet (the Standard map, also the labels on Satellite) up to view
    zoom 15 (vector tiles stop at 14, MapLibre overzooms), the info panel's mini-map tiles (Esri
    World Street Map, overview + detail, mini_map.layout_tiles) and, when Satellite is the
    chosen base, Esri World Imagery. OpenTopoMap / waymarkedtrails are never bulk-fetched (their
    tile usage policies forbid it); the map page still keeps what the user views.
  * MapJobs (one background job at a time, plain threads, the GUI only polls state()):
      - precache: the plan above, through TileServer.resolve on PRECACHE_WORKERS threads in the
        "background" upstream lane (BACKGROUND_PER_HOST per server), EcoQoS threads
      - region: a bulk download of a REGIONS region (a country / territory or a continent, one
        or more boxes from the world_regions catalogue) into a new MBTiles pack, z0 to the chosen
        detail; refused above tile_store.MAX_REGION_TILES
      - world base (Sprint 4): before the photo plan, the whole world z0..WORLD_ZMAX goes into its
        own pack (tile_store.WORLD_PACK) once, so the globe is never blank offline. A pack is
        outside the LRU budget: a full store can never evict the overview.
      - clear: empties the store (DELETE + VACUUM on the store's writer thread)
    Each worker checks before every tile: the user is interacting (governor busy) -> wait;
    metered connection -> wait; on battery (automatic runs, policy not "full") -> wait;
    the store reaching PRECACHE_BUDGET_SHARE of its budget -> stop. Repeated network failures
    -> stop as "offline" (a later run continues: stored tiles are skipped).

No database read and no network call happens on the GUI thread: places are read with a
private connection inside the job thread, the vector TileJSON is fetched there too.
"""
import ctypes
import json
import math
import os
import queue
import sys
import threading
import time
import uuid

import governor
import power
import tile_store

CELL_Z = 12                         # photo grid (z12 cell ~ 9.8 km at the equator)
REGION_Z = 8                        # photo regions (bounding boxes for the low zooms)
REGION_PAD = 0.15
LEVEL_MAX = 900                     # a level wider than this falls back to the photo cells
VIEW_ZMAX = 15
AUTO_MAX_KEYS = 15000
MANUAL_MAX_KEYS = 60000
PRECACHE_WORKERS = 3
REGION_WORKERS = 3                  # = tile_store.BACKGROUND_PER_HOST: the same courtesy per server
REGION_CHUNK = 48                   # tiles fetched between two interaction checks
PRECACHE_BUDGET_SHARE = 0.85        # never fill the store beyond this share of its budget
OFFLINE_AFTER_FAILS = 12            # consecutive network failures -> stop as offline
WAIT_POLL_S = 0.1                   # how quickly a waiting worker notices the user stopped
METERED_RECHECK_S = 30.0
BATTERY_RECHECK_S = 5.0
PANEL_W, PANEL_H = 360, 180         # the info panel's mini-map (logical px, with a margin)
OFM_HOST = "tiles.openfreemap.org"
OFM_TILEJSON_PATH = "/planet"
ESRI_IMAGERY = ("server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")


# ── tiles ──────────────────────────────────────────────────────────────────────
class Source:
    """A tiled source: key template ({z} {x} {y}), tile zoom range, tile zoom = view zoom + offset."""

    def __init__(self, sid, template, minz=0, maxz=14, offset=0, rank=0):
        self.sid, self.template = sid, template
        self.minz, self.maxz, self.offset, self.rank = minz, maxz, offset, rank

    def key(self, z, x, y):
        return self.template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


def vector_source(tilejson):
    """Source from OpenFreeMap's TileJSON ({"tiles": ["https://.../{z}/{x}/{y}.pbf"], ...})."""
    tiles = (tilejson or {}).get("tiles") or []
    url = tiles[0] if tiles and isinstance(tiles[0], str) else ""
    if not url.startswith("https://"):
        return None
    return Source("openmaptiles", url[len("https://"):], int(tilejson.get("minzoom") or 0),
                  int(tilejson.get("maxzoom") or 14), 0, rank=0)


def raster_offset(dpr, hidpi_raster):
    """map_vault.html rasterTileSize(): 128 px tiles on HiDPI with map_hidpi_raster, else 256;
    MapLibre's tile zoom = view zoom + log2(512 / size)."""
    return 2 if (dpr or 1.0) >= 1.5 and hidpi_raster else 1


def read_places(conn):
    """[(lon, lat, photos)] of the visible library, rounded to ~11 m. Worker thread."""
    from database import visible_media_sql
    rows = conn.execute(f"""
        SELECT ROUND(m.longitude, 4), ROUND(m.latitude, 4), COUNT(*) FROM media_items m
        WHERE m.latitude IS NOT NULL AND m.longitude IS NOT NULL AND {visible_media_sql('m')}
        GROUP BY 1, 2""").fetchall()
    out = []
    for lon, lat, n in rows:
        try:
            lon, lat = float(lon), float(lat)
        except (TypeError, ValueError):
            continue
        if abs(lat) <= 85 and abs(lon) <= 180 and (lat, lon) != (0.0, 0.0):
            out.append((lon, lat, int(n)))
    return out


class PhotoAreaPlan:
    """Where the photos are: z12 cells (weighted) and z8 regions (bounding boxes)."""

    def __init__(self, places):
        self.places = sorted(places, key=lambda p: -p[2])
        cells, regions = {}, {}
        for lon, lat, n in self.places:
            c = (tile_store.lon2tile(lon, CELL_Z), tile_store.lat2tile(lat, CELL_Z))
            cells[c] = cells.get(c, 0) + n
            r = (tile_store.lon2tile(lon, REGION_Z), tile_store.lat2tile(lat, REGION_Z))
            box = regions.get(r)
            if box is None:
                regions[r] = [lon, lat, lon, lat, n]
            else:
                box[0], box[1] = min(box[0], lon), min(box[1], lat)
                box[2], box[3] = max(box[2], lon), max(box[3], lat)
                box[4] += n
        self.cells = sorted(cells, key=lambda c: -cells[c])
        self.regions = []
        for w, s, e, n, weight in sorted(regions.values(), key=lambda b: -b[4]):
            px, py = max(0.01, (e - w) * REGION_PAD), max(0.01, (n - s) * REGION_PAD)
            self.regions.append((w - px, max(-85.0, s - py), e + px, min(85.0, n + py)))

    def _cells_at(self, tz, ring):
        """Tiles at zoom tz covering the photo cells (and their neighbours when ring)."""
        out = []
        n12 = 2 ** CELL_Z
        for cx, cy in self.cells:
            span = [(cx + dx, cy + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)] if ring else [(cx, cy)]
            for x, y in span:
                if not 0 <= y < n12:
                    continue
                x %= n12
                if tz <= CELL_Z:
                    out.append((x >> (CELL_Z - tz), y >> (CELL_Z - tz)))
                else:
                    k = 2 ** (tz - CELL_Z)
                    out.extend((x * k + i, y * k + j) for j in range(k) for i in range(k))
        return out

    def _points_at(self, tz):
        out = []
        n = 2 ** tz
        for lon, lat, _w in self.places:
            cx, cy = tile_store.lon2tile(lon, tz), tile_store.lat2tile(lat, tz)
            out.extend(((cx + dx) % n, cy + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if 0 <= cy + dy < n)
        return out

    def area_keys(self, src, view_zmax=VIEW_ZMAX):
        """[(view zoom, rank, key)] for one source, parents first, busiest places first."""
        out, done = [], set()
        for vz in range(0, view_zmax + 1):
            tz = min(src.maxz, vz + src.offset)
            if tz < src.minz or tz in done:
                continue
            done.add(tz)
            tiles = None
            if tz < CELL_Z:
                tiles = []
                for box in self.regions:
                    x0, y0, x1, y1 = tile_store.bbox_tiles(box, tz)
                    if (x1 - x0 + 1) * (y1 - y0 + 1) > LEVEL_MAX:
                        tiles = None
                        break
                    tiles.extend((x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
            if tiles is None:
                if tz == CELL_Z or tz < CELL_Z:
                    tiles = self._cells_at(tz, ring=True)
                elif 4 ** (tz - CELL_Z) <= 16:
                    tiles = self._cells_at(tz, ring=False)
                else:
                    tiles = self._points_at(tz)
            out.extend((vz, src.rank, src.key(tz, x, y)) for x, y in tiles)
        return out

    def panel_keys(self, dpr):
        """The info panel's mini-map for every place: overview tiles (view 12), detail (view 15)."""
        import mini_map
        out = []
        for lon, lat, _w in self.places:
            z, _size, tiles = mini_map.layout_tiles(lat, lon, PANEL_W, PANEL_H, dpr)
            oz = max(0, z - mini_map.OVERVIEW_DROP)
            cx, cy = mini_map.world_px(lat, lon, oz)
            out.append((12, 5, mini_map.tile_key(oz, int(cx // 256) % (2 ** oz), int(cy // 256))))
            out.extend((15, 5, mini_map.tile_key(z, x, y)) for x, y, _l, _t in tiles)
        return out


def plan_keys(places, sources, dpr=1.0, panel=True, view_zmax=VIEW_ZMAX, max_keys=AUTO_MAX_KEYS):
    """All keys to keep offline, deduplicated, most important first, at most max_keys."""
    plan = PhotoAreaPlan(places)
    items = []
    for src in sources:
        items.extend(plan.area_keys(src, view_zmax))
    if panel:
        items.extend(plan.panel_keys(dpr))
    items.sort(key=lambda t: (t[0], t[1]))         # stable: busiest places first within a level
    seen, keys = set(), []
    for _vz, _rank, key in items:
        if key not in seen:
            seen.add(key)
            keys.append(key)
            if len(keys) >= max_keys:
                break
    return keys


# ── metered network (Windows INetworkCostManager, no extra dependency) ───────────
NLM_CONNECTION_COST_UNRESTRICTED = 0x1
NLM_CONNECTION_COST_FIXED = 0x2
NLM_CONNECTION_COST_VARIABLE = 0x4
NLM_CONNECTION_COST_OVERDATALIMIT = 0x10000
NLM_CONNECTION_COST_ROAMING = 0x40000
NLM_CONNECTION_COST_APPROACHINGDATALIMIT = 0x80000
_CLSID_NETWORK_LIST_MANAGER = "DCB00C01-570F-4A9B-8D69-199FDBA5723B"
_IID_INETWORK_COST_MANAGER = "DCB00008-570F-4A9B-8D69-199FDBA5723B"


def is_metered_cost(cost):
    """True for a connection Windows reports as metered (fixed / variable plan, roaming, near or
    over the data limit). 0 = unknown -> not metered."""
    if not cost:
        return False
    if cost & (NLM_CONNECTION_COST_ROAMING | NLM_CONNECTION_COST_OVERDATALIMIT
               | NLM_CONNECTION_COST_APPROACHINGDATALIMIT):
        return True
    return bool(cost & (NLM_CONNECTION_COST_FIXED | NLM_CONNECTION_COST_VARIABLE))


def read_network_cost():
    """NLM_CONNECTION_COST flags of the current connection, 0 when unknown (not Windows, error).
    COM is initialised on the calling thread (job threads only)."""
    if sys.platform != "win32":
        return 0
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("b", ctypes.c_ubyte * 16)]

        def guid(s):
            g = GUID()
            ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16)
            return g
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx(None, 0)                    # S_FALSE / RPC_E_CHANGED_MODE are fine
        ptr = ctypes.c_void_p()
        clsid, iid = guid(_CLSID_NETWORK_LIST_MANAGER), guid(_IID_INETWORK_COST_MANAGER)
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr.value:
            return 0
        vtbl = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        get_cost = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.c_void_p)(vtbl[3])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        cost = wintypes.DWORD(0)
        try:
            hr = get_cost(ptr, ctypes.byref(cost), None)
        finally:
            release(ptr)
        return int(cost.value) if hr == 0 else 0
    except Exception:                                  # noqa: BLE001 – unknown = not metered
        return 0


class NetworkCost:
    """Cached metered check (one COM call per METERED_RECHECK_S). override: tests."""

    def __init__(self, reader=read_network_cost, recheck_s=METERED_RECHECK_S):
        self._reader, self._recheck = reader, recheck_s
        self._at, self._metered = -1e9, False
        self._lock = threading.Lock()
        self.override = None

    def metered(self):
        if self.override is not None:
            return bool(self.override)
        now = time.monotonic()
        with self._lock:
            if now - self._at >= self._recheck:
                self._metered = is_metered_cost(self._reader())
                self._at = now
            return self._metered


network = NetworkCost()


# ── jobs ───────────────────────────────────────────────────────────────────────
IDLE, PREPARING, RUNNING, WAITING, DONE, OFFLINE, BUDGET, FAILED, CANCELLED = (
    "idle", "preparing", "running", "waiting", "done", "offline", "budget", "failed", "cancelled")
FINAL = (DONE, OFFLINE, BUDGET, FAILED, CANCELLED)


class MapJobs:
    """One offline-map job at a time: precache, region pack, clear. Thread-safe; the GUI reads
    state() from a timer, nothing here ever touches Qt."""

    def __init__(self, service_factory=None, connect=None, gov=None, net=None, power_monitor=None):
        self._service_factory = service_factory or (lambda budget_mb: tile_store.start(budget_mb))
        self._connect = connect
        self._gov = gov
        self._net = net or network
        self._power = power_monitor
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self._state = {"kind": None, "phase": IDLE}
        self.wait_seconds = 0.0             # how long workers waited for the user (bench / tests)

    # ── state ──
    def state(self):
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            if self._state.get("phase") in FINAL and kw.get("phase") not in FINAL:
                kw.pop("phase", None)                  # a finished job stays finished
            self._state.update(kw)

    def _bump(self, **kw):
        with self._lock:
            for k, v in kw.items():
                self._state[k] = self._state.get(k, 0) + v

    def running(self):
        t = self._thread
        return t is not None and t.is_alive()

    def cancel(self, wait_s=0.0):
        self._stop.set()
        t = self._thread
        if t is not None and wait_s:
            t.join(wait_s)

    def _start(self, kind, target, **initial):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop = threading.Event()
            self._state = {"kind": kind, "phase": PREPARING, "total": 0, "done": 0, "skipped": 0,
                           "failed": 0, "bytes": 0, "reason": "", "started": time.time()}
            self._state.update(initial)
            self._thread = threading.Thread(target=self._run_guarded, args=(target,), name=f"map-{kind}", daemon=True)
            self._thread.start()
        return True

    def _run_guarded(self, target):
        try:
            target()
        except Exception as e:                         # noqa: BLE001 – reported in state
            print(f"[MapJobs] {e}")
            self._set(phase=FAILED, reason=str(e))
        finally:
            with self._lock:
                if self._state.get("phase") not in FINAL:
                    self._state["phase"] = CANCELLED if self._stop.is_set() else DONE
                self._state["finished"] = time.time()

    # ── gates ──
    def _governor(self):
        return self._gov or governor.get()

    def _power_monitor(self):
        return self._power or power.get()

    def _blocker(self, manual, policy):
        """Why a worker must wait now, or ''."""
        if self._governor().busy():
            return "busy"
        if self._net.metered():
            return "metered"
        if not manual and policy != "full":
            try:
                if self._power_monitor().state().on_battery:
                    return "battery"
            except Exception:                          # noqa: BLE001
                pass
        return ""

    def _gate(self, manual, policy):
        """Blocks while something says wait. False = the job was cancelled."""
        waited = 0.0
        while not self._stop.is_set():
            why = self._blocker(manual, policy)
            if not why:
                if waited:
                    self._set(phase=RUNNING, reason="")
                return True
            if why != "busy" or waited > 1.0:          # a short interaction doesn't flicker the UI
                self._set(phase=WAITING, reason=why)
            step = WAIT_POLL_S if why == "busy" else (METERED_RECHECK_S / 30 if why == "metered" else BATTERY_RECHECK_S / 5)
            time.sleep(step)
            waited += step
            self.wait_seconds += step
        return False

    # ── precache ──
    def start_precache(self, manual=False, layer="standard", dpr=1.0, hidpi_raster=False,
                       budget_mb=tile_store.DEFAULT_BUDGET_MB, policy=power.DEFAULT_POLICY, places=None,
                       world=False):
        """Photo places -> tile store. places: tests pass them in, otherwise read from the DB
        inside the job thread. world: first build the world-base pack when it is missing."""
        opts = dict(manual=manual, layer=layer, dpr=dpr, hidpi_raster=hidpi_raster, budget_mb=budget_mb,
                    policy=policy, places=places, world=world)
        return self._start("precache", lambda: self._precache(**opts), manual=bool(manual))

    def _read_places(self):
        if self._connect is not None:
            conn = self._connect()
        else:
            from database import get_connection
            conn = get_connection()
        try:
            return read_places(conn)
        finally:
            conn.close()

    def _vector_tilejson(self, srv):
        status, data, _ctype, enc = srv.upstream.fetch(OFM_HOST, OFM_TILEJSON_PATH, "background")
        if status != 200:
            raise OSError(f"TileJSON HTTP {status}")
        if enc == "gzip":
            import zlib
            data = zlib.decompress(data, 47)
        return json.loads(data.decode("utf-8"))

    def _precache(self, manual, layer, dpr, hidpi_raster, budget_mb, policy, places, world=False):
        srv = self._service_factory(budget_mb)
        if srv is None:
            raise RuntimeError("tile store unavailable")
        srv.store.ready.wait(30)
        if world and not world_base_installed(srv):
            if not self._world_base(srv, manual, policy):
                return                          # offline / cancelled: the next run tries again
            with self._lock:                    # the pack step ended DONE; the job goes on
                self._state.update(stage="photos", done=0, skipped=0, failed=0, bytes=0, total=0,
                                   phase=PREPARING, pack="")
        if places is None:
            places = self._read_places()
        self._set(places=len(places))
        if not places:
            self._set(phase=DONE)
            return
        if not self._gate(manual, policy):
            return
        sources = []
        try:
            vec = vector_source(self._vector_tilejson(srv))
        except (OSError, ValueError) as e:
            self._set(phase=OFFLINE, reason=str(e))
            return
        if vec is not None:
            sources.append(vec)
        if layer == "satellite":
            sources.append(tile_store_source_imagery(raster_offset(dpr, hidpi_raster)))
        keys = plan_keys(places, sources, dpr=dpr, panel=True,
                         max_keys=MANUAL_MAX_KEYS if manual else AUTO_MAX_KEYS)
        self._set(total=len(keys), phase=RUNNING)
        self._fetch_all(srv, keys, manual, policy)

    def _fetch_all(self, srv, keys, manual, policy):
        work = queue.SimpleQueue()
        for k in keys:
            work.put(k)
        fails = [0]
        cap = srv.store.budget * PRECACHE_BUDGET_SHARE
        end = threading.Event()
        # store.bytes is counted by the store's writer thread, after the fact: four workers would
        # overshoot the cap by the queue's lag. Count our own fetched bytes too (upper bound).
        base = srv.store.bytes
        fetched = [0]
        fetched_lock = threading.Lock()

        def worker():
            power.set_thread_eco(True)                 # efficiency cores, lower clock (Windows 11)
            while not self._stop.is_set() and not end.is_set():
                if not self._gate(manual, policy):
                    return
                try:
                    key = work.get_nowait()
                except queue.Empty:
                    return
                if srv.has_tile(key):
                    self._bump(skipped=1)
                    continue
                if max(srv.store.bytes, base + fetched[0]) >= cap:
                    self._set(phase=BUDGET)
                    end.set()
                    return
                try:
                    status, data, *_rest = srv.resolve(key, lane="background")
                except Exception:                      # noqa: BLE001
                    status, data = 599, b""
                if status == 200:
                    fails[0] = 0
                    with fetched_lock:
                        fetched[0] += len(data or b"")
                    self._bump(done=1, bytes=len(data or b""))
                elif status in (502, 504, 599):
                    fails[0] += 1
                    self._bump(failed=1)
                    if fails[0] >= OFFLINE_AFTER_FAILS:
                        self._set(phase=OFFLINE)
                        end.set()
                        return
                else:                                  # 404 = tile does not exist (sea, edge)
                    self._bump(failed=1)

        threads = [threading.Thread(target=worker, name="map-precache-fetch", daemon=True)
                   for _ in range(PRECACHE_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if self._stop.is_set():
            self._set(phase=CANCELLED)

    # ── region packs ──
    def start_region(self, region, zmax=tile_store.REGION_ZMAX, title="", budget_mb=tile_store.DEFAULT_BUDGET_MB):
        """A REGIONS id -> one MBTiles pack z0..zmax. False: unknown, or above MAX_REGION_TILES."""
        boxes = tile_store.region_boxes(tile_store.REGIONS.get(region))
        if not boxes or not tile_store.region_fits(boxes, zmax):
            return False
        tiles, size = tile_store.estimate_region(boxes, zmax)
        return self._start("region", lambda: self._region(region, boxes, zmax, title or region, budget_mb),
                           region=region, total=tiles, estimate=size)

    def _region(self, region, bbox, zmax, title, budget_mb):
        srv = self._service_factory(budget_mb)
        if srv is None:
            raise RuntimeError("tile store unavailable")
        self._build_pack(srv, f"region-{region}", bbox, zmax, title, True, "full")

    def _world_base(self, srv, manual, policy):
        """The world z0..WORLD_ZMAX into tile_store.WORLD_PACK. True when it is in place."""
        total, size = tile_store.estimate_region(tile_store.WORLD_BOX, tile_store.WORLD_ZMAX)
        self._set(stage="world", total=total, estimate=size)
        return self._build_pack(srv, tile_store.WORLD_PACK, tile_store.WORLD_BOX, tile_store.WORLD_ZMAX,
                                "World", manual, policy)

    def _build_pack(self, srv, name, bbox, zmax, title, manual, policy):
        """Downloads a region (one box or a list) z0..zmax into packs/<name>.mbtiles. The pack
        appears only when complete (staging file + rename). True = done."""
        if not self._gate(manual, policy):
            return False
        try:
            vec = vector_source(self._vector_tilejson(srv))
        except (OSError, ValueError) as e:
            self._set(phase=OFFLINE, reason=str(e))
            return False
        if vec is None:
            self._set(phase=FAILED, reason="no vector source")
            return False
        self._set(phase=RUNNING)
        fails = [0]
        offline = threading.Event()

        def fetch(zxy):
            key = vec.key(*zxy)
            host, rest = key.split("/", 1)
            try:
                status, data, _ctype, _enc = srv.upstream.fetch(host, "/" + rest, "background")
            except OSError:
                return zxy, None, True
            if status == 200 and data:
                return zxy, data, False
            return zxy, None, status not in (204, 404)      # 404 = no tile there (open sea)

        def tiles():
            from concurrent.futures import ThreadPoolExecutor
            todo = tile_store.region_tiles(bbox, 0, min(zmax, vec.maxz))
            with ThreadPoolExecutor(REGION_WORKERS, thread_name_prefix="map-region-fetch") as pool:
                while True:
                    if not self._gate(manual, policy):       # waits while the user interacts
                        raise InterruptedError("stopped")
                    chunk = [t for _, t in zip(range(REGION_CHUNK), todo)]
                    if not chunk:
                        return
                    for (z, x, y), data, net_error in pool.map(fetch, chunk):
                        if data is None:
                            fails[0] = fails[0] + 1 if net_error else fails[0]
                            self._bump(failed=1)
                            if fails[0] >= OFFLINE_AFTER_FAILS:
                                offline.set()
                                raise InterruptedError("offline")
                            continue
                        fails[0] = 0
                        self._bump(done=1, bytes=len(data))
                        yield z, x, y, data

        packs = srv.packs if srv.packs is not None else tile_store.PackManager()
        os.makedirs(packs.dir, exist_ok=True)
        path = os.path.join(packs.dir, name + tile_store.PACK_SUFFIX)
        staging = path + ".new"             # not *.mbtiles: never picked up half-written
        try:
            tile_store.write_pack(staging, "openmaptiles", "pbf", tiles(), title=title,
                                  bounds=tile_store.region_bounds(bbox),
                                  minzoom=0, maxzoom=min(zmax, vec.maxz), should_stop=self._stop.is_set)
        except InterruptedError:
            self._set(phase=OFFLINE if offline.is_set() else CANCELLED)
            return False
        packs.remove(name)                  # an older copy is open for reading: close it first
        os.replace(staging, path)
        packs.scan()
        self._set(phase=DONE, pack=os.path.basename(path))
        return True

    def remove_pack(self, name):
        srv = tile_store.service()
        packs = srv.packs if srv is not None and srv.packs is not None else tile_store.PackManager()
        return packs.remove(name)

    def start_import(self, src_path, budget_mb=tile_store.DEFAULT_BUDGET_MB):
        def run():
            srv = self._service_factory(budget_mb)
            packs = srv.packs if srv is not None and srv.packs is not None else tile_store.PackManager()
            try:
                info = packs.install(src_path)
            except (ValueError, OSError) as e:
                self._set(phase=FAILED, reason=str(e))
                return
            self._set(phase=DONE, pack=(info or {}).get("name", ""))
        return self._start("import", run)

    # ── clear ──
    def start_clear(self, budget_mb=tile_store.DEFAULT_BUDGET_MB):
        def run():
            srv = self._service_factory(budget_mb)
            if srv is None:
                raise RuntimeError("tile store unavailable")
            srv.store.ready.wait(30)
            if not srv.store.clear(timeout=120):
                raise RuntimeError("clear timed out")
            self._set(phase=DONE)
        return self._start("clear", run)


def world_base_installed(srv=None):
    """The world-base pack is on disk (GUI-thread safe). With a live service the pack must also
    reach WORLD_ZMAX: an older, shallower world pack (z0-5) is rebuilt once at the new depth."""
    packs = getattr(srv, "packs", None) if srv is not None else None
    folder = packs.dir if packs is not None else tile_store.packs_dir()
    if not os.path.isfile(os.path.join(folder, tile_store.WORLD_PACK + tile_store.PACK_SUFFIX)):
        return False
    if packs is None:
        return True
    info = [p for p in packs.list() if p["name"] == tile_store.WORLD_PACK]
    return bool(info) and int(info[0].get("maxzoom") or 0) >= tile_store.WORLD_ZMAX


def tile_store_source_imagery(offset):
    return Source("esri:World_Imagery", ESRI_IMAGERY, 0, 19, offset, rank=2)


def usage():
    """{"bytes", "budget", "packs", "pack_bytes"} without touching SQLite (GUI-thread safe):
    the running store's counters, else the file size on disk."""
    srv = tile_store.service()
    if srv is not None:
        st = srv.store
        used = st.bytes if st.ready.is_set() else st.disk_bytes()
        packs = srv.packs.list() if srv.packs is not None else []
        return {"bytes": used, "budget": st.budget, "packs": packs,
                "pack_bytes": sum(p["bytes"] for p in packs), "live": True}
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += os.path.getsize(tile_store.store_path() + suffix)
        except OSError:
            pass
    return {"bytes": size, "budget": None, "packs": [], "pack_bytes": 0, "live": False}


_jobs = None
_jobs_lock = threading.Lock()


def jobs():
    global _jobs
    with _jobs_lock:
        if _jobs is None:
            _jobs = MapJobs()
        return _jobs

