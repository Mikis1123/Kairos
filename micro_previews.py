"""Micro previews (LQIP) for the grid (Sprint 2, 2026-11-05).

Every photo has a tiny JPEG (long edge MICRO_LONG_EDGE px, ~0.7-1.5 kB). All of them are held in
RAM as compressed bytes (~20-30 MB for 20 000 photos). A grid tile whose real thumbnail is not
decoded yet paints a soft, upscaled micro preview instead of a flat grey rectangle, so a fast
fling never shows empty tiles. The sharp thumbnail replaces it as soon as a decode worker
delivers.

* Storage: one SQLite file in the cache root (micro.sqlite). It is a cache: it can be deleted at
  any time and is rebuilt. It has `secure_delete` on, so rows purged when a photo moves to the
  Hidden folder are overwritten, not just unlinked (Stage 3: no plaintext traces of hidden items).
* Load: a background thread reads every row into RAM at start-up (one SELECT).
* Generation:
  - for free as a by-product: whenever a thumbnail worker decodes a tile and the item has no
    micro preview yet, it shrinks the decoded image (main.ThumbnailWorker);
  - a low-priority backfill thread covers the rest from the on-disk 840 px thumbnails. libjpeg
    decodes them at 1/8 scale in the DCT, about 1 ms each. The thread yields during flings and
    background holds (governor).
  - Quick Edits apply (crop / straighten / tone) and a saved edit invalidates the item.
* Paint: decoded on the GUI thread on demand (~30 us) into a small LRU of QPixmaps.
* Hidden (vault) items never get a micro preview, and hiding purges the existing one.
"""
import os
import sqlite3
import threading
import time
from collections import OrderedDict

MICRO_LONG_EDGE = 48
MICRO_QUALITY = 70
MICRO_VERSION = 1                 # bump = every stored preview is rebuilt
DECODED_CAP = 2500                # decoded QPixmaps kept (48x36x4 B each, ~17 MB at most)
FLUSH_EVERY = 200                 # backfill: rows per SQLite write
CHECKPOINT_EVERY = 48             # backfill: items per governor checkpoint. An item is ~1 ms, and
                                  # the battery throttle pauses 0.25 s per checkpoint: per item,
                                  # that would stretch a 20 000-photo backfill to ~80 minutes
SOURCE_DCT_TARGET = 2 * MICRO_LONG_EDGE   # decode the thumbnail to at least this long edge


def _store_path():
    import paths
    return os.path.join(paths.CACHE_ROOT, "micro.sqlite")


def micro_from_qimage(img):
    """JPEG bytes of the micro preview for a decoded (display-ready) QImage; b'' if empty."""
    from PyQt6.QtCore import QBuffer, QIODevice, Qt
    if img is None or img.isNull():
        return b""
    w, h = img.width(), img.height()
    k = MICRO_LONG_EDGE / max(w, h)
    if k < 1.0:
        img = img.scaled(max(1, round(w * k)), max(1, round(h * k)),
                         Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "JPG", MICRO_QUALITY):
        return b""
    return bytes(buf.data())


def decode_source(path, adjustments=None):
    """A small display image from an on-disk thumbnail (DCT-scaled decode), with the photo's
    Quick Edit applied. A null QImage if unreadable."""
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QImage, QImageReader
    if not path or not os.path.exists(path):
        return QImage()
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    src = reader.size()
    if src.isValid() and max(src.width(), src.height()) > 0:
        long_edge = max(src.width(), src.height())
        div = 1
        for d in (8, 4, 2):                   # libjpeg's exact DCT scales: no Qt resampling
            if long_edge / d >= SOURCE_DCT_TARGET:
                div = d
                break
        if div > 1:
            reader.setScaledSize(QSize(-(-src.width() // div), -(-src.height() // div)))
    img = reader.read()
    if img.isNull() or adjustments is None:
        return img
    try:
        import quick_edit
        return quick_edit.render_qimage(img, adjustments)
    except Exception:
        return img


class MicroStore:
    def __init__(self, path=None):
        self.path = path or _store_path()
        self._lock = threading.Lock()
        self._data = {}                    # item_id -> JPEG bytes
        self._pending = {}                 # item_id -> bytes | None (None = delete), not yet on disk
        self._decoded = OrderedDict()      # item_id -> QPixmap (GUI thread only)
        self._excluded = set()             # vault items: never stored
        self.loaded = threading.Event()
        self.generated = 0                 # stats / tests
        self._backfill = None
        self.pause_until = 0.0             # monotonic: backfill sleeps until then (grid fling)
        self.on_ready = None               # callable() after load, from the loader thread

    # ── load / persist ──
    def _connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS micro (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = conn.execute("SELECT v FROM meta WHERE k = 'version'").fetchone()
        if row is None or row[0] != str(MICRO_VERSION):
            conn.execute("DELETE FROM micro")
            conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('version', ?)", (str(MICRO_VERSION),))
            conn.commit()
        return conn

    def load_async(self):
        threading.Thread(target=self.load, name="micro-load", daemon=True).start()

    def load(self):
        try:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT id, data FROM micro").fetchall()
            finally:
                conn.close()
            with self._lock:
                for item_id, data in rows:
                    if item_id not in self._data and item_id not in self._excluded:
                        self._data[item_id] = bytes(data)
        except Exception as e:
            print(f"[Micro] load failed: {e}")
        self.loaded.set()
        cb = self.on_ready
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def flush(self):
        with self._lock:
            pending, self._pending = self._pending, {}
        if not pending:
            return 0
        try:
            conn = self._connect()
            try:
                puts = [(i, d) for i, d in pending.items() if d is not None]
                dels = [(i,) for i, d in pending.items() if d is None]
                if puts:
                    conn.executemany("INSERT OR REPLACE INTO micro (id, data) VALUES (?, ?)", puts)
                if dels:
                    conn.executemany("DELETE FROM micro WHERE id = ?", dels)
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[Micro] write failed: {e}")
        return len(pending)

    # ── access ──
    def __len__(self):
        with self._lock:
            return len(self._data)

    def has(self, item_id):
        return item_id in self._data           # dict read: atomic under the GIL

    def get_bytes(self, item_id):
        return self._data.get(item_id)

    def nbytes(self):
        with self._lock:
            return sum(len(d) for d in self._data.values())

    def put(self, item_id, data):
        item_id = int(item_id)
        if not data:
            return False
        with self._lock:
            if item_id in self._excluded:
                return False
            self._data[item_id] = data
            self._pending[item_id] = data
        self._decoded.pop(item_id, None)
        return True

    def put_image(self, item_id, img):
        return self.put(item_id, micro_from_qimage(img))

    def invalidate(self, ids):
        """The pixels changed (a Quick Edit saved): drop the previews; the backfill rebuilds them."""
        with self._lock:
            for i in ids:
                i = int(i)
                if self._data.pop(i, None) is not None:
                    self._pending[i] = None
        for i in ids:
            self._decoded.pop(int(i), None)

    def exclude(self, ids):
        """Vault items: never stored while this process runs (on top of the purge)."""
        with self._lock:
            self._excluded.update(int(i) for i in ids)

    def purge(self, ids):
        """Hidden-folder purge: gone from RAM and overwritten on disk now (secure_delete)."""
        ids = [int(i) for i in ids]
        if not ids:
            return
        self.exclude(ids)
        self.invalidate(ids)
        self.flush()

    def pixmap(self, item_id):
        """The decoded micro preview (GUI thread), or None."""
        pm = self._decoded.get(item_id)
        if pm is not None:
            self._decoded.move_to_end(item_id)
            return pm
        data = self._data.get(item_id)
        if not data:
            return None
        from PyQt6.QtGui import QPixmap
        pm = QPixmap()
        if not pm.loadFromData(data, "JPG"):
            return None
        self._decoded[item_id] = pm
        while len(self._decoded) > DECODED_CAP:
            self._decoded.popitem(last=False)
        return pm

    # ── backfill ──
    def request(self, jobs, edit_lookup=None):
        """jobs: (item_id, thumbnail_path) in the order they matter (nearest the viewport first).
        Items that already have a preview are skipped. Runs on one low-priority thread."""
        missing = [(int(i), p) for i, p in jobs if p and int(i) not in self._data and int(i) not in self._excluded]
        if not missing:
            return 0
        bf = self._backfill
        if bf is None or not bf.is_alive():
            bf = self._backfill = _Backfill(self)
            bf.replace(missing, edit_lookup)
            bf.start()
        else:
            bf.replace(missing, edit_lookup)
        return len(missing)

    def backfill_idle(self):
        bf = self._backfill
        return bf is None or not bf.is_alive() or bf.idle

    def stop(self):
        bf = self._backfill
        if bf is not None:
            bf.stop()
        self.flush()


class _Backfill(threading.Thread):
    def __init__(self, store):
        super().__init__(name="micro-backfill", daemon=True)
        self.store = store
        self._cv = threading.Condition()
        self._jobs = []
        self._edit_lookup = None
        self._stop = False
        self.idle = False

    def replace(self, jobs, edit_lookup):
        """The newest request goes first (its order is current); jobs still queued from other
        grids (library, album, search...) stay behind it instead of being forgotten."""
        with self._cv:
            seen = {i for i, _p in jobs}
            self._jobs = list(jobs) + [j for j in self._jobs if j[0] not in seen]
            self._edit_lookup = edit_lookup
            self.idle = False
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stop = True
            self._cv.notify()

    def run(self):
        store = self.store
        store.loaded.wait(30)
        try:
            import governor
            gov = governor.get()
        except Exception:
            gov = None
        since_flush = 0
        since_check = CHECKPOINT_EVERY            # the first item checks in
        while True:
            with self._cv:
                while not self._jobs and not self._stop:
                    if since_flush:
                        break
                    self.idle = True
                    self._cv.wait()
                if self._stop:
                    break
                job = self._jobs.pop(0) if self._jobs else None
                lookup = self._edit_lookup
            if job is None:                    # queue drained: persist what we made
                store.flush()
                since_flush = 0
                continue
            while time.monotonic() < store.pause_until and not self._stop:
                time.sleep(0.05)               # the grid is flinging: its decoders go first
            if gov is not None:
                if since_check >= CHECKPOINT_EVERY:
                    since_check = 0
                    if gov.checkpoint(lambda: self._stop):   # holds, power policy, eco QoS
                        break
                elif gov.busy() and not gov.wait_quiet(lambda: self._stop):
                    break                                    # map / gesture hold: yield at once
            since_check += 1
            item_id, path = job
            if store.has(item_id):
                continue
            adj = None
            if lookup is not None:
                try:
                    adj = lookup(item_id)
                except Exception:
                    adj = None
            img = decode_source(path, adj)
            if not img.isNull() and store.put_image(item_id, img):
                store.generated += 1
                since_flush += 1
            if since_flush >= FLUSH_EVERY:
                store.flush()
                since_flush = 0
        store.flush()


_STORE = None
_STORE_LOCK = threading.Lock()


def store():
    """The process-wide store (every grid shares it). Loading starts on first use."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = MicroStore()
            _STORE.load_async()
        return _STORE


def peek():
    """The store if one exists (no start-up side effect)."""
    return _STORE


def purge_ids(ids):
    """Hidden-folder hook: also usable when no grid ever created the store."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    st = _STORE
    if st is not None:
        st.purge(ids)
        return
    path = _store_path()
    if not os.path.exists(path):
        return
    try:
        conn = sqlite3.connect(path, timeout=10)
        try:
            conn.execute("PRAGMA secure_delete = ON")
            conn.executemany("DELETE FROM micro WHERE id = ?", [(i,) for i in ids])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[Micro] purge failed: {e}")


def reset_for_tests():
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.stop()
        _STORE = None
