"""Ozvěny – zobrazení (echoes.py hledá, tady se ukazuje).

EchoesPanel: mřížka karet „tehdy | teď“ (výpočet v EchoJob na pozadí, prázdný stav, když
knihovna žádný návrat nemá). ThenNowView: srovnání dvou snímků jednoho místa – snímek „teď“
celý, „tehdy“ přes něj vlevo od posuvné dělicí čáry. Jsou-li to stejné scény, srovná je
echoes.align_pair (ORB + RANSAC) – tažením čáry pak ubíhá čas, ne obraz.
"""
from i18n import tr
import os

from PyQt6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QTransform
from PyQt6.QtWidgets import QGridLayout, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import echoes
import motion
import tokens
from ui_controls import EmptyState, SegmentedControl, smooth_rect_path

CARD_HEIGHT = 230
CARD_MIN_W = 340
CARD_IMAGE_EDGE = 720
COMPARE_EDGE = 2048
INTRO_FROM = 0.86                # posuvník při otevření sjede zprava doprostřed (ukáže, že jde táhnout)


def _qimage_from(path, edge):
    if not path or not os.path.exists(path):
        return QImage()
    from viewer_hud import load_image_file
    return load_image_file(path, QSize(edge, edge))


def _cover(img, w, h):
    """Výplň (bez deformace) přesně na w × h, mírně nad středem jako karta vzpomínky."""
    if img.isNull() or w <= 0 or h <= 0:
        return QImage()
    s = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    return s.copy((s.width() - w) // 2, int((s.height() - h) * 0.42), w, h)


# ── Výpočet na pozadí ─────────────────────────────────────────────────────────

class _Signals(QObject):
    done = pyqtSignal(object)


class EchoJob(QRunnable):
    """find_echoes + zmenšené obrázky prvního a posledního roku každé ozvěny."""

    def __init__(self, connect):
        super().__init__()
        self.connect = connect
        self.signals = _Signals()

    def run(self):
        found, images = [], {}
        try:
            conn = self.connect()
            try:
                found = echoes.find_echoes(conn)
            finally:
                conn.close()
            for e in found:
                for it in (e["items"][0], e["items"][-1]):
                    if it["id"] not in images:
                        img = _qimage_from(it["thumb"], CARD_IMAGE_EDGE)
                        if img.isNull():
                            img = _qimage_from(it["path"], CARD_IMAGE_EDGE)
                        images[it["id"]] = img
        except Exception as e:
            print(f"[Echoes] {e}")
            found, images = [], {}
        try:
            self.signals.done.emit((found, images))
        except RuntimeError:
            pass


class PairJob(QRunnable):
    """Ostré obrázky dvojice + srovnávací transformace (tehdy -> teď) nebo None."""

    def __init__(self, then_item, now_item):
        super().__init__()
        self.items = (then_item, now_item)
        self.signals = _Signals()

    def run(self):
        imgs, mat = [], None
        try:
            for it in self.items:
                img = _qimage_from(it["path"], COMPARE_EDGE)
                if img.isNull():
                    img = _qimage_from(it["thumb"], COMPARE_EDGE)
                imgs.append(img.convertToFormat(QImage.Format.Format_RGB888) if not img.isNull() else img)
            if all(not i.isNull() for i in imgs):
                mat = echoes.align_pair(*(_to_array(i) for i in imgs))
        except Exception as e:
            print(f"[Echoes] srovnání: {e}")
        try:
            self.signals.done.emit((self.items, imgs, mat))
        except RuntimeError:
            pass


def _to_array(img):
    import numpy as np
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    a = np.frombuffer(ptr, dtype=np.uint8).reshape(img.height(), img.bytesPerLine())
    return a[:, :img.width() * 3].reshape(img.height(), img.width(), 3).copy()


# ── Karta ozvěny ──────────────────────────────────────────────────────────────

class EchoCard(QWidget):
    opened = pyqtSignal(object)

    def __init__(self, echo, then_img, now_img, parent=None):
        super().__init__(parent)
        self.echo = echo
        self.images = (then_img, now_img)
        self._scaled = {}
        self.setFixedHeight(CARD_HEIGHT)
        self.setMinimumWidth(CARD_MIN_W)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        first, last = echo["items"][0], echo["items"][-1]
        self.setAccessibleName(tr("echoes-a11y-card", value=echo.get('place') or '', year=first['year'], year2=last['year']))
        self.setToolTip(tr("echoes-compare-tooltip"))
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.opened.emit(self.echo)
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.echo)
            return
        super().keyPressEvent(e)

    def _half(self, i, w, h):
        key = (i, w, h)
        if key not in self._scaled:
            self._scaled[key] = _cover(self.images[i], w, h)
        return self._scaled[key]

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = tokens.palette()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(tokens.RADIUS["lg"])
        hover = max(0.0, min(1.0, float(self._hover.value)))
        p.save()
        p.setClipPath(smooth_rect_path(r, radius))
        p.fillRect(r, tokens.qcolor(t["surface_3"]))
        dpr = self.devicePixelRatioF()
        split = r.left() + r.width() * (0.5 - 0.04 * hover)       # najetí: „teď“ se trochu rozevře
        for i, rect in enumerate((QRectF(r.left(), r.top(), split - r.left() - 1, r.height()),
                                  QRectF(split + 1, r.top(), r.right() - split - 1, r.height()))):
            img = self._half(i, max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)))
            if not img.isNull():
                p.drawImage(rect, img)
        shade = QLinearGradient(0, r.bottom() - 96, 0, r.bottom())
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 200))
        p.fillRect(r, shade)
        p.restore()
        # roky na obou polovinách
        chip = QFont(tokens.ui_family())
        chip.setPixelSize(12)
        chip.setWeight(QFont.Weight.Bold)
        p.setFont(chip)
        fm = p.fontMetrics()
        first, last = self.echo["items"][0], self.echo["items"][-1]
        for text, x, align_right in ((str(first["year"]), r.left() + 14, False), (str(last["year"]), r.right() - 14, True)):
            w = fm.horizontalAdvance(text) + 18
            cr = QRectF(x - w if align_right else x, r.top() + 14, w, 24)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(12, 12, 14, 160))
            p.drawRoundedRect(cr, 12, 12)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(cr, int(Qt.AlignmentFlag.AlignCenter), text)
        p.setFont(tokens.font("title"))
        th = p.fontMetrics().height()
        sub_f = QFont(tokens.ui_family())
        sub_f.setPixelSize(13)
        pad, tw = 18, int(r.width() - 36)
        title = self.echo.get("place") or tr("echoes-echo")
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRectF(r.left() + pad, r.bottom() - pad - 18 - th, tw, th), int(Qt.AlignmentFlag.AlignLeft),
                   p.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, tw))
        p.setFont(sub_f)
        p.setPen(QColor(235, 235, 240, 220))
        p.drawText(QRectF(r.left() + pad, r.bottom() - pad - 17, tw, 18), int(Qt.AlignmentFlag.AlignLeft),
                   echoes.subtitle(self.echo))
        if self.hasFocus():
            p.setPen(tokens.qcolor(t["accent"]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(r.adjusted(1, 1, -1, -1), radius))
        p.end()


class EchoesPanel(QWidget):
    """Obsah stránky Ozvěny: mřížka karet / prázdný stav. opened(echo) -> srovnání."""
    opened = pyqtSignal(object)
    loaded = pyqtSignal(int)

    def __init__(self, connect, parent=None):
        super().__init__(parent)
        self.connect = connect
        self.echoes = []
        self.cards = []
        self._job = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.empty = EmptyState("echo", tr("echoes-loading-title"),
                                tr("echoes-loading-body"))
        lay.addWidget(self.empty, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(24, 16, 24, 24)
        self.grid.setSpacing(16)
        self.scroll.setWidget(self.body)
        self.scroll.hide()
        lay.addWidget(self.scroll, 1)

    def load(self):
        job = EchoJob(self.connect)
        job.signals.done.connect(self._on_done)
        self._job = job
        QThreadPool.globalInstance().start(job)

    def _on_done(self, result):
        self._job = None
        found, images = result
        self.echoes = found
        for c in self.cards:
            c.deleteLater()
        self.cards = []
        if not found:
            self.empty.set_content("echo", tr("echoes-empty-title"),
                                   tr("echoes-empty-body"))
            self.empty.show()
            self.scroll.hide()
            self.loaded.emit(0)
            return
        for e in found:
            card = EchoCard(e, images.get(e["items"][0]["id"], QImage()), images.get(e["items"][-1]["id"], QImage()))
            card.opened.connect(self.opened.emit)
            self.cards.append(card)
        self._relayout()
        self.empty.hide()
        self.scroll.show()
        self.loaded.emit(len(found))

    def _columns(self):
        return max(1, min(3, (self.width() - 48 + 16) // (CARD_MIN_W + 16)))

    def _relayout(self):
        cols = self._columns()
        for i, c in enumerate(self.cards):
            self.grid.addWidget(c, i // cols, i % cols)
        self.grid.setRowStretch(len(self.cards) // cols + 1, 1)
        self._cols = cols

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.cards and self._columns() != getattr(self, "_cols", 0):
            for c in self.cards:
                self.grid.removeWidget(c)
            self._relayout()


# ── Srovnání tehdy / teď ──────────────────────────────────────────────────────

class ThenNowView(QWidget):
    """„Teď“ celé, „tehdy“ vlevo od dělicí čáry (tažení myší, šipky ←/→, Home/End)."""
    open_photo = pyqtSignal(int)

    def __init__(self, echo, parent=None):
        super().__init__(parent)
        self.echo = echo
        self.then_i, self.now_i = 0, len(echo["items"]) - 1
        self.images = (QImage(), QImage())
        self.matrix = None
        self.split = motion.Tween(INTRO_FROM, lambda v: self.update(), self)
        self._drag = False
        self._job = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(320)
        self.setAccessibleName(tr("echoes-compare-a11y"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 16)
        lay.addStretch(1)
        self.years = None
        if len(echo["items"]) > 2:              # víc návratů: vybrat, který rok je „tehdy“
            self.years = SegmentedControl([(str(it["year"]), str(k)) for k, it in enumerate(echo["items"][:-1])],
                                          current="0")
            self.years.changed.connect(lambda key: self.set_then(int(key)))
            lay.addWidget(self.years, 0, Qt.AlignmentFlag.AlignHCenter)
        self._load()

    def set_then(self, i):
        if 0 <= i < self.now_i and i != self.then_i:
            self.then_i = i
            self._load()

    def _load(self):
        self.images, self.matrix = (QImage(), QImage()), None
        job = PairJob(self.echo["items"][self.then_i], self.echo["items"][self.now_i])
        job.signals.done.connect(self._on_pair)
        self._job = job
        QThreadPool.globalInstance().start(job)
        self.update()

    def _on_pair(self, result):
        items, imgs, mat = result
        if items != (self.echo["items"][self.then_i], self.echo["items"][self.now_i]):
            return                              # mezitím vybrán jiný rok
        self.images, self.matrix = tuple(imgs), mat
        self.split.set(INTRO_FROM)
        self.split.to(0.5, "scene")
        self.update()

    # ── geometrie ──
    def image_rect(self):
        now = self.images[1]
        area = QRectF(self.rect()).adjusted(24, 12, -24, -(64 if self.years else 24))
        if now.isNull() or area.width() <= 0 or area.height() <= 0:
            return area
        k = min(area.width() / now.width(), area.height() / now.height())
        w, h = now.width() * k, now.height() * k
        return QRectF(area.center().x() - w / 2, area.center().y() - h / 2, w, h)

    def _then_transform(self, rect):
        """Pixely „tehdy“ -> obrazovka: přes srovnávací matici do pixelů „teď“, pak na rect."""
        then, now = self.images
        to_screen = QTransform(rect.width() / now.width(), 0, 0, rect.height() / now.height(), rect.left(), rect.top())
        if self.matrix is not None:
            m = self.matrix
            align = QTransform(float(m[0, 0]), float(m[1, 0]), float(m[0, 1]), float(m[1, 1]), float(m[0, 2]), float(m[1, 2]))
            return align * to_screen
        # bez srovnání: výplň stejného rámečku
        k = max(rect.width() / then.width(), rect.height() / then.height())
        return QTransform(k, 0, 0, k, rect.center().x() - then.width() * k / 2, rect.center().y() - then.height() * k / 2)

    # ── interakce ──
    def _set_from_x(self, x):
        r = self.image_rect()
        if r.width() > 0:
            self.split.set(max(0.0, min(1.0, (x - r.left()) / r.width())))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.image_rect().contains(e.position()):
            self._drag = True
            self._set_from_x(e.position().x())     # set() zastaví i úvodní animaci
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        r = self.image_rect()
        near = abs(e.position().x() - (r.left() + r.width() * self.split.value)) < 14 and r.contains(e.position())
        self.setCursor(Qt.CursorShape.SplitHCursor if (self._drag or near) else Qt.CursorShape.ArrowCursor)
        if self._drag:
            self._set_from_x(e.position().x())

    def mouseReleaseEvent(self, e):
        self._drag = False
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        r = self.image_rect()
        if r.contains(e.position()):
            left = e.position().x() < r.left() + r.width() * self.split.value
            self.open_photo.emit(self.echo["items"][self.then_i if left else self.now_i]["id"])

    def keyPressEvent(self, e):
        step = {Qt.Key.Key_Left: -0.05, Qt.Key.Key_Right: 0.05}.get(e.key())
        if step is not None:
            self.split.set(max(0.0, min(1.0, self.split.value + step)))
            return
        if e.key() in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self.split.to(1.0 if e.key() == Qt.Key.Key_End else 0.0, "standard")
            return
        super().keyPressEvent(e)

    # ── kreslení ──
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        then, now = self.images
        r = self.image_rect()
        if now.isNull() or then.isNull():
            p.setPen(tokens.qcolor(tokens.palette()["text_secondary"]))
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), tr("echoes-preparing"))
            p.end()
            return
        x = r.left() + r.width() * max(0.0, min(1.0, float(self.split.value)))
        clip = smooth_rect_path(r, float(tokens.RADIUS["lg"]))
        p.save()
        p.setClipPath(clip)
        p.drawImage(r, now)
        p.setClipRect(QRectF(r.left(), r.top(), x - r.left(), r.height()), Qt.ClipOperation.IntersectClip)
        p.fillRect(r, QColor(10, 10, 12))             # okraje srovnaného snímku = tma, ne „teď“
        p.setTransform(self._then_transform(r))
        p.drawImage(QPointF(0, 0), then)
        p.restore()
        # dělicí čára s úchytem
        p.setPen(QColor(255, 255, 255, 230))
        p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 240))
        p.drawEllipse(QPointF(x, r.center().y()), 15, 15)
        p.setPen(QColor(20, 20, 24))
        f = QFont(tokens.ui_family())
        f.setPixelSize(13)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(QRectF(x - 15, r.center().y() - 15, 30, 30), int(Qt.AlignmentFlag.AlignCenter), "‹ ›")
        # roky v rozích
        then_it, now_it = self.echo["items"][self.then_i], self.echo["items"][self.now_i]
        fm = p.fontMetrics()
        for text, left in ((str(then_it["year"]), True), (str(now_it["year"]), False)):
            w = fm.horizontalAdvance(text) + 20
            cr = QRectF(r.left() + 14 if left else r.right() - 14 - w, r.top() + 14, w, 26)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(12, 12, 14, 170))
            p.drawRoundedRect(cr, 13, 13)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(cr, int(Qt.AlignmentFlag.AlignCenter), text)
        if self.hasFocus():
            p.setPen(tokens.qcolor(tokens.palette()["accent"]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(clip)
        p.end()
