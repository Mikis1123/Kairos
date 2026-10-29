"""Bezpečné čištění mezipaměti náhledů (bez závislosti na Qt, snadno testovatelné)."""
import os
import shutil

from paths import THUMB_DIR
from database import db_transaction


def clear_thumbnail_cache():
    """Smaže POUZE složku s miniaturami a vynuluje thumbnail_path v databázi.

    Nikdy nesahá na .cache/faces (ořezy tváří), databázi ani originály.
    Vrací (ok: bool, zprava: str). Chybějící miniatury se na pozadí
    dogenerují (BackgroundAIDaemon -> scanner.repair_missing_thumbnails).
    """
    if os.path.basename(os.path.normpath(THUMB_DIR)) != "thumbnails":
        return False, "Neočekávaná cesta k mezipaměti, mazání zrušeno."

    problems = []
    try:
        if os.path.isdir(THUMB_DIR):
            shutil.rmtree(THUMB_DIR)
    except Exception as e:
        problems.append(f"Nepodařilo se smazat všechny soubory: {e}")
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except Exception as e:
        problems.append(f"Nepodařilo se vytvořit složku: {e}")

    try:
        with db_transaction() as conn:
            conn.execute("UPDATE media_items SET thumbnail_path = NULL")
    except Exception as e:
        return False, f"Miniatury smazány, ale databázi se nepodařilo aktualizovat: {e}"

    if problems:
        return True, "Hotovo s upozorněním: " + " ".join(problems)
    return True, "Mezipaměť miniatur byla vyčištěna."