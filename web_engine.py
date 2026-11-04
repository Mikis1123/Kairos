"""Lazy QtWebEngine.

Importing PyQt6.QtWebEngineWidgets maps Qt6WebEngineCore.dll (~200 MB) plus its ICU / resource
files. The map runs in WebView2 (map_host.py), so QtWebEngine is only needed for the viewer's
Info-panel minimap and as the map fallback when WebView2 cannot start. Every module therefore
imports QtWebEngine at first use, never at module level.

Qt requires either that import or Qt.AA_ShareOpenGLContexts before the QApplication exists;
prepare_app() sets the attribute, so the import can happen any time later.
"""
import sys


def prepare_app():
    """Call before QApplication(...) is constructed."""
    from PyQt6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)


def loaded():
    """True once anything has imported QtWebEngine in this process."""
    return "PyQt6.QtWebEngineCore" in sys.modules
