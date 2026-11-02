"""Alba – zobrazení (albums.py drží data).

AlbumsPanel: mřížka karet jedné úrovně stromu alb (složky, ruční a chytrá alba) a prázdný stav.
Ruční album se otevře jako mřížka položek, chytré jako hledání v mřížce Fotografie, složka jako
další AlbumsPanel o úroveň níž (main.py).

Alba v2: karty se dají přetahovat – na střed složky = přesun do ní, k okraji karty = nové pořadí,
na pruh „O úroveň výš“ = do nadřazené složky. Nabídka karty nabízí totéž bez myši
(„Přesunout do…“), plus výběr obálky a export.
"""
from i18n import N_, tr
import os

from PyQt6.QtCore import QMimeData, QPoint, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QApplication, QGridLayout, QLabel, QMenu, QScrollArea, QVBoxLayout, QWidget

import albums
import icons
import motion
import tokens
from ui_controls import EmptyState, smooth_rect_path

CARD_W, CARD_H = 240, 236
IMAGE_H = 180
FILTER_LABELS = {"all": "", "favorites": N_("albums-in-favorites"), "photo": N_("albums-only-photos"),
                 "video": N_("albums-only-videos")}
NODE_MIME = "application/x-kairos-album-node"
INTO_BAND = 0.22                          # okraje karty složky (podíl šířky) = vložit vedle, střed = do ní


def drop_action(node_kind, width, x):
    """Co znamená puštění nad kartou: "into" uprostřed složky, jinak "before" / "after"."""
    if node_kind == "folder" and width * INTO_BAND <= x <= width * (1 - INTO_BAND):
        return "into"
    return "before" if x < width / 2 else "after"


def _cover(path, w, h):
    if not path or not os.path.exists(path):
        return QImage()
    img = QImage(path)
    if img.isNull():
        return img
    s = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    return s.copy((s.width() - w) // 2, (s.height() - h) // 2, w, h)


def subtitle(album):
    if album["kind"] == "folder":
        return albums.czech_folder_summary(album.get("count") or 0)
    if album["kind"] == "smart":
        extra = FILTER_LABELS.get(album.get("filter") or "all", "")
        return tr("albums-search-label", query=album['query']) + (" " + tr(extra) if extra else "")
    return tr("count-items", count=album.get("count") or 0)


class AlbumCard(QWidget):
    opened = pyqtSignal(object)
    menu_requested = pyqtSignal(object, object)        # album, globální pozice

    def __init__(self, album, parent=None):
        super().__init__(parent)
        self.album = album
        self._img = None
        self._press = None
        self.drop_mode = None                           # "into" / "before" / "after" během tahu
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        kind = {"folder": tr("albums-kind-folder"), "smart": tr("albums-kind-smart")}.get(album["kind"], tr("albums-kind-album"))
        self.setAccessibleName(f"{kind} {album['name']}, {subtitle(album)}")
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.MouseButton.LeftButton and \
                (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
            self._press = None
            self._start_drag()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        pressed, self._press = self._press, None
        if e.button() == Qt.MouseButton.LeftButton and pressed is not None and \
                self.rect().contains(e.position().toPoint()):
            self.opened.emit(self.album)
            return
        super().mouseReleaseEvent(e)

    def _start_drag(self):
        mime = QMimeData()
        mime.setData(NODE_MIME, str(self.album["id"]).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self.grab().scaled(CARD_W // 2, CARD_H // 2, Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 3))
        drag.exec(Qt.DropAction.MoveAction)

    def set_drop_mode(self, mode):
        if mode != self.drop_mode:
            self.drop_mode = mode
            self.update()

    def contextMenuEvent(self, e):
        self.menu_requested.emit(self.album, e.globalPos())

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(self.album)
            return
        super().keyPressEvent(e)

    def _paint_folder(self, p, img_r, dpr):
        covers = [c for c in (self.album.get("covers") or []) if c][:albums.FOLDER_COVERS]
        if self._img is None:                   # mozaika 2 × 2 z obálek alb uvnitř (1 obálka = celá)
            w, h = round(img_r.width() * dpr), round(img_r.height() * dpr)
            canvas = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
            canvas.fill(Qt.GlobalColor.transparent)
            if covers:
                cp = QPainter(canvas)
                if len(covers) == 1:
                    cp.drawImage(0, 0, _cover(covers[0], w, h))
                else:
                    gap = round(2 * dpr)
                    tw, th = (w - gap) // 2, (h - gap) // 2
                    for i, c in enumerate(covers):
                        cp.drawImage((i % 2) * (tw + gap), (i // 2) * (th + gap), _cover(c, tw, th))
                cp.end()
            self._img = canvas if covers else QImage()
        if not self._img.isNull():
            p.drawImage(img_r, self._img)
            g = QLinearGradient(0, img_r.top(), 0, img_r.top() + 60)
            g.setColorAt(0, QColor(0, 0, 0, 130))
            g.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(img_r, g)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        t = tokens.palette()
        radius = float(tokens.RADIUS["lg"])
        hover = max(0.0, min(1.0, float(self._hover.value)))
        img_r = QRectF(0.5, 0.5, self.width() - 1, IMAGE_H)
        dpr = self.devicePixelRatioF()
        kind = self.album["kind"]
        p.save()
        p.setClipPath(smooth_rect_path(img_r, radius))
        p.fillRect(img_r, tokens.qcolor(t["surface_3"]))
        if kind == "folder":
            self._paint_folder(p, img_r, dpr)
        else:
            if self._img is None:
                self._img = _cover(self.album.get("cover"), round(img_r.width() * dpr), round(img_r.height() * dpr))
            if not self._img.isNull():
                p.drawImage(img_r, self._img)
        if self._img.isNull():                  # bez obálky: ikona (chytré album = jiskra)
            icon = {"smart": "sparkle", "folder": "folder"}.get(kind, "photos")
            pm = icons.pixmap(icon, tokens.qcolor(t["accent"] if kind != "manual" else t["text_tertiary"]), 40)
            p.drawPixmap(int(img_r.center().x() - 20), int(img_r.center().y() - 20), pm)
        if hover:
            p.fillRect(img_r, QColor(255, 255, 255, int(18 * hover)))
        if kind == "smart" and not self._img.isNull():
            g = QLinearGradient(0, img_r.top(), 0, img_r.top() + 60)
            g.setColorAt(0, QColor(0, 0, 0, 120))
            g.setColorAt(1, QColor(0, 0, 0, 0))
            p.fillRect(img_r, g)
        if self.drop_mode == "into":
            tint = tokens.qcolor(t["accent"])
            tint.setAlpha(60)
            p.fillRect(img_r, tint)
        p.restore()
        if kind in ("smart", "folder") and not self._img.isNull():      # odznak druhu
            p.drawPixmap(int(img_r.left() + 10), int(img_r.top() + 10),
                         icons.pixmap("sparkle" if kind == "smart" else "folder", QColor("#FFFFFF"), 18))
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
        if self.hasFocus() or self.drop_mode == "into":
            p.setPen(tokens.qcolor(t["accent"]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(img_r.adjusted(1, 1, -1, -1), radius))
        if self.drop_mode in ("before", "after"):        # čára vložení
            x = 0 if self.drop_mode == "before" else self.width() - 3
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tokens.qcolor(t["accent"]))
            p.drawRoundedRect(QRectF(x, 4, 3, IMAGE_H - 8), 1.5, 1.5)
        p.end()


class _UpTarget(QLabel):
    """Pruh „O úroveň výš“ – viditelný jen během tahu karty v podsložce."""

    def __init__(self, parent=None):
        super().__init__(tr("albums-drop-up-level"), parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(40)
        self.set_active(False)
        self.hide()

    def set_active(self, on):
        t = tokens.palette()
        border = t["accent"] if on else t["hairline_strong"]
        self.setStyleSheet(f"QLabel {{ color: {t['text_secondary']}; border: 1px dashed {border}; "
                           f"border-radius: 8px; margin: 8px 24px 0 24px; }}")


class AlbumsPanel(QWidget):
    """Obsah stránky Alba (jedna úroveň stromu). opened(node); požadavky na úpravy řeší main.py,
    přesuny přetažením panel zapíše sám přes edit(fn) a ohlásí changed()."""
    opened = pyqtSignal(object)
    rename_requested = pyqtSignal(object)
    delete_requested = pyqtSignal(object)
    export_requested = pyqtSignal(object)
    cover_requested = pyqtSignal(object)
    cover_reset_requested = pyqtSignal(object)
    changed = pyqtSignal()

    def __init__(self, connect, parent=None, folder_id=None, edit=None):
        super().__init__(parent)
        self.connect = connect
        self.folder_id = folder_id
        self._edit = edit
        self.cards = []
        self.albums = []
        self._parent_id = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.up = _UpTarget(self)
        lay.addWidget(self.up)
        body = (tr("albums-folder-empty-body")
                if folder_id is not None else
                tr("albums-empty-body"))
        self.empty = EmptyState("folder" if folder_id is not None else "album",
                                tr("albums-empty-folder") if folder_id is not None else tr("albums-empty-title"), body)
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
        self.setAcceptDrops(True)

    def edit(self, fn):
        if self._edit is not None:
            return self._edit(fn)
        conn = self.connect()
        try:
            albums.ensure_tables(conn)
            out = fn(conn)
            conn.commit()
            return True if out is None else out
        finally:
            conn.close()

    def load(self):
        conn = self.connect()
        try:
            albums.ensure_tables(conn)
            self.albums = albums.children(conn, self.folder_id)
            me = albums.get_album(conn, self.folder_id) if self.folder_id is not None else None
            self._parent_id = me["parent_id"] if me else None
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

    # ── nabídka ──
    def _menu(self, album, pos):
        menu = QMenu(self)
        act_open = menu.addAction(tr("action-open"))
        act_rename = menu.addAction(tr("action-rename-ellipsis"))
        act_cover = act_auto = act_export = None
        if album["kind"] == "manual":
            act_cover = menu.addAction(tr("albums-choose-cover"))
            if album.get("cover_id") is not None:
                act_auto = menu.addAction(tr("albums-auto-cover"))
        if album["kind"] in ("manual", "folder"):
            act_export = menu.addAction(tr("action-export-ellipsis"))
        move_targets = self._move_menu(menu, album)
        menu.addSeparator()
        act_delete = menu.addAction(tr("albums-delete-folder") if album["kind"] == "folder" else tr("albums-delete-album"))
        chosen = menu.exec(pos)
        if chosen is None:
            return
        if chosen == act_open:
            self.opened.emit(album)
        elif chosen == act_rename:
            self.rename_requested.emit(album)
        elif chosen == act_cover:
            self.cover_requested.emit(album)
        elif chosen == act_auto:
            self.cover_reset_requested.emit(album)
        elif chosen == act_export:
            self.export_requested.emit(album)
        elif chosen == act_delete:
            self.delete_requested.emit(album)
        elif chosen in move_targets:
            self.move(album["id"], move_targets[chosen], None)

    def _move_menu(self, menu, album):
        """Podnabídka „Přesunout do“: kořen + všechny složky kromě současné, sebe a potomků."""
        conn = self.connect()
        try:
            folders = albums.list_folders(conn)
            blocked = {album["id"]} | ({d["id"] for d in albums.descendants(conn, album["id"])}
                                       if album["kind"] == "folder" else set())
        finally:
            conn.close()
        sub = menu.addMenu(tr("albums-move-to"))
        targets = {}
        if album.get("parent_id") is not None:
            targets[sub.addAction(tr("albums-top-level"))] = None
        for fid, label in folders:
            if fid in blocked or fid == album.get("parent_id"):
                continue
            targets[sub.addAction(label)] = fid
        sub.setEnabled(bool(targets))
        return targets

    def move(self, node_id, parent_id, before_id):
        if self.edit(lambda conn: albums.move_node(conn, node_id, parent_id, before_id)) is None:
            return False
        self.load()
        self.changed.emit()
        return True

    # ── přetažení ──
    def _card_at(self, pos):
        for c in self.cards:
            if c.isVisible() and c.geometry().contains(c.parentWidget().mapFrom(self, pos)):
                return c
        return None

    def _clear_drop(self):
        for c in self.cards:
            c.set_drop_mode(None)
        self.up.set_active(False)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(NODE_MIME):
            self.up.setVisible(self.folder_id is not None)
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasFormat(NODE_MIME):
            return
        pos = e.position().toPoint()
        dragged = int(bytes(e.mimeData().data(NODE_MIME)).decode("ascii"))
        self._clear_drop()
        if self.up.isVisible() and self.up.geometry().contains(pos):
            self.up.set_active(True)
        else:
            card = self._card_at(pos)
            if card is not None and card.album["id"] != dragged:
                local = card.mapFrom(self, pos)
                card.set_drop_mode(drop_action(card.album["kind"], card.width(), local.x()))
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._clear_drop()
        self.up.hide()

    def dropEvent(self, e):
        if not e.mimeData().hasFormat(NODE_MIME):
            return
        pos = e.position().toPoint()
        dragged = int(bytes(e.mimeData().data(NODE_MIME)).decode("ascii"))
        to_parent = self.up.isVisible() and self.up.geometry().contains(pos)
        self._clear_drop()
        self.up.hide()
        e.acceptProposedAction()
        if to_parent:
            self.move(dragged, self._parent_id, None)
            return
        card = self._card_at(pos)
        if card is None:                                  # prázdné místo = na konec úrovně
            self.move(dragged, self.folder_id, None)
            return
        if card.album["id"] == dragged:
            return
        mode = drop_action(card.album["kind"], card.width(), card.mapFrom(self, pos).x())
        if mode == "into":
            self.move(dragged, card.album["id"], None)
            return
        ids = [c.album["id"] for c in self.cards if c.album["id"] != dragged]
        at = ids.index(card.album["id"]) + (1 if mode == "after" else 0)
        self.move(dragged, self.folder_id, ids[at] if at < len(ids) else None)

    # ── rozvržení ──
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
