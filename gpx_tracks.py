"""Gate 2 C: GPX track import, bound automatically to the matching trip.

A GPX file (watch, phone app, bike computer) holds the line that was actually travelled. It
is imported once into the library (`gpx_tracks`; the file itself is not needed afterwards).
When a Journey plays, each leg between two stops takes the slice of the track recorded
between leaving one stop and arriving at the next, so the line is the true path 1:1 and not
a reconstruction.

Binding needs no setup:
* GPX times are UTC and photo times are local wall-clock time. The offset is estimated from
  the data: the candidate offset (15-minute steps, +/-14 h) that puts the trip's GPS photos
  closest to the track wins. If no photo can tell, the system time zone at that date is used.
* A track belongs to every event whose time span it overlaps. It is looked up when the
  Journey is built, so it stays bound even after events are re-clustered.
"""
import calendar
import hashlib
import json
import math
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

MAX_STORED_POINTS = 20000
MAX_LEG_POINTS = 1500
OFFSET_STEP_MIN = 15
OFFSET_RANGE_H = 14
GOOD_MATCH_KM = 5.0


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


class GpxError(ValueError):
    pass


def _parse_time(s):
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        return dt.timestamp()
    return float(calendar.timegm(dt.timetuple()))     # GPX without zone = UTC by the spec


def parse_gpx(data):
    """GPX bytes -> (name, [(lat, lon, utc epoch)]) sorted by time; track points with a time."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise GpxError(f"not a GPX file: {e}") from e
    if not root.tag.lower().endswith("gpx"):
        raise GpxError("not a GPX file")
    ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
    name = None
    for path in (f"{ns}metadata/{ns}name", f"{ns}trk/{ns}name", f"{ns}name"):
        el = root.find(path)
        if el is not None and (el.text or "").strip():
            name = el.text.strip()
            break
    pts = []
    for tag in ("trkpt", "rtept"):
        for el in root.iter(f"{ns}{tag}"):
            try:
                lat, lon = float(el.get("lat")), float(el.get("lon"))
            except (TypeError, ValueError):
                continue
            t = _parse_time(el.findtext(f"{ns}time"))
            if t is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            pts.append((lat, lon, t))
        if pts:
            break
    if len(pts) < 2:
        raise GpxError("the GPX file has no timed track points")
    pts.sort(key=lambda p: p[2])
    return name, pts


def downsample(pts, limit):
    if len(pts) <= limit:
        return list(pts)
    step = (len(pts) - 1) / (limit - 1)
    return [pts[round(i * step)] for i in range(limit)]


def position_at(pts, t):
    """Interpolated (lat, lon) on the track at utc t, or None outside it."""
    if not pts or t < pts[0][2] or t > pts[-1][2]:
        return None
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][2] <= t:
            lo = mid
        else:
            hi = mid
    a, b = pts[lo], pts[hi]
    span = b[2] - a[2]
    f = 0.0 if span <= 0 else (t - a[2]) / span
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f


def _naive_epoch(local_str):
    """'2026-07-09 10:30[:00]' (local wall clock) -> epoch as if it were UTC."""
    s = str(local_str or "")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16)):
        try:
            return float(calendar.timegm(datetime.strptime(s[:n], fmt).timetuple()))
        except ValueError:
            continue
    return None


def system_offset_min(utc_epoch):
    try:
        return int(round(time.localtime(utc_epoch).tm_gmtoff / 60))
    except (OverflowError, OSError, ValueError):
        return 0


def estimate_offset_min(pts, photos):
    """photos = [(local date string, lat, lon)]. Returns (offset minutes, matched, median km)."""
    samples = [(_naive_epoch(d), lat, lon) for d, lat, lon in photos
               if lat is not None and lon is not None]
    samples = [s for s in samples if s[0] is not None]
    best = (None, 0, math.inf)
    for step in range(-OFFSET_RANGE_H * 60 // OFFSET_STEP_MIN, OFFSET_RANGE_H * 60 // OFFSET_STEP_MIN + 1):
        off = step * OFFSET_STEP_MIN
        dists = []
        for t_local, lat, lon in samples:
            p = position_at(pts, t_local - off * 60)
            if p is not None:
                dists.append(haversine_km(lat, lon, p[0], p[1]))
        if len(dists) < 2:
            continue
        dists.sort()
        med = dists[len(dists) // 2]
        if (med, -len(dists)) < (best[2], -best[1]):
            best = (off, len(dists), med)
    if best[0] is None or best[2] > GOOD_MATCH_KM:
        return system_offset_min(pts[0][2]), 0, best[2]
    return best


# ── storage ──────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gpx_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            source TEXT,
            fingerprint TEXT UNIQUE,
            start_utc REAL, end_utc REAL,
            tz_offset_min INTEGER,
            start_local TEXT, end_local TEXT,
            points TEXT,
            imported_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_gpx_span ON gpx_tracks(start_local, end_local)")


def _local_str(utc, off_min):
    return _utc(utc + off_min * 60).strftime("%Y-%m-%d %H:%M:%S")


def _candidate_photos(conn, pts):
    """GPS photos within a day of the track (local time unknown yet, so be generous)."""
    lo = _utc(pts[0][2] - 86400).strftime("%Y-%m-%d %H:%M:%S")
    hi = _utc(pts[-1][2] + 86400).strftime("%Y-%m-%d %H:%M:%S")
    return conn.execute("""
        SELECT date_taken, latitude, longitude FROM media_items
        WHERE latitude IS NOT NULL AND date_taken >= ? AND date_taken <= ?
        ORDER BY date_taken LIMIT 4000
    """, (lo, hi)).fetchall()


def import_gpx(conn, data, source=""):
    """Imports one GPX file. Returns {id, name, points, duplicate, event_id, event_title, offset_min}.
    Raises GpxError for files without a timed track. Caller commits."""
    ensure_schema(conn)
    name, pts = parse_gpx(data)
    fp = hashlib.sha1(data).hexdigest()
    row = conn.execute("SELECT id, name FROM gpx_tracks WHERE fingerprint = ?", (fp,)).fetchone()
    if row is not None:
        res = {"id": int(row[0]), "name": row[1], "points": len(pts), "duplicate": True}
    else:
        off, _n, _med = estimate_offset_min(pts, _candidate_photos(conn, pts))
        stored = downsample(pts, MAX_STORED_POINTS)
        cur = conn.execute("""
            INSERT INTO gpx_tracks (name, source, fingerprint, start_utc, end_utc, tz_offset_min,
                                    start_local, end_local, points, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name or source, source, fp, pts[0][2], pts[-1][2], off, _local_str(pts[0][2], off),
              _local_str(pts[-1][2], off), json.dumps([[round(a, 6), round(b, 6), round(t, 1)] for a, b, t in stored]),
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        res = {"id": int(cur.lastrowid), "name": name or source, "points": len(pts), "duplicate": False}
    ev = best_event(conn, res["id"])
    res["event_id"], res["event_title"] = (ev if ev else (None, None))
    tr_row = conn.execute("SELECT tz_offset_min FROM gpx_tracks WHERE id = ?", (res["id"],)).fetchone()
    res["offset_min"] = int(tr_row[0]) if tr_row else 0
    return res


def best_event(conn, track_id):
    """(event id, title) of the top-level event the track overlaps most, or None."""
    row = conn.execute("SELECT start_local, end_local FROM gpx_tracks WHERE id = ?", (track_id,)).fetchone()
    if row is None:
        return None
    s, e = row
    try:
        kind_filter = "AND COALESCE(kind, '') != 'chapter'" if _has_kind(conn) else ""
        rows = conn.execute(f"""
            SELECT id, title, start_date, end_date FROM events
            WHERE start_date <= ? AND end_date >= ? {kind_filter}
        """, (e[:16], s[:16])).fetchall()
    except Exception:
        return None
    best, best_ov = None, -1.0
    ts, te = _naive_epoch(s), _naive_epoch(e)
    for eid, title, es, ee in rows:
        a, b = _naive_epoch(es), _naive_epoch(ee)
        if a is None or b is None:
            continue
        ov = min(te, b + 60) - max(ts, a)
        if ov > best_ov:
            best, best_ov = (int(eid), title), ov
    return best


def _has_kind(conn):
    return any(r[1] == "kind" for r in conn.execute("PRAGMA table_info(events)"))


def tracks_for_span(conn, start_local, end_local):
    """[(offset_min, points)] of tracks overlapping a local time span."""
    try:
        rows = conn.execute("""
            SELECT tz_offset_min, points FROM gpx_tracks
            WHERE start_local <= ? AND end_local >= ? ORDER BY start_utc
        """, (str(end_local)[:16] + ":59", str(start_local)[:16])).fetchall()
    except Exception:
        return []                                  # older library without the table
    out = []
    for off, raw in rows:
        try:
            out.append((int(off or 0), [tuple(p) for p in json.loads(raw)]))
        except (TypeError, ValueError):
            continue
    return out


def _simplify(pts, limit):
    return downsample(pts, limit) if len(pts) > limit else pts


def leg_tracks(conn, stops):
    """Journey stops -> {leg index: [[lon, lat], ...]} from imported tracks (true lines)."""
    if len(stops) < 2:
        return {}
    tracks = tracks_for_span(conn, stops[0].get("start"), stops[-1].get("end") or stops[-1].get("start"))
    if not tracks:
        return {}
    out = {}
    for i in range(1, len(stops)):
        A, B = stops[i - 1], stops[i]
        t0, t1 = _naive_epoch(A.get("end") or A.get("start")), _naive_epoch(B.get("start"))
        if t0 is None or t1 is None or t1 <= t0:
            continue
        for off, pts in tracks:
            u0, u1 = t0 - off * 60, t1 - off * 60 + 59        # stop times are whole minutes
            seg = [p for p in pts if u0 <= p[2] <= u1]
            if len(seg) < 2:
                continue
            line = [[round(p[1], 6), round(p[0], 6)] for p in _simplify(seg, MAX_LEG_POINTS)]
            ex, en = A.get("exit") or A, B.get("entry") or B
            try:
                line.insert(0, [round(float(ex["lon"]), 6), round(float(ex["lat"]), 6)])
                line.append([round(float(en["lon"]), 6), round(float(en["lat"]), 6)])
            except (KeyError, TypeError, ValueError):
                pass
            out[i] = line
            break
    return out


def list_tracks(conn):
    ensure_schema(conn)
    return conn.execute("SELECT id, name, start_local, end_local FROM gpx_tracks ORDER BY start_utc DESC").fetchall()


def delete_track(conn, track_id):
    ensure_schema(conn)
    conn.execute("DELETE FROM gpx_tracks WHERE id = ?", (int(track_id),))
