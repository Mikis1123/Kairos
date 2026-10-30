"""Atlas (Sprint 4): celoživotní mapa cest na zeměkouli.

Z knihovny vzniknou:
  * domov – nejčastější místo (mřížka ~11 km vážená počtem různých dní, ne počtem fotek: jeden
    výlet s 500 fotkami domovem není),
  * výlety – události s polohou: těžiště (medián), rok, počet, vzdálenost od domova,
  * zářící oblouky domov -> výlet po hlavní kružnici (GeoJSON, line-progress pro náběh),
  * hustota – hexagonální mřížka ve třech úrovních (zeměkoule / země / region), počty po letech,
    aby šel filtr časové osy bez přepočtu,
  * statistiky – výlety, země, místa, km (tam i zpět), roky.
Data jdou do window.setAtlas() v map_vault.html; klik na oblouk nebo výlet spustí Cestu.
"""
import math
from collections import Counter, defaultdict

from database import visible_media_sql
from journey import _valid, haversine_km, iso_minute

HOME_CELL_DEG = 0.1           # mřížka domova (~11 km)
TRIP_MIN_KM = 40              # dál než tolik od domova = výlet (oblouk); blíž = místní událost
THREAD_MIN_KM = 5             # nit životem: oblouk mezi po sobě jdoucími výlety, jsou-li od sebe dál
ARC_POINTS = 48
HEX_LEVELS = ((0, 3.2, 180.0), (3.2, 6.0, 40.0), (6.0, 24.0, 8.0))   # (od zoomu, do zoomu, poloměr km)
MAX_ROWS = 200_000


def _year(date_s):
    s = iso_minute(date_s)
    return int(s[:4]) if s[:4].isdigit() else None


def geo_rows(conn):
    """[(id, lat, lon, iso datum, event_id, město, země)] viditelných fotek s polohou."""
    out = []
    for mid, lat, lon, taken, eid, city, country in conn.execute(f"""
            SELECT m.id, m.latitude, m.longitude, m.date_taken, m.event_id, m.place_city, m.place_country
            FROM media_items m
            WHERE m.latitude IS NOT NULL AND m.date_taken IS NOT NULL AND {visible_media_sql('m')}
            LIMIT {MAX_ROWS}"""):
        ll = _valid(lat, lon)
        if ll:
            out.append((int(mid), ll[0], ll[1], iso_minute(taken), eid, city or "", country or ""))
    return out


def find_home(rows):
    """Buňka s nejvíc různými dny (pak fotkami) -> (lat, lon) průměr fotek v ní, nebo None."""
    days, count, sums = defaultdict(set), Counter(), defaultdict(lambda: [0.0, 0.0])
    for _mid, lat, lon, d, _e, _c, _k in rows:
        cell = (round(lat / HOME_CELL_DEG), round(lon / HOME_CELL_DEG))
        days[cell].add(d[:10])
        count[cell] += 1
        sums[cell][0] += lat
        sums[cell][1] += lon
    if not count:
        return None
    best = max(count, key=lambda c: (len(days[c]), count[c]))
    return (sums[best][0] / count[best], sums[best][1] / count[best])


def _median(vals):
    v = sorted(vals)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def great_circle(a, b, n=ARC_POINTS):
    """[(lon, lat)] po hlavní kružnici z a do b (lat, lon) – sférická interpolace."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    p1 = (math.cos(la1) * math.cos(lo1), math.cos(la1) * math.sin(lo1), math.sin(la1))
    p2 = (math.cos(la2) * math.cos(lo2), math.cos(la2) * math.sin(lo2), math.sin(la2))
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(p1, p2))))
    om = math.acos(dot)
    pts = []
    prev_lon = None
    for k in range(n + 1):
        t = k / n
        if om < 1e-9:
            x, y, z = p1
        else:
            s1, s2 = math.sin((1 - t) * om) / math.sin(om), math.sin(t * om) / math.sin(om)
            x, y, z = (s1 * p1[i] + s2 * p2[i] for i in range(3))
        lat = math.degrees(math.atan2(z, math.hypot(x, y)))
        lon = math.degrees(math.atan2(y, x))
        if prev_lon is not None:                   # bez skoku přes antimeridián (MapLibre bere > 180)
            while lon - prev_lon > 180:
                lon -= 360
            while lon - prev_lon < -180:
                lon += 360
        prev_lon = lon
        pts.append((round(lon, 5), round(lat, 5)))
    return pts


# ── hexagonální mřížka (rovnoběžková plocha: x = lon·cos φ0 ve stupních) ──
def _hex_key(lat, lon, size_deg):
    """Osová souřadnice (q, r) šestiúhelníku s „špičkou nahoru“ a velikostí size_deg."""
    x, y = lon, lat
    q = (math.sqrt(3) / 3 * x - y / 3) / size_deg
    r = (2 / 3 * y) / size_deg
    # zaokrouhlení v krychlových souřadnicích
    cx, cz = q, r
    cy = -cx - cz
    rx, ry, rz = round(cx), round(cy), round(cz)
    dx, dy, dz = abs(rx - cx), abs(ry - cy), abs(rz - cz)
    if dx > dy and dx > dz:
        rx = -ry - rz
    elif dy > dz:
        ry = -rx - rz
    else:
        rz = -rx - ry
    return int(rx), int(rz)


def _hex_center(q, r, size_deg):
    return (size_deg * 1.5 * r, size_deg * math.sqrt(3) * (q + r / 2))     # (lat, lon)


def hex_polygon(q, r, size_deg):
    lat0, lon0 = _hex_center(q, r, size_deg)
    ring = []
    for k in range(6):
        ang = math.radians(60 * k + 30)
        lat = lat0 + size_deg * math.sin(ang)
        lon = lon0 + size_deg * math.cos(ang) / max(0.2, math.cos(math.radians(max(-80, min(80, lat0)))))
        ring.append((round(lon, 5), round(max(-85, min(85, lat)), 5)))
    ring.append(ring[0])
    return ring


def hexbins(rows, radius_km):
    """{(q, r): {rok: počet}} – velikost buňky ve stupních šířky; délka se v hex_polygon roztáhne
    podle cos φ (buňky na severu nejsou úzké)."""
    size = radius_km / 111.0
    bins = defaultdict(Counter)
    for _mid, lat, lon, d, _e, _c, _k in rows:
        x = lon * math.cos(math.radians(lat))
        bins[_hex_key(lat, x, size)][_year(d)] += 1
    return bins, size


def _hex_lonlat_key(lat, lon, size):
    return _hex_key(lat, lon * math.cos(math.radians(lat)), size)


def _arc(a, b, trip, kind):
    return {"type": "Feature", "geometry": {"type": "LineString", "coordinates": great_circle(a, b)},
            "properties": {"id": trip["id"], "year": trip["year"], "km": trip["km"], "count": trip["count"],
                           "playable": trip["playable"], "kind": kind}}


def build(conn):
    """Data Atlasu pro window.setAtlas(): {home, trips, arcs, hex, years, stats}."""
    rows = geo_rows(conn)
    if not rows:
        return None
    home = find_home(rows)
    by_event = defaultdict(list)
    for r in rows:
        if r[4] is not None:
            by_event[int(r[4])].append(r)
    titles = {int(e): (t, loc, s, en) for e, t, loc, s, en in
              conn.execute("SELECT id, title, location_name, start_date, end_date FROM events")}
    trips, arcs = [], []
    for eid, rs in by_event.items():
        if len(rs) < 2:
            continue
        lat, lon = _median([r[1] for r in rs]), _median([r[2] for r in rs])
        dates = sorted(r[3] for r in rs)
        km = haversine_km(home, (lat, lon)) if home else 0.0
        title, loc, _s, _e = titles.get(eid, ("", "", "", ""))
        city = Counter(r[5] for r in rs if r[5]).most_common(1)
        span = max(haversine_km((lat, lon), (r[1], r[2])) for r in rs)
        from reels import clean_title
        trip = {"id": eid, "title": clean_title(title) or loc or (city[0][0] if city else "Výlet"),
                "place": loc or (city[0][0] if city else ""), "lat": round(lat, 5), "lon": round(lon, 5),
                "year": _year(dates[0]), "start": dates[0], "end": dates[-1], "count": len(rs),
                "km": round(km, 1), "span_km": round(span, 1), "far": km >= TRIP_MIN_KM,
                "playable": span >= 0.4}               # Cesta potřebuje aspoň dvě místa (journey.STOP_RADII_KM[0])
        trips.append(trip)
        if home and trip["far"]:
            arcs.append(_arc(home, (lat, lon), trip, "home"))
    trips.sort(key=lambda t: t["start"])
    # nit životem: z výletu na další výlet (i když mezitím byl doma) – oblouk vede do novějšího
    for a, b in zip(trips, trips[1:]):
        if haversine_km((a["lat"], a["lon"]), (b["lat"], b["lon"])) >= THREAD_MIN_KM:
            arcs.append(_arc((a["lat"], a["lon"]), (b["lat"], b["lon"]), b, "thread"))
    hexes = []
    for lo, hi, radius in HEX_LEVELS:
        bins, size = hexbins(rows, radius)
        for (q, r), per_year in bins.items():
            ring = hex_polygon(q, r, size)
            total = sum(per_year.values())
            for k, (y, n) in enumerate(per_year.items()):
                # first: jediný prvek buňky pro pohled „všechny roky“ (bez skládání průhlednosti)
                hexes.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                              "properties": {"lvl": radius, "z0": lo, "z1": hi, "year": y, "n": n, "all": total,
                                             "first": k == 0}})
    years = Counter(_year(r[3]) for r in rows)
    countries = {r[6] for r in rows if r[6]}
    places = {r[5] for r in rows if r[5]}
    stats = {"photos": len(rows), "trips": sum(1 for t in trips if t["far"]), "events": len(trips),
             "countries": len(countries), "places": len(places),
             "km": round(sum(2 * t["km"] for t in trips if t["far"])),
             "years": sorted(y for y in years if y)}
    return {"home": {"lat": round(home[0], 5), "lon": round(home[1], 5)} if home else None,
            "trips": trips, "arcs": {"type": "FeatureCollection", "features": arcs},
            "hex": {"type": "FeatureCollection", "features": hexes},
            "years": [{"year": y, "count": years[y]} for y in sorted(y for y in years if y)], "stats": stats,
            "hexLevels": [list(lv) for lv in HEX_LEVELS]}
