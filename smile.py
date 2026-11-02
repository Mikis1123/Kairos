"""Úsměv na tvářích (Sprint 4, Throwback Reels) – offline, bez dalšího modelu.

Ořez tváře (face_instances.face_crop_path, 256 px) projde obrazovým CLIP (ai_onnx, už v paměti
kvůli hledání) a porovná se se dvěma sadami popisů: úsměv / smích proti neutrálnímu výrazu.
Pravděpodobnost = sigmoida rozdílu průměrných kosinů × LOGIT_SCALE (teplota CLIP).

Ověřeno na skutečné knihovně (469 tváří): 8 nejvyšších = široké úsměvy a smích, 8 nejnižších =
vážné / neutrální výrazy; medián 0,33. Výsledek v face_instances.smile (NULL = nespočítáno).
"""
import numpy as np

import paths

SMILE_PROMPTS = ["a photo of a smiling person", "a happy face with a big smile", "a person laughing",
                 # i18n-ok: search vocabulary (CLIP prompts)
                 "usmívající se člověk", "šťastný úsměv"]
NEUTRAL_PROMPTS = ["a photo of a person with a neutral face", "a serious face",
                   # i18n-ok: search vocabulary (CLIP prompts)
                   "a person with closed mouth, not smiling", "vážný obličej", "neutrální výraz"]
LOGIT_SCALE = 100.0
BATCH = 32

_prompt_cache = {"model": None, "vecs": None}


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n > 0, n, 1.0)


def prompt_vectors(text_model):
    if _prompt_cache["model"] is not text_model:
        pos = _norm(text_model.encode(SMILE_PROMPTS)).mean(axis=0)
        neg = _norm(text_model.encode(NEUTRAL_PROMPTS)).mean(axis=0)
        _prompt_cache.update(model=text_model, vecs=(pos, neg))
    return _prompt_cache["vecs"]


def smile_probs(face_vecs, text_model):
    """(n, 512) vektory ořezů -> pravděpodobnosti úsměvu (n,)."""
    v = _norm(face_vecs)
    if v.ndim == 1:
        v = v[None, :]
    pos, neg = prompt_vectors(text_model)
    logit = LOGIT_SCALE * (v @ pos - v @ neg)
    return 1.0 / (1.0 + np.exp(-np.clip(logit, -40, 40)))


def pending_faces(conn, media_ids=None, limit=None):
    """[(face_id, cesta ořezu)] tváří bez skóre úsměvu (volitelně jen z těchto médií)."""
    where = ""
    params = []
    if media_ids is not None:
        ids = [int(i) for i in media_ids]
        if not ids:
            return []
        where = f" AND media_id IN ({','.join('?' * len(ids))})"
        params = ids
    sql = (f"SELECT id, face_crop_path FROM face_instances WHERE smile IS NULL "
           f"AND COALESCE(is_valid, 1) = 1{where} ORDER BY id")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def score_faces(conn, media_ids=None, should_stop=None, image_model=None, text_model=None):
    """Spočítá úsměv chybějícím tvářím a zapíše do face_instances.smile. Vrací počet tváří.
    Ořez, který nejde načíst, dostane 0 (nepočítat znovu při každém běhu)."""
    import ai_engine
    from PIL import Image
    rows = pending_faces(conn, media_ids)
    if not rows:
        return 0
    image_model = image_model or ai_engine.get_image_model()
    text_model = text_model or ai_engine.get_text_model()
    done = 0
    for k in range(0, len(rows), BATCH):
        if should_stop and should_stop():
            break
        chunk = rows[k:k + BATCH]
        images, ids, broken = [], [], []
        for fid, path in chunk:
            real = paths.resolve_face_path(path)
            try:
                with Image.open(real) as im:
                    images.append(im.convert("RGB"))
                ids.append(fid)
            except Exception:
                broken.append(fid)
        probs = smile_probs(image_model.encode(images), text_model) if images else []
        conn.executemany("UPDATE face_instances SET smile = ? WHERE id = ?",
                         [(round(float(p), 4), fid) for fid, p in zip(ids, probs)] + [(0.0, f) for f in broken])
        conn.commit()
        done += len(chunk)
    return done
