"""Stavební kameny prohlížeče: plátno (fotka + video v jedné scéně), skleněný HUD,
automatické skrývání ovládání a prezentace (slideshow).

Proč QGraphicsVideoItem místo QVideoWidget: QVideoWidget je na Windows nativní povrch
(„airspace“) – průhledné ovládání nad ním bliká nebo prosvítá a nejde ho rozostřit.
Ve scéně QGraphicsView je video obyčejná položka, takže HUD nad ním je běžný widget.

Výkon: video běží přes QQuickWidget (snímky zůstávají na GPU), fotky drží předem zmenšené
na velikost plochy (každé překreslení = prosté vykreslení bez škálování) a matné sklo HUD
se počítá jen při změně obrazu – během přehrávání videa je HUD čistě průsvitný.

Proč NE QOpenGLWidget jako viewport: okno s QWebEngineView (mapa) a QQuickWidget (video)
skládá Qt na Windows přes Direct3D 11; QOpenGLWidget s tím nefunguje („'D3D11' is not
compatible with QOpenGLWidget“) a nekreslí vůbec nic (QPainter: engine == 0). Rastrový
viewport s předem zmenšeným snímkem je stejně rychlý a funguje s jakýmkoli skládáním.
"""
import os
from collections import OrderedDict

from PyQt6.QtCore import (
    Qt, QObject, QTimer, QEvent, QPoint, QPointF, QRect, QRectF, QSize, QSizeF, QUrl,
    QVariantAnimation, QPropertyAnimation, QEasingCurve, QRunnable, QThreadPool, pyqtSignal,
    QThread, QEventLoop,
)
from PyQt6.QtGui import (
    QPixmap, QImage, QImageReader, QPainter, QPainterPath, QColor, QPen, QLinearGradient,
    QKeyEvent, QCursor,
)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QGraphicsOpacityEffect, QPushButton, QSlider, QLabel, QVBoxLayout, QSizePolicy,
)
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem

import frame_meter
from frame_clock import VsyncAnimation, VsyncPropertyAnimation
import viewer_zoom

HUD_HIDE_DELAY_MS = 2000
HUD_FADE_MS = 220
SLIDE_INTERVAL_MS = 4000
SLIDE_FADE_MS = 700
SMOOTH_REFIT_MS = 120        # po dokončení změny velikosti okna znovu vyhladit
SOFTWARE_RENDER_ENV = "OMNIPHOTO_SOFTWARE_RENDER"   # =1 vypne GPU video (staré ovladače / RDP)
VIDEO_REVEAL_DELAY_MS = 20   # po 1. platném snímku nechat QQuickWidget ho vykreslit, pak odkrýt


def _video_qml_path():
    from paths import RESOURCE_DIR
    return os.path.join(RESOURCE_DIR, "video_output.qml")


def _make_quick_video(parent):
    """Video přes QQuickWidget + QML VideoOutput: snímky zůstávají na GPU (žádné kopírování
    4K snímku do RAM a převod na QImage v GUI vlákně jako u QGraphicsVideoItem) a widget se
    skládá jako běžný widget, takže průsvitný HUD nad ním funguje (žádný „airspace“).
    Vrací (widget, [video_output, ...]) nebo (None, None), když QtQuick není k dispozici.
    Patch 11: dva výstupy – druhý drží předem načtené sousední video (viewer.preload)."""
    if os.environ.get(SOFTWARE_RENDER_ENV) == "1":
        return None, None
    surface = None
    try:
        from PyQt6.QtQuickWidgets import QQuickWidget
        qml = _video_qml_path()
        if not os.path.exists(qml):
            raise FileNotFoundError(qml)
        surface = QQuickWidget(parent)
        surface.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        surface.setClearColor(QColor("#000000"))
        # setSource: kořen scény vlastní QQuickWidget (C++), ne Python – čisté ukončení aplikace
        surface.setSource(QUrl.fromLocalFile(qml))
        if surface.status() != QQuickWidget.Status.Ready:
            raise RuntimeError("; ".join(e.toString() for e in surface.errors()))
        root = surface.rootObject()
        output = root.findChild(QObject, "video")
        if output is None:
            raise RuntimeError("VideoOutput nenalezen")
        spare = root.findChild(QObject, "video2")
        # Fáze 0: skutečně vykreslené snímky videa (QQuickWidget kreslí v GUI vlákně)
        window = surface.quickWindow()
        if window is not None:
            window.afterRendering.connect(lambda: frame_meter.tick("video"),
                                          Qt.ConnectionType.DirectConnection)
        return surface, [output] + ([spare] if spare is not None else [])
    except Exception as e:
        if surface is not None:
            surface.deleteLater()
        print(f"[Viewer] QtQuick video není dostupné, použiji QGraphicsVideoItem: {e}")
        return None, None


# ── Plátno ────────────────────────────────────────────────────────────────────

class MediaCanvas(QGraphicsView):
    """Fotka i video v jedné scéně. Souřadnice scény = pixely viewportu, každá položka
    se sama vejde do plochy (KeepAspectRatio), takže prolínání dvou různě orientovaných
    fotek funguje bez přepočtu transformace pohledu."""
    double_clicked = pyqtSignal()
    content_changed = pyqtSignal()      # nový obraz / velikost -> sklo HUD si přepočítá pozadí

    def __init__(self, parent=None, with_video=True):
        super().__init__(parent)
        self.uses_opengl = False            # viz docstring modulu: rastrový viewport je záměr
        self.setScene(QGraphicsScene(self))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setStyleSheet("QGraphicsView { background: #000000; border: none; }")
        self.setBackgroundBrush(QColor("#000000"))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # jediná položka přes celou plochu -> celé překreslení je nejlevnější varianta
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
        self.setCacheMode(QGraphicsView.CacheModeFlag.CacheNone)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

        self.back_item = QGraphicsPixmapItem()      # odcházející snímek při prolínání
        self.photo_item = QGraphicsPixmapItem()
        for z, item in enumerate((self.back_item, self.photo_item)):
            item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            item.setZValue(z)
            self.scene().addItem(item)
        self.back_item.setVisible(False)

        # Video: QML VideoOutput (GPU), záložně položka scény QGraphicsVideoItem
        self.video_item = None
        self.video_surface, outputs = (_make_quick_video(self) if with_video else (None, None))
        self.video_outputs = list(outputs or [])
        self._active_output = 0
        if self.video_surface is not None:
            self.video_backend = "quick"
            self.video_surface.setMouseTracking(True)
            self.video_surface.installEventFilter(self)
            # Vrstva videa je POŘÁD viditelná, jen schovaná pod fotkou (viewportem). Skrytý
            # QQuickWidget při prvním zobrazení vykreslí černou, než dostane snímek = černé
            # bliknutí; zakrytý ale vykresluje průběžně, takže odkrytí je okamžité.
            self.video_surface.show()
            self.video_surface.stackUnder(self.viewport())
        else:
            self.video_backend = "graphics"
            self.video_item = QGraphicsVideoItem()
            self.video_item.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
            self.video_item.setZValue(2)
            self.video_item.setVisible(False)
            self.scene().addItem(self.video_item)
            self.video_outputs = [self.video_item]
        self._video_on = False

        self._pixmap = QPixmap()            # zdrojový (plný) snímek aktuální fotky
        self._sources = {self.photo_item: QPixmap(), self.back_item: QPixmap()}
        self._content_rev = 0
        self._fade = VsyncAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade.valueChanged.connect(self._on_fade_step)
        self._fade.finished.connect(self._on_fade_done)
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(SMOOTH_REFIT_MS)
        self._refit_timer.timeout.connect(self._refit_smooth)

        # Stage 2: hluboké přiblížení (viewer_zoom) – sklo HUD se přepočítá až po zastavení pohybu
        self.zoom = viewer_zoom.ZoomController(self)
        self._zoom_settle = QTimer(self)
        self._zoom_settle.setSingleShot(True)
        self._zoom_settle.setInterval(120)
        self._zoom_settle.timeout.connect(self._bump)
        self.zoom.changed.connect(lambda _p: self._zoom_settle.start())
        self.zoom_badge = ZoomBadge(self)
        self.zoom.changed.connect(self._on_zoom_changed)

    def _on_zoom_changed(self, percent):
        if self.zoom.is_zoomed() or self.zoom_badge.isVisible():
            self.zoom_badge.flash(percent)

    def event(self, event):
        # sevření prstů (macOS / některé ovladače Windows posílají nativní gesto)
        if event.type() == QEvent.Type.NativeGesture and getattr(self, "zoom", None) is not None:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.zoom.gesture_zoom(event.value(), event.position())
                return True
        return super().event(event)

    def wheelEvent(self, event):
        if not self.is_video_shown() and self.zoom.wheel(event):
            event.accept()
            return
        event.ignore()      # žádné posouvání scény kolečkem

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.zoom.press(event.position()):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.zoom.move(event.position()):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.zoom.release(event.position()):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        frame_meter.tick("viewer")              # Fáze 0: doba snímku prohlížeče (Ctrl+Shift+F)

    # obraz
    def current_pixmap(self):
        return self._pixmap

    def displayed_pixmap(self):
        """Co se skutečně kreslí: snímek zmenšený na plochu (v pixelech zařízení)."""
        return self.photo_item.pixmap()

    def content_key(self):
        return self._content_rev

    def _bump(self):
        self._content_rev += 1
        self.content_changed.emit()

    def is_fading(self):
        return self._fade.state() == QVariantAnimation.State.Running

    def set_image(self, pixmap, fade_ms=0):
        if pixmap is None or pixmap.isNull():
            return
        self._scrub_frames = False                  # nový obraz nahrazuje i náhled z pásu
        self.photo_item.setScale(1.0)
        self._fade.stop()
        if fade_ms > 0 and not self._pixmap.isNull() and self.photo_item.isVisible():
            self._sources[self.back_item] = self._sources[self.photo_item]
            self.back_item.setPixmap(self.photo_item.pixmap())
            self.back_item.setPos(self.photo_item.pos())
            self.back_item.setOpacity(1.0)
            self.back_item.setVisible(True)
            self.photo_item.setOpacity(0.0)
            self._fade.setDuration(int(fade_ms))
            self._fade.start()
        else:
            self._on_fade_done()
            self.photo_item.setOpacity(1.0)
        self._pixmap = pixmap
        self._sources[self.photo_item] = pixmap
        self._fit(self.photo_item)
        self.zoom.set_base(pixmap)           # přiblížení pokračuje i po výměně náhledu za originál
        self._bump()

    # ── Patch 14: snímky náhledového pásu při tažení časové osy ──
    def show_scrub_frame(self, image):
        """Malý snímek pásu (480 px) přes celou plochu – zvětšení při kreslení (setScale),
        žádné přepočítávání zdroje, prolínání ani přepočet skla HUD. Původní obraz (plakát)
        zůstává uložen a end_scrub_frames() ho vrátí."""
        pm = QPixmap.fromImage(image) if isinstance(image, QImage) else image
        if pm is None or pm.isNull():
            return False
        vw, vh = self.viewport().width(), self.viewport().height()
        if vw <= 0 or vh <= 0:
            return False
        self._fade.stop()
        self.back_item.setVisible(False)
        scale = min(vw / pm.width(), vh / pm.height())
        self.photo_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.photo_item.setPixmap(pm)
        self.photo_item.setScale(scale)
        self.photo_item.setPos((vw - pm.width() * scale) / 2, (vh - pm.height() * scale) / 2)
        self.photo_item.setOpacity(1.0)
        self._scrub_frames = True
        if self._video_on:
            self.show_video(False)                  # fotka (pás) nad videem, dokud nepřijde přesný snímek
        else:
            self.photo_item.setVisible(True)
        return True

    def show_edit_frame(self, image):
        """Pillar 2: a Quick Edits frame (drag proxy or full display size) fitted at paint time
        with setScale – no smooth re-fit of the source per slider step. set_image() is the rest
        state (the cropped result once the edit session ends)."""
        pm = QPixmap.fromImage(image) if isinstance(image, QImage) else image
        if pm is None or pm.isNull():
            return False
        vw, vh = self.viewport().width(), self.viewport().height()
        if vw <= 0 or vh <= 0:
            return False
        self._fade.stop()
        self.back_item.setVisible(False)
        pm.setDevicePixelRatio(1.0)
        scale = min(vw / pm.width(), vh / pm.height())
        self.photo_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.photo_item.setPixmap(pm)
        self.photo_item.setScale(scale)
        self.photo_item.setPos((vw - pm.width() * scale) / 2, (vh - pm.height() * scale) / 2)
        self.photo_item.setOpacity(1.0)
        self.photo_item.setVisible(True)
        return True

    def end_scrub_frames(self):
        """Konec tažení: vrátit původní obraz (plakát / fotku) v plném rozlišení."""
        if getattr(self, "_scrub_frames", False):
            self._scrub_frames = False
            src = self._sources.get(self.photo_item)
            if src is None or src.isNull():          # video bez plakátu: nic starého nenechat
                self.photo_item.setPixmap(QPixmap())
                self.photo_item.setScale(1.0)
            else:
                self._fit(self.photo_item)
            self._bump()

    def is_showing_scrub_frame(self):
        return getattr(self, "_scrub_frames", False)

    def clear_image(self):
        self._fade.stop()
        self.zoom.set_photo()
        self._scrub_frames = False
        self.photo_item.setScale(1.0)
        self._pixmap = QPixmap()
        self._sources[self.photo_item] = QPixmap()
        self.photo_item.setPixmap(QPixmap())
        self.back_item.setVisible(False)
        self._bump()

    def _on_fade_step(self, t):
        t = float(t)
        self.photo_item.setOpacity(t)
        self.back_item.setOpacity(1.0 - t)

    def _on_fade_done(self):
        self.photo_item.setOpacity(1.0)
        self.back_item.setVisible(False)
        self.back_item.setPixmap(QPixmap())
        self._sources[self.back_item] = QPixmap()
        if self._fade.state() != QVariantAnimation.State.Running:
            self._content_rev += 1          # konec prolínání = jiný obraz pod sklem

    # video
    def show_video(self, on: bool):
        """True = video nad fotkou (po prvním snímku), False = statická fotka / plakát."""
        on = bool(on)
        changed = on != self._video_on
        if on and self.zoom.is_zoomed():
            self.zoom.reset(animate=False)           # video (Live Photo) hraje vždy celé
        self._video_on = on
        if self.video_surface is not None:
            self.video_surface.setGeometry(self.rect())
            # jen prohodit pořadí vrstev (HUD – pozdější děti plátna – zůstává nahoře)
            if on:
                self.viewport().stackUnder(self.video_surface)
            else:
                self.video_surface.stackUnder(self.viewport())
        else:
            self.video_item.setVisible(on)
        self.photo_item.setVisible(not on)
        if on:
            self._fade.stop()
            self._on_fade_done()
        if changed:
            self._bump()

    def is_video_shown(self):
        return self._video_on

    @property
    def video_output(self):
        """Výstup aktivního přehrávače (QML VideoOutput / záložně QGraphicsVideoItem)."""
        return self.video_outputs[self._active_output] if self.video_outputs else None

    def set_active_output(self, index: int):
        """Patch 11: zobrazí výstup `index`, ostatní (předem načtené video) schová.
        Skrytý VideoOutput snímky dál přijímá, takže po přepnutí je obraz hned na místě."""
        if not 0 <= index < len(self.video_outputs) or index == self._active_output:
            return
        self._active_output = index
        if self.video_backend == "quick":
            for i, out in enumerate(self.video_outputs):
                out.setProperty("visible", i == index)

    def mouse_surfaces(self):
        """Widgety, nad kterými se hýbe myš (HUD je sleduje kvůli automatickému skrývání)."""
        return [w for w in (self.viewport(), self.video_surface) if w is not None]

    def eventFilter(self, obj, event):
        # Qt volá filtr už během konstrukce (viewport) – atribut ještě nemusí existovat;
        # výjimka ve virtuální metodě by v PyQt6 proces rovnou ukončila (qFatal)
        surface = getattr(self, "video_surface", None)
        if surface is not None and obj is surface and event.type() == QEvent.Type.MouseButtonDblClick:
            if event.button() == Qt.MouseButton.LeftButton:
                self.double_clicked.emit()
                return True
        return super().eventFilter(obj, event)

    # rozvržení
    def _fit(self, item, mode=Qt.TransformationMode.SmoothTransformation):
        """Zdroj se zmenší JEDNOU na velikost plochy (s ohledem na DPI) – kreslení je pak
        prosté vykreslení bez škálování, i u 12 Mpx HEIC."""
        src = self._sources.get(item)
        vw, vh = self.viewport().width(), self.viewport().height()
        if src is None or src.isNull() or vw <= 0 or vh <= 0:
            return
        dpr = self.devicePixelRatioF()
        # 3K sharpness: fit in DEVICE pixels and put the top-left on a whole device pixel. The
        # pixmap is then copied 1:1. Before, at 125 % / 150 % the logical centring (x.5) landed
        # on a fractional device pixel, and the smooth item resampled the whole photo once more
        # (a second resample, a soft image).
        dw, dh = max(1, round(vw * dpr)), max(1, round(vh * dpr))
        size = src.deviceIndependentSize()
        pw, ph = max(1.0, size.width()), max(1.0, size.height())
        scale = min(dw / pw, dh / ph)
        target = QSize(max(1, round(pw * scale)), max(1, round(ph * scale)))
        if src.size() == target:
            shown = QPixmap(src)
        else:
            shown = src.scaled(target, Qt.AspectRatioMode.IgnoreAspectRatio, mode)
        shown.setDevicePixelRatio(dpr)
        item.setScale(1.0)
        item.setPixmap(shown)
        item.setPos(((dw - target.width()) // 2) / dpr, ((dh - target.height()) // 2) / dpr)

    def _refit_smooth(self):
        self._fit(self.photo_item)
        if self.back_item.isVisible():
            self._fit(self.back_item)
        self._bump()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        vw, vh = self.viewport().width(), self.viewport().height()
        self.setSceneRect(QRectF(0, 0, vw, vh))
        if self.video_surface is not None:
            self.video_surface.setGeometry(self.rect())
        else:
            self.video_item.setPos(0, 0)
            self.video_item.setSize(QSizeF(vw, vh))
        self.zoom.resized()
        if getattr(self, "zoom_badge", None) is not None:
            self.zoom_badge.place()
        # během tažení okna rychlé zmenšení, vyhlazené až po zastavení
        self._fit(self.photo_item, Qt.TransformationMode.FastTransformation)
        if self.back_item.isVisible():
            self._fit(self.back_item, Qt.TransformationMode.FastTransformation)
        self._refit_timer.start()

    def snapshot(self, rect, shrink=1):
        """Výřez scény (souřadnice viewportu) vykreslený rovnou zmenšený 1:shrink –
        nezávislé na typu viewportu (OpenGL i software) a bez widgetů HUD nad ním."""
        w, h = max(1, rect.width() // shrink), max(1, rect.height() // shrink)
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor("#000000"))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.scene().render(p, QRectF(0, 0, w, h), QRectF(rect))
        p.end()
        return img

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.zoom.enabled and not self._video_on and not self.is_showing_scrub_frame():
                self.zoom.toggle_one_to_one(event.position())     # fotka: dvojklik = 1:1 a zpět
            else:
                self.double_clicked.emit()                        # video: celá obrazovka
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ZoomBadge(QLabel):
    """Krátce ukázané procento přiblížení (100 % = pixel originálu na pixel displeje)."""

    def __init__(self, canvas):
        super().__init__(canvas)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("QLabel { background: rgba(28, 28, 30, 0.78); color: #FFFFFF; border-radius: 13px;"
                           " padding: 4px 12px; font-size: 12px; font-weight: 600; }")
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(900)
        self._timer.timeout.connect(self.hide)

    def flash(self, percent):
        self.setText(f"{percent:.0f} %")
        self.adjustSize()
        self.place()
        self.show()
        self.raise_()
        self._timer.start()

    def place(self):
        p = self.parentWidget()
        if p is not None:
            self.move((p.width() - self.width()) // 2, 18)



# ── Ikony ─────────────────────────────────────────────────────────────────────

def _icon_path(name, r):
    """Vektorové ikony v duchu SF Symbols (bez obrázkových souborů a emoji)."""
    p = QPainterPath()
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    cx, cy = r.center().x(), r.center().y()
    if name == "play":
        p.moveTo(x + w * 0.30, y + h * 0.18)
        p.lineTo(x + w * 0.84, cy)
        p.lineTo(x + w * 0.30, y + h * 0.82)
        p.closeSubpath()
    elif name == "pause":
        p.addRoundedRect(QRectF(x + w * 0.24, y + h * 0.18, w * 0.17, h * 0.64), 2, 2)
        p.addRoundedRect(QRectF(x + w * 0.59, y + h * 0.18, w * 0.17, h * 0.64), 2, 2)
    elif name in ("volume", "mute"):
        p.moveTo(x + w * 0.12, y + h * 0.38)
        p.lineTo(x + w * 0.30, y + h * 0.38)
        p.lineTo(x + w * 0.52, y + h * 0.18)
        p.lineTo(x + w * 0.52, y + h * 0.82)
        p.lineTo(x + w * 0.30, y + h * 0.62)
        p.lineTo(x + w * 0.12, y + h * 0.62)
        p.closeSubpath()
    elif name == "heart":
        p.moveTo(cx, y + h * 0.84)
        p.cubicTo(x - w * 0.05, y + h * 0.45, x + w * 0.22, y + h * 0.02, cx, y + h * 0.30)
        p.cubicTo(x + w * 0.78, y + h * 0.02, x + w * 1.05, y + h * 0.45, cx, y + h * 0.84)
    elif name == "prev":
        p.moveTo(x + w * 0.62, y + h * 0.18)
        p.lineTo(x + w * 0.32, cy)
        p.lineTo(x + w * 0.62, y + h * 0.82)
    elif name == "next":
        p.moveTo(x + w * 0.38, y + h * 0.18)
        p.lineTo(x + w * 0.68, cy)
        p.lineTo(x + w * 0.38, y + h * 0.82)
    return p


class HudIconButton(QPushButton):
    """Kulaté tlačítko HUD s vektorovou ikonou; checked = modře podsvícené."""

    def __init__(self, icon, tooltip="", size=34, parent=None):
        super().__init__(parent)
        self._icon = icon
        self.setFixedSize(size, size)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or icon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFlat(True)
        self.setStyleSheet("QPushButton { background: transparent; border: none; }")

    def icon_name(self):
        return self._icon

    def set_icon(self, name):
        if name != self._icon:
            self._icon = name
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(10, 132, 255, 220))
            p.drawEllipse(r)
        elif self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 50))
            p.drawEllipse(r)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 30))
            p.drawEllipse(r)
        color = QColor("#FFFFFF") if self.isEnabled() else QColor(255, 255, 255, 90)
        s = min(r.width(), r.height()) * 0.50
        ir = QRectF(r.center().x() - s / 2, r.center().y() - s / 2, s, s)
        self._paint_icon(p, self._icon, ir, color)

    @staticmethod
    def _paint_icon(p, name, r, color):
        stroke = max(1.6, r.width() * 0.11)
        pen = QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        x, y, w, h = r.x(), r.y(), r.width(), r.height()
        if name in ("play", "pause", "heart_fill"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#FF375F") if name == "heart_fill" else color)
            p.drawPath(_icon_path("heart" if name == "heart_fill" else name, r))
            return
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if name in ("heart", "prev", "next"):
            p.drawPath(_icon_path(name, r))
        elif name in ("volume", "mute"):
            p.setBrush(color)
            p.drawPath(_icon_path(name, r))
            p.setBrush(Qt.BrushStyle.NoBrush)
            if name == "mute":
                p.drawLine(QPointF(x + w * 0.66, y + h * 0.36), QPointF(x + w * 0.92, y + h * 0.64))
                p.drawLine(QPointF(x + w * 0.92, y + h * 0.36), QPointF(x + w * 0.66, y + h * 0.64))
            else:
                p.drawArc(QRectF(x + w * 0.40, y + h * 0.30, w * 0.34, h * 0.40), -55 * 16, 110 * 16)
                p.drawArc(QRectF(x + w * 0.36, y + h * 0.14, w * 0.58, h * 0.72), -55 * 16, 110 * 16)
        elif name in ("fullscreen", "fullscreen_exit"):
            k = w * 0.30
            corners = [(x, y, 1, 1), (x + w, y, -1, 1), (x, y + h, 1, -1), (x + w, y + h, -1, -1)]
            for cx, cy, dx, dy in corners:
                if name == "fullscreen":
                    p.drawLine(QPointF(cx, cy), QPointF(cx + dx * k, cy))
                    p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * k))
                else:
                    ix, iy = cx + dx * k, cy + dy * k
                    p.drawLine(QPointF(ix, iy), QPointF(ix - dx * k, iy))
                    p.drawLine(QPointF(ix, iy), QPointF(ix, iy - dy * k))
        elif name == "info":
            p.drawEllipse(r)
            p.drawLine(QPointF(r.center().x(), y + h * 0.45), QPointF(r.center().x(), y + h * 0.74))
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            d = stroke * 1.1
            p.drawEllipse(QRectF(r.center().x() - d / 2, y + h * 0.24, d, d))
        elif name == "live":
            c = r.center()
            for frac in (0.16, 0.32):
                p.drawEllipse(c, w * frac, h * frac)
            p.setPen(QPen(color, stroke * 0.8, Qt.PenStyle.DotLine, Qt.PenCapStyle.RoundCap))
            p.drawEllipse(c, w * 0.48, h * 0.48)
        elif name == "slideshow":
            p.drawRoundedRect(QRectF(x, y + h * 0.12, w, h * 0.66), 3, 3)
            tri = QPainterPath()
            tri.moveTo(x + w * 0.42, y + h * 0.30)
            tri.lineTo(x + w * 0.64, y + h * 0.45)
            tri.lineTo(x + w * 0.42, y + h * 0.60)
            tri.closeSubpath()
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(tri)
            p.setPen(pen)
            p.drawLine(QPointF(x + w * 0.30, y + h * 0.92), QPointF(x + w * 0.70, y + h * 0.92))
        elif name in ("skip_back", "skip_fwd"):
            # kruhová šipka s mezerou nahoře + „10“ uprostřed (jako ±10 s v QuickTime)
            fwd = name == "skip_fwd"
            ring = QRectF(x - w * 0.08, y - h * 0.04, w * 1.16, h * 1.16)
            p.drawArc(ring, 115 * 16, 310 * 16) if not fwd else p.drawArc(ring, 65 * 16, -310 * 16)
            top = QPointF(ring.center().x() + (w * 0.22 if fwd else -w * 0.22), ring.top() + h * 0.02)
            d = 1 if fwd else -1
            p.drawLine(top, QPointF(top.x() - d * w * 0.20, top.y() - h * 0.14))
            p.drawLine(top, QPointF(top.x() - d * w * 0.20, top.y() + h * 0.14))
            f = p.font()
            f.setPixelSize(max(7, int(h * 0.48)))
            f.setBold(True)
            p.setFont(f)
            p.drawText(ring, Qt.AlignmentFlag.AlignCenter, "10")
        elif name == "back":
            p.drawPath(_icon_path("prev", r))
        else:
            # every other name (adjust, share, …) from the shared SVG set – without this fallback
            # a new HUD button painted nothing at all (Quick Edits / Export looked like empty slots)
            import icons
            if name in icons.names():
                icons.paint(p, name, r, color)


# ── Skleněný panel ────────────────────────────────────────────────────────────

class GlassPanel(QWidget):
    """Plovoucí kapsle s matným sklem: rozostřený výřez toho, co je pod ní (backdrop),
    tmavý tón, jemný světlý okraj a měkký stín. Zobrazení / skrytí plynule prolíná."""

    SHADOW = 10

    def __init__(self, parent=None, backdrop=None, radius=18):
        super().__init__(parent)
        self.backdrop = backdrop          # MediaCanvas (snapshot scény) nebo libovolný widget
        self.radius = radius
        self._blur = QPixmap()
        self._blur_key = None
        self.frosted = True
        self.refreshes = 0                # kolikrát se sklo skutečně přepočítalo (měření / testy)
        self._shown = True
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setAutoFillBackground(False)
        self.setMouseTracking(True)
        s = self.SHADOW
        self.setContentsMargins(s, s, s, s)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity)
        self._anim = VsyncPropertyAnimation(self._opacity, b"opacity", self)
        self._anim.setDuration(HUD_FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_done)

    def glass_rect(self):
        s = self.SHADOW
        return QRectF(self.rect()).adjusted(s, s, -s, -s)

    def is_shown(self):
        """Logický stav (během zmizení už False), na rozdíl od isVisible()."""
        return self._shown and self.isVisible()

    def fade_in(self):
        if self._shown and self.isVisible() and self._anim.state() != QPropertyAnimation.State.Running:
            self.raise_()        # už je vidět – nepřekreslovat sklo při každém pohybu myši
            return
        self._shown = True
        if not self.isVisible():
            self._opacity.setOpacity(0.0)
            self.show()
        self.raise_()
        self.refresh_backdrop()
        self._animate_to(1.0)

    def fade_out(self):
        if not self.isVisible():
            self._shown = False
            return
        self._shown = False
        self._animate_to(0.0)

    def hide_now(self):
        self._shown = False
        self._anim.stop()
        self.hide()

    def _animate_to(self, value):
        self._anim.stop()
        self._anim.setStartValue(self._opacity.opacity())
        self._anim.setEndValue(value)
        self._anim.start()

    def _on_anim_done(self):
        if not self._shown:
            self.hide()

    def set_frosted(self, on: bool):
        """False = čistě průsvitná kapsle bez snímkování pozadí (během přehrávání videa)."""
        on = bool(on)
        if on == self.frosted:
            return
        self.frosted = on
        if not on:
            self._blur = QPixmap()
            self._blur_key = None
            self.update()

    def refresh_backdrop(self):
        """Rozostřený výřez obrazu pod panelem. Scéna se vykreslí rovnou zmenšená (1:14),
        pak se zvětší s vyhlazením = levné rozmazání; výsledek se drží v cache, dokud se
        nezmění obraz ani poloha panelu (pohyb myši ho nepřepočítává)."""
        bd = self.backdrop
        if not self.frosted or bd is None or not self.isVisible() or not bd.isVisible():
            return
        g = self.glass_rect().toRect()
        if g.width() < 4 or g.height() < 4:
            return
        source = bd.viewport() if isinstance(bd, QGraphicsView) else bd
        top_left = source.mapFromGlobal(self.mapToGlobal(g.topLeft()))
        area = QRect(top_left, g.size()).intersected(source.rect())
        if area.isEmpty():
            self._blur = QPixmap()
            return
        key = (bd.content_key() if hasattr(bd, "content_key") else None,
               area.x(), area.y(), area.width(), area.height())
        if key[0] is not None and key == self._blur_key and not self._blur.isNull():
            return
        if hasattr(bd, "snapshot"):
            small = bd.snapshot(area, shrink=14)
        else:
            img = source.grab(area).toImage()
            small = img.scaled(max(1, img.width() // 14), max(1, img.height() // 14),
                               Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        w, h = area.width(), area.height()
        mid = small.scaled(max(1, w // 7), max(1, h // 7), Qt.AspectRatioMode.IgnoreAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self._blur = QPixmap.fromImage(mid.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))
        self._blur_offset = area.topLeft() - top_left
        self._blur_key = key
        self.refreshes += 1
        self.update()

    def has_backdrop(self):
        return not self._blur.isNull()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = self.glass_rect()
        # měkký stín pod sklem
        for i in range(self.SHADOW, 0, -2):
            alpha = int(34 * (1 - i / (self.SHADOW + 1)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(g.adjusted(-i, -i + 3, i, i + 3), self.radius + i, self.radius + i)
        path = QPainterPath()
        path.addRoundedRect(g, self.radius, self.radius)
        p.save()
        p.setClipPath(path)
        if not self._blur.isNull():
            off = getattr(self, "_blur_offset", QPoint(0, 0))
            p.drawPixmap(g.topLeft() + QPointF(off), self._blur)
            p.fillPath(path, QColor(22, 22, 24, 150))
        else:
            # bez skla (video): tmavá průsvitná kapsle – obraz prosvítá, nic se nepřepočítává
            p.fillPath(path, QColor(24, 24, 26, 178))
        sheen = QLinearGradient(g.topLeft(), g.bottomLeft())
        sheen.setColorAt(0.0, QColor(255, 255, 255, 22))
        sheen.setColorAt(0.5, QColor(255, 255, 255, 0))
        p.fillPath(path, sheen)
        p.restore()
        p.setPen(QPen(QColor(255, 255, 255, 38), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(g.adjusted(0.5, 0.5, -0.5, -0.5), self.radius, self.radius)


# ── Automatické skrývání ──────────────────────────────────────────────────────

class HudAutoHider(QObject):
    """Ovládání + kurzor zmizí po 2 s bez pohybu myši; zůstanou, dokud je myš nad panelem,
    táhne se posuvník nebo je otevřené menu."""
    visibility_changed = pyqtSignal(bool)

    def __init__(self, surface, panels_fn, delay_ms=HUD_HIDE_DELAY_MS, parent=None):
        super().__init__(parent)
        # plochy, kde se hýbe myš a skrývá kurzor (viewport fotky + vrstva videa)
        self.surfaces = list(surface) if isinstance(surface, (list, tuple)) else [surface]
        self.surface = self.surfaces[0]
        self.panels_fn = panels_fn        # -> panely, které mají být v tomto stavu vidět
        self.all_panels = []
        self.visible = True
        self.enabled = True
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay_ms)
        self.timer.timeout.connect(self._try_hide)
        for s in self.surfaces:
            s.setMouseTracking(True)
            s.installEventFilter(self)

    def _set_cursor(self, hidden):
        for s in self.surfaces:
            if hidden:
                s.setCursor(Qt.CursorShape.BlankCursor)
            else:
                s.unsetCursor()

    def track(self, *panels):
        for panel in panels:
            if panel not in self.all_panels:
                self.all_panels.append(panel)
                panel.installEventFilter(self)
                for child in panel.findChildren(QWidget):
                    child.setMouseTracking(True)
                    child.installEventFilter(self)

    def poke(self):
        """Pohyb myši: ukázat ovládání a kurzor, odpočet znovu od nuly."""
        if not self.enabled:
            return
        wanted = list(self.panels_fn())
        for panel in self.all_panels:
            if panel in wanted:
                panel.fade_in()
            else:
                panel.hide_now()
        self._set_cursor(False)
        if not self.visible:
            self.visible = True
            self.visibility_changed.emit(True)
        self.timer.start()

    def sync(self):
        """Změna položky (foto <-> video): přepnout sadu panelů, ale neprodlužovat zobrazení."""
        if self.visible:
            wanted = list(self.panels_fn())
            for panel in self.all_panels:
                if panel in wanted:
                    if not panel.is_shown():
                        panel.fade_in()
                elif panel.isVisible():
                    panel.hide_now()

    def is_interacting(self):
        for panel in self.all_panels:
            if not panel.isVisible():
                continue
            if panel.underMouse() or panel.rect().contains(panel.mapFromGlobal(QCursor.pos())):
                return True
            if any(s.isSliderDown() for s in panel.findChildren(QSlider)):
                return True
        return QApplication.activePopupWidget() is not None

    def _try_hide(self):
        if not self.enabled:
            return
        if self.is_interacting():
            self.timer.start()
            return
        self.hide_all()

    def hide_all(self):
        for panel in self.all_panels:
            panel.fade_out()
        self._set_cursor(True)
        if self.visible:
            self.visible = False
            self.visibility_changed.emit(False)

    def stop(self):
        """Prohlížeč odchází: nic neskrývat na pozadí a vrátit kurzor."""
        self.timer.stop()
        self._set_cursor(False)

    def eventFilter(self, obj, event):
        t = event.type()
        if t in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
                 QEvent.Type.Enter):
            self.poke()
        return False


# ── Prezentace ────────────────────────────────────────────────────────────────

PIL_ONLY_FORMATS = {".heic", ".heif", ".dng"}     # Qt je bez pluginů nenačte


def _fits_preview(max_size):
    from preview_cache import PREVIEW_EDGE
    return max_size is not None and max(max_size.width(), max_size.height()) <= PREVIEW_EDGE


def _read_preview(path, max_size):
    from preview_cache import get_preview
    p = get_preview(path)
    if not p:
        return QImage()
    img = QImageReader(p).read()
    if not img.isNull() and max_size is not None:
        import imaging
        img = imaging.fit_qimage(img, max_size.width(), max_size.height())
    return img


def load_image_file(path, max_size=None, use_preview=True):
    """Fotka jako QImage – přes nativní pipeline (imaging): bezpečné mimo GUI vlákno.

    use_preview: pokud cílová velikost nepřesahuje náhled a platný náhled existuje, načte se ten
    (~25 ms místo ~300 ms u HEIC; náhled je už v kontraktu displeje). Jinak originál: Pillow
    (+ pillow_heif) s vloženým ICC a orientací z EXIFu, Lanczos3 do max_size (menší se
    nezvětšuje), JEDEN převod barev do kontraktu displeje. Bez ostření – prohlížeč je věrné okno.
    Formáty, které Pillow neotevře, čte QImageReader (+ qimage_to_output). Prázdný QImage při chybě."""
    if not path or not os.path.exists(path):
        return QImage()
    if use_preview and _fits_preview(max_size):
        img = _read_preview(path, max_size)
        if not img.isNull():
            return img
    import color_manage
    import imaging
    if color_manage.is_cache_file(path):           # soubor z cache je už v kontraktu displeje
        img = QImageReader(path).read()
        return imaging.fit_qimage(img, max_size.width(), max_size.height())             if max_size is not None and not img.isNull() else img
    try:
        size_fn = None
        need = None
        if max_size is not None:
            bw, bh = max_size.width(), max_size.height()
            size_fn = lambda w, h: imaging.fit_box(w, h, bw, bh)
            need = max(bw, bh)
        return imaging.pil_to_qimage(imaging.render_still(path, size_fn, need_long=need))
    except Exception:
        pass
    reader = QImageReader(path)
    reader.setAutoTransform(True)          # orientace z EXIFu
    img = reader.read()
    if img.isNull():
        return img
    img = color_manage.qimage_to_output(img)
    if max_size is not None:
        img = imaging.fit_qimage(img, max_size.width(), max_size.height())
    return img


AV_LOG_ERROR = 16
_ffmpeg_quiet = None


def quiet_ffmpeg_logs(level=AV_LOG_ERROR):
    """Varování FFmpeg (např. „Multiple Dolby Vision RPUs found in one AU“ u každého snímku
    4K DV videa) jdou výchozím loggerem FFmpeg rovnou na konzoli – Qt ho přebírá jen s
    QT_FFMPEG_DEBUG. Stovky řádků za sekundu brzdí dekódování i GUI. Ponechá jen chyby.
    Nastavuje se v avutil, který používá Qt (stejný soubor = stejný modul v procesu)."""
    global _ffmpeg_quiet
    if _ffmpeg_quiet is not None or os.environ.get("QT_FFMPEG_DEBUG"):
        return bool(_ffmpeg_quiet)
    _ffmpeg_quiet = False
    try:
        import ctypes
        import glob
        import PyQt6
        dlls = glob.glob(os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "bin", "avutil-*.dll"))
        if dlls:
            ctypes.CDLL(dlls[0]).av_log_set_level(int(level))
            _ffmpeg_quiet = True
    except Exception as e:
        print(f"[Video] úroveň logů FFmpeg nelze nastavit: {e}")
    return _ffmpeg_quiet


QT_FFMPEG_NOISE = ("QObject::disconnect: wildcard call disconnects from destroyed signal of QFFmpeg::",)
_qt_filter_installed = False


def quiet_qt_ffmpeg_noise():
    """Interní hlášení Qt FFmpeg při výměně dekodéru/rendereru (neškodné, Qt ho vypisuje
    při každém přesunu) – zahodit přesně tuto zprávu, vše ostatní předat beze změny."""
    global _qt_filter_installed
    if _qt_filter_installed:
        return
    import sys as _sys
    from PyQt6.QtCore import qInstallMessageHandler
    previous = None

    def handler(mode, context, message):
        if message.startswith(QT_FFMPEG_NOISE):
            return
        if previous is not None:
            previous(mode, context, message)
        elif _sys.stderr is not None:          # pythonw / shortcut: no console, stderr is None
            _sys.stderr.write(message + "\n")

    previous = qInstallMessageHandler(handler)
    _qt_filter_installed = True


SCRUB_MAX_HZ = 16            # nejvýš tolik přesunů za sekundu při tažení časové osy
SCRUB_SEEK_TIMEOUT_MS = 300  # dekodér neohlásil snímek -> další přesun i tak povolit
SCRUB_REST_MS = 140          # kurzor se zastavil -> přesný 4K snímek místo náhledu z pásu


class VideoScrubber(QObject):
    """Plynulé tažení časové osy videa.

    Každé setPosition() znamená u 4K HEVC dekódování od klíčového snímku až k cíli. Přesuny
    přímo z sliderMoved (desítky za sekundu) se řadily za sebe a dekodér zaostával. Proto:
      * během tažení přehrávání stojí a zvuk je ztlumený (žádné zaseknuté audio buffery),
      * přesun se pošle nejvýš SCRUB_MAX_HZ krát za sekundu A jen když dekodér dokončil
        předchozí (ohlásil snímek) – jinak se cíl jen přepíše, vždy vyhrává nejnovější,
      * po puštění jeden přesný přesun na konečnou pozici a obnovení přehrávání / zvuku.
    Qt neumožňuje zvolit „rychlý přesun na klíčový snímek“; omezení souběhu má stejný účinek
    – dekodér nikdy nedekóduje cíle, které uživatel mezitím přejel.

    Patch 14 – náhledový pás (preview_fn): pokud pro pozici existuje snímek pásu, ukáže se
    hned (žádný přesun dekodéru) a přesný snímek se vyžádá, až když se kurzor zastaví
    (SCRUB_REST_MS) a po puštění. Bez pásu (ještě se tvoří) platí omezené živé přesuny výše.
    show_live říká, zda se má snímek z dekodéru ukázat (zastaralý přesun nesmí přebít náhled)."""

    def __init__(self, player_fn, audio_fn, parent=None, max_hz=SCRUB_MAX_HZ, clock=None,
                 preview_fn=None, rest_ms=SCRUB_REST_MS):
        super().__init__(parent)
        import time as _time
        self.player_fn, self.audio_fn = player_fn, audio_fn
        self.preview_fn = preview_fn
        self.clock = clock or _time.monotonic
        self.active = False
        self.seeks = 0                    # počet odeslaných přesunů (ladění / testy)
        self.previews = 0                 # počet snímků z pásu (ladění / testy)
        self.show_live = True
        self._target = None
        self._in_flight = False
        self._sent_at = 0.0
        self._last_sent = None
        self._preview_since_seek = False
        self._was_playing = False
        self._was_muted = False
        self.timer = QTimer(self)
        self.timer.setInterval(max(1, int(1000 / max_hz)))
        self.timer.timeout.connect(self._pump)
        self.rest_timer = QTimer(self)
        self.rest_timer.setSingleShot(True)
        self.rest_timer.setInterval(int(rest_ms))
        self.rest_timer.timeout.connect(self._on_rest)

    def begin(self):
        if self.active:
            return
        from PyQt6.QtMultimedia import QMediaPlayer
        player, audio = self.player_fn(), self.audio_fn()
        self._was_playing = player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if self._was_playing:
            player.pause()
        self._was_muted = audio.isMuted()
        audio.setMuted(True)
        self.active = True
        self._in_flight = False
        self._last_sent = None
        self.timer.start()

    def move(self, pos):
        """Nová cílová pozice z tažení. Se snímkem pásu: ukázat hned, přesně až po zastavení.
        Bez pásu: první přesun hned (okamžitá odezva na stisk), další omezeně."""
        if not self.active:
            self.begin()
        self._target = int(pos)
        if self.preview_fn is not None and self.preview_fn(self._target):
            self.previews += 1
            self._preview_since_seek = True
            self.show_live = False             # rozpracovaný živý přesun nesmí přebít náhled
            self.rest_timer.start()            # (re)start: přesný snímek až kurzor zastaví
            return
        self.rest_timer.stop()
        self.show_live = True
        if not self._in_flight and self._last_sent is None:
            self._pump()

    def frame_ready(self):
        """Dekodér vykreslil snímek po přesunu – může se poslat další."""
        self._in_flight = False

    def _on_rest(self):
        """Kurzor stojí: přesný snímek na pozici náhledu (i kdyby dekodér ještě pracoval)."""
        if not self.active or self._target is None:
            return
        target, self._target = self._target, None
        self.show_live = True
        self._send(target)

    def _pump(self):
        if self._target is None or self.rest_timer.isActive():
            return                             # náhled z pásu čeká na zastavení kurzoru
        if self._in_flight and (self.clock() - self._sent_at) * 1000 < SCRUB_SEEK_TIMEOUT_MS:
            return                         # dekodér ještě pracuje – cíl jen čeká (přepisuje se)
        target, self._target = self._target, None
        if target == self._last_sent:
            return
        self._send(target)

    def _send(self, pos):
        self._in_flight = True
        self._sent_at = self.clock()
        self._last_sent = pos
        self._preview_since_seek = False
        self.seeks += 1
        self.player_fn().setPosition(pos)

    def cancel(self):
        """Odchod z položky uprostřed tažení: bez dalšího přesunu, jen vrátit zvuk."""
        if not self.active:
            return
        self.timer.stop()
        self.rest_timer.stop()
        self.active = False
        self.show_live = True
        self._target, self._last_sent, self._in_flight = None, None, False
        self._preview_since_seek = False
        self.audio_fn().setMuted(self._was_muted)

    def end(self, pos=None):
        """Puštění: přesný přesun na konečnou pozici, obnovení zvuku a přehrávání.
        Vrací True, když se poslal přesun (přijde nový snímek z dekodéru)."""
        if not self.active:
            return False
        self.timer.stop()
        self.rest_timer.stop()
        final = int(pos) if pos is not None else (self._target if self._target is not None else self._last_sent)
        self._target = None
        self.active = False
        self._in_flight = False
        self.show_live = True
        player = self.player_fn()
        # po náhledech z pásu je vidět malý snímek – přesný je potřeba i na stejné pozici
        sent = final is not None and (final != self._last_sent or self._preview_since_seek)
        if sent:
            self.seeks += 1
            player.setPosition(final)
        self._last_sent = None
        self._preview_since_seek = False
        self.audio_fn().setMuted(self._was_muted)
        if self._was_playing:
            player.play()
        return sent


class MediaWarmUp(QThread):
    """Jednorázové zahřátí FFmpeg / hardwarového dekodéru v pracovním vlákně.

    Inicializace (načtení knihoven, D3D11VA zařízení, dekodér HEVC) stojí ~0,3–0,45 s a část
    běží v GUI vlákně, ať se spustí odkudkoli – nelze ji odstranit, jen přesunout do klidu po
    startu aplikace. Změřeno: první video pak blokuje GUI ~90 ms místo ~365 ms."""
    WARM_MS = 1200

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self.frames = 0

    def run(self):
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput, QVideoSink
        player = QMediaPlayer()
        audio = QAudioOutput()
        audio.setMuted(True)
        player.setAudioOutput(audio)
        sink = QVideoSink()
        player.setVideoSink(sink)
        sink.videoFrameChanged.connect(self._on_frame)
        player.setSource(QUrl.fromLocalFile(self.path))
        player.play()
        loop = QEventLoop()
        QTimer.singleShot(self.WARM_MS, loop.quit)
        loop.exec()
        player.stop()
        player.setSource(QUrl())

    def _on_frame(self, frame):
        self.frames += 1


class _PipelineSignals(QObject):
    done = pyqtSignal(str, QImage)


class _PipelineJob(QRunnable):
    """Progresivně: platný náhled (~25 ms) hned, originál jen když cíl potřebuje víc než
    2048 px (fullscreen na HiDPI). Dekódovaný originál zároveň uloží chybějící náhled
    (cache se plní běžným prohlížením)."""

    def __init__(self, path, max_size, write_previews=True):
        super().__init__()
        self.path, self.max_size, self.write_previews = path, max_size, write_previews
        self.signals = _PipelineSignals()

    def run(self):
        from preview_cache import save_preview, get_preview, PREVIEW_EDGE
        preview = _read_preview(self.path, self.max_size)
        if not preview.isNull():
            self.signals.done.emit(self.path, preview)
            if _fits_preview(self.max_size) or max(preview.width(), preview.height()) < PREVIEW_EDGE - 1:
                return                         # náhled stačí (nebo je originál menší než náhled)
        img = load_image_file(self.path, self.max_size, use_preview=False)
        if self.write_previews and not img.isNull() and get_preview(self.path, touch=False) is None \
                and os.path.splitext(self.path)[1].lower() not in {".mov", ".mp4", ".m4v"}:
            long_edge = max(img.width(), img.height())
            # náhled jen z dekódu v plné velikosti náhledu, nebo z celého (menšího) originálu –
            # jinak by z 1600px dekódu (duplicity) vznikl podměrečný „2048px“ náhled
            full_original = self.max_size is None or (img.width() < self.max_size.width()
                                                      and img.height() < self.max_size.height())
            if long_edge >= PREVIEW_EDGE - 1 or full_original:
                try:
                    save_preview(self.path, img)
                except Exception:
                    pass
        self.signals.done.emit(self.path, img)


class ImagePipeline(QObject):
    """Dekódování fotek mimo GUI vlákno + LRU cache + přednačítání sousedů.

    HEIC 12 Mpx se dekóduje ~300 ms – v GUI vlákně to bylo zaseknutí při každé šipce v
    prohlížeči a 1s prodleva v kontrole duplicit. get() vrátí hotový snímek z cache,
    request()/prefetch() ho nechají připravit na pozadí a ohlásí signálem loaded."""
    loaded = pyqtSignal(str, QImage)

    def __init__(self, max_size_fn, capacity=8, threads=2, parent=None):
        super().__init__(parent)
        self.max_size_fn = max_size_fn          # -> QSize (např. obrazovka × DPI)
        self.capacity = capacity
        self._cache = OrderedDict()
        self._pending = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(threads)

    def get(self, path):
        img = self._cache.get(path)
        if img is not None:
            self._cache.move_to_end(path)
        return img

    def request(self, path):
        """Zajistí načtení; vrací hotový snímek z cache nebo None (pak přijde signál loaded)."""
        if not path:
            return None
        img = self.get(path)
        if img is not None:
            return img
        if path not in self._pending and os.path.exists(path):
            self._pending.add(path)
            job = _PipelineJob(path, self.max_size_fn())
            job.signals.done.connect(self._on_done)
            self.pool.start(job)
        return None

    def prefetch(self, paths):
        for p in paths:
            self.request(p)

    def is_pending(self, path):
        return path in self._pending

    def _on_done(self, path, image):
        self._pending.discard(path)
        if image.isNull():
            return
        self._cache[path] = image
        self._cache.move_to_end(path)
        while len(self._cache) > self.capacity:
            self._cache.popitem(last=False)
        self.loaded.emit(path, image)

    def wait(self, msecs=10000):
        self.pool.waitForDone(msecs)
        QApplication.processEvents()


def load_slide_image(record, max_size=None):
    """Snímek pro prezentaci jako QImage (bezpečné mimo GUI vlákno).
    Fotka / Live Photo = originál se správnou orientací, video = jeho náhled."""
    path, media_type, thumb = record[2], record[3], record[5]
    candidates = [thumb, path] if media_type == "video" else [path, thumb]
    for src in candidates:
        img = load_image_file(src, max_size)
        if not img.isNull():
            return img
    return QImage()


class _SlideSignals(QObject):
    ready = pyqtSignal(int, int, QImage)     # token, index, obrázek


class _SlideLoader(QRunnable):
    def __init__(self, token, index, record, max_size):
        super().__init__()
        self.token, self.index, self.record, self.max_size = token, index, record, max_size
        self.signals = _SlideSignals()

    def run(self):
        self.signals.ready.emit(self.token, self.index, load_slide_image(self.record, self.max_size))


class SlideshowView(QWidget):
    """Čistá prezentace přes celou obrazovku: černé pozadí, žádné ovládání, prolínání.
    Mezerník = pauza / pokračovat, šipky = ručně, Esc = konec (back_requested)."""
    back_requested = pyqtSignal()
    index_changed = pyqtSignal(int)

    def __init__(self, records, start_index=0, interval_ms=SLIDE_INTERVAL_MS,
                 fade_ms=SLIDE_FADE_MS, parent=None):
        super().__init__(parent)
        self.setProperty("omni_fixed_dark", True)   # prezentace je vždy na černé (theme.py)
        self.records = list(records)
        self.index = max(0, min(int(start_index), len(self.records) - 1)) if self.records else 0
        self.fade_ms = fade_ms
        self.paused = False
        self.running = False
        self._token = 0
        self._cache = {}          # index -> QPixmap (aktuální + sousední)
        self._loading = set()
        self.setObjectName("Slideshow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#Slideshow { background: #000000; }")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.BlankCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.canvas = MediaCanvas(self, with_video=False)     # prezentace ukazuje jen statické snímky
        self.canvas.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.canvas.viewport().setCursor(Qt.CursorShape.BlankCursor)
        lay.addWidget(self.canvas)

        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(lambda: self.advance(+1))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)

        # krátká značka pauzy (jediný prvek, který se kdy ukáže – sama zmizí)
        self.indicator = QLabel(self)
        self.indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.indicator.setFixedSize(84, 84)
        self.indicator.setStyleSheet("QLabel { background: rgba(28,28,30,190); color: #FFFFFF;"
                                     " border-radius: 42px; font-size: 30px; font-weight: 600; }")
        self.indicator.hide()
        self._indicator_timer = QTimer(self)
        self._indicator_timer.setSingleShot(True)
        self._indicator_timer.setInterval(900)
        self._indicator_timer.timeout.connect(self.indicator.hide)

    # řízení
    def start(self):
        if not self.records:
            self.back_requested.emit()
            return
        self.running = True
        self.show_index(self.index, fade=False)
        if not self.paused:
            self.timer.start()

    def stop(self):
        self.running = False
        self.timer.stop()
        self._token += 1            # pozdě doběhlá načtení se zahodí
        self.pool.clear()
        self._loading.clear()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.timer.stop()
        elif self.running:
            self.timer.start()
        self._flash("❚❚" if self.paused else "▶")

    def advance(self, step=1, manual=False):
        if not self.records:
            return
        self.show_index((self.index + step) % len(self.records), fade=True)
        if manual and self.running and not self.paused:
            self.timer.start()      # ruční posun = plný interval na nový snímek

    def show_index(self, i, fade=True):
        n = len(self.records)
        if n == 0:
            return
        self.index = i % n
        pix = self._cache.get(self.index)
        if pix is None:           # ještě nenačteno na pozadí -> načíst hned (výjimečně)
            pix = QPixmap.fromImage(load_slide_image(self.records[self.index], self._target_size()))
            self._cache[self.index] = pix
        self.canvas.set_image(pix, self.fade_ms if fade else 0)
        self.index_changed.emit(self.index)
        keep = {self.index, (self.index + 1) % n, (self.index - 1) % n}
        for k in list(self._cache):
            if k not in keep:
                del self._cache[k]
        self._prefetch((self.index + 1) % n)
        self._prefetch((self.index - 1) % n)

    def _target_size(self):
        screen = self.screen() or QApplication.primaryScreen()
        size = screen.size() if screen is not None else QSize(1920, 1080)
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        return QSize(int(size.width() * dpr), int(size.height() * dpr))

    def _prefetch(self, i):
        if i in self._cache or i in self._loading or not self.running:
            return
        self._loading.add(i)
        loader = _SlideLoader(self._token, i, self.records[i], self._target_size())
        loader.signals.ready.connect(self._on_loaded)
        self.pool.start(loader)

    def _on_loaded(self, token, i, image):
        self._loading.discard(i)
        if token != self._token or image.isNull():
            return
        self._cache[i] = QPixmap.fromImage(image)

    def _flash(self, text):
        self.indicator.setText(text)
        self.indicator.move((self.width() - self.indicator.width()) // 2,
                            (self.height() - self.indicator.height()) // 2)
        self.indicator.show()
        self.indicator.raise_()
        self._indicator_timer.start()

    # vstup
    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Space:
            self.toggle_pause()
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown):
            self.advance(+1, manual=True)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
            self.advance(-1, manual=True)
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_S):
            self.stop()
            self.back_requested.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def hideEvent(self, event):
        self.timer.stop()           # skrytá prezentace nesmí dál listovat
        super().hideEvent(event)
