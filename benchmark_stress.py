"""Zátěžový test OmniPhotoVault – škála knihovny, úniky paměti, rozpočet mezipaměti.

Spuštění (ve složce projektu):   python benchmark_stress.py [--quick] [--out výsledky.json]

Vše běží v DOČASNÉ SLOŽCE (DB, config, miniatury, náhledy, obličeje, web cache). Cesty se
přesměrují dřív, než se načte jakýkoli modul aplikace, a ochrana skutečné DB je zapnutá –
omyl vyvolá výjimku místo zápisu do photos.db. Na konci se ověří, že se skutečné soubory
aplikace (photos.db, config.json, .cache) nezměnily.

A) Hromadný import: ~1300 médií (data přes 30 dní, téměř-duplicity, videa, Live Photos)
   skutečným skenerem a skutečným AI démonem (miniatury, pHash, náhledy 2048 px, InsightFace,
   CLIP) – a celou dobu dvě čtecí vlákna (dotazy UI) a zapisující vlákno (oblíbené, koš)
   nad stejnou DB. Pak 3000 syntetických obličejů + přeshlukování pod stejnou zátěží.
   Hlídá se „database is locked“, výjimky, uváznutí (časové limity) a rychlost zastavení.
B) Úniky: skutečné MainWindow – 30× otevřít/zavřít prohlížeč, rychlé listování šipkami
   (1200+ kroků), tažení časové osy u videí (tvorba náhledových pásů). Paměť procesu,
   handly, vlákna, haldy Pythonu (tracemalloc), osiřelé ffmpeg/exiftool procesy.
C) Mezipaměť: >2,5 GB náhledů ze 4 vláken a souběžné enforce_budget(2048) – strop, LRU,
   zbylé .tmp soubory, otevřené handly.
"""
import argparse
import ctypes
import gc
import json
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

# ── 0. Pískoviště (před importem čehokoli z aplikace) ─────────────────────────
SANDBOX = tempfile.mkdtemp(prefix="omni_stress_")
LIB = os.path.join(SANDBOX, "library")

import paths  # noqa: E402

paths.CACHE_ROOT = os.path.join(SANDBOX, ".cache")
paths.THUMB_DIR = os.path.join(paths.CACHE_ROOT, "thumbnails")
paths.PREVIEW_DIR = os.path.join(paths.CACHE_ROOT, "previews")
paths.WEB_CACHE_DIR = os.path.join(paths.CACHE_ROOT, "web")
paths.FACE_DIR = os.path.join(paths.CACHE_ROOT, "faces")
for d in (paths.THUMB_DIR, paths.PREVIEW_DIR, paths.FACE_DIR, LIB):
    os.makedirs(d, exist_ok=True)

import database  # noqa: E402

os.environ[database.TEST_GUARD_ENV] = "1"          # skutečná photos.db = výjimka
database.DB_PATH = os.path.join(SANDBOX, "photos.db")
import config  # noqa: E402

config.CONFIG_FILE = os.path.join(SANDBOX, "config.json")
config.LEGACY_CONFIG_FILE = os.path.join(SANDBOX, "legacy", "config.json")

REAL_FILES = [os.path.join(HERE, n) for n in ("photos.db", "photos.db-wal", "config.json")]
REAL_CACHE = os.path.join(HERE, ".cache")


def fingerprint_real():
    out = {}
    for p in REAL_FILES:
        out[p] = (os.path.getsize(p), os.path.getmtime(p)) if os.path.exists(p) else None
    latest, count = 0.0, 0
    for root, _, files in os.walk(REAL_CACHE):
        for f in files:
            try:
                latest = max(latest, os.path.getmtime(os.path.join(root, f)))
                count += 1
            except OSError:
                pass
    out[REAL_CACHE] = (count, latest)
    return out


REAL_BEFORE = fingerprint_real()

# ── Windows: paměť, handly, vlákna, podřízené procesy ────────────────────────


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


class PE32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_k32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE32)]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
PID = os.getpid()


def mem():
    c = PMC()
    c.cb = ctypes.sizeof(PMC)
    _psapi.GetProcessMemoryInfo(_k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return {"ws_mb": c.WorkingSetSize / 2**20, "peak_ws_mb": c.PeakWorkingSetSize / 2**20,
            "private_mb": c.PrivateUsage / 2**20}


def handles():
    n = wintypes.DWORD()
    _k32.GetProcessHandleCount(_k32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


def processes():
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    entries = []
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    ok = _k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        entries.append((e.th32ProcessID, e.th32ParentProcessID, e.szExeFile, e.cntThreads))
        ok = _k32.Process32NextW(snap, ctypes.byref(e))
    _k32.CloseHandle(snap)
    return entries


def my_threads():
    return next((t for pid, _, _, t in processes() if pid == PID), 0)


def descendants():
    procs = processes()
    kids, frontier = [], {PID}
    while frontier:
        nxt = {pid for pid, ppid, _, _ in procs if ppid in frontier and pid not in frontier}
        kids += [(pid, exe) for pid, ppid, exe, _ in procs if pid in nxt]
        frontier = nxt - {PID}
        if not nxt:
            break
    return kids


def tool_processes():
    """ffmpeg / exiftool spuštěné tímto procesem (přímo i přes podprocesy)."""
    return [(pid, exe) for pid, exe in descendants() if exe.lower() in ("ffmpeg.exe", "exiftool.exe")]


# ── Zachycení výstupu (hledání „database is locked“, tracebacků, chyb) ───────

class Tee:
    def __init__(self, stream, sink):
        self.stream, self.sink, self.lock = stream, sink, threading.Lock()

    def write(self, s):
        with self.lock:
            self.sink.append(s)
        try:
            self.stream.write(s)
        except Exception:
            pass

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


CAPTURED = []
sys.stdout = Tee(sys.stdout, CAPTURED)
sys.stderr = Tee(sys.stderr, CAPTURED)


def captured_problems(since=0):
    text = "".join(CAPTURED[since:])
    lines = text.splitlines()
    keys = ("database is locked", "Traceback", "Chyba", "error", "Error", "Exception", "QThread: Destroyed")
    bad = [l for l in lines if any(k in l for k in keys)
           and "[Bench]" not in l and "errors=0" not in l]
    return bad


def log(msg):
    print(f"[Bench] {msg}", flush=True)


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))] * 1000, 1)


# ── Qt ────────────────────────────────────────────────────────────────────────

from PyQt6.QtCore import Qt, QEventLoop, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402,F401

APP = QApplication(sys.argv)


def wait(ms):
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec()


def wait_thread(thread, timeout_s):
    end = time.monotonic() + timeout_s
    while thread.isRunning() and time.monotonic() < end:
        wait(50)
    return not thread.isRunning()


import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import face_engine  # noqa: E402

face_engine.FACE_CACHE_DIR = paths.FACE_DIR
import scanner  # noqa: E402
import preview_cache  # noqa: E402
import main  # noqa: E402

assert scanner.CACHE_DIR == paths.THUMB_DIR and main.CACHE_DIR == paths.THUMB_DIR, "pískoviště: miniatury"
assert database.DB_PATH.startswith(SANDBOX)
database.init_db()


# ═════════════════════════════════════════════════════════════════════════════
# Generování knihovny
# ═════════════════════════════════════════════════════════════════════════════

def make_photo(i, rng_seed, day0=0, when=None, path=None, quality=88):
    rng = random.Random(rng_seed)
    w, h = 1600, 1200
    base = np.zeros((h, w, 3), dtype=np.uint8)
    c1 = np.array([rng.randrange(256) for _ in range(3)], dtype=np.float32)
    c2 = np.array([rng.randrange(256) for _ in range(3)], dtype=np.float32)
    t = np.linspace(0, 1, w, dtype=np.float32)[None, :, None]
    base[:] = (c1 * (1 - t) + c2 * t).astype(np.uint8)
    img = Image.fromarray(base)
    d = ImageDraw.Draw(img)
    for _ in range(rng.randrange(6, 14)):
        x0, y0 = rng.randrange(w), rng.randrange(h)
        x1, y1 = x0 + rng.randrange(80, 600), y0 + rng.randrange(80, 500)
        col = tuple(rng.randrange(256) for _ in range(3))
        (d.ellipse if rng.random() < 0.5 else d.rectangle)([x0, y0, x1, y1], fill=col)
    noise = np.random.default_rng(rng_seed).integers(-12, 12, (h, w, 3), dtype=np.int16)
    img = Image.fromarray(np.clip(np.asarray(img, dtype=np.int16) + noise, 0, 255).astype(np.uint8))
    when = when or time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(1780000000 + (i // 40) * 86400 + (i % 40) * 600))
    exif = Image.Exif()
    exif[0x0132] = when
    exif.get_ifd(0x8769)[36867] = when
    exif.get_ifd(0x8769)[36868] = when
    path = path or os.path.join(LIB, f"day{i // 40:02d}", f"IMG_{i:05d}.JPG")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "JPEG", quality=quality, exif=exif.tobytes())
    return path, when


def make_video(path, seconds, when):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.strptime(when, "%Y:%m:%d %H:%M:%S"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = [paths.FFMPEG_PATH, "-loglevel", "quiet", "-y", "-f", "lavfi", "-i",
           f"testsrc2=size=1280x720:rate=30:duration={seconds}", "-pix_fmt", "yuv420p",
           "-metadata", f"creation_time={iso}", path]
    subprocess.run(cmd, capture_output=True, timeout=120, creationflags=0x08000000)
    return path


def generate_library(n_photos, n_dupes, n_videos, n_live):
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.perf_counter()
    with ThreadPoolExecutor(8) as ex:
        photos = list(ex.map(lambda i: make_photo(i, 1000 + i), range(n_photos)))
    # téměř-duplicity: stejná scéna, jiná komprese, o pár sekund později
    for k in range(n_dupes):
        src_path, when = photos[k * (n_photos // max(1, n_dupes))]
        i = int(os.path.basename(src_path)[4:9])
        t = time.strftime("%Y:%m:%d %H:%M:%S",
                          time.localtime(time.mktime(time.strptime(when, "%Y:%m:%d %H:%M:%S")) + 3))
        make_photo(i, 1000 + i, when=t, quality=72,
                   path=os.path.join(os.path.dirname(src_path), f"IMG_{i:05d}_copy.JPG"))
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda k: make_video(os.path.join(LIB, f"day{k:02d}", f"VID_{k:03d}.MP4"), 2 + k % 3,
                                         photos[k * 40][1]), range(n_videos)))
        # Live Photos: fotka + krátké video se stejným názvem
        def live(k):
            p, when = make_photo(90000 + k, 5000 + k, path=os.path.join(LIB, "live", f"LIVE_{k:03d}.JPG"))
            make_video(os.path.join(LIB, "live", f"LIVE_{k:03d}.MOV"), 2, when)
        list(ex.map(live, range(n_live)))
    files = sum(len(f) for _, _, f in os.walk(LIB))
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(LIB) for f in fs)
    return {"files": files, "size_mb": round(size / 2**20), "seconds": round(time.perf_counter() - t0, 1)}


# ═════════════════════════════════════════════════════════════════════════════
# Souběžná zátěž DB (jako UI během importu / AI)
# ═════════════════════════════════════════════════════════════════════════════

class DbLoad:
    """2 čtecí vlákna (dotazy mřížky, osob, událostí, mapy) + 1 zapisující (oblíbené, koš)."""

    def __init__(self):
        self.stop = threading.Event()
        self.lat = {"read": [], "write": []}
        self.errors = []
        self.ops = {"read": 0, "write": 0}
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self._reader, args=(k,), daemon=True) for k in range(2)]
        self.threads.append(threading.Thread(target=self._writer, daemon=True))

    def start(self):
        for t in self.threads:
            t.start()
        return self

    def finish(self, timeout=30):
        self.stop.set()
        for t in self.threads:
            t.join(timeout)
        return all(not t.is_alive() for t in self.threads)

    def _record(self, kind, dt, err=None):
        with self.lock:
            self.ops[kind] += 1
            self.lat[kind].append(dt)
            if err:
                self.errors.append(f"{kind}: {err}")

    def _reader(self, k):
        from events_view import fetch_event_cards
        rng = random.Random(k)
        while not self.stop.is_set():
            t0 = time.perf_counter()
            try:
                q = rng.randrange(4)
                if q == 0:
                    main.fetch_media_records()
                elif q == 1:
                    conn = database.get_connection()
                    try:
                        conn.execute("SELECT p.id, p.name, COUNT(f.id) FROM people p LEFT JOIN face_instances f "
                                     "ON f.person_id = p.id AND COALESCE(f.is_valid, 1) = 1 GROUP BY p.id").fetchall()
                    finally:
                        conn.close()
                elif q == 2:
                    conn = database.get_connection()
                    try:
                        fetch_event_cards(conn)
                    finally:
                        conn.close()
                else:
                    main.fetch_media_records("m.is_favorite = 1")
                self._record("read", time.perf_counter() - t0)
            except Exception as e:
                self._record("read", time.perf_counter() - t0, repr(e))
            time.sleep(0.01)

    def _writer(self):
        rng = random.Random(7)
        while not self.stop.is_set():
            ids = self._ids()
            if not ids:
                time.sleep(0.05)
                continue
            mid = rng.choice(ids)
            t0 = time.perf_counter()
            try:
                if rng.random() < 0.6:
                    database.toggle_favorite(mid)
                else:
                    database.trash_media([mid])
                    database.restore_media([mid])
                self._record("write", time.perf_counter() - t0)
            except Exception as e:
                self._record("write", time.perf_counter() - t0, repr(e))
            time.sleep(0.02)

    _cache = (0.0, [])

    def _ids(self):
        if time.monotonic() - self._cache[0] > 2:
            try:
                conn = database.get_connection()
                try:
                    ids = [r[0] for r in conn.execute("SELECT id FROM media_items LIMIT 5000")]
                finally:
                    conn.close()
                self._cache = (time.monotonic(), ids)
            except Exception:
                pass
        return self._cache[1]

    def summary(self):
        return {"reads": self.ops["read"], "writes": self.ops["write"],
                "read_p50_ms": pct(self.lat["read"], .5), "read_p99_ms": pct(self.lat["read"], .99),
                "read_max_ms": pct(self.lat["read"], 1.0),
                "write_p50_ms": pct(self.lat["write"], .5), "write_p99_ms": pct(self.lat["write"], .99),
                "write_max_ms": pct(self.lat["write"], 1.0),
                "errors": len(self.errors), "error_samples": self.errors[:5],
                "locked_errors": sum("locked" in e for e in self.errors)}


def q1(sql, params=()):
    conn = database.get_connection()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# A) Import
# ═════════════════════════════════════════════════════════════════════════════

def scenario_a(args):
    res = {}
    log("A: generuji knihovnu…")
    res["library"] = generate_library(args.photos, args.dupes, args.videos, args.live)
    log(f"A: knihovna {res['library']}")
    mark = len(CAPTURED)

    # A1 – sken (exiftool, zápisy dávek, Live Photos, miniatury, pHash, události) pod zátěží
    load = DbLoad().start()
    t0 = time.perf_counter()
    scan = scanner.MediaScannerThread(LIB)
    result = {}
    scan.finished_signal.connect(lambda n: result.__setitem__("processed", n))
    scan.start()
    finished = wait_thread(scan, args.scan_timeout)
    scan_s = time.perf_counter() - t0
    ok_threads = load.finish()
    n_media = q1("SELECT COUNT(*) FROM media_items")
    res["scan"] = {
        "finished": finished, "seconds": round(scan_s, 1), "processed": result.get("processed"),
        "media_rows": n_media, "items_per_s": round(n_media / scan_s, 1) if scan_s else None,
        "thumbnails": q1("SELECT COUNT(*) FROM media_items WHERE thumbnail_path IS NOT NULL"),
        "phashes": q1("SELECT COUNT(*) FROM media_items WHERE phash IS NOT NULL"),
        "events": q1("SELECT COUNT(*) FROM events"),
        "live_pairs": q1("SELECT COUNT(*) FROM media_items WHERE paired_video_path IS NOT NULL"),
        "db_load": load.summary(), "load_threads_stopped": ok_threads,
    }
    log(f"A1 sken: {res['scan']}")

    # A2 – AI démon (miniatury, ořezy, pHash, náhledy, embeddingy, obličeje, CLIP, portréty)
    load = DbLoad().start()
    phases = []
    daemon = main.BackgroundAIDaemon(face_threshold=0.62)
    daemon.status_changed.connect(lambda s: s and (not phases or phases[-1][1].split("(")[0] != s.split("(")[0])
                                  and phases.append((time.perf_counter(), s)))
    t0 = time.perf_counter()
    daemon.start()
    finished = wait_thread(daemon, args.daemon_budget)
    run_s = time.perf_counter() - t0
    stop_s = None
    if not finished:                                # časový rozpočet: změřit i rychlost zastavení
        t1 = time.perf_counter()
        daemon.stop()
        wait_thread(daemon, 60)
        stop_s = round(time.perf_counter() - t1, 2)
    ok_threads = load.finish()
    scanned = q1("SELECT COUNT(*) FROM media_items WHERE faces_scanned = 1")
    face_phase = [t for t, s in phases if s.startswith("Analýza tváří")]
    face_end = next((t for t, s in phases if face_phase and t > face_phase[0] and not s.startswith("Analýza tváří")),
                    t0 + run_s)
    res["daemon"] = {
        "finished_within_budget": finished, "seconds": round(run_s, 1), "stop_latency_s": stop_s,
        "phases": [(round(t - t0, 1), s[:40]) for t, s in phases][:12],
        "previews": len([f for f in os.listdir(paths.PREVIEW_DIR) if f.endswith(".jpg")]),
        "faces_scanned": scanned, "image_embeddings": q1("SELECT COUNT(*) FROM image_embeddings"),
        "face_items_per_s": round(scanned / (face_end - face_phase[0]), 2) if face_phase else None,
        "db_load": load.summary(), "load_threads_stopped": ok_threads,
        "still_running": daemon.isRunning(),
    }
    log(f"A2 démon: {res['daemon']}")

    # A3 – 3000 syntetických obličejů (40 identit) + přeshlukování / návrhy / portréty pod zátěží
    res["faces"] = synthetic_faces()
    res["output_problems"] = captured_problems(mark)[:15]
    res["tool_processes_left"] = tool_processes()
    return res


def synthetic_faces(n_faces=3000, n_ident=40):
    """Syntetické identity přes celou knihovnu – jedna osoba nejvýš jednou na fotce (jako ve
    skutečnosti; dvě tváře na jedné fotce shlukování správně nikdy nespojí). Ověří čistotu
    (osoba = jediná identita) i úplnost (identita = jediná osoba)."""
    import face_cluster
    rng = np.random.default_rng(42)
    conn = database.get_connection()
    try:
        media = [r for (r,) in conn.execute("SELECT id FROM media_items WHERE media_type != 'video'")]
    finally:
        conn.close()
    if len(media) < 200:                                  # samostatný běh (F): fiktivní fotky
        with database.db_transaction() as c:
            c.executemany("INSERT INTO media_items (file_path, file_name, media_type, date_taken) VALUES (?, ?, 'photo', ?)",
                          [(os.path.join(LIB, f"face_{i:05d}.jpg"), f"face_{i:05d}.jpg", "2026-06-01 10:00:00")
                           for i in range(1300)])
        conn = database.get_connection()
        try:
            media = [r for (r,) in conn.execute("SELECT id FROM media_items WHERE media_type != 'video'")]
        finally:
            conn.close()
    centers = rng.normal(size=(n_ident, 512)).astype(np.float32)
    centers /= np.linalg.norm(centers, axis=1, keepdims=True)
    crop = Image.new("RGB", (160, 160), (180, 140, 120))
    rows, identity_of, used = [], [], set()
    for k in range(n_faces):
        ident = k % n_ident
        while True:
            m = media[int(rng.integers(len(media)))]
            if (m, ident) not in used:
                used.add((m, ident))
                break
        v = centers[ident] + rng.normal(scale=0.035, size=512).astype(np.float32)
        v = (v / np.linalg.norm(v) * 21.0).astype(np.float32)
        crop_path = os.path.join(paths.FACE_DIR, f"synth_{k:05d}.jpg")
        crop.save(crop_path, "JPEG", quality=70)
        # quality_score = ostrost ořezu (brána MIN_QUALITY_SCORE = 8), reálně ~20–150
        rows.append((m, crop_path, v.tobytes(), 0.1, 0.1, 0.2, 0.2,
                     float(rng.uniform(40, 120)), 2, 0.9, 0, 1, 0.9, 0.9, 1))
        identity_of.append(ident)
    with database.db_transaction() as conn:
        first = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM face_instances").fetchone()[0] or 0) + 1
        conn.executemany("INSERT INTO face_instances (media_id, face_crop_path, embedding, bbox_x, bbox_y, bbox_w, bbox_h,"
                         " quality_score, crop_version, frontality, crowded, is_valid, face_score, det_score, gate_version)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    load = DbLoad().start()
    t0 = time.perf_counter()
    stats = face_cluster.strict_recluster_faces(threshold=0.62)
    recluster_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    suggestions = face_cluster.suggest_person_merges()
    suggest_s = time.perf_counter() - t1
    t2 = time.perf_counter()
    covers = face_cluster.refresh_person_covers()
    covers_s = time.perf_counter() - t2
    ok_threads = load.finish()
    conn = database.get_connection()
    try:
        assigned = dict(conn.execute("SELECT id, person_id FROM face_instances WHERE id >= ?", (first,)).fetchall())
    finally:
        conn.close()
    by_person, by_ident = {}, {}
    for k, ident in enumerate(identity_of):
        pid = assigned.get(first + k)
        if pid is None:
            continue
        by_person.setdefault(pid, set()).add(ident)
        by_ident.setdefault(ident, set()).add(pid)
    out = {"faces": n_faces, "identities": n_ident, "people_found": len(by_person),
           "faces_unassigned": sum(1 for k in range(n_faces) if assigned.get(first + k) is None),
           "pure_people": sum(1 for s in by_person.values() if len(s) == 1),
           "identities_in_one_person": sum(1 for s in by_ident.values() if len(s) == 1),
           "recluster_s": round(recluster_s, 2), "suggest_s": round(suggest_s, 2),
           "covers_s": round(covers_s, 2), "covers_changed": covers, "suggestions": len(suggestions or []),
           "faces_per_s": round(n_faces / recluster_s), "stats": {k: v for k, v in (stats or {}).items()
                                                                 if isinstance(v, (int, float))},
           "db_load": load.summary(), "load_threads_stopped": ok_threads}
    log(f"A3 obličeje: {out}")
    return out


# ═════════════════════════════════════════════════════════════════════════════
# B) Úniky – prohlížeč, listování, tažení, náhledové pásy
# ═════════════════════════════════════════════════════════════════════════════

def running_qthreads():
    """Živé a běžící QThread objekty (smazaný C++ objekt = už neběží)."""
    from PyQt6.QtCore import QThread
    n = 0
    for o in gc.get_objects():
        if isinstance(o, QThread):
            try:
                n += bool(o.isRunning())
            except RuntimeError:
                pass
    return n


def snapshot():
    gc.collect()
    APP.processEvents()
    m = mem()
    m.update(handles=handles(), threads=my_threads())
    return m


def scenario_b(args):
    import tracemalloc
    from unittest import mock
    from PyQt6.QtCore import QThread
    res = {}
    patcher = mock.patch.object(main.MainWindow, "_start_ai_daemon", lambda self: None)   # démon měřen v A
    patcher.start()
    w = main.MainWindow()
    w.resize(1500, 900)
    w.show()
    wait(1500)
    records = list(w._filtered_records)
    videos = [i for i, r in enumerate(records) if r[3] == "video"]
    log(f"B: {len(records)} položek v mřížce, {len(videos)} videí")
    rng = random.Random(3)
    steps = {"nav": 0, "scrub_moves": 0, "opens": 0}

    def cycle(n_nav, scrub=True):
        if videos and rng.random() < 0.5:
            idx = max(0, rng.choice(videos) - rng.randrange(0, 4))        # brzy narazí na video
        else:
            idx = rng.randrange(0, max(1, len(records) - n_nav))
        w._open_viewer(records, idx, w.grid_view.photo_model)
        steps["opens"] += 1
        v = w._viewer
        wait(120)
        direction = 1 if rng.random() < 0.7 else -1
        for _ in range(n_nav):
            (v.show_next if direction > 0 else v.show_prev)()
            steps["nav"] += 1
            wait(12)                                   # podržená šipka (~80 kroků/s)
            if scrub and v._is_video and v.player.duration() <= 0:
                wait(250)                              # dát videu načíst délku
            if scrub and v._is_video and v.player.duration() > 0:
                dur = v.player.duration()
                for k in range(25):
                    v.scrub_to(int(dur * k / 25))
                    steps["scrub_moves"] += 1
                    wait(16)
                v.end_scrub(int(dur * 0.5))
                wait(200)
        w._close_viewer()
        wait(60)

    cycle(30)                                          # zahřátí (první prohlížeč, dekodéry, cache)
    wait(500)
    base = snapshot()
    tracemalloc.start(15)
    tm0 = tracemalloc.take_snapshot()
    samples = []
    t0 = time.perf_counter()
    for c in range(args.cycles):
        cycle(40)
        samples.append(snapshot())
    nav_s = time.perf_counter() - t0
    tm1 = tracemalloc.take_snapshot()
    top = [str(s)[:160] for s in tm1.compare_to(tm0, "lineno")[:8]]
    py_growth = sum(s.size_diff for s in tm1.compare_to(tm0, "filename")) / 2**20
    tracemalloc.stop()

    # pásy dokončit, pak nečinnost: nesmí zůstat ffmpeg ani běžící generátor
    import scrub_strip
    mgr = scrub_strip.manager()
    end = time.monotonic() + 60
    while (mgr._builder is not None) and time.monotonic() < end:
        wait(100)
    wait(500)
    idle_tools = tool_processes()
    settled = snapshot()

    priv = [s["private_mb"] for s in samples]
    xs = list(range(len(priv)))
    tail = max(5, len(priv) // 3)
    slope = float(np.polyfit(xs[-tail:], priv[-tail:], 1)[0]) if len(priv) >= 5 else None
    strips = len([f for f in os.listdir(paths.PREVIEW_DIR) if f.endswith(".scrub")])
    res.update({
        "cycles": args.cycles, "opens": steps["opens"], "nav_steps": steps["nav"], "scrub_moves": steps["scrub_moves"],
        "nav_steps_per_s": round(steps["nav"] / nav_s, 1), "seconds": round(nav_s, 1),
        "strips_on_disk": strips,
        "baseline": {k: round(v, 1) for k, v in base.items()},
        "peak_private_mb": round(max(priv), 1), "peak_ws_mb": round(max(s["peak_ws_mb"] for s in samples), 1),
        "settled": {k: round(v, 1) for k, v in settled.items()},
        "private_growth_mb": round(settled["private_mb"] - base["private_mb"], 1),
        "private_slope_mb_per_cycle_last_third": None if slope is None else round(slope, 2),
        "handle_growth": settled["handles"] - base["handles"],
        "thread_growth": settled["threads"] - base["threads"],
        "python_heap_growth_mb": round(py_growth, 2), "tracemalloc_top": top,
        "tool_processes_idle": idle_tools,
        "qthreads_running_idle": running_qthreads(),
    })
    # zavření okna = úklid jako při ukončení aplikace
    t1 = time.perf_counter()
    w.close()
    wait(300)
    res["close_seconds"] = round(time.perf_counter() - t1, 2)
    w.deleteLater()
    wait(300)
    patcher.stop()
    res["qthreads_running_after_close"] = running_qthreads()
    res["tool_processes_after_close"] = tool_processes()
    log(f"B: {res}")
    return res


# ═════════════════════════════════════════════════════════════════════════════
# C) Rozpočet mezipaměti náhledů pod zátěží
# ═════════════════════════════════════════════════════════════════════════════

def dir_bytes(d):
    total = 0
    with os.scandir(d) as it:
        for e in it:
            try:
                if e.is_file():
                    total += e.stat().st_size
            except OSError:
                pass
    return total


def scenario_c(args):
    from PyQt6.QtGui import QImage
    res = {}
    cdir = os.path.join(SANDBOX, "cache_c")
    os.makedirs(cdir, exist_ok=True)
    saved_dir = paths.PREVIEW_DIR
    paths.PREVIEW_DIR = cdir
    budget_mb = 2048
    try:
        noise = np.random.default_rng(1).integers(0, 255, (1536, 2048, 3), dtype=np.uint8)
        imgs = []
        for k in range(4):                              # 4 nekomprimovatelné 2048px snímky (~1,5–2,5 MB JPEG)
            arr = np.ascontiguousarray(np.roll(noise, k * 97, axis=1))
            q = QImage(arr.data, 2048, 1536, 2048 * 3, QImage.Format.Format_RGB888).copy()
            imgs.append(q)
        h0 = handles()
        stop = threading.Event()
        errors, written, sizes = [], [0], []
        hot = [os.path.join(SANDBOX, "src", f"hot_{k:03d}.jpg") for k in range(40)]
        lock = threading.Lock()

        def writer(t):
            k = 0
            while not stop.is_set():
                src = os.path.join(SANDBOX, "src", f"w{t}_{k:05d}.jpg")
                try:
                    if preview_cache.save_preview(src, imgs[(t + k) % 4]) is None:
                        raise RuntimeError("save_preview vrátil None")
                    with lock:
                        written[0] += os.path.getsize(preview_cache.preview_path(src))
                except Exception as e:
                    with lock:
                        errors.append(f"write: {e!r}"[:160])
                k += 1

        def enforcer():                                  # démon + generátor pásů volají souběžně
            while not stop.is_set():
                try:
                    preview_cache.enforce_budget(budget_mb)
                except Exception as e:
                    errors.append(f"enforce: {e!r}"[:160])
                time.sleep(0.25)

        def toucher():                                   # LRU: „horké“ náhledy se pořád používají
            while not stop.is_set():
                for src in hot:
                    p = preview_cache.preview_path(src)
                    if os.path.exists(p):
                        try:
                            os.utime(p, None)
                        except OSError:
                            pass
                time.sleep(0.2)

        for src in hot:                                  # horké vznikly NEJDŘÍV (nejstarší zápis)
            preview_cache.save_preview(src, imgs[0])
        threads = [threading.Thread(target=writer, args=(t,), daemon=True) for t in range(4)]
        threads += [threading.Thread(target=enforcer, daemon=True), threading.Thread(target=toucher, daemon=True)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        peak, target_bytes = 0, args.cache_gb * 2**30
        while written[0] < target_bytes and time.perf_counter() - t0 < args.cache_timeout:
            time.sleep(0.1)
            try:
                sz = dir_bytes(cdir)
                sizes.append(sz)
                peak = max(peak, sz)
            except OSError:
                pass
        stop.set()
        for t in threads:
            t.join(30)
        write_s = time.perf_counter() - t0
        preview_cache.enforce_budget(budget_mb)          # závěrečný úklid (jako konec průchodu démona)
        final = dir_bytes(cdir)
        files = os.listdir(cdir)
        tmp_left = [f for f in files if f.endswith(".tmp") or ".tmp." in f]
        locked = []
        for f in files:                                 # otevřený handle = přejmenování selže
            p = os.path.join(cdir, f)
            try:
                os.replace(p, p + ".chk")
                os.replace(p + ".chk", p)
            except OSError as e:
                locked.append(f"{f}: {e}")
        hot_left = sum(os.path.exists(preview_cache.preview_path(s)) for s in hot)
        res.update({
            "budget_mb": budget_mb, "written_gb": round(written[0] / 2**30, 2), "seconds": round(write_s, 1),
            "throughput_mb_s": round(written[0] / 2**20 / write_s, 1),
            "files_written_per_s": round(written[0] / 2**20 / write_s / 1.9, 1),
            "peak_mb": round(peak / 2**20), "overshoot_mb": round(max(0, peak - budget_mb * 2**20) / 2**20),
            "final_mb": round(final / 2**20, 1), "final_within_budget": final <= budget_mb * 2**20,
            "files_final": len(files), "tmp_files_left": tmp_left[:5], "locked_files": locked[:5],
            "hot_survived": f"{hot_left}/{len(hot)}", "errors": len(errors), "error_samples": errors[:5],
            "handle_growth": handles() - h0,
        })
    finally:
        paths.PREVIEW_DIR = saved_dir
    log(f"C: {res}")
    return res


# ═════════════════════════════════════════════════════════════════════════════

def main_cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="menší škála (kontrola skriptu)")
    ap.add_argument("--out", default=os.path.join(SANDBOX, "results.json"))
    ap.add_argument("--only", default="ABC")
    args = ap.parse_args()
    q = args.quick
    args.photos, args.dupes, args.videos, args.live = (120, 6, 3, 3) if q else (1200, 60, 12, 20)
    args.scan_timeout, args.daemon_budget = (300, 120) if q else (900, 600)
    args.cycles = 4 if q else 30
    args.cache_gb, args.cache_timeout = (0.3, 60) if q else (2.6, 400)
    results = {"sandbox": SANDBOX, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    results["start"] = {k: round(v, 1) for k, v in mem().items()}
    t0 = time.perf_counter()
    try:
        if "A" in args.only:
            results["A"] = scenario_a(args)
        if "B" in args.only:
            results["B"] = scenario_b(args)
        if "C" in args.only:
            results["C"] = scenario_c(args)
        if "F" in args.only and "A" not in args.only:      # jen obličeje (bez importu)
            results["F"] = synthetic_faces()
    except Exception:
        results["crash"] = traceback.format_exc()
        log(results["crash"])
    results["total_seconds"] = round(time.perf_counter() - t0, 1)
    results["end"] = {k: round(v, 1) for k, v in mem().items()}
    results["tool_processes_at_end"] = tool_processes()
    results["real_app_files_unchanged"] = fingerprint_real() == REAL_BEFORE
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    log(f"výsledky: {args.out}")
    log(f"skutečné soubory aplikace beze změny: {results['real_app_files_unchanged']}")
    import scrub_strip
    scrub_strip.manager().shutdown()
    sys.stdout.flush()
    try:
        shutil.rmtree(SANDBOX, ignore_errors=True) if not os.environ.get("OMNI_KEEP_SANDBOX") else None
    except Exception:
        pass
    os._exit(0 if "crash" not in results else 1)


if __name__ == "__main__":
    main_cli()
