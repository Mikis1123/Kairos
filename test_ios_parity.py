"""Parity sprint (2026-11-10): LAN bridge throughput lanes, semantic search, content matching for
the phone's three-state library, and Trip mode journeys.

  * a burst far larger than the interactive lane queues instead of answering 503 "busy"
  * bulk transfers (packs, bulk originals, uploads) have their own lane
  * /v1/search returns visible ids only, through the desktop's provider when it is installed
  * /v1/match proves "the desktop already has this phone asset" by SHA-256: through the import
    journal, then by hashing same-size library files (cached), and a Live motion half resolves
    to its still
  * /v1/journey carries stops, legs and days without any file path or URL
"""
import hashlib
import http.client
import json
import os
import ssl
import threading
import time
import unittest
from unittest import mock

import database
import sync_server as ss
import test_ios_sync


class _Parity(test_ios_sync.TestServer):
    """The server fixture of test_ios_sync; its own tests run there, not again here."""


for _name in [n for n in dir(test_ios_sync.TestServer) if n.startswith("test_")]:
    setattr(_Parity, _name, None)


class TestLanes(_Parity):
    def test_lane_classification(self):
        self.assertEqual(ss.SyncServer.lane("GET", ["v1", "packs", "cz"], {}), "bulk")
        self.assertEqual(ss.SyncServer.lane("POST", ["v1", "import"], {}), "bulk")
        self.assertEqual(ss.SyncServer.lane("GET", ["v1", "media", "1", "original"], {"bulk": "1"}), "bulk")
        self.assertEqual(ss.SyncServer.lane("GET", ["v1", "media", "1", "thumb"], {}), "interactive")
        self.assertEqual(ss.SyncServer.lane("GET", ["v1", "packs"], {}), "interactive")
        self.assertEqual(ss.SyncServer.lane("GET", ["v1", "search"], {"q": "dog"}), "interactive")

    def test_burst_queues_instead_of_busy(self):
        """Three times the lane's width at once, every request is answered 200."""
        self.pair()
        statuses, lock = [], threading.Lock()
        real = self.srv._media_row

        def slow_row(media_id):
            time.sleep(0.05)                         # hold the slot like a slow disk would
            return real(media_id)

        def one():
            r, body, _ = self.req("GET", "/v1/media/1/thumb")
            with lock:
                statuses.append(r.status)

        with mock.patch.object(self.srv, "_media_row", slow_row):
            threads = [threading.Thread(target=one) for _ in range(ss.INTERACTIVE_SLOTS * 3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(len(statuses), ss.INTERACTIVE_SLOTS * 3)
        self.assertEqual(set(statuses), {200}, statuses)
        self.assertGreater(self.srv.stats["waited"], 0)        # the lane really filled and queued
        self.assertEqual(self.srv.stats["busy"], 0)

    def test_full_lane_times_out_with_busy(self):
        self.pair()
        lane = self.srv._lanes["interactive"]
        for _ in range(ss.INTERACTIVE_SLOTS):
            lane.acquire()
        try:
            with mock.patch.object(ss, "ADMIT_WAIT_S", 0.2):
                r, body, _ = self.req("GET", "/v1/media/1/thumb")
            self.assertEqual((r.status, r.getheader("Retry-After")), (503, "1"))
            # the bulk lane is independent: a pack download still starts
            r, _, _ = self.req("HEAD", "/v1/packs/cz")
            self.assertEqual(r.status, 200)
        finally:
            for _ in range(ss.INTERACTIVE_SLOTS):
                lane.release()

    def test_keep_alive_reuses_one_connection(self):
        self.pair()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection("127.0.0.1", self.srv.port, context=ctx, timeout=10)
        try:
            socks = set()
            t0 = time.perf_counter()
            for _ in range(20):
                conn.request("GET", "/v1/media/1/thumb", headers={"Authorization": "Bearer " + self.token})
                r = conn.getresponse()
                r.read()
                self.assertEqual(r.status, 200)
                socks.add(id(conn.sock))
            per_req = (time.perf_counter() - t0) / 20
        finally:
            conn.close()
        self.assertEqual(len(socks), 1)                         # no reconnect, no new TLS handshake
        self.assertLess(per_req, 0.035, "keep-alive reply stalled (Nagle / delayed ACK?)")

    def test_hello_lists_features(self):
        r, body, _ = self.req("GET", "/v1/hello")
        self.assertEqual(set(json.loads(body)["features"]), set(ss.FEATURES))


class TestSearch(_Parity):
    def test_provider_results_are_filtered_to_visible(self):
        self.pair()
        seen = []

        def provider(q, limit):
            seen.append((q, limit))
            return {"ids": [4, 2, 1, 999], "confident": 2, "semantic": True, "labels": ["pes"]}

        self.srv.search_fn = provider
        r, body, _ = self.req("GET", "/v1/search?q=pes%20na%20pl%C3%A1%C5%BEi&limit=50")
        out = json.loads(body)
        self.assertEqual(r.status, 200)
        self.assertEqual(seen, [("pes na pláži", 50)])
        self.assertEqual(out["ids"], [2, 1])                    # 4 is hidden, 999 does not exist
        self.assertTrue(out["semantic"])
        self.assertLessEqual(out["confident"], len(out["ids"]))

    def test_empty_query_and_provider_error(self):
        self.pair()
        self.assertEqual(json.loads(self.req("GET", "/v1/search?q=")[1])["ids"], [])

        def boom(q, limit):
            raise RuntimeError("model missing")

        self.srv.search_fn = boom
        r, body, _ = self.req("GET", "/v1/search?q=dog")
        self.assertEqual((r.status, json.loads(body)["ids"]), (200, []))

    def test_cold_provider_falls_back_to_bridge_vectors(self):
        self.pair()
        self.srv.search_fn = lambda q, limit: None
        with mock.patch.object(self.srv, "_search_default",
                               return_value={"ids": [1], "confident": 1, "semantic": True, "labels": []}) as d:
            out = json.loads(self.req("GET", "/v1/search?q=dog")[1])
        d.assert_called_once()
        self.assertEqual(out["ids"], [1])

    def test_module_provider_hook(self):
        self.pair()
        try:
            ss.set_search_provider(lambda q, limit: {"ids": [1], "confident": 1, "semantic": False, "labels": []})
            self.assertEqual(json.loads(self.req("GET", "/v1/search?q=x")[1])["ids"], [1])
        finally:
            ss.set_search_provider(None)

    def test_search_requires_auth(self):
        self.assertEqual(self.req("GET", "/v1/search?q=dog", auth=False)[0].status, 401)


class TestMatch(_Parity):
    def sha(self, path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    def test_hash_of_same_size_file_matches_and_is_cached(self):
        self.pair()
        digest = self.sha(self.photo)
        r, body, _ = self.req("POST", "/v1/match", {"assets": [
            {"sha256": digest, "size": 10240, "name": "img_0001.jpg"},
            {"sha256": "0" * 64, "size": 10240, "name": "other.jpg"},
            {"sha256": "nope", "size": 1},
        ]})
        self.assertEqual(r.status, 200, body)
        self.assertEqual(json.loads(body)["matches"], {digest: 1})
        # second time: answered from sync/hashes.sqlite, no file read
        with mock.patch.object(self.srv, "_file_sha", side_effect=AssertionError("re-hashed")):
            self.assertEqual(json.loads(self.req("POST", "/v1/match", {"assets": [
                {"sha256": digest, "size": 10240, "name": "x"}]})[1])["matches"], {digest: 1})

    def test_changed_file_is_rehashed(self):
        self.pair()
        old = self.sha(self.photo)
        self.assertEqual(json.loads(self.req("POST", "/v1/match", {"assets": [
            {"sha256": old, "size": 10240}]})[1])["matches"], {old: 1})
        with open(self.photo, "wb") as fh:
            fh.write(b"\x01" * 10240)
        st = os.stat(self.photo)
        os.utime(self.photo, (st.st_atime, st.st_mtime + 5))
        out = json.loads(self.req("POST", "/v1/match", {"assets": [
            {"sha256": old, "size": 10240}, {"sha256": self.sha(self.photo), "size": 10240}]})[1])["matches"]
        self.assertEqual(out, {self.sha(self.photo): 1})

    def test_import_journal_and_live_motion(self):
        self.pair()
        motion = os.path.join(self.tmp, "IMG_0002.MOV")
        with open(motion, "wb") as fh:
            fh.write(b"motion" * 100)
        # the motion half belongs to item 2 (its still), even though the MOV is also row 3
        conn = database.get_connection()
        with conn:
            conn.execute("DELETE FROM media_items WHERE id = 3")
        conn.close()
        out = json.loads(self.req("POST", "/v1/match", {"assets": [
            {"sha256": self.sha(motion), "size": 600, "name": "IMG_0002.MOV"}]})[1])["matches"]
        self.assertEqual(out, {self.sha(motion): 2})
        # journal: bytes the phone uploaded resolve without hashing
        inbox_file = os.path.join(self.srv.inbox_dir, "2026-07", "IMG_0001.JPG")
        os.makedirs(os.path.dirname(inbox_file), exist_ok=True)
        conn = database.get_connection()
        with conn:
            conn.execute("UPDATE media_items SET file_path = ? WHERE id = 1", (inbox_file,))
        conn.close()
        iconn = self.srv._imports_db()
        with iconn:
            iconn.execute("INSERT INTO imports VALUES (?, ?, 'dev1', '')", ("a" * 64, os.path.join("2026-07", "IMG_0001.JPG")))
        iconn.close()
        with mock.patch.object(self.srv, "_file_sha", side_effect=AssertionError("hashed")):
            out = json.loads(self.req("POST", "/v1/match", {"assets": [{"sha256": "a" * 64, "size": 1}]})[1])
        self.assertEqual(out["matches"], {"a" * 64: 1})

    def test_hidden_items_never_match(self):
        self.pair()
        secret = os.path.join(self.tmp, "secret.jpg")
        with open(secret, "wb") as fh:
            fh.write(b"s" * 77)
        out = json.loads(self.req("POST", "/v1/match", {"assets": [
            {"sha256": self.sha(secret), "size": 77, "name": "secret.jpg"}]})[1])
        self.assertEqual(out["matches"], {})

    def test_bad_body(self):
        self.pair()
        self.assertEqual(self.req("POST", "/v1/match", {"nope": 1})[0].status, 400)


class TestJourney(_Parity):
    def test_journey_has_no_paths(self):
        self.pair()
        conn = database.get_connection()
        with conn:
            for i, (lat, lon, t) in enumerate([(49.0, 13.5, "10:00"), (49.2, 13.9, "13:00"),
                                               (49.5, 14.4, "17:00")], start=10):
                path = os.path.join(self.tmp, f"j{i}.jpg")
                conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, latitude, "
                             "longitude, event_id, thumbnail_path) VALUES (?, ?, ?, 'photo', ?, ?, ?, 1, ?)",
                             (i, path, f"j{i}.jpg", f"2026:07:02 {t}:00", lat, lon, path))
        conn.close()
        r, body, _ = self.req("GET", "/v1/journey/1")
        self.assertEqual(r.status, 200, body)
        out = json.loads(body)
        self.assertGreaterEqual(len(out["stops"]), 2)
        stop = out["stops"][0]
        self.assertTrue({"lat", "lon", "start", "end", "count", "best", "photos"} <= set(stop))
        self.assertNotIn(self.tmp.replace("\\", "\\\\"), body.decode())
        self.assertNotIn("thumb", body.decode())
        times = [s["start"] for s in out["stops"]]
        self.assertEqual(times, sorted(times))

    def test_unknown_journey(self):
        self.pair()
        self.assertEqual(self.req("GET", "/v1/journey/999")[0].status, 404)


if __name__ == "__main__":
    unittest.main()
