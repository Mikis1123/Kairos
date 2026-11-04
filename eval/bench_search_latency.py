"""Pillar 1 – natural-language search latency (query typed -> ranked ids).

Measures search_engine.run_search end to end on the real library and on a synthetic library
(random unit vectors, same 512-d CLIP space) of N items, with the ONNX text model warm:
  text encode (multilingual DistilBERT, CPU) + vector ranking + soft-AND + facets.
Usage: python eval/bench_search_latency.py [synthetic_n=20000]
"""
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

QUERIES = ["západ slunce", "sunset", "pes běží", "dog running", "hory", "mountains", "narozeninový dort",
           "birthday cake", "moře a pláž", "snow"]


def library_embeddings():
    from database import get_connection
    conn = get_connection()
    try:
        rows = conn.execute("SELECT media_id, embedding FROM image_embeddings").fetchall()
    finally:
        conn.close()
    return {int(m): bytes(e) for m, e in rows}


def synthetic(n, dim=512, seed=7):
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, dim)).astype("<f4")
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    return {i + 1_000_000: v[i].tobytes() for i in range(n)}


def bench(label, embeddings, runs=3):
    import ai_engine
    import search_engine
    universe = list(embeddings)
    vocab = search_engine.load_vocab()
    search_engine.run_search(QUERIES[0], universe, embeddings, vocab=vocab)      # index build + warm
    totals, encodes = [], []
    for _ in range(runs):
        for q in QUERIES:
            ai_engine._query_cache.clear() if hasattr(ai_engine, "_query_cache") else None
            t0 = time.perf_counter()
            res = search_engine.run_search(q, universe, embeddings, vocab=vocab)
            totals.append((time.perf_counter() - t0) * 1000)
            t1 = time.perf_counter()
            ai_engine.query_vector(q + " ", ai_engine.text_model_for_dim(512))   # uncached encode only
            encodes.append((time.perf_counter() - t1) * 1000)
    totals.sort()
    p95 = totals[min(len(totals) - 1, int(len(totals) * 0.95))]
    print(f"{label:<26} n={len(embeddings):>6}  total p50 {statistics.median(totals):6.1f} ms  p95 {p95:6.1f} ms"
          f"   (text encode p50 {statistics.median(encodes):5.1f} ms)   last hits {len(res.confident)}")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    import ai_engine
    t0 = time.perf_counter()
    ai_engine.text_model_for_dim(512)
    print(f"text model load (one-off, deferred after boot): {(time.perf_counter() - t0) * 1000:.0f} ms")
    lib = library_embeddings()
    if lib:
        bench("real library", lib)
    bench("synthetic", synthetic(n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
