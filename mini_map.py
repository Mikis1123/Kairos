"""Native mini-map of the viewer's Info panel (Sprint 3, 2026-11-05).

Replaces mini_map.html: a second Chromium page (QWebEngineView + MapLibre) that cost ~1 s on the
GUI thread the first time Info showed a place, its own renderer process and GPU context, and
went blank whenever the network was missing. The panel only ever showed a static place, so it is
now drawn by Qt: raster tiles of the light Esri World Street Map (the main map's Standard raster,
the same tile keys) composited around the photo's position and a QPainter "you are here" dot.

  * Tiles come from the app's SQLite tile store (tile_store.py) on small daemon worker threads:
    a stored tile is a disk read, a missing one is downloaded once through the store and kept
    for good, an expired one is still shown offline. Nothing touches the network on the GUI
    thread, and a closing app never waits for a worker.
  * Progressive: one tile OVERVIEW_DROP levels up covers the whole panel and arrives first (a
    soft preview, like the grid's micro previews), then the sharp tiles fill in from the centre
    out. Any missing tile is drawn from the deepest cached ancestor, so the panel is never empty.
  * HiDPI: on a >= 1.5x screen the tiles come one level deeper and are drawn at 1:1 device pixels.
  * A click emits clicked(lat, lon); the main window opens the Map tab there.
"""
import math
import queue
import threading
import zlib
from collections import OrderedDict

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QSizePolicy, QWidget

import tile_store

TILE_HOST = "server.arcgisonline.com"
TILE_PATH = "ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}"
ATTRIBUTION = "© Esri · OpenStreetMap"
VIEW_ZOOM = 15                      # logical zoom (256 logical px per tile): a neighbourhood
MAX_ZOOM = 19                       # deepest Esri World Street Map level
OVERVIEW_DROP = 3                   # overview tile = 8x8 detail tiles: one request covers the panel
HEIGHT = 180
RADIUS = 10.0
BACKGROUND = "#E4E8EC"              # Standard's land colour (map_vault.html baseBgColor)
BORDER = "#2C2C2E"
CACHE_TILES = 160                   # decoded tiles kept (~40 MB at 256 px RGBA worst case)
WORKERS = 3
MAX_LAT = 85.05112878


def tile_key(z, x, y):
    return f"{TILE_HOST}/{TILE_PATH.format(z=z, x=x, y=y)}"


def world_px(lat, lon, z):
    """Web Mercator pixel position at zoom z (256 px tiles)."""
    n = 256.0 * (2 ** z)
    lat = max(-MAX_LAT, min(MAX_LAT, float(lat)))
    x = (float(lon) + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def tile_zoom(dpr):
    return min(MAX_ZOOM, VIEW_ZOOM + (1 if dpr >= 1.5 else 0))


def layout_tiles(lat, lon, width, height, dpr):
    """Tiles covering a width x height logical panel centred on (lat, lon), nearest first.
    -> (zoom, tile size in logical px, [(x, y, left, top)]). x wraps around the antimeridian."""
    z = tile_zoom(dpr)
    size = 256.0 / (2 ** (z - VIEW_ZOOM))
    cx, cy = world_px(lat, lon, z)
    cx, cy = cx / 256.0 * size, cy / 256.0 * size            # in logical px at the drawn scale
    left, top = cx - width / 2.0, cy - height / 2.0
    n = 2 ** z
    out = []
    for ty in range(int(math.floor(top / size)), int(math.floor((top + height) / size)) + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(int(math.floor(left / size)), int(math.floor((left + width) / size)) + 1):
            out.append((tx % n, ty, tx * size - left, ty * size - top))
    out.sort(key=lambda t: (t[2] + size / 2 - width / 2) ** 2 + (t[3] + size / 2 - height / 2) ** 2)
    return z, size, out


def fetch_tile(key, service=None):
    """Encoded tile bytes for a store key, or None. Worker thread only (disk / network)."""
    srv = service if service is not None else tile_store.service()
    if srv is None:
        return None
    try:
        row = srv.store.get(key)
        if row is not None and srv.store.fresh(row, key):
            data, enc = row[0], row[2]
        else:
            status, data, _ctype, enc, _src = srv.resolve(key, lane="map")
            if status != 200 or not data:
                return None
        if enc == "gzip":
            data = zlib.decompress(data, 47)
        return bytes(data)
    except Exception as e:                    # noqa: BLE001 – a missing tile is not an error
        print(f"[MiniMap] tile {key}: {e}")
        return None


class _Workers:
    """A few daemon threads. Daemon on purpose: a 20 s network timeout must never hold the
    app's exit, which a QThreadPool (waitForDone in its destructor) would."""

    def __init__(self, n=WORKERS):
        self.q = queue.LifoQueue()            # newest request first: the photo on screen now
        self._lock = threading.Lock()
        self._n, self._threads = n, []

    def submit(self, fn):
        with self._lock:
            if len(self._threads) < self._n:
                t = threading.Thread(target=self._loop, name="mini-map-tiles", daemon=True)
                self._threads.append(t)
                t.start()
        self.q.put(fn)

    def _loop(self):
        while True:
            fn = self.q.get()
            try:
                fn()
            except Exception as e:            # noqa: BLE001
                print(f"[MiniMap] worker: {e}")


_workers = _Workers()


class _Signals(QObject):
    tile = pyqtSignal(str, object)            # key, QImage or None


class MiniMapWidget(QWidget):
    """Static map of one place with a centred position dot. show_place(lat, lon) per photo."""

    clicked = pyqtSignal(float, float)

    # () -> TileServer or None. The viewer points it at main's starter, so the tile store runs
    # even when the Map tab was never opened in this session.
    service_provider = staticmethod(tile_store.service)

    def __init__(self, parent=None, accent="#5E64E8"):
        super().__init__(parent)
        self.setFixedHeight(HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.place = None
        self.accent = QColor(accent)
        self._cache = OrderedDict()           # key -> QPixmap (LRU)
        self._pending = set()                 # keys a worker is fetching
        self._failed = set()                  # keys that came back empty (until the next place)
        self._signals = _Signals(self)
        self._signals.tile.connect(self._on_tile)
        self.requests = 0                     # tiles asked of the workers (tests, diagnostics)

    # ── public ──
    def show_place(self, lat, lon):
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return False
        self.place = (lat, lon)
        self._failed.clear()
        self._request()
        self.update()
        return True

    def set_accent(self, hex_color):
        c = QColor(hex_color)
        if c.isValid():
            self.accent = c
            self.update()

    def visible_keys(self):
        """Detail tile keys the panel needs right now (tests)."""
        if self.place is None:
            return []
        z, _size, tiles = layout_tiles(*self.place, max(1, self.width()), self.height(), self._dpr())
        return [tile_key(z, x, y) for x, y, _l, _t in tiles]

    def loaded(self):
        """All tiles of the current view decoded (tests)."""
        return bool(self.place) and all(k in self._cache or k in self._failed for k in self.visible_keys())

    # ── tiles ──
    def _dpr(self):
        try:
            return float(self.devicePixelRatioF()) or 1.0
        except RuntimeError:
            return 1.0

    def _request(self):
        if self.place is None or self.width() <= 0:
            return
        lat, lon = self.place
        z, _size, tiles = layout_tiles(lat, lon, self.width(), self.height(), self._dpr())
        oz = max(0, z - OVERVIEW_DROP)
        cx, cy = world_px(lat, lon, oz)
        wanted = [tile_key(oz, int(cx // 256) % (2 ** oz), int(cy // 256))]   # soft preview first
        wanted += [tile_key(z, x, y) for x, y, _l, _t in tiles]
        # LIFO queue: submit in reverse so the overview, then the centre, come off first
        for key in reversed(wanted):
            if key in self._cache or key in self._pending or key in self._failed:
                continue
            self._pending.add(key)
            self.requests += 1
            _workers.submit(self._job(key))

    def _job(self, key):
        signals, provider = self._signals, self.service_provider

        def run():
            data = fetch_tile(key, provider())
            img = None
            if data:
                img = QImage.fromData(data)   # decoded off the GUI thread (QImage is thread-safe)
                if img.isNull():
                    img = None
            try:
                signals.tile.emit(key, img)
            except RuntimeError:              # the widget is gone (viewer closed)
                pass
        return run

    def _on_tile(self, key, img):
        self._pending.discard(key)
        if img is None:
            self._failed.add(key)
            return
        self._cache[key] = QPixmap.fromImage(img)
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_TILES:
            self._cache.popitem(last=False)
        self.update()

    def _ancestor(self, z, x, y):
        """(pixmap, source rect) of the deepest cached tile above (z, x, y), or None."""
        for up in range(1, z + 1):
            pix = self._cache.get(tile_key(z - up, x >> up, y >> up))
            if pix is not None:
                span = 256.0 / (2 ** up)
                ox, oy = (x - ((x >> up) << up)) * span, (y - ((y >> up) << up)) * span
                sx, sy = pix.width() / 256.0, pix.height() / 256.0
                return pix, QRectF(ox * sx, oy * sy, span * sx, span * sy)
        return None

    # ── Qt ──
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._request()

    def showEvent(self, event):
        super().showEvent(event)
        self._request()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.place and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(*self.place)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(r, RADIUS, RADIUS)
        p.setClipPath(clip)
        p.fillRect(r, QColor(BACKGROUND))
        if self.place is not None:
            dpr = self._dpr()
            z, size, tiles = layout_tiles(*self.place, self.width(), self.height(), dpr)
            # Tiles edge to edge on whole device pixels, without antialiasing: fractional edges
            # were blended against the background and showed as hairline seams between tiles
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            snap = lambda v: round(v * dpr) / dpr    # noqa: E731
            for x, y, left, top in tiles:
                x0, y0 = snap(left), snap(top)
                target = QRectF(x0, y0, snap(left + size) - x0, snap(top + size) - y0)
                pix = self._cache.get(tile_key(z, x, y))
                if pix is not None:
                    p.drawPixmap(target, pix, QRectF(pix.rect()))
                else:
                    anc = self._ancestor(z, x, y)
                    if anc is not None:
                        p.drawPixmap(target, anc[0], anc[1])
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            self._paint_pin(p, QPointF(self.width() / 2.0, self.height() / 2.0))
            self._paint_attribution(p)
        p.setClipping(False)
        p.setPen(QPen(QColor(BORDER), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, RADIUS, RADIUS)
        p.end()

    def _paint_pin(self, p, c):
        """The main map's "here" dot: soft accent halo, drop shadow, white ring, accent core."""
        p.setPen(Qt.PenStyle.NoPen)
        halo = QColor(self.accent)
        halo.setAlphaF(0.20)
        p.setBrush(halo)
        p.drawEllipse(c, 17.0, 17.0)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawEllipse(c + QPointF(0, 1.0), 10.0, 10.0)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(c, 9.0, 9.0)
        p.setBrush(self.accent)
        p.drawEllipse(c, 6.0, 6.0)

    def _paint_attribution(self, p):
        f = QFont(self.font())
        f.setPixelSize(9)
        p.setFont(f)
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(ATTRIBUTION) + 10, fm.height() + 2
        box = QRectF(self.width() - w - 5, self.height() - h - 5, w, h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 170))
        p.drawRoundedRect(box, h / 2.0, h / 2.0)
        p.setPen(QColor("#48484A"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, ATTRIBUTION)
