"""Vypůjčená poloha (Sprint 2): fotky bez GPS (zrcadlovka, kompakt, sken z akce) dostanou polohu
z fotek s GPS (telefon), pořízených ve stejné události ve stejnou chvíli.

Pravidla:
  * dárce = položka se zapsanou polohou ze souboru (ne jiná vypůjčená – žádné řetězení chyb),
    ze stejné události (event_id), časový rozdíl ≤ WINDOW_S;
  * jsou-li dárci před i po (oba v okně) a leží-li blízko sebe (≤ INTERP_MAX_KM), poloha se
    lineárně interpoluje podle času – fotka z procházky mezi dvěma snímky z telefonu;
  * jinak nejbližší dárce v čase, ale jen do NEAREST_S (dál je jistota příliš malá);
  * zapisuje se gps_source = 'borrowed' (a XMP sidecar, viz xmp_sync) – původní GPS ze souboru
    se nikdy nepřepíše a vypůjčenou jde zase odebrat (clear_borrowed).
"""
import math
from datetime import datetime

WINDOW_S = 30 * 60          # dárci pro interpolaci
NEAREST_S = 10 * 60         # jediný dárce: nejvýš tak daleko v čase
INTERP_MAX_KM = 5.0         # dárci před a po dál od sebe = mezi nimi cesta autem, neinterpolovat


def _ts(s):
    try:
        return datetime.strptime((s or "")[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _km(a, b):
    r = math.pi / 180
    dlat, dlon = (b[0] - a[0]) * r, (b[1] - a[1]) * r
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin(dlon / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def ensure_column(conn):
    if not any(r[1] == "gps_source" for r in conn.execute("PRAGMA table_info(media_items)")):
        conn.execute("ALTER TABLE media_items ADD COLUMN gps_source TEXT")


def estimate(target_t, donors):
    """donors = [(t, lat, lon)] seřazené podle času. Vrací (lat, lon) nebo None."""
    before = [d for d in donors if d[0] <= target_t and target_t - d[0] <= WINDOW_S]
    after = [d for d in donors if d[0] > target_t and d[0] - target_t <= WINDOW_S]
    b = before[-1] if before else None
    a = after[0] if after else None
    if b and a and _km(b[1:], a[1:]) <= INTERP_MAX_KM:
        span = a[0] - b[0]
        u = (target_t - b[0]) / span if span > 0 else 0.0
        return (b[1] + (a[1] - b[1]) * u, b[2] + (a[2] - b[2]) * u)
    best = min((d for d in (b, a) if d), key=lambda d: abs(d[0] - target_t), default=None)
    if best and abs(best[0] - target_t) <= NEAREST_S:
        return (best[1], best[2])
    return None


def borrow(conn, media_ids=None):
    """Doplní polohu fotkám bez GPS. media_ids = jen tyto (jinak celá knihovna).
    Vrací seznam id, kterým byla poloha vypůjčena. Volající commituje."""
    ensure_column(conn)
    rows = conn.execute("""
        SELECT id, event_id, date_taken, latitude, longitude, gps_source FROM media_items
        WHERE event_id IS NOT NULL AND date_taken IS NOT NULL AND date_taken != ''
          AND COALESCE(is_trashed, 0) = 0""").fetchall()
    wanted = set(int(i) for i in media_ids) if media_ids is not None else None
    donors, targets = {}, []
    for mid, ev, dt, lat, lon, src in rows:
        t = _ts(dt)
        if t is None:
            continue
        if lat is not None and lon is not None:
            if src in (None, "", "exif"):
                donors.setdefault(ev, []).append((t, lat, lon))
        elif wanted is None or mid in wanted:
            targets.append((mid, ev, t))
    for d in donors.values():
        d.sort()
    updates = []
    for mid, ev, t in targets:
        est = estimate(t, donors.get(ev, []))
        if est:
            updates.append((est[0], est[1], mid))
    conn.executemany("UPDATE media_items SET latitude = ?, longitude = ?, gps_source = 'borrowed', "
                     "place_city = NULL, place_country = NULL WHERE id = ? AND latitude IS NULL", updates)
    return [u[2] for u in updates]


def clear_borrowed(conn):
    """Odebere všechny vypůjčené polohy (vrací počet)."""
    ensure_column(conn)
    cur = conn.execute("UPDATE media_items SET latitude = NULL, longitude = NULL, place_city = NULL, "
                       "place_country = NULL, gps_source = NULL WHERE gps_source = 'borrowed'")
    return cur.rowcount
