"""Map host experiment: the real map (map_vault.html + tile store + a real trip) in WebView2.

    python eval/bench_map_webview2.py                     # current display mode
    python eval/bench_map_webview2.py --hz 60             # temporarily switch the panel to 60 Hz
    python eval/bench_map_webview2.py --tier baseline     # emulate a low-end machine's profile

Isolated: no Qt, no app window. A tiny localhost server serves the project files (with
window.TILE_STORE_CONFIG injected into map_vault.html, as main.install_tile_store does) and the
trip's thumbnails. The library and tile store are COPIED to a temp profile, so nothing of the
user's is written. Reports what QtWebEngine could not give (it is locked to 60.0 fps):
  * the host's real vsync cadence and the GPU WebGL runs on (hardware acceleration check),
  * Journey director frame times (p50/p95/p99, dropped = over 1.5x the frame budget),
  * MapLibre render CPU per frame, tile holes during camera motion,
  * the adaptive resolution controller: time spent at each canvas pixel ratio.
--hz switches the display with ChangeDisplaySettingsEx(flags=0): a dynamic change that is never
written to the registry, restored in a finally block (and a crash still reverts on sign-out).
"""
import argparse
import atexit
import ctypes
import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.parse
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import paths  # noqa: E402
import perf_profile  # noqa: E402

WORK = os.path.join(tempfile.gettempdir(), "kairos_wv2_bench")


# ── display mode (Win32) ─────────────────────────────────────────────────────

class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wintypes.WCHAR * 32), ("dmLogPixels", wintypes.WORD),
                ("dmBitsPerPel", wintypes.DWORD), ("dmPelsWidth", wintypes.DWORD),
                ("dmPelsHeight", wintypes.DWORD), ("dmDisplayFlags", wintypes.DWORD),
                ("dmDisplayFrequency", wintypes.DWORD), ("dmICMMethod", wintypes.DWORD),
                ("dmICMIntent", wintypes.DWORD), ("dmMediaType", wintypes.DWORD),
                ("dmDitherType", wintypes.DWORD), ("dmReserved1", wintypes.DWORD),
                ("dmReserved2", wintypes.DWORD), ("dmPanningWidth", wintypes.DWORD),
                ("dmPanningHeight", wintypes.DWORD)]


def display_hz():
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm))
    return int(dm.dmDisplayFrequency)


def set_display_hz(hz):
    dm = DEVMODEW()
    dm.dmSize = ctypes.sizeof(DEVMODEW)
    ctypes.windll.user32.EnumDisplaySettingsW(None, -1, ctypes.byref(dm))
    dm.dmDisplayFrequency = int(hz)
    dm.dmFields = 0x400000 | 0x80000 | 0x100000
    return ctypes.windll.user32.ChangeDisplaySettingsExW(None, ctypes.byref(dm), None, 0, None) == 0


def restore_display():
    ctypes.windll.user32.ChangeDisplaySettingsExW(None, None, None, 0, None)


# ── isolated data: library copy (re-clustered with the current trips.py) + tile store copy ──

def prepare_library(event_id):
    import journey
    import trips
    os.makedirs(WORK, exist_ok=True)
    db = os.path.join(WORK, "photos.db")
    src = sqlite3.connect(paths.DB_PATH)
    dst = sqlite3.connect(db)
    src.backup(dst)                             # consistent copy incl. WAL
    src.close()
    trips.ensure_current(dst)
    dst.commit()
    try:
        if event_id is None:
            best, n = None, 0
            for j in journey.list_journeys(dst):
                data = journey.build_journey(dst, j["id"])
                if data and len(data["stops"]) > n:
                    best, n = j["id"], len(data["stops"])
            event_id = best
        if event_id is None:
            return None
        return journey.build_journey(dst, event_id, thumb_url=lambda p: thumb_url(p))
    finally:
        dst.close()


PORT = {"n": 0}


def thumb_url(p):
    if not p or not os.path.exists(p):
        return ""
    return f"http://127.0.0.1:{PORT['n']}/_file?p={urllib.parse.quote(os.path.abspath(p))}"


def start_tile_store():
    import tile_store
    src = paths.TILE_STORE_PATH
    os.makedirs(WORK, exist_ok=True)
    dst = os.path.join(WORK, "tiles.sqlite")
    if os.path.exists(src) and not os.path.exists(dst):
        s, d = sqlite3.connect(src), sqlite3.connect(dst)
        s.backup(d)
        s.close()
        d.close()
    paths.TILE_STORE_PATH = dst
    return tile_store.start()


# ── localhost server: project files + injected config + thumbnails ────────────

def serve(store_cfg, workers):
    head = (f"<script>window.TILE_STORE_CONFIG = {json.dumps(store_cfg)};"
            + (f"window.TILE_WORKERS_OVERRIDE = {int(workers)};" if workers else "") + "</script>")

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=ROOT, **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/_file":
                p = urllib.parse.parse_qs(u.query).get("p", [""])[0]
                if not os.path.isfile(p):
                    self.send_error(404)
                    return
                with open(p, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", self.guess_type(p))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/map_vault.html":
                with open(os.path.join(ROOT, "map_vault.html"), encoding="utf-8") as f:
                    html = f.read().replace("<head>", "<head>" + head, 1)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    srv = S(("127.0.0.1", 0), H)
    PORT["n"] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def load_probe():
    src = open(os.path.join(HERE, "bench_journey_live.py"), encoding="utf-8").read()
    return src.split('PROBE = r"""', 1)[1].split('"""', 1)[0]


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else 0.0


GL_INFO = """(function () {
  var c = document.createElement('canvas'), gl = c.getContext('webgl2') || c.getContext('webgl');
  if (!gl) return {gl: 'none'};
  var e = gl.getExtension('WEBGL_debug_renderer_info');
  return {gl: gl instanceof WebGL2RenderingContext ? 'webgl2' : 'webgl1',
          renderer: e ? gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
          dpr: window.devicePixelRatio, w: innerWidth, h: innerHeight, ua: navigator.userAgent};
})()"""

RAF = """(function () {
  window.__raf = null; var t = [], last = 0;
  function f(now) { if (last) t.push(now - last); last = now;
    if (t.length < 240) requestAnimationFrame(f); else window.__raf = t; }
  requestAnimationFrame(f); return 0; })()"""


def run_bench(w, args, trip, prof, out):
    def js(code):
        return w.evaluate_js(code)

    def until(code, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = js(code)
            if r:
                return r
            time.sleep(0.1)
        return None

    try:
        until("typeof styleReady !== 'undefined' && styleReady === true && getBaseLayerEngine() === 'vector'", 40)
        out["gl"] = js(GL_INFO)
        js(RAF)
        raf = until("window.__raf", 20) or []
        out["raf"] = {"fps": round(1000 / statistics.mean(raf), 1) if raf else 0,
                      "p50": round(pct(raf, 50), 2), "p99": round(pct(raf, 99), 2)}
        js(f"setPerfProfile({json.dumps(prof.for_map())}); reduceMotion = false; 0")
        out["cadence_ms"] = js("getFrameCadence()")
        probe = load_probe()
        for label in args.runs.split(","):
            js("resetFrameStats(); 0")
            js(probe)
            js("""window.__rstop = false; window.__ratios = {}; window.__rlast = performance.now();
              (function tick() { var now = performance.now(), r = map.getPixelRatio().toFixed(2);
                __ratios[r] = (__ratios[r] || 0) + (now - __rlast); __rlast = now;
                if (!window.__rstop) setTimeout(tick, 100); })(); 0""")
            t0 = time.time()
            if not js(f"playJourney({json.dumps(trip)})"):
                out["error"] = "playJourney refused the data"
                break
            while time.time() - t0 < args.timeout and not js("journeyState().ended"):
                time.sleep(0.25)
            wall = time.time() - t0
            js("window.__rstop = true; 0")
            stats = js("getFrameStats()")["journey"]
            b = js("__b")
            ratios = js("__ratios") or {}
            js("window.endJourney(true); 0")
            mv = max(1, b["moving"])
            tot = sum(ratios.values()) or 1
            out["runs"].append({
                "label": label, "wall_s": round(wall, 1), **stats,
                "dropped_pct": round(100 * stats.get("dropped", 0) / max(1, stats.get("frames", 1)), 1),
                "render_p50": round(pct(b["ren"], 50), 2), "render_p95": round(pct(b["ren"], 95), 2),
                "holes_pct": round(100 * b["holes"] / mv, 1),
                "void_area_pct": round(100 * b.get("voidArea", 0) / mv, 2),
                "ratio_time": {k: round(100 * v / tot, 1) for k, v in sorted(ratios.items())},
                "perf": js("getPerfState()")})
            time.sleep(1.0)
    except Exception as e:                       # report whatever was measured
        out["error"] = repr(e)
    finally:
        w.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=int, default=0, help="switch the panel to this refresh rate for the run")
    ap.add_argument("--tier", default="auto")
    ap.add_argument("--event", type=int)
    ap.add_argument("--size", default="1440x900", help="window size in logical pixels")
    ap.add_argument("--runs", default="warm-up,measured")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    import webview
    import tile_store
    srv_store = start_tile_store()
    if srv_store is None:
        print("tile store did not start")
        return 1
    httpd = serve(srv_store.config(), args.workers)
    trip = prepare_library(args.event)
    if trip is None:
        print("no journey with at least two places")
        return 1

    switched = False
    if args.hz and display_hz() != args.hz:
        atexit.register(restore_display)
        switched = set_display_hz(args.hz)
        time.sleep(2.0)
    try:
        hz = display_hz()
        prof = perf_profile.build(perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb())
                                  if args.tier == "auto" else args.tier,
                                  os.cpu_count() or 4, perf_profile.total_ram_gb(), float(hz))
        out = {"host": "WebView2", "display_hz": hz, "tier": prof.tier, "trip": trip["id"],
               "title": trip["title"], "stops": len(trip["stops"]), "runs": []}
        ww, hh = (int(x) for x in args.size.split("x"))
        win = webview.create_window("Kairos map / WebView2 bench",
                                    f"http://127.0.0.1:{PORT['n']}/map_vault.html", width=ww, height=hh)
        webview.start(run_bench, (win, args, trip, prof, out), gui="edgechromium",
                      storage_path=os.path.join(WORK, "wv2_profile"), private_mode=False)
    finally:
        if switched:
            restore_display()
        httpd.shutdown()
        tile_store.stop()
    out["display_hz_after"] = display_hz()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
