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

Sprint R:
  * hold s životností (set_active(..., ttl_s)) – stav hlášený z mapy (JS) nesmí zůstat viset,
    když „konec pohybu“ nikdy nepřijde (moveend během Cesty, znovu načtená stránka);
  * každý zaregistrovaný proces jde do Job Objectu „zabít při zavření“ – pád nebo tvrdé
    ukončení aplikace už nenechá ffmpeg běžet (ani zmrazený NtSuspendProcess) na pozadí.
"""
import ctypes
import sys
import threading
import time
from contextlib import contextmanager

import power

QUIET_S = 2.0
POLL_S = 0.1
POWER_POLL_S = 1.0          # na baterii pozastavená těžká práce: jak často se dívat na napájení
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class Governor:
    def __init__(self, quiet_s=QUIET_S):
        self.quiet_s = quiet_s
        self._lock = threading.RLock()
        self._holds = {}                   # důvod -> počet
        self._last_release = 0.0
        self._procs = {}                   # pid -> suspended (bool)
        self._timer = None
        self._item_pause = 0.0
        self._leases = {}                  # důvod -> threading.Timer (automatické uvolnění)
        self.suspend_count = 0             # statistika (testy / ladění)
        self.power_paused = False          # Gate 0: těžká práce čeká na napájení ze sítě
        self.throttled_items = 0           # Gate 0: položky zpomalené kvůli baterii

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

    def set_active(self, reason, active, ttl_s=None):
        """Idempotentní varianta pro stavové zdroje (přehrávání ano/ne). ttl_s: hold se sám
        uvolní, nepřijde-li do té doby nové hlášení (zdroj, který „konec“ nemusí poslat)."""
        with self._lock:
            has = reason in self._holds
            self._cancel_lease(reason)
            if active and ttl_s:
                lease = threading.Timer(float(ttl_s), self._expire, args=(reason,))
                lease.daemon = True
                self._leases[reason] = lease
                lease.start()
        if active and not has:
            self.hold(reason)
        elif not active and has:
            with self._lock:
                self._holds.pop(reason, None)
                if not self._holds:
                    self._last_release = time.monotonic()
                    self._schedule_resume()

    def _expire(self, reason):
        with self._lock:
            self._leases.pop(reason, None)
        self.set_active(reason, False)

    def _cancel_lease(self, reason):
        lease = self._leases.pop(reason, None)
        if lease is not None:
            lease.cancel()

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

    def checkpoint(self, should_stop=None, heavy=False):
        """Volá se mezi položkami práce na pozadí. True = skončit.

        Gate 0 – napájení (power.py): na baterii se těžká práce (heavy: CLIP, SigLIP, tváře,
        OCR, videa) zpomalí a vlákno přejde do EcoQoS; při Spořiči baterie / slabé baterii
        (nebo politice „pause“) těžká práce čeká, dokud se notebook nepřipojí."""
        if not self.wait_quiet(should_stop):
            return True
        mon = power.get()
        act = mon.action(heavy)
        power.set_thread_eco(act != power.RUN)
        if act == power.PAUSE:
            self.power_paused = True
            while mon.action(heavy) == power.PAUSE:
                if should_stop is not None and should_stop():
                    return True
                time.sleep(POWER_POLL_S)
            self.power_paused = False
            power.set_thread_eco(mon.action(heavy) != power.RUN)
            if not self.wait_quiet(should_stop):
                return True
        pause = self._item_pause
        if act == power.THROTTLE:
            pause += power.HEAVY_PAUSE_S if heavy else power.LIGHT_PAUSE_S
            self.throttled_items += 1
        if pause:
            time.sleep(pause)
        return bool(should_stop is not None and should_stop())

    # ── procesy (ffmpeg) ──
    def register_process(self, pid):
        """Proces na pozadí: pozastaví se, kdykoli je rušno (i hned, když už rušno je)."""
        assign_to_job(pid)
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


# ── Job Object: podřízené procesy umřou s aplikací ──
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_job = None
_job_lock = threading.Lock()


def _job_handle():
    """Jeden Job Object za běh (handle drží proces; zavře ho až systém při zániku procesu,
    a tím zabije všechny přiřazené procesy). None mimo Windows / když ho nelze vytvořit."""
    global _job
    if sys.platform != "win32":
        return None
    with _job_lock:
        if _job is None:
            try:
                from ctypes import wintypes

                class _Basic(ctypes.Structure):
                    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                                ("SchedulingClass", wintypes.DWORD)]

                class _Io(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

                class _Extended(ctypes.Structure):
                    _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

                kernel32 = ctypes.windll.kernel32
                kernel32.CreateJobObjectW.restype = wintypes.HANDLE
                handle = kernel32.CreateJobObjectW(None, None)
                if not handle:
                    _job = 0
                    return None
                info = _Extended()
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not kernel32.SetInformationJobObject(wintypes.HANDLE(handle), _JOB_EXTENDED_LIMIT_INFORMATION,
                                                        ctypes.byref(info), ctypes.sizeof(info)):
                    kernel32.CloseHandle(wintypes.HANDLE(handle))
                    _job = 0
                    return None
                _job = handle
            except Exception:
                _job = 0
        return _job or None


def assign_to_job(pid):
    """Přiřadí proces do Job Objectu aplikace. True = povedlo se."""
    job = _job_handle()
    if not job:
        return False
    try:
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(handle)))
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        return False


def _resume_pid(pid):
    return _nt_call(pid, "NtResumeProcess")


_governor = Governor()


def get():
    return _governor
