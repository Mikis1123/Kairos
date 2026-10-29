"""Okno Kairos a Windows (dávka 3): titulkový pruh podle motivu a v barvě aplikace.

Proč ne Mica: ověřeno (Qt 6.11, Windows 11 26200) – Mica funguje jen v okně bez QtWebEngine.
Jakmile okno obsahuje QWebEngineView (mapa, minimapa prohlížeče), skládá se přes GPU a průsvitné
oblasti Windows vykreslí jako plochou šedou. Kairos proto používá neprůhledný postranní panel
a místo Mica sjednotí titulkový pruh s aplikací: tmavý / světlý podle motivu a na Windows 11
v barvě pozadí, takže plynule navazuje na lišty stránek.
"""
import sys

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_CAPTION_COLOR = 35          # Windows 11 (build 22000+)
DWMWA_TEXT_COLOR = 36
CAPTION_MIN_BUILD = 22000


def windows_build():
    if sys.platform != "win32":
        return 0
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def _set_attr(hwnd, attr, value):
    try:
        import ctypes
        val = ctypes.c_int(int(value))
        return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0
    except Exception:
        return False


def _colorref(hex_color):
    """'#RRGGBB' -> COLORREF (0x00BBGGRR)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def style_title_bar(window, dark, caption_hex=None, text_hex=None):
    """Titulkový pruh podle motivu; na Windows 11 i barva pruhu a textu. Funguje i za běhu.
    Vrací True, když systém přijal alespoň tmavý / světlý režim."""
    if sys.platform != "win32":
        return False
    hwnd = int(window.winId())
    ok = _set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    if windows_build() >= CAPTION_MIN_BUILD:
        if caption_hex:
            _set_attr(hwnd, DWMWA_CAPTION_COLOR, _colorref(caption_hex))
        if text_hex:
            _set_attr(hwnd, DWMWA_TEXT_COLOR, _colorref(text_hex))
    return ok


def style_for_theme(window):
    """Titulek pro aktuální motiv (barvy z tokenů)."""
    import theme
    import tokens
    t = tokens.palette()
    return style_title_bar(window, theme.current() == "dark", t["bg"], t["text"])
