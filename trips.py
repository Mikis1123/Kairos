"""Gate 2 B: adaptive trip clustering with stable event identity.

None of this needs configuration, and none of it assumes the user ever takes a photo at home.

0. **Night anchors** (`night_anchors`). Where was each night spent? Evidence, strongest first:
   a night-time photo (20:00-05:00), the day's last photo in the evening (>= 18:00), the next
   day's first photo in the morning (< 10:00). Anchors are clustered into places.

1. **Home inference** (`HomeModel`). Home at a given date is the anchor place that holds most
   of the nights within +/-180 days (at least HOME_MIN_NIGHTS nights and HOME_MIN_SHARE of
   them) AND whose nights spread over at least HOME_MIN_SPAN_DAYS. A lodging is one block of
   a few nights, so a week at a cottage is never mistaken for home. A move to a new flat shows
   up as a new dominant place, so home follows the data over time. The "away" radius comes
   from the spread of the home place itself. Libraries without home photos have no home.

1b. **Basecamps** (`find_stays`). A place other than home that anchors >= STAY_MIN_NIGHTS
   nights in one block (at most STAY_MAX_GAP_NIGHTS unevidenced nights in a row, no night
   evidenced elsewhere) is a stay: daily movement starts and returns there. The whole stay,
   arrival day to departure day, is one **journey** named after the hub, and each day (a day
   trip out of the hub) is a chapter. Works with or without a known home.

2. **Segmentation** (`segment`). Photo days ("day" = local date shifted back by 4 h, so
   02:00 still belongs to the evening before) are away when a GPS photo is beyond the away
   radius. Two consecutive away days are linked (one multi-day journey) when the night between
   them was spent away:
     * there is a night-time photo away from home, or
     * the next morning starts too early and too far for a drive from home, or
     * the morning picks up near where the evening ended (closer to each other than to home).
   A run of linked days is a **journey** (parent event) with one **chapter** per day. An
   unlinked away day is a **day trip**. Photos at home keep the classic gap clustering
   (**moment**), and so do days that are neither in a stay nor away from a known home.

3. **Stable identity** (`apply`). New clusters are matched to existing events by member
   overlap (greedy, largest overlap first). A matched cluster keeps its event id, chosen cover
   and tint, so covers, journeys, reels, memories and Echoes links survive a re-cluster. Only
   events that no cluster claims are deleted, and only after nothing references them.

4. **Migration** (`ensure_current`). ALGO_VERSION lives in app_meta; a library clustered by an
   older algorithm is re-clustered once at startup (stable ids keep covers and links).
"""
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# i18n-ok: stored data markers (localized at display time by i18n.event_title)
STORED_TRIP = "Výlet"  # i18n-ok: stored data marker
STORED_UNKNOWN_PLACE = "Neznámé místo"  # i18n-ok: stored data marker

KIND_MOMENT, KIND_DAY_TRIP, KIND_JOURNEY, KIND_CHAPTER = "moment", "day_trip", "journey", "chapter"

NIGHT_START_H, NIGHT_END_H = 20, 5      # night-time photo window (local time)
DAY_SHIFT_H = 4                         # 02:00 belongs to the previous day
NIGHT_CLUSTER_KM = 3.0                  # night anchors within this distance are one place
EVENING_H, MORNING_H = 18, 10           # last photo after / first photo before = where the night was
W_NIGHT, W_EVENING, W_MORNING = 3, 2, 1 # anchor strength (a night photo beats a guess)
HOME_WINDOW_DAYS = 180
HOME_MIN_NIGHTS = 3
HOME_MIN_SHARE = 0.25
HOME_MIN_SPAN_DAYS = 10                 # home recurs over weeks; a lodging is one block of nights
STAY_MIN_NIGHTS = 2                     # a basecamp anchors at least two nights...
STAY_MAX_GAP_NIGHTS = 2                 # ...with at most this many unevidenced nights in a row
ALGO_VERSION = 3                        # bump -> existing libraries re-cluster once (ensure_current)
AWAY_KM_MIN, AWAY_KM_MAX = 20.0, 60.0   # adaptive away radius bounds
DRIVE_KMH = 90.0                        # "could they have driven here since dawn?"
DAWN_H = 6.0
MAX_LINK_GAP_DAYS = 2                   # one photo-less day inside a journey is fine


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _has_gps(it):
    return it["lat"] is not None and it["lon"] is not None and not (it["lat"] == 0 and it["lon"] == 0)


def photo_day(dt):
    return (dt - timedelta(hours=DAY_SHIFT_H)).date()


def is_night(dt):
    return dt.hour >= NIGHT_START_H or dt.hour < NIGHT_END_H


# ── 1. home ──────────────────────────────────────────────────────────────────

class _Place:
    __slots__ = ("lat", "lon", "n", "pts")

    def __init__(self, lat, lon):
        self.lat, self.lon, self.n, self.pts = lat, lon, 0, []

    def add(self, lat, lon):
        self.n += 1
        self.lat += (lat - self.lat) / self.n
        self.lon += (lon - self.lon) / self.n
        self.pts.append((lat, lon))


def night_of(dt):
    """The night a photo belongs to: its evening's date (00:00-11:59 -> the night before)."""
    return dt.date() if dt.hour >= 12 else dt.date() - timedelta(days=1)


def night_anchors(items):
    """[(night date, lat, lon, weight)]: evidence of where each night was spent."""
    out, by_day = [], defaultdict(list)
    for it in items:
        if _has_gps(it):
            by_day[photo_day(it["dt"])].append(it)
    for day, its in by_day.items():
        has_night = False
        for it in its:
            if is_night(it["dt"]):
                out.append((night_of(it["dt"]), it["lat"], it["lon"], W_NIGHT))
                has_night = True
        first, last = its[0], its[-1]
        if not has_night and last["dt"].hour >= EVENING_H:
            out.append((day, last["lat"], last["lon"], W_EVENING))
        if not is_night(first["dt"]) and first["dt"].hour < MORNING_H:
            out.append((day - timedelta(days=1), first["lat"], first["lon"], W_MORNING))
    return out


class HomeModel:
    """Anchor places + where each night was spent; home = the long-running dominant place."""

    def __init__(self, places, nights, strong=()):
        self.places = places                # [_Place]
        self.nights = sorted(nights)        # [(night date, place index)], one place per night
        self.strong = set(strong)           # nights backed by a night-time photo
        self._cache = {}

    @classmethod
    def fit(cls, items):
        places, votes = [], defaultdict(Counter)
        for night, lat, lon, w in night_anchors(sorted(items, key=lambda it: it["dt"])):
            best, best_d = None, None
            for i, p in enumerate(places):
                d = haversine_km(p.lat, p.lon, lat, lon)
                if d <= NIGHT_CLUSTER_KM and (best_d is None or d < best_d):
                    best, best_d = i, d
            if best is None:
                places.append(_Place(lat, lon))
                best = len(places) - 1
            places[best].add(lat, lon)
            votes[night][best] += w
        nights = [(night, c.most_common(1)[0][0]) for night, c in votes.items()]
        strong = [night for night, c in votes.items() if c.most_common(1)[0][1] >= W_NIGHT]
        return cls(places, nights, strong)

    def home_at(self, day):
        """(lat, lon, away_km) of home around this date, or None when the data can't tell."""
        key = day.toordinal() // 7                 # weekly resolution is plenty
        if key in self._cache:
            return self._cache[key]
        lo, hi = day - timedelta(days=HOME_WINDOW_DAYS), day + timedelta(days=HOME_WINDOW_DAYS)
        window = [(d, p) for d, p in self.nights if lo <= d <= hi]
        counts = Counter(p for _d, p in window)
        res = None
        if counts:
            pid, n = counts.most_common(1)[0]
            dates = [d for d, p in window if p == pid]
            span = (max(dates) - min(dates)).days
            if n >= HOME_MIN_NIGHTS and n / len(window) >= HOME_MIN_SHARE and span >= HOME_MIN_SPAN_DAYS:
                p = self.places[pid]
                spread = sorted(haversine_km(p.lat, p.lon, a, b) for a, b in p.pts)
                p90 = spread[int(0.9 * (len(spread) - 1))] if spread else 0.0
                away = min(AWAY_KM_MAX, max(AWAY_KM_MIN, 8.0 * p90))
                res = (p.lat, p.lon, away)
        self._cache[key] = res
        return res

    def has_home(self):
        return any(self.home_at(d) for d, _ in self.nights[:: max(1, len(self.nights) // 12)])


class Stay:
    """A block of nights at one non-home place: the basecamp of a journey."""
    __slots__ = ("place", "nights")

    def __init__(self, place, nights):
        self.place, self.nights = place, nights

    def covers(self, day):
        """Arrival day .. departure day (the morning after the last night)."""
        return self.nights[0] <= day <= self.nights[-1] + timedelta(days=1)

    def __repr__(self):
        return f"Stay({self.nights[0]}..{self.nights[-1]}, {len(self.nights)} nights)"


def find_stays(model):
    """Basecamps: >= STAY_MIN_NIGHTS nights at one place that isn't home, small gaps only."""
    stays, run = [], []

    def close():
        if len(run) >= STAY_MIN_NIGHTS:
            p = model.places[run[0][1]]
            home = model.home_at(run[0][0])
            if home is None or haversine_km(home[0], home[1], p.lat, p.lon) > home[2]:
                stays.append(Stay(p, [d for d, _ in run]))
        run.clear()

    nights = model.nights                   # sorted by date, one place per night
    for i, (night, pid) in enumerate(nights):
        if run and pid != run[-1][1] and night not in model.strong:
            # a lone guess elsewhere (last photo of a day trip at 18:30) doesn't end a stay
            # when the next night is back at the hub within the gap allowance
            nxt = nights[i + 1] if i + 1 < len(nights) else None
            if nxt is not None and nxt[1] == run[-1][1] and \
                    (nxt[0] - run[-1][0]).days <= STAY_MAX_GAP_NIGHTS + 1:
                continue
        if run and (pid != run[-1][1] or (night - run[-1][0]).days > STAY_MAX_GAP_NIGHTS + 1):
            close()
        run.append((night, pid))
    close()
    return stays


# ── 2. segmentation ─────────────────────────────────────────────────────────

def classic_clusters(items):
    """The original gap clustering (time gaps, plus distance jumps after a pause)."""
    clusters, cur = [], []
    last_dt = last_lat = last_lon = None
    for it in items:
        dt = it["dt"]
        if not cur:
            cur = [it]
        else:
            hours = (dt - last_dt).total_seconds() / 3600.0
            if hours > 26.0:
                clusters.append(cur)
                cur = [it]
            elif _has_gps(it) and last_lat is not None:
                if haversine_km(last_lat, last_lon, it["lat"], it["lon"]) > 45.0 and hours > 8.0:
                    clusters.append(cur)
                    cur = [it]
                else:
                    cur.append(it)
            elif hours > 14.0:
                clusters.append(cur)
                cur = [it]
            else:
                cur.append(it)
        last_dt = dt
        if _has_gps(it):
            last_lat, last_lon = it["lat"], it["lon"]
    if cur:
        clusters.append(cur)
    return clusters


class Cluster:
    def __init__(self, kind, items, children=None, hub=None):
        self.kind = kind
        self.items = items                  # sorted by time
        self.children = children or []      # chapters of a journey
        self.hub = hub                      # basecamp _Place of a journey, if it had one

    @property
    def ids(self):
        return {it["id"] for it in self.items}

    def __repr__(self):
        return f"Cluster({self.kind}, {len(self.items)} items, {len(self.children)} chapters)"


def _day_info(day, its, home):
    gps = [it for it in its if _has_gps(it)]
    info = {"day": day, "items": its, "away": False, "home": home}
    if home is None or not gps:
        return info
    hlat, hlon, away_km = home
    dist = [(haversine_km(hlat, hlon, it["lat"], it["lon"]), it) for it in gps]
    info["away"] = max(d for d, _ in dist) > away_km
    info["first"], info["last"] = gps[0], gps[-1]
    info["dist_first"], info["dist_last"] = dist[0][0], dist[-1][0]
    info["night_away"] = any(is_night(it["dt"]) and d > away_km for d, it in dist)
    info["at_home_late"] = any(d <= away_km and (it["dt"].hour >= 17 or it["dt"].hour < DAY_SHIFT_H)
                               for d, it in dist)
    info["at_home_early"] = any(d <= away_km and it["dt"].hour < 12 for d, it in dist)
    # photos at home before leaving / after getting back belong to home, not to the trip
    near = {it["id"] for d, it in dist if d <= away_km}
    far = [i for i, it in enumerate(its) if _has_gps(it) and it["id"] not in near]
    if far:
        info["head"] = [it for it in its[:far[0]] if it["id"] in near]
        info["tail"] = [it for it in its[far[-1] + 1:] if it["id"] in near]
    return info


def slept_away(a, b):
    """Was the night between away-days a and b spent away from home?"""
    if (b["day"] - a["day"]).days > MAX_LINK_GAP_DAYS:
        return False
    if a.get("at_home_late") or b.get("at_home_early"):
        return False                        # a photo at home in between = they went home
    if a.get("night_away") or b.get("night_away"):
        return True
    first = b.get("first")
    if first is not None:
        hours_since_dawn = first["dt"].hour + first["dt"].minute / 60.0 - DAWN_H
        if b["dist_first"] > DRIVE_KMH * max(0.25, hours_since_dawn):
            return True                     # too far, too early to have left home this morning
        last = a.get("last")
        if last is not None:
            jump = haversine_km(last["lat"], last["lon"], first["lat"], first["lon"])
            if jump < 0.5 * min(a["dist_last"], b["dist_first"]):
                return True                 # morning continues where the evening ended
    return False


def _fill_unknown_days(days):
    """A day without GPS between two linked away days belongs to that journey."""
    for i in range(1, len(days) - 1):
        d = days[i]
        if d["home"] is not None and "first" not in d and days[i - 1]["away"] and days[i + 1]["away"]:
            if slept_away(days[i - 1], days[i + 1]):
                d["away"] = True
                d["bridge"] = True


def _stays_linked(a, b):
    """Same basecamp, or the next basecamp of a road trip (moved on without going home)."""
    sa, sb = a.get("stay"), b.get("stay")
    if sa is None or sb is None:
        return False
    return sa is sb or 0 <= (sb.nights[0] - sa.nights[-1]).days <= STAY_MAX_GAP_NIGHTS + 1


def segment(items):
    """items: [{"id", "dt", "lat", "lon", "city"}] sorted by dt -> [Cluster]."""
    items = sorted(items, key=lambda it: (it["dt"], it["id"]))
    if not items:
        return []
    home = HomeModel.fit(items)
    stays = find_stays(home)
    if not stays and not home.has_home():
        return [Cluster(KIND_MOMENT, c) for c in classic_clusters(items)]
    by_day = defaultdict(list)
    for it in items:
        by_day[photo_day(it["dt"])].append(it)
    days = [_day_info(d, by_day[d], home.home_at(d)) for d in sorted(by_day)]
    for d in days:
        stay = next((st for st in stays if st.covers(d["day"])), None)
        if stay is not None:
            d["stay"], d["away"] = stay, True
            d.pop("head", None)                 # the hub isn't home: nothing to trim off
            d.pop("tail", None)
    _fill_unknown_days(days)

    out, home_buf, run = [], [], []

    def flush_home():
        if home_buf:
            out.extend(Cluster(KIND_MOMENT, c) for c in classic_clusters(home_buf))
            home_buf.clear()

    def flush_run():
        if not run:
            return
        head = {it["id"] for it in run[0].get("head", ())}
        tail = {it["id"] for it in run[-1].get("tail", ())}
        home_buf.extend(it for it in run[0]["items"] if it["id"] in head)
        flush_home()
        day_items = [[it for it in d["items"] if it["id"] not in head and it["id"] not in tail] for d in run]
        stays_in_run = {id(d["stay"]): d["stay"] for d in run if d.get("stay") is not None}
        hub = next(iter(stays_in_run.values())).place if len(stays_in_run) == 1 else None
        if len(run) == 1:
            out.append(Cluster(KIND_DAY_TRIP, day_items[0]))
        else:
            chapters = [Cluster(KIND_CHAPTER, its) for its in day_items if its]
            out.append(Cluster(KIND_JOURNEY, [it for its in day_items for it in its], chapters, hub=hub))
        home_buf.extend(it for it in run[-1]["items"] if it["id"] in tail)
        run.clear()

    for d in days:
        if not d["away"]:
            flush_run()
            home_buf.extend(d["items"])
            continue
        if run and not (_stays_linked(run[-1], d) or d.get("bridge") or run[-1].get("bridge")
                        or slept_away(run[-1], d)):
            flush_run()
        run.append(d)
    flush_run()
    flush_home()
    out.sort(key=lambda c: c.items[0]["dt"])
    return out


# ── titles (stored canonical form; i18n.event_title localizes) ───────────────

def hub_city(items, hub):
    """The basecamp's place name: the commonest city among the photos taken at the hub."""
    if hub is None:
        return None
    near = [it["city"] for it in items if _has_gps(it) and it.get("city")
            and it["city"] != STORED_UNKNOWN_PLACE
            and haversine_km(hub.lat, hub.lon, it["lat"], it["lon"]) <= NIGHT_CLUSTER_KM]
    return Counter(near).most_common(1)[0][0] if near else None


def title_for(items, hub=None):
    start, end = items[0]["dt"], items[-1]["dt"]
    if start.date() == end.date():
        label = start.strftime("%d. %m. %Y")
    else:
        label = f"{start.strftime('%d. %m.')} – {end.strftime('%d. %m. %Y')}"
    cities = [it["city"] for it in items if it.get("city") and it["city"] != STORED_UNKNOWN_PLACE]
    if not cities:
        return f"{STORED_TRIP} ({label})", STORED_UNKNOWN_PLACE
    dominant = hub_city(items, hub) or Counter(cities).most_common(1)[0][0]
    if len(set(cities)) > 1:
        return f"{STORED_TRIP}: {dominant} a okolí ({label})", dominant  # i18n-ok: stored title
    return f"{STORED_TRIP}: {dominant} ({label})", dominant


# ── 3. persistence with stable ids ──────────────────────────────────────────

def ensure_schema(cursor):
    cols = {r[1] for r in cursor.execute("PRAGMA table_info(events)")}
    for name, decl in (("kind", "TEXT"), ("parent_id", "INTEGER")):
        if name not in cols:
            cursor.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
    mcols = {r[1] for r in cursor.execute("PRAGMA table_info(media_items)")}
    if "chapter_id" not in mcols:
        cursor.execute("ALTER TABLE media_items ADD COLUMN chapter_id INTEGER")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_chapter ON media_items(chapter_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_id)")


def match_ids(clusters, old_members):
    """Greedy max-overlap matching. old_members: {event_id: set(media ids)} -> [id or None]."""
    owner = {}
    for eid, mids in old_members.items():
        for m in mids:
            owner[m] = eid
    pairs = []
    for i, c in enumerate(clusters):
        for eid, n in Counter(owner[m] for m in c.ids if m in owner).items():
            pairs.append((n, -abs(len(old_members[eid]) - len(c.items)), i, eid))
    pairs.sort(reverse=True)
    result, used = [None] * len(clusters), set()
    for _n, _size, i, eid in pairs:
        if result[i] is None and eid not in used:
            result[i] = eid
            used.add(eid)
    return result


def load_items(cursor):
    rows = cursor.execute("""
        SELECT id, date_taken, latitude, longitude, place_city FROM media_items
        WHERE date_taken IS NOT NULL AND date_taken != '' AND COALESCE(is_hidden, 0) = 0
        ORDER BY date_taken ASC
    """).fetchall()
    items = []
    for mid, dt_str, lat, lon, city in rows:
        try:
            dt = datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(str(dt_str)[:16], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
        items.append({"id": int(mid), "dt": dt, "lat": lat, "lon": lon,
                      "city": city or STORED_UNKNOWN_PLACE})
    return items


def apply(conn, clusters):
    """Writes clusters as events, reusing ids. Returns stats. Caller commits."""
    cur = conn.cursor()
    ensure_schema(cur)
    old_top = defaultdict(set)
    old_ch = defaultdict(set)
    for mid, eid, chid in cur.execute("SELECT id, event_id, chapter_id FROM media_items "
                                      "WHERE event_id IS NOT NULL OR chapter_id IS NOT NULL"):
        if eid is not None:
            old_top[int(eid)].add(int(mid))
        if chid is not None:
            old_ch[int(chid)].add(int(mid))
    existing = {int(r[0]): r for r in cur.execute(
        "SELECT id, cover_media_id, kind, parent_id FROM events")}
    # chapters may only reuse chapter ids, top-level events only top-level ids
    old_top = {k: v for k, v in old_top.items() if k in existing and existing[k][2] != KIND_CHAPTER}
    old_ch = {k: v for k, v in old_ch.items() if k in existing and existing[k][2] == KIND_CHAPTER}

    stats = {"kept": 0, "created": 0, "deleted": 0}
    keep = set()

    def write(c, eid, parent_id):
        title, location = title_for(c.items, c.hub)
        start, end = c.items[0]["dt"].strftime("%Y-%m-%d %H:%M"), c.items[-1]["dt"].strftime("%Y-%m-%d %H:%M")
        if eid is not None:
            cover = existing[eid][1]
            if cover is not None and int(cover) not in c.ids:
                cover = None                   # the chosen cover left this event
            cur.execute("UPDATE events SET title = ?, location_name = ?, start_date = ?, end_date = ?, "
                        "item_count = ?, cover_media_id = ?, kind = ?, parent_id = ? WHERE id = ?",
                        (title, location, start, end, len(c.items), cover, c.kind, parent_id, eid))
            stats["kept"] += 1
        else:
            cur.execute("INSERT INTO events (title, location_name, start_date, end_date, item_count, kind, parent_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (title, location, start, end, len(c.items), c.kind, parent_id))
            eid = cur.lastrowid
            stats["created"] += 1
        keep.add(eid)
        return eid

    top_ids = match_ids(clusters, old_top)
    item_event, item_chapter = {}, {}
    chapters_all, chapter_parent = [], []
    for c, eid in zip(clusters, top_ids):
        eid = write(c, eid, None)
        for it in c.items:
            item_event[it["id"]] = eid
        for ch in c.children:
            chapters_all.append(ch)
            chapter_parent.append(eid)
    for ch, chid, parent in zip(chapters_all, match_ids(chapters_all, old_ch), chapter_parent):
        chid = write(ch, chid, parent)
        for it in ch.items:
            item_chapter[it["id"]] = chid

    cur.execute("UPDATE media_items SET event_id = NULL, chapter_id = NULL "
                "WHERE event_id IS NOT NULL OR chapter_id IS NOT NULL")
    cur.executemany("UPDATE media_items SET event_id = ?, chapter_id = ? WHERE id = ?",
                    [(eid, item_chapter.get(mid), mid) for mid, eid in item_event.items()])
    gone = [eid for eid in existing if eid not in keep]
    if gone:
        cur.executemany("DELETE FROM events WHERE id = ?", [(e,) for e in gone])
    stats["deleted"] = len(gone)
    return stats


def recluster(conn):
    """Full re-cluster of the library (scanner entry point). Returns stats."""
    items = load_items(conn.cursor())
    if not items:
        return {"kept": 0, "created": 0, "deleted": 0, "kinds": {}}   # nothing dated: keep events
    clusters = segment(items)
    stats = apply(conn, clusters)
    stats["kinds"] = dict(Counter(c.kind for c in clusters))
    return stats


def ensure_current(conn):
    """Re-clusters once when the library was clustered by an older ALGO_VERSION.
    Returns the recluster stats, or None when nothing had to be done. Caller commits."""
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM app_meta WHERE key = 'trips_algo'").fetchone()
    if row is not None and str(row[0]) == str(ALGO_VERSION):
        return None
    stats = recluster(conn)
    conn.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('trips_algo', ?)",
                 (str(ALGO_VERSION),))
    return stats
