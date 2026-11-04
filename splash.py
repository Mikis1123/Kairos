"""Úvodní obrazovka Kairos (dávka 5): kometa oběhne prstenec, v mezeře škrtne jiskra
a prstenec se zmenší do „o“ slovní značky. Hraje, zatímco se na pozadí načítají těžké
knihovny (OpenCV, NumPy, databáze, AI moduly); okno aplikace se postaví až ve chvíli,
kdy značka stojí – zamrznutí GUI vlákna během stavby okna tak není vidět jako trhaná animace.

Spouští se z main.py (boot()); bez animací (Předvolby / Windows) se ukáže rovnou výsledná
značka. KAIROS_NO_SPLASH=1 spustí aplikaci bez úvodní obrazovky.
"""
from i18n import tr
import math
import sys
import threading

from PyQt6.QtCore import QEasingCurve, QEventLoop, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

import brand
import motion
import tokens

INTRO_MS = 1500
FADE_MS = 250
REVEAL_TIMEOUT_MS = 2200     # nejdéle tak dlouho čeká splash na první hotový snímek mřížky
WORDMARK_PX = 60
BIG_MARK = 148
CARD = (520, 300)
SHADOW = 28

# Těžké závislosti bez widgetů: načtou se ve vlákně, zatímco hraje animace
PRELOAD = ("numpy", "cv2", "PIL.Image", "pillow_heif", "database", "scanner", "ai_engine",
           "face_engine", "face_cluster")


def _ease_in_out(t):
    return QEasingCurve(QEasingCurve.Type.InOutCubic).valueForProgress(max(0.0, min(1.0, t)))


def _ease_out(t):
    return QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(max(0.0, min(1.0, t)))


def _ease_back(t):
    c = QEasingCurve(QEasingCurve.Type.OutBack)
    c.setOvershoot(2.2)
    return c.valueForProgress(max(0.0, min(1.0, t)))


def _phase(t, a, b):
    return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)


def _lerp_rect(a, b, k):
    return QRectF(a.x() + (b.x() - a.x()) * k, a.y() + (b.y() - a.y()) * k,
                  a.width() + (b.width() - a.width()) * k, a.height() + (b.height() - a.height()) * k)


class KairosSplash(QWidget):
    """Bezrámové okno s kartou; t (0–1) = průběh úvodní animace."""

    def __init__(self, mode=None):
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(brand.APP_NAME)
        self._t = 0.0
        self._status = ""
        self._dark = (mode or "dark") == "dark"
        self.resize(CARD[0] + 2 * SHADOW, CARD[1] + 2 * SHADOW)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._set_t)
        self._fade = None

    # ── průběh ──
    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def play(self):
        if motion.reduced():
            self._set_t(1.0)
            return
        self._anim.setDuration(INTRO_MS)
        self._anim.start()

    def intro_done(self):
        return self._t >= 1.0

    def set_status(self, text):
        self._status = text
        self.update()

    def wait_until(self, ready, timeout_ms=120000):
        """Vnořená smyčka událostí (animace běží), dokud nedohraje úvod a ready() nevrátí True."""
        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(30)
        waited = [0]

        def check():
            waited[0] += 30
            if self.intro_done() and not self._status and waited[0] > INTRO_MS + 400:
                self.set_status(tr("splash-preparing"))        # načítání trvá déle než úvod
            if (self.intro_done() and ready()) or waited[0] > timeout_ms:
                loop.quit()

        timer.timeout.connect(check)
        timer.start()
        loop.exec()
        timer.stop()

    def hand_off(self, window, first_frame, timeout_ms=None):
        """Founder audit: splash zůstane, dokud mřížka nenakreslí první hotový snímek
        (first_frame = signál firstFrameRendered), pak se prolne do okna. Pojistka: timeout."""
        timeout_ms = REVEAL_TIMEOUT_MS if timeout_ms is None else timeout_ms
        done = [False]

        def go():
            if done[0]:
                return
            done[0] = True
            try:
                first_frame.disconnect(go)
            except (TypeError, RuntimeError):
                pass
            self.finish(window)

        first_frame.connect(go)
        QTimer.singleShot(int(timeout_ms), go)
        return go

    def finish(self, window=None):
        """Okno aplikace je připravené: prolnutí (cross-dissolve, FADE_MS) – splash mizí, okno
        (pokud čekalo neviditelné s průhledností 0) se zároveň vynoří. Opakované volání nic nedělá."""
        if self._fade is not None or getattr(self, "_finished", False):
            return
        self._finished = True
        if window is not None:
            window.raise_()
            window.activateWindow()
        self.raise_()
        reveal = window is not None and window.windowOpacity() < 1.0
        ms = 0 if motion.reduced() else FADE_MS
        if ms <= 0:
            if reveal:
                window.setWindowOpacity(1.0)
            self.close()
            self.deleteLater()
            return
        curve = QEasingCurve(QEasingCurve.Type.InOutCubic)

        def step(v):
            k = curve.valueForProgress(float(v))
            self.setWindowOpacity(1.0 - k)
            if reveal:
                window.setWindowOpacity(max(0.0, min(1.0, k)))

        def done():
            if reveal:
                window.setWindowOpacity(1.0)        # 1.0 => Qt zruší WS_EX_LAYERED (plný výkon)
            self.close()
            self.deleteLater()

        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(ms)
        self._fade.valueChanged.connect(step)
        self._fade.finished.connect(done)
        self._fade.start()

    # ── kreslení ──
    def _card_rect(self):
        return QRectF(SHADOW, SHADOW, CARD[0], CARD[1])

    def _wordmark_origin(self):
        card = self._card_rect()
        width = brand.wordmark_layout(WORDMARK_PX)[0]
        return QPointF(card.center().x() - width / 2, card.center().y() + WORDMARK_PX * 0.22)

    def paintEvent(self, _):
        from ui_controls import smooth_rect_path
        t = self._t
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        card = self._card_rect()
        radius = 26

        for i, alpha in enumerate((34, 20, 10, 5)):                 # měkký stín pod kartou
            g = 4 + i * 6
            p.fillPath(smooth_rect_path(card.adjusted(-g, -g + 6, g, g + 8), radius + g),
                       QColor(0, 0, 0, alpha if self._dark else alpha // 2))
        bg_top, bg_bottom = (("#24242A", "#151518") if self._dark else ("#FFFFFF", "#EEEEF2"))
        from PyQt6.QtGui import QLinearGradient
        grad = QLinearGradient(card.topLeft(), card.bottomLeft())
        grad.setColorAt(0.0, QColor(bg_top))
        grad.setColorAt(1.0, QColor(bg_bottom))
        path = smooth_rect_path(card, radius)
        p.fillPath(path, grad)
        p.setPen(QPen(QColor(255, 255, 255, 22) if self._dark else QColor(0, 0, 0, 18), 1))
        p.drawPath(path)
        p.save()
        p.setClipPath(path)

        # 1) kometa oběhne prstenec (0–0.5), 2) jiskra škrtne (0.44–0.7), 3) značka se složí (0.62–1)
        sweep = _ease_in_out(_phase(t, 0.0, 0.50))
        fire = _phase(t, 0.44, 0.70)
        settle = _ease_in_out(_phase(t, 0.62, 0.90))

        big = QRectF(card.center().x() - BIG_MARK / 2, card.center().y() - BIG_MARK / 2 - 6, BIG_MARK, BIG_MARK)
        origin = self._wordmark_origin()
        small = brand.ring_rect_in_wordmark(origin, WORDMARK_PX)
        mark = _lerp_rect(big, small, settle)

        self._paint_glow(p, mark, sweep, settle)
        brand.paint_mark(p, mark, ring=sweep, spark=0.0)
        if 0 < sweep < 1:
            self._paint_comet(p, mark, sweep)
        if fire > 0:
            self._paint_spark(p, mark, fire)

        letters = _ease_out(_phase(t, 0.80, 1.0))
        if letters > 0:                       # písmena se rozevřou od „o“ do stran
            text = QColor("#F5F5F7" if self._dark else "#1C1C1E")
            brand.paint_wordmark(p, origin, WORDMARK_PX, text, letters=letters, ring=0.0, spark=0.0,
                                 spread=(1.0 - letters) * 22)

        if self._status:
            f = QFont(tokens.ui_family())
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(174, 174, 178) if self._dark else QColor(110, 110, 115))
            p.drawText(QRectF(card.left(), card.bottom() - 52, card.width(), 20),
                       int(Qt.AlignmentFlag.AlignCenter), self._status)
        p.restore()
        p.end()

    @staticmethod
    def _to_mark(mark, pt):
        """Bod v souřadnicích 64-mřížky loga -> okno."""
        k = mark.width() / brand.GRID
        return QPointF(mark.left() + pt.x() * k, mark.top() + pt.y() * k)

    def _paint_glow(self, p, mark, sweep, settle):
        """Jemná záře Iris za prstencem, sílí s oběhem komety a slábne při složení."""
        strength = sweep * (1.0 - 0.75 * settle)
        if strength <= 0:
            return
        c = mark.center()
        r = mark.width() * 0.75
        g = QRadialGradient(c, r)
        col = QColor(brand.IRIS_DEEP)
        col.setAlpha(round(70 * strength if self._dark else 40 * strength))
        g.setColorAt(0.0, col)
        col.setAlpha(0)
        g.setColorAt(1.0, col)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, r, r)

    def _paint_comet(self, p, mark, sweep):
        """Hlava komety na konci kresleného prstence + světlejší ohon za ní."""
        k = mark.width() / brand.GRID
        head_angle = brand.RING_START + brand.RING_SPAN * sweep
        head = self._to_mark(mark, brand._polar(head_angle, brand.RING_R))
        # ohon: posledních ~70° zesvětlené
        tail_span = min(70.0, brand.RING_SPAN * sweep)
        rect = QRectF(self._to_mark(mark, QPointF(brand.CENTER.x() - brand.RING_R, brand.CENTER.y() - brand.RING_R)),
                      self._to_mark(mark, QPointF(brand.CENTER.x() + brand.RING_R, brand.CENTER.y() + brand.RING_R)))
        steps = 14
        for i in range(steps):
            a0 = head_angle - tail_span * (i + 1) / steps
            pen = QPen(QColor(255, 255, 255, round(110 * (1 - i / steps) ** 2)), brand.RING_STROKE * k * 0.55)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, round(a0 * 16), round(tail_span / steps * 16) + 1)
        r = brand.RING_STROKE * k * 1.6
        g = QRadialGradient(head, r)
        g.setColorAt(0.0, QColor(255, 255, 255, 255))
        g.setColorAt(0.35, QColor(205, 210, 255, 200))
        g.setColorAt(1.0, QColor(139, 147, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(head, r, r)

    def _paint_spark(self, p, mark, fire):
        """Záblesk a paprsky v mezeře, jiskra naskočí s přetočením."""
        k = mark.width() / brand.GRID
        c = self._to_mark(mark, brand.spark_center())
        flash = math.sin(math.pi * min(1.0, fire * 1.6)) if fire < 0.625 else 0.0
        if flash > 0:
            r = 34 * k * (0.4 + fire)
            g = QRadialGradient(c, r)
            g.setColorAt(0.0, QColor(233, 255, 252, round(200 * flash)))
            g.setColorAt(0.4, QColor(79, 209, 197, round(90 * flash)))
            g.setColorAt(1.0, QColor(79, 209, 197, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(g)
            p.drawEllipse(c, r, r)
            for i in range(8):                                     # paprsky
                ang = math.radians(i * 45 + 22.5)
                inner, outer = 10 * k * (0.5 + fire), 26 * k * (0.4 + fire * 1.2)
                pen = QPen(QColor(233, 255, 252, round(170 * flash)), max(1.0, 1.4 * k))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(c.x() + inner * math.cos(ang), c.y() - inner * math.sin(ang)),
                           QPointF(c.x() + outer * math.cos(ang), c.y() - outer * math.sin(ang)))
        p.save()
        p.translate(mark.topLeft())
        p.scale(k, k)
        sc = brand.spark_center()
        p.translate(sc)
        p.rotate((1.0 - min(1.0, fire * 1.4)) * -90)               # dotočí se do svislé polohy
        p.translate(-sc)
        brand.paint_spark(p, _ease_back(fire))
        p.restore()


def _preload():
    import importlib
    for name in PRELOAD:
        try:
            importlib.import_module(name)
        except Exception:
            pass                       # chybu ukáže až skutečný import v main (s celou stopou)


def boot():
    """Vstupní bod z main.py: QApplication, splash, načtení na pozadí, pak main.main()."""
    import boot_trace
    from PyQt6 import QtWebEngineWidgets  # noqa: F401 – musí být načten před QApplication
    app = QApplication(sys.argv)
    app.setApplicationName(brand.APP_NAME)
    app.setOrganizationName(brand.APP_NAME)
    brand.set_windows_app_id()
    app.setWindowIcon(brand.app_icon())

    # Gate 0: data do AppData (jednorázová migrace) DŘÍV, než config / databáze sáhnou na disk
    import data_migration
    data_migration.ensure_ready()

    import theme
    from config import load_config
    cfg = load_config()
    import i18n
    i18n.init_from_config(cfg)            # Gate 1: úvodní obrazovka už v jazyce uživatele
    tokens.configure(cfg)
    motion.configure(cfg)

    splash = KairosSplash(theme.resolve(cfg.get("theme", theme.DEFAULT_THEME)))
    splash.show()
    splash.play()
    app.processEvents()
    boot_trace.mark("splash_shown")
    loader = threading.Thread(target=_preload, name="kairos-preload", daemon=True)
    loader.start()
    splash.wait_until(lambda: not loader.is_alive())
    splash.repaint()
    boot_trace.mark("preload_done")

    import main
    boot_trace.mark("main_imported")
    return main.main(app=app, splash=splash)
