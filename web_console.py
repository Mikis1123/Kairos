"""Kairos – tichá konzole vložených webových stránek (mapa, minimapa).

QWebEnginePage ve výchozím stavu vypisuje KAŽDOU zprávu konzole stránky do stderr („js: …“).
WebGL / MapLibre přitom některá varování hlásí za každý vykreslený snímek:
  * „performance warning: READ-usage buffer was written, then fenced, but written again before
    being read back“ – Chromium o měření chyby projekce zeměkoule v MapLibre (PBO + fence),
    neškodná nápověda k výkonu, kód je ve vendor knihovně ověřené SHA,
  * „WebGL: too many errors, no more errors will be reported to the console for this context.“,
  * „calculateFogMatrix is not supported on globe projection.“ / „terrain is not fully supported
    on vertical perspective projection.“ – mapa je ošetřená (terén jen v rovině), zbytky během
    přechodu nejsou chyby.
Takové zprávy se zahodí úplně; ostatní varování a chyby se vypíšou nejvýš MAX_REPEATS× každá
(pak jednou souhrn), informační zprávy jen s KAIROS_JS_DEBUG=1. Nativní logy GPU procesu
Chromia tlumí --log-level=3 v QTWEBENGINE_CHROMIUM_FLAGS (main.py).
"""
import os
import sys

from PyQt6.QtWebEngineCore import QWebEnginePage

NOISE = (
    "READ-usage buffer",
    "GPU stall due to ReadPixels",
    "WebGL: too many errors",
    "WebGL: INVALID_",
    "GL_INVALID_",
    "[.WebGL-",
    "calculateFogMatrix is not supported",
    "terrain is not fully supported",
    "Expected value to be of type number, but found null",
)
MAX_REPEATS = 3
_LEVEL_NAMES = {
    QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "info",
    QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "warning",
    QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "error",
}


def is_noise(message):
    msg = str(message or "")
    return any(n in msg for n in NOISE)


class QuietWebPage(QWebEnginePage):
    """QWebEnginePage, která nezaplaví terminál opakovanými hlášeními WebGL / MapLibre."""

    def __init__(self, *args, tag="Web", stream=None):
        super().__init__(*args)
        self.tag = tag
        self.stream = stream               # None = sys.stderr v okamžiku zápisu (testy ho podvrhnou)
        self.suppressed = 0                # zahozené zprávy (šum + opakování) – ladění / testy
        self.printed = 0
        self._seen = {}
        self._debug = os.environ.get("KAIROS_JS_DEBUG") == "1"

    def javaScriptConsoleMessage(self, level, message, line, source):
        if is_noise(message):
            self.suppressed += 1
            return
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel and not self._debug:
            self.suppressed += 1
            return
        key = (int(level.value) if hasattr(level, "value") else int(level), str(message)[:200])
        n = self._seen.get(key, 0) + 1
        self._seen[key] = n
        if n > MAX_REPEATS + 1:
            self.suppressed += 1
            return
        out = self.stream or sys.stderr
        name = _LEVEL_NAMES.get(level, "log")
        where = f" ({os.path.basename(str(source or ''))}:{line})" if source else ""
        text = str(message) if n <= MAX_REPEATS else f"{str(message)[:120]} … (další opakování se nevypisují)"
        try:
            out.write(f"[{self.tag} {name}] {text}{where}\n")
        except Exception:
            pass
        self.printed += 1
