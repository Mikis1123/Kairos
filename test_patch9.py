"""Regresní testy pro Patch 9 – vykreslování, avatary, ostré duplicity a Fáze 4 (mapa).

Spuštění (ve složce projektu):   python -m pytest test_patch9.py -v

Pokrývá:
  Vykreslování – žádný QOpenGLWidget v okně skládaném přes D3D11 (dřív nekreslil nic + záplava
                 „QPainter::begin: engine == 0“), plátno v hlavním okně opravdu kreslí, vrstva
                 videa se odkryje až s prvním platným snímkem (žádné černé bliknutí).
  Avatary      – čtvercový ořez i u obličeje u okraje fotky, oprava starých protažených ořezů.
  Duplicity    – plné rozlišení (HEIC přes Pillow, EXIF orientace) místo rozmazané miniatury.
  Mapa         – přepínač podkladů (Základní / Satelitní / Turistická), povolené hosty dlaždic,
                 zapamatování volby, náhledové karty, plynulé přiblížení, slučování překryvů.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db, config.json ani fotky se nepoužijí.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image, ImageDraw

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QUrl, QSize, qInstallMessageHandler
    from PyQt6.QtGui import QColor, QPixmap, QImage
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import face_engine

if HAS_QT:
    import main
    import viewer_hud
    import viewer

HERE = os.path.dirname(os.path.abspath(__file__))


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class TempEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p9_test_")
        self._saved = (database.DB_PATH, face_engine.FACE_CACHE_DIR)
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        self.faces = os.path.join(self.tmp, "faces")
        os.makedirs(self.faces)
        face_engine.FACE_CACHE_DIR = self.faces
        database.init_db()

    def tearDown(self):
        database.DB_PATH, face_engine.FACE_CACHE_DIR = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def q(self, sql, params=()):
        conn = database.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def x(self, sql, params=()):
        with database.db_transaction() as conn:
            return conn.execute(sql, params).lastrowid

    def add_media(self, path, media_type="photo", thumb=None, date="2026-07-07 10:00:00"):
        return self.x("INSERT INTO media_items (file_path, file_name, media_type, thumbnail_path, date_taken) "
                      "VALUES (?, ?, ?, ?, ?)", (path, os.path.basename(path), media_type, thumb, date))

    def jpg(self, name, size=(640, 480), color=(200, 90, 40), exif_orientation=None, draw=None):
        path = os.path.join(self.tmp, name)
        img = Image.new("RGB", size, color)
        if draw:
            draw(ImageDraw.Draw(img))
        kw = {"quality": 92}
        if exif_orientation:
            exif = Image.Exif()
            exif[274] = exif_orientation
            kw["exif"] = exif.tobytes()
        img.save(path, "JPEG", **kw)
        return path


# ═════════════════════════════════════════════════════════════════════════════
# 1. Vykreslování
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestRenderingArchitecture(TempEnv):

    def test_canvas_never_uses_opengl_viewport(self):
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
        canvas = viewer_hud.MediaCanvas()
        self.addCleanup(canvas.deleteLater)
        self.assertNotIsInstance(canvas.viewport(), QOpenGLWidget)
        self.assertFalse(hasattr(viewer_hud, "_make_gl_viewport"))

    def test_viewer_in_main_window_paints_without_qpainter_warnings(self):
        """Hlavní okno má WebEngine mapu (D3D11) – plátno musí kreslit a nesmí logovat chyby."""
        photo = self.jpg("IMG_1.jpg", size=(800, 600), color=(30, 160, 220))
        self.add_media(photo)
        messages = []
        qInstallMessageHandler(lambda mode, ctx, text: messages.append(text))
        self.addCleanup(qInstallMessageHandler, None)
        with mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None):
            w = main.MainWindow()
        self.addCleanup(lambda: (w.close(), w.deleteLater()))
        w.resize(1200, 800)
        w.show()
        wait(300)
        w._on_photo_activated(0)
        wait(500)
        img = w._viewer.grab().toImage()
        centre = QColor(img.pixel(img.width() // 3, img.height() // 2))
        self.assertGreater(centre.blue(), 150, "fotka se na plátně opravdu vykreslí")
        bad = [m for m in messages if "QPainter" in m or "not compatible with QOpenGLWidget" in m]
        self.assertEqual(bad, [])


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestVideoReveal(TempEnv):

    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "IMG_V.MOV")
        with open(self.video, "wb") as f:
            f.write(b"\x00" * 128)
        thumb = self.jpg("thumb_v.jpg", size=(320, 180), color=(240, 200, 20))
        self.add_media(self.video, "video", thumb=thumb)
        self.add_media(self.jpg("IMG_P.jpg"), date="2026-07-08 10:00:00")
        self.viewer = viewer.MediaViewerWidget()
        self.viewer.resize(900, 600)
        self.viewer.show()
        self.records = main.fetch_media_records(order_by="m.date_taken, m.id")
        self.addCleanup(self._close)

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()

    def frame(self, valid=True):
        from PyQt6.QtMultimedia import QVideoFrame, QVideoFrameFormat
        if not valid:
            return QVideoFrame()
        return QVideoFrame(QVideoFrameFormat(QSize(8, 8), QVideoFrameFormat.PixelFormat.Format_BGRA8888))

    def test_layer_is_always_visible_and_only_restacked(self):
        canvas = self.viewer.canvas
        if canvas.video_backend != "quick":
            self.skipTest("QtQuick není k dispozici")
        self.viewer.set_media_data(self.records, 0)
        surface = canvas.video_surface
        order = lambda w: canvas.children().index(w)      # pořadí dětí = pořadí vrstev (vždy aktuální)
        self.assertTrue(surface.isVisible(), "skrytý QQuickWidget by při zobrazení blikl černou")
        self.assertLess(order(surface), order(canvas.viewport()), "do 1. snímku drží obraz plakát")
        hud = self.viewer.top_hud
        self.viewer._on_video_frame(self.frame())
        wait(80)
        self.assertGreater(order(surface), order(canvas.viewport()))
        self.assertGreater(order(hud), order(surface), "HUD zůstává nad videem")

    def test_reveal_waits_for_valid_frame(self):
        self.viewer.set_media_data(self.records, 0)
        self.viewer.on_position_changed(500)
        self.viewer._on_video_frame(self.frame(valid=False))
        wait(80)
        self.assertFalse(self.viewer.canvas.is_video_shown())
        self.assertFalse(self.viewer.canvas.current_pixmap().isNull(), "plakát (náhled) drží obraz")

    def test_stale_reveal_after_switching_item_is_ignored(self):
        self.viewer.set_media_data(self.records, 0)
        self.viewer._on_video_frame(self.frame())
        self.viewer.show_next()                       # přepnuto dřív, než proběhlo odkrytí
        wait(80)
        self.assertFalse(self.viewer.canvas.is_video_shown(), "fotka nesmí zmizet pod starým videem")


# ═════════════════════════════════════════════════════════════════════════════
# 2. Avatary bez deformace
# ═════════════════════════════════════════════════════════════════════════════

class TestSquareFaceCrops(TempEnv):

    def _disc_image(self, w=600, h=400, cx=300, cy=40, r=30):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        yy, xx = np.ogrid[:h, :w]
        img[(xx - cx) ** 2 + (yy - cy) ** 2 <= r * r] = 255
        return img

    def test_window_is_square_and_inside_image(self):
        for bbox in ((280, 5, 320, 60), (0, 0, 40, 50), (560, 340, 600, 400), (100, 100, 500, 390)):
            x, y, side = face_engine.square_crop_window(600, 400, bbox)
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + side, 600)
            self.assertLessEqual(y + side, 400)

    def test_face_at_edge_is_not_stretched(self):
        """Kruh u horního okraje musí v ořezu zůstat kruhem (dřív svisle protažený ovál)."""
        img = self._disc_image()
        crop = face_engine._extract_apple_avatar_crop(img, (270, 10, 330, 70), target_size=256)
        self.assertEqual(crop.shape[:2], (256, 256))
        ys, xs = np.nonzero(crop[:, :, 0] > 128)
        width, height = xs.max() - xs.min(), ys.max() - ys.min()
        self.assertAlmostEqual(width / height, 1.0, delta=0.06)

    def test_stretch_detector(self):
        self.assertTrue(face_engine._is_stretched_window(600, 400, (270, 10, 330, 70)))
        self.assertFalse(face_engine._is_stretched_window(600, 400, (280, 180, 320, 220)))

    def _face(self, media_id, bbox_rel, crop_path, version=None):
        bx, by, bw, bh = bbox_rel
        return self.x("INSERT INTO face_instances (media_id, face_crop_path, embedding, bbox_x, bbox_y, "
                      "bbox_w, bbox_h, crop_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (media_id, crop_path, b"\x00" * 16, bx, by, bw, bh, version))

    def test_repair_rewrites_only_stretched_crops(self):
        photo = self.jpg("edge.jpg", size=(600, 400), color=(0, 0, 0),
                         draw=lambda d: d.ellipse((270, 10, 330, 70), fill=(255, 255, 255)))
        mid = self.add_media(photo)
        edge_crop = os.path.join(self.faces, "edge_crop.jpg")
        centre_crop = os.path.join(self.faces, "centre_crop.jpg")
        Image.new("RGB", (256, 256), (9, 9, 9)).save(edge_crop)          # „starý“ protažený ořez
        Image.new("RGB", (256, 256), (9, 9, 9)).save(centre_crop)
        edge = self._face(mid, (270 / 600, 10 / 400, 60 / 600, 60 / 400), edge_crop)
        centre = self._face(mid, (280 / 600, 180 / 400, 40 / 600, 40 / 400), centre_crop)
        pid = self.x("INSERT INTO people (name, cover_face_path, key_face_id) VALUES ('Eva', ?, ?)", (edge_crop, edge))

        self.assertEqual(face_engine.repair_stretched_face_crops(), 1)
        with Image.open(edge_crop) as im:
            arr = np.asarray(im.convert("L"))
        ys, xs = np.nonzero(arr > 128)
        self.assertAlmostEqual((xs.max() - xs.min()) / (ys.max() - ys.min()), 1.0, delta=0.08)
        with Image.open(centre_crop) as im:
            self.assertEqual(im.getpixel((5, 5)), (9, 9, 9), "nedeformovaný ořez se nepřepisuje")
        self.assertEqual({r[0] for r in self.q("SELECT crop_version FROM face_instances")},
                         {face_engine.CROP_VERSION})
        self.assertEqual(face_engine.repair_stretched_face_crops(), 0, "jednorázová oprava")
        self.assertEqual(self.q("SELECT cover_face_path FROM people WHERE id=?", (pid,))[0][0], edge_crop)

    def test_video_faces_are_marked_not_decoded(self):
        video = os.path.join(self.tmp, "clip.MOV")
        open(video, "wb").close()
        mid = self.add_media(video, "video")
        fid = self._face(mid, (0.0, 0.0, 0.2, 0.2), os.path.join(self.faces, "v.jpg"))
        self.assertEqual(face_engine.repair_stretched_face_crops(), 0)
        self.assertEqual(self.q("SELECT crop_version FROM face_instances WHERE id=?", (fid,))[0][0],
                         face_engine.CROP_VERSION)

    def test_new_faces_are_stored_with_current_crop_version(self):
        mid = self.add_media(self.jpg("n.jpg"))
        database.save_faces_for_media(mid, [{"crop_path": None, "embedding": b"\x00" * 16,
                                             "crop_version": face_engine.CROP_VERSION}])
        self.assertEqual(self.q("SELECT crop_version FROM face_instances")[0][0], face_engine.CROP_VERSION)

    @unittest.skipUnless(HAS_QT, "Qt není dostupné")
    def test_main_window_reloads_avatars_after_repair(self):
        fake = mock.Mock()
        fake._avatar_cache = main.LRUCache(64)
        fake._avatar_cache.put((1, "x"), QImage(4, 4, QImage.Format.Format_ARGB32))
        main.MainWindow._on_face_crops_repaired(fake, 3)
        self.assertIsNone(fake._avatar_cache.get((1, "x")), "cache klíčovaná cestou by držela starý obrázek")
        fake._load_people.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# 3. Ostré snímky v kontrole duplicit
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestFullResolutionLoading(TempEnv):

    def test_jpeg_exif_orientation_is_applied(self):
        path = self.jpg("rot.jpg", size=(300, 200), exif_orientation=6)
        img = viewer_hud.load_image_file(path)
        self.assertEqual((img.width(), img.height()), (200, 300))
        pix = viewer.load_hi_res_pixmap(path)
        self.assertEqual((pix.width(), pix.height()), (200, 300), "prohlížeč také otočí podle EXIFu")

    def test_heic_decodes_at_full_resolution(self):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
            path = os.path.join(self.tmp, "IMG.HEIC")
            Image.new("RGB", (2400, 1800), (10, 120, 220)).save(path, format="HEIF")
        except Exception as e:
            self.skipTest(f"HEIF kodér není k dispozici: {e}")
        img = viewer_hud.load_image_file(path)
        self.assertEqual((img.width(), img.height()), (2400, 1800))
        small = viewer_hud.load_image_file(path, QSize(1600, 1600))
        self.assertEqual(small.width(), 1600)
        self.assertGreater(QColor(small.pixel(10, 10)).blue(), 180)

    def test_duplicate_review_upgrades_thumbnail_to_full_resolution(self):
        thumb_a = self.jpg("thumb_a.jpg", size=(360, 240))
        thumb_b = self.jpg("thumb_b.jpg", size=(360, 240))
        a = self.add_media(self.jpg("A.jpg", size=(3000, 2000)), thumb=thumb_a)
        b = self.add_media(self.jpg("B.jpg", size=(3000, 2000)), thumb=thumb_b)
        path_a, path_b = (self.q("SELECT file_path FROM media_items WHERE id=?", (i,))[0][0] for i in (a, b))
        view = main.DuplicateReviewView([(a, path_a, b, path_b, 1)])
        self.addCleanup(view.deleteLater)
        self.assertEqual(view.lbl_a._source.width(), 360, "nejdřív okamžitě miniatura")
        view.wait_for_images()
        for label in (view.lbl_a, view.lbl_b):
            self.assertTrue(label.full_res)
            self.assertGreaterEqual(max(label._source.width(), label._source.height()),
                                    main.REVIEW_IMAGE_MIN_EDGE)

    def test_late_image_of_previous_pair_is_ignored(self):
        a = self.add_media(self.jpg("A.jpg"))
        b = self.add_media(self.jpg("B.jpg"))
        view = main.DuplicateReviewView([(a, "", b, "", 1)])
        self.addCleanup(view.deleteLater)
        view._load_token += 1
        view._on_full_image(view._load_token - 1, "a", QImage(4000, 3000, QImage.Format.Format_RGB32))
        self.assertFalse(getattr(view.lbl_a, "full_res", False))


# ═════════════════════════════════════════════════════════════════════════════
# 4. Mapa – podklady, karty, přiblížení
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapLayerPolicy(unittest.TestCase):

    def test_tile_hosts_whitelisted(self):
        # Patch 10: podklady Základní = CARTO Voyager, Turistická = OpenTopoMap + Waymarked Trails
        self.assertTrue({"server.arcgisonline.com", "tile.opentopomap.org"} <= main.ALLOWED_WEB_HOSTS)
        ok = ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/12/1400/2210",
              "https://tile.opentopomap.org/12/2210/1400.png"]
        bad = ["http://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/1/1/1",
               "https://tile.opentopomap.org.evil.com/1/1/1.png",
               "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/1/1/1",
               "https://a.tile.opentopomap.org/1/1/1.png"]
        for u in ok:
            self.assertTrue(main.is_allowed_web_url(u), u)
        for u in bad:
            self.assertFalse(main.is_allowed_web_url(u), u)

    def test_html_switcher_and_attributions(self):
        html = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
        for needle in ('data-layer="standard"', 'data-layer="satellite"', 'data-layer="outdoor"',
                       ">Standard<", ">Satellite<", ">Outdoor<",
                       "World_Imagery/MapServer/tile/{z}/{y}/{x}", "tile.opentopomap.org/{z}/{x}/{y}.png",
                       "Esri", "OpenTopoMap", "backdrop-filter", "map.flyTo("):
            self.assertIn(needle, html)
        self.assertNotIn("invert(", html, "Patch 10: žádné invertující filtry přes dlaždice")

    def test_bridge_saves_only_known_layers(self):
        fake = mock.Mock()
        bridge = main.MapBridge(fake)
        bridge.setMapLayer("outdoor")
        bridge.setMapLayer("javascript:alert(1)")
        fake.set_map_layer.assert_called_once_with("outdoor")


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapLayerPersistence(TempEnv):

    def setUp(self):
        super().setUp()
        patches = [mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None),
                   mock.patch.object(main, "save_config")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.save = main.save_config
        self.w = main.MainWindow()
        self.w._ensure_map_view()                 # pillar 5: the map host is built lazily
        self.addCleanup(lambda: (self.w.close(), self.w.deleteLater()))

    def test_choice_is_saved_once(self):
        self.w.config["map_layer"] = "dark"
        self.w.set_map_layer("satellite")
        self.w.set_map_layer("satellite")
        self.w.set_map_layer("bogus")
        self.assertEqual(self.save.call_count, 1)
        self.assertEqual(self.w.config["map_layer"], "satellite")

    def test_saved_layer_restored_on_load(self):
        self.w.config["map_layer"] = "topo"           # hodnota uložená Patchem 9 -> nový název
        calls = []
        with mock.patch.object(self.w.map_view.page(), "runJavaScript", side_effect=lambda code, *a: calls.append(code)):
            self.w._on_map_loaded(True)
        self.assertTrue(any('setBaseLayer("outdoor")' in c for c in calls), calls)
        self.w.config["map_layer"] = "<script>"
        calls.clear()
        with mock.patch.object(self.w.map_view.page(), "runJavaScript", side_effect=lambda code, *a: calls.append(code)):
            self.w._on_map_loaded(True)
        self.assertTrue(any('setBaseLayer("standard")' in c for c in calls), "neznámá hodnota -> výchozí")


class _FakeMain:
    def __init__(self):
        self.layers, self.opened, self.opened_many = [], [], []

    def set_map_layer(self, name):
        self.layers.append(name)

    def open_viewer_by_id(self, i):
        self.opened.append(i)

    def open_viewer_for_ids(self, ids):
        self.opened_many.append(ids)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestMapPageLayers(unittest.TestCase):
    """Skutečná stránka v QWebEngine (bez sítě): přepínač, karty, shlukování."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWebEngineCore import (QWebEngineProfile, QWebEnginePage,
                                           QWebEngineUrlRequestInterceptor, QWebEngineSettings)
        from PyQt6.QtWebChannel import QWebChannel

        class Recorder(QWebEngineUrlRequestInterceptor):
            def __init__(self):
                super().__init__()
                self.urls = []

            def interceptRequest(self, info):
                url = info.requestUrl()
                self.urls.append(url.toString())
                if url.scheme() not in ("file", "qrc", "data"):
                    info.block(True)           # testy nesahají na síť

        cls.recorder = Recorder()
        cls.profile = QWebEngineProfile()
        cls.profile.setUrlRequestInterceptor(cls.recorder)
        cls.page = QWebEnginePage(cls.profile)
        cls.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        cls.fake = _FakeMain()
        cls.bridge = main.MapBridge(cls.fake)
        cls.channel = QWebChannel()
        cls.channel.registerObject("bridge", cls.bridge)
        cls.page.setWebChannel(cls.channel)
        loop = QEventLoop()
        cls.page.loadFinished.connect(lambda ok: loop.quit())
        QTimer.singleShot(15000, loop.quit)
        cls.page.load(QUrl.fromLocalFile(os.path.join(HERE, "map_vault.html")))
        loop.exec()
        for _ in range(50):
            if cls.js("bridge !== null"):
                break
            wait(100)

    @classmethod
    def tearDownClass(cls):
        cls.page.deleteLater()
        wait(50)

    @classmethod
    def js(cls, code, timeout=5000):
        loop, out = QEventLoop(), {}
        cls.page.runJavaScript(code, lambda r: (out.__setitem__("r", r), loop.quit()))
        QTimer.singleShot(timeout, loop.quit)
        loop.exec()
        return out.get("r")

    def test_switch_layers(self):
        self.assertEqual(self.js("setBaseLayer('satellite')"), "satellite")
        self.assertIn("theme-satellite", self.js("document.getElementById('map').className"))
        self.assertEqual(self.js("setBaseLayer('topo')"), "outdoor", "starý název -> nový")
        # max. přiblížení podle podkladu (z17 u OpenTopoMap): test_patch11_maps test_13 na viditelné mapě
        self.assertEqual(self.js("setBaseLayer('nesmysl')"), "standard")
        pressed = self.js("Array.from(document.querySelectorAll('#layers button'))"
                          ".map(function(b){return b.dataset.layer + ':' + b.getAttribute('aria-pressed')}).join(',')")
        self.assertEqual(pressed, "standard:true,satellite:false,outdoor:false")

    def test_button_click_notifies_bridge(self):
        self.fake.layers.clear()
        self.js("document.querySelector('#layers button[data-layer=satellite]').click()")
        for _ in range(30):
            if self.fake.layers:
                break
            wait(50)
        self.assertEqual(self.fake.layers, ["satellite"])
        self.js("setBaseLayer('standard')")

    def test_tile_requests_target_only_allowed_hosts(self):
        self.recorder.urls.clear()
        for name in ("satellite", "outdoor", "standard"):
            self.js(f"setBaseLayer('{name}')")
            wait(200)
        remote = [u for u in self.recorder.urls if not u.startswith(("file:", "qrc:", "data:"))]
        self.assertTrue(all(main.is_allowed_web_url(u) for u in remote), remote)

    def test_hover_card_has_thumbnails_place_and_date_range(self):
        markers = [{"id": i, "lat": 49.24 + i * 1e-5, "lon": 13.52, "name": f"IMG_{i}", "city": "Sušice",
                    "date": f"2026-07-{10 + i:02d} 10:00", "thumb": f"file:///C:/t/thumb_{i}.jpg"} for i in range(5)]
        self.js(f"setMarkers({json.dumps(markers)})")
        card = self.js("getClusters()[0].card")
        self.assertEqual(card.count('class="t"'), 4, "nejvýš 4 náhledy")
        self.assertNotIn("<img", card, "náhledy jako CSS pozadí, žádné <img>")
        self.assertIn("Sušice", card)
        self.assertIn("5 photos", card)
        self.assertIn("7/10/2026 – 7/14/2026", card)

    def test_single_photo_card(self):
        markers = [{"id": 7, "lat": 49.0, "lon": 13.0, "name": "IMG_7.HEIC", "city": "Kolinec",
                    "date": "2026-07-06 09:00", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        card = self.js("getClusters()[0].card")
        self.assertIn("IMG_7.HEIC", card)
        self.assertIn("Kolinec · 7/6/2026", card)

    def test_overlapping_cells_are_merged(self):
        """Dva body těsně u sebe, ale v sousedních buňkách mřížky -> jeden pin, ne dva přes sebe."""
        zoom = 10
        size = self.js("TILE_PX") * 2 ** zoom
        cell = self.js("CELL_PX")
        p = self.js(f"mercPx(49.5, 13.5, {zoom})")
        x0 = (p["x"] // cell) * cell + cell - 10           # bod u hranice buňky a druhý 20 px za ní

        def unproject(x, y):
            lon = x / size * 360 - 180
            lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size))))
            return lat, lon
        (la, lo), (lb, lob) = unproject(x0, p["y"]), unproject(x0 + 20, p["y"])
        markers = [{"id": 1, "lat": la, "lon": lo, "name": "a", "city": "", "date": "", "thumb": ""},
                   {"id": 2, "lat": lb, "lon": lob, "name": "b", "city": "", "date": "", "thumb": ""}]
        self.js(f"setMarkers({json.dumps(markers)})")
        self.assertEqual(self.js(f"computeClusters({zoom}).length"), 1)
    def test_cluster_click_zooms_smoothly(self):
        src = open(os.path.join(HERE, "map_vault.html"), encoding="utf-8").read()
        self.assertIn("map.flyTo(Object.assign(opts", src)
        self.assertIn("prefers-reduced-motion", src, "bez animace pro uživatele s omezeným pohybem")


if __name__ == "__main__":
    unittest.main(verbosity=2)
