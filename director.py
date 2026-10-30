"""Kairos Director – sdílený filmový engine, protějšek director.js (Sprint 4).

Stejné jádro plánuje Cestu (map_vault.html přes director.js) i Throwback Reels (reels.py,
reel_player.html, export MP4 v reel_export.py): časová osa úseků, přichycení střihů na doby
hudby a hladký pohyb kamery (Ken Burns) z pevného časového kroku. Python i JS počítají totéž –
přehrávač v aplikaci a exportované MP4 tak mají střihy na stejných snímcích
(test_sprint4_director.py porovnává obě implementace).
"""
import bisect
import math


# ── easing ──
def clamp01(u):
    return max(0.0, min(1.0, u))


def smootherstep(u):
    u = clamp01(u)
    return u * u * u * (u * (6 * u - 15) + 10)


def smoothstep(u):
    u = clamp01(u)
    return u * u * (3 - 2 * u)


def lerp(a, b, t):
    return a + (b - a) * t


def hermite(u, m0, m1):
    u = clamp01(u)
    u2, u3 = u * u, u * u * u
    return (u3 - 2 * u2 + u) * m0 + (3 * u2 - 2 * u3) + (u3 - u2) * m1


# ── časová osa ──
def layout(segs):
    """segs = [{"dur": s, ...}] -> doplní "t0", vrací celkovou délku."""
    t = 0.0
    for g in segs:
        g["t0"] = t
        t += g["dur"]
    return t


def index_at(segs, t):
    lo, hi = 0, len(segs) - 1
    while lo < hi:
        mid = (lo + hi + 1) >> 1
        if segs[mid]["t0"] <= t:
            lo = mid
        else:
            hi = mid - 1
    return lo


def compress(segs, max_s, fixed, floor):
    total = sum(g["dur"] for g in segs)
    if total <= max_s:
        return total
    keep = sum(g["dur"] for g in segs if fixed(g))
    k = (max_s - keep) / max(1e-9, total - keep)
    for g in segs:
        if not fixed(g):
            g["dur"] = max(floor(g), g["dur"] * k)
    return sum(g["dur"] for g in segs)


# ── beat ──
def beat_grid(beat_map, until):
    """beat_map = {period, bar, offset, length} (ambient_music.beat_map) -> {beats, downbeats, period, bar}.
    Smyčka skladby (length) začíná mřížku znovu – stejně jako director.js beatGrid."""
    period = float(beat_map.get("period") or 0)
    bar = max(1, int(round(float(beat_map.get("bar") or 4))))
    off = float(beat_map.get("offset") or 0)
    length = float(beat_map.get("length") or 0)
    beats, downs = [], []
    if not period > 0.05:
        return {"beats": beats, "downbeats": downs, "period": 0.0, "bar": bar}
    loop = 0
    while True:
        base = loop * length if length > 0 else 0.0
        if base > until:
            break
        k = 0
        while True:
            t = base + off + k * period
            if t > until or (length > 0 and t >= base + length):
                break
            beats.append(t)
            if k % bar == 0:
                downs.append(t)
            k += 1
        if not length > 0:
            break
        loop += 1
    return {"beats": beats, "downbeats": downs, "period": period, "bar": bar}


def nearest(values, t):
    if not values:
        return None
    i = bisect.bisect_left(values, t)
    cands = [values[j] for j in (i - 1, i) if 0 <= j < len(values)]
    return min(cands, key=lambda v: (abs(v - t), v))      # remíza = dřívější doba (jako JS)


def snap_cuts(segs, grid, cut, stretch, min_k=0.75, max_k=1.35, down_tol=None):
    """Střihy na doby (viz director.js snapCuts): úsek s cut(g) začne na době – přednostně těžké
    v toleranci down_tol – natažením / zkrácením předchozích pružných úseků v mezích min_k–max_k.
    Vrací počet přichycených střihů."""
    if not grid["beats"]:
        layout(segs)
        return 0
    down_tol = grid["period"] * 1.1 if down_tol is None else down_tol
    snapped, t, pool = 0, 0.0, []
    for g in segs:
        if cut(g) and pool:
            down, beat = nearest(grid["downbeats"], t), nearest(grid["beats"], t)
            target = down if down is not None and abs(down - t) <= down_tol else beat
            flex = sum(s["dur"] for s in pool)
            shift = target - t
            lo, hi = flex * (min_k - 1), flex * (max_k - 1)
            if shift < lo or shift > hi:
                target = beat
                shift = target - t
            if lo <= shift <= hi and flex > 0:
                k = (flex + shift) / flex
                for s in pool:
                    s["dur"] *= k
                t += shift
                snapped += 1
            pool = []
        if stretch(g):
            pool.append(g)
        g["t0"] = t
        t += g["dur"]
    layout(segs)
    return snapped


# ── kamera: Ken Burns ──
def ken_burns(focus, seed, zoom=(1.0, 1.12), aspect_in=1.0, aspect_out=16 / 9):
    """Pohyb výřezu fotky během záběru: {"from": (cx, cy, s), "to": (cx, cy, s)} v souřadnicích
    fotky 0–1 (s = výška výřezu / výška fotky). Přítah míří k tvářím (focus = journey.face_focus),
    bez tváří střídá směr podle seed – stejná fotka = stejný pohyb (náhled = export)."""
    # největší výřez poměru aspect_out, který se do fotky vejde (výška jako podíl výšky fotky)
    s_full = min(1.0, aspect_in / aspect_out)
    s0, s1 = s_full / zoom[0], s_full / zoom[1]
    if seed % 2:                                   # každý druhý záběr odjíždí místo přítahu
        s0, s1 = s1, s0
    if focus:
        fx, fy = float(focus[0]), float(focus[1])
        a, b = (0.5, 0.5), (fx, fy)
        if seed % 2:
            a, b = b, a
    else:
        dirs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        dx, dy = dirs[seed % 4]
        a, b = (0.5 - 0.04 * dx, 0.5 - 0.04 * dy), (0.5 + 0.04 * dx, 0.5 + 0.04 * dy)
    return {"from": _clamp_rect(a[0], a[1], s0, aspect_in, aspect_out),
            "to": _clamp_rect(b[0], b[1], s1, aspect_in, aspect_out)}


def _clamp_rect(cx, cy, s, aspect_in, aspect_out):
    """Střed výřezu tak, aby výřez (výška s, šířka s·aspect_out/aspect_in) nevyjel z fotky."""
    w = s * aspect_out / aspect_in
    cx = min(1 - w / 2, max(w / 2, cx)) if w < 1 else 0.5
    cy = min(1 - s / 2, max(s / 2, cy)) if s < 1 else 0.5
    return (round(cx, 4), round(cy, 4), round(s, 4))


def kb_at(kb, u):
    """Výřez v čase u (0–1 záběru): stálá rychlost uprostřed, měkký rozjezd i dojezd (hermite)."""
    e = hermite(u, 0.6, 0.6)
    a, b = kb["from"], kb["to"]
    return tuple(lerp(a[i], b[i], e) for i in range(3))


def frame_times(total, fps):
    """Časy snímků exportu (s) – deterministicky, ať náhled i MP4 sdílí stejnou mřížku."""
    n = max(1, int(math.ceil(total * fps)))
    return [k / fps for k in range(n)]
