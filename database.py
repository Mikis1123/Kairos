import os
import sys
import sqlite3
from contextlib import contextmanager

from paths import BASE_DIR, DB_PATH, resolve_face_path  # jediný zdroj pravdy pro cesty

# Skutečná knihovna uživatele. Testy přesměrovávají database.DB_PATH do dočasné složky;
# s OMNIPHOTO_TEST_GUARD=1 (nastavuje conftest.py) je připojení k ní chyba, ne tichý zápis.
REAL_DB_PATH = DB_PATH
TEST_GUARD_ENV = "OMNIPHOTO_TEST_GUARD"


class RealDatabaseAccessError(RuntimeError):
    """Test se pokusil otevřít skutečnou photos.db (chybí izolace přes database.DB_PATH)."""


def _check_test_guard(path):
    if os.environ.get(TEST_GUARD_ENV) != "1":
        return
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(REAL_DB_PATH)):
        raise RealDatabaseAccessError(
            f"Test přistupuje ke skutečné knihovně {REAL_DB_PATH} – přesměrujte database.DB_PATH")


def visible_media_sql(alias="m"):
    """SQL podmínka pro položky, které uživatel vidí: bez pohybových videí Live Photos
    (patří k fotce) a bez položek v koši."""
    return (f"COALESCE({alias}.is_trashed, 0) = 0 AND NOT EXISTS ("
            f"SELECT 1 FROM media_items lp WHERE lp.paired_video_path = {alias}.file_path)")


# Měsíc-den data pořízení („07-06“ z „2023-07-06 …“ i z EXIF „2023:07:06 …“). Stejný výraz je
# v indexu idx_media_md – SQLite index použije jen pro shodný výraz (bez aliasu tabulky).
MONTH_DAY_SQL = "replace(substr(date_taken, 6, 5), ':', '-')"


def get_connection():
    _check_test_guard(DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_transaction():
    """Kontextový manažer pro bezpečný zápis s automatickým commit/rollback."""
    conn = get_connection()
    try:
        # IMMEDIATE: write-lock se získá hned na začátku (a čeká se na něj podle timeoutu).
        # Odložená (deferred) transakce, která nejdřív čte a pak píše, může v WAL
        # spadnout na "database is locked" okamžitě, bez čekání.
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"Chyba databázové transakce, proveden rollback: {e}")
        raise
    finally:
        conn.close()


BACKUP_KEEP = 7                 # denních záloh (nejstarší se mažou)
BACKUP_PREFIX = "photos-"


def backup_database(backup_dir, keep=BACKUP_KEEP, today=None):
    """Denní záloha knihovny (Sprint R): online backup API SQLite – konzistentní snímek i za
    běhu zápisů (WAL), do dočasného souboru a teprve pak přejmenovat. Jedna záloha za den,
    ponechá se `keep` nejnovějších. Vrací cestu k nové záloze, nebo None (dnešní už je /
    knihovna neexistuje / chyba)."""
    import datetime as _dt
    if not os.path.exists(DB_PATH):
        return None
    day = (today or _dt.date.today()).isoformat()
    target = os.path.join(backup_dir, f"{BACKUP_PREFIX}{day}.db")
    if os.path.exists(target):
        return None
    os.makedirs(backup_dir, exist_ok=True)
    tmp = target + ".tmp"
    try:
        src = get_connection()
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst, pages=4096)          # po dávkách: zápisy aplikace nečekají celou dobu
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, target)
    except (sqlite3.Error, OSError) as e:
        print(f"[Backup] {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    try:
        olds = sorted(f for f in os.listdir(backup_dir)
                      if f.startswith(BACKUP_PREFIX) and f.endswith(".db"))
        for name in olds[:max(0, len(olds) - int(keep))]:
            os.remove(os.path.join(backup_dir, name))
    except OSError as e:
        print(f"[Backup] rotace: {e}")
    return target


def _ensure_columns(cursor, table, columns):
    """Přidá do tabulky chybějící sloupce. Vrací seznam názvů skutečně přidaných sloupců."""
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    added = []
    for col_name, col_type in columns:
        if col_name in existing:
            continue
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
            added.append(col_name)
        except sqlite3.OperationalError as e:
            print(f"Migrace {table}.{col_name} selhala: {e}")
    return added


def init_db():
    with db_transaction() as conn:
        cursor = conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            location_name TEXT,
            start_date TEXT,
            end_date TEXT,
            item_count INTEGER DEFAULT 0,
            cover_media_id INTEGER
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE,
            file_name TEXT,
            file_size INTEGER,
            file_mtime REAL,
            file_ext TEXT,
            media_type TEXT,
            date_taken TEXT,
            subsec_time TEXT,
            timezone_offset TEXT,
            latitude REAL,
            longitude REAL,
            altitude REAL,
            compass_heading REAL,
            place_city TEXT,
            place_country TEXT,
            device_make TEXT,
            device_model TEXT,
            lens_model TEXT,
            f_number REAL,
            exposure_time TEXT,
            iso INTEGER,
            focal_length REAL,
            focal_length_35mm REAL,
            content_identifier TEXT,
            paired_video_path TEXT,
            thumbnail_path TEXT,
            is_favorite INTEGER DEFAULT 0,
            event_id INTEGER,
            raw_metadata_json TEXT,
            phash TEXT,
            sharpness_score REAL DEFAULT 0.0,
            is_duplicate INTEGER DEFAULT 0,
            duplicate_group_id INTEGER,
            faces_scanned INTEGER DEFAULT 0,
            FOREIGN KEY (event_id) REFERENCES events (id)
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS image_embeddings (
            media_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE
        );
        """)

        # Sprint 4.0: přesné hledání – vektory SigLIP 2 (768 rozměrů) vedle CLIP. Vlastní tabulka:
        # vypnutí / zapnutí modelu nic nepřepočítává a CLIP index zůstává nedotčený.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS siglip_embeddings (
            media_id INTEGER PRIMARY KEY,
            embedding BLOB NOT NULL,
            source_sig TEXT,
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE
        );
        """)

        # Vícesnímkový index videí (video_frames): CLIP vektory snímků z celého klipu, plakát
        # zůstává v image_embeddings. source_sig = EMBEDDING_SIG_SQL videa, ze kterého vznikly.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS video_frame_embeddings (
            media_id INTEGER NOT NULL,
            frame_ms INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            source_sig TEXT,
            PRIMARY KEY (media_id, frame_ms),
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            cover_face_path TEXT,
            key_face_id INTEGER,
            is_favorite INTEGER DEFAULT 0
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS face_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_id INTEGER NOT NULL,
            person_id INTEGER,
            face_crop_path TEXT,
            embedding BLOB NOT NULL,
            bbox TEXT,
            bbox_json TEXT,
            bbox_x REAL,
            bbox_y REAL,
            bbox_w REAL,
            bbox_h REAL,
            quality_score REAL DEFAULT 0.0,
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE SET NULL
        );
        """)

        # Bezpečné migrace pro existující databáze (přidání chybějících sloupců)
        added_media = _ensure_columns(cursor, "media_items", [
            ("phash", "TEXT"),
            ("sharpness_score", "REAL DEFAULT 0.0"),
            ("is_duplicate", "INTEGER DEFAULT 0"),
            ("duplicate_group_id", "INTEGER"),
            ("file_mtime", "REAL"),
            ("faces_scanned", "INTEGER DEFAULT 0"),
            # NULL = otisk chybí nebo je ze staré verze -> přepočítá ho ai_engine.refresh_phashes()
            ("phash_version", "INTEGER"),
            # Koš (měkké smazání): soubor na disku zůstává, položka se jen skryje
            ("is_trashed", "INTEGER DEFAULT 0"),
            ("trashed_at", "TEXT"),
            # Sprint 2: původ polohy – NULL/'exif' = ze souboru, 'borrowed' = z telefonu ve stejné
            # chvíli (location_borrow), 'manual' = oprava uživatelem; opravy jdou i do XMP sidecaru
            ("gps_source", "TEXT"),
        ])
        if "faces_scanned" in added_media:
            # Fotky, které už mají nalezené obličeje, se znovu analyzovat nemusí.
            cursor.execute("""
                UPDATE media_items SET faces_scanned = 1
                WHERE id IN (SELECT DISTINCT media_id FROM face_instances)
            """)

        # Polish P4: odstín obálky události (tint.py). tint '' = šedá obálka (bez odstínu),
        # NULL = ještě nespočítáno; tint_source = cesta náhledu, ze které vznikl (jiná obálka = přepočet)
        _ensure_columns(cursor, "events", [
            ("tint", "TEXT"),
            ("tint_source", "TEXT"),
        ])

        # Původ embeddingu (EMBEDDING_SIG_SQL položky, ze které vznikl). NULL nebo jiný podpis =
        # embedding nepatří k aktuálnímu souboru -> přepočítá ho démon (refresh_embeddings).
        # Starší knihovny měly embeddingy fotek posunuté o desítky id (re-import pod novým id)
        # a hledání tak porovnávalo dotaz s obsahem JINÉ fotky.
        _ensure_columns(cursor, "image_embeddings", [
            ("source_sig", "TEXT"),
        ])

        _ensure_columns(cursor, "people", [
            ("key_face_id", "INTEGER"),
            ("is_favorite", "INTEGER DEFAULT 0"),
            # Potvrzeno uživatelem (sloučení, přejmenování, „Je to stejná osoba?“) = kotva,
            # kterou přeshlukování nikdy nesmaže, i když zůstane „Neznámá osoba“
            ("is_confirmed", "INTEGER DEFAULT 0"),
            # Portrét zvolený uživatelem – automatický výběr nejlepší tváře ho nepřepíše
            ("cover_locked", "INTEGER DEFAULT 0"),
        ])

        _ensure_columns(cursor, "face_instances", [
            ("bbox", "TEXT"),
            ("bbox_json", "TEXT"),
            ("bbox_x", "REAL"),
            ("bbox_y", "REAL"),
            ("bbox_w", "REAL"),
            ("bbox_h", "REAL"),
            ("quality_score", "REAL DEFAULT 0.0"),
            # verze algoritmu ořezu (face_engine.CROP_VERSION) – starší ořezy se zkontrolují / opraví
            ("crop_version", "INTEGER"),
            # výběr portrétu osoby: natočení hlavy (1 = čelně, 0 = profil / týl) a jiný obličej ve výřezu
            ("frontality", "REAL"),
            ("crowded", "INTEGER"),
            # anatomická brána (face_engine.face_validity): 1 = obličej, 0 = karanténa (týl hlavy,
            # textura, kámen) – vyřazeno ze shlukování, ale nic se nemaže; NULL = ještě nekontrolováno
            ("is_valid", "INTEGER"),
            ("face_score", "REAL"),
            ("det_score", "REAL"),
            ("gate_version", "INTEGER"),
            # Sprint 4 (Reels): pravděpodobnost úsměvu 0–1 (smile.py, CLIP na ořezu); NULL = nespočítáno
            ("smile", "REAL"),
        ])

        # Páry, které uživatel v dialogu duplicit označil "Ponechat obě" (id_a < id_b)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS duplicate_dismissed (
            id_a INTEGER NOT NULL,
            id_b INTEGER NOT NULL,
            PRIMARY KEY (id_a, id_b)
        );
        """)

        # Návrhy „Je to stejná osoba?“, které uživatel odmítl (id_a < id_b)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS person_merge_dismissed (
            id_a INTEGER NOT NULL,
            id_b INTEGER NOT NULL,
            PRIMARY KEY (id_a, id_b)
        );
        """)

        # Sprint 3: text rozpoznaný na fotkách (ocr_engine, OCR Windows). text '' = zkontrolováno,
        # nic; source_sig = podpis souboru (změněný soubor = znovu)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS media_ocr (
            media_id INTEGER PRIMARY KEY,
            text TEXT NOT NULL DEFAULT '',
            source_sig TEXT,
            FOREIGN KEY (media_id) REFERENCES media_items (id) ON DELETE CASCADE
        );
        """)

        # Sprint 2: ruční a chytrá alba
        import albums
        albums.ensure_tables(cursor)

        # MODUL 3: SQLite R*Tree prostorový index pro bleskové dotazy mapy (< 2 ms)
        try:
            cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS media_geo_index USING rtree(
                id,
                min_lat, max_lat,
                min_lon, max_lon
            );
            """)
        except Exception:
            pass

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_date ON media_items(date_taken);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_phash ON media_items(phash);")
        # Vzpomínky (memories.py): „MM-DD“ z ISO i EXIF data – dotaz musí použít PŘESNĚ MONTH_DAY_SQL
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_media_md ON media_items({MONTH_DAY_SQL});")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_face_person ON face_instances(person_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_face_media ON face_instances(media_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_event ON media_items(event_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_paired ON media_items(paired_video_path);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_content_id ON media_items(content_identifier);")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_faces_pending ON media_items(id) "
            "WHERE faces_scanned = 0 AND media_type != 'video';"
        )

    sync_geo_index()


def relocate_face_paths(face_dir=None):
    """Opraví zastaralé absolutní cesty k ořezům obličejů po přesunu složky aplikace.

    Přepíše jen cesty, které na disku neexistují a jejichž soubor byl nalezen
    v aktuální složce faces. Ostatní záznamy nechá beze změny. Vrací počet opravených řádků.
    """
    fixed = 0
    with db_transaction() as conn:
        c = conn.cursor()
        for table, col in (("face_instances", "face_crop_path"), ("people", "cover_face_path")):
            c.execute(f"SELECT id, {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''")
            updates = []
            for row_id, path in c.fetchall():
                if os.path.exists(path):
                    continue
                new_path = resolve_face_path(path, face_dir)
                if new_path:
                    updates.append((new_path, row_id))
            if updates:
                c.executemany(f"UPDATE {table} SET {col} = ? WHERE id = ?", updates)
                fixed += len(updates)
    return fixed


def purge_orphan_test_rows():
    """Odstraní záznamy s RELATIVNÍ cestou k neexistujícímu souboru.

    Scanner ukládá vždy absolutní cesty, takže takové řádky vznikly jen únikem testovacích
    dat do skutečné knihovny (v mřížce = černé karty bez metadat). Chybějící soubory
    s absolutní cestou se NEmažou – může jít jen o odpojený externí disk.
    Vrací počet odstraněných položek.
    """
    with db_transaction() as conn:
        c = conn.cursor()
        rows = c.execute("SELECT id, file_path FROM media_items").fetchall()
        orphan_ids = [
            mid for mid, path in rows
            if not path or (not os.path.isabs(path)
                            and not os.path.exists(os.path.join(BASE_DIR, path)))
        ]
        if not orphan_ids:
            return 0
        params = [(i,) for i in orphan_ids]
        # image_embeddings a face_instances mají ON DELETE CASCADE (foreign_keys = ON)
        c.executemany("DELETE FROM duplicate_dismissed WHERE id_a = ? OR id_b = ?",
                      [(i, i) for i in orphan_ids])
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='media_geo_index'").fetchone():
            c.executemany("DELETE FROM media_geo_index WHERE id = ?", params)
        c.executemany("DELETE FROM media_items WHERE id = ?", params)
    return len(orphan_ids)


GEO_EPS = 1e-5     # R*Tree ukládá float32 (~4e-6° v našich šířkách) – menší rozdíl = stejná poloha


def _sync_geo(conn):
    """R*Tree = přesně viditelné položky s GPS (v transakci volajícího).

    Dřív se při každé synchronizaci index celý smazal a znovu naplnil – u koše / obnovení
    2× na akci, pod zátěží AI až ~0,5 s v GUI vlákně. Výsledek je stejný, ale přepisuje se
    jen rozdíl: odebrat neviditelné / přesunuté, doplnit chybějící."""
    c = conn.cursor()
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_geo_index'").fetchone():
        return
    visible = f"""SELECT m.id FROM media_items m
                  WHERE m.latitude IS NOT NULL AND m.longitude IS NOT NULL AND {visible_media_sql('m')}"""
    c.execute(f"DELETE FROM media_geo_index WHERE id NOT IN ({visible})")
    c.execute(f"""DELETE FROM media_geo_index WHERE id IN (
                      SELECT g.id FROM media_geo_index g JOIN media_items m ON m.id = g.id
                      WHERE abs(g.min_lat - m.latitude) > {GEO_EPS} OR abs(g.min_lon - m.longitude) > {GEO_EPS})""")
    c.execute(f"""
        INSERT INTO media_geo_index (id, min_lat, max_lat, min_lon, max_lon)
        SELECT m.id, m.latitude, m.latitude, m.longitude, m.longitude
        FROM media_items m
        WHERE m.latitude IS NOT NULL AND m.longitude IS NOT NULL
          AND {visible_media_sql('m')}
          AND m.id NOT IN (SELECT id FROM media_geo_index)
    """)


def sync_geo_index():
    """Synchronizuje SQLite R*Tree prostorový index s tabulkou media_items."""
    try:
        with db_transaction() as conn:
            _sync_geo(conn)
    except Exception as e:
        print(f"Chyba při synchronizaci R*Tree geo indexu: {e}")


def query_geo_bounds(min_lat, max_lat, min_lon, max_lon):
    """Bleskový prostorový dotaz přes R*Tree index (< 2 ms)."""
    conn = get_connection()
    try:
        c = conn.cursor()
        try:
            c.execute("""
                SELECT m.id, m.latitude, m.longitude, m.file_name, m.place_city, m.date_taken, m.thumbnail_path, m.media_type
                FROM media_geo_index g
                JOIN media_items m ON g.id = m.id
                WHERE g.min_lat >= ? AND g.max_lat <= ?
                  AND g.min_lon >= ? AND g.max_lon <= ?
            """, (min_lat, max_lat, min_lon, max_lon))
            return c.fetchall()
        except sqlite3.OperationalError:
            c.execute(f"""
                SELECT m.id, m.latitude, m.longitude, m.file_name, m.place_city, m.date_taken, m.thumbnail_path, m.media_type
                FROM media_items m
                WHERE m.latitude BETWEEN ? AND ?
                  AND m.longitude BETWEEN ? AND ?
                  AND {visible_media_sql('m')}
            """, (min_lat, max_lat, min_lon, max_lon))
            return c.fetchall()
    finally:
        conn.close()


# ==============================================================================
# Koš (měkké smazání) a rozhodnutí o duplicitách
# ==============================================================================

def trash_media(media_ids):
    """Přesune položky do koše (soubory na disku zůstávají). Vrací počet změněných řádků."""
    ids = [(int(i),) for i in media_ids]
    if not ids:
        return 0
    with db_transaction() as conn:
        c = conn.cursor()
        c.executemany("UPDATE media_items SET is_trashed = 1, trashed_at = datetime('now', 'localtime') "
                      "WHERE id = ? AND COALESCE(is_trashed, 0) = 0", ids)
        changed = c.rowcount
        _sync_geo(conn)                  # mapa ve stejné transakci (atomicky), jen rozdíl
    return changed


def restore_media(media_ids):
    """Vrátí položky z koše zpět do knihovny. Vrací počet změněných řádků."""
    ids = [(int(i),) for i in media_ids]
    if not ids:
        return 0
    with db_transaction() as conn:
        c = conn.cursor()
        c.executemany("UPDATE media_items SET is_trashed = 0, trashed_at = NULL "
                      "WHERE id = ? AND is_trashed = 1", ids)
        changed = c.rowcount
        _sync_geo(conn)                  # mapa ve stejné transakci (atomicky), jen rozdíl
    return changed


def _pair_key(id_a, id_b):
    a, b = int(id_a), int(id_b)
    return (a, b) if a < b else (b, a)


def dismiss_duplicate_pair(id_a, id_b):
    """"Ponechat obě": pár se v dialogu duplicit už nenabídne."""
    with db_transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO duplicate_dismissed (id_a, id_b) VALUES (?, ?)",
                     _pair_key(id_a, id_b))


def undismiss_duplicate_pair(id_a, id_b):
    with db_transaction() as conn:
        conn.execute("DELETE FROM duplicate_dismissed WHERE id_a = ? AND id_b = ?",
                     _pair_key(id_a, id_b))


def get_dismissed_duplicate_pairs(conn):
    return {(a, b) for a, b in conn.execute("SELECT id_a, id_b FROM duplicate_dismissed")}


def toggle_favorite(media_id):
    """Atomicky přepne stav oblíbené (0 <-> 1) a vrátí nový stav."""
    with db_transaction() as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE media_items
            SET is_favorite = CASE WHEN COALESCE(is_favorite, 0) = 1 THEN 0 ELSE 1 END
            WHERE id = ?
        """, (media_id,))
        c.execute("SELECT is_favorite FROM media_items WHERE id = ?", (media_id,))
        row = c.fetchone()
    import xmp_sync                      # oblíbené do XMP sidecaru (Sprint 2)
    xmp_sync.touch([media_id])
    return int(row[0]) if row and row[0] else 0


# Podpis souboru, ze kterého embedding vznikl: cesta | velikost | mtime (v sekundách)
EMBEDDING_SIG_SQL = ("COALESCE(m.file_path, '') || '|' || COALESCE(m.file_size, '') || '|' || "
                     "COALESCE(CAST(m.file_mtime AS INTEGER), '')")


def save_image_embedding(media_id, embedding_bytes, source_sig=None):
    """Uloží nebo přepíše embedding v bezpečné transakci (s podpisem zdroje, je-li znám)."""
    with db_transaction() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO image_embeddings (media_id, embedding, source_sig)
            VALUES (?, ?, ?)
        """, (media_id, embedding_bytes, source_sig))


def get_media_needing_embedding():
    """[(id, file_path, thumbnail_path, media_type, podpis)] položek bez platného embeddingu:
    chybí, nemá podpis (starší verze – může patřit jiné fotce), nebo je ze starší verze souboru.
    Bez pohybových videí Live Photos (patří k fotce, hledání je nevidí); i koš (hledá se v něm)."""
    conn = get_connection()
    try:
        return conn.execute(f"""
            SELECT m.id, m.file_path, m.thumbnail_path, m.media_type, {EMBEDDING_SIG_SQL}
            FROM media_items m LEFT JOIN image_embeddings e ON e.media_id = m.id
            WHERE NOT EXISTS (SELECT 1 FROM media_items lp WHERE lp.paired_video_path = m.file_path)
              AND (e.media_id IS NULL OR e.source_sig IS NULL OR e.source_sig != {EMBEDDING_SIG_SQL})
            ORDER BY m.date_taken DESC, m.id DESC
        """).fetchall()
    finally:
        conn.close()


def get_media_needing_siglip():
    """[(id, file_path, thumbnail_path, media_type, podpis)] položek bez platného vektoru SigLIP 2
    (stejný výběr jako get_media_needing_embedding – stejné položky, jiný model)."""
    conn = get_connection()
    try:
        return conn.execute(f"""
            SELECT m.id, m.file_path, m.thumbnail_path, m.media_type, {EMBEDDING_SIG_SQL}
            FROM media_items m LEFT JOIN siglip_embeddings e ON e.media_id = m.id
            WHERE NOT EXISTS (SELECT 1 FROM media_items lp WHERE lp.paired_video_path = m.file_path)
              AND (e.media_id IS NULL OR e.source_sig IS NULL OR e.source_sig != {EMBEDDING_SIG_SQL})
            ORDER BY m.date_taken DESC, m.id DESC
        """).fetchall()
    finally:
        conn.close()


def save_siglip_embeddings(rows):
    """rows = [(media_id, bytes, podpis)]"""
    if not rows:
        return
    with db_transaction() as conn:
        conn.executemany("INSERT OR REPLACE INTO siglip_embeddings (media_id, embedding, source_sig) "
                         "VALUES (?, ?, ?)", rows)


def siglip_coverage():
    """(položek s platným vektorem SigLIP, položek, které ho mají mít) – kdy přepnout hledání."""
    conn = get_connection()
    try:
        # měřítko = položky, které má CLIP (fotka bez miniatury / rozbité video nemá ani jeden)
        need = conn.execute(f"""SELECT COUNT(*) FROM image_embeddings e JOIN media_items m ON m.id = e.media_id
            WHERE e.source_sig = {EMBEDDING_SIG_SQL}""").fetchone()[0]
        have = conn.execute(f"""SELECT COUNT(*) FROM siglip_embeddings e JOIN media_items m ON m.id = e.media_id
            WHERE e.source_sig = {EMBEDDING_SIG_SQL}""").fetchone()[0]
        return int(have), int(need)
    finally:
        conn.close()


def get_siglip_vectors(ids=None):
    """{id: bytes} vektorů SigLIP 2 (videa jen plakátem – snímky klipů zatím indexuje jen CLIP)."""
    conn = get_connection()
    try:
        if ids is None:
            rows = conn.execute("SELECT media_id, embedding FROM siglip_embeddings").fetchall()
        else:
            ids = [int(i) for i in ids]
            if not ids:
                return {}
            rows = conn.execute(f"SELECT media_id, embedding FROM siglip_embeddings "
                                f"WHERE media_id IN ({','.join('?' * len(ids))})", ids).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def get_media_needing_ocr():
    """[(id, file_path, podpis)] fotek bez platného OCR (videa ne – text na videu zatím ne)."""
    conn = get_connection()
    try:
        return conn.execute(f"""
            SELECT m.id, m.file_path, {EMBEDDING_SIG_SQL}
            FROM media_items m LEFT JOIN media_ocr o ON o.media_id = m.id
            WHERE m.media_type IN ('photo', 'live_photo') AND COALESCE(m.is_trashed, 0) = 0
              AND (o.media_id IS NULL OR o.source_sig IS NULL OR o.source_sig != {EMBEDDING_SIG_SQL})
            ORDER BY m.date_taken DESC, m.id DESC
        """).fetchall()
    finally:
        conn.close()


def save_ocr_results(rows):
    """rows = [(media_id, text, podpis)]"""
    if not rows:
        return
    with db_transaction() as conn:
        conn.executemany("INSERT OR REPLACE INTO media_ocr (media_id, text, source_sig) VALUES (?, ?, ?)", rows)


def get_all_embeddings():
    """Čtecí operace – bez nutnosti transakčního manažeru."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT media_id, embedding FROM image_embeddings")
        rows = c.fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


VIDEO_FRAME_MARKER_MS = -1      # řádek „snímky nejdou získat“ (bez vektoru): nezkoušet při každém startu


def save_video_frame_embeddings(media_id, frames, source_sig):
    """Nahradí snímky videa: frames = [(čas ms, bytes embeddingu), ...]. Prázdný seznam =
    značka, že z videa snímky nejdou (rozbitý soubor) – další spuštění ho přeskočí."""
    with db_transaction() as conn:
        conn.execute("DELETE FROM video_frame_embeddings WHERE media_id = ?", (media_id,))
        rows = [(media_id, int(ms), emb, source_sig) for ms, emb in frames] or \
               [(media_id, VIDEO_FRAME_MARKER_MS, b"", source_sig)]
        conn.executemany("INSERT INTO video_frame_embeddings (media_id, frame_ms, embedding, source_sig) "
                         "VALUES (?, ?, ?, ?)", rows)


def get_videos_needing_frames():
    """[(id, file_path, podpis)] viditelných videí bez snímků z aktuální verze souboru."""
    conn = get_connection()
    try:
        return conn.execute(f"""
            SELECT m.id, m.file_path, {EMBEDDING_SIG_SQL}
            FROM media_items m
            WHERE m.media_type = 'video' AND {visible_media_sql('m')}
              AND NOT EXISTS (SELECT 1 FROM video_frame_embeddings v
                              WHERE v.media_id = m.id AND v.source_sig = {EMBEDDING_SIG_SQL})
            ORDER BY m.date_taken DESC, m.id DESC
        """).fetchall()
    finally:
        conn.close()


def get_search_vectors(ids=None):
    """Vektory pro hledání: {id: bytes} (plakát / fotka), u videí se snímky z celého klipu
    {id: (plakát, snímek, ...)} – ai_engine.rank_semantic hodnotí video nejlepším z nich.
    ids: jen tyto položky (průběžné doplnění cache bez načtení celé knihovny)."""
    if ids is None:
        out = get_all_embeddings()
        where, params = "", ()
    else:
        ids = [int(i) for i in ids]
        if not ids:
            return {}
        where = f" AND media_id IN ({','.join('?' * len(ids))})"
        params = tuple(ids)
    conn = get_connection()
    try:
        if ids is not None:
            out = {r[0]: r[1] for r in conn.execute(
                f"SELECT media_id, embedding FROM image_embeddings WHERE 1=1{where}", params)}
        rows = conn.execute("SELECT media_id, embedding FROM video_frame_embeddings "
                            f"WHERE frame_ms >= 0{where} ORDER BY media_id, frame_ms", params).fetchall()
    finally:
        conn.close()
    frames = {}
    for mid, emb in rows:
        frames.setdefault(mid, []).append(emb)
    for mid, vecs in frames.items():
        poster = out.get(mid)
        out[mid] = tuple(([poster] if poster is not None else []) + vecs)
    return out


# ==============================================================================
# UPSERT médií (nahrazuje INSERT OR REPLACE, který mazal oblíbené, obličeje a embeddingy)
# ==============================================================================

UPSERT_MEDIA_SQL = """
INSERT INTO media_items (
    file_path, file_name, file_size, file_mtime, file_ext, media_type,
    date_taken, subsec_time, timezone_offset,
    latitude, longitude, altitude, compass_heading,
    device_make, device_model, lens_model,
    f_number, exposure_time, iso, focal_length, focal_length_35mm,
    content_identifier, raw_metadata_json
) VALUES (
    :file_path, :file_name, :file_size, :file_mtime, :file_ext, :media_type,
    :date_taken, :subsec_time, :timezone_offset,
    :latitude, :longitude, :altitude, :compass_heading,
    :device_make, :device_model, :lens_model,
    :f_number, :exposure_time, :iso, :focal_length, :focal_length_35mm,
    :content_identifier, :raw_metadata_json
)
ON CONFLICT(file_path) DO UPDATE SET
    file_name = excluded.file_name,
    file_ext = excluded.file_ext,
    file_mtime = excluded.file_mtime,
    -- Párování Live Photos se nesmí vrátit zpátky na "photo"
    media_type = CASE
        WHEN media_items.media_type = 'live_photo' AND excluded.media_type = 'photo'
        THEN media_items.media_type ELSE excluded.media_type END,
    date_taken = excluded.date_taken,
    subsec_time = excluded.subsec_time,
    timezone_offset = excluded.timezone_offset,
    altitude = excluded.altitude,
    compass_heading = excluded.compass_heading,
    device_make = excluded.device_make,
    device_model = excluded.device_model,
    lens_model = excluded.lens_model,
    f_number = excluded.f_number,
    exposure_time = excluded.exposure_time,
    iso = excluded.iso,
    focal_length = excluded.focal_length,
    focal_length_35mm = excluded.focal_length_35mm,
    content_identifier = excluded.content_identifier,
    raw_metadata_json = excluded.raw_metadata_json,
    -- Změnila se poloha -> přegeokódovat město/zemi
    place_city = CASE WHEN media_items.latitude IS NOT excluded.latitude
                        OR media_items.longitude IS NOT excluded.longitude
                      THEN NULL ELSE media_items.place_city END,
    place_country = CASE WHEN media_items.latitude IS NOT excluded.latitude
                           OR media_items.longitude IS NOT excluded.longitude
                         THEN NULL ELSE media_items.place_country END,
    latitude = excluded.latitude,
    longitude = excluded.longitude,
    -- Změnil se obsah souboru (velikost) -> znovu vytvořit miniaturu
    thumbnail_path = CASE WHEN media_items.file_size IS NOT excluded.file_size
                          THEN NULL ELSE media_items.thumbnail_path END,
    -- ... a přepočítat otisk pro duplicity (SET v SQLite čte vždy PŮVODNÍ hodnoty řádku)
    phash_version = CASE WHEN media_items.file_size IS NOT excluded.file_size
                         THEN NULL ELSE media_items.phash_version END,
    file_size = excluded.file_size
    -- is_favorite, event_id, paired_video_path, phash, obličeje a embeddingy
    -- se záměrně NEMĚNÍ: jsou to uživatelská / odvozená data.
"""

_UPSERT_FIELDS = (
    "file_path", "file_name", "file_size", "file_mtime", "file_ext", "media_type",
    "date_taken", "subsec_time", "timezone_offset",
    "latitude", "longitude", "altitude", "compass_heading",
    "device_make", "device_model", "lens_model",
    "f_number", "exposure_time", "iso", "focal_length", "focal_length_35mm",
    "content_identifier", "raw_metadata_json",
)


def upsert_media_item(cursor, data):
    """Vloží nový záznam, nebo aktualizuje metadata existujícího (podle file_path)
    BEZ smazání řádku, takže zůstanou is_favorite, ID, obličeje a embeddingy."""
    params = {k: data.get(k) for k in _UPSERT_FIELDS}
    cursor.execute(UPSERT_MEDIA_SQL, params)


# ==============================================================================
# Obličeje: fronta analýzy a atomické uložení
# ==============================================================================

def get_media_pending_face_scan():
    """Fotky, které ještě neprošly detekcí obličejů (včetně těch bez jediné tváře)."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, file_path FROM media_items
            WHERE faces_scanned = 0 AND file_path IS NOT NULL AND media_type != 'video'
            ORDER BY id
        """)
        return c.fetchall()
    finally:
        conn.close()


def save_faces_for_media(media_id, faces):
    """Atomicky uloží nalezené obličeje (vč. quality_score a normalizovaného bboxu)
    a označí fotku jako zpracovanou - i když žádný obličej nenašla."""
    with db_transaction() as conn:
        c = conn.cursor()
        for f in faces or []:
            c.execute("""
                INSERT INTO face_instances
                    (media_id, face_crop_path, embedding, bbox,
                     bbox_x, bbox_y, bbox_w, bbox_h, quality_score, crop_version, frontality, crowded,
                     is_valid, face_score, det_score, gate_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                media_id, f.get("crop_path"), f["embedding"], f.get("bbox"),
                f.get("bbox_x"), f.get("bbox_y"), f.get("bbox_w"), f.get("bbox_h"),
                float(f.get("quality_score") or 0.0), f.get("crop_version"),
                f.get("frontality"), f.get("crowded"),
                f.get("is_valid"), f.get("face_score"), f.get("det_score"), f.get("gate_version"),
            ))
        c.execute("UPDATE media_items SET faces_scanned = 1 WHERE id = ?", (media_id,))