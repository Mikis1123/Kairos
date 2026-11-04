"""Cold-boot milestones (pillar 5).

mark(name) records milliseconds since the OS created this process (GetProcessTimes on
Windows, so interpreter start-up is included). With KAIROS_BOOT_TRACE=<file.json> the marks are
written there when the grid paints its first frame and the app quits – eval/bench_cold_boot.py
spawns the app repeatedly that way. Without the variable the module only keeps a tiny list.
"""
import os
import sys
import time

_marks = []
_t0_perf = time.perf_counter()
_spawn_offset_ms = 0.0          # ms between process creation and this module's import


def _process_age_ms():
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        # restype matters: the default c_int truncates the pseudo-handle (-1) to 0xFFFFFFFF on
        # 64-bit and GetProcessTimes then fails with "invalid handle" -> offset silently 0
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        ft = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(k32.GetCurrentProcess(), *[ctypes.byref(f) for f in ft]):
            return 0.0
        now = wintypes.FILETIME()
        k32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))
        to100ns = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime     # noqa: E731
        return max(0.0, (to100ns(now) - to100ns(ft[0])) / 10_000.0)
    except Exception:
        return 0.0


_spawn_offset_ms = _process_age_ms()


def now_ms():
    """Milliseconds since process creation."""
    return _spawn_offset_ms + (time.perf_counter() - _t0_perf) * 1000.0


def mark(name):
    _marks.append((name, round(now_ms(), 1)))


def marks():
    return list(_marks)


def target():
    return os.environ.get("KAIROS_BOOT_TRACE") or None


def finish(app=None):
    """First frame painted: dump the trace (when requested) and end the traced run."""
    mark("first_frame")
    path = target()
    if not path:
        return
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"marks": _marks, "spawn_offset_ms": round(_spawn_offset_ms, 1)}, f)
    if app is not None and os.environ.get("KAIROS_BOOT_TRACE_STAY") != "1":
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(50, app.quit)


mark("interpreter")
