"""Deep-zoom & DPI precision torture (2026-11-06): one synthetic 48 MP original, the real viewer
canvas (viewer_hud.MediaCanvas + viewer_zoom.ZoomController), at each display scale factor.

    python eval/torture_deep_zoom.py                      # 1.0, 1.25, 1.5, 2.0 (one child each)
    python eval/torture_deep_zoom.py --scales 1.5 --mp 100

Per scale factor (a child process with QT_SCALE_FACTOR chosen so devicePixelRatio == target):
  fit     the fitted photo is drawn 1:1 into device pixels (pixmap size == drawn device rect,
          integer device origin): no second resample after the one Lanczos fit
  1:1     100 % zoom with the original's pyramid: rendered device pixels == source pixels
          (exact match share over the visible interior; off-grid sub-pixel offsets fail it)
  400 %   MAX_DEVICE_SCALE: 4x4 device blocks per source pixel, nearest (no bilinear mush)
  rubber  a drag 600 px past the left edge stays bounded (rubber band) and springs back to the
          clamp; the image ends exactly at the edge
  timing  canvas render at fit / 100 % / 400 % (median ms), full-res decode + pyramid time,
          pyramid RAM
Colour: the source is read through the app's own pipeline (display contract), so pixel tests
compare against the pyramid's level 0, not the file.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


def make_original(path, mp):
    import numpy as np
    from PIL import Image
    w = int(round((mp * 1e6 * 4 / 3) ** 0.5))
    h = int(round(w * 3 / 4))
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.empty((h, w, 3), np.uint8)
    a[..., 0] = (xx * 255 // w).astype(np.uint8)
    a[..., 1] = (yy * 255 // h).astype(np.uint8)
    a[..., 2] = (((xx ^ yy) & 1) * 255).astype(np.uint8)          # 1 px checker: any resample shows
    blocks = rng.integers(0, 256, (h // 16 + 1, w // 16 + 1, 1), np.uint8)
    a[..., 2] ^= np.repeat(np.repeat(blocks, 16, 0), 16, 1)[:h, :w, 0] & 0x7F
    Image.fromarray(a, "RGB").save(path, "PNG", compress_level=1)
    return w, h


def child(path, target, out_path):
    from PyQt6.QtCore import QEventLoop, QPointF, QSize, QTimer, QRect
    from PyQt6.QtGui import QImage, QPainter, QPixmap
    from PyQt6.QtWidgets import QApplication
    import numpy as np
    app = QApplication.instance() or QApplication(sys.argv)
    import color_manage
    import viewer_hud
    import viewer_zoom
    color_manage.configure()

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(int(ms), loop.quit)
        loop.exec()

    canvas = viewer_hud.MediaCanvas(with_video=False)
    canvas.resize(1280, 800)
    canvas.show()
    pump(150)
    dpr = canvas.devicePixelRatioF()
    vp = canvas.viewport()
    res = {"target": target, "dpr": dpr, "viewport": [vp.width(), vp.height()]}

    def render():
        # the viewport's real paint path at device resolution (QGraphicsView.render() adds its
        # own source -> target mapping and is not what the screen gets)
        return vp.grab().toImage()

    def timed(n=15):
        ts = []
        img = None
        for _ in range(n):
            t = time.perf_counter()
            img = render()
            ts.append((time.perf_counter() - t) * 1000)
        return round(statistics.median(ts), 2), img

    def arr(img):
        img = img.convertToFormat(QImage.Format.Format_RGB32)
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        a = np.frombuffer(bytes(ptr), np.uint8).reshape(img.height(), img.bytesPerLine() // 4, 4)
        return a[:, :img.width(), :3]

    size = viewer_zoom.original_size(path)
    res["original"] = list(size)
    canvas.zoom.set_photo(path, size)
    canvas.zoom.set_enabled(True)
    t = time.perf_counter()
    base = viewer_hud.load_image_file(path, QSize(round(vp.width() * dpr), round(vp.height() * dpr)),
                                      use_preview=False)
    res["base_decode_ms"] = round((time.perf_counter() - t) * 1000)
    canvas.set_image(QPixmap.fromImage(base))
    pump(50)

    # ── fit: drawn 1:1 ──
    item = canvas.photo_item
    pm = item.pixmap()
    pos = item.pos()
    dev_x, dev_y = pos.x() * dpr, pos.y() * dpr
    res["fit"] = {"pixmap": [pm.width(), pm.height()], "base": [base.width(), base.height()],
                  "origin_dev": [round(dev_x, 3), round(dev_y, 3)],
                  "integer_origin": abs(dev_x - round(dev_x)) < 1e-6 and abs(dev_y - round(dev_y)) < 1e-6,
                  "item_scale": item.scale(), "pixmap_dpr": pm.devicePixelRatio()}
    res["fit"]["render_ms"], fit_img = timed()
    a = arr(fit_img)
    b = arr(pm.toImage())
    ox, oy = round(dev_x), round(dev_y)
    h = min(b.shape[0], a.shape[0] - oy)
    w = min(b.shape[1], a.shape[1] - ox)
    res["fit"]["exact_share"] = round(float((a[oy:oy + h, ox:ox + w] == b[:h, :w]).all(-1).mean()), 4)

    # ── full-res pyramid ──
    got = {}
    t0 = time.perf_counter()

    def on_full():
        job = viewer_zoom.FullResJob(path, canvas.zoom.token)
        job.signals.done.connect(lambda p, tok, lv: (canvas.zoom.set_full_levels(tok, lv),
                                                    got.setdefault("ms", (time.perf_counter() - t0) * 1000)))
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(job)
    canvas.zoom.full_res_wanted.connect(on_full)
    center = QPointF(vp.width() / 2, vp.height() / 2)
    canvas.zoom.zoom_to(canvas.zoom.one_to_one_w(), center, animate=False)
    for _ in range(300):
        if "ms" in got:
            break
        pump(50)
    pump(50)
    levels = canvas.zoom._levels
    res["full_res_ms"] = round(got.get("ms", -1))
    res["pyramid"] = {"levels": [[lv.width(), lv.height()] for lv in levels],
                      "ram_mb": round(sum(lv.sizeInBytes() for lv in levels) / 2**20, 1)}

    def check_scale(name, w_target, block):
        canvas.zoom.zoom_to(w_target, center, animate=False)
        pump(30)
        z = canvas.zoom
        src = z.item.source()
        out = {"percent": round(z.percent(z.w), 2), "source_w": src.width() if src is not None else 0,
               "smooth": z.item.smooth}
        out["render_ms"], img = timed()
        a = arr(img)
        s = arr(src if isinstance(src, QImage) else src.toImage())
        scale = z.item.scale() * dpr                       # device px per source px
        px, py = z.item.pos().x() * dpr, z.item.pos().y() * dpr
        out["origin_dev"] = [round(px, 3), round(py, 3)]
        H, W = a.shape[:2]
        m = 8 * block
        ys, xs = np.mgrid[m:H - m, m:W - m]
        # device pixel centre -> source pixel (nearest at >= 2x, exact at 1x)
        sx = np.floor((xs + 0.5 - px) / scale).astype(int)
        sy = np.floor((ys + 0.5 - py) / scale).astype(int)
        ok = (sx >= 0) & (sy >= 0) & (sx < s.shape[1]) & (sy < s.shape[0])
        want = s[sy.clip(0, s.shape[0] - 1), sx.clip(0, s.shape[1] - 1)]
        got_px = a[m:H - m, m:W - m]
        match = (np.abs(got_px.astype(int) - want.astype(int)) <= 1).all(-1) & ok
        out["exact_share"] = round(float(match.sum() / max(1, ok.sum())), 4)
        return out

    res["zoom_100"] = check_scale("100", canvas.zoom.one_to_one_w(), 1)
    canvas.zoom.pan_by(37.37, -21.61)                      # a fractional logical pan (trackpad)
    pump(30)
    res["zoom_100_fractional_pan"] = check_scale("100p", canvas.zoom.one_to_one_w(), 1)
    res["zoom_50"] = check_scale("50", canvas.zoom.one_to_one_w() * 0.5, 1)
    res["zoom_400"] = check_scale("400", canvas.zoom.max_w(), 4)

    # ── rubber band at the left edge ──
    z = canvas.zoom
    z.zoom_to(z.max_w(), center, animate=False)
    pump(30)
    z.pan_by(1e7, 0)                                       # clamp: the image's left edge at the viewport's
    pump(30)
    res_left0 = z.item.pos().x() * dpr
    start = QPointF(vp.width() / 2, vp.height() / 2)
    z.press(start)
    peak = 0.0
    for i in range(1, 31):
        pump(8)
        z.move(QPointF(start.x() + 20 * i, start.y()))     # 600 px past the edge
        peak = max(peak, abs(z.overscroll()[0]))
    pump(120)                                              # finger rests: no fling velocity
    z.release(QPointF(start.x() + 600, start.y()))
    t = time.perf_counter()
    while (z.is_moving() or any(abs(o) > 0.01 for o in z._over)) and time.perf_counter() - t < 3:
        pump(16)
    settle = (time.perf_counter() - t) * 1000
    left_dev = z.item.pos().x() * dpr
    res["rubber"] = {"left_before_dev_px": round(res_left0, 3), "peak_overscroll_px": round(peak, 1), "bounded": peak < vp.width() / 2,
                     "settle_ms": round(settle), "over_after": [round(o, 3) for o in z._over],
                     "image_left_dev_px": round(left_dev, 3), "at_edge": abs(left_dev) < 0.51}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    canvas.close()


def native_dpr():
    out = subprocess.run([sys.executable, "-c",
                          "from PyQt6.QtWidgets import QApplication;a=QApplication([]);"
                          "print(a.primaryScreen().devicePixelRatio())"], capture_output=True, text=True)
    return float(out.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", default="1.0,1.25,1.5,2.0")
    ap.add_argument("--mp", type=float, default=48)
    ap.add_argument("--json", default="")
    ap.add_argument("--_child", nargs=3, help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._child:
        child(args._child[0], float(args._child[1]), args._child[2])
        return 0
    tmp = tempfile.mkdtemp(prefix="kairos_zoom_")
    src = os.path.join(tmp, "big.png")
    t = time.perf_counter()
    w, h = make_original(src, args.mp)
    print(f"original {w}x{h} ({w * h / 1e6:.1f} MP) in {time.perf_counter() - t:.1f} s", flush=True)
    nat = native_dpr()
    print(f"native DPR {nat}", flush=True)
    results = []
    for target in [float(s) for s in args.scales.split(",")]:
        env = dict(os.environ, QT_SCALE_FACTOR=f"{target / nat:.6f}",
                   QT_SCALE_FACTOR_ROUNDING_POLICY="PassThrough")
        out = os.path.join(tmp, f"r_{target}.json")
        subprocess.run([sys.executable, os.path.abspath(__file__), "--_child", src, str(target), out],
                       env=env, timeout=600)
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                r = json.load(f)
            results.append(r)
            print(json.dumps(r), flush=True)
        else:
            print(f"scale {target}: child failed", flush=True)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=1)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
