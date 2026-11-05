"""Sprint 3 item 3 benchmark: offline map (map_precache + region packs).

  1. planner at scale: synthetic library of N places over Europe -> plan time, keys, est. MB
  2. real-network precache of the REAL library's photo places into a throwaway store
     (never the app's store), GUI-thread jitter sampled by a 4 ms QTimer meanwhile
  3. network severed: every planned key requested over HTTP from the local server
     -> served share, source, latency p50 / p99
  4. small real region pack (Prague box z0-14) -> download time, size, offline serving from the pack

Usage: python eval/bench_offline_map.py [--places 20000] [--no-network]
The real library is opened read-only. Network volume is small (hundreds of tiles).
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import paths  # noqa: E402

REAL_DB = paths.DB_PATH


def cpu_gate():
    t = time.perf_counter()
    hashlib.sha256(os.urandom(64 * 2 ** 20)).hexdigest()
    mbs = 64 / (time.perf_counter() - t)
    return round(mbs)


def pct(vals, q):
    if not vals:
        return 0.0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q))]


def bench_planner(n):
    import map_precache
    rnd = random.Random(7)
    hubs = [(14.42, 50.08), (16.37, 48.21), (13.40, 52.52), (11.58, 48.14), (19.04, 47.50), (2.35, 48.86),
            (12.50, 41.90), (-3.70, 40.42), (21.01, 52.23), (17.11, 48.15)]
    places = []
    for i in range(n):
        lon, lat = hubs[i % len(hubs)] if rnd.random() < 0.8 else (rnd.uniform(-9, 28), rnd.uniform(36, 60))
        places.append((lon + rnd.gauss(0, 0.15), lat + rnd.gauss(0, 0.1), rnd.randint(1, 30)))
    vec = map_precache.Source("openmaptiles", "tiles.openfreemap.org/planet/V/{z}/{x}/{y}.pbf", 0, 14)
    out = {}
    for label, cap in (("auto", map_precache.AUTO_MAX_KEYS), ("manual", map_precache.MANUAL_MAX_KEYS)):
        t = time.perf_counter()
        keys = map_precache.plan_keys(places, [vec], dpr=1.75, panel=True, max_keys=cap)
        ms = (time.perf_counter() - t) * 1000
        out[label] = {"keys": len(keys), "plan_ms": round(ms, 1)}
    return {"places": n, **out}


def real_places():
    import map_precache
    conn = sqlite3.connect("file:" + REAL_DB.replace("\\", "/") + "?mode=ro", uri=True)
    try:
        return map_precache.read_places(conn)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--places", type=int, default=20000)
    ap.add_argument("--no-network", action="store_true")
    args = ap.parse_args()
    res = {"cpu_sha256_mbs": cpu_gate()}
    print(f"CPU gate sha256 {res['cpu_sha256_mbs']} MB/s (healthy ~1600; < 600 = throttled, numbers invalid)")
    res["planner"] = bench_planner(args.places)
    print("planner", json.dumps(res["planner"]))
    if args.no_network:
        print(json.dumps(res, indent=1))
        return

    tmp = tempfile.mkdtemp(prefix="bench_offline_")
    paths.TILE_STORE_PATH = os.path.join(tmp, "tiles.sqlite")       # throwaway store, never the app's
    paths.MAP_PACKS_DIR = os.path.join(tmp, "map_packs")
    import map_precache
    import tile_store
    from PyQt6.QtCore import QElapsedTimer, QEventLoop, QTimer
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    srv = tile_store.start(1024)
    srv.store.ready.wait(10)
    try:
        places = real_places()
        res["library_places"] = len(places)
        mj = map_precache.MapJobs(service_factory=lambda _mb: srv)
        gaps, clock = [], QElapsedTimer()
        clock.start()
        last = [clock.nsecsElapsed()]
        loop = QEventLoop()

        def tick():
            now = clock.nsecsElapsed()
            gaps.append((now - last[0]) / 1e6)
            last[0] = now
            if not mj.running() and len(gaps) > 20:
                loop.quit()
        timer = QTimer()
        timer.setInterval(4)
        timer.timeout.connect(tick)
        timer.start()
        t0 = time.perf_counter()
        call_ms = time.perf_counter()
        mj.start_precache(manual=True, dpr=1.75, places=None)
        call_ms = (time.perf_counter() - call_ms) * 1000
        QTimer.singleShot(300000, loop.quit)
        loop.exec()
        timer.stop()
        wall = time.perf_counter() - t0
        st = mj.state()
        srv.store.flush()
        res["precache"] = {"phase": st["phase"], "planned": st["total"], "downloaded": st["done"],
                           "skipped": st["skipped"], "failed": st["failed"], "mb": round(st["bytes"] / 2 ** 20, 1),
                           "wall_s": round(wall, 1), "tiles_per_s": round((st["done"] or 0) / max(wall, 1e-3), 1),
                           "start_call_ms": round(call_ms, 2),
                           "gui_timer_ms": {"p50": round(pct(gaps, 0.5), 1), "p99": round(pct(gaps, 0.99), 1),
                                            "max": round(max(gaps or [0]), 1), "samples": len(gaps)}}
        print("precache", json.dumps(res["precache"]))

        # network severed: what the map would get for every planned key
        tj = mj._vector_tilejson(srv)
        keys = map_precache.plan_keys(places, [map_precache.vector_source(tj)], dpr=1.75, panel=True,
                                      max_keys=map_precache.MANUAL_MAX_KEYS)
        srv.online = False
        srv.upstream.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("severed"))
        lat, sources = [], {}
        for key in keys:
            url = srv.bases()[tile_store.shard_of(key, len(srv.ports))] + key
            t = time.perf_counter()
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    r.read()
                    src = r.headers.get("X-Tile-Source")
                    status = r.status
            except urllib.error.HTTPError as e:
                status, src = e.code, "error"
            lat.append((time.perf_counter() - t) * 1000)
            sources[f"{status}:{src}"] = sources.get(f"{status}:{src}", 0) + 1
        served = sum(v for k, v in sources.items() if k.startswith("200"))
        res["offline_serve"] = {"keys": len(keys), "served_pct": round(100.0 * served / max(1, len(keys)), 2),
                                "sources": sources, "p50_ms": round(pct(lat, 0.5), 2), "p99_ms": round(pct(lat, 0.99), 2)}
        print("offline", json.dumps(res["offline_serve"]))

        # small real region pack, then offline serving from it
        srv.online = True
        srv.upstream = tile_store._Upstream()
        tile_store.REGIONS["bench-prague"] = (14.38, 50.06, 14.46, 50.11)
        est_tiles, est_bytes = tile_store.estimate_region(tile_store.REGIONS["bench-prague"])
        mj2 = map_precache.MapJobs(service_factory=lambda _mb: srv)
        t0 = time.perf_counter()
        mj2.start_region("bench-prague", title="Prague (bench)")
        while mj2.running():
            time.sleep(0.1)
        wall = time.perf_counter() - t0
        st = mj2.state()
        pack = next((p for p in srv.packs.list() if p["name"] == "region-bench-prague"), None)
        srv.online = False
        srv.upstream.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError("severed"))
        vec = map_precache.vector_source(tj)
        pk_lat, pk_src = [], {}
        for z, x, y in tile_store.region_tiles(tile_store.REGIONS["bench-prague"], 0, 14):
            key = vec.key(z, x, y)
            t = time.perf_counter()
            r = srv.resolve(key)
            pk_lat.append((time.perf_counter() - t) * 1000)
            pk_src[r[4]] = pk_src.get(r[4], 0) + 1
        res["region_pack"] = {"phase": st["phase"], "tiles": st["done"], "failed": st["failed"],
                              "estimate_tiles": est_tiles, "estimate_mb": round(est_bytes / 2 ** 20, 1),
                              "pack_mb": round((pack or {}).get("bytes", 0) / 2 ** 20, 1), "wall_s": round(wall, 1),
                              "offline_sources": pk_src, "resolve_p50_ms": round(pct(pk_lat, 0.5), 3),
                              "resolve_p99_ms": round(pct(pk_lat, 0.99), 3)}
        print("region", json.dumps(res["region_pack"]))
    finally:
        tile_store.stop()
        app.processEvents()
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
