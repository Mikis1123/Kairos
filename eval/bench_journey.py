"""Fáze 0 – měření: doba snímku Cesty na skutečné stránce mapy (MapLibre v QtWebEngine).

    python eval/bench_journey.py

Přehraje výlet (místní úseky po trase i dlouhý let) v okně 1600×1000 a vypíše p50 / p95 / p99
a vynechané snímky z měřáků stránky (getFrameStats: smyčka režiséra a vykreslené snímky mapy).
Síť je zablokovaná jako v testech: měří se kamera, trasa a kreslení, ne stahování dlaždic.
Skutečné dlaždice a „díry“ při letu měří eval/bench_journey_live.py (rozhodující měření Cesty).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401
app = QApplication.instance() or QApplication(sys.argv)

import perf_profile  # noqa: E402
from test_patch11_maps import ShownMapPage, wait  # noqa: E402

TRIP = {"id": 1, "title": "Šumava", "location": "", "start": "2026-07-06 09:00", "end": "2026-07-07 18:00",
        "count": 40, "stops": [
            {"lat": 49.200, "lon": 13.400, "start": "2026-07-06 09:00", "city": "A", "count": 12, "photos": []},
            {"lat": 49.212, "lon": 13.430, "start": "2026-07-06 10:30", "city": "B", "count": 4, "photos": []},
            {"lat": 49.230, "lon": 13.520, "start": "2026-07-06 12:00", "city": "C", "count": 8, "photos": []},
            {"lat": 48.200, "lon": 16.370, "start": "2026-07-07 12:00", "city": "D", "count": 16, "photos": []}]}


def main():
    prof = perf_profile.detect()
    m = ShownMapPage((1600, 1000))
    for _ in range(60):
        if m.js("styleReady === true"):
            break
        wait(100)
    m.js(f"setPerfProfile({json.dumps(prof.for_map())}); resetFrameStats(); reduceMotion = false; 0")
    assert m.js(f"playJourney({json.dumps(TRIP)})")
    t0 = time.time()
    while time.time() - t0 < 40 and not m.js("journeyState().ended"):
        wait(250)
    stats = m.js("getFrameStats()")
    state = m.js("getPerfState()")
    print(f"Profil {prof.tier}, displej {prof.refresh_hz:.0f} Hz (rozpočet {prof.frame_budget_ms:.1f} ms), "
          f"poměr pixelů {state['ratio']:.2f} (max {state['max']:.2f}), dokončeno: {m.js('journeyState().ended')}")
    for name in ("journey", "map"):
        s = stats.get(name) or {}
        if s.get("frames"):
            print(f"  {name:8s} p50 {s['p50']:5.1f}  p95 {s['p95']:5.1f}  p99 {s['p99']:5.1f} ms  "
                  f"vynechané {s['dropped']} z {s['frames']}")
    m.js("window.endJourney(true); 0")
    m.close()


if __name__ == "__main__":
    main()
