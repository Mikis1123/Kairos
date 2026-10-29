"""Snímky z celého videa pro chytré hledání (vícesnímkový index).

Video mělo v hledání jediný vektor – z plakátu (první snímek). Klip, kde se plakát točí na
chodník a za deset sekund je vodopád, se na „vodopád“ nenašel. Teď se z videa vezme
FRAMES_MAX snímků rozprostřených přes celou délku (bez krajních – rozjezd a zastavení
kamery bývají rozmazané), každý dostane vlastní CLIP vektor a video se v hledání hodnotí
nejlepším z nich (ai_engine.rank_semantic).

Zdroj: 1080p SDR proxy, existuje-li (video_proxy) – jinak originál, u HDR s převodem do SDR
(stejný řetěz zscale + tonemap jako proxy: nepřevedené HLG / PQ snímky jsou vybledlé a CLIP
je pak hodnotí hůř). Rychlé hledání pozice (-ss před -i) = klíčový snímek, ne přesný čas –
pro index obsahu stačí a 4K HEVC se nedekóduje od začátku.
"""
import io
import os
import subprocess

import paths
import video_proxy

FRAMES_MAX = 6
FRAMES_SHORT = 2                 # klip do SHORT_MS: dva snímky (víc by byly skoro stejné)
SHORT_MS = 2500
EDGE = 448                       # CLIP pracuje s 224 px; rezerva na výřez středu
EDGE_TRIM = 0.08                 # vynechat prvních a posledních 8 % délky


def frame_times(duration_ms):
    """Časy snímků (ms) rozprostřené přes video bez krajních EDGE_TRIM."""
    d = int(duration_ms or 0)
    if d <= 0:
        return [0]
    n = FRAMES_SHORT if d <= SHORT_MS else FRAMES_MAX
    lo, hi = d * EDGE_TRIM, d * (1.0 - EDGE_TRIM)
    if n == 1:
        return [int((lo + hi) / 2)]
    return [int(lo + (hi - lo) * i / (n - 1)) for i in range(n)]


def _vf(info, from_proxy):
    scale = f"scale='if(gt(iw,ih),{EDGE},-2)':'if(gt(iw,ih),-2,{EDGE})':flags=area"
    if from_proxy or not (info and info.get("hdr")):
        return scale + ",format=yuvj420p"
    trc = info.get("transfer") or "arib-std-b67"
    return (scale + f",setparams=color_primaries=bt2020:color_trc={trc}:colorspace=bt2020nc:range=tv,"
            "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=t=bt709:m=bt709:r=tv,format=yuvj420p")


def extract_frames(source, should_stop=None):
    """[(čas ms, PIL.Image RGB), ...] z celého videa; [] když to nejde (žádný ffmpeg, vadný soubor)."""
    if not source or not os.path.exists(source) or not os.path.exists(paths.FFMPEG_PATH):
        return []
    from PIL import Image
    proxy = video_proxy.get_proxy(source)
    src = proxy or source
    info = video_proxy.probe(src)
    if not info:
        return []
    flags = subprocess.CREATE_NO_WINDOW | 0x00004000 if os.name == "nt" else 0   # BELOW_NORMAL
    out = []
    for ms in frame_times(info.get("duration_ms", 0)):
        if should_stop is not None and should_stop():
            return []
        cmd = [paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", f"{ms / 1000:.3f}", "-i", src, "-map", "0:v:0", "-frames:v", "1",
               "-vf", _vf(info, bool(proxy)), "-c:v", "mjpeg", "-q:v", "3", "-f", "image2pipe", "-"]
        try:
            data = subprocess.run(cmd, capture_output=True, timeout=60, creationflags=flags).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not data:
            continue
        try:
            with Image.open(io.BytesIO(data)) as img:
                out.append((ms, img.convert("RGB")))
        except OSError:
            continue
    return out
