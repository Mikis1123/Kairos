"""Živý náhled Live Photo v mřížce (Polish P4).

Kurzor ~HOVER_DELAY_MS nad dlaždicí Live Photo -> pás snímků z scrub_strip (z RAM / disku hned,
jinak ho StripManager vytvoří na pozadí) -> smyčka ~12 snímků/s přímo v dlaždici, po odjetí
zpět statický snímek. Žádný přehrávač, dekodér ani zvukové zařízení.

Paměť: snímky se dekódují postupně (jeden za tik) rovnou v rozměru dlaždice a drží se v LRU
pro CACHE_ENTRIES naposledy přehraných fotek (~4 × 36 snímků velikosti dlaždice). Zastaví se
při odjetí, posunu mřížky, skrytí stránky, neaktivním okně i při omezených animacích.
"""
import os
from collections import OrderedDict

from PyQt6.QtCore import QObject, QSize, QTimer
from PyQt6.QtGui import QPixmap

import motion
import scrub_strip

HOVER_DELAY_MS = 350
FRAME_MS = 83                # ~12 snímků/s = hustý průchod pásu (DENSE_FPS)
MAX_FRAMES = 36              # ~3 s Live Photo
CACHE_ENTRIES = 4
POLL_MS = 120                # čekání na rozpracovaný pás
WAIT_STRIP_MS = 6000
FADE_FRAMES = 3              # prolnutí ze statického snímku na začátku


def pick_indices(count, k=MAX_FRAMES):
    """Rovnoměrně rozprostřené indexy snímků pásu (nejvýš k)."""
    if count <= k:
        return list(range(count))
    step = count / float(k)
    return [int(i * step) for i in range(k)]


class _Entry:
    __slots__ = ("indices", "frames")

    def __init__(self, indices):
        self.indices = indices
        self.frames = [None] * len(indices)


class LivePreview(QObject):
    """Jeden na mřížku. Mřížka volá hover(row) a stop(); delegát kreslí frame_for(row)."""

    def __init__(self, grid, strips=None):
        super().__init__(grid)
        # pillar 2: media id -> quick_edit.Adjustments | None (the grid's EDITS); the motion is
        # shown with the still's crop and tone, so hover never "un-edits" the photo
        self.edit_for = lambda _media_id: None
        self._edit = None
        self.grid = grid
        self._strips = strips
        self.row = -1                 # řádek pod kurzorem (kandidát)
        self.playing_row = -1
        self._video = None
        self._strip = None
        self._entry = None
        self._pos = 0
        self._size = QSize()
        self._shown = 0               # počet zobrazených snímků (prolnutí na začátku)
        self.cache = OrderedDict()    # (video, w, h) -> _Entry
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(HOVER_DELAY_MS)
        self._delay.timeout.connect(self._begin)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check_strip)
        self._poll_left = 0
        self._tick = QTimer(self)
        self._tick.setInterval(FRAME_MS)
        self._tick.timeout.connect(self._advance)

    def strips(self):
        if self._strips is None:
            self._strips = scrub_strip.manager()
        return self._strips

    # ── vstup z mřížky ──
    def video_for_row(self, row):
        model = self.grid.photo_model
        if not 0 <= row < len(model.records):
            return None
        rec = model.records[row]
        if rec[3] != "live_photo" or not rec[4] or not os.path.exists(rec[4]):
            return None
        return rec[4]

    def hover(self, row):
        if row == self.row:
            return
        self.stop()
        self.row = row
        if row >= 0 and not motion.reduced() and self.video_for_row(row):
            self._delay.start()

    def stop(self):
        """Odjetí, posun, skrytí: zpět statický snímek. Snímky v LRU zůstávají."""
        self._delay.stop()
        self._poll.stop()
        self._tick.stop()
        row, self.playing_row = self.playing_row, -1
        self.row = -1
        self._strip = self._entry = self._video = None
        if row >= 0:
            self.grid._repaint_row(row)

    def is_active(self):
        return self._tick.isActive() or self._poll.isActive() or self._delay.isActive()

    # ── pás ──
    def _allowed(self):
        win = self.grid.window()
        return self.grid.isVisible() and (win is None or win.isActiveWindow()) and not motion.reduced()

    def _begin(self):
        video = self.video_for_row(self.row)
        if video is None or not self._allowed():
            return
        self._video = video
        self._strip = self.strips().request(video)
        self._poll_left = WAIT_STRIP_MS // POLL_MS
        if not self._check_strip():
            self._poll.start()

    def _check_strip(self):
        strip = self._strip if self._strip is not None else self.strips().get(self._video or "")
        if strip is not None and strip.complete and len(strip):
            self._poll.stop()
            self._play(strip)
            return True
        self._poll_left -= 1
        if self._poll_left <= 0 or not self._allowed():
            self._poll.stop()
        elif self._strip is None:
            self._strip = self.strips().get(self._video or "")
        return False

    def _cell_size(self):
        rect = self.grid.visualRect(self.grid.photo_model.index(self.row, 0))
        dpr = self.grid.devicePixelRatioF()
        return QSize(max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr)))

    def _row_edit(self, row):
        model = self.grid.photo_model
        try:
            return self.edit_for(int(model.records[row][0])) if 0 <= row < len(model.records) else None
        except Exception:
            return None

    def _play(self, strip):
        size = self._cell_size()
        self._edit = self._row_edit(self.row)
        key = (self._video, size.width(), size.height(), self._edit.key() if self._edit is not None else "")
        entry = self.cache.get(key)
        if entry is None or len(entry.indices) != len(pick_indices(len(strip))):
            entry = _Entry(pick_indices(len(strip)))
            self.cache[key] = entry
        self.cache.move_to_end(key)
        while len(self.cache) > CACHE_ENTRIES:
            self.cache.popitem(last=False)
        self._strip, self._entry, self._size = strip, entry, size
        self.playing_row, self._pos, self._shown = self.row, -1, 0
        self._advance()
        self._tick.start()

    def _advance(self):
        if self._entry is None or not self._allowed():
            self.stop()
            return
        self._pos = (self._pos + 1) % len(self._entry.indices)
        if self._entry.frames[self._pos] is None:          # jeden dekód za tik
            img = self._decode_frame(self._entry.indices[self._pos])
            if img is not None:
                pm = QPixmap.fromImage(img)
                pm.setDevicePixelRatio(self.grid.devicePixelRatioF())
                self._entry.frames[self._pos] = pm
        self._shown += 1
        self.grid._repaint_row(self.playing_row)

    def _decode_frame(self, index):
        if self._edit is None:
            return self._strip.decode(index, self._size)
        img = self._strip.decode(index, None)               # full frame: the crop is in its coordinates
        if img is None:
            return None
        import quick_edit
        from PyQt6.QtCore import Qt
        img = quick_edit.render_qimage(img, self._edit, fast=True)
        return img.scaled(self._size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)

    # ── kreslení ──
    def frame_for(self, row):
        """(QPixmap, krytí) pro delegáta, nebo None (kreslí se statický náhled)."""
        if row != self.playing_row or self._entry is None:
            return None
        pm = self._entry.frames[self._pos] if 0 <= self._pos < len(self._entry.frames) else None
        if pm is None:
            return None
        return pm, min(1.0, self._shown / float(FADE_FRAMES))

    def cached_frames(self):
        return sum(1 for e in self.cache.values() for f in e.frames if f is not None)
