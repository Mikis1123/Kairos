"""Real-app map probe: the actual MainWindow (python main.py), real library, real tile store,
real WebView2 host – not an isolated page. Scripted gestures (pan, notched wheel, trackpad zoom,
right-drag orbit, double-click) are dispatched inside the page, one input event per vsync.

    python eval/bench_map_live.py                 # app's own frame cap, then uncapped
    python eval/bench_map_live.py --caps 0

Per gesture: vsync cadence, MapLibre frames drawn + intervals, render CPU, long animation frames,
tile fetches (local store vs remote), plus GUI-thread stalls (>40 ms) with a stack sampler –
a blocked Qt thread also stalls input into the WebView2 child window.

CPU gate: the 2026-11-04 audit ran on a laptop throttled to ~1/15 of its speed (sha256 88 MB/s,
V8 1e8 loop 1.4 s) and every frame time came out 10-15x too high. The probe measures the CPU
first and marks the report as invalid below CPU_GATE_MBPS.
"""
import json, os, sys, time, statistics, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("KAIROS_NO_SPLASH", "1")
ap = argparse.ArgumentParser()
ap.add_argument("--caps", default="app,0")
ap.add_argument("--out", default=os.path.join(HERE, "bench_map_live.json"))
ap.add_argument("--scen", default="pan:3000,wheelin:2700,pan:3000,orbit:3000,trackpad:2500,wheelout:3500,dbl:0,pan:2500")
args = ap.parse_args()

import main as M  # noqa
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, QElapsedTimer

M.web_engine.prepare_app()
app = QApplication(sys.argv)
PROBE = open(os.path.join(HERE, "bench_map_live.js"), encoding="utf-8").read()
results = {"runs": [], "gui_late": []}
CPU_GATE_MBPS = 600          # sha256 throughput; a healthy Ryzen AI 7 350 does well over 1000


def cpu_check():
    import hashlib
    buf = os.urandom(32 * 1024 * 1024)
    t = time.perf_counter()
    hashlib.sha256(buf).digest()
    mbps = 32 / (time.perf_counter() - t)
    ok = mbps >= CPU_GATE_MBPS
    print(f"CPU gate: sha256 {mbps:.0f} MB/s – {'ok' if ok else 'THROTTLED, frame times are not representative'}")
    return {"sha256_mbps": round(mbps), "valid": ok}


results["cpu"] = cpu_check()
steps = []


def pct(v, p):
    if not v:
        return 0
    s = sorted(v)
    return round(s[min(len(s) - 1, int(len(s) * p))], 2)


# GUI-thread heartbeat: how late a 4 ms timer fires (GIL / long slots block WebView2 input routing too)
T0 = time.perf_counter()
hb = QElapsedTimer(); hb.start(); last = [0]
def beat():
    now = hb.elapsed()
    if last[0] and now - last[0] > 40:
        results["gui_late"].append([round(time.perf_counter(), 3), now - last[0]])
    last[0] = now
beatT = QTimer(); beatT.setInterval(4); beatT.timeout.connect(beat)


def win():
    for w in app.topLevelWidgets():
        if isinstance(w, M.MainWindow):
            return w


def js(code, cb=None):
    win().map_view.run_js(code, cb)


def later(ms, fn):
    QTimer.singleShot(ms, fn)


def wait_js(expr, then, timeout=60000, t0=None):
    t0 = t0 or time.perf_counter()
    def got(v):
        if v:
            then()
        elif (time.perf_counter() - t0) * 1000 > timeout:
            print("TIMEOUT waiting", expr); then()
        else:
            later(100, lambda: wait_js(expr, then, timeout, t0))
    js(f"!!({expr})", got)


def start():
    w = win()
    if w is None:
        return later(200, start)
    def go():
        w._ensure_map_view()
        w.map_view.add_init_script("window.__vraf = window.requestAnimationFrame.bind(window);")
        w._switch_page(3)
        later(500, wait_loaded)
    later(2500, go)


def wait_loaded():
    w = win()
    if not getattr(w, "_map_loaded", False):
        return later(200, wait_loaded)
    results["t_page_loaded"] = time.perf_counter()
    def st():
        js("JSON.stringify({m: !!window.map, style: !!(window.map && window.map.isStyleLoaded()), tiles: !!(window.map && window.map.areTilesLoaded()), z: window.map && window.map.getZoom(), errs: (window.mapErrors||[]).slice(-5), t: performance.now()})",
           lambda v: print("STATE", round(time.perf_counter()-results["t_page_loaded"],1), v))
    for i in range(0, 30000, 2000): later(i, st)
    wait_js("window.map && window.map.loaded() && window.map.areTilesLoaded()", lambda: (print("TILES DONE after", round(time.perf_counter()-results["t_page_loaded"],1)), inject()), 30000)


def inject():
    print("GUI stalls so far (>40ms):", [(round(t - T0, 1), ms) for t, ms in results["gui_late"]])
    js("""JSON.stringify({nav: performance.getEntriesByType('navigation').map(function(n){return {dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd, resp: n.responseEnd}}),
        res: performance.getEntriesByType('resource').map(function(r){return [Math.round(r.startTime), Math.round(r.duration), r.initiatorType, r.name.slice(0,110)]}),
        loaf: (performance.getEntriesByType('long-animation-frame')||[]).length})""", lambda v: open(os.path.join(HERE, "bench_map_live_boot.json"), "w").write(v or "null"))
    js(PROBE, lambda v: (print("probe", v), later(3000, next_cap) if v in ('installed','already') else finish()))


caps = args.caps.split(",")
scen = [(s.split(":")[0], int(s.split(":")[1])) for s in args.scen.split(",")]


def next_cap():
    if not caps:
        return finish()
    cap = caps.pop(0)
    if cap != "app":
        js(f"window.setFrameCap({int(cap)})")
    js("window.getFrameCap()", lambda v: print("cap", cap, v))
    # back to the library view each run (same start point)
    js("(window.fitAll||function(){})(); 1")
    later(4000, lambda: run_scen(cap, list(scen)))


def run_scen(cap, todo):
    if not todo:
        return next_cap()
    kind, ms = todo.pop(0)
    t_wall = time.perf_counter()
    late0 = len(results["gui_late"])
    js("__probe.start()")
    if kind == "dbl":
        js("__probe.dbl()")
        later(1200, lambda: collect(cap, kind, ms, t_wall, late0, todo))
        return
    js(f"__probe.gesture('{kind}', {ms})")
    def poll():
        js("window.__gdone", lambda v: later(700, lambda: collect(cap, kind, ms, t_wall, late0, todo)) if v else later(50, poll))
    later(ms, poll)


def collect(cap, kind, ms, t_wall, late0, todo):
    def got(v):
        d = json.loads(v) if isinstance(v, str) else v
        vs, rend, cost = d["vs"], d["rend"], d["cost"]
        budget = 1000 / 120
        res = d["res"]
        tiles = [r for r in res if "/tile" in r["n"] or r["n"].endswith((".pbf", ".png", ".jpg", ".webp", ".mvt")) or "{z}" in r["n"]]
        local = [r for r in tiles if "127.0.0.1" in r["n"] or "localhost" in r["n"]]
        remote = [r for r in tiles if r not in local]
        loaf = sorted(d["loaf"], key=lambda e: -e["d"])[:5]
        row = {"cap": cap, "kind": kind, "ms": ms, "zoom": round(d["zoom"], 2), "pitch": round(d["pitch"]), "proj": d["proj"],
               "terrain": d["terrain"], "ratio": d["ratio"],
               "vsync_n": len(vs), "vsync_p50": pct(vs, .5), "vsync_p95": pct(vs, .95), "vsync_max": pct(vs, 1),
               "frames_drawn": len(rend), "draw_p50": pct(rend, .5), "draw_p95": pct(rend, .95), "draw_p99": pct(rend, .99),
               "draw_gaps_over_25ms": sum(1 for x in rend if x > 25),
               "cost_p50": pct(cost, .5), "cost_p95": pct(cost, .95), "cost_max": pct(cost, 1),
               "loaf_n": len(d["loaf"]), "loaf_top": loaf, "longtasks": len(d["long"]),
               "heap_drops": d["heapDrops"], "warns": d["warns"][:5],
               "tiles_local": len(local), "tiles_remote": len(remote),
               "tile_local_p50": pct([r["d"] for r in local], .5), "tile_local_p95": pct([r["d"] for r in local], .95),
               "tile_local_wait_p95": pct([r["wait"] for r in local], .95), "tile_local_queue_p95": pct([r["q"] for r in local], .95),
               "tile_remote_p50": pct([r["d"] for r in remote], .5), "tile_remote_p95": pct([r["d"] for r in remote], .95),
               "remote_hosts": sorted({r["n"].split("/")[2] for r in remote}),
               "gui_late": results["gui_late"][late0:]}
        results["runs"].append(row)
        print(json.dumps({k: row[k] for k in row if k not in ("loaf_top",)}, ensure_ascii=False))
        later(300, lambda: run_scen(cap, todo))
    js("__probe.stop()", got)


def finish():
    js("JSON.stringify({dpr: devicePixelRatio, ua: navigator.userAgent, gl: (function(){var g=map.getCanvas().getContext('webgl2');var e=g.getExtension('WEBGL_debug_renderer_info');return e?g.getParameter(e.UNMASKED_RENDERER_WEBGL):''})(), cad: window.getFrameCadence(), errors: window.mapErrors||null})",
       lambda v: (results.update(env=v), save()))


def save():
    results["stall_main"] = STALLS.most_common(25)
    results["stall_others"] = STALL_OTHERS.most_common(25)
    print("STALL MAIN (25ms samples while GUI thread late >120ms):")
    for k, n in STALLS.most_common(18): print(f"  {n:5d}  {k}")
    print("OTHER THREADS during stalls:")
    for k, n in STALL_OTHERS.most_common(14): print(f"  {n:5d}  {k}")
    json.dump(results, open(args.out, "w"), indent=1, ensure_ascii=False)
    print("ENV", results.get("env"))
    print("saved", args.out)
    win().close()


beatT.start()
import threading, traceback, collections
STALLS = collections.Counter(); STALL_OTHERS = collections.Counter()
main_id = threading.main_thread().ident
def sampler():
    while True:
        time.sleep(0.025)
        age = hb.elapsed() - last[0] if last[0] else 0
        if age > 120:
            frames = sys._current_frames()
            f = frames.get(main_id)
            if f is not None:
                st = traceback.extract_stack(f)[-6:]
                STALLS[" <- ".join(f"{os.path.basename(x.filename)}:{x.lineno} {x.name}" for x in reversed(st))] += 1
            for tid, fr in frames.items():
                if tid in (main_id, threading.get_ident()): continue
                top = traceback.extract_stack(fr)[-3:]
                STALL_OTHERS[" <- ".join(f"{os.path.basename(x.filename)}:{x.lineno} {x.name}" for x in reversed(top))] += 1
threading.Thread(target=sampler, daemon=True, name="stall-sampler").start()
QTimer.singleShot(0, start)
M.main(app)
