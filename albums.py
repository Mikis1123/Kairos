"""Alba (Sprint 2): ruční alba (vybrané položky) a chytrá alba (uložené hledání).

Ruční album = řádky album_items; položka v koši se v albu nezobrazuje, ale z alba nezmizí
(obnovení z koše ji vrátí). Chytré album = uložený dotaz hledání + kolekce (vše / oblíbené /
fotky / videa) – otevře se jako hledání v mřížce Fotografie, takže výsledky vždy odpovídají
aktuální knihovně a modelu hledání.

Alba se do XMP sidecarů nezapisují: jsou organizace knihovny, ne vlastnost fotky – chrání je
denní záloha databáze (database.backup_database) stejně jako osoby.
"""
from datetime import datetime

SMART_FILTERS = ("all", "favorites", "photo", "video")


def ensure_tables(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'manual',
            query TEXT,
            filter TEXT,
            created_at TEXT,
            cover_media_id INTEGER
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS album_items (
            album_id INTEGER NOT NULL,
            media_id INTEGER NOT NULL,
            added_at TEXT,
            PRIMARY KEY (album_id, media_id),
            FOREIGN KEY (album_id) REFERENCES albums (id) ON DELETE CASCADE,
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_album_items_media ON album_items (media_id)")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_name(name):
    n = " ".join((name or "").split())
    if not n:
        raise ValueError("Album musí mít název")
    return n[:120]


def create_album(conn, name, media_ids=()):
    cur = conn.execute("INSERT INTO albums (name, kind, created_at) VALUES (?, 'manual', ?)",
                       (_clean_name(name), _now()))
    aid = cur.lastrowid
    add_items(conn, aid, media_ids)
    return aid


def create_smart_album(conn, name, query, filter_key="all"):
    q = " ".join((query or "").split())
    if not q:
        raise ValueError("Chytré album potřebuje hledaný výraz")
    if filter_key not in SMART_FILTERS:
        filter_key = "all"
    cur = conn.execute("INSERT INTO albums (name, kind, query, filter, created_at) VALUES (?, 'smart', ?, ?, ?)",
                       (_clean_name(name), q, filter_key, _now()))
    return cur.lastrowid


def rename_album(conn, album_id, name):
    conn.execute("UPDATE albums SET name = ? WHERE id = ?", (_clean_name(name), album_id))


def delete_album(conn, album_id):
    conn.execute("DELETE FROM album_items WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))


def add_items(conn, album_id, media_ids):
    """Vrací počet NOVĚ přidaných položek (už přítomné se nepočítají)."""
    now = _now()
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO album_items (album_id, media_id, added_at) VALUES (?, ?, ?)",
                     [(album_id, int(m), now) for m in dict.fromkeys(media_ids)])
    return conn.total_changes - before


def remove_items(conn, album_id, media_ids):
    conn.executemany("DELETE FROM album_items WHERE album_id = ? AND media_id = ?",
                     [(album_id, int(m)) for m in media_ids])
    row = conn.execute("SELECT cover_media_id FROM albums WHERE id = ?", (album_id,)).fetchone()
    if row and row[0] is not None and int(row[0]) in {int(m) for m in media_ids}:
        conn.execute("UPDATE albums SET cover_media_id = NULL WHERE id = ?", (album_id,))


def set_cover(conn, album_id, media_id):
    conn.execute("UPDATE albums SET cover_media_id = ? WHERE id = ?", (media_id, album_id))


def get_album(conn, album_id):
    row = conn.execute("SELECT id, name, kind, query, filter FROM albums WHERE id = ?", (album_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "kind": row[2], "query": row[3], "filter": row[4] or "all"}


def item_ids(conn, album_id):
    """Viditelné položky ručního alba (bez koše a Skrytých), nejstarší první podle data pořízení."""
    return [r[0] for r in conn.execute("""
        SELECT a.media_id FROM album_items a JOIN media_items m ON m.id = a.media_id
        WHERE a.album_id = ? AND COALESCE(m.is_trashed, 0) = 0 AND COALESCE(m.is_hidden, 0) = 0
        ORDER BY m.date_taken, m.id""", (album_id,))]


def list_albums(conn):
    """[{id, name, kind, query, filter, count, cover}] – ruční alba s počtem a obálkou
    (zvolená, jinak nejnovější položka), chytrá bez počtu (počítá se hledáním)."""
    out = []
    for aid, name, kind, query, flt, cover_id in conn.execute(
            "SELECT id, name, kind, query, filter, cover_media_id FROM albums ORDER BY name COLLATE NOCASE, id"):
        count, cover = None, None
        if kind == "manual":
            count = conn.execute("""SELECT COUNT(*) FROM album_items a JOIN media_items m ON m.id = a.media_id
                                    WHERE a.album_id = ? AND COALESCE(m.is_trashed, 0) = 0 AND COALESCE(m.is_hidden, 0) = 0""", (aid,)).fetchone()[0]
            row = None
            if cover_id is not None:
                row = conn.execute("SELECT thumbnail_path FROM media_items WHERE id = ? AND COALESCE(is_trashed, 0) = 0 AND COALESCE(is_hidden, 0) = 0",
                                   (cover_id,)).fetchone()
            if not row:
                row = conn.execute("""SELECT m.thumbnail_path FROM album_items a JOIN media_items m ON m.id = a.media_id
                                      WHERE a.album_id = ? AND COALESCE(m.is_trashed, 0) = 0 AND COALESCE(m.is_hidden, 0) = 0
                                      ORDER BY m.date_taken DESC, m.id DESC LIMIT 1""", (aid,)).fetchone()
            cover = row[0] if row else None
        out.append({"id": aid, "name": name, "kind": kind, "query": query, "filter": flt or "all",
                    "count": count, "cover": cover})
    return out


def albums_for_media(conn, media_id):
    return [r[0] for r in conn.execute("SELECT album_id FROM album_items WHERE media_id = ?", (media_id,))]


def czech_albums(n):
    return f"{n} {'album' if n == 1 else 'alba' if 2 <= n <= 4 else 'alb'}"
