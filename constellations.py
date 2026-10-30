"""Konstelace (Sprint 4): soukromá síť vztahů z tváří – vše offline, nic neopouští počítač.

Uzel = osoba (people) s aspoň MIN_PHOTOS fotkami. Hrana = lidé spolu na fotkách (face_instances
na stejném médiu). Síla hrany = počet společných fotek, normalizovaná podle méně častého z obou
(spolu na 20 z 25 fotek = blízký vztah, i když jeden má fotek 500). Rozvržení = silově řízené
(Fruchterman–Reingold) s pevným semínkem – stejná knihovna, stejná obloha.

Společný příběh (story): chvíle, kdy byli dva lidé spolu – po událostech (nebo dnech u fotek
bez události), chronologicky, s obálkou a počtem fotek; první a poslední společná fotka.
"""
import math
import zlib
from collections import Counter, defaultdict

from database import visible_media_sql

MIN_PHOTOS = 3
MIN_TOGETHER = 2              # hrana až od dvou společných fotek (jedna = náhoda v davu)
LAYOUT_ITERS = 300


def _people(conn):
    rows = conn.execute(f"""
        SELECT p.id, p.name, p.cover_face_path, COALESCE(p.is_favorite, 0), COUNT(DISTINCT f.media_id)
        FROM people p JOIN face_instances f ON f.person_id = p.id
        JOIN media_items m ON m.id = f.media_id
        WHERE COALESCE(f.is_valid, 1) = 1 AND {visible_media_sql('m')}
        GROUP BY p.id HAVING COUNT(DISTINCT f.media_id) >= {MIN_PHOTOS}""").fetchall()
    return {int(pid): {"id": int(pid), "name": name or "", "cover": cover, "fav": bool(fav), "photos": int(n)}
            for pid, name, cover, fav, n in rows}


def display_name(person, k=None):
    name = (person.get("name") or "").strip()
    if name and name.lower() not in ("neznámá osoba", "unknown"):
        return name
    return f"Osoba {k}" if k else "Neznámá osoba"


def graph(conn):
    """{nodes: [{id, name, photos, fav, cover, x, y}], edges: [{a, b, n, w}]}"""
    people = _people(conn)
    ids = set(people)
    per_media = defaultdict(set)
    for mid, pid in conn.execute(f"""
            SELECT f.media_id, f.person_id FROM face_instances f JOIN media_items m ON m.id = f.media_id
            WHERE f.person_id IS NOT NULL AND COALESCE(f.is_valid, 1) = 1 AND {visible_media_sql('m')}"""):
        if pid in ids:
            per_media[mid].add(int(pid))
    together = Counter()
    for ps in per_media.values():
        ps = sorted(ps)
        for i in range(len(ps)):
            for j in range(i + 1, len(ps)):
                together[(ps[i], ps[j])] += 1
    edges = []
    for (a, b), n in together.items():
        if n >= MIN_TOGETHER:
            w = n / max(1, min(people[a]["photos"], people[b]["photos"]))
            edges.append({"a": a, "b": b, "n": n, "w": round(min(1.0, w), 4)})
    edges.sort(key=lambda e: -e["n"])
    order = sorted(people.values(), key=lambda p: -p["photos"])
    unnamed = 0
    for p in order:
        if display_name(p) == "Neznámá osoba":
            unnamed += 1
            p["label"] = display_name(p, unnamed)
        else:
            p["label"] = display_name(p)
    nodes = order
    pos = layout([p["id"] for p in nodes], edges)
    for p in nodes:
        p["x"], p["y"] = pos[p["id"]]
        p["degree"] = sum(1 for e in edges if p["id"] in (e["a"], e["b"]))
    return {"nodes": nodes, "edges": edges}


def layout(ids, edges, iters=LAYOUT_ITERS, seed=None):
    """Fruchterman–Reingold s váhami hran -> {id: (x, y)} v intervalu −1…1. Deterministické."""
    import numpy as np
    n = len(ids)
    if n == 0:
        return {}
    if n == 1:
        return {ids[0]: (0.0, 0.0)}
    index = {pid: k for k, pid in enumerate(ids)}
    rng = np.random.default_rng(seed if seed is not None else zlib.crc32(",".join(map(str, sorted(ids))).encode()))
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pos = np.stack([np.cos(ang), np.sin(ang)], axis=1) + rng.normal(0, 0.05, (n, 2))
    ea = np.array([index[e["a"]] for e in edges], dtype=int)
    eb = np.array([index[e["b"]] for e in edges], dtype=int)
    ew = np.array([0.4 + 1.6 * e["w"] for e in edges], dtype=float)
    k = math.sqrt(4.0 / n)
    temp = 0.25
    for _ in range(iters):
        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.maximum(1e-3, np.linalg.norm(delta, axis=2))
        disp = (delta / dist[..., None] * (k * k / dist)[..., None]).sum(axis=1)         # odpuzování
        if len(ea):
            d = pos[ea] - pos[eb]
            dl = np.maximum(1e-3, np.linalg.norm(d, axis=1))
            f = (d / dl[:, None]) * (dl * dl / k * ew)[:, None]                          # přitažlivost
            np.add.at(disp, ea, -f)
            np.add.at(disp, eb, f)
        disp -= pos * 0.08 * np.linalg.norm(pos, axis=1, keepdims=True)                   # gravitace ke středu
        length = np.maximum(1e-6, np.linalg.norm(disp, axis=1, keepdims=True))
        pos += disp / length * np.minimum(length, temp)
        temp = max(0.005, temp * 0.985)
    pos -= pos.mean(axis=0)
    scale = np.abs(pos).max() or 1.0
    pos /= scale
    return {pid: (round(float(pos[index[pid], 0]), 4), round(float(pos[index[pid], 1]), 4)) for pid in ids}


def story(conn, a, b):
    """Společné chvíle dvou osob: {together, first, last, moments: [{key, title, date, count, ids, cover}]}."""
    rows = conn.execute(f"""
        SELECT m.id, m.date_taken, m.event_id, m.thumbnail_path, e.title, COALESCE(m.sharpness_score, 0),
               COALESCE(m.is_favorite, 0)
        FROM media_items m LEFT JOIN events e ON e.id = m.event_id
        WHERE {visible_media_sql('m')}
          AND EXISTS (SELECT 1 FROM face_instances f WHERE f.media_id = m.id AND f.person_id = ?)
          AND EXISTS (SELECT 1 FROM face_instances f WHERE f.media_id = m.id AND f.person_id = ?)
        ORDER BY m.date_taken, m.id""", (int(a), int(b))).fetchall()
    from journey import iso_minute
    from reels import clean_title
    groups = {}
    for mid, taken, eid, thumb, title, sharp, fav in rows:
        d = iso_minute(taken)
        key = f"e{eid}" if eid is not None else f"d{d[:10]}"
        g = groups.setdefault(key, {"key": key, "event_id": eid, "title": clean_title(title) if title else "",
                                    "date": d, "end": d, "ids": [], "best": None})
        g["ids"].append(int(mid))
        g["end"] = d
        score = 3 * fav + math.log1p(sharp)
        if g["best"] is None or score > g["best"][0]:
            g["best"] = (score, int(mid), thumb)
    moments = sorted(groups.values(), key=lambda g: g["date"])
    for g in moments:
        g["count"] = len(g["ids"])
        g["cover"] = g["best"][2] if g["best"] else None
        g["cover_id"] = g["best"][1] if g["best"] else None
        del g["best"]
    dates = [iso_minute(r[1]) for r in rows if r[1]]
    return {"together": len(rows), "first": dates[0] if dates else "", "last": dates[-1] if dates else "",
            "moments": moments}


def companions(g, pid, limit=8):
    """Nejbližší lidé osoby pid podle společných fotek: [(id, n)]."""
    out = [(e["b"] if e["a"] == pid else e["a"], e["n"]) for e in g["edges"] if pid in (e["a"], e["b"])]
    return sorted(out, key=lambda x: -x[1])[:limit]
