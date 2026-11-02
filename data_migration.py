"""Gate 0: data uživatele mimo složku aplikace + přesun mezipaměti.

Do verze Stage 4 leželo vše vedle .exe (photos.db, config.json, vault/, backups/, .cache/).
Program Files je ale jen pro čtení a odinstalace / nová verze by knihovnu smazala. Nově:

    %APPDATA%\\Kairos        photos.db, config.json, vault\\, backups\\      (data, malá)
    %LOCALAPPDATA%\\Kairos   faces\\, cache\\, models\\, logs\\               (velká, vázaná na PC)

migrate_legacy_layout() proběhne jednou, před prvním otevřením databáze (splash.boot /
main.main). Pravidla:
  * původní soubory se NEMAŽOU – photos.db, config.json, vault i zálohy zůstanou ve staré
    složce jako záchrana (+ soubor KAIROS_DATA_MOVED.txt s novým umístěním); přesouvá se jen
    mezipaměť a ořezy obličejů (na stejném disku = přejmenování, okamžité);
  * databáze se kopíruje online backup API SQLite (konzistentní i s WAL) do dočasného
    souboru, v kopii se přepíšou absolutní cesty do mezipaměti, ověří se integrita a počet
    položek – a teprve pak se kopie přejmenuje na photos.db. Existence nové photos.db je
    tedy „hotovo“: pád uprostřed migrace = při příštím startu se zopakuje;
  * každý krok je idempotentní (cíl existuje = přeskočit), složky se přesouvají přes
    „.partial“, takže napůl zkopírovaná složka nikdy nevypadá jako hotová.

relocate_cache() přesune mezipaměť na jiný disk (Předvolby › Úložiště): uloží se jen
config „cache_dir“ + „cache_dir_pending_from“ a přesun proběhne při dalším startu, než se
cokoli z mezipaměti otevře.
"""
import json
import os
import shutil
import sqlite3
import time

import paths

MARKER_NAME = "KAIROS_DATA_MOVED.txt"
LOG_NAME = "migration.log"

# Obsah mezipaměti (vše zahoditelné) – přesouvá se celé
CACHE_ENTRIES = ("thumbnails", "previews", "proxies", "web", "highlights", "ui", "tiles.sqlite",
                 "tiles.sqlite-wal", "tiles.sqlite-shm", "beats.json")

# Sloupce s absolutní cestou do mezipaměti / ořezů (po přesunu se přepíšou)
PATH_COLUMNS = (("media_items", "thumbnail_path"), ("events", "tint_source"),
                ("face_instances", "face_crop_path"), ("people", "cover_face_path"))

_done = False


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p))) if p else ""


def rebase(path, old_root, new_root):
    """Cesta pod old_root -> stejná relativní cesta pod new_root; jinak None."""
    if not path or not old_root:
        return None
    n, o = _norm(path), _norm(old_root)
    if n == o or not n.startswith(o + os.sep):
        return None
    return os.path.join(new_root, os.path.normpath(os.path.abspath(path))[len(o) + 1:])


def rebase_db_paths(conn, mapping):
    """mapping = [(stará_složka, nová_složka)]; přepíše PATH_COLUMNS. Vrací počet řádků."""
    changed = 0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, col in PATH_COLUMNS:
        if table not in tables:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            continue
        updates = []
        for row_id, value in conn.execute(f"SELECT rowid, {col} FROM {table} "
                                          f"WHERE {col} IS NOT NULL AND {col} != ''"):
            for old, new in mapping:
                moved = rebase(value, old, new)
                if moved:
                    updates.append((moved, row_id))
                    break
        if updates:
            conn.executemany(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", updates)
            changed += len(updates)
    return changed


def _same_volume(a, b):
    return os.path.splitdrive(_norm(a))[0] == os.path.splitdrive(_norm(b))[0]


def _move_entry(src, dst):
    """Přesune soubor / složku. Na stejném disku přejmenování; jinak kopie přes .partial
    a smazání zdroje až po úspěchu. Cíl už existuje = sloučí složky (nic nepřepisuje)."""
    if not os.path.exists(src):
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src) and os.path.isdir(dst):
        moved = 0
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if not os.path.exists(d):
                moved += _move_entry(s, d)
        shutil.rmtree(src, ignore_errors=True) if not os.listdir(src) else None
        return moved
    if os.path.exists(dst):
        return 0
    if _same_volume(src, dst):
        os.replace(src, dst)
        return 1
    partial = dst + ".partial"
    if os.path.isdir(src):
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(src, partial)
        os.replace(partial, dst)
        shutil.rmtree(src, ignore_errors=True)
    else:
        shutil.copy2(src, partial)
        os.replace(partial, dst)
        os.remove(src)
    return 1


def _copy_entry(src, dst):
    """Kopie dat (zdroj zůstává jako záchrana). Cíl existuje = přeskočit."""
    if not os.path.exists(src) or os.path.exists(dst):
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    partial = dst + ".partial"
    if os.path.isdir(src):
        shutil.rmtree(partial, ignore_errors=True)
        shutil.copytree(src, partial)
    else:
        shutil.copy2(src, partial)
    os.replace(partial, dst)
    return 1


def _copy_database(src, dst, mapping):
    """Konzistentní kopie DB + přepis cest + ověření; pak atomicky na místo. Vrací počet položek."""
    tmp = dst + ".migrating"
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
        n_src = s.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] if s.execute(
            "SELECT 1 FROM sqlite_master WHERE name='media_items'").fetchone() else 0
    finally:
        s.close()
    d = sqlite3.connect(tmp)
    try:
        rebase_db_paths(d, mapping)
        d.commit()
        ok = d.execute("PRAGMA quick_check").fetchone()[0]
        n_dst = d.execute("SELECT COUNT(*) FROM media_items").fetchone()[0] if d.execute(
            "SELECT 1 FROM sqlite_master WHERE name='media_items'").fetchone() else 0
    finally:
        d.close()
    if ok != "ok" or n_dst != n_src:
        os.remove(tmp)
        raise RuntimeError(f'library copy failed verification ({ok}, {n_dst}/{n_src} items)')
    os.replace(tmp, dst)
    return n_dst


def _log(lines):
    try:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        with open(os.path.join(paths.LOG_DIR, LOG_NAME), "a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def needs_migration(legacy_dir=None, data_dir=None):
    legacy_dir = legacy_dir or paths.LEGACY_DIR
    data_dir = data_dir or paths.DATA_DIR
    return (_norm(legacy_dir) != _norm(data_dir)
            and os.path.exists(os.path.join(legacy_dir, "photos.db"))
            and not os.path.exists(os.path.join(data_dir, "photos.db")))


def migrate_legacy_layout(legacy_dir=None, data_dir=None, cache_root=None, face_dir=None,
                          models_dir=None):
    """Jednorázový přesun ze složky aplikace. Vrací slovník se statistikou, nebo None
    (není co migrovat). Výjimku hlásí volajícímu – stará data zůstávají nedotčená."""
    legacy_dir = legacy_dir or paths.LEGACY_DIR
    data_dir = data_dir or paths.DATA_DIR
    cache_root = cache_root or paths.CACHE_ROOT
    face_dir = face_dir or paths.FACE_DIR
    models_dir = models_dir or paths.MODELS_DIR
    if not needs_migration(legacy_dir, data_dir):
        return None
    t0 = time.perf_counter()
    os.makedirs(data_dir, exist_ok=True)
    legacy_cache = os.path.join(legacy_dir, ".cache")
    legacy_faces = os.path.join(legacy_cache, "faces")
    stats = {"copied": 0, "moved": 0}
    # 1) malá data: kopie (originál zůstává jako záchrana)
    for name in ("config.json", "vault", "backups"):
        stats["copied"] += _copy_entry(os.path.join(legacy_dir, name), os.path.join(data_dir, name))
    # 2) ořezy obličejů a mezipaměť: přesun (na stejném disku okamžitý)
    stats["moved"] += _move_entry(legacy_faces, face_dir)
    for name in CACHE_ENTRIES:
        stats["moved"] += _move_entry(os.path.join(legacy_cache, name), os.path.join(cache_root, name))
    legacy_siglip = os.path.join(legacy_dir, "models", "siglip2")
    if os.path.isdir(legacy_siglip) and _same_volume(legacy_siglip, models_dir):
        stats["moved"] += _move_entry(legacy_siglip, os.path.join(models_dir, "siglip2"))
    # 3) databáze jako poslední – její existence = migrace hotová
    mapping = [(legacy_faces, face_dir), (legacy_cache, cache_root)]
    stats["items"] = _copy_database(os.path.join(legacy_dir, "photos.db"),
                                    os.path.join(data_dir, "photos.db"), mapping)
    stats["seconds"] = round(time.perf_counter() - t0, 3)
    try:
        with open(os.path.join(legacy_dir, MARKER_NAME), "w", encoding="utf-8") as f:
            f.write("The Kairos library has moved (Gate 0).\n"
                    f"Data:          {data_dir}\nCache:         {cache_root}\nFaces:         {face_dir}\n"
                    "photos.db, config.json, vault and backups in this folder are only a backup of the\n"
                    "state before the move; the app no longer uses them.\n")
    except OSError:
        pass
    _log([f"migrace {legacy_dir} -> {data_dir}: {json.dumps(stats)}"])
    return stats


def relocate_cache(config_path=None, db_path=None):
    """Dokončí přesun mezipaměti zvolený v Předvolbách (při startu, než se otevře). Vrací
    statistiku, nebo None. config: cache_dir = nové místo, cache_dir_pending_from = staré."""
    config_path = config_path or paths.CONFIG_PATH
    db_path = db_path or paths.DB_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    old = cfg.get("cache_dir_pending_from")
    if not old:
        return None
    new = cfg.get("cache_dir") or paths.DEFAULT_CACHE_ROOT
    stats = {"moved": 0, "rebased": 0}
    t0 = time.perf_counter()
    if _norm(old) != _norm(new):
        for name in CACHE_ENTRIES:
            try:
                stats["moved"] += _move_entry(os.path.join(old, name), os.path.join(new, name))
            except OSError as e:
                _log([f"přesun mezipaměti {name}: {e}"])      # zahoditelné – vznikne znovu
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path, timeout=30)
            try:
                stats["rebased"] = rebase_db_paths(conn, [(old, new)])
                conn.commit()
            finally:
                conn.close()
    cfg.pop("cache_dir_pending_from", None)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    os.replace(tmp, config_path)
    stats["seconds"] = round(time.perf_counter() - t0, 3)
    _log([f"mezipaměť {old} -> {new}: {json.dumps(stats)}"])
    return stats


def ensure_ready():
    """Volá se jednou za běh před prvním přístupem k datům. Chyba migrace nesmí zabránit
    startu: vrací (stats | None, chyba | None)."""
    global _done
    if _done:
        return None, None
    _done = True
    stats, error = None, None
    try:
        stats = migrate_legacy_layout()
    except Exception as e:                       # stará data jsou nedotčená -> start pokračuje
        error = str(e)
        _log([f"migrace selhala: {e}"])
        if needs_migration():
            # nová photos.db nevznikla: tento běh ve starém rozložení (žádná prázdná knihovna
            # v AppData, která by příští pokus o migraci zablokovala)
            paths.use_legacy_layout()
            return stats, error
    try:
        relocate_cache()
    except Exception as e:
        _log([f"přesun mezipaměti selhal: {e}"])
    for d in (paths.DATA_DIR, paths.CACHE_ROOT, paths.FACE_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return stats, error
