"""Unicode-bezpečné čtení a zápis obrázků přes OpenCV.

cv2.imread / cv2.imwrite na Windows neumí cesty s diakritikou (např. uživatel
"Jiří" nebo složka "Řím"): imread vrátí None a imwrite vrátí False BEZ výjimky,
takže se soubor tiše nevytvoří. Řešení: soubor načíst/zapsat přes Python
(numpy / open) a OpenCV použít jen pro (de)kódování z/do paměti.
"""
import os
import cv2
import fs_atomic
import numpy as np


def imread(path, flags=cv2.IMREAD_COLOR):
    """Náhrada cv2.imread. Vrací ndarray, nebo None při selhání."""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        if data.size == 0:
            return None
        return cv2.imdecode(data, flags)
    except Exception:
        return None


def imwrite(path, img, params=None):
    """Náhrada cv2.imwrite. Vrací True/False; zápis je atomický (tmp + replace)."""
    path = str(path)
    ext = os.path.splitext(path)[1] or ".jpg"
    tmp = fs_atomic.unique_tmp(path)
    try:
        ok, buf = cv2.imencode(ext, img, params or [])
        if not ok:
            return False
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
        return fs_atomic.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False