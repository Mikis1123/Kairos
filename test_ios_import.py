"""iOS Phase 3, desktop side: camera-roll import from the phone (POST /v1/import).

A real SyncServer (plain HTTP on port 0, temp library) receives uploads the way the iOS
ImportQueue sends them: raw bytes, ?name=&sha256=, bearer token. Verified: the file lands in the
inbox only after its SHA-256 checks out, retries are idempotent by content, names cannot escape the
inbox, and the desktop GUI hook fires once per landed file.
"""
import hashlib
import http.client
import json
import os
import shutil
import tempfile
import unittest

import database
import scanner
import sync_server as ss


class TestPhoneImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-import-")
        self._old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.inbox = os.path.join(self.tmp, "inbox")
        self.srv = ss.SyncServer(host="127.0.0.1", port=0, state_dir=os.path.join(self.tmp, "sync"),
                                 packs_dir=os.path.join(self.tmp, "packs"), use_tls=False,
                                 name="TEST-PC", inbox_dir=self.inbox)
        self.events = []
        self.srv.on_remote_change = lambda kind, mid: self.events.append((kind, mid))
        pin = self.srv.credentials.begin_pairing()
        self.token, _ = self.srv.credentials.pair(pin, "Test iPhone", "dev-1")

    def tearDown(self):
        self.srv.shutdown()
        database.DB_PATH = self._old_db
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, method, path, body=None, auth=True, length=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        try:
            h = {"Authorization": f"Bearer {self.token}"} if auth else {}
            if body is not None:
                h["Content-Type"] = "application/octet-stream"
                h["Content-Length"] = str(len(body) if length is None else length)
            c.request(method, path, body=body, headers=h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        finally:
            c.close()

    def upload(self, data, name="IMG_0001.HEIC", sha=None):
        sha = sha or hashlib.sha256(data).hexdigest()
        return self.req("POST", f"/v1/import?name={name}&sha256={sha}", data)

    def inbox_files(self):
        out = []
        for root, dirs, files in os.walk(self.inbox):
            dirs[:] = [d for d in dirs if d != ".partial"]
            out += [os.path.relpath(os.path.join(root, f), self.inbox).replace(os.sep, "/") for f in files]
        return sorted(out)

    def test_upload_lands_in_month_folder_and_notifies_gui(self):
        data = os.urandom(300_000)
        status, body = self.upload(data)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "imported")
        self.assertRegex(body["name"], r"^\d{4}-\d{2}/IMG_0001\.heic$")
        with open(os.path.join(self.inbox, body["name"]), "rb") as fh:
            self.assertEqual(fh.read(), data)
        self.assertEqual(self.events, [("import", 0)])
        self.assertEqual(os.listdir(os.path.join(self.inbox, ".partial")), [])

    def test_retry_is_idempotent_and_precheck_reports_presence(self):
        data = os.urandom(5000)
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(self.req("GET", f"/v1/import/{sha}")[1]["status"], "absent")
        self.assertEqual(self.upload(data)[1]["status"], "imported")
        status, body = self.upload(data, name="IMG_9999.HEIC")
        self.assertEqual((status, body["status"]), (200, "duplicate"))
        self.assertEqual(self.req("GET", f"/v1/import/{sha}")[1]["status"], "present")
        self.assertEqual(len(self.inbox_files()), 1)
        self.assertEqual(len(self.events), 1)

    def test_deleted_import_may_come_again(self):
        data = os.urandom(5000)
        _, body = self.upload(data)
        os.remove(os.path.join(self.inbox, body["name"]))
        self.assertEqual(self.upload(data)[1]["status"], "imported")

    def test_sha_mismatch_writes_nothing(self):
        status, body = self.upload(b"x" * 1000, sha="0" * 64)
        self.assertEqual((status, body.get("error")), (422, "sha_mismatch"))
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual(self.events, [])

    def test_name_collision_with_different_content_gets_suffix(self):
        self.upload(b"a" * 100, name="IMG_0002.JPG")
        _, body = self.upload(b"b" * 100, name="IMG_0002.JPG")
        self.assertTrue(body["name"].endswith("IMG_0002 (2).jpg"), body)

    def test_rejects(self):
        data = b"z" * 64
        self.assertEqual(self.upload(data, name="notes.txt")[0], 415)
        self.assertEqual(self.upload(data, sha="nothex")[0], 400)
        self.assertEqual(self.req("POST", "/v1/import?name=a.jpg&sha256=" + "0" * 64, data, auth=False)[0], 401)
        self.assertEqual(self.inbox_files(), [])

    # ── Phase 1 (C1/C2): a full desktop disk is a pause for the phone, never a 500 or a rejection ──

    def test_precheck_reports_free_space(self):
        status, body = self.req("GET", "/v1/import/" + "a" * 64)
        self.assertEqual(status, 200)
        self.assertIsInstance(body["free_bytes"], int)
        self.assertEqual(body["reserve_bytes"], ss.IMPORT_FREE_RESERVE)

    def test_low_disk_answers_507_before_the_body_and_writes_nothing(self):
        self.srv._inbox_free_bytes = lambda: ss.IMPORT_FREE_RESERVE + 100       # 100 bytes of headroom
        status, body = self.upload(b"x" * 5000)
        self.assertEqual((status, body.get("error")), (507, "insufficient_storage"))
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual(self.events, [])
        small = self.upload(b"y" * 50, name="IMG_0002.HEIC")                      # fits: still accepted
        self.assertEqual(small[0], 200, small)

    def test_disk_full_mid_upload_answers_507_and_cleans_partial(self):
        import errno

        real_open = open

        class FullDisk:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fh.close()

            def write(self, buf):
                raise OSError(errno.ENOSPC, "No space left on device")

        ss.open = lambda path, mode="r", *a, **kw: FullDisk(real_open(path, mode)) if "w" in mode else real_open(path, mode, *a, **kw)
        try:
            status, body = self.upload(b"z" * 4000)
        finally:
            del ss.open
        self.assertEqual((status, body.get("error")), (507, "insufficient_storage"))
        self.assertEqual(self.inbox_files(), [])
        self.assertEqual(os.listdir(os.path.join(self.inbox, ".partial")), [])

    def test_safe_import_name(self):
        f = ss.safe_import_name
        self.assertEqual(f("../../Windows/evil.JPG"), "evil.jpg")
        self.assertEqual(f("C:\\Users\\x\\IMG.HEIC"), "IMG.heic")
        self.assertEqual(f("CON.jpg"), "_CON.jpg")
        self.assertEqual(f('a<b>:"c|?*.mov'), "a_b___c___.mov")
        self.assertEqual(f(".hidden.png"), "hidden.png")
        self.assertEqual(f("photo.exe"), "")
        self.assertEqual(f(""), "")

    def test_import_formats_are_scannable(self):
        self.assertLessEqual(set(ss.IMPORT_EXTS), set(scanner.SUPPORTED_EXTS))


if __name__ == "__main__":
    unittest.main()
