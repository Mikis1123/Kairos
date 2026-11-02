"""Značka Kairos (dávka 5): logo Iris, ikona aplikace, slovní značka.

Logo = prstenec s mezerou „v půl druhé“ a jiskra v té mezeře (kairos = ten pravý okamžik).
Jediný zdroj geometrie: z týchž konstant se kreslí QPainterem (splash, postranní panel,
ikona okna) i generuje SVG a vícevelikostní .ico (build_ico, assets/).
Kreslí se na mřížce 64×64; stupnici a polohu určí volající přes QPainter.
"""
import io
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, \
    QRadialGradient
from PyQt6.QtWidgets import QSizePolicy, QWidget

import tokens

APP_NAME = "Kairos"
APP_ID = "Kairos.PhotoLibrary"          # AppUserModelID: vlastní ikona na hlavním panelu Windows

GRID = 64.0
CENTER = QPointF(32.0, 32.0)
RING_R = 20.0
RING_STROKE = 7.0
GAP_AT = 45.0                           # „půl druhé“ na ciferníku = 45° nad osou x
GAP = 50.0
RING_START = GAP_AT + GAP / 2           # prstenec začíná za mezerou a jde proti směru hodin
RING_SPAN = 360.0 - GAP
SPARK_R = 22.5                          # střed jiskry kousek vně prstence
SPARK_ARM = 7.5
SPARK_PINCH = 0.16

# Barvy Iris (tokens.ACCENTS['iris'] v tmavém režimu) – značka má vlastní stálé barvy,
# nemění se se zvoleným zvýrazněním
IRIS_LIGHT = "#8B93FF"
IRIS_DEEP = "#5E64E8"
SPARK = "#4FD1C5"
SPARK_CORE = "#E9FFFC"
TILE_TOP = "#2A2A33"
TILE_BOTTOM = "#141418"

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def _polar(angle_deg, r, center=CENTER):
    a = math.radians(angle_deg)
    return QPointF(center.x() + r * math.cos(a), center.y() - r * math.sin(a))


def spark_center():
    return _polar(GAP_AT, SPARK_R)


def spark_path(center=None, arm=SPARK_ARM):
    """Čtyřcípá jiskra (prohnuté strany) se středem center."""
    c = center or spark_center()
    k = arm * SPARK_PINCH
    x, y = c.x(), c.y()
    path = QPainterPath(QPointF(x, y - arm))
    path.quadTo(QPointF(x + k, y - k), QPointF(x + arm, y))
    path.quadTo(QPointF(x + k, y + k), QPointF(x, y + arm))
    path.quadTo(QPointF(x - k, y + k), QPointF(x - arm, y))
    path.quadTo(QPointF(x - k, y - k), QPointF(x, y - arm))
    path.closeSubpath()
    return path


def ring_gradient():
    g = QLinearGradient(QPointF(12, 12), QPointF(52, 52))
    g.setColorAt(0.0, QColor(IRIS_LIGHT))
    g.setColorAt(1.0, QColor(IRIS_DEEP))
    return g


def spark_gradient(center=None, arm=SPARK_ARM):
    c = center or spark_center()
    g = QRadialGradient(c, arm)
    g.setColorAt(0.0, QColor(SPARK_CORE))
    g.setColorAt(0.45, QColor(SPARK))
    g.setColorAt(1.0, QColor(SPARK))
    return g


def paint_ring(p, progress=1.0, stroke=RING_STROKE, color=None):
    """Prstenec od začátku (za mezerou) do progress (0–1) své délky. color=None -> přechod Iris."""
    if progress <= 0:
        return
    pen = QPen(QBrush(QColor(color)) if color else QBrush(ring_gradient()), stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(CENTER.x() - RING_R, CENTER.y() - RING_R, 2 * RING_R, 2 * RING_R)
    p.drawArc(rect, round(RING_START * 16), round(RING_SPAN * min(1.0, progress) * 16))


def paint_spark(p, scale=1.0, color=None):
    if scale <= 0:
        return
    arm = SPARK_ARM * scale
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color) if color else spark_gradient(arm=arm))
    p.drawPath(spark_path(arm=arm))


def paint_mark(p, rect, ring=1.0, spark=1.0, small=False, mono=None):
    """Logo vepsané do rect (logické souřadnice). small = tučnější tahy pro 16–24 px.
    mono = jedna barva místo přechodů (např. v textu)."""
    rect = QRectF(rect)
    side = min(rect.width(), rect.height())
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
    p.scale(side / GRID, side / GRID)
    paint_ring(p, ring, RING_STROKE * (1.6 if small else 1.0), mono)
    paint_spark(p, spark * (1.55 if small else 1.0), mono)
    p.restore()


def paint_app_tile(p, side):
    """Ikona aplikace: tmavá dlaždice s plynulými rohy a logem uprostřed."""
    from ui_controls import smooth_rect_path
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    small = side <= 24
    inset = 0.0 if small else side * 0.03
    tile = QRectF(inset, inset, side - 2 * inset, side - 2 * inset)
    bg = QLinearGradient(tile.topLeft(), tile.bottomLeft())
    bg.setColorAt(0.0, QColor(TILE_TOP))
    bg.setColorAt(1.0, QColor(TILE_BOTTOM))
    path = smooth_rect_path(tile, tile.width() * 0.22)
    p.fillPath(path, bg)
    if not small:                                    # jemná světlá hrana nahoře (sklo)
        p.setPen(QPen(QColor(255, 255, 255, 26), max(1.0, side / 128)))
        p.drawPath(path)
    pad = tile.width() * (0.02 if small else 0.15)
    paint_mark(p, tile.adjusted(pad, pad, -pad, -pad), small=small)
    p.restore()


def render_tile(side):
    img = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    paint_app_tile(p, side)
    p.end()
    return img


def app_icon():
    """QIcon okna a hlavního panelu (vykreslená dlaždice v běžných velikostech)."""
    icon = QIcon()
    for side in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(QPixmap.fromImage(render_tile(side)))
    return icon


def set_windows_app_id():
    """Windows seskupí okna pod vlastní ikonu místo ikony python.exe."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def build_ico(path, sizes=ICO_SIZES):
    """Vícevelikostní .ico: každá velikost vykreslená zvlášť (ne zmenšená z 256 px)."""
    from PIL import Image
    from PyQt6.QtCore import QBuffer, QIODevice
    images = []
    for side in sorted(sizes, reverse=True):
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        render_tile(side).save(buf, "PNG")
        images.append(Image.open(io.BytesIO(bytes(buf.data()))).convert("RGBA"))
    images[0].save(path, format="ICO", sizes=[(s, s) for s in sorted(sizes, reverse=True)],
                   append_images=images[1:])
    return path


def _f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def mark_svg(tile=False):
    """SVG loga (64×64) ze stejné geometrie; tile=True s tmavou dlaždicí ikony."""
    a = _polar(RING_START, RING_R)
    b = _polar(RING_START + RING_SPAN, RING_R)
    c = spark_center()
    k = SPARK_ARM * SPARK_PINCH
    x, y, arm = c.x(), c.y(), SPARK_ARM
    spark = (f"M{_f(x)} {_f(y - arm)}Q{_f(x + k)} {_f(y - k)} {_f(x + arm)} {_f(y)}"
             f"Q{_f(x + k)} {_f(y + k)} {_f(x)} {_f(y + arm)}Q{_f(x - k)} {_f(y + k)} {_f(x - arm)} {_f(y)}"
             f"Q{_f(x - k)} {_f(y - k)} {_f(x)} {_f(y - arm)}Z")
    body = (
        '<defs>'
        f'<linearGradient id="ring" x1="12" y1="12" x2="52" y2="52" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{IRIS_LIGHT}"/><stop offset="1" stop-color="{IRIS_DEEP}"/></linearGradient>'
        f'<radialGradient id="spark" cx="{_f(x)}" cy="{_f(y)}" r="{_f(arm)}" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{SPARK_CORE}"/><stop offset=".45" stop-color="{SPARK}"/></radialGradient>'
        + (f'<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1">'
           f'<stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/></linearGradient>'
           if tile else '')
        + '</defs>'
    )
    if tile:
        body += '<rect x="2" y="2" width="60" height="60" rx="14" fill="url(#tile)"/>'
        body += '<g transform="translate(32 32) scale(.7) translate(-32 -32)">'
    body += (f'<path d="M{_f(a.x())} {_f(a.y())}A{_f(RING_R)} {_f(RING_R)} 0 1 0 {_f(b.x())} {_f(b.y())}" '
             f'fill="none" stroke="url(#ring)" stroke-width="{_f(RING_STROKE)}" stroke-linecap="round"/>'
             f'<path d="{spark}" fill="url(#spark)"/>')
    if tile:
        body += '</g>'
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{body}</svg>\n'


# ── slovní značka: „Kairos“, kde prstenec je „o“ ─────────────────────────────

def wordmark_font(px):
    f = QFont(tokens.display_family())
    f.setPixelSize(round(px))
    f.setWeight(QFont.Weight.DemiBold)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 98)
    return f


def wordmark_layout(px):
    """Rozměry slovní značky pro písmo px: (šířka, výška, x „Kair“, x prstence, x „s“, strana prstence).
    Prstenec má výšku malých písmen + přesah, sedí na účaří jako „o“."""
    from PyQt6.QtGui import QFontMetricsF
    fm = QFontMetricsF(wordmark_font(px))
    w_kai = fm.horizontalAdvance("Kair")
    w_s = fm.horizontalAdvance("s")
    ring_side = fm.xHeight() * 1.18 * GRID / (2 * RING_R + RING_STROKE)   # vnější průměr ≈ výška „o“
    gap = fm.horizontalAdvance(" ") * 0.12
    ring_w = ring_side * (2 * RING_R + RING_STROKE) / GRID
    width = w_kai + gap + ring_w + gap + w_s
    return width, fm.height(), 0.0, w_kai + gap, w_kai + gap + ring_w + gap, ring_side, fm


def ring_rect_in_wordmark(origin, px):
    """Obdélník 64-mřížky loga, kterým „o“ ve slovní značce sedí na účaří (origin = levý okraj účaří)."""
    width, _h, _xk, x_ring, _xs, side, fm = wordmark_layout(px)
    ring_outer = side * (2 * RING_R + RING_STROKE) / GRID
    cy = origin.y() - fm.xHeight() / 2
    cx = origin.x() + x_ring + ring_outer / 2
    return QRectF(cx - side / 2, cy - side / 2, side, side)


def paint_wordmark(p, origin, px, color, letters=1.0, ring=1.0, spark=1.0, spread=0.0):
    """„Kair“ + prstenec + „s“ od origin (levý okraj, účaří). letters = průhlednost písmen,
    spread = o kolik px jsou písmena ještě přitažená k „o“ (splash je rozevírá)."""
    width, _h, x_kai, _xr, x_s, _side, _fm = wordmark_layout(px)
    if letters > 0:
        p.save()
        p.setFont(wordmark_font(px))
        c = QColor(color)
        c.setAlphaF(c.alphaF() * max(0.0, min(1.0, letters)))
        p.setPen(c)
        # i18n-ok: brand wordmark (the "o" is the ring)
        p.drawText(QPointF(origin.x() + x_kai + spread, origin.y()), "Kair")
        p.drawText(QPointF(origin.x() + x_s - spread, origin.y()), "s")
        p.restore()
    paint_mark(p, ring_rect_in_wordmark(origin, px), ring=ring, spark=spark)
    return width


ASSETS = ("kairos.ico", "kairos.svg", "kairos-mark.svg")


def write_assets(folder):
    """assets/: kairos.ico (16–256 px), kairos.svg (ikona s dlaždicí), kairos-mark.svg (samotné logo)."""
    import os
    os.makedirs(folder, exist_ok=True)
    build_ico(os.path.join(folder, "kairos.ico"))
    for name, tile in (("kairos.svg", True), ("kairos-mark.svg", False)):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(mark_svg(tile=tile))
    return [os.path.join(folder, n) for n in ASSETS]


class Wordmark(QWidget):
    """Slovní značka „Kairos“ s prstencem místo „o“ (O aplikaci); barva textu z tokenů."""

    def __init__(self, px=34, parent=None):
        super().__init__(parent)
        self._px = px
        self.setAccessibleName(APP_NAME)
        width, height = wordmark_layout(px)[:2]
        self.setFixedSize(round(width) + 4, round(height) + 4)

    def paintEvent(self, _):
        from ui_controls import palette_for
        from PyQt6.QtGui import QFontMetricsF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        ascent = QFontMetricsF(wordmark_font(self._px)).ascent()
        paint_wordmark(p, QPointF(2, 2 + ascent), self._px, tokens.qcolor(palette_for(self)["text"]))
        p.end()


class BrandMark(QWidget):
    """Hlavička postranního panelu: logo + název aplikace (barvy z tokenů, překreslí se s motivem)."""

    MARK = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("brandMark")
        self.setAccessibleName(APP_NAME)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(40)

    def sizeHint(self):
        return QSize(180, 40)

    def _mark_rect(self):
        x, cy = 10.0, self.height() / 2 - 3
        return QRectF(x, cy - self.MARK / 2, self.MARK, self.MARK)

    def spark_pos(self):
        """Střed jiskry loga v souřadnicích widgetu (výchozí bod SparkPulse)."""
        r = self._mark_rect()
        side = min(r.width(), r.height())
        c = spark_center()
        return QPointF(r.center().x() - side / 2 + c.x() * side / GRID,
                       r.center().y() - side / 2 + c.y() * side / GRID)

    def paintEvent(self, _):
        from ui_controls import palette_for
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        x = 10.0
        paint_mark(p, self._mark_rect())
        f = tokens.font("headline")
        f.setPixelSize(17)
        p.setFont(f)
        p.setPen(tokens.qcolor(t["text"]))
        p.drawText(QRectF(x + self.MARK + 9, 0, self.width() - x - self.MARK - 9, self.height() - 6),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), APP_NAME)
        p.end()


class SparkPulse(QWidget):
    """„Kairos Spark“: při přepnutí hlavní záložky vyletí z jiskry loga drobná kometa
    zvýrazňovací barvy (PULSE_MS) k ikoně záložky a tam krátce blikne.

    Průhledná vrstva nad postranním panelem: myš propouští, mimo animaci je skrytá (žádné
    kreslení ani časovač v klidu), jeden QVariantAnimation – nic neblokuje. Omezený pohyb
    (motion.reduced) = bez pulsu."""

    PULSE_MS = 180
    TAIL = 0.38                       # délka ohonu jako zlomek dráhy
    BOW = 22.0                        # vyboulení dráhy doprava (px), ať neletí po ikonách

    def __init__(self, host, mark, parent=None):
        super().__init__(parent or host)
        from PyQt6.QtCore import QVariantAnimation, QEasingCurve
        self._host, self._mark = host, mark
        self._a = self._b = QPointF()
        self._t = 0.0
        self.fired = 0                    # počet pulsů (testy)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(self.PULSE_MS)
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self.hide)

    def fire(self, target):
        """target = widget záložky; puls letí ke středu jeho ikony (levý okraj)."""
        import motion
        if target is None or motion.reduced() or not self._host.isVisible():
            return False
        self.setGeometry(self._host.rect())
        self._a = QPointF(self._mark.mapTo(self._host, self._mark.spark_pos().toPoint()))
        tr = target.geometry()
        top_left = target.parentWidget().mapTo(self._host, tr.topLeft()) if target.parentWidget() else tr.topLeft()
        self._b = QPointF(top_left.x() + 22, top_left.y() + tr.height() / 2)
        self.fired += 1
        self._anim.stop()
        self._t = 0.0
        self.show()
        self.raise_()
        self._anim.start()
        return True

    def is_running(self):
        from PyQt6.QtCore import QAbstractAnimation
        return self._anim.state() == QAbstractAnimation.State.Running

    def _step(self, v):
        self._t = float(v)
        self.update()

    def _at(self, t):
        a, b = self._a, self._b
        c = QPointF(max(a.x(), b.x()) + self.BOW, (a.y() + b.y()) / 2)
        u = 1.0 - t
        return QPointF(u * u * a.x() + 2 * u * t * c.x() + t * t * b.x(),
                       u * u * a.y() + 2 * u * t * c.y() + t * t * b.y())

    def paintEvent(self, _):
        t = self._t
        if t <= 0.0:
            return
        accent = tokens.qcolor(tokens.palette()["accent"])
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fade = 1.0 - max(0.0, (t - 0.85) / 0.15)       # konec: ohon pohasne
        steps, tail = 9, max(0.0, t - self.TAIL)
        prev = self._at(tail)
        for i in range(1, steps + 1):                   # ohon: tenký a průhledný -> hlava
            k = i / steps
            pt = self._at(tail + (t - tail) * k)
            c = QColor(accent)
            c.setAlphaF(min(1.0, 0.85 * k * fade))
            p.setPen(QPen(c, 0.6 + 2.0 * k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(prev, pt)
            prev = pt
        head = self._at(t)
        glow = QRadialGradient(head, 7.0)
        core = QColor(SPARK_CORE)
        core.setAlphaF(0.95 * fade)
        mid = QColor(accent)
        mid.setAlphaF(0.55 * fade)
        glow.setColorAt(0.0, core)
        glow.setColorAt(0.4, mid)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(head, 7.0, 7.0)
        if t > 0.7:                                     # dopad: krátký kroužek světla u ikony
            k = (t - 0.7) / 0.3
            ring = QColor(accent)
            ring.setAlphaF(0.5 * (1.0 - k))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(ring, 1.4))
            p.drawEllipse(self._b, 4 + 9 * k, 4 + 9 * k)
        p.end()


if __name__ == "__main__":          # python brand.py -> přegeneruje assets/ (volá i app.spec)
    import os
    import sys
    from PyQt6.QtGui import QGuiApplication
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    for path in write_assets(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")):
        print(path)
