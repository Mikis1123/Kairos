"""Stage 2: hluboké přiblížení v prohlížeči – kolečko, sevření prstů, dvojklik 1:1, posun se setrvačností.

Prohlížeč dřív fotku jen vešel do plochy (24 Mpx HEIC nikdy víc než ~4 Mpx na displeji). Teď:

* Stav = šířka obrazu na ploše W (logické px) a střed pohledu (u, v) v normalizovaných
  souřadnicích obrazu 0–1. Nezávisí na zdroji: náhled v rozlišení displeje i originál se
  vykreslí na stejné místo, výměna zdroje uprostřed přiblížení je neviditelná.
* Zdroj podle potřeby: zprvu dosavadní obraz (rozlišení displeje), od prvního přiblížení se na
  pozadí dekóduje originál (~0,9 s u 24 Mpx HEIC) do pyramidy plné / ½ / ¼ velikosti. Kreslí
  se úroveň, která má aspoň tolik pixelů, kolik jich pohled potřebuje (žádné zvětšování ani
  aliasing velkého zmenšení), bilineárně – naměřeno ~1,5–2 ms na snímek 2880×1800.
* 100 % = 1 pixel originálu na 1 fyzický pixel displeje; strop 200 %.
* Kolečko myši = plynulý zoom ke kurzoru (animace 180 ms), sevření prstů na touchpadu (Ctrl +
  kolečko po malých krocích) přímo; dvouprstý posun touchpadu a tažení myší posouvají,
  po puštění se setrvačností (τ 325 ms jako iOS).

Čistá geometrie (fit_width, clamp_center, zoom_about, pick_level) je bez Qt widgetů a testovaná.
"""
import math
import os
import time

from PyQt6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsItem

MAX_DEVICE_SCALE = 2.0          # 200 % = 2 fyzické px na pixel originálu
ZOOM_ANIM_MS = 180
NOTCH_FACTOR = 1.25             # jeden zub kolečka myši
INERTIA_TAU_S = 0.325
INERTIA_MIN_SPEED = 25.0        # logické px/s – pod tím setrvačnost končí
FIT_EPS = 1e-3
MIN_W = 1e-6                    # pod tím je šířka „neplatná“ (0.0 = vešlé je sentinel, ne rozměr)
MIN_FACTOR, MAX_FACTOR = 0.05, 50.0   # jeden krok zoom_by – rychlé/zběsilé vstupy nesmí přeskočit do ≤ 0 / ∞


def _finite_pos(x):
    return isinstance(x, (int, float)) and math.isfinite(x) and x > MIN_W


# ── geometrie (čistá) ─────────────────────────────────────────────────────────

def fit_width(aspect, vw, vh):
    """Šířka obrazu (logické px) vešlého do plochy; aspect = výška / šířka."""
    if vw <= 0 or vh <= 0 or aspect <= 0:
        return 0.0
    return min(float(vw), float(vh) / aspect)


def clamp_center(u, v, w, aspect, vw, vh):
    """Střed tak, aby obraz větší než plocha nenechal okraj; menší rozměr se centruje."""
    if not (_finite_pos(w) and _finite_pos(aspect)) or vw <= 0 or vh <= 0:
        return 0.5, 0.5
    h = w * aspect
    if w <= vw:
        u = 0.5
    else:
        half = vw / (2.0 * w)
        u = min(max(u, half), 1.0 - half)
    if h <= vh:
        v = 0.5
    else:
        half = vh / (2.0 * h)
        v = min(max(v, half), 1.0 - half)
    return u, v


def zoom_about(u, v, w, new_w, ax, ay, vw, vh, aspect):
    """Nový střed tak, aby bod obrazu pod (ax, ay) zůstal pod kurzorem.
    Neplatné rozměry (w / new_w / aspect ≤ 0, prázdná plocha) -> beze změny, žádné dělení."""
    if vw <= 0 or vh <= 0:
        return u, v
    if not (_finite_pos(w) and _finite_pos(new_w) and _finite_pos(aspect)):
        return u, v
    h, new_h = w * aspect, new_w * aspect
    pu = u + (ax - vw / 2.0) / w
    pv = v + (ay - vh / 2.0) / h
    nu = pu - (ax - vw / 2.0) / new_w
    nv = pv - (ay - vh / 2.0) / new_h
    return clamp_center(nu, nv, new_w, aspect, vw, vh)


def pick_level(widths, need_px):
    """Index nejmenší úrovně pyramidy (šířky sestupně), která má aspoň need_px pixelů na šířku;
    jinak největší (index 0)."""
    best = 0
    for i, w in enumerate(widths):
        if w >= need_px - 0.5:
            best = i
    return best


def build_pyramid(img, levels=3):
    """[plná, ½, ¼] jako QImage RGB32 (raster je kreslí bez převodu formátu)."""
    if img.format() != QImage.Format.Format_RGB32:
        img = img.convertToFormat(QImage.Format.Format_RGB32)
    out = [img]
    for _ in range(levels - 1):
        prev = out[-1]
        if min(prev.width(), prev.height()) < 512:
            break
        out.append(prev.scaled(max(1, prev.width() // 2), max(1, prev.height() // 2),
                               Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
    return out


def original_size(path):
    """(šířka, výška) originálu po EXIF orientaci jen z hlavičky (~5 ms i u HEIC), nebo None."""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in {".heic", ".heif", ".dng", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            from PIL import Image
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            with Image.open(path) as im:
                w, h = im.size
                if im.getexif().get(274) in (5, 6, 7, 8):
                    w, h = h, w
                return w, h
    except Exception:
        pass
    from PyQt6.QtGui import QImageReader
    r = QImageReader(path)
    r.setAutoTransform(True)
    s = r.size()
    if s.isValid():
        return s.width(), s.height()
    return None


# ── dekódování originálu na pozadí ───────────────────────────────────────────

class _FullSignals(QObject):
    done = pyqtSignal(str, int, list)


class FullResJob(QRunnable):
    """Originál v plném rozlišení (profil displeje, EXIF orientace) -> pyramida."""

    def __init__(self, path, token, adjustments=None):
        super().__init__()
        self.path, self.token = path, token
        self.adjustments = adjustments          # pillar 2: Quick Edit applied before the pyramid
        self.signals = _FullSignals()

    def run(self):
        levels = []
        try:
            from viewer_hud import load_image_file
            img = load_image_file(self.path, None, use_preview=False)
            if not img.isNull() and self.adjustments is not None:
                import quick_edit
                img = quick_edit.render_qimage(img, self.adjustments)
            if not img.isNull():
                levels = build_pyramid(img)
        except Exception as e:
            print(f"[Zoom] {os.path.basename(self.path)}: {e}")
        self.signals.done.emit(self.path, self.token, levels)


# ── položka scény ─────────────────────────────────────────────────────────────

class ZoomImageItem(QGraphicsItem):
    """Kreslí QImage / QPixmap s transformací položky (měřítko + posun), bilineárně."""

    def __init__(self):
        super().__init__()
        self._src = None
        self._w = self._h = 1
        self.smooth = True

    def set_source(self, src):
        self.prepareGeometryChange()
        self._src = src
        self._w, self._h = (src.width(), src.height()) if src is not None else (1, 1)

    def source(self):
        return self._src

    def boundingRect(self):
        return QRectF(0, 0, self._w, self._h)

    def paint(self, painter, option, widget=None):
        src = self._src
        if src is None:
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.smooth)
        if isinstance(src, QImage):
            painter.drawImage(QPointF(0, 0), src)
        else:
            painter.drawPixmap(QPointF(0, 0), src)


# ── ovladač ───────────────────────────────────────────────────────────────────

class ZoomController(QObject):
    """Stav přiblížení plátna. Plátno volá wheel/press/move/release/double_click a resize;
    prohlížeč volá set_photo (nová položka), set_enabled a set_full_levels (přes full_res_wanted)."""
    changed = pyqtSignal(float)         # procenta (100 = 1:1 originálu)
    full_res_wanted = pyqtSignal()
    zoomed_changed = pyqtSignal(bool)

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.item = ZoomImageItem()
        self.item.setZValue(1.5)
        self.item.setVisible(False)
        canvas.scene().addItem(self.item)
        self.enabled = False
        self.aspect = 0.0
        self.orig = None                # (w, h) originálu
        self.w = 0.0                    # šířka obrazu na ploše (logické px); 0 = vešlé
        self.u = self.v = 0.5
        self._levels = []               # pyramida originálu
        self._base = None               # dosavadní obraz prohlížeče (rozlišení displeje)
        self._wanted = False
        self._zoomed = False
        self.token = 0
        # animace (W, u, v) -> cíl
        self._anim_from = self._anim_to = None
        self._anim_t0 = 0.0
        self._anim_ms = ZOOM_ANIM_MS
        # setrvačnost
        self._vel = (0.0, 0.0)
        self._last = 0.0
        self._drag = None
        self._samples = []
        self._ticker = QTimer(self)
        self._ticker.setTimerType(Qt.TimerType.PreciseTimer)
        self._ticker.setInterval(6)     # o něco rychleji než 120 Hz: jitter časovače Windows (~1 ms) nesmí shodit snímek
        self._ticker.timeout.connect(self._tick)

    # ── vstup od prohlížeče ──
    def set_photo(self, path=None, size=None):
        """Nová položka: zpět na vešlé, zahodit originál."""
        self.token += 1
        self._stop_motion()
        self._levels = []
        self._base = None
        self._wanted = False
        self.orig = size
        self.path = path
        self.w = 0.0
        self.u = self.v = 0.5
        self._apply()

    def set_enabled(self, on):
        self.enabled = bool(on)
        if not self.enabled and self.is_zoomed():
            self.reset(animate=False)

    def set_base(self, pixmap):
        """Obraz, který prohlížeč právě ukazuje (náhled / originál v rozlišení displeje)."""
        self._base = pixmap if pixmap is not None and not pixmap.isNull() else None
        if self._base is not None:
            self.aspect = self._base.height() / float(max(1, self._base.width()))
            if self.orig is None:
                self.orig = (self._base.width(), self._base.height())
        if self.is_zoomed():
            self._apply()

    def set_full_levels(self, token, levels):
        if token != self.token or not levels:
            return
        self._levels = levels
        self.orig = (levels[0].width(), levels[0].height())
        self.aspect = levels[0].height() / float(max(1, levels[0].width()))
        if self.is_zoomed():
            self._apply()

    # ── dotazy ──
    def is_zoomed(self):
        return self.w > 0.0

    def fit_w(self):
        vp = self.canvas.viewport()
        return fit_width(self.aspect, vp.width(), vp.height())

    def max_w(self):
        dpr = self.canvas.devicePixelRatioF()
        ow = self.orig[0] if self.orig else 0
        return max(self.fit_w(), ow * MAX_DEVICE_SCALE / max(dpr, 1e-6))

    def one_to_one_w(self):
        dpr = self.canvas.devicePixelRatioF()
        ow = self.orig[0] if self.orig else 0
        return max(self.fit_w(), ow / max(dpr, 1e-6))

    def percent(self, w=None):
        w = self.fit_w() if not w else w
        ow = self.orig[0] if self.orig else 0
        if ow <= 0:
            return 100.0
        return 100.0 * w * self.canvas.devicePixelRatioF() / ow

    # ── akce ──
    def zoom_to(self, new_w, anchor=None, animate=True):
        if not self.enabled or not _finite_pos(self.aspect):
            return
        vp = self.canvas.viewport()
        vw, vh = vp.width(), vp.height()
        fit = self.fit_w()
        if vw <= 0 or vh <= 0 or not _finite_pos(fit):
            return                                          # ještě bez rozvržení (plocha 0×0)
        if not isinstance(new_w, (int, float)) or math.isnan(new_w):
            return
        cur_w = self.w or fit
        new_w = min(max(new_w, fit), self.max_w())
        ax, ay = (anchor.x(), anchor.y()) if anchor is not None else (vw / 2.0, vh / 2.0)
        # z cíle běžící animace (rychlé zuby kolečka se sčítají), jinak z aktuálního stavu
        base = self._anim_to if self._anim_to is not None else (cur_w, self.u, self.v)
        bw, bu, bv = base
        bw = bw or fit                                      # 0.0 = cíl „vešlé“ (zoom ven během animace)
        nu, nv = zoom_about(bu, bv, bw, new_w, ax, ay, vw, vh, self.aspect)
        target = (0.0 if abs(new_w - fit) <= fit * FIT_EPS else new_w, nu, nv)
        if new_w > fit * (1 + FIT_EPS) and not self._wanted:
            self._wanted = True
            self.full_res_wanted.emit()
        self._vel = (0.0, 0.0)
        if animate and not _reduce_motion():
            self._anim_from = (cur_w, self.u, self.v)
            self._anim_to = target
            self._anim_t0 = time.perf_counter()
            self._ticker.start()
        else:
            self._anim_from = self._anim_to = None
            self.w, self.u, self.v = target
            self._apply()

    def zoom_by(self, factor, anchor=None, animate=True):
        cur = self._anim_to[0] if self._anim_to is not None else self.w
        cur = cur or self.fit_w()
        if not _finite_pos(cur):
            return
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return
        if not math.isfinite(factor) or factor <= 0:
            return
        factor = min(max(factor, MIN_FACTOR), MAX_FACTOR)
        self.zoom_to(cur * factor, anchor, animate)

    def reset(self, animate=True):
        if self.is_zoomed() or self._anim_to is not None:
            self.zoom_to(self.fit_w(), None, animate)

    def toggle_one_to_one(self, anchor=None):
        if self.is_zoomed():
            self.reset()
        else:
            self.zoom_to(max(self.one_to_one_w(), self.fit_w() * 1.6), anchor)

    def pan_by(self, dx, dy, apply=True):
        if not self.is_zoomed() or not _finite_pos(self.aspect):
            return
        vp = self.canvas.viewport()
        self.u, self.v = clamp_center(self.u - dx / self.w, self.v - dy / (self.w * self.aspect),
                                      self.w, self.aspect, vp.width(), vp.height())
        if apply:
            self._apply()

    # ── myš / kolečko ──
    def wheel(self, event):
        """True = událost spotřebována."""
        if not self.enabled:
            return False
        dy = event.angleDelta().y()
        dx = event.angleDelta().x()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        notch = dy != 0 and dx == 0 and dy % 120 == 0 and event.pixelDelta().isNull()
        pos = event.position()
        if ctrl:                                          # sevření prstů (Windows = Ctrl + kolečko)
            self.zoom_by(2.0 ** (dy / 240.0), pos, animate=notch)
            return True
        if notch:                                         # kolečko myši
            self.zoom_by(NOTCH_FACTOR ** (dy / 120.0), pos)
            return True
        if self.is_zoomed():                              # dvouprstý posun touchpadu
            pd = event.pixelDelta()
            if not pd.isNull():
                self.pan_by(pd.x(), pd.y())
            else:
                self.pan_by(dx / 2.0, dy / 2.0)
            return True
        return False

    def gesture_zoom(self, value, pos):
        """QNativeGestureEvent ZoomNativeGesture: value = přírůstek (0.1 = +10 %)."""
        if self.enabled:
            self.zoom_by(1.0 + float(value), pos, animate=False)

    def press(self, pos):
        if not self.is_zoomed():
            return False
        self._stop_motion()
        self._drag = pos
        self._samples = [(time.perf_counter(), pos)]
        self.canvas.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        return True

    def move(self, pos):
        if self._drag is None:
            return False
        self.pan_by(pos.x() - self._drag.x(), pos.y() - self._drag.y())
        self._drag = pos
        now = time.perf_counter()
        self._samples.append((now, pos))
        self._samples = [s for s in self._samples if now - s[0] <= 0.1]
        return True

    def release(self, pos):
        if self._drag is None:
            return False
        self._drag = None
        self.canvas.viewport().setCursor(Qt.CursorShape.OpenHandCursor if self.is_zoomed()
                                         else Qt.CursorShape.ArrowCursor)
        now = time.perf_counter()
        samples = [s for s in self._samples if now - s[0] <= 0.1]
        if len(samples) >= 2 and not _reduce_motion():
            (t0, p0), (t1, p1) = samples[0], samples[-1]
            dt = max(1e-3, t1 - t0)
            self._vel = ((p1.x() - p0.x()) / dt, (p1.y() - p0.y()) / dt)
            if math.hypot(*self._vel) > INERTIA_MIN_SPEED:
                self._last = now
                self._ticker.start()
        return True

    def resized(self):
        if self.is_zoomed():
            vp = self.canvas.viewport()
            if vp.width() <= 0 or vp.height() <= 0:
                return                                      # minimalizace: stav nechat, srovná další resize
            self.w = max(self.w, self.fit_w())
            self.u, self.v = clamp_center(self.u, self.v, self.w, self.aspect, vp.width(), vp.height())
            self._apply()

    # ── animace ──
    def _stop_motion(self):
        self._anim_from = self._anim_to = None
        self._vel = (0.0, 0.0)
        self._ticker.stop()

    def _tick(self):
        now = time.perf_counter()
        busy = False
        if self._anim_to is not None:
            t = min(1.0, (now - self._anim_t0) * 1000.0 / max(1, self._anim_ms))
            e = 1.0 - (1.0 - t) ** 3                       # ease-out cubic
            (w0, u0, v0), (w1, u1, v1) = self._anim_from, self._anim_to
            fit = self.fit_w()
            a, b = (w0 or fit), (w1 or fit)
            if _finite_pos(a) and _finite_pos(b):
                w = math.exp(math.log(a) + (math.log(b) - math.log(a)) * e)   # zoom v log měřítku
            else:                                          # plocha zmizela uprostřed animace
                w, t = (b if _finite_pos(b) else 0.0), 1.0
            self.w = 0.0 if (t >= 1.0 and w1 == 0.0) else w
            self.u, self.v = u0 + (u1 - u0) * e, v0 + (v1 - v0) * e
            if self.w:
                vp = self.canvas.viewport()
                self.u, self.v = clamp_center(self.u, self.v, self.w, self.aspect, vp.width(), vp.height())
            if t >= 1.0:
                self._anim_from = self._anim_to = None
            else:
                busy = True
        elif self._vel != (0.0, 0.0):
            dt = max(0.0, now - self._last)
            self._last = now
            vx, vy = self._vel
            before = (self.u, self.v)
            self.pan_by(vx * dt, vy * dt, apply=False)
            k = math.exp(-dt / INERTIA_TAU_S)
            vx, vy = vx * k, vy * k
            if self.u == before[0]:
                vx = 0.0                                   # narazil na okraj
            if self.v == before[1]:
                vy = 0.0
            self._vel = (vx, vy) if math.hypot(vx, vy) > INERTIA_MIN_SPEED else (0.0, 0.0)
            busy = self._vel != (0.0, 0.0)
        self._apply()
        if not busy:
            self._ticker.stop()

    # ── vykreslení ──
    def _apply(self):
        canvas = self.canvas
        zoomed = self.is_zoomed() and self.aspect > 0
        if zoomed:
            dpr = canvas.devicePixelRatioF()
            need = self.w * dpr
            src = None
            if self._levels:
                src = self._levels[pick_level([lv.width() for lv in self._levels], need)]
            if src is None or (self._base is not None and src.width() < self._base.width()
                               and src.width() < need):
                src = self._base or src
            if src is None or src.width() <= 0:
                zoomed = False
            else:
                if self.item.source() is not src:
                    self.item.set_source(src)
                vp = canvas.viewport()
                scale = self.w / float(src.width())
                h = self.w * self.aspect
                self.item.setScale(scale)
                self.item.setPos(vp.width() / 2.0 - self.u * self.w, vp.height() / 2.0 - self.v * h)
        self.item.setVisible(zoomed)
        canvas.photo_item.setVisible(not zoomed and not canvas.is_video_shown())
        if zoomed != self._zoomed:
            self._zoomed = zoomed
            if not zoomed:
                self.item.set_source(None)
            canvas.viewport().setCursor(Qt.CursorShape.OpenHandCursor if zoomed else Qt.CursorShape.ArrowCursor)
            self.zoomed_changed.emit(zoomed)
        canvas.viewport().update()
        self.changed.emit(self.percent(self.w or None))


def _reduce_motion():
    try:
        import motion
        return bool(motion.reduced())
    except Exception:
        return False
