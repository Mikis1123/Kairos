"""Kontextový odstín (Polish P4): převládající barva obálky v OKLCH.

Obálka (uložený náhled) se zmenší na 24×24, každý pixel přispěje do histogramu odstínů vahou
podle sytosti (šedé, skoro černé a přepálené pixely nepočítají). Vyhraje nejsilnější výseč
(vyhlazená se sousedy); málo barevná fotka = žádný odstín (''). Pod 1 ms na obrázek, běží
v TintJob na pozadí a výsledek se uloží do events.tint – podruhé se nepočítá.

Odstín se jen vmíchá do zvýraznění (tokens.context_glow) pro kreslené ambientní plochy.
"""
import math
import os

from PyQt6.QtCore import QObject, QRunnable, Qt, pyqtSignal
from PyQt6.QtGui import QImage

import tokens

SAMPLE = 24
BINS = 36                    # výseče po 10°
MIN_CHROMA = 0.035
MIN_COLORFUL = 0.012         # průměrná váha na pixel – pod tím je fotka „šedá“
TINT_L, TINT_C = 0.70, 0.14  # odstín se vrací v jednotné světlosti / sytosti


def dominant_tint(image):
    """QImage -> '#RRGGBB' převládajícího odstínu, nebo '' (šedá / prázdná fotka)."""
    if image is None or image.isNull():
        return ""
    small = image.scaled(SAMPLE, SAMPLE, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.FastTransformation).convertToFormat(QImage.Format.Format_RGB32)
    bins = [0.0] * BINS
    hue_x, hue_y = [0.0] * BINS, [0.0] * BINS
    n = small.width() * small.height()
    for y in range(small.height()):
        for x in range(small.width()):
            rgb = small.pixel(x, y) & 0xFFFFFF
            L, C, H = tokens.hex_to_oklch(f"#{rgb:06X}")
            if C < MIN_CHROMA or L < 0.18 or L > 0.96:
                continue
            w = C * (1.0 - min(1.0, abs(L - 0.62) * 1.4))
            if w <= 0:
                continue
            b = int(H // (360 / BINS)) % BINS
            bins[b] += w
            hue_x[b] += w * math.cos(math.radians(H))
            hue_y[b] += w * math.sin(math.radians(H))
    if sum(bins) / max(1, n) < MIN_COLORFUL:
        return ""
    smooth = [bins[i - 1] * 0.5 + bins[i] + bins[(i + 1) % BINS] * 0.5 for i in range(BINS)]
    best = max(range(BINS), key=smooth.__getitem__)
    sx = sum(hue_x[j % BINS] for j in (best - 1, best, best + 1))
    sy = sum(hue_y[j % BINS] for j in (best - 1, best, best + 1))
    hue = math.degrees(math.atan2(sy, sx)) % 360
    return tokens.oklch_to_hex(TINT_L, TINT_C, hue)


def tint_of_file(path):
    return dominant_tint(QImage(path)) if path and os.path.exists(path) else ""


# ── Události: výpočet na pozadí s uložením ───────────────────────────────────

class _TintSignals(QObject):
    done = pyqtSignal(object)        # {event_id: '#RRGGBB' | ''}


class TintJob(QRunnable):
    """[(event_id, cesta náhledu obálky)] -> odstíny; spočítané se uloží do events.tint."""

    def __init__(self, items, connect):
        super().__init__()
        self.items, self.connect = list(items), connect
        self.signals = _TintSignals()

    def run(self):
        out = {}
        try:
            for event_id, path in self.items:
                out[int(event_id)] = tint_of_file(path)
            conn = self.connect()
            try:
                conn.executemany("UPDATE events SET tint = ?, tint_source = ? WHERE id = ?",
                                 [(out[int(e)], p, int(e)) for e, p in self.items])
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[Tint] {e}")
        try:
            self.signals.done.emit(out)
        except RuntimeError:
            pass


def stale_items(conn, cards):
    """Karty událostí, jejichž odstín chybí nebo vznikl z jiné obálky: [(id, náhled)].
    Spočítané odstíny zapíše rovnou do karet (card['tint'])."""
    rows = {int(i): (t, s) for i, t, s in conn.execute("SELECT id, tint, tint_source FROM events")}
    todo = []
    for card in cards:
        thumb = (card.get("cover") or {}).get("thumb")
        tint, source = rows.get(int(card["id"]), (None, None))
        if tint is not None and source == thumb:
            card["tint"] = tint
        elif thumb:
            todo.append((int(card["id"]), thumb))
    return todo
