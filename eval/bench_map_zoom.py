"""Zoom-in fidelity bench: does flying into photos ever show blank (unloaded) map?

    python eval/bench_map_zoom.py                 # cold: empty tile store + fresh WebView2 profile
    python eval/bench_map_zoom.py --store copy    # start from a copy of the user's tile store
    python eval/bench_map_zoom.py --targets 6 --layer satellite

Same isolated host as bench_map_webview2.py (WebView2 via pywebview, copied library, the
project's map_vault.html served from localhost with the tile store injected). The scenario is
what the user does: from a regional view (z6) click a photo cluster, i.e. window.flyToPhotos
(the same flyIn as a cluster click: z~16-18, 52 deg tilt, terrain), then look for 2.5 s.

Per rendered frame the probe takes the ideal tiles of the vector basemap and counts those that
have neither themselves, an ancestor nor a descendant loaded (= nothing of the basemap drawn
there). "blank" additionally subtracts places covered by the raster underlay *while it is
actually visible* (evaluated raster-opacity >= 0.25). Reported separately for the flight and
for the 2.5 s after arrival, plus the time from arrival until the view is complete.
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import bench_map_webview2 as wv2  # noqa: E402
import paths  # noqa: E402
import perf_profile  # noqa: E402

PROBE = r"""
window.__z = {phase: 'idle', frames: {fly: 0, settle: 0}, vec: {fly: 0, settle: 0}, blank: {fly: 0, settle: 0},
              blankFrames: {fly: 0, settle: 0}, completeAt: null, arrivedAt: null, err: null, missZ: {}};
(function () {
  var tms = map.style.tileManagers || map.style.sourceCaches || {};
  function hook() {
    tms = map.style.tileManagers || map.style.sourceCaches || {};
    Object.keys(tms).forEach(function (id) {
      var tm = tms[id];
      if (!tm || tm.__zh || typeof tm._updateRetainedTiles !== 'function') return;
      tm.__zh = true;
      var o = tm._updateRetainedTiles.bind(tm);
      tm._updateRetainedTiles = function (ideal, z) { var r = o(ideal, z); tm.__ideal = ideal; tm.__ret = r; return r; };
    });
  }
  hook();
  map.on('style.load', hook);
  function loaded(tm) {
    var out = [];
    Object.keys(tm.__ret || {}).forEach(function (k) {
      var t = tm.getTileByID ? tm.getTileByID(k) : (tm._inViewTiles && tm._inViewTiles.getTileById(k));
      if (t && t.hasData && t.hasData()) out.push(t.tileID);
    });
    return out;
  }
  function contains(a, b) {
    var A = a.canonical, B = b.canonical;
    if (a.wrap !== b.wrap || A.z > B.z) return false;
    var d = B.z - A.z;
    return (B.x >> d) === A.x && (B.y >> d) === A.y;
  }
  function underlayOpacity() {
    try { var l = map.style._layers.underlay; return l && l.visibility !== 'none' ? +l.paint.get('raster-opacity') : 0; }
    catch (e) { return 0; }
  }
  function stats() {
    var vec = null, under = tms.underlay;
    Object.keys(tms).forEach(function (id) { if (!vec && tms[id].getSource && tms[id].getSource().type === 'vector') vec = tms[id]; });
    if (!vec || !vec.__ideal || !vec.__ideal.length) return null;
    var have = loaded(vec), lay = under && underlayOpacity() >= 0.25 ? loaded(under) : [];
    var vm = 0, bm = 0;
    vec.__ideal.forEach(function (t) {
      if (have.some(function (h) { return contains(h, t) || contains(t, h); })) return;
      vm++;
      if (__z.phase === 'settle') { var k = 'z' + t.canonical.z; __z.missZ[k] = (__z.missZ[k] || 0) + 1; }
      if (!lay.some(function (h) { return contains(h, t); })) bm++;
    });
    return { n: vec.__ideal.length, vm: vm, bm: bm };
  }
  var orig = map._render.bind(map);
  map._render = function (t) {
    var r = orig(t), z = __z, ph = z.phase;
    if (ph === 'fly' || ph === 'settle') {
      try {
        var s = stats();
        if (s) {
          z.frames[ph]++;
          z.vec[ph] += s.vm / s.n;
          z.blank[ph] += s.bm / s.n;
          if (s.bm) z.blankFrames[ph]++;
          if (ph === 'settle' && z.completeAt === null && !s.vm && map.areTilesLoaded()) z.completeAt = performance.now();
        }
      } catch (e) { z.err = String(e); }
    }
    return r;
  };
  map.on('moveend', function () { if (__z.phase === 'fly') { __z.phase = 'settle'; __z.arrivedAt = performance.now(); } });
})();
0
"""


ERRS = ("<script>window.__errs = []; addEventListener('error', function (e) {"
        " __errs.push(e.message + ' @' + (e.filename || '').slice(-30) + ':' + e.lineno); });</script>")


def targets_from(trip, n):
    out = []
    for s in trip["stops"]:
        pts = [s.get("entry"), s.get("exit"), {"lat": s["lat"], "lon": s["lon"]}]
        out.append([{"lat": p["lat"], "lon": p["lon"]} for p in pts if p])
    # spread over the trip, not the first n stops
    if len(out) > n:
        step = len(out) / n
        out = [out[int(i * step)] for i in range(n)]
    return out


def run(w, args, trip, prof, out):
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
        ok = until("!!window.map && typeof styleReady !== 'undefined' && styleReady === true"
                   " && getBaseLayerEngine() === 'vector'", 90)
        if not ok:
            out["error"] = "map not ready: " + json.dumps(js("({map: !!window.map, engine: window.getBaseLayerEngine && getBaseLayerEngine(),"
                                                         " w: innerWidth, h: innerHeight, ml: typeof maplibregl, nogl: document.getElementById('nogl').style.display, mw: document.getElementById('map').clientWidth, errs: window.getMapErrors && getMapErrors(), res: performance.getEntriesByType('resource').map(function(e){return e.name.slice(-40)+' '+e.responseStatus}).slice(0,8), url: location.href, jserr: window.__errs})"))
            return
        pm = prof.for_map()
        js(f"setPerfProfile({json.dumps(pm)}); reduceMotion = false; 0")
        if args.no_terrain:
            js("TERRAIN_MIN_ZOOM = 99; 0")
        if args.layer != "standard":
            js(f"setBaseLayer({json.dumps(args.layer)}); 0")
            time.sleep(1.0)
            until("styleReady === true", 20)
        out["gl"] = js(wv2.GL_INFO)
        out["ratio_rest"] = None
        js(PROBE)
        if args.warm_hook:
            js(args.warm_hook)
        res = []
        for k, items in enumerate(targets_from(trip, args.targets)):
            c = items[-1]
            js(f"map.jumpTo({{center: [{c['lon']}, {c['lat']}], zoom: 6, pitch: 0, bearing: 0}}); 0")
            until("map.areTilesLoaded()", 8)
            time.sleep(0.4)
            js("__z.phase='fly'; __z.frames={fly:0,settle:0}; __z.vec={fly:0,settle:0}; __z.blank={fly:0,settle:0};"
               "__z.blankFrames={fly:0,settle:0}; __z.completeAt=null; __z.arrivedAt=null; __z.missZ={}; 0")
            js("resetFrameStats(); 0")
            js(f"flyToPhotos({json.dumps(items)})")
            until("__z.phase === 'settle'", 12)
            time.sleep(2.5)
            z = js("__z")
            fs = js("getFrameStats()")["map"]
            js("__z.phase='idle'; 0")
            if k == 0:
                out["ratio_rest"] = js("map.getPixelRatio()")
            f, s = max(1, z["frames"]["fly"]), max(1, z["frames"]["settle"])
            done = (z["completeAt"] - z["arrivedAt"]) if z["completeAt"] and z["arrivedAt"] else None
            res.append({"zoom": round(js("map.getZoom()"), 2),
                        "fly_vec_pct": round(100 * z["vec"]["fly"] / f, 2),
                        "fly_blank_pct": round(100 * z["blank"]["fly"] / f, 2),
                        "fly_blank_frames_pct": round(100 * z["blankFrames"]["fly"] / f, 1),
                        "settle_vec_pct": round(100 * z["vec"]["settle"] / s, 2),
                        "settle_blank_pct": round(100 * z["blank"]["settle"] / s, 2),
                        "complete_ms": round(done) if done is not None else None, "err": z["err"],
                        "frames": z["frames"], "miss_z": z["missZ"], "terrain": js("isTerrainOn()"),
                        "frame_p95": fs.get("p95"), "frame_p99": fs.get("p99"),
                        "dropped_pct": round(100 * fs.get("dropped", 0) / max(1, fs.get("frames", 1)), 1)})
        out["targets"] = res

        def mean(key):
            v = [r[key] for r in res if r[key] is not None]
            return round(statistics.mean(v), 2) if v else None
        out["summary"] = {k: mean(k) for k in ("fly_vec_pct", "fly_blank_pct", "fly_blank_frames_pct",
                                               "settle_vec_pct", "settle_blank_pct", "complete_ms",
                                               "frame_p95", "frame_p99", "dropped_pct")}
        out["incomplete_after_2500ms"] = sum(1 for r in res if r["complete_ms"] is None)
        try:
            out["store"] = js("window.getWarmState ? getWarmState() : null")
        except Exception:
            pass
    except Exception as e:
        out["error"] = repr(e)
    finally:
        w.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", choices=("empty", "copy", "keep"), default="empty")
    ap.add_argument("--profile", choices=("fresh", "keep"), default="fresh")
    ap.add_argument("--layer", default="standard")
    ap.add_argument("--targets", type=int, default=6)
    ap.add_argument("--event", type=int)
    ap.add_argument("--tier", default="auto")
    ap.add_argument("--size", default="1440x900")
    ap.add_argument("--warm-hook", default="", help="JS run after the probe (e.g. a region warm-up)")
    ap.add_argument("--no-terrain", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    import webview
    import tile_store
    os.makedirs(wv2.WORK, exist_ok=True)
    store = os.path.join(wv2.WORK, "tiles.sqlite")
    if args.store != "keep":
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(store + suffix)
            except FileNotFoundError:
                pass
    if args.store == "empty":
        paths.TILE_STORE_PATH = store
        srv = tile_store.start()
    else:
        srv = wv2.start_tile_store()
    prof_dir = os.path.join(wv2.WORK, "wv2_zoom_profile")
    if args.profile == "fresh":
        shutil.rmtree(prof_dir, ignore_errors=True)
    httpd = wv2.serve(srv.config(), 0, ERRS)
    trip = wv2.prepare_library(args.event)
    if trip is None:
        print("no journey with at least two places")
        return 1
    prof = perf_profile.build(perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb())
                              if args.tier == "auto" else args.tier,
                              os.cpu_count() or 4, perf_profile.total_ram_gb(), float(wv2.display_hz()))
    out = {"store": args.store, "profile": args.profile, "layer": args.layer, "tier": prof.tier,
           "trip": trip["id"], "title": trip["title"]}
    ww, hh = (int(x) for x in args.size.split("x"))
    win = webview.create_window("Kairos map / zoom bench", f"http://127.0.0.1:{wv2.PORT['n']}/map_vault.html",
                                width=ww, height=hh)
    try:
        webview.start(run, (win, args, trip, prof, out), gui="edgechromium", storage_path=prof_dir,
                      private_mode=False)
    finally:
        httpd.shutdown()
        out["store_stats"] = srv.store.stats() if srv else None
        tile_store.stop()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
