"""Roky a Měsíce (Kairos dávka 4): karty s obálkou pro rychlou orientaci v čase.

Záznamy jsou řádky MEDIA_COLUMNS (id, název, cesta, typ, párové video, náhled, datum, oblíbené).
Seskupení zachovává pořadí záznamů (knihovna je řazená od nejnovějších). Obálka = uložený
náhled první položky skupiny. Klik na rok otevře jeho měsíce, klik na měsíc mřížku na tom místě.
"""
import os

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QScrollArea, QWidget

import motion
import tokens
from ui_controls import palette_for, smooth_rect_path

MONTHS = ["Leden", "Únor", "Březen", "Duben", "Květen", "Červen", "Červenec", "Srpen", "Září",
          "Říjen", "Listopad", "Prosinec"]
MONTHS_GENITIVE = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna", "září",
                   "října", "listopadu", "prosince"]
WEEKDAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
NO_DATE = "Bez data"
CARD_SIZE = {"years": QSize(320, 220), "months": QSize(248, 176)}
SPACING = 16


def _items(n):
    return "1 položka" if n == 1 else (f"{n} položky" if 2 <= n <= 4 else f"{n} položek")


def czech_day(date_text):
    """'2025-06-14 10:22:01' -> 'Sobota 14. června 2025' (bez data -> '')."""
    from datetime import datetime
    try:
        d = datetime.strptime(str(date_text)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day}. {MONTHS_GENITIVE[d.month - 1]} {d.year}"


def group_records(records, by):
    """by = 'years' | 'months' -> [{key, title, subtitle, count, cover}] v pořadí záznamů."""
    groups, index = [], {}
    for r in records:
        date = str(r[6] or "")
        if len(date) >= 7 and date[:4].isdigit() and date[5:7].isdigit():
            key = date[:4] if by == "years" else date[:7]
        else:
            key = ""
        g = index.get(key)
        if g is None:
            if not key:
                title = NO_DATE
            elif by == "years":
                title = key
            else:
                title = f"{MONTHS[int(key[5:7]) - 1]} {key[:4]}"
            g = {"key": key, "title": title, "count": 0, "cover": None}
            index[key] = g
            groups.append(g)
        g["count"] += 1
        if g["cover"] is None and r[5] and os.path.exists(r[5]):
            g["cover"] = r[5]
    for g in groups:
        g["subtitle"] = _items(g["count"])
    return groups


_covers = {}


def _cover_image(path, size):
    """Obálka zmenšená (výplní) na velikost karty; jedno načtení na cestu a velikost."""
    key = (path, size.width(), size.height())
    if key not in _covers:
        img = QImage(path) if path else QImage()
        if not img.isNull():
            img = img.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        _covers[key] = img
        if len(_covers) > 400:
            _covers.pop(next(iter(_covers)))
    return _covers[key]


class MomentCard(QWidget):
    clicked = pyqtSignal(str)

    def __init__(self, group, mode, parent=None):
        super().__init__(parent)
        self.group, self.mode = group, mode
        self.setFixedSize(CARD_SIZE[mode])
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"{group['title']}, {group['subtitle']}")
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit(self.group["key"])
        super().mouseReleaseEvent(e)

    def paintEvent(self, _):
        t = palette_for(self)
        h = max(0.0, min(1.0, float(self._hover.value)))
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        inset = 6.0 - 4.0 * h                              # najetí: karta doroste a zvedne se
        rect = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        radius = tokens.RADIUS["lg"]
        for i, alpha in enumerate((40, 22, 10)):
            grow = 2 + i * 3
            p.fillPath(smooth_rect_path(rect.adjusted(-grow, -grow + 4, grow, grow + 6), radius + grow),
                       QColor(0, 0, 0, round(alpha * (0.4 + 0.6 * h))))
        path = smooth_rect_path(rect, radius)
        p.save()
        p.setClipPath(path)
        img = _cover_image(self.group.get("cover"), self.size())
        if img.isNull():
            p.fillPath(path, tokens.qcolor(t["surface_2"]))
        else:
            zoom = 1.0 + 0.04 * h
            w, hh = img.width() * zoom, img.height() * zoom
            p.drawImage(QRectF(rect.center().x() - w / 2, rect.center().y() - hh / 2, w, hh), img)
        grad = QLinearGradient(0, rect.top() + rect.height() * 0.35, 0, rect.bottom())
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, 170))
        p.fillRect(rect, grad)
        p.restore()

        white = QColor("#FFFFFF")
        title_f = tokens.font("large_title" if self.mode == "years" else "title")
        p.setFont(title_f)
        p.setPen(white)
        th = p.fontMetrics().height()
        sub_f = QFont(tokens.ui_family())
        sub_f.setPixelSize(12)
        sub_f.setWeight(QFont.Weight.Medium)
        sub_h = 16
        x, bottom = rect.left() + 16, rect.bottom() - 14
        p.drawText(QRectF(x, bottom - sub_h - th, rect.width() - 32, th),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom), self.group["title"])
        p.setFont(sub_f)
        p.setPen(QColor(255, 255, 255, 210))
        p.drawText(QRectF(x, bottom - sub_h, rect.width() - 32, sub_h),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.group["subtitle"])
        p.end()


class _Canvas(QWidget):
    """Karty v mřížce podle šířky (sloupce se dopočítají, vše na středu)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards = []

    def relayout(self, width):
        if not self.cards:
            self.setMinimumHeight(0)
            return
        cw, ch = self.cards[0].width(), self.cards[0].height()
        cols = max(1, (width - SPACING) // (cw + SPACING))
        left = max(SPACING, (width - cols * cw - (cols - 1) * SPACING) // 2)
        for i, card in enumerate(self.cards):
            r, c = divmod(i, cols)
            card.move(left + c * (cw + SPACING), SPACING + r * (ch + SPACING))
        rows = (len(self.cards) + cols - 1) // cols
        self.setMinimumHeight(SPACING + rows * (ch + SPACING))


class MomentsView(QScrollArea):
    """Roky / Měsíce: card_clicked(režim, klíč)."""
    card_clicked = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.canvas = _Canvas()
        self.setWidget(self.canvas)
        self.mode = None

    def set_groups(self, mode, groups):
        for card in self.canvas.cards:
            card.deleteLater()
        self.mode = mode
        self.canvas.cards = []
        for g in groups:
            card = MomentCard(g, mode, self.canvas)
            card.clicked.connect(lambda key, m=mode: self.card_clicked.emit(m, key))
            card.show()
            self.canvas.cards.append(card)
        self.canvas.relayout(self.viewport().width())
        self.verticalScrollBar().setValue(0)

    def keys(self):
        return [c.group["key"] for c in self.canvas.cards]

    def scroll_to_key_prefix(self, prefix):
        for card in self.canvas.cards:
            if card.group["key"].startswith(prefix):
                self.verticalScrollBar().setValue(max(0, card.y() - SPACING))
                return True
        return False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.canvas.relayout(self.viewport().width())
