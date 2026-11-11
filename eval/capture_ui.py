"""UI captures (map tab + Settings, three tabs). Run: python eval/capture_ui.py. Captures via QWidget.grab() only (+ WebView2's own CapturePreview for the map surface). No desktop capture."""
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
        w.map_view.capture_jpeg(os.path.join(OUT, "map_surface.jpg"), lambda ok: print("map capture", ok) or later(200, step_settings))
    later(800, wait)

def step_settings():
    from settings_dialog import SettingsDialog
    w = win()
    dlg = SettingsDialog()
    dlg.resize(760, 1500)
    dlg.show()
    def shot(i):
        dlg.tabs.setCurrentIndex(i); 
        app.processEvents()
        later(400, lambda: (dlg.grab().save(os.path.join(OUT, f"settings_tab{i}.png")), nxt(i)))
    def nxt(i):
        if i < 2: shot(i + 1)
        else:
            dlg.close(); print("DONE"); app.quit()
    later(300, lambda: shot(0))

QTimer.singleShot(2500, step_map)
M.main(app)
