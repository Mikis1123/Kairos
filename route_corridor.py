"""Gate 2 C: journey legs routed along real roads and trails, independent of the camera.

Before, the map page built its road graph from whatever vector tiles MapLibre had loaded for
the current camera, so legs over ~15 km, or legs the camera had not looked at yet, fell back
to a smooth curve. This module fetches the OpenMapTiles `transportation` layer along each
leg's corridor itself (through the local tile store, so tiles are cached and shared with the
map), decodes it with a small built-in MVT reader, and runs A* off the UI thread.

* The zoom level follows the leg length: z14 (every path) for walks and short drives, down to
  z8 (motorways and trunk roads) for long drives, so a 300 km leg needs about as many tiles
  as a 3 km hike.
* Travel mode follows the photos: on foot when the pace between stops is at or below walking
  speed, otherwise by car (footpaths banned, motorways cheap). A pace faster than a car, or a
  distance no one drives in a day, is a flight and keeps its great-circle arc.
* No routing service sees the trip. Only map tiles are requested, the same ones the map
  would load anyway.
"""
import heapq
import math
import zlib

WALK_KMH = 8.0
FLIGHT_KMH = 150.0            # faster than this between stops = flew
MAX_GROUND_KM = 900.0
MIN_LEG_KM = 0.03
MAX_TILES = 160
DETOUR_FACTOR, DETOUR_KM = 3.2, 0.6
MAX_POINTS, SIMPLIFY_KM = 1500, 0.004

ROAD_COST = {   # multiple of length; 0 = not allowed (same table as map_vault.html)
    "foot": {"motorway": 0, "trunk": 0, "primary": 2.5, "secondary": 1.8, "tertiary": 1.4, "minor": 1.1,
             "service": 1.15, "track": 1, "path": 1, "ferry": 3, "raceway": 0, "busway": 0,
             "bus_guideway": 0, "rail": 0, "transit": 0, "_other": 1.6},
    "car": {"motorway": 1, "trunk": 1, "primary": 1.05, "secondary": 1.1, "tertiary": 1.2, "minor": 1.4,
            "service": 1.8, "track": 3, "path": 0, "ferry": 2.5, "raceway": 0, "busway": 0,
            "bus_guideway": 0, "rail": 0, "transit": 0, "_other": 2},
}
# OpenMapTiles: which road classes exist at a zoom, and how far a photo may be from one
SNAP_KM = {14: 0.25, 13: 0.4, 12: 0.8, 11: 1.5, 10: 3.0, 9: 5.0, 8: 8.0}


def haversine_km(a, b):
    """a, b = (lon, lat)."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


# ── mode and zoom ────────────────────────────────────────────────────────────

def travel_mode(km, hours):
    """'foot' | 'car' | 'flight' from distance and the time between the two stops."""
    if km > MAX_GROUND_KM:
        return "flight"
    if hours is None or not math.isfinite(hours) or hours <= 0:
        return "car" if km > 3 else "foot"
    speed = km / hours
    if speed > FLIGHT_KMH and km > 80:
        return "flight"
    return "car" if speed > WALK_KMH else "foot"


def zoom_for(km, mode):
    if mode == "foot" or km <= 8:
        return 14
    if km <= 30:
        return 12
    if km <= 120:
        return 10
    if km <= 400:
        return 9
    return 8


# ── tiles ────────────────────────────────────────────────────────────────────

def lonlat_to_tile(lon, lat, z):
    n = 1 << z
    lat = max(-85.0511, min(85.0511, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile_km(lat, z):
    return 40075.0 * math.cos(math.radians(lat)) / (1 << z)


def corridor_tiles(points, z, buffer_km):
    """Tiles within buffer_km of the polyline through points ((lon, lat), ...)."""
    out = set()
    n = 1 << z
    for (a, b) in zip(points, points[1:]) if len(points) > 1 else [(points[0], points[0])]:
        size = max(0.05, tile_km((a[1] + b[1]) / 2, z))
        steps = max(1, int(math.ceil(haversine_km(a, b) / (size * 0.5))))
        ring = int(math.ceil(buffer_km / size))
        for s in range(steps + 1):
            t = s / steps
            lon, lat = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
            x, y = lonlat_to_tile(lon, lat, z)
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    tx, ty = int(x) + dx, int(y) + dy
                    if 0 <= ty < n:
                        out.add((z, tx % n, ty))
    return out


def plan_tiles(points, km, mode):
    """(zoom, tiles) for a leg; coarser zoom until the corridor fits MAX_TILES."""
    z = zoom_for(km, mode)
    buffer_km = max(0.6, 0.15 * km)
    while True:
        tiles = corridor_tiles(points, z, buffer_km)
        if len(tiles) <= MAX_TILES or z <= 8:
            return z, tiles
        z -= 1 if z > 12 else 2 if z > 9 else 1


# ── minimal Mapbox Vector Tile reader (transportation layer only) ────────────

def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7


def _fields(buf, start=0, end=None):
    """Yields (field, wire type, value or (start, end)) for one protobuf message."""
    i, end = start, len(buf) if end is None else end
    while i < end:
        key, i = _varint(buf, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(buf, i)
            yield field, wt, v
        elif wt == 2:
            ln, i = _varint(buf, i)
            yield field, wt, (i, i + ln)
            i += ln
        elif wt == 1:
            yield field, wt, (i, i + 8)
            i += 8
        elif wt == 5:
            yield field, wt, (i, i + 4)
            i += 4
        else:
            raise ValueError("unsupported wire type")


def _packed(buf, span):
    i, end, out = span[0], span[1], []
    while i < end:
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _zigzag(n):
    return (n >> 1) ^ -(n & 1)


def decode_roads(data, z, x, y, layer_name="transportation"):
    """Raw tile bytes (gzip or plain) -> [(class, [[(lon, lat), ...], ...])]."""
    if data[:2] == b"\x1f\x8b":
        data = zlib.decompress(data, 47)
    buf = memoryview(data)
    out = []
    for field, wt, span in _fields(buf):
        if field != 3 or wt != 2:
            continue
        name, keys, values, feats, extent = None, [], [], [], 4096
        for f, w, v in _fields(buf, *span):
            if f == 1:
                name = bytes(buf[v[0]:v[1]]).decode("utf-8", "replace")
            elif f == 2:
                feats.append(v)
            elif f == 3:
                keys.append(bytes(buf[v[0]:v[1]]).decode("utf-8", "replace"))
            elif f == 4:
                val = None
                for vf, vw, vv in _fields(buf, *v):
                    if vf == 1:
                        val = bytes(buf[vv[0]:vv[1]]).decode("utf-8", "replace")
                values.append(val)
            elif f == 5:
                extent = v
        if name != layer_name:
            continue
        try:
            class_key = keys.index("class")
        except ValueError:
            class_key = -1
        n = float(1 << z)
        for fs in feats:
            tags, geom, gtype = [], None, 0
            for f, w, v in _fields(buf, *fs):
                if f == 2:
                    tags = _packed(buf, v)
                elif f == 3:
                    gtype = v
                elif f == 4:
                    geom = _packed(buf, v)
            if gtype != 2 or not geom:
                continue
            cls = None
            for k in range(0, len(tags) - 1, 2):
                if tags[k] == class_key:
                    cls = values[tags[k + 1]] if tags[k + 1] < len(values) else None
                    break
            lines, cur, cx, cy, i = [], None, 0, 0, 0
            while i < len(geom):
                cmd, count = geom[i] & 7, geom[i] >> 3
                i += 1
                if cmd == 7:
                    continue
                for _ in range(count):
                    cx += _zigzag(geom[i])
                    cy += _zigzag(geom[i + 1])
                    i += 2
                    wx = (x + cx / extent) / n
                    wy = (y + cy / extent) / n
                    lon = wx * 360.0 - 180.0
                    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * wy))))
                    if cmd == 1:
                        cur = [(lon, lat)]
                        lines.append(cur)
                    elif cur is not None:
                        cur.append((lon, lat))
            out.append((cls, [ln for ln in lines if len(ln) > 1]))
    return out


# ── graph + A* ───────────────────────────────────────────────────────────────

def build_graph(roads, costs, bbox):
    nodes, adj = {}, {}

    def node(c):
        k = (round(c[0] * 1e5), round(c[1] * 1e5))
        if k not in nodes:
            nodes[k] = c
            adj[k] = []
        return k

    lo_x, lo_y, hi_x, hi_y = bbox
    for cls, lines in roads:
        w = costs.get(cls, costs["_other"])
        if not w:
            continue
        for line in lines:
            for a, b in zip(line, line[1:]):
                if not (lo_x <= a[0] <= hi_x and lo_y <= a[1] <= hi_y) and \
                        not (lo_x <= b[0] <= hi_x and lo_y <= b[1] <= hi_y):
                    continue
                ka, kb = node(a), node(b)
                if ka == kb:
                    continue
                d = haversine_km(a, b) * w
                adj[ka].append((kb, d))
                adj[kb].append((ka, d))
    return nodes, adj


def nearest(nodes, c, max_km):
    best, bd = None, max_km
    # cheap prefilter in degrees before the exact distance
    dlat = max_km / 110.0
    dlon = max_km / (111.0 * max(0.05, math.cos(math.radians(c[1]))))
    for k, p in nodes.items():
        if abs(p[1] - c[1]) > dlat or abs(p[0] - c[0]) > dlon:
            continue
        d = haversine_km(c, p)
        if d <= bd:
            best, bd = k, d
    return best


def astar(nodes, adj, s, t):
    if s == t:
        return [nodes[s]]
    goal = nodes[t]
    g = {s: 0.0}
    prev = {}
    heap = [(haversine_km(nodes[s], goal), 0.0, s)]
    closed = set()
    while heap:
        _f, gc, k = heapq.heappop(heap)
        if k == t:
            break
        if k in closed:
            continue
        closed.add(k)
        for nb, w in adj[k]:
            ng = gc + w
            if ng < g.get(nb, math.inf):
                g[nb] = ng
                prev[nb] = k
                heapq.heappush(heap, (ng + haversine_km(nodes[nb], goal), ng, nb))
    if t not in g:
        return None
    path, k = [nodes[t]], t
    while k != s:
        k = prev.get(k)
        if k is None:
            return None
        path.append(nodes[k])
    return path[::-1]


def simplify(pts, tol_km):
    if len(pts) < 3:
        return list(pts)
    kx = math.cos(math.radians(pts[0][1])) * 111.32
    ky = 110.57
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = pts[a][0] * kx, pts[a][1] * ky
        dx, dy = pts[b][0] * kx - ax, pts[b][1] * ky - ay
        l2 = dx * dx + dy * dy
        far, fd = -1, tol_km
        for i in range(a + 1, b):
            px, py = pts[i][0] * kx - ax, pts[i][1] * ky - ay
            t = max(0.0, min(1.0, (px * dx + py * dy) / l2)) if l2 > 0 else 0.0
            d = math.hypot(px - t * dx, py - t * dy)
            if d > fd:
                far, fd = i, d
        if far >= 0:
            keep[far] = True
            stack.extend([(a, far), (far, b)])
    return [p for p, k in zip(pts, keep) if k]


def path_km(pts):
    return sum(haversine_km(a, b) for a, b in zip(pts, pts[1:]))


def route_leg(wps, mode, fetch):
    """wps = [(lon, lat), ...] (stop centre, exit, entry, next centre) -> [[lon, lat], ...] or None.
    fetch(z, x, y) -> tile bytes or None."""
    if mode not in ROAD_COST or len(wps) < 2:
        return None
    km = sum(haversine_km(a, b) for a, b in zip(wps, wps[1:]))
    if km < MIN_LEG_KM:
        return None
    z, tiles = plan_tiles(wps, km, mode)
    roads = []
    for (tz, tx, ty) in sorted(tiles):
        data = fetch(tz, tx, ty)
        if not data:
            continue
        try:
            roads.extend(decode_roads(data, tz, tx, ty))
        except (ValueError, IndexError, zlib.error):
            continue
    if not roads:
        return None
    pad_km = max(0.6, 0.3 * km)
    lons, lats = [p[0] for p in wps], [p[1] for p in wps]
    kx = max(0.05, math.cos(math.radians(lats[0])))
    bbox = (min(lons) - pad_km / 111.0 / kx, min(lats) - pad_km / 111.0,
            max(lons) + pad_km / 111.0 / kx, max(lats) + pad_km / 111.0)
    nodes, adj = build_graph(roads, ROAD_COST[mode], bbox)
    if len(nodes) < 2:
        return None
    snap = SNAP_KM.get(z, 8.0)
    keys = [nearest(nodes, p, snap) for p in wps]
    if any(k is None for k in keys):
        return None
    path = [wps[0]]
    for s, t in zip(keys, keys[1:]):
        seg = astar(nodes, adj, s, t)
        if seg is None:
            return None
        path.extend(seg)
    path.append(wps[-1])
    path = [p for i, p in enumerate(path) if i == 0 or p != path[i - 1]]
    if path_km(path) > DETOUR_FACTOR * km + DETOUR_KM:
        return None
    tol = SIMPLIFY_KM * (1 << max(0, 14 - z)) ** 0.5
    path = simplify(path, tol)
    while len(path) > MAX_POINTS:
        tol *= 2
        path = simplify(path, tol)
    return [[round(p[0], 6), round(p[1], 6)] for p in path] if len(path) >= 2 else None


# ── journey legs ─────────────────────────────────────────────────────────────

def _ll(p):
    try:
        return (float(p["lon"]), float(p["lat"]))
    except (KeyError, TypeError, ValueError):
        return None


def _hours(a, b):
    from datetime import datetime
    try:
        ta = datetime.strptime(str(a)[:16], "%Y-%m-%d %H:%M")
        tb = datetime.strptime(str(b)[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (tb - ta).total_seconds() / 3600.0


def leg_requests(stops):
    """[(leg index, waypoints, mode)] for the legs worth routing, nearest first."""
    out = []
    for i in range(1, len(stops)):
        A, B = stops[i - 1], stops[i]
        a, b = _ll(A), _ll(B)
        if a is None or b is None:
            continue
        km = haversine_km(a, b)
        if km < MIN_LEG_KM:
            continue
        mode = travel_mode(km, _hours(A.get("end") or A.get("start"), B.get("start")))
        if mode == "flight":
            continue
        wps = [a]
        for p in (_ll(A.get("exit") or {}), _ll(B.get("entry") or {})):
            if p and haversine_km(p, wps[-1]) > 0.03 and haversine_km(p, b) > 0.03:
                wps.append(p)
        wps.append(b)
        out.append((i, wps, mode))
    return out


def tile_key(template, z, x, y):
    """Tile store key from a tile URL template (upstream or the local store's own URL)."""
    url = template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
    for host in ("tiles.openfreemap.org/",):
        k = url.find(host)
        if k >= 0:
            return url[k:]
    return None


def store_fetcher(template, resolve):
    """fetch(z, x, y) through tile_store's resolve(key) -> (status, data, ...)."""
    def fetch(z, x, y):
        key = tile_key(template, z, x, y)
        if not key:
            return None
        try:
            status, data, _ctype, _enc, _src = resolve(key, lane="bake")
        except Exception:
            return None
        return data if status == 200 and data else None
    return fetch
