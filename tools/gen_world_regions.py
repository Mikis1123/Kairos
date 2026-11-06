"""Generates world_regions.py: every country / territory with photos-worthy land, as boxes.

Dev tool (run on Windows 10 1903+): python tools/gen_world_regions.py
Sources, both offline:
  * GeoNames cities1000 (bundled with reverse_geocoder, already an app dependency): every
    populated place >= 1000 people, with its ISO country code. A country's extent is the
    bounding box of its places, grown by PAD_DEG, split where places are more than GAP_CELLS
    grid cells apart (Alaska / Hawaii / mainland US, the Canaries, Kaliningrad, Russia across
    the antimeridian), so a download never covers an ocean between two parts.
  * ICU (Windows' icu.dll): country and continent names in the 7 UI languages (CLDR), and the
    UN M49 continent of each country.
Territories without a single such place (Antarctica, Bouvet Island ...) are left out.
"""
import csv
import ctypes
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "world_regions.py")
LANGS = {"cs": "cs", "de": "de", "en": "en", "es": "es", "fr": "fr", "ja": "ja", "zh-CN": "zh_Hans"}
CELL_DEG = 1.0
GAP_CELLS = 3            # parts closer than ~3 deg are one box
PAD_DEG = 0.2            # margin around the outermost places (~20 km)
# UN M49: subcontinents that make up "North America" (Northern America, Central America, Caribbean)
CONTINENTS = {"150": "europe", "142": "asia", "002": "africa", "003": "north-america",
              "005": "south-america", "009": "oceania"}
NORTH_AMERICA = {"021", "013", "029"}

icu = ctypes.CDLL("icu.dll")
icu.uloc_getDisplayCountry.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_wchar_p, ctypes.c_int32,
                                       ctypes.POINTER(ctypes.c_int)]
icu.uregion_getRegionFromCode.restype = ctypes.c_void_p
icu.uregion_getRegionFromCode.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
icu.uregion_getContainingRegionOfType.restype = ctypes.c_void_p
icu.uregion_getContainingRegionOfType.argtypes = [ctypes.c_void_p, ctypes.c_int]
icu.uregion_getRegionCode.restype = ctypes.c_char_p
icu.uregion_getRegionCode.argtypes = [ctypes.c_void_p]
URGN_CONTINENT, URGN_SUBCONTINENT = 3, 4


def display(code, lang):
    buf = ctypes.create_unicode_buffer(160)
    st = ctypes.c_int(0)
    n = icu.uloc_getDisplayCountry(("und_" + code).encode(), lang.encode(), buf, 160, ctypes.byref(st))
    return buf.value[:n] if st.value <= 0 and n > 0 else ""


def continent_of(cc):
    st = ctypes.c_int(0)
    r = icu.uregion_getRegionFromCode(cc.encode(), ctypes.byref(st))
    if not r or st.value > 0:
        return None
    cont = icu.uregion_getContainingRegionOfType(r, URGN_CONTINENT)
    sub = icu.uregion_getContainingRegionOfType(r, URGN_SUBCONTINENT)
    cont = icu.uregion_getRegionCode(cont).decode() if cont else None
    sub = icu.uregion_getRegionCode(sub).decode() if sub else None
    if cont == "019":
        return "north-america" if sub in NORTH_AMERICA else ("south-america" if sub == "005" else None)
    return CONTINENTS.get(cont)


def read_places():
    import reverse_geocoder
    path = os.path.join(os.path.dirname(reverse_geocoder.__file__), "rg_cities1000.csv")
    by_cc = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (KeyError, ValueError):
                continue
            by_cc.setdefault(row["cc"], []).append((lon, lat))
    return by_cc


def clusters(points):
    """Connected parts of the 1-degree cells holding places (gap <= GAP_CELLS, wraps at 180)."""
    cells = {}
    for lon, lat in points:
        cells.setdefault((int(math.floor(lon / CELL_DEG)), int(math.floor(lat / CELL_DEG))), []).append((lon, lat))
    ncol = int(360 / CELL_DEG)
    seen, parts = set(), []
    for start in cells:
        if start in seen:
            continue
        todo, part = [start], []
        seen.add(start)
        while todo:
            cx, cy = todo.pop()
            part.extend(cells[(cx, cy)])
            for dx in range(-GAP_CELLS, GAP_CELLS + 1):
                for dy in range(-GAP_CELLS, GAP_CELLS + 1):
                    nx = (cx + dx + ncol // 2) % ncol - ncol // 2
                    nb = (nx, cy + dy)
                    if nb in cells and nb not in seen:
                        seen.add(nb)
                        todo.append(nb)
        parts.append(part)
    return parts


def part_boxes(part):
    """Bounding box(es) of one part; a part across the antimeridian becomes two boxes."""
    lats = [p[1] for p in part]
    s, n = max(-85.0, min(lats) - PAD_DEG), min(85.0, max(lats) + PAD_DEG)
    lons = sorted(p[0] for p in part)
    # widest empty arc between consecutive longitudes: the box is the complement of it
    gaps = [(lons[i + 1] - lons[i], i) for i in range(len(lons) - 1)]
    wrap_gap = lons[0] + 360 - lons[-1]
    widest, at = max(gaps) if gaps else (0, -1)
    if not gaps or wrap_gap >= widest:
        w, e = lons[0] - PAD_DEG, lons[-1] + PAD_DEG
        return [(max(-180.0, w), s, min(180.0, e), n)]
    w, e = lons[at + 1] - PAD_DEG, lons[at] + PAD_DEG       # crosses 180: west edge > east edge
    return [(w, s, 180.0, n), (-180.0, s, e, n)]


def r2(v):
    return round(v, 2)


def main():
    by_cc = read_places()
    countries = {}
    for cc, pts in sorted(by_cc.items()):
        names = {k: display(cc, v) for k, v in LANGS.items()}
        if not names["en"] or names["en"] == cc:
            print("skip (no name)", cc, file=sys.stderr)
            continue
        cont = continent_of(cc)
        if cont is None:
            print("skip (no continent)", cc, names["en"], file=sys.stderr)
            continue
        boxes = []
        for part in clusters(pts):
            boxes.extend(part_boxes(part))
        boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
        countries[cc.lower()] = (cont, [tuple(r2(v) for v in b) for b in boxes], names)
    conts = {}
    for cid, code in [(v, k) for k, v in CONTINENTS.items()]:
        conts[cid] = {k: display(code, v) for k, v in LANGS.items()}

    lines = ['"""World regions for offline map packs: continents and countries, boxes and names.',
             "",
             "GENERATED by tools/gen_world_regions.py - do not edit by hand. Extents come from GeoNames",
             "cities1000 (populated places, + a ~20 km margin, split into parts across open sea), names",
             "and continents from CLDR via ICU, in the 7 UI languages.",
             '"""', "",
             "# continent id -> {language: name}",
             "CONTINENTS = {"]
    for cid, names in conts.items():
        lines.append(f"    {cid!r}: {names!r},")
    lines += ["}", "",
              "# ISO 3166 alpha-2 (lower case) -> (continent id, [(west, south, east, north), ...], {language: name})",
              "COUNTRIES = {"]
    for cc, (cont, boxes, names) in countries.items():
        lines.append(f"    {cc!r}: ({cont!r}, {boxes!r},")
        lines.append(f"           {names!r}),")
    lines += ["}", ""]
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    print(f"{len(countries)} countries, {sum(len(c[1]) for c in countries.values())} boxes -> {OUT}")


if __name__ == "__main__":
    main()
