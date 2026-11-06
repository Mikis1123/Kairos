"""Místní úložiště mapových dlaždic v SQLite (Cesta 3.0, C7: „pre-baked tile store“).

Proč: HTTP cache Chromia dlaždice drží, ale řídí se hlavičkami serverů – Esri a AWS posílají
krátkou nebo žádnou platnost, takže i „teplá“ dlaždice se za letu Cesty ověřovala po síti
(round-trip = díra v obraze). A po smazání profilu / výměně cache je všechno pryč.

Jak: stránka mapy neposílá dlaždice přímo na servery, ale na místní HTTP server
(127.0.0.1, náhodné porty, tajný token v cestě). Ten dlaždici vrátí z SQLite; chybějící
přesměruje (307) na původní server – Chromium ji stáhne přímo přes HTTP/2 – a kopii si
stáhne a uloží na pozadí (jen hosty z ALLOWED_WEB_HOSTS). Při startu Cesty stránka pošle
seznam dlaždic celého filmu (_bake) a úložiště je stahuje v pořadí filmu před kamerou.
Verzované dlaždice (OpenFreeMap planet/<verze>/, terén AWS) jsou neměnné; rastry Esri /
OpenTopoMap se po RASTER_TTL_S obnoví (do té doby i bez sítě platí starší kopie).

Několik portů: Chromium otevírá na jeden host:port nejvýš 6 spojení HTTP/1.1 – jeden port by
za letu (desítky dlaždic naráz) byl úzkým hrdlem. URL se na port mapuje deterministicky
(stejná dlaždice = stejná adresa, ať ji chce přednačtení nebo MapLibre).

Rozpočet: config „tile_store_mb“ (výchozí 1 GB), LRU podle posledního použití. Zápisy (nové
dlaždice, čas použití, mazání) dělá jedno vlákno; čtení jde paralelně (WAL).

Fidelity pass (2026-11-03): three download queues by priority, worked by one pool.
  * urgent: the arrival view of a flight into photos (sent when the flight starts, ~1.5-4 s ahead)
  * film: the whole Journey film (_bake, replaces the previous film)
  * background: regions to keep warm (a trip's bounding box z10-16, the library's photo
    hotspots). Lowest priority, at most BACKGROUND_PER_HOST at once per server.
The most recently used tiles also stay in RAM (HOT_CACHE_MB), so a hot tile is served without
touching SQLite. There are 8 ports (8 × 6 Chromium connections).

Sprint 3 item 3 (offline): region packs. A pack is one MBTiles file (SQLite, the standard
metadata + tiles(zoom_level, tile_column, tile_row TMS) schema) in paths.MAP_PACKS_DIR that
stands in for one tile source (SOURCE_PATTERNS: OpenMapTiles vector, Esri imagery / street map,
OpenTopoMap, terrain). route() / resolve() look into packs after a store miss, before the
network, so a pack-covered region renders with the network unplugged. Packs are outside the LRU
budget and outside "Clear map cache"; they come from an import (any OpenMapTiles .mbtiles
extract, e.g. planetiler / openmaptiles tools) or from build_region_pack() (a bulk download of
a REGIONS bounding box into a new pack). Pack metadata is read on a worker thread (scan_async).
"""
import http.client
import http.server
import json
import math
import os
import queue
import re
import secrets
import shutil
import socketserver
import sqlite3
import threading
import time
import zlib
from collections import OrderedDict
from urllib.parse import urlsplit

import paths

DEFAULT_BUDGET_MB = 1024
EVICT_TO = 0.9                      # po překročení rozpočtu smazat nejdéle nepoužité na 90 %
SHARDS = 8                          # portů (× 6 spojení Chromia = 48 souběžných dlaždic)
UPSTREAM_PER_HOST = 12              # souběžná stahování z jednoho serveru (slušnost k OFM / Esri)
BAKE_WORKERS = 12                   # pool for the three queues (urgent, film, background)
BAKE_PER_HOST = 8                   # film + urgent downloads per server
BACKGROUND_PER_HOST = 3             # background warm-up per server: never crowds the camera
WARM_MAX_KEYS = 12000               # one warm request (a trip region z10-16 is ~2-6k)
BACKGROUND_MAX_QUEUE = 30000        # oldest background work is dropped beyond this
HOT_CACHE_MB = 96                   # RAM copy of the most recently used tiles
BACKGROUND_THREADS = 4              # of BAKE_WORKERS; the rest stay free for urgent / film
GATE_POLL_S = 0.1                   # a gated background worker looks again after this
BAKE_BODY_MAX = 2 * 1024 * 1024     # seznam klíčů jedné Cesty (~6000 × 80 B)
PRIORITIES = ("urgent", "film", "background")
UPSTREAM_TIMEOUT_S = 20
RASTER_TTL_S = 30 * 24 * 3600       # neverzované rastry (Esri, OpenTopoMap, trasy)
TOUCH_EVERY_S = 3600                # čas použití se zapisuje nejvýš jednou za hodinu na dlaždici
USER_AGENT = "Kairos/1.0 (desktop photo library; map tile cache)"
# Neměnné zdroje: obsah se pod stejnou adresou nikdy nemění
IMMUTABLE_PREFIXES = ("tiles.openfreemap.org/planet/", "elevation-tiles-prod.s3.amazonaws.com/")
# Co se do úložiště smí: jen dlaždice (styl, písma a ikony nechá HTTP cache Chromia)
TILE_HOSTS = frozenset({
    "server.arcgisonline.com", "tile.opentopomap.org", "tile.waymarkedtrails.org",
    "tiles.openfreemap.org", "elevation-tiles-prod.s3.amazonaws.com",
})


def store_path():
    return paths.TILE_STORE_PATH                # čteno za běhu (testy ho přesměrují)


def shard_of(key, n=SHARDS):
    """Port pro dlaždici – stejný výpočet jako storeUrl() v map_vault.html (součet kódů znaků)."""
    return sum(key.encode("utf-8")) % max(1, n)


def is_immutable(key):
    return key.startswith(IMMUTABLE_PREFIXES)


class TileStore:
    """SQLite tabulka tiles(key = host/cesta?dotaz). Čtení z libovolného vlákna, zápisy frontou."""

    def __init__(self, path=None, budget_mb=DEFAULT_BUDGET_MB, hot_mb=HOT_CACHE_MB):
        self.path = path or store_path()
        self.budget = int(budget_mb) * 1024 * 1024
        self.hot_budget = max(0, int(hot_mb)) * 1024 * 1024
        self._hot = OrderedDict()           # key -> row (data, ctype, enc, fetched), LRU order
        self._hot_bytes = 0
        self._hot_lock = threading.Lock()
        self.hot_hits = 0
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._local = threading.local()
        self._writes = queue.Queue()
        self._touched = {}                  # key -> kdy naposledy zapsán čas použití
        self.hits = self.misses = self.stale = self.redirects = 0
        conn = self._conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""CREATE TABLE IF NOT EXISTS tiles (
            key TEXT PRIMARY KEY, data BLOB NOT NULL, ctype TEXT, enc TEXT,
            fetched REAL NOT NULL, used REAL NOT NULL, size INTEGER NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS tiles_used ON tiles(used)")
        conn.commit()
        # Store size is counted by the writer thread (_open_writer), not here: `size` sits after
        # the BLOB, so SUM(size) without an index walks every tile's overflow pages – measured
        # 5.5 s for a 238 MB store, on the GUI thread, each time the map was built.
        self.bytes = 0
        self.ready = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, name="tile-store-writer", daemon=True)
        self._writer.start()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    def _hot_put(self, key, row):
        if not self.hot_budget or row is None or len(row[0]) > self.hot_budget // 8:
            return
        with self._hot_lock:
            old = self._hot.pop(key, None)
            if old is not None:
                self._hot_bytes -= len(old[0])
            self._hot[key] = row
            self._hot_bytes += len(row[0])
            while self._hot_bytes > self.hot_budget and self._hot:
                _, gone = self._hot.popitem(last=False)
                self._hot_bytes -= len(gone[0])

    def _hot_get(self, key):
        with self._hot_lock:
            row = self._hot.get(key)
            if row is not None:
                self._hot.move_to_end(key)
            return row

    def hot_stats(self):
        with self._hot_lock:
            return {"tiles": len(self._hot), "bytes": self._hot_bytes, "budget": self.hot_budget, "hits": self.hot_hits}

    def has(self, key):
        """Fresh copy stored? (no touch, no RAM copy: queue checks)."""
        row = self._hot_get(key)
        if row is None:
            try:
                row = self._conn().execute("SELECT fetched FROM tiles WHERE key = ?", (key,)).fetchone()
            except sqlite3.Error:
                return False
            if row is None:
                return False
            return is_immutable(key) or (time.time() - row[0]) < RASTER_TTL_S
        return self.fresh(row, key)

    def get(self, key):
        """(data, ctype, enc, fetched) nebo None."""
        row = self._hot_get(key)
        if row is not None:
            self.hot_hits += 1
        else:
            try:
                row = self._conn().execute("SELECT data, ctype, enc, fetched FROM tiles WHERE key = ?", (key,)).fetchone()
            except sqlite3.Error:
                return None
            if row is not None:
                self._hot_put(key, row)
        if row is not None:
            now = time.time()
            if now - self._touched.get(key, 0) > TOUCH_EVERY_S:
                self._touched[key] = now
                self._writes.put(("touch", key, now))
        return row

    def fresh(self, row, key, now=None):
        return is_immutable(key) or ((now or time.time()) - row[3]) < RASTER_TTL_S

    def put(self, key, data, ctype, enc):
        now = time.time()
        data = bytes(data)
        with self._hot_lock:
            cached = key in self._hot
        if cached:                                      # a refreshed copy replaces the RAM one
            self._hot_put(key, (data, ctype, enc, now))
        self._writes.put(("put", key, data, ctype, enc, now))

    def flush(self, timeout=5.0):
        """Počká, než writer zapíše vše z fronty (testy, ukončení aplikace)."""
        done = threading.Event()
        self._writes.put(("sync", done))
        return done.wait(timeout)

    def close(self):
        self.flush()
        self._writes.put(None)
        self._writer.join(5)

    def stats(self):
        return {"bytes": self.bytes, "budget": self.budget, "hits": self.hits, "misses": self.misses,
                "stale": self.stale, "redirects": self.redirects}

    def clear(self, timeout=10):
        """Deletes every stored tile and gives the disk space back (VACUUM on the writer thread).
        Blocks up to timeout: call it from a worker thread (map_precache.clear_cache_async)."""
        done = threading.Event()
        self._writes.put(("clear", done))
        return done.wait(timeout)

    def set_budget(self, budget_mb):
        """New disk budget; a lower one evicts on the writer thread right away."""
        self.budget = int(budget_mb) * 1024 * 1024
        self._writes.put(("sync", threading.Event()))

    def disk_bytes(self):
        """Size of the store files on disk (stat only, no SQLite: safe on the GUI thread)."""
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total

    def _open_writer(self):
        """Writer connection + covering indexes, so size bookkeeping never reads tile data.
        The first run on an existing store builds them once (one pass), later runs are instant."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS tiles_used_size ON tiles(used, size, key)")
            conn.execute("CREATE INDEX IF NOT EXISTS tiles_key_size ON tiles(key, size)")
            conn.commit()
            self.bytes = conn.execute("SELECT COALESCE(SUM(size), 0) FROM tiles INDEXED BY tiles_used_size"
                                      ).fetchone()[0]
        except sqlite3.Error as e:
            print(f"[TileStore] index: {e}")
        self.ready.set()
        return conn

    def _write_loop(self):
        conn = self._open_writer()
        while True:
            item = self._writes.get()
            if item is None:
                break
            batch = [item]
            while len(batch) < 256:             # dávka = jedna transakce (stovky dlaždic za letu)
                try:
                    nxt = self._writes.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._writes.put(None)
                    break
                batch.append(nxt)
            events = []
            vacuum = False
            try:
                for op in batch:
                    kind = op[0]
                    if kind == "put":
                        _, key, data, ctype, enc, now = op
                        old = conn.execute("SELECT size FROM tiles INDEXED BY tiles_key_size WHERE key = ?",
                                           (key,)).fetchone()
                        conn.execute("INSERT OR REPLACE INTO tiles(key, data, ctype, enc, fetched, used, size) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, data, ctype, enc, now, now, len(data)))
                        self.bytes += len(data) - (old[0] if old else 0)
                    elif kind == "touch":
                        conn.execute("UPDATE tiles SET used = ? WHERE key = ?", (op[2], op[1]))
                    elif kind == "clear":
                        conn.execute("DELETE FROM tiles")
                        self.bytes = 0
                        self._touched.clear()
                        with self._hot_lock:
                            self._hot.clear()
                            self._hot_bytes = 0
                        events.append(op[1])
                        vacuum = True
                    elif kind == "sync":
                        events.append(op[1])
                if self.bytes > self.budget:
                    self._evict(conn)
                conn.commit()
                if vacuum:                      # "Clear map cache": the file shrinks too
                    try:
                        conn.execute("VACUUM")
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except sqlite3.Error as e:
                        print(f"[TileStore] vacuum: {e}")
            except sqlite3.Error as e:
                print(f"[TileStore] zápis selhal: {e}")
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            for ev in events:
                ev.set()
        conn.close()

    def _evict(self, conn):
        target = int(self.budget * EVICT_TO)
        while self.bytes > target:
            rows = conn.execute("SELECT key, size FROM tiles INDEXED BY tiles_used_size ORDER BY used LIMIT 500"
                                ).fetchall()
            if not rows:
                self.bytes = 0
                break
            doomed = []
            for k, s in rows:
                if self.bytes <= target:
                    break
                doomed.append((k,))
                self.bytes -= s
                self._touched.pop(k, None)
                with self._hot_lock:
                    old = self._hot.pop(k, None)
                    if old is not None:
                        self._hot_bytes -= len(old[0])
            conn.executemany("DELETE FROM tiles WHERE key = ?", doomed)


class _Upstream:
    """Udržovaná HTTPS spojení na povolené servery (TLS handshake za každou dlaždici by stál
    víc než samotná dlaždice) + strop souběžných stahování na server."""

    def __init__(self):
        self._idle = {}
        self._lock = threading.Lock()
        self._limits = {}

    def _limit(self, host, lane):
        with self._lock:
            n = {"bake": BAKE_PER_HOST, "background": BACKGROUND_PER_HOST}.get(lane, UPSTREAM_PER_HOST)
            return self._limits.setdefault((host, lane), threading.BoundedSemaphore(n))

    def fetch(self, host, path, lane="map"):
        """(status, data, ctype, enc). Výjimka = síť nedostupná. lane: mapa má vlastní strop,
        předpečení ji nesmí vytlačit (dlaždice pod kamerou mají přednost)."""
        with self._limit(host, lane):
            for attempt in (0, 1):              # udržované spojení mohl server mezitím zavřít
                with self._lock:
                    pool = self._idle.setdefault(host, [])
                    conn = pool.pop() if pool else None
                if conn is None:
                    conn = http.client.HTTPSConnection(host, timeout=UPSTREAM_TIMEOUT_S)
                try:
                    conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
                    resp = conn.getresponse()
                    data = resp.read()
                except (http.client.HTTPException, OSError):
                    conn.close()
                    if attempt:
                        raise
                    continue
                result = (resp.status, data, resp.getheader("Content-Type") or "application/octet-stream",
                          (resp.getheader("Content-Encoding") or "").lower() or None)
                if resp.will_close:
                    conn.close()
                else:
                    with self._lock:
                        self._idle.setdefault(host, []).append(conn)
                return result
        raise OSError("unreachable")


# ── Offline region packs (Sprint 3 item 3) ─────────────────────────────────────
# Which tile source a store key belongs to: (source id, key regex, axis order of the groups).
# A pack declares one source id (metadata "kairos:source"); OpenMapTiles vector extracts are
# recognised without it (format pbf). OpenFreeMap versions its planet path, every version
# shares the OpenMapTiles schema, so one vector pack serves all of them.
SOURCE_PATTERNS = (
    ("openmaptiles", re.compile(r"^tiles\.openfreemap\.org/planet/[^/?]+/(\d+)/(\d+)/(\d+)\.pbf$"), "zxy"),
    ("esri", re.compile(r"^server\.arcgisonline\.com/ArcGIS/rest/services/(.+?)/MapServer/tile/(\d+)/(\d+)/(\d+)$"), "nzyx"),
    ("opentopomap", re.compile(r"^tile\.opentopomap\.org/(\d+)/(\d+)/(\d+)\.png$"), "zxy"),
    ("terrarium", re.compile(r"^elevation-tiles-prod\.s3\.amazonaws\.com/terrarium/(\d+)/(\d+)/(\d+)\.png$"), "zxy"),
)
PACK_FORMATS = {"pbf": "application/x-protobuf", "png": "image/png", "jpg": "image/jpeg",
                "jpeg": "image/jpeg", "webp": "image/webp"}
PACK_SUFFIX = ".mbtiles"
# Region packs for every country / territory and continent (Sprint 4: universal selection).
# world_regions.py is generated (tools/gen_world_regions.py) from GeoNames populated places and
# CLDR names; a region is a list of (west, south, east, north) boxes in WGS84, west > east never
# (parts across the antimeridian are two boxes). Ids: ISO 3166 alpha-2 ("cz", "us") and
# "continent-<id>" ("continent-europe"). A single 4-tuple is accepted too (tests, custom boxes).
CONTINENT_PREFIX = "continent-"


def _build_regions():
    import world_regions
    out = {cc: [tuple(b) for b in boxes] for cc, (_cont, boxes, _names) in world_regions.COUNTRIES.items()}
    for cid in world_regions.CONTINENTS:
        out[CONTINENT_PREFIX + cid] = [tuple(b) for _cc, (cont, boxes, _n) in world_regions.COUNTRIES.items()
                                       if cont == cid for b in boxes]
    return out


REGIONS = _build_regions()
# The whole world at low zoom (Sprint 4): built once into its own pack (WORLD_PACK) so the globe
# is never blank offline from high altitude. Measured 2026-11-05 on the OpenFreeMap planet (bytes
# as stored, gzip): z0-5 = 1365 tiles, 53.5 MB (z2 6.4, z3 8.6, z4 12.2, z5 25.8 MB); z6 would add
# 4096 tiles, ~48 MB (sampled), so z0-6 ~100 MB. Founder decision 2026-11-06: z0-6 (5461 tiles,
# ~100 MB) - highways, ranges and regional towns from altitude; z6 overzooms cleanly to ~7-8.
WORLD_BOX = (-180.0, -85.05112878, 180.0, 85.05112878)
WORLD_PACK = "world-base"
WORLD_ZMAX = 6
# One pack at most this many tiles: OpenFreeMap is a free service, a continent at street level
# (tens of millions of tiles) is a planet download, not a region. Larger -> lower detail.
MAX_REGION_TILES = 1_500_000
DETAIL_ZOOMS = (8, 11, 14)          # overview / roads / streets (the settings' detail choice)
REGION_ZMAX = 14                    # OpenMapTiles' deepest level (MapLibre overzooms beyond)
# Average stored tile size in KB per zoom (measured 2026-11-05 on the real store, OpenFreeMap
# planet), used for size estimates before a download
VECTOR_AVG_KB = {0: 430, 1: 430, 2: 430, 3: 232, 4: 121, 5: 137, 6: 142, 7: 187, 8: 148, 9: 134,
                 10: 95, 11: 53, 12: 30, 13: 17, 14: 11}
MAX_LAT = 85.05112878


def parse_key(key):
    """'host/path' -> (source id, z, x, y) or None. Esri ids carry the service name."""
    for sid, rx, order in SOURCE_PATTERNS:
        m = rx.match(key)
        if not m:
            continue
        g = m.groups()
        if order == "nzyx":
            return f"esri:{g[0]}", int(g[1]), int(g[3]), int(g[2])
        return sid, int(g[0]), int(g[1]), int(g[2])
    return None


def lon2tile(lon, z):
    n = 2 ** z
    return max(0, min(n - 1, int(math.floor((float(lon) + 180.0) / 360.0 * n))))


def lat2tile(lat, z):
    n = 2 ** z
    r = math.radians(max(-MAX_LAT, min(MAX_LAT, float(lat))))
    return max(0, min(n - 1, int(math.floor((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n))))


def bbox_tiles(bbox, z):
    """(x0, y0, x1, y1) tile range covering a (w, s, e, n) box at zoom z."""
    w, s, e, n = bbox
    return lon2tile(w, z), lat2tile(n, z), lon2tile(e, z), lat2tile(s, z)


def region_boxes(region):
    """A REGIONS value (one (w, s, e, n) box or a list of them) -> list of boxes."""
    if not region:
        return []
    if len(region) == 4 and all(isinstance(v, (int, float)) for v in region):
        return [tuple(float(v) for v in region)]
    return [tuple(float(v) for v in b) for b in region]


def region_bounds(region):
    """One (w, s, e, n) box around all parts (MBTiles "bounds" metadata)."""
    boxes = region_boxes(region)
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _zoom_tiles(boxes, z):
    """Distinct (x, y) at zoom z covered by the boxes (several boxes may share tiles)."""
    if len(boxes) == 1:
        x0, y0, x1, y1 = bbox_tiles(boxes[0], z)
        return ((x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
    seen = set()
    for b in boxes:
        x0, y0, x1, y1 = bbox_tiles(b, z)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                seen.add((x, y))
    return sorted(seen, key=lambda t: (t[1], t[0]))


def _zoom_count(boxes, z):
    """Distinct tiles of the boxes at zoom z, exact, without enumerating them: a sweep over the
    rows where the set of boxes changes, times the union length of their column ranges."""
    rects = [bbox_tiles(b, z) for b in boxes]
    if len(rects) == 1:
        x0, y0, x1, y1 = rects[0]
        return (x1 - x0 + 1) * (y1 - y0 + 1)
    edges = sorted({r[1] for r in rects} | {r[3] + 1 for r in rects})
    total = 0
    for top, bottom in zip(edges, edges[1:]):
        spans = sorted((x0, x1) for x0, y0, x1, y1 in rects if y0 <= top and y1 >= bottom - 1)
        width, end = 0, -1
        for x0, x1 in spans:
            if x1 > end:
                width += x1 - max(x0, end + 1) + 1
                end = x1
        total += width * (bottom - top)
    return total


def region_tiles(bbox, zmin=0, zmax=REGION_ZMAX):
    """(z, x, y) of a region (one box or a list), parents first, each tile once."""
    boxes = region_boxes(bbox)
    for z in range(zmin, zmax + 1):
        for x, y in _zoom_tiles(boxes, z):
            yield z, x, y


def estimate_region(bbox, zmax=REGION_ZMAX):
    """(tiles, bytes) of a vector region pack z0..zmax (pure math, safe on the GUI thread).
    Bytes are an upper estimate: the averages are land tiles, open sea is ~100 bytes a tile."""
    boxes = region_boxes(bbox)
    tiles = size = 0
    for z in range(0, zmax + 1):
        n = _zoom_count(boxes, z)
        tiles += n
        size += n * VECTOR_AVG_KB.get(z, 11) * 1024
    return tiles, size


def region_fits(bbox, zmax, cap=None):
    """True when the region's pack z0..zmax stays within MAX_REGION_TILES."""
    cap = MAX_REGION_TILES if cap is None else cap
    tiles = 0
    boxes = region_boxes(bbox)
    for z in range(0, zmax + 1):
        tiles += _zoom_count(boxes, z)
        if tiles > cap:
            return False
    return True


def region_max_detail(bbox, choices=DETAIL_ZOOMS, cap=None):
    """Deepest zoom among choices whose pack fits the tile cap (None if not even the first)."""
    best = None
    for z in sorted(choices):
        if not region_fits(bbox, z, cap):
            break
        best = z
    return best


def packs_dir():
    return paths.MAP_PACKS_DIR                  # read at call time (tests redirect it)


class RegionPack:
    """One MBTiles file, read-only, one SQLite connection per reading thread."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self._local = threading.local()
        self._conns = []
        self._lock = threading.Lock()
        self.closed = False
        try:
            meta = dict(self._conn().execute("SELECT name, value FROM metadata").fetchall())
            self._conn().execute("SELECT 1 FROM tiles LIMIT 1").fetchall()
        except sqlite3.Error:
            self.close()
            raise
        self.meta = meta
        self.format = (meta.get("format") or "").lower()
        self.source = meta.get("kairos:source") or ("openmaptiles" if self.format == "pbf" else "")
        if not self.source or self.format not in PACK_FORMATS:
            self.close()
            raise ValueError(f"unsupported pack (format {self.format!r}, source {self.source!r})")
        self.title = meta.get("kairos:title") or meta.get("name") or self.name
        self.minzoom = int(meta.get("minzoom") or 0)
        self.maxzoom = int(meta.get("maxzoom") or REGION_ZMAX)
        try:
            b = tuple(float(v) for v in (meta.get("bounds") or "").split(","))
            self.bounds = b if len(b) == 4 else None
        except ValueError:
            self.bounds = None
        self.size = os.path.getsize(path)
        self.ctype = PACK_FORMATS[self.format]

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.closed:
                raise sqlite3.ProgrammingError("pack closed")
            uri = "file:" + os.path.abspath(self.path).replace("\\", "/") + "?mode=ro"
            c = sqlite3.connect(uri, uri=True, timeout=5, check_same_thread=False)
            self._local.conn = c
            with self._lock:
                self._conns.append(c)
        return c

    def covers(self, z, x, y):
        if z < self.minzoom or z > self.maxzoom:
            return False
        if self.bounds is None:
            return True
        x0, y0, x1, y1 = bbox_tiles(self.bounds, z)
        return x0 <= x <= x1 and y0 <= y <= y1

    def get(self, z, x, y):
        """(data, ctype, enc) or None. MBTiles rows are TMS (y counted from the south)."""
        if self.closed or not self.covers(z, x, y):
            return None
        try:
            row = self._conn().execute(
                "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
                (z, x, (2 ** z) - 1 - y)).fetchone()
        except sqlite3.Error:
            return None
        if row is None or not row[0]:
            return None
        data = bytes(row[0])
        return data, self.ctype, "gzip" if data[:2] == b"\x1f\x8b" else None

    def info(self):
        return {"name": self.name, "title": self.title, "source": self.source, "format": self.format,
                "minzoom": self.minzoom, "maxzoom": self.maxzoom, "bounds": self.bounds, "bytes": self.size,
                "path": self.path}

    def close(self):
        self.closed = True
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass


class PackManager:
    """Installed region packs (paths.MAP_PACKS_DIR/*.mbtiles). Lookups read an immutable tuple
    (no lock on the tile path); scan / install / remove swap it."""

    def __init__(self, directory=None):
        self.dir = directory or packs_dir()
        self._packs = ()                    # RegionPack, deepest maxzoom first
        self._lock = threading.Lock()
        self.errors = {}                    # file name -> why it was skipped
        self.ready = threading.Event()
        self.hits = 0

    def scan(self):
        """Opens every pack (reads its metadata). Worker thread only, never the GUI thread."""
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.lower().endswith(PACK_SUFFIX))
        except OSError:
            names = []
        with self._lock:
            old = {p.path: p for p in self._packs}
        found, errors = [], {}
        for n in names:
            path = os.path.join(self.dir, n)
            if path in old and os.path.getsize(path) == old[path].size:
                found.append(old.pop(path))
                continue
            try:
                found.append(RegionPack(path))
            except (sqlite3.Error, ValueError, OSError) as e:
                errors[n] = str(e)
        for p in old.values():
            p.close()
        found.sort(key=lambda p: -p.maxzoom)
        with self._lock:
            self._packs = tuple(found)
            self.errors = errors
        self.ready.set()
        return self.list()

    def scan_async(self):
        threading.Thread(target=self.scan, name="tile-packs-scan", daemon=True).start()

    def list(self):
        return [p.info() for p in self._packs]

    def total_bytes(self):
        return sum(p.size for p in self._packs)

    def lookup(self, key):
        packs = self._packs
        if not packs:
            return None
        parsed = parse_key(key)
        if parsed is None:
            return None
        sid, z, x, y = parsed
        for p in packs:
            if p.source == sid:
                got = p.get(z, x, y)
                if got is not None:
                    self.hits += 1
                    return got
        return None

    def has(self, key):
        return self.lookup(key) is not None

    def install(self, src_path):
        """Validates an .mbtiles file and copies it into the packs folder (worker thread).
        -> pack info; ValueError when the file is not a usable pack."""
        try:
            probe = RegionPack(src_path)
        except sqlite3.Error as e:
            raise ValueError(f"not an MBTiles file: {e}") from e
        probe.close()
        os.makedirs(self.dir, exist_ok=True)
        base = re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(src_path))[0]) or "pack"
        dest = os.path.join(self.dir, base + PACK_SUFFIX)
        if os.path.normcase(os.path.abspath(dest)) != os.path.normcase(os.path.abspath(src_path)):
            tmp = dest + ".part"
            shutil.copyfile(src_path, tmp)
            self.remove(base)               # an older copy of the same pack
            os.replace(tmp, dest)
        self.scan()
        return next((p for p in self.list() if p["name"] == base), None)

    def remove(self, name):
        """Deletes an installed pack (its connections close first: Windows locks open files)."""
        with self._lock:
            gone = [p for p in self._packs if p.name == name]
            self._packs = tuple(p for p in self._packs if p.name != name)
        for p in gone:
            p.close()
        path = os.path.join(self.dir, name + PACK_SUFFIX)
        if os.path.exists(path):
            os.remove(path)
            return True
        return bool(gone)

    def close(self):
        with self._lock:
            packs, self._packs = self._packs, ()
        for p in packs:
            p.close()


def write_pack(path, source, fmt, tiles, title="", bounds=None, minzoom=None, maxzoom=None, should_stop=None):
    """Writes an MBTiles pack from an iterable of (z, x, y, data). -> tiles written. The zoom
    range defaults to what was written. Stopped early (should_stop) or failed: no file is left."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    n, zmin, zmax = 0, 99, -1
    try:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        conn.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        conn.execute("CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row)")
        for z, x, y, data in tiles:
            if should_stop is not None and should_stop():
                raise InterruptedError("stopped")
            conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)", (z, x, (2 ** z) - 1 - y, data))
            n += 1
            zmin, zmax = min(zmin, z), max(zmax, z)
            if n % 500 == 0:
                conn.commit()
        meta = {"name": title or os.path.splitext(os.path.basename(path))[0], "format": fmt,
                "kairos:source": source, "kairos:title": title, "type": "baselayer",
                "minzoom": str(minzoom if minzoom is not None else (zmin if n else 0)),
                "maxzoom": str(maxzoom if maxzoom is not None else (zmax if n else 0))}
        if bounds:                      # one box, or a region's list of boxes (their outline)
            meta["bounds"] = ",".join(f"{v:.5f}" for v in region_bounds(bounds))
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
        conn.commit()
        conn.close()
        os.replace(tmp, path)
        return n
    except BaseException:
        conn.close()
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


class TileServer:
    """Místní HTTP server nad TileStore: http://127.0.0.1:<port>/<token>/<host>/<cesta>."""

    def __init__(self, store, hosts=TILE_HOSTS, shards=SHARDS, online=True, redirect_misses=True, packs=None):
        self.store = store
        self.packs = packs                  # PackManager or None (offline region packs)
        # Sprint 3 item 3: callable -> False while background work must wait (the user is
        # interacting: governor busy). urgent / film never wait.
        self.background_gate = None
        self.hosts = frozenset(h.lower() for h in hosts)
        self.token = secrets.token_hex(8)
        self.online = online
        # chybějící dlaždici stáhne Chromium přímo (měřeno: proxy přes HTTP/1.1 zhoršil první
        # přehrání 10,5 -> 15 % děr), úložiště si kopii stáhne samo na pozadí
        self.redirect_misses = redirect_misses
        self.upstream = _Upstream()
        self._inflight = {}
        self._inflight_lock = threading.Lock()
        self._bake_lock = threading.Lock()
        self._bake_gen = 0
        # pop() from the end = next. urgent: flight arrival views + tiles the map is fetching
        # itself; film: the Journey being played; background: regions kept warm
        self._queues = {p: [] for p in PRIORITIES}
        self._bake_threads = 0
        self._bg_active = 0
        self.baking = {"total": 0, "done": 0, "gen": 0}
        self.warming = {p: {"total": 0, "done": 0} for p in ("urgent", "background")}
        self._servers = []
        handler = self._handler_class()
        for _ in range(max(1, shards)):
            srv = _Server(("127.0.0.1", 0), handler)
            srv.tile_server = self
            threading.Thread(target=srv.serve_forever, name="tile-store-http", daemon=True).start()
            self._servers.append(srv)
        self.ports = [s.server_address[1] for s in self._servers]

    def bases(self):
        """Předpony adres pro stránku (index = shard_of(klíč))."""
        return [f"http://127.0.0.1:{p}/{self.token}/" for p in self.ports]

    def config(self):
        return {"bases": self.bases(), "hosts": sorted(self.hosts)}

    def owns(self, host, port):
        return host == "127.0.0.1" and port in self.ports

    def _valid_keys(self, keys):
        return [k for k in keys if isinstance(k, str) and k.partition("/")[0].lower() in self.hosts]

    def _spawn(self):
        """Caller holds _bake_lock. Enough workers for what is queued (at most BAKE_WORKERS)."""
        want = min(BAKE_WORKERS, sum(len(q) for q in self._queues.values()))
        while self._bake_threads < want:
            self._bake_threads += 1
            threading.Thread(target=self._bake_loop, name="tile-store-bake", daemon=True).start()

    def bake(self, keys):
        """Předpečení: stáhne chybějící dlaždice v daném pořadí (BAKE_WORKERS souběžně).
        Nový seznam ruší zbytek předchozího (jiná Cesta). Vrací počet přijatých klíčů."""
        keys = self._valid_keys(keys)
        with self._bake_lock:
            self._bake_gen += 1
            gen = self._bake_gen
            self._queues["film"] = list(reversed(keys))   # pop() z konce = v pořadí filmu
            self.baking = {"total": len(keys), "done": 0, "gen": gen}
            self._spawn()
        return len(keys)

    def warm(self, keys, priority="background"):
        """Keeps tiles on disk ahead of need. keys in order of importance (first = soonest).
        urgent goes ahead of everything (a flight's arrival view). background runs behind the
        film, newest region first, at most BACKGROUND_THREADS workers and BACKGROUND_PER_HOST
        downloads per server. Stored keys are skipped by the workers. Returns how many were taken."""
        if priority not in ("urgent", "background"):
            priority = "background"
        keys = self._valid_keys(keys)[:WARM_MAX_KEYS]
        if not keys:
            return 0
        with self._bake_lock:
            q = self._queues[priority]
            q.extend(reversed(keys))
            if len(q) > BACKGROUND_MAX_QUEUE:
                del q[:len(q) - BACKGROUND_MAX_QUEUE]     # the oldest, least important work
            self.warming[priority]["total"] += len(keys)
            self._spawn()
        return len(keys)

    def persist_async(self, key):
        """Dlaždici, kterou si mapa právě stahuje sama (přesměrování), uložit na pozadí –
        přednostně před vším ostatním (je na obrazovce teď)."""
        with self._bake_lock:
            self._queues["urgent"].append(key)
            self.warming["urgent"]["total"] += 1
            self._spawn()

    def has_tile(self, key):
        """Available without the network: fresh in the store or inside an installed pack."""
        return self.store.has(key) or (self.packs is not None and self.packs.has(key))

    def _pack_hit(self, key):
        got = self.packs.lookup(key) if self.packs is not None else None
        return (200, got[0], got[1], got[2], "pack") if got is not None else None

    def kick(self):
        """Background work may continue (the gate opened again): wake enough workers."""
        with self._bake_lock:
            self._spawn()

    def state(self):
        with self._bake_lock:
            queued = {p: len(q) for p, q in self._queues.items()}
            film = dict(self.baking)
            warming = {p: dict(v) for p, v in self.warming.items()}
            threads = self._bake_threads
        return {"queued": queued, "film": film, "warming": warming, "threads": threads,
                "store": self.store.stats(), "hot": self.store.hot_stats(), "ports": len(self.ports),
                "packs": len(self.packs.list()) if self.packs is not None else 0,
                "pack_hits": self.packs.hits if self.packs is not None else 0}

    def route(self, key):
        """Co udělat s požadavkem mapy: ('serve', výsledek) nebo ('redirect', https adresa).
        Uložená -> z disku; prošlý rastr -> starší kopie hned + obnova na pozadí; právě se
        stahuje -> počkat na to stažení; jinak přesměrovat na server (Chromium ho stáhne přímo
        přes HTTP/2, rychleji než přes tento proxy) a kopii uložit na pozadí."""
        row = self.store.get(key)
        if row is not None and self.store.fresh(row, key):
            self.store.hits += 1
            return "serve", (200, row[0], row[1], row[2], "store")
        hit = self._pack_hit(key)
        if hit is not None:
            return "serve", hit
        if row is not None and self.online:
            self.store.stale += 1
            self.persist_async(key)
            return "serve", (200, row[0], row[1], row[2], "stale")
        with self._inflight_lock:
            busy = key in self._inflight
        if busy or not self.online or not self.redirect_misses:
            return "serve", self.resolve(key)
        self.store.redirects += 1
        self.persist_async(key)
        return "redirect", "https://" + key

    def _next_job(self):
        """Caller holds _bake_lock: (priority, key) or None. Background work only while fewer
        than BACKGROUND_THREADS workers do it, so urgent and film always find a free worker."""
        for p in ("urgent", "film"):
            if self._queues[p]:
                return p, self._queues[p].pop()
        gate = self.background_gate
        if self._queues["background"] and self._bg_active < BACKGROUND_THREADS and (gate is None or gate()):
            self._bg_active += 1
            return "background", self._queues["background"].pop()
        return None

    def _bake_loop(self):
        while True:
            parked = False
            with self._bake_lock:
                job = self._next_job() if self._servers else None
                if job is None:
                    # background work held by the gate (the user is interacting) or by the
                    # BACKGROUND_THREADS cap: up to that many workers wait for it instead of
                    # exiting (nothing else would wake them when the user stops)
                    if self._servers and self._queues["background"] and self._bake_threads <= BACKGROUND_THREADS:
                        parked = True
                    else:
                        self._bake_threads -= 1
                        return
            if parked:
                time.sleep(GATE_POLL_S)
                continue
            prio, key = job
            try:
                if not self.has_tile(key):
                    self.resolve(key, lane="background" if prio == "background" else "bake")
            except Exception:
                pass
            with self._bake_lock:
                if prio == "film":
                    self.baking["done"] += 1
                else:
                    self.warming[prio]["done"] += 1
                if prio == "background":
                    self._bg_active -= 1

    def shutdown(self):
        for s in self._servers:
            try:
                s.shutdown()
                s.server_close()
            except Exception:
                pass
        self._servers = []

    # ── jedna dlaždice ──
    def resolve(self, key, lane="map"):
        """(status, data, ctype, enc, zdroj) pro klíč host/cesta?dotaz."""
        row = self.store.get(key)
        if row is not None and self.store.fresh(row, key):
            self.store.hits += 1
            return 200, row[0], row[1], row[2], "store"
        hit = self._pack_hit(key)
        if hit is not None:
            return hit
        # stejnou dlaždici chce přednačtení i mapa zároveň -> stáhnout jednou
        with self._inflight_lock:
            pending = self._inflight.get(key)
            owner = pending is None
            if owner:
                pending = self._inflight[key] = {"ev": threading.Event(), "r": None}
        if not owner:
            pending["ev"].wait(UPSTREAM_TIMEOUT_S * 2 + 1)
            return pending["r"] if pending["r"] is not None else self._download(key, row, lane)
        result = None
        try:
            result = self._download(key, row, lane)
        finally:
            pending["r"] = result
            with self._inflight_lock:
                self._inflight.pop(key, None)
            pending["ev"].set()
        return result

    def _download(self, key, stale_row, lane="map"):
        host, _, rest = key.partition("/")
        if not self.online:
            if stale_row is not None:
                self.store.stale += 1
                return 200, stale_row[0], stale_row[1], stale_row[2], "stale"
            return 504, b"", "text/plain", None, "offline"
        try:
            status, data, ctype, enc = self.upstream.fetch(host, "/" + rest, lane)
        except OSError:
            if stale_row is not None:           # bez sítě: starší kopie je lepší než díra
                self.store.stale += 1
                return 200, stale_row[0], stale_row[1], stale_row[2], "stale"
            return 502, b"", "text/plain", None, "error"
        self.store.misses += 1
        if status == 200 and data:
            if enc not in (None, "gzip", "identity"):
                return status, data, ctype, enc, "net"     # neznámé kódování neukládat
            if enc == "gzip":
                try:
                    zlib.decompress(data, 47)              # poškozený přenos do úložiště nepatří
                except zlib.error:
                    return 502, b"", "text/plain", None, "error"
            self.store.put(key, data, ctype, enc if enc == "gzip" else None)
        return status, data, ctype, enc, "net"

    def _handler_class(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):      # žádný výpis za každou dlaždici
                pass

            def do_GET(self):
                ts = self.server.tile_server
                parts = urlsplit(self.path)
                token, _, key = parts.path.lstrip("/").partition("/")
                host = key.partition("/")[0].lower()
                if token == ts.token and key == "_state":            # queues, RAM cache (bench, tests)
                    return self._send(200, json.dumps(ts.state()).encode(), "application/json", None)
                if token != ts.token or host not in ts.hosts:
                    return self._send(403, b"", "text/plain", None)
                if parts.query:
                    key += "?" + parts.query
                action, result = ts.route(key)
                if action == "redirect":
                    return self._redirect(result)
                status, data, ctype, enc, source = result
                self._send(status, data, ctype, enc, source)

            def _redirect(self, url):
                try:
                    self.send_response(307)
                    self.send_header("Location", url)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.send_header("X-Tile-Source", "redirect")
                    self.end_headers()
                except (ConnectionError, OSError):
                    pass

            def do_POST(self):
                # /<token>/_bake  body = JSON [key, ...]  (the Journey film, replaces the last one)
                # /<token>/_warm  body = JSON {"keys": [...], "priority": "urgent" | "background"}
                ts = self.server.tile_server
                token, _, rest = urlsplit(self.path).path.lstrip("/").partition("/")
                if token != ts.token or rest not in ("_bake", "_warm"):
                    return self._send(403, b"", "text/plain", None)
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), BAKE_BODY_MAX)
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                except (ValueError, OSError):
                    return self._send(400, b"", "text/plain", None)
                if rest == "_bake":
                    accepted = ts.bake(body if isinstance(body, list) else [])
                else:
                    body = body if isinstance(body, dict) else {}
                    keys = body.get("keys")
                    accepted = ts.warm(keys if isinstance(keys, list) else [], str(body.get("priority") or ""))
                self._send(200, json.dumps({"accepted": accepted}).encode(), "application/json", None)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _send(self, status, data, ctype, enc, source=""):
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype or "application/octet-stream")
                    if enc == "gzip":
                        self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    # Chromium si kopii nedrží – zdrojem pravdy je úložiště (dvojí disk by byl zbytečný)
                    self.send_header("Cache-Control", "no-store")
                    if source:
                        self.send_header("X-Tile-Source", source)
                    self.end_headers()
                    self.wfile.write(data)
                except (ConnectionError, OSError):
                    pass                    # MapLibre požadavek zrušil (kamera odletěla)
        return Handler


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return                          # Chromium zavřel spojení zrušené dlaždice – běžné za letu
        super().handle_error(request, client_address)


_service = None
_service_lock = threading.Lock()


def _user_idle():
    try:
        import governor
        return not governor.get().busy()
    except Exception:                       # noqa: BLE001 – never block tiles on a broken gate
        return True


def start(budget_mb=DEFAULT_BUDGET_MB):
    """Jedna služba za běh aplikace. None, když ji nelze spustit (mapa pak jde přímo na servery)."""
    global _service
    with _service_lock:
        if _service is None:
            try:
                packs = PackManager()
                _service = TileServer(TileStore(budget_mb=budget_mb), packs=packs)
                packs.scan_async()              # pack metadata is read off the calling thread
                # Sprint 3 item 3: background warm-up waits while the user interacts
                _service.background_gate = _user_idle
            except Exception as e:
                print(f"[TileStore] úložiště dlaždic nelze spustit: {e}")
                return None
        else:
            _service.store.set_budget(budget_mb)
    return _service


def service():
    return _service


def stop():
    global _service
    with _service_lock:
        srv, _service = _service, None
    if srv is not None:
        srv.shutdown()
        srv.store.close()
        if srv.packs is not None:
            srv.packs.close()


def region_name(region, lang="en"):
    """Display name of a REGIONS id in the UI language (CLDR names from world_regions)."""
    import world_regions
    lang = lang if lang in ("cs", "de", "en", "es", "fr", "ja", "zh-CN") else "en"
    if region == WORLD_PACK:
        return None
    if region.startswith(CONTINENT_PREFIX):
        names = world_regions.CONTINENTS.get(region[len(CONTINENT_PREFIX):])
    else:
        names = (world_regions.COUNTRIES.get(region) or (None, None, None))[2]
    return (names or {}).get(lang) or (names or {}).get("en") or region


def region_ids():
    """(continent ids, country ids) of the built-in regions."""
    conts = tuple(r for r in REGIONS if r.startswith(CONTINENT_PREFIX))
    return conts, tuple(r for r in REGIONS if not r.startswith(CONTINENT_PREFIX))
