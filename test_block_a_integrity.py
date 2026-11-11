"""Fix Block A: data integrity and scanner reliability (audit 2026-11-10, H10-H16 / H1).

  1. exiftool gets the file paths in a UTF-8 args file (-charset filename=utf8): Czech and
     international file names keep their EXIF date. Through the command line they were dropped
     (Japanese) or came back as "???_cz.jpg" (Czech) and the photo was stored without date and GPS.
  2. no external tool call can stall the scanner: every one-shot subprocess call has a timeout, and
     long-running streaming children are watched for lack of progress (tool_timeouts.StallWatchdog).
  3. two uploads of the same bytes never create two files (the dedupe check and the rename are one
     critical section).
  4. the iOS backup contract (Swift sources are text-checked here; BackupIntegrityTests.swift
     exercises the same rules on a Mac / in CI): nothing is "sent" unless the desktop acknowledged it,
     and a full or failing desktop only pauses the queue.
"""
import ast
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image

import paths
import scanner
import tool_timeouts

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# ── 1. exiftool and non-ASCII file names ─────────────────────────────────────────────────────

NAMES = ["plain.jpg", "ěščřžýáíé_cz.jpg", "日本語の写真.jpg", "Фото_Москва.jpg", "emoji_😀.jpg"]
DATE = "2019:07:06 12:34:56"


def _make_photo(path):
    img = Image.new("RGB", (32, 32), (40, 90, 160))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = DATE                    # DateTimeOriginal
    img.save(path, "JPEG", exif=exif)


class TestExiftoolUtf8Names(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kairos-utf8-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paths_go_through_a_utf8_args_file_not_the_command_line(self):
        seen = {}

        def fake_run(cmd, *a, **k):
            seen["cmd"] = list(cmd)
            seen["args"] = open(cmd[cmd.index("-@") + 1], encoding="utf-8").read().splitlines()
            return type("R", (), {"stdout": b"[]", "stderr": b"", "returncode": 0})()

        s = scanner.MediaScannerThread(self.tmp, exiftool_path=__file__)
        old = scanner.subprocess.run
        scanner.subprocess.run = fake_run
        try:
            s._run_exiftool([os.path.join(self.tmp, n) for n in NAMES])
        finally:
            scanner.subprocess.run = old
        cmd = seen["cmd"]
        self.assertEqual(cmd[1:3], ["-charset", "filename=utf8"])
        for n in NAMES:
            self.assertNotIn(os.path.join(self.tmp, n), cmd, "no file path on the command line (ANSI code page)")
            self.assertIn(os.path.join(self.tmp, n), seen["args"])
        self.assertIn("-json", seen["args"])
        # the temporary args file does not stay behind
        self.assertFalse(os.path.exists(cmd[cmd.index("-@") + 1]))

    @unittest.skipUnless(os.path.exists(paths.EXIFTOOL_PATH), "exiftool.exe not present")
    def test_real_exiftool_keeps_the_date_of_every_international_file_name(self):
        files = []
        for n in NAMES:
            p = os.path.join(self.tmp, n)
            _make_photo(p)
            files.append(p)
        s = scanner.MediaScannerThread(self.tmp, exiftool_path=paths.EXIFTOOL_PATH)
        meta = s._extract_batch_metadata(files)
        for p in files:
            m = meta[scanner._path_key(p)]
            self.assertEqual(m.get("DateTimeOriginal"), DATE, f"EXIF lost for {os.path.basename(p)}")
            self.assertEqual(m.get("SourceFile"), p)

    @unittest.skipUnless(os.path.exists(paths.EXIFTOOL_PATH), "exiftool.exe not present")
    def test_a_scan_stores_the_exif_date_for_non_ascii_names(self):
        """End to end through the scanner's own pipeline: the row must carry the EXIF date, not the
        file's mtime (what the fallback stores when exiftool returned nothing for the file)."""
        import database
        old_db = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        lib = os.path.join(self.tmp, "lib")
        os.makedirs(lib)
        try:
            database.init_db()
            for n in NAMES:
                _make_photo(os.path.join(lib, n))
            s = scanner.MediaScannerThread(lib, exiftool_path=paths.EXIFTOOL_PATH)
            s._insert_all = None
            meta = s._extract_batch_metadata([os.path.join(lib, n) for n in NAMES])
            with database.db_transaction() as conn:
                cur = conn.cursor()
                for n in NAMES:
                    p = os.path.join(lib, n)
                    st = os.stat(p)
                    s._insert_item(cur, meta[scanner._path_key(p)], os.path.normpath(p), st.st_size, st.st_mtime)
            conn = database.get_connection()
            try:
                rows = dict(conn.execute("SELECT file_name, date_taken FROM media_items").fetchall())
            finally:
                conn.close()
            for n in NAMES:
                self.assertEqual(rows.get(n), "2019-07-06 12:34:56", n)
        finally:
            database.DB_PATH = old_db


# ── 2. nothing can stall the scanner ─────────────────────────────────────────────────────────

class TestToolTimeouts(unittest.TestCase):
    def test_every_one_shot_subprocess_call_has_a_timeout(self):
        missing = []
        for f in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
            base = os.path.basename(f)
            if base.startswith(("test_", "conftest", "benchmark_")):
                continue
            with open(f, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("run", "check_output", "check_call", "call")
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
                        and not any(k.arg == "timeout" for k in n.keywords)):
                    missing.append(f"{base}:{n.lineno}")
        self.assertEqual(missing, [], "subprocess call without timeout= (a corrupt video stalls it forever)")

    def test_a_hung_ffmpeg_does_not_stall_thumbnail_creation(self):
        """The call is made for real; only the program is swapped for a direct child that hangs
        (a .cmd wrapper would leave a grandchild holding the pipes, which is not what ffmpeg does)."""
        tmp = tempfile.mkdtemp(prefix="kairos-hang-")
        try:
            fake = os.path.join(tmp, "ffmpeg.exe")                  # must exist; never actually run
            open(fake, "wb").close()
            video = os.path.join(tmp, "broken.mp4")
            with open(video, "wb") as fh:
                fh.write(b"not a video")
            out = os.path.join(tmp, "thumb.jpg")
            seen = {}
            real_run = subprocess.run

            def hanging_run(cmd, *a, **k):
                if cmd and cmd[0] == fake:
                    seen["timeout"] = k.get("timeout")
                    cmd = [sys.executable, "-I", "-c", "import time; time.sleep(20)"]
                return real_run(cmd, *a, **k)

            old = (scanner.FFMPEG_ONESHOT_TIMEOUT_S, scanner._video_thumb_filter, scanner.subprocess.run)
            scanner.FFMPEG_ONESHOT_TIMEOUT_S = 1
            scanner._video_thumb_filter = lambda *a, **k: "scale=16:16"
            scanner.subprocess.run = hanging_run
            t0 = time.monotonic()
            try:
                ok = scanner.make_thumbnail(video, out, "video", None, ffmpeg_path=fake)
            finally:
                scanner.FFMPEG_ONESHOT_TIMEOUT_S, scanner._video_thumb_filter, scanner.subprocess.run = old
            self.assertFalse(ok)
            self.assertEqual(seen["timeout"], 1, "the scanner's deadline must reach subprocess.run")
            self.assertLess(time.monotonic() - t0, 8.0, "must give up at the timeout, not wait for ffmpeg")
            self.assertFalse(os.path.exists(out))
            self.assertEqual([n for n in os.listdir(tmp) if ".tmp" in n], [], "no stranded temp file")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _sleeper():
    return subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])


class TestStallWatchdog(unittest.TestCase):
    def test_kills_a_child_that_makes_no_progress(self):
        proc = _sleeper()
        wd = tool_timeouts.StallWatchdog(proc, idle_s=0.4, poll_s=0.05)
        try:
            proc.wait(timeout=10)
        finally:
            wd.stop()
            if proc.poll() is None:
                proc.kill()
        self.assertTrue(wd.fired)
        self.assertIsNotNone(proc.returncode)

    def test_progress_keeps_a_slow_child_alive(self):
        proc = _sleeper()
        wd = tool_timeouts.StallWatchdog(proc, idle_s=0.5, poll_s=0.05)
        try:
            for _ in range(15):                     # 1.5 s > idle_s, but progress arrives every 0.1 s
                time.sleep(0.1)
                wd.kick()
            self.assertIsNone(proc.poll(), "a child that keeps reporting progress is never killed")
            self.assertFalse(wd.fired)
        finally:
            wd.stop()
            proc.kill()
            proc.wait()

    def test_time_the_governor_holds_the_process_does_not_count(self):
        proc = _sleeper()
        wd = tool_timeouts.StallWatchdog(proc, idle_s=0.3, paused=lambda: True, poll_s=0.05)
        try:
            time.sleep(1.0)                         # suspended for the user's interaction
            self.assertIsNone(proc.poll())
            self.assertFalse(wd.fired)
        finally:
            wd.stop()
            proc.kill()
            proc.wait()

    def test_streaming_ffmpeg_users_are_wired_to_the_watchdog(self):
        for rel in ("video_proxy.py", "scrub_strip.py"):
            src = _read(rel)
            self.assertIn("StallWatchdog(", src, rel)
            self.assertIn("watchdog.stop()", src, rel)


# ── 3. concurrent uploads of the same bytes ──────────────────────────────────────────────────

class TestConcurrentImport(unittest.TestCase):
    def setUp(self):
        import database
        import sync_server as ss
        self.database = database
        self.tmp = tempfile.mkdtemp(prefix="kairos-race-")
        self._old = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self.inbox = os.path.join(self.tmp, "inbox")
        self.srv = ss.SyncServer(host="127.0.0.1", port=0, state_dir=os.path.join(self.tmp, "sync"),
                                 packs_dir=os.path.join(self.tmp, "packs"), use_tls=False, name="T",
                                 inbox_dir=self.inbox)
        pin = self.srv.credentials.begin_pairing()
        self.token, _ = self.srv.credentials.pair(pin, "iPhone", "dev-1")

    def tearDown(self):
        import shutil
        self.srv.shutdown()
        self.database.DB_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload(self, data, sha, name, out):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=30)
        try:
            c.request("POST", f"/v1/import?name={name}&sha256={sha}", body=data,
                      headers={"Authorization": f"Bearer {self.token}", "Content-Length": str(len(data))})
            r = c.getresponse()
            out.append((r.status, json.loads(r.read())))
        finally:
            c.close()

    def _files(self):
        found = []
        for root, dirs, files in os.walk(self.inbox):
            dirs[:] = [d for d in dirs if d != ".partial"]
            found += files
        return sorted(found)

    def test_parallel_uploads_of_the_same_bytes_land_exactly_once(self):
        import hashlib
        base = os.urandom(1_500_000)
        for round_no in range(3):                   # the window is narrow: repeat to catch a regression
            out = []
            payload = base + bytes([round_no])      # different bytes per round, same bytes within a round
            sha = hashlib.sha256(payload).hexdigest()
            name = f"IMG_{round_no}.HEIC"           # distinct names: only a real duplicate may get "(2)"
            threads = [threading.Thread(target=self._upload, args=(payload, sha, name, out)) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            statuses = sorted(b["status"] for _, b in out)
            self.assertEqual(statuses, ["duplicate"] * 3 + ["imported"], out)
        files = self._files()
        self.assertEqual(files, ["IMG_0.heic", "IMG_1.heic", "IMG_2.heic"], files)

    def test_same_name_different_bytes_still_get_distinct_files(self):
        import hashlib
        a, b = os.urandom(50_000), os.urandom(50_000)
        out = []
        ts = [threading.Thread(target=self._upload, args=(d, hashlib.sha256(d).hexdigest(), "IMG_9.HEIC", out))
              for d in (a, b)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(s for _, s in [(x, y["status"]) for x, y in out]), ["imported", "imported"])
        self.assertEqual(self._files(), ["IMG_9 (2).heic", "IMG_9.heic"])

    def test_a_sequential_retry_is_still_answered_duplicate(self):
        import hashlib
        d = os.urandom(10_000)
        sha = hashlib.sha256(d).hexdigest()
        out = []
        self._upload(d, sha, "IMG_2.HEIC", out)
        self._upload(d, sha, "IMG_2.HEIC", out)
        self.assertEqual([b["status"] for _, b in out], ["imported", "duplicate"])
        self.assertEqual(len(self._files()), 1)


# ── 4. the iOS backup contract (text-checked here, exercised by BackupIntegrityTests.swift) ──

class TestIosBackupContract(unittest.TestCase):
    QUEUE = "ios/KairosCore/Sources/KairosCore/Sync/ImportQueue.swift"
    BACKUP = "ios/KairosUI/Sources/KairosUI/Phone/PhoneBackup.swift"

    def test_only_definite_file_rejections_drop_an_upload(self):
        src = _read(self.QUEUE)
        m = re.search(r"permanentRejections: Set<Int> = \[([0-9, ]+)\]", src)
        self.assertIsNotNone(m)
        permanent = {int(x) for x in m.group(1).split(",")}
        self.assertEqual(permanent, {400, 411, 413, 415})
        # the statuses the desktop uses for ITS OWN condition must never be in that set
        for status in (404, 500, 502, 503, 507):
            self.assertNotIn(status, permanent)
        self.assertNotIn("retryLater", src, "counting 5xx/507 as the file's failure was the data-loss bug")

    def test_the_desktop_really_answers_507_and_415_as_the_phone_expects(self):
        src = _read("sync_server.py")
        self.assertIn('_HttpError(507, "insufficient_storage"', src)
        self.assertIn('_HttpError(415, "unsupported_type")', src)

    def test_sent_is_settled_from_recorded_outcomes_not_from_leaving_the_outbox(self):
        src = _read(self.BACKUP)
        self.assertIn("BackupSettlement.settle(", src)
        self.assertNotIn("stillQueued", src, "inferring 'sent' from 'not in the outbox' was the bug")
        self.assertIn("setBackupFailed", src)
        # the asset a file belongs to travels with it, so a Live Photo needs both halves acknowledged
        self.assertIn("group: a.localId", src)

    def test_outcome_is_persisted_before_the_item_leaves_the_outbox(self):
        src = _read(self.QUEUE)
        body = src[src.index("private func finish("):]
        self.assertLess(body.index("saveOutcomes()"), body.index("removeItem(at:"),
                        "if the app dies between the two writes the item must still be queued")
        self.assertIn('"outcomes.json"', src.split("let known =")[1].split("\n")[0],
                      "init must not sweep the outcome log as a stray file")

    def test_failed_state_is_visible_with_a_retry_action(self):
        ui = _read("ios/KairosUI/Sources/KairosUI/Library/LibraryGridView.swift")
        self.assertIn("couldn't be backed up", ui)
        self.assertIn("retryFailedBackups()", ui)
        self.assertIn("Backup paused", ui)
        self.assertNotIn("(unsupported format)", ui, "the pill used to blame the format for every refusal")
        store = _read("ios/KairosCore/Sources/KairosCore/Database/LibraryStore.swift")
        self.assertIn("ALTER TABLE phone_assets ADD COLUMN backup_error TEXT", store)
        self.assertIn("schemaVersion: Int64 = 4", store)   # Fix Block C added v4 (favourite queue)


if __name__ == "__main__":
    unittest.main()
