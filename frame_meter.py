"""Měření doby snímku (Fáze 0): p50 / p95 / p99 a vynechané snímky.

Pojmenované měřáky (mřížka, prohlížeč, video, mapa, Cesta) zaznamenávají okamžiky vykreslení;
statistika se počítá z intervalů mezi nimi, ale jen v „aktivních“ úsecích (pohyb, přehrávání):
interval delší než IDLE_GAP_S znamená klid (nic se nehýbalo), ne vynechaný snímek.

  * Ctrl+Shift+F v hlavním okně: překryv se statistikou všech měřáků (FrameStatsOverlay).
  * KAIROS_FRAMESTATS=1: při ukončení se statistika zapíše do perf_logs/framestats-<čas>.csv.

Rozpočet snímku = 1000 / obnovovací frekvence (perf_profile); vynechaný = interval > 1,5 × rozpočet.
Mapa (WebGL v QtWebEngine) měří sama v map_vault.html (window.getFrameStats) a stránka hlásí
výsledky přes most do meteru „map“ / „journey“ (record_external).
"""
import csv
import os
import time
from collections import deque

IDLE_GAP_S = 0.25          # delší mezera = klid (nic se nekreslilo), ne vynechaný snímek
WINDOW = 1200              # posledních N intervalů na měřák
DROP_FACTOR = 1.5


class FrameMeter:
    def __init__(self, name, budget_ms):
        self.name = name
        self.budget_ms = float(budget_ms)
        self._last = None
        self.intervals = deque(maxlen=WINDOW)
        self.external = None              # hotová statistika z webové stránky (mapa)

    def tick(self, now=None):
        now = time.perf_counter() if now is None else now
        last, self._last = self._last, now
        if last is not None:
            dt = now - last
            if 0 < dt <= IDLE_GAP_S:
                self.intervals.append(dt * 1000.0)

    def reset(self):
        self._last = None
        self.intervals.clear()
        self.external = None

    def stats(self):
        if self.external is not None:
            return dict(self.external)
        data = sorted(self.intervals)
        n = len(data)
        if not n:
            return {"name": self.name, "frames": 0}
        pct = lambda p: data[min(n - 1, int(round(p / 100.0 * (n - 1))))]
        dropped = sum(1 for d in data if d > DROP_FACTOR * self.budget_ms)
        return {"name": self.name, "frames": n, "p50": pct(50), "p95": pct(95), "p99": pct(99),
                "dropped": dropped, "drop_pct": 100.0 * dropped / n, "budget": self.budget_ms}


_meters = {}
_budget_ms = 1000.0 / 60.0


def set_refresh(hz):
    global _budget_ms
    _budget_ms = 1000.0 / max(30.0, float(hz or 60.0))
    for m in _meters.values():
        m.budget_ms = _budget_ms


def meter(name):
    m = _meters.get(name)
    if m is None:
        m = _meters[name] = FrameMeter(name, _budget_ms)
    return m


def tick(name):
    meter(name).tick()


def record_external(name, stats):
    """Statistika spočtená jinde (stránka mapy): {'frames','p50','p95','p99','dropped'}."""
    m = meter(name)
    s = {"name": name, "budget": m.budget_ms}
    s.update({k: stats[k] for k in ("frames", "p50", "p95", "p99", "dropped") if k in stats})
    if s.get("frames"):
        s["drop_pct"] = 100.0 * s.get("dropped", 0) / s["frames"]
    m.external = s


def all_stats():
    return [m.stats() for m in _meters.values()]


def format_stats(s):
    if not s.get("frames"):
        return f"{s['name']:8s}  –"
    return (f"{s['name']:8s}  p50 {s['p50']:5.1f}  p95 {s['p95']:5.1f}  p99 {s['p99']:5.1f} ms"
            f"  drop {s.get('dropped', 0)} ({s.get('drop_pct', 0):.1f} %)  n={s['frames']}")


def enabled_logging():
    return os.environ.get("KAIROS_FRAMESTATS") == "1"


def write_log(directory):
    """Zapíše statistiku všech měřáků do CSV (vrací cestu, nebo None když není co psát)."""
    rows = [s for s in all_stats() if s.get("frames")]
    if not rows:
        return None
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, time.strftime("framestats-%Y%m%d-%H%M%S.csv"))
    keys = ["name", "frames", "p50", "p95", "p99", "dropped", "drop_pct", "budget"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for s in rows:
            w.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in s.items()})
    return path
