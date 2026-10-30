"""Cesta se skutečnou sítí a skutečným výletem z knihovny – plynulost i „děravé“ dlaždice.

    python eval/bench_journey_live.py               # nejdelší výlet z photos.db, studená + teplá cache
    python eval/bench_journey_live.py --event 12    # konkrétní událost
    python eval/bench_journey_live.py --warm-only   # bez mazání cache (opakované měření)

Na rozdíl od bench_journey.py (síť zablokovaná = měří se jen kamera a kreslení) jde o to, co
vidí uživatel: vektorové dlaždice OpenFreeMap, terén AWS, piny a trasa výletu. Měří se:
  * doba snímku režiséra (p50 / p95 / p99, vynechané snímky nad 1,5× rozpočtu),
  * čas kreslení MapLibre (_render) – CPU cena jednoho snímku mapy,
  * „díry“: podíl snímků pohybu kamery, kdy viditelné dlaždice nejsou načtené (areTilesLoaded),
  * příjezdy: kolikrát v cíli úseku chyběly dlaždice a jak dlouho se na ně čekalo.
Studená cache = smazaný profil (první přehrání), teplá = hned druhé přehrání.
Profil prohlížeče (HTTP cache) leží v %TEMP%/kairos_bench_profile, ne v profilu aplikace.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from PyQt6.QtCore import Qt, QUrl, QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
app = QApplication.instance() or QApplication(sys.argv)

import main  # noqa: E402  (příznaky Chromia, povolené hosty)
import journey  # noqa: E402
import perf_profile  # noqa: E402

PROFILE_DIR = os.path.join(tempfile.gettempdir(), "kairos_bench_profile")

# Měřák vložený do stránky: _render, snímky pohybu bez načtených dlaždic, čekání v cíli
PROBE = r"""
window.__b = {ren: [], moving: 0, holes: 0, base: 0, src: {}, vecVoidFrames: 0, voidFrames: 0,
              vecVoidArea: 0, voidArea: 0};
(function () {
  // Sprint 1 – skutečná díra: ideální dlaždice vektorového zdroje (ty, které MapLibre pro záběr
  // chce), pro kterou není načtená ona, předek ani potomek = na obrazovce není nic z podkladu.
  // „voidArea“ navíc odečte místa, která kryje načtený rastrový podklad (underlay).
  var tms = map.style.tileManagers || map.style.sourceCaches || {};
  Object.keys(tms).forEach(function (id) {
    var tm = tms[id];
    if (!tm || tm.__hooked || typeof tm._updateRetainedTiles !== 'function') return;
    tm.__hooked = true;
    var o = tm._updateRetainedTiles.bind(tm);
    tm._updateRetainedTiles = function (ideal, z) { var ret = o(ideal, z); tm.__ideal = ideal; tm.__ret = ret; return ret; };
  });
  function loadedTiles(tm) {
    var out = [];
    Object.keys(tm.__ret || {}).forEach(function (k) {
      var t = tm.getTileByID ? tm.getTileByID(k) : (tm._inViewTiles && tm._inViewTiles.getTileById(k));
      if (t && t.hasData && t.hasData()) out.push(t.tileID);
    });
    return out;
  }
  function contains(a, b) {           // a (předek nebo stejná) obsahuje b
    var A = a.canonical, B = b.canonical;
    if (a.wrap !== b.wrap || A.z > B.z) return false;
    var d = B.z - A.z;
    return (B.x >> d) === A.x && (B.y >> d) === A.y;
  }
  function voidStats() {
    var vec = null, under = tms.underlay;
    Object.keys(tms).forEach(function (id) { if (!vec && tms[id].getSource && tms[id].getSource().type === 'vector') vec = tms[id]; });
    if (!vec || !vec.__ideal || !vec.__ideal.length) return null;
    var have = loadedTiles(vec), lay = under && under.used !== false ? loadedTiles(under) : [];
    var vecMiss = 0, miss = 0;
    vec.__ideal.forEach(function (t) {
      if (have.some(function (h) { return contains(h, t) || contains(t, h); })) return;
      vecMiss++;
      if (!lay.some(function (h) { return contains(h, t); })) miss++;
    });
    return { n: vec.__ideal.length, vecMiss: vecMiss, miss: miss };
  }
  var orig = map._render.bind(map);
  map._render = function (t) {
    var a = performance.now(), r = orig(t);
    __b.ren.push(performance.now() - a);
    if (director.directing && journey.active && !journey.paused) {
      __b.moving++;
      try {
        var v = voidStats();
        if (v) {
          if (v.vecMiss) __b.vecVoidFrames++;
          if (v.miss) __b.voidFrames++;
          __b.vecVoidArea += v.vecMiss / v.n;
          __b.voidArea += v.miss / v.n;
        }
      } catch (e) { __b.err = String(e); }
      try {
        if (!map.areTilesLoaded()) {
          __b.holes++;
          // které zdroje díru tvoří (vektory / reliéf / rastr)
          var sc = map.style.sourceCaches || map.style.tileManagers || {}, base = false;
          Object.keys(sc).forEach(function (id) {
            try {
              if (!sc[id].loaded()) {
                __b.src[id] = (__b.src[id] || 0) + 1;
                // podklad = dlaždice ze sítě; trasa Cesty (j-*) a piny jsou GeoJSON přepočítávaný
                // ve workeru při přechodu úrovně zoomu – mezitím zůstává vidět, díra to není
                if (id.indexOf('j-') !== 0 && id !== 'pins') base = true;
              }
            } catch (e) {}
          });
          if (base) __b.base++;
        }
      } catch (e) {}
    }
    return r;
  };
})();
0
"""


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def js(page, code, timeout=8000):
    loop, out = QEventLoop(), {}
    page.runJavaScript(code, lambda r: (out.__setitem__("r", r), loop.quit()))
    QTimer.singleShot(timeout, loop.quit)
    loop.exec()
    return out.get("r")


def pick_event(conn, event_id=None):
    if event_id is not None:
        return int(event_id)
    best, best_n = None, 0
    for j in journey.list_journeys(conn):
        data = journey.build_journey(conn, j["id"])
        if data and len(data["stops"]) > best_n:
            best, best_n = j["id"], len(data["stops"])
    return best


def build_trip(event_id):
    conn = sqlite3.connect(os.path.join(ROOT, "photos.db"))
    try:
        eid = pick_event(conn, event_id)
        if eid is None:
            return None
        to_url = lambda p: QUrl.fromLocalFile(p).toString() if p and os.path.exists(p) else ""  # noqa: E731
        return journey.build_journey(conn, eid, thumb_url=to_url)
    finally:
        conn.close()


def make_profile():
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    profile = QWebEngineProfile("kairos_bench")
    profile.setPersistentStoragePath(PROFILE_DIR)
    profile.setCachePath(os.path.join(PROFILE_DIR, "cache"))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    profile._interceptor = main.WebUrlInterceptor()
    profile.setUrlRequestInterceptor(profile._interceptor)
    return profile


def open_page(size, html="map_vault.html", profile=None, store=True, workers=0):
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineScript
    profile = profile or make_profile()
    interceptor = profile._interceptor
    page = QWebEnginePage(profile)
    if store:                                   # C7e: dlaždice přes místní úložiště (jako aplikace)
        main.install_tile_store(page)
    if workers:                                 # počet workerů MapLibre se nastavuje před vznikem mapy
        s = QWebEngineScript()
        s.setSourceCode(f"window.TILE_WORKERS_OVERRIDE = {int(workers)};")
        s.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        s.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        page.scripts().insert(s)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
    view = QWebEngineView()
    view.setPage(page)
    view.resize(*size)
    view.show()
    loop = QEventLoop()
    page.loadFinished.connect(lambda ok: loop.quit())
    QTimer.singleShot(15000, loop.quit)
    page.load(QUrl.fromLocalFile(os.path.join(ROOT, html)))
    loop.exec()
    return profile, interceptor, page, view


def pct(values, p):
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))] if v else 0.0


def play(page, trip, label, timeout_s, tick=None):
    js(page, "resetFrameStats(); window.__arrivals = []; 0")
    js(page, PROBE)
    # příjezd = začátek zastávky; zaznamenat, zda byly dlaždice hotové a jak dlouho se čekalo
    js(page, """(function () {
      if (window.__arriveHooked) return 0;
      window.__arriveHooked = true;
      var o = window.__benchArrive = window.__benchArrive || null;
      if (typeof onStopArrive === 'function') {
        var orig = onStopArrive;
        onStopArrive = function (i, info) { __arrivals.push(info || {}); return orig.apply(this, arguments); };
      }
      return 0; })()""")
    t0 = time.time()
    if not js(page, f"playJourney({json.dumps(trip)})"):
        print(f"{label}: playJourney odmítl data")
        return None
    while time.time() - t0 < timeout_s and not js(page, "journeyState().ended"):
        if tick:
            tick()
        wait(250)
    wall = time.time() - t0
    stats = js(page, "getFrameStats()")["journey"]
    probe = js(page, "__b")
    arrivals = js(page, "window.__arrivals || []") or []
    js(page, "window.endJourney(true); 0")
    holes = probe["holes"] / max(1, probe["moving"])
    mv = max(1, probe["moving"])
    ren = probe["ren"]
    line = (f"{label:6s} {wall:5.1f} s  snímky {stats.get('frames', 0):5d}  p50 {stats.get('p50', 0):5.1f}  "
            f"p95 {stats.get('p95', 0):5.1f}  p99 {stats.get('p99', 0):5.1f}  vynechané {stats.get('dropped', 0):4d} "
            f"({100 * stats.get('dropped', 0) / max(1, stats.get('frames', 1)):4.1f} %)  "
            f"render p50 {pct(ren, 50):5.1f} p95 {pct(ren, 95):5.1f}  díry {100 * holes:4.1f} %  "
            f"podklad {100 * probe.get('base', 0) / max(1, probe['moving']):4.1f} %  "
            f"DÍRY vektor {100 * probe.get('vecVoidFrames', 0) / mv:4.1f} % snímků / {100 * probe.get('vecVoidArea', 0) / mv:4.2f} % plochy, "
            f"s podkladem {100 * probe.get('voidFrames', 0) / mv:4.1f} % / {100 * probe.get('voidArea', 0) / mv:4.2f} %")
    if probe.get("err"):
        line += f"  [chyba měřáku: {probe['err']}]"
    if probe.get("src"):
        line += "  zdroje děr " + ", ".join(f"{k} {100 * v / max(1, probe['moving']):.1f} %"
                                           for k, v in sorted(probe["src"].items(), key=lambda kv: -kv[1]))
    if arrivals:
        waited = [a.get("waitedMs", 0) for a in arrivals]
        unready = sum(1 for a in arrivals if not a.get("ready", True))
        line += f"  příjezdy {len(arrivals)}: nehotové {unready}, čekání p95 {pct(waited, 95):.0f} ms"
    print(line, flush=True)
    return stats


def main_():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", type=int)
    ap.add_argument("--warm-only", action="store_true")
    ap.add_argument("--size", default="1600x1000")
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--inject", default="", help="JS před přehráním (pokusy: vypnout vrstvu, změnit parametr)")
    ap.add_argument("--report", default="", help="JS výraz vypsaný po každém přehrání (diagnostika)")
    ap.add_argument("--html", default="map_vault.html", help="stránka ve složce projektu (srovnání verzí)")
    ap.add_argument("--no-store", action="store_true", help="bez místního úložiště dlaždic (srovnání)")
    ap.add_argument("--workers", type=int, default=0, help="strop workerů MapLibre (ablace; 0 = jako aplikace)")
    ap.add_argument("--clips", action="store_true",
                    help="klipy karet (Live Photo / video) jako v aplikaci: chybějící se vyrábí ZA BĚHU Cesty")
    ap.add_argument("--restart", action="store_true",
                    help="navíc přehrání v nové stránce (= další spuštění aplikace: prázdná paměť MapLibre)")
    args = ap.parse_args()
    trip = build_trip(args.event)
    if trip is None:
        print("V knihovně není výlet s aspoň dvěma místy.")
        return 1
    km = sum(journey.haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"]))
             for a, b in zip(trip["stops"], trip["stops"][1:]))
    print(f"Výlet {trip['id']} „{trip['title']}“: {len(trip['stops'])} zastávek, {km:.0f} km")
    if not args.warm_only:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    import paths
    import tile_store
    paths.TILE_STORE_PATH = os.path.join(PROFILE_DIR, "tiles.sqlite")
    w, h = (int(x) for x in args.size.split("x"))
    prof = perf_profile.detect()

    def ready_page(profile=None):
        profile, _, page, view = open_page((w, h), args.html, profile, store=not args.no_store, workers=args.workers)
        for _ in range(100):
            if js(page, "typeof styleReady !== 'undefined' && styleReady === true && getBaseLayerEngine() === 'vector'"):
                break
            wait(100)
        js(page, f"setPerfProfile({json.dumps(prof.for_map())}); reduceMotion = false; 0")
        if args.inject:
            js(page, args.inject + "; 0")
        return profile, page, view

    profile, page, view = ready_page()
    srv = None if args.no_store else tile_store.service()
    print(f"Profil {prof.tier}, podklad {js(page, 'getBaseLayerEngine()')}, okno {w}×{h}, úložiště dlaždic "
          + ("vypnuto" if srv is None else f"{srv.store.bytes / 2**20:.0f} MB"))
    runs = ([] if args.warm_only else ["studená"]) + ["teplá"] + (["restart"] if args.restart else [])
    clip_state = {"worker": None, "ready": [], "built": 0, "t": 0.0}
    if args.clips:
        import queue
        import card_media
        paths.HIGHLIGHT_DIR = os.path.join(PROFILE_DIR, "highlights")   # studená = vyrábět znovu
        js(page, "setPerfProfile({tier: 'high'}); 0")
        clip_state["q"] = queue.Queue()

    def start_clips():
        conn = sqlite3.connect(os.path.join(ROOT, "photos.db"))
        try:
            clips = card_media.plan(conn, trip)
        finally:
            conn.close()
        for c in clips:
            if c["ready"]:
                trip["stops"][c["stop"]]["best"]["clip"] = QUrl.fromLocalFile(c["path"]).toString()
        clip_state["n"] = len(clips)
        clip_state["t"] = time.time()
        w = card_media.ClipWorker(clips, lambda item, p: clip_state["q"].put((item["stop"], p, time.time())))
        clip_state["worker"] = w
        w.start()

    def tick_clips():
        while not clip_state["q"].empty():
            stop, p, at = clip_state["q"].get()
            clip_state["built"] += 1
            clip_state["ready"].append(at - clip_state["t"])
            js(page, f"journeyClip({trip['id']}, {stop}, {json.dumps(QUrl.fromLocalFile(p).toString())}); 0")

    for label in runs:
        if label == "restart":                 # nová stránka = prázdná paměť MapLibre, disk zůstává
            view.close()
            page.deleteLater()
            wait(300)
            profile, page, view = ready_page(profile)
        before = dict(srv.store.stats()) if srv else None
        if args.clips:
            for s in trip["stops"]:
                (s.get("best") or {}).pop("clip", None)
            clip_state.update(built=0, ready=[])
            start_clips()
        play(page, trip, label, args.timeout, tick=tick_clips if args.clips else None)
        if args.clips:
            clip_state["worker"].stop()
            on = js(page, "getClipState()")
            print(f"    klipy: {clip_state['n']} v plánu, vyrobeno za běhu {clip_state['built']} "
                  f"({', '.join(f'{t:.1f}' for t in clip_state['ready'])} s od startu), stav {json.dumps(on)}",
                  flush=True)
        if before is not None:
            st = srv.store.stats()
            print(f"    úložiště: z disku {st['hits'] - before['hits']}, přesměrováno {st['redirects'] - before['redirects']}, "
                  f"staženo {st['misses'] - before['misses']}, "
                  f"{st['bytes'] / 2**20:.0f} MB, předpečeno {srv.baking['done']}/{srv.baking['total']}", flush=True)
        if args.report:
            print("   ", json.dumps(js(page, args.report), ensure_ascii=False))
    view.close()
    page.deleteLater()
    wait(200)
    profile.deleteLater()
    tile_store.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main_())
