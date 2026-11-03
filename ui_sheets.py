"""Listy v okně (dávka 3): dotazy a potvrzení jako překryv nad hlavním oknem místo
samostatných systémových dialogů. Pozadí se ztlumí, karta přijede pružinou zdola.

InWindowDialog má stejné volání jako QInputDialog (getText / getItem → (hodnota, ok)), takže
stačí vyměnit jméno třídy. Čeká lokální smyčkou událostí jako QDialog.exec(): klávesové
zkratky okna pod listem jsou mezitím blokované, Esc = Zrušit, Enter = potvrdit.
"""
from i18n import tr
from PyQt6.QtCore import QEvent, QEventLoop, QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QLineEdit, QVBoxLayout, QWidget)

import motion
import tokens
from ui_controls import KButton, palette_for

CARD_WIDTH = 420
RISE_PX = 18


class _ShortcutBlocker(QObject):
    """Během listu nesmí zkratky okna pod ním (Esc = zpět, Delete, …) nic spustit."""

    def __init__(self, sheet):
        super().__init__(sheet)
        self.sheet = sheet

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Shortcut, QEvent.Type.ShortcutOverride) and isinstance(obj, QWidget) \
                and not self.sheet.isAncestorOf(obj) and obj is not self.sheet:
            return event.type() == QEvent.Type.Shortcut
        return False


class _SheetBase(QWidget):
    """Překryv přes celé okno: ztlumené pozadí, měkký stín a karta, která přijede pružinou.
    Podtřídy naplní self.card a určí její obdélník (_card_rect)."""
    done = pyqtSignal(bool)
    RISE = RISE_PX

    def __init__(self, host, card_name="sheetCard"):
        window = host.window() if host is not None else None
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.result = False
        self._closing = False
        self._finished = False
        self.card = QFrame(self)
        self.card.setObjectName(card_name)
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._effect = QGraphicsOpacityEffect(self.card)
        self.card.setGraphicsEffect(self._effect)
        self._progress = motion.Tween(0.0, self._step, self)
        self._progress.finished.connect(self._on_tween_done)
        self._blocker = _ShortcutBlocker(self)
        if window is not None:
            window.installEventFilter(self)
        self.hide()

    # ── rozvržení a kreslení ──
    def _card_rect(self):
        """(x, y, w, h) karty v klidu."""
        raise NotImplementedError

    def _step(self, v):
        v = float(v)
        x, y, w, h = self._card_rect()
        self.card.setGeometry(x, y + round(self.RISE * (1.0 - v)), w, h)
        # v klidu bez efektu: velký obsah (Předvolby) se pak posouvá bez kreslení přes pixmapu
        self._effect.setEnabled(not (v >= 1.0 and not self._closing))
        self._effect.setOpacity(max(0.0, min(1.0, v)))
        self.update()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            self._step(self._progress.value)
        return False

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        v = max(0.0, min(1.0, float(self._progress.value)))
        scrim = tokens.qcolor(t["scrim"])
        scrim.setAlphaF(scrim.alphaF() * v)
        p.fillRect(self.rect(), scrim)
        # měkký stín pod kartou (efekt průhlednosti a stín nejdou na jednom widgetu)
        r = QRectF(self.card.geometry())
        for i, alpha in enumerate((26, 18, 10, 5)):
            grow = 4 + i * 5
            shadow = tokens.qcolor(t["shadow"])
            shadow.setAlpha(round(alpha * v))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shadow)
            p.drawRoundedRect(r.adjusted(-grow, -grow + 6, grow, grow + 10), 20 + grow, 20 + grow)
        p.end()

    # ── otevření / zavření ──
    def _initial_focus(self):
        return None

    def open(self):
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.show()
        self.raise_()
        QApplication.instance().installEventFilter(self._blocker)
        self._step(0.0)
        self._progress.to(1.0, "spring")
        focus = self._initial_focus()
        if focus is not None:
            focus.setFocus(Qt.FocusReason.OtherFocusReason)

    def accept(self):
        self._close(True)

    def reject(self):
        self._close(False)

    def _close(self, result):
        if self._closing:
            return
        self._closing = True
        self.result = result
        QApplication.instance().removeEventFilter(self._blocker)
        if self._progress.value == 0.0 and not self._progress.is_running():
            self._finish()
        else:
            self._effect.setEnabled(True)
            self._progress.to(0.0, "standard")

    def _on_tween_done(self):
        if self._closing and self._progress.value == 0.0:
            self._finish()

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        self.hide()
        self.done.emit(self.result)
        self.deleteLater()

    def mousePressEvent(self, e):
        e.accept()                        # klik mimo kartu nic nespustí (list je modální)

    def exec(self):
        """Ukáže list a počká na odpověď (lokální smyčka událostí). Vrací True = potvrzeno."""
        loop = QEventLoop()
        answer = []
        self.done.connect(lambda ok: (answer.append(ok), loop.quit()))
        self.open()
        if not answer:
            loop.exec()
        return bool(answer and answer[0])


class Sheet(_SheetBase):
    """Dotaz / potvrzení: titulek, text, obsah, tlačítka."""

    def __init__(self, host, title, body="", action_text=None, cancel_text=None, destructive=False):
        action_text = action_text or tr("action-ok")
        cancel_text = cancel_text or tr("action-cancel")
        super().__init__(host)
        self.card.setFixedWidth(CARD_WIDTH)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(8)
        self.lbl_title = QLabel(title)
        self.lbl_title.setObjectName("sheetTitle")
        self.lbl_title.setWordWrap(True)
        lay.addWidget(self.lbl_title)
        self.lbl_body = QLabel(body)
        self.lbl_body.setObjectName("sheetBody")
        self.lbl_body.setWordWrap(True)
        self.lbl_body.setVisible(bool(body))
        lay.addWidget(self.lbl_body)
        self.content = QVBoxLayout()
        self.content.setSpacing(8)
        lay.addLayout(self.content)
        lay.addSpacing(6)
        row = QHBoxLayout()
        row.addStretch()
        self.btn_cancel = KButton(cancel_text, self.card)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_action = KButton(action_text, self.card, variant="danger" if destructive else "primary")
        self.btn_action.clicked.connect(self.accept)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_action)
        lay.addLayout(row)

    def _card_rect(self):
        self.card.adjustSize()
        w, h = self.card.width(), self.card.sizeHint().height()
        return (self.width() - w) // 2, max(16, (self.height() - h) // 2 - 20), w, h

    def _initial_focus(self):
        return next((w for w in self.card.findChildren(QWidget)
                     if isinstance(w, (QLineEdit, QComboBox))), self.btn_action)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
        elif e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.btn_action.isEnabled():
            self.accept()
        else:
            super().keyPressEvent(e)


PANEL_MAX = (720, 820)       # Předvolby jako list v okně: nejvýš tak velká karta (logické px)
PANEL_MARGIN = 40


class PanelSheet(_SheetBase):
    """Founder audit: velký list v okně pro celé obrazovky (Předvolby) místo samostatného
    systémového okna. Obsah (widget) vyplní kartu; klik do ztlumeného pozadí = request_close
    (obsah si ho může přesměrovat, např. na Zrušit)."""
    RISE = 28

    def __init__(self, host, content, max_size=PANEL_MAX):
        super().__init__(host, card_name="panelCard")
        self._max = max_size
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(1, 1, 1, 1)       # vlasová hrana karty zůstane vidět
        lay.setSpacing(0)
        self.content_widget = content
        content.setParent(self.card)
        lay.addWidget(content)
        self.request_close = self.reject

    def _card_rect(self):
        w = max(320, min(self._max[0], self.width() - 2 * PANEL_MARGIN))
        h = max(320, min(self._max[1], self.height() - 2 * PANEL_MARGIN))
        return (self.width() - w) // 2, max(16, (self.height() - h) // 2), w, h

    def mousePressEvent(self, e):
        if not self.card.geometry().contains(e.position().toPoint()):
            self.request_close()                  # klik vedle karty = Zrušit (jako Esc)
        e.accept()


class InWindowDialog:
    """Náhrada QInputDialog / potvrzovacích oken – stejné návratové hodnoty, list v okně."""

    @staticmethod
    def getText(parent, title, label, text="", action_text=None):
        sheet = Sheet(parent, title, label, action_text or tr("action-save"))
        edit = QLineEdit(text)
        edit.selectAll()
        edit.returnPressed.connect(sheet.accept)
        sheet.content.addWidget(edit)
        value = []
        sheet.done.connect(lambda _ok: value.append(edit.text()))   # dřív, než se list smaže
        ok = sheet.exec()
        return (value[0] if value else text), ok

    @staticmethod
    def getItem(parent, title, label, items, current=0, editable=False, action_text=None):
        sheet = Sheet(parent, title, label, action_text or tr("action-choose"))
        combo = QComboBox()
        combo.addItems(list(items))
        combo.setCurrentIndex(max(0, min(current, combo.count() - 1)))
        combo.setEditable(bool(editable))
        sheet.content.addWidget(combo)
        value = []
        sheet.done.connect(lambda _ok: value.append(combo.currentText()))
        ok = sheet.exec()
        return (value[0] if value else ""), ok

    @staticmethod
    def confirm(parent, title, body, action_text=None, destructive=True):
        return Sheet(parent, title, body, action_text or tr("action-continue"), destructive=destructive).exec()
