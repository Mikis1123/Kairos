"""C7e: místní úložiště mapových dlaždic (tile_store.py) – SQLite, LRU rozpočet, místní server."""
import gzip
import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import tile_store

ROOT = os.path.dirname(os.path.abspath(__file__))


class FakeUpstream:
    def __init__(self, delay=0.0, fail=False):
        self.calls = []
        self.delay = delay
        self.fail = fail
        self.lock = threading.Lock()

    def fetch(self, host, path, lane="map"):
        with self.lock:
            self.calls.append(host + path)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise OSError("offline")
        if path.endswith(".pbf"):
            return 200, gzip.compress(b"vector:" + path.encode()), "application/x-protobuf", "gzip"
        if "missing" in path:
            return 404, b"", "text/plain", None
        return 200, b"png:" + path.encode(), "image/png", None


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, b"", dict(e.headers)


class TileStoreDbTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tiles_")
        self.store = tile_store.TileStore(os.path.join(self.dir, "t.sqlite"), budget_mb=1)

    def tearDown(self):
        self.store.close()

    def test_put_get_and_size_accounting(self):
        self.store.put("a.org/1/2/3.png", b"x" * 1000, "image/png", None)
        self.store.flush()
        row = self.store.get("a.org/1/2/3.png")
        self.assertEqual(row[0], b"x" * 1000)
        self.assertEqual(row[1], "image/png")
        self.assertEqual(self.store.bytes, 1000)
        self.store.put("a.org/1/2/3.png", b"y" * 400, "image/png", None)      # přepis nepočítat dvakrát
        self.store.flush()
        self.assertEqual(self.store.bytes, 400)

    def test_budget_evicts_least_recently_used(self):
        chunk = b"z" * (200 * 1024)
        for i in range(4):
            self.store.put(f"h/{i}", chunk, "image/png", None)
        self.store.flush()
        self.store._touched.clear()
        time.sleep(0.05)                            # hrubé hodiny Windows: jiný čas než zápis
        self.store.get("h/0")                     # nejstarší, ale právě použitá -> zůstane
        self.store.flush()
        for i in range(4, 7):                       # 7 × 200 kB > 1 MB
            self.store.put(f"h/{i}", chunk, "image/png", None)
        self.store.flush()
        self.assertLessEqual(self.store.bytes, self.store.budget)
        self.assertIsNotNone(self.store.get("h/0"))
        self.assertIsNone(self.store.get("h/1"))
        self.assertIsNotNone(self.store.get("h/6"))

    def test_survives_reopen(self):
        self.store.put("h/k", b"data", "image/png", None)
        self.store.close()
        self.store = tile_store.TileStore(os.path.join(self.dir, "t.sqlite"), budget_mb=1)
        self.assertEqual(self.store.get("h/k")[0], b"data")
        self.assertEqual(self.store.bytes, 4)

    def test_freshness_versioned_vs_raster(self):
        old = time.time() - tile_store.RASTER_TTL_S - 10
        row = (b"", "", None, old)
        self.assertTrue(self.store.fresh(row, "tiles.openfreemap.org/planet/20251015_x/5/1/2.pbf"))
        self.assertTrue(self.store.fresh(row, "elevation-tiles-prod.s3.amazonaws.com/terrarium/9/1/2.png"))
        self.assertFalse(self.store.fresh(row, "server.arcgisonline.com/ArcGIS/rest/x/tile/5/1/2"))


class TileServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tiles_srv_")
        self.store = tile_store.TileStore(os.path.join(self.dir, "t.sqlite"), budget_mb=8)
        # proxy režim (bez přesměrování na skutečné servery – testy nesmí na internet)
        self.srv = tile_store.TileServer(self.store, redirect_misses=False)
        self.up = self.srv.upstream = FakeUpstream()

    def tearDown(self):
        self.srv.shutdown()
        self.store.close()

    def url(self, key):
        return self.srv.bases()[tile_store.shard_of(key)] + key

    def test_miss_then_hit_without_network(self):
        key = "server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/5/10/17"
        s, body, h = get(self.url(key))
        self.assertEqual((s, h.get("X-Tile-Source")), (200, "net"))
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(h.get("Cache-Control"), "no-store")
        self.store.flush()
        s, body2, h = get(self.url(key))
        self.assertEqual((s, h.get("X-Tile-Source"), body2), (200, "store", body))
        self.assertEqual(len(self.up.calls), 1)

    def test_gzip_vector_tile_passes_through_encoded(self):
        key = "tiles.openfreemap.org/planet/20251015_001001_pt/9/277/173.pbf"
        get(self.url(key))
        self.store.flush()
        row = self.store.get(key)
        self.assertEqual(row[2], "gzip")
        self.assertEqual(gzip.decompress(row[0]), b"vector:/planet/20251015_001001_pt/9/277/173.pbf")
        s, body, h = get(self.url(key))
        self.assertEqual(h.get("Content-Encoding"), "gzip")

    def test_rejects_wrong_token_and_foreign_host(self):
        port = self.srv.ports[0]
        self.assertEqual(get(f"http://127.0.0.1:{port}/deadbeef/tile.opentopomap.org/1/1/1.png")[0], 403)
        self.assertEqual(get(self.srv.bases()[0] + "evil.example.com/1/1/1.png")[0], 403)
        self.assertEqual(self.up.calls, [])

    def test_upstream_404_is_not_stored(self):
        key = "tile.opentopomap.org/missing/1/1.png"
        self.assertEqual(get(self.url(key))[0], 404)
        self.store.flush()
        self.assertIsNone(self.store.get(key))

    def test_concurrent_requests_download_once(self):
        self.up.delay = 0.3
        key = "tile.opentopomap.org/12/2200/1400.png"
        out = []
        threads = [threading.Thread(target=lambda: out.append(get(self.url(key))[0])) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(out, [200] * 5)
        self.assertEqual(len(self.up.calls), 1)

    def test_offline_serves_stale_copy(self):
        key = "server.arcgisonline.com/x/tile/3/1/1"
        self.store.put(key, b"old", "image/png", None)
        self.store.flush()
        self.store._conn().execute("UPDATE tiles SET fetched = 0")
        self.store._conn().commit()
        self.up.fail = True
        s, body, h = get(self.url(key))
        self.assertEqual((s, body, h.get("X-Tile-Source")), (200, b"old", "stale"))
        self.assertEqual(get(self.url("server.arcgisonline.com/x/tile/3/9/9"))[0], 502)

    def test_bake_downloads_list_in_order_and_skips_stored(self):
        self.store.put("tile.opentopomap.org/1/0/0.png", b"have", "image/png", None)
        self.store.flush()
        self.up.delay = 0.05                        # síť něco trvá: dělníci se nepředbíhají
        keys =[f"tile.opentopomap.org/9/{x}/1.png" for x in range(30)] + ["tile.opentopomap.org/1/0/0.png",
                                                                            "evil.example.com/1/1/1.png"]
        body = json.dumps(keys).encode()
        req = urllib.request.Request(self.srv.bases()[0] + "_bake", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(json.loads(r.read())["accepted"], 31)          # cizí host vyřazen
        deadline = time.time() + 10
        while self.srv.baking["done"] < 31 and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.srv.baking["done"], 31)
        self.assertEqual(len(self.up.calls), 30)                            # uložená se nestahuje
        # pořadí filmu: první dávka souběžných stažení = prvních BAKE_WORKERS klíčů
        n = tile_store.BAKE_WORKERS
        self.assertEqual(set(self.up.calls[:n]), set(keys[:n]))
        self.store.flush()
        self.assertIsNotNone(self.store.get("tile.opentopomap.org/9/29/1.png"))

    def test_miss_redirects_to_origin_and_persists_in_background(self):
        import http.client
        self.srv.redirect_misses = True
        key = "tile.opentopomap.org/11/1100/700.png"
        port = self.srv.ports[tile_store.shard_of(key)]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)    # nesleduje přesměrování
        conn.request("GET", f"/{self.srv.token}/{key}")
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 307)
        self.assertEqual(r.getheader("Location"), "https://" + key)
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")
        deadline = time.time() + 5
        while self.store.get(key) is None and time.time() < deadline:
            self.store.flush()
            time.sleep(0.05)
        self.assertIsNotNone(self.store.get(key))                           # příště z disku
        conn.request("GET", f"/{self.srv.token}/{key}")
        r = conn.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("X-Tile-Source")), (200, "store"))
        conn.close()

    def test_expired_raster_served_stale_and_refreshed(self):
        key = "server.arcgisonline.com/x/tile/4/2/2"
        self.store.put(key, b"old", "image/png", None)
        self.store.flush()
        self.store._conn().execute("UPDATE tiles SET fetched = 0")
        self.store._conn().commit()
        s, body, h = get(self.url(key))
        self.assertEqual((s, body, h.get("X-Tile-Source")), (200, b"old", "stale"))
        deadline = time.time() + 5
        while (self.store.get(key) or (b"old",))[0] == b"old" and time.time() < deadline:
            self.store.flush()
            time.sleep(0.05)
        self.assertEqual(self.store.get(key)[0], b"png:/x/tile/4/2/2")

    def test_bake_requires_token(self):
        port = self.srv.ports[0]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/bad/_bake", data=b"[]", method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("bez tokenu nesmí projít")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_shards_spread_and_match_page_formula(self):
        self.assertEqual(len(self.srv.ports), tile_store.SHARDS)
        keys = [f"tiles.openfreemap.org/planet/v/{z}/{x}/{y}.pbf" for z in (8, 9) for x in range(4) for y in range(4)]
        self.assertEqual(len({tile_store.shard_of(k) for k in keys}), tile_store.SHARDS)
        html = open(os.path.join(ROOT, "map_vault.html"), encoding="utf-8").read()
        # storeUrl(): součet charCodeAt klíče mod počet portů – musí sedět se shard_of
        self.assertRegex(html, r"sum \+= key\.charCodeAt\(i\)")
        self.assertIn("TILE_STORE.bases[sum % TILE_STORE.bases.length] + key", html)


class TileStoreWiringTest(unittest.TestCase):
    def test_page_routes_tiles_and_prefetch_through_store(self):
        html = open(os.path.join(ROOT, "map_vault.html"), encoding="utf-8").read()
        self.assertIn("transformRequest: transformRequest", html)
        self.assertIn("fetch(storeUrl(url))", html)
        self.assertIn("setTileStore(window.TILE_STORE_CONFIG)", html)
        self.assertIn("journey.baked = bakeFilm()", html)                   # celý film předem
        self.assertTrue(re.search(r"if \(type !== 'Tile'\) return undefined", html))

    def test_tile_hosts_are_allowed_hosts(self):
        import main
        self.assertTrue(tile_store.TILE_HOSTS <= main.ALLOWED_WEB_HOSTS)

    def test_interceptor_allows_only_store_ports(self):
        import main
        srv = tile_store.start(16)
        try:
            self.assertTrue(main.is_allowed_web_url(srv.bases()[0] + "tile.opentopomap.org/1/1/1.png"))
            self.assertFalse(main.is_allowed_web_url("http://127.0.0.1:1/x"))
            self.assertFalse(main.is_allowed_web_url("http://example.com/x"))
        finally:
            tile_store.stop()
        self.assertFalse(main.is_allowed_web_url("http://127.0.0.1:%d/x" % srv.ports[0]))

    def test_default_budget_is_one_gigabyte(self):
        import config
        self.assertEqual(config.DEFAULT_CONFIG["tile_store_mb"], 1024)
        self.assertEqual(tile_store.DEFAULT_BUDGET_MB, 1024)


if __name__ == "__main__":
    unittest.main()
