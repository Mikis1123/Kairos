"""Vyhodnocení hledání proti zlaté sadě (Fáze 0 / 1).

    python eval/eval_search.py            # embeddingy z photos.db (to, co vidí aplikace)
    python eval/eval_search.py --fresh    # embeddingy znovu z miniatur, jen v paměti (DB se nemění)
    python eval/eval_search.py -v         # i jednotlivé dotazy

Metriky (jen viditelné položky mřížky; „ignore“ se nepočítá):
  R@20  = zásahy v prvních 20 / min(|relevantní|, 20)
  P@10  = zásahy v prvních k / k,  k = min(10, |relevantní|)
  facet = výsledek přesně odpovídá SQL podmínce (místo, čas, typ, příznak)
  negative = pojem v knihovně není: kolik dotazů má jistou shodu (má být 0) a kolik je prázdných
Cíl Fáze 1: R@20 ≥ 0,80, P@10 ≥ 0,70, žádná jistá shoda u nepřítomných pojmů.

Videa (vícesnímkový index, video_frames):
    python eval/eval_search.py --frames            # + snímky celých videí (v paměti, cache v %TEMP%)
    python eval/eval_search.py --frames --videos   # + zlatá sada videí (search_golden_video.json)
    python eval/eval_search.py --videos            # totéž jen s plakáty (srovnání)
  video R@3 = relevantní videa mezi prvními 3 v kolekci Videa (vesmír = jen videa), MRR = průměr
  1/pořadí prvního relevantního, „v knihovně“ = relevantní video je ve výsledku hledání celé knihovny.
"""
import argparse
import json
import os
import pickle
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
FRAME_CACHE = os.path.join(tempfile.gettempdir(), "kairos_eval_frames.pkl")


def frame_vectors(conn, ids):
    """{id videa: [bytes, ...]} snímků celého klipu; spočítané jednou, pak z FRAME_CACHE."""
    import ai_engine
    import video_frames
    try:
        with open(FRAME_CACHE, "rb") as f:
            cache = pickle.load(f)
    except (OSError, pickle.UnpicklingError, EOFError):
        cache = {}
    changed = False
    for mid, path in conn.execute("SELECT id, file_path FROM media_items WHERE media_type = 'video'"):
        if mid not in ids:
            continue
        key = f"{mid}|{path}|{video_frames.FRAMES_MAX}|{video_frames.EDGE_TRIM}"
        if key not in cache:
            frames = video_frames.extract_frames(path)
            cache[key] = ai_engine.embed_pil([img for _ms, img in frames]) if frames else []
            changed = True
    if changed:
        with open(FRAME_CACHE, "wb") as f:
            pickle.dump(cache, f)
    out = {}
    for key, vecs in cache.items():
        mid = int(key.split("|", 1)[0])
        if mid in ids and vecs:
            out[mid] = vecs
    return out


def eval_videos(conn, universe, emb, vocab):
    import search_engine
    gold = json.load(open(os.path.join(HERE, "search_golden_video.json"), encoding="utf-8"))["queries"]
    vids = [r[0] for r in conn.execute("SELECT id FROM media_items WHERE media_type = 'video'") if r[0] in set(universe)]
    vids = [m for m in universe if m in set(vids)]          # pořadí kolekce
    r3, mrr, in_lib, n_rel = [], [], 0, 0
    for g in gold:
        rel = set(g["videos"]) & set(vids)
        if not rel:
            continue
        res = search_engine.run_search(g["q"], vids, emb, vocab=vocab)
        got = res.ids
        k = min(3, len(rel))
        r3.append(len(set(got[:3]) & rel) / k)
        first = next((i for i, m in enumerate(got) if m in rel), None)
        mrr.append(0.0 if first is None else 1.0 / (first + 1))
        lib = set(search_engine.run_search(g["q"], universe, emb, vocab=vocab).ids)
        in_lib += len(rel & lib)
        n_rel += len(rel)
    print(f"Videa ({len(r3)} dotazů): R@3 {sum(r3) / len(r3):.3f}   MRR {sum(mrr) / len(mrr):.3f}   "
          f"v knihovně {in_lib}/{n_rel}")


def expand(spec):
    out = set()
    for s in spec or []:
        a, _, b = str(s).partition("-")
        out |= set(range(int(a), int(b or a) + 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="embeddingy z miniatur (v paměti)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--db", default=os.path.join(ROOT, "photos.db"))
    ap.add_argument("--frames", action="store_true", help="videa i se snímky celého klipu")
    ap.add_argument("--videos", action="store_true", help="i zlatá sada videí")
    ap.add_argument("--penalty", type=float, help="SEARCH_FRAME_Z_PENALTY (kalibrace)")
    args = ap.parse_args()

    import ai_engine
    import search_engine
    from database import visible_media_sql

    conn = sqlite3.connect(args.db)
    universe = [r[0] for r in conn.execute(
        f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} ORDER BY m.date_taken DESC, m.id DESC")]
    if args.fresh:
        emb = {}
        for mid, thumb in conn.execute("SELECT id, thumbnail_path FROM media_items"):
            if mid in set(universe):
                v = ai_engine.compute_image_embedding(thumb)
                if v is not None:
                    emb[mid] = v
    else:
        emb = {r[0]: r[1] for r in conn.execute("SELECT media_id, embedding FROM image_embeddings")}
    if args.penalty is not None:
        ai_engine.SEARCH_FRAME_Z_PENALTY = args.penalty
    if args.frames:
        for mid, vecs in frame_vectors(conn, set(universe)).items():
            emb[mid] = tuple([emb[mid]] if mid in emb else []) + tuple(vecs)
    vocab = search_engine.load_vocab(conn)
    gold = json.load(open(os.path.join(HERE, "search_golden.json"), encoding="utf-8"))["queries"]
    uni = set(universe)

    rec, prec, facet_ok, facet_n, neg_conf, neg_empty, neg_n = [], [], 0, 0, 0, 0, 0
    for g in gold:
        res = search_engine.run_search(g["q"], universe, emb, vocab=vocab)
        got = res.ids
        if g["kind"] in ("visual", "combined"):
            rel = expand(g["relevant"]) & uni
            ign = expand(g.get("ignore")) & uni
            judged = [m for m in got if m not in ign]
            k = min(10, len(rel))
            r = len(set(judged[:20]) & rel) / min(len(rel), 20)
            p = len(set(judged[:k]) & rel) / k if k else 1.0
            rec.append(r)
            prec.append(p)
            line = f"R@20 {r:.2f}  P@10 {p:.2f}"
        elif g["kind"] == "facet":
            want = {r[0] for r in conn.execute(
                f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} AND ({g['expect_sql']})")}
            ok = set(res.confident) == want           # „možné“ = jen přes název výletu
            facet_n += 1
            facet_ok += ok
            line = f"{'OK ' if ok else 'ŠPATNĚ'} očekáváno {len(want)}, jistých {len(res.confident)}"
        else:
            neg_n += 1
            neg_conf += bool(res.confident)
            neg_empty += res.is_empty()
            line = f"jisté {len(res.confident)}  možné {len(res.possible)}"
        if args.verbose:
            labels = ", ".join(res.labels)
            print(f"  {g['kind'][:4]:4s} {g['q']:22s} jisté {len(res.confident):3d} možné {len(res.possible):3d}"
                  f"  {line}{'  [' + labels + ']' if labels else ''}")

    n = len(rec)
    print(f"Obraz ({n} dotazů): R@20 {sum(rec) / n:.3f}   P@10 {sum(prec) / n:.3f}")
    print(f"Fazety: {facet_ok}/{facet_n} přesně")
    print(f"Nepřítomné pojmy ({neg_n}): s jistou shodou {neg_conf}, prázdné {neg_empty}")
    if args.videos:
        eval_videos(conn, universe, emb, vocab)
    target = sum(rec) / n >= 0.80 and sum(prec) / n >= 0.70 and neg_conf == 0 and facet_ok == facet_n
    print("CÍL SPLNĚN" if target else "CÍL NESPLNĚN")
    return 0 if target else 1


if __name__ == "__main__":
    sys.exit(main())
