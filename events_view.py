"""Události a výlety jako Bento mřížka (ve stylu Apple Photos – Vzpomínky).

Karty s panoramatickou obálkou (ořez kolem obličejů), názvem, místem, rozsahem dat a počtem
fotek. Nejnovější událost je velká karta 2×2, každá pátá široká 2×1, ostatní 1×1; počet sloupců
se řídí šířkou okna. Obálka = nejostřejší fotka s obličeji (známé osoby mají přednost).

Patch 11 (dávka D): „živé“ karty – viditelné karty v klidu prolínají 3–5 různorodých fotek
události (k-center přes čas, GPS a CLIP, bez téměř stejných snímků). Jeden sdílený časovač,
nejvýš dvě prolínání naráz, pauza mimo stránku / v neaktivním okně / při vypnutých animacích.
"""
from i18n import event_kicker, event_place, event_title, tr
import math
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime

from PyQt6.QtCore import (Qt, QRect, QRectF, QSize, QPoint, pyqtSignal, QObject, QTimer, QEvent,
                          QVariantAnimation, QRunnable)
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QLayout, QWidget, QSizePolicy

from database import visible_media_sql
import icons
import motion
import tokens
from ui_controls import smooth_rect_path

EVENT_MIN_COL = 280          # nejužší sloupec mřížky (px)
EVENT_GAP = 16
EVENT_UNIT_ASPECT = 0.62     # výška / šířka karty 1×1 (panoramatický poměr ~16:10)
COVER_MAX_EDGE = 1600        # obálky se dekódují v rozlišení velké karty, ne v plném 12 Mpx
DEFAULT_FOCUS = (0.5, 0.42)  # bez obličejů: lehce nad středem (krajina, horizont)
JOURNEY_CHIP_W = 84          # kapsle „Cesta“ na kartě (ikona trasy + popisek)

AMBIENT_FRAMES = 4           # snímků na kartu včetně obálky (3–5)
AMBIENT_PHASH_MIN = 10       # pHash blíž než tohle = „stejná fotka“, do střídání nepatří
AMBIENT_MAX_CANDIDATES = 300 # velké události se pro výběr rovnoměrně prořídí
AMBIENT_TICK_MS = 250        # jeden sdílený časovač pro všechny karty
AMBIENT_FADE_MS = 600
AMBIENT_PERIOD_MS = 3000     # + 0–1000 ms podle události (karty se nestřídají naráz)
AMBIENT_MAX_FADING = 2


# ── Data ──────────────────────────────────────────────────────────────────────

def _czech_count(n):
    return tr("count-photos", count=int(n or 0))


_TRAILING_DATES = re.compile(r"\s*\((?:[^()]*\d[^()]*)\)\s*$")


def split_event_title(title):
    """'Výlet: Frauenau a okolí (09. 07. – 12. 07. 2026)' -> ('Výlet', 'Frauenau a okolí').

    Scanner skládá název i s daty; ta už ukazuje podtitulek karty, takže v nadpisu by se
    opakovala. Předpona do dvojtečky se zobrazí jako malý štítek nad nadpisem."""
    text = _TRAILING_DATES.sub("", (title or "").strip())
    kicker = ""
    if ":" in text:
        head, rest = text.split(":", 1)
        if rest.strip() and len(head) <= 20:
            kicker, text = head.strip(), rest.strip()
    return event_kicker(kicker), (event_place(text) if text else (title or tr("event-default-title")))


def kind_kicker(event, fallback):
    """Gate 2 B: 'Day trip' / 'Journey · 4 days' from the trip hierarchy; older events keep
    the kicker parsed from the stored title."""
    kind = (event or {}).get("kind")
    if kind == "day_trip":
        return tr("event-kind-day-trip")
    if kind == "journey":
        return tr("event-kind-journey", days=max(2, int(event.get("chapters") or 0)))
    return fallback


def format_event_dates(start, end):
    """'2026-07-06 10:00', '2026-07-12 18:00' -> '6.–12. 7. 2026' (kompaktně jako v Apple Photos)."""
    def parse(v):
        try:
            return datetime.strptime(str(v)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    a, b = parse(start), parse(end)
    if a is None and b is None:
        return ""
    a, b = a or b, b or a
    if a > b:
        a, b = b, a
    if a.date() == b.date():
        return f"{a.day}. {a.month}. {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a.day}.–{b.day}. {a.month}. {a.year}"
    if a.year == b.year:
        return f"{a.day}. {a.month}. – {b.day}. {b.month}. {b.year}"
    return f"{a.day}. {a.month}. {a.year} – {b.day}. {b.month}. {b.year}"


def select_event_cover(conn, event_id, preferred=None):
    """Obálka události: nejostřejší fotka s obličeji (známé osoby navrch), bez videí.

    Vrací dict(media_id, path, thumb, focus=(fx, fy)) nebo None. focus = střed obličejů
    v normalizovaných souřadnicích – panoramatický ořez je pak vede, ne náhodný střed."""
    rows = conn.execute(f"""
        SELECT m.id, m.file_path, m.thumbnail_path, COALESCE(m.sharpness_score, 0),
               (SELECT COUNT(*) FROM face_instances f WHERE f.media_id = m.id),
               (SELECT COUNT(*) FROM face_instances f WHERE f.media_id = m.id AND f.person_id IS NOT NULL)
        FROM media_items m
        WHERE m.event_id = ? AND m.media_type != 'video' AND {visible_media_sql('m')}
    """, (event_id,)).fetchall()
    if not rows:
        rows = conn.execute(f"""
            SELECT m.id, m.file_path, m.thumbnail_path, 0, 0, 0 FROM media_items m
            WHERE m.event_id = ? AND {visible_media_sql('m')} ORDER BY m.date_taken LIMIT 1
        """, (event_id,)).fetchall()
        if not rows:
            return None
    chosen = next((r for r in rows if preferred is not None and r[0] == preferred), None)
    if chosen is None:
        max_sharp = max(r[3] for r in rows) or 1.0

        def score(r):
            return 0.55 * (r[3] / max_sharp) + 0.30 * (1 if r[4] else 0) + 0.15 * min(r[5], 3) / 3.0
        chosen = max(rows, key=score)
    media_id, path, thumb = chosen[0], chosen[1], chosen[2]
    return {"media_id": media_id, "path": path, "thumb": thumb, "focus": face_focus(conn, media_id)}


def face_focus(conn, media_id):
    """Střed obličejů fotky v normalizovaných souřadnicích (nebo DEFAULT_FOCUS)."""
    boxes = conn.execute("SELECT bbox_x, bbox_y, bbox_w, bbox_h FROM face_instances WHERE media_id = ? "
                         "AND bbox_x IS NOT NULL", (media_id,)).fetchall()
    if not boxes:
        return DEFAULT_FOCUS
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def _timestamp(value):
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").timestamp()
        except (TypeError, ValueError):
            return None


def _hamming(a, b):
    if not a or not b or len(a) != 16 or len(b) != 16:
        return 64
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 64


def _gps_km(a, b):
    """Přibližná vzdálenost dvou (lat, lon) v km (ekvirektangulární – na výlet stačí)."""
    lat = math.radians((a[0] + b[0]) / 2.0)
    dx = math.radians(b[1] - a[1]) * math.cos(lat)
    dy = math.radians(b[0] - a[0])
    return 6371.0 * math.hypot(dx, dy)


def _decode_clip(raw):
    if raw is None:
        return None
    try:
        import numpy as np
        from ai_engine import decode_embedding
        vec = decode_embedding(raw)
        if vec is None:
            return None
        vec = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(vec))
        return vec / n if n > 0 else None
    except Exception:
        return None


def select_ambient_frames(conn, event_id, cover=None, k=AMBIENT_FRAMES):
    """3–5 fotek pro střídání na kartě: obálka + k-center výběr co nejodlišnějších snímků.

    Vzdálenost = čas (část trvání události) + místo (GPS, část rozptylu) + obsah (CLIP),
    chybějící složky se z průměru vynechají. Téměř stejné snímky (pHash < 10) se vyřadí,
    ostré fotky a fotky s obličeji mají přednost. Vrací [dict(media_id, path, thumb, focus)]."""
    rows = conn.execute(f"""
        SELECT m.id, m.file_path, m.thumbnail_path, m.date_taken, m.latitude, m.longitude,
               COALESCE(m.sharpness_score, 0), m.phash,
               (SELECT COUNT(*) FROM face_instances f WHERE f.media_id = m.id AND COALESCE(f.is_valid, 1) = 1),
               e.embedding
        FROM media_items m LEFT JOIN image_embeddings e ON e.media_id = m.id
        WHERE m.event_id = ? AND m.media_type != 'video' AND {visible_media_sql('m')}
        ORDER BY m.date_taken, m.id
    """, (event_id,)).fetchall()
    rows = [r for r in rows if r[1] and os.path.exists(r[1])]
    if not rows:
        return [cover] if cover else []
    cover_id = (cover or {}).get("media_id")
    if len(rows) > AMBIENT_MAX_CANDIDATES:
        step = len(rows) / float(AMBIENT_MAX_CANDIDATES)
        keep = {int(i * step) for i in range(AMBIENT_MAX_CANDIDATES)}
        rows = [r for i, r in enumerate(rows) if i in keep or r[0] == cover_id]

    times = [_timestamp(r[3]) for r in rows]
    known_t = [t for t in times if t is not None]
    t_span = (max(known_t) - min(known_t)) if len(known_t) > 1 else 0.0
    gps = [(r[4], r[5]) if r[4] is not None and r[5] is not None else None for r in rows]
    known_g = [g for g in gps if g]
    g_span = max((_gps_km(known_g[0], g) for g in known_g), default=0.0) * 2.0 if len(known_g) > 1 else 0.0
    clips = [_decode_clip(r[9]) for r in rows]
    max_sharp = max(r[6] for r in rows) or 1.0
    quality = [0.6 * (r[6] / max_sharp) + 0.4 * (1.0 if r[8] else 0.0) for r in rows]

    def dist(i, j):
        parts = []
        if t_span > 0 and times[i] is not None and times[j] is not None:
            parts.append((1.0, min(1.0, abs(times[i] - times[j]) / t_span)))
        if g_span > 0 and gps[i] and gps[j]:
            parts.append((0.7, min(1.0, _gps_km(gps[i], gps[j]) / g_span)))
        if clips[i] is not None and clips[j] is not None and clips[i].shape == clips[j].shape:
            parts.append((0.9, max(0.0, min(1.0, (1.0 - float(clips[i] @ clips[j])) * 2.0))))
        if not parts:
            return abs(i - j) / max(1, len(rows) - 1)      # bez metadat: rovnoměrně v pořadí
        return sum(w * d for w, d in parts) / sum(w for w, _ in parts)

    start = next((i for i, r in enumerate(rows) if r[0] == cover_id), None)
    if start is None:
        start = max(range(len(rows)), key=lambda i: quality[i])
    chosen = [start]
    min_d = [dist(start, j) for j in range(len(rows))]
    while len(chosen) < min(k, len(rows)):
        best, best_score = None, 0.0
        for j in range(len(rows)):
            if j in chosen or min(_hamming(rows[j][7], rows[c][7]) for c in chosen) < AMBIENT_PHASH_MIN:
                continue
            score = min_d[j] * (0.6 + 0.4 * quality[j])
            if score > best_score:
                best, best_score = j, score
        if best is None:
            break
        chosen.append(best)
        min_d = [min(min_d[j], dist(best, j)) for j in range(len(rows))]

    frames = []
    for i in chosen:
        r = rows[i]
        if i == start and cover and cover.get("media_id") == r[0]:
            frames.append(cover)
        else:
            frames.append({"media_id": r[0], "path": r[1], "thumb": r[2], "focus": face_focus(conn, r[0])})
    return frames


class _FramesSignals(QObject):
    done = pyqtSignal(object)        # {event_id: [frames]}


class AmbientFramesJob(QRunnable):
    """Výběr snímků pro všechny karty mimo GUI vlákno (vlastní spojení s DB)."""

    def __init__(self, events, connect):
        super().__init__()
        self.events = [(int(e["id"]), e.get("cover")) for e in events]
        self.connect = connect
        self.signals = _FramesSignals()

    def run(self):
        result = {}
        try:
            conn = self.connect()
            try:
                for eid, cover in self.events:
                    result[eid] = select_ambient_frames(conn, eid, cover)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Events] výběr snímků: {e}")
        self.signals.done.emit(result)


def fetch_event_cards(conn):
    """Události s alespoň jednou viditelnou položkou, od nejnovější."""
    events = conn.execute(f"""
        SELECT e.id, e.title, e.location_name, e.start_date, e.end_date, e.cover_media_id, COUNT(m.id),
               e.kind, (SELECT COUNT(*) FROM events c WHERE c.parent_id = e.id)
        FROM events e JOIN media_items m ON m.event_id = e.id AND {visible_media_sql('m')}
        GROUP BY e.id HAVING COUNT(m.id) > 0
        ORDER BY e.start_date DESC, e.id DESC
    """).fetchall()
    cards = []
    for eid, title, location, start, end, cover_id, count, kind, chapters in events:
        if not location:
            cities = [c for (c,) in conn.execute(
                f"SELECT m.place_city FROM media_items m WHERE m.event_id = ? AND m.place_city IS NOT NULL "
                f"AND {visible_media_sql('m')}", (eid,))]
            location = Counter(cities).most_common(1)[0][0] if cities else ""
        cards.append({
            "id": eid, "title": title or tr("event-default-title"), "location": location or "",
            "start": start, "end": end, "count": int(count),
            "cover": select_event_cover(conn, eid, cover_id),
            "kind": kind, "chapters": int(chapters or 0),
        })
    return cards


def card_span(index, columns):
    """Rytmus Bento mřížky: první karta velká, každá pátá široká."""
    if columns < 2:
        return 1, 1
    if index == 0:
        return 2, 2
    if index % 5 == 3:
        return 2, 1
    return 1, 1


# ── Karta ─────────────────────────────────────────────────────────────────────

def focus_crop(src_w, src_h, dst_w, dst_h, focus):
    """Výřez zdroje s poměrem cíle, posunutý tak, aby bod focus byl co nejblíž středu."""
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return QRect()
    scale = max(dst_w / src_w, dst_h / src_h)
    cw, ch = dst_w / scale, dst_h / scale
    fx, fy = focus
    x = min(max(fx * src_w - cw / 2.0, 0.0), src_w - cw)
    y = min(max(fy * src_h - ch / 2.0, 0.0), src_h - ch)
    return QRect(int(round(x)), int(round(y)), int(round(cw)), int(round(ch)))


class EventCard(QWidget):
    clicked = pyqtSignal(int)
    journey_requested = pyqtSignal(int)    # skleněná kapsle „Cesta“ (jen události s ≥ 2 místy)

    def __init__(self, event, parent=None):
        super().__init__(parent)
        self.event = event
        self.span = (1, 1)
        self.hero = False
        self._pixmap = QPixmap()
        self._focus = (event.get("cover") or {}).get("focus", DEFAULT_FOCUS)
        self.full_res = False
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)   # Kairos: plynulé najetí
        # ambientní střídání: frames[0] = obálka; zdrojové snímky a jejich výřezy v rozměru karty
        self.frames = []
        self.frame_index = 0
        self._frame_pixmaps = {}
        self._scaled = {}
        self._prev = None                # (pixmap, focus) odcházejícího snímku během prolínání
        self._fade = 1.0
        self._fade_anim = None
        self.next_switch = 0.0
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.kicker, self.display_title = split_event_title(event["title"])
        self.kicker = kind_kicker(event, self.kicker)
        self.setAccessibleName(f"{self.display_title}, {self.subtitle()}, {_czech_count(event['count'])}")
        self.setToolTip(event_title(self.event["title"]) + ("\n" + tr("events-journey-hint") if self.has_journey() else ""))

    def subtitle(self):
        return " · ".join(filter(None, [self.event.get("location"), format_event_dates(self.event["start"],
                                                                                         self.event["end"])]))

    def set_cover(self, pixmap, full_res=False):
        if pixmap is not None and not pixmap.isNull():
            self._frame_pixmaps[0] = pixmap
            self.full_res = full_res
            if self.frame_index == 0:            # ostrá obálka nepřebije právě zobrazený snímek
                self._pixmap = pixmap
                self.update()

    def cover_path(self):
        cover = self.event.get("cover") or {}
        return cover.get("path")

    # ── ambientní střídání ──
    def set_frames(self, frames):
        """Snímky pro střídání; obálka je vždy první (karta začíná tím, co už ukazuje)."""
        cover = self.event.get("cover")
        frames = [f for f in (frames or []) if f and f.get("path")]
        if cover and cover.get("path"):
            frames = [cover] + [f for f in frames if f.get("path") != cover.get("path")]
        self.frames = frames
        self.frame_index = 0
        self._frame_pixmaps = {0: self._pixmap} if not self._pixmap.isNull() else {}

    def next_frame_index(self):
        return (self.frame_index + 1) % len(self.frames) if self.frames else 0

    def has_frame(self, index):
        return index in self._frame_pixmaps

    def set_frame_pixmap(self, index, pixmap):
        """Uloží snímek zmenšený na ~1,25× karty (1600px snímek ×4 na kartu by stál ~25 MB)."""
        if pixmap is None or pixmap.isNull() or not 0 <= index < len(self.frames):
            return
        dpr = self.devicePixelRatioF()
        target = QSize(max(self.width(), 320), max(self.height(), 200)) * (1.25 * dpr)
        if pixmap.width() > target.width() and pixmap.height() > target.height():
            pixmap = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
        self._frame_pixmaps[index] = pixmap

    def is_fading(self):
        return self._prev is not None

    def show_frame(self, index, animate=True):
        """Přepne na snímek `index` (prolnutím AMBIENT_FADE_MS). False, když snímek není načtený."""
        pix = self._frame_pixmaps.get(index)
        if pix is None or index == self.frame_index or not 0 <= index < len(self.frames):
            return False
        old = (self._pixmap, self._focus)
        self._pixmap = pix
        self._focus = self.frames[index].get("focus", DEFAULT_FOCUS)
        self.frame_index = index
        if self._fade_anim is not None:
            self._fade_anim.stop()
        if animate and not old[0].isNull():
            self._prev, self._fade = old, 0.0
            anim = QVariantAnimation(self)
            anim.setDuration(AMBIENT_FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.valueChanged.connect(self._on_fade)
            anim.finished.connect(self._end_fade)
            self._fade_anim = anim
            anim.start()
        else:
            self._end_fade()
        self.update()
        return True

    def _on_fade(self, value):
        self._fade = float(value)
        self.update()

    def _end_fade(self):
        self._prev, self._fade, self._fade_anim = None, 1.0, None
        self.update()

    def _card_pixmap(self, pixmap, focus, r):
        """Výřez kolem obličejů zmenšený PŘESNĚ na kartu – jednou, ne při každém kreslení
        (prolínání pak kreslí jen dvě hotové bitmapy bez škálování)."""
        dpr = self.devicePixelRatioF()
        w, h = max(1, int(round(r.width() * dpr))), max(1, int(round(r.height() * dpr)))
        key = (pixmap.cacheKey(), w, h, focus)
        hit = self._scaled.get(key)
        if hit is None:
            src = focus_crop(pixmap.width(), pixmap.height(), w, h, focus)
            hit = pixmap.copy(src).scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)
            hit.setDevicePixelRatio(dpr)
            if len(self._scaled) >= 4:
                self._scaled.clear()
            self._scaled[key] = hit
        return hit

    def glow_color(self):
        """Záře při najetí v odstínu obálky, nebo None (bílý lem). Karta je vždy tmavá."""
        hue = self.event.get("tint")
        if not hue or not tokens.adaptive():
            return None
        return tokens.qcolor(tokens.context_glow(hue, "dark"))

    def has_journey(self):
        return bool(self.event.get("journey"))

    def journey_rect(self):
        """Kapsle „Cesta“ vlevo nahoře (naproti počtu fotek); kreslí se, žádný podřízený widget."""
        pad = 22 if self.hero else 16
        return QRectF(pad, pad - 4, JOURNEY_CHIP_W, 24)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        pos = e.position()
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(pos.toPoint()):
            if self.has_journey() and self.journey_rect().adjusted(-4, -4, 4, 4).contains(pos):
                self.journey_requested.emit(int(self.event["id"]))
            else:
                self.clicked.emit(int(self.event["id"]))
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(int(self.event["id"]))
            return
        if e.key() == Qt.Key.Key_J and self.has_journey():
            self.journey_requested.emit(int(self.event["id"]))
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 20.0 if self.hero else 16.0
        hover = max(0.0, min(1.0, float(self._hover.value)))
        clip = smooth_rect_path(r, radius)
        p.setClipPath(clip)
        if not self._pixmap.isNull():
            # najetí: fotka se jemně přiblíží kolem středu (karta zůstává v mřížce na místě)
            zoom = 1.0 + 0.035 * hover
            p.save()
            p.translate(r.center())
            p.scale(zoom, zoom)
            p.translate(-r.center())
            if self._prev is not None and not self._prev[0].isNull():
                p.drawPixmap(r.topLeft(), self._card_pixmap(self._prev[0], self._prev[1], r))
                p.setOpacity(self._fade)
            p.drawPixmap(r.topLeft(), self._card_pixmap(self._pixmap, self._focus, r))
            p.setOpacity(1.0)
            p.restore()
        else:
            t = tokens.palette()
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, tokens.qcolor(t["surface_3"]))
            g.setColorAt(1.0, tokens.qcolor(t["surface"]))
            p.fillRect(r, g)
        shade = QLinearGradient(0, r.top() + r.height() * 0.40, 0, r.bottom())
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(1.0, QColor(0, 0, 0, 205))
        p.fillRect(r, shade)

        pad = 22 if self.hero else 16
        # počet fotek – skleněná kapsle vpravo nahoře
        f = QFont(tokens.ui_family())
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.DemiBold)
        f.setFeature(QFont.Tag("tnum"), 1)
        p.setFont(f)
        label = _czech_count(self.event["count"])
        tw = p.fontMetrics().horizontalAdvance(label) + 20
        pill = QRectF(r.right() - pad - tw, r.top() + pad - 4, tw, 24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 12, 14, 140))
        p.drawRoundedRect(pill, 12, 12)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)

        # Cesta – vynoří se při najetí / fokusu; zvýraznění drží jen ikona, kapsle je sklo jako počet
        chip_op = max(hover, 1.0 if self.hasFocus() else 0.0)
        if self.has_journey() and chip_op > 0.01:
            p.setOpacity(chip_op)
            chip = self.journey_rect()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(12, 12, 14, 160))
            p.drawRoundedRect(chip, 12, 12)
            icons.paint(p, "route", QRectF(chip.left() + 8, chip.top() + 4, 16, 16),   # tmavé sklo i ve
                        tokens.qcolor(tokens.accent("dark")["accent"]))                # světlém režimu
            p.setPen(QColor("#FFFFFF"))
            p.drawText(chip.adjusted(28, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       tr("journey"))
            p.setOpacity(1.0)

        # název + místo a datum
        title_f = tokens.font("large_title" if self.hero else "headline")
        if not self.hero:
            title_f.setPixelSize(17)
        sub_f = QFont(tokens.ui_family())
        sub_f.setPixelSize(14 if self.hero else 12)
        text_w = int(r.width() - 2 * pad)
        p.setFont(sub_f)
        sub_h = p.fontMetrics().height()
        sub = p.fontMetrics().elidedText(self.subtitle(), Qt.TextElideMode.ElideRight, text_w)
        p.setPen(QColor(235, 235, 240, 220))
        p.drawText(QRect(int(r.left() + pad), int(r.bottom() - pad - sub_h), text_w, sub_h),
                   Qt.AlignmentFlag.AlignLeft, sub)
        p.setFont(title_f)
        title_h = p.fontMetrics().height()
        title = p.fontMetrics().elidedText(self.display_title, Qt.TextElideMode.ElideRight, text_w)
        p.setPen(QColor("#FFFFFF"))
        title_y = int(r.bottom() - pad - sub_h - title_h - 2)
        p.drawText(QRect(int(r.left() + pad), title_y, text_w, title_h), Qt.AlignmentFlag.AlignLeft, title)
        if self.kicker:
            k_f = QFont(tokens.ui_family())
            k_f.setPixelSize(12 if self.hero else 11)
            k_f.setWeight(QFont.Weight.Bold)
            k_f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
            p.setFont(k_f)
            k_h = p.fontMetrics().height()
            p.setPen(QColor(255, 255, 255, 190))
            p.drawText(QRect(int(r.left() + pad), title_y - k_h, text_w, k_h), Qt.AlignmentFlag.AlignLeft,
                       self.kicker.upper())
        p.setClipping(False)
        if hover > 0.01:
            glow = self.glow_color()
            p.setBrush(Qt.BrushStyle.NoBrush)
            if glow is not None:              # Polish P4: záře v odstínu obálky (Přizpůsobit obsahu)
                for width, alpha in ((6.0, 46), (2.0, 150)):
                    c = QColor(glow)
                    c.setAlpha(round(alpha * hover))
                    p.setPen(QPen(c, width))
                    p.drawPath(smooth_rect_path(r.adjusted(1, 1, -1, -1), radius))
            else:                             # jemný světlý lem při najetí
                p.setPen(QPen(QColor(255, 255, 255, round(60 * hover)), 1.0))
                p.drawPath(smooth_rect_path(r, radius))
        if self.hasFocus():
            p.setPen(QPen(tokens.qcolor(tokens.palette()["accent"]), 2.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(r.adjusted(1, 1, -1, -1), radius))


# ── Rozvržení ─────────────────────────────────────────────────────────────────

class BentoLayout(QLayout):
    """Mřížka s kartami různé velikosti (sloupce × řádky), husté skládání shora zleva."""

    def __init__(self, parent=None, min_col=EVENT_MIN_COL, gap=EVENT_GAP, aspect=EVENT_UNIT_ASPECT):
        super().__init__(parent)
        self._items = []
        self.min_col, self.gap, self.aspect = min_col, gap, aspect
        self.placements = []      # [(QRect)] po posledním rozložení (testy / ladění)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):
        return QSize(self.min_col, int(self.min_col * self.aspect))

    def minimumSize(self):
        return QSize(self.min_col // 2, 0)

    def columns_for(self, width):
        return max(1, (width + self.gap) // (self.min_col + self.gap))

    def _layout(self, rect, apply):
        cols = self.columns_for(rect.width())
        unit_w = (rect.width() - (cols - 1) * self.gap) / cols
        unit_h = round(unit_w * self.aspect)
        occupied = set()
        bottom = 0
        placements = []
        visible = [it for it in self._items if it.widget() is not None and not it.widget().isHidden()]
        for idx, item in enumerate(visible):
            w = item.widget()
            cs, rs = card_span(idx, cols)
            cs = min(cs, cols)
            row = 0
            while True:
                spot = next((c for c in range(cols - cs + 1)
                             if all((row + dr, c + dc) not in occupied for dr in range(rs) for dc in range(cs))), None)
                if spot is not None:
                    break
                row += 1
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((row + dr, spot + dc))
            x = rect.x() + round(spot * (unit_w + self.gap))
            y = rect.y() + row * (unit_h + self.gap)
            width = round(cs * unit_w + (cs - 1) * self.gap)
            height = rs * unit_h + (rs - 1) * self.gap
            geo = QRect(x, y, width, height)
            placements.append(geo)
            if apply:
                w.span = (cs, rs)
                w.hero = cs == 2 and rs == 2
                item.setGeometry(geo)
            bottom = max(bottom, y + height - rect.y())
        if apply:
            self.placements = placements
        return bottom


# ── Ambientní střídání ────────────────────────────────────────────────────────

def system_animations_enabled():
    """Windows: Nastavení › Usnadnění › Efekty animace (SPI_GETCLIENTAREAANIMATION)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        value = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except Exception:
        pass
    return True


def ambient_period_ms(event_id):
    """3–4 s podle události – stálé pro kartu, různé mezi kartami."""
    return AMBIENT_PERIOD_MS + (int(event_id) * 397) % 1000


class AmbientCycler(QObject):
    """Jeden sdílený časovač pro všechny karty událostí.

    Střídají se jen karty ve viditelné části posuvné oblasti, nejvýš AMBIENT_MAX_FADING
    naráz; každá má vlastní periodu i fázi (žádné „blikání“ celé mřížky najednou). Další
    snímek se vyžádá z obrazové pipeline dopředu (náhled ~25 ms na pozadí). Mimo stránku,
    v neaktivním / minimalizovaném okně a při vypnutých animacích systému časovač stojí."""

    def __init__(self, viewport, cards_fn, image_fn, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.viewport = viewport
        self.cards_fn, self.image_fn, self.clock = cards_fn, image_fn, clock
        self.animations = system_animations_enabled()
        self.timer = QTimer(self)
        self.timer.setInterval(AMBIENT_TICK_MS)
        self.timer.timeout.connect(self.tick)
        self._window = None
        viewport.installEventFilter(self)
        self._hook_window()

    def _hook_window(self):
        win = self.viewport.window()
        if win is not self._window and win is not None and win is not self.viewport:
            if self._window is not None:
                self._window.removeEventFilter(self)
            self._window = win
            win.installEventFilter(self)

    def should_run(self):
        if not self.animations or not self.viewport.isVisible():
            return False
        win = self.viewport.window()
        return win is not None and win.isActiveWindow() and not win.isMinimized()

    def sync(self):
        """Spustí / zastaví časovač podle viditelnosti stránky a stavu okna."""
        self._hook_window()
        run = self.should_run()
        if run and not self.timer.isActive():
            self.restart_phases()
            self.timer.start()
        elif not run and self.timer.isActive():
            self.timer.stop()

    def is_running(self):
        return self.timer.isActive()

    def restart_phases(self):
        now = self.clock()
        for i, card in enumerate(self.cards_fn()):
            period = ambient_period_ms(card.event["id"]) / 1000.0
            card.next_switch = now + period + (i * 0.85) % period

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.ActivationChange,
                            QEvent.Type.WindowStateChange, QEvent.Type.ParentChange):
            QTimer.singleShot(0, self.sync)
        return False

    def visible_cards(self):
        area = self.viewport.rect()
        for card in self.cards_fn():
            if len(card.frames) < 2 or not card.isVisible():
                continue
            if QRect(card.mapTo(self.viewport, QPoint(0, 0)), card.size()).intersects(area):
                yield card

    def tick(self):
        if not self.should_run():
            self.timer.stop()
            return
        now = self.clock()
        fading = sum(1 for c in self.cards_fn() if c.is_fading())
        for card in self.visible_cards():
            nxt = card.next_frame_index()
            if not card.has_frame(nxt):
                img = self.image_fn(card.frames[nxt]["path"])     # None = dekóduje se na pozadí
                if img is not None and not img.isNull():
                    card.set_frame_pixmap(nxt, QPixmap.fromImage(img))
            if now < card.next_switch or fading >= AMBIENT_MAX_FADING or not card.has_frame(nxt):
                continue
            if card.show_frame(nxt, animate=True):
                fading += 1
                card.next_switch = now + ambient_period_ms(card.event["id"]) / 1000.0
