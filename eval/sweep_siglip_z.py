"""Sprint 4.0: kalibrace prahů z (jisté / možné / vyčnívá) pro SigLIP 2 na zlaté sadě.

    python eval/sweep_siglip_z.py

Prahy SEARCH_Z_* jsou laděné pro CLIP; SigLIP má jiné rozložení podobností k bance pojmů.
Vektory fotek se spočítají jednou (z miniatur, DB se nemění), pak se mřížkou prahů projde
celé řazení aplikace (search_engine.run_search). Cíl: R@20 a P@10 aplikace co nejvýš při
nule „jistých“ shod u nepřítomných pojmů (negativní dotazy).
"""
import itertools
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from eval_search import expand  # noqa: E402


def main():
    import ai_engine
    import search_engine
    import siglip_engine
    from PIL import Image
    from database import visible_media_sql
    conn = sqlite3.connect(os.path.join(ROOT, "photos.db"))
    universe = [r[0] for r in conn.execute(
        f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} ORDER BY m.date_taken DESC, m.id DESC")]
    uni = set(universe)
    gold = json.load(open(os.path.join(HERE, "search_golden.json"), encoding="utf-8"))["queries"]
    rows = [(mid, t) for mid, t in conn.execute("SELECT id, thumbnail_path FROM media_items")
            if mid in uni and t and os.path.exists(t)]
    vecs = ai_engine.siglip_embed_pil([Image.open(t).convert("RGB") for _, t in rows])
    emb = {mid: v for (mid, _), v in zip(rows, vecs)}
    vocab = search_engine.load_vocab(conn)
    tm = ai_engine.get_siglip_text_model()
    raw_encode, memo = tm.encode, {}

    def encode(texts, **kw):                 # mřížka = stejné dotazy znovu: text jen jednou
        key = texts if isinstance(texts, str) else tuple(texts)
        if key not in memo:
            memo[key] = raw_encode(texts, **kw)
        return memo[key]
    tm.encode = encode
    base = dict(ai_engine.Z_PROFILES[siglip_engine.DIM])
    results = []
    grid = {"confident": [3.0, 3.4, 3.8, 4.2, 4.6], "possible": [0.2, 0.5, 0.8, 1.2], "show": [1.8, 2.6],
            "lib": [1.0]}
    for c, p, sh, lib in itertools.product(*grid.values()):
        ai_engine.Z_PROFILES[siglip_engine.DIM].update(confident=c, possible=p, show=sh, lib=lib)
        rec, prec, neg = [], [], 0
        for g in gold:
            res = search_engine.run_search(g["q"], universe, emb, vocab=vocab)
            if g["kind"] == "visual":
                rel, ign = expand(g["relevant"]) & uni, expand(g.get("ignore")) & uni
                got = [m for m in res.ids if m not in ign]
                k = min(10, len(rel))
                rec.append(len(set(got[:20]) & rel) / min(20, len(rel)))
                prec.append(len(set(got[:k]) & rel) / k)
            elif g["kind"] == "negative" and res.confident:
                neg += 1
        m = sum(rec) / len(rec), sum(prec) / len(prec)
        results.append((neg, -(m[0] + m[1]), c, p, sh, lib, m))
    ai_engine.Z_PROFILES[siglip_engine.DIM].update(base)
    results.sort()
    for neg, _s, c, p, sh, lib, m in results[:12]:
        print(f"confident {c} possible {p} show {sh} lib {lib}: R@20 {m[0]:.3f} P@10 {m[1]:.3f} negativní {neg}/8")


if __name__ == "__main__":
    main()
