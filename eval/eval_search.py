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
"""
import argparse
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


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
    target = sum(rec) / n >= 0.80 and sum(prec) / n >= 0.70 and neg_conf == 0 and facet_ok == facet_n
    print("CÍL SPLNĚN" if target else "CÍL NESPLNĚN")
    return 0 if target else 1


if __name__ == "__main__":
    sys.exit(main())
