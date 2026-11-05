"""Společné nastavení pytestu: testy nesmí sáhnout na skutečnou knihovnu photos.db.

Dřívější běh testů zanechal ve skutečné DB řádky s testovacími cestami (photo_a.jpg,
anchor.jpg, ...), které se v mřížce zobrazovaly jako černé karty. S touto pojistkou
database.get_connection() vyhodí RealDatabaseAccessError, pokud test zapomene
přesměrovat database.DB_PATH do dočasné složky.

Patch 11: stejně tak náhledová cache (paths.PREVIEW_DIR) – testy ji píšou do dočasné složky,
ne do .cache/previews aplikace.
"""
import atexit
import os
import shutil
import tempfile

import database
import paths

os.environ[database.TEST_GUARD_ENV] = "1"

_preview_tmp = tempfile.mkdtemp(prefix="omni_test_previews_")
paths.PREVIEW_DIR = _preview_tmp
paths.WEB_CACHE_DIR = os.path.join(_preview_tmp, "web")      # HTTP cache mapy (Patch 11)
paths.PROXY_DIR = os.path.join(_preview_tmp, "proxies")      # proxy videí (Fáze 1)
paths.BACKUP_DIR = os.path.join(_preview_tmp, "backups")     # denní zálohy DB (Sprint R)
paths.TILE_STORE_PATH = os.path.join(_preview_tmp, "tiles.sqlite")   # úložiště dlaždic (C7e)
paths.HIGHLIGHT_DIR = os.path.join(_preview_tmp, "highlights")       # klipy karet Cesty
paths.VAULT_DIR = os.path.join(_preview_tmp, "vault")                # složka Skryté (Stage 3)
# Nativní pipeline: ani miniatury, ořezy tváří a kořen mezipaměti. Dřív testy s hlavním oknem
# pouštěly démona údržby proti DOČASNÉ DB, ale SKUTEČNÝM složkám – zapsal značku verze miniatur
# a úklid ořezů by smazal skutečné ořezy (žádný odkaz v dočasné DB). Moduly si cesty berou při
# importu, proto se nastavují tady, před importem testů (a pro jistotu i v už načtených).
paths.CACHE_ROOT = paths.DEFAULT_CACHE_ROOT = os.path.join(_preview_tmp, "cache")
paths.THUMB_DIR = os.path.join(paths.CACHE_ROOT, "thumbnails")
paths.MAP_PACKS_DIR = os.path.join(paths.CACHE_ROOT, "map_packs")   # offline region packs
paths.FACE_DIR = os.path.join(_preview_tmp, "faces")
for _d in (paths.THUMB_DIR, paths.FACE_DIR):
    os.makedirs(_d, exist_ok=True)
import sys as _sys  # noqa: E402
if "scanner" in _sys.modules:
    _sys.modules["scanner"].CACHE_DIR = paths.THUMB_DIR
if "face_engine" in _sys.modules:
    _sys.modules["face_engine"].FACE_CACHE_DIR = paths.FACE_DIR
atexit.register(shutil.rmtree, _preview_tmp, True)

# Patch 11: ani config.json aplikace – load_config() jinak při chybějícím souboru sáhl na
# „starší umístění“ cwd/config.json, tj. na skutečné předvolby (testy běží ve složce projektu)
import config  # noqa: E402

_config_tmp = tempfile.mkdtemp(prefix="omni_test_config_")
config.CONFIG_FILE = os.path.join(_config_tmp, "config.json")
config.LEGACY_CONFIG_FILE = os.path.join(_config_tmp, "legacy", "config.json")
atexit.register(shutil.rmtree, _config_tmp, True)

# Gate 0: governor na baterii zpomaluje / pozastavuje práci na pozadí (power.py) – testy běží
# vždy „ze sítě“, ať výsledek nezávisí na tom, jestli je notebook právě připojený
import power  # noqa: E402

power.get().override = power.PowerState()

# Gate 1: angličtina je zdrojový jazyk, starší testy ale ověřují české texty rozhraní –
# běží proto v češtině (test_gate1_i18n si jazyk nastavuje sám a vrací ho zpět)
import i18n  # noqa: E402

i18n.set_language("cs", retranslate=False)

# 120 Hz pass: background library reads (bg_jobs) run inline in tests – deterministic, and a
# read can never outlive the test's temporary database (the real-DB guard caught exactly that)
import bg_jobs  # noqa: E402

bg_jobs.SYNC = True
