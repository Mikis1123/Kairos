"""Gate 2: the in-app map hosted in WebView2 (Edge Chromium) instead of QtWebEngine.

Why: QtWebEngine 6.11 locks requestAnimationFrame to exactly 60.0 fps on a 120 Hz panel, and no
Chromium flag lifts that cap with vsync pacing intact. WebView2 measured 119.5 fps, vsync-paced,
on the same machine (eval/bench_map_webview2.py). See the memory notes for the measurements.

How:
  * WebView2Host is a native QWidget. A CoreWebView2Controller is created with that widget's
    HWND as its parent, through pythonnet and the WebView2 SDK's Microsoft.Web.WebView2.Core.dll
    (vendor/webview2, with WebView2Loader.dll beside it). No WinForms, no second message loop:
    WebView2 runs on the Qt GUI thread's own Win32 message pump.
  * The widget mimics the small part of QWebEngineView main.py uses: load(), page() ->
    runJavaScript(code, callback), loadStarted / loadFinished, contextMenuPolicy. That keeps
    the ~15 call sites (and the tests that mock page().runJavaScript) unchanged.
  * Python <- JS: an init script defines window.__kairosBridge with the same method names as
    MapBridge; each call posts {"m": name, "a": [args]} through chrome.webview.postMessage and
    WebMessageBridge dispatches it to the matching MapBridge slot (whitelisted names only).
  * Network allowlist: a Content-Security-Policy in map_vault.html (static, test-pinned against
    main.ALLOWED_WEB_HOSTS), not WebResourceRequested: that event would put every tile request
    through a synchronous round trip to the GUI thread. Navigation away from the map page and
    new windows are refused here.
  * Airspace: the WebView2 surface is an HWND, so Qt siblings cannot paint over it. Overlays
    that float over the map are handled two ways (AirspaceGuard):
      - "native" overlays (toasts, the F12 meter) get their own child HWND above the map,
      - modal overlays (sheets, command palette, guide callouts) freeze the map: a still frame
        is captured (CapturePreviewAsync), painted by this widget, and the live view is hidden
        until the last such overlay closes.
  * Keyboard: while WebView2 has focus, Qt never sees the keys. Accelerator keys that match a
    QShortcut / QAction of the window are handed back to Qt; browser accelerators (reload, find,
    print, zoom) are disabled.
  * Files dropped on the map arrive as CoreWebView2File objects (postMessageWithAdditionalObjects)
    with real paths -> filesDropped(list).

Fallback: if pythonnet, the SDK or the Evergreen runtime is missing, or the Qt platform has no
real HWNDs (offscreen tests), available() is False and main.py keeps the QtWebEngine view.
KAIROS_MAP_HOST=qtwebengine forces the old host; =webview2 refuses the fallback.
"""
import json
import os
import sys
import tempfile
import time

from PyQt6.QtCore import QEvent, QKeyCombination, QObject, QRect, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeySequence, QPainter, QAction, QShortcut, QPainterPath, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

RESOURCE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
SDK_DIR = os.path.join(RESOURCE_DIR, "vendor", "webview2")
CORE_DLL = "Microsoft.Web.WebView2.Core.dll"
LOADER_DLL = "WebView2Loader.dll"

# --allow-file-access-from-files: map_vault.html is a file:// page whose thumbnails, clips and
#   music are file:// too, fetched by MapLibre (QtWebEngine allowed this via LocalContentCanAccess*).
# --autoplay-policy: Journey music starts from an app button, not a gesture in the page.
BROWSER_ARGS = ("--allow-file-access-from-files --autoplay-policy=no-user-gesture-required "
                "--disable-features=ElasticOverscroll,msSmartScreenProtection "
                "--ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy")
DISK_CACHE_MB = 256
POLL_MS = 4                      # pending .NET tasks are polled from a Qt timer (no blocking waits)
SNAPSHOT_QUALITY_FORMAT = "Jpeg"

BRIDGE_JS = """(function () {
  if (!(window.chrome && chrome.webview)) return;
  var names = %s, b = {};
  names.forEach(function (n) {
    b[n] = function () {
      chrome.webview.postMessage(JSON.stringify({m: n, a: Array.prototype.slice.call(arguments)}));
    };
  });
  b.dropFiles = function (list) { chrome.webview.postMessageWithAdditionalObjects('kairos-drop', list); };
  window.__kairosBridge = b;
  window.__kairosHost = 'webview2';
})();"""

_clr = None                      # namespace of loaded .NET types, or an Exception
_availability = None


def _load_clr():
    """Loads pythonnet + the WebView2 SDK once. Returns a dict of .NET types; raises on failure."""
    global _clr
    if isinstance(_clr, dict):
        return _clr
    if isinstance(_clr, Exception):
        raise _clr
    try:
        dll = os.path.join(SDK_DIR, CORE_DLL)
        if not (os.path.isfile(dll) and os.path.isfile(os.path.join(SDK_DIR, LOADER_DLL))):
            raise FileNotFoundError(f"WebView2 SDK missing in {SDK_DIR}")
        import clr                                  # pythonnet (.NET Framework 4.x runtime)
        clr.AddReference("System.Drawing")
        clr.AddReference(dll)
        import System
        from System import IntPtr
        from System.Drawing import Color, Rectangle
        from Microsoft.Web.WebView2 import Core
        _clr = {"System": System, "IntPtr": IntPtr, "Color": Color, "Rectangle": Rectangle, "Core": Core}
    except Exception as e:                          # noqa: BLE001 – any failure = fallback host
        _clr = e
        raise
    return _clr


def runtime_version():
    """Installed WebView2 (Evergreen) runtime version, or None."""
    try:
        return str(_load_clr()["Core"].CoreWebView2Environment.GetAvailableBrowserVersionString(None) or "") or None
    except Exception:
        return None


def requested_backend():
    return (os.environ.get("KAIROS_MAP_HOST") or "").strip().lower()


def available():
    """True when the map can be hosted in WebView2 in this process."""
    global _availability
    if requested_backend() == "qtwebengine":
        return False
    if _availability is None:
        app = QApplication.instance()
        platform = app.platformName() if app is not None else ""
        _availability = bool(sys.platform == "win32" and platform == "windows" and runtime_version())
    return _availability


def file_url(path):
    """Percent-encoded file URL (WebView2 reports URLs encoded: "Program%20Files")."""
    return encoded_url(QUrl.fromLocalFile(os.path.abspath(path)))


def encoded_url(url):
    u = url if isinstance(url, QUrl) else QUrl(str(url))
    return bytes(u.toEncoded()).decode("ascii")


def same_page(a, b):
    """Same document, ignoring the fragment and how the URL is encoded / cased (file paths)."""
    qa, qb = QUrl(str(a or "")), QUrl(str(b or ""))
    if qa.isLocalFile() and qb.isLocalFile():
        return (os.path.normcase(os.path.normpath(qa.toLocalFile()))
                == os.path.normcase(os.path.normpath(qb.toLocalFile())))
    strip = QUrl.UrlFormattingOption.RemoveFragment
    return qa.adjusted(strip) == qb.adjusted(strip)


# ── keyboard: accelerator keys back to Qt ────────────────────────────────────

_VK_NAMED = {0x08: Qt.Key.Key_Backspace, 0x09: Qt.Key.Key_Tab, 0x0D: Qt.Key.Key_Return,
             0x1B: Qt.Key.Key_Escape, 0x20: Qt.Key.Key_Space, 0x21: Qt.Key.Key_PageUp,
             0x22: Qt.Key.Key_PageDown, 0x23: Qt.Key.Key_End, 0x24: Qt.Key.Key_Home,
             0x25: Qt.Key.Key_Left, 0x26: Qt.Key.Key_Up, 0x27: Qt.Key.Key_Right, 0x28: Qt.Key.Key_Down,
             0x2E: Qt.Key.Key_Delete, 0xBB: Qt.Key.Key_Plus, 0xBD: Qt.Key.Key_Minus,
             0xBC: Qt.Key.Key_Comma, 0xBE: Qt.Key.Key_Period, 0xA6: Qt.Key.Key_Back}


def vk_to_qt_key(vk):
    vk = int(vk)
    if 0x41 <= vk <= 0x5A:
        return Qt.Key(int(Qt.Key.Key_A.value) + vk - 0x41)
    if 0x30 <= vk <= 0x39:
        return Qt.Key(int(Qt.Key.Key_0.value) + vk - 0x30)
    if 0x70 <= vk <= 0x87:
        return Qt.Key(int(Qt.Key.Key_F1.value) + vk - 0x70)
    return _VK_NAMED.get(vk)


def _modifiers_now():
    import ctypes
    state = ctypes.windll.user32.GetKeyState
    mods = Qt.KeyboardModifier.NoModifier
    if state(0x11) & 0x8000:
        mods |= Qt.KeyboardModifier.ControlModifier
    if state(0x10) & 0x8000:
        mods |= Qt.KeyboardModifier.ShiftModifier
    if state(0x12) & 0x8000:
        mods |= Qt.KeyboardModifier.AltModifier
    return mods


def key_sequence(key, mods):
    return QKeySequence(QKeyCombination(mods, key)) if key is not None else QKeySequence()


def find_shortcut(window, seq):
    """QShortcut / QAction of `window` bound to `seq` (enabled), or None. Plain keys without a
    modifier are left to the page (Esc ends a Journey there) – except function keys."""
    if window is None or seq.isEmpty():
        return None
    for sc in window.findChildren(QShortcut):
        try:
            if sc.isEnabled() and any(k.matches(seq) == QKeySequence.SequenceMatch.ExactMatch for k in sc.keys()):
                return sc
        except RuntimeError:
            continue
    for act in window.findChildren(QAction):
        try:
            if act.isEnabled() and any(k.matches(seq) == QKeySequence.SequenceMatch.ExactMatch
                                       for k in act.shortcuts()):
                return act
        except RuntimeError:
            continue
    return None


def is_qt_accelerator(key, mods):
    """Which keys the page may NOT keep: anything with Ctrl/Alt, and F-keys."""
    if key is None:
        return False
    if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier):
        return True
    return int(Qt.Key.Key_F1.value) <= int(key.value) <= int(Qt.Key.Key_F24.value) or key == Qt.Key.Key_Back


# ── page() shim ──────────────────────────────────────────────────────────────

class _PageShim:
    """What main.py calls on view.page(): runJavaScript and setBackgroundColor."""

    def __init__(self, host):
        self._host = host

    def runJavaScript(self, code, *args):
        callback = next((a for a in args if callable(a)), None)
        self._host.run_js(code, callback)

    def setBackgroundColor(self, color):
        self._host.set_background(color)


class WebMessageBridge(QObject):
    """chrome.webview messages -> slots of the bridge object (MapBridge). Only whitelisted names."""

    def __init__(self, target, names, parent=None):
        super().__init__(parent)
        self.target = target
        self.names = frozenset(names)
        self.dispatched = 0

    def dispatch(self, text):
        try:
            msg = json.loads(text)
            name, args = msg.get("m"), msg.get("a") or []
        except (TypeError, ValueError, AttributeError):
            return False
        if name not in self.names or not isinstance(args, list):
            return False
        fn = getattr(self.target, name, None)
        if fn is None:
            return False
        try:
            fn(*args)
        except Exception as e:                   # a bad call from the page must not kill the app
            print(f"[Map] bridge {name}: {e}")
            return False
        self.dispatched += 1
        return True


def bridge_slot_names(cls):
    """Public methods of the bridge class decorated with pyqtSlot (their __pyqtSignature__)."""
    out = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        fn = getattr(cls, name, None)
        if callable(fn) and hasattr(fn, "__pyqtSignature__"):
            out.append(name)
    return sorted(out)


# ── the host widget ──────────────────────────────────────────────────────────

class WebView2Host(QWidget):
    """A QWebEngineView-shaped widget backed by a WebView2 controller (see module docstring)."""
    loadStarted = pyqtSignal()
    loadFinished = pyqtSignal(bool)
    message = pyqtSignal(str)
    filesDropped = pyqtSignal(list)
    ready = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, parent=None, user_data_dir=None, tag="Mapa", debug=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(1, 1)
        self.tag = tag
        self._debug = os.environ.get("KAIROS_JS_DEBUG") == "1" if debug is None else bool(debug)
        self._udf = user_data_dir or os.path.join(tempfile.gettempdir(), "kairos_wv2")
        self._bg = QColor("#1C1C1E")
        self._page = _PageShim(self)
        self._env = self._ctl = self._core = None
        self._url = None
        self._map_url = None
        self._init_scripts = []
        self._script_tasks = []
        self._queued_js = []
        self._pending = []                       # [(task, on_done)]
        self._closed = False
        self._frozen = False
        self._snapshot = None
        self._snap_seq = 0
        self._handlers = []                      # keep .NET delegates alive
        self._wv_focus = False                   # Win32 keyboard focus is inside WebView2
        self.console_printed = 0
        self.console_suppressed = 0
        self._seen = {}
        self.last_error = None
        self.created_at = None
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_tasks)
        self._airspace = AirspaceGuard(self)
        self.winId()                             # create the HWND now – WebView2 needs a parent
        QTimer.singleShot(0, self._create)

    # ── QWebEngineView-like API ──
    def page(self):
        return self._page

    def load(self, url):
        self._url = encoded_url(url) if isinstance(url, QUrl) else str(url)
        self._map_url = self._url
        if self._core is not None and not self._script_tasks:
            self._navigate()

    def url(self):
        return QUrl(self._url or "")

    def add_init_script(self, code):
        """Runs before any page script on every document (like QWebEngineScript DocumentCreation)."""
        self._init_scripts.append(code)
        if self._core is not None:
            self._add_script(code)

    def set_background(self, color):
        self._bg = QColor(color)
        if self._ctl is not None:
            try:
                C = _load_clr()["Color"]
                self._ctl.DefaultBackgroundColor = C.FromArgb(255, self._bg.red(), self._bg.green(), self._bg.blue())
            except Exception:
                pass
        self.update()

    def is_ready(self):
        return self._core is not None

    def run_js(self, code, callback=None):
        if self._closed:
            return
        if self._core is None:
            self._queued_js.append((code, callback))
            return
        try:
            task = self._core.ExecuteScriptAsync(code)
        except Exception as e:
            print(f"[{self.tag}] ExecuteScript: {e}")
            return
        if callback is not None:
            def done(t, cb=callback):
                value = None
                try:
                    raw = t.Result
                    value = json.loads(raw) if raw else None
                except Exception:
                    value = None
                cb(value)
            self._watch(task, done)

    def capture_jpeg(self, path, callback):
        """Still frame of the live view into `path` (JPEG); callback(ok)."""
        if self._core is None:
            callback(False)
            return
        try:
            ns = _load_clr()
            fs = ns["System"].IO.FileStream(path, ns["System"].IO.FileMode.Create)
            fmt = getattr(ns["Core"].CoreWebView2CapturePreviewImageFormat, SNAPSHOT_QUALITY_FORMAT)
            task = self._core.CapturePreviewAsync(fmt, fs)
        except Exception as e:
            print(f"[{self.tag}] capture: {e}")
            callback(False)
            return

        def done(t):
            try:
                fs.Close()
            except Exception:
                pass
            callback(not t.IsFaulted)
        self._watch(task, done)

    # ── creation ──
    def _create(self):
        if self._closed:
            return
        try:
            ns = _load_clr()
            Core = ns["Core"]
            os.makedirs(self._udf, exist_ok=True)
            opts = Core.CoreWebView2EnvironmentOptions()
            opts.AdditionalBrowserArguments = f"{BROWSER_ARGS} --disk-cache-size={DISK_CACHE_MB * 1024 * 1024}"
            try:
                import i18n
                opts.Language = i18n.current()
            except Exception:
                pass
            self.created_at = time.perf_counter()
            task = Core.CoreWebView2Environment.CreateAsync(None, self._udf, opts)
        except Exception as e:
            self._fail(f"environment: {e}")
            return
        self._watch(task, self._on_env)

    def _on_env(self, task):
        if self._closed:
            return
        if task.IsFaulted:
            self._fail(f"environment: {task.Exception}")
            return
        self._env = task.Result
        try:
            ns = _load_clr()
            ctask = self._env.CreateCoreWebView2ControllerAsync(ns["IntPtr"](int(self.winId())))
        except Exception as e:
            self._fail(f"controller: {e}")
            return
        self._watch(ctask, self._on_controller)

    def _on_controller(self, task):
        if self._closed:
            try:
                task.Result.Close()
            except Exception:
                pass
            return
        if task.IsFaulted:
            self._fail(f"controller: {task.Exception}")
            return
        self._ctl = task.Result
        self._core = self._ctl.CoreWebView2
        ns = _load_clr()
        s = self._core.Settings
        s.AreDefaultContextMenusEnabled = False
        s.AreDevToolsEnabled = self._debug
        s.AreBrowserAcceleratorKeysEnabled = False
        s.IsStatusBarEnabled = False
        s.IsZoomControlEnabled = False
        s.IsPinchZoomEnabled = False             # MapLibre does its own pinch zoom
        s.IsSwipeNavigationEnabled = False
        s.IsGeneralAutofillEnabled = False
        s.IsPasswordAutosaveEnabled = False
        self._ctl.AllowExternalDrop = True       # the page forwards dropped files (dropFiles)
        self.set_background(self._bg)
        self._hook("NavigationStarting", self._core, self._on_nav_starting)
        self._hook("NavigationCompleted", self._core, self._on_nav_completed)
        self._hook("NewWindowRequested", self._core, self._on_new_window)
        self._hook("WebMessageReceived", self._core, self._on_message)
        self._hook("ProcessFailed", self._core, self._on_process_failed)
        self._hook("AcceleratorKeyPressed", self._ctl, self._on_accelerator)
        self._hook("GotFocus", self._ctl, self._on_wv_got_focus)
        self._hook("LostFocus", self._ctl, self._on_wv_lost_focus)
        self._hook_console()
        self._sync_bounds()
        self._sync_visible()
        for code in self._init_scripts:
            self._add_script(code)
        if self.created_at is not None:
            print(f"[{self.tag}] WebView2 {runtime_version()} ready in "
                  f"{(time.perf_counter() - self.created_at) * 1000:.0f} ms")
        self.ready.emit()
        queued, self._queued_js = self._queued_js, []
        for code, cb in queued:
            self.run_js(code, cb)
        if self._url and not self._script_tasks:
            self._navigate()

    def _add_script(self, code):
        try:
            task = self._core.AddScriptToExecuteOnDocumentCreatedAsync(code)
        except Exception as e:
            print(f"[{self.tag}] init script: {e}")
            return
        self._script_tasks.append(task)

        def done(t):
            if t in self._script_tasks:
                self._script_tasks.remove(t)
            if not self._script_tasks and self._url and self._core is not None and not getattr(self, "_navigated", False):
                self._navigate()
        self._watch(task, done)

    def _navigate(self):
        self._navigated = True
        try:
            self._core.Navigate(self._url)
        except Exception as e:
            print(f"[{self.tag}] navigate: {e}")

    def _hook(self, event, obj, fn):
        def handler(sender, args, fn=fn):
            try:
                fn(args)
            except Exception as e:               # never let a Python error unwind into .NET
                print(f"[{self.tag}] {event}: {e}")
        ev = getattr(obj, event)
        ev += handler
        self._handlers.append((obj, event, handler))

    def _fail(self, why):
        self.last_error = why
        print(f"[{self.tag}] WebView2 unavailable: {why}")
        self.failed.emit(why)

    # ── task polling ──
    def _watch(self, task, on_done):
        self._pending.append((task, on_done))
        if not self._poll.isActive():
            self._poll.start()

    def _poll_tasks(self):
        if not self._pending:
            self._poll.stop()
            return
        still = []
        done = []
        for task, cb in self._pending:
            try:
                finished = task.IsCompleted
            except Exception:
                finished = True
            (done if finished else still).append((task, cb))
        self._pending = still
        for task, cb in done:
            try:
                cb(task)
            except Exception as e:
                print(f"[{self.tag}] task callback: {e}")
        if not self._pending:
            self._poll.stop()

    # ── events from WebView2 ──
    def _on_nav_starting(self, args):
        uri = str(args.Uri or "")
        if self._map_url and not same_page(uri, self._map_url) and not uri.startswith("about:"):
            args.Cancel = True                   # links in the page never replace the map
            return
        self.loadStarted.emit()

    def _on_nav_completed(self, args):
        ok = bool(args.IsSuccess)
        if not ok:
            print(f"[{self.tag}] navigation failed: {args.WebErrorStatus}")
        self.loadFinished.emit(ok)

    def _on_new_window(self, args):
        args.Handled = True                      # no popups (attribution links etc.)

    def _on_message(self, args):
        try:
            text = args.TryGetWebMessageAsString()
        except Exception:
            text = None
        if text == "kairos-drop":
            paths = []
            try:
                objs = args.AdditionalObjects
                for i in range(int(objs.Count)):
                    p = getattr(objs[i], "Path", None)
                    if p:
                        paths.append(str(p))
            except Exception as e:
                print(f"[{self.tag}] drop: {e}")
            if paths:
                self.filesDropped.emit(paths)
            return
        if text:
            self.message.emit(text)

    def _on_process_failed(self, args):
        kind = str(args.ProcessFailedKind)
        print(f"[{self.tag}] WebView2 process failed: {kind}")
        if "BrowserProcessExited" in kind or "RenderProcess" in kind:
            QTimer.singleShot(500, self._reload)

    def _reload(self):
        if self._core is not None and self._url:
            try:
                self._core.Reload()
            except Exception:
                pass

    def _on_accelerator(self, args):
        kind = str(args.KeyEventKind)
        if "KeyDown" not in kind:
            return
        key = vk_to_qt_key(args.VirtualKey)
        mods = _modifiers_now()
        if not is_qt_accelerator(key, mods):
            return
        target = find_shortcut(self.window(), key_sequence(key, mods))
        if target is None:
            return
        args.Handled = True
        if isinstance(target, QShortcut):
            QTimer.singleShot(0, target.activated.emit)
        else:
            QTimer.singleShot(0, target.trigger)

    def _hook_console(self):
        """Console + uncaught errors via DevTools protocol, filtered like web_console.QuietWebPage."""
        try:
            for ev, fn in (("Runtime.consoleAPICalled", self._on_console),
                           ("Runtime.exceptionThrown", self._on_exception)):
                recv = self._core.GetDevToolsProtocolEventReceiver(ev)
                self._hook("DevToolsProtocolEventReceived", recv, fn)
            self._core.CallDevToolsProtocolMethodAsync("Runtime.enable", "{}")
        except Exception as e:
            print(f"[{self.tag}] console hook: {e}")

    def _console(self, level, text):
        try:
            from web_console import is_noise, MAX_REPEATS
        except Exception:
            is_noise, MAX_REPEATS = (lambda m: False), 3
        if is_noise(text) or (level in ("log", "info", "debug") and not self._debug):
            self.console_suppressed += 1
            return
        key = (level, text[:200])
        n = self._seen.get(key, 0) + 1
        self._seen[key] = n
        if n > MAX_REPEATS + 1:
            self.console_suppressed += 1
            return
        msg = text if n <= MAX_REPEATS else f"web console: {text[:120]} … (further repeats suppressed)"
        print(f"[{self.tag} {level}] {msg}", file=sys.stderr)
        self.console_printed += 1

    def _on_console(self, args):
        data = json.loads(str(args.ParameterObjectAsJson))
        level = data.get("type", "log")
        level = "warning" if level == "warning" else ("error" if level in ("error", "assert") else level)
        text = " ".join(str(a.get("value", a.get("description", ""))) for a in data.get("args", []))
        self._console(level, text)

    def _on_exception(self, args):
        data = json.loads(str(args.ParameterObjectAsJson)).get("exceptionDetails", {})
        exc = data.get("exception", {})
        self._console("error", str(exc.get("description") or data.get("text") or "uncaught exception"))

    # ── geometry / visibility / painting ──
    def _sync_bounds(self):
        if self._ctl is None:
            return
        dpr = self.devicePixelRatioF()
        R = _load_clr()["Rectangle"]
        try:
            self._ctl.Bounds = R(0, 0, max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        except Exception as e:
            print(f"[{self.tag}] bounds: {e}")

    def _sync_visible(self):
        if self._ctl is not None:
            try:
                win = self.window()
                self._ctl.IsVisible = bool(self.isVisible() and not self._frozen
                                           and not (win is not None and win.isMinimized()))
            except Exception:
                pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_bounds()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_bounds()
        self._sync_visible()
        self._airspace.attach()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._sync_visible()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == getattr(QEvent.Type, "DevicePixelRatioChange", None):
            self._sync_bounds()

    def window_moved(self):
        if self._ctl is not None:
            try:
                self._ctl.NotifyParentWindowPositionChanged()
            except Exception:
                pass

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._ctl is not None and not self._wv_focus:      # Tab / setFocus() into the map
            try:
                self._ctl.MoveFocus(_load_clr()["Core"].CoreWebView2MoveFocusReason.Programmatic)
            except Exception:
                pass

    def focusOutEvent(self, event):
        """Qt focus moved to another widget (a click on the search field…): WebView2 still holds
        the Win32 keyboard focus, and Qt never asks for it back between its own alien widgets."""
        super().focusOutEvent(event)
        if self._wv_focus:
            self._wv_focus = False
            reclaim_keyboard_focus(self.window())

    def _on_wv_got_focus(self, _args):
        """A click in the map: Qt's focus follows (otherwise the old focus widget – e.g. the search
        field – keeps it, and clicking it again would not move keyboard focus back)."""
        self._wv_focus = True
        if not self.hasFocus():
            self.setFocus(Qt.FocusReason.MouseFocusReason)

    def _on_wv_lost_focus(self, _args):
        self._wv_focus = False

    def paintEvent(self, _):
        p = QPainter(self)
        if self._frozen and self._snapshot is not None:
            p.drawImage(self.rect(), self._snapshot)
        else:
            p.fillRect(self.rect(), self._bg)
        p.end()

    # ── airspace freeze ──
    def is_frozen(self):
        return self._frozen

    def freeze(self, on):
        """Still frame instead of the live view while a modal Qt overlay covers the map."""
        on = bool(on)
        if on == self._frozen:
            return
        self._frozen = on
        self._snap_seq += 1
        if not on:
            self._snapshot = None
            self._sync_visible()
            self.update()
            return
        if self._core is None or not self.isVisible():
            self._sync_visible()
            return
        seq = self._snap_seq
        path = os.path.join(tempfile.gettempdir(), f"kairos_map_still_{os.getpid()}.jpg")

        def done(ok):
            if seq != self._snap_seq or not self._frozen:
                return
            if ok:
                img = QImage(path)
                if not img.isNull():
                    img.setDevicePixelRatio(self.devicePixelRatioF())
                    self._snapshot = img
            try:
                os.remove(path)
            except OSError:
                pass
            self.update()
            self._sync_visible()                 # hide the live view only once the still is up
        self.capture_jpeg(path, done)

    # ── teardown ──
    def close_webview(self):
        if self._closed:
            return
        self._closed = True
        self._poll.stop()
        self._airspace.detach()
        for obj, event, handler in self._handlers:
            try:
                ev = getattr(obj, event)
                ev -= handler
            except Exception:
                pass
        self._handlers.clear()
        if self._ctl is not None:
            try:
                self._ctl.Close()
            except Exception:
                pass
        self._ctl = self._core = self._env = None

    def closeEvent(self, event):
        self.close_webview()
        super().closeEvent(event)

    def __del__(self):
        try:
            self.close_webview()
        except Exception:
            pass


# ── airspace ─────────────────────────────────────────────────────────────────

AIRSPACE_PROP = "kairos_airspace"     # "native" | "freeze" | "ignore" on an overlay widget


class AirspaceGuard(QObject):
    """Watches the window for Qt overlays that would end up under the map's HWND."""

    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.freezers = set()
        self._attached = False
        self._window = None

    def attach(self):
        if self._attached:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        self._window = self.host.window()
        self._attached = True

    def detach(self):
        if self._attached:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
        self._attached = False
        self.freezers.clear()

    def classify(self, w):
        """None (not an overlay of the map) / "native" / "freeze"."""
        host = self.host
        try:
            if not isinstance(w, QWidget) or w is host or w.isWindow() or w.window() is not host.window():
                return None
            parent = w.parentWidget()
            if parent is None or host.isAncestorOf(w) or w.isAncestorOf(host) or not parent.isAncestorOf(host):
                return None
            lay = parent.layout()
            if lay is not None and lay.indexOf(w) >= 0:
                return None                     # a laid-out sibling sits beside the map, not over it
            mode = w.property(AIRSPACE_PROP)
            if mode == "ignore":
                return None
            if mode not in ("native", "freeze"):
                if w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents):
                    return None                 # decorative pulses / flashes: harmless if hidden
                mode = "freeze"
            return mode
        except RuntimeError:
            return None

    def _overlaps(self, w):
        host = self.host
        try:
            win = host.window()
            a = QRect(host.mapTo(win, host.rect().topLeft()), host.size())
            b = QRect(w.mapTo(win, w.rect().topLeft()), w.size())
            return a.intersects(b)
        except RuntimeError:
            return False

    def eventFilter(self, obj, event):
        try:
            t = event.type()
            if t not in (QEvent.Type.Show, QEvent.Type.Hide):
                if obj is self._window and t == QEvent.Type.Move:
                    self.host.window_moved()
                elif obj is self._window and t == QEvent.Type.WindowStateChange:
                    self.host._sync_visible()          # minimized: WebView2 stops rendering
                elif (t == QEvent.Type.Resize and isinstance(obj, QWidget)
                      and obj.property(AIRSPACE_PROP) == "native" and obj.property("kairos_airspace_radius")
                      and obj.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)):
                    apply_round_mask(obj, float(obj.property("kairos_airspace_radius")))
                return False
            mode = self.classify(obj)
            if mode is None:
                return False
            if t == QEvent.Type.Show:
                if mode == "native":
                    make_native_overlay(obj)
                elif self.host.isVisible() and self._overlaps(obj):
                    self.freezers.add(id(obj))
                    obj.destroyed.connect(lambda *_a, k=id(obj): self._release(k))
                    self.host.freeze(True)
            else:
                self._release(id(obj))
        except Exception as e:
            print(f"[Map] airspace: {e}")
        return False

    def _release(self, key):
        if key in self.freezers:
            self.freezers.discard(key)
            if not self.freezers:
                try:
                    self.host.freeze(False)
                except RuntimeError:
                    pass


def reclaim_keyboard_focus(window):
    """Win32 keyboard focus back to the Qt top-level (from the WebView2 child HWND)."""
    if window is None or sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.user32.SetFocus(int(window.winId()))
    except Exception:
        pass


def make_native_overlay(w):
    """Own HWND above the map (raised), clipped to its rounded shape when it has one."""
    if not w.testAttribute(Qt.WidgetAttribute.WA_NativeWindow):
        w.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        w.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        w.winId()
    w.raise_()
    radius = w.property("kairos_airspace_radius")
    if radius:
        apply_round_mask(w, float(radius))


def apply_round_mask(w, radius):
    path = QPainterPath()
    path.addRoundedRect(0, 0, w.width(), w.height(), radius, radius)
    w.setMask(QRegion(path.toFillPolygon().toPolygon()))
