"""Fix Block B: desktop concurrency and server desync (audit 2026-11-10).

  1. keep-alive desync: a request answered early (401 / 415 / 413 / bad json) never leaves its body in
     the socket, so the next request on the same connection is not parsed out of leftover bytes
     (the phone saw "501 Unsupported method").
  2. WebSocket zombies: a feed whose phone stopped answering pings is reaped, and a phone that
     reconnects replaces its own dead feed instead of meeting 503 "busy".
  3. the GUI thread never runs face re-grouping or the duplicate search (source-level pin + the
     real threaded bg_jobs path).
  4. Live Photo pairing is a dict lookup: 30k photos in well under a second, same pairs.
"""
import base64
import hashlib
import inspect
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest

import database
import scanner
import sync_server as ss

ROOT = os.path.dirname(os.path.abspath(__file__))


class _ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-blockb-")
        self._old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.srv = ss.SyncServer(host="127.0.0.1", port=0, state_dir=os.path.join(self.tmp, "sync"),
                                 packs_dir=os.path.join(self.tmp, "packs"), use_tls=False,
                                 name="TEST-PC", inbox_dir=os.path.join(self.tmp, "inbox"))
        pin = self.srv.credentials.begin_pairing()
        self.token, _ = self.srv.credentials.pair(pin, "Test iPhone", "dev-1")

    def tearDown(self):
        self.srv.shutdown()
        database.DB_PATH = self._old_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def connect(self):
        s = socket.create_connection(("127.0.0.1", self.srv.port), timeout=10)
        self.addCleanup(s.close)
        return s


def _read_response(sock):
    """(status, headers dict, body bytes) of one HTTP/1.1 response on a keep-alive socket."""
    head = b""
    while b"\r\n\r\n" not in head:
        part = sock.recv(1)
        if not part:
            return None
        head += part
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
    n = int(headers.get("content-length", 0))
    body = b""
    while len(body) < n:
        body += sock.recv(n - len(body))
    return status, headers, body


# ── 1. keep-alive desync ─────────────────────────────────────────────────────────────────────

class TestKeepAliveDesync(_ServerCase):
    def post(self, sock, path, body, auth=True, extra=""):
        head = (f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\n{extra}"
                + (f"Authorization: Bearer {self.token}\r\n" if auth else "") + "\r\n")
        sock.sendall(head.encode() + body)

    def hello(self, sock):
        sock.sendall(b"GET /v1/hello HTTP/1.1\r\nHost: x\r\n\r\n")
        return _read_response(sock)

    def test_unauthorized_post_with_body_keeps_the_connection_in_sync(self):
        s = self.connect()
        for _ in range(3):
            self.post(s, "/v1/import?name=a.heic&sha256=" + "0" * 64, os.urandom(40_000), auth=False)
            self.assertEqual(_read_response(s)[0], 401)
            status, _, body = self.hello(s)                       # same socket: must be a clean 200
            self.assertEqual(status, 200, body)

    def test_unsupported_type_415_then_next_request_is_clean(self):
        s = self.connect()
        self.post(s, "/v1/import?name=evil.exe&sha256=" + "0" * 64, os.urandom(200_000))
        self.assertEqual(_read_response(s)[0], 415)
        self.assertEqual(self.hello(s)[0], 200)

    def test_bad_json_and_oversize_json_bodies_are_drained(self):
        s = self.connect()
        self.post(s, "/v1/match", b"{not json")
        self.assertEqual(_read_response(s)[0], 400)
        self.assertEqual(self.hello(s)[0], 200)
        self.post(s, "/v1/pair", b" " * (300 * 1024))            # over the 256 KB cap: never read by the handler
        self.assertEqual(_read_response(s)[0], 400)
        self.assertEqual(self.hello(s)[0], 200)                  # ...but drained, so still in sync
        self.post(s, "/v1/match", b" " * (ss.BODY_DRAIN_MAX + 10))   # too big to drain: reply says close
        status, headers, _ = _read_response(s)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection", "").lower(), "close")

    def test_huge_unread_body_closes_the_connection_instead(self):
        s = self.connect()
        head = (f"POST /v1/import?name=a.exe&sha256={'0' * 64} HTTP/1.1\r\nHost: x\r\n"
                f"Content-Length: {ss.BODY_DRAIN_MAX * 8}\r\nAuthorization: Bearer {self.token}\r\n\r\n")
        s.sendall(head.encode() + os.urandom(1000))               # the rest is never sent
        status, headers, _ = _read_response(s)
        self.assertEqual(status, 415)
        self.assertEqual(headers.get("connection", "").lower(), "close")

    def test_successful_upload_stays_keep_alive(self):
        s = self.connect()
        data = os.urandom(50_000)
        self.post(s, f"/v1/import?name=ok.heic&sha256={hashlib.sha256(data).hexdigest()}", data)
        self.assertEqual(_read_response(s)[0], 200)
        self.assertEqual(self.hello(s)[0], 200)

    def test_chunked_request_is_never_misparsed(self):
        s = self.connect()
        s.sendall((f"POST /v1/import?name=a.heic&sha256={'0' * 64} HTTP/1.1\r\nHost: x\r\n"
                   f"Transfer-Encoding: chunked\r\nAuthorization: Bearer {self.token}\r\n\r\n"
                   "5\r\nhello\r\n0\r\n\r\n").encode())
        first = _read_response(s)
        self.assertIsNotNone(first)
        self.assertEqual(first[1].get("connection", "").lower(), "close")
        self.assertNotEqual(first[0], 501)


# ── 2. WebSocket zombies ─────────────────────────────────────────────────────────────────────

class TestWebSocketZombies(_ServerCase):
    def open_feed(self, token=None):
        sock = self.connect()
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /v1/events HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      f"Authorization: Bearer {token or self.token}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            part = sock.recv(1)
            if not part:
                break
            head += part
        return sock, head

    @staticmethod
    def frame(sock):
        b0, b1 = sock.recv(1)[0], sock.recv(1)[0]
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", sock.recv(2))[0]
        data = b""
        while len(data) < n:
            data += sock.recv(n - len(data))
        return b0 & 0x0F, data

    def wait_for(self, cond, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_silent_socket_is_reaped_after_an_unanswered_ping(self):
        ss.WS_KEEPALIVE_S, ss.WS_PONG_TIMEOUT_S = 0.5, 1.0
        self.addCleanup(lambda: (setattr(ss, "WS_KEEPALIVE_S", 20.0), setattr(ss, "WS_PONG_TIMEOUT_S", 10.0)))
        sock, head = self.open_feed()
        self.assertIn(b"101", head)
        self.assertEqual(json.loads(self.frame(sock)[1])["type"], "hello")
        # the client reads nothing and never answers: a phone that left Wi-Fi without a FIN
        self.assertTrue(self.wait_for(lambda: self.srv.stats["ws_reaped"] == 1), "zombie never reaped")
        self.assertTrue(self.wait_for(lambda: not self.srv._ws_open))
        self.assertTrue(self.wait_for(lambda: self.srv._sockets.acquire(blocking=False)),
                        "its slot is free again")
        self.srv._sockets.release()

    def test_a_pong_keeps_the_feed_alive(self):
        ss.WS_KEEPALIVE_S, ss.WS_PONG_TIMEOUT_S = 0.4, 1.0
        self.addCleanup(lambda: (setattr(ss, "WS_KEEPALIVE_S", 20.0), setattr(ss, "WS_PONG_TIMEOUT_S", 10.0)))
        sock, _ = self.open_feed()
        self.frame(sock)                                          # hello
        end = time.monotonic() + 3.0
        pings = 0
        while time.monotonic() < end:
            op, payload = self.frame(sock)
            if op == 0x9:
                pings += 1
                sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))  # masked empty pong
        self.assertGreaterEqual(pings, 3)
        self.assertEqual(self.srv.stats["ws_reaped"], 0)

    def test_reconnecting_phone_replaces_its_own_dead_feed_even_when_slots_are_full(self):
        # the same device opens MAX_SOCKETS feeds and never closes them (a phone roaming between
        # access points leaves one zombie per attempt); the next reconnect must still get in
        feeds = []
        for _ in range(ss.MAX_SOCKETS):
            sock, head = self.open_feed()
            self.assertIn(b"101", head)
            feeds.append(sock)
            self.assertTrue(self.wait_for(lambda: len(self.srv._ws_open) >= 1))
        # each new feed kicked the previous one of the same device, so at most one is alive
        self.assertTrue(self.wait_for(lambda: len(self.srv._ws_open) == 1), self.srv._ws_open)
        sock, head = self.open_feed()
        self.assertIn(b"101", head, head)
        self.assertEqual(self.srv.stats["busy"], 0)
        self.assertGreaterEqual(self.srv.stats["ws_replaced"], ss.MAX_SOCKETS)

    def test_other_devices_feeds_are_not_kicked(self):
        pin = self.srv.credentials.begin_pairing()
        token2, _ = self.srv.credentials.pair(pin, "iPad", "dev-2")
        a, _ = self.open_feed()
        self.assertTrue(self.wait_for(lambda: len(self.srv._ws_open) == 1))
        b, head = self.open_feed(token2)
        self.assertIn(b"101", head)
        self.assertTrue(self.wait_for(lambda: len(self.srv._ws_open) == 2))
        time.sleep(1.5)
        self.assertEqual(len(self.srv._ws_open), 2)

    def test_unauthorized_feed_is_401_not_503(self):
        sock, head = self.open_feed("wrong-token")
        self.assertIn(b"401", head.split(b"\r\n")[0])


# ── 3. GUI thread ────────────────────────────────────────────────────────────────────────────

class TestGuiThreadOffload(unittest.TestCase):
    def test_gui_methods_hand_the_work_to_a_worker_thread(self):
        import main
        for name in ("_recluster_faces", "_open_duplicate_review"):
            src = inspect.getsource(getattr(main.MainWindow, name))
            self.assertIn("_heavy_loads()", src, name)
            self.assertNotIn("processEvents", src, name)
        # the heavy work itself is only referenced inside the worker closure
        src = inspect.getsource(main.MainWindow._recluster_faces)
        self.assertLess(src.index("def work"), src.index("strict_recluster_faces("))
        src = inspect.getsource(main.MainWindow._open_duplicate_review)
        self.assertLess(src.index("def work"), src.index("refresh_phashes("))
        self.assertNotIn("processEvents", inspect.getsource(main.MainWindow._on_phash_progress))

    def test_close_waits_for_the_heavy_worker_and_phash_refresh_can_stop(self):
        import main
        self.assertIn("_heavy_bg", inspect.getsource(main.MainWindow.closeEvent))
        self.assertIn("should_stop", inspect.getsource(main.MainWindow._open_duplicate_review))

    def test_threaded_job_runs_off_the_gui_thread_and_delivers_on_it(self):
        from PyQt6.QtCore import QCoreApplication
        from PyQt6.QtWidgets import QApplication
        import bg_jobs
        app = QApplication.instance() or QApplication([])
        old = bg_jobs.SYNC
        bg_jobs.SYNC = False
        try:
            heavy = bg_jobs.BackgroundLoads(threads=1)
            seen = {}
            release = threading.Event()

            def work():
                seen["worker"] = threading.current_thread().name
                release.wait(5)
                return 42

            heavy.run("x", work, lambda r: seen.update(result=r, delivered=threading.current_thread().name))
            t0 = time.monotonic()
            self.assertTrue(heavy.pending("x"))                    # run() returned while the job is blocked
            self.assertLess(time.monotonic() - t0, 0.5)
            app.processEvents()                                    # the GUI stays live meanwhile
            self.assertNotIn("result", seen)
            release.set()
            end = time.monotonic() + 5
            while "result" not in seen and time.monotonic() < end:
                QCoreApplication.processEvents()
                time.sleep(0.01)
            self.assertEqual(seen["result"], 42)
            self.assertNotEqual(seen["worker"], threading.main_thread().name)
            self.assertEqual(seen["delivered"], threading.current_thread().name)
        finally:
            bg_jobs.SYNC = old


# ── 4. Live Photo pairing ────────────────────────────────────────────────────────────────────

class TestLivePairing(unittest.TestCase):
    def test_pairs_same_folder_same_stem(self):
        rows = [(1, "D:/p/IMG_1.HEIC", "photo"), (2, "D:/p/IMG_1.MOV", "video"),
                (3, "D:/p/IMG_2.HEIC", "photo"),                      # no video
                (4, "D:/q/IMG_3.HEIC", "photo"), (5, "D:/p/IMG_3.MOV", "video"),   # other folder
                (6, "D:/p/clip.MOV", "video")]
        self.assertEqual(scanner.pair_live_photos(rows), [("D:/p/IMG_1.MOV", 1)])

    def test_stem_and_folder_compare_case_insensitively_and_first_video_wins(self):
        rows = [(1, "D:/P/img_1.heic", "photo"), (2, "d:/p/IMG_1.mov", "video"), (3, "d:/p/IMG_1.mp4", "video")]
        self.assertEqual(scanner.pair_live_photos(rows), [("d:/p/IMG_1.mov", 1)])

    def test_character_set_rtrim_bug_is_gone(self):
        # the SQL rtrim(path, name) stripped a character SET: both of these collapsed to "D:/"
        rows = [(1, "D:/ab/a.heic", "photo"), (2, "D:/ba/a.mov", "video")]
        self.assertEqual(scanner.pair_live_photos(rows), [])

    def test_thirty_thousand_photos_in_well_under_a_second(self):
        rows, n = [], 30_000
        for i in range(n):
            folder = f"D:/Photos/{i // 500}"
            rows.append((i * 2 + 1, f"{folder}/IMG_{i}.HEIC", "photo"))
            if i % 3 == 0:
                rows.append((i * 2 + 2, f"{folder}/IMG_{i}.MOV", "video"))
        t0 = time.perf_counter()
        pairs = scanner.pair_live_photos(rows)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(pairs), n // 3)
        self.assertLess(elapsed, 1.0, elapsed)

    def test_scanner_step_updates_the_database(self):
        tmp = tempfile.mkdtemp(prefix="kairos-pair-")
        old = database.DB_PATH
        database.DB_PATH = os.path.join(tmp, "photos.db")
        try:
            database.init_db()
            conn = database.get_connection()
            with conn:
                for i, (name, kind) in enumerate([("A.HEIC", "photo"), ("A.MOV", "video"), ("B.HEIC", "photo")]):
                    conn.execute("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?,?,?)",
                                 (os.path.join(tmp, name), name, kind))
            inst = scanner.MediaScannerThread.__new__(scanner.MediaScannerThread)
            inst._pair_live_photos(conn)
            rows = conn.execute("SELECT file_name, media_type, paired_video_path FROM media_items "
                                "ORDER BY id").fetchall()
            conn.close()
            self.assertEqual(rows[0][1:], ("live_photo", os.path.join(tmp, "A.MOV")))
            self.assertEqual(rows[1][1], "video")
            self.assertEqual(rows[2][1], "photo")
        finally:
            database.DB_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
