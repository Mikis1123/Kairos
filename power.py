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


# ── Interactive QoS for the map's web processes (2026-11-04) ──
# MapLibre's JavaScript runs in the WebView2 RENDERER process (msedgewebview2.exe), and frames
# are composited by its GPU process. Neither owns a window. Measured with
# GetProcessInformation(ProcessPowerThrottling): Chromium sets EcoQoS explicitly on BACKGROUND
# renderers (ControlMask=5, StateMask=5 on another app's WebView2), but on the VISIBLE map
# renderer it leaves execution speed to Windows (ControlMask=4: timer bit only). On battery /
# Energy Saver / "best power efficiency", Windows' heuristics may then put a windowless process
# on efficiency cores at low clock. That is the "slow on battery" map: every frame is
# main-thread JS. While the map page is shown, the app's own web processes opt out
# (EXECUTION_SPEED controlled, state 0 = HighQoS). When it is hidden they return to the
# system default. An idle map draws 0 frames, so the opt-out costs nothing at rest.
_ProcessPowerThrottling = 4
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1
WEB_PROCESS_NAMES = ("msedgewebview2.exe", "qtwebengineprocess.exe")


class _PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong), ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", ctypes.c_ulong),
                ("cntThreads", ctypes.c_ulong), ("th32ParentProcessID", ctypes.c_ulong),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


def _process_table():
    """[(pid, parent pid, exe name lower-case)] of all processes (Toolhelp snapshot)."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)                 # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile.lower()))
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(e))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return out


def web_process_ids(root_pid=None, table=None):
    """PIDs of the web-engine processes descended from root_pid (default: this process)."""
    import os
    root = os.getpid() if root_pid is None else int(root_pid)
    table = _process_table() if table is None else table
    kids = {}
    for pid, ppid, name in table:
        kids.setdefault(ppid, []).append((pid, name))
    found, todo, seen = [], [root], {root}
    while todo:
        for pid, name in kids.get(todo.pop(), ()):
            if pid in seen:
                continue
            seen.add(pid)
            todo.append(pid)
            if name in WEB_PROCESS_NAMES:
                found.append(pid)
    return found


def set_process_high_qos(pid, high):
    """high=True: the process opts out of execution-speed throttling (EcoQoS); False: back to
    the system's default. Other throttling bits (timer resolution) are kept as they are."""
    if sys.platform != "win32":
        return False
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x0200 | 0x1000, False, int(pid))   # SET_INFORMATION | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        st = _PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
        if not k32.GetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)):
            st = _PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
        st.Version = 1
        if high:
            st.ControlMask |= _PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            st.StateMask &= ~_PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        else:
            st.ControlMask &= ~_PROCESS_POWER_THROTTLING_EXECUTION_SPEED
            st.StateMask &= ~_PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        return bool(k32.SetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)))
    except Exception:
        return False
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


_PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4


def set_process_timer_resolution_honoured(pid=None):
    """This process (default) keeps its timer-resolution requests even while none of its windows
    is visible (Windows 11 otherwise drops them: 4 ms timers tick every 15.6 ms). Execution-speed
    bits are left as they are. True when applied."""
    if sys.platform != "win32":
        return False
    import os
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x0200 | 0x1000, False, int(os.getpid() if pid is None else pid))
    if not h:
        return False
    try:
        st = _PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
        if not k32.GetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)):
            st = _PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
        st.Version = 1
        st.ControlMask |= _PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        st.StateMask &= ~_PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
        return bool(k32.SetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)))
    except Exception:
        return False
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def process_qos_state(pid):
    """(ControlMask, StateMask) of a process's power throttling, or None (tests / diagnostics)."""
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        st = _PROCESS_POWER_THROTTLING_STATE(1, 0, 0)
        if not k32.GetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)):
            return None
        return st.ControlMask, st.StateMask
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def set_web_processes_interactive(high):
    """All of this app's web-engine processes to HighQoS (map shown) or system default (hidden).
    Returns the number of processes changed. Safe to call repeatedly: Chromium rewrites the
    flags on its own visibility changes, so the map re-applies this on its power poll."""
    if sys.platform != "win32":
        return 0
    try:
        return sum(1 for pid in web_process_ids() if set_process_high_qos(pid, high))
    except Exception:
        return 0


_monitor = PowerMonitor()


def get():
    return _monitor
