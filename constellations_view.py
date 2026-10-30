"""Konstelace – zobrazení (Objevy › Konstelace).

ConstellationView: noční obloha – osoby jako hvězdy s tváří (velikost podle počtu fotek), vztahy
jako zářící spojnice (šířka a jas podle síly). Najetí zvýrazní lidi kolem osoby, klik osobu
vybere (ostatní ztlumí), klik na spojnici nebo na dvojici v panelu otevře společný příběh.
StoryView: časová osa chvil, kdy byli dva lidé spolu (constellations.story) – klik = fotky.
Výpočet grafu na pozadí (GraphJob); vše offline z face_instances.
"""
import math
import random

from PyQt6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import constellations
import motion
import paths
import tokens
from ui_controls import EmptyState

AVATAR_PX = 96
NODE_MIN, NODE_MAX = 16, 38
STARS = 260
MAX_EDGES = 400               # víc pout je jen šum – kreslí se nejsilnější
GLOW_EDGES = 120              # nad tolik pout září jen zvýrazněná (záře = široké průhledné pero, drahé)


class _Signals(QObject):
    done = pyqtSignal(object)


def _circle(img, d):
    out = QImage(d, d, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    if img.isNull():
        return out
    s = img.scaled(d, d, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, d, d)
    p.setClipPath(path)
    p.drawImage((d - s.width()) // 2, (d - s.height()) // 2, s)
    p.end()
    return out


class GraphJob(QRunnable):
    def __init__(self, connect):
        super().__init__()
        self.connect = connect
        self.signals = _Signals()

    def run(self):
        g, avatars = None, {}
        try:
            conn = self.connect()
            try:
                g = constellations.graph(conn)
            finally:
                conn.close()
            for n in g["nodes"]:
                path = paths.resolve_face_path(n.get("cover"))
                img = QImage(path) if path else QImage()
                avatars[n["id"]] = _circle(img, AVATAR_PX)
        except Exception as e:
            print(f"[Konstelace] {e}")
        try:
            self.signals.done.emit((g, avatars))
        except RuntimeError:
            pass


class ConstellationView(QWidget):
    story_requested = pyqtSignal(int, int)        # dvojice osob
    person_requested = pyqtSignal(int)            # dvojklik na osobu

    def __init__(self, graph, avatars, parent=None):
        super().__init__(parent)
        self.g = graph
        self.avatars = avatars
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.max_photos = max([n["photos"] for n in graph["nodes"]] or [1])
        self.hover = None             # ("node", id) / ("edge", (a, b))
        self.selected = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(520, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("Konstelace – síť lidí z vašich fotek")
        rng = random.Random(7)
        self.stars = [(rng.random(), rng.random(), rng.uniform(0.3, 1.4), rng.uniform(40, 160)) for _ in range(STARS)]
        self._intro = motion.Tween(0.0, lambda v: self.update(), self)
        self._intro.to(1.0, "emphasized")
        self._focus = motion.Tween(0.0, lambda v: self.update(), self)
        self._bg = None               # pozadí s hvězdami (QPixmap podle velikosti a náběhu)
        self.draw_edges = graph["edges"][:MAX_EDGES]
        self.glow_all = len(self.draw_edges) <= GLOW_EDGES

    # ── geometrie ──
    def radius(self, n):
        return NODE_MIN + (NODE_MAX - NODE_MIN) * math.sqrt(n["photos"] / self.max_photos)

    def pos(self, n):
        w, h = self.width(), self.height()
        m = NODE_MAX + 30
        return QPointF(w / 2 + n["x"] * (w / 2 - m), h / 2 + n["y"] * (h / 2 - m))

    def hit(self, pt):
        for n in sorted(self.g["nodes"], key=lambda n: self.radius(n)):
            if (self.pos(n) - pt).manhattanLength() < self.radius(n) * 1.3 and \
                    math.dist((self.pos(n).x(), self.pos(n).y()), (pt.x(), pt.y())) <= self.radius(n) + 3:
                return ("node", n["id"])
        best, bd = None, 7.0
        for e in self.draw_edges:
            a, b = self.pos(self.nodes[e["a"]]), self.pos(self.nodes[e["b"]])
            d = _seg_dist(pt, a, b)
            if d < bd:
                best, bd = ("edge", (e["a"], e["b"])), d
        return best

    def mouseMoveEvent(self, e):
        h = self.hit(e.position())
        if h != self.hover:
            self.hover = h
            self.setCursor(Qt.CursorShape.PointingHandCursor if h else Qt.CursorShape.ArrowCursor)
            self.setToolTip(self._tip(h))
            self.update()

    def leaveEvent(self, e):
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        h = self.hit(e.position())
        if h and h[0] == "edge":
            self.story_requested.emit(*h[1])
        elif h and h[0] == "node":
            self.select(None if self.selected == h[1] else h[1])
        else:
            self.select(None)

    def mouseDoubleClickEvent(self, e):
        h = self.hit(e.position())
        if h and h[0] == "node":
            self.person_requested.emit(h[1])

    def select(self, pid):
        self.selected = pid
        self._focus.to(1.0 if pid is not None else 0.0, "standard")
        self.update()

    def _node_layer(self, w, h, intro, focus, near, dim, acc):
        """Osoby (záře, tvář, jméno) jako vrstva v mezipaměti – překreslí se při změně výběru / náběhu."""
        key = (w, h, round(intro, 2), focus, round(dim, 2), acc.name())
        if getattr(self, "_nodes_pm", None) is not None and self._nodes_pm[0] == key:
            return self._nodes_pm[1]
        from PyQt6.QtGui import QPixmap
        dpr = self.devicePixelRatioF()
        pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        for n in sorted(self.g["nodes"], key=lambda n: n["photos"]):
            c = self.pos(n)
            r = self.radius(n) * (0.6 + 0.4 * intro)
            on = near is None or n["id"] in near
            op = intro * (1.0 if on else 1.0 - 0.75 * dim)
            p.setOpacity(op)
            halo = QRadialGradient(c, r * 2.2)
            hc = QColor(acc)
            hc.setAlpha(150 if (focus == n["id"]) else 80)
            halo.setColorAt(0, hc)
            halo.setColorAt(1, QColor(0, 0, 0, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(c, r * 2.2, r * 2.2)
            img = self.avatars.get(n["id"])
            rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
            if img is not None and not img.isNull():
                p.drawImage(rect, img)
            else:
                p.setBrush(QColor(60, 60, 72))
                p.drawEllipse(rect)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 230) if focus == n["id"] else acc, 2.0 if focus == n["id"] else 1.4))
            p.drawEllipse(rect)
            p.setPen(QColor(235, 235, 245))
            p.drawText(QRectF(c.x() - 90, c.y() + r + 4, 180, 18), int(Qt.AlignmentFlag.AlignHCenter), n["label"])
        p.end()
        self._nodes_pm = (key, pm)
        return pm

    def _draw_edge(self, p, e, intro, on, hot, acc, dim):
        a, b = self.pos(self.nodes[e["a"]]), self.pos(self.nodes[e["b"]])
        alpha = (0.25 + 0.6 * e["w"]) * intro * (1.0 if on else 1.0 - 0.85 * dim)
        width = 1.0 + 4.0 * e["w"]
        if self.glow_all or hot or on and dim > 0:
            glow = QColor(acc)
            glow.setAlphaF(min(1.0, alpha * (0.55 if not hot else 0.9)))
            p.setPen(QPen(glow, width * 3.2, cap=Qt.PenCapStyle.RoundCap))
            p.drawLine(a, b)
        core = QColor(255, 255, 255) if hot else QColor(220, 215, 255)
        core.setAlphaF(min(1.0, alpha * (1.0 if hot else 0.8)))
        p.setPen(QPen(core, width * 0.6 + (1.0 if hot else 0), cap=Qt.PenCapStyle.RoundCap))
        p.drawLine(a, b)

    def _edge_layer(self, w, h, intro, focus, dim, acc):
        """Všechna pouta jako jedna průhledná vrstva – překreslí se jen při změně výběru / náběhu
        (naměřeno: 42 pout se září 29 ms na snímek při DPR 2, z mezipaměti ~1 ms)."""
        key = (w, h, round(intro, 2), focus, round(dim, 2), acc.name())
        if getattr(self, "_edges", None) is None or self._edges[0] != key:
            from PyQt6.QtGui import QPixmap
            dpr = self.devicePixelRatioF()
            pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            for e in reversed(self.draw_edges):
                on = focus is None or focus in (e["a"], e["b"])
                self._draw_edge(q, e, intro, on, False, acc, dim)
            q.end()
            self._edges = (key, pm)
        return self._edges[1]

    def _background(self, w, h, intro):
        key = (w, h, round(intro, 2))
        if self._bg is None or self._bg[0] != key:
            from PyQt6.QtGui import QPixmap
            dpr = self.devicePixelRatioF()
            pm = QPixmap(max(1, int(w * dpr)), max(1, int(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            bg = QRadialGradient(QPointF(w / 2, h * 0.45), max(w, h) * 0.75)
            bg.setColorAt(0, QColor(22, 26, 48))
            bg.setColorAt(0.6, QColor(11, 13, 24))
            bg.setColorAt(1, QColor(5, 6, 11))
            q.fillRect(0, 0, w, h, bg)
            q.setPen(Qt.PenStyle.NoPen)
            for x, y, r, a in self.stars:
                q.setBrush(QColor(255, 255, 255, int(a * intro)))
                q.drawEllipse(QPointF(x * w, y * h), r, r)
            q.end()
            self._bg = (key, pm)
        return self._bg[1]

    def _tip(self, h):
        if not h:
            return ""
        if h[0] == "node":
            n = self.nodes[h[1]]
            return f"{n['label']} – {n['photos']} fotek"
        e = next(x for x in self.g["edges"] if (x["a"], x["b"]) == h[1])
        return f"{self.nodes[e['a']]['label']} a {self.nodes[e['b']]['label']}: {e['n']} společných fotek – otevřít příběh"

    def _related(self, pid):
        return {e["b"] if e["a"] == pid else e["a"] for e in self.g["edges"] if pid in (e["a"], e["b"])}

    # ── kreslení ──
    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        intro = self._intro.value
        p.drawPixmap(0, 0, self._background(w, h, intro))
        acc = QColor(tokens.accent("dark")["accent"])
        focus = self.selected if self.selected is not None else (self.hover[1] if self.hover and self.hover[0] == "node" else None)
        near = self._related(focus) | {focus} if focus is not None else None
        dim = self._focus.value if self.selected is not None else (1.0 if focus is not None else 0.0)
        hover_edge = self.hover[1] if self.hover and self.hover[0] == "edge" else None
        p.drawPixmap(0, 0, self._edge_layer(w, h, intro, focus, dim, acc))
        if hover_edge is not None:                      # pouto pod kurzorem živě navrch
            e = next((x for x in self.draw_edges if (x["a"], x["b"]) == hover_edge), None)
            if e is not None:
                self._draw_edge(p, e, intro, True, True, acc, 0.0)
        p.drawPixmap(0, 0, self._node_layer(w, h, intro, focus, near, dim, acc))
        p.setOpacity(1.0)
        p.end()


def _seg_dist(p, a, b):
    ax, ay, bx, by, px, py = a.x(), a.y(), b.x(), b.y(), p.x(), p.y()
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class SidePanel(QWidget):
    """Vpravo: kdo je s kým nejčastěji (vybraná osoba, jinak nejsilnější dvojice) – klik = příběh."""
    story_requested = pyqtSignal(int, int)

    def __init__(self, graph, parent=None):
        super().__init__(parent)
        self.g = graph
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.setFixedWidth(270)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(16, 16, 16, 16)
        self.lay.setSpacing(6)
        self.show_for(None)

    def show_for(self, pid):
        while self.lay.count():
            it = self.lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        head = QLabel("Nejsilnější pouta" if pid is None else self.nodes[pid]["label"])
        head.setStyleSheet("font-size:15px; font-weight:700; color:#F5F5F7;")
        self.lay.addWidget(head)
        sub = QLabel("Dvojice, které jsou nejčastěji spolu na fotkách" if pid is None
                     else f"{self.nodes[pid]['photos']} fotek · nejčastěji s:")
        sub.setWordWrap(True)
        sub.setStyleSheet("font-size:11px; color:#8E8E93;")
        self.lay.addWidget(sub)
        pairs = ([(e["a"], e["b"], e["n"]) for e in self.g["edges"][:8]] if pid is None
                 else [(pid, o, n) for o, n in constellations.companions(self.g, pid)])
        for a, b, n in pairs:
            row = _PairRow(f"{self.nodes[a]['label']} & {self.nodes[b]['label']}" if pid is None else self.nodes[b]["label"],
                           f"{n} společných fotek")
            row.clicked.connect(lambda a=a, b=b: self.story_requested.emit(a, b))
            self.lay.addWidget(row)
        if not pairs:
            none = QLabel("Zatím s nikým na společné fotce.")
            none.setStyleSheet("font-size:12px; color:#8E8E93;")
            self.lay.addWidget(none)
        self.lay.addStretch(1)


class _PairRow(QWidget):
    clicked = pyqtSignal()

    def __init__(self, title, sub, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("pairRow")
        self.setStyleSheet("QWidget#pairRow { border-radius:10px; } QWidget#pairRow:hover { background:rgba(255,255,255,0.07); }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-size:13px; font-weight:600; color:#F5F5F7; background:transparent;")
        s = QLabel(sub)
        s.setStyleSheet("font-size:11px; color:#8E8E93; background:transparent;")
        lay.addWidget(t)
        lay.addWidget(s)
        self.setToolTip("Otevřít společný příběh")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class ConstellationsPanel(QWidget):
    story_requested = pyqtSignal(int, int)
    person_requested = pyqtSignal(int)
    loaded = pyqtSignal(int, int)

    def __init__(self, connect, parent=None):
        super().__init__(parent)
        self.connect = connect
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(0)
        self.view = self.side = None
        self._job = None
        self.placeholder = EmptyState("constellation", "Skládám konstelace…", "Hledám, kdo je s kým na fotkách.")
        self.lay.addWidget(self.placeholder)

    def load(self, pool=None):
        job = GraphJob(self.connect)
        job.signals.done.connect(self._on_loaded)
        self._job = job
        (pool or QThreadPool.globalInstance()).start(job)

    def _on_loaded(self, result):
        g, avatars = result
        self.placeholder.setParent(None)
        if not g or len(g["nodes"]) < 2:
            self.placeholder = EmptyState("constellation", "Zatím žádné konstelace",
                                          "Konstelace vzniknou, až budou na fotkách aspoň dva lidé "
                                          f"(každý na {constellations.MIN_PHOTOS} a více snímcích).")
            self.lay.addWidget(self.placeholder)
            self.loaded.emit(0, 0)
            return
        self.view = ConstellationView(g, avatars, self)
        self.side = SidePanel(g, self)
        self.view.story_requested.connect(self.story_requested.emit)
        self.view.person_requested.connect(self.person_requested.emit)
        self.side.story_requested.connect(self.story_requested.emit)
        orig = self.view.select

        def select(pid):
            orig(pid)
            self.side.show_for(pid)
        self.view.select = select
        self.lay.addWidget(self.view, 1)
        self.lay.addWidget(self.side)
        self.loaded.emit(len(g["nodes"]), len(g["edges"]))


class StoryView(QScrollArea):
    """Společná časová osa dvojice: řádek = chvíle (událost / den), obálka, název, datum, počet."""
    open_moment = pyqtSignal(object)              # seznam media id

    def __init__(self, story, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(28, 18, 28, 24)
        lay.setSpacing(0)
        self.rows = []
        moments = story["moments"]
        for k, m in enumerate(moments):
            row = _MomentRow(m, first=k == 0, last=k == len(moments) - 1)
            row.clicked.connect(lambda m=m: self.open_moment.emit(m["ids"]))
            lay.addWidget(row)
            self.rows.append(row)
        if not moments:
            lay.addWidget(EmptyState("constellation", "Žádná společná fotka", "Tihle dva spolu na fotce zatím nejsou."))
        lay.addStretch(1)
        self.setWidget(host)


class _MomentRow(QWidget):
    clicked = pyqtSignal()
    H = 96

    def __init__(self, moment, first=False, last=False, parent=None):
        super().__init__(parent)
        self.m, self.first, self.last = moment, first, last
        self.setFixedHeight(self.H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Zobrazit společné fotky")
        self.img = QImage()
        if moment.get("cover"):
            from viewer_hud import load_image_file
            self.img = load_image_file(moment["cover"], QSize(240, 240))
        self._hover = motion.Tween(0.0, lambda v: self.update(), self)

    def enterEvent(self, e):
        self._hover.to(1.0, "micro")

    def leaveEvent(self, e):
        self._hover.to(0.0, "standard")

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        acc = QColor(tokens.accent("dark")["accent"])
        x = 14
        p.setPen(QPen(QColor(255, 255, 255, 40), 2))
        p.drawLine(QPointF(x, 0 if not self.first else self.H / 2), QPointF(x, self.H if not self.last else self.H / 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(acc)
        p.drawEllipse(QPointF(x, self.H / 2), 6, 6)
        card = QRectF(40, 8, self.width() - 48, self.H - 16)
        p.setBrush(QColor(255, 255, 255, int(10 + 14 * self._hover.value)))
        p.drawRoundedRect(card, 12, 12)
        thumb = QRectF(card.left() + 8, card.top() + 8, (card.height() - 16) * 1.5, card.height() - 16)
        if not self.img.isNull():
            path = QPainterPath()
            path.addRoundedRect(thumb, 8, 8)
            p.save()
            p.setClipPath(path)
            s = self.img.scaled(thumb.size().toSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                Qt.TransformationMode.SmoothTransformation)
            p.drawImage(QPointF(thumb.left() - (s.width() - thumb.width()) / 2, thumb.top() - (s.height() - thumb.height()) / 2), s)
            p.restore()
        tx = thumb.right() + 14
        f = QFont(self.font())
        f.setPixelSize(15)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor(245, 245, 247))
        title = self.m.get("title") or _cz(self.m["date"])
        p.drawText(QRectF(tx, card.top() + 14, card.right() - tx - 10, 22), int(Qt.AlignmentFlag.AlignLeft), title)
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.Normal)
        p.setFont(f)
        p.setPen(QColor(160, 160, 170))
        when = _cz(self.m["date"]) if self.m["date"][:10] == self.m["end"][:10] else f"{_cz(self.m['date'])} – {_cz(self.m['end'])}"
        n = self.m["count"]
        p.drawText(QRectF(tx, card.top() + 40, card.right() - tx - 10, 20), int(Qt.AlignmentFlag.AlignLeft),
                   f"{when} · {n} {'společná fotka' if n == 1 else 'společné fotky' if 2 <= n <= 4 else 'společných fotek'}")
        p.end()


def _cz(s):
    s = str(s or "")
    return f"{int(s[8:10])}. {int(s[5:7])}. {s[:4]}" if len(s) >= 10 and s[4] == "-" else s


def story_subtitle(story):
    n = story["together"]
    if not n:
        return "Zatím žádná společná fotka"
    span = _cz(story["first"]) if story["first"][:10] == story["last"][:10] else f"{_cz(story['first'])} – {_cz(story['last'])}"
    return f"{n} {'společná fotka' if n == 1 else 'společné fotky' if 2 <= n <= 4 else 'společných fotek'} · {span}"
