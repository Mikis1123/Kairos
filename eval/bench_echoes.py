"""Ozvěny na skutečných fotkách (ne syntetické vektory).

    python eval/bench_echoes.py                  # knihovna tak, jak je (photos.db jen pro čtení)
    python eval/bench_echoes.py --shift 5        # kopie, kde každá druhá fotka je o 5 let starší
                                                 #  = umělé „návraty“ se skutečnými CLIP vektory
    python eval/bench_echoes.py --shift 5 --sheet out.jpg   # náhledy nejlepších ozvěn vedle sebe

Měří: počet ozvěn, čas hledání, kolik dvojic se podařilo srovnat (align_pair) a čas srovnání.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import echoes  # noqa: E402


def shifted_copy(years):
    tmp = tempfile.mkdtemp(prefix="echoes_bench_")
    db = os.path.join(tmp, "photos.db")
    src = sqlite3.connect(f"file:{os.path.join(ROOT, 'photos.db')}?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    rows = dst.execute("SELECT id, date_taken FROM media_items WHERE date_taken IS NOT NULL ORDER BY date_taken, id").fetchall()
    for k, (mid, d) in enumerate(rows):
        if k % 2:
            s = str(d)
            dst.execute("UPDATE media_items SET date_taken = ? WHERE id = ?", (str(int(s[:4]) - years) + s[4:], mid))
    dst.commit()
    return dst, tmp


def load_rgb(path, edge=1600):
    from PIL import Image, ImageOps
    import numpy as np
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((edge, edge))
        return np.asarray(im)


def best_image(item):
    import preview_cache
    p = preview_cache.get_preview(item["path"], touch=False) if item.get("path") else None
    for cand in (p, item.get("path"), item.get("thumb")):
        if cand and os.path.exists(cand):
            return cand
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shift", type=int, default=0)
    ap.add_argument("--sheet", default="")
    ap.add_argument("--align", type=int, default=12, help="kolik ozvěn zkusit srovnat")
    args = ap.parse_args()
    tmp = None
    if args.shift:
        conn, tmp = shifted_copy(args.shift)
    else:
        conn = sqlite3.connect(f"file:{os.path.join(ROOT, 'photos.db')}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM media_items m JOIN image_embeddings e ON e.media_id = m.id "
                         "WHERE m.latitude IS NOT NULL").fetchone()[0]
        t0 = time.perf_counter()
        found = echoes.find_echoes(conn)
        took = time.perf_counter() - t0
        print(f"Kandidátů s polohou a vektorem: {n}; ozvěn: {len(found)} za {took * 1000:.0f} ms")
        aligned, align_ms = 0, []
        for e in found[:args.align]:
            a, b = best_image(e["items"][0]), best_image(e["items"][-1])
            if not (a and b):
                continue
            t1 = time.perf_counter()
            m = echoes.align_pair(load_rgb(a), load_rgb(b))
            align_ms.append((time.perf_counter() - t1) * 1000)
            aligned += m is not None
            print(f"  {e['items'][0]['year']}→{e['items'][-1]['year']}  skóre {e['score']:.3f}  "
                  f"{e['place'] or '?':18s} srovnáno {'ano' if m is not None else 'ne '}  "
                  f"{os.path.basename(a)} | {os.path.basename(b)}")
        if align_ms:
            print(f"Srovnáno {aligned}/{len(align_ms)}, čas srovnání p50 {sorted(align_ms)[len(align_ms) // 2]:.0f} ms")
        if args.sheet and found:
            from PIL import Image
            tiles = []
            for e in found[:6]:
                pair = [best_image(e["items"][0]), best_image(e["items"][-1])]
                if not all(pair):
                    continue
                row = Image.new("RGB", (640, 240), (20, 20, 24))
                for k, p in enumerate(pair):
                    im = Image.fromarray(load_rgb(p, 400))
                    im.thumbnail((316, 236))
                    row.paste(im, (k * 322 + 2, 2))
                tiles.append(row)
            sheet = Image.new("RGB", (640, 240 * len(tiles)), (0, 0, 0))
            for k, t in enumerate(tiles):
                sheet.paste(t, (0, 240 * k))
            sheet.save(args.sheet, quality=85)
            print(f"Náhled: {args.sheet}")
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(tmp, True)


if __name__ == "__main__":
    main()
