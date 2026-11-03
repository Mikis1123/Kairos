"""Live Photo framing: where does the still sit inside the motion video?

    python eval/bench_live_framing.py            # 12 random Live Photos from the library
    python eval/bench_live_framing.py --n 40

For each Live Photo, this decodes the motion video's FULL coded frame at the still moment
(ffmpeg -apply_cropping codec). It then template-matches the still (EXIF-rotated, greyscale)
into that frame at scales 0.86-1.00 of the frame. The best scale says which framing the
still has:
  ~1.00   the full coded frame (what Qt Multimedia plays)
  ~0.908  the clean aperture (`clap` 1744x1308 of 1920x1440: what ffmpeg shows by default)
Read-only: the library is only read, and frames go to a temp dir.
"""
import argparse
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import paths  # noqa: E402
import video_clap  # noqa: E402

STILL_MOMENT_S = 1.4          # iPhone: the still is ~1.4-1.5 s into the 3 s clip


def match(still_path, video_path, tmp):
    import cv2
    import numpy as np
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    frame_png = os.path.join(tmp, "frame.png")
    subprocess.run([paths.FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-ss", str(STILL_MOMENT_S),
                    *video_clap.ffmpeg_full_frame_args(paths.FFMPEG_PATH), "-i", video_path,
                    "-frames:v", "1", "-y", frame_png], check=True, timeout=60)
    full = np.asarray(Image.open(frame_png).convert("L"), dtype=np.float32)
    st = ImageOps.exif_transpose(Image.open(still_path)).convert("L")
    h, w = full.shape
    if (st.width > st.height) != (w > h):
        st = st.rotate(90, expand=True)
    best = (-1.0, 0.0)
    for sc in np.arange(0.86, 1.0001, 0.004):
        t = np.asarray(st.resize((int(w * sc), int(h * sc)), Image.BILINEAR), dtype=np.float32)
        r = cv2.matchTemplate(full, t, cv2.TM_CCOEFF_NORMED)
        mx = float(r.max())
        if mx > best[0]:
            best = (mx, float(sc))
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("SELECT file_path, paired_video_path FROM media_items WHERE media_type = 'live_photo'"
                        " AND paired_video_path IS NOT NULL").fetchall()
    rows = [r for r in rows if os.path.exists(r[0]) and os.path.exists(r[1])]
    random.Random(args.seed).shuffle(rows)
    scales, clean = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for still, video in rows[:args.n]:
            crop = video_clap.parse(video)
            try:
                ncc, sc = match(still, video, tmp)
            except Exception as e:                 # unreadable pair: report and go on
                print(f"{os.path.basename(still)}: {e!r}")
                continue
            cw = crop.w if crop else 1.0
            clean.append(cw)
            if ncc >= 0.6:                         # a confident match only (motion can break it)
                scales.append(sc)
            print(f"{os.path.basename(still):>16}  still scale {sc:.3f}  ncc {ncc:.3f}  "
                  f"clean aperture {cw:.3f}  {crop!r}")
    if scales:
        print(f"\nstill / full frame: median {statistics.median(scales):.3f} "
              f"(min {min(scales):.3f}, max {max(scales):.3f}) over {len(scales)} confident matches")
        print(f"clean aperture / full frame: {statistics.median(clean):.3f}")
        verdict = "FULL FRAME" if abs(statistics.median(scales) - 1) < abs(statistics.median(scales) -
                                                                            statistics.median(clean)) else "CLEAN APERTURE"
        print(f"the still matches the {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
