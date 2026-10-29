"""Tipy a průvodce – Kairos Spark (Polish P2).

Obsah je jen data (CHAPTERS): kapitoly s tipy. Tip může mít klávesy (klávesové čepičky),
kotvu (anchor) a stránku, na které kotva žije. Kotva je vlastnost widgetu „guide_anchor“
(mark()), ne objectName – ten už slouží stylům QSS (btn_primary …). Prohlídka i „Ukázat“
používají jediný SpotlightOverlay: ztmaví okno kromě kotvy a vedle ní ukáže bublinu s radou.

Zdroj kláves (source) drží test test_polish_p2 v souladu s obsluhou kláves v kódu.
"""
from dataclasses import dataclass, field

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

import motion
import tokens
from ui_controls import KButton, SidebarNav, palette_for, smooth_rect_path
from ui_nav import DetailPage

ANCHOR_PROPERTY = "guide_anchor"
PAGE_PHOTOS, PAGE_PEOPLE, PAGE_EVENTS, PAGE_MAP = 0, 1, 2, 3


@dataclass(frozen=True)
class Tip:
    id: str
    title: str
    body: str
    keys: tuple = ()          # (("Ctrl", "+"), ("Ctrl", "−")) = dvě kombinace
    anchor: str = ""
    page: int = None          # stránka stacku, kde kotva žije
    group: str = ""           # mezititulek v kapitole
    source: tuple = ()        # (soubor, ("Key_F", …)) – obsluha kláves v kódu


@dataclass(frozen=True)
class Chapter:
    id: str
    title: str
    icon: str
    intro: str
    tips: tuple = field(default_factory=tuple)


CHAPTERS = (
    Chapter("shortcuts", "Klávesové zkratky", "sparkle",
            "Kairos se dá ovládat skoro celý z klávesnice. Zkratky platí tam, kde je kurzor.", (
                Tip("grid_open", "Otevřít fotku", "Vybranou fotku v mřížce otevře v prohlížeči.",
                    (("Mezerník",), ("Enter",)), group="Mřížka fotek",
                    source=("main.py", ("Key_Space", "Key_Return"))),
                Tip("grid_fav", "Oblíbená", "Přidá vybrané fotky do Oblíbených, nebo je odtud odebere.",
                    (("F",),), group="Mřížka fotek", source=("main.py", ("Key_F",))),
                Tip("grid_show", "Prezentace", "Spustí prezentaci přes celou obrazovku od vybrané fotky.",
                    (("S",),), group="Mřížka fotek", source=("main.py", ("Key_S",))),
                Tip("grid_zoom", "Velikost dlaždic", "Funguje i Ctrl + kolečko myši nebo sevření prstů "
                    "na touchpadu.", (("Ctrl", "+"), ("Ctrl", "−")), group="Mřížka fotek",
                    source=("main.py", ("Key_Plus", "Key_Minus"))),
                Tip("grid_esc", "Zrušit výběr", "První Esc zruší výběr, další vrátí z podstránky zpět.",
                    (("Esc",),), group="Mřížka fotek", source=("main.py", ("Key_Escape",))),
                Tip("nav_back", "Zpět", "Vrátí na předchozí stránku přesně tam, kde jste byli.",
                    (("Alt", "←"),), group="Mřížka fotek"),
                Tip("viewer_arrows", "Předchozí a další", "Listování fotkami v prohlížeči.",
                    (("←",), ("→",)), group="Prohlížeč", source=("viewer.py", ("Key_Left", "Key_Right"))),
                Tip("viewer_info", "Informace", "Panel s datem, místem, fotoaparátem a lidmi na fotce.",
                    (("I",),), group="Prohlížeč", source=("viewer.py", ("Key_I",))),
                Tip("viewer_full", "Celá obrazovka", "Esc pak postupně zavře celou obrazovku, panel Info "
                    "a nakonec prohlížeč.", (("F11",), ("Alt", "Enter")), group="Prohlížeč",
                    source=("viewer.py", ("Key_F11", "Key_Escape"))),
                Tip("viewer_play", "Přehrát", "Spustí nebo zastaví video i Live Photo.",
                    (("Mezerník",),), group="Prohlížeč", source=("viewer.py", ("Key_Space",))),
                Tip("video_skip", "O 10 sekund", "Posun videa zpět a vpřed; stejně funguje Shift + ← / →.",
                    (("J",), ("L",)), group="Video", source=("viewer.py", ("Key_J", "Key_L"))),
                Tip("video_frame", "Po snímku", "Krok o jeden snímek zpět a vpřed.",
                    ((",",), (".",)), group="Video", source=("viewer.py", ("Key_Comma", "Key_Period"))),
                Tip("video_mute", "Ztlumit", "Vypne a zapne zvuk videa.", (("M",),), group="Video",
                    source=("viewer.py", ("Key_M",))),
                Tip("event_journey", "Cesta z události", "Na vybrané kartě události přehraje její cestu "
                    "na mapě.", (("J",),), group="Události", source=("events_view.py", ("Key_J",))),
                Tip("review_undo", "Vrátit rozhodnutí", "V kontrole duplicit a nejistých tváří vrátí "
                    "poslední krok.", (("Ctrl", "Z"),), group="Kontroly", source=("main.py", ("Key_Z",))),
                Tip("help", "Tipy a průvodce", "Otevře tuto stránku odkudkoli.", (("F1",),), group="Nápověda"),
            )),
    Chapter("map", "Ovládání mapy", "map",
            "Mapa je skutečná zeměkoule. Fotky s polohou se na ní shlukují podle místa.", (
                Tip("map_globe", "Zeměkoule a přelet", "Klik na shluk fotek přiletí do krajiny s nakloněnou "
                    "kamerou. Tlačítko Zeměkoule nebo Esc vás vrátí zpět.", (("Esc",),),
                    anchor="map", page=PAGE_MAP, source=("map_vault.html", ("Escape",))),
                Tip("map_layers", "Tři podklady", "Základní, Satelitní a Turistická (vrstevnice a značené "
                    "trasy) – přepínač je přímo na mapě.", anchor="map", page=PAGE_MAP),
                Tip("map_terrain", "3D terén", "Při přiblížení do krajiny se sám zapne plastický terén; "
                    "na zeměkouli se vypne, aby mapa zůstala svižná."),
                Tip("map_journey", "Cesta", "Kinematické přehrání výletu po zastávkách s polaroidy a filmovým "
                    "pásem. Spustíte ji z karty události nebo tlačítkem Cesty na mapě.",
                    (("Mezerník",), ("←",), ("→",)), anchor="map", page=PAGE_MAP,
                    source=("map_vault.html", (" ", "ArrowLeft", "ArrowRight"))),
            )),
    Chapter("faces", "Rozpoznávání tváří", "people",
            "Kairos hledá tváře na pozadí a skládá je do osob. Vy jen potvrdíte a pojmenujete.", (
                Tip("faces_name", "Pojmenujte osobu", "Otevřete kartu v Osobách a zadejte jméno. Pojmenované "
                    "osoby slouží jako kotvy – další fotky se k nim přiřadí přesněji.",
                    anchor="people", page=PAGE_PEOPLE),
                Tip("faces_merge", "Sloučit dvě osoby", "Ctrl + klik vybere víc karet, pak stiskněte Sloučit. "
                    "Sloučení jde vrátit.", (("Ctrl", "klik"),), anchor="people", page=PAGE_PEOPLE),
                Tip("faces_borderline", "Nejisté tváře", "Tváře, u kterých si Kairos není jistý, projdete "
                    "rychle ano / ne.", anchor="faces_borderline", page=PAGE_PEOPLE),
                Tip("faces_duplicates", "Duplicity", "Téměř stejné snímky vedle sebe – necháte lepší, "
                    "druhý jde do koše (Ctrl + Z vrátí).", anchor="faces_duplicates", page=PAGE_PEOPLE),
                Tip("faces_recluster", "Přeskupit znovu", "Když se osoby pomíchají, přeskupení je složí "
                    "znovu; potvrzené osoby zůstanou.", anchor="faces_recluster", page=PAGE_PEOPLE),
            )),
    Chapter("search", "Hledání", "search",
            "Hledání rozumí obsahu fotek, ne jen názvům souborů.", (
                Tip("search_text", "Hledejte popisem", "Zkuste „západ slunce“, „pes na pláži“ nebo „hory "
                    "v zimě“. Poprvé se chvíli připravuje model, pak je hledání okamžité.",
                    anchor="search", page=PAGE_PHOTOS),
                Tip("search_time", "Roky, Měsíce, Dny", "Přepínač nad mřížkou přeskočí v čase – klik na rok "
                    "otevře jeho měsíce, klik na měsíc mřížku na tom místě.",
                    anchor="time_switch", page=PAGE_PHOTOS),
                Tip("search_memories", "Vzpomínky", "Když máte fotky ze stejného dne v minulých letech, "
                    "objeví se nad mřížkou karta „Před 3 lety“.", anchor="memories", page=PAGE_PHOTOS),
                Tip("search_import", "Import", "Přidejte složku s fotkami; Kairos je zařadí na místě, "
                    "nic nekopíruje.", anchor="import", page=PAGE_PHOTOS),
            )),
)


def chapter(chapter_id):
    return next((c for c in CHAPTERS if c.id == chapter_id), None)


def all_tips():
    return [t for c in CHAPTERS for t in c.tips]


def mark(widget, anchor):
    widget.setProperty(ANCHOR_PROPERTY, anchor)
    return widget


def find_anchor(root, anchor, visible_only=True):
    """První (viditelný) widget s kotvou pod root, jinak None."""
    if not anchor:
        return None
    candidates = [root] + root.findChildren(QWidget)
    for w in candidates:
        if w.property(ANCHOR_PROPERTY) == anchor and (not visible_only or w.isVisible()):
            return w
    return None


# ── Hub ───────────────────────────────────────────────────────────────────────

class KeyCaps(QWidget):
    """Klávesové čepičky: ((„Ctrl“, „+“), („F“,)) -> [Ctrl] + [+]  nebo  [F]."""

    def __init__(self, combos, parent=None):
        super().__init__(parent)
        self.combos = combos
        f = QFont(tokens.ui_family())
        f.setPixelSize(12)
        f.setWeight(QFont.Weight.DemiBold)
        self.setFont(f)
        self.setFixedSize(self._layout_width(), 26)
        self.setAccessibleName(" nebo ".join(" + ".join(c) for c in combos))

    def _parts(self):
        fm = self.fontMetrics()
        parts = []
        for i, combo in enumerate(self.combos):
            if i:
                parts.append(("sep", "/", fm.horizontalAdvance("/") + 12))
            for j, key in enumerate(combo):
                if j:
                    parts.append(("plus", "+", fm.horizontalAdvance("+") + 8))
                parts.append(("key", key, max(24, fm.horizontalAdvance(key) + 16)))
        return parts

    def _layout_width(self):
        return sum(w for _, _, w in self._parts())

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        x = 0.0
        for kind, text, w in self._parts():
            r = QRectF(x, 1, w, 24)
            if kind == "key":
                p.setPen(QPen(tokens.qcolor(t["separator"]), 1))
                p.setBrush(tokens.qcolor(t["surface_2"]))
                p.drawPath(smooth_rect_path(r.adjusted(0.5, 0.5, -0.5, -2.5), 6))
                p.setPen(tokens.qcolor(t["text"]))
                p.drawText(r.adjusted(0, 0, 0, -2), int(Qt.AlignmentFlag.AlignCenter), text)
            else:
                p.setPen(tokens.qcolor(t["text_tertiary"]))
                p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), text)
            x += w
        p.end()


class TipCard(QWidget):
    show_requested = pyqtSignal(object)      # Tip

    def __init__(self, tip, parent=None):
        super().__init__(parent)
        self.tip = tip
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(tip.title)
        title.setFont(tokens.font("headline"))
        head.addWidget(title, 1)
        if tip.keys:
            head.addWidget(KeyCaps(tip.keys, self), 0, Qt.AlignmentFlag.AlignRight)
        lay.addLayout(head)
        body = QLabel(tip.body)
        body.setWordWrap(True)
        body.setObjectName("tipBody")
        lay.addWidget(body)
        self.btn_show = None
        if tip.anchor:
            self.btn_show = KButton("Ukázat", compact=True, variant="ghost", icon="sparkle")
            self.btn_show.setToolTip("Přejde na místo v aplikaci a zvýrazní ho")
            self.btn_show.clicked.connect(lambda: self.show_requested.emit(self.tip))
            lay.addWidget(self.btn_show, 0, Qt.AlignmentFlag.AlignLeft)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillPath(smooth_rect_path(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), tokens.RADIUS["lg"]),
                   tokens.qcolor(t["surface"]))
        p.end()


class GuidePage(DetailPage):
    """Podstránka „Tipy a průvodce“: kapitoly vlevo (SidebarNav), tipy vpravo."""
    show_requested = pyqtSignal(object)      # Tip -> hlavní okno přejde na místo a zvýrazní ho

    def __init__(self, chapter_id=None, parent=None):
        super().__init__("Tipy a průvodce", "Kairos Spark · krátké rady, jak z knihovny dostat víc", parent=parent)
        body = QWidget()
        row = QHBoxLayout(body)
        row.setContentsMargins(16, 16, 0, 0)
        row.setSpacing(16)
        self.chapters = SidebarNav([(c.id, c.title, c.icon) for c in CHAPTERS])
        self.chapters.setFixedWidth(220)
        self.chapters.current_changed.connect(self.show_chapter)
        side = QVBoxLayout()
        side.addWidget(self.chapters)
        side.addStretch(1)
        row.addLayout(side)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        row.addWidget(self.scroll, 1)
        self.set_content(body)
        self.cards = []
        self.show_chapter(chapter_id if chapter(chapter_id) else CHAPTERS[0].id)

    def current_chapter(self):
        return self.chapters.current_key()

    def show_chapter(self, chapter_id):
        ch = chapter(chapter_id)
        if ch is None:
            return
        self.chapters.set_current(ch.id)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 20, 24)
        lay.setSpacing(10)
        title = QLabel(ch.title)
        title.setFont(tokens.font("large_title"))
        lay.addWidget(title)
        intro = QLabel(ch.intro)
        intro.setWordWrap(True)
        intro.setObjectName("pageSubtitle")
        lay.addWidget(intro)
        self.cards = []
        group = None
        for tip in ch.tips:
            if tip.group and tip.group != group:
                group = tip.group
                cap = QLabel(group.upper())
                cap.setObjectName("groupTitle")
                lay.addSpacing(6)
                lay.addWidget(cap)
            card = TipCard(tip)
            card.show_requested.connect(self.show_requested.emit)
            lay.addWidget(card)
            self.cards.append(card)
        lay.addStretch(1)
        old = self.scroll.takeWidget()
        self.scroll.setWidget(content)
        if old is not None:
            old.deleteLater()


# ── Spotlight (základ prohlídky) ─────────────────────────────────────────────

class SpotlightOverlay(QWidget):
    """Ztmaví host kromě cíle, kolem cíle prstenec zvýraznění a bublina s radou.
    Klik, Esc nebo Enter zavře. Jediná instance na okno; po zavření se zničí."""
    closed = pyqtSignal()

    CALLOUT_W = 320
    GAP = 14

    def __init__(self, host, target, title, body):
        super().__init__(host)
        self.host, self.target = host, target
        self.title, self.body = title, body
        self._closing = False
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Tip: {title}. {body}")
        self._opacity = motion.Tween(0.0, lambda v: self.update(), self)
        self._opacity.finished.connect(self._after_fade)
        host.installEventFilter(self)
        target.destroyed.connect(self.dismiss)
        self.setGeometry(host.rect())
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self._opacity.to(1.0, "emphasized")

    def hole(self):
        """Obdélník cíle v souřadnicích hostu (s okrajem)."""
        try:
            top_left = self.target.mapTo(self.host, QPoint(0, 0))
        except RuntimeError:
            return QRectF()
        return QRectF(QRect(top_left, self.target.size())).adjusted(-6, -6, 6, 6)

    def callout_rect(self):
        hole = self.hole()
        w = min(self.CALLOUT_W, max(160, self.width() - 32))
        h = self._callout_height(w)
        x = min(max(16.0, hole.center().x() - w / 2), self.width() - 16 - w)
        below = hole.bottom() + self.GAP
        y = below if below + h <= self.height() - 16 else max(16.0, hole.top() - self.GAP - h)
        return QRectF(x, y, w, h)

    def _fonts(self):
        tf = tokens.font("headline")
        bf = QFont(tokens.ui_family())
        bf.setPixelSize(13)
        hf = QFont(tokens.ui_family())
        hf.setPixelSize(11)
        return tf, bf, hf

    def _callout_height(self, w):
        tf, bf, hf = self._fonts()
        inner = int(w - 32)
        body_h = QFontMetrics(bf).boundingRect(QRect(0, 0, inner, 2000), int(Qt.TextFlag.TextWordWrap),
                                               self.body).height()
        return 16 + QFontMetrics(tf).height() + 6 + body_h + 10 + QFontMetrics(hf).height() + 14

    def dismiss(self):
        if self._closing:
            return
        self._closing = True
        self._opacity.to(0.0, "standard")

    def _after_fade(self):
        if self._closing and self._opacity.value <= 0.0:
            try:
                self.host.removeEventFilter(self)
            except RuntimeError:
                pass
            self.closed.emit()
            self.close()

    def eventFilter(self, obj, event):
        if obj is self.host and event.type() == QEvent.Type.Resize:
            self.setGeometry(self.host.rect())
        return False

    def mousePressEvent(self, e):
        self.dismiss()
        e.accept()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.dismiss()
            e.accept()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _):
        op = max(0.0, min(1.0, float(self._opacity.value)))
        if op <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setOpacity(op)
        hole = self.hole()
        radius = float(tokens.RADIUS["lg"])
        dim = QPainterPath()
        dim.addRect(QRectF(self.rect()))
        if not hole.isEmpty():
            dim = dim.subtracted(smooth_rect_path(hole, radius))
        p.fillPath(dim, QColor(0, 0, 0, 150))
        acc = tokens.qcolor(tokens.palette()["accent"])
        if not hole.isEmpty():
            glow = QColor(acc)
            glow.setAlpha(70)
            p.setPen(QPen(glow, 6))
            p.drawPath(smooth_rect_path(hole.adjusted(-2, -2, 2, 2), radius + 2))
            p.setPen(QPen(acc, 2))
            p.drawPath(smooth_rect_path(hole, radius))
        t = tokens.palette()
        box = self.callout_rect()
        p.setPen(QPen(tokens.qcolor(t["separator"]), 1))
        p.setBrush(tokens.qcolor(t["surface"]))
        p.drawPath(smooth_rect_path(box, radius))
        tf, bf, hf = self._fonts()
        x, y, inner = box.left() + 16, box.top() + 16, box.width() - 32
        p.setFont(tf)
        p.setPen(tokens.qcolor(t["text"]))
        th = p.fontMetrics().height()
        p.drawText(QRectF(x, y, inner, th), int(Qt.AlignmentFlag.AlignLeft), self.title)
        y += th + 6
        p.setFont(bf)
        p.setPen(tokens.qcolor(t["text_secondary"]))
        body_r = p.fontMetrics().boundingRect(QRect(0, 0, int(inner), 2000), int(Qt.TextFlag.TextWordWrap), self.body)
        p.drawText(QRectF(x, y, inner, body_r.height()), int(Qt.TextFlag.TextWordWrap), self.body)
        y += body_r.height() + 10
        p.setFont(hf)
        p.setPen(acc)
        p.drawText(QRectF(x, y, inner, p.fontMetrics().height()), int(Qt.AlignmentFlag.AlignLeft),
                   "Klikněte kamkoli nebo stiskněte Esc")
        p.end()
