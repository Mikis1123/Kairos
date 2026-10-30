"""Stage 2: jednorázové přegenerování obrazových cache knihovny novou cestou (bez spuštění aplikace).

Totéž udělá démon aplikace na pozadí sám (verze miniatur / náhledů / ořezů se změnila); tento
skript to provede hned a změří to. Spustit jen když aplikace neběží:

    python tools/rebuild_display_caches.py [--no-previews]
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from PyQt6 import QtWebEngineWidgets  # noqa: F401 – musí být před QApplication (import main)
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    import color_manage
    import paths
    import preview_cache
    from config import load_config
    cfg = load_config()
    tgt = color_manage.configure(cfg.get("color_output", color_manage.MODE_AUTO))
    scr = app.primaryScreen()
    edge = preview_cache.configure_for_display(round(max(scr.size().width(), scr.size().height())
                                                     * scr.devicePixelRatio()))
    print(f"výstup barev: {tgt['name']} ({tgt['reason']}); náhledy {edge} px")

    import scanner
    t = time.perf_counter()
    n = scanner.repair_missing_thumbnails(progress=lambda i, k: print(f"\r  miniatury {i}/{k}", end=""))
    print(f"\nminiatury: {n} za {time.perf_counter() - t:.1f} s, verze {scanner.thumb_version_state()[0]}")
    thumbs = glob.glob(os.path.join(paths.THUMB_DIR, "*.jpg"))
    print(f"  na disku {len(thumbs)} souborů, {sum(os.path.getsize(f) for f in thumbs) / 1e6:.1f} MB")

    import ai_engine
    t = time.perf_counter()
    k = ai_engine.refresh_phashes()
    print(f"otisky (pHash v{ai_engine.PHASH_VERSION}): {k} za {time.perf_counter() - t:.1f} s")

    import face_engine
    t = time.perf_counter()
    k = face_engine.recolor_face_crops()
    print(f"ořezy tváří převedené do profilu displeje: {k} za {time.perf_counter() - t:.1f} s")

    if "--no-previews" not in sys.argv:
        removed = preview_cache.sweep_legacy()
        print(f"náhledy staré verze smazány: {removed}")
        import main as app_main
        t = time.perf_counter()
        built = 0
        for path, mtype in app_main.preview_build_order():
            if preview_cache.get_preview(path, touch=False) is None and preview_cache.build_preview(path, mtype):
                built += 1
                print(f"\r  náhledy {built}", end="")
        prev = glob.glob(os.path.join(preview_cache.preview_dir(), "*.jpg"))
        print(f"\nnáhledy: {built} za {time.perf_counter() - t:.1f} s; na disku {len(prev)} souborů, "
              f"{sum(os.path.getsize(f) for f in prev) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
