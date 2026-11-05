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


def stage_grid(out, passes=3, micro="cold"):
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
    beats = []
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
            "mem": mem_mb(), "py_blocks_growth": sys.getallocatedblocks() - blocks0})
    hb.stop()
    type(grid).paintEvent = orig_paint
    res["passes"] = passes_out
    if len(passes_out) >= 3:
        res["leak_rss_mb_pass2_to_3"] = round(passes_out[2]["mem"]["rss"] - passes_out[1]["mem"]["rss"], 1)
        res["leak_private_mb_pass2_to_3"] = round(passes_out[2]["mem"]["private"] - passes_out[1]["mem"]["private"], 1)
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
            stage_grid(out, args.passes, args.micro)
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
