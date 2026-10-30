"""Sprint 3: srovnání CLIP ViT-B/32 (aplikace) se SigLIP 2 (multilingvální) na zlaté sadě.

    python eval/eval_siglip.py                              # google/siglip2-base-patch16-256
    python eval/eval_siglip.py --model google/siglip2-base-patch16-384

Oba modely dostanou stejné podmínky: embeddingy fotek znovu z miniatur (v paměti, DB se nemění),
celé řazení aplikace (search_engine.run_search -> ai_engine.rank_semantic s kalibrací bankou pojmů).
Prahy SEARCH_Z_* jsou kalibrované pro CLIP – vypisuje se proto i čisté pořadí (R@20 / P@10 nad
celou knihovnou bez prahů), které na prazích nezávisí. Potřebuje torch + transformers (jen vývoj).
"""
import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402

from eval_search import expand  # noqa: E402


class SiglipImage:
    def __init__(self, name):
        import torch
        from transformers import AutoModel, AutoProcessor
        self.torch = torch
        self.model = AutoModel.from_pretrained(name).eval()
        self.proc = AutoProcessor.from_pretrained(name)

    def encode(self, images, convert_to_numpy=True, show_progress_bar=False):
        single = not isinstance(images, (list, tuple))
        batch = [images] if single else list(images)
        with self.torch.no_grad():
            x = self.proc(images=batch, return_tensors="pt")
            v = self.model.get_image_features(**x)
            v = getattr(v, "pooler_output", v)
        v = v.numpy().astype(np.float32)
        return v[0] if single else v


class SiglipText:
    def __init__(self, img):
        self.img = img

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        with self.img.torch.no_grad():
            # SigLIP je trénovaný s výplní na 64 tokenů (padding="max_length") – jinak horší
            x = self.img.proc(text=[t.lower() for t in batch], padding="max_length", max_length=64,
                              truncation=True, return_tensors="pt")
            v = self.img.model.get_text_features(**x)
            v = getattr(v, "pooler_output", v)
        v = v.numpy().astype(np.float32)
        return v[0] if single else v


def evaluate(label, conn, universe, gold, dim):
    import ai_engine
    import search_engine
    from PIL import Image
    ai_engine.EMBED_DIM, ai_engine.EMBED_BYTES = dim, dim * 4
    ai_engine._cached_index_state.update({"count": -1, "matrix": None, "faiss_index": None, "media_ids": []})
    ai_engine._bank_cache.update({"model": None, "vectors": None})
    uni = set(universe)
    t0 = time.time()
    rows = [(mid, t) for mid, t in conn.execute("SELECT id, thumbnail_path FROM media_items")
            if mid in uni and t and os.path.exists(t)]
    model = ai_engine.get_image_model()
    emb = {}
    for k in range(0, len(rows), 16):
        chunk = rows[k:k + 16]
        vecs = model.encode([Image.open(t).convert("RGB") for _, t in chunk])
        for (mid, _), v in zip(chunk, np.atleast_2d(vecs)):
            emb[mid] = ai_engine.encode_embedding(v)
    t_img = (time.time() - t0) / max(1, len(rows)) * 1000
    vocab = search_engine.load_vocab(conn)
    rec, prec, neg, raw_r, raw_p = [], [], 0, [], []
    tm = ai_engine.get_text_model()
    ids = list(emb)
    M = np.stack([ai_engine.decode_embedding(emb[i]) for i in ids])
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    for g in gold:
        res = search_engine.run_search(g["q"], universe, emb, vocab=vocab)
        if g["kind"] == "visual":
            rel, ign = expand(g["relevant"]) & uni, expand(g.get("ignore")) & uni
            got = [m for m in res.ids if m not in ign]
            k = min(10, len(rel))
            rec.append(len(set(got[:20]) & rel) / min(20, len(rel)))
            prec.append(len(set(got[:k]) & rel) / k)
            q = ai_engine.query_vector(g["q"], tm)                  # čisté pořadí bez prahů
            order = [ids[i] for i in np.argsort(-(M @ q)) if ids[i] not in ign]
            raw_r.append(len(set(order[:20]) & rel) / min(20, len(rel)))
            raw_p.append(len(set(order[:k]) & rel) / k)
        elif g["kind"] == "negative" and res.confident:
            neg += 1
    m = lambda v: sum(v) / len(v)  # noqa: E731
    print(f"{label:34s} aplikace R@20 {m(rec):.3f} P@10 {m(prec):.3f} | pořadí R@20 {m(raw_r):.3f} "
          f"P@10 {m(raw_p):.3f} | nepřítomné s jistou shodou {neg}/8 | {t_img:.0f} ms/foto", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="google/siglip2-base-patch16-256")
    args = ap.parse_args()
    import ai_engine
    from database import visible_media_sql
    conn = sqlite3.connect(os.path.join(ROOT, "photos.db"))
    universe = [r[0] for r in conn.execute(
        f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} ORDER BY m.date_taken DESC, m.id DESC")]
    gold = json.load(open(os.path.join(HERE, "search_golden.json"), encoding="utf-8"))["queries"]
    evaluate("CLIP ViT-B/32 multilingual (ONNX)", conn, universe, gold, 512)
    img = SiglipImage(args.model)
    ai_engine._image_model, ai_engine._text_model = img, SiglipText(img)
    dim = int(np.atleast_2d(img.encode([__import__("PIL.Image").Image.new("RGB", (64, 64))])).shape[1])
    evaluate(args.model, conn, universe, gold, dim)


if __name__ == "__main__":
    main()
