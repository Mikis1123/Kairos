"""Throwback Reels – export do H.264 MP4 (Sprint 4).

Snímky kreslí stejný ReelRenderer jako přehrávač (reel_render.py), jdou rourou do ffmpeg
(rawvideo bgr0 -> libx264 High, yuv420p, CRF 21, +faststart = přehratelné hned při stahování,
sdílení do telefonu i na web). Zvuk: skladba sestřihu od začátku (čas videa = čas skladby, takže
střihy padnou na doby stejně jako v přehrávači), AAC 192 kb/s, doznění na konci.
Bez libx264 (jiná sestava ffmpeg) se použije kodér Windows Media Foundation (h264_mf).
ffmpeg běží v Job Objectu aplikace (governor) – s aplikací skončí.
"""
import os
import subprocess
import time

import paths

FPS = 30
SIZE = (1920, 1080)
CRF = 21                      # 60 s sestřih 1080p ≈ 50 MB (CRF 19: 83 MB, rozdíl na oko není)
AUDIO_FADE_S = 2.5
_encoder_cache = {}


def video_encoder(ffmpeg_path=None):
    """libx264, nebo h264_mf (Windows), nebo None."""
    ff = ffmpeg_path or paths.FFMPEG_PATH
    if ff not in _encoder_cache:
        try:
            out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except Exception:
            out = ""
        _encoder_cache[ff] = "libx264" if " libx264 " in out else "h264_mf" if " h264_mf " in out else None
    return _encoder_cache[ff]


def ffmpeg_command(out_path, total, music=None, fps=FPS, size=SIZE, encoder="libx264", ffmpeg_path=None):
    w, h = size
    cmd = [ffmpeg_path or paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "bgr0", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
    cmd += ["-map", "0:v"]
    if music:
        fade_at = max(0.0, total - AUDIO_FADE_S)
        cmd += ["-map", "1:a", "-af", f"afade=t=in:d=0.4,afade=t=out:st={fade_at:.3f}:d={AUDIO_FADE_S}",
                "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}"]
    if encoder == "libx264":
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(CRF), "-profile:v", "high",
                "-tune", "film"]
    else:
        cmd += ["-c:v", encoder, "-b:v", "12M"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-metadata", "comment=Kairos Throwback Reel", out_path]
    return cmd


class ExportCancelled(Exception):
    pass


def export(renderer, out_path, music=None, fps=FPS, size=SIZE, progress=None, should_stop=None,
           ffmpeg_path=None, governor=None):
    """Vykreslí celý sestřih do MP4. renderer = reel_render.ReelRenderer s načtenými obrázky.
    progress(hotovo 0–1). Vrací statistiku {frames, seconds, render_ms, encode_fps, mb}.
    Zrušení (should_stop) smaže rozpracovaný soubor a vyhodí ExportCancelled."""
    encoder = video_encoder(ffmpeg_path)
    if encoder is None:
        raise RuntimeError("ffmpeg neumí H.264")
    total = float(renderer.plan["total"])
    w, h = size
    n = max(1, int(round(total * fps)))
    tmp = out_path + ".part.mp4"
    proc = subprocess.Popen(ffmpeg_command(tmp, total, music, fps, size, encoder, ffmpeg_path),
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        import governor as gov_mod
        gov_mod.assign_to_job(proc.pid)
    except Exception:
        pass
    t0 = time.perf_counter()
    render_s = 0.0
    try:
        for k in range(n):
            if should_stop and should_stop():
                raise ExportCancelled()
            if governor is not None:
                governor.checkpoint(lambda: bool(should_stop and should_stop()))
            r0 = time.perf_counter()
            img = renderer.frame(k / fps, w, h)
            ptr = img.constBits()
            ptr.setsize(img.sizeInBytes())
            render_s += time.perf_counter() - r0
            proc.stdin.write(bytes(ptr))
            if progress and (k % 15 == 0 or k == n - 1):
                progress((k + 1) / n)
        proc.stdin.close()
        err = proc.stderr.read().decode("utf-8", "replace")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg: {err.strip()[-400:]}")
    except BaseException:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, out_path)
    secs = time.perf_counter() - t0
    return {"frames": n, "seconds": round(secs, 2), "render_ms": round(render_s / n * 1000, 2),
            "encode_fps": round(n / secs, 1), "realtime_x": round(total / secs, 2),
            "mb": round(os.path.getsize(out_path) / 1e6, 2), "encoder": encoder}


def default_name(plan):
    import re
    base = re.sub(r"[^\w\- ]+", "", plan.get("title") or "Sestrih", flags=re.UNICODE).strip() or "Sestrih"
    return f"{base[:60]}.mp4"
