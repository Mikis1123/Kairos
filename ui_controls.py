"""Kreslené ovládací prvky Kairos (dávka 2): tlačítko, položka postranního panelu s jezdcem,
segmentový přepínač a vypínač. Barvy z tokens.palette, pohyb z motion.Tween.

Všechny prvky kreslí samy sebe (styly QSS neumí přechody) a animují jen při interakci.
API zůstává Qt: KButton je QPushButton, položky jsou zaškrtávací tlačítka v QButtonGroup.
"""
import re

from PyQt6.QtCore import QObject, QPointF, QRect, QRectF, QSize, Qt, pyqtSignal, QEvent
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QAbstractButton, QButtonGroup, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QVBoxLayout, QWidget)

import icons
import motion
import theme
import tokens

_KEYBOARD_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                     Qt.FocusReason.ShortcutFocusReason)


def palette_for(widget):
    """Tokeny pro widget – uvnitř prohlížeče / prezentace vždy tmavé."""
    return tokens.palette("dark" if theme.is_fixed_dark(widget) else theme.current())


def _c(t, name):
    return tokens.qcolor(t[name])


def _rounded(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    return path


def _focus_ring(p, t, rect, radius):
    p.setPen(QPen(_c(t, "focus_ring"), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), radius, radius)


class _Interactive:
    """Společné najetí / stisk / fokus z klávesnice pro kreslené prvky."""

    def _init_interaction(self):
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)
        self._press = motion.Tween(0.0, lambda v: self.update(), self)
        self._kbd_focus = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, e):
        if self.isEnabled():
            self._hover.to(1.0, "micro")
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._press.to(1.0, "micro")
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self._press.to(0.0, "spring")
        super().mouseReleaseEvent(e)

    def focusInEvent(self, e):
        self._kbd_focus = e.reason() in _KEYBOARD_REASONS
        self.update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._kbd_focus = False
        self.update()
        super().focusOutEvent(e)

    def changeEvent(self, e):
        if e.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._hover.set(0.0)
            self._press.set(0.0)
        super().changeEvent(e)


# ── tlačítko ──────────────────────────────────────────────────────────────────
class KButton(_Interactive, QPushButton):
    """QPushButton s plynulým najetím, stiskem na 97 % a prstencem fokusu z klávesnice.

    Varianta: primary | secondary | ghost | danger | danger_outline | warning_outline.
    Bez explicitní varianty se odvodí z objectName (btn_primary, btnSave, btnReset, …)."""

    OBJECT_VARIANTS = {"btn_primary": "primary", "btnSave": "primary", "btn_danger": "danger",
                       "btnDanger": "danger", "btnReset": "danger_outline", "btnCleanCache": "warning_outline"}

    ICON_SIZE = 16

    def __init__(self, text="", parent=None, variant=None, compact=False, icon=None):
        super().__init__(text, parent)
        self._variant = variant
        self._compact = compact
        self._icon_name = icon          # vektorová ikona (icons.py) obarvená barvou textu
        self._init_interaction()

    def setIconName(self, name):
        self._icon_name = name
        self.updateGeometry()
        self.update()

    def iconName(self):
        return self._icon_name

    def variant(self):
        return self._variant or self.OBJECT_VARIANTS.get(self.objectName(), "secondary")

    def setVariant(self, variant):
        self._variant = variant
        self.update()

    def sizeHint(self):
        fm = self.fontMetrics()
        w = fm.horizontalAdvance(self.text()) + (24 if self._compact else 30)   # +rezerva na zaokrouhlení
        if self._icon_name:
            w += self.ICON_SIZE + (6 if self.text() else 0)
        elif not self.icon().isNull():
            w += self.iconSize().width() + (6 if self.text() else 0)
        h = 28 if self._compact else 32
        if not self.text():
            return QSize(h, h)              # čistě ikonové tlačítko = čtverec
        return QSize(max(w, 56), h)

    def minimumSizeHint(self):
        return self.sizeHint()

    def _colors(self, t):
        """(pozadí, pozadí při najetí, pozadí při stisku, text, okraj)."""
        clear = QColor(0, 0, 0, 0)
        v = self.variant()
        if v == "primary":
            return _c(t, "fill"), _c(t, "fill_hover"), _c(t, "fill_pressed"), _c(t, "on_accent"), clear
        if v == "danger":
            base = _c(t, "danger")
            return base, tokens.mix(base, QColor("#FFFFFF"), .14), tokens.mix(base, QColor("#000000"), .12), \
                QColor("#FFFFFF"), clear
        if v == "danger_outline":
            return clear, _c(t, "danger_soft"), _c(t, "danger_soft"), _c(t, "danger_text"), _c(t, "danger_border")
        if v == "warning_outline":
            return clear, _c(t, "warning_soft"), _c(t, "warning_soft"), _c(t, "warning_text"), _c(t, "warning_border")
        if v == "ghost":
            return clear, _c(t, "accent_soft"), _c(t, "accent_soft"), _c(t, "accent"), clear
        if self.isChecked():
            return _c(t, "surface_3"), _c(t, "surface_3"), _c(t, "surface_pressed"), _c(t, "text"), _c(t, "hairline")
        return _c(t, "surface_2"), _c(t, "surface_3"), _c(t, "surface_pressed"), _c(t, "text"), _c(t, "hairline")

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bg, bg_hover, bg_press, fg, border = self._colors(t)
        if not self.isEnabled():
            bg = _c(t, "surface") if bg.alpha() else bg
            fg, border = _c(t, "text_tertiary"), _c(t, "hairline")
        press = max(0.0, min(1.0, self._press.value))
        fill = tokens.mix(tokens.mix(bg, bg_hover, self._hover.value), bg_press, press * .7)

        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        scale = 1.0 - 0.03 * self._press.value            # pružina po puštění lehce přetočí
        p.translate(rect.center())
        p.scale(scale, scale)
        p.translate(-rect.center())
        radius = tokens.RADIUS["sm"]
        path = _rounded(rect, radius)
        p.fillPath(path, fill)
        if border.alpha():
            p.setPen(QPen(border, 1))
            p.drawPath(path)
        if self.variant() == "primary" and self.isEnabled():
            p.setPen(QPen(QColor(255, 255, 255, 40), 1))     # jemné horní světlo výplně
            p.drawLine(QPointF(rect.left() + radius, rect.top() + 1), QPointF(rect.right() - radius, rect.top() + 1))
        if self._kbd_focus:
            _focus_ring(p, t, rect, radius)

        p.setPen(fg)
        p.setFont(self.font())
        text_rect = rect.adjusted(8, 0, -8, 0)
        icon = self.icon()
        if self._icon_name:
            isz = self.ICON_SIZE
            tw = self.fontMetrics().horizontalAdvance(self.text()) if self.text() else 0
            total = isz + (6 + tw if tw else 0)
            x = rect.center().x() - total / 2
            icons.paint(p, self._icon_name, QRectF(x, rect.center().y() - isz / 2, isz, isz), fg)
            if not tw:
                p.end()
                return
            text_rect.setLeft(x + isz + 6)
            align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        elif not icon.isNull():
            isz = self.iconSize()
            tw = self.fontMetrics().horizontalAdvance(self.text())
            total = isz.width() + (6 + tw if self.text() else 0)
            x = rect.center().x() - total / 2
            pm = icon.pixmap(isz, QIcon_mode(self))
            p.drawPixmap(QPointF(x, rect.center().y() - isz.height() / 2), pm)
            text_rect.setLeft(x + isz.width() + 6)
            align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        else:
            align = Qt.AlignmentFlag.AlignCenter
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
        p.drawText(text_rect, int(align), text)
        p.end()


def QIcon_mode(widget):
    from PyQt6.QtGui import QIcon
    return QIcon.Mode.Normal if widget.isEnabled() else QIcon.Mode.Disabled


# ── postranní panel: položky a posuvný jezdec ────────────────────────────────
class NavItem(_Interactive, QAbstractButton):
    """Položka postranního panelu. Výběr nekreslí sama – pod ní jezdí SidebarNav pill."""
    HEIGHT = 32

    def __init__(self, text, parent=None, icon=None, key=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.key = key
        self.icon_name = icon           # název z icons.py; vybraná položka ho kreslí zvýrazněním
        self.count = None
        self._sel = motion.Tween(0.0, lambda v: self.update(), self)
        self.toggled.connect(lambda on: self._sel.to(1.0 if on else 0.0, "standard"))
        self._init_interaction()

    def sizeHint(self):
        return QSize(150, self.HEIGHT)

    def set_count(self, n):
        """Počet vpravo (tabulkové číslice); None/0 = nic."""
        n = n or None
        if n != self.count:
            self.count = n
            self.update()

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        sel = max(0.0, min(1.0, self._sel.value))
        hover = self._hover.value * (1.0 - sel)
        if hover > 0.001:
            col = _c(t, "hover")
            col.setAlphaF(col.alphaF() * hover)
            p.fillPath(_rounded(rect, tokens.RADIUS["sm"]), col)
        if self._kbd_focus:
            _focus_ring(p, t, rect, tokens.RADIUS["sm"])
        fg = tokens.mix(_c(t, "text_secondary"), _c(t, "text"), max(sel, hover * .8))
        x = 10.0
        if self.icon_name:
            icon_col = tokens.mix(_c(t, "text_secondary"), _c(t, "accent"), sel)
            icons.paint(p, self.icon_name, QRectF(x, rect.center().y() - 9, 18, 18), icon_col)
            x += 18 + 10
        f = self.font()
        f.setPixelSize(13)
        f.setWeight(f.Weight.Medium)        # stálá váha – žádné poskočení textu při výběru
        p.setFont(f)
        right = rect.width() - 10
        if self.count:
            nf = QFont(f)
            nf.setPixelSize(11)
            nf.setFeature(QFont.Tag("tnum"), 1)            # tabulkové číslice
            p.setFont(nf)
            p.setPen(_c(t, "text_tertiary"))
            label = f"{self.count:,}".replace(",", " ")
            w = p.fontMetrics().horizontalAdvance(label)
            p.drawText(QRectF(right - w, 0, w, rect.height()), int(Qt.AlignmentFlag.AlignVCenter), label)
            right -= w + 8
            p.setFont(f)
        p.setPen(fg)
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(right - x))
        p.drawText(QRectF(x, 0, right - x, rect.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        p.end()


class NavSection:
    """Nadpis oddílu v SidebarNav („Knihovna“, „Kolekce“)."""

    def __init__(self, title):
        self.title = title


class _SectionLabel(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.title = title
        self.setFixedHeight(28)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        f = tokens.font("caption")
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(_c(t, "text_tertiary"))
        p.drawText(QRectF(10, 0, self.width() - 10, self.height() - 4),
                   int(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft), self.title.upper())
        p.end()


class _Pill(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        path = _rounded(rect, tokens.RADIUS["sm"])
        p.fillPath(path, _c(t, "pill"))
        p.setPen(QPen(_c(t, "pill_border"), 1))
        p.drawPath(path)
        p.end()


class SidebarNav(QWidget):
    """Seznam položek s jedním jezdcem (pill), který při změně plynule přejede – i mezi oddíly.

    entries: text (klíč = pořadí), (klíč, text, ikona) nebo NavSection("Kolekce").
    current_changed(klíč) po kliknutí."""
    current_changed = pyqtSignal(object)

    def __init__(self, entries, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self._pill = _Pill(self)
        self._pill.hide()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.items = []
        for entry in entries:
            if isinstance(entry, NavSection):
                lay.addWidget(_SectionLabel(entry.title, self))
                continue
            key, label, icon = entry if isinstance(entry, tuple) else (len(self.items), entry, None)
            item = NavItem(label, self, icon=icon, key=key)
            self.group.addButton(item, len(self.items))
            lay.addWidget(item)
            item.installEventFilter(self)
            self.items.append(item)
        self._pill.lower()
        self._rect = motion.Tween(QRect(), self._place, self)
        self.group.buttonToggled.connect(self._on_toggled)
        self.group.idClicked.connect(lambda i: self.current_changed.emit(self.items[i].key))

    def item(self, key):
        return next((it for it in self.items if it.key == key), None)

    def set_current(self, key):
        """Programová změna výběru (bez current_changed)."""
        it = self.item(key)
        if it is not None and not it.isChecked():
            it.setChecked(True)

    def current_key(self):
        b = self.group.checkedButton()
        return b.key if b else None

    def _place(self, r):
        self._pill.setGeometry(r)
        self._pill.setVisible(not r.isEmpty())

    def _on_toggled(self, button, checked):
        if not checked:
            return
        target = QRect(button.geometry())
        if self._rect.value.isEmpty() or not self.isVisible():
            self._rect.set(target)
        else:
            self._rect.to(target, "emphasized")

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize) and obj is self.group.checkedButton() \
                and not self._rect.is_running():
            self._rect.set(QRect(obj.geometry()))
        return False


# ── výběr z nabídky (náhrada QComboBox) ──────────────────────────────────────
class MenuSelect(_Interactive, QAbstractButton):
    """Kapsle ve stylu segmentového přepínače: ikona, aktuální volba, šipka; klik otevře
    nabídku (QMenu ve stylu Kairos) se zaškrtnutou volbou. Rozhraní jako QComboBox
    (currentIndex, setCurrentIndex, currentText, count, itemText, currentIndexChanged),
    takže nahradí nativní rozbalovací seznam bez změny volajících."""
    currentIndexChanged = pyqtSignal(int)
    HEIGHT = 28

    def __init__(self, items=(), parent=None, icon=None, prefix=""):
        super().__init__(parent)
        self._items = [str(i) for i in items]
        self._index = 0 if self._items else -1
        self._icon_name = icon
        self._prefix = prefix               # např. „Řadit:“ před volbou (tlumeně)
        self._menu = None
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.clicked.connect(self.showPopup)
        self._init_interaction()
        self._sync_tooltip()

    # QComboBox API
    def addItems(self, items):
        self._items.extend(str(i) for i in items)
        if self._index < 0 and self._items:
            self._index = 0
        self.updateGeometry()
        self.update()

    def count(self):
        return len(self._items)

    def itemText(self, i):
        return self._items[i] if 0 <= i < len(self._items) else ""

    def currentIndex(self):
        return self._index

    def currentText(self):
        return self.itemText(self._index)

    def setCurrentIndex(self, i):
        i = int(i)
        if not -1 <= i < len(self._items) or i == self._index:
            return
        self._index = i
        self._sync_tooltip()
        self.updateGeometry()
        self.update()
        self.currentIndexChanged.emit(i)

    def _sync_tooltip(self):
        self.setToolTip(f"{self._prefix} {self.currentText()}".strip())

    def showPopup(self):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        for i, text in enumerate(self._items):
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(i == self._index)
            act.triggered.connect(lambda _=False, i=i: self.setCurrentIndex(i))
        self._menu = menu
        self.update()
        # pod kapslí, zarovnaná vlevo
        menu.exec(self.mapToGlobal(QPointF(0, self.height() + 4)).toPoint())
        self._menu = None
        menu.deleteLater()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self._items:
            step = -1 if e.key() == Qt.Key.Key_Up else 1
            self.setCurrentIndex(max(0, min(len(self._items) - 1, self._index + step)))
            return
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.showPopup()
            return
        super().keyPressEvent(e)

    def _label_font(self):
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Medium)
        return f

    def sizeHint(self):
        from PyQt6.QtGui import QFontMetrics
        fm = QFontMetrics(self._label_font())
        widest = max((fm.horizontalAdvance(t) for t in self._items), default=40)   # bez skákání šířky
        w = 12 + widest + 6 + 12 + 10
        if self._prefix:
            w += fm.horizontalAdvance(self._prefix) + 5
        if self._icon_name:
            w += 14 + 6
        return QSize(int(w), self.HEIGHT)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        path = _rounded(rect, tokens.RADIUS["sm"])
        open_ = self._menu is not None
        fill = tokens.mix(_c(t, "seg_track"), _c(t, "surface_3"), max(self._hover.value * .8, 1.0 if open_ else 0.0))
        p.fillPath(path, fill)
        p.setPen(QPen(_c(t, "hairline"), 1))
        p.drawPath(path)
        if self._kbd_focus:
            _focus_ring(p, t, rect, tokens.RADIUS["sm"])
        enabled = self.isEnabled()
        fg = _c(t, "text") if enabled else _c(t, "text_tertiary")
        dim = _c(t, "text_secondary") if enabled else _c(t, "text_tertiary")
        x = 12.0
        cy = rect.center().y()
        if self._icon_name:
            icons.paint(p, self._icon_name, QRectF(x, cy - 7, 14, 14), dim)
            x += 14 + 6
        f = self._label_font()
        p.setFont(f)
        right = rect.right() - 12 - 12 - 4
        if self._prefix:
            p.setPen(dim)
            pw = p.fontMetrics().horizontalAdvance(self._prefix)
            p.drawText(QRectF(x, 0, pw, rect.height()), int(Qt.AlignmentFlag.AlignVCenter), self._prefix)
            x += pw + 5
        p.setPen(fg)
        text = p.fontMetrics().elidedText(self.currentText(), Qt.TextElideMode.ElideRight, int(right - x))
        p.drawText(QRectF(x, 0, right - x, rect.height()),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        icons.paint(p, "chevron_down", QRectF(rect.right() - 12 - 12, cy - 6, 12, 12), dim)
        p.end()


# ── segmentový přepínač ──────────────────────────────────────────────────────
class Segment(_Interactive, QAbstractButton):
    def __init__(self, text, key, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.key = key
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._sel = motion.Tween(0.0, lambda v: self.update(), self)
        self.toggled.connect(lambda on: self._sel.to(1.0 if on else 0.0, "standard"))
        self._init_interaction()

    def sizeHint(self):
        return QSize(self.fontMetrics().horizontalAdvance(self.text()) + 26, 24)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and isinstance(self.parent(), SegmentedControl):
            self.parent().step(-1 if e.key() == Qt.Key.Key_Left else 1)
            return
        super().keyPressEvent(e)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        sel = max(0.0, min(1.0, self._sel.value))
        if self._kbd_focus:
            _focus_ring(p, t, QRectF(self.rect()), tokens.RADIUS["xs"])
        p.setPen(tokens.mix(_c(t, "text_secondary"), _c(t, "text"), max(sel, self._hover.value * .7)))
        f = self.font()
        f.setPixelSize(12)
        f.setWeight(f.Weight.DemiBold if sel > .5 else f.Weight.Medium)
        p.setFont(f)
        p.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter), self.text())
        p.end()


class SegmentedControl(QWidget):
    """macOS segmentový přepínač: jeden podklad, výběr = světlejší „jezdec“, který přejede.

    items: [(text, klíč)]; changed(klíč) po kliknutí. group / buttons() jako u QButtonGroup.
    Klíč může být text i číslo (index záložky)."""
    changed = pyqtSignal(object)
    PAD = 2

    def __init__(self, items, parent=None, current=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(self.PAD, self.PAD, self.PAD, self.PAD)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.segments = []
        for i, (text, key) in enumerate(items):
            seg = Segment(text, key, self)
            self.group.addButton(seg, i)
            lay.addWidget(seg)
            seg.installEventFilter(self)
            self.segments.append(seg)
        self._thumb = motion.Tween(QRectF(), lambda v: self.update(), self)
        self.group.buttonToggled.connect(self._on_toggled)
        self.group.idClicked.connect(lambda i: self.changed.emit(self.segments[i].key))
        self.setFixedHeight(28)
        if self.segments:
            self.set_current(current if current is not None else self.segments[0].key)

    def buttons(self):
        return self.group.buttons()

    def current_key(self):
        b = self.group.checkedButton()
        return b.key if b else None

    def set_current(self, key):
        for seg in self.segments:
            if seg.key == key:
                seg.setChecked(True)
                return

    def step(self, delta):
        idx = self.group.checkedId()
        nxt = self.segments[(idx + delta) % len(self.segments)]
        nxt.click()
        nxt.setFocus(Qt.FocusReason.TabFocusReason)

    def _on_toggled(self, seg, checked):
        if not checked:
            return
        target = QRectF(seg.geometry())
        if self._thumb.value.isEmpty() or not self.isVisible():
            self._thumb.set(target)
        else:
            self._thumb.to(target, "emphasized")

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Move, QEvent.Type.Resize) and obj is self.group.checkedButton() \
                and not self._thumb.is_running():
            self._thumb.set(QRectF(obj.geometry()))
        return False

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        path = _rounded(track, tokens.RADIUS["sm"])
        p.fillPath(path, _c(t, "seg_track"))
        p.setPen(QPen(_c(t, "hairline"), 1))
        p.drawPath(path)
        thumb = self._thumb.value
        if not thumb.isEmpty():
            r = QRectF(thumb)
            shadow = _rounded(r.translated(0, 1), tokens.RADIUS["xs"])
            p.fillPath(shadow, _c(t, "shadow"))
            tp = _rounded(r, tokens.RADIUS["xs"])
            p.fillPath(tp, _c(t, "seg_thumb"))
            p.setPen(QPen(_c(t, "pill_border"), 1))
            p.drawPath(tp)
        p.end()


# ── vypínač ───────────────────────────────────────────────────────────────────
class ToggleSwitch(_Interactive, QAbstractButton):
    """Vypínač ve stylu macOS. S textem = celý řádek (text vlevo, vypínač vpravo), klikatelný celý.
    API jako QCheckBox: setChecked / isChecked / toggled."""
    TRACK_W, TRACK_H = 38, 22

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pos = motion.Tween(0.0, lambda v: self.update(), self)
        self.toggled.connect(lambda on: self._pos.to(1.0 if on else 0.0, "spring"))
        self._init_interaction()

    def sizeHint(self):
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) + 16 if self.text() else 0
        return QSize(text_w + self.TRACK_W + 4, max(28, fm.height() + 8))

    def minimumSizeHint(self):
        return QSize(self.TRACK_W + 4, self.sizeHint().height())

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def track_rect(self):
        return QRectF(self.width() - self.TRACK_W - 2, (self.height() - self.TRACK_H) / 2, self.TRACK_W, self.TRACK_H)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tr = self.track_rect()
        if self.text():
            p.setPen(_c(t, "text") if self.isEnabled() else _c(t, "text_tertiary"))
            p.setFont(self.font())
            text_rect = QRectF(0, 0, tr.left() - 12, self.height())
            text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
            p.drawText(text_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        pos = self._pos.value
        on = max(0.0, min(1.0, pos))
        track_col = tokens.mix(_c(t, "switch_off"), _c(t, "fill"), on)
        if not self.isEnabled():
            track_col.setAlphaF(track_col.alphaF() * .45)
        radius = self.TRACK_H / 2
        p.fillPath(_rounded(tr, radius), track_col)
        if self._kbd_focus:
            p.setPen(QPen(_c(t, "focus_ring"), 2))
            p.drawRoundedRect(tr.adjusted(-2, -2, 2, 2), radius + 2, radius + 2)
        d = self.TRACK_H - 4
        kw = d + 5 * max(0.0, min(1.0, self._press.value))      # při stisku se jezdec protáhne
        travel = tr.width() - 4 - kw
        x = tr.left() + 2 + travel * pos
        x = max(tr.left() + 1, min(tr.right() - 1 - kw, x))       # přetočení pružiny nevyjede z dráhy
        knob = QRectF(x, tr.top() + 2, kw, d)
        p.fillPath(_rounded(knob.translated(0, .8), d / 2), _c(t, "shadow"))
        p.fillPath(_rounded(knob, d / 2), _c(t, "knob"))
        p.end()


# ── prázdný stav ──────────────────────────────────────────────────────────────
class _Glyph(QWidget):
    """Ikona prázdného stavu: měkký čtverec ve zvýraznění se symbolem."""
    SIZE = 64

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self.icon_name = icon
        self.setFixedSize(self.SIZE, self.SIZE)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillPath(_rounded(QRectF(self.rect()), 20), _c(t, "accent_soft"))
        icons.paint(p, self.icon_name, QRectF(18, 18, 28, 28), _c(t, "accent"))
        p.end()


class EmptyState(QWidget):
    """Prázdný stav: ikona, nadpis, vysvětlení a volitelná akce. Barvy a písmo z tokenů
    (QLabel#emptyTitle / #emptyBody v hlavním stylu). Při zobrazení jednou jemně vyjede."""
    action_triggered = pyqtSignal()
    TEXT_WIDTH = 380

    def __init__(self, icon="photos", title="", body="", action_text=None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.glyph = _Glyph(icon, self)
        lay.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(10)
        self.lbl_title = QLabel(title, self)
        self.lbl_title.setObjectName("emptyTitle")
        self.lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_title.setWordWrap(True)
        self.lbl_body = QLabel(body, self)
        self.lbl_body.setObjectName("emptyBody")
        self.lbl_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_body.setWordWrap(True)
        for lbl in (self.lbl_title, self.lbl_body):
            lbl.setFixedWidth(self.TEXT_WIDTH)
        lay.addWidget(self.lbl_title, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addWidget(self.lbl_body, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(10)
        self.btn_action = KButton(action_text or "", self, compact=True)
        self.btn_action.clicked.connect(self.action_triggered.emit)
        self.btn_action.setVisible(bool(action_text))
        lay.addWidget(self.btn_action, 0, Qt.AlignmentFlag.AlignHCenter)
        lay.addStretch(1)
        self._layout = lay
        self._enter = motion.Tween(1.0, self._step_enter, self)
        self._fit_text()

    def _fit_text(self):
        """Zalomený QLabel se zarovnáním v layoutu nedostane výšku podle šířky – dopočítat ji."""
        for lbl in (self.lbl_title, self.lbl_body):
            lbl.setFixedHeight(lbl.heightForWidth(self.TEXT_WIDTH) if lbl.text() else 0)

    def set_content(self, icon, title, body, action_text=None):
        self.glyph.icon_name = icon
        self.glyph.update()
        self.lbl_title.setText(title)
        self.lbl_body.setText(body)
        self._fit_text()
        self.btn_action.setText(action_text or "")
        self.btn_action.setVisible(bool(action_text))
        self.btn_action.updateGeometry()

    def title(self):
        return self.lbl_title.text()

    def _step_enter(self, v):
        v = float(v)
        effect = self.graphicsEffect()
        if v >= 1.0:
            if effect is not None:
                self.setGraphicsEffect(None)          # po doběhnutí bez efektu (levnější kreslení)
            self._layout.setContentsMargins(24, 24, 24, 24)
            return
        if effect is None:
            effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(effect)
        effect.setOpacity(max(0.0, min(1.0, v)))
        rise = round(12 * (1.0 - v))
        self._layout.setContentsMargins(24, 24 + rise, 24, max(0, 24 - rise))

    def showEvent(self, e):
        super().showEvent(e)
        self._fit_text()                      # písmo ze stylu je známé až po polish
        if motion.duration("emphasized") > 0:
            self._enter.set(0.0)
            self._enter.to(1.0, "emphasized")


class _OverlayPin(QObject):
    def __init__(self, overlay, host):
        super().__init__(overlay)
        self.overlay, self.host = overlay, host
        host.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() == QEvent.Type.Resize:
            self.overlay.setGeometry(self.host.rect())
        return False


def pin_overlay(overlay, host):
    """Překryv (prázdný stav nad mřížkou) drží velikost hostitele i po změně velikosti okna."""
    overlay.setGeometry(host.rect())
    _OverlayPin(overlay, host)


# ── tvary a nápověda ─────────────────────────────────────────────────────────
def smooth_rect_path(rect, radius, smoothing=0.6):
    """Obdélník s plynulými rohy (squircle): oblouk začne dřív a dobíhá pozvolna,
    roh nikdy nevypadá „ostře nasazený“. radius = vizuálně srovnatelný s běžným zaoblením."""
    r = QRectF(rect)
    reach = min(radius * (1.0 + smoothing), r.width() / 2, r.height() / 2)
    k = 0.45 - 0.25 * smoothing          # kratší řídicí body = hladší, „napjatější“ roh
    x0, y0, x1, y1 = r.left(), r.top(), r.right(), r.bottom()
    path = QPainterPath(QPointF(x0 + reach, y0))
    path.lineTo(x1 - reach, y0)
    path.cubicTo(x1 - reach * k, y0, x1, y0 + reach * k, x1, y0 + reach)
    path.lineTo(x1, y1 - reach)
    path.cubicTo(x1, y1 - reach * k, x1 - reach * k, y1, x1 - reach, y1)
    path.lineTo(x0 + reach, y1)
    path.cubicTo(x0 + reach * k, y1, x0, y1 - reach * k, x0, y1 - reach)
    path.lineTo(x0, y0 + reach)
    path.cubicTo(x0, y0 + reach * k, x0 + reach * k, y0, x0 + reach, y0)
    path.closeSubpath()
    return path


class HintPill(QWidget):
    """Krátká nápověda (např. „5 sloupců“ při zoomu): objeví se, chvíli zůstane, zmizí."""
    HOLD_MS = 900

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.text = ""
        self._alpha = motion.Tween(0.0, lambda v: self.update(), self)
        self._alpha.finished.connect(lambda: self.hide() if self._alpha.value == 0.0 else None)
        from PyQt6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._alpha.to(0.0, "standard"))
        self.hide()

    def _place(self, text):
        self.text = text
        self.setFont(tokens.font("headline"))
        w = self.fontMetrics().horizontalAdvance(text) + 32
        parent = self.parentWidget()
        self.setGeometry((parent.width() - w) // 2, 18, w, 34)
        self.show()
        self.raise_()
        self._alpha.to(1.0, "micro")

    def flash(self, text):
        """Ukáže text a po chvíli sám zmizí."""
        self._place(text)
        self._timer.start(self.HOLD_MS)

    def show_sticky(self, text):
        """Zůstane, dokud se nezavolá hide_sticky (např. datum dne při posouvání)."""
        self._timer.stop()
        if text != self.text or not self.isVisible():
            self._place(text)
            self.update()

    def hide_sticky(self):
        self._timer.stop()
        if self.isVisible():
            self._alpha.to(0.0, "standard")

    def paintEvent(self, _):
        a = max(0.0, min(1.0, float(self._alpha.value)))
        if a <= 0:
            return
        t = tokens.palette("dark")             # tmavá „HUD“ kapsle nad fotkami v obou režimech
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(a)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        bg = tokens.qcolor(t["surface"])
        bg.setAlpha(235)
        p.fillPath(_rounded(rect, rect.height() / 2), bg)
        p.setPen(QPen(_c(t, "hairline_strong"), 1))
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        p.setPen(_c(t, "text"))
        p.setFont(self.font())
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self.text)
        p.end()


# ── výběr zvýraznění ─────────────────────────────────────────────────────────
class _Swatch(_Interactive, QAbstractButton):
    SIZE = 26

    def __init__(self, key, title, parent=None):
        super().__init__(parent)
        self.key, self.custom_hex = key, None
        self.setCheckable(True)
        self.setToolTip(title)
        self.setAccessibleName(title)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setFixedSize(self.SIZE + 8, self.SIZE + 8)
        self._sel = motion.Tween(0.0, lambda v: self.update(), self)
        self.toggled.connect(lambda on: self._sel.to(1.0 if on else 0.0, "spring"))
        self._init_interaction()

    def colors(self, mode):
        if self.key == "custom":
            if self.custom_hex:
                dark, light = tokens.derive_accent(self.custom_hex)
                a, _, b = dark if mode == "dark" else light
                return a, b
            return None
        a, _, b = tokens.ACCENTS[self.key][mode]
        return a, b

    def paintEvent(self, _):
        from PyQt6.QtGui import QConicalGradient, QLinearGradient
        t = palette_for(self)
        mode = "dark" if theme.is_fixed_dark(self) else theme.current()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QRectF(self.rect()).center()
        r = self.SIZE / 2 * (1.0 + 0.06 * self._hover.value - 0.06 * max(0.0, min(1.0, self._press.value)))
        disc = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        colors = self.colors(mode)
        if colors:
            g = QLinearGradient(disc.topLeft(), disc.bottomRight())
            g.setColorAt(0, QColor(colors[0]))
            g.setColorAt(1, QColor(colors[1]))
        else:                                  # vlastní barva ještě nevybraná = duha
            g = QConicalGradient(c, 90)
            for i, col in enumerate(("#FF6B6B", "#FFB547", "#A3D977", "#4FD1C5", "#8B93FF", "#E879F9", "#FF6B6B")):
                g.setColorAt(i / 6, QColor(col))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawEllipse(disc)
        sel = max(0.0, min(1.0, float(self._sel.value)))
        if sel > 0.01:
            ring = QColor(_c(t, "text"))
            ring.setAlphaF(0.9 * sel)
            p.setPen(QPen(ring, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            grow = 2 + 2 * sel
            p.drawEllipse(disc.adjusted(-grow, -grow, grow, grow))
        if self.key == "custom" and not self.custom_hex:
            icons.paint(p, "plus", disc.adjusted(7, 7, -7, -7), QColor("#FFFFFF"))
        if self._kbd_focus:
            _focus_ring(p, t, QRectF(self.rect()), self.width() / 2)
        p.end()


class AccentPicker(QWidget):
    """Zvýraznění: Iris, Ember, Moss, Klasická modrá a Vlastní (výběr barvy).
    changed(klíč, hex vlastní barvy nebo '')."""
    changed = pyqtSignal(str, str)

    def __init__(self, key="iris", custom_hex="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.swatches = {}
        for k in ("iris", "ember", "moss", "classic"):
            sw = _Swatch(k, tokens.ACCENTS[k]["name"], self)
            self.group.addButton(sw)
            lay.addWidget(sw)
            self.swatches[k] = sw
        custom = _Swatch("custom", "Vlastní barva…", self)
        custom.clicked.connect(self._pick_custom)
        self.group.addButton(custom)
        lay.addWidget(custom)
        self.swatches["custom"] = custom
        for k in ("iris", "ember", "moss", "classic"):
            self.swatches[k].clicked.connect(lambda _=False, key=k: self.changed.emit(key, ""))
        self.set_value(key, custom_hex)

    def set_value(self, key, custom_hex=""):
        if custom_hex:
            self.swatches["custom"].custom_hex = custom_hex
            self.swatches["custom"].setToolTip(f"Vlastní barva {custom_hex}")
        sw = self.swatches.get(key) or self.swatches[tokens.DEFAULT_ACCENT]
        sw.setChecked(True)
        self.update()

    def value(self):
        sw = self.group.checkedButton()
        key = sw.key if sw else tokens.DEFAULT_ACCENT
        return key, (self.swatches["custom"].custom_hex or "") if key == "custom" else ""

    def _pick_custom(self):
        from PyQt6.QtWidgets import QColorDialog
        sw = self.swatches["custom"]
        start = QColor(sw.custom_hex or tokens.ACCENTS["iris"]["dark"][0])
        col = QColorDialog.getColor(start, self, "Vlastní barva zvýraznění")
        if col.isValid():
            sw.custom_hex = col.name().upper()
            sw.setToolTip(f"Vlastní barva {sw.custom_hex}")
            sw.setChecked(True)
            sw.update()
            self.changed.emit("custom", sw.custom_hex)
        elif not sw.custom_hex:                    # zrušeno bez barvy -> zpět na předchozí volbu
            self.set_value(tokens.accent_key())



# ── Kairos dávka 5: indikátor práce na pozadí ─────────────────────────────────

_PROGRESS = re.compile(r"\((\d+)\s*/\s*(\d+)\)|(\d{1,3})\s*%")


def parse_progress(text):
    """'Počítám otisky (12/40)...' -> 0.3; '45 %' -> 0.45; jinak None (neurčitý průběh)."""
    m = _PROGRESS.search(text or "")
    if not m:
        return None
    if m.group(1) is not None:
        done, total = int(m.group(1)), int(m.group(2))
        return max(0.0, min(1.0, done / total)) if total > 0 else None
    return max(0.0, min(1.0, int(m.group(3)) / 100))


class ActivityStatus(QWidget):
    """Úlohy na pozadí v postranním panelu: kometa obíhá prstenec (motiv loga), vedle text,
    pod ním tenký průběh, pokud text nese „(i/n)“ nebo „n %“.

    Náhrada QLabel stavu: setText()/text() pracují s výchozí úlohou; další úlohy (hledání,
    příprava modelu) mají vlastní klíč a nepřepisují se navzájem. Ukazuje se poslední
    změněná úloha. Objeví se až po SHOW_DELAY_MS (krátké úlohy neblikají) a animace běží
    jen, dokud je co ukazovat."""
    SHOW_DELAY_MS = 250
    HEIGHT = 46
    ORBIT = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        from PyQt6.QtCore import QTimer, QVariantAnimation
        self.setObjectName("activityStatus")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._tasks = {}                      # klíč -> text (pořadí vložení = pořadí změn)
        self._alpha = motion.Tween(0.0, lambda v: self.update(), self)
        self._alpha.finished.connect(self._after_fade)
        self._progress = motion.Tween(0.0, lambda v: self.update(), self)
        self._angle = 0.0
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(1100)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._on_spin)
        self._show_timer = QTimer(self)
        self._show_timer.setSingleShot(True)
        self._show_timer.timeout.connect(self._reveal)
        self.setFixedHeight(0)
        self.hide()

    # ── API ──
    def setText(self, text):
        self.set_task("default", text)

    def text(self):
        return self._tasks.get("default", "")

    def current_text(self):
        return next(reversed(self._tasks.values()), "") if self._tasks else ""

    def tasks(self):
        return dict(self._tasks)

    def set_task(self, key, text):
        text = (text or "").strip()
        if not text:
            self.clear_task(key)
            return
        self._tasks.pop(key, None)
        self._tasks[key] = text
        self.setToolTip("\n".join(reversed(list(self._tasks.values()))))
        frac = parse_progress(text)
        if frac is not None:
            self._progress.to(frac, "standard")
        if self.isHidden() or self._alpha.target == 0.0:
            if not self._show_timer.isActive():
                self._show_timer.start(self.SHOW_DELAY_MS)
        self.update()

    def clear_task(self, key):
        if self._tasks.pop(key, None) is None:
            return
        if self._tasks:
            self.setToolTip("\n".join(reversed(list(self._tasks.values()))))
            self.update()
            return
        self._show_timer.stop()
        if self.isVisible():
            self._alpha.to(0.0, "standard")
        else:
            self._collapse()

    # ── viditelnost ──
    def _reveal(self):
        if not self._tasks:
            return
        self.setFixedHeight(self.HEIGHT)
        self.show()
        if not motion.reduced():
            self._spin.start()
        self._alpha.to(1.0, "emphasized")

    def _after_fade(self):
        if self._alpha.value == 0.0 and not self._tasks:
            self._collapse()

    def _collapse(self):
        self._spin.stop()
        self._progress.set(0.0)
        self.setFixedHeight(0)
        self.hide()

    def _on_spin(self, v):
        self._angle = float(v)
        self.update()

    def hideEvent(self, e):
        self._spin.stop()
        super().hideEvent(e)

    def showEvent(self, e):
        if self._tasks and not motion.reduced() and self._spin.state() != self._spin.State.Running:
            self._spin.start()
        super().showEvent(e)

    # ── kreslení ──
    def paintEvent(self, _):
        alpha = max(0.0, min(1.0, float(self._alpha.value)))
        text = self.current_text()
        if alpha <= 0 or not text:
            return
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(alpha)
        card = QRectF(self.rect()).adjusted(2, 4, -2, -2)
        p.fillPath(smooth_rect_path(card, tokens.RADIUS["md"]), _c(t, "hover"))

        orbit = QRectF(card.left() + 12, card.center().y() - self.ORBIT / 2, self.ORBIT, self.ORBIT)
        self._paint_orbit(p, t, orbit)

        frac = parse_progress(text)
        text_left = orbit.right() + 10
        text_rect = QRectF(text_left, card.top(), card.right() - text_left - 10,
                           card.height() - (8 if frac is not None else 0))
        f = QFont(tokens.ui_family())
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.Medium)
        p.setFont(f)
        p.setPen(_c(t, "text_secondary"))
        shown = p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        p.drawText(text_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), shown)

        if frac is not None:
            track = QRectF(text_left, card.bottom() - 11, text_rect.width(), 3)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c(t, "hairline_strong"))
            p.drawRoundedRect(track, 1.5, 1.5)
            fill = QRectF(track)
            fill.setWidth(max(3.0, track.width() * max(0.0, min(1.0, float(self._progress.value)))))
            from PyQt6.QtGui import QLinearGradient
            g = QLinearGradient(track.topLeft(), track.topRight())
            g.setColorAt(0.0, _c(t, "accent"))
            g.setColorAt(1.0, _c(t, "accent_2"))
            p.setBrush(g)
            p.drawRoundedRect(fill, 1.5, 1.5)
        p.end()

    def _paint_orbit(self, p, t, rect):
        """Tenká stopa prstence + kometa (přechod zvýraznění) a jiskra v hlavě."""
        import math
        pen = QPen(_c(t, "hairline_strong"), 2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect.adjusted(1, 1, -1, -1))
        accent, accent_2 = _c(t, "accent"), _c(t, "accent_2")
        if motion.reduced():                     # bez pohybu: celý prstenec s mezerou jako logo
            pen = QPen(accent, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(rect.adjusted(1, 1, -1, -1), 70 * 16, 310 * 16)
            return
        head = -self._angle                       # po směru hodin
        span = 110
        steps = 10
        for i in range(steps):
            k = 1 - i / steps
            col = QColor(accent)
            col.setAlphaF(k * k)
            pen = QPen(col, 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            a0 = head + span * i / steps
            p.drawArc(rect.adjusted(1, 1, -1, -1), round(a0 * 16), round(span / steps * 16) + 2)
        r = rect.width() / 2 - 1
        c = rect.center()
        hx = c.x() + r * math.cos(math.radians(head))
        hy = c.y() - r * math.sin(math.radians(head))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent_2)
        p.drawEllipse(QPointF(hx, hy), 2.2, 2.2)
