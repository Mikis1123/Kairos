"""Střední vrstva obrazové cache: náhledy ~2048 px (Patch 11 / dávka B).

Tři úrovně jako v Apple Photos: miniatura 360 px (mřížka) -> náhled 2048 px (prohlížeč,
duplicity, události, prezentace) -> originál. Dekódování 12 Mpx HEIC trvá ~300 ms, JPEG
náhledu ~20–30 ms – prohlížení je pak „okamžitě ostré“.

* Klíč = otisk cesty originálu; náhled je platný jen když je novější než originál
  (upravená fotka nikdy neukáže starý obraz).
* LRU podle času posledního použití (mtime náhledu se při použití posune) a pevný rozpočet
  na disku (výchozí 2 GB, config „preview_cache_mb“); nejdéle nepoužité se mažou.
* Videa: plakát = snímek z videa v rozlišení náhledu (ffmpeg), místo rozmazané miniatury.
* Vše bezpečné mimo GUI vlákno (QImage, soubory, subprocess).
"""
import hashlib
import os
import subprocess
import threading
import time

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QImage

import fs_atomic
import paths

# Stage 2: náhled = delší strana displeje ve fyzických px (configure_for_display), takže je to
# rovnou výsledný obraz prohlížeče – žádné zvětšené 2048 px a „doostření“ o půl sekundy později.
PREVIEW_EDGE = 2880
PREVIEW_MIN_EDGE, PREVIEW_MAX_EDGE = 2048, 3840
PREVIEW_QUALITY = 90             # 4:4:4 (imaging.encode_jpeg)
PREVIEW_VERSION = 4              # 2 = rozměr displeje + profil displeje (color_manage)
                                 # 3 = HEIC bez dvojího převodu + tón Live Photo (live_tone)
                                 # 4 = imaging pipeline: Lanczos3, 4:4:4, kontrakt Windows, bez tónu
DEFAULT_BUDGET_MB = 2048
TOUCH_INTERVAL_S = 3600          # mtime posouvat nejvýš jednou za hodinu (méně zápisů na disk)
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}

_lock = threading.Lock()


def _is_hidden(source):
    """Originál ve složce Skryté (locked_folder): žádné náhledy na disku."""
    try:
        import locked_folder
    except ImportError:          # pragma: no cover
        return False
    return locked_folder.is_hidden_path(source)


def preview_dir():
    return paths.PREVIEW_DIR


def configure_for_display(long_edge_px):
    """Velikost náhledu podle delší strany displeje (fyzické px), v mezích 2048–3840."""
    global PREVIEW_EDGE
    PREVIEW_EDGE = int(max(PREVIEW_MIN_EDGE, min(PREVIEW_MAX_EDGE, int(long_edge_px or 0) or PREVIEW_EDGE)))
    return PREVIEW_EDGE


def base_path(source):
    """Společný základ názvů v cache pro originál (náhled, pás časové osy)."""
    key = hashlib.sha1(os.path.normcase(os.path.abspath(source)).encode("utf-8")).hexdigest()[:20]
    return os.path.join(preview_dir(), key)


def version_suffix():
    import color_manage
    return f"p{PREVIEW_VERSION}{color_manage.target_fingerprint()}"


def preview_path(source):
    return f"{base_path(source)}.{version_suffix()}.jpg"


def sweep_legacy():
    """Smaže náhledy jiné verze / jiného profilu displeje (pásy .scrub nechá). Vrací počet."""
    import re
    keep = re.compile(r"^[0-9a-f]{20}\." + re.escape(version_suffix()) + r"\.jpg$")
    removed = 0
    with _lock:
        try:
            for e in os.scandir(preview_dir()):
                if e.is_file() and e.name.endswith(".jpg") and ".tmp" not in e.name \
                        and not keep.match(e.name):
                    try:
                        os.remove(e.path)
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            return 0
    return removed


def get_preview(source, touch=True):
    """Cesta k platnému náhledu, nebo None. Platný = existuje a je novější než originál."""
    if not source:
        return None
    p = preview_path(source)
    try:
        st = os.stat(p)
        src_mtime = os.path.getmtime(source)
    except OSError:
        return None
    if st.st_size <= 0 or st.st_mtime < src_mtime:
        return None
    if touch and time.time() - st.st_mtime > TOUCH_INTERVAL_S:
        try:
            os.utime(p, None)                # LRU: naposledy použito
        except OSError:
            pass
    return p


def save_preview(source, image):
    """Uloží (zmenšený) QImage jako náhled originálu. Atomicky (tmp + replace)."""
    if image is None or image.isNull() or not source:
        return None
    if _is_hidden(source):                   # Skryté: na disk nikdy nešifrovaně
        return None
    import imaging
    image = imaging.fit_qimage(image, PREVIEW_EDGE, PREVIEW_EDGE)
    os.makedirs(preview_dir(), exist_ok=True)
    target = preview_path(source)
    return target if imaging.write_jpeg(imaging.qimage_to_pil(image), target, PREVIEW_QUALITY) else None


def _video_poster(source, target, ffmpeg_path=None):
    ffmpeg_path = ffmpeg_path or paths.FFMPEG_PATH
    if not os.path.exists(ffmpeg_path):
        return False
    tmp = fs_atomic.unique_tmp(target)
    edge = PREVIEW_EDGE
    # HDR (HLG / Dolby Vision z iPhonu) -> SDR jako proxy; bez toho vybledlý plakát. Profil
    # displeje se na plakát NEuplatní: musí barevně navazovat na (nespravované) video.
    try:
        import video_proxy
        sdr = video_proxy.sdr_filter(video_proxy.probe(source, ffmpeg_path))
    except Exception:
        sdr = ""
    vf = f"scale='if(gt(iw,ih),min({edge},iw),-2)':'if(gt(iw,ih),-2,min({edge},ih))':flags=lanczos+accurate_rnd" + (
        "," + sdr if sdr else "") + ",format=yuvj444p"
    cmd = [ffmpeg_path, "-loglevel", "quiet", "-y", "-ss", "00:00:00.200", "-i", source,
           "-map", "0:v:0", "-frames:v", "1",
           "-vf", vf,
           "-q:v", "2", tmp]
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        subprocess.run(cmd, capture_output=True, startupinfo=startupinfo, timeout=60)
        if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            return fs_atomic.replace(tmp, target)
    except Exception:
        pass
    try:
        os.remove(tmp)
    except OSError:
        pass
    return False


def build_preview(source, media_type=None):
    """Vytvoří náhled (fotka) nebo plakát (video). Vrací cestu nebo None. Existující platný
    náhled se nepřepočítává."""
    if not source or not os.path.exists(source) or _is_hidden(source):
        return None
    existing = get_preview(source, touch=False)
    if existing:
        return existing
    is_video = media_type == "video" or os.path.splitext(source)[1].lower() in VIDEO_EXTS
    if is_video:
        os.makedirs(preview_dir(), exist_ok=True)
        target = preview_path(source)
        return target if _video_poster(source, target) else None
    import imaging                                  # nativní pipeline: přímo PIL -> JPEG
    try:
        edge = PREVIEW_EDGE
        img = imaging.render_still(source, lambda w, h: imaging.fit_box(w, h, edge, edge), need_long=edge)
        target = preview_path(source)
        return target if imaging.write_jpeg(img, target, PREVIEW_QUALITY) else None
    except Exception:
        pass
    from viewer_hud import load_image_file          # formáty mimo Pillow (QImageReader)
    img = load_image_file(source, QSize(PREVIEW_EDGE, PREVIEW_EDGE), use_preview=False)
    return save_preview(source, img)


def cache_size():
    total = 0
    try:
        with os.scandir(preview_dir()) as it:
            for e in it:
                if e.is_file():
                    total += e.stat().st_size
    except OSError:
        return 0
    return total


def enforce_budget(budget_mb=DEFAULT_BUDGET_MB):
    """Smaže nejdéle nepoužité náhledy, dokud cache nepřesahuje rozpočet. Vrací uvolněné bajty."""
    budget = int(float(budget_mb) * 1024 * 1024)
    with _lock:
        try:
            entries = [(e.stat().st_mtime, e.stat().st_size, e.path)
                       for e in os.scandir(preview_dir()) if e.is_file()]
        except OSError:
            return 0
        total = sum(s for _, s, _ in entries)
        freed = 0
        for _, size, path in sorted(entries):          # nejstarší použití první
            if total <= budget:
                break
            try:
                os.remove(path)
                total -= size
                freed += size
            except OSError:
                pass
        return freed


def clear_cache():
    """Smaže všechny náhledy (Nastavení › Pokročilé). Vrací uvolněné bajty."""
    freed = cache_size()
    with _lock:
        try:
            for e in os.scandir(preview_dir()):
                if e.is_file():
                    try:
                        os.remove(e.path)
                    except OSError:
                        pass
        except OSError:
            return 0
    return freed - cache_size()
