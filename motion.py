"""Pohybový engine Kairos (dávka 2): délky a křivky z tokenů, omezení animací, Tween.

Pravidla: animace běží jen jako odezva na interakci (najetí, stisk, přepnutí) a po doběhnutí
se zastaví – žádné tikání na pozadí. Při omezeném pohybu (Windows: Nastavení › Přístupnost ›
Vizuální efekty › Animační efekty vypnuté, nebo volba v Předvolbách) se hodnota nastaví hned.
"""
import sys

from PyQt6.QtCore import QAbstractAnimation, QEasingCurve, QObject, QVariantAnimation, pyqtSignal

import tokens

_user_reduce = False        # Předvolby › Omezit animace
_system_cache = None        # stav Windows (zjišťuje se jednou, obnoví refresh_system())


def system_animations_enabled():
    """Windows: SPI_GETCLIENTAREAANIMATION (Animační efekty). Jinde vždy True."""
    global _system_cache
    if _system_cache is None:
        _system_cache = True
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                value = wintypes.BOOL(True)
                if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
                    _system_cache = bool(value.value)
            except Exception:
                pass
    return _system_cache


def refresh_system():
    global _system_cache
    _system_cache = None
    return system_animations_enabled()


def configure(cfg):
    global _user_reduce
    _user_reduce = bool(cfg.get("reduce_motion", False))


def set_reduce_motion(value):
    global _user_reduce
    _user_reduce = bool(value)


def reduced():
    return _user_reduce or not system_animations_enabled()


def duration(kind="standard"):
    """Délka v ms podle tokens.MOTION; při omezeném pohybu 0."""
    return 0 if reduced() else tokens.MOTION[kind][0]


def easing(kind="standard"):
    curve = QEasingCurve(getattr(QEasingCurve.Type, tokens.MOTION[kind][1]))
    if kind == "spring":
        curve.setOvershoot(1.3)            # jemné přetočení, ne poskakování
    return curve


class Tween(QObject):
    """Animovaná hodnota (float, QColor, QRect, QRectF, QPointF …).

    to() navazuje plynule z aktuální hodnoty; na_change(value) se volá při každém kroku.
    Skrytý widget, omezený pohyb nebo nulová změna = hodnota se nastaví okamžitě.
    finished se vysílá po dosažení cíle (i okamžitém)."""
    finished = pyqtSignal()

    def __init__(self, value, on_change, owner=None):
        super().__init__(owner)
        self.value = value
        self.target = value
        self._owner = owner
        self._on_change = on_change
        self._anim = QVariantAnimation(self)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self._done)

    def _done(self):
        # Widget (a s ním Tween) se může rušit uprostřed animace; výjimka ze slotu by v PyQt6
        # znamenala qFatal (pád 0xC0000409) – umírající Tween proto mlčí.
        try:
            self.finished.emit()
        except (RuntimeError, AttributeError):
            pass

    def _step(self, v):
        self.value = v
        try:
            self._on_change(v)
        except RuntimeError:            # vlastník už je zrušený (wrapped C/C++ object deleted)
            self._anim.stop()

    def is_running(self):
        return self._anim.state() == QAbstractAnimation.State.Running

    def set(self, value):
        self._anim.stop()
        self.target = value
        if value != self.value:
            self.value = value
            self._on_change(value)
        self._done()

    def to(self, value, kind="standard"):
        if value == self.target and (self.is_running() or value == self.value):
            return
        self.target = value
        ms = duration(kind)
        owner_hidden = self._owner is not None and hasattr(self._owner, "isVisible") and not self._owner.isVisible()
        if ms <= 0 or owner_hidden:
            self.set(value)
            return
        self._anim.stop()
        self._anim.setStartValue(self.value)
        self._anim.setEndValue(value)
        self._anim.setDuration(ms)
        self._anim.setEasingCurve(easing(kind))
        self._anim.start()
