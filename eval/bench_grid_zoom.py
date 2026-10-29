"""Fáze 0 – měření: doba snímku zoomu mřížky, dřívější cesta proti zmrazenému rozvržení.

    python eval/bench_grid_zoom.py

Stejná mřížka (400 dlaždic s náhledy, okno 1600×1000, skutečné DPR displeje) projde stejný
přechod 7 -> 6 sloupců ve 40 krocích:
  * dřív: každý krok = setGridSize + layoutChanged + doItemsLayout + překreslení (dlaždice
    v nové velikosti se rastrují znovu) – co dělal _apply_zoom_step za snímek,
  * teď: každý krok = posun průběhu e zmrazeného rozvržení + překreslení (kopie hotových dlaždic).
Vypíše p50 / p95 / max doby kroku v ms a rozpočet snímku displeje.
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("KAIROS_NO_SPLASH", "1")

from PyQt6.QtCore import QPointF, QSize, Qt
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401 – před QApplication (jako main.py)
app = QApplication.instance() or QApplication(sys.argv)
import main  # noqa: E402
import perf_profile  # noqa: E402

N, STEPS = 400, 40


def make_grid():
    grid = main.AppleGridView(None)
    recs = [(i, f"IMG_{i}.jpg", f"/nic/{i}.jpg", "live_photo" if i % 5 == 0 else "photo", None, None,
             "2026-07-07", int(i % 11 == 0)) for i in range(1, N + 1)]
    grid.photo_model.set_records(recs)
    for i in range(1, N + 1):                       # náhledy 360 px jako ze skutečné mezipaměti
        img = QImage(360, 270, QImage.Format.Format_RGB32)
        img.fill(QColor.fromHsv((i * 37) % 360, 120, 200))
        grid.photo_model.ram_cache.put(i, QPixmap.fromImage(img))
    grid.resize(1600, 1000)
    grid.show()
    grid.set_columns(7, animate=False)
    for _ in range(5):
        app.processEvents()
    grid.viewport().repaint()                       # klidové dlaždice v cache (jako po posouvání)
    return grid


def stats(samples):
    s = sorted(samples)
    return {"p50": statistics.median(s), "p95": s[int(0.95 * (len(s) - 1))], "max": s[-1]}


def bench_old(grid):
    start, end = grid._cell_for_columns(7), grid._cell_for_columns(6)
    out = []
    for k in range(1, STEPS + 1):
        cell = round(start + (end - start) * k / STEPS)
        t0 = time.perf_counter()
        grid._apply_zoom_step(cell)                 # setGridSize + layoutChanged (dřívější krok)
        grid.doItemsLayout()                        # kotva: synchronní rozvržení
        grid.viewport().repaint()
        out.append((time.perf_counter() - t0) * 1000)
    return out


def bench_new(grid):
    grid._cols = 6
    start = grid._current_grid()
    target = grid._target_grid(6, QPointF(800, 500), start)
    grid._begin_freeze(main._BlendLayout(start, target, 0.0))
    out = []
    for k in range(1, STEPS + 1):
        t0 = time.perf_counter()
        grid._on_zoom_progress(k / STEPS)
        grid.viewport().repaint()
        out.append((time.perf_counter() - t0) * 1000)
    grid._commit_zoom(target)
    return out


def main_bench():
    prof = perf_profile.detect()
    old_grid = make_grid()
    dpr = old_grid.viewport().devicePixelRatioF()
    old = stats(bench_old(old_grid))
    old_grid.close()
    new_grid = make_grid()
    new = stats(bench_new(new_grid))
    new_grid.close()
    print(f"Displej {prof.refresh_hz:.0f} Hz, rozpočet snímku {prof.frame_budget_ms:.1f} ms, DPR {dpr:.2f}, "
          f"{N} dlaždic, okno 1600×1000")
    for name, s in (("dřív (rozvržení + rastr za krok)", old), ("teď (zmrazené rozvržení)", new)):
        print(f"  {name:34s} p50 {s['p50']:6.2f}  p95 {s['p95']:6.2f}  max {s['max']:6.2f} ms")


if __name__ == "__main__":
    main_bench()
