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
"""
import http.client
import http.server
import json
import os
import queue
import secrets
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
        self.bytes = conn.execute("SELECT COALESCE(SUM(size), 0) FROM tiles").fetchone()[0]
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

    def clear(self):
        done = threading.Event()
        self._writes.put(("clear", done))
        done.wait(10)

    def _write_loop(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=NORMAL")
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
            try:
                for op in batch:
                    kind = op[0]
                    if kind == "put":
                        _, key, data, ctype, enc, now = op
                        old = conn.execute("SELECT size FROM tiles WHERE key = ?", (key,)).fetchone()
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
                    elif kind == "sync":
                        events.append(op[1])
                if self.bytes > self.budget:
                    self._evict(conn)
                conn.commit()
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
            rows = conn.execute("SELECT key, size FROM tiles ORDER BY used LIMIT 500").fetchall()
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


class TileServer:
    """Místní HTTP server nad TileStore: http://127.0.0.1:<port>/<token>/<host>/<cesta>."""

    def __init__(self, store, hosts=TILE_HOSTS, shards=SHARDS, online=True, redirect_misses=True):
        self.store = store
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

    def state(self):
        with self._bake_lock:
            queued = {p: len(q) for p, q in self._queues.items()}
            film = dict(self.baking)
            warming = {p: dict(v) for p, v in self.warming.items()}
            threads = self._bake_threads
        return {"queued": queued, "film": film, "warming": warming, "threads": threads,
                "store": self.store.stats(), "hot": self.store.hot_stats(), "ports": len(self.ports)}

    def route(self, key):
        """Co udělat s požadavkem mapy: ('serve', výsledek) nebo ('redirect', https adresa).
        Uložená -> z disku; prošlý rastr -> starší kopie hned + obnova na pozadí; právě se
        stahuje -> počkat na to stažení; jinak přesměrovat na server (Chromium ho stáhne přímo
        přes HTTP/2, rychleji než přes tento proxy) a kopii uložit na pozadí."""
        row = self.store.get(key)
        if row is not None and self.store.fresh(row, key):
            self.store.hits += 1
            return "serve", (200, row[0], row[1], row[2], "store")
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
        if self._queues["background"] and self._bg_active < BACKGROUND_THREADS:
            self._bg_active += 1
            return "background", self._queues["background"].pop()
        return None

    def _bake_loop(self):
        while True:
            with self._bake_lock:
                job = self._next_job() if self._servers else None
                if job is None:
                    self._bake_threads -= 1
                    return
            prio, key = job
            try:
                if not self.store.has(key):
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


def start(budget_mb=DEFAULT_BUDGET_MB):
    """Jedna služba za běh aplikace. None, když ji nelze spustit (mapa pak jde přímo na servery)."""
    global _service
    with _service_lock:
        if _service is None:
            try:
                _service = TileServer(TileStore(budget_mb=budget_mb))
            except Exception as e:
                print(f"[TileStore] úložiště dlaždic nelze spustit: {e}")
                return None
        else:
            _service.store.budget = int(budget_mb) * 1024 * 1024
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
