"""UI captures (map tab + every Settings page). Run: python eval/capture_ui.py. Captures via QWidget.grab() only (+ WebView2's own CapturePreview for the map surface). No desktop capture."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); OUT = os.path.join(os.path.dirname(ROOT), "kairos_captures")
os.makedirs(OUT, exist_ok=True); sys.path.insert(0, ROOT); os.chdir(ROOT)
os.environ.setdefault("KAIROS_NO_SPLASH", "1")
import main as M
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
M.web_engine.prepare_app()
app = QApplication(sys.argv)

def win():
    for w in app.topLevelWidgets():
        if w.__class__.__name__ == "MainWindow": return w
def later(ms, fn): QTimer.singleShot(ms, fn)

def step_map():
    w = win()
    if w is None: return later(300, step_map)
    w._ensure_map_view(); w._switch_page(3)
    def wait():
        if not getattr(w, "_map_loaded", False): return later(300, wait)
        later(4000, snap)
    def snap():
        w.grab().save(os.path.join(OUT, "window_map_tab_grab.png"))
        if not hasattr(w.map_view, "capture_jpeg"):      # QtWebEngine fallback host: no surface capture
            print("map capture skipped (no WebView2 host)"); return later(200, step_settings)
        w.map_view.capture_jpeg(os.path.join(OUT, "map_surface.jpg"), lambda ok: print("map capture", ok) or later(200, step_settings))
    later(800, wait)

def step_settings():
    """Every Settings page (category rail) in the active theme + a search state; QWidget.grab() only."""
    from settings_dialog import SettingsDialog
    dlg = SettingsDialog()
    dlg.resize(1000, 720)
    dlg.show()
    jobs = [(i, None, "dark") for i in range(dlg.pages.count())] + [(None, "mapa", "dark")]         + [(i, None, "light") for i in (0, 4, 6)]

    def shot(n):
        if n >= len(jobs):
            dlg.close(); print("DONE"); return app.quit()
        i, query, mode = jobs[n]
        if mode != M.theme.current():
            M.theme.apply(mode)
            dlg.hide(); dlg.show()
        dlg.search.setText(query or "")
        if i is not None:
            dlg.pages.setCurrentIndex(i)            # a search jumps to the first page with hits by itself
        app.processEvents()
        name = f"settings_{dlg.PAGE_KEYS[dlg.pages.currentIndex()]}" + ("_search" if query else "")             + ("_light" if mode == "light" else "") + ".png"
        later(350, lambda: (dlg.grab().save(os.path.join(OUT, name)), shot(n + 1)))
    later(300, lambda: shot(0))

QTimer.singleShot(2500, step_map)
M.main(app)
