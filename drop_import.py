"""Přetažení složek a fotek do okna Kairos (Polish P3).

DropRouter je filtr událostí aplikace jen pro čtyři typy tažení: mapa (QWebEngineView) i video
(QQuickWidget) by si přetažení jinak nechaly pro sebe. Zpracuje jen tažení nad hlavním oknem,
kterým rozumí (lokální složky nebo podporované soubory) – ostatní propustí beze změny.
DropOverlay je jediný překryv vytvořený při prvním tažení: zářící lem zvýraznění a kapsle
„Pusťte pro import“; nereaguje na myš a mimo tažení je skrytý.
"""
from i18n import tr
import os

from PyQt6.QtCore import QEvent, QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget

import icons
import motion
import tokens
from scanner import SUPPORTED_EXTS
from ui_controls import pin_overlay, smooth_rect_path

DRAG_EVENTS = (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.DragLeave, QEvent.Type.Drop)


def _is_inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return path != folder and path.startswith(folder.rstrip("\\/") + os.sep)


def classify_paths(paths):
    """Přetažené cesty -> (složky, soubory). Jen existující složky a podporované soubory;
    co leží uvnitř jiné přetažené složky, se vynechá (naskenuje se s ní)."""
    folders, files = [], []
    for p in paths:
        if not p:
            continue
        p = os.path.normpath(p)
        if os.path.isdir(p):
            folders.append(p)
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXTS:
            files.append(p)
    folders = sorted(set(folders), key=len)
    top = []
    for f in folders:
        if not any(_is_inside(f, t) for t in top):
            top.append(f)
    files = [f for f in dict.fromkeys(files) if not any(_is_inside(f, t) for t in top)]
    return top, files


def mime_paths(mime):
    if mime is None or not mime.hasUrls():
        return []
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]


def describe(folders, files):
    """„2 složky · 12 souborů“ pro kapsli překryvu."""
    parts = []
    if folders:
        n = len(folders)
        parts.append(tr("quoted", text=os.path.basename(folders[0]) or folders[0]) if n == 1 else
                     tr("count-folders", count=n))
    if files:
        n = len(files)
        parts.append(tr("count-files", count=n))
    return " · ".join(parts)


class DropOverlay(QWidget):
    """Zářící lem + kapsle uprostřed. Průhledný pro myš, opacity přes motion.Tween."""

    def __init__(self, host):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.detail = ""
        self._opacity = motion.Tween(0.0, lambda v: self.update(), self)
        self._opacity.finished.connect(self._after_fade)
        pin_overlay(self, host)
        self.hide()

    def appear(self, detail):
        self.detail = detail
        if not self.isVisible():
            self.setGeometry(self.parentWidget().rect())
            self.show()
        self.raise_()
        self._opacity.to(1.0, "micro")
        self.update()

    def vanish(self):
        self._opacity.to(0.0, "standard")

    def _after_fade(self):
        if self._opacity.value <= 0.0:
            self.hide()

    def paintEvent(self, _):
        op = max(0.0, min(1.0, float(self._opacity.value)))
        if op <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(op)
        t = tokens.palette()
        acc = tokens.qcolor(t["accent"])
        p.fillRect(self.rect(), QColor(0, 0, 0, 70))
        r = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        radius = float(tokens.RADIUS["xl"])
        for width, alpha in ((14, 28), (8, 55), (2.5, 255)):      # záře zvenku dovnitř
            c = QColor(acc)
            c.setAlpha(alpha)
            p.setPen(QPen(c, width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(r, radius))

        title_f = tokens.font("headline")
        sub_f = QFont(tokens.ui_family())
        sub_f.setPixelSize(12)
        p.setFont(title_f)
        title = tr("drop-release-to-import")
        tw = p.fontMetrics().horizontalAdvance(title)
        th = p.fontMetrics().height()
        sub = (self.detail + " · " if self.detail else "") + tr("drop-in-place")
        p.setFont(sub_f)
        sw = p.fontMetrics().horizontalAdvance(sub)
        sh = p.fontMetrics().height()
        w = max(tw + 44, sw) + 48
        h = 22 + th + 6 + sh + 20
        pill = QRectF(r.center().x() - w / 2, r.center().y() - h / 2, w, h)
        p.setPen(QPen(tokens.qcolor(t["separator"]), 1))
        p.setBrush(tokens.qcolor(t["surface"]))
        p.drawPath(smooth_rect_path(pill, radius))
        top = pill.top() + 22
        icons.paint(p, "import", QRectF(pill.center().x() - (tw + 30) / 2, top + (th - 22) / 2, 22, 22), acc)
        p.setFont(title_f)
        p.setPen(tokens.qcolor(t["text"]))
        p.drawText(QRectF(pill.center().x() - (tw + 30) / 2 + 30, top, tw + 2, th),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), title)
        p.setFont(sub_f)
        p.setPen(tokens.qcolor(t["text_secondary"]))
        p.drawText(QRectF(pill.left(), top + th + 6, pill.width(), sh), int(Qt.AlignmentFlag.AlignHCenter), sub)
        p.end()


class DropRouter(QObject):
    """Filtr tažení pro celé hlavní okno. dropped(složky, soubory) po puštění."""
    dropped = pyqtSignal(list, list)

    def __init__(self, window, host):
        super().__init__(window)
        self.window, self.host = window, host
        self.overlay = None
        self._active = None                  # (složky, soubory) právě taženého obsahu
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def detach(self):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

    def _ours(self, obj):
        if not isinstance(obj, QWidget):
            return False
        try:
            return obj.window() is self.window and self.window.isEnabled()
        except RuntimeError:
            return False

    def eventFilter(self, obj, event):
        # PyQt6: výjimka z virtuální metody = pád procesu -> vše v try
        try:
            if event.type() not in DRAG_EVENTS or not self._ours(obj):
                return False
            return self.handle(event)
        except Exception as e:
            print(f"[Drop] {e}")
            return False

    def handle(self, event):
        kind = event.type()
        if kind == QEvent.Type.DragEnter:
            folders, files = classify_paths(mime_paths(event.mimeData()))
            if not folders and not files:
                self._active = None
                return False                  # cizí obsah (text, obrázek z prohlížeče) nechat být
            self._active = (folders, files)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self._show(describe(folders, files))
            return True
        if self._active is None:
            return False
        if kind == QEvent.Type.DragMove:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return True
        if kind == QEvent.Type.DragLeave:
            # přechod mezi podřízenými widgety posílá Leave + Enter; skrýt jen při opuštění okna
            if not self.window.geometry().contains(QCursor.pos()):
                self.cancel()
            return True
        if kind == QEvent.Type.Drop:
            folders, files = self._active
            self.cancel()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.dropped.emit(folders, files)
            return True
        return False

    def _show(self, detail):
        if self.overlay is None:
            self.overlay = DropOverlay(self.host)
        self.overlay.appear(detail)

    def cancel(self):
        self._active = None
        if self.overlay is not None:
            self.overlay.vanish()
