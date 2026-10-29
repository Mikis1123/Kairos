"""Strážce interakce: práce na pozadí nikdy nebrzdí to, na co se uživatel právě dívá.

Kdo potřebuje plný výkon (přehrávané video, gesto zoomu, posouvání mřížky, Cesta, let mapy),
drží „hold“ s důvodem. Dokud nějaký hold trvá – a ještě QUIET_S po posledním uvolnění – je
stav „busy“:
  * vlákna na pozadí (démon AI, náhledy, otisky) čekají v checkpoint() mezi položkami,
  * zaregistrované procesy ffmpeg (náhledový pás, proxy videí) jsou pozastavené na úrovni
    systému (NtSuspendProcess) a po zklidnění pokračují tam, kde přestaly.

Nižší výkonnostní profil (perf_profile) navíc dává mezi položkami pauzu, aby i nečinné
základní PC mělo pro UI vzduch. Modul je bezpečný pro více vláken: hold/release volá GUI
vlákno, checkpoint pracovní vlákna.
"""
import ctypes
import sys
import threading
import time
from contextlib import contextmanager

QUIET_S = 2.0
POLL_S = 0.1
PROCESS_SUSPEND_RESUME = 0x0800


class Governor:
    def __init__(self, quiet_s=QUIET_S):
        self.quiet_s = quiet_s
        self._lock = threading.RLock()
        self._holds = {}                   # důvod -> počet
        self._last_release = 0.0
        self._procs = {}                   # pid -> suspended (bool)
        self._timer = None
        self._item_pause = 0.0
        self.suspend_count = 0             # statistika (testy / ladění)

    # ── interakce (GUI vlákno) ──
    def hold(self, reason):
        with self._lock:
            self._holds[reason] = self._holds.get(reason, 0) + 1
            self._cancel_timer()
            self._suspend_all()

    def release(self, reason):
        with self._lock:
            n = self._holds.get(reason, 0)
            if n <= 1:
                self._holds.pop(reason, None)
            else:
                self._holds[reason] = n - 1
            if not self._holds:
                self._last_release = time.monotonic()
                self._schedule_resume()

    def set_active(self, reason, active):
        """Idempotentní varianta pro stavové zdroje (přehrávání ano/ne)."""
        with self._lock:
            has = reason in self._holds
        if active and not has:
            self.hold(reason)
        elif not active and has:
            with self._lock:
                self._holds.pop(reason, None)
                if not self._holds:
                    self._last_release = time.monotonic()
                    self._schedule_resume()

    @contextmanager
    def holding(self, reason):
        self.hold(reason)
        try:
            yield
        finally:
            self.release(reason)

    def reasons(self):
        with self._lock:
            return sorted(self._holds)

    def busy(self):
        with self._lock:
            return bool(self._holds) or (time.monotonic() - self._last_release) < self.quiet_s

    # ── pozadí (pracovní vlákna) ──
    def set_item_pause(self, seconds):
        self._item_pause = max(0.0, float(seconds))

    def wait_quiet(self, should_stop=None, timeout=None):
        """Počká, až bude klid. False = máme skončit (should_stop) nebo vypršel timeout."""
        start = time.monotonic()
        while self.busy():
            if should_stop is not None and should_stop():
                return False
            if timeout is not None and time.monotonic() - start >= timeout:
                return False
            time.sleep(POLL_S)
        return not (should_stop is not None and should_stop())

    def checkpoint(self, should_stop=None):
        """Volá se mezi položkami práce na pozadí. True = skončit."""
        if not self.wait_quiet(should_stop):
            return True
        if self._item_pause:
            time.sleep(self._item_pause)
        return bool(should_stop is not None and should_stop())

    # ── procesy (ffmpeg) ──
    def register_process(self, pid):
        """Proces na pozadí: pozastaví se, kdykoli je rušno (i hned, když už rušno je)."""
        with self._lock:
            self._procs[int(pid)] = False
            if self.busy():
                self._suspend(int(pid))
                if not self._holds:
                    self._schedule_resume()

    def unregister_process(self, pid):
        with self._lock:
            suspended = self._procs.pop(int(pid), False)
        if suspended:
            _resume_pid(int(pid))         # zabitý / dokončený proces nenechat zmrazený

    def _suspend_all(self):
        for pid in list(self._procs):
            self._suspend(pid)

    def _suspend(self, pid):
        if not self._procs.get(pid) and _suspend_pid(pid):
            self._procs[pid] = True
            self.suspend_count += 1

    def _resume_all(self):
        with self._lock:
            if self.busy():
                self._schedule_resume()
                return
            for pid, suspended in list(self._procs.items()):
                if suspended:
                    _resume_pid(pid)
                    self._procs[pid] = False

    def _schedule_resume(self):
        self._cancel_timer()
        if not any(self._procs.values()):
            return
        delay = max(0.05, self.quiet_s - (time.monotonic() - self._last_release))
        self._timer = threading.Timer(delay, self._resume_all)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


def _nt_call(pid, fn_name):
    if sys.platform != "win32":
        return False
    try:
        kernel32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
        handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
        if not handle:
            return False
        try:
            return getattr(ntdll, fn_name)(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _suspend_pid(pid):
    return _nt_call(pid, "NtSuspendProcess")


def _resume_pid(pid):
    return _nt_call(pid, "NtResumeProcess")


_governor = Governor()


def get():
    return _governor
