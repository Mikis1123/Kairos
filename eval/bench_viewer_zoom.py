"""Stage 2: měření přiblížení v prohlížeči na skutečné fotce (MediaCanvas, DPR displeje).

Spuštění:  python eval/bench_viewer_zoom.py [cesta_k_fotce]
Bez argumentu vezme největší fotku z knihovny (photos.db). Měří:
  * doba vykreslení snímku plátna ve vešlém stavu, při 100 % a 200 % (render do obrazu
    velikosti plochy ve fyzických px = stejná práce jako paintEvent viewportu),
  * průběh animovaného zoomu kolečkem a setrvačného posunu (doby snímků z frame callbacku),
  * příchod originálu (dekódování + pyramida) a ostrost: rozptyl Laplaciánu výřezu 100 %
    z náhledu displeje vs. z originálu.
"""
import os
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QPointF, QPoint, Qt, QRect, QRectF, QEvent
from PyQt6.QtGui import QImage, QPainter, QPixmap, QWheelEvent, QColor
from PyQt6.QtWidgets import QApplication


def pick_photo():
    import database
    c = sqlite3.connect(database.DB_PATH)
    rows = c.execute("SELECT file_path FROM media_items WHERE media_type IN ('photo','live_photo') "
                     "ORDER BY file_size DESC LIMIT 1").fetchall()
    return rows[0][0]


def render_ms(canvas, n=30):
    vp = canvas.viewport()
    dpr = canvas.devicePixelRatioF()
    img = QImage(round(vp.width() * dpr), round(vp.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(dpr)
    times = []
    for _ in range(n):
        t = time.perf_counter()
        p = QPainter(img)
        canvas.render(p)
        p.end()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times), max(times), img


def sharpness(img, rect):
    import numpy as np
    import cv2
    sub = img.copy(rect).convertToFormat(QImage.Format.Format_Grayscale8)
    ptr = sub.constBits()
    ptr.setsize(sub.sizeInBytes())
    a = np.frombuffer(bytes(ptr), np.uint8).reshape(sub.height(), sub.bytesPerLine())[:, :sub.width()]
    return float(cv2.Laplacian(a, cv2.CV_64F).var())


def wheel(canvas, pos, dy):
    vp = canvas.viewport()
    ev = QWheelEvent(QPointF(pos), QPointF(vp.mapToGlobal(pos)), QPoint(0, 0), QPoint(0, dy),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(vp, ev)


def pump(ms):
    """Skutečná smyčka událostí (jako v aplikaci) – ne processEvents se sleep, který časovače zdržuje."""
    from PyQt6.QtCore import QEventLoop, QTimer
    loop = QEventLoop()
    QTimer.singleShot(int(ms), loop.quit)
    loop.exec()


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    import color_manage
    import preview_cache
    import viewer_hud
    import viewer_zoom
    color_manage.configure()
    scr = app.primaryScreen()
    preview_cache.configure_for_display(round(max(scr.size().width(), scr.size().height()) * scr.devicePixelRatio()))
    path = sys.argv[1] if len(sys.argv) > 1 else pick_photo()
    print(f"fotka: {os.path.basename(path)}  výstup barev: {color_manage.target()['name']}")
    canvas = viewer_hud.MediaCanvas(with_video=False)
    canvas.resize(1440, 900)
    canvas.show()
    pump(100)
    dpr = canvas.devicePixelRatioF()
    vp = canvas.viewport()
    size = viewer_zoom.original_size(path)
    canvas.zoom.set_photo(path, size)
    canvas.zoom.set_enabled(True)
    base = viewer_hud.load_image_file(path, QSize_(vp.width() * dpr, vp.height() * dpr))
    canvas.set_image(QPixmap.fromImage(base))
    pump(50)
    fit_med, fit_max, _ = render_ms(canvas)
    print(f"DPR {dpr}, plocha {vp.width()}x{vp.height()} log., originál {size}, obraz displeje {base.width()}x{base.height()}")
    print(f"vešlá:         render medián {fit_med:.2f} ms, max {fit_max:.2f} ms")

    full_t = {}
    t0 = time.perf_counter()
    def on_full():
        job = viewer_zoom.FullResJob(path, canvas.zoom.token)
        job.signals.done.connect(lambda p, tok, lv: (canvas.zoom.set_full_levels(tok, lv),
                                                    full_t.setdefault("ms", (time.perf_counter() - t0) * 1000)))
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(job)
    canvas.zoom.full_res_wanted.connect(on_full)

    # zoom kolečkem: 6 zubů ke středu, snímky během animace
    frames = []
    last = [time.perf_counter()]
    def tick(_p):
        now = time.perf_counter()
        frames.append((now - last[0]) * 1000)
        last[0] = now
    canvas.zoom.changed.connect(tick)
    center = QPoint(vp.width() // 2, vp.height() // 2)
    canvas.zoom.zoom_to(canvas.zoom.one_to_one_w(), QPointF(center), animate=False)
    pump(20)
    med_prev, _, img_prev = render_ms(canvas, 5)
    crop = QRect(round(vp.width() * dpr * 0.35), round(vp.height() * dpr * 0.35),
                 round(vp.width() * dpr * 0.3), round(vp.height() * dpr * 0.3))
    sharp_prev = sharpness(img_prev, crop)
    pump(3000)
    if "ms" not in full_t:
        pump(3000)
    print(f"originál připraven za {full_t.get('ms', float('nan')):.0f} ms (dekódování + profil + pyramida)")
    med100, max100, img100 = render_ms(canvas)
    sharp_full = sharpness(img100, crop)
    print(f"100 % před originálem: ostrost výřezu {sharp_prev:.0f}; s originálem {sharp_full:.0f} "
          f"({sharp_full / max(sharp_prev, 1e-6):.1f}×)")
    print(f"100 %:          render medián {med100:.2f} ms, max {max100:.2f} ms, procento {canvas.zoom.percent(canvas.zoom.w):.0f} %")
    canvas.zoom.zoom_to(canvas.zoom.max_w(), QPointF(center), animate=False)
    pump(20)
    med200, max200, _ = render_ms(canvas)
    print(f"200 %:          render medián {med200:.2f} ms, max {max200:.2f} ms")
    for lvl_w in (canvas.zoom.fit_w() * 1.5, canvas.zoom.fit_w() * 2.5):
        canvas.zoom.zoom_to(lvl_w, QPointF(center), animate=False)
        pump(10)
        m, mx, _ = render_ms(canvas)
        print(f"{canvas.zoom.percent(canvas.zoom.w):5.0f} % (pyramida {canvas.zoom.item.source().width()} px): "
              f"render medián {m:.2f} ms, max {mx:.2f} ms")

    # animace zoomu kolečkem a setrvačnost – doby mezi snímky ovladače
    canvas.zoom.reset(animate=False)
    pump(20)
    frames.clear()
    last[0] = time.perf_counter()
    for _ in range(4):
        wheel(canvas, center, 120)
        pump(60)
    pump(300)
    anim = frames[1:]
    canvas.zoom.press(QPointF(center))
    for i in range(10):
        pump(8)
        canvas.zoom.move(QPointF(center.x() - 30 * (i + 1), center.y() - 12 * (i + 1)))
    frames.clear()
    last[0] = time.perf_counter()
    canvas.zoom.release(QPointF(center.x() - 300, center.y() - 120))
    pump(1500)
    inert = frames[1:]
    for name, fr in (("animace zoomu", anim), ("setrvačnost", inert)):
        if fr:
            fr = sorted(fr)
            print(f"{name}: {len(fr)} snímků, interval medián {statistics.median(fr):.1f} ms, "
                  f"p95 {fr[int(len(fr) * 0.95) - 1]:.1f} ms")
    canvas.close()


def QSize_(w, h):
    from PyQt6.QtCore import QSize
    return QSize(int(w), int(h))


if __name__ == "__main__":
    main()
