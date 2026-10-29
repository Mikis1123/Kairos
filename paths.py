"""Centrální definice všech cest aplikace (jediný zdroj pravdy).

Dřív měl každý modul vlastní BASE_DIR / CACHE_DIR a dialog nastavení dokonce
pracoval s os.getcwd(), takže mazal / měřil jinou složku než zbytek aplikace.
"""
import os
import sys

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Přibalené zdroje jen pro čtení (map_vault.html, mini_map.html, vendor/maplibre). PyInstaller 6 je dává
# do _internal (sys._MEIPASS), ne vedle .exe; ze zdrojáků je to totéž co BASE_DIR.
RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "photos.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

CACHE_ROOT = os.path.join(BASE_DIR, ".cache")

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

# NEZAHAZOVAT: ořezy obličejů, na které odkazuje databáze (face_instances, people).
FACE_DIR = os.path.join(CACHE_ROOT, "faces")


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