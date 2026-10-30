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
window.__b = {ren: [], moving: 0, holes: 0};
(function () {
  var orig = map._render.bind(map);
  map._render = function (t) {
    var a = performance.now(), r = orig(t);
    __b.ren.push(performance.now() - a);
    if (director.directing && journey.active && !journey.paused) {
      __b.moving++;
      try { if (!map.areTilesLoaded()) __b.holes++; } catch (e) {}
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


def open_page(size, html="map_vault.html"):
    from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings
    profile = QWebEngineProfile("kairos_bench")
    profile.setPersistentStoragePath(PROFILE_DIR)
    profile.setCachePath(os.path.join(PROFILE_DIR, "cache"))
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    interceptor = main.WebUrlInterceptor()
    profile.setUrlRequestInterceptor(interceptor)
    page = QWebEnginePage(profile)
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


def play(page, trip, label, timeout_s):
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
        wait(250)
    wall = time.time() - t0
    stats = js(page, "getFrameStats()")["journey"]
    probe = js(page, "__b")
    arrivals = js(page, "window.__arrivals || []") or []
    js(page, "window.endJourney(true); 0")
    holes = probe["holes"] / max(1, probe["moving"])
    ren = probe["ren"]
    line = (f"{label:6s} {wall:5.1f} s  snímky {stats.get('frames', 0):5d}  p50 {stats.get('p50', 0):5.1f}  "
            f"p95 {stats.get('p95', 0):5.1f}  p99 {stats.get('p99', 0):5.1f}  vynechané {stats.get('dropped', 0):4d} "
            f"({100 * stats.get('dropped', 0) / max(1, stats.get('frames', 1)):4.1f} %)  "
            f"render p50 {pct(ren, 50):5.1f} p95 {pct(ren, 95):5.1f}  díry {100 * holes:4.1f} %")
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
    w, h = (int(x) for x in args.size.split("x"))
    profile, interceptor, page, view = open_page((w, h), args.html)
    for _ in range(100):
        if js(page, "typeof styleReady !== 'undefined' && styleReady === true && getBaseLayerEngine() === 'vector'"):
            break
        wait(100)
    prof = perf_profile.detect()
    js(page, f"setPerfProfile({json.dumps(prof.for_map())}); reduceMotion = false; 0")
    if args.inject:
        js(page, args.inject + "; 0")
    print(f"Profil {prof.tier}, podklad {js(page, 'getBaseLayerEngine()')}, okno {w}×{h}")
    runs = ([] if args.warm_only else ["studená"]) + ["teplá"]
    for label in runs:
        play(page, trip, label, args.timeout)
        if args.report:
            print("   ", json.dumps(js(page, args.report), ensure_ascii=False))
    view.close()
    page.deleteLater()
    wait(200)
    profile.deleteLater()
    return 0


if __name__ == "__main__":
    sys.exit(main_())
