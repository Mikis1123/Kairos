"""Sprint 4.3: výkon Atlasu – sestavení dat a snímky mapy (syntetická světová knihovna).

    python eval/bench_atlas.py [--trip-photos 400] [--home 20000] [--shot cesta.png]

Měří: atlas.build (ms, velikost JSON), přepnutí do Atlasu, snímky během náběhu oblouků
a během otáčení zeměkoule (p50 / p95 / vynechané), přepnutí roku. Síť blokovaná (podstrčený styl).
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip-photos", type=int, default=400)
    ap.add_argument("--home", type=int, default=20000)
    ap.add_argument("--shot")
    args = ap.parse_args()
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    import atlas
    from test_patch11_maps import FIXTURE_STYLE, ShownMapPage, wait
    from test_sprint4_atlas import make_world_db
    d = tempfile.mkdtemp(prefix="bench_atlas_")
    conn = make_world_db(os.path.join(d, "w.db"), photos_per_trip=args.trip_photos, home_photos=args.home)
    n = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
    t = time.perf_counter()
    data = atlas.build(conn)
    build_ms = (time.perf_counter() - t) * 1000
    blob = json.dumps(data)
    print(f"knihovna {n} fotek, {len(data['trips'])} událostí, {len(data['arcs']['features'])} oblouků, "
          f"{len(data['hex']['features'])} šestiúhelníků: build {build_ms:.0f} ms, JSON {len(blob) / 1e6:.2f} MB")
    m = ShownMapPage((1600, 900))
    m.wait_style()
    m.js(f"setVectorStyle({json.dumps(FIXTURE_STYLE)})")
    m.wait_style()
    m.js("resetFrameStats(); 0")
    t = time.perf_counter()
    m.js(f"window.setAtlas({blob}); window.atlasMode(true); 0", timeout=20000)
    print(f"přepnutí do Atlasu (JS, vč. přenosu dat): {(time.perf_counter() - t) * 1000:.0f} ms")
    wait(2600)
    s = m.js("getFrameStats().map")
    print(f"náběh oblouků + přelet: p50 {s['p50']:.1f} ms, p95 {s['p95']:.1f} ms, vynechané {s['dropped']}/{s['frames']}")
    if args.shot:
        m.view.grab().save(args.shot)
    m.js("resetFrameStats(); map.jumpTo({center: [14, 40], zoom: 1.7}); 0")
    wait(300)
    m.js("map.easeTo({center: [194, 40], zoom: 1.7, duration: 3000, easing: function (t) { return t; }}); 0")
    wait(3300)
    s = m.js("getFrameStats().map")
    print(f"otáčení zeměkoule s Atlasem: p50 {s['p50']:.1f} ms, p95 {s['p95']:.1f} ms, vynechané {s['dropped']}/{s['frames']}")
    ys = [y["year"] for y in data["years"]]
    t = time.perf_counter()
    for y in ys:
        m.js(f"window.atlasYear({y}); 0")
    print(f"přepnutí roku: {(time.perf_counter() - t) * 1000 / len(ys):.1f} ms (průměr {len(ys)} let)")
    print("chyby mapy:", [e for e in m.js("getMapErrors()") if "a-" in e or "atlas" in e][:3])
    m.close()
    conn.close()


if __name__ == "__main__":
    main()
