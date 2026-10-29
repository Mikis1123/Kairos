"""Fáze 0/1 – Cesta: stejné měření pro výchozí stav (první commit) a aktuální režisér.

    python eval/bench_journey_compare.py

Obě verze map_vault.html se načtou do stejného okna 1600×1000 (síť zablokovaná), přehrají stejný
výlet se stejnými časy zastávek a do stránky se vloží STEJNÝ měřák: intervaly mezi vykreslenými
snímky mapy (událost 'render') během Cesty. Výchozí verze se bere z gitu (commit Baseline).
"""
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401
app = QApplication.instance() or QApplication(sys.argv)

import test_patch11_maps as tpm  # noqa: E402
from eval.bench_journey import TRIP  # noqa: E402

GIT = r"C:\Program Files\Git\cmd\git.exe"
METER = ("window.__iv = []; window.__last = 0; map.on('render', function () { var n = performance.now();"
         " if (window.__last && n - window.__last < 250) window.__iv.push(n - window.__last); window.__last = n; }); 0")


def run(folder):
    tpm.HERE = folder
    m = tpm.ShownMapPage((1600, 1000))
    m.js("reduceMotion = false; POP_MS = 500; HOLD_MS = 700; DROP_MS = 400; ORBIT_MS = 400; 0")
    m.js(METER)
    assert m.js(f"playJourney({json.dumps(TRIP)})")
    t0 = time.time()
    while time.time() - t0 < 45 and not m.js("journeyState().ended"):
        tpm.wait(250)
    iv = sorted(m.js("window.__iv") or [])
    done = m.js("journeyState().ended")
    m.js("window.endJourney(true); 0")
    m.close()
    if not iv:
        return None
    n = len(iv)
    cadence = statistics.median(iv)
    return {"frames": n, "p50": cadence, "p95": iv[int(0.95 * (n - 1))], "p99": iv[int(0.99 * (n - 1))],
            "late": sum(1 for d in iv if d > 1.5 * 16.7), "done": done, "secs": round(time.time() - t0, 1)}


def main():
    base = tempfile.mkdtemp(prefix="kairos_baseline_map_")
    try:
        html = subprocess.run([GIT, "show", "f247cd7:map_vault.html"], cwd=ROOT, capture_output=True, check=True).stdout
        open(os.path.join(base, "map_vault.html"), "wb").write(html)
        shutil.copytree(os.path.join(ROOT, "vendor"), os.path.join(base, "vendor"))
        results = [("výchozí (flyTo + setTimeout)", run(base)), ("teď (režisér)", run(ROOT))]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print("Snímky mapy během Cesty (okno 1600×1000, síť zablokovaná), pozdní = > 25 ms (vynechaný snímek 60 Hz)")
    for name, r in results:
        if r is None:
            print(f"  {name:30s} bez dat")
            continue
        print(f"  {name:30s} p50 {r['p50']:5.1f}  p95 {r['p95']:5.1f}  p99 {r['p99']:5.1f} ms  "
              f"pozdní {r['late']} z {r['frames']} ({100 * r['late'] / r['frames']:.1f} %)  "
              f"dokončeno {r['done']} za {r['secs']} s")


if __name__ == "__main__":
    main()
