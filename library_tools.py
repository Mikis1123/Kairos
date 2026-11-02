"""Údržba knihovny pro Nastavení (Patch 11 / dávka E).

* Vysypání koše: soubory jdou do Koše Windows (obnovitelné), ne do nenávratna. Položky na
  vyjímatelných a síťových discích Koš Windows nemá – ty se ve výchozím stavu PŘESKOČÍ
  (zůstanou v koši aplikace) a trvale se smažou jen po výslovném potvrzení.
* Chytrý import: kolik nových fotek je ve sledovaných složkách (bez importu).
* Obnovení továrního nastavení: jen config.json (+ volitelně mezipaměti). Databáze, osoby
  ani originály se NIKDY nemažou.
"""
import copy
import os
import string
import sys
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

import config as config_module
import paths

DRIVE_FIXED, DRIVE_REMOVABLE, DRIVE_NETWORK, DRIVE_MISSING = "fixed", "removable", "network", "missing"
_LIVE_STILL_EXTS = {".heic", ".jpg", ".jpeg"}


# ── Disky ─────────────────────────────────────────────────────────────────────

def drive_kind(path):
    """'fixed' | 'removable' | 'network' | 'missing' pro umístění souboru."""
    if not path:
        return DRIVE_MISSING
    p = os.path.abspath(path)
    if p.startswith("\\\\") or p.startswith("//"):
        return DRIVE_NETWORK                        # UNC \\server\share
    if sys.platform != "win32":
        return DRIVE_FIXED if os.path.exists(p) else DRIVE_MISSING
    drive = os.path.splitdrive(p)[0]
    if not drive or drive[0].upper() not in string.ascii_uppercase:
        return DRIVE_MISSING
    try:
        import ctypes
        kind = ctypes.windll.kernel32.GetDriveTypeW(f"{drive[0].upper()}:\\")
    except Exception:
        kind = 3
    # 2 = vyjímatelný, 3 = pevný, 4 = síťový, 5 = CD, 6 = RAM disk, 0/1 = neznámý / neexistuje
    if kind in (0, 1):
        return DRIVE_MISSING
    if kind == 4:
        return DRIVE_NETWORK
    if kind in (2, 5):
        return DRIVE_REMOVABLE
    return DRIVE_FIXED


def send_to_recycle_bin(path):
    """Přesune soubor do Koše Windows (obnovitelné). True při úspěchu."""
    try:
        from send2trash import send2trash      # volitelná závislost – když je, použije se
        send2trash(path)
        return True
    except ImportError:
        pass
    except Exception:
        return False
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = os.path.abspath(path) + "\0"           # seznam končí dvojitou nulou
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)


# ── Koš ───────────────────────────────────────────────────────────────────────

def plan_empty_trash(conn):
    """Co se při vysypání stane: {'recycle': [...], 'removable': [...], 'missing': [...]},
    položka = (media_id, [soubory]) – Live Photo nese i své video."""
    plan = {"recycle": [], "removable": [], "missing": []}
    rows = conn.execute("SELECT id, file_path, paired_video_path FROM media_items WHERE is_trashed = 1").fetchall()
    for media_id, path, paired in rows:
        files = [f for f in (path, paired) if f]
        existing = [f for f in files if os.path.exists(f)]
        if not existing:
            plan["missing"].append((media_id, files))
            continue
        kinds = {drive_kind(f) for f in existing}
        bucket = "recycle" if kinds <= {DRIVE_FIXED} else "removable"
        plan[bucket].append((media_id, existing))
    return plan


def plan_summary(plan):
    return {k: len(v) for k, v in plan.items()}


def _drop_cached(path):
    """Náhled 2048 px smazané fotky už nikdo nepotřebuje."""
    try:
        import preview_cache
        p = preview_cache.preview_path(path)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def empty_trash(plan, delete_removable_permanently=False, recycle=None):
    """Vysype koš podle plánu. Soubory na pevných discích -> Koš Windows; na vyjímatelných /
    síťových jen s delete_removable_permanently=True (trvale), jinak zůstanou v koši aplikace.
    Řádek v DB se odstraní jen u položek, jejichž soubory jsou opravdu pryč.
    Vrací {'recycled', 'deleted', 'skipped', 'failed', 'forgotten'}."""
    from database import db_transaction, sync_geo_index
    recycle = recycle or send_to_recycle_bin
    done, stats = [], {"recycled": 0, "deleted": 0, "skipped": 0, "failed": 0, "forgotten": 0}
    for media_id, files in plan.get("recycle", []):
        if all(recycle(f) for f in files):
            stats["recycled"] += 1
            done.append((media_id, files))
        else:
            stats["failed"] += 1
    for media_id, files in plan.get("removable", []):
        if not delete_removable_permanently:
            stats["skipped"] += 1
            continue
        try:
            for f in files:
                if drive_kind(f) == DRIVE_FIXED:
                    if not recycle(f):
                        raise OSError(f)
                else:
                    os.remove(f)
            stats["deleted"] += 1
            done.append((media_id, files))
        except OSError:
            stats["failed"] += 1
    for media_id, files in plan.get("missing", []):
        stats["forgotten"] += 1                 # soubor už neexistuje – jen zapomenout záznam
        done.append((media_id, files))
    if done:
        ids = [(int(m),) for m, _ in done]
        with db_transaction() as conn:
            c = conn.cursor()
            # image_embeddings a face_instances mají ON DELETE CASCADE (foreign_keys = ON)
            c.executemany("DELETE FROM duplicate_dismissed WHERE id_a = ? OR id_b = ?",
                          [(m, m) for (m,) in ids])
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_geo_index'").fetchone():
                c.executemany("DELETE FROM media_geo_index WHERE id = ?", ids)
            c.executemany("DELETE FROM media_items WHERE id = ? AND is_trashed = 1", ids)
        sync_geo_index()
        for _, files in done:
            for f in files:
                _drop_cached(f)
    return stats


# ── Chytrý import ─────────────────────────────────────────────────────────────

def find_new_media(folders, known_keys, should_stop=None):
    """Nové soubory ve sledovaných složkách (cesty, které DB nezná). Video Live Photo se
    stejným názvem jako fotka vedle se nepočítá zvlášť – uživatel vidí „fotky“, ne soubory."""
    from scanner import SUPPORTED_EXTS, _path_key
    import cloud_files
    found = []
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for root, _, files in os.walk(folder):
            if should_stop and should_stop():
                return found
            stills = {Path(f).stem.lower() for f in files if Path(f).suffix.lower() in _LIVE_STILL_EXTS}
            for f in files:
                ext = Path(f).suffix.lower()
                if ext not in SUPPORTED_EXTS:
                    continue
                full = os.path.join(root, f)
                if _path_key(full) in known_keys:
                    continue
                if ext == ".mov" and Path(f).stem.lower() in stills:
                    continue                       # pohybová část Live Photo
                if cloud_files.is_placeholder(full):
                    continue                       # Gate 0: jen v cloudu – nabídne je až sken se souhlasem
                found.append(full)
    return found


def known_media_keys(conn):
    from scanner import _path_key
    return {_path_key(p) for (p,) in conn.execute("SELECT file_path FROM media_items") if p}


class NewMediaProbe(QThread):
    """Na pozadí spočítá nové fotky ve sledovaných složkách (nic neimportuje)."""
    found = pyqtSignal(int, list)            # počet, složky s novinkami

    def __init__(self, folders, parent=None):
        super().__init__(parent)
        self.folders = list(folders or [])
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        try:
            from database import get_connection
            conn = get_connection()
            try:
                known = known_media_keys(conn)
            finally:
                conn.close()
            new = find_new_media(self.folders, known, lambda: not self._running)
        except Exception as e:
            print(f"[SmartImport] {e}")
            return
        if not self._running:
            return
        roots = [f for f in self.folders
                 if any(os.path.normcase(n).startswith(os.path.normcase(os.path.join(f, ""))) for n in new)]
        self.found.emit(len(new), roots)


# ── Tovární nastavení ─────────────────────────────────────────────────────────

def changed_settings(cfg):
    """Klíče, které se od výchozích hodnot liší (náhled dopadu obnovení)."""
    return sorted(k for k, v in config_module.DEFAULT_CONFIG.items() if cfg.get(k, v) != v)


def factory_reset(keep_folders=True, clear_thumbnails=False, clear_previews=False):
    """Vrátí config.json na výchozí hodnoty (+ volitelně smaže mezipaměti, které se samy
    vytvoří znovu). Databáze, osoby, obličeje ani originály se nemění. Vrací nový config."""
    old = config_module.load_config()
    fresh = copy.deepcopy(config_module.DEFAULT_CONFIG)
    if keep_folders:
        fresh["library_folders"] = list(old.get("library_folders", []))
    config_module.save_config(fresh)
    if clear_thumbnails:
        from cache_tools import clear_thumbnail_cache
        clear_thumbnail_cache()
    if clear_previews:
        import preview_cache
        preview_cache.clear_cache()
    return fresh


def protected_paths():
    """Co Factory Reset NIKDY nesmaže (pro testy a pro text v potvrzení)."""
    import database
    return [database.DB_PATH, paths.FACE_DIR]
