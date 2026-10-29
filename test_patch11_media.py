"""Regresní testy pro Patch 11 – dávka B/C: náhledová vrstva 2048 px a přednačítání videa.

Spuštění (ve složce projektu):   python -m pytest test_patch11_media.py -v

Pokrývá:
  Náhledy     – platnost (novější než originál), zmenšení na 2048 px, LRU rozpočet, vymazání,
                load_image_file z náhledu, progresivní pipeline (náhled -> originál), pravidla
                zápisu (žádný podměrečný „2048px“ náhled), plakát videa, pořadí tvorby.
  Přednačtení – dva přehrávače, preload do neaktivního, prohození bez setSource, nikdy během
                přehrávání, signály neaktivního přehrávače mimo UI, přenos hlasitosti, směr
                listování, najetí myší v mřížce, pin D3D11VA.

Testy pracují VÝHRADNĚ v dočasných složkách (conftest přesměruje i paths.PREVIEW_DIR).
"""
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
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QSize, QUrl
    from PyQt6.QtGui import QImage, QColor
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import paths

if HAS_QT:
    import main
    import preview_cache
    import viewer_hud
    from viewer import MediaViewerWidget
    from PyQt6.QtMultimedia import QMediaPlayer


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_until(cond, timeout_ms=5000):
    end = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < end:
        if cond():
            return True
        wait(20)
    return cond()


def make_jpeg(path, w, h, color):
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*color))
    assert img.save(path, "JPG", 92)
    return path


def make_video(path, seconds=2):
    """Krátké testovací video (ffmpeg lavfi); None, když ffmpeg chybí."""
    if not os.path.exists(paths.FFMPEG_PATH):
        return None
    cmd = [paths.FFMPEG_PATH, "-loglevel", "quiet", "-y", "-f", "lavfi", "-i",
           f"testsrc=size=320x240:rate=25:duration={seconds}", "-pix_fmt", "yuv420p", path]
    subprocess.run(cmd, capture_output=True, timeout=60)
    return path if os.path.exists(path) and os.path.getsize(path) > 0 else None


def color_of(img):
    return QColor(img.pixel(img.width() // 2, img.height() // 2))


def media_record(i, path, media_type):
    """Záznam ve tvaru MEDIA_COLUMNS (id, name, path, type, paired, thumb, date, fav)."""
    return (i, os.path.basename(path), path, media_type, None, None, "2026-01-01", 0)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestPreviewCache(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p11m_")
        self._saved_dir = paths.PREVIEW_DIR
        paths.PREVIEW_DIR = os.path.join(self.tmp, "previews")

    def tearDown(self):
        paths.PREVIEW_DIR = self._saved_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, name="a.jpg", w=3000, h=2000, color=(200, 30, 30)):
        return make_jpeg(os.path.join(self.tmp, name), w, h, color)

    def test_preview_dir_follows_paths_at_call_time(self):
        self.assertTrue(preview_cache.preview_dir().startswith(self.tmp))
        self.assertTrue(preview_cache.preview_path(self._src()).startswith(self.tmp))

    def test_valid_only_when_newer_than_source(self):
        src = self._src()
        self.assertIsNone(preview_cache.get_preview(src))
        p = preview_cache.save_preview(src, QImage(src))
        self.assertEqual(preview_cache.get_preview(src), p)
        later = time.time() + 60
        os.utime(src, (later, later))               # originál upraven po vytvoření náhledu
        self.assertIsNone(preview_cache.get_preview(src), "upravená fotka nesmí ukázat starý náhled")

    def test_saved_preview_is_scaled_to_edge(self):
        src = self._src(w=4000, h=1000)
        p = preview_cache.save_preview(src, QImage(src))
        img = QImage(p)
        self.assertEqual(max(img.width(), img.height()), preview_cache.PREVIEW_EDGE)
        self.assertEqual(img.width(), 2048)
        self.assertEqual(img.height(), 512)
        self.assertFalse([f for f in os.listdir(preview_cache.preview_dir()) if f.endswith(".tmp")])

    def test_budget_evicts_least_recently_used_first(self):
        made = []
        for i in range(4):
            src = self._src(f"s{i}.jpg", 1200, 800, (i * 50, 100, 100))
            p = preview_cache.save_preview(src, QImage(src))
            os.utime(p, (1000 + i, 1000 + i))       # s0 nejdéle nepoužitý
            made.append(p)
        size_one = os.path.getsize(made[0])
        total = preview_cache.cache_size()
        budget_mb = (total - size_one) / (1024 * 1024)
        freed = preview_cache.enforce_budget(budget_mb)
        self.assertGreater(freed, 0)
        self.assertFalse(os.path.exists(made[0]), "nejstarší použití se maže první")
        self.assertTrue(os.path.exists(made[-1]))

    def test_clear_cache(self):
        src = self._src()
        preview_cache.save_preview(src, QImage(src))
        self.assertGreater(preview_cache.clear_cache(), 0)
        self.assertEqual(preview_cache.cache_size(), 0)
        self.assertTrue(os.path.exists(src), "originál zůstává nedotčen")

    def test_load_image_file_prefers_preview_within_edge(self):
        src = self._src(color=(200, 30, 30))
        # náhled záměrně jiné barvy, ať je poznat, odkud snímek přišel
        preview_cache.save_preview(src, make_and_load(os.path.join(self.tmp, "blue.jpg"), (30, 30, 200)))
        img = viewer_hud.load_image_file(src, QSize(1920, 1080))
        self.assertGreater(color_of(img).blue(), 150, "cíl <= 2048 px = náhled")
        orig = viewer_hud.load_image_file(src, QSize(1920, 1080), use_preview=False)
        self.assertGreater(color_of(orig).red(), 150)
        big = viewer_hud.load_image_file(src, QSize(3000, 3000))
        self.assertGreater(color_of(big).red(), 150, "cíl > 2048 px = originál")

    def _run_job(self, src, max_size):
        got = []
        job = viewer_hud._PipelineJob(src, max_size)
        job.signals.done.connect(lambda p, img: got.append(img))
        job.setAutoDelete(False)
        job.run()
        _app.processEvents()
        return got

    def test_pipeline_progressive_preview_then_original(self):
        src = self._src(w=4000, h=3000, color=(200, 30, 30))
        preview_cache.save_preview(src, make_and_load(os.path.join(self.tmp, "blue.jpg"), (30, 30, 200), 4000, 3000))
        got = self._run_job(src, QSize(3840, 2160))
        self.assertEqual(len(got), 2, "nejdřív náhled, pak ostrý originál")
        self.assertGreater(color_of(got[0]).blue(), 150)
        self.assertGreater(color_of(got[1]).red(), 150)
        self.assertEqual(len(self._run_job(src, QSize(1920, 1080))), 1, "náhled stačí – bez dekódu originálu")

    def test_pipeline_never_writes_undersized_preview(self):
        src = self._src(w=3000, h=2000)
        self._run_job(src, QSize(1600, 1600))       # duplicity dřív dekódovaly 1600 px
        self.assertIsNone(preview_cache.get_preview(src), "1600px dekód není 2048px náhled")
        self._run_job(src, QSize(2048, 2048))
        p = preview_cache.get_preview(src)
        self.assertIsNotNone(p)
        self.assertEqual(QImage(p).width(), 2048)

    def test_pipeline_writes_preview_from_full_small_original(self):
        src = self._src(w=900, h=600)
        self._run_job(src, QSize(1600, 1600))
        p = preview_cache.get_preview(src)
        self.assertIsNotNone(p, "celý (menší) originál je platný náhled")
        self.assertEqual(QImage(p).width(), 900)

    def test_video_poster(self):
        video = make_video(os.path.join(self.tmp, "clip.mp4"))
        if video is None:
            self.skipTest("ffmpeg není k dispozici")
        p = preview_cache.build_preview(video, "video")
        self.assertIsNotNone(p)
        self.assertEqual(QImage(p).width(), 320, "plakát v rozlišení videa (nezvětšuje se)")
        self.assertEqual(preview_cache.build_preview(video, "video"), p, "platný se nepřepočítává")

    def test_build_order_duplicates_then_events_then_library(self):
        class FakeConn:
            def execute(self, sql, params=()):
                return iter([("/lib/new.jpg", "photo"), ("/dup/a.jpg", "photo"), ("/lib/clip.mp4", "video")])

            def close(self):
                pass
        pairs = [(1, "/dup/a.jpg", 2, "/dup/b.jpg", 0.97)]
        cards = [{"cover": {"path": "/ev/cover.jpg"}}, {"cover": {"path": "/dup/b.jpg"}}, {}]
        with mock.patch.object(main, "get_connection", return_value=FakeConn()), \
                mock.patch("ai_engine.get_duplicate_pairs", return_value=pairs), \
                mock.patch.object(main, "fetch_event_cards", return_value=cards):
            order = main.preview_build_order()
        self.assertEqual([p for p, _ in order],
                         ["/dup/a.jpg", "/dup/b.jpg", "/ev/cover.jpg", "/lib/new.jpg", "/lib/clip.mp4"])
        self.assertEqual(order[-1][1], "video")

    def test_duplicates_review_decodes_at_preview_edge(self):
        view = main.DuplicateReviewView.__new__(main.DuplicateReviewView)
        self.assertEqual(main.DuplicateReviewView._review_size(view), QSize(2048, 2048))


def make_and_load(path, color, w=3000, h=2000):
    return QImage(make_jpeg(path, w, h, color))


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestVideoPrebuffer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_p11v_")
        cls.video_a = make_video(os.path.join(cls.tmp, "a.mp4"))
        cls.video_b = make_video(os.path.join(cls.tmp, "b.mp4"))
        cls.photo = make_jpeg(os.path.join(cls.tmp, "p.jpg"), 800, 600, (90, 90, 90))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.viewer = MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        _app.processEvents()
        self.addCleanup(self._close)
        if not self.viewer.can_preload():
            self.skipTest("QtQuick video není dostupné (jeden výstup)")

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def _need_videos(self):
        if not (self.video_a and self.video_b):
            self.skipTest("ffmpeg není k dispozici")

    def test_two_players_on_two_outputs(self):
        v = self.viewer
        self.assertEqual(len(v._players), 2)
        self.assertEqual([o.objectName() for o in v.canvas.video_outputs], ["video", "video2"])
        self.assertIs(v.player.videoOutput(), v.canvas.video_output)
        self.assertIs(v._players[1].videoOutput(), v.canvas.video_outputs[1])
        self.assertFalse(v.canvas.video_outputs[1].property("visible"), "záložní výstup je skrytý")

    def test_preload_then_open_swaps_without_set_source(self):
        self._need_videos()
        v = self.viewer
        v.media_list = [media_record(1, self.photo, "photo"), media_record(2, self.video_a, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(v.preload(self.video_a))
        spare = v._players[1]
        self.assertEqual(spare.source().toLocalFile().lower(), QUrl.fromLocalFile(self.video_a).toLocalFile().lower())
        self.assertTrue(wait_until(lambda: v._preload_ready), "záložní přehrávač připravil 1. snímek")
        with mock.patch.object(spare, "setSource") as set_source:
            v.show_next()
        set_source.assert_not_called()
        self.assertEqual(v._active, 1)
        self.assertIs(v.player, spare)
        self.assertIs(v.player.videoOutput(), v.canvas.video_output)
        self.assertTrue(v.canvas.video_outputs[1].property("visible"))
        self.assertFalse(v.canvas.video_outputs[0].property("visible"))
        self.assertIsNone(v._preloaded_path)
        self.assertTrue(wait_until(v.canvas.is_video_shown, 2000), "video odkryto hned po prohození")

    def test_no_preload_while_playing(self):
        self._need_videos()
        v = self.viewer
        with mock.patch.object(v.player, "playbackState", return_value=QMediaPlayer.PlaybackState.PlayingState):
            self.assertFalse(v.preload(self.video_b))
        self.assertIsNone(v._preloaded_path)
        self.assertTrue(v._players[1].source().isEmpty())

    def test_spare_player_signals_do_not_reach_ui(self):
        v = self.viewer
        v.timeline_slider.setRange(0, 10000)
        v.timeline_slider.setValue(0)
        v._players[1].positionChanged.emit(4321)
        v._players[1].durationChanged.emit(9999)
        self.assertEqual(v.timeline_slider.value(), 0)
        self.assertEqual(v.timeline_slider.maximum(), 10000)
        # Patch 13: osa se řídí jen aktivním přehrávačem se známou délkou (bez ní se pozice ignoruje)
        with mock.patch.object(v._players[0], "duration", return_value=10000):
            v._players[0].positionChanged.emit(1234)
        self.assertEqual(v.timeline_slider.value(), 1234)

    def test_swap_carries_volume_and_mute(self):
        self._need_videos()
        v = self.viewer
        v.on_volume_changed(35)
        v.toggle_mute()
        self.assertTrue(v.preload(self.video_a))
        v._swap_to_preloaded()
        self.assertAlmostEqual(v.audio_output.volume(), 0.35, places=2)
        self.assertTrue(v.audio_output.isMuted())
        v.toggle_mute()
        self.assertFalse(v.audio_output.isMuted())

    def test_neighbor_preload_follows_direction(self):
        self._need_videos()
        v = self.viewer
        v.media_list = [media_record(1, self.video_b, "video"), media_record(2, self.photo, "photo"),
                        media_record(3, self.video_a, "video")]
        v.current_index = 1
        with mock.patch.object(v, "preload") as preload:
            v._last_step = 1
            v._preload_neighbor()
            preload.assert_called_once_with(self.video_a)
            preload.reset_mock()
            v._last_step = -1
            v._preload_neighbor()
            preload.assert_called_once_with(self.video_b)

    def test_hidden_viewer_does_not_preload_neighbors(self):
        v = self.viewer
        v.media_list = [media_record(1, self.photo, "photo"), media_record(2, self.video_a or self.photo, "video")]
        v.current_index = 0
        v.hide()
        with mock.patch.object(v, "preload") as preload:
            v._preload_neighbor()
        preload.assert_not_called()


class _FakePlayer:
    def __init__(self, playing=True):
        self.playing = playing
        self.seeks, self.calls = [], []

    def playbackState(self):
        return QMediaPlayer.PlaybackState.PlayingState if self.playing else QMediaPlayer.PlaybackState.PausedState

    def pause(self):
        self.playing = False
        self.calls.append("pause")

    def play(self):
        self.playing = True
        self.calls.append("play")

    def setPosition(self, pos):
        self.seeks.append(pos)


class _FakeAudio:
    def __init__(self, muted=False):
        self.muted = muted

    def isMuted(self):
        return self.muted

    def setMuted(self, m):
        self.muted = bool(m)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestVideoScrubber(unittest.TestCase):
    """Patch 12: tažení časové osy bez zahlcení dekodéru (4K HEVC / Dolby Vision)."""

    def make(self, playing=True, muted=False):
        self.player, self.audio = _FakePlayer(playing), _FakeAudio(muted)
        self.now = [100.0]
        s = viewer_hud.VideoScrubber(lambda: self.player, lambda: self.audio, clock=lambda: self.now[0])
        self.scrubber = s                              # bez rodiče: drží ho test (jinak GC i s časovačem)
        self.addCleanup(lambda: self.scrubber.timer.stop())
        return s

    def test_drag_pauses_mutes_and_release_restores(self):
        s = self.make(playing=True, muted=False)
        s.move(1000)
        self.assertTrue(s.active)
        self.assertEqual(self.player.calls[0], "pause", "během tažení se nepřehrává")
        self.assertTrue(self.audio.muted, "během tažení ticho – žádné zaseknuté audio buffery")
        s.end(5000)
        self.assertFalse(s.active)
        self.assertFalse(self.audio.muted, "zvuk se vrátí")
        self.assertEqual(self.player.calls[-1], "play", "přehrávání pokračuje")
        self.assertEqual(self.player.seeks[-1], 5000, "přesný přesun na konečnou pozici")

    def test_one_seek_in_flight_latest_target_wins(self):
        s = self.make()
        s.move(1000)
        self.assertEqual(self.player.seeks, [1000], "první přesun hned (okamžitá odezva)")
        for pos in range(1100, 9000, 100):           # rychlé tažení: ~80 pohybů
            s.move(pos)
            s._pump()                                  # i tik časovače: dekodér ještě pracuje
        self.assertEqual(self.player.seeks, [1000], "dokud dekodér neohlásí snímek, nic dalšího")
        s.frame_ready()
        s._pump()
        self.assertEqual(self.player.seeks, [1000, 8900], "mezilehlé cíle se přeskočí – jen nejnovější")

    def test_rate_capped_by_timer(self):
        s = self.make()
        self.assertLessEqual(1000 / s.timer.interval(), viewer_hud.SCRUB_MAX_HZ + 0.5)
        self.assertGreaterEqual(1000 / s.timer.interval(), 12, "pořád plynulý náhled (≥12 Hz)")
        s.move(0)
        for i in range(1, 6):                          # 5 tiků časovače, dekodér vždy stihne
            s.frame_ready()
            s.move(i * 1000)
            s.move(i * 1000 + 500)
            s._pump()
        self.assertEqual(self.player.seeks, [0, 1500, 2500, 3500, 4500, 5500], "nejvýš jeden přesun na tik")

    def test_stalled_decoder_does_not_freeze_scrub(self):
        s = self.make()
        s.move(1000)
        s.move(2000)
        self.now[0] += (viewer_hud.SCRUB_SEEK_TIMEOUT_MS - 50) / 1000
        s._pump()
        self.assertEqual(self.player.seeks, [1000])
        self.now[0] += 0.1                             # snímek nepřišel ani po limitu
        s._pump()
        self.assertEqual(self.player.seeks, [1000, 2000])

    def test_no_duplicate_seek_on_release(self):
        s = self.make()
        s.move(3000)
        s.end(3000)                                    # klik bez tažení
        self.assertEqual(self.player.seeks, [3000])

    def test_paused_video_stays_paused(self):
        s = self.make(playing=False, muted=True)
        s.move(1000)
        s.end(1200)
        self.assertNotIn("play", self.player.calls)
        self.assertTrue(self.audio.muted, "ztlumené video zůstane ztlumené")

    def test_cancel_restores_sound_without_seek_or_play(self):
        s = self.make(playing=True)
        s.move(1000)
        s.move(2000)
        s.cancel()
        self.assertFalse(s.active)
        self.assertFalse(self.audio.muted)
        self.assertEqual(self.player.seeks, [1000])
        self.assertNotIn("play", self.player.calls)

    def test_ffmpeg_warnings_silenced(self):
        if sys.platform != "win32":
            self.skipTest("jen Windows (avutil z PyQt6)")
        self.assertTrue(viewer_hud.quiet_ffmpeg_logs())
        self.assertTrue(viewer_hud.quiet_ffmpeg_logs(), "opakované volání nic nerozbije")
        import ctypes
        import glob
        import PyQt6
        dll = glob.glob(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "bin", "avutil-*.dll"))[0]
        self.assertEqual(ctypes.CDLL(dll).av_log_get_level(), viewer_hud.AV_LOG_ERROR,
                         "varování (Dolby Vision RPU) se nevypisují, chyby ano")


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestViewerScrubbing(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_p12s_")
        cls.video = make_video(os.path.join(cls.tmp, "scrub.mp4"), seconds=4)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not self.video:
            self.skipTest("ffmpeg není k dispozici")
        self.viewer = MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        self.addCleanup(self._close)
        self.viewer.media_list = [media_record(1, self.video, "video")]
        self.viewer.current_index = 0
        self.viewer.load_current_item()
        self.assertTrue(wait_until(lambda: self.viewer.player.duration() > 3000, 5000))
        self.assertTrue(wait_until(lambda: self.viewer.player.playbackState()
                                   == QMediaPlayer.PlaybackState.PlayingState, 3000))

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def test_drag_does_not_flood_decoder(self):
        v = self.viewer
        real_set = v.player.setPosition
        seeks = []
        with mock.patch.object(v.player, "setPosition", side_effect=lambda p: (seeks.append(p), real_set(p))):
            for pos in range(0, 3000, 50):            # 60 pohybů myši bez prodlevy
                v.scrub_to(pos)
            self.assertLessEqual(len(seeks), 2, "60 pohybů ≠ 60 přesunů dekodéru")
            self.assertTrue(v.audio_output.isMuted(), "během tažení ticho")
            self.assertNotEqual(v.player.playbackState(), QMediaPlayer.PlaybackState.PlayingState)
            self.assertEqual(v.lbl_time_cur.text(), "00:02", "čas ukazuje cíl tažení hned")
            v.timeline_slider.setValue(2950)
            v.player.positionChanged.emit(100)         # pozdní hlášení dekodéru
            self.assertEqual(v.timeline_slider.value(), 2950, "úchyt drží uživatel, neskáče zpět")
            v.end_scrub(2950)
        self.assertEqual(seeks[-1], 2950, "po puštění přesný přesun na konečnou pozici")
        self.assertFalse(v.audio_output.isMuted())
        self.assertTrue(wait_until(lambda: v.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState))
        self.assertTrue(wait_until(lambda: v.player.position() >= 2900, 3000))

    def test_slider_release_always_ends_scrub(self):
        from PyQt6.QtTest import QTest
        from PyQt6.QtCore import QPoint
        v = self.viewer
        s = v.timeline_slider
        got = []
        s.mouse_released.connect(got.append)
        y = s.height() // 2
        QTest.mousePress(s, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(s.width() // 4, y))
        self.assertTrue(v._scrubber.active, "stisk na ose = začátek tažení")
        QTest.mouseRelease(s, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(s.width() // 2, y))
        self.assertEqual(len(got), 1)
        self.assertFalse(v._scrubber.active, "puštění vždy ukončí tažení (zvuk se vrátí)")
        self.assertFalse(v.audio_output.isMuted())

    def test_navigation_mid_drag_restores_sound(self):
        v = self.viewer
        v.scrub_to(1000)
        self.assertTrue(v.audio_output.isMuted())
        v._halt_playback()                            # šipka na další položku během tažení
        self.assertFalse(v._scrubber.active)
        self.assertFalse(v.audio_output.isMuted())


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestTimelineSync(unittest.TestCase):
    """Patch 13: časová osa musí odpovídat AKTIVNÍMU přehrávači – i po prohození za předem
    načtené video (jeho durationChanged přišel, když byl neaktivní, a UI ho zahodilo)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_p13t_")
        cls.short = make_video(os.path.join(cls.tmp, "short4.mp4"), seconds=4)
        cls.long = make_video(os.path.join(cls.tmp, "long6.mp4"), seconds=6)
        cls.photo = make_jpeg(os.path.join(cls.tmp, "p.jpg"), 800, 600, (90, 90, 90))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not (self.short and self.long):
            self.skipTest("ffmpeg není k dispozici")
        self.viewer = MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        _app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def _playing(self):
        return wait_until(lambda: self.viewer.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                          and self.viewer.player.position() > 0, 5000)

    def assert_timeline_matches_player(self, msg=""):
        v = self.viewer
        dur = v.player.duration()
        self.assertGreater(dur, 0, msg)
        self.assertEqual(v.timeline_slider.maximum(), dur, f"rozsah osy = délka aktivního videa {msg}")
        self.assertLessEqual(abs(v.timeline_slider.value() - v.player.position()), 400,
                             f"úchyt = pozice přehrávání {msg}")
        self.assertEqual(v.lbl_time_total.text(), v.format_time(dur), msg)

    def test_preloaded_video_gets_its_own_timeline(self):
        v = self.viewer
        if not v.can_preload():
            self.skipTest("jeden přehrávač")
        v.media_list = [media_record(1, self.photo, "photo"), media_record(2, self.long, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(v.preload(self.long))
        self.assertTrue(wait_until(lambda: v._players[1 - v._active].duration() > 0, 5000))
        v.show_next()                                  # prohození za předem načtené video
        self.assertTrue(self._playing())
        wait(300)
        self.assert_timeline_matches_player("po prohození")
        self.assertLess(v.timeline_slider.value(), v.timeline_slider.maximum() / 2,
                        "video hraje od začátku -> úchyt vlevo, ne na konci")

    def test_switch_between_videos_of_different_length(self):
        v = self.viewer
        v.media_list = [media_record(1, self.short, "video"), media_record(2, self.long, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(self._playing())
        wait(200)
        self.assert_timeline_matches_player("krátké video")
        if v.can_preload():
            wait_until(lambda: v._preloaded_path is not None, 3000)   # soused se přednačte po pauze
        v.show_next()
        self.assertTrue(self._playing())
        wait(300)
        self.assert_timeline_matches_player("delší video")
        v.show_prev()
        self.assertTrue(self._playing())
        wait(300)
        self.assert_timeline_matches_player("zpět na krátké")

    def test_drag_and_release_stays_where_dropped(self):
        v = self.viewer
        if not v.can_preload():
            self.skipTest("jeden přehrávač")
        v.media_list = [media_record(1, self.photo, "photo"), media_record(2, self.long, "video")]
        v.current_index = 0
        v.load_current_item()
        v.preload(self.long)
        wait_until(lambda: v._players[1 - v._active].duration() > 0, 5000)
        v.show_next()
        self.assertTrue(self._playing())
        v.scrub_to(3000)
        v.end_scrub(3000)
        self.assertTrue(wait_until(lambda: v.player.position() >= 2900, 3000))
        wait(200)
        self.assertGreater(v.timeline_slider.value(), 2800, "úchyt zůstane, kde byl puštěn")
        self.assertLess(v.timeline_slider.value(), v.timeline_slider.maximum() - 1500,
                        "nesmí odskočit na konec")

    def test_new_item_resets_timeline_before_playback(self):
        v = self.viewer
        v.media_list = [media_record(1, self.short, "video"), media_record(2, self.long, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(self._playing())
        wait(800)
        self.assertGreater(v.timeline_slider.value(), 300)
        with mock.patch.object(v, "_start_media"):     # ještě nic nenačteno – jen nová položka
            v.show_next()
        self.assertEqual(v.timeline_slider.value(), 0, "nová položka začíná vlevo")
        self.assertEqual(v.lbl_time_cur.text(), "00:00")

    def test_stray_position_without_duration_is_ignored(self):
        v = self.viewer
        v.media_list = [media_record(1, self.photo, "photo")]
        v.current_index = 0
        v.load_current_item()
        self.assertEqual(v.player.duration(), 0)
        v._is_video = True                             # „video“, ale délka ještě neznámá
        v.player.positionChanged.emit(5000)            # zbloudilé hlášení
        self.assertEqual(v.timeline_slider.value(), 0)
        self.assertEqual(v.lbl_time_cur.text(), "00:00")
        v.scrub_to(1500)                               # tažení bez známé délky nic nedělá
        self.assertFalse(v._scrubber.active)

    def test_position_is_clamped_to_duration(self):
        v = self.viewer
        v.media_list = [media_record(1, self.short, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(self._playing())
        dur = v.player.duration()
        v.player.positionChanged.emit(dur + 5000)      # hlášení za koncem (zaokrouhlení demuxeru)
        self.assertEqual(v.timeline_slider.value(), dur)
        v.player.positionChanged.emit(-40)
        self.assertEqual(v.timeline_slider.value(), 0)


def build_strip_sync(path, timeout_ms=60000):
    import scrub_strip
    strip = scrub_strip.ScrubStrip(path)
    builder = scrub_strip.StripBuilder(strip)
    done = {}
    loop = QEventLoop()
    builder.finished_strip.connect(lambda s, ok: (done.__setitem__("ok", ok), loop.quit()))
    builder.start()
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    builder.wait(5000)
    return strip, done.get("ok")


def jpeg_bytes(w, h, color):
    from PyQt6.QtCore import QBuffer, QIODevice
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(*color))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "JPG", 80)
    return bytes(buf.data())


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestScrubStrip(unittest.TestCase):
    """Patch 14: náhledový pás – datová struktura, soubor, platnost."""

    def setUp(self):
        import scrub_strip
        self.S = scrub_strip
        self.tmp = tempfile.mkdtemp(prefix="omni_p14s_")
        self._saved = paths.PREVIEW_DIR
        paths.PREVIEW_DIR = os.path.join(self.tmp, "previews")
        self.src = os.path.join(self.tmp, "clip.mp4")
        with open(self.src, "wb") as f:
            f.write(b"video")
        old = time.time() - 60
        os.utime(self.src, (old, old))

    def tearDown(self):
        paths.PREVIEW_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_insertion_keeps_order_and_skips_near_duplicates(self):
        s = self.S.ScrubStrip(self.src, 5000)
        for t in (2000, 0, 4000, 1000):                # klíčové snímky v libovolném pořadí
            self.assertTrue(s.add(t, jpeg_bytes(48, 27, (t // 20, 0, 0))))
        self.assertTrue(s.add(1500, jpeg_bytes(48, 27, (0, 99, 0)), min_gap_ms=40))   # hustý průchod
        self.assertFalse(s.add(1020, jpeg_bytes(48, 27, (0, 0, 0)), min_gap_ms=40), "už je 1000 ms")
        self.assertEqual(s.times, [0, 1000, 1500, 2000, 4000])

    def test_nearest_and_tolerance(self):
        s = self.S.ScrubStrip(self.src, 10000)
        self.assertEqual(s.nearest(500), (None, None))
        s.add(0, jpeg_bytes(48, 27, (200, 0, 0)))
        s.add(3000, jpeg_bytes(48, 27, (0, 200, 0)))
        self.assertEqual(s.nearest(1400)[1], 0)
        self.assertEqual(s.nearest(1600)[1], 3000)
        img = s.frame_near(2900, 1200)
        self.assertGreater(QColor(img.pixel(10, 10)).green(), 150)
        self.assertIsNone(s.frame_near(8000, 1200), "pás tam ještě není -> živý přesun")
        self.assertIs(s.frame_near(2950, 1200), img, "dekódovaný snímek z cache")

    def test_save_load_roundtrip_in_preview_cache(self):
        s = self.S.ScrubStrip(self.src, 3000)
        for t in (0, 1000, 2000):
            s.add(t, jpeg_bytes(64, 36, (t // 10, 50, 50)))
        s.dense = True
        path = s.save()
        self.assertEqual(os.path.dirname(path), preview_cache.preview_dir(),
                         "jeden soubor v mezipaměti náhledů (rozpočet LRU i mazání)")
        loaded = self.S.ScrubStrip.load(self.src)
        self.assertEqual(loaded.times, [0, 1000, 2000])
        self.assertTrue(loaded.complete and loaded.dense)
        self.assertEqual(loaded.duration_ms, 3000)
        self.assertEqual(loaded.image(1).width(), 64)
        self.assertGreater(preview_cache.clear_cache(), 0)
        self.assertIsNone(self.S.ScrubStrip.load(self.src), "vymazání mezipaměti smaže i pásy")

    def test_stale_or_foreign_file_is_ignored(self):
        s = self.S.ScrubStrip(self.src, 1000)
        s.add(0, jpeg_bytes(32, 18, (1, 2, 3)))
        path = s.save()
        later = time.time() + 120
        os.utime(self.src, (later, later))             # video upraveno po vytvoření pásu
        self.assertIsNone(self.S.ScrubStrip.load(self.src))
        os.utime(self.src, (time.time() - 600,) * 2)
        with open(path, "r+b") as f:
            f.write(b"JUNK")
        self.assertIsNone(self.S.ScrubStrip.load(self.src), "cizí / poškozený soubor")


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestStripBuilder(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_p14b_")
        cls.video = make_video(os.path.join(cls.tmp, "clip.mp4"), seconds=4)
        cls.other = make_video(os.path.join(cls.tmp, "other.mp4"), seconds=3)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if not self.video:
            self.skipTest("ffmpeg není k dispozici")
        import scrub_strip
        self.S = scrub_strip

    def test_short_video_gets_dense_strip(self):
        strip, ok = build_strip_sync(self.video)
        self.assertTrue(ok)
        self.assertTrue(strip.complete and strip.dense, "krátké video = hustý průchod")
        self.assertGreater(strip.duration_ms, 3500)
        self.assertGreaterEqual(len(strip), 4 * self.S.DENSE_FPS - 4, "~12 snímků za sekundu")
        self.assertEqual(strip.times, sorted(strip.times))
        self.assertLessEqual(strip.times[-1], strip.duration_ms)
        gaps = [b - a for a, b in zip(strip.times, strip.times[1:])]
        self.assertLessEqual(max(gaps), 200, "žádné sekundové skoky")
        img = strip.image(len(strip) // 2)
        self.assertEqual((img.width(), img.height()), (320, 240), "menší video se nezvětšuje")
        loaded = self.S.ScrubStrip.load(self.video)
        self.assertIsNotNone(loaded, "uloženo do mezipaměti")
        self.assertEqual(loaded.times, strip.times)

    def test_manager_builds_once_and_reuses_disk(self):
        mgr = self.S.StripManager()
        self.addCleanup(mgr.shutdown)
        preview_cache.clear_cache()
        strip = mgr.request(self.other)
        self.assertIsNotNone(strip)
        self.assertFalse(strip.complete)
        self.assertIs(mgr.request(self.other), strip, "druhý požadavek nic znovu nespouští")
        self.assertTrue(wait_until(lambda: strip.complete, 30000))
        fresh = self.S.StripManager()
        self.addCleanup(fresh.shutdown)
        again = fresh.request(self.other)
        self.assertTrue(again.complete, "z disku, bez ffmpeg")
        self.assertIsNone(fresh._builder)

    def test_newest_request_wins(self):
        mgr = self.S.StripManager()
        self.addCleanup(mgr.shutdown)
        preview_cache.clear_cache()
        mgr.request(self.video)
        self.assertTrue(mgr._building(self.video))
        mgr.request(self.other)                         # uživatel přešel na jiné video
        self.assertFalse(mgr._building(self.video) and mgr._builder._cancel is False,
                         "rozpracovaný pás předchozího videa se zruší")
        self.assertTrue(wait_until(lambda: mgr.get(self.other) is not None and mgr.get(self.other).complete, 30000),
                        "aktuální video dostane pás")
        self.assertTrue(wait_until(lambda: mgr._builder is None, 5000))
        self.assertEqual(mgr._queue, [], "nic dalšího nečeká")


class _ScrubPreview:
    def __init__(self, covered=range(0, 10**9)):
        self.covered, self.shown = covered, []

    def __call__(self, pos):
        if pos in self.covered:
            self.shown.append(pos)
            return True
        return False


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestTrickplayScrubber(unittest.TestCase):
    """Patch 14: s pásem žádné přesuny dekodéru během tažení; přesně až po zastavení / puštění."""

    def make(self, preview):
        self.player, self.audio = _FakePlayer(True), _FakeAudio(False)
        s = viewer_hud.VideoScrubber(lambda: self.player, lambda: self.audio, preview_fn=preview, rest_ms=40)
        self.scrubber = s
        self.addCleanup(lambda: (self.scrubber.timer.stop(), self.scrubber.rest_timer.stop()))
        return s

    def test_drag_uses_strip_and_never_seeks(self):
        prev = _ScrubPreview()
        s = self.make(prev)
        for pos in range(0, 6000, 50):
            s.move(pos)
        self.assertEqual(self.player.seeks, [], "120 pohybů, 0 přesunů dekodéru")
        self.assertEqual(len(prev.shown), 120)
        self.assertFalse(s.show_live, "zastaralý snímek z dekodéru nesmí přebít náhled")
        self.assertTrue(self.audio.muted)

    def test_rest_requests_exact_frame_once(self):
        s = self.make(_ScrubPreview())
        s.move(1000)
        s.move(2500)
        self.assertTrue(wait_until(lambda: self.player.seeks, 1000))
        self.assertEqual(self.player.seeks, [2500], "po zastavení přesně na pozici náhledu")
        self.assertTrue(s.show_live)
        wait(100)
        self.assertEqual(self.player.seeks, [2500], "jen jednou")
        s.move(4000)                                    # znovu táhne
        self.assertFalse(s.show_live)

    def test_release_after_previews_always_seeks(self):
        s = self.make(_ScrubPreview())
        s.move(3000)
        self.assertTrue(wait_until(lambda: self.player.seeks, 1000))      # přesný snímek na 3000
        s.move(3000)                                    # náhled znovu přes přesný snímek
        self.assertTrue(s.end(3000), "malý náhled je vidět -> přesný snímek i na stejném místě")
        self.assertEqual(self.player.seeks, [3000, 3000])
        self.assertEqual(self.player.calls[-1], "play")
        self.assertFalse(self.audio.muted)

    def test_falls_back_to_live_seeks_where_strip_missing(self):
        prev = _ScrubPreview(covered=range(0, 2000))    # pás se teprve tvoří (jen začátek)
        s = self.make(prev)
        s.move(500)
        self.assertEqual(self.player.seeks, [])
        s.move(5000)                                    # mimo pás
        self.assertTrue(s.show_live)
        self.assertEqual(self.player.seeks, [5000], "bez náhledu živý přesun (omezený)")
        s.move(5100)
        self.assertEqual(self.player.seeks, [5000], "pořád jen jeden přesun najednou")


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestViewerTrickplay(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="omni_p14v_")
        cls.video = make_video(os.path.join(cls.tmp, "trick.mp4"), seconds=4)
        if cls.video:
            build_strip_sync(cls.video)                 # pás předem (uloží se do mezipaměti)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import scrub_strip
        if not self.video or scrub_strip.ScrubStrip.load(self.video) is None:
            self.skipTest("ffmpeg / pás není k dispozici")
        self.viewer = MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        self.addCleanup(self._close)
        v = self.viewer
        v.media_list = [media_record(1, self.video, "video")]
        v.current_index = 0
        v.load_current_item()
        self.assertTrue(wait_until(lambda: v.player.duration() > 3000 and v.player.position() > 0, 5000))
        v._request_strip()
        self.assertTrue(wait_until(lambda: v._strips.get(self.video) is not None
                                   and v._strips.get(self.video).complete, 5000))
        self.assertTrue(wait_until(v.canvas.is_video_shown, 3000))

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def test_drag_shows_strip_frames_without_decoder(self):
        v = self.viewer
        seeks = []
        real_set = v.player.setPosition
        with mock.patch.object(v.player, "setPosition", side_effect=lambda p: (seeks.append(p), real_set(p))):
            keys = set()
            for pos in range(200, 3800, 60):
                v.scrub_to(pos)
                keys.add(v.canvas.photo_item.pixmap().cacheKey())
            self.assertEqual(seeks, [], "žádný přesun dekodéru během tažení")
            self.assertTrue(v.canvas.is_showing_scrub_frame())
            self.assertFalse(v.canvas.is_video_shown(), "náhled z pásu nad videem")
            self.assertGreater(len(keys), 25, "obraz se mění s kurzorem (hustý pás)")
            self.assertEqual(v.lbl_time_cur.text(), "00:03")
            self.assertTrue(wait_until(lambda: len(seeks) == 1, 1500), "kurzor stojí -> přesný snímek")
            self.assertTrue(wait_until(v.canvas.is_video_shown, 1500), "přesný 4K snímek nahradí náhled")
            v.scrub_to(1000)
            self.assertFalse(v.canvas.is_video_shown())
            v.end_scrub(1000)
        self.assertEqual(seeks[-1], 1000)
        self.assertTrue(wait_until(lambda: v.canvas.is_video_shown() and not v.canvas.is_showing_scrub_frame(), 2000))
        self.assertEqual(v.canvas.photo_item.scale(), 1.0, "plakát vrácen v plném rozlišení")
        self.assertFalse(v.audio_output.isMuted())
        self.assertTrue(wait_until(lambda: v.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState))

    def test_scrub_frame_keeps_poster_source(self):
        v = self.viewer
        poster = v.canvas._sources[v.canvas.photo_item]
        v.scrub_to(2000)
        self.assertTrue(v.canvas.is_showing_scrub_frame())
        self.assertIs(v.canvas._sources[v.canvas.photo_item], poster, "náhled nepřepíše uložený obraz")
        v.end_scrub(2000)

    def test_leaving_mid_drag_clears_strip_frame(self):
        v = self.viewer
        v.scrub_to(2500)
        self.assertTrue(v.canvas.is_showing_scrub_frame())
        v._halt_playback()
        self.assertFalse(v.canvas.is_showing_scrub_frame())
        self.assertFalse(v.audio_output.isMuted())


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestQtNoiseFilter(unittest.TestCase):

    def test_only_the_ffmpeg_disconnect_noise_is_dropped(self):
        import io
        from PyQt6.QtCore import qWarning
        # jiný test (test_patch9) mohl filtr nahradit vlastním handlerem a pak qInstallMessageHandler(None)
        viewer_hud._qt_filter_installed = False
        viewer_hud.quiet_qt_ffmpeg_noise()
        buf = io.StringIO()
        with mock.patch.object(sys, "stderr", buf):
            qWarning(viewer_hud.QT_FFMPEG_NOISE[0] + "PlaybackEngine::objectDestroyed")
            qWarning("real warning must pass")         # qWarning v PyQt6 bere jen ASCII
        out = buf.getvalue()
        self.assertNotIn("wildcard", out)
        self.assertIn("real warning must pass", out)


@unittest.skipUnless(HAS_QT, "PyQt6 není k dispozici")
class TestPrebufferWiring(unittest.TestCase):

    def test_d3d11va_pinned_on_windows(self):
        if sys.platform != "win32":
            self.skipTest("jen Windows")
        self.assertEqual(os.environ.get("QT_FFMPEG_DECODING_HW_DEVICE_TYPES"), "d3d11va")

    def test_qml_has_two_outputs(self):
        with open(viewer_hud._video_qml_path(), encoding="utf-8") as f:
            qml = f.read()
        self.assertIn('objectName: "video"', qml)
        self.assertIn('objectName: "video2"', qml)

    def test_grid_hover_preloads_video_in_viewer(self):
        win = mock.MagicMock()
        win._viewer = mock.MagicMock()
        win.nav.depth.return_value = 0
        tmp = tempfile.mkdtemp(prefix="omni_p11h_")
        try:
            clip = os.path.join(tmp, "c.mp4")
            open(clip, "wb").close()
            win._filtered_records = [media_record(1, os.path.join(tmp, "x.jpg"), "photo"),
                                     media_record(2, clip, "video")]
            win._hover_row = 1
            main.MainWindow._preload_hovered(win)
            win._viewer.preload.assert_called_once_with(clip)
            win._viewer.preload.reset_mock()
            win._hover_row = 0                  # fotka – nic
            main.MainWindow._preload_hovered(win)
            win.nav.depth.return_value = 1      # prohlížeč otevřen – mřížka nepřednačítá
            win._hover_row = 1
            main.MainWindow._preload_hovered(win)
            win._viewer.preload.assert_not_called()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
