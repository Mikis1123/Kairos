"""Stage 3: složka Skryté 🔒 – Qt část (jádro bez UI je locked_folder.py).

* UnlockSheet – Windows Hello (PIN / otisk / obličej) se spustí hned; heslo jako záloha.
  Při prvním otevření nastavení hesla.
* CaptureShield – po dobu odemčení okno chybí ve snímcích obrazovky, sdílení, Alt+Tab
  a náhledech hlavního panelu (SetWindowDisplayAffinity + DWM).
* SessionWatcher – zamčení relace Windows (Win+L), uspání -> zamknout.
* IdleWatcher – 2 minuty bez myši / klávesnice -> zamknout.
* LockedFolderController – drží to pohromadě; MainWindow volá request_unlock() / lock().
"""
from i18n import N_, tr
import ctypes
import os

from PyQt6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

import locked_folder
from ui_controls import KButton

HELLO_MESSAGE = N_("locked-hello-prompt")


# ── ochrana obrazovky ────────────────────────────────────────────────────────────────────

WDA_NONE = 0x0
WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11
DWMWA_FORCE_ICONIC_REPRESENTATION = 7
DWMWA_DISALLOW_PEEK = 11


def _dwm_bool(hwnd, attr, value):
    v = ctypes.c_int(1 if value else 0)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))
    except Exception:
        pass


def set_capture_protection(hwnd, on):
    """True = okno je vyloučené ze snímání (screenshoty, sdílení obrazovky, náhledy Alt+Tab
    a hlavního panelu ukážou prázdno / ikonu). Vrací použitou afinitu (0 = nic)."""
    if os.name != "nt" or not hwnd:
        return 0
    user32 = ctypes.windll.user32
    user32.SetWindowDisplayAffinity.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    applied = 0
    if on:
        for affinity in (WDA_EXCLUDEFROMCAPTURE, WDA_MONITOR):      # Win10 2004+ / starší
            if user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), affinity):
                applied = affinity
                break
    else:
        user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), WDA_NONE)
    _dwm_bool(hwnd, DWMWA_FORCE_ICONIC_REPRESENTATION, on)     # hlavní panel: ikona, ne obsah
    _dwm_bool(hwnd, DWMWA_DISALLOW_PEEK, on)                   # Aero Peek neprohlédne
    return applied


def get_capture_affinity(hwnd):
    if os.name != "nt" or not hwnd:
        return 0
    out = ctypes.c_uint32(0)
    user32 = ctypes.windll.user32
    user32.GetWindowDisplayAffinity.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
    user32.GetWindowDisplayAffinity(ctypes.c_void_p(hwnd), ctypes.byref(out))
    return out.value


# ── zamčení relace / uspání ──────────────────────────────────────────────────────────────

WM_WTSSESSION_CHANGE = 0x02B1
WM_POWERBROADCAST = 0x0218
WTS_SESSION_LOCK = 0x7
WTS_CONSOLE_DISCONNECT = 0x2
WTS_REMOTE_DISCONNECT = 0x4
PBT_APMSUSPEND = 0x4
NOTIFY_FOR_THIS_SESSION = 0


class SessionWatcher(QAbstractNativeEventFilter):
    """Zprávy Windows pro hlavní okno: zamčení relace, odpojení, uspání -> on_lock()."""

    def __init__(self, on_lock):
        super().__init__()
        self._on_lock = on_lock
        self._hwnd = 0

    def register(self, hwnd):
        if os.name != "nt" or not hwnd:
            return False
        self._hwnd = int(hwnd)
        try:
            return bool(ctypes.windll.wtsapi32.WTSRegisterSessionNotification(
                ctypes.c_void_p(self._hwnd), NOTIFY_FOR_THIS_SESSION))
        except Exception:
            return False

    def unregister(self):
        if self._hwnd and os.name == "nt":
            try:
                ctypes.windll.wtsapi32.WTSUnRegisterSessionNotification(ctypes.c_void_p(self._hwnd))
            except Exception:
                pass
        self._hwnd = 0

    @staticmethod
    def is_lock_message(message, wparam):
        if message == WM_WTSSESSION_CHANGE:
            return wparam in (WTS_SESSION_LOCK, WTS_CONSOLE_DISCONNECT, WTS_REMOTE_DISCONNECT)
        return message == WM_POWERBROADCAST and wparam == PBT_APMSUSPEND

    def nativeEventFilter(self, event_type, message):
        if event_type in (b"windows_generic_MSG", "windows_generic_MSG") and message is not None:
            try:
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if self.is_lock_message(msg.message, msg.wParam):
                    self._on_lock("session")
            except Exception:
                pass
        return False, 0


# ── nečinnost ────────────────────────────────────────────────────────────────────────────

_ACTIVITY_EVENTS = {QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress,
                    QEvent.Type.Wheel, QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                    QEvent.Type.NativeGesture}


class IdleWatcher(QObject):
    """Filtr událostí aplikace: každý vstup posune odpočet; po timeout_s bez vstupu timeout."""
    timeout = pyqtSignal()

    def __init__(self, timeout_s=locked_folder.AUTO_LOCK_IDLE_S, parent=None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(timeout_s * 1000))
        self._timer.timeout.connect(self.timeout)
        self._active = False

    def start(self):
        if not self._active:
            QApplication.instance().installEventFilter(self)
            self._active = True
        self._timer.start()

    def stop(self):
        if self._active:
            QApplication.instance().removeEventFilter(self)
            self._active = False
        self._timer.stop()

    def is_running(self):
        return self._timer.isActive()

    def eventFilter(self, obj, event):
        if self._active and event.type() in _ACTIVITY_EVENTS:
            self._timer.start()
        return False


# ── Windows Hello mimo GUI vlákno ────────────────────────────────────────────────────────

class HelloWorker(QThread):
    done = pyqtSignal(str)

    def __init__(self, message=None, parent=None):
        super().__init__(parent)
        self.message = message or tr(HELLO_MESSAGE)

    def run(self):
        self.done.emit(locked_folder.hello_verify(self.message))


class HelloProbe(QThread):
    done = pyqtSignal(bool)

    def run(self):
        self.done.emit(locked_folder.hello_available())


# ── list odemčení / nastavení ────────────────────────────────────────────────────────────

def _label(text, name=None, wrap=True):
    lbl = QLabel(text)
    lbl.setWordWrap(wrap)
    if name:
        lbl.setObjectName(name)
    return lbl


class UnlockSheet(QDialog):
    """mode "setup" = nastavit heslo (poprvé), "unlock" = Hello + heslo, "change" = změnit heslo."""

    def __init__(self, vault, mode="unlock", hello=False, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.mode = mode
        self.method = None                    # "hello" | "passcode" po úspěchu
        self._hello_worker = None
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setWindowTitle(tr("nav-locked"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        titles = {"setup": tr("locked-setup-title"), "unlock": tr("nav-locked"), "change": tr("locked-change-title")}
        lay.addWidget(_label(f"<b style='font-size:15px'>{titles[mode]}</b>", "sheetTitle"))
        bodies = {
            "setup": tr("locked-setup-body"),
            "unlock": (tr("locked-unlock-body-hello") if hello
                       else tr("locked-unlock-body")),
            "change": tr("locked-change-body"),
        }
        lay.addWidget(_label(bodies[mode], "sheetBody"))
        self.edit_old = self.edit_new = self.edit_repeat = None
        if mode in ("unlock", "change"):
            self.edit_old = self._field(tr("locked-current-passcode") if mode == "change" else tr("locked-passcode"), lay)
        if mode in ("setup", "change"):
            self.edit_new = self._field(tr("locked-new-passcode", PASSCODE_MIN=locked_folder.PASSCODE_MIN), lay)
            self.edit_repeat = self._field(tr("locked-new-passcode-again"), lay)
        self.lbl_error = _label("", "sheetBody")
        self.lbl_error.setStyleSheet("color: #ff6b6b;")
        self.lbl_error.hide()
        lay.addWidget(self.lbl_error)
        row = QHBoxLayout()
        self.btn_hello = None
        if mode == "unlock" and hello:
            self.btn_hello = KButton(tr("locked-windows-hello"))
            self.btn_hello.clicked.connect(self.start_hello)
            row.addWidget(self.btn_hello)
        row.addStretch()
        cancel = KButton(tr("action-cancel"))
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        actions = {"setup": "Nastavit", "unlock": "Odemknout", "change": tr("action-change")}
        self.btn_ok = KButton(actions[mode])
        self.btn_ok.setObjectName("btnSave")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self._submit)
        row.addWidget(self.btn_ok)
        lay.addLayout(row)
        self._lockout_timer = QTimer(self)
        self._lockout_timer.setInterval(1000)
        self._lockout_timer.timeout.connect(self._sync_lockout)
        if mode == "unlock":
            self._sync_lockout()
            if hello:
                QTimer.singleShot(150, self.start_hello)       # Hello hned, heslo jako záloha

    def _field(self, placeholder, lay):
        edit = QLineEdit()
        edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setPlaceholderText(placeholder)
        edit.setFixedHeight(32)
        edit.returnPressed.connect(self._submit)
        lay.addWidget(edit)
        return edit

    def _error(self, text):
        self.lbl_error.setText(text)
        self.lbl_error.setVisible(bool(text))

    def _sync_lockout(self):
        wait = self.vault.lockout_remaining() if self.vault.is_configured() else 0
        if self.edit_old is not None:
            self.edit_old.setEnabled(wait <= 0)
        self.btn_ok.setEnabled(wait <= 0)
        if wait > 0:
            self._error(tr("locked-too-many-attempts", wait=wait))
            self._lockout_timer.start()
        elif self._lockout_timer.isActive():
            self._lockout_timer.stop()
            self._error("")

    # ── Hello ──
    def start_hello(self):
        if self._hello_worker is not None and self._hello_worker.isRunning():
            return
        if self.btn_hello is not None:
            self.btn_hello.setEnabled(False)
        self._hello_worker = HelloWorker(parent=self)
        self._hello_worker.done.connect(self._on_hello)
        self._hello_worker.start()

    def _on_hello(self, result):
        if self.btn_hello is not None:
            self.btn_hello.setEnabled(True)
        if result == locked_folder.HELLO_VERIFIED:
            try:
                self.vault.unlock_with_os()
            except locked_folder.VaultError as e:
                self._error(tr("locked-error-enter-passcode", error=str(e)))
                return
            self.method = "hello"
            self.accept()
            return
        if result == locked_folder.HELLO_UNAVAILABLE:
            self._error(tr("locked-hello-unavailable"))
        elif result == locked_folder.HELLO_FAILED:
            self._error(tr("locked-verify-failed"))
        if self.edit_old is not None:
            self.edit_old.setFocus()

    # ── heslo ──
    def _submit(self):
        if not self.btn_ok.isEnabled():
            return
        try:
            if self.mode == "setup":
                new, rep = self.edit_new.text(), self.edit_repeat.text()
                if new != rep:
                    self._error(tr("locked-mismatch"))
                    return
                self.vault.setup(new)
                self.method = "passcode"
                self.accept()
            elif self.mode == "unlock":
                self.btn_ok.setEnabled(False)
                QApplication.processEvents()
                ok = self.vault.unlock_with_passcode(self.edit_old.text())
                self.btn_ok.setEnabled(True)
                if ok:
                    self.method = "passcode"
                    self.accept()
                else:
                    self.edit_old.selectAll()
                    self._error(tr("locked-wrong"))
                    self._sync_lockout()
            else:
                new, rep = self.edit_new.text(), self.edit_repeat.text()
                if new != rep:
                    self._error(tr("locked-new-mismatch"))
                    return
                if self.vault.change_passcode(self.edit_old.text(), new):
                    self.method = "passcode"
                    self.accept()
                else:
                    self._error(tr("locked-current-wrong"))
                    self._sync_lockout()
        except locked_folder.LockedOut as e:
            self.btn_ok.setEnabled(True)
            self._error(tr("locked-too-many-attempts-seconds", seconds=e.seconds))
            self._sync_lockout()
        except locked_folder.VaultError as e:
            self.btn_ok.setEnabled(True)
            self._error(str(e))

    def done(self, r):
        if self.edit_old is not None:            # hesla v paměti widgetu co nejkratší dobu
            self.edit_old.clear()
        for e in (self.edit_new, self.edit_repeat):
            if e is not None:
                e.clear()
        if self._hello_worker is not None and self._hello_worker.isRunning():
            self._hello_worker.wait(100)
        super().done(r)


# ── řadič ────────────────────────────────────────────────────────────────────────────────

class LockedFolderController(QObject):
    """Stav odemčení složky Skryté pro jedno hlavní okno."""
    unlocked = pyqtSignal()
    locked = pyqtSignal(str)          # důvod: "manual" | "minimize" | "session" | "idle" | "navigation"

    def __init__(self, window, vault=None, idle_s=locked_folder.AUTO_LOCK_IDLE_S):
        super().__init__(window)
        self.window = window
        self.vault = vault or locked_folder.get()
        self._hello = None                # None = ještě nezjištěno
        self.shield_affinity = 0
        self.idle = IdleWatcher(idle_s, self)
        self.idle.timeout.connect(lambda: self.lock("idle"))
        self.session = SessionWatcher(self.lock)
        self._session_registered = False
        self._probe = HelloProbe(self)
        self._probe.done.connect(self._on_probe)
        self._probe.start()
        self._reason = "manual"
        self.vault.add_lock_listener(self._on_vault_locked)

    def _on_probe(self, ok):
        self._hello = bool(ok)

    def hello_available(self):
        if self._hello is None:
            if self._probe.isRunning():
                self._probe.wait(1500)
            if self._hello is None:
                self._hello = locked_folder.hello_available()
        return self._hello and self.vault.os_key_available()

    @property
    def is_unlocked(self):
        return self.vault.is_unlocked

    def install_session_watch(self):
        """Po zobrazení okna (je HWND): zamčení relace / uspání."""
        if self._session_registered:
            return
        app = QApplication.instance()
        if app is not None:
            app.installNativeEventFilter(self.session)
        self._session_registered = self.session.register(int(self.window.winId()))

    def shutdown(self):
        self.lock("manual")
        self.session.unregister()
        app = QApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(self.session)

    def request_unlock(self, parent=None):
        """Odemkne (nastaví při prvním použití). True = odemčeno."""
        if self.vault.is_unlocked:
            self._on_unlocked()
            return True
        mode = "unlock" if self.vault.is_configured() else "setup"
        sheet = UnlockSheet(self.vault, mode, hello=(mode == "unlock" and self.hello_available()),
                            parent=parent or self.window)
        if sheet.exec() != QDialog.DialogCode.Accepted or not self.vault.is_unlocked:
            return False
        self._on_unlocked()
        return True

    def ensure_configured(self, parent=None):
        """Před prvním skrytím: nastavit heslo (odemkne). True = trezor existuje."""
        if self.vault.is_configured():
            return True
        return self.request_unlock(parent)

    def change_passcode(self, parent=None):
        if not self.vault.is_configured():
            return self.request_unlock(parent)
        was = self.vault.is_unlocked
        sheet = UnlockSheet(self.vault, "change", parent=parent or self.window)
        ok = sheet.exec() == QDialog.DialogCode.Accepted
        if ok and not was:
            self.vault.lock()            # změna hesla nemá nechat složku odemčenou
        return ok

    def _on_unlocked(self):
        self.shield_affinity = set_capture_protection(int(self.window.winId()), True)
        self.idle.start()
        self.install_session_watch()
        self.unlocked.emit()

    def lock(self, reason="manual"):
        self._reason = reason
        if not self.vault.lock():
            self._teardown()             # už zamčeno: aspoň jistota, že štít a odpočet nejsou
        return True

    def _on_vault_locked(self):
        self._teardown()
        self.locked.emit(self._reason)
        self._reason = "manual"

    def _teardown(self):
        self.idle.stop()
        if self.shield_affinity:
            set_capture_protection(int(self.window.winId()), False)
            self.shield_affinity = 0
