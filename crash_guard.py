"""Crash guard: no Python exception in a Qt slot may silently kill the app.

PyQt6 turns an exception escaping a slot / timer callback / event handler into qFatal() when
sys.excepthook is the default one, so the process dies on the spot (exit 127 / 0xC0000409) with
no message at all when there is no console (pythonw, shortcut). install() replaces the hook:
the traceback is appended to %LOCALAPPDATA%\\Kairos\\logs\\crash.log (and printed when a console
exists) and the event loop keeps running. Native crashes (access violations) are written to
fault.log by faulthandler, with every thread's Python stack.
"""
import datetime
import faulthandler
import os
import sys
import threading
import traceback

CRASH_LOG = "crash.log"
FAULT_LOG = "fault.log"
MAX_LOG_BYTES = 512 * 1024

_installed = False
_fault_file = None
_lock = threading.Lock()
_log_dir = None


def _log_path(name):
    if _log_dir is None:
        return None
    try:
        os.makedirs(_log_dir, exist_ok=True)
        path = os.path.join(_log_dir, name)
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")              # one generation of history is enough
        return path
    except OSError:
        return None


def report(exc_type, exc, tb, where="slot"):
    """Writes one exception to crash.log (+ stderr when present). Never raises."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        block = f"--- {stamp} [{where}] {threading.current_thread().name}\n{text}\n"
        path = _log_path(CRASH_LOG)
        if path:
            with _lock, open(path, "a", encoding="utf-8") as f:
                f.write(block)
        stream = sys.stderr
        if stream is not None:
            stream.write(block)
            stream.flush()
    except Exception:
        pass


def _excepthook(exc_type, exc, tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    report(exc_type, exc, tb)


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    report(args.exc_type, args.exc_value, args.exc_traceback, where=f"thread {getattr(args.thread, 'name', '?')}")


def install(log_dir):
    """Idempotent. Call before QApplication / the first widget."""
    global _installed, _fault_file, _log_dir
    if _installed:
        return                                         # the first caller's log dir wins
    _installed = True
    _log_dir = log_dir
    sys.excepthook = _excepthook
    threading.excepthook = _thread_hook
    path = _log_path(FAULT_LOG)
    try:
        if path:
            _fault_file = open(path, "a", encoding="utf-8")
            _fault_file.write(f"--- session {datetime.datetime.now().isoformat(timespec='seconds')} pid {os.getpid()}\n")
            _fault_file.flush()
            faulthandler.enable(_fault_file, all_threads=True)
        elif sys.stderr is not None:
            faulthandler.enable(all_threads=True)
    except Exception:
        pass
