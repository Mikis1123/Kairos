"""Fyzický prohlížeč (Polish P5): stažení dolů = zavření se setrvačností a „hero“ otevření z dlaždice.

Jediný překryv HeroOverlay (dítě prohlížeče, nad plátnem i HUD) kreslí během přechodu tři vrstvy:
snímek stránky pod prohlížečem (mřížka, jeden grab() na začátku), černé pozadí prohlížeče
a fotku v obdélníku s plynulými rohy. Plátno, QQuickWidget ani QGraphicsView se netransformují.
Oba snímky žijí jen po dobu přechodu – pak se překryv skryje a pustí je.

Gesto: stisk na fotce (ne video, ne celá obrazovka) a převážně svislý tah > DRAG_START_PX.
Fotka sleduje prst, zmenšuje se (1 − 0,35 · postup), pozadí prolíná do mřížky. Puštění:
postup ≥ DISMISS_PROGRESS nebo rychlost dolů ≥ DISMISS_VELOCITY = doletět do dlaždice
(délka podle rychlosti), jinak pružinou zpět. Omezené animace = okamžitý výsledek.
"""
import time
from collections import deque

from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QRectF, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QWidget

import motion
import tokens
from ui_controls import smooth_rect_path

DRAG_START_PX = 8
DRAG_RANGE = 0.6             # tah o 60 % výšky = postup 1
SHRINK = 0.35
DISMISS_PROGRESS = 0.25
DISMISS_VELOCITY = 900.0     # px/s dolů
CANCEL_VELOCITY = -300.0     # rychlý švih nahoru gesto zruší
SAMPLE_WINDOW_MS = 80
MIN_SETTLE_MS, MAX_SETTLE_MS = 160, 380


# ── čistá fyzika (testovatelná bez okna) ──────────────────────────────────────

def drag_progress(dy, height):
    return max(0.0, min(1.0, dy / max(1.0, height * DRAG_RANGE)))


def drag_rect(fit, dx, dy, progress):
    """Obdélník fotky během tahu: zmenšení kolem středu + posun za prstem (nahoru s odporem)."""
    s = 1.0 - SHRINK * progress
    w, h = fit.width() * s, fit.height() * s
    if dy < 0:
        dy *= 0.25
    c = fit.center()
    return QRectF(c.x() - w / 2 + dx, c.y() - h / 2 + dy, w, h)


def velocity(samples):
    """samples [(t_ms, y)] -> svislá rychlost v px/s z posledních SAMPLE_WINDOW_MS."""
    if len(samples) < 2:
        return 0.0
    t_end, y_end = samples[-1]
    first = next(((t, y) for t, y in samples if t_end - t <= SAMPLE_WINDOW_MS), samples[-2])
    dt = max(1.0, t_end - first[0])
    return (y_end - first[1]) / dt * 1000.0


def should_dismiss(progress, vy):
    if vy <= CANCEL_VELOCITY:
        return False
    return progress >= DISMISS_PROGRESS or vy >= DISMISS_VELOCITY


def settle_ms(distance, vy):
    """Doletová doba: rychlý švih = kratší let (setrvačnost), pomalé puštění = klidný let."""
    if vy > 1.0:
        return int(max(MIN_SETTLE_MS, min(MAX_SETTLE_MS, distance / vy * 1000.0 * 1.6)))
    return MAX_SETTLE_MS - 60


def lerp(a, b, t):
    return a + (b - a) * t


def lerp_rect(a, b, t):
    return QRectF(lerp(a.x(), b.x(), t), lerp(a.y(), b.y(), t),
                  lerp(a.width(), b.width(), t), lerp(a.height(), b.height(), t))


def fit_rect(size, area):
    """Obdélník velikosti size (KeepAspectRatio) uprostřed area."""
    w, h = max(1.0, float(size.width())), max(1.0, float(size.height()))
    s = min(area.width() / w, area.height() / h)
    return QRectF(area.center().x() - w * s / 2, area.center().y() - h * s / 2, w * s, h * s)


def cover_source(size, rect):
    """Výřez zdroje (size) s poměrem rect – fotka v dlaždici je oříznutá „výplní“."""
    w, h = max(1.0, float(size.width())), max(1.0, float(size.height()))
    target = rect.width() / max(1.0, rect.height())
    if w / h > target:
        cw = h * target
        return QRectF((w - cw) / 2, 0, cw, h)
    ch = w / target
    return QRectF(0, (h - ch) / 2, w, ch)


# ── překryv ───────────────────────────────────────────────────────────────────

class HeroOverlay(QWidget):
    """Kreslí přechod. state: rect, radius, backdrop (0 = černá prohlížeče, 1 = mřížka), crop (0–1)."""
    finished = pyqtSignal(str)       # "dismissed" | "restored" | "opened"

    def __init__(self, viewer):
        super().__init__(viewer)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.image = None
        self.backdrop = None
        self.rect_now = QRectF()
        self.radius = 0.0
        self.backdrop_op = 0.0
        self.crop = 0.0                  # 1 = výřez jako v dlaždici, 0 = celá fotka
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self._done)
        self._from = self._to = None
        self._outcome = ""
        self.hide()

    def begin(self, image, backdrop):
        self.image, self.backdrop = image, backdrop
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()

    def set_state(self, rect, radius, backdrop_op, crop=0.0):
        self.rect_now, self.radius, self.backdrop_op, self.crop = QRectF(rect), radius, backdrop_op, crop
        self.update()

    def state(self):
        return (QRectF(self.rect_now), self.radius, self.backdrop_op, self.crop)

    def animate_to(self, target, ms, curve, outcome):
        """target = (rect, radius, backdrop, crop). ms <= 0 nebo omezené animace = hned."""
        self._anim.stop()
        self._from, self._to, self._outcome = self.state(), target, outcome
        if ms <= 0 or motion.reduced():
            self._step(1.0)
            self._done()
            return
        self._anim.setDuration(int(ms))
        self._anim.setEasingCurve(curve)
        self._anim.start()

    def is_animating(self):
        return self._anim.state() == QVariantAnimation.State.Running

    def _step(self, t):
        t = float(t)
        (r0, rad0, b0, c0), (r1, rad1, b1, c1) = self._from, self._to
        self.set_state(lerp_rect(r0, r1, t), lerp(rad0, rad1, t),
                       max(0.0, min(1.0, lerp(b0, b1, t))), max(0.0, min(1.0, lerp(c0, c1, t))))

    def _done(self):
        outcome, self._outcome = self._outcome, ""
        try:
            self.finished.emit(outcome)
        except RuntimeError:
            pass

    def release(self):
        """Konec přechodu: skrýt a pustit snímky (paměť jen po dobu přechodu)."""
        self._anim.stop()
        self.image = self.backdrop = None
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        b = self.backdrop_op
        if self.backdrop is not None and b > 0.0:
            p.drawPixmap(self.rect(), self.backdrop)
            p.fillRect(self.rect(), QColor(0, 0, 0, round(255 * (1.0 - b))))
        else:                                 # bez snímku mřížky: neprůhledně k barvě pozadí aplikace
            p.fillRect(self.rect(), tokens.mix(QColor("#000000"), tokens.qcolor(tokens.palette()["bg"]), b))
        if self.image is not None and not self.image.isNull() and not self.rect_now.isEmpty():
            src_full = QRectF(0, 0, self.image.width(), self.image.height())
            src_crop = cover_source(self.image.size(), self.rect_now)
            src = lerp_rect(src_full, src_crop, self.crop)
            p.setClipPath(smooth_rect_path(self.rect_now, self.radius))
            p.drawPixmap(self.rect_now, self.image, src)
        p.end()


# ── gesto ─────────────────────────────────────────────────────────────────────

class DismissController(QObject):
    """Filtr myši na plátně prohlížeče. Spolupráce s prohlížečem přes:
       viewer.can_drag_dismiss() -> bool, viewer.photo_rect() -> QRectF (souřadnice prohlížeče),
       viewer.transition_provider(media_id) -> (backdrop QPixmap | None, cíl QRectF | None, poloměr)
       a viewer.request_close()."""
    dismissed = pyqtSignal()

    def __init__(self, viewer, surface):
        super().__init__(viewer)
        self.viewer, self.surface = viewer, surface
        self.overlay = HeroOverlay(viewer)
        self.overlay.finished.connect(self._on_finished)
        self.active = False
        self._press = None
        self._samples = deque(maxlen=12)
        self._fit = QRectF()
        surface.installEventFilter(self)

    def eventFilter(self, obj, event):
        try:                                  # PyQt6: výjimka z virtuální metody = pád procesu
            if obj is not self.surface:
                return False
            kind = event.type()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                if not self.overlay.is_animating():
                    self._press = event.position()
                    self._samples.clear()
                    self._sample(event.position().y())
                return False
            if kind == QEvent.Type.MouseMove and self._press is not None \
                    and event.buttons() & Qt.MouseButton.LeftButton:
                return self.drag_to(event.position())
            if kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                was = self.active
                self.release()
                return was
        except Exception as e:
            print(f"[Viewer gesto] {e}")
        return False

    def _sample(self, y):
        self._samples.append((time.monotonic() * 1000.0, float(y)))

    def drag_to(self, pos):
        """Pohyb myši (souřadnice plátna). True = událost spotřebována gestem."""
        if self._press is None:
            return False
        dx, dy = pos.x() - self._press.x(), pos.y() - self._press.y()
        self._sample(pos.y())
        if not self.active:
            if abs(dy) < DRAG_START_PX or abs(dy) < abs(dx) * 1.2 or dy < 0 or not self.viewer.can_drag_dismiss():
                return False
            self._start()
            if not self.active:
                return False
        h = max(1, self.viewer.height())
        prog = drag_progress(dy, h)
        self.overlay.set_state(drag_rect(self._fit, dx, dy, prog), 14.0 * prog, prog * 0.85, 0.0)
        return True

    def _start(self):
        image = self.viewer.canvas.displayed_pixmap()
        fit = self.viewer.photo_rect()
        if image is None or image.isNull() or fit.isEmpty():
            self._press = None
            return
        self._backdrop, self._target, self._radius = self.viewer.transition_provider(self.viewer.current_media_id())
        self._fit = fit
        self.active = True
        self.overlay.begin(image, self._backdrop)
        self.overlay.set_state(fit, 0.0, 0.0)

    def release(self):
        if not self.active:
            self._press = None
            return
        self.active = False
        self._press = None
        rect, radius, back, _ = self.overlay.state()
        prog = drag_progress(rect.center().y() - self._fit.center().y(), max(1, self.viewer.height()))
        vy = velocity(list(self._samples))
        if should_dismiss(prog, vy):
            if self._target is not None and self._backdrop is not None:
                target = (QRectF(self._target), self._radius, 1.0, 1.0)
            else:                             # dlaždice není k dispozici: zmenšit k místu a rozplynout
                c = rect.center()
                target = (QRectF(c.x() - rect.width() * 0.2, c.y() - rect.height() * 0.2,
                                 rect.width() * 0.4, rect.height() * 0.4), 14.0, 1.0, 0.0)
            dist = abs(target[0].center().y() - rect.center().y()) + abs(target[0].center().x() - rect.center().x())
            self.overlay.animate_to(target, settle_ms(dist, vy), motion.easing("emphasized"), "dismissed")
        else:
            self.overlay.animate_to((QRectF(self._fit), 0.0, 0.0, 0.0), motion.duration("spring"),
                                    motion.easing("spring"), "restored")

    def cancel(self):
        self.active = False
        self._press = None
        self.overlay.release()

    def _on_finished(self, outcome):
        if outcome == "dismissed":
            self.dismissed.emit()
            self.viewer.request_close()       # mřížka pod překryvem = poslední snímek přechodu
            self.overlay.release()
        elif outcome in ("restored", "opened"):
            self.overlay.release()

    # ── hero otevření ──
    def play_open(self, image, from_rect, radius, backdrop):
        """Opak zavření: z dlaždice (souřadnice prohlížeče) do fotky přes celou plochu."""
        if image is None or image.isNull() or from_rect is None or motion.reduced():
            return False
        area = self.viewer.photo_area()
        if area.isEmpty():
            return False
        self.cancel()
        self.overlay.begin(image, backdrop)
        self.overlay.set_state(QRectF(from_rect), radius, 1.0, 1.0)
        self.overlay.animate_to((fit_rect(image.size(), area), 0.0, 0.0, 0.0), motion.duration("emphasized"),
                                motion.easing("emphasized"), "opened")
        return True
