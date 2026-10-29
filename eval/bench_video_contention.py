"""Fáze 0 – měření: soupeření dekodérů při přehrávání 4K HDR videa.

    python eval/bench_video_contention.py [cesta-k-videu]

Rezerva dekódování = rychlost hardwarového dekódování (D3D11VA) vůči reálnému času; přehrávání
potřebuje ≥ 1,0×. Měří se:
  1. samotné video (jako přehrávač),
  2. video + souběžný hustý průchod náhledového pásu Z ORIGINÁLU (dřívější chování 0,7 s po startu),
  3. video + souběžný pás z 1080p proxy (nové chování – a navíc se během přehrávání pozastaví).
Čte jen zadané video; proxy a výstupy jdou do dočasné složky.
"""
import os
import re
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402
import scrub_strip  # noqa: E402
import video_proxy  # noqa: E402

FF = paths.FFMPEG_PATH
_SPEED = re.compile(r"speed=\s*([\d.]+)x")


def decode_speed(path, seconds=8):
    """Rychlost dekódování jako přehrávač (D3D11VA, bez výstupu) – násobek reálného času."""
    r = subprocess.run([FF, "-hide_banner", "-nostdin", "-hwaccel", "d3d11va", "-t", str(seconds), "-i", path,
                        "-map", "0:v:0", "-f", "null", "-"], capture_output=True)
    found = _SPEED.findall(r.stderr.decode("utf-8", "replace"))
    return float(found[-1]) if found else 0.0


def strip_load(source, stop):
    """Hustý průchod pásu (fps 12, 480 px, MJPEG) dokola, dokud běží měření."""
    cmd = scrub_strip._ffmpeg_cmd(source, keyframes_only=False, hw=True)
    while not stop.is_set():
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while p.poll() is None and not stop.is_set():
            stop.wait(0.1)
        if p.poll() is None:
            p.kill()
        p.wait()


def with_load(video, load_source):
    stop = threading.Event()
    t = threading.Thread(target=strip_load, args=(load_source, stop), daemon=True)
    t.start()
    try:
        return decode_speed(video)
    finally:
        stop.set()
        t.join(5)


def main():
    video = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\mikip\Documents\fotky\výlet\IMG_1382.MOV"
    info = video_proxy.probe(video)
    print(f"Video: {os.path.basename(video)}  {info}")
    tmp = tempfile.mkdtemp(prefix="kairos_bench_")
    paths.PROXY_DIR = os.path.join(tmp, "proxies")
    proxy = video_proxy.build_proxy(video, threads=2)
    alone = decode_speed(video)
    old = with_load(video, video)
    new = with_load(video, proxy) if proxy else None
    print(f"  samotné přehrávání                    {alone:5.2f}× reálného času")
    print(f"  + pás z originálu 4K (dřív)           {old:5.2f}×")
    if new is not None:
        print(f"  + pás z 1080p proxy (teď, bez pauzy)  {new:5.2f}×")
    print("  (přehrávání potřebuje ≥ 1,00×; teď se pás během přehrávání navíc pozastaví – governor)")


if __name__ == "__main__":
    main()
