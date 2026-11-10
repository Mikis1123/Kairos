"""Náhledový pás videa pro plynulé tažení časové osy (Patch 14, „trickplay“).

Přesun (seek) v 4K HEVC znamená dekódovat od klíčového snímku k cíli – ~110–160 ms na
přesun, naměřeno. Živé tažení tak nikdy nemůže být plynulejší než ~7 snímků/s. Proto se při
otevření videa na pozadí (nízká priorita, D3D11VA) připraví malé JPEG snímky (480 px):

  1. průchod: jen klíčové snímky (-skip_frame nokey) – ~1 snímek/s videa; 16 min 4K za ~10 s,
     první snímky do ~0,3 s. U dlouhých videí to odpovídá rozlišení posuvníku (px ≈ 1 s).
  2. průchod (jen videa do DENSE_MAX_S): všechny snímky přes fps filtr (DENSE_FPS) – krátké
     klipy se pak táhnou plynule, ne po sekundových skocích.

Během tažení se ukazuje nejbližší snímek pásu (okamžitě, žádný dekodér), přesný 4K snímek
až když se kurzor zastaví a po puštění. Pás se ukládá jako jeden soubor do mezipaměti
náhledů (stejný rozpočet LRU i mazání jako náhledy 2048 px), platný jen pokud je novější
než video.

Formát souboru: b"OPVS" + verze (1 B) + délka hlavičky (uint32 LE) + JSON hlavička
{"duration_ms", "dense", "frames": [[ms, offset, length], ...]} + JPEG data za sebou.

Fáze 1 (adaptivní video): pás se dekóduje z 1080p proxy, existuje-li (video_proxy) – 4K HDR
originál nikdo podruhé nedekóduje. Odložený pás (governed) čeká na klid a jeho ffmpeg je
během přehrávání pozastavený (governor); tažení časové osy ho okamžitě povýší na prioritní.
Pásy hotových videí připravuje démon předem, takže při prvním tažení už leží na disku.
"""
import bisect
import json
import os
import re
import struct
import subprocess
import threading
import time
from collections import OrderedDict

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtGui import QImage

import governor
import paths
from tool_timeouts import StallWatchdog
import video_clap
import preview_cache
import video_proxy

STRIP_EDGE = 480
STRIP_QUALITY = 6                # ffmpeg -q:v (2 = nejlepší … 31); 6 ≈ 10–40 kB na snímek
# Krátké klipy (Live Photo, ~3 s) hraje živý náhled v mřížce přes celou dlaždici: na HiDPI je
# dlaždice při 3 sloupcích ~900 px – pás 480 px byl roztažený 2–2,5× a rozmazaný. Krátké klipy
# proto ostřeji: delší strana SHORT_EDGE, zmenšení „area“ (bez aliasů) a lepší JPEG. Celý pás
# klipu do SHORT_MAX_MS má pak ~36 snímků po ~60–120 kB.
SHORT_MAX_MS = 10000
SHORT_EDGE = 1080
SHORT_QUALITY = 3
DENSE_MAX_S = 90                 # hustý průchod jen pro krátká videa (4K dekód ~0,45 s / s videa)
DENSE_FPS = 12
STRIP_MAGIC = b"OPVS"
STRIP_VERSION = 3               # 2 = HDR originál (bez proxy) převedený na SDR jako přehrávání
                                # 3 = celý kódovaný snímek jako Qt a fotka (bez kontejnerového clap, video_clap)
DECODED_CACHE = 48               # dekódované QImage v RAM na pás
_PTS = re.compile(r"pts_time:\s*(-?[0-9.]+)")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_EOI = b"\xff\xd9"


def strip_path(source):
    return preview_cache.base_path(source) + ".scrub"


def edge_for(duration_ms):
    """Delší strana snímků pásu podle délky videa (0 = neznámá -> běžný pás)."""
    return SHORT_EDGE if 0 < int(duration_ms or 0) <= SHORT_MAX_MS else STRIP_EDGE


def probe_duration_ms(source):
    """Délka videa z hlavičky (ffmpeg -i), 0 = nezjištěno."""
    if not os.path.exists(paths.FFMPEG_PATH):
        return 0
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        err = subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-i", source], capture_output=True,
                             timeout=20, creationflags=flags).stderr.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return 0
    d = _DURATION.search(err)
    if not d:
        return 0
    h, mi, s = d.groups()
    return int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)


class ScrubStrip:
    """Seřazené snímky pásu; přidávají se z vlákna generátoru, čtou z GUI vlákna."""

    def __init__(self, source, duration_ms=0):
        self.source = source
        self.duration_ms = int(duration_ms or 0)
        self.times = []
        self._blobs = []
        self.dense = False
        self.complete = False
        self.edge = edge_for(self.duration_ms)   # delší strana snímků (StripBuilder ji určí po sondě)
        self._lock = threading.Lock()
        self._decoded = OrderedDict()

    def __len__(self):
        return len(self.times)

    def add(self, ms, jpeg, min_gap_ms=0):
        """Vloží snímek (na správné místo – hustý průchod vyplňuje mezery). False = duplicita."""
        ms = int(ms)
        with self._lock:
            i = bisect.bisect_left(self.times, ms)
            for j in (i - 1, i):
                if 0 <= j < len(self.times) and abs(self.times[j] - ms) <= min_gap_ms:
                    return False
            self.times.insert(i, ms)
            self._blobs.insert(i, bytes(jpeg))
            self._decoded.clear()           # indexy se posunuly
        return True

    def nearest(self, ms):
        """(index, čas) nejbližšího snímku, nebo (None, None) u prázdného pásu."""
        with self._lock:
            if not self.times:
                return None, None
            i = bisect.bisect_left(self.times, int(ms))
            best = min((j for j in (i - 1, i) if 0 <= j < len(self.times)),
                       key=lambda j: abs(self.times[j] - ms))
            return best, self.times[best]

    def image(self, index):
        with self._lock:
            img = self._decoded.get(index)
            if img is not None:
                self._decoded.move_to_end(index)
                return img
            blob = self._blobs[index] if 0 <= index < len(self._blobs) else None
        if blob is None:
            return None
        img = QImage.fromData(blob, "JPG")
        if img.isNull():
            return None
        with self._lock:
            self._decoded[index] = img
            while len(self._decoded) > DECODED_CACHE:
                self._decoded.popitem(last=False)
        return img

    def decode(self, index, size=None):
        """Snímek jako QImage BEZ ukládání do _decoded (náhled v mřížce si drží vlastní,
        menší snímky – sdílená mezipaměť pásu zůstane prohlížeči). size = výplň na QSize."""
        with self._lock:
            blob = self._blobs[index] if 0 <= index < len(self._blobs) else None
        img = QImage.fromData(blob, "JPG") if blob else QImage()
        if img.isNull():
            return None
        if size is not None:
            from PyQt6.QtCore import Qt
            img = img.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        return img

    def frame_near(self, ms, tolerance_ms):
        """QImage nejbližšího snímku, pokud leží do tolerance; jinak None (pás tam ještě není)."""
        index, t = self.nearest(ms)
        if index is None or abs(t - int(ms)) > tolerance_ms:
            return None
        return self.image(index)

    # ── disk ──
    def save(self, path=None):
        import locked_folder
        if locked_folder.is_hidden_path(self.source):
            return None                      # Skryté: pás jen v paměti, na disk ne
        path = path or strip_path(self.source)
        with self._lock:
            frames, blobs, offset = [], [], 0
            for t, b in zip(self.times, self._blobs):
                frames.append([t, offset, len(b)])
                blobs.append(b)
                offset += len(b)
            header = json.dumps({"duration_ms": self.duration_ms, "dense": self.dense, "edge": self.edge,
                                 "frames": frames}).encode("utf-8")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(STRIP_MAGIC + bytes([STRIP_VERSION]) + struct.pack("<I", len(header)) + header)
            for b in blobs:
                f.write(b)
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, source, path=None):
        """Platný pás z disku (novější než video, stejná verze), jinak None."""
        path = path or strip_path(source)
        try:
            if os.path.getmtime(path) < os.path.getmtime(source):
                return None
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if data[:4] != STRIP_MAGIC or data[4] != STRIP_VERSION:
            return None
        (hlen,) = struct.unpack("<I", data[5:9])
        try:
            header = json.loads(data[9:9 + hlen].decode("utf-8"))
        except ValueError:
            return None
        # pás krátkého klipu z doby před SHORT_EDGE (480 px) se přestaví ostřejší
        edge = int(header.get("edge") or STRIP_EDGE)
        if edge < edge_for(header.get("duration_ms", 0)):
            return None
        base = 9 + hlen
        strip = cls(source, header.get("duration_ms", 0))
        strip.edge = edge
        for t, off, n in header.get("frames", []):
            strip.times.append(int(t))
            strip._blobs.append(data[base + off: base + off + n])
        strip.dense = bool(header.get("dense"))
        strip.complete = True
        return strip


def _ffmpeg_cmd(source, keyframes_only, hw, threads=2, edge=STRIP_EDGE, sdr=""):
    sharp = edge > STRIP_EDGE
    # delší strana nejvýš edge, menší videa se nezvětšují
    scale = (f"scale='if(gt(iw,ih),min({edge},iw),-2)':'if(gt(iw,ih),-2,min({edge},ih))'"
             f":flags={'area' if sharp else 'fast_bilinear'}")
    if sdr:                                          # HDR -> SDR až po zmenšení (video_proxy.sdr_filter)
        scale += "," + sdr
    vf = (scale if keyframes_only else f"fps={DENSE_FPS},{scale}") + ",format=yuvj420p,showinfo"
    cmd = [paths.FFMPEG_PATH, "-hide_banner", "-nostats", "-loglevel", "info", "-threads", str(threads)]
    if hw:
        cmd += ["-hwaccel", "d3d11va"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += video_clap.ffmpeg_full_frame_args(paths.FFMPEG_PATH)   # framing = Qt přehrávání = fotka
    cmd += ["-i", source, "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf]
    if keyframes_only:
        cmd += ["-fps_mode", "passthrough"]
    cmd += ["-c:v", "mjpeg", "-q:v", str(SHORT_QUALITY if sharp else STRIP_QUALITY), "-f", "image2pipe", "-"]
    return cmd


class StripBuilder(QThread):
    """Vytvoří pás pro jedno video (oba průchody), průběžně plní `strip`."""
    progress = pyqtSignal(str, int)          # zdroj, počet snímků
    finished_strip = pyqtSignal(str, bool)   # zdroj, úspěch

    def __init__(self, strip, parent=None, governed=False, dense=True, threads=2):
        super().__init__(parent)
        self.strip = strip
        self._proc = None
        self._cancel = False
        self.governed = governed          # odložený: čeká na klid, při přehrávání pozastavený
        self.dense = dense                # hustý průchod (výkonnostní profil)
        self.threads = threads
        self.should_stop = None           # démon: zrušit při ukončení aplikace
        self.on_fraction = None           # démon: průběh 0–1 přes oba průchody (stavový řádek)
        self._passes = (0, 1)             # (hotové průchody, průchodů celkem)

    def cancel(self):
        self._cancel = True
        proc = self._proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def prioritize(self):
        """Uživatel táhne časovou osu: přestat čekat na klid a pozastavený ffmpeg pustit."""
        self.governed = False
        proc = self._proc
        if proc is not None:
            governor.get().unregister_process(proc.pid)

    def decode_source(self):
        """Z čeho dekódovat: 1080p proxy (4K / HDR), jinak originál."""
        return video_proxy.get_proxy(self.strip.source) or self.strip.source

    def run(self):
        ok = False
        try:
            if not self.strip.duration_ms:          # rozlišení pásu závisí na délce (edge_for)
                self.strip.duration_ms = probe_duration_ms(self.strip.source)
            self.strip.edge = edge_for(self.strip.duration_ms)
            dense = self.dense and 0 < self.strip.duration_ms <= DENSE_MAX_S * 1000
            self._passes = (0, 2 if dense else 1)
            ok = self._pass(keyframes_only=True)
            if ok and dense and not self._cancel:
                self._passes = (1, 2)
                gap = int(1000 / DENSE_FPS / 2)
                if self._pass(keyframes_only=False, min_gap_ms=gap):
                    self.strip.dense = True
            if ok and not self._cancel:
                try:                                  # nejdřív uložit, pak teprve „hotovo“
                    self.strip.save()
                    preview_cache.enforce_budget(preview_cache.DEFAULT_BUDGET_MB)
                except OSError as e:
                    print(f"[Scrub] pás nelze uložit: {e}")
                self.strip.complete = True
        except Exception as e:
            print(f"[Scrub] {os.path.basename(self.strip.source)}: {e}")
            ok = False
        self.finished_strip.emit(self.strip.source, bool(ok and not self._cancel))

    def _pass(self, keyframes_only, min_gap_ms=0):
        for hw in (True, False):                     # bez D3D11VA (staré ovladače) softwarově
            added = self._run_ffmpeg(keyframes_only, hw, min_gap_ms)
            if added is None:
                return False                          # zrušeno
            if added > 0 or len(self.strip):
                return True
        return False

    def _run_ffmpeg(self, keyframes_only, hw, min_gap_ms):
        if not os.path.exists(paths.FFMPEG_PATH):
            return 0
        gov = governor.get()
        stop = self.should_stop or (lambda: False)
        if self.governed and not gov.wait_quiet(lambda: self._cancel or stop() or not self.governed):
            if self._cancel or stop():
                return None                           # zrušeno během čekání na klid
        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | 0x00004000   # BELOW_NORMAL_PRIORITY_CLASS
        src = self.decode_source()
        sdr = ""
        if src == self.strip.source:                  # proxy je už SDR; originál může být HDR
            if not hasattr(self, "_sdr"):
                try:
                    self._sdr = video_proxy.sdr_filter(video_proxy.probe(src))
                except Exception:
                    self._sdr = ""
            sdr = self._sdr
        proc = subprocess.Popen(_ffmpeg_cmd(src, keyframes_only, hw, self.threads, self.strip.edge, sdr),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        self._proc = proc
        if self.governed:
            gov.register_process(proc.pid)            # přehrávání / gesto = pozastavit
        else:
            governor.assign_to_job(proc.pid)          # ffmpeg nepřežije aplikaci (Sprint R)
        pts, done_err = [], threading.Event()

        def read_err():
            for raw in proc.stderr:
                line = raw.decode("utf-8", "replace")
                m = _PTS.search(line)
                if m and "showinfo" in line:
                    pts.append(float(m.group(1)))
                elif not self.strip.duration_ms:
                    d = _DURATION.search(line)
                    if d:
                        h, mi, s = d.groups()
                        self.strip.duration_ms = int((int(h) * 3600 + int(mi) * 60 + float(s)) * 1000)
            done_err.set()

        threading.Thread(target=read_err, daemon=True).start()
        buf, index, added, last_emit = b"", 0, 0, 0.0
        # zaseknutý ffmpeg (poškozené video) by blokoval read() navždy: watchdog ho po STALL_S bez
        # výstupu (mimo pozastavení strážcem) zabije a read() skončí
        watchdog = StallWatchdog(proc, paused=(gov.busy if self.governed else None))
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            watchdog.kick()
            buf += chunk
            while True:
                end = buf.find(_EOI)
                if end < 0:
                    break
                jpeg, buf = buf[:end + 2], buf[end + 2:]
                start = jpeg.find(b"\xff\xd8")
                if start < 0:
                    continue
                deadline = time.monotonic() + 2.0       # showinfo předbíhá kodér, jen pro jistotu
                while len(pts) <= index and time.monotonic() < deadline and not done_err.is_set():
                    time.sleep(0.002)
                if len(pts) > index and self.strip.add(round(pts[index] * 1000), jpeg[start:], min_gap_ms):
                    added += 1
                index += 1
                now = time.monotonic()
                if now - last_emit > 0.15:
                    last_emit = now
                    self.progress.emit(self.strip.source, len(self.strip))
                    if self.on_fraction is not None and self.strip.duration_ms and pts:
                        done, total = self._passes
                        part = min(1.0, pts[-1] * 1000.0 / self.strip.duration_ms)
                        self.on_fraction((done + part) / total)
            if not self._cancel and self.should_stop is not None and self.should_stop():
                self.cancel()                         # démon končí (zavírání aplikace)
            if self._cancel:
                break
        watchdog.stop()
        proc.wait()
        gov.unregister_process(proc.pid)
        self._proc = None
        if self._cancel:
            return None
        self.progress.emit(self.strip.source, len(self.strip))
        return added


class StripManager(QObject):
    """Pásy v RAM (pár naposledy použitých) + jeden generátor najednou, nejnovější požadavek vyhrává."""
    strip_progress = pyqtSignal(str, int)

    MEMORY_STRIPS = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self._strips = OrderedDict()
        self._builder = None
        self._queue = []

    def get(self, source):
        """Pás (třeba rozpracovaný) nebo None; nic nespouští."""
        strip = self._strips.get(source)
        if strip is not None:
            self._strips.move_to_end(source)
            return strip
        strip = ScrubStrip.load(source)
        if strip is not None:
            self._remember(strip)
        return strip

    def request(self, source, priority=True):
        """Zajistí pás pro video: z RAM / z disku hned, jinak vytvoření na pozadí.
        priority=False = odložený (prohlížeč hned po spuštění videa): čeká na klid a během
        přehrávání stojí; priority=True (tažení, náhled v mřížce) běží hned."""
        if not source or not os.path.exists(source):
            return None
        strip = self.get(source)
        if strip is not None and strip.complete:
            return strip
        if self._building(source):
            if priority:
                self._builder.prioritize()
            return self._builder.strip
        if self._builder is not None and self._builder.isRunning():
            self._builder.cancel()                    # jiné video – aktuální má přednost
            self._queue = [(s, p) for s, p in self._queue if s != source]
            self._queue.insert(0, (source, priority))
            return strip
        return self._start(source, priority)

    def _building(self, source):
        return self._builder is not None and self._builder.isRunning() and self._builder.strip.source == source

    def _start(self, source, priority=True):
        import perf_profile
        prof = perf_profile.current()
        strip = ScrubStrip(source)
        self._remember(strip)
        builder = StripBuilder(strip, self, governed=not priority, dense=prof.dense_strips,
                               threads=max(1, prof.bg_threads))
        builder.progress.connect(self.strip_progress)
        builder.finished_strip.connect(self._on_finished)
        self._builder = builder
        builder.start(QThread.Priority.LowPriority)
        return strip

    def _remember(self, strip):
        self._strips[strip.source] = strip
        self._strips.move_to_end(strip.source)
        while len(self._strips) > self.MEMORY_STRIPS:
            self._strips.popitem(last=False)

    def _on_finished(self, source, ok):
        builder, self._builder = self._builder, None
        if builder is not None:
            builder.deleteLater()
        if not ok and source in self._strips and not self._strips[source].complete:
            self._strips.pop(source, None)            # rozpracovaný (zrušený) pás nedržet
        if self._queue:
            self.request(*self._queue.pop(0))

    def shutdown(self, timeout_ms=3000):
        self._queue.clear()
        if self._builder is not None:
            self._builder.cancel()
            self._builder.wait(timeout_ms)


def build_strip_now(source, should_stop=None, dense=True, threads=1, progress=None):
    """Pás předem (démon, jeho vlákno): odložený, pozastavený při interakci. True = hotový.
    progress(podíl 0–1) průběžně z obou průchodů."""
    if not source or not os.path.exists(source) or has_valid_strip(source):
        return False
    builder = StripBuilder(ScrubStrip(source), governed=True, dense=dense, threads=threads)
    builder.should_stop = should_stop
    builder.on_fraction = progress
    done = []
    builder.finished_strip.connect(lambda _s, ok: done.append(ok), type=_direct())
    builder.run()                                     # synchronně v aktuálním vlákně
    return bool(done and done[0])


def has_valid_strip(source, path=None):
    """Totéž rozhodnutí jako ScrubStrip.load, ale čte jen hlavičku (démon prochází celou knihovnu
    videí při každém spuštění – načítat kvůli tomu megabajty JPEG by bylo zbytečné)."""
    path = path or strip_path(source)
    try:
        if os.path.getmtime(path) < os.path.getmtime(source):
            return False
        with open(path, "rb") as f:
            head = f.read(9)
            if len(head) < 9 or head[:4] != STRIP_MAGIC or head[4] != STRIP_VERSION:
                return False
            (hlen,) = struct.unpack("<I", head[5:9])
            header = json.loads(f.read(hlen).decode("utf-8"))
    except (OSError, ValueError):
        return False
    return int(header.get("edge") or STRIP_EDGE) >= edge_for(header.get("duration_ms", 0))


def _direct():
    from PyQt6.QtCore import Qt
    return Qt.ConnectionType.DirectConnection


_manager = None


def manager():
    """Jeden správce na proces (ne dítě prohlížeče: smazání prohlížeče by zničilo běžící
    QThread generátoru = pád). Ukončí se s aplikací (aboutToQuit) i při konci interpretu."""
    global _manager
    if _manager is None:
        import atexit
        from PyQt6.QtWidgets import QApplication
        _manager = StripManager()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(_manager.shutdown)
        atexit.register(lambda: _manager is not None and _manager.shutdown())
    return _manager
