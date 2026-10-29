"""Výkonnostní profil stroje (adaptivní škálování).

Kairos má běžet plynule od základního PC (4 jádra, 8 GB RAM, integrovaná grafika, 60 Hz)
po silné stroje (120 Hz, mnoho jader). Profil se zjistí jednou při startu a určuje strop
kvality i zátěž na pozadí:

  high      – plná kvalita: nativní obnovovací frekvence, 4K HDR přímo, hustý terén, nejostřejší dlaždice
  standard  – plná kvalita, mírnější rezerva dlaždic a vláken na pozadí
  baseline  – lehká paměť, přehrávání přes 1080p proxy u 4K/HDR, přiškrcené pozadí, nižší LOD mapy

Mapa se navíc přizpůsobuje za běhu podle skutečné doby snímku (map_vault.html, dynamické
rozlišení) – profil jí dává jen meze. Přepsání: config „performance_tier“ nebo proměnná
prostředí KAIROS_TIER (auto | high | standard | baseline).
"""
import ctypes
import os
import sys
from dataclasses import asdict, dataclass

TIERS = ("baseline", "standard", "high")


@dataclass(frozen=True)
class Profile:
    tier: str
    cores: int
    ram_gb: float
    refresh_hz: float
    # pozadí
    bg_threads: int              # vlákna ffmpeg / dekódování na pozadí
    bg_item_pause_s: float       # pauza mezi položkami démona (nižší profil = víc vzduchu pro UI)
    dense_strips: bool           # hustý průchod náhledového pásu (každý snímek krátkých videí)
    # video
    proxy_playback: bool         # 4K / HDR přehrávat z 1080p proxy (je-li hotová)
    # mapa (meze dynamického rozlišení a LOD)
    map_pixel_ratio_max: float   # 0 = nativní poměr zařízení
    map_pixel_ratio_min: float   # spodní mez při pohybu kamery
    map_lod_levels: int          # setSourceTileLodParams(maxZoomLevelsOnScreen, …)
    map_lod_ratio: float
    map_terrain: bool
    map_terrain_exaggeration: float
    map_hillshade: bool
    # paměť
    thumb_cache_mb: int          # RAM náhledů mřížky

    @property
    def frame_budget_ms(self):
        return 1000.0 / max(30.0, self.refresh_hz or 60.0)

    def for_map(self):
        """Část profilu pro window.setPerfProfile() v map_vault.html."""
        d = asdict(self)
        keep = ("tier", "refresh_hz", "map_pixel_ratio_max", "map_pixel_ratio_min", "map_lod_levels",
                "map_lod_ratio", "map_terrain", "map_terrain_exaggeration", "map_hillshade")
        return {k: d[k] for k in keep}


def total_ram_gb():
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys / 1024 ** 3
        except Exception:
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (AttributeError, ValueError, OSError):
        return 8.0


def screen_refresh_hz():
    try:
        from PyQt6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        screen = app.primaryScreen() if app is not None else None
        if screen is not None and screen.refreshRate() > 1:
            return float(screen.refreshRate())
    except Exception:
        pass
    return 60.0


def classify(cores, ram_gb):
    """Úroveň podle jader a paměti (8GB stroj hlásí ~7,8 GB)."""
    if cores <= 4 or ram_gb < 11.5:
        return "baseline"
    if cores >= 8 and ram_gb >= 15.0:
        return "high"
    return "standard"


def build(tier, cores, ram_gb, refresh_hz):
    if tier == "high":
        return Profile(tier, cores, ram_gb, refresh_hz, bg_threads=4, bg_item_pause_s=0.0, dense_strips=True,
                       proxy_playback=False, map_pixel_ratio_max=0.0, map_pixel_ratio_min=1.25,
                       map_lod_levels=6, map_lod_ratio=3.0, map_terrain=True, map_terrain_exaggeration=1.35,
                       map_hillshade=True, thumb_cache_mb=512)
    if tier == "standard":
        return Profile(tier, cores, ram_gb, refresh_hz, bg_threads=2, bg_item_pause_s=0.01, dense_strips=True,
                       proxy_playback=False, map_pixel_ratio_max=2.0, map_pixel_ratio_min=1.0,
                       map_lod_levels=5, map_lod_ratio=3.0, map_terrain=True, map_terrain_exaggeration=1.25,
                       map_hillshade=True, thumb_cache_mb=320)
    return Profile("baseline", cores, ram_gb, refresh_hz, bg_threads=1, bg_item_pause_s=0.05, dense_strips=False,
                   proxy_playback=True, map_pixel_ratio_max=1.25, map_pixel_ratio_min=0.75,
                   map_lod_levels=4, map_lod_ratio=2.0, map_terrain=True, map_terrain_exaggeration=1.1,
                   map_hillshade=False, thumb_cache_mb=160)


_current = None


def detect(override=None):
    """Zjistí profil (override: 'auto' | úroveň; jinak KAIROS_TIER) a uloží ho jako aktuální."""
    global _current
    cores = os.cpu_count() or 4
    ram = total_ram_gb()
    hz = screen_refresh_hz()
    want = (os.environ.get("KAIROS_TIER") or override or "auto").strip().lower()
    tier = want if want in TIERS else classify(cores, ram)
    _current = build(tier, cores, ram, hz)
    return _current


def current():
    return _current or detect()
