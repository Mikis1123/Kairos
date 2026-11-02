"""Paleta příkazů (Ctrl + K) – Sprint 2.

Jedno pole pro všechno: stránky, kolekce, alba, osoby, události, akce aplikace a jako poslední
řádek vždy „Hledat … v knihovně“ (chytré hledání podle obsahu). Shoda je bez diakritiky
a velikosti písmen, po slovech i po písmenech (fuzzy): „ost“ najde Ostravu, „jn ns“ Jana Nováka.

Paleta nic neví o aplikaci: položky dodá provider() (main.py) jako seznam Command.
"""
from i18n import N_, tr
import unicodedata
from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QEvent, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QLineEdit, QListWidget,
                             QListWidgetItem, QStyledItemDelegate, QVBoxLayout, QWidget)

import icons
import motion
import tokens
from ui_controls import palette_for

CARD_WIDTH = 600
MAX_ROWS = 9
ROW_H = 44
KIND_LABELS = {"page": N_("palette-kind-page"), "album": N_("palette-kind-album"), "person": N_("palette-kind-person"),
               "event": N_("palette-kind-event"), "action": N_("palette-kind-action"), "search": N_("palette-kind-search")}
KIND_WEIGHT = {"page": 0.06, "action": 0.04, "album": 0.03, "person": 0.02, "event": 0.0, "search": 0.0}


@dataclass
class Command:
    title: str
    run: Callable[[], None]
    kind: str = "action"
    subtitle: str = ""
    icon: str = "sparkle"
    keywords: str = ""
    _fold: str = field(default="", repr=False)


def fold(text):
    """Bez diakritiky, malá písmena: 'Žluťoučký Kůň' -> 'zlutoucky kun'."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def score(query, text):
    """0 = neshoda, jinak vyšší = lepší. Prefix celého textu > začátek slova > podřetězec >
    zkratka po písmenech (každé slovo dotazu musí najít své místo)."""
    q, t = fold(query).split(), fold(text)
    if not q:
        return 0.0
    words = t.split()
    total = 0.0
    for part in q:
        if t.startswith(part):
            s = 1.0
        elif any(w.startswith(part) for w in words):
            s = 0.85
        elif part in t:
            s = 0.6
        else:                                    # písmena v pořadí (zkratka), hustěji = lépe
            pos, first, last = -1, None, None
            for ch in part:
                pos = t.find(ch, pos + 1)
                if pos < 0:
                    return 0.0
                first = pos if first is None else first
                last = pos
            span = last - first + 1
            s = 0.35 * len(part) / span if span else 0.0
        total += s
    # kratší cíl = přesnější shoda (stejná slova v kratším názvu vyhrají)
    return total / len(q) + 0.05 * min(1.0, len(" ".join(q)) / max(1, len(t)))


def rank(query, commands, limit=MAX_ROWS - 1):
    """Seřazené příkazy pro dotaz; prázdný dotaz = výchozí pořadí (stránky, akce)."""
    if not query.strip():
        return [c for c in commands if c.kind in ("page", "action")][:limit]
    scored = []
    for i, c in enumerate(commands):
        s = max(score(query, c.title), 0.9 * score(query, c.keywords) if c.keywords else 0.0)
        if s > 0:
            scored.append((s + KIND_WEIGHT.get(c.kind, 0.0), -i, c))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [c for _, _, c in scored[:limit]]


class _RowDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(CARD_WIDTH - 40, ROW_H)

    def paint(self, p, option, index):
        cmd = index.data(Qt.ItemDataRole.UserRole)
        if cmd is None:
            return
        t = palette_for(option.widget)
        r = QRectF(option.rect).adjusted(6, 2, -6, -2)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = index.row() == option.widget.currentRow()
        if selected:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tokens.qcolor(t["accent_soft"]))
            p.drawRoundedRect(r, 9, 9)
        color = tokens.qcolor(t["accent"] if selected else t["text_secondary"])
        p.drawPixmap(int(r.left() + 12), int(r.center().y() - 9), icons.pixmap(cmd.icon, color, 18))
        f = QFont(tokens.ui_family())
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(tokens.qcolor(t["text"]))
        x = r.left() + 42
        kind = KIND_LABELS.get(cmd.kind, "")
        kind = tr(kind) if kind else ""
        fk = QFont(f)
        fk.setPixelSize(11)
        kind_w = 80
        title_w = r.width() - 42 - kind_w - 12
        fm = p.fontMetrics()
        title = fm.elidedText(cmd.title, Qt.TextElideMode.ElideRight, int(title_w))
        p.drawText(QRectF(x, r.top(), title_w, r.height() if not cmd.subtitle else r.height() * 0.6),
                   int(Qt.AlignmentFlag.AlignLeft | (Qt.AlignmentFlag.AlignVCenter if not cmd.subtitle
                                                     else Qt.AlignmentFlag.AlignBottom)), title)
        if cmd.subtitle:
            fs = QFont(f)
            fs.setPixelSize(12)
            p.setFont(fs)
            p.setPen(tokens.qcolor(t["text_secondary"]))
            p.drawText(QRectF(x, r.top() + r.height() * 0.58, title_w, r.height() * 0.42),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
                       p.fontMetrics().elidedText(cmd.subtitle, Qt.TextElideMode.ElideRight, int(title_w)))
        p.setFont(fk)
        p.setPen(tokens.qcolor(t["text_tertiary"]))
        p.drawText(QRectF(r.right() - kind_w - 12, r.top(), kind_w, r.height()),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), kind)
        p.restore()


class CommandPalette(QWidget):
    """Překryv přes okno: ztlumené pozadí, karta s polem a výsledky u horního okraje."""
    closed = pyqtSignal()

    def __init__(self, window, provider, search_command):
        super().__init__(window)
        self.provider = provider               # () -> [Command]
        self.search_command = search_command   # (dotaz) -> Command „Hledat … v knihovně“
        self.commands = []
        self.card = QFrame(self)
        self.card.setObjectName("sheetCard")
        self.card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.card.setFixedWidth(CARD_WIDTH)
        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("palette-placeholder"))
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumHeight(40)
        self.edit.textChanged.connect(self._refresh)
        self.edit.installEventFilter(self)
        lay.addWidget(self.edit)
        self.list = QListWidget()
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setItemDelegate(_RowDelegate(self.list))
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setStyleSheet("QListWidget { background: transparent; }")
        self.list.itemClicked.connect(lambda it: self._run(it.data(Qt.ItemDataRole.UserRole)))
        self.list.currentRowChanged.connect(lambda _r: self.list.viewport().update())
        lay.addWidget(self.list)
        self._effect = QGraphicsOpacityEffect(self.card)
        self.card.setGraphicsEffect(self._effect)
        self._progress = motion.Tween(0.0, self._step, self)
        self._progress.finished.connect(self._on_tween_done)
        self._closing = False
        window.installEventFilter(self)
        self.hide()

    # ── data ──
    def open(self, text=""):
        try:
            self.commands = list(self.provider())
        except Exception as e:                   # paleta se otevře i s neúplnými daty
            print(f"[Paleta] {e}")
            self.commands = []
        self.setGeometry(self.parentWidget().rect())
        self._closing = False
        self.show()
        self.raise_()
        self.edit.setText(text)
        self._refresh(text)
        self.edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.edit.selectAll()
        self._progress.to(1.0, "standard")

    def visible_commands(self):
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def _refresh(self, text=None):
        text = self.edit.text() if text is None else text
        found = rank(text, self.commands)
        if text.strip():
            found.append(self.search_command(text.strip()))
        self.list.clear()
        for c in found:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, c)
            it.setSizeHint(QSize(CARD_WIDTH - 40, ROW_H))
            self.list.addItem(it)
        self.list.setFixedHeight(min(MAX_ROWS, max(1, len(found))) * ROW_H + 2 * self.list.frameWidth() + 8)
        self.list.setVisible(bool(found))
        if found:
            self.list.setCurrentRow(0)
        self._step(self._progress.value)

    def _run(self, cmd):
        if cmd is None:
            return
        self.close_palette()
        try:
            cmd.run()
        except Exception as e:
            print(f"[Paleta] {cmd.title}: {e}")

    def run_current(self):
        it = self.list.currentItem()
        self._run(it.data(Qt.ItemDataRole.UserRole) if it else None)

    # ── klávesnice ──
    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self.setGeometry(obj.rect())
            self._step(self._progress.value)
        elif obj is self.edit and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.run_current()
                return True
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp, Qt.Key.Key_Tab):
                n = self.list.count()
                if n:
                    step = -1 if key in (Qt.Key.Key_Up, Qt.Key.Key_PageUp) else 1
                    self.list.setCurrentRow((self.list.currentRow() + step) % n)
                return True
        return False

    def mousePressEvent(self, e):
        if not self.card.geometry().contains(e.position().toPoint()):
            self.close_palette()
            return
        super().mousePressEvent(e)

    def close_palette(self):
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        self._progress.to(0.0, "standard")

    def _on_tween_done(self):
        if self._closing:
            self.hide()
            self._closing = False
            self.closed.emit()

    # ── kreslení ──
    def _step(self, v):
        v = float(v)
        self.card.adjustSize()
        h = self.card.sizeHint().height()
        x = (self.width() - CARD_WIDTH) // 2
        y = max(24, int(self.height() * 0.14))
        self.card.setGeometry(x, y - round(10 * (1.0 - v)), CARD_WIDTH, h)
        self._effect.setOpacity(max(0.0, min(1.0, v)))
        self.update()

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        v = max(0.0, min(1.0, float(self._progress.value)))
        scrim = tokens.qcolor(t["scrim"])
        scrim.setAlphaF(scrim.alphaF() * v * 0.8)
        p.fillRect(self.rect(), scrim)
        r = QRectF(self.card.geometry())
        for i, alpha in enumerate((26, 18, 10, 5)):
            grow = 4 + i * 5
            shadow = QColor(tokens.qcolor(t["shadow"]))
            shadow.setAlpha(round(alpha * v))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(shadow)
            p.drawRoundedRect(r.adjusted(-grow, -grow + 6, grow, grow + 10), 20 + grow, 20 + grow)
        p.end()


def install(window, provider, search_command, shortcut="Ctrl+K"):
    """Zkratka v okně; vrací paletu (pro testy a další vstupy, např. menu)."""
    from PyQt6.QtGui import QKeySequence, QShortcut
    palette = CommandPalette(window, provider, search_command)
    sc = QShortcut(QKeySequence(shortcut), window)
    sc.setContext(Qt.ShortcutContext.WindowShortcut)
    sc.activated.connect(lambda: palette.open())
    palette._shortcut = sc
    return palette
