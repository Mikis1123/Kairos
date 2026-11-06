"""iOS Phase 2 contract fixtures: crop tool math and map pin clustering.

    crop_contract_v1.json   quick_edit.fit_aspect / with_aspect / drag_crop, in the phone's
                            CropGeometry.swift. A crop made on the phone matches the desktop's.
    pin_clusters_v1.json    map_vault.html computeClusters / mercPx / pinHitDistance, run in node
                            from the page's own source, for PinClustering.swift. The same photos
                            sit under the same pins at the same zoom on both maps.

The Swift side (Phase2ContractTests.swift) decodes these files. Regenerate them with
KAIROS_REGEN_FIXTURES=1 after an intentional change, and port the change in the same commit.
"""
import json
import math
import os
import random
import re
import shutil
import subprocess
import unittest

import quick_edit as qe

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(ROOT, "ios", "KairosCore", "Tests", "KairosCoreTests", "Fixtures")
REGEN = os.environ.get("KAIROS_REGEN_FIXTURES") == "1"
NODE = shutil.which("node")


def _write_or_compare(test, name, doc):
    path = os.path.join(FIXTURES, name)
    text = json.dumps(doc, indent=1, sort_keys=True) + "\n"
    if REGEN or not os.path.exists(path):
        os.makedirs(FIXTURES, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    with open(path, encoding="utf-8") as fh:
        test.assertEqual(json.load(fh), json.loads(text), f"{name}: contract changed, regenerate + port to Swift")


def _r(v):
    return round(float(v), 12)


# ── crop ─────────────────────────────────────────────────────────────────────

class TestCropContract(unittest.TestCase):
    W, H = 4032, 3024

    def cases(self):
        rnd = random.Random(2026_11_06)
        crops = [(0.0, 0.0, 1.0, 1.0), (0.1, 0.05, 0.7, 0.8), (0.4, 0.4, 0.2, 0.3), (0.0, 0.5, 0.5, 0.5),
                 (0.9, 0.9, 0.1, 0.1)]
        ratios = [None, qe.aspect_ratio("original", self.W, self.H), 1.0, 16 / 9, 4 / 5]
        drags = []
        for crop in crops:
            for handle in ("move", "tl", "tr", "bl", "br", "l", "r", "t", "b"):
                for ratio in ratios:
                    dx, dy = rnd.uniform(-0.3, 0.3), rnd.uniform(-0.3, 0.3)
                    out = qe.drag_crop(crop, handle, dx, dy, ratio, self.W, self.H)
                    drags.append({"crop": list(crop), "handle": handle, "dx": _r(dx), "dy": _r(dy),
                                  "ratio": None if ratio is None else _r(ratio), "out": [_r(v) for v in out]})
        fits = []
        for crop in crops:
            for ratio in ratios:
                out = qe.fit_aspect(crop, ratio, self.W, self.H)
                fits.append({"crop": list(crop), "ratio": None if ratio is None else _r(ratio),
                             "out": [_r(v) for v in out]})
        aspects = []
        for base in ({}, {"crop": [0.1, 0.05, 0.7, 0.8]}, {"angle": 5.0}, {"crop": [0.2, 0.2, 0.3, 0.6], "angle": -3}):
            for aspect in ("free", "original", "1:1", "16:9", "4:5"):
                adj = qe.with_aspect(qe.Adjustments.from_dict(base), aspect, self.W, self.H)
                aspects.append({"input": base, "aspect": aspect, "out": adj.to_dict()})
        ratio_table = {k: (None if qe.aspect_ratio(k, self.W, self.H) is None else _r(qe.aspect_ratio(k, self.W, self.H)))
                       for k in qe.ASPECTS}
        return {"size": [self.W, self.H], "min_crop": qe.MIN_CROP, "ratios": ratio_table,
                "drag": drags, "fit": fits, "with_aspect": aspects}

    def test_crop_fixture(self):
        _write_or_compare(self, "crop_contract_v1.json", self.cases())

    def test_ratio_kept_or_crop_unchanged(self):
        """drag_crop never distorts a locked ratio: it keeps it, or it refuses the move."""
        for c in self.cases()["drag"]:
            if c["ratio"] is None or c["handle"] == "move" or c["out"] == c["crop"]:
                continue
            x, y, w, h = c["out"]
            self.assertAlmostEqual(w * self.W / (h * self.H), c["ratio"], places=6)
            self.assertGreaterEqual(min(x, y), -1e-12)
            self.assertLessEqual(max(x + w, y + h), 1 + 1e-12)


# ── pins ─────────────────────────────────────────────────────────────────────

def _js_function(src, name):
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def _js_const(src, name):
    m = re.search(rf"var {name} = ([0-9.]+)", src)
    return float(m.group(1))


def _points():
    rnd = random.Random(52)
    hubs = [(50.0875, 14.4214), (48.9744, 13.6061), (35.6762, 139.6503), (40.7128, -74.0060),
            (-33.8688, 151.2093), (64.1466, -21.9426), (0.0, 179.9)]
    pts, pid = [], 1
    for la, lo in hubs:
        for _ in range(rnd.randint(20, 60)):
            spread = rnd.choice((0.0005, 0.01, 0.2, 2.0))
            pts.append({"id": pid, "lat": round(la + rnd.gauss(0, spread), 7),
                        "lon": round(((lo + rnd.gauss(0, spread) + 180) % 360) - 180, 7)})
            pid += 1
    for _ in range(5):                                  # exact duplicates (burst shots)
        pts.append({"id": pid, "lat": 50.0875, "lon": 14.4214})
        pid += 1
    return pts


@unittest.skipIf(NODE is None and REGEN, "node is needed to regenerate the pin fixture")
class TestPinContract(unittest.TestCase):
    ZOOMS = [1.7, 3, 5, 8.5, 11, 14.3, 17]

    def run_desktop_js(self):
        src = open(os.path.join(ROOT, "map_vault.html"), encoding="utf-8").read()
        consts = {k: _js_const(src, k) for k in ("CELL_PX", "PIN_PX", "TILE_PX")}
        pin = re.search(r"var PIN_D = (\d+), PIN_PAD = (\d+)", src)
        tail = _js_const(src, "PIN_TAIL")
        script = "\n".join([
            f"var CELL_PX = {consts['CELL_PX']}, PIN_PX = {consts['PIN_PX']}, TILE_PX = {consts['TILE_PX']};",
            f"var PIN_D = {pin.group(1)}, PIN_PAD = {pin.group(2)}, PIN_TAIL = {tail};",
            "var PIN_LIFT = PIN_D / 2 + PIN_TAIL;",
            "var markers = []; function viewFilter() { return null; }",
            _js_function(src, "mercPx"), _js_function(src, "pxDist"),
            _js_function(src, "computeClusters"), _js_function(src, "pinHitDistance"),
            "var input = JSON.parse(require('fs').readFileSync(0, 'utf8'));",
            "var out = {clusters: {}, hits: []};",
            "input.zooms.forEach(function (z) {",
            "  out.clusters[String(z)] = computeClusters(z, input.points).map(function (c) {",
            "    return {ids: c.items.map(function (m) { return m.id; }), lat: c.lat, lon: c.lon}; }); });",
            "input.hits.forEach(function (h) { var d = pinHitDistance({x: 100, y: 200}, {x: h[0], y: h[1]});",
            "  out.hits.push(isFinite(d) ? d : null); });",
            "process.stdout.write(JSON.stringify(out));",
        ])
        hits = [[100 + dx, 200 + dy] for dy in range(-70, 8, 6) for dx in range(-32, 33, 8)]
        payload = json.dumps({"zooms": self.ZOOMS, "points": _points(), "hits": hits})
        res = subprocess.run([NODE, "-e", script], input=payload, capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        return {
            "constants": {"cell_px": consts["CELL_PX"], "pin_px": consts["PIN_PX"], "tile_px": consts["TILE_PX"],
                          "pin_d": float(pin.group(1)), "pin_pad": float(pin.group(2)), "pin_tail": tail},
            "points": _points(),
            "zooms": self.ZOOMS,
            "clusters": {z: [{"ids": c["ids"], "lat": _r(c["lat"]), "lon": _r(c["lon"])} for c in cs]
                         for z, cs in out["clusters"].items()},
            "hit_tip": [100, 200],
            "hits": [{"point": p, "distance": None if d is None else _r(d)} for p, d in zip(hits, out["hits"])],
        }

    @unittest.skipIf(NODE is None, "node not installed: the pin fixture is checked against the JS when it is")
    def test_pin_fixture(self):
        doc = self.run_desktop_js()
        _write_or_compare(self, "pin_clusters_v1.json", doc)
        # sanity: the fixture exercises merging, singles and the teardrop tail
        sizes = {len(c["ids"]) for cs in doc["clusters"].values() for c in cs}
        self.assertIn(1, sizes)
        self.assertGreater(max(sizes), 50)
        tail_hits = [h for h in doc["hits"] if h["distance"] is not None and h["point"][1] > 200 - 13]
        self.assertTrue(tail_hits, "some taps land on the tail below the disc")

    def test_mercator_invariants(self):
        # exact by construction: (0, 0) is the world centre, ±180° the edges, latitude symmetric
        def merc(lat, lon, z):
            size = 512 * 2 ** z
            s = max(-0.9999, min(0.9999, math.sin(math.radians(lat))))
            return (lon + 180) / 360 * size, (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * size
        self.assertEqual(merc(0, 0, 3), (2048.0, 2048.0))
        self.assertEqual(merc(0, 180, 0)[0], 512.0)
        north, south = merc(50, 0, 4)[1], merc(-50, 0, 4)[1]
        self.assertAlmostEqual(north + south, 512 * 2 ** 4, places=9)


if __name__ == "__main__":
    unittest.main()
