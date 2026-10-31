"""Pohyb v kartách Cesty (Cesta 3.0): Live Photo se rozpohybuje, video ukáže 3 s bez zvuku.

Stránka mapy (QtWebEngine z PyPI) umí jen VP8 / VP9 / AV1 – H.264 ani HEVC z iPhonu přímo
nepřehraje (ověřeno canPlayType). Proto se z originálu (nebo 1080p proxy) vyřízne krátký
klip WebM VP9: 3 s, delší strana HIGHLIGHT_EDGE, bez zvuku, HDR převedené do SDR stejným
řetězem jako proxy. Live Photo = celý párový klip (≈ 1,5–3 s), video = HIGHLIGHT_S kolem
nejreprezentativnějšího snímku (vektor nejbližší průměru snímků z vícesnímkového indexu,
video_frames.py) – ne roztřesený začátek.

Soubory: HIGHLIGHT_DIR/<otisk cesty>.webm (platný, je-li novější než originál).
Jen vysoký výkonový profil (rozhodnutí 2026-10-30). Kódování: realtime VP9, 2 vlákna,
nízká priorita – klipy vznikají i za běhu Cesty (karta je dostane, jakmile jsou hotové).
"""
import hashlib
import os
import subprocess
import threading

import paths
import video_proxy

HIGHLIGHT_S = 3.0
HIGHLIGHT_EDGE = 960            # karta je ~420 px široká (× DPR 2)
HIGHLIGHT_FPS = 30
HIGHLIGHT_CRF = 34
BUILD_TIMEOUT_S = 60
KINDS = ("live_photo", "video")


def highlight_dir():
    return paths.HIGHLIGHT_DIR                  # čteno za běhu (testy ho přesměrují)


def highlight_path(source):
    key = os.path.normcase(os.path.abspath(source))
    return os.path.join(highlight_dir(), hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".webm")


def get_highlight(source):
    p = highlight_path(source)
    try:
        if os.path.getsize(p) > 0 and os.path.getmtime(p) >= os.path.getmtime(source):
            return p
    except OSError:
        pass
    return None


def representative_ms(frames):
    """frames = [(čas ms, vektor numpy)] -> čas snímku nejbližšího průměru (kosinus), nebo None."""
    import numpy as np
    vecs = [(ms, v) for ms, v in frames if v is not None and getattr(v, "size", 0)]
    if not vecs:
        return None
    m = np.stack([v / (np.linalg.norm(v) or 1.0) for _, v in vecs])
    mean = m.mean(axis=0)
    return int(vecs[int(np.argmax(m @ mean))][0])


def highlight_start(duration_ms, centre_ms=None, length_s=HIGHLIGHT_S):
    """Začátek výřezu (ms): kolem centre_ms, jinak ve 30 % délky; vždy celý uvnitř videa."""
    d, span = max(0, int(duration_ms or 0)), int(length_s * 1000)
    if d <= span:
        return 0
    c = centre_ms if centre_ms is not None else int(d * 0.3)
    return int(max(0, min(d - span, c - span // 2)))


def _frame_vectors(conn, media_id):
    import numpy as np
    try:
        rows = conn.execute("SELECT frame_ms, embedding FROM video_frame_embeddings "
                            "WHERE media_id = ? AND frame_ms >= 0", (int(media_id),)).fetchall()
    except Exception:
        return []
    return [(ms, np.frombuffer(emb, dtype=np.float32)) for ms, emb in rows if emb]


def plan(conn, data):
    """Klipy pro nejlepší snímky zastávek Cesty: [{stop, id, kind, source, centre_ms, path, ready}].
    data = výsledek journey.build_journey (best má id a type). Jen dotazy do DB – sondování
    videa (ffmpeg) a výběr výřezu dělá až build() ve vlákně."""
    out = []
    for i, stop in enumerate(data.get("stops") or []):
        best = stop.get("best") or {}
        if best.get("type") not in KINDS:
            continue
        row = conn.execute("SELECT file_path, paired_video_path FROM media_items WHERE id = ?",
                           (int(best["id"]),)).fetchone()
        if not row:
            continue
        source = row[1] if best["type"] == "live_photo" else row[0]
        if not source or not os.path.exists(source):
            continue
        centre = representative_ms(_frame_vectors(conn, best["id"])) if best["type"] == "video" else None
        ready = get_highlight(source)
        out.append({"stop": i, "id": int(best["id"]), "kind": best["type"], "source": source, "centre_ms": centre,
                    "path": ready or highlight_path(source), "ready": ready is not None})
    return out


def ffmpeg_cmd(source, target, start_ms, info, threads=2, ffmpeg_path=None):
    edge = HIGHLIGHT_EDGE
    vf = [f"scale=w='if(gt(iw,ih),min({edge},iw),-2)':h='if(gt(iw,ih),-2,min({edge},ih))':flags=bicubic"]
    if info and info.get("fps", 0) > HIGHLIGHT_FPS + 1:
        vf.append(f"fps={HIGHLIGHT_FPS}")
    if info and info.get("hdr"):
        trc = info.get("transfer") or "arib-std-b67"
        vf.append(f"setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc:range=tv,"
                  "zscale=t=linear:npl=100,format=gbrpf32le,"
                  "zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
    vf.append("format=yuv420p")
    return [ffmpeg_path or paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{start_ms / 1000:.3f}", "-i", source, "-t", f"{HIGHLIGHT_S:.2f}",
            "-map", "0:v:0", "-an", "-vf", ",".join(vf),
            "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1",
            "-crf", str(HIGHLIGHT_CRF), "-b:v", "0", "-threads", str(threads), "-f", "webm", target]


def build(item, should_stop=None, threads=2, ffmpeg_path=None):
    """Vyrobí klip pro položku z plan(): Live Photo celý (≤ 3 s), video HIGHLIGHT_S kolem
    centre_ms (jinak ve 30 % délky). Vrací cestu, nebo None (chyba / zrušeno)."""
    source = item["source"]
    import locked_folder
    if locked_folder.is_hidden_path(source):
        return None                          # Skryté: žádný klip na disku
    done = get_highlight(source)
    if done:
        return done
    if not os.path.exists(ffmpeg_path or paths.FFMPEG_PATH):
        return None
    os.makedirs(highlight_dir(), exist_ok=True)
    target = highlight_path(source)
    tmp = f"{target}.{threading.get_ident()}.tmp.webm"
    # zdroj: proxy (1080p SDR) je-li hotová, jinak originál s převodem HDR
    proxy = video_proxy.get_proxy(source)
    src = proxy or source
    info = video_proxy.probe(src, ffmpeg_path) or {}
    if proxy:
        info = dict(info, hdr=False)
    start = 0 if item.get("kind") == "live_photo" else highlight_start(info.get("duration_ms", 0), item.get("centre_ms"))
    flags = video_proxy._no_window() | (0x00004000 if os.name == "nt" else 0)   # BELOW_NORMAL
    try:
        proc = subprocess.Popen(ffmpeg_cmd(src, tmp, start, info, threads, ffmpeg_path),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, creationflags=flags)
    except OSError:
        return None
    import governor as _gov
    _gov.assign_to_job(proc.pid)                # ffmpeg nepřežije aplikaci
    waited = 0.0
    while proc.poll() is None:
        if (should_stop is not None and should_stop()) or waited > BUILD_TIMEOUT_S:
            proc.kill()
            proc.wait()
            _remove(tmp)
            return None
        try:
            proc.wait(0.1)
        except subprocess.TimeoutExpired:
            waited += 0.1
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        _remove(tmp)
        return None
    try:
        os.replace(tmp, target)
    except OSError:
        _remove(tmp)
        return None
    return target


class ClipWorker(threading.Thread):
    """Postupně vyrobí chybějící klipy jedné Cesty (v pořadí zastávek) a každý hotový ohlásí
    on_ready(položka, cesta) – z vlákna na pozadí, příjemce si ho předá do GUI sám."""

    def __init__(self, items, on_ready, threads=2):
        super().__init__(name="cesta-clips", daemon=True)
        self.items = [it for it in items if not it.get("ready")]
        self.on_ready = on_ready
        self.threads = threads
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        for item in self.items:
            if self._stop.is_set():
                return
            path = build(item, should_stop=self._stop.is_set, threads=self.threads)
            if path and not self._stop.is_set():
                try:
                    self.on_ready(item, path)
                except RuntimeError:
                    return                      # příjemce (okno) už neexistuje


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass
