"""iOS Phase 1: LAN bridge (sync_server.py) and the cross-language contract fixtures.

The fixtures in ios/KairosCore/Tests/KairosCoreTests/Fixtures are the shared truth between this
server and the Swift replica: the Swift tests decode the same change page and reproduce the same
edit LUTs. Regenerate with KAIROS_REGEN_FIXTURES=1 after an intentional contract change.
"""
import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import struct
import tempfile
import time
import unittest

import database
import power
import quick_edit as qe
import sync_server as ss

ROOT = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(ROOT, "ios", "KairosCore", "Tests", "KairosCoreTests", "Fixtures")
REGEN = os.environ.get("KAIROS_REGEN_FIXTURES") == "1"


class _Library(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-sync-")
        self._old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.photo = os.path.join(self.tmp, "IMG_0001.JPG")
        with open(self.photo, "wb") as fh:
            fh.write(bytes(range(256)) * 40)            # 10 240 bytes, Range-checkable
        self.thumb = os.path.join(self.tmp, "t1.jpg")
        with open(self.thumb, "wb") as fh:
            fh.write(b"\xff\xd8thumb\xff\xd9")
        conn = database.get_connection()
        with conn:
            conn.execute("INSERT INTO events (id, title, start_date, end_date, item_count, kind) "
                         "VALUES (1, 'Šumava', '2026-07-01', '2026-07-03', 2, 'journey')")
            conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, latitude, "
                         "longitude, event_id, thumbnail_path, fingerprint, file_size, is_favorite) VALUES "
                         "(1, ?, 'IMG_0001.JPG', 'photo', '2026:07:01 10:00:00', 48.97, 13.6, 1, ?, 'fp1', 10240, 1)",
                         (self.photo, self.thumb))
            conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, date_taken, paired_video_path) "
                         "VALUES (2, ?, 'IMG_0002.HEIC', 'photo', '2026:07:01 11:00:00', ?)",
                         (os.path.join(self.tmp, "IMG_0002.HEIC"), os.path.join(self.tmp, "IMG_0002.MOV")))
            conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type) VALUES (3, ?, 'IMG_0002.MOV', 'video')",
                         (os.path.join(self.tmp, "IMG_0002.MOV"),))
            conn.execute("INSERT INTO media_items (id, file_path, file_name, media_type, is_hidden) "
                         "VALUES (4, ?, 'secret.jpg', 'photo', 1)", (os.path.join(self.tmp, "secret.jpg"),))
            conn.execute("INSERT INTO people (id, name, is_confirmed) VALUES (1, 'Anna', 1)")
            conn.execute("INSERT INTO face_instances (id, media_id, person_id, embedding, bbox_x, bbox_y, bbox_w, "
                         "bbox_h, quality_score, is_valid) VALUES (1, 1, 1, x'00', 0.1, 0.2, 0.3, 0.4, 0.9, 1)")
            conn.execute("INSERT INTO face_instances (id, media_id, person_id, embedding, is_valid) "
                         "VALUES (2, 4, 1, x'00', 1)")
            conn.execute("INSERT INTO albums (id, name, kind, position) VALUES (1, 'Best', 'manual', 0)")
            conn.execute("INSERT INTO album_items (album_id, media_id, position) VALUES (1, 1, 0)")
            conn.execute("INSERT INTO album_items (album_id, media_id, position) VALUES (1, 4, 1)")
            qe.save(conn, 1, qe.Adjustments(exposure=25, crop=(0.1, 0.1, 0.8, 0.8)))
        conn.close()

    def tearDown(self):
        database.DB_PATH = self._old_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conn(self):
        c = database.get_connection()
        c.isolation_level = None
        return c


class TestJournal(_Library):
    def setUp(self):
        super().setUp()
        c = database.get_connection()
        with c:
            self.assertTrue(ss.ensure_schema(c))
            self.assertFalse(ss.ensure_schema(c))           # idempotent
        c.close()

    def page(self, since=0, limit=500):
        c = self.conn()
        try:
            return ss.changes(c, since, limit)
        finally:
            c.close()

    def test_backfill_tombstones_and_privacy(self):
        p = self.page()
        self.assertFalse(p["more"])
        by = {(ch["entity"], ch["id"]): ch for ch in p["changes"]}
        self.assertEqual(by[("media", 1)]["op"], "upsert")
        m = by[("media", 1)]["data"]
        self.assertTrue(m["has_thumb"] and m["is_favorite"] and not m["live"])
        self.assertTrue(by[("media", 2)]["data"]["live"])
        self.assertEqual(by[("media", 3)]["op"], "delete")      # Live motion half
        self.assertEqual(by[("media", 4)]["op"], "delete")      # vault
        self.assertEqual(by[("face", 2)]["op"], "delete")       # face on a vault photo
        self.assertEqual(by[("album", 1)]["data"]["items"], [1])
        self.assertEqual(by[("edit", 1)]["data"]["params"]["exposure"], 25.0)
        blob = json.dumps(p)
        self.assertNotIn(self.tmp.replace("\\", "\\\\"), blob)  # no absolute paths on the wire
        self.assertNotIn("secret.jpg", blob)

    def test_only_synced_columns_bump(self):
        top = self.page()["cursor"]
        c = self.conn()
        c.execute("UPDATE media_items SET phash = 'abc', faces_scanned = 1, sharpness_score = 3 WHERE id = 1")
        c.execute("UPDATE media_items SET is_favorite = 1 WHERE id = 1")          # unchanged value
        self.assertEqual(self.page(top)["changes"], [])
        c.execute("UPDATE media_items SET is_favorite = 0 WHERE id = 1")
        c.close()
        ch = self.page(top)["changes"]
        self.assertEqual([(x["entity"], x["id"]) for x in ch], [("media", 1)])
        self.assertFalse(ch[0]["data"]["is_favorite"])

    def test_hide_and_unhide_round_trip(self):
        top = self.page()["cursor"]
        c = self.conn()
        c.execute("UPDATE media_items SET is_hidden = 1 WHERE id = 1")
        p = self.page(top)
        ops = {(x["entity"], x["id"]): x["op"] for x in p["changes"]}
        self.assertEqual(ops[("media", 1)], "delete")
        c.execute("UPDATE media_items SET is_hidden = 0 WHERE id = 1")
        c.close()
        ops = {(x["entity"], x["id"]): x["op"] for x in self.page(p["cursor"])["changes"]}
        self.assertEqual(ops[("media", 1)], "upsert")
        self.assertEqual(ops[("face", 1)], "upsert")
        self.assertEqual(ops[("edit", 1)], "upsert")
        self.assertEqual(ops[("album", 1)], "upsert")

    def test_delete_and_insert_or_ignore(self):
        top = self.page()["cursor"]
        c = self.conn()
        c.execute("DELETE FROM album_items WHERE album_id = 1 AND media_id = 1")
        c.execute("INSERT OR IGNORE INTO events (id, title) VALUES (2, 'New')")   # outer OR IGNORE
        c.execute("DELETE FROM media_edits WHERE media_id = 1")
        c.close()
        got = {(x["entity"], x["id"]): x for x in self.page(top)["changes"]}
        self.assertEqual(got[("album", 1)]["data"]["items"], [])
        self.assertEqual(got[("event", 2)]["data"]["title"], "New")
        self.assertEqual(got[("edit", 1)]["op"], "delete")

    def test_pagination_is_lossless_and_reset(self):
        everything = {(x["entity"], x["id"]) for x in self.page()["changes"]}
        seen, cur = set(), 0
        while True:
            p = self.page(cur, limit=3)
            seen |= {(x["entity"], x["id"]) for x in p["changes"]}
            cur = p["cursor"]
            if not p["more"]:
                break
        self.assertEqual(seen, everything)
        self.assertTrue(self.page(cur + 1000)["reset"])

    def test_micro_frame_round_trip(self):
        items = [(1, b"\xff\xd8a"), (70000, b"")]
        self.assertEqual(ss.unpack_micro(ss.pack_micro(items)), items)

    def test_contract_fixture(self):
        """The change page the Swift replica decodes (Fixtures/sync_changes_v1.json)."""
        p = self.page()
        p["library_id"] = "fixture"
        for ch in p["changes"]:                    # strip machine-specific values
            if ch["entity"] == "edit" and ch.get("data"):
                ch["data"]["updated_at"] = "2026-11-06 12:00:00"
        path = os.path.join(FIXTURES, "sync_changes_v1.json")
        text = json.dumps(p, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        if REGEN or not os.path.exists(path):
            os.makedirs(FIXTURES, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), json.loads(text), "contract changed: regenerate + update Swift")


EDIT_CASES = [
    {"exposure": 40},
    {"temperature": -60, "contrast": 35},
    {"highlights": -80, "shadows": 70},
    {"exposure": -100, "highlights": 100, "shadows": -100, "temperature": 100, "contrast": -100},
    {"angle": 7.5, "crop": [0.1, 0.05, 0.7, 0.8], "exposure": 12.5},
]


class TestEditContract(unittest.TestCase):
    def test_edit_fixture(self):
        """LUTs + inverse warp the Swift ImagePipeline must reproduce (±1 level, 1e-9 matrix)."""
        cases = []
        for d in EDIT_CASES:
            adj = qe.Adjustments.from_dict(d)
            lut = qe.build_lut(adj, "RGB").reshape(256, 3)
            cases.append({"input": d, "normalized": adj.to_dict(), "size": [4032, 3024],
                          "lut_r": lut[:, 0].tolist(), "lut_g": lut[:, 1].tolist(), "lut_b": lut[:, 2].tolist(),
                          "inverse_matrix": [round(v, 10) for v in qe.inverse_matrix(4032, 3024, adj).ravel().tolist()],
                          "output_size": list(qe.output_size(4032, 3024, adj))})
        doc = {"edit_version": qe.EDIT_VERSION, "cases": cases}
        path = os.path.join(FIXTURES, "edit_contract_v1.json")
        if REGEN or not os.path.exists(path):
            os.makedirs(FIXTURES, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(doc, fh, indent=1, sort_keys=True)
                fh.write("\n")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), json.loads(json.dumps(doc)), "edit math changed: regenerate + port")


class TestServer(_Library):
    def setUp(self):
        super().setUp()
        self.packs = os.path.join(self.tmp, "packs")
        os.makedirs(self.packs)
        import sqlite3
        pk = sqlite3.connect(os.path.join(self.packs, "cz.mbtiles"))
        pk.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        pk.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
        pk.executemany("INSERT INTO metadata VALUES (?, ?)", [("name", "Czechia"), ("format", "pbf"),
                                                               ("minzoom", "0"), ("maxzoom", "14")])
        pk.commit()
        pk.close()
        self.srv = ss.SyncServer(host="127.0.0.1", port=0, db_path=database.DB_PATH,
                                 state_dir=os.path.join(self.tmp, "sync"), packs_dir=self.packs,
                                 micro_source=lambda ids: [(i, b"\xff\xd8m%d" % i) for i in ids])
        self.token = None

    def tearDown(self):
        self.srv.shutdown()
        power.get().override = power.PowerState()
        super().tearDown()

    def ctx(self):
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE                 # pinning by fingerprint instead (as on iOS)
        return c

    def req(self, method, path, body=None, headers=None, auth=True):
        conn = http.client.HTTPSConnection("127.0.0.1", self.srv.port, context=self.ctx(), timeout=10)
        h = dict(headers or {})
        if auth and self.token:
            h["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        der = conn.sock.getpeercert(binary_form=True) if conn.sock else None
        conn.close()
        return r, payload, der

    def pair(self):
        pin = self.srv.credentials.begin_pairing()
        r, body, _ = self.req("POST", "/v1/pair", {"pin": pin, "device_name": "Test iPhone", "device_id": "dev1"})
        self.assertEqual(r.status, 200, body)
        self.token = json.loads(body)["token"]

    def test_hello_pinning_and_auth(self):
        r, body, der = self.req("GET", "/v1/hello")
        self.assertEqual(r.status, 200)
        self.assertEqual(hashlib.sha256(der).hexdigest(), self.srv.fingerprint)
        self.assertEqual(json.loads(body)["protocol"], ss.PROTOCOL)
        self.assertEqual(self.req("GET", "/v1/changes")[0].status, 401)
        self.pair()
        r, body, _ = self.req("GET", "/v1/changes?since=0")
        self.assertEqual(r.status, 200)
        self.assertGreater(len(json.loads(body)["changes"]), 5)
        r, gz, _ = self.req("GET", "/v1/changes?since=0", headers={"Accept-Encoding": "gzip"})
        import gzip
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertEqual(json.loads(gzip.decompress(gz)), json.loads(body))
        with open(os.path.join(self.tmp, "sync", "devices.json"), encoding="utf-8") as fh:
            self.assertNotIn(self.token, fh.read())       # only the hash is stored
        self.assertEqual(self.req("POST", "/v1/unpair")[0].status, 200)
        self.assertEqual(self.req("GET", "/v1/changes")[0].status, 401)

    def test_pin_is_single_use_and_rate_limited(self):
        pin = self.srv.credentials.begin_pairing()
        wrong = "000000" if pin != "000000" else "111111"
        for _ in range(ss.PIN_MAX_ATTEMPTS):
            self.assertEqual(self.req("POST", "/v1/pair", {"pin": wrong})[0].status, 403)
        r, body, _ = self.req("POST", "/v1/pair", {"pin": pin})
        self.assertEqual((r.status, json.loads(body)["error"]), (403, "no_pairing"))
        offer = self.srv.begin_pairing(host="192.168.1.5")
        self.assertIn(f"fp={self.srv.fingerprint}", offer["uri"])
        self.assertEqual(self.req("POST", "/v1/pair", {"pin": offer["pin"]})[0].status, 200)
        self.assertEqual(self.req("POST", "/v1/pair", {"pin": offer["pin"]})[0].status, 403)

    def test_media_streams_range_and_privacy(self):
        self.pair()
        r, body, _ = self.req("GET", "/v1/media/1/original", headers={"Range": "bytes=256-511"})
        self.assertEqual(r.status, 206)
        self.assertEqual(body, bytes(range(256)))
        self.assertEqual(r.getheader("Content-Range"), "bytes 256-511/10240")
        r, body, _ = self.req("GET", "/v1/media/1/thumb")
        self.assertEqual((r.status, body), (200, b"\xff\xd8thumb\xff\xd9"))
        etag = r.getheader("ETag")
        self.assertEqual(self.req("GET", "/v1/media/1/thumb", headers={"If-None-Match": etag})[0].status, 304)
        self.assertEqual(self.req("GET", "/v1/media/4/original")[0].status, 404)    # vault
        self.assertEqual(self.req("GET", "/v1/media/3/original")[0].status, 404)    # Live motion half
        r, body, _ = self.req("GET", "/v1/micro?ids=1,2,4")
        self.assertEqual([i for i, _ in ss.unpack_micro(body)], [1, 2])            # never the vault

    def test_packs_whitelist_and_power(self):
        self.pair()
        r, body, _ = self.req("GET", "/v1/packs")
        packs = json.loads(body)["packs"]
        self.assertEqual([(p["id"], p["title"], p["maxzoom"]) for p in packs], [("cz", "Czechia", 14)])
        r, body, _ = self.req("GET", "/v1/packs/cz")
        self.assertEqual((r.status, len(body)), (200, packs[0]["size"]))
        self.assertEqual(self.req("GET", "/v1/packs/..%2Fphotos")[0].status, 404)
        power.get().override = power.PowerState(on_battery=True, percent=10, saver=True, has_battery=True)
        r, _, _ = self.req("GET", "/v1/packs/cz")
        self.assertEqual((r.status, r.getheader("Retry-After")), (503, str(ss.RETRY_AFTER_S)))
        self.assertEqual(self.req("GET", "/v1/media/1/original")[0].status, 200)    # interactive still works
        self.assertEqual(self.req("GET", "/v1/media/1/original?bulk=1")[0].status, 503)

    def test_edit_write_back_is_canonical(self):
        self.pair()
        seen = []
        self.srv.on_remote_change = lambda kind, mid: seen.append((kind, mid))
        r, body, _ = self.req("PUT", "/v1/media/1/edit", {"params": {"exposure": 250, "angle": 3, "future": 1}})
        out = json.loads(body)
        self.assertEqual(out["params"]["exposure"], 100.0)            # clamped like the desktop
        self.assertEqual(out["params"]["future"], 1)                  # unknown keys survive
        c = database.get_connection()
        self.assertEqual(qe.load(c, 1).exposure, 100.0)
        c.close()
        self.assertEqual(self.req("DELETE", "/v1/media/1/edit")[0].status, 200)
        self.assertEqual(self.req("PUT", "/v1/media/4/edit", {"params": {"exposure": 5}})[0].status, 404)
        self.assertEqual(self.req("PUT", "/v1/media/1/favorite", {"value": False})[0].status, 200)
        self.assertEqual(seen, [("edit", 1), ("edit", 1), ("favorite", 1)])

    def test_websocket_change_feed(self):
        self.pair()
        raw = socket.create_connection(("127.0.0.1", self.srv.port), timeout=10)
        sock = self.ctx().wrap_socket(raw)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /v1/events HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Authorization: Bearer {self.token}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(1)
        self.assertIn(b"101", head.split(b"\r\n")[0])
        expect = base64.b64encode(hashlib.sha1((key + ss.WS_GUID).encode()).digest())
        self.assertIn(expect, head)

        def frame():
            b0, b1 = sock.recv(1)[0], sock.recv(1)[0]
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", sock.recv(2))[0]
            data = b""
            while len(data) < n:
                data += sock.recv(n - len(data))
            return b0 & 0x0F, data

        op, data = frame()
        self.assertEqual(json.loads(data)["type"], "hello")
        c = database.get_connection()
        with c:
            c.execute("UPDATE media_items SET is_favorite = 0 WHERE id = 1")
        c.close()
        t0 = time.monotonic()
        while True:
            op, data = frame()
            if op == 0x1:
                break
        self.assertEqual(json.loads(data)["type"], "changes")
        self.assertLess(time.monotonic() - t0, ss.NOTIFY_POLL_S * 3 + 1)
        sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))      # masked close
        while (op := frame()[0]) in (0x1, 0x9):
            pass
        self.assertEqual(op, 0x8)
        sock.close()


if __name__ == "__main__":
    unittest.main()
