"""Kairos – Cesta: přehrávání výletu na mapě (Batch 6b).

Událost (events) s fotkami s polohou -> zastávky v časovém pořadí. Zastávka = souvislý úsek
fotek do STOP_RADIUS_KM od jejího těžiště; návrat na dřívější místo je nová zastávka (trasa,
ne seznam míst). Příliš mnoho zastávek se sloučí větším poloměrem, aby přehrávání nebylo
nekonečné. Data jdou do window.playJourney() v map_vault.html.

Túra (fotky po stovkách metrů) dostane zastávku skoro u každé vyhlídky (nejmenší poloměr
0,4 km) – kamera pak jede po skutečné stezce mezi nimi (map_vault.html trailLeg). Na každé
zastávce se ukáže jediný nejlepší snímek (best_shot: oblíbená > série > ostrost).
"""
import math
from collections import Counter

from database import visible_media_sql

STOP_RADII_KM = (0.4, 1.5, 4.0, 10.0, 25.0, 60.0, 150.0)   # zkouší se postupně, dokud zastávek není moc
MAX_STOPS = 24
MIN_STOPS = 2                 # jedna zastávka není cesta – kamera by neměla kam letět
POLAROIDS_PER_STOP = 4        # fotky zastávky v datech (pás / náhledy); ukazuje se best
MAX_JOURNEY_ROWS = 5000
BURST_HAMMING = 10            # pHash do této vzdálenosti = stejný záběr (série / duplikát)
BURST_BONUS = 0.6             # váha velikosti série (log2) proti ostrosti (log)
FAVORITE_BONUS = 3.0
VIDEO_PENALTY = 1.0


def haversine_km(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _valid(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180) \
            or (lat == 0 and lon == 0):
        return None
    return lat, lon


def group_stops(photos, radius_km):
    """photos = [{lat, lon, date, ...}] seřazené podle času -> [[photo, ...], ...]."""
    stops, cur, centre = [], [], None
    for p in photos:
        here = (p["lat"], p["lon"])
        if cur and haversine_km(centre, here) <= radius_km:
            cur.append(p)
            n = len(cur)
            centre = (centre[0] + (here[0] - centre[0]) / n, centre[1] + (here[1] - centre[1]) / n)
            continue
        if cur:
            stops.append(cur)
        cur, centre = [p], here
    if cur:
        stops.append(cur)
    return stops


def pick_spread(items, k):
    """k položek rovnoměrně přes celý úsek (ne prvních k ze stejné minuty)."""
    if len(items) <= k:
        return list(items)
    step = (len(items) - 1) / (k - 1) if k > 1 else 0
    return [items[round(i * step)] for i in range(k)]


def _hamming(a, b):
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def best_shot(photos):
    """Jediný snímek, který zastávku reprezentuje. Série (pHash ≈ stejný záběr) znamená, že
    fotografa něco zaujalo -> bonus podle její velikosti; v ní vyhrává nejostřejší (Laplace).
    Oblíbená vždy vyhraje, video (náhled = 1 snímek) prohrává s fotkou."""
    if not photos:
        return None
    groups = []                                 # [(reprezentativní pHash, [photo, ...])]
    for p in photos:
        h = p.get("phash")
        for rep, members in groups:
            if h and rep and _hamming(h, rep) <= BURST_HAMMING:
                members.append(p)
                break
        else:
            groups.append((h, [p]))
    size = {id(p): len(members) for _rep, members in groups for p in members}

    def score(p):
        sharp = max(0.0, float(p.get("sharpness") or 0.0))
        return (FAVORITE_BONUS * bool(p.get("fav")) + math.log1p(sharp)
                + BURST_BONUS * math.log2(size[id(p)]) - VIDEO_PENALTY * (p.get("type") == "video"))
    return max(photos, key=lambda p: (score(p), -photos.index(p)))


def _public(p):
    out = {"id": p["id"], "thumb": p["thumb"], "date": p["date"]}
    if p.get("focus"):
        out["focus"] = p["focus"]
    return out


def face_focus(boxes):
    """Cesta 3.0 (C5): kam má Ken Burns mířit – střed tváří vážený plochou (větší tvář = blíž
    a důležitější), v rozměrech fotky 0–1. boxes = [(x, y, w, h)] normalizované; None = bez tváří."""
    total = sx = sy = 0.0
    for x, y, w, h in boxes:
        try:
            x, y, w, h = float(x), float(y), float(w), float(h)
        except (TypeError, ValueError):
            continue
        if not (w > 0 and h > 0) or not (-0.1 <= x <= 1.1 and -0.1 <= y <= 1.1):
            continue
        a = w * h
        total += a
        sx += a * (x + w / 2)
        sy += a * (y + h / 2)
    if total <= 0:
        return None
    return [round(min(1.0, max(0.0, sx / total)), 3), round(min(1.0, max(0.0, sy / total)), 3)]


def _event_focus(conn, event_id):
    """{media_id: [fx, fy]} pro fotky události s rozpoznanými tvářemi (jeden dotaz)."""
    try:
        rows = conn.execute("""
            SELECT f.media_id, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h
            FROM face_instances f JOIN media_items m ON m.id = f.media_id
            WHERE m.event_id = ? AND f.bbox_w IS NOT NULL
        """, (event_id,)).fetchall()
    except Exception:
        return {}                   # starší / testovací DB bez tabulky tváří
    boxes = {}
    for mid, x, y, w, h in rows:
        boxes.setdefault(int(mid), []).append((x, y, w, h))
    return {mid: f for mid, f in ((m, face_focus(b)) for m, b in boxes.items()) if f}


def _hero(p):
    """Nejlepší snímek zastávky: i ostrý obraz (náhled 2048 px), je-li hotový – miniatura
    360 px roztažená na kartu polaroidu byla rozmazaná."""
    out = _public(p)
    out["image"] = p.get("image") or p["thumb"]
    out["type"] = p.get("type") or "photo"      # Live Photo / video: karta dostane klip (card_media)
    return out


def build_stops(photos):
    """Fotky s polohou (seřazené podle času) -> zastávky; nejmenší poloměr, který dá ≤ MAX_STOPS."""
    groups = []
    for radius in STOP_RADII_KM:
        groups = group_stops(photos, radius)
        if len(groups) <= MAX_STOPS:
            break
    stops = []
    for g in groups:
        lat = sum(p["lat"] for p in g) / len(g)
        lon = sum(p["lon"] for p in g) / len(g)
        cities = Counter(p["city"] for p in g if p.get("city"))
        stops.append({
            "lat": lat, "lon": lon,
            "start": g[0]["date"], "end": g[-1]["date"],
            "city": cities.most_common(1)[0][0] if cities else "",
            "count": len(g),
            # skutečný příjezd a odjezd (první a poslední fotka): trasa mezi zastávkami vede tudy,
            # ne pravítkem mezi těžišti (map_vault.html legPath)
            "entry": {"lat": round(g[0]["lat"], 6), "lon": round(g[0]["lon"], 6)},
            "exit": {"lat": round(g[-1]["lat"], 6), "lon": round(g[-1]["lon"], 6)},
            "photos": [_public(p) for p in pick_spread(g, POLAROIDS_PER_STOP)],
            "best": _hero(best_shot(g)),
        })
    return stops


def iso_minute(value):
    """'2023:07:06 08:10:00' (EXIF) i '2023-07-06 08:10:00' -> '2023-07-06 08:10'."""
    s = str(value or "")[:16]
    return s.replace(":", "-", 2) if s[4:5] == ":" else s


def _geo_photos(conn, event_id, thumb_url, image_url=None):
    rows = conn.execute(f"""
        SELECT m.id, m.latitude, m.longitude, m.place_city, m.date_taken, m.thumbnail_path,
               m.phash, m.sharpness_score, m.is_favorite, m.media_type, m.file_path
        FROM media_items m
        WHERE m.event_id = ? AND m.latitude IS NOT NULL AND m.date_taken IS NOT NULL
          AND {visible_media_sql('m')}
        LIMIT {MAX_JOURNEY_ROWS}
    """, (event_id,)).fetchall()
    focus = _event_focus(conn, event_id)
    photos = []
    for mid, lat, lon, city, date, thumb, phash, sharp, fav, mtype, path in rows:
        ll = _valid(lat, lon)
        if ll is None:
            continue
        photos.append({"id": int(mid), "lat": ll[0], "lon": ll[1], "city": city or "",
                       "date": iso_minute(date), "thumb": thumb_url(thumb),
                       "image": image_url(path) if image_url else "",
                       "phash": phash, "sharpness": sharp or 0.0, "fav": bool(fav), "type": mtype,
                       "focus": focus.get(int(mid))})
    # řadit až po převodu: EXIF „2023:07:06“ by v SQL skončilo za ISO „2023-07-06“
    photos.sort(key=lambda p: (p["date"], p["id"]))
    return photos


def list_journeys(conn):
    """Události, ze kterých jde přehrát cesta (≥ 2 místa), od nejnovější – pro výběr v mapě."""
    rows = conn.execute(f"""
        SELECT e.id, e.title, e.location_name, e.start_date, e.end_date, COUNT(m.id),
               MIN(m.latitude), MAX(m.latitude), MIN(m.longitude), MAX(m.longitude)
        FROM events e JOIN media_items m ON m.event_id = e.id
        WHERE m.latitude IS NOT NULL AND m.date_taken IS NOT NULL AND {visible_media_sql('m')}
        GROUP BY e.id HAVING COUNT(m.id) >= {MIN_STOPS}
        ORDER BY e.start_date DESC, e.id DESC
    """).fetchall()
    out = []
    for eid, title, location, start, end, count, la0, la1, lo0, lo1 in rows:
        if None in (la0, la1, lo0, lo1):
            continue
        # rychlý filtr: všechny fotky na jednom místě (< nejmenší poloměr) = žádná cesta
        if haversine_km((la0, lo0), (la1, lo1)) < STOP_RADII_KM[0]:
            continue
        out.append({"id": int(eid), "title": title or "Cesta", "location": location or "",
                    "start": start or "", "end": end or "", "count": int(count)})
    return out


def build_journey(conn, event_id, thumb_url=lambda p: "", image_url=None):
    """Data pro window.playJourney(), nebo None, když událost nemá aspoň dvě místa.
    image_url(cesta originálu) -> URL ostrého náhledu pro nejlepší snímek zastávky ('' = není)."""
    ev = conn.execute("SELECT id, title, location_name, start_date, end_date FROM events WHERE id = ?",
                      (int(event_id),)).fetchone()
    if ev is None:
        return None
    stops = build_stops(_geo_photos(conn, ev[0], thumb_url, image_url))
    if len(stops) < MIN_STOPS:
        return None
    location = ev[2] or next((s["city"] for s in stops if s["city"]), "")
    return {"id": int(ev[0]), "title": ev[1] or "Cesta", "location": location,
            "start": stops[0]["start"], "end": stops[-1]["end"],
            "count": sum(s["count"] for s in stops), "stops": stops}
