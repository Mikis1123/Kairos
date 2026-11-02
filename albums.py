"""Alba (Sprint 2, Stage 4 = Alba v2): ruční alba, chytrá alba a složky.

Ruční album = řádky album_items; položka v koši se v albu nezobrazuje, ale z alba nezmizí
(obnovení z koše ji vrátí). Chytré album = uložený dotaz hledání + kolekce (vše / oblíbené /
fotky / videa) – otevře se jako hledání v mřížce Fotografie, takže výsledky vždy odpovídají
aktuální knihovně a modelu hledání.

Alba v2: stromová hierarchie – albums.kind = 'folder' je složka, parent_id ukazuje na složku
(NULL = kořen). position drží ruční pořadí mezi sourozenci (přetažení karet), u položek alba
album_items.position pořadí uvnitř alba, když sort_mode = 'custom' (jinak podle data).
Smazání složky nic nemaže: její obsah se přesune o úroveň výš.

Alba se do XMP sidecarů nezapisují: jsou organizace knihovny, ne vlastnost fotky – chrání je
denní záloha databáze (database.backup_database) stejně jako osoby.
"""
from i18n import tr
from datetime import datetime

SMART_FILTERS = ("all", "favorites", "photo", "video")
SORT_MODES = ("date", "custom")
FOLDER_COVERS = 4                       # mozaika obálky složky (2 × 2)
_VISIBLE = "COALESCE(m.is_trashed, 0) = 0 AND COALESCE(m.is_hidden, 0) = 0"


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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
    # Alba v2 – migrace existujících knihoven (sloupce jen přibývají)
    cols = _columns(conn, "albums")
    for name, decl in (("parent_id", "INTEGER"), ("position", "INTEGER"), ("sort_mode", "TEXT DEFAULT 'date'")):
        if name not in cols:
            conn.execute(f"ALTER TABLE albums ADD COLUMN {name} {decl}")
    if "position" not in _columns(conn, "album_items"):
        conn.execute("ALTER TABLE album_items ADD COLUMN position INTEGER")
    if "position" not in cols:                   # první spuštění v2: pořadí = dosavadní abecední
        for i, (aid,) in enumerate(conn.execute(
                "SELECT id FROM albums ORDER BY name COLLATE NOCASE, id").fetchall()):
            conn.execute("UPDATE albums SET position = ? WHERE id = ?", (i, aid))


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean_name(name):
    n = " ".join((name or "").split())
    if not n:
        raise ValueError(tr("albums-error-name-required"))
    return n[:120]


def _check_parent(conn, parent_id):
    if parent_id is None:
        return None
    row = conn.execute("SELECT kind FROM albums WHERE id = ?", (parent_id,)).fetchone()
    if not row or row[0] != "folder":
        raise ValueError(tr("albums-error-target-folder"))
    return int(parent_id)


def _next_position(conn, parent_id):
    row = conn.execute("SELECT MAX(position) FROM albums WHERE parent_id IS ?", (parent_id,)).fetchone()
    return (row[0] + 1) if row and row[0] is not None else 0


def _insert(conn, name, kind, parent_id=None, query=None, filter_key=None):
    parent_id = _check_parent(conn, parent_id)
    cur = conn.execute("""INSERT INTO albums (name, kind, query, filter, created_at, parent_id, position, sort_mode)
                          VALUES (?, ?, ?, ?, ?, ?, ?, 'date')""",
                       (_clean_name(name), kind, query, filter_key, _now(), parent_id,
                        _next_position(conn, parent_id)))
    return cur.lastrowid


def create_album(conn, name, media_ids=(), parent_id=None):
    aid = _insert(conn, name, "manual", parent_id)
    add_items(conn, aid, media_ids)
    return aid


def create_smart_album(conn, name, query, filter_key="all", parent_id=None):
    q = " ".join((query or "").split())
    if not q:
        raise ValueError(tr("albums-error-smart-query"))
    if filter_key not in SMART_FILTERS:
        filter_key = "all"
    return _insert(conn, name, "smart", parent_id, q, filter_key)


def create_folder(conn, name, parent_id=None):
    return _insert(conn, name, "folder", parent_id)


def rename_album(conn, album_id, name):
    conn.execute("UPDATE albums SET name = ? WHERE id = ?", (_clean_name(name), album_id))


def delete_album(conn, album_id):
    """Smaže album (fotky v knihovně zůstanou). Složka: obsah se přesune do nadřazené úrovně."""
    row = conn.execute("SELECT kind, parent_id FROM albums WHERE id = ?", (album_id,)).fetchone()
    if row and row[0] == "folder":
        for child in [r[0] for r in conn.execute(
                "SELECT id FROM albums WHERE parent_id = ? ORDER BY position, id", (album_id,)).fetchall()]:
            conn.execute("UPDATE albums SET parent_id = ?, position = ? WHERE id = ?",
                         (row[1], _next_position(conn, row[1]), child))
    conn.execute("DELETE FROM album_items WHERE album_id = ?", (album_id,))
    conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))


# ── položky ručního alba ─────────────────────────────────────────────────────

def add_items(conn, album_id, media_ids):
    """Vrací počet NOVĚ přidaných položek (už přítomné se nepočítají). Nové jdou na konec
    ručního pořadí."""
    now = _now()
    row = conn.execute("SELECT MAX(position) FROM album_items WHERE album_id = ?", (album_id,)).fetchone()
    start = (row[0] + 1) if row and row[0] is not None else 0
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO album_items (album_id, media_id, added_at, position) VALUES (?, ?, ?, ?)",
                     [(album_id, int(m), now, start + i) for i, m in enumerate(dict.fromkeys(media_ids))])
    return conn.total_changes - before


def remove_items(conn, album_id, media_ids):
    conn.executemany("DELETE FROM album_items WHERE album_id = ? AND media_id = ?",
                     [(album_id, int(m)) for m in media_ids])
    row = conn.execute("SELECT cover_media_id FROM albums WHERE id = ?", (album_id,)).fetchone()
    if row and row[0] is not None and int(row[0]) in {int(m) for m in media_ids}:
        conn.execute("UPDATE albums SET cover_media_id = NULL WHERE id = ?", (album_id,))


def set_cover(conn, album_id, media_id):
    """Vlastní obálka alba; None = automatická (nejnovější položka)."""
    if media_id is not None and not conn.execute(
            "SELECT 1 FROM album_items WHERE album_id = ? AND media_id = ?", (album_id, int(media_id))).fetchone():
        raise ValueError(tr("albums-error-cover-member"))
    conn.execute("UPDATE albums SET cover_media_id = ? WHERE id = ?", (media_id, album_id))


def set_sort_mode(conn, album_id, mode):
    if mode not in SORT_MODES:
        raise ValueError(f'Unknown sort order: {mode}')
    if mode == "custom":                          # ruční pořadí začíná od toho, co je vidět
        _renumber_items(conn, album_id, item_ids(conn, album_id, include_all=True))
    conn.execute("UPDATE albums SET sort_mode = ? WHERE id = ?", (mode, album_id))


def _renumber_items(conn, album_id, ordered):
    conn.executemany("UPDATE album_items SET position = ? WHERE album_id = ? AND media_id = ?",
                     [(i, album_id, int(m)) for i, m in enumerate(ordered)])


def move_items(conn, album_id, media_ids, before_id=None):
    """Přetažení v albu: media_ids (v daném pořadí) před položku before_id (None = na konec).
    Album se tím přepne na vlastní pořadí. Vrací nové pořadí všech položek."""
    current = item_ids(conn, album_id, include_all=True)
    present = set(current)
    moving = [m for m in dict.fromkeys(int(x) for x in media_ids) if m in present]
    moving_set = set(moving)
    rest = [m for m in current if m not in moving_set]
    at = rest.index(int(before_id)) if before_id is not None and int(before_id) in rest else len(rest)
    order = rest[:at] + moving + rest[at:]
    _renumber_items(conn, album_id, order)
    conn.execute("UPDATE albums SET sort_mode = 'custom' WHERE id = ?", (album_id,))
    return order


def item_ids(conn, album_id, include_all=False):
    """Viditelné položky ručního alba (bez koše a Skrytých). Řazení podle data pořízení,
    nejstarší první, nebo vlastní (sort_mode = 'custom'). include_all = i koš / Skryté – jen pro
    přečíslování, aby skryté položky po návratu nespadly na nesmyslné místo."""
    row = conn.execute("SELECT sort_mode FROM albums WHERE id = ?", (album_id,)).fetchone()
    custom = bool(row and row[0] == "custom")
    order = ("COALESCE(a.position, 2147483647), m.date_taken, m.id" if custom else "m.date_taken, m.id")
    where = "" if include_all else f"AND {_VISIBLE}"
    return [r[0] for r in conn.execute(f"""
        SELECT a.media_id FROM album_items a JOIN media_items m ON m.id = a.media_id
        WHERE a.album_id = ? {where} ORDER BY {order}""", (album_id,))]


# ── strom ────────────────────────────────────────────────────────────────────

def _parent(conn, node_id):
    row = conn.execute("SELECT parent_id FROM albums WHERE id = ?", (node_id,)).fetchone()
    return row[0] if row else None


def ancestors(conn, node_id):
    """[(id, name)] od kořene po node_id včetně (drobečková navigace)."""
    out, seen = [], set()
    while node_id is not None and node_id not in seen:
        seen.add(node_id)
        row = conn.execute("SELECT name, parent_id FROM albums WHERE id = ?", (node_id,)).fetchone()
        if not row:
            break
        out.append((node_id, row[0]))
        node_id = row[1]
    return out[::-1]


def move_node(conn, node_id, parent_id=None, before_id=None):
    """Přesune album / složku do složky parent_id (None = kořen) před sourozence before_id
    (None = na konec). Složku nelze vložit do sebe ani do vlastního potomka."""
    node_id = int(node_id)
    parent_id = _check_parent(conn, parent_id)
    if parent_id is not None and node_id in {a for a, _ in ancestors(conn, parent_id)}:
        raise ValueError(tr("albums-error-move-into-self"))
    siblings = [r[0] for r in conn.execute(
        "SELECT id FROM albums WHERE parent_id IS ? AND id != ? ORDER BY COALESCE(position, 2147483647), id",
        (parent_id, node_id)).fetchall()]
    at = siblings.index(int(before_id)) if before_id is not None and int(before_id) in siblings else len(siblings)
    siblings.insert(at, node_id)
    conn.execute("UPDATE albums SET parent_id = ? WHERE id = ?", (parent_id, node_id))
    conn.executemany("UPDATE albums SET position = ? WHERE id = ?", [(i, s) for i, s in enumerate(siblings)])


def _album_count_and_cover(conn, aid, cover_id):
    count = conn.execute(f"""SELECT COUNT(*) FROM album_items a JOIN media_items m ON m.id = a.media_id
                             WHERE a.album_id = ? AND {_VISIBLE}""", (aid,)).fetchone()[0]
    row = None
    if cover_id is not None:
        row = conn.execute("SELECT thumbnail_path FROM media_items m WHERE id = ? AND " + _VISIBLE,
                           (cover_id,)).fetchone()
    if not row:
        row = conn.execute(f"""SELECT m.thumbnail_path FROM album_items a JOIN media_items m ON m.id = a.media_id
                               WHERE a.album_id = ? AND {_VISIBLE}
                               ORDER BY m.date_taken DESC, m.id DESC LIMIT 1""", (aid,)).fetchone()
    return count, (row[0] if row else None)


def _node(conn, row):
    aid, name, kind, query, flt, cover_id, parent_id, sort_mode = row
    node = {"id": aid, "name": name, "kind": kind, "query": query, "filter": flt or "all",
            "count": None, "cover": None, "cover_id": cover_id, "parent_id": parent_id,
            "sort_mode": sort_mode or "date"}
    if kind == "manual":
        node["count"], node["cover"] = _album_count_and_cover(conn, aid, cover_id)
    elif kind == "folder":
        kids = descendants(conn, aid)
        node["count"] = sum(1 for k in kids if k["kind"] != "folder")
        node["covers"] = [k["cover"] for k in kids if k.get("cover")][:FOLDER_COVERS]
        node["cover"] = node["covers"][0] if node["covers"] else None
    return node


_NODE_SQL = "SELECT id, name, kind, query, filter, cover_media_id, parent_id, sort_mode FROM albums"


def children(conn, parent_id=None):
    """Obsah jedné úrovně (složky i alba) v ručním pořadí."""
    rows = conn.execute(f"{_NODE_SQL} WHERE parent_id IS ? ORDER BY COALESCE(position, 2147483647), id",
                        (parent_id,)).fetchall()
    return [_node(conn, r) for r in rows]


def descendants(conn, folder_id):
    """Všechna alba a složky pod složkou (do hloubky, v pořadí stromu)."""
    out, stack, seen = [], [folder_id], {folder_id}
    while stack:
        fid = stack.pop(0)
        for r in conn.execute(f"{_NODE_SQL} WHERE parent_id = ? ORDER BY COALESCE(position, 2147483647), id",
                              (fid,)).fetchall():
            if r[0] in seen:
                continue
            seen.add(r[0])
            if r[2] == "folder":
                out.append({"id": r[0], "name": r[1], "kind": "folder"})
                stack.append(r[0])
            else:
                out.append(_node(conn, r))
    return out


def get_album(conn, album_id):
    row = conn.execute("SELECT id, name, kind, query, filter, parent_id, sort_mode, cover_media_id FROM albums WHERE id = ?",
                       (album_id,)).fetchone()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "kind": row[2], "query": row[3], "filter": row[4] or "all",
            "parent_id": row[5], "sort_mode": row[6] or "date", "cover_id": row[7]}


def path_label(conn, node_id, sep=" › "):
    return sep.join(name for _, name in ancestors(conn, node_id))


def list_albums(conn):
    """[{id, name, kind, query, filter, count, cover, path}] – všechna alba (bez složek) pro nabídky:
    ruční s počtem a obálkou (zvolená, jinak nejnovější položka), chytrá bez počtu (počítá se
    hledáním). path = „Složka › Album“."""
    out = []
    for r in conn.execute(f"{_NODE_SQL} WHERE kind != 'folder' ORDER BY name COLLATE NOCASE, id").fetchall():
        node = _node(conn, r)
        node["path"] = path_label(conn, node["id"]) if node["parent_id"] is not None else node["name"]
        out.append(node)
    return out


def list_folders(conn):
    """[(id, „Cesta › Složka“)] všech složek – cíle pro „Přesunout do…“."""
    rows = conn.execute("SELECT id FROM albums WHERE kind = 'folder'").fetchall()
    return sorted(((r[0], path_label(conn, r[0])) for r in rows), key=lambda x: x[1].casefold())


def export_item_ids(conn, node_id):
    """Položky k exportu: ruční album = jeho pořadí, složka = všechna ruční alba pod ní (bez
    opakování). Chytrá alba se exportují z výsledků hledání (main.py)."""
    album = get_album(conn, node_id)
    if not album:
        return []
    if album["kind"] == "manual":
        return item_ids(conn, node_id)
    if album["kind"] != "folder":
        return []
    out = {}
    for k in descendants(conn, node_id):
        if k["kind"] == "manual":
            out.update(dict.fromkeys(item_ids(conn, k["id"])))
    return list(out)


def albums_for_media(conn, media_id):
    return [r[0] for r in conn.execute("SELECT album_id FROM album_items WHERE media_id = ?", (media_id,))]


def czech_albums(n):
    return tr("count-albums", count=n)


def czech_folder_summary(n):
    return tr("count-albums", count=n) if n else tr("albums-empty-folder")
