"""Bezpečné čištění mezipaměti náhledů (bez závislosti na Qt, snadno testovatelné)."""
from i18n import tr
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
        return False, tr("cache-unexpected-path")

    problems = []
    try:
        if os.path.isdir(THUMB_DIR):
            shutil.rmtree(THUMB_DIR)
    except Exception as e:
        problems.append(tr("cache-delete-failed", e=e))
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except Exception as e:
        problems.append(tr("cache-mkdir-failed", e=e))

    try:
        with db_transaction() as conn:
            conn.execute("UPDATE media_items SET thumbnail_path = NULL")
    except Exception as e:
        return False, tr("cache-db-update-failed", e=e)

    if problems:
        return True, tr("cache-done-with-warning", detail=" ".join(problems))
    return True, tr("cache-thumbs-cleared")