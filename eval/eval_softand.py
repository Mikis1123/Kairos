"""Měkké AND (Sprint 3): dotazy ze dvou pojmů postavené ze zlaté sady.

    python eval/eval_softand.py                 # váhy 0 (jen celý dotaz), 0.3, 0.5, 0.7
    python eval/eval_softand.py --weights 0 0.5 -v

Pro každou dvojici vizuálních dotazů zlaté sady, jejichž relevantní množiny se překrývají
(≥ MIN_BOTH položek), vznikne dotaz „A B“ s pravdou = průnik. Měří se P@k (k = min(10, |průnik|))
a R@20 proti průniku a „čistota“: podíl prvních k výsledků, které obsahují OBA pojmy. Navíc
původní zlatá sada (R@20 / P@10, nepřítomné pojmy), aby jednoslovné dotazy nezhoršil.
"""
import argparse
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

MIN_BOTH = 2
SKIP = {"u vody", "water", "lidé", "v autě"}       # synonyma jiných dotazů sady


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", type=float, nargs="*", default=[0.0, 0.3, 0.5, 0.7])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    import ai_engine
    import search_engine
    from database import visible_media_sql
    conn = sqlite3.connect(os.path.join(ROOT, "photos.db"))
    universe = [r[0] for r in conn.execute(
        f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} ORDER BY m.date_taken DESC, m.id DESC")]
    uni = set(universe)
    emb = {r[0]: r[1] for r in conn.execute("SELECT media_id, embedding FROM image_embeddings")}
    vocab = search_engine.load_vocab(conn)
    gold = json.load(open(os.path.join(HERE, "search_golden.json"), encoding="utf-8"))["queries"]
    visual = [g for g in gold if g["kind"] == "visual" and g["q"] not in SKIP and " " not in g["q"]]
    pairs = []
    for a, b in itertools.combinations(visual, 2):
        both = expand(a["relevant"]) & expand(b["relevant"]) & uni
        if len(both) >= MIN_BOTH:
            ign = (expand(a.get("ignore")) | expand(b.get("ignore"))) & uni
            pairs.append((f"{a['q']} {b['q']}", both, ign))
    print(f"{len(pairs)} dvojic s průnikem >= {MIN_BOTH}")

    for w in args.weights:
        ai_engine.SOFTAND_WEIGHT = w
        P, R = [], []
        for q, both, ign in pairs:
            got = [m for m in search_engine.run_search(q, universe, emb, vocab=vocab).ids if m not in ign]
            k = min(10, len(both))
            P.append(len(set(got[:k]) & both) / k)
            R.append(len(set(got[:20]) & both) / min(20, len(both)))
            if args.verbose:
                print(f"   w={w:.1f} {q:22s} P@{k} {P[-1]:.2f} R@20 {R[-1]:.2f}  ({len(got)} výsledků)")
        # původní zlatá sada (jednoslovné + „lidi u jezera“) a nepřítomné pojmy
        gp, gr, neg = [], [], 0
        for g in gold:
            res = search_engine.run_search(g["q"], universe, emb, vocab=vocab)
            if g["kind"] in ("visual", "combined"):
                rel, ign = expand(g["relevant"]) & uni, expand(g.get("ignore")) & uni
                got = [m for m in res.ids if m not in ign]
                k = min(10, len(rel))
                gp.append(len(set(got[:k]) & rel) / k)
                gr.append(len(set(got[:20]) & rel) / min(20, len(rel)))
            elif g["kind"] == "negative" and res.confident:
                neg += 1
        mean = lambda v: sum(v) / max(1, len(v))  # noqa: E731
        print(f"váha {w:.1f}:  dvojice P@k {mean(P):.3f} R@20 {mean(R):.3f}   |   zlatá sada R@20 {mean(gr):.3f} "
              f"P@10 {mean(gp):.3f}, nepřítomné s jistou shodou {neg}", flush=True)


if __name__ == "__main__":
    main()
