"""Massive-scale stress benchmark: 20,000 geotagged assets over 5 years.

    python eval/bench_massive_scale.py                  # all stages, 20 000 items
    python eval/bench_massive_scale.py --n 50000        # bigger
    python eval/bench_massive_scale.py --stages db,grid # pick stages (db, grid, map)
    python eval/bench_massive_scale.py --json out.json

Everything runs in a TEMP SANDBOX: DB, config, caches, thumbnails, tile store and logs are
redirected before any app module is imported, and the real-DB guard is on (an accidental real
DB access raises). The user's library is never opened.

Synthetic library: a home city (~55 %) plus ~45 trips (weekends, day trips, 1-2 week holidays)
in real regions (Šumava, Krkonoše, Alps, Adriatic coast, Tatras, Paris, Rome, ...), with GPS
jitter around each place. 8 % videos, 14 % Live Photos with their motion clips. Thumbnails are
real 840 px JPEGs (THUMB_SHORT_EDGE), from a pool of unique images, each hard-linked to its own
path. The grid's path-keyed caches therefore see 20 000 distinct files, as with a real library.

Stages:
  db    bulk insert, the app's grid query (fetch_media_records), map query, month/day query,
        full trip re-cluster (trips.recluster), DB size
  grid  the real MainWindow (AI daemon off): time to the first complete frame, then wheel
        flings through the WHOLE grid, three passes. Per pass: frame interval p50/p95/p99,
        paint CPU, frames with blank (still loading) visible tiles, the main-thread heartbeat
        (2 ms timer: longest stall, stalls > 50 ms), RSS / Python heap growth between passes.
  map   (subprocess, WebView2 via pywebview) setMarkers(all GPS items): time, clusters per
        zoom level, re-cluster cost per zoom change, orbit fps with the pins on the map
"""
import argparse
import ctypes
import gc
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

# ── regions of the synthetic world (lat, lon, spread km, cities) ─────────────────────────────
HOME = (50.0875, 14.4214, 9.0, ["Praha"])
TRIP_REGIONS = [
    (49.15, 13.45, 18, ["Susice", "Kasperske Hory", "Zelezna Ruda", "Hartmanice", "Kolinec"]),
    (50.70, 15.65, 12, ["Spindleruv Mlyn", "Pec pod Snezkou", "Harrachov"]),
    (47.27, 11.40, 25, ["Innsbruck", "Hall in Tirol", "Seefeld"]),
    (46.55, 11.85, 20, ["Cortina", "Canazei", "Ortisei"]),
    (43.51, 16.44, 30, ["Split", "Trogir", "Makarska", "Omis"]),
    (42.65, 18.09, 15, ["Dubrovnik", "Cavtat"]),
    (49.17, 20.10, 15, ["Poprad", "Stary Smokovec", "Zakopane"]),
    (48.86, 2.35, 10, ["Paris"]),
    (41.90, 12.49, 10, ["Roma"]),
    (52.37, 4.90, 8, ["Amsterdam"]),
    (48.20, 16.37, 8, ["Wien"]),
    (50.93, 14.30, 10, ["Hrensko", "Bad Schandau"]),
    (49.75, 13.38, 6, ["Plzen"]),
    (49.19, 16.61, 7, ["Brno"]),
    (37.98, 23.73, 12, ["Athina"]),
]
START = datetime(2021, 11, 1)
YEARS = 5


def setup_sandbox():
    sandbox = tempfile.mkdtemp(prefix="kairos_scale_")
    import paths
    cache = os.path.join(sandbox, "cache")
    paths.CACHE_ROOT = paths.DEFAULT_CACHE_ROOT = cache
    paths.THUMB_DIR = os.path.join(cache, "thumbnails")
    paths.PREVIEW_DIR = os.path.join(cache, "previews")
    paths.WEB_CACHE_DIR = os.path.join(cache, "web")
    paths.PROXY_DIR = os.path.join(cache, "proxies")
    paths.HIGHLIGHT_DIR = os.path.join(cache, "highlights")
    paths.TILE_STORE_PATH = os.path.join(cache, "tiles.sqlite")
    paths.FACE_DIR = os.path.join(sandbox, "faces")
    paths.BACKUP_DIR = os.path.join(sandbox, "backups")
    paths.VAULT_DIR = os.path.join(sandbox, "vault")
    paths.LOG_DIR = os.path.join(sandbox, "logs")
    for d in (paths.THUMB_DIR, paths.PREVIEW_DIR, paths.FACE_DIR, paths.LOG_DIR):
        os.makedirs(d, exist_ok=True)
    import database                          # captures the REAL db path for its guard first
    os.environ[database.TEST_GUARD_ENV] = "1"
    database.DB_PATH = paths.DB_PATH = os.path.join(sandbox, "photos.db")
    import config
    config.CONFIG_FILE = os.path.join(sandbox, "config.json")
    config.LEGACY_CONFIG_FILE = os.path.join(sandbox, "legacy", "config.json")
    import power
    power.get().override = power.PowerState()
    return sandbox


# ── memory ────────────────────────────────────────────────────────────────────

class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


def mem_mb():
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return {"rss": round(pmc.WorkingSetSize / 2**20, 1), "private": round(pmc.PrivateUsage / 2**20, 1)}


def pct(v, p):
    v = sorted(v)
    return round(v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))], 2) if v else 0.0


# ── synthetic library ─────────────────────────────────────────────────────────

def make_thumb_pool(folder, n, rng):
    """n distinct 840 px JPEGs (gradients + shapes: realistic JPEG sizes and decode cost)."""
    import numpy as np
    from PIL import Image, ImageDraw
    os.makedirs(folder, exist_ok=True)
    out = []
    yy, xx = np.mgrid[0:1120, 0:1120].astype(np.float32)
    for i in range(n):
        portrait = i % 3 == 0
        w, h = (840, 1120) if portrait else (1120, 840)
        c0, c1 = rng.randrange(256), rng.randrange(256)
        base = np.stack([(xx[:h, :w] / w * c0) % 256, (yy[:h, :w] / h * c1) % 256,
                         ((xx[:h, :w] + yy[:h, :w]) * 0.13 + i * 7) % 256], -1)
        noise = np.random.default_rng(i).integers(0, 24, (h, w, 3))
        img = Image.fromarray(np.clip(base + noise, 0, 255).astype("uint8"), "RGB")
        d = ImageDraw.Draw(img)
        for _ in range(6):
            x, y, r = rng.randrange(w), rng.randrange(h), rng.randrange(30, 220)
            d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rng.randrange(256) for _ in range(3)))
        p = os.path.join(folder, f"pool_{i:04d}.jpg")
        img.save(p, "JPEG", quality=88, subsampling=0)
        out.append(p)
    return out


def plan_library(n, rng):
    """[(datetime, lat, lon, city, cc, kind)] over YEARS years."""
    import math
    days = YEARS * 365
    trips = []
    t = 20
    while t < days - 20:
        kind = rng.choices(["weekend", "day", "holiday"], [5, 4, 2])[0]
        length = {"weekend": 2, "day": 1, "holiday": rng.randrange(6, 15)}[kind]
        region = rng.choice(TRIP_REGIONS)
        trips.append((t, length, region))
        t += length + rng.randrange(18, 60)
    trip_days = {}
    for start, length, region in trips:
        for d in range(start, start + length):
            trip_days[d] = region
    n_trip = int(n * 0.45)
    n_home = n - n_trip
    trip_day_list = sorted(trip_days)
    out = []

    def jitter(lat, lon, km):
        r = km * math.sqrt(rng.random())
        a = rng.random() * 2 * math.pi
        return lat + r / 111.0 * math.cos(a), lon + r / (111.0 * math.cos(math.radians(lat))) * math.sin(a)

    for _ in range(n_home):
        d = rng.randrange(days)
        when = START + timedelta(days=d, seconds=rng.randrange(7 * 3600, 23 * 3600))
        lat, lon = jitter(HOME[0], HOME[1], HOME[2])
        out.append((when, lat, lon, HOME[3][0], "CZ"))
    for _ in range(n_trip):
        d = rng.choice(trip_day_list)
        region = trip_days[d]
        when = START + timedelta(days=d, seconds=rng.randrange(8 * 3600, 22 * 3600))
        lat, lon = jitter(region[0], region[1], region[2])
        out.append((when, lat, lon, rng.choice(region[3]), "XX"))
    out.sort(key=lambda r: r[0])
    return out, len(trips)


def build_library(sandbox, n, pool_n, rng):
    import database
    t0 = time.perf_counter()
    pool = make_thumb_pool(os.path.join(sandbox, "pool"), pool_n, rng)
    t_pool = time.perf_counter() - t0
    plan, n_trips = plan_library(n, rng)
    lib = os.path.join(sandbox, "library")
    thumbs = os.path.join(sandbox, "cache", "thumbnails")
    os.makedirs(lib, exist_ok=True)
    database.init_db()
    rows, links = [], 0
    t1 = time.perf_counter()
    for i, (when, lat, lon, city, cc) in enumerate(plan):
        src = pool[i % len(pool)]
        kind = rng.choices(["photo", "live_photo", "video"], [78, 14, 8])[0]
        ext = ".mov" if kind == "video" else ".heic"
        fp = os.path.join(lib, f"IMG_{i:06d}{ext}")
        tp = os.path.join(thumbs, f"t_{i:06d}.jpg")
        os.link(src, tp)
        os.link(src, fp)                      # a real file behind every item (availability checks)
        links += 2
        paired = None
        if kind == "live_photo":
            paired = os.path.join(lib, f"IMG_{i:06d}.mov")
            os.link(src, paired)
            links += 1
        rows.append((fp, os.path.basename(fp), os.path.getsize(src), ext, kind, when.strftime("%Y-%m-%d %H:%M:%S"),
                     lat, lon, city, cc, "Apple", "iPhone 15", paired, tp, int(rng.random() < 0.04)))
    t_files = time.perf_counter() - t1
    conn = sqlite3.connect(paths_db())
    t2 = time.perf_counter()
    conn.executemany("INSERT INTO media_items (file_path, file_name, file_size, file_ext, media_type, date_taken, "
                     "latitude, longitude, place_city, place_country, device_make, device_model, paired_video_path, "
                     "thumbnail_path, is_favorite) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # the Live Photo motion clips exist as their own rows too (the grid query hides them)
    clips = [(r[12], os.path.basename(r[12]), r[2], ".mov", "video", r[5], None, None, None, None, "Apple",
              "iPhone 15", None, None, 0) for r in rows if r[12]]
    conn.executemany("INSERT INTO media_items (file_path, file_name, file_size, file_ext, media_type, date_taken, "
                     "latitude, longitude, place_city, place_country, device_make, device_model, paired_video_path, "
                     "thumbnail_path, is_favorite) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", clips)
    conn.commit()
    t_insert = time.perf_counter() - t2
    conn.close()
    return {"items": len(rows), "motion_clips": len(clips), "trips_planned": n_trips, "pool": len(pool),
            "hardlinks": links + len(clips) * 0, "pool_s": round(t_pool, 1), "files_s": round(t_files, 1),
            "insert_s": round(t_insert, 2), "insert_rows_per_s": int((len(rows) + len(clips)) / max(1e-6, t_insert))}


def paths_db():
    import paths
    return paths.DB_PATH


# ── stage: db ─────────────────────────────────────────────────────────────────

def stage_db(out):
    import database
    import main
    import trips

    def timed(fn, runs=7):
        ts, res = [], None
        for _ in range(runs):
            t = time.perf_counter()
            res = fn()
            ts.append((time.perf_counter() - t) * 1000)
        return {"p50_ms": pct(ts, 50), "max_ms": round(max(ts), 1)}, res

    r = {}
    r["grid_query"], recs = timed(lambda: main.fetch_media_records())
    r["grid_records"] = len(recs)
    conn = database.get_connection()
    try:
        r["map_query"], geo = timed(lambda: conn.execute(
            "SELECT m.id, m.latitude, m.longitude, m.file_name, m.place_city, m.date_taken, m.thumbnail_path, "
            "m.media_type FROM media_items m WHERE m.latitude IS NOT NULL AND COALESCE(m.is_trashed, 0) = 0 "
            "AND COALESCE(m.is_hidden, 0) = 0").fetchall())
        r["map_rows"] = len(geo)
        r["markers_build"], markers = timed(lambda: main.build_map_markers(geo), runs=3)
        r["markers_json_kb"] = round(len(json.dumps(markers)) / 1024)
        r["on_this_day_query"], _ = timed(lambda: conn.execute(
            f"SELECT id FROM media_items WHERE {database.MONTH_DAY_SQL} = ?", ("07-08",)).fetchall())
        r["plan_grid"] = [row[3] for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT m.id FROM media_items m WHERE NOT EXISTS (SELECT 1 FROM media_items lp "
            "WHERE lp.paired_video_path = m.file_path) ORDER BY m.date_taken DESC")]
        t = time.perf_counter()
        stats = trips.recluster(conn)
        conn.commit()
        r["recluster_s"] = round(time.perf_counter() - t, 2)
        r["recluster"] = stats
        r["events"] = conn.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind").fetchall()
        r["sample_titles"] = [row[0] for row in conn.execute(
            "SELECT title FROM events WHERE kind = 'journey' ORDER BY item_count DESC LIMIT 6")]
    finally:
        conn.close()
    r["db_mb"] = round(os.path.getsize(paths_db()) / 2**20, 1)
    out["db"] = r
    return markers


# ── stage: grid ───────────────────────────────────────────────────────────────

def warm_micro(res):
    """A returning user: every item already has its micro preview (Sprint 2 LQIP) on disk."""
    import micro_previews
    import main
    import power
    saved = power.get().override
    power.get().override = power.PowerState()        # build at full speed (no battery throttle)
    st = micro_previews.store()
    st.loaded.wait(30)
    t = time.perf_counter()
    recs = main.fetch_media_records()
    st.request([(int(r[0]), r[5]) for r in recs])
    while not st.backfill_idle():
        time.sleep(0.05)
    st.flush()
    power.get().override = saved
    res["micro_warm_s"] = round(time.perf_counter() - t, 1)
    res["micro_count"] = len(st)
    res["micro_ram_mb"] = round(st.nbytes() / 2**20, 1)
    res["micro_disk_mb"] = round(os.path.getsize(st.path) / 2**20, 1)
    micro_previews.reset_for_tests()                  # the app reloads it from disk like a restart


class BackgroundLoad:
    """--load: the background work a real session runs while the user flings the grid, all at
    once: map precache (real TileServer + MapJobs, fake 2 ms network), a search indexer (real
    search_engine.run_search over a 20k x 512 embedding matrix), an AI/face indexer (thumbnail
    decode + resize + 512-d projection, like CLIP image indexing) and library refreshes (the
    daemon's _load_photos_async every 2 s). Counters per worker."""

    def __init__(self, sandbox, window):
        import threading
        self.stop = threading.Event()
        self.counts = {"precache_tiles": 0, "searches": 0, "ai_items": 0, "refreshes": 0, "errors": []}
        self.window = window
        self.sandbox = sandbox
        self.threads = []
        self.refresh_timer = None

    def _guard(self, name, fn):
        try:
            fn()
        except Exception as e:                           # noqa: BLE001
            import traceback
            self.counts["errors"].append(f"{name}: {e!r}")
            traceback.print_exc()

    def start(self):
        import threading
        from PyQt6.QtCore import QTimer
        for name, fn in (("precache", self._precache), ("search", self._search), ("ai", self._ai)):
            t = threading.Thread(target=self._guard, args=(name, fn), name=f"load-{name}", daemon=True)
            t.start()
            self.threads.append(t)
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._refresh)
        self.refresh_timer.start(2000)

    def _refresh(self):
        try:
            self.window._load_photos_async()
            self.counts["refreshes"] += 1
        except Exception as e:                           # noqa: BLE001
            self.counts["errors"].append(f"refresh: {e!r}")

    def _precache(self):
        import gzip as _gz
        import map_precache
        import tile_store
        import types as _t

        class Up:
            def fetch(self, host, path, lane="map"):
                time.sleep(0.002)
                if path == map_precache.OFM_TILEJSON_PATH:
                    return 200, json.dumps({"tiles": ["https://tiles.openfreemap.org/planet/x/{z}/{x}/{y}.pbf"],
                                            "minzoom": 0, "maxzoom": 14}).encode(), "application/json", None
                return 200, _gz.compress(os.urandom(2048) + path.encode()), "application/x-protobuf", "gzip"
        d = os.path.join(self.sandbox, "load_tiles")
        os.makedirs(d, exist_ok=True)
        store = tile_store.TileStore(os.path.join(d, "tiles.sqlite"), budget_mb=512)
        packs = tile_store.PackManager(os.path.join(d, "packs"))
        srv = tile_store.TileServer(store, shards=2, packs=packs)
        srv.upstream = Up()
        store.ready.wait(10)
        gov = _t.SimpleNamespace(busy=lambda: False)
        net = _t.SimpleNamespace(metered=lambda: False)
        pwr = _t.SimpleNamespace(state=lambda: _t.SimpleNamespace(on_battery=False, saver=False, percent=80))
        k = 0
        try:
            while not self.stop.is_set():
                mj = map_precache.MapJobs(service_factory=lambda _mb: srv, gov=gov, net=net, power_monitor=pwr)
                places = [(5 + (k * 37 + i * 1.7) % 20, 42 + (k * 11 + i * 0.9) % 10, 30) for i in range(60)]
                k += 1
                mj.start_precache(places=places, manual=True, world=False)
                while mj.running() and not self.stop.is_set():
                    time.sleep(0.05)
                if mj.running():
                    mj.cancel(wait_s=5)
                self.counts["precache_tiles"] += int(mj.state().get("done") or 0)
        finally:
            srv.shutdown()
            store.close()
            packs.close()

    def _search(self):
        import numpy as np
        import database
        import search_engine
        conn = database.get_connection()
        try:
            ids = [r[0] for r in conn.execute("SELECT id FROM media_items")]
        finally:
            conn.close()
        rng = np.random.default_rng(3)
        mat = rng.standard_normal((len(ids), 512)).astype(np.float32)
        mat /= np.linalg.norm(mat, axis=1, keepdims=True)
        emb = {i: mat[j].tobytes() for j, i in enumerate(ids)}
        row_of = {i: j for j, i in enumerate(ids)}
        vocab = search_engine.load_vocab()

        def semantic(text, pool, embeddings, face_ids):
            # as ai_engine ranks: one cached matrix, numpy ops (they release the GIL). A bytes
            # join of 20k vectors per query held the GIL ~15 ms each and was not the app.
            q = rng.standard_normal(512).astype(np.float32)
            idx = np.fromiter((row_of[p] for p in pool), np.int64, len(pool))
            sc = mat[idx] @ q
            order = np.argpartition(-sc, 200)[:200]
            ranked = [pool[i] for i in order[np.argsort(-sc[order])]]
            return ranked[:60], ranked[60:]
        queries = ["les u jezera", "Praha 2023", "pes na plazi", "hory v zime", "zapad slunce", "Sumava leto"]
        n = 0
        while not self.stop.is_set():
            search_engine.run_search(queries[n % len(queries)], ids, emb, vocab=vocab, semantic_fn=semantic)
            n += 1
            self.counts["searches"] = n
            time.sleep(0.15)                             # a user typing: a query every ~150 ms

    def _ai(self):
        import numpy as np
        import power
        from PIL import Image
        power.set_thread_eco(True)                       # as the AI daemon's workers
        folder = os.path.join(self.sandbox, "pool")
        pool = sorted(os.path.join(folder, f) for f in os.listdir(folder))
        proj = np.random.default_rng(5).standard_normal((224 * 224 * 3 // 64, 512)).astype(np.float32)
        n = 0
        while not self.stop.is_set():
            with Image.open(pool[n % len(pool)]) as im:
                im.draft("RGB", (448, 448))
                a = np.asarray(im.convert("RGB").resize((224, 224), Image.BICUBIC), np.float32)
            v = a.reshape(-1, 64).mean(1) @ proj
            _ = v / (np.linalg.norm(v) + 1e-6)
            n += 1
            self.counts["ai_items"] = n

    def finish(self):
        self.stop.set()
        if self.refresh_timer is not None:
            self.refresh_timer.stop()
        for t in self.threads:
            t.join(15)
        self.counts["alive_after_stop"] = [t.name for t in self.threads if t.is_alive()]
        return dict(self.counts)




def cache_census(grid):
    """MB held by the grid's bounded caches (thumbnail LRU, pretiles, micro previews)."""
    out = {}
    try:
        m = grid.model()
        out["thumbs"] = round(m.ram_cache.bytes / 2**20, 1)
        out["thumbs_n"] = len(m.ram_cache.cache)
        out["pretiles"] = round(m._pretile_bytes / 2**20, 1)
        out["micro"] = round(m.micro.nbytes() / 2**20, 1)
    except Exception as e:                               # noqa: BLE001
        out["err"] = repr(e)
    return out


def stage_grid(out, passes=3, micro="cold", load=False, sandbox=None):
    from unittest import mock
    from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, Qt, QTimer, QElapsedTimer
    from PyQt6.QtGui import QWheelEvent
    from PyQt6.QtWidgets import QApplication
    import main

    app = QApplication.instance()
    clock = QElapsedTimer()
    clock.start()

    def now_ms():
        return clock.nsecsElapsed() / 1e6

    def pump(ms):
        end = now_ms() + ms
        while now_ms() < end:
            app.processEvents()
            time.sleep(0.001)

    pre = {}
    if micro == "warm":
        warm_micro(pre)
    m0 = mem_mb()
    patches = [mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)]
    if os.environ.get("BENCH_NO_MAP"):
        patches.append(mock.patch.object(main.MainWindow, "_ensure_map_view", lambda self: False))
    for name in filter(None, os.environ.get("BENCH_NOOP", "").split(",")):
        patches.append(mock.patch.object(main.MainWindow, name, lambda self, *a, **k: None))
    if os.environ.get("BENCH_NO_DEFERRED"):
        patches.append(mock.patch.object(main.MainWindow, "_run_deferred_services", lambda self: None))
    if os.environ.get("BENCH_NO_MICRO"):
        patches.append(mock.patch.object(main.ApplePhotoModel, "request_micro_backfill", lambda self: 0))
    if os.environ.get("BENCH_NO_VIEWER_PREWARM"):
        patches.append(mock.patch.object(main.MainWindow, "_prewarm_viewer", lambda self: None))
    sys.setswitchinterval(float(os.environ.get("BENCH_SWITCH") or 0.005))
    for p in patches:
        p.start()
    t_open = now_ms()
    w = main.MainWindow()
    w.resize(1500, 950)
    first = {"t": None}
    w.grid_view.firstFrameRendered.connect(lambda: first.__setitem__("t", now_ms()))
    w.show()
    while first["t"] is None and now_ms() - t_open < 60000:
        app.processEvents()
        time.sleep(0.001)
    grid = w.grid_view
    res = {"records": grid.model().rowCount(), "first_frame_ms": round((first["t"] or now_ms()) - t_open),
           "mem_after_open": mem_mb(), "mem_before": m0, "micro": micro, **pre}
    pump(1500)
    if os.environ.get("BENCH_IDLE_S"):                   # deferred services settle, no scrolling
        for k in range(int(os.environ["BENCH_IDLE_S"])):
            pump(1000)
        res["mem_idle"] = mem_mb()
    bg_load = None
    if load:
        bg_load = BackgroundLoad(sandbox, w)
        bg_load.start()
        pump(1000)

    # instrumentation: viewport paints (interval + CPU), blank visible tiles (sampled every 4th
    # frame, outside the timed paint), heartbeat. No tracemalloc while scrolling: it taxes every
    # allocation; the Python heap is read as allocated blocks between passes.
    frames = []
    tiles = []          # per sampled frame: (visible, empty, low-res placeholder, sharp)
    orig_paint = type(grid).paintEvent

    def tile_census(view):
        """What the eye sees per visible tile: EMPTY (a flat placeholder), a LOW-RES micro
        preview (Sprint 2 LQIP), or the real thumbnail. Reads caches only (no decode requests)."""
        model = view.model()
        vp = view.viewport().rect()
        first = view.indexAt(QPoint(vp.left() + 6, vp.top() + 6))
        row = first.row() if first.isValid() else 0
        has_micro = getattr(model, "has_micro", None)
        n = empty = low = sharp = 0
        while row < model.rowCount():
            idx = model.index(row, 0)
            r = view.visualRect(idx)
            if r.top() > vp.bottom():
                break
            if r.intersects(vp):
                n += 1
                item_id = int(model.records[row][0])
                if model.ram_cache.get(item_id) is not None:
                    sharp += 1
                elif has_micro is not None and has_micro(item_id):
                    low += 1
                else:
                    empty += 1
            row += 1
        return n, empty, low, sharp

    def paint(self, ev):
        a = now_ms()
        orig_paint(self, ev)
        b = now_ms()
        blank = (not self.visible_tiles_ready()) if len(frames) % 4 == 0 else None
        if blank is not None:
            tiles.append(tile_census(self))
        frames.append((a, b - a, blank))
    type(grid).paintEvent = paint
    import power
    if hasattr(power, "set_process_timer_resolution_honoured"):
        power.set_process_timer_resolution_honoured()    # a hidden / locked session must not quantise the heartbeat
    import kinetic_scroll
    _vb = kinetic_scroll._VBlank.open() if kinetic_scroll.HAS_QT else None
    res["vblank_ok"] = bool(_vb and _vb())                 # False: session locked / display off -> fps invalid
    # main-thread stack sampler: while the heartbeat is > 150 ms late, sample the GUI thread
    import threading as _th
    import traceback as _tb
    import collections as _co
    stall_stacks = _co.Counter()
    main_id = _th.main_thread().ident
    sampler_on = [True]

    def _sampler():
        while sampler_on[0]:
            time.sleep(0.03)
            if beats and now_ms() - beats[-1] > 150:
                f = sys._current_frames().get(main_id)
                if f is not None:
                    st = _tb.extract_stack(f)[-7:]
                    stall_stacks[" <- ".join(f"{os.path.basename(x.filename)}:{x.lineno} {x.name}"
                                             for x in reversed(st))] += 1
    beats = []
    _th.Thread(target=_sampler, daemon=True, name="stall-sampler").start()
    hb = QTimer()
    hb.setTimerType(Qt.TimerType.PreciseTimer)
    hb.timeout.connect(lambda: beats.append(now_ms()))
    hb.start(4)

    vp = grid.viewport()
    bar = grid.verticalScrollBar()
    from PyQt6.QtCore import QEventLoop

    def run_pass(direction):
        """Flings until the end of the grid, driven by timers inside a real event loop: bursts of
        5 wheel notches 14 ms apart (a fast flick), the next burst 120 ms later."""
        loop = QEventLoop()
        st = {"n": 0, "flings": 0}
        target = bar.maximum() if direction > 0 else bar.minimum()

        def at_end():
            return bar.value() >= target - 2 if direction > 0 else bar.value() <= target + 2

        def notch():
            if at_end():
                QTimer.singleShot(800, loop.quit)
                return
            ev = QWheelEvent(QPointF(700, 400), QPointF(vp.mapToGlobal(QPoint(700, 400))), QPoint(0, 0),
                             QPoint(0, -120 * direction), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                             Qt.ScrollPhase.NoScrollPhase, False)
            QApplication.sendEvent(vp, ev)
            st["n"] += 1
            if st["n"] < 5:
                QTimer.singleShot(14, notch)
            else:
                st["n"] = 0
                st["flings"] += 1
                QTimer.singleShot(120, notch)
        QTimer.singleShot(0, notch)
        loop.exec()
        return st["flings"]

    passes_out = []
    blocks0 = sys.getallocatedblocks()
    for k in range(passes):
        frames.clear()
        tiles.clear()
        beats.clear()
        direction = 1 if k % 2 == 0 else -1
        t0 = now_ms()
        n_flings = run_pass(direction)
        gc.collect()
        dur = now_ms() - t0
        moving = list(frames)
        iv = [b[0] - a[0] for a, b in zip(moving, moving[1:]) if b[0] - a[0] < 250]
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        sampled = [f for f in moving if f[2] is not None]
        passes_out.append({
            "direction": "down" if direction > 0 else "up", "seconds": round(dur / 1000, 1), "flings": n_flings,
            "px": bar.maximum(), "frames": len(moving), "fps_moving": round(1000 / statistics.mean(iv), 1) if iv else 0,
            "interval_p50": pct(iv, 50), "interval_p95": pct(iv, 95), "interval_p99": pct(iv, 99),
            "over_1_5x_budget_pct": round(100 * sum(1 for x in iv if x > 1.5 * 1000 / 120) / max(1, len(iv)), 1),
            "paint_p50": pct([f[1] for f in moving], 50), "paint_p95": pct([f[1] for f in moving], 95),
            "paint_max": round(max([f[1] for f in moving] or [0]), 1),
            "blank_frames_pct": round(100 * sum(1 for f in sampled if f[2]) / max(1, len(sampled)), 1),
            "frames_with_empty_tile_pct": round(100 * sum(1 for t in tiles if t[1]) / max(1, len(tiles)), 1),
            "empty_tiles_pct": round(100 * sum(t[1] for t in tiles) / max(1, sum(t[0] for t in tiles)), 1),
            "lowres_tiles_pct": round(100 * sum(t[2] for t in tiles) / max(1, sum(t[0] for t in tiles)), 1),
            "sharp_tiles_pct": round(100 * sum(t[3] for t in tiles) / max(1, sum(t[0] for t in tiles)), 1),
            "stall_max_ms": round(max(gaps or [0]), 1), "stalls_over_50ms": sum(1 for g in gaps if g > 50),
            "hb_late_p50": pct([g - 4 for g in gaps], 50), "hb_late_p99": pct([g - 4 for g in gaps], 99),
            "hb_late_over_5ms_pct": round(100 * sum(1 for g in gaps if g - 4 > 5) / max(1, len(gaps)), 2),
            "gc_objects": len(gc.get_objects()),
            "mem": mem_mb(), "py_blocks_growth": sys.getallocatedblocks() - blocks0,
            "caches_mb": cache_census(grid)})
    hb.stop()
    sampler_on[0] = False
    res["stall_stacks"] = stall_stacks.most_common(12)
    type(grid).paintEvent = orig_paint
    res["passes"] = passes_out
    if bg_load is not None:
        res["load"] = bg_load.finish()
    try:
        res["ram_cache_items"] = len(grid.model().ram_cache)
    except Exception:                                    # noqa: BLE001
        pass
    if len(passes_out) >= 4:
        res["leak_rss_mb_pass2_to_last"] = round(passes_out[-1]["mem"]["rss"] - passes_out[1]["mem"]["rss"], 1)
        res["leak_private_mb_pass2_to_last"] = round(passes_out[-1]["mem"]["private"] - passes_out[1]["mem"]["private"], 1)
        res["gc_objects_pass2_to_last"] = passes_out[-1]["gc_objects"] - passes_out[1]["gc_objects"]
    if len(passes_out) >= 3:
        res["leak_rss_mb_pass2_to_3"] = round(passes_out[2]["mem"]["rss"] - passes_out[1]["mem"]["rss"], 1)
        res["leak_private_mb_pass2_to_3"] = round(passes_out[2]["mem"]["private"] - passes_out[1]["mem"]["private"], 1)
    # what is left when every grid cache is dropped (bounded caches vs anything else)
    try:
        m = grid.model()
        m.ram_cache.clear()
        m._pretiles.clear()
        m._pretile_bytes = 0
        grid.photo_delegate._tiles.clear()
        grid.photo_delegate._tiles_bytes = 0
        gc.collect()
        pump(800)
        res["mem_after_cache_drop"] = mem_mb()
        from PyQt6.QtWidgets import QApplication as _QA
        res["widgets"] = len(_QA.allWidgets())
    except Exception as e:                               # noqa: BLE001
        res["cache_drop_err"] = repr(e)
    w.close()
    w.deleteLater()
    pump(500)
    for p in patches:
        p.stop()
    out["grid"] = res


# ── stage: map (own process: pywebview runs its own UI loop) ──────────────────

MAP_JS = r"""
(function (markers) {
  var r = {};
  var t = performance.now(); setMarkers(markers); r.setMarkers_ms = +(performance.now() - t).toFixed(1);
  r.clusters_at = {};
  r.recluster_ms = {};
  [3, 5, 7, 9, 11, 13, 15].forEach(function (z) {
    map.jumpTo({ center: [14.42, 50.08], zoom: z, pitch: 0, bearing: 0 });
    var a = performance.now(); render(); r.recluster_ms[z] = +(performance.now() - a).toFixed(1);
    r.clusters_at[z] = getClusterCount();
  });
  return r;
})
"""


def stage_map(markers_path, out_path, seconds):
    import bench_map_webview2 as wb
    import bench_map_orbit as bo
    import perf_profile
    import webview
    import tile_store
    with open(markers_path, encoding="utf-8") as f:
        markers = json.load(f)
    store = wb.start_tile_store()
    httpd = wb.serve(store.config(), 0)
    # the app loads the page from file: and pins load file: thumbnails; this page is served over
    # localhost, where file: images are blocked, so thumbnails go through the bench server
    import urllib.parse
    import urllib.request
    for m in markers:
        u = m.get("thumb") or ""
        if u.startswith("file:"):
            m["thumb"] = wb.thumb_url(urllib.request.url2pathname(urllib.parse.urlparse(u).path))
    res = {"markers": len(markers)}

    def run(w):
        js = w.evaluate_js
        try:
            t0 = time.time()
            while time.time() - t0 < 40 and not js("typeof styleReady !== 'undefined' && styleReady === true"):
                time.sleep(0.2)
            prof = perf_profile.build(perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb()),
                                      os.cpu_count() or 4, perf_profile.total_ram_gb(), float(wb.display_hz()))
            js(f"setPerfProfile({json.dumps(prof.for_map())}); reduceMotion = false; 0")
            js("window.__mk = null; 0")
            # markers are big: hand them over in chunks, then run the measurement in the page
            chunk = 2000
            js("window.__mk = []; 0")
            for i in range(0, len(markers), chunk):
                js("window.__mk.push.apply(window.__mk, %s); 0" % json.dumps(markers[i:i + chunk]))
            res.update(js("(%s)(window.__mk)" % MAP_JS.strip()))
            js("map.jumpTo({center: [13.45, 49.15], zoom: 10, pitch: 50, bearing: 0}); 0")
            t0 = time.time()
            while time.time() - t0 < 25 and not js("!!map.getTerrain() && map.areTilesLoaded() && !map.isMoving()"):
                time.sleep(0.2)
            time.sleep(1.0)
            t0 = time.time()                 # pin thumbnails in view loaded (each arrival re-renders pins)
            while time.time() - t0 < 15 and js("getPinState().thumbs") < min(40, js("getPinCount()")):
                time.sleep(0.2)
            time.sleep(0.5)
            res["pins_in_view"] = js("getPinCount()")
            res["pin_thumbs_loaded"] = js("getPinState().thumbs")
            js("window.__pr0 = getPinRenders(); 0")
            js(bo.PROBE)
            js("(%s)(%f, %f)" % (bo.ORBIT.strip(), seconds, 45.0))
            t0 = time.time()
            while time.time() - t0 < seconds + 20 and not js("window.__orb.done"):
                time.sleep(0.2)
            budget = js("frameBudgetMs()")
            s = bo.summarise(js("window.__orb.frames"), budget)
            res["orbit"] = {k: s[k] for k in ("fps", "p50", "p95", "p99", "dropped_pct", "render_p50", "render_p95")}
            res["pin_renders_during_orbit"] = js("(function(){var a=getPinRenders(),b=window.__pr0;"
                                                 "return {applied:a.applied-b.applied, skipped:a.skipped-b.skipped};})()")
        except Exception as e:
            res["error"] = repr(e)
        finally:
            w.destroy()

    try:
        win = webview.create_window("Kairos scale / map", f"http://127.0.0.1:{wb.PORT['n']}/map_vault.html",
                                    width=1440, height=900)
        webview.start(run, (win,), gui="edgechromium", storage_path=os.path.join(wb.WORK, "wv2_profile"),
                      private_mode=False)
    finally:
        httpd.shutdown()
        tile_store.stop()
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f)


def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--pool", type=int, default=600, help="distinct thumbnail images (hard-linked per item)")
    ap.add_argument("--stages", default="db,grid,map")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--orbit-seconds", type=float, default=10.0)
    ap.add_argument("--json", default="")
    ap.add_argument("--micro", choices=("cold", "warm"), default="cold",
                    help="warm: micro previews built before the grid opens (returning user)")
    ap.add_argument("--load", action="store_true",
                    help="grid passes run with precache + search + AI indexing + library refreshes active")
    ap.add_argument("--_map", nargs=2, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._map:                                    # child process: the map stage
        stage_map(args._map[0], args._map[1], args.orbit_seconds)
        return 0

    stages = set(args.stages.split(","))
    sandbox = setup_sandbox()
    out = {"n": args.n, "sandbox": sandbox}
    rng = random.Random(7)
    try:
        if "grid" in stages:                         # Qt before anything imports QtWebEngine
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QApplication
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
            from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
            _app = QApplication.instance() or QApplication(sys.argv)
        t = time.perf_counter()
        out["library"] = build_library(sandbox, args.n, args.pool, rng)
        out["library"]["total_s"] = round(time.perf_counter() - t, 1)
        print("library", json.dumps(out["library"]), flush=True)
        markers = stage_db(out)
        print("db", json.dumps(out["db"], ensure_ascii=False, default=str), flush=True)
        if "grid" in stages:
            stage_grid(out, args.passes, args.micro, args.load, sandbox)
            print("grid", json.dumps(out["grid"]), flush=True)
        if "map" in stages:
            mk = os.path.join(sandbox, "markers.json")
            res = os.path.join(sandbox, "map.json")
            with open(mk, "w", encoding="utf-8") as f:
                json.dump(markers, f)
            subprocess.run([sys.executable, os.path.abspath(__file__), "--_map", mk, res,
                            "--orbit-seconds", str(args.orbit_seconds)], timeout=600)
            if os.path.exists(res):
                with open(res, encoding="utf-8") as f:
                    out["map"] = json.load(f)
            print("map", json.dumps(out.get("map")), flush=True)
    finally:
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1, default=str)
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main_())
