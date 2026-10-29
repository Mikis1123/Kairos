"""Sledované složky (Polish P3): lehké hlídání změn s odloženým přeskenováním.

QFileSystemWatcher na Windows není rekurzivní, proto hlídá strom sledované složky do hloubky
MAX_DEPTH (po úrovních, mělké složky mají přednost) – nejvýš MAX_PATHS cest celkem. Album
typu Fotky/2026/Léto/Chorvatsko/Den1 se tak zachytí hned. Každá změna jen znovu spustí
DEBOUNCE_MS časovač; až se složka uklidní, obnoví se hlídané podsložky (nové přibudou,
smazané odpadnou) a vyšle se changed() s kořeny ke skenování (skener je inkrementální –
známé soubory přeskočí). Pojistka pro to, co watcher propásl (spánek, síťový disk): každý
návrat do okna porovná mtime hlídaných složek se známým stavem. Žádné vlákno ani tikání.
"""
import os
import time
from collections import deque

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal

MAX_PATHS = 512          # strop handle/vláken QFileSystemWatcher (Windows: vlákno na 63 cest)
MAX_DEPTH = 7            # kořen = 0; Fotky/2026/Léto/Chorvatsko/Den1 = 4
DEBOUNCE_MS = 3000
ACTIVATION_MIN_S = 2.0   # rychlé střídání aktivace (dialogy) = jedna kontrola


def _key(path):
    return os.path.normcase(os.path.normpath(path))


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def watch_paths(root, budget, max_depth=None):
    """Kořen + podsložky do hloubky max_depth (bez skrytých / systémových), po úrovních,
    nejvýš budget cest."""
    if max_depth is None:
        max_depth = MAX_DEPTH
    if budget <= 0 or not os.path.isdir(root):
        return []
    out = [os.path.normpath(root)]
    queue = deque([(out[0], 0)])
    while queue and len(out) < budget:
        path, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            with os.scandir(path) as it:
                subdirs = sorted(e.path for e in it
                                 if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")))
        except OSError:
            continue
        for s in subdirs[:budget - len(out)]:
            s = os.path.normpath(s)
            out.append(s)
            queue.append((s, depth + 1))
    return out


class FolderWatcher(QObject):
    changed = pyqtSignal(list)           # kořeny sledovaných složek ke skenování

    def __init__(self, parent=None, debounce_ms=DEBOUNCE_MS):
        super().__init__(parent)
        self._fs = QFileSystemWatcher(self)
        self._fs.directoryChanged.connect(self._on_change)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._flush)
        self._roots = []
        self._owner = {}                 # klíč hlídané cesty -> kořen
        self._known = {}                 # klíč hlídané cesty -> (cesta, mtime_ns při posledním pohledu)
        self._dirty = set()
        self._last_check = None

    def roots(self):
        return list(self._roots)

    def watched(self):
        return self._fs.directories()

    def set_roots(self, roots):
        roots = [os.path.normpath(r) for r in dict.fromkeys(roots or []) if r and os.path.isdir(r)]
        old = self._fs.directories()
        if old:
            self._fs.removePaths(old)
        self._roots, self._owner, self._known = roots, {}, {}
        self._dirty &= set(roots)
        for root in roots:
            self._rewatch(root)

    def _rewatch(self, root):
        """Hlídané cesty kořene podle aktuálního stromu; nezměněné se nepřidávají znovu
        (odebrání a přidání by mohlo propásnout změnu)."""
        current = {k for k, r in self._owner.items() if r == root}
        paths = watch_paths(root, MAX_PATHS - (len(self._owner) - len(current)))
        fresh = {_key(p): p for p in paths}
        gone = [self._known[k][0] for k in current - fresh.keys()]
        if gone:
            self._fs.removePaths(gone)
        for k in current - fresh.keys():
            del self._owner[k]
            del self._known[k]
        added = [p for k, p in fresh.items() if k not in current]
        if added:
            self._fs.addPaths(added)
        for p in added:
            self._owner[_key(p)] = root
            self._known[_key(p)] = (p, _mtime(p))

    def _on_change(self, path):
        k = _key(path)
        root = self._owner.get(k)
        if root is None:
            return
        self._known[k] = (self._known[k][0], _mtime(path))
        self._dirty.add(root)
        self._timer.start()              # debounce: každá další změna odloží sken

    def window_activated(self):
        """Návrat do okna: hned levně porovnat mtime hlídaných složek (os.stat, žádné procházení
        stromu) – zachytí změny, které watcher propásl. Nic se nezměnilo = nic se neskenuje."""
        now = time.monotonic()
        if not self._roots or (self._last_check is not None and now - self._last_check < ACTIVATION_MIN_S):
            return
        self._last_check = now
        for k, (path, seen) in list(self._known.items()):
            root = self._owner[k]
            if root in self._dirty:
                continue
            mtime = _mtime(path)
            if mtime != seen and os.path.isdir(root):   # odpojený disk se neskenuje
                self._known[k] = (path, mtime)
                self._dirty.add(root)
        if self._dirty and not self._timer.isActive():
            self._flush()                # okno je zpět: skenovat hned, ne až po debounce

    def pending(self):
        return sorted(self._dirty)

    def _flush(self):
        self._timer.stop()
        roots = [r for r in self._roots if r in self._dirty]
        self._dirty.clear()
        for root in roots:
            self._rewatch(root)          # nové podsložky začít hlídat, smazané pustit
        if roots:
            self.changed.emit(roots)
