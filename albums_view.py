"""Alba – zobrazení (albums.py drží data).

AlbumsPanel: mřížka karet alb (obálka, název, počet / dotaz chytrého alba) a prázdný stav.
Ruční album se otevře jako mřížka položek, chytré jako hledání v mřížce Fotografie (main.py).
"""
import os

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QGridLayout, QMenu, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import albums
import icons
import motion
import tokens
from ui_controls import EmptyState, smooth_rect_path

CARD_W, CARD_H = 240, 236
IMAGE_H = 180
FILTER_LABELS = {"all": "", "favorites": "v Oblíbených", "photo": "jen fotky", "video": "jen videa"}


def _cover(path, w, h):
    if not path or not os.path.exists(path):
        return QImage()
    img = QImage(path)
    if img.isNull():
        return img
    s = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    return s.copy((s.width() - w) // 2, (s.height() - h) // 2, w, h)


def subtitle(album):
    if album["kind"] == "smart":
        extra = FILTER_LABELS.get(album.get("filter") or "all", "")
        return f"Hledání „{album['query']}“" + (f" {extra}" if extra else "")
    n = album.get("count") or 0
    return f"{n} {'položka' if n == 1 else 'položky' if 2 <= n <= 4 else 'položek'}"


class AlbumCard(QWidget):
    opened = pyqtSignal(object)
    menu_requested = pyqtSignal(object, object)        # album, globální pozice

    def __init__(self, album, parent=None):
        super().__init__(parent)
        self.album = album
        self._img = None
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Album {album['name']}, {subtitle(album)}")
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.opened.emit(self.album)
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.menu_requested.emit(self.album, e.globalPos())

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.album)
            return
        super().keyPressEvent(e)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = tokens.palette()
        radius = float(tokens.RADIUS["lg"])
        hover = max(0.0, min(1.0, float(self._hover.value)))
        img_r = QRectF(0.5, 0.5, self.width() - 1, IMAGE_H)
        dpr = self.devicePixelRatioF()
        if self._img is None:
            self._img = _cover(self.album.get("cover"), round(img_r.width() * dpr), round(img_r.height() * dpr))
        p.save()
        p.setClipPath(smooth_rect_path(img_r, radius))
        p.fillRect(img_r, tokens.qcolor(t["surface_3"]))
        if not self._img.isNull():
            p.drawImage(img_r, self._img)
        else:                                   # bez obálky: ikona (chytré album = jiskra)
            icon = "sparkle" if self.album["kind"] == "smart" else "photos"
            pm = icons.pixmap(icon, tokens.qcolor(t["accent"] if self.album["kind"] == "smart" else t["text_tertiary"]), 40)
            p.drawPixmap(int(img_r.center().x() - 20), int(img_r.center().y() - 20), pm)
        if hover:
            p.fillRect(img_r, QColor(255, 255, 255, int(18 * hover)))
        if self.album["kind"] == "smart" and not self._img.isNull():
            g = QLinearGradient(0, img_r.top(), 0, img_r.top() + 60)
            g.setColorAt(0, QColor(0, 0, 0, 120))
            g.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(img_r, g)
        p.restore()
        if self.album["kind"] == "smart":       # odznak chytrého alba
            p.drawPixmap(int(img_r.left() + 10), int(img_r.top() + 10), icons.pixmap("sparkle", QColor("#FFFFFF"), 18))
        p.setPen(tokens.qcolor(t["text"]))
        f = QFont(tokens.ui_family())
        f.setPixelSize(14)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        tw = self.width() - 4
        p.drawText(QRectF(2, IMAGE_H + 10, tw, 20), int(Qt.AlignmentFlag.AlignLeft),
                   p.fontMetrics().elidedText(self.album["name"], Qt.TextElideMode.ElideRight, tw))
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(tokens.qcolor(t["text_secondary"]))
        p.drawText(QRectF(2, IMAGE_H + 31, tw, 18), int(Qt.AlignmentFlag.AlignLeft),
                   p.fontMetrics().elidedText(subtitle(self.album), Qt.TextElideMode.ElideRight, tw))
        if self.hasFocus():
            p.setPen(tokens.qcolor(t["accent"]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(img_r.adjusted(1, 1, -1, -1), radius))
        p.end()


class AlbumsPanel(QWidget):
    """Obsah stránky Alba. opened(album); rename/delete požadavky řeší main.py."""
    opened = pyqtSignal(object)
    rename_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)

    def __init__(self, connect, parent=None):
        super().__init__(parent)
        self.connect = connect
        self.cards = []
        self.albums = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.empty = EmptyState("album", "Zatím žádná alba",
                                "Vyberte fotky v mřížce a v nabídce pravého tlačítka zvolte „Přidat do alba“. "
                                "Chytré album uloží hledání – třeba „západ slunce“ – a samo se doplňuje.")
        lay.addWidget(self.empty, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body = QWidget()
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(24, 16, 24, 24)
        self.grid.setHorizontalSpacing(20)
        self.grid.setVerticalSpacing(20)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)

    def load(self):
        conn = self.connect()
        try:
            albums.ensure_tables(conn)
            self.albums = albums.list_albums(conn)
        finally:
            conn.close()
        for c in self.cards:
            c.deleteLater()
        self.cards = []
        for a in self.albums:
            card = AlbumCard(a)
            card.opened.connect(self.opened.emit)
            card.menu_requested.connect(self._menu)
            self.cards.append(card)
        self.empty.setVisible(not self.albums)
        self.scroll.setVisible(bool(self.albums))
        self._relayout()
        return len(self.albums)

    def _menu(self, album, pos):
        menu = QMenu(self)
        act_open = menu.addAction("Otevřít")
        act_rename = menu.addAction("Přejmenovat…")
        menu.addSeparator()
        act_delete = menu.addAction("Smazat album")
        chosen = menu.exec(pos)
        if chosen == act_open:
            self.opened.emit(album)
        elif chosen == act_rename:
            self.rename_requested.emit(album)
        elif chosen == act_delete:
            self.delete_requested.emit(album)

    def _columns(self):
        return max(1, (self.width() - 48 + 20) // (CARD_W + 20))

    def _relayout(self):
        cols = self._columns()
        for c in self.cards:
            self.grid.removeWidget(c)
        for i, c in enumerate(self.cards):
            self.grid.addWidget(c, i // cols, i % cols)
        self._cols = cols

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.cards and self._columns() != getattr(self, "_cols", 0):
            self._relayout()

    def sizeHint(self):
        return QSize(800, 600)
