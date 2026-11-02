"""1080p SDR proxy videí 4K / HDR (Fáze 1, adaptivní video engine).

Originál 4K HEVC Main 10 (HDR, Dolby Vision, 60 fps) se přehrává přímo hardwarově. Vše
ostatní – tažení časové osy (náhledový pás), náhledy v mřížce, Cesta a přehrávání na slabších
PC (perf_profile.proxy_playback) – pracuje s proxy: H.264 8 bit, delší strana ≤ 1920 px,
≤ 30 fps, HDR převedené na SDR (zscale + tonemap hable), zvuk AAC.

Proxy vytváří démon na pozadí, nikdy během přehrávání: ffmpeg je zaregistrovaný ve strážci
interakce (governor) a během přehrávání / gest je pozastavený. Změřeno (Ryzen AI 7 350,
libx264 veryfast): 9,3 s 4K60 HDR Dolby Vision za 12,6 s.

Soubory: PROXY_DIR/<otisk cesty>.mp4 (platný, je-li novější než originál), pro videa, která
proxy nepotřebují, prázdná značka <otisk>.none (aby se při každém startu znovu nesondovalo).
Rozpočet na disku: config „proxy_cache_mb“ (LRU podle posledního použití).
"""
from i18n import tr
import hashlib
import os
import re
import subprocess
import threading
import time

import paths

PROXY_EDGE = 1920
PROXY_FPS = 30
PROXY_CRF = 26
PROXY_MAXRATE = "6M"
DEFAULT_BUDGET_MB = 4096
_STREAM = re.compile(r"Stream #\d+:\d+[^:]*: Video: (?P<codec>\w+)(?P<rest>[^\n]*)")
_RES = re.compile(r"(\d{3,5})x(\d{3,5})")
_FPS = re.compile(r"([\d.]+) fps")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_OUT_TIME = re.compile(rb"out_time_(?:us|ms)=(\d+)")      # obojí jsou mikrosekundy (ffmpeg)
_lock = threading.Lock()


def _key(source):
    return hashlib.sha1(os.path.normcase(os.path.abspath(source)).encode("utf-8")).hexdigest()[:20]


def proxy_path(source):
    return os.path.join(paths.PROXY_DIR, _key(source) + ".mp4")


def _marker_path(source):
    return os.path.join(paths.PROXY_DIR, _key(source) + ".none")


def _fresh(path, source):
    try:
        return os.path.getsize(path) >= 0 and os.path.getmtime(path) >= os.path.getmtime(source)
    except OSError:
        return False


def get_proxy(source):
    """Cesta k platné proxy, nebo None (žádná / zastaralá / video ji nepotřebuje)."""
    if not source:
        return None
    p = proxy_path(source)
    if _fresh(p, source) and os.path.getsize(p) > 0:
        return p
    return None


def is_settled(source):
    """Proxy je hotová, nebo je známo, že ji video nepotřebuje."""
    return get_proxy(source) is not None or _fresh(_marker_path(source), source)


# ── sondování ──

def probe(source, ffmpeg_path=None):
    """{'width','height','fps','ten_bit','hdr'} z hlavičky (ffmpeg -i), nebo None."""
    ffmpeg_path = ffmpeg_path or paths.FFMPEG_PATH
    try:
        out = subprocess.run([ffmpeg_path, "-hide_banner", "-i", source], capture_output=True,
                             timeout=30, creationflags=_no_window()).stderr
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = out.decode("utf-8", "replace")
    m = _STREAM.search(text)
    if not m:
        return None
    rest = m.group("rest")
    res = _RES.search(rest)
    fps = _FPS.search(rest)
    w, h = (int(res.group(1)), int(res.group(2))) if res else (0, 0)
    d = _DURATION.search(text)
    duration_ms = int((int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3))) * 1000) if d else 0
    return {"width": w, "height": h, "fps": float(fps.group(1)) if fps else 0.0, "duration_ms": duration_ms,
            "ten_bit": bool(re.search(r"p010|10le|10be|Main 10", rest)),
            "hdr": bool(re.search(r"bt2020|arib-std-b67|smpte2084", rest)) or "DOVI" in text,
            "transfer": "smpte2084" if "smpte2084" in rest else "arib-std-b67"}   # PQ (HDR10) / HLG (iPhone)


def sdr_filter(info):
    """Filtr ffmpeg HDR -> SDR (BT.709) pro HDR video, jinak "". Sdílí proxy, miniatury i plakáty.

    Řadit až ZA zmenšení (čtvrtina pixelů). HDR -> SDR: lineární světlo, BT.709, tonemap hable.
    Barevné vlastnosti SNÍMKŮ výslovně (BT.2020, HLG / PQ): zscale bere vlastnosti snímku
    přednostně před svými volbami (tin=…), a u souborů se skromnějšími metadaty než iPhone
    končil „no path between colorspaces“. Ověřeno na iPhonu i na syntetickém HLG klipu."""
    if not info or not info.get("hdr"):
        return ""
    trc = info.get("transfer") or "arib-std-b67"
    return (f"setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc:range=tv,"
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")


def needs_proxy(info):
    """Proxy má smysl pro > 1080p, 10 bit nebo HDR (náročné dekódování / převod barev)."""
    if not info:
        return False
    return max(info["width"], info["height"]) > PROXY_EDGE or info["ten_bit"] or info["hdr"]


# ── stavba ──

def ffmpeg_cmd(source, target, info, threads=2, ffmpeg_path=None):
    edge = PROXY_EDGE
    vf = [f"scale=w='if(gt(iw,ih),min({edge},iw),-2)':h='if(gt(iw,ih),-2,min({edge},ih))':flags=bilinear"]
    if info and info.get("fps", 0) > PROXY_FPS + 1:
        vf.append(f"fps={PROXY_FPS}")
    if info and info.get("hdr"):
        vf.append(sdr_filter(info))
    vf.append("format=yuv420p")
    return [ffmpeg_path or paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", str(threads), "-hwaccel", "d3d11va", "-i", source,
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", ",".join(vf),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF),
            "-maxrate", PROXY_MAXRATE, "-bufsize", "12M", "-threads", str(threads),
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", target]


def build_proxy(source, governor=None, should_stop=None, threads=2, ffmpeg_path=None, progress=None):
    """Vytvoří proxy (nebo značku „nepotřebuje“). Vrací cestu k proxy, None, nebo False,
    když bylo zrušeno. Blokuje – volá se z vlákna na pozadí. progress(podíl 0–1) průběžně
    z ffmpeg -progress (převod 4K HDR trvá minuty – uživatel má vidět, že se něco děje)."""
    if not source or not os.path.exists(source):
        return None
    import locked_folder
    if locked_folder.is_hidden_path(source):
        return None                          # Skryté: žádná nešifrovaná kopie na disku
    if is_settled(source):
        return get_proxy(source)
    os.makedirs(paths.PROXY_DIR, exist_ok=True)
    info = probe(source, ffmpeg_path)
    if not needs_proxy(info):
        _touch(_marker_path(source))
        return None
    target = proxy_path(source)
    tmp = f"{target}.{threading.get_ident()}.tmp.mp4"
    cmd = ffmpeg_cmd(source, tmp, info, threads=threads, ffmpeg_path=ffmpeg_path)
    cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
    flags = _no_window() | (0x00004000 if os.name == "nt" else 0)   # BELOW_NORMAL_PRIORITY_CLASS
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
    except OSError:
        return None
    if governor is not None:
        governor.register_process(proc.pid)
    else:
        import governor as _gov
        _gov.assign_to_job(proc.pid)          # i bez strážce: ffmpeg nepřežije aplikaci
    done_us, err_tail = [0], []
    threading.Thread(target=_read_progress, args=(proc.stdout, done_us), daemon=True).start()
    # stderr se čte průběžně: plná roura (≈ 4 KB varování u vadného HDR souboru) zablokovala
    # ffmpeg navždy – převod „stál“ a démon s ním (Sprint R)
    err_reader = threading.Thread(target=_drain_tail, args=(proc.stderr, err_tail), daemon=True)
    err_reader.start()
    total_us = max(0, int(info.get("duration_ms") or 0)) * 1000
    cancelled = False
    try:
        while proc.poll() is None:
            if should_stop is not None and should_stop():
                cancelled = True
                proc.kill()
                break
            if progress is not None and total_us:
                progress(min(1.0, done_us[0] / total_us))
            time.sleep(0.2)
        proc.wait()
    finally:
        if governor is not None:
            governor.unregister_process(proc.pid)
    err_reader.join(2.0)
    if cancelled or proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _remove(tmp)
        if not cancelled:
            err = b"".join(err_tail).decode("utf-8", "replace").strip().splitlines()[-1:]
            print(f"[Proxy] {os.path.basename(source)}: ffmpeg {proc.returncode} {' '.join(err)}")
            _touch(_marker_path(source))      # nepřevoditelné: nezkoušet při každém startu
        return False if cancelled else None
    os.replace(tmp, target)
    return target


def _read_progress(stream, done_us):
    """Vlákno: čte ffmpeg -progress (klíč=hodnota po řádcích), drží zpracovaný čas (µs)."""
    try:
        for line in stream:
            m = _OUT_TIME.match(line)
            if m:
                done_us[0] = int(m.group(1))
    except (OSError, ValueError):
        pass


ERR_TAIL_LINES = 20


def _drain_tail(stream, tail):
    """Vlákno: vyprázdní rouru do konce, ponechá posledních ERR_TAIL_LINES řádků (hlášení chyby)."""
    try:
        for line in stream:
            tail.append(line)
            if len(tail) > ERR_TAIL_LINES:
                del tail[0]
    except (OSError, ValueError):
        pass


def sweep_temp(max_age_s=3600):
    """Úklid po pádu / tvrdém ukončení: polotovary *.tmp* v mezipaměti starší než max_age_s
    (běžící převod svůj soubor průběžně zapisuje – mladší se nechávají). Vrací počet smazaných."""
    removed, now = 0, time.time()
    for root in (paths.PROXY_DIR, paths.PREVIEW_DIR, paths.THUMB_DIR):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file() and ".tmp" in e.name and now - e.stat().st_mtime > max_age_s and _remove(e.path):
                    removed += 1
            except OSError:
                continue
    return removed


class PrepProgress:
    """Celkový průběh přípravy videí pro stavový řádek: vážený délkou videí (16min video není
    totéž co 3s klip), odhad zbývajícího času jen z času skutečné práce (pozastavení během
    přehrávání / gest se nepočítá) a jasné „čeká“, když je ffmpeg pozastavený.

    weights = délky čekajících videí (ms; 0 = neznámá -> průměr ostatních)."""
    MIN_ETA_S = 8.0                      # dřív je odhad jen šum
    MIN_ETA_FRAC = 0.03

    def __init__(self, weights, clock=time.monotonic):
        known = [w for w in weights if w > 0]
        mean = sum(known) / len(known) if known else 1.0
        self.weights = [w if w > 0 else mean for w in weights]
        self.total = sum(self.weights) or 1.0
        self._before = [sum(self.weights[:i]) for i in range(len(self.weights))]
        self._clock = clock
        self._last = None
        self.active_s = 0.0
        self.fraction = 0.0

    def update(self, index, item_fraction, paused=False):
        """Položka index je hotová z item_fraction (0–1). Vrací text stavového řádku."""
        now = self._clock()
        if self._last is not None and not paused:
            self.active_s += max(0.0, now - self._last)
        self._last = now
        index = max(0, min(len(self.weights) - 1, index)) if self.weights else 0
        done = (self._before[index] + self.weights[index] * max(0.0, min(1.0, item_fraction))) if self.weights else 0
        self.fraction = max(self.fraction, min(1.0, done / self.total))   # nikdy necouvá
        return self.text(index, paused)

    def eta_s(self):
        if self.active_s < self.MIN_ETA_S or self.fraction < self.MIN_ETA_FRAC:
            return None
        return self.active_s * (1.0 - self.fraction) / self.fraction

    def text(self, index, paused=False):
        n = len(self.weights)
        pct = int(self.fraction * 100)
        head = tr("proxy-paused") if paused else tr("proxy-preparing")
        parts = [head, tr("progress-of", index=index + 1, total=n), tr("percent", value=pct)]
        eta = None if paused else self.eta_s()
        if eta is not None:
            parts.append(_eta_text(eta))
        return " · ".join(parts)


def _eta_text(seconds):
    if seconds < 60:
        return tr("proxy-eta-under-minute")
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return tr("proxy-eta-minutes", minutes=minutes)
    return tr("proxy-eta-hours", hours=minutes // 60, minutes=format(minutes % 60, "02d"))


def cache_size():
    try:
        return sum(e.stat().st_size for e in os.scandir(paths.PROXY_DIR) if e.is_file())
    except OSError:
        return 0


def enforce_budget(budget_mb=DEFAULT_BUDGET_MB, keep=()):
    """Smaže nejdéle nepoužité proxy nad rozpočet (keep = cesty, které se mazat nesmí)."""
    budget = int(float(budget_mb) * 1024 * 1024)
    keep = {os.path.normcase(p) for p in keep}
    with _lock:
        try:
            entries = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(paths.PROXY_DIR)
                       if e.is_file() and e.name.endswith(".mp4")]
        except OSError:
            return 0
        total, freed = sum(s for _, s, _ in entries), 0
        for _, size, path in sorted(entries):
            if total <= budget:
                break
            if os.path.normcase(path) in keep:
                continue
            if _remove(path):
                total -= size
                freed += size
        return freed


def clear_cache():
    freed = cache_size()
    with _lock:
        try:
            for e in os.scandir(paths.PROXY_DIR):
                if e.is_file():
                    _remove(e.path)
        except OSError:
            return 0
    return freed - cache_size()


def touch(path):
    """LRU: proxy právě použitá (přehrávání / pás) se maže poslední."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def _touch(path):
    try:
        with open(path, "wb"):
            pass
    except OSError:
        pass


def _remove(path):
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _no_window():
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
