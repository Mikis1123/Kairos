"""Sprint R – skryté chyby enginu: opakovaný průchod démona, životnost vláken při ukončení,
snímky videí ve strážci, vyprázdněné roury ffmpeg, Job Object, průběžné doplňování vektorů,
LRU podle bajtů, hold s životností, úklid polotovarů a denní zálohy knihovny.

Testy NESAHAJÍ na síť ani na photos.db.
"""
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import governor
import paths
import video_frames
import video_proxy

if HAS_QT:
    import main

ROOT = os.path.dirname(os.path.abspath(__file__))


class TestGovernorLease(unittest.TestCase):

    def test_hold_with_ttl_expires_without_release(self):
        g = governor.Governor(quiet_s=0.0)
        g.set_active("map", True, ttl_s=0.15)
        self.assertEqual(g.reasons(), ["map"])
        time.sleep(0.3)
        self.assertEqual(g.reasons(), [], "zapomenutý konec pohybu hold neudrží navždy")

    def test_renewal_extends_and_release_cancels_lease(self):
        g = governor.Governor(quiet_s=0.0)
        g.set_active("map", True, ttl_s=0.2)
        time.sleep(0.12)
        g.set_active("map", True, ttl_s=0.2)          # nové hlášení = nová lhůta
        time.sleep(0.12)
        self.assertEqual(g.reasons(), ["map"])
        g.set_active("map", False)
        self.assertEqual(g._leases, {})

    def test_journey_hold_without_ttl_stays(self):
        g = governor.Governor(quiet_s=0.0)
        g.set_active("journey", True)
        time.sleep(0.1)
        self.assertEqual(g.reasons(), ["journey"])


@unittest.skipUnless(sys.platform == "win32", "Job Object jen ve Windows")
class TestJobObject(unittest.TestCase):

    def test_registered_process_lands_in_kill_on_close_job(self):
        import ctypes
        from ctypes import wintypes
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            g = governor.Governor(quiet_s=0.0)
            g.register_process(proc.pid)
            k32 = ctypes.windll.kernel32
            k32.OpenProcess.restype = wintypes.HANDLE
            h = k32.OpenProcess(0x1000, False, proc.pid)      # PROCESS_QUERY_LIMITED_INFORMATION
            in_job = wintypes.BOOL()
            try:
                self.assertTrue(k32.IsProcessInJob(wintypes.HANDLE(h), wintypes.HANDLE(governor._job_handle()),
                                                   ctypes.byref(in_job)))
            finally:
                k32.CloseHandle(wintypes.HANDLE(h))
            self.assertTrue(in_job.value, "ffmpeg umře s aplikací")
            g.unregister_process(proc.pid)
        finally:
            proc.kill()
            proc.wait()


class TestPipesAndTemp(unittest.TestCase):

    def test_stderr_drain_keeps_tail_and_never_blocks_child(self):
        # 400 KB na stderr: bez průběžného čtení by se dítě zablokovalo na plné rouře
        code = "import sys\nfor i in range(8000): sys.stderr.write('varovani %05d %s\\n' % (i, 'x' * 40))\n"
        proc = subprocess.Popen([sys.executable, "-c", code], stderr=subprocess.PIPE)
        tail = []
        import threading
        t = threading.Thread(target=video_proxy._drain_tail, args=(proc.stderr, tail), daemon=True)
        t.start()
        self.assertEqual(proc.wait(timeout=20), 0)
        t.join(5)
        self.assertLessEqual(len(tail), video_proxy.ERR_TAIL_LINES)
        self.assertIn(b"07999", tail[-1])

    def test_sweep_temp_removes_only_stale_partials(self):
        old = os.path.join(paths.PROXY_DIR, "a.123.tmp.mp4")
        fresh = os.path.join(paths.PROXY_DIR, "b.456.tmp.mp4")
        keep = os.path.join(paths.PROXY_DIR, "c.mp4")
        os.makedirs(paths.PROXY_DIR, exist_ok=True)
        for p in (old, fresh, keep):
            with open(p, "wb") as f:
                f.write(b"x")
        past = time.time() - 7200
        os.utime(old, (past, past))
        os.utime(keep, (past, past))
        with mock.patch.object(paths, "THUMB_DIR", os.path.join(paths.PROXY_DIR, "neni")):
            self.assertEqual(video_proxy.sweep_temp(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh), "běžící převod si svůj soubor nechá")
        self.assertTrue(os.path.exists(keep))
        for p in (fresh, keep):
            os.remove(p)


class TestGovernedFrames(unittest.TestCase):

    def test_cancel_kills_child_promptly(self):
        start = time.monotonic()
        stop_at = start + 0.3
        with self.assertRaises(video_frames.Cancelled):
            video_frames._run_governed([sys.executable, "-c", "import time; time.sleep(30)"], 0,
                                       governor.Governor(quiet_s=0.0), lambda: time.monotonic() > stop_at)
        self.assertLess(time.monotonic() - start, 5, "dřív subprocess.run blokoval až 60 s")

    def test_paused_time_does_not_count_towards_timeout(self):
        import threading
        g = governor.Governor(quiet_s=0.0)
        g.hold("video")                              # proces se hned pozastaví (NtSuspendProcess)
        threading.Timer(0.8, g.release, args=("video",)).start()
        with mock.patch.object(video_frames, "FRAME_TIMEOUT_S", 0.6):
            t0 = time.monotonic()
            out = video_frames._run_governed([sys.executable, "-c", "import sys; sys.stdout.write('ok')"],
                                             0, g, None)
        self.assertEqual(out, b"ok", "pozastavený ffmpeg není „zaseklý“")
        if sys.platform == "win32":
            self.assertGreaterEqual(time.monotonic() - t0, 0.7, "během přehrávání stál")

    def test_stdout_returned(self):
        out = video_frames._run_governed([sys.executable, "-c", "import sys; sys.stdout.write('A' * 300000)"], 0, None, None)
        self.assertEqual(len(out), 300000)


class DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_sprint_r_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(database, "DB_PATH", os.path.join(self.tmp, "photos.db"))
        p.start()
        self.addCleanup(p.stop)
        database.init_db()

    def media(self, n, mtype="photo"):
        with database.db_transaction() as conn:
            cur = conn.execute("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?, ?, ?)",
                               (os.path.join(self.tmp, f"m{n}.jpg"), f"m{n}.jpg", mtype))
            return cur.lastrowid


class TestIncrementalVectors(DbCase):

    def test_subset_matches_full_load(self):
        a, b, v = self.media(1), self.media(2), self.media(3, "video")
        for mid in (a, b, v):
            database.save_image_embedding(mid, bytes([mid]) * 8, "sig")
        database.save_video_frame_embeddings(v, [(100, b"f1" * 4), (900, b"f2" * 4)], "sig")
        full = database.get_search_vectors()
        part = database.get_search_vectors([b, v])
        self.assertEqual(set(part), {b, v})
        self.assertEqual(part[b], full[b])
        self.assertEqual(part[v], full[v], "video: plakát + snímky jako při plném načtení")
        self.assertEqual(database.get_search_vectors([]), {})


class TestBackups(DbCase):

    def test_daily_backup_is_consistent_once_per_day_and_rotates(self):
        self.media(1)
        bdir = os.path.join(self.tmp, "backups")
        day = datetime.date(2026, 10, 1)
        first = database.backup_database(bdir, keep=3, today=day)
        self.assertTrue(first and os.path.exists(first))
        conn = sqlite3.connect(first)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()
        self.assertIsNone(database.backup_database(bdir, keep=3, today=day), "jednou za den")
        for k in range(1, 6):
            database.backup_database(bdir, keep=3, today=day + datetime.timedelta(days=k))
        names = sorted(os.listdir(bdir))
        self.assertEqual(names, ["photos-2026-10-04.db", "photos-2026-10-05.db", "photos-2026-10-06.db"])

    def test_missing_library_makes_no_backup(self):
        with mock.patch.object(database, "DB_PATH", os.path.join(self.tmp, "neni.db")):
            self.assertIsNone(database.backup_database(os.path.join(self.tmp, "b")))


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestByteCappedLru(unittest.TestCase):

    def test_bytes_cap_evicts_oldest(self):
        from PyQt6.QtGui import QPixmap
        pix = QPixmap(100, 100)                      # 40 KB při 32 bitech
        size = main.pixmap_bytes(pix)
        lru = main.LRUCache(1000, max_bytes=size * 3, sizer=main.pixmap_bytes)
        for k in range(5):
            lru.put(k, pix)
        self.assertEqual(list(lru.cache), [2, 3, 4])
        self.assertEqual(lru.bytes, size * 3)
        lru.put(3, pix)                              # přepsání se nepočítá dvakrát
        self.assertEqual(lru.bytes, size * 3)
        lru.clear()
        self.assertEqual(lru.bytes, 0)

    def test_count_cap_unchanged_without_sizer(self):
        lru = main.LRUCache(64)
        for k in range(100):
            lru.put(k, k)
        self.assertEqual(len(lru.cache), 64)

    def test_grid_model_is_byte_capped(self):
        model = main.ApplePhotoModel(1200)
        self.assertEqual(model.ram_cache.max_bytes, main.THUMB_RAM_BUDGET_MB * 1024 * 1024)


class _FakeDaemon:
    def __init__(self, running):
        self.running = running
        self.stopped = False

    def isRunning(self):
        return self.running

    def stop(self):
        self.stopped = True

    def wait(self, ms=None):
        return True


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestDaemonLifecycle(unittest.TestCase):

    def _host(self, daemon):
        host = mock.MagicMock()
        host._ai_daemon = daemon
        host._shutting_down = False
        host._ai_daemon_rerun = False
        return host

    def test_start_while_running_schedules_rerun(self):
        host = self._host(_FakeDaemon(True))
        main.MainWindow._start_ai_daemon(host)
        self.assertTrue(host._ai_daemon_rerun, "import během průchodu se nezahodí")

    def test_finished_with_rerun_restarts(self):
        host = self._host(_FakeDaemon(False))
        host._ai_daemon_rerun = True
        with mock.patch.object(main.QTimer, "singleShot") as shot:
            main.MainWindow._on_ai_daemon_finished(host)
        shot.assert_called_once()
        self.assertIs(shot.call_args[0][1], host._start_ai_daemon)

    def test_no_restart_while_shutting_down(self):
        host = self._host(_FakeDaemon(False))
        host._ai_daemon_rerun = True
        host._shutting_down = True
        with mock.patch.object(main.QTimer, "singleShot") as shot:
            main.MainWindow._on_ai_daemon_finished(host)
        shot.assert_not_called()

    def test_settings_apply_has_no_timed_wait(self):
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        block = src[src.index("Restart démona s novým prahem"):src.index("# ── Vzpomínky (Polish P2)")]
        self.assertNotIn(".wait(2000)", block)

    def test_parked_thread_survives_and_is_joined(self):
        from PyQt6.QtCore import QThread, QObject

        class Slow(QThread):
            def run(self):
                time.sleep(0.4)

        parent = QObject()
        t = Slow(parent)
        t.start()
        main.park_thread(t)
        self.assertIsNone(t.parent(), "smazání okna vlákno nezruší")
        del parent
        self.assertEqual(main.join_parked_threads(5000), 0)
        self.assertFalse(t.isRunning())


class TestMapHolds(unittest.TestCase):

    def setUp(self):
        self.html = open(os.path.join(ROOT, "map_vault.html"), encoding="utf-8").read()

    def test_moveend_releases_before_director_guard(self):
        # moveend -> cameraRested() (an orbit session calls it once at its end, test_map_engine)
        i = self.html.index("map.on('moveend'")
        self.assertIn("cameraRested()", self.html[i:i + 200])
        i = self.html.index("function cameraRested()")
        block = self.html[i:i + 300]
        self.assertLess(block.index("setInteraction('map', false)"), block.index("if (director.directing) return"))

    def test_late_trail_never_swaps_started_leg(self):
        i = self.html.index("function onTrailComputed")
        self.assertIn("legStarted(d.leg)", self.html[i:i + 800])

    @unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
    def test_bridge_map_hold_has_ttl(self):
        bridge = main.MapBridge(mock.MagicMock())
        with mock.patch.object(governor.get(), "set_active") as sa:
            bridge.setInteraction("map", True)
        self.assertEqual(sa.call_args.kwargs.get("ttl_s"), main.MAP_HOLD_TTL_S)


class TestDeadFilesPurged(unittest.TestCase):

    def test_legacy_leaflet_map_removed(self):
        self.assertFalse(os.path.exists(os.path.join(ROOT, "map_app.py")))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "map_embedded.html")))


if __name__ == "__main__":
    unittest.main()
