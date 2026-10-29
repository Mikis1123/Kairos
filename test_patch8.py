"""Regresní testy pro Patch 8 – výkon prohlížeče a Fáze 3 (osoby: slučování).

Spuštění (ve složce projektu):   python -m pytest test_patch8.py -v

Pokrývá:
  Výkon      – OpenGL viewport plátna, fotky předem zmenšené na plochu, video přes QML
               VideoOutput (GPU, ne převod snímků v GUI vlákně), sklo HUD jen při změně obrazu
               (cache), během přehrávání bez snímkování pozadí.
  Shlukování – slučovací průchod (average linkage), omezení: dvě kotvy / společná fotka se
               nikdy nespojí; is_confirmed kotvy přežijí přeshlukování.
  Slučování  – merge_people (+ přitažení nepřiřazených obličejů), undo_merge, návrhy
               „Je to stejná osoba?“, odmítnutí návrhu, UI: Ctrl+klik výběr, Sloučit, Vrátit.

Testy pracují VÝHRADNĚ v dočasných složkách; tvoje photos.db ani fotky se nepoužijí.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np

HAS_QT = False
try:
    from PyQt6.QtCore import Qt, QEventLoop, QTimer, QEvent, QPointF, QRectF
    from PyQt6.QtGui import QPixmap, QImage, QColor, QPainter, QMouseEvent
    from PyQt6.QtWidgets import QApplication
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    _app = QApplication.instance() or QApplication(sys.argv)
    HAS_QT = True
except Exception:
    HAS_QT = False

import database
import face_cluster

if HAS_QT:
    import main
    import viewer_hud
    from viewer import MediaViewerWidget
    from PyQt6.QtMultimedia import QMediaPlayer

HERE = os.path.dirname(os.path.abspath(__file__))
DIM = 128


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def basis(seed):
    """Dvojice navzájem kolmých jednotkových vektorů."""
    rng = np.random.default_rng(seed)
    u = unit(rng.normal(size=DIM))
    w = rng.normal(size=DIM)
    w = unit(w - (w @ u) * u)
    return u, w


def at_cos(u, w, cos):
    return unit(cos * u + np.sqrt(max(0.0, 1 - cos ** 2)) * w)


def jitter(v, seed, amount=0.03):
    rng = np.random.default_rng(1_000_000 + seed)     # jiný prostor semínek než basis()
    return unit(v + amount * rng.normal(size=DIM))


class TempEnv(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omni_p8_test_")
        self._saved = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp, "photos.db")
        database.init_db()
        self._media = 0
        face_cluster.invalidate_centroid_cache()

    def tearDown(self):
        database.DB_PATH = self._saved
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

    def media(self, path=None):
        self._media += 1
        path = path or os.path.join(self.tmp, f"m{self._media}.jpg")
        return self.x("INSERT INTO media_items (file_path, file_name, media_type) VALUES (?, ?, 'photo')",
                      (path, os.path.basename(path)))

    def face(self, vec, media_id=None, person_id=None, quality=30.0):
        media_id = media_id or self.media()
        return self.x("INSERT INTO face_instances (media_id, person_id, embedding, quality_score, "
                      "bbox_x, bbox_y, bbox_w, bbox_h) VALUES (?, ?, ?, ?, 0.1, 0.1, 0.2, 0.3)",
                      (media_id, person_id, np.asarray(vec, dtype=np.float32).tobytes(), quality))

    def person(self, name=face_cluster.UNKNOWN_NAME, confirmed=0, favorite=0):
        return self.x("INSERT INTO people (name, is_confirmed, is_favorite) VALUES (?, ?, ?)",
                      (name, confirmed, favorite))

    def people_faces(self):
        rows = self.q("SELECT person_id, COUNT(*) FROM face_instances WHERE person_id IS NOT NULL "
                      "GROUP BY person_id ORDER BY 2 DESC")
        return [n for _, n in rows]


# ═════════════════════════════════════════════════════════════════════════════
# Výkon prohlížeče
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestCanvasPerformance(unittest.TestCase):

    def setUp(self):
        self.canvas = viewer_hud.MediaCanvas()
        self.canvas.resize(800, 600)
        self.canvas.show()
        _app.processEvents()
        self.addCleanup(self.canvas.deleteLater)

    def test_raster_viewport_not_opengl(self):
        """Patch 9: QOpenGLWidget viewport v okně skládaném přes D3D11 (WebEngine + QQuickWidget)
        nekreslil vůbec nic – plátno je záměrně rastrové (původně Patch 8 test na OpenGL)."""
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
        self.assertFalse(self.canvas.uses_opengl)
        self.assertNotIsInstance(self.canvas.viewport(), QOpenGLWidget)

    def test_software_fallback_switch(self):
        with mock.patch.dict(os.environ, {viewer_hud.SOFTWARE_RENDER_ENV: "1"}):
            c = viewer_hud.MediaCanvas()
        self.addCleanup(c.deleteLater)
        self.assertEqual(c.video_backend, "graphics", "bez GPU záložní QGraphicsVideoItem")

    def test_photo_is_prescaled_to_the_viewport(self):
        """Každé překreslení = prosté vykreslení, ne vyhlazené zmenšení 12 Mpx snímku."""
        big = QPixmap(4032, 3024)
        big.fill(QColor("darkgreen"))
        self.canvas.set_image(big)
        shown = self.canvas.displayed_pixmap()
        dpr = self.canvas.devicePixelRatioF()
        vw, vh = self.canvas.viewport().width(), self.canvas.viewport().height()
        self.assertLessEqual(shown.width(), round(vw * dpr) + 1)
        self.assertLessEqual(shown.height(), round(vh * dpr) + 1)
        self.assertEqual(self.canvas.current_pixmap().width(), 4032, "zdroj zůstává v plném rozlišení")
        self.assertEqual(self.canvas.photo_item.scale(), 1.0)

    def test_resize_refits_smoothly_after_drag(self):
        src = QPixmap(2000, 1000)
        src.fill(QColor("navy"))
        self.canvas.set_image(src)
        self.canvas.resize(500, 400)
        _app.processEvents()
        self.assertTrue(self.canvas._refit_timer.isActive(), "vyhlazení až po dokončení tažení")
        wait(viewer_hud.SMOOTH_REFIT_MS + 150)
        dpr = self.canvas.devicePixelRatioF()
        self.assertLessEqual(self.canvas.displayed_pixmap().width(), round(self.canvas.viewport().width() * dpr) + 1)

    def test_video_layer_is_gpu_and_overlay_safe(self):
        from PyQt6.QtQuickWidgets import QQuickWidget
        self.assertEqual(self.canvas.video_backend, "quick")
        surface = self.canvas.video_surface
        self.assertIsInstance(surface, QQuickWidget)
        self.assertFalse(surface.isWindow())
        self.assertFalse(surface.testAttribute(Qt.WidgetAttribute.WA_NativeWindow))
        # Patch 9: vrstva je pořád viditelná, mění se jen pořadí pod / nad fotkou (bez černého bliknutí)
        order = lambda w: self.canvas.children().index(w)
        self.assertTrue(surface.isVisible())
        self.assertLess(order(surface), order(self.canvas.viewport()), "video pod fotkou")
        self.canvas.show_video(True)
        _app.processEvents()
        self.assertTrue(surface.isVisible())
        self.assertGreater(order(surface), order(self.canvas.viewport()), "video nad fotkou")
        self.assertEqual(surface.geometry(), self.canvas.rect())
        self.canvas.show_video(False)
        self.assertTrue(surface.isVisible())
        self.assertLess(order(surface), order(self.canvas.viewport()))

    def test_double_click_on_video_layer(self):
        got = []
        self.canvas.double_clicked.connect(lambda: got.append(1))
        self.canvas.show_video(True)
        surface = self.canvas.video_surface
        ev = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(20, 20), QPointF(surface.mapToGlobal(surface.rect().center())),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(surface, ev)
        self.assertEqual(got, [1])

    def test_snapshot_renders_scene_region_downscaled(self):
        src = QPixmap(800, 600)
        src.fill(QColor(200, 30, 30))
        self.canvas.set_image(src)
        img = self.canvas.snapshot(self.canvas.viewport().rect().adjusted(100, 100, -100, -100), shrink=14)
        self.assertLessEqual(img.width(), 600 // 14 + 1)
        self.assertGreater(QColor(img.pixel(img.width() // 2, img.height() // 2)).red(), 150)

    def test_qml_file_is_packaged(self):
        self.assertTrue(os.path.exists(os.path.join(HERE, "video_output.qml")))
        with open(os.path.join(HERE, "app.spec"), encoding="utf-8") as f:
            spec = f.read()
        for needle in ("video_output.qml", "PyQt6.QtQuickWidgets", "PyQt6.QtOpenGLWidgets"):
            self.assertIn(needle, spec)


@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestGlassCaching(unittest.TestCase):

    def setUp(self):
        self.viewer = MediaViewerWidget()
        self.viewer.resize(1000, 700)
        self.viewer.show()
        src = QPixmap(1200, 800)
        src.fill(QColor(30, 120, 200))
        self.viewer.canvas.set_image(src)
        _app.processEvents()
        wait(80)
        self.addCleanup(self._close)

    def _close(self):
        self.viewer.cleanup_playback()
        self.viewer.hide()
        self.viewer.deleteLater()
        _app.processEvents()

    def test_backdrop_is_cached_until_content_changes(self):
        panel = self.viewer.top_hud
        panel.fade_in()
        panel.refresh_backdrop()
        n = panel.refreshes
        for _ in range(5):
            panel.refresh_backdrop()            # pohyb myši / opakované zobrazení
        self.assertEqual(panel.refreshes, n, "bez změny obrazu se sklo nepřepočítává")
        other = QPixmap(1200, 800)
        other.fill(QColor(220, 180, 20))
        self.viewer.canvas.set_image(other)
        panel.refresh_backdrop()
        self.assertEqual(panel.refreshes, n + 1)

    def test_no_periodic_backdrop_polling(self):
        self.assertFalse(hasattr(self.viewer, "_backdrop_timer"), "žádné snímkování 4× za sekundu")
        self.assertTrue(self.viewer._backdrop_debounce.isSingleShot())

    def test_playback_uses_plain_translucent_pills(self):
        with mock.patch.object(self.viewer.player, "playbackState",
                               return_value=QMediaPlayer.PlaybackState.PlayingState):
            self.viewer._sync_play_icon()
        for panel in self.viewer.hud_panels:
            self.assertFalse(panel.frosted)
            self.assertFalse(panel.has_backdrop())
        with mock.patch.object(self.viewer.player, "playbackState",
                               return_value=QMediaPlayer.PlaybackState.PausedState):
            self.viewer._sync_play_icon()
        self.assertTrue(all(p.frosted for p in self.viewer.hud_panels), "pauza nad fotkou = zase sklo")

    def test_player_renders_to_qml_video_output(self):
        self.assertIs(self.viewer.player.videoOutput(), self.viewer.canvas.video_output)
        self.assertIn(self.viewer.canvas.video_surface, self.viewer.hud.surfaces,
                      "pohyb myši nad videem ukáže HUD")


# ═════════════════════════════════════════════════════════════════════════════
# Slučovací průchod
# ═════════════════════════════════════════════════════════════════════════════

class TestMergePass(unittest.TestCase):

    def _clusters(self, groups, anchors=(), media=None):
        vecs = [np.vstack(g) for g in groups]
        sums = np.vstack([v.sum(axis=0) for v in vecs]).astype(np.float32)
        nsum = [len(v) for v in vecs]
        media_sets = [set(m) for m in (media or [[100 * k + i for i in range(len(g))] for k, g in enumerate(groups)])]
        members = [list(range(len(g))) for g in groups]
        is_anchor = [k in anchors for k in range(len(groups))]
        return sums, nsum, media_sets, members, is_anchor

    def test_merges_similar_groups(self):
        u, w = basis(1)
        a = [jitter(u, i) for i in range(3)]
        b = [jitter(at_cos(u, w, 0.6), 10 + i) for i in range(3)]
        sums, nsum, media, members, anchors = self._clusters([a, b])
        self.assertEqual(face_cluster._merge_pass(sums, nsum, media, members, anchors, 0.48), 1)
        self.assertEqual(sorted(len(m) for m in members), [0, 6])

    def test_dissimilar_groups_stay_apart(self):
        u, w = basis(2)
        sums, nsum, media, members, anchors = self._clusters(
            [[jitter(u, 1)], [jitter(at_cos(u, w, 0.3), 2)]])
        self.assertEqual(face_cluster._merge_pass(sums, nsum, media, members, anchors, 0.48), 0)

    def test_shared_photo_blocks_merge(self):
        u, w = basis(3)
        groups = [[jitter(u, 1), jitter(u, 2)], [jitter(u, 3)]]
        sums, nsum, media, members, anchors = self._clusters(groups, media=[[1, 2], [2]])
        self.assertEqual(face_cluster._merge_pass(sums, nsum, media, members, anchors, 0.48), 0,
                         "dva obličeje z jedné fotky nejsou tatáž osoba")

    def test_two_anchors_never_merge_but_free_joins_anchor(self):
        u, w = basis(4)
        groups = [[jitter(u, 1)], [jitter(u, 2)], [jitter(u, 3)]]
        sums, nsum, media, members, anchors = self._clusters(groups, anchors=(1, 2))
        merges = face_cluster._merge_pass(sums, nsum, media, members, anchors, 0.48)
        self.assertEqual(merges, 1)
        self.assertEqual(members[0], [], "volný shluk se připojil ke kotvě (kotva přežívá)")
        self.assertTrue(members[1] and members[2], "dvě kotvy zůstaly oddělené")

    def test_threshold_is_calibrated_constant(self):
        self.assertAlmostEqual(face_cluster.MERGE_THRESHOLD, 0.48)
        self.assertLess(face_cluster.SUGGEST_MIN_SIMILARITY, face_cluster.MERGE_THRESHOLD)


class TestReclusterWithMerge(TempEnv):

    def _split_person(self, seed=7, n=4, cos=0.58):
        """Jedna osoba ve dvou „světlech“: hladový průchod (0.62) ji rozdělí na dva shluky."""
        u, w = basis(seed)
        v = at_cos(u, w, cos)
        for i in range(n):
            self.face(jitter(u, seed * 100 + i, 0.02))
        for i in range(n):
            self.face(jitter(v, seed * 100 + 50 + i, 0.02))
        return u, v

    def test_over_split_person_is_merged(self):
        self._split_person()
        stats = face_cluster.strict_recluster_faces(threshold=0.62, merge_threshold=1.0)
        self.assertEqual(self.people_faces(), [4, 4], "bez slučování 2 osoby")
        stats = face_cluster.strict_recluster_faces(threshold=0.62)
        self.assertEqual(self.people_faces(), [8])
        self.assertGreaterEqual(stats["merged"], 1)
        self.assertEqual(stats["merge_threshold"], face_cluster.MERGE_THRESHOLD)

    def test_different_people_are_not_merged(self):
        self._split_person(seed=7)
        u2, _ = basis(99)
        for i in range(4):
            self.face(jitter(u2, 900 + i, 0.02))
        face_cluster.strict_recluster_faces(threshold=0.62)
        self.assertEqual(self.people_faces(), [8, 4])

    def test_confirmed_unnamed_person_survives_recluster(self):
        u, w = basis(11)
        pid = self.person(confirmed=1)
        ids = [self.face(jitter(u, i, 0.02), person_id=pid) for i in range(3)]
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT COUNT(*) FROM people WHERE id=?", (pid,))[0][0], 1)
        self.assertEqual({r[0] for r in self.q("SELECT person_id FROM face_instances WHERE id IN (?,?,?)", ids)},
                         {pid})

    def test_unconfirmed_auto_person_is_rebuilt(self):
        u, _ = basis(12)
        pid = self.person()
        for i in range(3):
            self.face(jitter(u, i, 0.02), person_id=pid)
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT COUNT(*) FROM people WHERE id=?", (pid,))[0][0], 0,
                         "nepotvrzená automatická osoba se staví znovu (nové id)")
        self.assertEqual(self.people_faces(), [3])

    def test_two_confirmed_similar_people_stay_separate(self):
        u, w = basis(13)
        a, b = self.person("Jana"), self.person(confirmed=1)
        for i in range(3):
            self.face(jitter(u, i, 0.02), person_id=a)
            self.face(jitter(u, 10 + i, 0.02), person_id=b)
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT COUNT(*) FROM people")[0][0], 2)

    def test_free_faces_join_named_anchor_in_merge_pass(self):
        u, w = basis(14)
        jana = self.person("Jana")
        for i in range(3):
            self.face(jitter(u, i, 0.02), person_id=jana)
        v = at_cos(u, w, 0.58)                   # pod prahem 1. průchodu, nad prahem slučování
        free = [self.face(jitter(v, 50 + i, 0.02)) for i in range(3)]
        face_cluster.strict_recluster_faces(threshold=0.62)
        rows = self.q(f"SELECT person_id FROM face_instances WHERE id IN ({','.join('?' * len(free))})", free)
        self.assertEqual({r[0] for r in rows}, {jana})
        self.assertEqual(self.q("SELECT COUNT(*) FROM people")[0][0], 1)


# ═════════════════════════════════════════════════════════════════════════════
# Ruční slučování, Vrátit, návrhy
# ═════════════════════════════════════════════════════════════════════════════

class TestMergePeople(TempEnv):

    def setUp(self):
        super().setUp()
        self.u, self.w = basis(21)
        self.a = self.person()
        self.b = self.person("Petr", favorite=1)
        self.fa = [self.face(jitter(self.u, i, 0.02), person_id=self.a) for i in range(3)]
        self.fb = [self.face(jitter(self.u, 10 + i, 0.02), person_id=self.b) for i in range(2)]

    def test_merge_moves_faces_confirms_and_keeps_name(self):
        res = face_cluster.merge_people(self.a, [self.b])
        self.assertEqual(res["merged"], 1)
        self.assertEqual(res["moved_faces"], 2)
        self.assertEqual(self.q("SELECT id FROM people"), [(self.a,)])
        name, fav, conf = self.q("SELECT name, is_favorite, is_confirmed FROM people WHERE id=?", (self.a,))[0]
        self.assertEqual((name, fav, conf), ("Petr", 1, 1), "jméno a srdíčko ze zdroje, potvrzeno")
        self.assertEqual({r[0] for r in self.q("SELECT person_id FROM face_instances")}, {self.a})

    def test_explicit_name_wins(self):
        face_cluster.merge_people(self.a, [self.b], name="Petr Novák")
        self.assertEqual(self.q("SELECT name FROM people WHERE id=?", (self.a,))[0][0], "Petr Novák")

    def test_pulls_matching_unassigned_faces_once_per_photo(self):
        shared = self.media()
        near1 = self.face(jitter(self.u, 30, 0.02), media_id=shared)
        near2 = self.face(jitter(self.u, 31, 0.02), media_id=shared)       # stejná fotka
        on_own_photo = self.face(jitter(self.u, 32, 0.02),
                                 media_id=self.q("SELECT media_id FROM face_instances WHERE id=?", (self.fa[0],))[0][0])
        far = self.face(jitter(at_cos(self.u, self.w, 0.1), 33, 0.02))
        res = face_cluster.merge_people(self.a, [self.b])
        self.assertEqual(res["pulled_in"], 1)
        pulled = {r[0] for r in self.q("SELECT id FROM face_instances WHERE person_id=? AND id IN (?,?,?,?)",
                                       (self.a, near1, near2, on_own_photo, far))}
        self.assertEqual(len(pulled & {near1, near2}), 1, "z jedné fotky nejvýš jeden obličej")
        self.assertNotIn(on_own_photo, pulled)
        self.assertNotIn(far, pulled)

    def test_undo_restores_everything(self):
        free = self.face(jitter(self.u, 40, 0.02))
        before_people = self.q("SELECT id, name, is_favorite, COALESCE(is_confirmed,0) FROM people ORDER BY id")
        before_faces = self.q("SELECT id, person_id FROM face_instances ORDER BY id")
        res = face_cluster.merge_people(self.a, [self.b])
        self.assertGreaterEqual(res["pulled_in"], 1)
        face_cluster.undo_merge(res["undo"])
        self.assertEqual(self.q("SELECT id, name, is_favorite, COALESCE(is_confirmed,0) FROM people ORDER BY id"),
                         before_people)
        self.assertEqual(self.q("SELECT id, person_id FROM face_instances ORDER BY id"), before_faces)
        self.assertIsNone(self.q("SELECT person_id FROM face_instances WHERE id=?", (free,))[0][0])

    def test_merging_into_itself_is_a_noop(self):
        self.assertIsNone(face_cluster.merge_people(self.a, [self.a]))


class TestSuggestions(TempEnv):

    def test_suggestions_sorted_and_filtered(self):
        u, w = basis(31)
        p1, p2, p3 = self.person(), self.person(), self.person()
        for i in range(3):
            self.face(jitter(u, i, 0.02), person_id=p1)
            self.face(jitter(at_cos(u, w, 0.45), 10 + i, 0.02), person_id=p2)
            self.face(jitter(at_cos(u, w, 0.9), 20 + i, 0.02), person_id=p3)
        s = face_cluster.suggest_person_merges(min_similarity=0.3)
        pairs = [frozenset((x["a"], x["b"])) for x in s]
        # cos: p1–p3 0.9 > p2–p3 ≈ 0.79 > p1–p2 0.45
        self.assertEqual(pairs, [frozenset((p1, p3)), frozenset((p2, p3)), frozenset((p1, p2))])
        sims = [x["similarity"] for x in s]
        self.assertEqual(sims, sorted(sims, reverse=True))

    def test_differently_named_and_shared_photo_pairs_excluded(self):
        u, _ = basis(32)
        jana, eva, anon = self.person("Jana"), self.person("Eva"), self.person()
        shared = self.media()
        self.face(jitter(u, 1, 0.02), person_id=jana)
        self.face(jitter(u, 2, 0.02), person_id=eva)
        self.face(jitter(u, 3, 0.02), person_id=anon, media_id=shared)
        self.face(jitter(u, 4, 0.02), person_id=jana, media_id=shared)
        pairs = {frozenset((x["a"], x["b"])) for x in face_cluster.suggest_person_merges()}
        self.assertNotIn(frozenset((jana, eva)), pairs, "různě pojmenované osoby se nenabízí")
        self.assertNotIn(frozenset((jana, anon)), pairs, "společná fotka = různé osoby")
        self.assertIn(frozenset((eva, anon)), pairs)

    def test_dismissed_pair_is_not_offered_again(self):
        u, _ = basis(33)
        a, b = self.person(), self.person()
        self.face(jitter(u, 1, 0.02), person_id=a)
        self.face(jitter(u, 2, 0.02), person_id=b)
        self.assertEqual(len(face_cluster.suggest_person_merges()), 1)
        face_cluster.dismiss_person_merge(b, a)
        self.assertEqual(face_cluster.suggest_person_merges(), [])

    def test_recluster_cleans_dismissals_of_deleted_people(self):
        u, _ = basis(34)
        a, b = self.person(), self.person()
        for i in range(2):
            self.face(jitter(u, i, 0.02), person_id=a)
            self.face(jitter(u, 10 + i, 0.02), person_id=b)
        face_cluster.dismiss_person_merge(a, b)
        face_cluster.strict_recluster_faces()
        self.assertEqual(self.q("SELECT COUNT(*) FROM person_merge_dismissed")[0][0], 0)

    def test_schema(self):
        cols = {r[1] for r in self.q("PRAGMA table_info(people)")}
        self.assertIn("is_confirmed", cols)
        self.assertTrue(self.q("SELECT name FROM sqlite_master WHERE name='person_merge_dismissed'"))


# ═════════════════════════════════════════════════════════════════════════════
# UI – stránka Osoby
# ═════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(HAS_QT, "Qt není dostupné")
class TestPeopleUI(TempEnv):

    def setUp(self):
        super().setUp()
        u, w = basis(41)
        self.p1, self.p2, self.p3 = self.person(), self.person(), self.person()
        for i in range(3):
            self.face(jitter(u, i, 0.02), person_id=self.p1)
            self.face(jitter(at_cos(u, w, 0.6), 10 + i, 0.02), person_id=self.p2)
            self.face(jitter(basis(77)[0], 20 + i, 0.02), person_id=self.p3)
        patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.w = main.MainWindow()
        self.w.resize(1280, 820)
        self.w.show()
        self.w._switch_page(1)
        _app.processEvents()
        self.addCleanup(self._close)

    def _close(self):
        self.w.close()
        self.w.deleteLater()
        _app.processEvents()

    def ctrl_click(self, pid):
        card = self.w._person_cards[pid]
        pos = QPointF(card.rect().center())
        for t in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            ev = QMouseEvent(t, pos, QPointF(card.mapToGlobal(card.rect().center())), Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton if t == QEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.ControlModifier)
            QApplication.sendEvent(card, ev)

    def test_ctrl_click_selects_and_shows_merge_button(self):
        self.ctrl_click(self.p1)
        self.assertFalse(self.w.btn_merge.isVisible())
        self.ctrl_click(self.p2)
        self.assertEqual(self.w._selected_people, {self.p1, self.p2})
        self.assertTrue(self.w._person_cards[self.p1].selected)
        self.assertTrue(self.w.btn_merge.isVisible())
        self.assertEqual(self.w.btn_merge.text(), "Sloučit osoby (2)")
        self.assertIs(self.w.stack.currentWidget(), self.w.page_people, "Ctrl+klik neotevírá osobu")
        self.ctrl_click(self.p2)
        self.assertEqual(self.w._selected_people, {self.p1})

    def test_merge_button_merges_with_undo_toast(self):
        self.ctrl_click(self.p1)
        self.ctrl_click(self.p2)
        self.w.btn_merge.click()
        self.assertEqual(len(self.w._person_cards), 2)
        self.assertEqual(self.w._selected_people, set())
        toast = self.w.toasts.toasts[-1]
        self.assertIn("Sloučeno", toast.text)
        toast.btn_action.click()                       # Vrátit
        self.assertEqual(len(self.w._person_cards), 3)

    def test_different_names_ask_which_to_keep(self):
        self.x("UPDATE people SET name='Jana' WHERE id=?", (self.p1,))
        self.x("UPDATE people SET name='Janička' WHERE id=?", (self.p2,))
        self.w._load_people()
        with mock.patch.object(main.InWindowDialog, "getItem", return_value=("Janička", True)) as ask:
            self.w._merge_people_ids([self.p1, self.p2])
        ask.assert_called_once()
        self.assertEqual([r[0] for r in self.q("SELECT name FROM people WHERE name LIKE 'Jan%'")], ["Janička"])

    def test_suggestion_bar_accept_and_reject(self):
        bar = self.w.merge_bar
        self.assertTrue(bar.isVisible())
        self.assertEqual({bar.current()["a"], bar.current()["b"]}, {self.p1, self.p2})
        bar.btn_no.click()
        self.assertEqual(self.q("SELECT COUNT(*) FROM person_merge_dismissed")[0][0], 1)
        self.w._refresh_merge_suggestions()
        self.assertFalse(any({s["a"], s["b"]} == {self.p1, self.p2} for s in bar.suggestions))
        self.x("DELETE FROM person_merge_dismissed")
        self.w._refresh_merge_suggestions()
        bar.btn_yes.click()
        self.assertEqual(self.q("SELECT COUNT(*) FROM people")[0][0], 2)

    def test_rename_confirms_person(self):
        with mock.patch.object(main.InWindowDialog, "getText", return_value=("Karel", True)):
            self.w._rename_person(self.p3, main.UNKNOWN_PERSON)
        self.assertEqual(self.q("SELECT is_confirmed FROM people WHERE id=?", (self.p3,))[0][0], 1)

    def test_escape_clears_selection(self):
        self.ctrl_click(self.p1)
        self.ctrl_click(self.p2)
        self.w._clear_person_selection()
        self.assertFalse(self.w.btn_merge.isVisible())
        self.assertFalse(any(c.selected for c in self.w._person_cards.values()))

    def test_selected_card_paints_blue_ring(self):
        img = QImage(main.PERSON_CARD_W, main.PERSON_CARD_H, QImage.Format.Format_ARGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        main.paint_person_card(p, QRectF(0, 0, main.PERSON_CARD_W, main.PERSON_CARD_H), None, "X", 1, selected=True)
        p.end()
        accent = main.tokens.palette()["accent"].lower()      # Kairos: prstenec ve zvýraznění (Iris)
        ring = sum(1 for x in range(0, img.width(), 2) for y in range(0, img.height(), 2)
                   if QColor(img.pixel(x, y)).name() == accent)
        self.assertGreater(ring, 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
