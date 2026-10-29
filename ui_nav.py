"""Navigace uvnitř hlavního okna (místo samostatných oken a modálních dialogů).

NavigationController vkládá podstránky (prohlížeč, fotky osoby / události, kontrola
duplicit, ...) do QStackedWidget hlavního okna nad aktuální stránku. Při návratu
(‹ Zpět, Esc, Alt+←) obnoví předchozí stránku včetně posunutí všech scrollovacích
oblastí a fokusu – uživatel se vrátí přesně tam, kde byl.
"""
import weakref

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QAbstractScrollArea,
    QAbstractItemView,
)

from ui_controls import KButton


def capture_view_state(widget):
    """Posunutí všech scrollovacích oblastí uvnitř widgetu + widget s fokusem."""
    areas = []
    scrollables = widget.findChildren(QAbstractScrollArea)
    if isinstance(widget, QAbstractScrollArea):
        scrollables.insert(0, widget)        # stránka sama může být scrollovací oblast
    for area in scrollables:
        areas.append((weakref.ref(area),
                       area.horizontalScrollBar().value(),
                       area.verticalScrollBar().value()))
    focus = widget.focusWidget()
    return {"areas": areas, "focus": weakref.ref(focus) if focus is not None else None}


def restore_view_state(state):
    for ref, h, v in state.get("areas", []):
        area = ref()
        if area is None:
            continue
        if isinstance(area, QAbstractItemView):
            area.doItemsLayout()     # QListView rozkládá líně: bez toho by se rozsah scrollbaru oříznul na 0
        area.horizontalScrollBar().setValue(h)
        area.verticalScrollBar().setValue(v)
    focus_ref = state.get("focus")
    focus = focus_ref() if focus_ref else None
    if focus is not None and focus.isVisible():
        focus.setFocus(Qt.FocusReason.OtherFocusReason)


class _Entry:
    __slots__ = ("page", "previous", "state", "on_pop", "dispose")

    def __init__(self, page, previous, state, on_pop, dispose):
        self.page, self.previous, self.state = page, previous, state
        self.on_pop, self.dispose = on_pop, dispose


class NavigationController(QObject):
    depth_changed = pyqtSignal(int)

    def __init__(self, stack, parent=None):
        super().__init__(parent)
        self.stack = stack
        self._entries = []

    def depth(self):
        return len(self._entries)

    def current(self):
        return self._entries[-1].page if self._entries else self.stack.currentWidget()

    def root(self):
        """Kořenová stránka pod všemi podstránkami (pro postranní panel)."""
        return self._entries[0].previous if self._entries else self.stack.currentWidget()

    def contains(self, page):
        return any(e.page is page for e in self._entries)

    def previous(self, page):
        """Stránka, nad kterou byla page vložena (pro přechody prohlížeče), jinak None."""
        return next((e.previous for e in self._entries if e.page is page), None)

    def push(self, page, on_pop=None, dispose=True):
        """Zobrazí page nad aktuální stránkou. dispose=False: stránka se po návratu jen skryje
        (znovupoužitelný prohlížeč), jinak se zničí. on_pop() se zavolá po návratu."""
        if self.contains(page):          # už je v zásobníku -> vrátit se k ní
            while self._entries and self._entries[-1].page is not page:
                self.pop()
            return
        previous = self.stack.currentWidget()
        state = capture_view_state(previous) if previous is not None else {}
        if self.stack.indexOf(page) < 0:
            self.stack.addWidget(page)
        if hasattr(page, "back_requested"):
            page.back_requested.connect(self.pop)
        self._entries.append(_Entry(page, previous, state, on_pop, dispose))
        self.stack.setCurrentWidget(page)
        page.setFocus(Qt.FocusReason.OtherFocusReason)
        self.depth_changed.emit(len(self._entries))

    def pop(self):
        if not self._entries:
            return False
        entry = self._entries.pop()
        if hasattr(entry.page, "back_requested"):
            try:
                entry.page.back_requested.disconnect(self.pop)
            except TypeError:
                pass
        if entry.previous is not None and self.stack.indexOf(entry.previous) >= 0:
            self.stack.setCurrentWidget(entry.previous)
            restore_view_state(entry.state)
            # druhé kolo po dokončení layoutu (změna velikosti stránky při přepnutí)
            QTimer.singleShot(0, lambda s=entry.state: restore_view_state(s))
        if entry.dispose:
            self.stack.removeWidget(entry.page)
            entry.page.deleteLater()
        if entry.on_pop:
            entry.on_pop()
        self.depth_changed.emit(len(self._entries))
        return True

    def reset(self):
        """Postranní panel: zavře všechny podstránky (každá dostane svůj on_pop)."""
        while self._entries:
            self.pop()


class _AmbientHeader(QWidget):
    """Hlavička podstránky: pozadí ze stylu + volitelná záře odstínu zleva (Polish P4)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.ambient = None

    def paintEvent(self, event):
        if self.ambient is None:
            return
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width() * 0.6, 0)
        c = QColor(self.ambient)
        c.setAlpha(64)
        g.setColorAt(0.0, c)
        c.setAlpha(0)
        g.setColorAt(1.0, c)
        p.fillRect(self.rect(), g)
        line = QColor(self.ambient)              # tenká linka dole: záře „sedí“ na hraně
        line.setAlpha(110)
        lg = QLinearGradient(0, 0, self.width() * 0.5, 0)
        lg.setColorAt(0.0, line)
        line.setAlpha(0)
        lg.setColorAt(1.0, line)
        p.fillRect(0, self.height() - 1, self.width(), 1, lg)
        p.end()


class DetailPage(QWidget):
    """Podstránka v okně: hlavička s ‹ Zpět, titulkem, podtitulkem a akcemi + obsah."""
    back_requested = pyqtSignal()

    def __init__(self, title, subtitle="", back_text="‹ Zpět", parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = _AmbientHeader()
        self.header = header
        header.setObjectName("DetailHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header.setStyleSheet("QWidget#DetailHeader { background:#1C1C1E; border-bottom:1px solid #2C2C2E; }")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 8, 16, 8)
        hl.setSpacing(10)
        self.btn_back = KButton(back_text, variant="ghost")
        self.btn_back.setObjectName("btn_back")
        self.btn_back.setToolTip("Zpět (Esc)")
        self.btn_back.setStyleSheet("QPushButton#btn_back { font-size:14px; font-weight:600; }")
        self.btn_back.clicked.connect(self.back_requested.emit)
        hl.addWidget(self.btn_back)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet("font-size:17px; font-weight:bold; color:#F5F5F7; background:transparent;")
        self.lbl_subtitle = QLabel(subtitle)
        self.lbl_subtitle.setStyleSheet("font-size:11px; color:#8E8E93; background:transparent;")
        self.lbl_subtitle.setVisible(bool(subtitle))
        titles.addWidget(self.lbl_title)
        titles.addWidget(self.lbl_subtitle)
        hl.addLayout(titles)
        hl.addStretch(1)
        self.actions_layout = QHBoxLayout()
        self.actions_layout.setSpacing(8)
        hl.addLayout(self.actions_layout)
        root.addWidget(header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        root.addLayout(self.body, 1)

    def set_ambient(self, hex_color):
        """Kontextový odstín (tokens.context_glow) jako tichá záře v hlavičce; None = bez záře."""
        self.header.ambient = QColor(hex_color) if hex_color else None
        self.header.update()

    def set_title(self, title, subtitle=None):
        self.lbl_title.setText(title)
        if subtitle is not None:
            self.lbl_subtitle.setText(subtitle)
            self.lbl_subtitle.setVisible(bool(subtitle))

    def add_action(self, text, slot, tooltip=""):
        btn = KButton(text, compact=True)
        btn.clicked.connect(slot)
        if tooltip:
            btn.setToolTip(tooltip)
        self.actions_layout.addWidget(btn)
        return btn

    def set_content(self, widget, stretch=1):
        self.body.addWidget(widget, stretch)
        return widget

    def keyPressEvent(self, event):
        # Esc, který nespotřebuje obsah (mřížka nejdřív zruší výběr), znamená zpět
        if event.key() == Qt.Key.Key_Escape:
            self.back_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
