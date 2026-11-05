"""Real-input map latency bench: the actual app (MainWindow, real library, WebView2 host) driven
by REAL OS mouse input (SendInput from a separate process, eval/input_injector.py) – unlike
bench_map_live.py, whose gestures are synthetic DOM events inside the page and therefore skip
the whole OS -> browser -> renderer input path.

    python eval/bench_map_input.py                # caps: app's own, then 60 (Battery Saver path)
    python eval/bench_map_input.py --caps "app@1,app@4,app@10"     # renderer CPU throttled 4x / 10x
    python eval/bench_map_input.py --caps app@4 --scen "goto:13.45:49.1:12:50;pan;orbit;wheelin"
    python eval/bench_map_input.py --caps 60@1 --scen "pan;hz=60;pan;hz=0"      # unplugged panel

--caps   comma list of <cap>[@<cpu throttle>]: cap = "app" (the app's own) or fps for setFrameCap;
         the throttle is Chromium's Emulation.setCPUThrottlingRate on the map renderer (a proxy for
         battery / thermal throttling: this laptop's firmware ignores Windows' max processor state).
--scen   gestures pan / wheelin / wheelout / orbit (real SendInput), plus goto:lng:lat:zoom:pitch,
         hz=<rate> (panel refresh, 0 = restore), hoverpin (cursor onto a pin: hover + card),
         js=<code> (use ";" as the separator when code contains commas).
--js     file evaluated and printed after every gesture; --profile <cap label>[+goto] = CDP CPU
         profile of that run (scratch kairos_map_profile.json). KAIROS_BENCH_RAW=1 keeps timelines.

Moves the real cursor for ~1 minute: don't touch the mouse while it runs.
Findings 2026-11-04: see the "Input latency" / "Frame pacing" comments in map_vault.html.

Per gesture and injected event:
  recv   = OS inject -> Chromium's event timestamp (coalesced event timeStamp)
  disp   = OS inject -> JS handler ran (pointer events are rAF-aligned in Chromium)
  frame  = OS inject -> start of the first MapLibre render after the handler + its cost
           (= the frame that shows the movement is submitted; display adds ~1 vsync)
plus MapLibre render intervals / costs during the gesture and GUI-thread stalls.
"""
import json, os, sys, time, subprocess, argparse, statistics, ctypes
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("KAIROS_NO_SPLASH", "1")
ap = argparse.ArgumentParser()
ap.add_argument("--caps", default="app,60")
ap.add_argument("--hz", type=int, default=250, help="input reports per second while dragging")
ap.add_argument("--out", default=os.path.join(HERE, "bench_map_input.json"))
ap.add_argument("--profile", default="", help="cap label to CPU-profile (CDP Profiler), e.g. app@4")
ap.add_argument("--scen", default="pan,wheelin,pan,orbit,wheelout,pan")
ap.add_argument("--js", default="", help="JS file evaluated (and printed) after each gesture")
ap.add_argument("--settle", type=int, default=4000)
args = ap.parse_args()

import main as M  # noqa
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer, QElapsedTimer

M.web_engine.prepare_app()
app = QApplication(sys.argv)
SCRATCH = os.environ.get("TEMP", HERE)
results = {"runs": [], "gui_late": []}


def cpu_check():
    import hashlib
    buf = os.urandom(32 * 1024 * 1024)
    t = time.perf_counter()
    hashlib.sha256(buf).digest()
    return round(32 / (time.perf_counter() - t))


results["cpu_sha256_mbps"] = cpu_check()
print("CPU sha256 MB/s", results["cpu_sha256_mbps"])

hb = QElapsedTimer(); hb.start(); last = [0]


def beat():
    now = hb.elapsed()
    if last[0] and now - last[0] > 30:
        results["gui_late"].append([round(time.time(), 3), now - last[0]])
    last[0] = now


beatT = QTimer(); beatT.setInterval(4); beatT.timeout.connect(beat)

PROBE = r"""
(function () { try {
  if (window.__ip) return 'already';
  var vraf = window.__vraf || window.requestAnimationFrame.bind(window);
  var O = performance.timeOrigin;
  var P = window.__ip = { on: false, ev: [], co: [], rend: [], vs: [], loaf: [] };
  try { new PerformanceObserver(function (l) { l.getEntries().forEach(function (e) { if (!P.on) return;
      P.loaf.push([O + e.startTime, e.duration, e.renderStart ? O + e.renderStart : 0, (e.scripts || []).map(function (s) {
        return [Math.round(s.duration), s.invoker || '', (s.sourceFunctionName || '') + '@' + (s.sourceURL || '').split('/').pop() + ':' + s.sourceCharPosition, s.forcedStyleAndLayoutDuration]; })]); }); })
      .observe({ type: 'long-animation-frame', buffered: false }); } catch (e) {}
  function rec(e) {
    if (!P.on) return;
    var now = performance.now();
    var list = e.getCoalescedEvents ? e.getCoalescedEvents() : [];
    if (!list.length) list = [e];
    for (var i = 0; i < list.length; i++) P.co.push([e.type, list[i].clientX, list[i].clientY, O + list[i].timeStamp, O + now]);
    var m = window.map;
    P.ev.push([e.type, e.clientX, e.clientY, O + e.timeStamp, O + now, e.buttons,
               m && m.dragPan.isActive(), m && m.isMoving(), m && +m.getCenter().lng.toFixed(3)]);
  }
  ['pointermove', 'pointerdown', 'pointerup', 'wheel'].forEach(function (t) {
    window.addEventListener(t, rec, { capture: true, passive: true });
  });
  function v(t) { if (P.on) P.vs.push(O + t); vraf(v); }
  vraf(v);
  function hook() {
    var m = window.map;
    if (!m) return setTimeout(hook, 200);
    var orig = m._render;
    m._render = function () {
      var a = performance.now(), r = orig.apply(this, arguments), c = performance.now() - a;
      if (P.on) { var ct = m.getCenter(); P.rend.push([O + a, c, ct.lng, ct.lat, m.getZoom(), m.getBearing(), m.getPitch(),
                                                       m.getPixelRatio(), m.areTilesLoaded() ? 1 : 0]); }
      return r;
    };
    m.on('moveend', function () { if (P.on) P.me.push(O + performance.now()); });
  }
  hook();
  window.__ipStart = function () { P.ev = []; P.co = []; P.rend = []; P.vs = []; P.loaf = []; P.me = []; P.on = true; return 1; };
  window.__ipStop = function () { P.on = false; return JSON.stringify({ loaf: P.loaf, ev: P.ev, co: P.co, rend: P.rend, vs: P.vs, me: P.me,
      momentum: window.getMomentumState ? window.getMomentumState() : null, thin: window.getThinState ? window.getThinState() : null,
      predict: window.getPredictState ? (function (s) { delete s.lastPoses; return s; })(window.getPredictState()) : null,
      dpr: devicePixelRatio, cap: window.getFrameCap ? window.getFrameCap() : null }); };
  return 'installed';
} catch (e) { return 'ERR ' + e; } })()
"""


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
        if v or (time.perf_counter() - t0) * 1000 > timeout:
            then()
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
    wait_js("window.map && window.map.loaded() && window.map.areTilesLoaded()",
            lambda: js(PROBE, lambda v: (print("probe", v), later(args.settle, next_cap))), 40000)


def host_rect():
    w = win()
    mv = w.map_view
    dpr = mv.devicePixelRatioF()
    g = mv.mapToGlobal(mv.rect().topLeft())
    # Qt global coords are logical; the injector is per-monitor DPI aware (physical px)
    scr = mv.screen()
    sg = scr.geometry()
    ox = round(sg.x() + (g.x() - sg.x()) * dpr)
    oy = round(sg.y() + (g.y() - sg.y()) * dpr)
    return ox, oy, round(mv.width() * dpr), round(mv.height() * dpr), dpr


def gesture_plan(kind, hz):
    ox, oy, w, h, dpr = host_rect()
    cx, cy = ox + w // 2, oy + h // 2
    if kind == "pan":
        steps = [["move", cx - 300, cy], ["sleep", 120], ["down"], ["drag", 600, 120, 1600, hz],
                 ["drag", -500, -200, 1400, hz], ["up"]]
    elif kind == "wheelin":
        steps = [["move", cx + 80, cy + 40], ["sleep", 120]] + sum([[["wheel", 120], ["sleep", 90]] for _ in range(10)], [])
    elif kind == "wheelout":
        steps = [["move", cx, cy], ["sleep", 120]] + sum([[["wheel", -120], ["sleep", 90]] for _ in range(6)], [])
    elif kind == "flick":             # short fast drag, released while still moving: the coast
        steps = [["move", cx - 300, cy], ["sleep", 120], ["down"], ["drag", 500, 0, 220, hz], ["up"]]
    elif kind == "orbit":
        steps = [["move", cx - 200, cy], ["sleep", 120], ["rdown"], ["drag", 400, -80, 1500, hz], ["rup"]]
    else:
        raise ValueError(kind)
    return {"x0": cx, "y0": cy, "steps": steps}, (ox, oy, dpr)


SCEN = args.scen.split(";") if ";" in args.scen else args.scen.split(",")
EXTRA_JS = open(args.js, encoding="utf-8").read() if args.js else ""
caps = args.caps.split(",")


def foreground():
    w = win()
    w.raise_(); w.activateWindow()
    try:
        ctypes.windll.user32.SetForegroundWindow(int(w.winId()))
    except Exception:
        pass


def next_cap():
    if not caps:
        return finish()
    cap = caps.pop(0)
    rate = 1
    if "@" in cap:                  # "60@4": frame cap 60, renderer CPU throttled 4x (DevTools emulation)
        cap, rate = cap.split("@")[0], float(cap.split("@")[1])
    core = win().map_view._core
    core.CallDevToolsProtocolMethodAsync("Emulation.setCPUThrottlingRate", json.dumps({"rate": rate}))
    js("window.setFrameCap(0)")
    cap_label = f"{cap}@{rate:g}"
    if cap != "app":
        js(f"window.setFrameCap({int(cap)})")
    js("JSON.stringify(window.getFrameCap())", lambda v: print("cap", cap, v))
    foreground()
    if args.profile == cap_label:
        core.CallDevToolsProtocolMethodAsync("Profiler.enable", "{}")
        core.CallDevToolsProtocolMethodAsync("Profiler.setSamplingInterval", json.dumps({"interval": 200}))
        core.CallDevToolsProtocolMethodAsync("Profiler.start", "{}")
        results["profiling"] = cap_label
    later(1500, lambda: run(cap_label, list(SCEN)))


def run(cap, todo):
    if not todo:
        if results.pop("profiling", None) == cap:
            mv = win().map_view
            t = mv._core.CallDevToolsProtocolMethodAsync("Profiler.stop", "{}")
            def saved(task):
                open(os.path.join(SCRATCH, "kairos_map_profile.json"), "w").write(str(task.Result))
                print("profile saved")
                next_cap()
            mv._watch(t, saved)
            return
        return next_cap()
    kind = todo.pop(0)
    if kind.startswith("js="):       # js=<code>: run in the page, print the result, settle, go on
        code = kind[3:]
        return js(code, lambda v: (print("JSRES", code[:60], v), later(2500, lambda: wait_js(
            "window.map.loaded() && window.map.areTilesLoaded()", lambda: later(800, lambda: run(cap, todo)), 15000))))
    if kind == "hoverpin":           # real cursor onto the first on-screen pin: hover state + card
        js("""(function(){ var r = map.getCanvas().getBoundingClientRect(), w = r.width, h = r.height;
                 var best = null; (window.__clustersForBench ? __clustersForBench() : []).forEach(function (c) {
                   var p = map.project([c.lon, c.lat]); if (p.x > 40 && p.y > 40 && p.x < w - 40 && p.y < h - 40 && !best) best = p; });
                 return best ? JSON.stringify({x: best.x, y: best.y}) : null; })()""", lambda v: hover_to(cap, todo, v))
        return
    if kind.startswith("hz="):       # hz=<rate>: switch the panel (dynamic, never saved), 0 = restore
        import atexit
        sys.path.insert(0, HERE)
        import bench_map_webview2 as BW
        hz = int(kind[3:])
        ok = BW.restore_display() if hz == 0 else BW.set_display_hz(hz)
        atexit.register(BW.restore_display)
        print("PANEL", hz, "->", BW.display_hz(), ok)
        return later(3000, lambda: run(cap, todo))
    if kind.startswith("goto:"):     # goto:lng:lat:zoom:pitch – jump, then wait for the tiles
        lng, lat, z, pt = (float(x) for x in kind.split(":")[1:])
        js(f"window.map.jumpTo({{center:[{lng},{lat}],zoom:{z},pitch:{pt},bearing:0}}); 1")
        def arrived():
            if args.profile == cap + "+goto":
                core = win().map_view._core
                core.CallDevToolsProtocolMethodAsync("Profiler.enable", "{}")
                core.CallDevToolsProtocolMethodAsync("Profiler.setSamplingInterval", json.dumps({"interval": 200}))
                core.CallDevToolsProtocolMethodAsync("Profiler.start", "{}")
                results["profiling"] = cap
            later(1500, lambda: run(cap, todo))
        return later(1500, lambda: wait_js("window.map.loaded() && window.map.areTilesLoaded()", arrived, 20000))
    plan, (ox, oy, dpr) = gesture_plan(kind, args.hz)
    pf = os.path.join(SCRATCH, "kairos_inj_plan.json")
    lf = os.path.join(SCRATCH, "kairos_inj_log.json")
    json.dump(plan, open(pf, "w"))
    late0 = len(results["gui_late"])

    def launch(_=None):
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "input_injector.py"), pf, lf])

        def poll():
            if proc.poll() is None:
                return later(30, poll)
            later(2600 if kind == "flick" else 900, lambda: js("window.__ipStop()", lambda v: analyse(cap, kind, v, lf, ox, oy, dpr, late0, todo)))
        later(30, poll)
    js("window.__ipStart()", launch)


def hover_to(cap, todo, v):
    if not v:
        print("HOVER no pin on screen")
        return later(300, lambda: run(cap, todo))
    pt = json.loads(v)
    ox, oy, w, h, dpr = host_rect()
    x, y = round(ox + pt["x"] * dpr), round(oy + pt["y"] * dpr)
    plan = {"x0": x - 200, "y0": y, "steps": [["sleep", 100], ["move", x - 4, y], ["sleep", 60], ["move", x, y], ["sleep", 400]]}
    pf = os.path.join(SCRATCH, "kairos_inj_plan.json")
    json.dump(plan, open(pf, "w"))
    subprocess.run([sys.executable, os.path.join(HERE, "input_injector.py"), pf, os.path.join(SCRATCH, "kairos_inj_log.json")])
    later(300, lambda: js("JSON.stringify({cursor: map.getCanvas().style.cursor, card: document.getElementById('card').classList.contains('show')})",
                          lambda r: (print("HOVER", r), results.setdefault("hover", []).append(r), later(300, lambda: run(cap, todo)))))


def pct(v, p):
    if not v:
        return None
    s = sorted(v)
    return round(s[min(len(s) - 1, int(len(s) * p))], 1)


def analyse(cap, kind, v, lf, ox, oy, dpr, late0, todo):
    d = json.loads(v)
    inj = json.load(open(lf))
    co = d["co"]; ev = d["ev"]; rend = d["rend"]
    want = {"move": "pointermove", "down": "pointerdown", "rdown": "pointerdown", "up": "pointerup",
            "rup": "pointerup", "wheel": "wheel"}
    recv, disp, frame, unmatched = [], [], [], 0
    used = set()
    j0 = 0
    armed = False                   # hover moves (no button) don't move the map: not a latency sample
    for k, x, y, t in inj:
        typ = want[k]
        if k in ("down", "rdown", "wheel"):
            armed = True
        elif k in ("up", "rup"):
            armed = False
            continue
        elif not armed:
            continue
        ex, ey = (x - ox) / dpr, (y - oy) / dpr
        hit = None
        for j in range(j0, len(co)):
            c = co[j]
            if j in used or c[0] != typ:
                continue
            if c[3] < (t - 0.002) * 1000 - 5:
                continue
            if typ == "wheel" or (abs(c[1] - ex) <= 1.01 and abs(c[2] - ey) <= 1.01):
                hit = j
                break
            if c[4] > t * 1000 + 400:
                break
        if hit is None:
            unmatched += 1
            continue
        used.add(hit)
        c = co[hit]
        tm = t * 1000
        recv.append(c[3] - tm)
        disp.append(c[4] - tm)
        nxt = next((r for r in rend if r[0] >= c[4]), None)
        if nxt:
            frame.append(nxt[0] + nxt[1] - tm)
    t_first = inj[0][3] * 1000 if inj else 0
    t_last = inj[-1][3] * 1000 if inj else 0
    rs = [r for r in rend if t_first <= r[0] <= t_last + 300]
    iv = [b[0] - a[0] for a, b in zip(rs, rs[1:])]
    cost = [r[1] for r in rs]
    vs = [x for x in d["vs"] if t_first <= x <= t_last + 300]
    vsi = [b - a for a, b in zip(vs, vs[1:])]
    hz = 1000 / statistics.median(vsi) if vsi else 0
    row = {"cap": cap, "kind": kind, "events": len(inj), "unmatched": unmatched,
           "recv_p50": pct(recv, .5), "recv_p95": pct(recv, .95), "recv_max": pct(recv, 1),
           "disp_p50": pct(disp, .5), "disp_p95": pct(disp, .95), "disp_max": pct(disp, 1),
           "frame_p50": pct(frame, .5), "frame_p95": pct(frame, .95), "frame_max": pct(frame, 1),
           "vsync_hz": round(hz, 1), "renders": len(rs), "render_iv_p50": pct(iv, .5), "render_iv_p95": pct(iv, .95),
           "render_iv_max": pct(iv, 1), "gaps_over_2vs": sum(1 for x in iv if hz and x > 2.5 * 1000 / hz),
           "cost_p50": pct(cost, .5), "cost_p95": pct(cost, .95), "cost_max": pct(cost, 1),
           "zoom": round(rs[-1][4], 2) if rs else None, "pitch": round(rs[-1][6]) if rs else None,
           "gui_late": results["gui_late"][late0:], "frame_cap": d.get("cap")}
    row["raw"] = {"inj0": inj[:3], "co0": co[:6], "rend0": [r[:2] for r in rend if r[0] >= t_first - 50][:8],
                  "loaf": [l for l in d.get("loaf", []) if l[1] > 25]}
    if os.environ.get("KAIROS_BENCH_RAW"):
        row["raw"]["inj"] = [[k, round(t * 1000 - t_first, 1)] for k, x, y, t in inj]
        row["raw"]["rend"] = [[round(r[0] - t_first, 1), round(r[1], 1), round(r[4], 3), round(r[2], 7), round(r[3], 7)] for r in rend]
        row["raw"]["co"] = [[c[0], round(c[3] - t_first, 1), round(c[4] - t_first, 1)] for c in co]
        row["raw"]["ev"] = [[e[0], round(e[1]), round(e[4] - t_first, 1)] + e[5:] for e in ev]
    # LOD recovery: last moveend -> first render back at the native pixel ratio
    me = d.get("me") or []
    full = d.get("dpr", dpr)
    if me and rend and len(rend[0]) > 7:
        t_end = me[-1]
        shed = any(r[7] < full - 0.01 for r in rend)
        back = next((r[0] for r in rend if r[0] >= t_end and r[7] >= full - 0.01), None)
        row["ratio_shed"] = shed
        row["ratio_restore_ms"] = round(back - t_end, 1) if (shed and back) else (0 if not shed else None)
        row["unloaded_frame_share"] = round(sum(1 for r in rs if not r[8]) / max(1, len(rs)), 3)
    if kind == "flick":
        row.update(coast_metrics(inj, rend, full))
    row["momentum"] = d.get("momentum"); row["thin"] = d.get("thin"); row["predict"] = d.get("predict")
    results["runs"].append(row)
    print(json.dumps({k: row[k] for k in row if k != "raw"}, ensure_ascii=False))
    if EXTRA_JS:
        js(EXTRA_JS, lambda v: (print("JS", v), later(600, lambda: run(cap, todo))))
    else:
        later(600, lambda: run(cap, todo))


def coast_metrics(inj, rend, dpr):
    """Camera speed (CSS px/s) frame by frame around the release of a flick."""
    import math
    t_up = inj[-1][3] * 1000
    def mx(lng): return (lng + 180) / 360
    def my(lat):
        r = math.radians(max(-85.05, min(85.05, lat)))
        return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2
    sp = []                          # (t, px/s, interval)
    for a, b in zip(rend, rend[1:]):
        dt = b[0] - a[0]
        if dt <= 0:
            continue
        world = 512 * 2 ** b[4]
        d = math.hypot((mx(b[2]) - mx(a[2])) * world, (my(b[3]) - my(a[3])) * world)
        sp.append((b[0], d / dt * 1000, dt))
    drag = [v for t, v, _ in sp if t_up - 60 <= t <= t_up]
    coast = [(t, v, dt) for t, v, dt in sp if t > t_up and v > 1]
    if not drag or not coast:
        return {"coast": None}
    v_rel = statistics.median(drag)
    # the frame spanning the release is reported on its own (release_frame_iv): a dropped vsync there
    # halves its apparent speed; continuity = the coast's own opening speed vs the hand's
    rel_iv = coast[0][2]
    first = statistics.median([v for _, v, _ in coast[1:4]]) if len(coast) > 3 else coast[0][1]
    ivs = [dt for _, _, dt in coast]
    # stutter: a frame whose speed jumps UP by > 8 % or drops by > 2.5x what the neighbours imply
    bumps = sum(1 for (_, a, _), (_, b, _) in zip(coast, coast[1:]) if b > a * 1.08)
    cliffs = sum(1 for (_, a, _), (_, b, _) in zip(coast, coast[1:]) if a > 30 and b < a / 2.5)
    return {"release_speed": round(v_rel), "coast_first_speed": round(first),
            "release_continuity": round(first / v_rel, 2) if v_rel else None,
            "release_frame_iv": round(rel_iv, 1),
            "coast_ms": round(coast[-1][0] - t_up), "coast_frames": len(coast),
            "coast_iv_p50": pct(ivs, .5), "coast_iv_p95": pct(ivs, .95), "coast_iv_max": pct(ivs, 1),
            "coast_speed_bumps": bumps, "coast_speed_cliffs": cliffs}


def finish():
    json.dump(results, open(args.out, "w"), indent=1, ensure_ascii=False)
    print("saved", args.out)
    win().close()


beatT.start()
QTimer.singleShot(0, start)
M.main(app)
