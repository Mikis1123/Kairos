"""Centrální definice všech cest aplikace (jediný zdroj pravdy).

Dřív měl každý modul vlastní BASE_DIR / CACHE_DIR a dialog nastavení dokonce
pracoval s os.getcwd(), takže mazal / měřil jinou složku než zbytek aplikace.
"""
import os
import sys

APP_NAME = "Kairos"

if getattr(sys, "frozen", False):
    INSTALL_DIR = os.path.dirname(sys.executable)
else:
    INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))

# Zpětná kompatibilita: BASE_DIR = složka aplikace (nástroje vedle .exe, relativní cesty).
# Data uživatele v ní od Gate 0 NELEŽÍ – Program Files je jen pro čtení a odinstalace
# / nová verze by knihovnu smazala.
BASE_DIR = INSTALL_DIR

# Přibalené zdroje jen pro čtení (map_vault.html, map_style.js, vendor/maplibre). PyInstaller 6 je dává
# do _internal (sys._MEIPASS), ne vedle .exe; ze zdrojáků je to totéž co BASE_DIR.
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

# Gate 0: kde ležela data do verze Stage 4 (vše vedle .exe) – zdroj jednorázové migrace
LEGACY_DIR = INSTALL_DIR
PORTABLE_FLAG = os.path.join(INSTALL_DIR, "portable.txt")


def _known_folder(env, fallback):
    base = os.environ.get(env) or os.path.join(os.path.expanduser("~"), *fallback)
    return os.path.join(base, APP_NAME)


def _read_cache_override(data_dir):
    """Uživatelem zvolené umístění mezipaměti (Předvolby › Úložiště): config.json „cache_dir“.
    Čte se přímo (config.py importuje paths -> žádný cyklus)."""
    env = os.environ.get("KAIROS_CACHE_DIR")
    if env:
        return env
    try:
        import json
        with open(os.path.join(data_dir, "config.json"), "r", encoding="utf-8") as f:
            value = json.load(f).get("cache_dir")
        return value if isinstance(value, str) and value.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


PORTABLE = bool(os.environ.get("KAIROS_PORTABLE")) or os.path.exists(PORTABLE_FLAG)
if os.environ.get("KAIROS_DATA_DIR"):
    DATA_DIR = os.environ["KAIROS_DATA_DIR"]
    LOCAL_DIR = os.environ.get("KAIROS_LOCAL_DIR") or DATA_DIR
elif PORTABLE:
    # Přenosná instalace (portable.txt vedle .exe): původní rozložení, vše vedle aplikace
    DATA_DIR = LOCAL_DIR = INSTALL_DIR
else:
    DATA_DIR = _known_folder("APPDATA", ("AppData", "Roaming"))          # DB, předvolby, klíče
    LOCAL_DIR = _known_folder("LOCALAPPDATA", ("AppData", "Local"))      # velká data vázaná na PC

DB_PATH = os.path.join(DATA_DIR, "photos.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

DEFAULT_CACHE_ROOT = os.path.join(INSTALL_DIR, ".cache") if PORTABLE and not os.environ.get(
    "KAIROS_DATA_DIR") else os.path.join(LOCAL_DIR, "cache")
CACHE_ROOT = _read_cache_override(DATA_DIR) or DEFAULT_CACHE_ROOT

# ZAHODITELNÉ: miniatury lze kdykoli znovu vygenerovat z originálů.
THUMB_DIR = os.path.join(CACHE_ROOT, "thumbnails")

# ZAHODITELNÉ: náhledy ~2048 px (rychlé zobrazení v prohlížeči, duplicitách, událostech)
# a plakáty videí; omezeno velikostí (LRU), znovu se vytvoří z originálů.
PREVIEW_DIR = os.path.join(CACHE_ROOT, "previews")

# ZAHODITELNÉ: 1080p SDR proxy videí 4K / HDR (tažení časové osy, náhledy, Cesta, přehrávání
# na slabších PC); vlastní rozpočet (config „proxy_cache_mb“), znovu se vytvoří z originálů.
PROXY_DIR = os.path.join(CACHE_ROOT, "proxies")

# ZAHODITELNÉ: HTTP cache mapy (vektorové dlaždice, styl, písma) – trvalý profil QtWebEngine.
WEB_CACHE_DIR = os.path.join(CACHE_ROOT, "web")

# ZAHODITELNÉ: místní úložiště mapových dlaždic (SQLite, tile_store.py, config „tile_store_mb“).
TILE_STORE_PATH = os.path.join(CACHE_ROOT, "tiles.sqlite")

# ZAHODITELNÉ: 3s klipy WebM pro karty Cesty (Live Photo, video; card_media.py).
HIGHLIGHT_DIR = os.path.join(CACHE_ROOT, "highlights")

# NEZAHAZOVAT: ořezy obličejů, na které odkazuje databáze (face_instances, people). Gate 0:
# mimo mezipaměť (tu smí uživatel vymazat i přesunout), v LOCALAPPDATA – stovky MB do
# cestovního profilu (Roaming) nepatří.
FACE_DIR = os.path.join(LOCAL_DIR, ".cache", "faces") if PORTABLE and LOCAL_DIR == INSTALL_DIR \
    else os.path.join(LOCAL_DIR, "faces")

# NEZAHAZOVAT: denní zálohy photos.db (rotace, database.backup_database).
BACKUP_DIR = os.path.join(DATA_DIR, "backups")

# NEZAHAZOVAT: složka Skryté (Stage 3, locked_folder.py) – vault.json se zabaleným klíčem
# a šifrované miniatury (t/<id>.kvt). Bez vault.json se skryté neodemknou.
VAULT_DIR = os.path.join(DATA_DIR, "vault")

# Stahované modely (SigLIP 2, ~1,5 GB) – zapisovatelné, mimo složku aplikace
MODELS_DIR = os.path.join(LOCAL_DIR, "models")

# Diagnostika (KAIROS_FRAMESTATS, záznam migrace)
LOG_DIR = os.path.join(LOCAL_DIR, "logs")


def use_legacy_layout():
    """Nouzový režim, když migrace do AppData selže: tento běh pracuje se starým rozložením
    (vše vedle aplikace), aby nevznikla prázdná knihovna v novém místě. Volat dřív, než si
    jiné moduly cesty převezmou (data_migration.ensure_ready na začátku startu)."""
    global DATA_DIR, LOCAL_DIR, DB_PATH, CONFIG_PATH, CACHE_ROOT, DEFAULT_CACHE_ROOT, THUMB_DIR,         PREVIEW_DIR, PROXY_DIR, WEB_CACHE_DIR, TILE_STORE_PATH, HIGHLIGHT_DIR, FACE_DIR, BACKUP_DIR, VAULT_DIR
    DATA_DIR = LOCAL_DIR = LEGACY_DIR
    DB_PATH = os.path.join(DATA_DIR, "photos.db")
    CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
    CACHE_ROOT = DEFAULT_CACHE_ROOT = os.path.join(LEGACY_DIR, ".cache")
    THUMB_DIR = os.path.join(CACHE_ROOT, "thumbnails")
    PREVIEW_DIR = os.path.join(CACHE_ROOT, "previews")
    PROXY_DIR = os.path.join(CACHE_ROOT, "proxies")
    WEB_CACHE_DIR = os.path.join(CACHE_ROOT, "web")
    TILE_STORE_PATH = os.path.join(CACHE_ROOT, "tiles.sqlite")
    HIGHLIGHT_DIR = os.path.join(CACHE_ROOT, "highlights")
    FACE_DIR = os.path.join(CACHE_ROOT, "faces")
    BACKUP_DIR = os.path.join(DATA_DIR, "backups")
    VAULT_DIR = os.path.join(DATA_DIR, "vault")


def resolve_face_path(path, face_dir=None):
    """Vrátí existující cestu k ořezu obličeje, nebo None.

    DB ukládá absolutní cesty; po přesunu složky aplikace (např. C:\\OmniPhotoValut ->
    C:\\OPV\\OmniPhotoValut) ukazují do neexistující složky, i když soubor leží
    v aktuální FACE_DIR. Všechny ořezy jsou v jedné ploché složce, stačí tedy název souboru.
    """
    if not path:
        return None
    if os.path.exists(path):
        return path
    name = os.path.basename(str(path).replace("\\", "/"))
    candidate = os.path.join(face_dir or FACE_DIR, name) if name else ""
    return candidate if candidate and os.path.exists(candidate) else None

def _tool_path(name, subdir=None, needs=None):
    """Přibalený nástroj: nejdřív vedle .exe (uživatel může dát novější verzi), pak ve vlastní
    podsložce balíčku (RESOURCE_DIR/subdir), pak přímo v _internal. Dřív se hledalo jen vedle
    .exe – čerstvý build bez ručního kopírování neměl ffmpeg (videa) ani exiftool (data, GPS).
    needs = složka, kterou nástroj potřebuje vedle sebe; kopie bez ní se přeskočí."""
    bases = [BASE_DIR] + ([os.path.join(RESOURCE_DIR, subdir)] if subdir else []) + [RESOURCE_DIR]
    for base in bases:
        candidate = os.path.join(base, name)
        if os.path.exists(candidate) and (not needs or os.path.isdir(os.path.join(base, needs))):
            return candidate
    return os.path.join(BASE_DIR, name)


# exiftool.exe (13.x) potřebuje vedle sebe exiftool_files (Perl + knihovny) a v balíčku leží
# ve VLASTNÍ složce: v _internal by si perl532.dll načetl kopii, kterou tam PyInstaller dal
# z analýzy závislostí, a @INC by byl prázdný („Can't locate strict.pm“ – import bez dat a GPS)
EXIFTOOL_PATH = _tool_path("exiftool.exe", subdir=os.path.join("tools", "exiftool"), needs="exiftool_files")
FFMPEG_PATH = _tool_path("ffmpeg.exe")