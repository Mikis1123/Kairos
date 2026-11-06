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

Sprint 4 – slabá grafika: úroveň podle jader / RAM nestačí (8jádrový notebook s Intel UHD 620
by byl „high“). Profil proto čte i grafický adaptér (DXGI, classify_gpu). Na integrované grafice
nižší třídy (Intel HD / UHD / Iris před Xe, AMD Vega) nebo softwarovém vykreslování mapa:
  * kreslí budovy ploše (žádné 3D vytažení – nejdražší vrstva v ulicích),
  * vypne stínování reliéfu a průhledné rozostřené panely nad plátnem (stínové / blur průchody),
  * drží pevných 60 snímků/s (rovnoměrná kadence místo 120 Hz s výpadky) a nižší strop rozlišení.
Přepsání: config „gpu_profile“ nebo KAIROS_GPU_PROFILE (auto | full | lite).
"""
import ctypes
import os
import re
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
    map_hidpi_raster: bool = False   # Gate 2 C: one zoom deeper for raster tiles on HiDPI (sharp)
    # grafika (Sprint 4)
    gpu_name: str = ""
    gpu_class: str = "unknown"       # classify_gpu()
    gpu_lite: bool = False           # snížená kvalita mapy kvůli grafice (classify_gpu / gpu_profile)
    map_buildings_3d: bool = True    # False = budovy ploše (map_style.flatBuildings)
    map_lite_ui: bool = False        # True = bez rozostření / stínů panelů nad plátnem mapy
    map_fps_cap: int = 0             # 0 = obnovovací frekvence panelu; 60 = pevná kadence

    @property
    def frame_budget_ms(self):
        return 1000.0 / max(30.0, self.refresh_hz or 60.0)

    def for_map(self):
        """Část profilu pro window.setPerfProfile() v map_vault.html."""
        d = asdict(self)
        keep = ("tier", "refresh_hz", "map_pixel_ratio_max", "map_pixel_ratio_min", "map_lod_levels",
                "map_lod_ratio", "map_terrain", "map_terrain_exaggeration", "map_hillshade",
                "map_hidpi_raster", "map_buildings_3d", "map_lite_ui")
        out = {k: d[k] for k in keep}
        out["gpu"] = self.gpu_class
        return out


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


# ── GPU (Sprint 4: low-spec hardware) ─────────────────────────────────────────────
# The map's cost is GPU fill rate (terrain render-to-texture, 3D building extrusions, hillshade)
# far more than CPU. Cores / RAM alone rated an 8-core laptop with Intel UHD 620 "high". The
# adapter is read through DXGI (a few ms, no WMI / PowerShell): adapter 0 is the one Windows
# gives the desktop and, by default, Chromium / WebView2, so it is the one that draws the map.
GPU_DISCRETE, GPU_INTEGRATED, GPU_INTEGRATED_LOW, GPU_SOFTWARE, GPU_UNKNOWN = (
    "discrete", "integrated", "integrated-low", "software", "unknown")
VENDOR_INTEL, VENDOR_AMD, VENDOR_NVIDIA, VENDOR_MICROSOFT, VENDOR_QUALCOMM = 0x8086, 0x1002, 0x10DE, 0x1414, 0x5143
DISCRETE_VRAM_MB = 1536            # dedicated memory above this = a card (iGPU carve-outs are 128-512 MB)
_DXGI_ADAPTER_FLAG_SOFTWARE = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint),
                ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", _LUID), ("Flags", ctypes.c_uint)]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]


_IID_IDXGIFactory1 = _GUID(0x770AAE78, 0xF26F, 0x4DBA, (ctypes.c_ubyte * 8)(0xA8, 0x29, 0x25, 0x3C, 0x83, 0xD1, 0xB3, 0x87))


def _vcall(obj, index, restype, argtypes, *args):
    """COM virtual call: obj is an interface pointer (c_void_p), argtypes after `this`."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def read_gpus():
    """[{"name", "vendor", "device", "vram_mb", "shared_mb", "software"}] in DXGI order (adapter 0
    first). [] off Windows or on failure."""
    if sys.platform != "win32":
        return []
    out = []
    try:
        dxgi = ctypes.windll.dxgi
        factory = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory)) != 0:
            return []
        try:
            for i in range(8):
                adapter = ctypes.c_void_p()
                # IDXGIFactory1::EnumAdapters1 = vtable 12 (IUnknown 3 + IDXGIObject 4 + IDXGIFactory 5)
                if _vcall(factory, 12, ctypes.c_long, (ctypes.c_uint, ctypes.c_void_p), i, ctypes.byref(adapter)) != 0:
                    break
                try:
                    desc = _DXGI_ADAPTER_DESC1()
                    # IDXGIAdapter1::GetDesc1 = vtable 10 (IUnknown 3 + IDXGIObject 4 + IDXGIAdapter 3)
                    if _vcall(adapter, 10, ctypes.c_long, (ctypes.c_void_p,), ctypes.byref(desc)) == 0:
                        out.append({"name": desc.Description.strip(), "vendor": int(desc.VendorId),
                                    "device": int(desc.DeviceId),
                                    "vram_mb": int(desc.DedicatedVideoMemory // 2 ** 20),
                                    "shared_mb": int(desc.SharedSystemMemory // 2 ** 20),
                                    "software": bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE)})
                finally:
                    _vcall(adapter, 2, ctypes.c_ulong, ())               # Release
        finally:
            _vcall(factory, 2, ctypes.c_ulong, ())
    except Exception:
        return out
    return out


def primary_gpu(gpus=None):
    """The adapter that draws the map: the first hardware adapter (else the first one)."""
    gpus = read_gpus() if gpus is None else gpus
    hw = [g for g in gpus if not g.get("software") and g.get("vendor") != VENDOR_MICROSOFT]
    return (hw or gpus or [None])[0]


def classify_gpu(gpu):
    """discrete | integrated | integrated-low | software | unknown.

    integrated-low = the iGPUs that drop frames on the 3D map at 1080p+: Intel HD / UHD / Iris
    (Plus) before Xe, and AMD's unnamed "Radeon(TM) Graphics" / Vega APUs. Intel Iris Xe / Arc
    and named Radeon 6x0M-8x0M iGPUs (RDNA 2+) hold 60 fps and keep the full map."""
    if not gpu:
        return GPU_UNKNOWN
    name = (gpu.get("name") or "").lower()
    vendor = gpu.get("vendor")
    if gpu.get("software") or vendor == VENDOR_MICROSOFT or "basic render" in name:
        return GPU_SOFTWARE
    if vendor == VENDOR_NVIDIA:
        return GPU_DISCRETE
    vram = int(gpu.get("vram_mb") or 0)
    if vendor == VENDOR_INTEL:
        if "arc" in name and vram >= DISCRETE_VRAM_MB:
            return GPU_DISCRETE                           # Arc A-series cards
        if "xe" in name or "arc" in name:
            return GPU_INTEGRATED                         # Iris Xe, Arc (Meteor / Lunar Lake)
        return GPU_INTEGRATED_LOW                         # HD / UHD / Iris (Plus) Graphics
    if vendor == VENDOR_AMD:
        if vram >= DISCRETE_VRAM_MB and not re.search(r"\b\d{3}m\b", name):
            return GPU_DISCRETE                           # Radeon RX / Pro cards
        if re.search(r"\b[6-9]\d0m\b", name):
            return GPU_INTEGRATED                         # Radeon 680M / 780M / 880M / 890M
        return GPU_INTEGRATED_LOW                         # "AMD Radeon(TM) Graphics", Vega 8 ...
    if vendor == VENDOR_QUALCOMM:
        return GPU_INTEGRATED                             # Snapdragon X (Adreno)
    return GPU_DISCRETE if vram >= DISCRETE_VRAM_MB else GPU_INTEGRATED


def low_power_gpu(gpu_class):
    return gpu_class in (GPU_INTEGRATED_LOW, GPU_SOFTWARE)


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
                       map_hillshade=True, thumb_cache_mb=512, map_hidpi_raster=True)
    if tier == "standard":
        return Profile(tier, cores, ram_gb, refresh_hz, bg_threads=2, bg_item_pause_s=0.01, dense_strips=True,
                       proxy_playback=False, map_pixel_ratio_max=2.0, map_pixel_ratio_min=1.0,
                       map_lod_levels=5, map_lod_ratio=3.0, map_terrain=True, map_terrain_exaggeration=1.25,
                       map_hillshade=True, thumb_cache_mb=320)
    return Profile("baseline", cores, ram_gb, refresh_hz, bg_threads=1, bg_item_pause_s=0.05, dense_strips=False,
                   proxy_playback=True, map_pixel_ratio_max=1.25, map_pixel_ratio_min=0.75,
                   map_lod_levels=4, map_lod_ratio=2.0, map_terrain=True, map_terrain_exaggeration=1.1,
                   map_hillshade=False, thumb_cache_mb=160)


GPU_PROFILES = ("auto", "full", "lite")
LITE_PIXEL_RATIO_MAX = 1.5          # 3K / 4K panel on an iGPU: map canvas at most 1.5x (sharp, 44 % fewer px than 2x)
LITE_FPS = 60


def wants_lite(gpu_class, mode="auto"):
    """Should the map run in its low-spec GPU form? mode: auto | full | lite."""
    if mode == "lite":
        return True
    if mode == "full":
        return False
    return low_power_gpu(gpu_class)


def apply_gpu(profile, gpu=None, mode="auto"):
    """Profile with the GPU's limits on the map (only lowers quality, never raises it)."""
    from dataclasses import replace
    gpu_class = classify_gpu(gpu)
    name = (gpu or {}).get("name", "") if gpu else ""
    if not wants_lite(gpu_class, mode):
        return replace(profile, gpu_name=name, gpu_class=gpu_class)
    ratio = profile.map_pixel_ratio_max
    ratio = LITE_PIXEL_RATIO_MAX if ratio <= 0 else min(ratio, LITE_PIXEL_RATIO_MAX)
    return replace(profile, gpu_name=name, gpu_class=gpu_class, gpu_lite=True,
                   map_buildings_3d=False, map_lite_ui=True, map_hillshade=False, map_hidpi_raster=False,
                   map_pixel_ratio_max=ratio, map_pixel_ratio_min=min(profile.map_pixel_ratio_min, 1.0),
                   map_lod_levels=min(profile.map_lod_levels, 4), map_lod_ratio=min(profile.map_lod_ratio, 2.0),
                   map_fps_cap=LITE_FPS if (profile.refresh_hz or 60) > LITE_FPS + 1 else 0)


_current = None


def detect(override=None, gpu_mode=None, gpus=None):
    """Zjistí profil (override: 'auto' | úroveň; jinak KAIROS_TIER; gpu_mode: auto | full | lite,
    jinak KAIROS_GPU_PROFILE) a uloží ho jako aktuální."""
    global _current
    cores = os.cpu_count() or 4
    ram = total_ram_gb()
    hz = screen_refresh_hz()
    want = (os.environ.get("KAIROS_TIER") or override or "auto").strip().lower()
    tier = want if want in TIERS else classify(cores, ram)
    mode = (os.environ.get("KAIROS_GPU_PROFILE") or gpu_mode or "auto").strip().lower()
    mode = mode if mode in GPU_PROFILES else "auto"
    _current = apply_gpu(build(tier, cores, ram, hz), primary_gpu(gpus), mode)
    return _current


def current():
    return _current or detect()
