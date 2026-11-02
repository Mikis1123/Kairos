"""Vzpomínky (Polish P2): tichá karta „Před 3 lety · Šumava“ nad mřížkou Fotografií.

Hledají se fotky ze stejného dne v minulých letech (index idx_media_md nad MONTH_DAY_SQL,
žádné procházení celé knihovny). Když žádný rok nemá aspoň MIN_PHOTOS fotek přesně z dneška,
použije se okno ±WINDOW_DAYS dní („Tento týden před 3 lety“). Vyhrává nejbližší rok.

Výpočet běží v MemoryJob (QThreadPool) včetně načtení náhledů do QImage; karta jen kreslí.
Prolínání snímků tiká jen u viditelné karty v aktivním okně a bez omezených animací.
"""
from i18n import tr
import os
from collections import Counter
from datetime import date, datetime, timedelta

from PyQt6.QtCore import QObject, QRectF, QRunnable, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget

from database import MONTH_DAY_SQL, visible_media_sql
from events_view import _czech_count, format_event_dates, split_event_title
import icons
import motion
import tint
import tokens
from ui_controls import smooth_rect_path

MIN_PHOTOS = 3
WINDOW_DAYS = 3
MAX_FRAMES = 4               # snímků pro prolínání na kartě
FRAME_EDGE = 640             # náhledy se do karty zmenší hned ve vlákně
CARD_HEIGHT = 168
FRAME_PERIOD_MS = 4500      # prolnutí trvá tokens.MOTION["scene"]


# ── Data ──────────────────────────────────────────────────────────────────────

def _parse(text):
    """'2023-07-06 10:00:00' i EXIF '2023:07:06 10:00:00' -> date (jinak None)."""
    s = str(text or "")[:10].replace(":", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def window_month_days(today, window=WINDOW_DAYS):
    """'MM-DD' pro dnešek ± window dní (dnešek první). 29. 2. se přidá k 28. 2. i 1. 3.,
    aby se přestupný den připomněl i v nepřestupném roce."""
    out = []
    for delta in sorted(range(-window, window + 1), key=abs):
        md = (today + timedelta(days=delta)).strftime("%m-%d")
        if md not in out:
            out.append(md)
    if ("02-28" in out or "03-01" in out) and "02-29" not in out:
        out.append("02-29")
    return out


def anniversary(taken, today, window=WINDOW_DAYS):
    """(před kolika lety, posun ve dnech) pro fotku v okně kolem výročí, jinak None.
    Okno přes přelom roku: 30. 12. 2026 se týká i fotek z 1. 1."""
    for year in (today.year, today.year - 1, today.year + 1):
        try:
            ann = taken.replace(year=year)
        except ValueError:                   # 29. 2. v nepřestupném roce
            ann = taken.replace(year=year, day=28)
        delta = (ann - today).days
        if abs(delta) <= window:
            years = year - taken.year
            return (years, delta) if years >= 1 else None
    return None


def years_ago_text(years, week=False):
    return tr("memories-this-week-years-ago" if week else "memories-years-ago", years=years)


def find_memory(conn, today=None, window=WINDOW_DAYS, min_photos=MIN_PHOTOS):
    """Vzpomínka na dnešek, nebo None.
    {kind 'day'|'week', years, title, place, subtitle, ids (podle data), frames [cesty náhledů]}"""
    today = today or date.today()
    mds = window_month_days(today, window)
    rows = conn.execute(f"""
        SELECT m.id, m.date_taken, m.event_id, m.place_city, m.thumbnail_path, COALESCE(m.is_favorite, 0)
        FROM media_items m
        WHERE {MONTH_DAY_SQL} IN ({",".join("?" * len(mds))})
          AND m.media_type IN ('photo', 'live_photo') AND {visible_media_sql('m')}
    """, mds).fetchall()
    exact, near = {}, {}
    for mid, taken, event_id, city, thumb, fav in rows:
        d = _parse(taken)
        hit = d and anniversary(d, today, window)
        if not hit:
            continue
        years, delta = hit
        item = (d, str(taken), int(mid), event_id, city, thumb, int(fav))
        near.setdefault(years, []).append(item)
        if delta == 0:
            exact.setdefault(years, []).append(item)
    kind, groups = "day", exact
    if not any(len(v) >= min_photos for v in exact.values()):
        kind, groups = "week", near
    candidates = sorted(y for y, v in groups.items() if len(v) >= min_photos)
    if not candidates:
        return None
    years = candidates[0]
    items = sorted(groups[years], key=lambda it: (it[1].replace(":", "-"), it[2]))
    place = _place(conn, items)
    title = years_ago_text(years, week=kind == "week")
    first, last = items[0][0].isoformat(), items[-1][0].isoformat()
    return {
        "kind": kind, "years": years, "place": place,
        "title": f"{title} · {place}" if place else title,
        "subtitle": " · ".join(filter(None, [format_event_dates(first, last), _czech_count(len(items))])),
        "ids": [it[2] for it in items],
        "frames": _pick_frames([it for it in items if it[5]]),
    }


def _place(conn, items):
    """Název události, pokud do ní patří většina fotek; jinak nejčastější město."""
    events = Counter(it[3] for it in items if it[3] is not None)
    if events:
        event_id, n = events.most_common(1)[0]
        if n * 2 > len(items):
            row = conn.execute("SELECT title FROM events WHERE id = ?", (event_id,)).fetchone()
            if row and row[0]:
                return split_event_title(row[0])[1]
    cities = Counter(it[4] for it in items if it[4])
    return cities.most_common(1)[0][0] if cities else ""


def _pick_frames(items, k=MAX_FRAMES):
    """Obálka = první oblíbená (jinak prostřední), pak rovnoměrně rozprostřené ostatní."""
    if not items:
        return []
    favs = [it for it in items if it[6]]
    cover = favs[0] if favs else items[len(items) // 2]
    rest = [it for it in items if it is not cover]
    step = max(1, len(rest) // max(1, k - 1))
    picked = [cover] + rest[::step][:k - 1]
    return [it[5] for it in picked]


# ── Výpočet na pozadí ─────────────────────────────────────────────────────────

class _MemorySignals(QObject):
    done = pyqtSignal(object)        # (memory | None, [QImage])


class MemoryJob(QRunnable):
    def __init__(self, connect, today=None):
        super().__init__()
        self.connect, self.today = connect, today
        self.signals = _MemorySignals()

    def run(self):
        memory, images = None, []
        try:
            conn = self.connect()
            try:
                memory = find_memory(conn, self.today)
            finally:
                conn.close()
            for path in (memory or {}).get("frames", []):
                img = QImage(path) if path and os.path.exists(path) else QImage()
                if not img.isNull():
                    if max(img.width(), img.height()) > FRAME_EDGE:
                        img = img.scaled(FRAME_EDGE, FRAME_EDGE, Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                    images.append(img)
            if memory is not None:
                memory["tint"] = tint.dominant_tint(images[0]) if images else ""
        except Exception as e:
            print(f"[Memories] {e}")
            memory, images = None, []
        try:
            self.signals.done.emit((memory, images))
        except RuntimeError:                 # okno se mezitím zavřelo
            pass


def ms_to_midnight(now=None):
    now = now or datetime.now()
    nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
    return int((nxt - now).total_seconds() * 1000) + 5000


# ── Karta ─────────────────────────────────────────────────────────────────────

class MemoryCard(QWidget):
    """Panoramatická karta: prolínané snímky, štítek VZPOMÍNKA, titulek, podtitulek, ✕."""
    opened = pyqtSignal(list)        # id fotek pro prohlížeč
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.memory = None
        self.images = []
        self.index = 0
        self._prev = None
        self.setObjectName("memoryCard")
        self.setProperty("guide_anchor", "memories")
        self.setFixedHeight(CARD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)
        self._fade = motion.Tween(1.0, lambda v: self.update(), self)
        self._fade.finished.connect(self._end_fade)
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_PERIOD_MS)
        self._timer.timeout.connect(self.next_frame)
        self._scaled = {}

    def set_memory(self, memory, images=()):
        self.memory = memory
        self.images = list(images)
        self.index, self._prev = 0, None
        self._scaled.clear()
        self._fade.set(1.0)
        if memory:
            self.setAccessibleName(tr("memories-a11y", title=memory['title'], subtitle=memory['subtitle']))
            self.setToolTip(tr("memories-open-tooltip"))
        self._sync_timer()
        self.update()

    # ── prolínání: jen viditelná karta, aktivní okno, víc snímků, animace povolené ──
    def _sync_timer(self):
        win = self.window()
        run = (len(self.images) > 1 and self.isVisible() and not motion.reduced()
               and (win is None or win.isActiveWindow()))
        if run and not self._timer.isActive():
            self._timer.start()
        elif not run:
            self._timer.stop()

    def next_frame(self):
        if len(self.images) < 2:
            return False
        self._prev = self.index
        self.index = (self.index + 1) % len(self.images)
        self._fade.set(0.0)
        self._fade.to(1.0, "scene")
        return True

    def _end_fade(self):
        if self._fade.value >= 1.0:
            self._prev = None

    def showEvent(self, e):
        super().showEvent(e)
        self._sync_timer()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.ActivationChange:
            self._sync_timer()

    # ── interakce ──
    def close_rect(self):
        return QRectF(self.width() - 16 - 28, 14, 28, 28)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        pos = e.position()
        if e.button() != Qt.MouseButton.LeftButton or not self.rect().contains(pos.toPoint()) or not self.memory:
            return super().mouseReleaseEvent(e)
        if self.close_rect().adjusted(-4, -4, 4, 4).contains(pos):
            self.dismissed.emit()
        else:
            self.opened.emit(list(self.memory["ids"]))

    def keyPressEvent(self, e):
        if self.memory and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.opened.emit(list(self.memory["ids"]))
            return
        if self.memory and e.key() == Qt.Key.Key_Delete:
            self.dismissed.emit()
            return
        super().keyPressEvent(e)

    # ── kreslení ──
    def _frame(self, i, w, h):
        """Snímek zmenšený (výplní) přesně na kartu – jednou na velikost, ne při každém kreslení."""
        key = (i, w, h)
        hit = self._scaled.get(key)
        if hit is None:
            img = self.images[i].scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                        Qt.TransformationMode.SmoothTransformation)
            hit = img.copy((img.width() - w) // 2, int((img.height() - h) * 0.42), w, h)
            if len(self._scaled) >= 2 * MAX_FRAMES:
                self._scaled.clear()
            self._scaled[key] = hit
        return hit

    def paintEvent(self, _):
        if not self.memory:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = float(tokens.RADIUS["lg"])
        hover = max(0.0, min(1.0, float(self._hover.value)))
        path = smooth_rect_path(r, radius)
        p.save()
        p.setClipPath(path)
        t = tokens.palette()
        if self.images:
            dpr = self.devicePixelRatioF()
            w, h = max(1, round(r.width() * dpr)), max(1, round(r.height() * dpr))
            zoom = 1.0 + 0.02 * hover
            p.translate(r.center())
            p.scale(zoom, zoom)
            p.translate(-r.center())
            if self._prev is not None:
                p.drawImage(r, self._frame(self._prev, w, h))
                p.setOpacity(max(0.0, min(1.0, float(self._fade.value))))
            p.drawImage(r, self._frame(self.index, w, h))
            p.setOpacity(1.0)
            p.resetTransform()
        else:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0.0, tokens.qcolor(t["surface_3"]))
            g.setColorAt(1.0, tokens.qcolor(t["surface"]))
            p.fillRect(r, g)
        shade = QLinearGradient(r.left(), 0, r.left() + r.width() * 0.75, 0)
        shade.setColorAt(0.0, QColor(0, 0, 0, 190))
        shade.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(r, shade)
        p.restore()

        pad = 24
        x, text_w = r.left() + pad, int(r.width() * 0.62)
        eyebrow = QFont(tokens.ui_family())
        eyebrow.setPixelSize(11)
        eyebrow.setWeight(QFont.Weight.Bold)
        eyebrow.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
        title_f = tokens.font("title")
        sub_f = QFont(tokens.ui_family())
        sub_f.setPixelSize(13)
        p.setFont(sub_f)
        sub_h = p.fontMetrics().height()
        p.setFont(title_f)
        title_h = p.fontMetrics().height()
        sub_y = r.bottom() - pad - sub_h
        title_y = sub_y - title_h - 2
        p.setFont(eyebrow)
        eb_h = p.fontMetrics().height()
        # tmavý stín i ve světlém motivu; Polish P4: štítek v odstínu vzpomínky (Přizpůsobit obsahu)
        p.setPen(tokens.qcolor(tokens.context_glow(self.memory.get("tint"), "dark")))
        p.drawText(QRectF(x, title_y - eb_h - 2, text_w, eb_h), int(Qt.AlignmentFlag.AlignLeft), tr("memories-eyebrow"))
        p.setFont(title_f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(QRectF(x, title_y, text_w, title_h), int(Qt.AlignmentFlag.AlignLeft),
                   p.fontMetrics().elidedText(self.memory["title"], Qt.TextElideMode.ElideRight, text_w))
        p.setFont(sub_f)
        p.setPen(QColor(235, 235, 240, 220))
        p.drawText(QRectF(x, sub_y, text_w, sub_h), int(Qt.AlignmentFlag.AlignLeft), self.memory["subtitle"])

        # ✕ – skleněné kolečko, plně viditelné při najetí nebo fokusu
        cr = self.close_rect()
        p.setOpacity(0.55 + 0.45 * max(hover, 1.0 if self.hasFocus() else 0.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 12, 14, 150))
        p.drawEllipse(cr)
        icons.paint(p, "x", cr.adjusted(7, 7, -7, -7), QColor("#FFFFFF"))
        p.setOpacity(1.0)
        if self.hasFocus():
            p.setPen(tokens.qcolor(t["accent"]))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(smooth_rect_path(r.adjusted(1, 1, -1, -1), radius))
        p.end()
