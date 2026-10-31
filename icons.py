"""Vektorové ikony Kairos (dávka 3) místo emoji v rozhraní.

Každá ikona je SVG na mřížce 24×24 (tahy 1.7, zaoblené konce). Vykreslí se jednou jako bílá
maska v potřebné velikosti a hustotě pixelů a při kreslení se obarví (SourceIn) – barva tak
může plynule animovat (najetí, výběr) bez nového vykreslování SVG.
"""
import atexit
from collections import OrderedDict

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap

try:
    from PyQt6.QtSvg import QSvgRenderer
except ImportError:          # pragma: no cover – bez QtSvg zůstanou ikony prázdné, nic nespadne
    QSvgRenderer = None

_STROKE = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" '
           'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{}</svg>')
_FILL = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="#FFFFFF" stroke="none">{}</svg>'

_HEART = "M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6C19.5 15.4 12 20 12 20z"

ICONS = {
    "photos": '<rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/>'
              '<rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/>',
    "people": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.6 3.4-5.5 6.5-5.5s5.7 1.9 6.5 5.5"/>'
              '<circle cx="17" cy="9" r="2.6"/><path d="M16.5 14.6c2.6.2 4.4 1.9 5 4.9"/>',
    "events": '<rect x="3.5" y="5" width="17" height="15.5" rx="3"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    "map": '<path d="M12 21s-6.5-6-6.5-11.2a6.5 6.5 0 0 1 13 0C18.5 15 12 21 12 21z"/><circle cx="12" cy="9.8" r="2.4"/>',
    "heart": f'<path d="{_HEART}"/>',
    "video": '<rect x="3" y="5.5" width="13" height="13" rx="3"/><path d="M16 10.5l5-3v9l-5-3"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.5h5V7M6 7l1 12.5a1.5 1.5 0 0 0 1.5 1.5h7a1.5 1.5 0 0 0 1.5-1.5L18 7"/>',
    "import": '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M4.5 19.5h15"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "settings": '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "info": '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.1"/>',
    "alert": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5.5M12 16.3v.1"/>',
    "back": '<path d="M14.5 5.5L8 12l6.5 6.5"/>',
    "live": '<circle cx="12" cy="12" r="2.5"/><circle cx="12" cy="12" r="5.5" stroke-dasharray="2 2.2"/><circle cx="12" cy="12" r="8.5"/>',
    "check": '<path d="M5.5 12.5l4 4 9-9"/>',
    "slideshow": '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M10.2 9.4v5.2l4.4-2.6z"/>',
    "refresh": '<path d="M19.5 11A7.5 7.5 0 0 0 6.2 7.2M4.5 4.5v3.7h3.7M4.5 13a7.5 7.5 0 0 0 13.3 3.8M19.5 19.5v-3.7h-3.7"/>',
    "face_question": '<circle cx="10" cy="8" r="3.5"/><path d="M3.5 20c.8-3.6 3.4-5.5 6.5-5.5 1.2 0 2.3.3 3.2.8"/>'
                     '<path d="M16.6 14.6a2 2 0 1 1 2.7 1.9c-.4.2-.6.5-.6.9v.4M18.7 20.3v.1"/>',
    "duplicates": '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.5"/>'
                  '<path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
    "merge": '<circle cx="6" cy="6" r="2.2"/><circle cx="6" cy="18" r="2.2"/><circle cx="18" cy="12" r="2.2"/>'
             '<path d="M8.2 6.5c4 .5 5.5 2.5 7.6 5M8.2 17.5c4-.5 5.5-2.5 7.6-5"/>',
    "folder": '<path d="M3.5 7a2 2 0 0 1 2-2h4l2 2.5h7a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>',
    "globe": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.6 2.4 3.8 5.2 3.8 8.5s-1.2 6.1-3.8 8.5'
             'c-2.6-2.4-3.8-5.2-3.8-8.5s1.2-6.1 3.8-8.5z"/>',
    "route": '<circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h6.5a3.5 3.5 0 0 0 0-7h-5a3.5 3.5 0 0 1 0-7H16"/>',
    "sort": '<path d="M7 5v14M4 16l3 3 3-3M14 7h6M14 12h4.5M14 17h3"/>',
    # Ozvěny: dva rámečky přes sebe (tehdy / teď) s ozvěnou oblouku
    "echo": '<rect x="3.5" y="7.5" width="10" height="10" rx="2"/><path d="M16.5 7.5a6 6 0 0 1 0 10M19 5a9.5 9.5 0 0 1 0 15"/>',
    # Alba: dva listy přes sebe (Sprint 2)
    "album": '<rect x="3.5" y="7.5" width="17" height="13" rx="2.5"/><path d="M6.5 4.5h11"/>',
    "chevron_down": '<path d="M7 10l5 5 5-5"/>',
    "sparkle":'<path d="M12 3.5q1 7.5 8.5 8.5q-7.5 1-8.5 8.5q-1-7.5-8.5-8.5q7.5-1 8.5-8.5z"/>',
    # Sprint 4: Sestřihy (filmový pás s přehráním)
    # Stage 3: složka Skryté
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2.5"/><path d="M8.5 10.5V7.5a3.5 3.5 0 0 1 7 0v3M12 14.5v2"/>',
    "reel": '<rect x="3.5" y="5.5" width="17" height="13" rx="2.5"/><path d="M3.5 9h17M3.5 15h17M10.5 10.6v2.8l2.6-1.4z"/>',
}
FILLED = {
    "heart_fill": f'<path d="{_HEART}"/>',
    "play_fill": '<path d="M8 5.5v13l10.5-6.5z"/>',
}

_masks = {}
_tinted = OrderedDict()
_TINT_CACHE = 256


def clear_cache():
    """QPixmap nesmí přežít QApplication (pád při ukončení) – uvolnit dřív, než Python ruší moduly."""
    _masks.clear()
    _tinted.clear()


atexit.register(clear_cache)


def names():
    return sorted(set(ICONS) | set(FILLED))


def _svg(name):
    if name in FILLED:
        return _FILL.format(FILLED[name])
    return _STROKE.format(ICONS[name])


def _dpr(dpr=None):
    if dpr:
        return float(dpr)
    screen = QGuiApplication.primaryScreen() if QGuiApplication.instance() else None
    return float(screen.devicePixelRatio()) if screen else 1.0


def mask(name, size=18, dpr=None):
    """Bílá ikona (alfa = tvar) v logické velikosti size px."""
    dpr = _dpr(dpr)
    key = (name, size, dpr)
    if key not in _masks:
        px = max(1, round(size * dpr))
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        if QSvgRenderer is not None and (name in ICONS or name in FILLED):
            renderer = QSvgRenderer(QByteArray(_svg(name).encode("utf-8")))
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(p, QRectF(0, 0, px, px))
            p.end()
        pm.setDevicePixelRatio(dpr)
        _masks[key] = pm
    return _masks[key]


def pixmap(name, color, size=18, dpr=None):
    """Ikona obarvená na color (QColor nebo '#RRGGBB')."""
    color = QColor(color)
    dpr = _dpr(dpr)
    key = (name, size, dpr, color.rgba())
    pm = _tinted.get(key)
    if pm is None:
        src = mask(name, size, dpr)
        pm = QPixmap(src.size())
        pm.setDevicePixelRatio(dpr)          # před kreslením: maska i cíl ve stejných souřadnicích
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.drawPixmap(0, 0, src)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(QRectF(0, 0, src.width() / dpr, src.height() / dpr), color)
        p.end()
        _tinted[key] = pm
        while len(_tinted) > _TINT_CACHE:
            _tinted.popitem(last=False)
    else:
        _tinted.move_to_end(key)
    return pm


def paint(painter, name, rect, color):
    """Nakreslí ikonu do obdélníku (logické souřadnice) v dané barvě."""
    rect = QRectF(rect)
    size = round(min(rect.width(), rect.height()))
    painter.drawPixmap(rect.topLeft(), pixmap(name, color, size, painter.device().devicePixelRatioF()))


def qicon(name, color, size=18):
    """QIcon pro widgety, které ikonu kreslí samy (QLineEdit akce, QLabel …)."""
    icon = QIcon()
    for dpr in (1.0, 2.0):
        icon.addPixmap(pixmap(name, color, size, dpr))
    return icon
