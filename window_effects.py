"""Okno Kairos a Windows (dávka 3): titulkový pruh podle motivu a v barvě aplikace.

Proč ne Mica: ověřeno (Qt 6.11, Windows 11 26200) – Mica funguje jen v okně bez QtWebEngine.
Jakmile okno obsahuje QWebEngineView (mapa, minimapa prohlížeče), skládá se přes GPU a průsvitné
oblasti Windows vykreslí jako plochou šedou. Kairos proto používá neprůhledný postranní panel
a místo Mica sjednotí titulkový pruh s aplikací: tmavý / světlý podle motivu a na Windows 11
v barvě pozadí, takže plynule navazuje na lišty stránek.

Windows 10 (Sprint 4): každý atribut DWM se posílá jen od buildu, který ho zná (DWM_ATTR_MIN_BUILD).
  * tmavý titulek: atribut 20 od 18985 (Windows 10 20H1+), dřív nedokumentovaný 19 (1809-1909),
    pod 17763 se nenastavuje (světlý systémový pruh);
  * barva pruhu / textu (35, 36) jen Windows 11 – na Windows 10 zůstane systémový tmavý / světlý pruh;
  * DWMWA_SYSTEMBACKDROP_TYPE (Mica / Acrylic, 38) a zaoblení rohů (33) Kairos nepoužívá; brána je
    pro ně přesto zapsaná, aby je pozdější kód nemohl poslat na starší systém.
Windows 10 překreslí nečinný titulek s novým režimem až po změně rámu: po přepnutí se proto
pošle SetWindowPos(SWP_FRAMECHANGED).
"""
import sys

DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19     # Windows 10 1809-1909 (nedokumentovaný)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_CAPTION_COLOR = 35          # Windows 11 (build 22000+)
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38    # Windows 11 22H2 (22621+)
CAPTION_MIN_BUILD = 22000
DARK_MODE_MIN_BUILD = 18985
DARK_MODE_OLD_MIN_BUILD = 17763
WIN11_MIN_BUILD = 22000
DWM_ATTR_MIN_BUILD = {
    DWMWA_USE_IMMERSIVE_DARK_MODE_OLD: DARK_MODE_OLD_MIN_BUILD,
    DWMWA_USE_IMMERSIVE_DARK_MODE: DARK_MODE_MIN_BUILD,
    DWMWA_WINDOW_CORNER_PREFERENCE: WIN11_MIN_BUILD,
    DWMWA_CAPTION_COLOR: CAPTION_MIN_BUILD,
    DWMWA_TEXT_COLOR: CAPTION_MIN_BUILD,
    DWMWA_SYSTEMBACKDROP_TYPE: 22621,
}
_SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020   # NOMOVE | NOSIZE | NOZORDER | NOACTIVATE | FRAMECHANGED


def windows_build():
    if sys.platform != "win32":
        return 0
    try:
        return sys.getwindowsversion().build
    except Exception:
        return 0


def is_windows11(build=None):
    return (windows_build() if build is None else build) >= WIN11_MIN_BUILD


def attr_supported(attr, build=None):
    """True when this Windows build knows the DWM attribute (unknown attributes: True)."""
    build = windows_build() if build is None else build
    return build >= DWM_ATTR_MIN_BUILD.get(attr, 0)


def dark_mode_attr(build=None):
    """The immersive dark mode attribute for this build, or None (too old)."""
    build = windows_build() if build is None else build
    if build >= DARK_MODE_MIN_BUILD:
        return DWMWA_USE_IMMERSIVE_DARK_MODE
    if build >= DARK_MODE_OLD_MIN_BUILD:
        return DWMWA_USE_IMMERSIVE_DARK_MODE_OLD
    return None


def _dwm_set(hwnd, attr, ctypes_value):
    import ctypes
    return ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(ctypes_value),
                                                      ctypes.sizeof(ctypes_value)) == 0


def _set_attr(hwnd, attr, value):
    """DwmSetWindowAttribute(int value) – refused (False) on a build that does not know attr."""
    if not attr_supported(attr):
        return False
    try:
        import ctypes
        return _dwm_set(hwnd, attr, ctypes.c_int(int(value)))
    except Exception:
        return False


def _refresh_frame(hwnd):
    try:
        import ctypes
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, _SWP_FLAGS)
    except Exception:
        pass


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
    build = windows_build()
    hwnd = int(window.winId())
    attr = dark_mode_attr(build)
    ok = _set_attr(hwnd, attr, 1 if dark else 0) if attr is not None else False
    if is_windows11(build):
        if caption_hex:
            _set_attr(hwnd, DWMWA_CAPTION_COLOR, _colorref(caption_hex))
        if text_hex:
            _set_attr(hwnd, DWMWA_TEXT_COLOR, _colorref(text_hex))
    elif ok:
        _refresh_frame(hwnd)              # Windows 10 repaints an inactive caption only on a frame change
    return ok


def style_for_theme(window):
    """Titulek pro aktuální motiv (barvy z tokenů)."""
    import theme
    import tokens
    t = tokens.palette()
    return style_title_bar(window, theme.current() == "dark", t["bg"], t["text"])
