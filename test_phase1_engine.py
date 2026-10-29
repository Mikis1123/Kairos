"""Fáze 1 – adaptivní engine: strážce interakce, výkonnostní profil, proxy videí, odložený
náhledový pás, zmrazený zoom mřížky a režisér Cesty (dt, van Wijk, pauza, trasa na hodinách).

Testy NESAHAJÍ na síť ani na photos.db; proxy se převádí z vygenerovaného klipu.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QPointF, QUrl
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – před QApplication
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import governor
import paths
import perf_profile
import video_proxy

if HAS_QT:
    import main
    import motion
    import scrub_strip
    from test_patch11_maps import ShownMapPage, wait
    from test_sprint_stabilization import TRIP


# ═════════════════════════════════════════════════════════════════════════════
# Strážce interakce
# ═════════════════════════════════════════════════════════════════════════════

class TestGovernor(unittest.TestCase):

    def test_hold_release_and_quiet_window(self):
        g = governor.Governor(quiet_s=0.15)
        self.assertFalse(g.busy())
        g.hold("video")
        g.hold("gesture")
        self.assertEqual(g.reasons(), ["gesture", "video"])
        g.release("video")
        self.assertTrue(g.busy())
        g.release("gesture")
        self.assertTrue(g.busy(), "chvíli po uvolnění ještě klid nenastal")
        time.sleep(0.2)
        self.assertFalse(g.busy())

    def test_set_active_is_idempotent(self):
        g = governor.Governor(quiet_s=0.0)
        g.set_active("video", True)
        g.set_active("video", True)
        self.assertEqual(g.reasons(), ["video"])
        g.set_active("video", False)
        g.set_active("video", False)
        self.assertEqual(g.reasons(), [])

    def test_checkpoint_waits_for_quiet_and_honours_stop(self):
        g = governor.Governor(quiet_s=0.1)
        g.hold("video")
        t0 = time.monotonic()
        stop_at = t0 + 0.25
        self.assertTrue(g.checkpoint(lambda: time.monotonic() > stop_at), "stop během čekání = konec")
        self.assertGreaterEqual(time.monotonic() - t0, 0.2)
        g.release("video")
        time.sleep(0.12)
        self.assertFalse(g.checkpoint(lambda: False))

    @unittest.skipUnless(sys.platform == "win32", "NtSuspendProcess jen ve Windows")
    def test_background_process_suspended_while_busy(self):
        g = governor.Governor(quiet_s=0.2)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            g.register_process(proc.pid)
            g.hold("video")
            self.assertEqual(g.suspend_count, 1)
            self.assertTrue(g._procs[proc.pid], "pozastaveno během přehrávání")
            g.release("video")
            for _ in range(30):
                if not g._procs[proc.pid]:
                    break
                time.sleep(0.05)
            self.assertFalse(g._procs[proc.pid], "po zklidnění pokračuje")
            g.unregister_process(proc.pid)
            self.assertIsNone(proc.poll(), "proces žije (pozastavení ho nezabilo)")
        finally:
            proc.kill()
            proc.wait()


# ═════════════════════════════════════════════════════════════════════════════
# Výkonnostní profil
# ═════════════════════════════════════════════════════════════════════════════

class TestPerfProfile(unittest.TestCase):

    def test_classify(self):
        self.assertEqual(perf_profile.classify(4, 7.8), "baseline", "4 jádra, 8 GB = základní PC")
        self.assertEqual(perf_profile.classify(8, 7.8), "baseline", "málo paměti")
        self.assertEqual(perf_profile.classify(6, 16), "standard")
        self.assertEqual(perf_profile.classify(16, 31.3), "high", "testovací Ryzen AI")

    def test_tiers_scale_quality_and_load(self):
        lo = perf_profile.build("baseline", 4, 8, 60)
        hi = perf_profile.build("high", 16, 32, 120)
        self.assertTrue(lo.proxy_playback and not hi.proxy_playback, "4K HDR přímo jen na silném stroji")
        self.assertFalse(lo.dense_strips)
        self.assertGreater(lo.bg_item_pause_s, hi.bg_item_pause_s)
        self.assertLess(lo.map_lod_levels, hi.map_lod_levels)
        self.assertAlmostEqual(hi.frame_budget_ms, 1000 / 120)
        self.assertEqual(set(hi.for_map()) >= {"tier", "map_lod_levels", "map_pixel_ratio_max"}, True)
        json.dumps(hi.for_map())

    def test_override(self):
        with mock.patch.dict(os.environ, {"KAIROS_TIER": "baseline"}):
            self.assertEqual(perf_profile.detect().tier, "baseline")
        self.assertEqual(perf_profile.detect("standard").tier, "standard")
        perf_profile.detect("auto")


# ═════════════════════════════════════════════════════════════════════════════
# Proxy videí
# ═════════════════════════════════════════════════════════════════════════════

HDR_INFO = {"width": 3840, "height": 2160, "fps": 59.94, "ten_bit": True, "hdr": True}


class TestVideoProxy(unittest.TestCase):

    def test_needs_proxy(self):
        self.assertTrue(video_proxy.needs_proxy(HDR_INFO))
        self.assertTrue(video_proxy.needs_proxy({"width": 1920, "height": 1080, "fps": 30, "ten_bit": True, "hdr": False}))
        self.assertFalse(video_proxy.needs_proxy({"width": 1080, "height": 1920, "fps": 30, "ten_bit": False, "hdr": False}))
        self.assertFalse(video_proxy.needs_proxy(None))

    def test_command_tonemaps_only_hdr_and_caps_fps(self):
        cmd = " ".join(video_proxy.ffmpeg_cmd("in.mov", "out.mp4", HDR_INFO))
        self.assertIn("tonemap=tonemap=hable", cmd)
        self.assertIn("fps=30", cmd)
        self.assertLess(cmd.index("scale="), cmd.index("zscale"), "nejdřív zmenšit, pak převádět HDR")
        self.assertIn("setparams=color_primaries=bt2020:color_trc=arib-std-b67", cmd,
                      "vlastnosti snímků výslovně (jinak zscale: no path between colorspaces)")
        sdr = " ".join(video_proxy.ffmpeg_cmd("in.mov", "out.mp4", {"width": 3840, "height": 2160, "fps": 30,
                                                                    "ten_bit": False, "hdr": False}))
        self.assertNotIn("tonemap", sdr)
        self.assertNotIn("fps=30", sdr)

    def test_probe_parses_iphone_dolby_vision(self):
        line = (b"  Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv, "
                b"bt2020nc/bt2020/arib-std-b67), 3840x2160, 60234 kb/s, 59.94 fps, 59.94 tbr, 600 tbn (default)\n"
                b"      Side data:\n        DOVI configuration record: version: 1.0, profile: 8\n")
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(stderr=line)):
            info = video_proxy.probe("x.mov")
        self.assertEqual((info["width"], info["height"]), (3840, 2160))
        self.assertTrue(info["ten_bit"] and info["hdr"])
        self.assertAlmostEqual(info["fps"], 59.94)

    def test_freshness_and_budget(self):
        tmp = tempfile.mkdtemp(prefix="omni_proxy_")
        try:
            src = os.path.join(tmp, "v.mov")
            open(src, "wb").write(b"x")
            os.makedirs(paths.PROXY_DIR, exist_ok=True)
            p = video_proxy.proxy_path(src)
            open(p, "wb").write(b"y" * 4096)
            self.assertEqual(video_proxy.get_proxy(src), p)
            os.utime(src, (time.time() + 10, time.time() + 10))
            self.assertIsNone(video_proxy.get_proxy(src), "novější originál = zastaralá proxy")
            old = os.path.join(paths.PROXY_DIR, "old.mp4")
            open(old, "wb").write(b"z" * 4096)
            os.utime(old, (1, 1))
            video_proxy.enforce_budget(4096 / 1024 / 1024, keep=[p])
            self.assertTrue(os.path.exists(p), "právě vytvořená proxy se nemaže")
            self.assertFalse(os.path.exists(old), "nejdéle nepoužitá pryč")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            video_proxy.clear_cache()

    @unittest.skipUnless(os.path.exists(paths.FFMPEG_PATH), "ffmpeg.exe chybí")
    def test_real_hdr_clip_becomes_1080p_sdr_proxy(self):
        """Skutečný převod: 10bit HLG 2560×1440 @ 60 -> H.264 8 bit, ≤ 1920, ≤ 30 fps, SDR."""
        tmp = tempfile.mkdtemp(prefix="omni_proxy_real_")
        try:
            src = os.path.join(tmp, "hdr.mov")
            gen = [paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                   "-i", "testsrc2=size=2560x1440:rate=60", "-t", "1", "-pix_fmt", "yuv420p10le",
                   "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
                   "-c:v", "libx265", "-x265-params", "log-level=error", src]
            if subprocess.run(gen, capture_output=True, timeout=120).returncode != 0:
                self.skipTest("ffmpeg neumí vytvořit testovací HDR klip (libx265)")
            info = video_proxy.probe(src)
            self.assertTrue(info["hdr"] and info["ten_bit"], info)
            g = governor.Governor(quiet_s=0.0)
            out = video_proxy.build_proxy(src, g, lambda: False, threads=2)
            self.assertTrue(out and os.path.exists(out), out)
            pinfo = video_proxy.probe(out)
            self.assertLessEqual(max(pinfo["width"], pinfo["height"]), 1920)
            self.assertFalse(pinfo["ten_bit"] or pinfo["hdr"], pinfo)
            self.assertLessEqual(pinfo["fps"], 30.5)
            self.assertTrue(video_proxy.is_settled(src))
            self.assertEqual(g._procs, {}, "proces odhlášen ze strážce")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            video_proxy.clear_cache()


# ═════════════════════════════════════════════════════════════════════════════
# Náhledový pás: odložený během přehrávání, z proxy, povýšení při tažení
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestStripGovernance(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_strip_")
        self.src = os.path.join(self.tmp, "v.mov")
        open(self.src, "wb").write(b"x")
        self.mgr = scrub_strip.StripManager()
        self.started = []
        patcher = mock.patch.object(scrub_strip.StripBuilder, "start",
                                    lambda b, *a: self.started.append(b))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_deferred_request_is_governed_then_prioritized(self):
        self.mgr.request(self.src, priority=False)
        builder = self.started[-1]
        self.assertTrue(builder.governed, "po spuštění videa: čeká na klid, při přehrávání stojí")
        with mock.patch.object(builder, "isRunning", return_value=True):
            self.mgr.request(self.src, priority=True)          # tažení časové osy
        self.assertFalse(builder.governed, "tažení = hned, bez čekání")
        self.assertEqual(len(self.started), 1, "stejný pás se nestaví dvakrát")

    def test_strip_decodes_from_proxy_when_available(self):
        self.mgr.request(self.src)
        builder = self.started[-1]
        self.assertEqual(builder.decode_source(), self.src)
        os.makedirs(paths.PROXY_DIR, exist_ok=True)
        proxy = video_proxy.proxy_path(self.src)
        open(proxy, "wb").write(b"p")
        try:
            self.assertEqual(builder.decode_source(), proxy, "4K HDR originál se podruhé nedekóduje")
        finally:
            video_proxy.clear_cache()


# ═════════════════════════════════════════════════════════════════════════════
# Zmrazený zoom mřížky
# ═════════════════════════════════════════════════════════════════════════════

class TestLayouts(unittest.TestCase):

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_grid_scaled_and_blend_math(self):
        from PyQt6.QtCore import QRectF
        g = main._GridLayout(100, 4, 0, 0, 50, 40)
        self.assertEqual(g.rect(5), QRectF(100, 50, 100, 100))
        self.assertEqual(g.index_range(QRectF(0, 0, 400, 300)), (0, 15), "řádky 0–3 (posun 50)")
        s = main._ScaledLayout(g, 2.0, QPointF(150, 100))
        r = s.rect(5)
        self.assertEqual((r.x(), r.y(), r.width()), (50.0, 0.0, 200.0), "měřítko kolem kotvy")
        first, last = s.index_range(QRectF(0, 0, 400, 300))
        self.assertLessEqual(last - first, 15, "zvětšeno = méně dlaždic na obrazovce")
        b = main._BlendLayout(g, main._GridLayout(50, 8, 0, 0, 0, 40), 0.5)
        self.assertEqual(b.rect(0).width(), 75.0)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestFrozenZoom(unittest.TestCase):

    def setUp(self):
        self.grid = main.AppleGridView(None)
        recs = [(i, f"IMG_{i}.jpg", f"/nic/{i}.jpg", "photo", None, None, "2026-07-07", 0) for i in range(1, 301)]
        self.grid.photo_model.set_records(recs)
        self.grid.resize(1000, 700)
        self.grid.show()
        _app.processEvents()
        self.addCleanup(lambda: (self.grid.close(), self.grid.deleteLater()))

    def test_no_relayout_during_zoom_animation(self):
        relayouts = []
        self.grid.photo_model.layoutChanged.connect(lambda *a: relayouts.append(1))
        before = self.grid.columns()
        with mock.patch.object(motion, "reduced", return_value=False):
            self.grid.zoom_by(1, QPointF(500, 350))
            self.assertIsNotNone(self.grid._frozen, "během animace zmrazené rozvržení")
            self.assertIn("gesture", governor.get().reasons(), "pozadí počká")
            self.grid.viewport().repaint()                      # kreslení zmrazeného snímku
            wait(60)
            self.assertEqual(relayouts, [], "žádné setGridSize / layoutChanged za snímek")
            for _ in range(40):
                if self.grid._frozen is None:
                    break
                wait(25)
        self.assertIsNone(self.grid._frozen)
        self.assertEqual(len(relayouts), 1, "jedno skutečné rozvržení na konci")
        self.assertEqual(self.grid.columns(), before - 1)
        self.assertEqual(self.grid.cell_size, self.grid._cell_for_columns(before - 1))
        self.assertNotIn("gesture", governor.get().reasons())

    def test_touchpad_pinch_is_continuous_then_settles(self):
        before = self.grid.columns()
        with mock.patch.object(motion, "reduced", return_value=False):
            self.grid._pinch_begin(QPointF(500, 350))
            for _ in range(6):
                self.grid._pinch_update(self.grid._pinch_scale * 1.06)
            self.assertIsInstance(self.grid._frozen, main._ScaledLayout, "1:1 s prsty, bez rozvržení")
            self.grid._pinch_end(animate=False)
        self.assertIsNone(self.grid._frozen)
        self.assertLess(self.grid.columns(), before, "přiblížení = méně sloupců")

    def test_model_reset_settles_running_zoom(self):
        with mock.patch.object(motion, "reduced", return_value=False):
            self.grid.zoom_by(-1, QPointF(500, 350))
            self.assertIsNotNone(self.grid._frozen)
            self.grid.photo_model.set_records(self.grid.photo_model.records[:50])
        self.assertIsNone(self.grid._frozen, "nová data = zoom dosedne hned")
        self.assertNotIn("gesture", governor.get().reasons())


# ═════════════════════════════════════════════════════════════════════════════
# Režisér Cesty (map_vault.html)
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestJourneyDirector(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.m = ShownMapPage((1000, 700))

    @classmethod
    def tearDownClass(cls):
        cls.m.close()

    def js(self, code, timeout=5000):
        return self.m.js(code, timeout)

    def setUp(self):
        self.assertTrue(self.until("styleReady === true"))
        self.js("if (journey.active) window.endJourney(true); reduceMotion = false; stopSpin(); 0")

    def until(self, code, ms=4000):
        for _ in range(ms // 50):
            if self.js(code):
                return True
            wait(50)
        return False

    def test_van_wijk_endpoints_and_arc(self):
        r = self.js("(function () { var w = worldW(13), v = vanWijk([13.4, 49.2], [16.37, 48.2], w, w, VW_RHO);"
                    " var a = v.at(0), b = v.at(v.S), mid = v.at(v.S / 2);"
                    " return [a[0], a[1] / w, b[0], b[1] / w, zoomOfW(mid[1]), v.S > 0]; })()")
        self.assertAlmostEqual(r[0], 0, places=4)
        self.assertAlmostEqual(r[1], 1, places=4)
        self.assertAlmostEqual(r[2], 1, places=3)
        self.assertAlmostEqual(r[3], 1, places=3)
        self.assertLess(r[4], 11, "uprostřed letu výš (oblouk), ne přímka ve stejné výšce")
        self.assertTrue(r[5])

    def test_steering_is_frame_rate_independent(self):
        h = self.js("(function () { function sim(dt) { director.heading = 0;"
                    " for (var t = 0; t < 0.6; t += dt) steer(90, dt); return director.heading; }"
                    " return [sim(1 / 60), sim(1 / 120)]; })()")
        self.assertAlmostEqual(h[0], h[1], delta=1.0, msg="stejná rychlost natáčení při 60 i 120 Hz")
        self.assertGreater(h[0], 30)

    def test_pause_freezes_the_timeline(self):
        self.assertTrue(self.js(f"playJourney({json.dumps(TRIP)})"))
        wait(300)
        t1 = self.js("director.t")
        self.assertGreater(t1, 0.1)
        self.js("journeyPause(); 0")
        wait(300)
        t2 = self.js("director.t")
        self.assertAlmostEqual(t1, t2, delta=0.12, msg="pauza zastaví hodiny (kamera, trasa, události)")
        self.js("journeyPause(); 0")
        wait(250)
        self.assertGreater(self.js("director.t"), t2 + 0.1, "pokračuje odtud, kde stála")
        self.js("window.endJourney(true); 0")
        self.assertFalse(self.js("director.directing"))

    def test_route_drawn_by_gradient_on_camera_clock(self):
        self.js("POP_MS = 60; HOLD_MS = 60; DROP_MS = 60; 0")
        self.assertTrue(self.js(f"playJourney({json.dumps(TRIP)})"))
        self.js("reduceMotion = true; journeySkip(1); reduceMotion = false; journeySkip(1); 0")   # úsek 0 -> 1
        self.assertEqual(self.js("journeyState().index"), 1)
        self.assertTrue(self.js("!!map.getLayer('j-cur')"))
        grad = self.js("map.getPaintProperty('j-cur', 'line-gradient')")
        self.assertEqual(grad[0], "step", "projetá část = přechod barvy na line-progress, ne setData")
        self.assertTrue(self.until("journey.curFrac > 0.05", 6000), "čára roste s kamerou")
        self.js("HOLD_MS = 1700; POP_MS = 700; DROP_MS = 760; window.endJourney(true); 0")

    def test_perf_profile_drives_lod_and_resolution(self):
        self.assertEqual(self.js("setPerfProfile({tier: 'baseline', map_lod_levels: 4, map_lod_ratio: 2,"
                                 " map_pixel_ratio_max: 1.0, map_pixel_ratio_min: 0.75})"), "baseline")
        self.assertEqual(self.js("getTileLod()"), [4, 2])
        self.assertLessEqual(self.js("getPerfState().ratio"), 1.0)
        self.js("setPerfProfile({tier: 'high', map_lod_levels: 6, map_lod_ratio: 3,"
                " map_pixel_ratio_max: 0, map_pixel_ratio_min: 1.25}); 0")
        self.assertEqual(self.js("getTileLod()"), [6, 3])
        stats = self.js("getFrameStats()")
        self.assertEqual(set(stats), {"map", "journey"})


if __name__ == "__main__":
    unittest.main()
