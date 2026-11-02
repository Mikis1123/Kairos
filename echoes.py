"""Ozvěny: tehdy a teď (první podpisová funkce, 2026-10-30).

Fotky ze stejného místa a se stejnou kompozicí, pořízené s odstupem let – návrat na
vyhlídku, stejný pohled z okna chalupy, děti na stejných schodech. Hledá se bez sítě i bez
nového modelu, z dat, která knihovna už má:
  * poloha: kandidáti do ECHO_RADIUS_M (mřížka buněk ~ poloměr, porovnává se jen 3 × 3 okolí),
  * čas: odstup aspoň MIN_GAP_DAYS (jiný rok, ne druhý den téhož výletu),
  * kompozice: kosinus CLIP vektorů (image_embeddings) ≥ MIN_SIMILARITY; pHash (rozložení
    jasu = skutečná kompozice) přidá bonus, když se záběry kryjí i geometricky.
Páry se spojí do míst (union-find); z každého roku místa se vezme nejlepší snímek, takže
ozvěna je řada „2019 → 2022 → 2026“, ne stovka sérií.

align_pair() srovná dva snímky stejné scény (ORB + RANSAC, podobnostní transformace), aby
posuvník tehdy / teď přecházel mezi záběry bez skoku.
"""
from i18n import tr
import math
from collections import defaultdict
from datetime import datetime

from database import visible_media_sql

ECHO_RADIUS_M = 150
MIN_GAP_DAYS = 300
MIN_SIMILARITY = 0.84            # CLIP: stejný záběr ~0,88+, stejné místo jiným směrem ~0,7–0,8
PHASH_CLOSE = 22                 # Hammingova vzdálenost 64bit pHash = podobné rozložení scény
PHASH_BONUS = 0.03
MAX_ECHOES = 80
MAX_ITEMS = 60000                # pojistka paměti (512 × float32 = 2 kB na fotku)


def _parse_date(text):
    """'2023-07-06 08:10:00' i EXIF '2023:07:06 08:10:00' (i bez sekund / času) -> datetime."""
    s = str(text or "").strip()
    if s[4:5] == ":":
        s = s.replace(":", "-", 2)
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def load_candidates(conn):
    """Fotky (i Live Photo) s polohou, datem a vektorem -> (items, matice vektorů)."""
    import numpy as np
    rows = conn.execute(f"""
        SELECT m.id, m.latitude, m.longitude, m.date_taken, m.thumbnail_path, m.file_path,
               m.place_city, m.phash, m.is_favorite, e.embedding
        FROM media_items m JOIN image_embeddings e ON e.media_id = m.id
        WHERE m.latitude IS NOT NULL AND m.longitude IS NOT NULL AND m.date_taken IS NOT NULL
          AND m.media_type IN ('photo', 'live_photo') AND {visible_media_sql('m')}
        LIMIT {MAX_ITEMS}
    """).fetchall()
    items, vecs, dim = [], [], None
    for mid, lat, lon, date, thumb, path, city, phash, fav, emb in rows:
        when = _parse_date(date)
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if when is None or not emb or (lat == 0 and lon == 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        v = np.frombuffer(emb, dtype=np.float32)
        if dim is None:
            dim = v.size
        if v.size != dim:
            continue
        items.append({"id": int(mid), "lat": lat, "lon": lon, "when": when, "thumb": thumb or "",
                      "path": path or "", "city": city or "", "phash": phash, "fav": bool(fav)})
        vecs.append(v)
    if not items:
        return [], np.zeros((0, 1), dtype=np.float32)
    m = np.stack(vecs).astype(np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-6)
    return items, m


def _cells(items, radius_m):
    """Mřížka buněk o hraně ~radius_m (délka zkrácená cos(šířky)) -> {(i, j): [index]}."""
    deg_lat = radius_m / 111320.0
    grid = defaultdict(list)
    for k, it in enumerate(items):
        deg_lon = deg_lat / max(0.05, math.cos(math.radians(it["lat"])))
        grid[(int(math.floor(it["lat"] / deg_lat)), int(math.floor(it["lon"] / deg_lon)))].append(k)
    return grid


def candidate_pairs(items, vecs, radius_m=ECHO_RADIUS_M, min_gap_days=MIN_GAP_DAYS, min_sim=MIN_SIMILARITY):
    """[(a, b, skóre)] dvojic napříč roky: blízko, s odstupem a se stejnou kompozicí."""
    import numpy as np
    grid = _cells(items, radius_m)
    days = np.array([it["when"].toordinal() for it in items], dtype=np.int64)
    lat = np.radians(np.array([it["lat"] for it in items]))
    lon = np.radians(np.array([it["lon"] for it in items]))
    ph, has_ph = _phash_array(items)
    pairs = []
    for (ci, cj), members in grid.items():
        near = [k for di in (-1, 0, 1) for dj in (-1, 0, 1) for k in grid.get((ci + di, cj + dj), ())]
        a_idx = np.array(members)
        b_idx = np.array(near)
        sims = vecs[a_idx] @ vecs[b_idx].T
        gap = np.abs(days[a_idx][:, None] - days[b_idx][None, :]) >= min_gap_days
        later = a_idx[:, None] < b_idx[None, :]                  # každou dvojici jen jednou
        x, y = np.nonzero((sims >= min_sim - PHASH_BONUS) & gap & later)
        if not x.size:
            continue
        a, b = a_idx[x], b_idx[y]
        # vzdálenost (haversine) a pHash pro všechny kandidáty naráz – v hustém místě jich jsou statisíce
        h = np.sin((lat[b] - lat[a]) / 2) ** 2 + np.cos(lat[a]) * np.cos(lat[b]) * np.sin((lon[b] - lon[a]) / 2) ** 2
        dist = 2 * 6371000.0 * np.arcsin(np.minimum(1.0, np.sqrt(h)))
        close = has_ph[a] & has_ph[b] & (_popcount(ph[a] ^ ph[b]) <= PHASH_CLOSE)
        score = sims[x, y] + PHASH_BONUS * close
        keep = (dist <= radius_m) & (score >= min_sim)
        pairs.extend(zip(a[keep].tolist(), b[keep].tolist(), score[keep].astype(float).tolist()))
    return pairs


def _phash_array(items):
    import numpy as np
    vals, ok = np.zeros(len(items), dtype=np.uint64), np.zeros(len(items), dtype=bool)
    for k, it in enumerate(items):
        try:
            vals[k] = np.uint64(int(it["phash"], 16) & 0xFFFFFFFFFFFFFFFF)
            ok[k] = True
        except (TypeError, ValueError):
            pass
    return vals, ok


def _popcount(v):
    import numpy as np
    return np.unpackbits(v.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def _groups(n, pairs):
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b, _ in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    out = defaultdict(set)
    for a, b, _ in pairs:
        out[find(a)].update((a, b))
    return list(out.values())


def years_text(n):
    return tr("count-years-span", count=n)


def find_echoes(conn, radius_m=ECHO_RADIUS_M, min_gap_days=MIN_GAP_DAYS, min_sim=MIN_SIMILARITY,
                limit=MAX_ECHOES):
    """Ozvěny knihovny, nejsilnější první: [{key, lat, lon, place, span_years, score,
    items: [{id, date, year, thumb, path}] (od nejstaršího)}]."""
    import numpy as np
    items, vecs = load_candidates(conn)
    if len(items) < 2:
        return []
    pairs = candidate_pairs(items, vecs, radius_m, min_gap_days, min_sim)
    if not pairs:
        return []
    score_of = defaultdict(float)                 # součet podobností k ostatním rokům (reprezentativnost)
    best_pair = defaultdict(float)
    for a, b, s in pairs:
        score_of[a] += s
        score_of[b] += s
        best_pair[a] = max(best_pair[a], s)
        best_pair[b] = max(best_pair[b], s)
    echoes = []
    for members in _groups(len(items), pairs):
        by_year = defaultdict(list)
        for k in members:
            by_year[items[k]["when"].year].append(k)
        if len(by_year) < 2:
            continue                                # odstup ≥ MIN_GAP, ale přes Silvestra – stejný „rok“ nevadí
        chain = []
        for year in sorted(by_year):
            k = max(by_year[year], key=lambda q: (items[q]["fav"], score_of[q], -q))
            chain.append(k)
        first, last = items[chain[0]], items[chain[-1]]
        span = max(1, round((last["when"] - first["when"]).days / 365.25))
        lat = float(np.mean([items[k]["lat"] for k in chain]))
        lon = float(np.mean([items[k]["lon"] for k in chain]))
        cities = [items[k]["city"] for k in chain if items[k]["city"]]
        echoes.append({
            "key": "-".join(str(items[k]["id"]) for k in chain),
            "lat": lat, "lon": lon, "place": max(set(cities), key=cities.count) if cities else "",
            "span_years": span, "score": round(max(best_pair[k] for k in chain), 4),
            "items": [{"id": items[k]["id"], "date": items[k]["when"].strftime("%Y-%m-%d"),
                       "year": items[k]["when"].year, "thumb": items[k]["thumb"], "path": items[k]["path"]}
                      for k in chain],
        })
    # nejdřív delší oblouk času, pak přesnější shoda kompozice
    echoes.sort(key=lambda e: (-(min(e["span_years"], 10) + 4 * e["score"]), e["key"]))
    return echoes[:limit]


def subtitle(echo):
    years = [it["year"] for it in echo["items"]]
    parts = [f"{years[0]} – {years[-1]}", tr("echoes-span-between", span=years_text(echo["span_years"]))]
    if len(years) > 2:
        parts.append(tr("count-returns", count=len(years)))
    return " · ".join(parts)


# ── srovnání záběrů (tehdy na teď) ────────────────────────────────────────────

ALIGN_EDGE = 1024
ALIGN_MIN_INLIERS = 24
ALIGN_SCALE = (0.6, 1.7)
ALIGN_MAX_ROT_DEG = 20


def align_pair(then_rgb, now_rgb):
    """Matice 2×3 (numpy), která převede snímek „tehdy“ do souřadnic snímku „teď“, nebo None,
    když se scény spolehlivě nekryjí. Vstup: numpy pole HxWx3 (libovolná velikost)."""
    import cv2
    import numpy as np

    def small(img):
        h, w = img.shape[:2]
        k = min(1.0, ALIGN_EDGE / float(max(h, w)))
        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        return (cv2.resize(g, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA) if k < 1 else g), k
    a, ka = small(then_rgb)
    b, kb = small(now_rgb)
    orb = cv2.ORB_create(2500)
    pa, da = orb.detectAndCompute(a, None)
    pb, db = orb.detectAndCompute(b, None)
    if da is None or db is None or len(pa) < ALIGN_MIN_INLIERS or len(pb) < ALIGN_MIN_INLIERS:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [m[0] for m in matches if len(m) == 2 and m[0].distance < 0.78 * m[1].distance]
    if len(good) < ALIGN_MIN_INLIERS:
        return None
    src = np.float32([pa[m.queryIdx].pt for m in good]) / ka          # zpět do plného rozlišení
    dst = np.float32([pb[m.trainIdx].pt for m in good]) / kb
    mat, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=4.0 / kb,
                                               maxIters=4000, confidence=0.995)
    if mat is None or inliers is None or int(inliers.sum()) < ALIGN_MIN_INLIERS:
        return None
    scale = math.hypot(mat[0, 0], mat[1, 0])
    rot = math.degrees(math.atan2(mat[1, 0], mat[0, 0]))
    if not (ALIGN_SCALE[0] <= scale <= ALIGN_SCALE[1]) or abs(rot) > ALIGN_MAX_ROT_DEG:
        return None
    return mat
