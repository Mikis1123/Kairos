"""Sprint 1 (2026-11-05): immediate UX wins.

1. E key: after a Quick Edit the keyboard focus returns to the viewer. Field bug: a clicked Done
   kept focus, and when the panel hid, Qt passed focus on to the sidebar (NavItem). E, Esc and the
   arrows then stopped working until the viewer was reopened.
2. Teardrop pins: the sharp tip is the coordinate. The image is bottom-anchored and the tip is
   drawn on its bottom row. Hit test, hover card and spring sprites follow the new geometry.
3. Cluster click zooms only as far as the cluster needs to split, in a short flight. The hover
   card is interactive: its thumbnails open a photo and "Show N photos" opens the whole cluster.

Sprint 2 (same day): zero-blank scrolling.

4. decode_queue: decodes are served nearest the viewport centre first, then ahead of the scroll.
   Rows that scrolled away are dropped at once, and rows ahead are prefetched.
5. micro_previews: a ~1 kB preview per photo, held in RAM. A tile still decoding shows it
   instead of a grey rectangle. Hidden items never get one, and hiding purges it (secure_delete).
"""
import json
import sys
import time
import unittest
from unittest import mock

import pytest

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer
    from PyQt6.QtTest import QTest
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401 – before QApplication
    from PyQt6.QtWidgets import QApplication
    HAS_QT = True
except Exception:                                       # pragma: no cover
    HAS_QT = False

pytestmark = pytest.mark.skipif(not HAS_QT, reason="Qt not available")

if HAS_QT:
    import main


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


_APP = []


def _app():
    if not _APP:                      # keep a reference: a collected QApplication deletes its QObjects
        _APP.append(QApplication.instance() or QApplication(sys.argv[:1]))   # [] kills QtWebEngine (exit 127)
    return _APP[0]


# ── 1. E key focus ────────────────────────────────────────────────────────────

class TestEditKeyFocus(unittest.TestCase):
    """The real MainWindow: the viewer is reused, so focus has somewhere to escape to (the sidebar)."""

    # One window for the class: a second shown MainWindow in the same process (viewer RHI surface
    # + QtWebEngine) ends the interpreter with exit 127.
    @classmethod
    def setUpClass(cls):
        _app()
        from test_patch9 import TempEnv
        cls.env = TempEnv("setUp")
        cls.env.setUp()
        cls.ids = []
        for i in range(3):
            p = cls.env.jpg(f"p{i}.jpg", size=(1600, 1000))
            cls.ids.append(cls.env.x(
                "INSERT INTO media_items (file_path, file_name, media_type, date_taken) VALUES (?,?,?,?)",
                (p, f"p{i}.jpg", "photo", f"2026-07-0{i + 1} 10:00:00")))
        cls.patches = [mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda s: None),
                       mock.patch.object(main, "save_config")]
        for p in cls.patches:
            p.start()
        cls.w = main.MainWindow()
        cls.w.resize(1400, 900)
        cls.w.show()
        cls.w.activateWindow()
        pump(500)

    @classmethod
    def tearDownClass(cls):
        cls.w.close()
        cls.w.deleteLater()
        QApplication.sendPostedEvents(None, 0)      # no widgets left for sip at exit (exit 127)
        pump(50)
        for p in cls.patches:
            p.stop()
        cls.env.tearDown()

    def tearDown(self):
        v = self.w._viewer
        if v is not None and v.is_editing():
            v._editor.cancel()
        if v is not None and self.w.nav.contains(v):
            self.w._close_viewer()
        pump(300)

    def _key(self, k):
        """Like a real key press: delivered to whatever holds keyboard focus."""
        QTest.keyClick(QApplication.focusWidget() or self.w, k)
        pump(80)

    def _click(self, button):
        """Like a real click: a spontaneous press gives a click-focusable widget the focus first
        (QTest's synthetic events skip that step, which is why the bug hid from unit tests)."""
        if button.focusPolicy() & Qt.FocusPolicy.ClickFocus:
            button.setFocus(Qt.FocusReason.MouseFocusReason)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        pump(100)

    def _editing_soon(self, v, ms=2000):
        """E pressed while the display image is still decoding opens the editor on arrival."""
        deadline = time.time() + ms / 1000
        while not v.is_editing() and time.time() < deadline:
            pump(30)
        return v.is_editing()

    def _open(self, item_id):
        self.w.open_viewer_by_id(item_id)
        pump(400)
        v = self.w._viewer
        deadline = time.time() + 5
        while v._edit_source is None and time.time() < deadline:
            pump(30)
        self.assertIsNotNone(v._edit_source)
        return v

    def test_edit_panel_buttons_never_take_focus(self):
        v = self._open(self.ids[0])
        self._key(Qt.Key.Key_E)
        self.assertTrue(self._editing_soon(v))
        panel = v._edit_panel
        for b in [panel.btn_done, panel.btn_cancel, panel.btn_revert, *panel.aspect_buttons.values()]:
            self.assertEqual(b.focusPolicy(), Qt.FocusPolicy.NoFocus, b.text())
        v._editor.cancel()

    def test_e_works_after_done_cancel_and_navigation(self):
        v = self._open(self.ids[0])
        for finish in ("done", "cancel", "enter", "esc"):
            self._key(Qt.Key.Key_E)
            self.assertTrue(self._editing_soon(v), f"E opens the editor (before {finish})")
            if finish == "done":
                self._click(v._edit_panel.btn_done)
            elif finish == "cancel":
                self._click(v._edit_panel.btn_cancel)
            elif finish == "enter":
                self._key(Qt.Key.Key_Return)
            else:
                self._key(Qt.Key.Key_Escape)
            self.assertFalse(v.is_editing(), finish)
            self.assertIs(QApplication.focusWidget(), v, f"focus back on the viewer after {finish}")
        before = v.current_index
        self._key(Qt.Key.Key_Right if before < len(v.media_list) - 1 else Qt.Key.Key_Left)
        self.assertNotEqual(v.current_index, before, "arrows still navigate")
        pump(300)
        self._key(Qt.Key.Key_E)
        self.assertTrue(self._editing_soon(v), "E on the next photo")
        self._click(v._edit_panel.btn_done)
        self._key(Qt.Key.Key_Escape)
        pump(500)
        self.assertIsNot(self.w.nav.current(), v, "Esc still closes the viewer")

    def test_e_works_on_every_reopen(self):
        for item_id in self.ids:
            v = self._open(item_id)
            self.assertIs(QApplication.focusWidget(), v, f"viewer has the keys on reopen ({item_id})")
            self._key(Qt.Key.Key_E)
            self.assertTrue(self._editing_soon(v), f"E on reopen ({item_id})")
            self._click(v._edit_panel.btn_done)
            self._key(Qt.Key.Key_Escape)
            pump(500)


# ── 2 + 3. Map: teardrop pins, cluster flight, hover card ─────────────────────

@pytest.fixture(scope="module")
def mappage():
    _app()
    from test_patch11_maps import ShownMapPage
    m = ShownMapPage((900, 600))
    yield m
    m.close()


SPREAD = [{"id": i, "lat": 49.03 + i * 0.002, "lon": 13.49 + i * 0.003, "name": f"IMG_{i}",
           "city": "Modrava", "date": "", "thumb": ""} for i in range(6)]


def _set(m, markers, center=(13.5, 49.04), zoom=7):
    m.js(f"setMarkers({json.dumps(markers)})")
    m.reset_view(center, zoom)


def test_pin_image_tip_is_the_bottom_centre(mappage):
    g = mappage.js("""(function () {
        var d = drawPin(null, 3, '#8B7CF6'), w = d.width, h = d.height;
        function a(x, y) { return d.data[(Math.round(y) * w + Math.round(x)) * 4 + 3]; }
        var cx = w / 2, s = PIN_DPR;
        return { w: w, h: h, dpr: s, boxH: PIN_H, lift: PIN_LIFT,
                 tipAlpha: a(cx - 0.5, h - 1 - 1.5 * s), tipSide: a(cx - 7 * s, h - 2 * s),
                 disc: a(cx, (PIN_PAD + PIN_D / 2) * s), bottomCorner: a(1, h - 1) };
    })()""")
    assert g["h"] == round(g["boxH"] * g["dpr"]), "image is the teardrop height"
    assert g["h"] >= round((mappage.js("PIN_PAD + PIN_D") + 10) * g["dpr"]), "a tail below the disc"
    assert g["tipAlpha"] > 150, "an opaque tip on the last rows, at the centre"
    assert g["tipSide"] < 60, "the tail tapers: transparent beside the tip"
    assert g["disc"] > 250 and g["bottomCorner"] == 0


def test_pin_layers_anchor_at_the_tip(mappage):
    _set(mappage, SPREAD[:1])
    assert mappage.js("map.getLayoutProperty('pins', 'icon-anchor')") == "bottom"
    assert mappage.js("JSON.stringify(map.getPaintProperty('pins-halo', 'circle-translate'))") == \
        json.dumps([0, -mappage.js("PIN_LIFT")]).replace(" ", "")


def test_pin_hit_shape_is_the_teardrop(mappage):
    lift, r = mappage.js("PIN_LIFT"), mappage.js("PIN_D / 2")

    def hit(dx, dy):
        return mappage.js(f"isFinite(pinHitDistance({{x: 300, y: 300}}, {{x: {300 + dx}, y: {300 + dy}}}))")
    assert hit(0, -lift), "the disc centre"
    assert hit(0, -2), "just above the tip"
    assert hit(r - 2, -lift), "the disc edge"
    assert not hit(r, -4), "beside the tip is the ground, not the pin"
    assert not hit(0, 8), "below the tip"
    assert not hit(0, -lift - r - 6), "above the disc"


def test_click_on_pin_disc_opens_it(mappage):
    mappage.fake.opened.clear()
    _set(mappage, [{"id": 77, "lat": 49.03, "lon": 13.49, "name": "x", "city": "", "date": "", "thumb": ""}],
         (13.49, 49.03), 12)
    mappage.js("map.stop(); 0")
    pt = mappage.js("(function () { var p = map.project([13.49, 49.03]); return [p.x, p.y - PIN_LIFT]; })()")
    assert mappage.js(f"pinAtPoint({{x: {pt[0]}, y: {pt[1]}}})") == 0, "the disc above the coordinate hits"
    assert mappage.js(f"pinAtPoint({{x: {pt[0]}, y: {pt[1] + 2 * mappage.js('PIN_LIFT') + 10}}})") is None


def test_cluster_click_zooms_just_far_enough_to_split(mappage):
    _set(mappage, SPREAD, (15.5, 49.8), 1.7)
    assert mappage.js("getClusterCount()") == 1
    ids = [m["id"] for m in SPREAD]
    split = mappage.js(f"splitZoomFor({json.dumps(ids)})")
    view = mappage.js("clusterViewOf(0)")
    old_fit = mappage.js("""(function () {
        var b = new maplibregl.LngLatBounds([13.49, 49.03], [13.505, 49.04]);
        return map.cameraForBounds(b, { padding: 90, maxZoom: map.getMaxZoom() - 1 }).zoom; })()""")
    assert split is not None and 3 < split < 15
    assert view["zoom"] == pytest.approx(min(split + mappage.js("SPLIT_MARGIN"), old_fit), abs=0.01)
    assert view["zoom"] < old_fit, "no longer dives to the bounding box"
    assert mappage.js(f"computeClusters({view['zoom']}, markers).length") > 1, "the cluster splits on arrival"
    assert mappage.js(f"computeClusters({split - 0.5}, markers).length") == 1, "and not earlier"


def test_cluster_flight_is_short_and_lands(mappage):
    _set(mappage, SPREAD, (15.5, 49.8), 1.7)
    target = mappage.js("clusterViewOf(0)")["zoom"]
    t0 = time.perf_counter()
    mappage.js("clickCluster(0); 0")
    pump(200)
    assert mappage.js("map.isMoving()"), "a smooth flight, not a jump"
    while mappage.js("map.isMoving()") and time.perf_counter() - t0 < 5:
        pump(50)
    elapsed = time.perf_counter() - t0
    assert elapsed < mappage.js("CLUSTER_FLY_MAX_MS") / 1000 + 0.6, f"flight took {elapsed:.2f} s"
    assert mappage.js("map.getZoom()") == pytest.approx(target, abs=0.05)
    mappage.js("render(); 0")
    assert mappage.js("getClusterCount()") > 1, "the group has split into several pins"


def test_same_spot_cluster_opens_photos_instead_of_flying(mappage):
    mappage.fake.opened_many.clear()
    same = [{"id": 100 + i, "lat": 49.03, "lon": 13.49, "name": "x", "city": "", "date": "", "thumb": ""}
            for i in range(4)]
    _set(mappage, same, (13.49, 49.03), 10)
    mappage.js("clickCluster(0); 0")
    for _ in range(30):
        if mappage.fake.opened_many:
            break
        pump(50)
    assert mappage.fake.opened_many == [[100, 101, 102, 103]]


def test_hover_card_offers_show_all_and_opens(mappage):
    mappage.fake.opened.clear()
    mappage.fake.opened_many.clear()
    thumbs = [dict(m, thumb="data:image/gif;base64,R0lGODlhAQABAAAAACw=") for m in SPREAD]
    _set(mappage, thumbs, (13.5, 49.04), 7)
    assert mappage.js("hoverCluster(0)")
    html = mappage.js("document.getElementById('card').innerHTML")
    assert 'data-open="all"' in html and "6" in html, "Show 6 photos button"
    assert html.count('class="t" data-open=') == 4, "four clickable thumbnails"
    assert mappage.js("getComputedStyle(document.getElementById('card')).pointerEvents") == "auto"
    assert mappage.js("cardAction(0, 'all')")
    assert mappage.js("cardAction(0, 3) && 1") == 1
    for _ in range(30):
        if mappage.fake.opened_many and mappage.fake.opened:
            break
        pump(50)
    assert mappage.fake.opened_many == [[0, 1, 2, 3, 4, 5]]
    assert mappage.fake.opened == [3]
    assert not mappage.js("document.getElementById('card').classList.contains('show')"), "card closes on open"


def test_hover_card_survives_the_trip_from_pin_to_card(mappage):
    _set(mappage, SPREAD, (13.5, 49.04), 7)
    mappage.js("setPinHover(0); 0")
    assert mappage.js("document.getElementById('card').classList.contains('show')")
    mappage.js("setPinHover(null); document.getElementById('card').dispatchEvent(new MouseEvent('mouseenter')); 0")
    pump(mappage.js("CARD_HIDE_MS") + 150)
    assert mappage.js("document.getElementById('card').classList.contains('show')"), "cursor reached the card"
    mappage.js("document.getElementById('card').dispatchEvent(new MouseEvent('mouseleave')); 0")
    pump(mappage.js("CARD_HIDE_MS") + 150)
    assert not mappage.js("document.getElementById('card').classList.contains('show')"), "left the card: hides"


def test_single_photo_card_has_no_show_all(mappage):
    _set(mappage, SPREAD[:1], (13.49, 49.03), 9)
    mappage.js("hoverCluster(0); 0")
    assert 'data-open="all"' not in mappage.js("document.getElementById('card').innerHTML")
    mappage.js("hidePinCard(); 0")


# ══════════════════════════════════════════════════════════════════════════════
# Sprint 2: viewport-prioritised decode queue + micro previews (LQIP)
# ══════════════════════════════════════════════════════════════════════════════

import os            # noqa: E402
import shutil        # noqa: E402
import sqlite3       # noqa: E402
import tempfile      # noqa: E402

import decode_queue  # noqa: E402
import micro_previews  # noqa: E402


class _Job:
    def __init__(self, name):
        self.name = name


class TestDecodeQueue(unittest.TestCase):
    def test_visible_centre_first_then_ahead_then_behind(self):
        q = decode_queue.DecodeQueue()
        q.set_viewport(100, 139, direction=+1)          # 40 visible, scrolling down
        for row in (100, 120, 139, 145, 95, 175):
            q.submit(row, row, _Job(row))
        order = [q.pop().name for _ in range(6)]
        self.assertEqual(order[0], 120, "the viewport centre first")
        self.assertEqual(set(order[:3]), {100, 120, 139}, "all visible rows before any off-screen row")
        self.assertLess(order.index(145), order.index(95), "ahead of the scroll before behind it")
        self.assertLess(order.index(145), order.index(175), "nearer the leading edge first")
        self.assertIsNone(q.pop())

    def test_priority_follows_the_latest_viewport_not_submit_order(self):
        q = decode_queue.DecodeQueue()
        q.set_viewport(0, 39, +1)
        for row in range(0, 80, 4):
            q.submit(row, row, _Job(row))
        q.set_viewport(40, 79, +1)                     # the user flew on before the workers ran
        self.assertEqual(q.pop().name, 60, "the rows on screen NOW go first")

    def test_rows_scrolled_away_are_dropped_at_once(self):
        q = decode_queue.DecodeQueue()
        q.set_viewport(0, 39, +1)
        for row in range(0, 80):
            q.submit(1000 + row, row, _Job(row))
        dropped = q.set_viewport(400, 439, +1)
        self.assertEqual(len(dropped), 80)
        self.assertEqual(len(q), 0)
        q.submit(1, 0, _Job(0))
        q.submit(2, 420, _Job(420))
        q.submit(3, 455, _Job(455))                    # ahead: kept
        q.submit(4, 395, _Job(395))                    # 5 behind: inside the behind margin
        q.submit(5, 300, _Job(300))
        self.assertEqual(sorted(q.set_viewport(400, 439, +1)), [1, 5])

    def test_fling_widens_the_window_ahead(self):
        q = decode_queue.DecodeQueue()
        q.set_viewport(100, 139, +1, fast=False)
        rest = q.window()
        q.set_viewport(100, 139, +1, fast=True)
        fling = q.window()
        self.assertGreater(fling[1], rest[1])
        self.assertEqual(rest[0], fling[0], "behind margin unchanged")

    def test_prefetch_rows_lead_in_the_scroll_direction(self):
        q = decode_queue.DecodeQueue()
        q.set_viewport(100, 139, +1)
        down = q.prefetch_rows()
        self.assertEqual(down[:3], [140, 141, 142])
        self.assertEqual(len(down), 40, "one screen ahead at rest")
        q.set_viewport(100, 139, -1)
        self.assertEqual(q.prefetch_rows()[:3], [99, 98, 97])
        q.set_viewport(100, 139, 0)
        self.assertEqual(q.prefetch_rows()[:4], [140, 99, 141, 98], "resting: both sides, alternating")

    def test_thread_safety_under_concurrent_pops(self):
        import threading
        q = decode_queue.DecodeQueue()
        q.set_viewport(0, 999, 0)
        for i in range(1000):
            q.submit(i, i, _Job(i))
        got, lock = [], threading.Lock()

        def worker():
            while True:
                j = q.pop()
                if j is None:
                    return
                with lock:
                    got.append(j.name)
        ts = [threading.Thread(target=worker) for _ in range(6)]
        for t in ts:
            t.start()
        for _ in range(50):
            q.set_viewport(0, 999, 0)
        for t in ts:
            t.join()
        self.assertEqual(sorted(got), list(range(1000)), "every job exactly once")


def _jpg(path, size=(840, 630), color=(200, 60, 40)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=90)
    return path


def _qimg(color="green", w=96, h=72):
    from PyQt6.QtGui import QImage, QColor
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(color))
    return img


def _file_bytes(path):
    out = b""
    for p in (path, path + "-wal"):
        if os.path.exists(p):
            with open(p, "rb") as f:
                out += f.read()
    return out


class TestMicroStore(unittest.TestCase):
    def setUp(self):
        _app()
        self.tmp = tempfile.mkdtemp(prefix="kairos-micro-")
        self.path = os.path.join(self.tmp, "micro.sqlite")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self):
        st = micro_previews.MicroStore(self.path)
        st.load()
        return st

    def test_micro_is_tiny_and_keeps_the_aspect(self):
        from PyQt6.QtGui import QImage, QColor
        img = QImage(840, 630, QImage.Format.Format_RGB32)
        img.fill(QColor(30, 160, 90))
        data = micro_previews.micro_from_qimage(img)
        self.assertLess(len(data), 2500, "a few hundred bytes to ~2 kB")
        st = self._store()
        st.put(7, data)
        pm = st.pixmap(7)
        self.assertEqual((pm.width(), pm.height()), (48, 36))
        c = pm.toImage().pixelColor(24, 18)
        self.assertLess(abs(c.green() - 160) + abs(c.red() - 30), 30)

    def test_persists_and_reloads_like_a_restart(self):
        st = self._store()
        for i in range(5):
            st.put_image(i, _qimg("red", 64, 48))
        st.flush()
        again = self._store()
        self.assertEqual(len(again), 5)
        self.assertTrue(again.has(3) and again.pixmap(3) is not None)

    def test_version_bump_rebuilds(self):
        st = self._store()
        st.put(1, b"\xff\xd8junk")
        st.flush()
        with mock.patch.object(micro_previews, "MICRO_VERSION", micro_previews.MICRO_VERSION + 1):
            self.assertEqual(len(self._store()), 0)

    def test_purge_overwrites_the_bytes_on_disk(self):
        """Hidden folder: a purged preview must not survive in the file's free pages."""
        st = self._store()
        marker = b"\xff\xd8" + b"SECRET-MICRO-PREVIEW" * 40
        st.put(42, marker)
        st.put(43, b"\xff\xd8" + b"keep" * 50)
        st.flush()
        self.assertIn(b"SECRET-MICRO-PREVIEW", _file_bytes(self.path))
        st.purge([42])
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        self.assertNotIn(b"SECRET-MICRO-PREVIEW", _file_bytes(self.path), "secure_delete overwrote the row")
        self.assertFalse(st.has(42))
        self.assertFalse(st.put(42, marker), "an excluded (hidden) item is never stored again")
        self.assertTrue(st.has(43))

    def test_backfill_builds_from_thumbnails_with_the_edit(self):
        import quick_edit as qe
        st = self._store()
        a = _jpg(os.path.join(self.tmp, "t", "a.jpg"), color=(40, 40, 40))
        b = _jpg(os.path.join(self.tmp, "t", "b.jpg"), color=(40, 40, 40))
        edits = {2: qe.Adjustments(exposure=80)}
        self.assertEqual(st.request([(1, a), (2, b), (3, None)], edit_lookup=edits.get), 2)
        deadline = time.time() + 10
        while not st.backfill_idle() and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(st.has(1) and st.has(2))
        plain = st.pixmap(1).toImage().pixelColor(24, 18).lightness()
        bright = st.pixmap(2).toImage().pixelColor(24, 18).lightness()
        self.assertGreater(bright, plain + 20, "the Quick Edit shows in the micro preview")
        self.assertEqual(len(self._store()), 2, "flushed to disk when the queue drained")
        st.stop()

    def test_invalidate_drops_ram_and_disk(self):
        st = self._store()
        st.put(5, micro_previews.micro_from_qimage(_qimg()))
        st.flush()
        st.invalidate([5])
        st.flush()
        self.assertFalse(st.has(5))
        self.assertFalse(self._store().has(5))


class TestGridIntegration(unittest.TestCase):
    """The model + delegate without a window: queue wiring, by-product, paint."""

    def setUp(self):
        _app()
        self.tmp = tempfile.mkdtemp(prefix="kairos-grid-")
        micro_previews.reset_for_tests()
        p = mock.patch.object(micro_previews, "_store_path", lambda: os.path.join(self.tmp, "micro.sqlite"))
        p.start()
        self.addCleanup(p.stop)
        self.model = main.ApplePhotoModel()
        self.model.micro.loaded.wait(5)
        self.recs = [(i, f"p{i}.jpg", os.path.join(self.tmp, f"p{i}.jpg"), "photo", None,
                      os.path.join(self.tmp, f"t{i}.jpg"), "2026-01-01", 0) for i in range(1, 401)]
        self.model.set_records(self.recs)

    def tearDown(self):
        self.model.thread_pool.clear()
        self.model.thread_pool.waitForDone(3000)
        micro_previews.reset_for_tests()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_workers_decode_the_best_job_not_their_own(self):
        started = []
        with mock.patch.object(self.model.thread_pool, "start", side_effect=started.append):
            for row in (0, 5, 10):                    # requested by an earlier paint, top of the grid
                self.model.data(self.model.index(row, 0), self.model.ThumbRole)
            self.model.set_viewport(200, 239, +1)       # ... but the user has flown on
            self.model.data(self.model.index(220, 0), self.model.ThumbRole)
        self.assertEqual(len(self.model.decode_queue), 1, "rows 0/5/10 dropped (scrolled away)")
        for i in (1, 6, 11):
            self.assertNotIn(i, self.model.loading_ids, "dropped rows re-request when they return")
        ran = []
        with mock.patch.object(main.ThumbnailWorker, "_decode", lambda w: ran.append(w.item_id)):
            started[0].run()                          # the oldest pool slot ...
        self.assertEqual(ran, [221], "... decodes the visible row")

    def test_prefetch_ahead_queues_rows_beyond_the_leading_edge(self):
        with mock.patch.object(self.model.thread_pool, "start"):
            self.model.set_viewport(100, 139, +1)
            n = self.model.prefetch_ahead()
        self.assertEqual(n, 40)
        rows = self.model.decode_queue.pending_rows()
        self.assertEqual((rows[0], rows[-1]), (140, 179))
        with mock.patch.object(self.model.thread_pool, "start") as start:
            self.assertEqual(self.model.prefetch_ahead(), 0, "no duplicates")
            start.assert_not_called()

    def test_cached_rows_are_not_prefetched_and_lru_order_is_untouched(self):
        from PyQt6.QtGui import QPixmap
        for i in (141, 142):
            self.model.ram_cache.put(i, QPixmap(8, 8))
        self.model.ram_cache.put(1, QPixmap(8, 8))
        order = list(self.model.ram_cache.cache)
        with mock.patch.object(self.model.thread_pool, "start"):
            self.model.set_viewport(100, 139, +1)
            self.model.prefetch_ahead()
        self.assertNotIn(140, self.model.decode_queue.pending_rows())
        self.assertEqual(list(self.model.ram_cache.cache), order)

    def test_worker_leaves_a_micro_preview_and_vault_never_does(self):
        path = _jpg(os.path.join(self.tmp, "t7.jpg"))
        w = main.ThumbnailWorker(7, path, None, 256)
        w.micro_store = self.model.micro
        w.run()
        self.assertTrue(self.model.micro.has(7))
        self.model.vault = object()
        self.assertFalse(self.model.has_micro(7))
        self.assertIsNone(self.model.micro_pixmap(7), "the Hidden grid never paints one")
        got = []
        with mock.patch.object(self.model.thread_pool, "start", side_effect=got.append):
            self.model.vault = mock.Mock()
            self.model._start_worker(9, self.recs[8])
        self.model.vault = None
        self.assertIsNone(got[0].micro_store, "vault workers get no micro sink")

    def test_saved_edit_invalidates_the_micro_preview(self):
        self.model.micro.put_image(3, _qimg())
        self.model.refresh_items([3])
        self.assertFalse(self.model.micro.has(3))

    def _paint_tile(self, row):
        from PyQt6.QtGui import QImage, QPainter
        from PyQt6.QtWidgets import QStyleOptionViewItem
        delegate = main.ApplePhotoDelegate()
        host = mock.Mock(live_preview=None, photo_model=self.model)
        host.model.return_value = self.model
        with mock.patch.object(delegate, "parent", return_value=host):
            img = QImage(180, 180, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(main.theme.qcolor("bg"))
            p = QPainter(img)
            opt = QStyleOptionViewItem()
            opt.rect = img.rect()
            with mock.patch.object(self.model.thread_pool, "start"):
                self.assertTrue(delegate._blit_resting(p, opt, self.model.index(row, 0)))
            p.end()
        return img

    def test_loading_tile_paints_the_micro_preview_with_rounded_corners(self):
        self.model.micro.put_image(1, _qimg("#20C040"))
        img = self._paint_tile(0)
        c = img.pixelColor(90, 90)
        self.assertGreater(c.green(), 150, f"the micro preview, not the grey placeholder ({c.name()})")
        bg = main.theme.qcolor("bg")
        corner = img.pixelColor(5, 5)
        self.assertLess(abs(corner.red() - bg.red()) + abs(corner.green() - bg.green()), 12,
                        "outside the rounded rect: the page background")

    def test_loading_tile_without_micro_keeps_the_placeholder(self):
        img = self._paint_tile(1)
        ph = main.theme.qcolor("placeholder")
        c = img.pixelColor(90, 90)
        self.assertLess(abs(c.red() - ph.red()) + abs(c.green() - ph.green()) + abs(c.blue() - ph.blue()), 6)


class TestHiddenFolderPurgesMicro(unittest.TestCase):
    def test_hide_items_purges_the_micro_previews(self):
        """Through the real hide path (Stage 3 env): the hidden item's micro preview is gone."""
        _app()
        from test_stage3_locked import Env
        env = Env("setUp")
        env.setUp()
        self.addCleanup(env.tearDown)
        micro_previews.reset_for_tests()
        p = mock.patch.object(micro_previews, "_store_path", lambda: os.path.join(env.tmp, "micro.sqlite"))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(micro_previews.reset_for_tests)
        st = micro_previews.store()
        st.loaded.wait(5)
        for key in "AB":
            st.put_image(env.ids[key], _qimg())
        st.flush()
        self.assertEqual(env.hide("B"), 1)
        self.assertFalse(st.has(env.ids["B"]), "hidden item's micro preview purged")
        self.assertTrue(st.has(env.ids["A"]), "visible items keep theirs")
        again = micro_previews.MicroStore(st.path)
        again.load()
        self.assertFalse(again.has(env.ids["B"]), "and gone from disk")

    def test_startup_sweep_purges_too(self):
        import locked_folder
        with open(locked_folder.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("micro_previews.purge_ids", src[src.index("def startup_sweep"):])

    def test_purge_ids_without_a_live_store_edits_the_file(self):
        _app()
        tmp = tempfile.mkdtemp(prefix="kairos-purge-")
        try:
            path = os.path.join(tmp, "micro.sqlite")
            st = micro_previews.MicroStore(path)
            st.load()
            st.put(11, b"\xff\xd8x")
            st.flush()
            micro_previews.reset_for_tests()
            with mock.patch.object(micro_previews, "_store_path", lambda: path):
                micro_previews.purge_ids([11])
            again = micro_previews.MicroStore(path)
            again.load()
            self.assertFalse(again.has(11))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ── a latent crash found by the grid benchmark (frame_clock, 120 Hz work) ──────

def test_frame_clock_survives_an_animation_started_as_vblank_dies():
    """The vblank thread sets _wait = None and queues a tick; an animation starting before that
    tick is delivered found no timer: AttributeError 'NoneType' object has no attribute 'isActive'."""
    _app()
    import frame_clock
    clock = frame_clock.FrameClock(wait=lambda: True)
    clock._wait = None                      # what _loop does when the wait fails
    assert clock._timer is None
    clock.subscribe("x", lambda t: None)   # used to raise
    assert clock._timer is not None and clock._timer.isActive()
    clock.unsubscribe("x")
    clock.shutdown()
