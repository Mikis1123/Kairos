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
atexit.register(shutil.rmtree, _preview_tmp, True)

# Patch 11: ani config.json aplikace – load_config() jinak při chybějícím souboru sáhl na
# „starší umístění“ cwd/config.json, tj. na skutečné předvolby (testy běží ve složce projektu)
import config  # noqa: E402

_config_tmp = tempfile.mkdtemp(prefix="omni_test_config_")
config.CONFIG_FILE = os.path.join(_config_tmp, "config.json")
config.LEGACY_CONFIG_FILE = os.path.join(_config_tmp, "legacy", "config.json")
atexit.register(shutil.rmtree, _config_tmp, True)
