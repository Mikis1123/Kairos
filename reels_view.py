"""Throwback Reels – zobrazení (Pro vás › Sestřihy).

ReelsPanel: karty návrhů (výlety, roky, „Před N lety“) – výpočet na pozadí (SuggestJob).
ReelPlayer: přehrávač sestřihu 16:9 – kreslí ReelRenderer (stejný jako export), hraje skladbu
a čas sestřihu jemně táhne za hodinami hudby (director.BeatClock), takže střihy padají na doby
i při zaváhání přehrávače. Mezerník / klik = pauza, ←/→ = o záběr.
ExportJob: MP4 na pozadí (reel_export) s průběhem.
"""
from i18n import tr
import os
import time

from PyQt6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import director
import motion
import reels
from ui_controls import EmptyState

CARD_H = 210
CARD_MIN_W = 300
PLAYER_EDGE = 1600            # přehrávač: obrázky do 1600 px (export 2048)
TICK_MS = 15
AUDIO_WAIT_S = 2.0            # start: sestřih čeká na skutečný rozběh hudby (QMediaPlayer ~1 s bufferuje)
SNAP_S = 0.25                 # větší odchylka od hudby (skok, zaváhání) se srovná hned, menší dotahuje zámek


class _Signals(QObject):
    done = pyqtSignal(object)
    progress = pyqtSignal(float)


def _music_for(key, tracks):
    """Stejný návrh = stejná skladba (deterministicky), ať náhled i export znějí stejně."""
    if not tracks:
        return None
    return tracks[reels._seed(key) % len(tracks)]


class SuggestJob(QRunnable):
    """Návrhy sestřihů + obálka každého (nejlepší snímek bez CLIP – rychle)."""

    def __init__(self, connect):
        super().__init__()
        self.connect = connect
        self.signals = _Signals()

    def run(self):
        out = []
        try:
            from viewer_hud import load_image_file
            conn = self.connect()
            try:
                for s in reels.suggestions(conn):
                    items = reels.score(reels.candidates(conn, s["key"]))
                    best = max(items, key=lambda m: m["score"]) if items else None
                    img = load_image_file(best["thumb"], QSize(720, 720)) if best and best.get("thumb") else QImage()
                    out.append((s, img))
            finally:
                conn.close()
        except Exception as e:
            print(f"[Reels] návrhy: {e}")
        try:
            self.signals.done.emit(out)
        except RuntimeError:
            pass


class BuildJob(QRunnable):
    """Kurátor + Director + načtení obrázků pro přehrávač."""

    def __init__(self, connect, suggestion, track):
        super().__init__()
        self.connect, self.suggestion, self.track = connect, suggestion, track
        self.signals = _Signals()

    def run(self):
        result = None
        try:
            import ambient_music
            import reel_render
            conn = self.connect()
            try:
                t0 = time.perf_counter()
                beats = ambient_music.beat_map(self.track) if self.track else None
                plan = reels.build(conn, self.suggestion, beats)
            finally:
                conn.close()
            if plan is not None:
                plan["music"] = self.track
                r = reel_render.ReelRenderer(plan, max_edge=PLAYER_EDGE)
                r.load_all()
                plan["build_ms"] = round((time.perf_counter() - t0) * 1000)
                result = r
        except Exception as e:
            print(f"[Reels] sestřih: {e}")
        try:
            self.signals.done.emit(result)
        except RuntimeError:
            pass


class ExportJob(QThread):
    progress = pyqtSignal(float)
    finished_ok = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def __init__(self, plan, out_path, parent=None):
        super().__init__(parent)
        self.plan, self.out_path = plan, out_path
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import reel_export
            import reel_render
            r = reel_render.ReelRenderer(self.plan, max_edge=2048)
            if not r.load_all(should_stop=lambda: self._stop):
                return
            music = (self.plan.get("music") or {}).get("path")
            stats = reel_export.export(r, self.out_path, music=music, progress=self.progress.emit,
                                       should_stop=lambda: self._stop)
            self.finished_ok.emit(self.out_path, stats)
        except Exception as e:
            if type(e).__name__ != "ExportCancelled":
                self.failed.emit(str(e))


# ── karta návrhu ──────────────────────────────────────────────────────────────

class ReelCard(QWidget):
    opened = pyqtSignal(object)

    def __init__(self, suggestion, image, parent=None):
        super().__init__(parent)
        self.suggestion, self.image = suggestion, image
        self._scaled = None
        self.setFixedHeight(CARD_H)
        self.setMinimumWidth(CARD_MIN_W)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(tr("reels-a11y", title=suggestion['title']))
        self.setToolTip(tr("reels-play-tooltip"))
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.opened.emit(self.suggestion)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.suggestion)
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        path = QPainterPath()
        path.addRoundedRect(r, 14, 14)
        p.setClipPath(path)
        p.fillRect(r, QColor(28, 28, 32))
        if not self.image.isNull():
            if self._scaled is None or self._scaled.size() != r.size().toSize():
                s = self.image.scaled(r.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
                self._scaled = s.copy((s.width() - int(r.width())) // 2, int((s.height() - r.height()) * 0.42),
                                      int(r.width()), int(r.height()))
            hv = self._hover.value
            p.drawImage(r.adjusted(-6 * hv, -6 * hv, 6 * hv, 6 * hv), self._scaled)
        from PyQt6.QtGui import QLinearGradient
        g = QLinearGradient(0, r.top() + r.height() * 0.35, 0, r.bottom())
        g.setColorAt(0, QColor(0, 0, 0, 0))
        g.setColorAt(1, QColor(0, 0, 0, 200))
        p.fillRect(r, g)
        # tlačítko přehrát
        c = r.center()
        rad = 22 + 3 * self._hover.value
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 60 + int(60 * self._hover.value)))
        p.drawEllipse(c, rad, rad)
        tri = QPainterPath()
        tri.moveTo(c.x() - 6, c.y() - 9)
        tri.lineTo(c.x() - 6, c.y() + 9)
        tri.lineTo(c.x() + 10, c.y())
        tri.closeSubpath()
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawPath(tri)
        p.setPen(QColor(255, 255, 255))
        f = QFont(self.font())
        f.setPixelSize(17)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.drawText(r.adjusted(16, 0, -16, -34), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                   self.suggestion["title"])
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 190))
        p.drawText(r.adjusted(16, 0, -16, -14), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom),
                   self.suggestion.get("subtitle", ""))


class ReelsPanel(QScrollArea):
    opened = pyqtSignal(object)
    loaded = pyqtSignal(int)

    def __init__(self, connect, parent=None):
        super().__init__(parent)
        self.connect = connect
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self._job = None
        host = QWidget()
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(20, 18, 20, 20)
        self.grid.setSpacing(10)
        self.setWidget(host)
        self.cards = []

    def load(self, pool=None):
        from PyQt6.QtCore import QThreadPool
        job = SuggestJob(self.connect)
        job.signals.done.connect(self._on_loaded)
        self._job = job
        (pool or QThreadPool.globalInstance()).start(job)

    def _on_loaded(self, found):
        for c in self.cards:
            c.deleteLater()
        self.cards = []
        if not found:
            empty = EmptyState("reel", tr("reels-empty-title"),
                               tr("reels-empty-body", count=reels.MIN_SHOTS))
            self.grid.addWidget(empty, 0, 0)
            self.cards.append(empty)
        cols = 2
        for k, (s, img) in enumerate(found):
            card = ReelCard(s, img)
            card.opened.connect(self.opened.emit)
            self.grid.addWidget(card, k // cols, k % cols)
            self.cards.append(card)
        self.grid.setRowStretch(len(found) // cols + 1, 1)
        self.loaded.emit(len(found))


# ── přehrávač ─────────────────────────────────────────────────────────────────

class ReelPlayer(QWidget):
    """Sestřih v reálném čase. Hudba je hodiny: čas sestřihu ji jemně dotahuje (BeatClock)."""
    ended = pyqtSignal()

    def __init__(self, renderer, parent=None, audio=True):
        super().__init__(parent)
        self.renderer = renderer
        self.plan = renderer.plan
        self.t = 0.0
        self.playing = False
        self._last = None
        self.clock = director.BeatClock(offset=0.0, relock=1e9)
        self.frame_ms = []               # doba vykreslení (měření)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(480, 270)
        self.setAutoFillBackground(False)
        self._wait_audio = 0.0
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        self.player = self.audio_out = None
        music = (self.plan.get("music") or {}).get("path")
        if audio and music and os.path.exists(music):
            try:
                from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
                self.audio_out = QAudioOutput(self)
                self.audio_out.setVolume(0.8)
                self.player = QMediaPlayer(self)
                self.player.setAudioOutput(self.audio_out)
                self.player.setSource(QUrl.fromLocalFile(music))
            except Exception as e:
                print(f"[Reels] hudba: {e}")
                self.player = None

    def play(self):
        if self.t >= self.plan["total"]:
            self.seek(0.0)
        self.playing = True
        self._last = time.perf_counter()
        self._wait_audio = AUDIO_WAIT_S if self.player is not None else 0.0
        if self.player is not None:
            self.player.setPosition(int(self.t * 1000))
            self.player.play()
        self.timer.start(TICK_MS)
        self.update()

    def pause(self):
        self.playing = False
        self.timer.stop()
        if self.player is not None:
            self.player.pause()
        self.update()

    def toggle(self):
        self.pause() if self.playing else self.play()

    def seek(self, t):
        self.t = max(0.0, min(self.plan["total"], t))
        if self.player is not None:
            self.player.setPosition(int(self.t * 1000))
        self.update()

    def step(self, delta):
        segs = self.plan["segs"]
        i = director.index_at(segs, self.t + 0.05)
        j = max(0, min(len(segs) - 1, i + delta))
        self.seek(segs[j]["t0"])

    def stop(self):
        self.pause()
        if self.player is not None:
            self.player.stop()

    def _audio_time(self):
        p = self.player
        if p is None:
            return None
        from PyQt6.QtMultimedia import QMediaPlayer
        if p.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            return None
        return p.position() / 1000.0

    def _tick(self):
        now = time.perf_counter()
        dt = min(0.1, now - (self._last or now))
        self._last = now
        at = self._audio_time()
        if self._wait_audio > 0:
            if at is None or at <= 0.0:            # hudba ještě nehraje: obraz stojí (nejvýš AUDIO_WAIT_S)
                self._wait_audio -= dt
                self.update()
                return
            self._wait_audio = 0.0
        if at is not None and at - self.t > SNAP_S:
            self.t = at                            # obraz zaostal (zaváhání) -> srovnat
            rate = 1.0
        elif at is not None and self.t - at > SNAP_S:
            rate = 0.0                             # hudba zaostala (bufferuje) -> obraz počká
        else:
            rate = self.clock.rate(self.t, at)     # jemný zámek ±12 %
        self.t += dt * rate
        if self.t >= self.plan["total"]:
            self.t = self.plan["total"]
            self.pause()
            if self.player is not None:
                self.player.stop()
            self.ended.emit()
        self.update()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key.Key_Space:
            self.toggle()
        elif k == Qt.Key.Key_Left:
            self.step(-1)
        elif k == Qt.Key.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            r = self._frame_rect()
            if e.position().y() > r.bottom() - 18 and r.width() > 0:      # pruh průběhu: skok
                self.seek((e.position().x() - r.left()) / r.width() * self.plan["total"])
            else:
                self.toggle()

    def _frame_rect(self):
        w, h = self.width(), self.height()
        fw = min(w, h * 16 / 9)
        fh = fw * 9 / 16
        return QRectF((w - fw) / 2, (h - fh) / 2, fw, fh)

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        r = self._frame_rect()
        t0 = time.perf_counter()
        p.save()
        p.translate(r.topLeft())
        p.setClipRect(QRectF(0, 0, r.width(), r.height()))
        self.renderer.render(p, r.width(), r.height(), self.t)
        p.restore()
        if self.playing:
            self.frame_ms.append((time.perf_counter() - t0) * 1000)
            del self.frame_ms[:-600]
        # průběh s ryskami střihů (doby)
        total = self.plan["total"] or 1
        y = r.bottom() - 4
        p.fillRect(QRectF(r.left(), y, r.width(), 3), QColor(255, 255, 255, 50))
        p.fillRect(QRectF(r.left(), y, r.width() * self.t / total, 3), QColor(255, 255, 255, 210))
        for c in self.plan.get("cuts", []):
            p.fillRect(QRectF(r.left() + r.width() * c / total - 0.5, y - 2, 1, 7), QColor(255, 255, 255, 90))
        if not self.playing:
            c = r.center()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 110))
            p.drawEllipse(c, 34, 34)
            tri = QPainterPath()
            tri.moveTo(c.x() - 9, c.y() - 14)
            tri.lineTo(c.x() - 9, c.y() + 14)
            tri.lineTo(c.x() + 15, c.y())
            tri.closeSubpath()
            p.setBrush(QColor(255, 255, 255, 230))
            p.drawPath(tri)
        p.end()


class ReelPage(QWidget):
    """Obsah stránky sestřihu: přehrávač (po načtení) nebo stav načítání."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.player = None
        self.placeholder = EmptyState("reel", tr("reels-rendering-title"), tr("reels-rendering-body"))
        self.lay.addWidget(self.placeholder)

    def set_renderer(self, renderer):
        if renderer is None:
            self.placeholder.setParent(None)
            self.placeholder = EmptyState("reel", tr("reels-failed-title"), tr("reels-failed-body"))
            self.lay.addWidget(self.placeholder)
            return
        self.placeholder.setParent(None)
        self.player = ReelPlayer(renderer, self)
        self.lay.addWidget(self.player)
        self.player.setFocus()
        self.player.play()
