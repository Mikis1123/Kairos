"""Throwback Reels (Sprint 4): automaticky vybrané filmové sestřihy – střihy na doby hudby.

Návrhy (suggestions): výlety a události, celé roky a „Před N lety“ (tento týden v dřívějších
letech). Kurátor (curate) vybere snímky podle:
  * ostrosti (Laplace, media_items.sharpness_score – normalizováno v rámci sestřihu),
  * oblíbených (is_favorite),
  * tváří a úsměvů (face_instances, smile.py – smějící se lidé jsou to, co si člověk pamatuje),
  * celkové kvality obrazu (CLIP: „krásná fotka / vzpomínka“ proti „rozmazané / snímku obrazovky /
    dokumentu“ – vektory z hledání, nic se nepočítá znovu),
pak vyřadí série a skoro-duplikáty (pHash, kosinus CLIP) a rozprostře výběr po celém období.

Plán (plan) staví Director (director.py): záběr = celé doby skladby (hlavní snímky dvojnásob),
každý střih tak padá na dobu; střih na těžkou dobu dostane delší prolnutí. Pohyb kamery v záběru
= Ken Burns k tvářím (director.ken_burns). Stejný plán kreslí přehrávač i export MP4
(reel_render.py) – náhled a video jsou snímek po snímku totéž.
"""
import math
import zlib
from datetime import date, datetime, timedelta

import director
from database import visible_media_sql

MIN_SHOTS = 6
MAX_SHOTS = 20                # ~1 min při dobách 2–2,5 s
SHOT_S = 2.4                  # cílová délka záběru (s) – zaokrouhlí se na celé doby skladby
HERO_SHARE = 0.15             # podíl nejlepších snímků s dvojnásobným záběrem
TITLE_BEATS_S = 4.5           # úvodní titulek (s, zaokrouhleno na doby)
END_S = 3.5                   # závěrečný titulek
CUT_FADE_S = 0.22             # prolnutí na běžné době
DOWNBEAT_FADE_S = 0.7         # delší prolnutí na těžké době
DEFAULT_BEATS = {"period": 2.4, "bar": 4, "offset": 0.0, "length": 0.0}   # bez hudby: pravidelný rytmus
BURST_HAMMING = 10
NEAR_DUP_COS = 0.93
SAME_SCENE_COS = 0.85         # během SAME_SCENE_MIN minut stačí menší podobnost (skupinovka 3× za sebou)
SAME_SCENE_MIN = 3
FAVORITE_BONUS = 1.5
FACE_BONUS = 0.35
SMILE_WEIGHT = 1.1
QUALITY_WEIGHT = 0.45
THROWBACK_DAYS = 7
GOOD_PROMPTS = ["a beautiful photo", "a memorable moment with friends", "a stunning landscape", "krásná fotka"]
BAD_PROMPTS = ["a blurry photo", "a screenshot", "a photo of a document", "a dark underexposed photo"]

_quality_cache = {"model": None, "vec": None}


def _seed(key):
    return zlib.crc32(str(key).encode("utf-8")) & 0xFFFFFFFF


def _parse_day(s):
    s = str(s or "")[:10].replace(":", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


# ── návrhy ────────────────────────────────────────────────────────────────────

def suggestions(conn, today=None, limit_events=12):
    """[{key, kind, title, subtitle, count, cover_id}] sestřihů, které knihovna unese."""
    today = today or date.today()
    out = []
    rows = conn.execute(f"""
        SELECT strftime('%m-%d', replace(substr(m.date_taken, 1, 10), ':', '-')) AS md,
               CAST(substr(m.date_taken, 1, 4) AS INTEGER) AS y, m.id
        FROM media_items m
        WHERE m.date_taken IS NOT NULL AND m.media_type IN ('photo', 'live_photo') AND {visible_media_sql('m')}
    """).fetchall()
    # Před N lety: tento týden v dřívějších letech
    window = {(today + timedelta(days=d)).strftime("%m-%d") for d in range(-THROWBACK_DAYS, THROWBACK_DAYS + 1)}
    years = {}
    for md, y, mid in rows:
        if md in window and y and y < today.year:
            years.setdefault(y, []).append(mid)
    tb = sum(len(v) for v in years.values())
    if tb >= MIN_SHOTS:
        ago = sorted(today.year - y for y in years)
        what = (f"Před {ago[0]} {'rokem' if ago[0] == 1 else 'lety'}" if len(ago) == 1
                else f"Tento týden před {ago[0]}–{ago[-1]} lety")
        out.append({"key": "throwback", "kind": "throwback", "title": what,
                    "subtitle": f"{tb} snímků z tohoto týdne", "count": tb})
    for eid, title, loc, start, n in conn.execute(f"""
            SELECT e.id, e.title, e.location_name, e.start_date, COUNT(m.id)
            FROM events e JOIN media_items m ON m.event_id = e.id
            WHERE m.media_type IN ('photo', 'live_photo') AND {visible_media_sql('m')}
            GROUP BY e.id HAVING COUNT(m.id) >= {MIN_SHOTS}
            ORDER BY e.start_date DESC, e.id DESC LIMIT {int(limit_events)}""").fetchall():
        d = _parse_day(start)
        out.append({"key": f"event:{eid}", "kind": "event", "event_id": int(eid),
                    "title": clean_title(title) or loc or "Událost",
                    "subtitle": " · ".join(x for x in (loc if loc != title else "", _cz_date(d)) if x), "count": int(n)})
    per_year = {}
    for _md, y, _mid in rows:
        if y:
            per_year[y] = per_year.get(y, 0) + 1
    for y in sorted(per_year, reverse=True):
        if per_year[y] >= MIN_SHOTS * 2:
            out.append({"key": f"year:{y}", "kind": "year", "year": y, "title": f"Rok {y}",
                        "subtitle": f"{per_year[y]} snímků", "count": per_year[y]})
    return out


def clean_title(title):
    """„Výlet: Frauenau a okolí (09. 07. – 12. 07. 2026)“ -> „Výlet: Frauenau a okolí“ (datum je v podtitulku)."""
    import re
    return re.sub(r"\s*\([^()]*\d{4}\)\s*$", "", str(title or "")).strip()


def _cz_date(d):
    return f"{d.day}. {d.month}. {d.year}" if d else ""


# ── kandidáti ────────────────────────────────────────────────────────────────

def _scope_sql(key, today):
    if key == "throwback":
        days = [(today + timedelta(days=d)).strftime("%m-%d") for d in range(-THROWBACK_DAYS, THROWBACK_DAYS + 1)]
        return (f"strftime('%m-%d', replace(substr(m.date_taken, 1, 10), ':', '-')) IN ({','.join('?' * len(days))}) "
                f"AND CAST(substr(m.date_taken, 1, 4) AS INTEGER) < ?", days + [today.year])
    kind, _, val = key.partition(":")
    if kind == "event":
        return "m.event_id = ?", [int(val)]
    if kind == "year":
        return "substr(m.date_taken, 1, 4) = ?", [str(int(val))]
    raise ValueError(f"neznámý sestřih {key}")


def candidates(conn, key, today=None):
    """Snímky sestřihu s tím, co kurátor potřebuje: [{id, path, thumb, date, fav, sharp, phash,
    w, h, faces, smile, focus, emb}] v časovém pořadí."""
    import ai_engine
    from journey import face_focus, iso_minute
    today = today or date.today()
    where, params = _scope_sql(key, today)
    rows = conn.execute(f"""
        SELECT m.id, m.file_path, m.thumbnail_path, m.date_taken, m.is_favorite, m.sharpness_score, m.phash,
               e.embedding
        FROM media_items m LEFT JOIN image_embeddings e ON e.media_id = m.id
        WHERE m.date_taken IS NOT NULL AND m.media_type IN ('photo', 'live_photo') AND {visible_media_sql('m')}
          AND {where}
    """, params).fetchall()
    faces = {}
    ids = [r[0] for r in rows]
    for k in range(0, len(ids), 900):
        chunk = ids[k:k + 900]
        try:
            for mid, x, y, w, h, smile in conn.execute(f"""
                    SELECT media_id, bbox_x, bbox_y, bbox_w, bbox_h, smile FROM face_instances
                    WHERE COALESCE(is_valid, 1) = 1 AND media_id IN ({','.join('?' * len(chunk))})""", chunk):
                faces.setdefault(mid, []).append(((x, y, w, h), smile))
        except Exception:
            break                                   # starší DB bez sloupců tváří
    out = []
    for mid, path, thumb, taken, fav, sharp, phash, emb in rows:
        fs = faces.get(mid, [])
        w, h = _thumb_size(thumb)
        smiles = [s for _b, s in fs if s is not None]
        out.append({"id": int(mid), "path": path, "thumb": thumb, "date": iso_minute(taken), "fav": bool(fav),
                    "sharp": float(sharp or 0.0), "phash": phash, "w": int(w or 0), "h": int(h or 0),
                    "faces": len(fs), "smile": max(smiles) if smiles else 0.0,
                    "focus": face_focus([b for b, _s in fs if b[0] is not None]),
                    "emb": ai_engine.decode_embedding(emb) if emb else None})
    out.sort(key=lambda m: (m["date"], m["id"]))
    return out


def _thumb_size(path):
    """Rozměry miniatury (už otočené podle EXIF) = poměr stran fotky; čte se jen hlavička."""
    if not path:
        return 0, 0
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return 0, 0


# ── hodnocení a výběr ─────────────────────────────────────────────────────────

def quality_vector(text_model):
    """Směr „dobrá vzpomínka – špatný snímek“ v prostoru CLIP (jednou na model)."""
    import numpy as np
    if _quality_cache["model"] is not text_model:
        def mean(ts):
            v = np.asarray(text_model.encode(ts), dtype=np.float32)
            v /= np.linalg.norm(v, axis=1, keepdims=True)
            return v.mean(axis=0)
        q = mean(GOOD_PROMPTS) - mean(BAD_PROMPTS)
        _quality_cache.update(model=text_model, vec=q / (np.linalg.norm(q) or 1.0))
    return _quality_cache["vec"]


def score(items, quality=None):
    """Doplní item["score"] (a složky) – vyšší = lepší záběr sestřihu."""
    import numpy as np
    if not items:
        return items
    sharp = np.log1p(np.maximum(0, [m["sharp"] for m in items]))
    top = float(np.percentile(sharp, 90)) or 1.0
    qz = np.zeros(len(items))
    if quality is not None:
        embs = [m["emb"] for m in items]
        have = [i for i, e in enumerate(embs) if e is not None and e.size == quality.size]
        if len(have) >= 3:
            q = np.array([float(embs[i] @ quality) / (np.linalg.norm(embs[i]) or 1.0) for i in have])
            sd = float(q.std()) or 1.0
            for i, v in zip(have, (q - q.mean()) / sd):
                qz[i] = float(np.clip(v, -2.5, 2.5))
    for m, sh, z in zip(items, sharp, qz):
        m["s_sharp"] = float(min(1.2, sh / top))
        m["s_quality"] = float(z)
        m["score"] = (m["s_sharp"] + FAVORITE_BONUS * m["fav"] + FACE_BONUS * (m["faces"] > 0)
                      + SMILE_WEIGHT * m["smile"] + QUALITY_WEIGHT * z)
    return items


def _hamming(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def _cos(a, b):
    import numpy as np
    if a is None or b is None or a.size != b.size:
        return 0.0
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


def _minutes(s):
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").timestamp() / 60
    except ValueError:
        return None


def _same_shot(a, b):
    """Série / skoro-duplikát: pHash, velmi podobný obsah, nebo podobný obsah během pár minut.
    (Naměřeno: tři skupinovky za sebou kosinus 0,87–0,92, pHash 22–32 – pHash sám nestačí.)"""
    if _hamming(a["phash"], b["phash"]) <= BURST_HAMMING:
        return True
    cos = _cos(a["emb"], b["emb"])
    if cos >= NEAR_DUP_COS:
        return True
    ta, tb = _minutes(a.get("date")), _minutes(b.get("date"))
    return cos >= SAME_SCENE_COS and ta is not None and tb is not None and abs(ta - tb) <= SAME_SCENE_MIN


def curate(items, n=None):
    """Výběr: nejlepší záběry bez sérií a skoro-duplikátů, rozprostřené po celém období
    (časové koše – nejvýš ~2× průměr na koš). Vrací v časovém pořadí."""
    if not items:
        return []
    n = n or max(MIN_SHOTS, min(MAX_SHOTS, int(round(4 * math.sqrt(len(items))))))
    n = min(n, len(items))
    buckets = max(1, min(n, 8))
    order = sorted(range(len(items)), key=lambda i: items[i]["date"])
    bucket_of = {i: min(buckets - 1, r * buckets // len(items)) for r, i in enumerate(order)}
    cap = max(1, math.ceil(2.0 * n / buckets))
    used = {}
    chosen = []
    for i in sorted(range(len(items)), key=lambda i: -items[i]["score"]):
        m = items[i]
        if used.get(bucket_of[i], 0) >= cap:
            continue
        if any(_same_shot(m, c) for c in chosen):
            continue
        chosen.append(m)
        used[bucket_of[i]] = used.get(bucket_of[i], 0) + 1
        if len(chosen) >= n:
            break
    return sorted(chosen, key=lambda m: (m["date"], m["id"]))


# ── plán střihu (Director) ───────────────────────────────────────────────────

def plan(chosen, beats=None, title="", subtitle="", key=""):
    """Časová osa sestřihu: [title, shot…, end] – každý střih na době skladby.
    beats = ambient_music.beat_map (None = pravidelný rytmus bez hudby)."""
    beats = beats if beats and float(beats.get("period") or 0) > 0 else DEFAULT_BEATS
    period = float(beats["period"])
    per_shot = max(1, round(SHOT_S / period))
    ranked = sorted(chosen, key=lambda m: -m.get("score", 0))
    heroes = {m["id"] for m in ranked[:max(1, int(len(chosen) * HERO_SHARE))]} if chosen else set()
    off = float(beats.get("offset") or 0.0) % period
    segs = [{"kind": "title", "dur": off + period * max(1, round(TITLE_BEATS_S / period))}]
    seed = _seed(key)
    for k, m in enumerate(chosen):
        w, h = m.get("w") or 0, m.get("h") or 0
        aspect = (w / h) if w and h else 1.5
        fit = "fill" if aspect >= 1.2 else "contain"           # na výšku: celá fotka na rozmazaném pozadí
        kb = director.ken_burns(m.get("focus"), seed + k, aspect_in=aspect if fit == "fill" else 16 / 9)
        segs.append({"kind": "shot", "id": m["id"], "path": m["path"], "thumb": m.get("thumb"),
                     "date": m.get("date", ""), "aspect": round(aspect, 4), "fit": fit, "kb": kb,
                     "dur": period * per_shot * (2 if m["id"] in heroes else 1), "hero": m["id"] in heroes})
    day = None                                # popisek dne na prvním záběru každého dne
    for g in segs[1:]:
        d = _parse_day(g["date"])
        if d and d != day and len(chosen) > 1:
            g["caption"] = _cz_date(d)
        day = d or day
    segs.append({"kind": "end", "dur": max(period, END_S)})
    total = director.layout(segs)
    grid = director.beat_grid(beats, total + period)
    downs = set(round(t, 6) for t in grid["downbeats"])
    for g in segs:
        g["downbeat"] = round(g["t0"], 6) in downs
        g["fade"] = DOWNBEAT_FADE_S if g["downbeat"] else CUT_FADE_S
    segs[0]["fade"] = 0.0
    return {"key": key, "title": title, "subtitle": subtitle, "segs": segs, "total": total,
            "beats": beats, "cuts": [g["t0"] for g in segs[1:]], "shots": len(chosen)}


def build(conn, suggestion, beats=None, today=None, text_model=None):
    """Návrh -> hotový plán sestřihu (kurátor + Director). None = málo snímků."""
    items = candidates(conn, suggestion["key"], today)
    if len(items) < MIN_SHOTS:
        return None
    quality = None
    try:
        import ai_engine
        quality = quality_vector(text_model or ai_engine.get_text_model())
    except Exception as e:
        print(f"[Reels] kvalita bez CLIP: {e}")
    chosen = curate(score(items, quality))
    if len(chosen) < MIN_SHOTS // 2:
        return None
    dates = [m["date"] for m in chosen]
    sub = suggestion.get("subtitle") or ""
    p = plan(chosen, beats, suggestion.get("title", ""), sub, suggestion["key"])
    p["range"] = (dates[0][:10], dates[-1][:10])
    return p
