"""Designové tokeny Kairos (dávka 1): barvy, zvýraznění, písmo, rozměry, pohyb a styly z nich.

Neutrální barvy zůstávají v theme.TOKENS (na jejich hodnoty spoléhá kreslení i testy); tady
přibývají jemnější stupně ploch, zvýraznění (Iris / Ember / Moss / Klasická modrá / vlastní)
a generátor stylů. Hlavní okno a předvolby už nemají ručně psaný tmavý styl: styl se skládá
z tokenů pro daný režim a theme.py ho při přepnutí motivu jen znovu vygeneruje.

Starší styly widgetů s literálem #0A84FF (a jeho hover / pressed odstíny) přebarví
recolor_accent() na zvolené zvýraznění – až po theme.remap_qss, takže převod tmavý -> světlý
zůstává beze změny. Klasická modrá = přesně původní vzhled (nic se nepřebarvuje).
"""
import math
import os
import re
import tempfile

import paths
import theme

# ── neutrální stupně navíc (k theme.TOKENS) ───────────────────────────────────
# Alfa v rgba() je 0–255: parser stylů Qt ji tak čte vždy stejně.
NEUTRAL_EXTRA = {
    "dark": {
        "surface_2": "#232328", "surface_3": "#2E2E35",
        "hairline": "rgba(255, 255, 255, 20)", "hairline_strong": "rgba(255, 255, 255, 36)",
        "hover": "rgba(255, 255, 255, 14)", "pressed": "rgba(255, 255, 255, 8)",
        "scroll": "rgba(255, 255, 255, 56)", "scroll_hover": "rgba(255, 255, 255, 100)",
        "knob": "#FFFFFF", "knob_border": "rgba(0, 0, 0, 70)",
        "danger_text": "#FF453A", "danger_border": "rgba(255, 69, 58, 90)", "danger_soft": "rgba(255, 69, 58, 34)",
        "warning_text": "#FF9F0A", "warning_border": "rgba(255, 159, 10, 90)", "warning_soft": "rgba(255, 159, 10, 30)",
        # kreslené ovládací prvky (dávka 2)
        "pill": "rgba(255, 255, 255, 24)", "pill_border": "rgba(255, 255, 255, 16)",
        "seg_track": "#1A1A1E", "seg_thumb": "#3A3A42", "switch_off": "#3A3A42", "shadow": "rgba(0, 0, 0, 110)",
        # okno (dávka 3): postranní panel o stupeň tmavší než obsah, tlumení pod listem
        "sidebar": "#121215", "scrim": "rgba(0, 0, 0, 130)",
    },
    "light": {
        "surface_2": "#F0F0F2", "surface_3": "#E5E5EA",
        "hairline": "rgba(0, 0, 0, 20)", "hairline_strong": "rgba(0, 0, 0, 36)",
        "hover": "rgba(0, 0, 0, 11)", "pressed": "rgba(0, 0, 0, 20)",
        "scroll": "rgba(0, 0, 0, 60)", "scroll_hover": "rgba(0, 0, 0, 105)",
        "knob": "#FFFFFF", "knob_border": "rgba(0, 0, 0, 55)",
        "danger_text": "#D70015", "danger_border": "rgba(215, 0, 21, 80)", "danger_soft": "rgba(255, 59, 48, 26)",
        "warning_text": "#C93400", "warning_border": "rgba(201, 52, 0, 80)", "warning_soft": "rgba(255, 149, 0, 30)",
        "pill": "#FFFFFF", "pill_border": "rgba(0, 0, 0, 18)",
        "seg_track": "#E5E5EA", "seg_thumb": "#FFFFFF", "switch_off": "#D1D1D6", "shadow": "rgba(0, 0, 0, 40)",
        "sidebar": "#ECECEF", "scrim": "rgba(0, 0, 0, 60)",
    },
}

# Pozadí, na kterých se kontroluje kontrast zvýraznění (bg a surface obou režimů)
CONTRAST_BACKGROUNDS = {"dark": ("#1C1C1E", "#2C2C2E"), "light": ("#F5F5F7", "#FFFFFF")}
ON_ACCENT = "#FFFFFF"           # text na vyplněném tlačítku – vždy bílý, výplň je k tomu laděná

# ── zvýraznění ────────────────────────────────────────────────────────────────
# accent = text, ikony, rámečky; fill = výplň tlačítek (bílý text >= 4.5 : 1); accent_2 = druhá
# barva značkového přechodu (splash, jiskra, mapa).
ACCENTS = {
    "iris": {"name": "Iris", "dark": ("#8B93FF", "#5E64E8", "#4FD1C5"), "light": ("#5457D6", "#5457D6", "#0E8C83")},
    "ember": {"name": "Ember", "dark": ("#FF8A5B", "#C94E1E", "#FFC56B"), "light": ("#C2491B", "#C2491B", "#A05F00")},
    "moss": {"name": "Moss", "dark": ("#A3D977", "#3F7D22", "#E3CF6E"), "light": ("#3F7D22", "#3F7D22", "#866A00")},
    # Původní systémová modrá (Apple). Výplň má jen ~3.6 : 1 s bílou – ponecháno kvůli shodě s originálem.
    "classic": {"name": "Klasická modrá", "dark": ("#0A84FF", "#0A84FF", "#64D2FF"), "light": ("#007AFF", "#007AFF", "#5AC8FA")},
}
DEFAULT_ACCENT = "iris"
LEGACY_ACCENT = "classic"


# ── barevná matematika (sRGB, WCAG, OKLCH) ────────────────────────────────────
def hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(r, g, b):
    return "#" + "".join(f"{max(0, min(255, round(v))):02X}" for v in (r, g, b))


def _to_lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _from_lin(c):
    c = max(0.0, min(1.0, c))
    return 255 * (12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055)


def luminance(hx):
    r, g, b = (_to_lin(c) for c in hex_to_rgb(hx))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def hex_to_oklch(hx):
    r, g, b = (_to_lin(c) for c in hex_to_rgb(hx))
    l = (0.4122214708 * r + 0.5363026296 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(A, B), math.degrees(math.atan2(B, A)) % 360


def _oklch_to_lin(L, C, H):
    a, b = C * math.cos(math.radians(H)), C * math.sin(math.radians(H))
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def oklch_to_hex(L, C, H):
    """Barva mimo gamut sRGB se dorovná snížením sytosti (odstín a světlost zůstanou)."""
    def ok(v):
        return all(-1e-4 <= x <= 1 + 1e-4 for x in v)
    rgb = _oklch_to_lin(L, C, H)
    if not ok(rgb):
        lo, hi = 0.0, C
        for _ in range(22):
            mid = (lo + hi) / 2
            if ok(_oklch_to_lin(L, mid, H)):
                lo = mid
            else:
                hi = mid
        rgb = _oklch_to_lin(L, lo, H)
    return rgb_to_hex(*(_from_lin(c) for c in rgb))


def shift_lightness(hx, delta):
    L, C, H = hex_to_oklch(hx)
    return oklch_to_hex(max(0.0, min(1.0, L + delta)), C, H)


def rgba(hx, alpha):
    r, g, b = hex_to_rgb(hx)
    return f"rgba({r}, {g}, {b}, {int(round(alpha))})"


_RGBA = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")


def qcolor(value):
    """Token ('#RRGGBB' nebo 'rgba(r, g, b, 0–255)') -> QColor pro kreslení."""
    from PyQt6.QtGui import QColor
    m = _RGBA.fullmatch(value.strip())
    if m:
        r, g, b, a = (int(x) for x in m.groups())
        return QColor(r, g, b, a)
    return QColor(value)


def mix(a, b, t):
    """Lineární prolnutí dvou QColor (včetně alfy), t 0–1."""
    from PyQt6.QtGui import QColor
    t = max(0.0, min(1.0, float(t)))
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


def _min_contrast(hx, mode):
    return min(contrast(hx, bg) for bg in CONTRAST_BACKGROUNDS[mode])


def derive_accent(hx):
    """Libovolná barva -> (tmavá, světlá) trojice (accent, fill, accent_2), obě s kontrastem AA.

    Odstín a sytost zůstávají, světlost se dorovná zvlášť pro každý režim: text zvýraznění
    >= 4.5 : 1 proti pozadí i ploše daného režimu, bílý text na výplni >= 4.5 : 1."""
    _, c0, hue = hex_to_oklch(hx)
    chroma = min(c0, 0.19)                  # šedá zůstane šedou, příliš sytá se zkrotí
    hue2 = (hue + 40) % 360

    ld = 0.74
    acc_d = oklch_to_hex(ld, chroma, hue)
    while _min_contrast(acc_d, "dark") < 4.6 and ld < 0.95:
        ld += 0.01
        acc_d = oklch_to_hex(ld, chroma, hue)
    lf = 0.58
    fill_d = oklch_to_hex(lf, chroma, hue)
    while contrast(fill_d, ON_ACCENT) < 4.6 and lf > 0.25:
        lf -= 0.01
        fill_d = oklch_to_hex(lf, chroma, hue)
    ll = 0.54
    acc_l = oklch_to_hex(ll, chroma, hue)
    while (_min_contrast(acc_l, "light") < 4.6 or contrast(acc_l, ON_ACCENT) < 4.6) and ll > 0.25:
        ll -= 0.01
        acc_l = oklch_to_hex(ll, chroma, hue)
    return ((acc_d, fill_d, oklch_to_hex(min(0.86, ld + 0.05), chroma * 0.9, hue2)),
            (acc_l, acc_l, oklch_to_hex(max(0.4, ll - 0.02), chroma * 0.9, hue2)))


# ── stav zvýraznění ───────────────────────────────────────────────────────────
_HEX6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
_accent_key = DEFAULT_ACCENT
_accent_sets = {"dark": ACCENTS[DEFAULT_ACCENT]["dark"], "light": ACCENTS[DEFAULT_ACCENT]["light"]}
_qss_cache = {}


def accent_key():
    return _accent_key


def set_accent(key, custom_hex=None):
    """'iris' | 'ember' | 'moss' | 'classic' | 'custom' (+ custom_hex). Neznámé -> výchozí.
    Po změně je potřeba theme.apply(), aby se přestylovaly existující widgety."""
    global _accent_key
    if key == "custom" and custom_hex and _HEX6.match(custom_hex):
        dark, light = derive_accent(custom_hex.upper())
    else:
        key = key if key in ACCENTS else DEFAULT_ACCENT
        dark, light = ACCENTS[key]["dark"], ACCENTS[key]["light"]
    _accent_key = key
    _accent_sets.update(dark=dark, light=light)
    _qss_cache.clear()
    theme.set_qss_postprocessor(retint)       # Graphite Night + zvýraznění pro starší styly widgetů
    return key


def configure(cfg):
    """Zvýraznění z konfigurace (klíče 'accent', 'accent_custom' a 'adaptive_accent')."""
    set_adaptive(cfg.get("adaptive_accent", True))
    return set_accent(cfg.get("accent", DEFAULT_ACCENT), cfg.get("accent_custom") or None)


# ── kontextový odstín (Polish P4) ─────────────────────────────────────────────
# Jen pro kreslené ambientní plochy (záře karet, hlavička události, plátno mapy). Ovládací
# prvky QSS zůstávají VŽDY na zvoleném zvýraznění – theme.apply() se kvůli odstínu nevolá.
CONTEXT_BLEND = 0.35        # kolik odstínu obsahu se vmíchá do zvýraznění uživatele
_adaptive = True
_glow_cache = {}


def set_adaptive(on):
    global _adaptive
    _adaptive = bool(on)


def adaptive():
    return _adaptive


def _mix_hue(h1, h2, t):
    d = ((h2 - h1 + 540) % 360) - 180      # kratší oblouk
    return (h1 + d * t) % 360


def context_glow(tint=None, mode=None):
    """Zvýraznění posunuté k odstínu obsahu (tint '#RRGGBB'), s kontrastem AA přes derive_accent.
    Bez odstínu nebo s vypnutým Přizpůsobit obsahu = zvýraznění uživatele."""
    mode = theme.resolve(mode or theme.current())
    base = accent(mode)["accent"]
    if not _adaptive or not tint or not _HEX6.match(tint):
        return base
    key = (tint.upper(), mode, _accent_key, tuple(_accent_sets[mode]))
    hit = _glow_cache.get(key)
    if hit is None:
        L, C, H = hex_to_oklch(_accent_sets["dark"][0])
        _, C2, H2 = hex_to_oklch(tint)
        blended = oklch_to_hex(L, C + (C2 - C) * CONTEXT_BLEND * 0.5, _mix_hue(H, H2, CONTEXT_BLEND))
        dark, light = derive_accent(blended)
        hit = (dark if mode == "dark" else light)[0]
        if len(_glow_cache) > 256:
            _glow_cache.clear()
        _glow_cache[key] = hit
    return hit


def accent(mode=None):
    """Tokeny zvýraznění pro režim: accent, accent_hover, fill, fill_hover, fill_pressed,
    accent_2, accent_soft, on_accent."""
    mode = theme.resolve(mode or theme.current())
    acc, fill, acc2 = _accent_sets[mode]
    return {
        "accent": acc, "accent_hover": shift_lightness(acc, 0.05 if mode == "dark" else -0.05),
        "fill": fill, "fill_hover": shift_lightness(fill, 0.05), "fill_pressed": shift_lightness(fill, -0.05),
        "accent_2": acc2, "accent_soft": rgba(acc, 46 if mode == "dark" else 32),
        "focus_ring": rgba(acc, 150), "on_accent": ON_ACCENT,
    }


def palette(mode=None):
    """Všechny barevné tokeny režimu: theme.TOKENS + jemné stupně + zvýraznění.
    Výsledek je sdílený (kreslení ho volá při každém překreslení) – neměnit."""
    mode = theme.resolve(mode or theme.current())
    key = ("palette", mode, _accent_key, tuple(_accent_sets[mode]))
    if key not in _qss_cache:
        t = dict(theme.TOKENS[mode])
        t.update(NEUTRAL_EXTRA[mode])
        t.update(accent(mode))
        _qss_cache[key] = t
    return _qss_cache[key]


# ── přebarvení starších stylů (po theme.remap_qss) ───────────────────────────
# Po remap_qss mají literály v obou režimech tyto podoby (viz theme._BG_MAP / _FG_MAP).
_LEGACY_ACCENT = {
    "dark": {"bg": {"#0A84FF": "fill", "#409CFF": "fill_hover", "#0071E3": "fill_pressed"},
             "fg": {"#0A84FF": "accent", "#409CFF": "accent_hover"},
             "border": {"#0A84FF": "accent", "#409CFF": "accent_hover", "#0071E3": "accent"}},
    "light": {"bg": {"#007AFF": "fill", "#0A84FF": "fill_hover", "#0062CC": "fill_pressed"},
              "fg": {"#007AFF": "accent"},
              "border": {"#007AFF": "accent", "#0A84FF": "accent_hover", "#0062CC": "accent"}},
}
_DECL = re.compile(r"([A-Za-z\-]+)(\s*:\s*)([^;{}]*)")
_HEX = re.compile(r"#[0-9A-Fa-f]{6}\b")


# Starší tmavé literály (Apple šedé) -> Graphite Night; jen v tmavém režimu (světlý přeloží remap_qss).
_LEGACY_DARK = {
    "bg": {"#1C1C1E": "#0E0E10", "#141416": "#09090B", "#121214": "#0E0E10", "#161618": "#121215",
           "#18181B": "#141417", "#1F1F24": "#1A1A1E", "#232325": "#16161A", "#242426": "#141417",
           "#242428": "#1A1A1E", "#262629": "#1A1A1E", "#27272A": "#1A1A1E", "#2C2C2E": "#1A1A1E",
           "#2F2F36": "#232328", "#323238": "#232328", "#333336": "#232328", "#3A3A3C": "#232328",
           "#48484A": "#2E2E35", "#636366": "#4A4A52"},
    "fg": {"#F5F5F7": "#F2F2F4", "#E1E1E6": "#E4E4E8", "#8E8E93": "#A1A1AA", "#AEAEB2": "#B4B4BC",
           "#636366": "#6B6B74", "#2C2C2E": "#232328", "#3A3A3C": "#2A2A30"},
    "border": {"#1C1C1E": "#0E0E10", "#18181B": "#141417", "#262629": "#1F1F24", "#2C2C2E": "#232328",
               "#2F2F36": "#2A2A30", "#333336": "#2A2A30", "#3A3A3C": "#2A2A30", "#48484A": "#3A3A42"},
}


def _prop_kind(prop):
    prop = prop.lower()
    if prop in ("color", "selection-color"):
        return "fg"
    if prop.startswith("border") or prop == "outline":
        return "border"
    if prop.startswith("background") or prop in ("selection-background-color", "alternate-background-color"):
        return "bg"
    return None


def retint(qss, mode):
    """Starší styl widgetu (po theme.remap_qss) do Kairos: systémová modrá -> zvolené zvýraznění,
    v tmavém režimu Apple šedé -> Graphite Night. Rozlišuje druh vlastnosti (pozadí / text / okraj)."""
    if not qss:
        return qss
    mode = theme.resolve(mode)
    accent_tables = None if _accent_key == LEGACY_ACCENT else _LEGACY_ACCENT[mode]
    tok = accent(mode)

    def decl(m):
        kind = _prop_kind(m.group(1))
        if kind is None:
            return m.group(0)

        def swap(h):
            hx = h.group(0).upper()
            if accent_tables and hx in accent_tables[kind]:
                return tok[accent_tables[kind][hx]]
            if mode == "dark" and hx in _LEGACY_DARK[kind]:
                return _LEGACY_DARK[kind][hx]
            return h.group(0)
        return m.group(1) + m.group(2) + _HEX.sub(swap, m.group(3))
    return _DECL.sub(decl, qss)


recolor_accent = retint            # dřívější jméno (dávka 1)


# ── písmo ─────────────────────────────────────────────────────────────────────
# role -> (px, váha CSS, prostrkání v %)
TYPE_SCALE = {
    "large_title": (28, 600, -2.2), "title": (20, 600, -1.5), "headline": (15, 600, 0.0),
    "body": (13, 400, 0.0), "callout": (12, 400, 0.0), "caption": (11, 600, 7.0),
}
DISPLAY_FROM_PX = 20            # od této velikosti optický řez Display
_families = {}


def _pick_family(candidates):
    try:
        from PyQt6.QtGui import QFontDatabase, QGuiApplication
        if QGuiApplication.instance() is None:
            return None
        available = set(QFontDatabase.families())
    except Exception:
        return None
    return next((f for f in candidates if f in available), None)


def ui_family():
    """Segoe UI Variable Text (Windows 11), jinak Segoe UI (Windows 10)."""
    if "ui" not in _families:
        fam = _pick_family(("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"))
        if fam is None:
            return "Segoe UI"            # bez aplikace se nic neukládá – zkusí se znovu později
        _families["ui"] = fam
    return _families["ui"]


def display_family():
    if "display" not in _families:
        fam = _pick_family(("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI"))
        if fam is None:
            return "Segoe UI"
        _families["display"] = fam
    return _families["display"]


def font(role="body"):
    """QFont pro roli typografické stupnice (pixelová velikost, váha, prostrkání)."""
    from PyQt6.QtGui import QFont
    px, weight, tracking = TYPE_SCALE[role]
    f = QFont(display_family() if px >= DISPLAY_FROM_PX else ui_family())
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(weight))
    if tracking:
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100 + tracking)
    return f


def qss_font(role="body"):
    px, weight, _ = TYPE_SCALE[role]
    fam = display_family() if px >= DISPLAY_FROM_PX else ui_family()
    return f'font-family: "{fam}"; font-size: {px}px; font-weight: {weight};'


# ── rozměry a pohyb (dávka 2 z nich staví animace) ───────────────────────────
RADIUS = {"xs": 6, "sm": 8, "md": 10, "lg": 14, "xl": 20}
SPACE = {"xxs": 2, "xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}
CONTROL_HEIGHT = {"sm": 24, "md": 28, "lg": 32}
# název -> (ms, křivka QEasingCurve.Type)
MOTION = {"micro": (120, "OutCubic"), "standard": (220, "InOutCubic"), "emphasized": (360, "OutCubic"),
          "spring": (420, "OutBack"), "scene": (600, "InOutCubic")}


# ── obrázky indikátorů (fajfka, tečka, šipka) ─────────────────────────────────
def _ui_dirs():
    return (os.path.join(paths.CACHE_ROOT, "ui"), os.path.join(tempfile.gettempdir(), "kairos-ui"))


def _png(name, size, draw):
    """Vykreslí ikonku do PNG (1× a @2x pro vysoké DPI); vrací cestu pro url() nebo None."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QImage, QPainter
    for folder in _ui_dirs():
        try:
            os.makedirs(folder, exist_ok=True)
            base = os.path.join(folder, name)
            ok = True
            for scale, suffix in ((1, ""), (2, "@2x")):
                target = f"{base}{suffix}.png"
                if os.path.exists(target):
                    continue
                img = QImage(size * scale, size * scale, QImage.Format.Format_ARGB32_Premultiplied)
                img.fill(Qt.GlobalColor.transparent)
                p = QPainter(img)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.scale(scale, scale)
                draw(p)
                p.end()
                ok = ok and img.save(target)
            if ok:
                return (base + ".png").replace("\\", "/")
        except OSError:
            continue
    return None


def _pen(color, width):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QPen
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def check_image(color=ON_ACCENT):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    def draw(p):
        path = QPainterPath(QPointF(4.2, 8.4))
        path.lineTo(6.9, 11.0)
        path.lineTo(11.9, 5.2)
        p.setPen(_pen(color, 1.9))
        p.drawPath(path)
    return _png(f"check-{color.lstrip('#').lower()}", 16, draw)


def dot_image(color=ON_ACCENT):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QColor

    def draw(p):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(8, 8), 3.2, 3.2)
    return _png(f"dot-{color.lstrip('#').lower()}", 16, draw)


def chevron_image(color):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QPainterPath

    def draw(p):
        path = QPainterPath(QPointF(3.0, 5.0))
        path.lineTo(6.0, 8.0)
        path.lineTo(9.0, 5.0)
        p.setPen(_pen(color, 1.5))
        p.drawPath(path)
    return _png(f"chevron-{color.lstrip('#').lower()}", 12, draw)


def _url(path):
    return f'url("{path}")' if path else "none"


# ── generované styly ──────────────────────────────────────────────────────────
def _controls_qss(t):
    """Ovládací prvky společné hlavnímu oknu i předvolbám."""
    check, dot, chevron = check_image(), dot_image(), chevron_image(t["text_secondary"])
    return f"""
QPushButton {{
    background-color: {t['surface_2']}; color: {t['text']};
    border: 1px solid {t['hairline']}; border-radius: {RADIUS['sm']}px;
    padding: 6px 14px; font-size: 13px; font-weight: 500;
}}
QPushButton:hover {{ background-color: {t['surface_3']}; }}
QPushButton:pressed {{ background-color: {t['surface_pressed']}; }}
QPushButton:disabled {{ color: {t['text_tertiary']}; background-color: {t['surface']}; border-color: {t['hairline']}; }}
QPushButton:default {{ border-color: {t['focus_ring']}; }}
QPushButton#btn_primary {{ background-color: {t['fill']}; color: {t['on_accent']}; border: none; font-weight: 600; }}
QPushButton#btn_primary:hover {{ background-color: {t['fill_hover']}; }}
QPushButton#btn_primary:pressed {{ background-color: {t['fill_pressed']}; }}
QPushButton#btn_danger {{ background-color: {t['danger']}; color: #FFFFFF; border: none; font-weight: 600; }}

QLineEdit {{
    background-color: {t['surface']}; color: {t['text']};
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['sm']}px; padding: 5px 10px;
    selection-background-color: {t['fill']}; selection-color: {t['on_accent']};
}}
QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
QLineEdit:disabled {{ color: {t['text_tertiary']}; }}

QComboBox {{
    background-color: {t['surface']}; color: {t['text']};
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['sm']}px; padding: 4px 28px 4px 10px;
}}
QComboBox:hover {{ border-color: {t['scroll_hover']}; }}
QComboBox:focus {{ border-color: {t['accent']}; }}
QComboBox::drop-down {{ border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: {_url(chevron)}; width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background-color: {t['surface']}; color: {t['text']}; outline: 0; padding: 4px;
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['md']}px;
    selection-background-color: {t['fill']}; selection-color: {t['on_accent']};
}}

QCheckBox, QRadioButton {{ color: {t['text']}; spacing: 8px; background: transparent; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px; border: 1px solid {t['hairline_strong']}; background-color: {t['surface']};
}}
QCheckBox::indicator {{ border-radius: 5px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background-color: {t['fill']}; border-color: {t['fill']}; image: {_url(check)}; }}
QCheckBox::indicator:checked:hover {{ background-color: {t['fill_hover']}; border-color: {t['fill_hover']}; }}
QRadioButton::indicator:checked {{ background-color: {t['fill']}; border-color: {t['fill']}; image: {_url(dot)}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background-color: {t['surface_2']}; border-color: {t['hairline']}; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {t['text_tertiary']}; }}

QListWidget {{
    background-color: {t['surface']}; color: {t['text']}; outline: 0;
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['md']}px; padding: 4px;
}}
QListWidget::item {{ padding: 3px 8px; border-radius: {RADIUS['xs']}px; }}
QListWidget::item:hover {{ background-color: {t['hover']}; }}
QListWidget::item:selected {{ background-color: {t['fill']}; color: {t['on_accent']}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['scroll']}; border-radius: 3px; min-height: 32px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['scroll']}; border-radius: 3px; min-width: 32px; }}
QScrollBar::handle:hover {{ background: {t['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSlider {{ background: transparent; }}
QSlider::groove:horizontal {{ background: {t['surface_3']}; height: 4px; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {t['fill']}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {t['knob']}; border: 1px solid {t['knob_border']};
    width: 16px; height: 16px; margin: -7px 0; border-radius: 9px;
}}
QSlider::handle:horizontal:hover {{ border-color: {t['accent']}; }}

QProgressBar {{ background-color: {t['surface_2']}; border: none; border-radius: 3px; max-height: 6px; text-align: center; }}
QProgressBar::chunk {{ background-color: {t['fill']}; border-radius: 3px; }}

QMenu {{
    background-color: {t['surface']}; color: {t['text']};
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['md']}px; padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: {RADIUS['xs']}px; }}
QMenu::item:selected {{ background-color: {t['fill']}; color: {t['on_accent']}; }}
QMenu::item:disabled {{ color: {t['text_tertiary']}; }}
QMenu::separator {{ height: 1px; background: {t['separator']}; margin: 4px 8px; }}

QToolTip {{
    background-color: {t['surface']}; color: {t['text']};
    border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['xs']}px; padding: 4px 8px;
}}
"""


def _main_qss(t):
    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {t['bg']}; color: {t['text']};
    font-family: "{ui_family()}"; font-size: 13px;
}}
QLabel {{ color: {t['text']}; background: transparent; }}
QListView {{ background-color: {t['bg']}; border: none; }}
QFrame[frameShape='4'], QFrame[frameShape='5'] {{ color: {t['separator']}; }}
QMessageBox {{ background-color: {t['surface']}; }}
QTabWidget::pane {{ border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['md']}px; }}
QTabBar::tab {{
    background: transparent; color: {t['text_secondary']};
    padding: 6px 14px; border-radius: {RADIUS['xs']}px; margin-right: 2px; font-weight: 500;
}}
QTabBar::tab:selected {{ background: {t['surface_2']}; color: {t['text']}; }}
QTabBar::tab:hover:!selected {{ color: {t['text']}; }}
""" + _shell_qss(t) + _controls_qss(t)


def _shell_qss(t):
    """Kostra okna (dávka 3): postranní panel, nástrojové lišty, titulky, prázdné stavy, listy."""
    return f"""
QWidget#centralRoot {{ background-color: transparent; }}
QWidget#sidebar {{ background-color: {t['sidebar']}; border-right: 1px solid {t['hairline']}; }}
QLabel#brand {{ {qss_font('headline')} color: {t['text']}; padding: 2px 10px 10px 10px; }}
QLabel#statusLabel {{ color: {t['text_secondary']}; font-size: 11px; padding: 2px 10px; }}
QWidget#toolbar {{ background-color: {t['bg']}; border-bottom: 1px solid {t['separator']}; }}
QLabel#pageTitle {{ {qss_font('title')} color: {t['text']}; }}
QLabel#pageSubtitle {{ color: {t['text_secondary']}; font-size: 11px; }}
QLabel#emptyTitle {{ font-family: "{display_family()}"; font-size: 17px; font-weight: 600; color: {t['text']}; }}
QLabel#emptyBody {{ color: {t['text_secondary']}; font-size: 13px; }}
QFrame#sheetCard {{
    background-color: {t['surface']}; border: 1px solid {t['hairline_strong']}; border-radius: {RADIUS['lg']}px;
}}
QLabel#sheetTitle {{ {qss_font('headline')} color: {t['text']}; }}
QLabel#sheetBody {{ color: {t['text_secondary']}; font-size: 13px; }}
"""


def _settings_qss(t):
    return f"""
QDialog {{ background-color: {t['bg_sunken']}; color: {t['text']}; font-family: "{ui_family()}"; font-size: 13px; }}
QWidget {{ font-family: "{ui_family()}"; }}
QLabel {{ color: {t['text']}; font-size: 13px; background: transparent; }}
QLabel#hint {{ color: {t['text_secondary']}; font-size: 12px; }}
QLabel#impact {{ color: {t['warning_text']}; font-size: 12px; }}
QLabel#groupTitle {{ color: {t['text_tertiary']}; font-size: 11px; font-weight: 600; padding: 12px 4px 2px 4px; }}
QFrame#group {{ background-color: {t['surface']}; border: 1px solid {t['hairline']}; border-radius: {RADIUS['md']}px; }}
QFrame#rowSep {{ background-color: {t['hairline']}; border: none; margin-left: 14px; }}
QWidget#row {{ background: transparent; }}
QLabel#rowValue {{ color: {t['text_secondary']}; font-size: 13px; }}
QScrollArea {{ border: none; background: transparent; }}
QWidget#page {{ background-color: {t['bg']}; }}

QTabWidget::pane {{
    border: 1px solid {t['hairline_strong']}; background-color: {t['bg']};
    border-radius: {RADIUS['md']}px;
}}
QTabBar::tab {{
    background: transparent; color: {t['text_secondary']}; padding: 7px 16px; margin-right: 4px;
    border-top-left-radius: {RADIUS['sm']}px; border-top-right-radius: {RADIUS['sm']}px;
    font-weight: 600; font-size: 13px;
}}
QTabBar::tab:selected {{ background: {t['bg']}; color: {t['text']}; border-bottom: 2px solid {t['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {t['text']}; }}
""" + _controls_qss(t) + f"""
QPushButton {{ font-size: 12px; font-weight: 600; padding: 7px 14px; }}
QPushButton#btnSave {{ background-color: {t['fill']}; color: {t['on_accent']}; border: none; padding: 8px 20px; }}
QPushButton#btnSave:hover {{ background-color: {t['fill_hover']}; }}
QPushButton#btnSave:pressed {{ background-color: {t['fill_pressed']}; }}
QPushButton#btnDanger {{ background-color: {t['danger']}; color: #FFFFFF; border: none; padding: 8px 18px; }}
QPushButton#btnReset {{ color: {t['danger_text']}; border: 1px solid {t['danger_border']}; background-color: transparent; }}
QPushButton#btnReset:hover {{ background-color: {t['danger_soft']}; border-color: {t['danger_text']}; }}
QPushButton#btnReset:disabled {{ color: {t['text_tertiary']}; border-color: {t['hairline']}; }}
QPushButton#btnCleanCache {{ color: {t['warning_text']}; border: 1px solid {t['warning_border']}; background-color: transparent; }}
QPushButton#btnCleanCache:hover {{ background-color: {t['warning_soft']}; border-color: {t['warning_text']}; }}
"""


_BUILDERS = {"main": _main_qss, "settings": _settings_qss}


def build_qss(kind, mode=None):
    mode = theme.resolve(mode or theme.current())
    key = (kind, mode, _accent_key, tuple(_accent_sets[mode]), ui_family())
    if key not in _qss_cache:
        _qss_cache[key] = _BUILDERS[kind](palette(mode))
    return _qss_cache[key]


def apply_stylesheet(widget, kind):
    """Widget dostane generovaný styl; theme.apply() ho při změně motivu sestaví znovu."""
    widget.setProperty(theme.TOKEN_QSS_PROPERTY, kind)
    theme.restyle_widget(widget)


theme.set_token_stylesheet_builder(build_qss)
theme.set_palette_accent(accent)
set_accent(DEFAULT_ACCENT)
