"""Orbit profiler: free-camera rotation + tilt over 3D terrain in WebView2, frame by frame.

    python eval/bench_map_orbit.py                         # current panel mode
    python eval/bench_map_orbit.py --hz 120 --laps 2
    python eval/bench_map_orbit.py --variants base,pool64,ring,pool64+ring

Same isolation as eval/bench_map_webview2.py (copied library + tile store, localhost server, no Qt).
The camera is driven like an orbit gesture: one camera step per frame (bearing + pitch), at a
mountain view (Šumava, z10, 40-60 deg), through window.orbitBy (the Alt+drag / Shift+wheel path;
'stock' uses a bare jumpTo, which is what orbitBy did before). Every frame records:
  * the frame interval (rAF) and MapLibre's render CPU (_render),
  * terrain render-to-texture work: renderable terrain tiles, layer stacks, tiles redrawn into
    the RTT pool, pool flushes (pool full -> draw + free everything),
  * GL uploads issued that frame: texImage2D/texSubImage2D count + bytes + CPU time,
    bufferData/bufferSubData bytes,
  * source tiles that finished loading that frame.
Frames over 1.5x the budget are then explained by the counters they carry.

Variants (runtime patches, nothing in map_vault.html is changed by the bench):
  base         as shipped
  pool<N>      RTT pool size N instead of MapLibre's fixed 30
  ring         warm a 360 deg ring around the camera before the orbit (window.forceOrbitRing)
  stock        disable the app's own orbit mitigations (window.ORBIT_TUNING_OFF = true)
"""
import argparse
import atexit
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import bench_map_webview2 as wb  # noqa: E402
import perf_profile  # noqa: E402

PROBE = r"""
(function () {
  if (window.__orb) return 0;
  var O = window.__orb = { frames: [], cur: null, on: false };
  function fresh() { return { tex: 0, texB: 0, texMs: 0, buf: 0, bufB: 0, rtt: 0, flush: 0,
                              loads: 0, ren: 0, renderable: 0, stacks: 0 }; }
  O.cur = fresh();
  var gl = map.painter.context.gl;
  function bytesOf(args) {
    // texImage2D(target, level, ifmt, w, h, border, fmt, type, data) or (..., fmt, type, source)
    if (args.length >= 9) return (args[3] * args[4] * 4) | 0;
    var s = args[5];
    return s && s.width ? (s.width * s.height * 4) | 0 : 0;
  }
  ['texImage2D', 'texSubImage2D'].forEach(function (n) {
    var o = gl[n].bind(gl);
    gl[n] = function () {
      var a = performance.now(); var r = o.apply(null, arguments);
      if (O.on) { O.cur.tex++; O.cur.texMs += performance.now() - a;
        var b = 0; try { b = n === 'texImage2D' ? bytesOf(arguments)
          : (arguments.length >= 9 ? arguments[4] * arguments[5] * 4
             : (arguments[6] && arguments[6].width ? arguments[6].width * arguments[6].height * 4 : 0)); } catch (e) {}
        O.cur.texB += b | 0; }
      return r;
    };
  });
  ['bufferData', 'bufferSubData'].forEach(function (n) {
    var o = gl[n].bind(gl);
    gl[n] = function () {
      var r = o.apply(null, arguments);
      if (O.on) { O.cur.buf++; var d = arguments[n === 'bufferData' ? 1 : 2];
        O.cur.bufB += (d && d.byteLength) || (typeof d === 'number' ? d : 0); }
      return r;
    };
  });
  function hookPool() {
    var rtt = map.painter.renderToTexture;
    if (!rtt || rtt.__orbHooked) return;
    rtt.__orbHooked = true;
    var p = rtt.pool, g = p.getOrCreateFreeObject.bind(p), f = p.freeAllObjects.bind(p);
    p.getOrCreateFreeObject = function () { if (O.on) O.cur.rtt++; return g(); };
    p.freeAllObjects = function () { if (O.on) O.cur.flush++; return f(); };
  }
  map.on('sourcedata', function (e) { if (O.on && e.tile && e.dataType === 'source') O.cur.loads++; });
  var orig = map._render;
  map._render = function (t) {
    hookPool();
    var a = performance.now(), r = orig.call(this, t);
    if (O.on) {
      O.cur.ren += performance.now() - a;
      var rtt = map.painter.renderToTexture;
      if (rtt) { O.cur.renderable = (rtt._renderableTiles || []).length; O.cur.stacks = (rtt._stacks || []).length; }
    }
    return r;
  };
  O.frame = function (dt) { var c = O.cur; c.dt = dt; O.frames.push(c); O.cur = fresh(); };
  return 0;
})()
"""

ORBIT = r"""
(function (seconds, degPerSec) {
  var O = window.__orb; O.frames = []; O.done = false;
  var t0 = 0, last = 0;
  function step(now) {
    if (!t0) { t0 = last = now; O.on = true; requestAnimationFrame(step); return; }
    var dt = now - last; last = now;
    O.frame(dt);
    var el = (now - t0) / 1000;
    if (el >= seconds) {
      O.on = false; O.done = true; return;
    }
    var pitch = 50 + 10 * Math.sin(el * 0.9), db = degPerSec * dt / 1000;
    if (window.ORBIT_TUNING_OFF || typeof orbitBy !== 'function') map.jumpTo({ bearing: map.getBearing() + db, pitch: pitch });
    else orbitBy(db, pitch - map.getPitch());
    requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
  return 0;
})
"""


def pct(v, p):
    return wb.pct(v, p)


def summarise(frames, budget):
    fr = frames[2:]                                # first intervals include the jump
    dts = [f["dt"] for f in fr]
    slow = [f for f in fr if f["dt"] > 1.5 * budget]
    fast = [f for f in fr if f["dt"] <= 1.5 * budget]

    def mean(rows, k):
        return round(statistics.mean([r[k] for r in rows]), 2) if rows else 0

    keys = ("ren", "rtt", "flush", "renderable", "stacks", "tex", "texB", "texMs", "buf", "bufB", "loads")
    return {
        "frames": len(fr), "fps": round(1000 / statistics.mean(dts), 1) if dts else 0,
        "p50": round(pct(dts, 50), 2), "p95": round(pct(dts, 95), 2), "p99": round(pct(dts, 99), 2),
        "max": round(max(dts), 1) if dts else 0,
        "dropped_pct": round(100 * len(slow) / max(1, len(fr)), 2),
        "render_p50": round(pct([f["ren"] for f in fr], 50), 2),
        "render_p95": round(pct([f["ren"] for f in fr], 95), 2),
        "frames_with_rtt_pct": round(100 * sum(1 for f in fr if f["rtt"]) / max(1, len(fr)), 1),
        "frames_with_flush_pct": round(100 * sum(1 for f in fr if f["flush"]) / max(1, len(fr)), 1),
        "slow": {k: mean(slow, k) for k in keys},
        "smooth": {k: mean(fast, k) for k in keys},
    }


def run(w, args, prof, out):
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
        out["gl"] = js(wb.GL_INFO)
        js(f"setPerfProfile({json.dumps(prof.for_map())}); reduceMotion = false; 0")
        js(PROBE)
        budget = js("frameBudgetMs()")
        out["budget_ms"] = round(budget, 2)
        for variant in args.variants.split(","):
            parts = variant.split("+")
            js("window.ORBIT_TUNING_OFF = %s; 0" % ("true" if "stock" in parts else "false"))
            pool = next((int(p[4:]) for p in parts if p.startswith("pool")), 0)
            # fresh view, fresh RTT + tile caches: every variant starts from the same cold-ish state
            js("map.jumpTo({center: [%f, %f], zoom: 4, pitch: 0, bearing: 0}); 0" % tuple(args.center))
            time.sleep(1.0)
            js("map.jumpTo({center: [%f, %f], zoom: %f, pitch: 50, bearing: 0}); 0"
               % (args.center[0], args.center[1], args.zoom))
            until("!!map.getTerrain() && map.areTilesLoaded() && !map.isMoving()", 30)
            time.sleep(1.5)
            js("(function(){var r=map.painter.renderToTexture; if (r) { r.pool._size = %d || r.pool.__base || 30;"
               " r.pool.__base = r.pool.__base || 30; } return 0;})()" % pool)
            if "ring" in parts:
                n = js("window.forceOrbitRing ? forceOrbitRing() : -1")
                until("prefetchState().queued === 0 && prefetchState().active === 0", 20)
                out.setdefault("ring_tiles", {})[variant] = n
            for lap in range(args.laps):
                js("(%s)(%f, %f)" % (ORBIT.strip(), args.seconds, args.speed))
                until("window.__orb.done", args.seconds + 20)
                frames = js("window.__orb.frames")
                s = summarise(frames, budget)
                s.update({"variant": variant, "lap": lap + 1, "terrain": bool(js("!!map.getTerrain()")),
                          "pool_size": js("map.painter.renderToTexture ? map.painter.renderToTexture.pool._size : 0"),
                          "pool_objects": js("map.painter.renderToTexture ? map.painter.renderToTexture.pool._objects.length : 0"),
                          "detail": js("getPerfState()")})
                out["runs"].append(s)
                time.sleep(0.8)
    except Exception as e:
        out["error"] = repr(e)
    finally:
        w.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=int, default=0)
    ap.add_argument("--tier", default="auto")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--center", type=float, nargs=2, default=[13.52, 49.05])   # Šumava
    ap.add_argument("--zoom", type=float, default=10.0)
    ap.add_argument("--seconds", type=float, default=12.0)
    ap.add_argument("--speed", type=float, default=45.0, help="deg of bearing per second")
    ap.add_argument("--laps", type=int, default=2)
    ap.add_argument("--variants", default="base")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    import webview
    import tile_store
    store = wb.start_tile_store()
    if store is None:
        print("tile store did not start")
        return 1
    httpd = wb.serve(store.config(), 0)
    switched = False
    if args.hz and wb.display_hz() != args.hz:
        atexit.register(wb.restore_display)
        switched = wb.set_display_hz(args.hz)
        time.sleep(2.0)
    try:
        hz = wb.display_hz()
        prof = perf_profile.build(perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb())
                                  if args.tier == "auto" else args.tier,
                                  os.cpu_count() or 4, perf_profile.total_ram_gb(), float(hz))
        out = {"host": "WebView2", "display_hz": hz, "tier": prof.tier, "runs": []}
        ww, hh = (int(x) for x in args.size.split("x"))
        win = webview.create_window("Kairos orbit bench", f"http://127.0.0.1:{wb.PORT['n']}/map_vault.html",
                                    width=ww, height=hh)
        webview.start(run, (win, args, prof, out), gui="edgechromium",
                      storage_path=os.path.join(wb.WORK, "wv2_profile"), private_mode=False)
    finally:
        if switched:
            wb.restore_display()
        httpd.shutdown()
        tile_store.stop()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
