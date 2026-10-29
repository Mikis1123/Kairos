import os
import json
import copy

from paths import CONFIG_PATH

CONFIG_FILE = CONFIG_PATH
# Starší verze ukládaly config.json do aktuálního pracovního adresáře.
LEGACY_CONFIG_FILE = os.path.join(os.getcwd(), "config.json")

# Výchozí tovární nastavení pro běžné uživatele
DEFAULT_CONFIG = {
    "library_folders": [],
    "auto_scan_on_startup": True,
    "default_thumbnail_size": 180,
    "default_sort_order": "Datum pořízení (Nejnovější)",
    "newsletter_url": "https://omniphotovault.com/updates",
    # P3.8 – Nastavitelný práh shody obličejů (0.0–1.0)
    # 0.62 = výchozí (přísný), 0.50 = benevolentní, 0.72 = velmi přísný
    "face_similarity_threshold": 0.62,
    # P3.9 – Kapacita LRU cache miniatur v RAM (počet pixmap)
    "thumbnail_lru_capacity": 1200,
    # Patch 11 – vzhled ("dark" | "light" | "system") a rozpočet mezipaměti náhledů 2048 px (MB)
    "theme": "dark",
    "preview_cache_mb": 2048,
    # Kairos – zvýraznění: "iris" | "ember" | "moss" | "classic" | "custom" (+ accent_custom "#RRGGBB")
    "accent": "iris",
    "accent_custom": "",
    # Polish P4 – Přizpůsobit obsahu: kreslené záře v odstínu fotek (ovládací prvky beze změny)
    "adaptive_accent": True,
    # Kairos dávka 2 – Omezit animace (vedle volby Windows „Animační efekty“)
    "reduce_motion": False,
    # Polish P2 – karta Vzpomínky („Před 3 lety“) nad Fotografiemi; skrytá křížkem jen pro daný den
    "show_memories": True,
    # Polish P3 – podmnožina library_folders, jejichž změny se naimportují samy (bez dotazu)
    "watched_folders": [],
    "memories_dismissed": "",
    # Fáze 1 – adaptivní škálování: "auto" | "high" | "standard" | "baseline" (perf_profile.py)
    "performance_tier": "auto",
    # Fáze 1 – 1080p SDR kopie videí 4K / HDR (video_proxy.py), limit na disku v MB
    "proxy_cache_mb": 4096,
}


def load_config():
    """Načte konfiguraci ze souboru nebo vytvoří výchozí tovární."""
    path = CONFIG_FILE
    if not os.path.exists(path) and os.path.exists(LEGACY_CONFIG_FILE):
        path = LEGACY_CONFIG_FILE  # jednorázová migrace ze starého umístění

    if not os.path.exists(path):
        save_config(DEFAULT_CONFIG)
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json neobsahuje objekt")
        for k, v in DEFAULT_CONFIG.items():
            data.setdefault(k, copy.deepcopy(v))
        if path != CONFIG_FILE:
            save_config(data)
        return data
    except Exception as e:
        print(f"Chyba při čtení {path}: {e}")
        # Poškozený soubor odložíme, ať ho další uložení nepřepíše bez stopy.
        try:
            os.replace(path, path + ".corrupt")
        except Exception:
            pass
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config_data):
    """Uloží nastavení do config.json (atomicky: tmp soubor + os.replace)."""
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, CONFIG_FILE)
    except Exception as e:
        print(f"Chyba při zápisu config.json: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
