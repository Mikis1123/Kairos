"""Neblokující plovoucí oznámení (toasty) místo informačních QMessageBoxů.

ToastManager se připojí k hostitelskému widgetu (hlavní okno) a kreslí toasty jako
překryvné děti dole uprostřed. Potvrzení zmizí samo po 3 s (pozastaví se pod myší),
chyby zůstávají, dokud je uživatel nezavře – neúspěšné uložení nesmí tiše zmizet.
"""
from i18n import tr
from PyQt6.QtCore import Qt, QObject, QTimer, QEvent, QPropertyAnimation, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QGraphicsOpacityEffect

import icons
import motion
import tokens

TOAST_TIMEOUT_MS = 3000
TOAST_MAX_VISIBLE = 3
TOAST_MARGIN = 24
TOAST_SPACING = 10

# Kairos dávka 3: barvy z tokenů (toast je vždy tmavý HUD), vektorový symbol, délky z motion
_KIND_TOKEN = {"info": "fill", "success": "success", "error": "danger"}
_KIND_ICON = {"info": "info", "success": "check", "error": "alert"}
_KIND_ACCENT = _KIND_TOKEN        # starší jméno (druhy toastů)


def _glyph_pixmap(kind, size=20):
    """Barevný kroužek s bílým symbolem (vykreslený pro hustotu pixelů obrazovky)."""
    t = tokens.palette("dark")
    dpr = icons._dpr()
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(tokens.qcolor(t[_KIND_TOKEN[kind]]))
    p.drawEllipse(0, 0, size, size)
    p.drawPixmap(3, 3, icons.pixmap(_KIND_ICON[kind], "#FFFFFF", size - 6, dpr))
    p.end()
    return pm


class Toast(QFrame):
    closed = pyqtSignal(object)

    def __init__(self, text, kind="info", action_text=None, on_action=None,
                 timeout_ms=TOAST_TIMEOUT_MS, parent=None, dismiss_text=None):
        super().__init__(parent)
        self.setProperty("omni_fixed_dark", True)     # tmavý „HUD“ i ve světlém režimu (theme.py)
        self.kind = kind if kind in _KIND_TOKEN else "info"
        self.text = text
        self._on_action = on_action
        self._closing = False
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        t = tokens.palette("dark")
        action = t["accent"] if self.kind == "info" else t[_KIND_TOKEN[self.kind]]
        self.setStyleSheet(f"""
            QFrame#Toast {{ background-color: rgba(44, 44, 46, 245);
                            border: 1px solid {t['hairline_strong']}; border-radius: {tokens.RADIUS['lg']}px; }}
            QLabel {{ background: transparent; color: {t['text']}; font-size: 13px; }}
            QLabel#ToastGlyph {{ background: transparent; }}
            QPushButton {{ background: transparent; border: none; color: {action};
                           font-size: 13px; font-weight: 600; padding: 2px 6px; }}
            QPushButton:hover {{ color: {t['text']}; }}
            QPushButton#ToastClose {{ color: {t['text_secondary']}; font-size: 14px; }}
            QPushButton#ToastDismiss {{ color: {t['text_secondary']}; font-weight: 500; }}
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 10, 9)
        row.setSpacing(10)
        glyph = QLabel()
        glyph.setObjectName("ToastGlyph")
        glyph.setFixedSize(20, 20)
        glyph.setPixmap(_glyph_pixmap(self.kind))
        row.addWidget(glyph)
        self.label = QLabel(text)
        # Zalamovat jen dlouhé zprávy – krátká hláška má zůstat na jednom řádku
        self.label.setWordWrap(len(text) > 70)
        self.label.setMaximumWidth(520)
        row.addWidget(self.label, 1)
        self.btn_action = None
        if action_text and on_action:
            self.btn_action = QPushButton(action_text)
            self.btn_action.setCursor(Qt.CursorShape.PointingHandCursor)
            self.btn_action.clicked.connect(self._run_action)
            row.addWidget(self.btn_action)
        # dismiss_text: zavření slovem („Přeskočit“) místo křížku – rozhodnutí, ne jen zavření
        self.btn_close = QPushButton(dismiss_text or "✕")
        self.btn_close.setObjectName("ToastDismiss" if dismiss_text else "ToastClose")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setAccessibleName(dismiss_text or tr("toast-close-a11y"))
        self.btn_close.clicked.connect(self.dismiss)
        row.addWidget(self.btn_close)
        self.setAccessibleName(text)

        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.dismiss)
        self.timeout_ms = timeout_ms   # None = zůstane, dokud se nezavře

    def popup(self):
        self.adjustSize()
        self.show()
        self.raise_()
        self._fade.stop()
        self._fade.setDuration(motion.duration("emphasized"))
        self._fade.setEasingCurve(motion.easing("emphasized"))
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(1.0)
        self._fade.start()
        if self.timeout_ms:
            self.timer.start(self.timeout_ms)

    def enterEvent(self, event):
        self.timer.stop()        # čtení pod myší nesmí přerušit odpočet
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self.timeout_ms and not self._closing:
            self.timer.start(1500)
        super().leaveEvent(event)

    def _run_action(self):
        callback, self._on_action = self._on_action, None
        self.dismiss()
        if callback:
            callback()

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        self.timer.stop()
        self._fade.stop()
        self._fade.setDuration(motion.duration("standard"))
        self._fade.setEasingCurve(motion.easing("standard"))
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self):
        self.hide()
        self.closed.emit(self)
        self.deleteLater()


class ToastManager(QObject):
    """Skládá toasty nad sebe dole uprostřed hostitele a drží je tam i při změně velikosti."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.toasts = []
        host.installEventFilter(self)

    def show(self, text, kind="info", action_text=None, on_action=None, timeout_ms=None, dismiss_text=None):
        if timeout_ms is None:
            timeout_ms = None if kind == "error" else TOAST_TIMEOUT_MS
        for t in self.toasts:
            if t.text == text and not t._closing:      # stejné hlášení se nezdvojuje
                if t.timeout_ms:
                    t.timer.start(t.timeout_ms)
                return t
        toast = Toast(text, kind, action_text, on_action, timeout_ms, self.host, dismiss_text)
        toast.closed.connect(self._on_closed)
        self.toasts.append(toast)
        while len(self.toasts) > TOAST_MAX_VISIBLE:
            old = self.toasts.pop(0)
            old.closed.disconnect(self._on_closed)
            old.dismiss()
        toast.popup()
        self._relayout()
        return toast

    def _on_closed(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
        self._relayout()

    def _relayout(self):
        y = self.host.height() - TOAST_MARGIN
        for toast in reversed(self.toasts):
            toast.adjustSize()
            w = min(toast.sizeHint().width(), max(200, self.host.width() - 2 * TOAST_MARGIN))
            toast.resize(w, toast.heightForWidth(w) if toast.hasHeightForWidth() else toast.sizeHint().height())
            y -= toast.height()
            toast.move((self.host.width() - w) // 2, y)
            toast.raise_()
            y -= TOAST_SPACING

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._relayout()
        return False


def show_toast(widget, text, kind="info", action_text=None, on_action=None, timeout_ms=None,
               dismiss_text=None):
    """Toast v okně, kam widget patří. Bez ToastManageru (samostatný widget v testech)
    se hlášení jen vypíše – nikdy nespadne a nikdy neotevře modální okno."""
    window = widget.window() if widget is not None else None
    manager = getattr(window, "toasts", None)
    if isinstance(manager, ToastManager):
        return manager.show(text, kind, action_text, on_action, timeout_ms, dismiss_text)
    print(f"[{kind}] {text}")
    return None
