"""Alba v2 – přeuspořádání položek ručního alba přetažením v mřížce.

AppleGridView zůstává beze změny (je laděný na výkon): GridReorder se k ní připojí filtrem
událostí na viewportu. Tah začne jen stiskem na UŽ VYBRANÉ dlaždici (jinak tah = výběr gumou
jako dřív), během tahu ukazuje svislou čáru v místě vložení a po puštění zavolá
on_move(ids, before_id) – before_id = položka, před kterou se vkládá (None = na konec).
"""
from PyQt6.QtCore import QMimeData, QObject, QPoint, QEvent, Qt
from PyQt6.QtGui import QColor, QDrag, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

import tokens

MIME = "application/x-kairos-album-items"


def insertion_row(rect_center_x, pos_x, row, count):
    """Řádek, před který se vkládá: levá polovina dlaždice = před ni, pravá = za ni."""
    if row < 0:
        return count
    return row if pos_x < rect_center_x else row + 1


class _Caret(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.hide()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = tokens.qcolor(tokens.palette()["accent"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(self.rect().adjusted(1, 0, -1, 0), 2, 2)
        p.end()


class GridReorder(QObject):
    def __init__(self, grid, on_move, enabled=lambda: True):
        super().__init__(grid)
        self.grid = grid
        self.on_move = on_move
        self.enabled = enabled
        self._press = None
        self._drop_before = None
        self.caret = _Caret(grid.viewport())
        grid.viewport().setAcceptDrops(True)
        grid.viewport().installEventFilter(self)

    # ── pomocné ──
    def _ids_for_rows(self, rows):
        recs = self.grid.photo_model.records
        return [int(recs[r][0]) for r in sorted(rows) if 0 <= r < len(recs)]

    def _target(self, pos):
        """(before_row, caret_rect) pro pozici ve viewportu."""
        model = self.grid.photo_model
        count = model.rowCount()
        idx = self.grid.indexAt(pos)
        if not idx.isValid():
            if count == 0:
                return 0, None
            last = self.grid.visualRect(model.index(count - 1, 0))
            return count, (last.right() - 1, last.top(), last.height())
        r = self.grid.visualRect(idx)
        row = insertion_row(r.center().x(), pos.x(), idx.row(), count)
        x = r.left() if row == idx.row() else r.right() + 1
        return row, (x - 2, r.top() + 4, r.height() - 8)

    def _drag_pixmap(self, rows):
        first = self.grid.visualRect(self.grid.photo_model.index(min(rows), 0))
        pm = self.grid.viewport().grab(first).scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation)
        if len(rows) > 1:
            out = QPixmap(pm.size())
            out.fill(Qt.GlobalColor.transparent)
            p = QPainter(out)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.drawPixmap(0, 0, pm)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(tokens.qcolor(tokens.palette()["accent"]))
            p.drawEllipse(out.width() - 30, 4, 26, 26)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(out.width() - 30, 4, 26, 26, int(Qt.AlignmentFlag.AlignCenter), str(len(rows)))
            p.end()
            pm = out
        return pm

    def _start_drag(self):
        rows = self.grid.selected_row_indices()
        ids = self._ids_for_rows(rows)
        if not ids:
            return
        mime = QMimeData()
        mime.setData(MIME, ",".join(map(str, ids)).encode("ascii"))
        drag = QDrag(self.grid.viewport())
        drag.setMimeData(mime)
        pm = self._drag_pixmap(rows)
        drag.setPixmap(pm)
        drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec(Qt.DropAction.MoveAction)
        self.caret.hide()

    # ── události ──
    def eventFilter(self, obj, e):
        try:
            return self._handle(e)
        except Exception as ex:                     # PyQt6: výjimka z filtru = pád procesu
            print(f"[Alba] přetažení: {ex}")
            return False

    def _handle(self, e):
        t = e.type()
        if t == QEvent.Type.MouseButtonPress:
            self._press = None
            if self.enabled() and e.button() == Qt.MouseButton.LeftButton and not e.modifiers():
                idx = self.grid.indexAt(e.position().toPoint())
                if idx.isValid() and self.grid.selectionModel().isSelected(idx):
                    self._press = e.position().toPoint()
            return False
        if t == QEvent.Type.MouseMove and self._press is not None:
            if not (e.buttons() & Qt.MouseButton.LeftButton):
                self._press = None
                return False
            if (e.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                self._start_drag()
                return True
            return True                              # do vzdálenosti tahu žádná guma
        if t == QEvent.Type.MouseButtonRelease:
            self._press = None
            return False
        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if not e.mimeData().hasFormat(MIME):
                return False
            row, caret = self._target(e.position().toPoint())
            self._drop_before = row
            if caret:
                x, y, h = caret
                self.caret.setGeometry(x, y, 4, max(8, h))
                self.caret.show()
                self.caret.raise_()
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
            return True
        if t == QEvent.Type.DragLeave:
            self.caret.hide()
            return False
        if t == QEvent.Type.Drop:
            self.caret.hide()
            if not e.mimeData().hasFormat(MIME):
                return False
            ids = [int(x) for x in bytes(e.mimeData().data(MIME)).decode("ascii").split(",") if x]
            row, _ = self._target(e.position().toPoint())
            recs = self.grid.photo_model.records
            moving = set(ids)
            # před první NEpřesouvanou položku od místa vložení (přesouvané se z pořadí vyjmou)
            before = next((int(recs[r][0]) for r in range(row, len(recs)) if int(recs[r][0]) not in moving), None)
            e.setDropAction(Qt.DropAction.MoveAction)
            e.accept()
            self.on_move(ids, before)
            return True
        return False
