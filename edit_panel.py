"""Quick Edits UI in the viewer (pillar 2): side panel, crop overlay and the edit session.

EditController owns one session: the display-sized source image of the current photo, the
working Adjustments and the render cadence.
  * While a slider or crop handle moves, the frame is rendered from a ≤ PROXY_LONG_EDGE proxy
    (quick_edit, fast warp) and shown through MediaCanvas.show_edit_frame (scaled at paint time,
    no re-fit of the source) – a few ms per frame.
  * REFINE_MS after the last change the full display-size frame replaces it (cubic warp).
  * During the session the canvas shows the straightened FULL frame and CropOverlay draws the
    crop on top of it; the cropped result appears on Done.
Persisting is the viewer's job (on_done callback) – this module never touches the database.
"""
from i18n import tr
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

import quick_edit as qe

REFINE_MS = 90               # full-quality frame this long after the last change
HANDLE_PX = 14               # hit radius of crop corner handles (logical px)
EDGE_PX = 9                  # hit band of crop edges
PANEL_WIDTH = 300
ANGLE_STEPS = 10             # straighten slider units per degree (0.1° resolution)

# (key, catalog id) – order of the tone sliders in the panel
TONE_ROWS = (("exposure", "edit-exposure"), ("highlights", "edit-highlights"), ("shadows", "edit-shadows"),
             ("contrast", "edit-contrast"), ("temperature", "edit-warmth"))
ASPECT_LABELS = (("free", "edit-aspect-free"), ("1:1", None), ("16:9", None), ("4:5", None))


class _Slider(QSlider):
    """Horizontal slider: double-click returns to the neutral value; reports drag start / end."""
    reset_requested = pyqtSignal()

    def __init__(self, lo, hi, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(lo, hi)
        self.setValue(0)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setPageStep(max(1, (hi - lo) // 20))

    def mouseDoubleClickEvent(self, e):
        self.reset_requested.emit()
        e.accept()


class EditPanel(QWidget):
    """The controls. Emits changed(Adjustments, interactive) for every edit; done / cancelled /
    revert for the buttons. set_adjustments() loads values without emitting."""
    changed = pyqtSignal(object, bool)
    done = pyqtSignal()
    cancelled = pyqtSignal()
    revert = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EditPanel")
        self.setFixedWidth(PANEL_WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("""
            QWidget#EditPanel { background: rgba(28, 28, 30, 0.94); border-left: 1px solid rgba(255,255,255,0.08); }
            QLabel { color: #F5F5F7; background: transparent; }
            QLabel#EditHeading { font-size: 15px; font-weight: 600; }
            QLabel#EditSection { font-size: 11px; font-weight: 600; color: #8E8E93; letter-spacing: 0.4px; }
            QLabel#EditValue { color: #AEAEB2; font-size: 12px; }
            QPushButton#EditAspect { background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);
                border-radius: 8px; padding: 4px 0; font-size: 12px; font-weight: 600; color: #F5F5F7; }
            QPushButton#EditAspect:checked { background: #F5F5F7; color: #1C1C1E; }
            QPushButton#EditDone { background: #0A84FF; border: none; color: white; }
            QPushButton#EditDone:hover { background: #409CFF; }
        """)
        self._adj = qe.Adjustments()
        self._loading = False
        self._dragging = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(tr("edit-title"))
        title.setObjectName("EditHeading")
        head.addWidget(title)
        head.addStretch(1)
        self.btn_cancel = QPushButton(tr("action-cancel"))
        self.btn_cancel.clicked.connect(self.cancelled.emit)
        self.btn_done = QPushButton(tr("common-done"))
        self.btn_done.setObjectName("EditDone")
        self.btn_done.clicked.connect(self.done.emit)
        head.addWidget(self.btn_cancel)
        head.addWidget(self.btn_done)
        lay.addLayout(head)

        lay.addSpacing(6)
        lay.addWidget(self._section(tr("edit-section-crop")))
        aspects = QHBoxLayout()
        aspects.setSpacing(6)
        self.aspect_buttons = {}
        for key, label in ASPECT_LABELS:
            b = QPushButton(tr(label) if label else key)
            b.setObjectName("EditAspect")
            b.setCheckable(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _c, k=key: self._on_aspect(k))
            aspects.addWidget(b)
            self.aspect_buttons[key] = b
        lay.addLayout(aspects)
        self.angle_slider, self.angle_value = self._row(lay, tr("edit-straighten"),
                                                        int(-qe.ANGLE_LIMIT * ANGLE_STEPS),
                                                        int(qe.ANGLE_LIMIT * ANGLE_STEPS), "angle")

        lay.addSpacing(6)
        lay.addWidget(self._section(tr("edit-section-light")))
        self.tone_sliders = {}
        self.tone_values = {}
        for key, label in TONE_ROWS:
            s, v = self._row(lay, tr(label), -100, 100, key)
            self.tone_sliders[key] = s
            self.tone_values[key] = v
        lay.addStretch(1)
        self.btn_revert = QPushButton(tr("edit-revert"))
        self.btn_revert.clicked.connect(self.revert.emit)
        lay.addWidget(self.btn_revert)
        self.set_adjustments(self._adj)

    # ── building blocks ──
    def _section(self, text):
        lbl = QLabel(text.upper())
        lbl.setObjectName("EditSection")
        return lbl

    def _row(self, lay, label, lo, hi, key):
        top = QHBoxLayout()
        name = QLabel(label)
        value = QLabel("0")
        value.setObjectName("EditValue")
        top.addWidget(name)
        top.addStretch(1)
        top.addWidget(value)
        lay.addLayout(top)
        s = _Slider(lo, hi)
        s.valueChanged.connect(lambda v, k=key: self._on_slider(k, v))
        s.sliderPressed.connect(lambda: self._set_dragging(True))
        s.sliderReleased.connect(lambda: self._set_dragging(False))
        s.reset_requested.connect(lambda k=key, sl=s: sl.setValue(0))
        lay.addWidget(s)
        return s, value

    # ── state ──
    def adjustments(self):
        return self._adj

    def set_adjustments(self, adj):
        self._loading = True
        try:
            self._adj = adj.normalized()
            self.angle_slider.setValue(int(round(self._adj.angle * ANGLE_STEPS)))
            self.angle_value.setText(_fmt_angle(self._adj.angle))
            for key, s in self.tone_sliders.items():
                v = int(round(getattr(self._adj, key)))
                s.setValue(v)
                self.tone_values[key].setText(_fmt_signed(v))
            for key, b in self.aspect_buttons.items():
                b.setChecked(key == self._adj.aspect)
        finally:
            self._loading = False

    def _set_dragging(self, on):
        self._dragging = on
        if not on:
            self.changed.emit(self._adj, False)

    def _on_slider(self, key, value):
        if self._loading:
            return
        a = qe.Adjustments(**self._adj.__dict__)
        if key == "angle":
            a.angle = value / float(ANGLE_STEPS)
            self.angle_value.setText(_fmt_angle(a.angle))
        else:
            setattr(a, key, float(value))
            self.tone_values[key].setText(_fmt_signed(value))
        self._adj = a.normalized()
        self.changed.emit(self._adj, self._dragging)

    def _on_aspect(self, key):
        for k, b in self.aspect_buttons.items():
            b.setChecked(k == key)
        self.aspect_requested(key)

    def aspect_requested(self, key):
        """Replaced by EditController (needs the image size to fit the ratio)."""


def _fmt_signed(v):
    v = int(round(v))
    return f"+{v}" if v > 0 else str(v)


def _fmt_angle(a):
    return f"{a:+.1f}°" if abs(a) >= 0.05 else "0°"


class CropOverlay(QWidget):
    """Crop rectangle over the photo: dims the outside, rule-of-thirds while dragging, corner
    handles. Coordinates are fractions of the straightened frame (quick_edit)."""
    crop_changed = pyqtSignal(tuple, bool)       # crop, interactive

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.photo = QRectF()          # where the straightened frame is on screen (this widget's coords)
        self.crop = (0.0, 0.0, 1.0, 1.0)
        self.ratio = None              # pixel ratio to keep (None = free)
        self.frame_px = (1, 1)         # pixel size of the frame (ratio maths)
        self._drag = None              # (handle, start pos, start crop)

    def set_state(self, photo, crop, ratio, frame_px):
        self.photo, self.crop, self.ratio, self.frame_px = QRectF(photo), tuple(crop), ratio, frame_px
        self.update()

    def crop_rect(self):
        x, y, w, h = self.crop
        p = self.photo
        return QRectF(p.left() + x * p.width(), p.top() + y * p.height(), w * p.width(), h * p.height())

    def hit(self, pos):
        r = self.crop_rect()
        corners = {"tl": r.topLeft(), "tr": r.topRight(), "bl": r.bottomLeft(), "br": r.bottomRight()}
        for name, c in corners.items():
            if abs(pos.x() - c.x()) <= HANDLE_PX and abs(pos.y() - c.y()) <= HANDLE_PX:
                return name
        inside_y = r.top() <= pos.y() <= r.bottom()
        inside_x = r.left() <= pos.x() <= r.right()
        if inside_y and abs(pos.x() - r.left()) <= EDGE_PX:
            return "l"
        if inside_y and abs(pos.x() - r.right()) <= EDGE_PX:
            return "r"
        if inside_x and abs(pos.y() - r.top()) <= EDGE_PX:
            return "t"
        if inside_x and abs(pos.y() - r.bottom()) <= EDGE_PX:
            return "b"
        if r.contains(pos):
            return "move"
        return None

    _CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                "move": Qt.CursorShape.SizeAllCursor}

    def mousePressEvent(self, e):
        h = self.hit(e.position())
        if h and e.button() == Qt.MouseButton.LeftButton:
            self._drag = (h, e.position(), self.crop)
            e.accept()
            self.update()
        else:
            e.ignore()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            h = self.hit(e.position())
            self.setCursor(self._CURSORS.get(h, Qt.CursorShape.ArrowCursor))
            return
        handle, start, crop0 = self._drag
        pw, ph = max(1.0, self.photo.width()), max(1.0, self.photo.height())
        d = e.position() - start
        self.crop = qe.drag_crop(crop0, handle, d.x() / pw, d.y() / ph, self.ratio, *self.frame_px)
        self.crop_changed.emit(self.crop, True)
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.crop_changed.emit(self.crop, False)
            self.update()

    def paintEvent(self, _):
        if self.photo.isEmpty():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.crop_rect()
        shade = QPainterPath()
        shade.addRect(self.photo)
        hole = QPainterPath()
        hole.addRect(r)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 150))
        if self._drag is not None:                       # rule of thirds while adjusting
            p.setPen(QPen(QColor(255, 255, 255, 110), 1))
            for i in (1, 2):
                x = r.left() + r.width() * i / 3
                y = r.top() + r.height() * i / 3
                p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
                p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)
        p.setPen(QPen(QColor(255, 255, 255), 4, cap=Qt.PenCapStyle.SquareCap))
        L = 18
        for (cx, cy, sx, sy) in ((r.left(), r.top(), 1, 1), (r.right(), r.top(), -1, 1),
                                 (r.left(), r.bottom(), 1, -1), (r.right(), r.bottom(), -1, -1)):
            p.drawLine(QPointF(cx, cy), QPointF(cx + sx * L, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + sy * L))
        p.end()


class EditController(QObject):
    """One edit session on the viewer canvas (see module docstring)."""
    finished = pyqtSignal(object, bool)          # Adjustments, saved?

    def __init__(self, canvas, panel, overlay, parent=None):
        super().__init__(parent)
        self.canvas, self.panel, self.overlay = canvas, panel, overlay
        self.source = None             # display-size QImage of the original (unedited)
        self.proxy = None
        self.proxy_k = 1.0
        self.adj = qe.Adjustments()
        self.initial = qe.Adjustments()
        self.active = False
        self.frames = 0                # rendered frames (bench / tests)
        self.last_ms = 0.0
        self._refine = QTimer(self)
        self._refine.setSingleShot(True)
        self._refine.setInterval(REFINE_MS)
        self._refine.timeout.connect(lambda: self.render(final=True))
        panel.changed.connect(self._on_panel)
        panel.aspect_requested = self._on_aspect
        panel.done.connect(self.commit)
        panel.cancelled.connect(self.cancel)
        panel.revert.connect(self.revert)
        overlay.crop_changed.connect(self._on_crop)

    # ── session ──
    def start(self, source, adj):
        from PyQt6.QtCore import Qt as _Qt
        self.source = source
        self.initial = (adj or qe.Adjustments()).normalized()
        self.adj = self.initial
        w, h = source.width(), source.height()
        self.proxy_k = qe.proxy_scale(w, h)
        self.proxy = source if self.proxy_k >= 1.0 else source.scaled(
            max(1, round(w * self.proxy_k)), max(1, round(h * self.proxy_k)),
            _Qt.AspectRatioMode.IgnoreAspectRatio, _Qt.TransformationMode.SmoothTransformation)
        self.active = True
        self.panel.set_adjustments(self.adj)
        self.render(final=True)

    def frame_size(self):
        return (self.source.width(), self.source.height()) if self.source is not None else (1, 1)

    def _ratio(self):
        return qe.aspect_ratio(self.adj.aspect, *self.frame_size())

    def _session_view(self, adj):
        """What the canvas shows while editing: the straightened full frame (crop drawn on top)."""
        v = qe.Adjustments(**adj.__dict__)
        v.crop = (0.0, 0.0, 1.0, 1.0)
        return v

    def render(self, final=False):
        import time
        if not self.active or self.source is None:
            return
        t0 = time.perf_counter()
        view = self._session_view(self.adj)
        if final:
            img = qe.render_qimage(self.source, view)
        else:
            img = qe.render_qimage(self.proxy, view, fast=True)
        self.canvas.show_edit_frame(img)
        self.frames += 1
        self.last_ms = (time.perf_counter() - t0) * 1000.0
        self._sync_overlay()

    def _sync_overlay(self):
        photo = self.canvas_photo_rect()
        self.overlay.set_state(photo, self.adj.crop, self._ratio(), self.frame_size())

    def canvas_photo_rect(self):
        """Photo rect in the overlay's coordinates."""
        item = self.canvas.photo_item
        pm = item.pixmap()
        if pm.isNull():
            return QRectF()
        size = pm.deviceIndependentSize() * item.scale()
        tl = self.canvas.viewport().mapTo(self.overlay.parentWidget(), item.pos().toPoint())
        tl = self.overlay.mapFrom(self.overlay.parentWidget(), tl)
        return QRectF(tl.x(), tl.y(), size.width(), size.height())

    def _changed(self, interactive):
        if interactive:
            self.render(final=False)
            self._refine.start()
        else:
            self._refine.stop()
            self.render(final=True)

    def _on_panel(self, adj, interactive):
        crop, aspect = self.adj.crop, self.adj.aspect
        self.adj = qe.Adjustments(**{**adj.__dict__, "crop": crop, "aspect": aspect}).normalized()
        self._changed(interactive)

    def _on_aspect(self, key):
        self.adj = qe.with_aspect(self.adj, key, *self.frame_size())
        self.panel.set_adjustments(self.adj)
        self._sync_overlay()

    def _on_crop(self, crop, interactive):
        self.adj = qe.Adjustments(**{**self.adj.__dict__, "crop": crop}).normalized()
        self.overlay.update()

    def revert(self):
        self.adj = qe.Adjustments()
        self.panel.set_adjustments(self.adj)
        self._changed(False)

    def commit(self):
        if not self.active:
            return
        self._end()
        self.finished.emit(self.adj, True)

    def cancel(self):
        if not self.active:
            return
        self._end()
        self.finished.emit(self.initial, False)

    def _end(self):
        self.active = False
        self._refine.stop()
