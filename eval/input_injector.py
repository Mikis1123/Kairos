"""Real OS input for the map latency bench, from a SEPARATE process (no GIL shared with the app).

    python eval/input_injector.py <plan.json> <log.json>

plan = {"x0","y0" (screen px), "steps": [[kind, ...], ...]} with kinds
    ["move", x, y]           absolute cursor move
    ["down"], ["up"]         left button
    ["rdown"], ["rup"]       right button
    ["wheel", delta]         120 = one notch
    ["sleep", ms]
    ["drag", dx, dy, ms, hz] straight drag at hz input reports per second (button already down)
The log has one [kind, x, y, epoch_s] per injected event (epoch from GetSystemTimePreciseAsFileTime,
the same wall clock Chromium's performance.timeOrigin is based on).
"""
import ctypes, json, sys, time
from ctypes import wintypes

user32 = ctypes.windll.user32
ctypes.windll.shcore.SetProcessDpiAwareness(2)   # before any metrics: physical px
winmm = ctypes.windll.winmm
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


MOVE, ABS, VDESK = 0x0001, 0x8000, 0x4000
LDOWN, LUP, RDOWN, RUP, WHEEL = 0x0002, 0x0004, 0x0008, 0x0010, 0x0800
SM_XV, SM_YV, SM_CXV, SM_CYV = 76, 77, 78, 79
vx, vy = user32.GetSystemMetrics(SM_XV), user32.GetSystemMetrics(SM_YV)
vw, vh = user32.GetSystemMetrics(SM_CXV), user32.GetSystemMetrics(SM_CYV)
log = []
cur = [0, 0]


def send(flags, dx=0, dy=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))


def move(x, y):
    cur[0], cur[1] = x, y
    ax = round((x - vx) * 65535 / max(1, vw - 1))
    ay = round((y - vy) * 65535 / max(1, vh - 1))
    send(MOVE | ABS | VDESK, ax, ay)
    log.append(["move", x, y, time.time()])


def precise_sleep_until(t):
    while True:
        d = t - time.perf_counter()
        if d <= 0:
            return
        if d > 0.002:
            time.sleep(d - 0.0015)


def main():
    plan = json.load(open(sys.argv[1]))
    winmm.timeBeginPeriod(1)
    move(plan["x0"], plan["y0"])
    for st in plan["steps"]:
        k = st[0]
        if k == "move":
            move(st[1], st[2])
        elif k in ("down", "up", "rdown", "rup"):
            send({"down": LDOWN, "up": LUP, "rdown": RDOWN, "rup": RUP}[k])
            log.append([k, cur[0], cur[1], time.time()])
        elif k == "wheel":
            send(WHEEL, data=int(st[1]))
            log.append(["wheel", cur[0], cur[1], time.time()])
        elif k == "sleep":
            time.sleep(st[1] / 1000)
        elif k == "drag":
            dx, dy, ms, hz = st[1], st[2], st[3], st[4]
            n = max(1, int(ms * hz / 1000))
            x0, y0 = cur
            t0 = time.perf_counter()
            for i in range(1, n + 1):
                precise_sleep_until(t0 + i / hz)
                move(round(x0 + dx * i / n), round(y0 + dy * i / n))
    winmm.timeEndPeriod(1)
    json.dump(log, open(sys.argv[2], "w"))


if __name__ == "__main__":
    main()
