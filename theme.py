"""Sémantické barvy aplikace + světlý režim (Patch 11 / dávka E).

Tokeny (bg, surface, text, accent, ...) mají tmavou a světlou hodnotu. Vlastní kreslení
(mřížka, karty osob) bere barvy přes qcolor("token"). Starší styly widgetů jsou psané
tmavými literály – při světlém režimu je apply()/install() přeloží podle vlastnosti CSS
(pozadí, okraj, písmo) na světlé protějšky a nově vzniklé widgety hlídá filtr událostí.
Prohlížeč a prezentace zůstávají tmavé vždy (FIXED_DARK_PROPERTY na kořeni podstromu).
"""
import re
import sys

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget

THEMES = ("dark", "light", "system")
DEFAULT_THEME = "dark"
FIXED_DARK_PROPERTY = "omni_fixed_dark"
TOKEN_QSS_PROPERTY = "omni_qss_kind"    # styl generovaný z tokenů (tokens.build_qss) – nepřekládá se
_ORIG_PROPERTY = "omni_qss_dark"        # původní (tmavý) styl widgetu
_APPLIED_PROPERTY = "omni_qss_applied"  # naposledy nastavený přeložený styl

TOKENS = {
    # Kairos „Graphite Night“ (dávka 4): hlubší grafit s nádechem do studena
    "dark": {
        "bg": "#0E0E10", "bg_sunken": "#09090B", "surface": "#1A1A1E", "surface_hover": "#232328",
        "surface_pressed": "#141417", "placeholder": "#141417", "separator": "#232328",
        "text": "#F2F2F4", "text_secondary": "#A1A1AA", "text_tertiary": "#6B6B74", "text_muted": "#B4B4BC",
        "text_on_accent": "#FFFFFF", "accent": "#0A84FF", "danger": "#FF453A", "warning": "#FF9F0A",
        "success": "#30D158", "hairline": "rgba(255,255,255,0.06)",
    },
    "light": {
        "bg": "#F5F5F7", "bg_sunken": "#ECECEF", "surface": "#FFFFFF", "surface_hover": "#F0F0F2",
        "surface_pressed": "#E5E5EA", "placeholder": "#E5E5EA", "separator": "#D1D1D6",
        "text": "#1D1D1F", "text_secondary": "#6E6E73", "text_tertiary": "#8E8E93", "text_muted": "#6E6E73",
        "text_on_accent": "#FFFFFF", "accent": "#007AFF", "danger": "#FF3B30", "warning": "#FF9500",
        "success": "#34C759", "hairline": "rgba(0,0,0,0.08)",
    },
}

# Tmavé literály ve stávajících stylech -> světlé, zvlášť podle druhu vlastnosti
_BG_MAP = {
    "#1C1C1E": "#F5F5F7", "#141416": "#ECECEF", "#121214": "#FFFFFF", "#161618": "#F2F2F4",
    "#18181B": "#FFFFFF", "#1F1F24": "#FFFFFF", "#232325": "#F2F2F4", "#242426": "#E5E5EA",
    "#242428": "#FFFFFF", "#262629": "#E5E5EA", "#27272A": "#E5E5EA", "#2C2C2E": "#FFFFFF",
    "#2F2F36": "#E5E5EA", "#323238": "#F0F0F2", "#333336": "#E5E5EA", "#3A3A3C": "#E5E5EA",
    "#48484A": "#D1D1D6", "#636366": "#C7C7CC", "#0A84FF": "#007AFF", "#409CFF": "#0A84FF",
    "#0071E3": "#0062CC", "#FF453A": "#FF3B30", "#30D158": "#34C759", "#FF9F0A": "#FF9500",
    "#3A1C1C": "#FDE7E6", "#321414": "#FCD9D6", "#3A2E1C": "#FFF2DC", "#2E2211": "#FFE8C2",
}
_BORDER_MAP = dict(_BG_MAP, **{
    "#2C2C2E": "#D1D1D6", "#3A3A3C": "#D1D1D6", "#262629": "#D1D1D6", "#2F2F36": "#D1D1D6",
    "#333336": "#D1D1D6", "#48484A": "#C7C7CC", "#3A1C1C": "#F5B5B0", "#3A2E1C": "#F5D28F",
    "#1C1C1E": "#D1D1D6", "#18181B": "#D1D1D6",
})
_FG_MAP = {
    "#F5F5F7": "#1D1D1F", "#FFFFFF": "#1D1D1F", "#E1E1E6": "#1D1D1F", "#D1D1D6": "#3A3A3C",
    "#AEAEB2": "#6E6E73", "#8E8E93": "#6E6E73", "#636366": "#8E8E93", "#0A84FF": "#007AFF",
    "#409CFF": "#007AFF", "#FF453A": "#FF3B30", "#FF9F0A": "#C93400", "#30D158": "#248A3D",
    "#2C2C2E": "#D1D1D6", "#3A3A3C": "#D1D1D6",   # „color“ u QFrame = barva dělicí čáry
}
# barevná (sytá) pozadí – bílé písmo na nich zůstává bílé
_SATURATED = {"#0A84FF", "#409CFF", "#0071E3", "#FF453A", "#30D158", "#FF9F0A", "#007AFF"}

_current = DEFAULT_THEME
_filter = None
# Háčky z tokens.py (Kairos, dávka 1); bez nich se theme chová přesně jako dřív.
_token_builder = None       # (kind, mode) -> QSS pro widgety s TOKEN_QSS_PROPERTY
_postprocess = None         # (qss, mode) -> QSS po remap_qss (přebarvení zvýraznění)
_palette_accent = None      # mode -> {"accent": ..., "fill": ..., "on_accent": ...}

_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")
_DECL = re.compile(r"([A-Za-z\-]+)(\s*:\s*)([^;{}]*)")
_WHITE_RGBA = re.compile(r"rgba\(\s*255\s*,\s*255\s*,\s*255\s*,")


def system_prefers_light():
    """Windows: Nastavení › Přizpůsobení › Barvy (AppsUseLightTheme)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return bool(winreg.QueryValueEx(key, "AppsUseLightTheme")[0])
    except OSError:
        return False


def resolve(name):
    """'dark' | 'light' | 'system' -> skutečný režim ('dark' / 'light')."""
    if name == "system":
        return "light" if system_prefers_light() else "dark"
    return name if name in TOKENS else DEFAULT_THEME


def current():
    return _current


def color(token, mode=None):
    return TOKENS[mode or _current][token]


def qcolor(token, alpha=None, mode=None):
    c = QColor(color(token, mode))
    if alpha is not None:
        c.setAlpha(int(alpha))
    return c


def set_token_stylesheet_builder(fn):
    global _token_builder
    _token_builder = fn


def set_qss_postprocessor(fn):
    global _postprocess
    _postprocess = fn


def set_palette_accent(fn):
    global _palette_accent
    _palette_accent = fn


def build_palette(mode):
    t = TOKENS[resolve(mode)]
    if _palette_accent is not None:
        acc = _palette_accent(resolve(mode))
        t = dict(t, accent=acc["fill"], accent_text=acc["accent"], text_on_accent=acc["on_accent"])
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["bg"], QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["surface"], QPalette.ColorRole.AlternateBase: t["surface_hover"],
        QPalette.ColorRole.ToolTipBase: t["surface"], QPalette.ColorRole.ToolTipText: t["text"],
        QPalette.ColorRole.Text: t["text"], QPalette.ColorRole.Button: t["surface_hover"],
        QPalette.ColorRole.ButtonText: t["text"], QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Link: t.get("accent_text", t["accent"]), QPalette.ColorRole.Highlight: t["accent"],
        QPalette.ColorRole.HighlightedText: t["text_on_accent"],
        QPalette.ColorRole.PlaceholderText: t["text_tertiary"],
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    return pal


def _map_value(prop, value, keep_white):
    prop = prop.lower()
    if prop in ("color", "selection-color"):
        table = _FG_MAP
    elif prop.startswith("border") or prop == "outline":
        table = _BORDER_MAP
    elif prop.startswith("background") or prop in ("selection-background-color", "alternate-background-color",
                                                   "gridline-color"):
        table = _BG_MAP
        value = _WHITE_RGBA.sub("rgba(0, 0, 0,", value)        # světlé překryvy -> tmavé
    else:
        return value

    def sub(m):
        hx = m.group(0).upper()
        if table is _FG_MAP and keep_white and hx == "#FFFFFF":
            return m.group(0)
        return table.get(hx, m.group(0))
    return _HEX.sub(sub, value)


def remap_qss(qss, mode="light"):
    """Přeloží tmavé literály stylu do daného režimu (pro 'dark' vrací styl beze změny)."""
    if resolve(mode) == "dark" or not qss:
        return qss
    out = []
    # po blocích { ... } – bílé písmo na sytém pozadí téhož bloku zůstává (tlačítka)
    for block in re.split(r"(?<=\})", qss):
        backgrounds = " ".join(v for p, _, v in _DECL.findall(block) if p.lower().startswith("background"))
        keep_white = any(h.upper() in _SATURATED for h in _HEX.findall(backgrounds))
        out.append(_DECL.sub(lambda m: m.group(1) + m.group(2) + _map_value(m.group(1), m.group(3), keep_white),
                             block))
    return "".join(out)


def is_fixed_dark(widget):
    w = widget
    while w is not None:
        if w.property(FIXED_DARK_PROPERTY):
            return True
        w = w.parentWidget()
    return False


def restyle_widget(widget, mode=None):
    """Nastaví widgetu styl pro režim; původní tmavý styl si pamatuje ve vlastnosti."""
    mode = resolve(mode or _current)
    fixed = is_fixed_dark(widget)
    if fixed and widget.property(FIXED_DARK_PROPERTY):
        widget.setPalette(build_palette("dark"))
    kind = widget.property(TOKEN_QSS_PROPERTY)
    if kind and _token_builder is not None:
        target = _token_builder(kind, "dark" if fixed else mode)
        widget.setProperty(_APPLIED_PROPERTY, target)
        if widget.styleSheet() != target:
            widget.setStyleSheet(target)
        return
    if fixed and _postprocess is None:
        return
    qss = widget.styleSheet()
    applied = widget.property(_APPLIED_PROPERTY)
    orig = widget.property(_ORIG_PROPERTY)
    if orig is None or (applied is not None and qss != applied):
        orig = qss                      # widget si mezitím nastavil nový (tmavý) styl
        widget.setProperty(_ORIG_PROPERTY, orig)
    # prohlížeč a prezentace zůstávají tmavé – jen se přebarví zvýraznění
    target = orig if fixed else remap_qss(orig, mode)
    if _postprocess is not None:
        target = _postprocess(target, "dark" if fixed else mode)
    widget.setProperty(_APPLIED_PROPERTY, target)
    if target != qss:
        widget.setStyleSheet(target)


class _ThemeFilter(QObject):
    """Nové a přestylované widgety (toasty, karty, podstránky) dostanou styl aktuálního režimu."""

    def eventFilter(self, obj, event):
        if event.type() not in (QEvent.Type.Polish, QEvent.Type.StyleChange) or not isinstance(obj, QWidget):
            return False
        if _current != "dark":
            if obj.styleSheet() != obj.property(_APPLIED_PROPERTY) or event.type() == QEvent.Type.Polish:
                restyle_widget(obj)
        elif _postprocess is not None and obj.styleSheet() \
                and obj.styleSheet() != obj.property(_APPLIED_PROPERTY):
            restyle_widget(obj)         # tmavý režim: jen přebarvení zvýraznění nových stylů
        return False


def uninstall():
    """Při ukončení aplikace: filtr odebrat dřív, než Python zruší jeho obal. Jinak Qt při
    posledních událostech volá eventFilter mrtvého Python objektu (pád v sip, 0xC0000005)."""
    global _filter
    app = QApplication.instance()
    if _filter is not None and app is not None:
        app.removeEventFilter(_filter)
    _filter = None


def apply(mode, root=None):
    """Přepne režim celé aplikace (paleta + styly existujících widgetů)."""
    global _current, _filter
    _current = resolve(mode)
    app = QApplication.instance()
    if app is None:
        return _current
    app.setPalette(build_palette(_current))
    if _filter is None:
        _filter = _ThemeFilter(app)
        app.installEventFilter(_filter)
    roots = [root] if root is not None else app.topLevelWidgets()
    for top in roots:
        for w in [top] + top.findChildren(QWidget):
            if w.property(_ORIG_PROPERTY) is not None or w.styleSheet() or w.property(FIXED_DARK_PROPERTY):
                restyle_widget(w, _current)
        top.update()
    return _current
