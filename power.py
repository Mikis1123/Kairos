"""Gate 0: napájení – těžká AI na pozadí nevybije notebook ani ho nepřehřeje.

Stav ze systému (GetSystemPowerStatus): napájení ze sítě / z baterie, procento, Spořič
baterie. Politika (config „battery_policy“):
  "throttle" (výchozí)  na baterii: mezi položkami těžké práce pauza (střída ~1 : 3) a vlákno
                        v režimu EcoQoS (Windows 11 ho pošle na úsporná jádra, nižší takt);
                        při Spořiči baterie nebo ≤ LOW_BATTERY_PCT % těžká práce stojí úplně
  "pause"               na baterii těžká práce stojí, dokud se notebook nepřipojí
  "full"                bez omezení (výkon i na baterii)
Lehká práce (miniatury, opravy ořezů) na baterii jen zpomalí, nikdy nestojí – knihovna musí
zůstat prohlížitelná.

Stolní PC bez baterie = vždy „ze sítě“. Modul je bezpečný pro více vláken; stav se čte
nejvýš jednou za CACHE_S.
"""
from i18n import tr
import ctypes
import sys
import threading
import time

LOW_BATTERY_PCT = 20
CACHE_S = 5.0
HEAVY_PAUSE_S = 1.5          # na baterii po každé těžké položce (~0,5 s práce : 1,5 s klid)
LIGHT_PAUSE_S = 0.25
POLICIES = ("throttle", "pause", "full")
DEFAULT_POLICY = "throttle"

# akce governoru
RUN, THROTTLE, PAUSE = "run", "throttle", "pause"


class PowerState:
    __slots__ = ("on_battery", "percent", "saver", "has_battery")

    def __init__(self, on_battery=False, percent=None, saver=False, has_battery=False):
        self.on_battery = bool(on_battery)
        self.percent = percent
        self.saver = bool(saver)
        self.has_battery = bool(has_battery)

    def __repr__(self):
        return (f"PowerState(battery={self.on_battery}, {self.percent}%, saver={self.saver}, "
                f"has_battery={self.has_battery})")


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]


def read_system():
    """Aktuální stav napájení ze systému. Mimo Windows / při chybě = ze sítě."""
    if sys.platform != "win32":
        return PowerState()
    try:
        st = _SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return PowerState()
        no_battery = st.BatteryFlag == 128 or st.BatteryFlag == 255
        pct = None if st.BatteryLifePercent == 255 else int(st.BatteryLifePercent)
        return PowerState(on_battery=(st.ACLineStatus == 0 and not no_battery), percent=pct,
                          saver=bool(st.SystemStatusFlag & 1), has_battery=not no_battery)
    except Exception:
        return PowerState()


def decide(state, policy=DEFAULT_POLICY, heavy=True):
    """RUN | THROTTLE | PAUSE pro položku práce na pozadí."""
    if not state.on_battery or policy == "full":
        return RUN
    if not heavy:
        return THROTTLE
    if policy == "pause" or state.saver or (state.percent is not None and state.percent <= LOW_BATTERY_PCT):
        return PAUSE
    return THROTTLE


class PowerMonitor:
    def __init__(self, reader=read_system, cache_s=CACHE_S):
        self._reader = reader
        self._cache_s = cache_s
        self._lock = threading.Lock()
        self._state = None
        self._at = 0.0
        self.policy = DEFAULT_POLICY
        self.override = None            # testy / benchmark: PowerState napevno

    def set_policy(self, policy):
        self.policy = policy if policy in POLICIES else DEFAULT_POLICY

    def state(self):
        if self.override is not None:
            return self.override
        now = time.monotonic()
        with self._lock:
            if self._state is None or now - self._at >= self._cache_s:
                self._state = self._reader()
                self._at = now
            return self._state

    def action(self, heavy=True):
        return decide(self.state(), self.policy, heavy)

    def describe(self):
        st = self.state()
        if not st.on_battery:
            return tr("power-ac")
        act = self.action(True)
        pct = f" {st.percent} %" if st.percent is not None else ""
        if act == PAUSE:
            why = tr("power-saver") if st.saver else (tr("power-low") if self.policy != "pause" else "na baterii")
            return f"baterie{pct}: AI pozastavena ({why})"
        if act == THROTTLE:
            return f"baterie{pct}: AI zpomalena"
        return tr("power-battery-full", pct=pct)


# ── EcoQoS (Windows 11): úsporná jádra / nižší takt pro vlákno ──
_ThreadPowerThrottling = 3
_THREAD_POWER_THROTTLING_CURRENT_VERSION = 1
_THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1


class _THREAD_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


_eco = threading.local()


def set_thread_eco(enabled):
    """EcoQoS pro VOLAJÍCÍ vlákno (jen při změně). True = povedlo se / beze změny."""
    if sys.platform != "win32":
        return False
    enabled = bool(enabled)
    if getattr(_eco, "on", None) is enabled:
        return True
    try:
        k32 = ctypes.windll.kernel32
        st = _THREAD_POWER_THROTTLING_STATE(_THREAD_POWER_THROTTLING_CURRENT_VERSION,
                                            _THREAD_POWER_THROTTLING_EXECUTION_SPEED,
                                            _THREAD_POWER_THROTTLING_EXECUTION_SPEED if enabled else 0)
        k32.GetCurrentThread.restype = ctypes.c_void_p
        k32.SetThreadInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        ok = bool(k32.SetThreadInformation(k32.GetCurrentThread(), _ThreadPowerThrottling,
                                           ctypes.byref(st), ctypes.sizeof(st)))
        if ok:
            _eco.on = enabled
        return ok
    except Exception:
        return False


_monitor = PowerMonitor()


def get():
    return _monitor
