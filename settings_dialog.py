"""Předvolby (Patch 11 / dávka E): Standardní a Pokročilé.

Vlevo lišta kategorií (Knihovna, Vzhled, Hudba, Rozpoznávání, Úložiště a mezipaměť, Mapa offline,
Soukromí; dole O aplikaci), nahoře hledání, vpravo karty s nastavením. Nebezpečné akce (vysypat koš,
smazat mezipaměť, tovární nastavení) jsou v nabídce „⋯“ v záhlaví karty; patička má jen Zrušit a Uložit.

Každý zásah ukazuje dopad PŘÍMO pod ovládacím prvkem (kolik osob, kolik MB, kolik položek).
Potvrzovací list jen u nevratných nebo hromadných akcí; nic z tohoto okna nesmaže databázi,
osoby ani originály (koš jde do Koše Windows, tovární nastavení = jen config.json).
"""
from i18n import N_, fmt_number, tr
import os
import re
import unicodedata
import webbrowser

from PyQt6.QtGui import QFont, QKeySequence, QPainter, QPen, QShortcut
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QFileDialog, QSlider, QMessageBox,
    QFrame, QWidget, QListWidget, QListWidgetItem, QButtonGroup, QLineEdit, QMenu, QPushButton,
    QCheckBox, QComboBox, QScrollArea, QProgressBar, QStackedWidget
)
from PyQt6.QtCore import QCollator, QEventLoop, QPoint, QRectF, QSize, Qt, QTimer
from ui_controls import (AccentPicker, InfoDot, KButton, SegmentedControl, ToggleSwitch, guard_wheel,
                         _c, _focus_ring, _rounded, palette_for)
from config import load_config, save_config, DEFAULT_CONFIG
from paths import THUMB_DIR, FACE_DIR
from cache_tools import clear_thumbnail_cache
import paths
import perf_profile
import power
import preview_cache
import theme
import video_proxy
import tokens
import ambient_music
import brand
import i18n
import map_precache
import tile_store

THEME_CHOICES = (("dark", N_("theme-dark")), ("light", N_("theme-light")), ("system", N_("theme-system")))
PREVIEW_MB_RANGE = (512, 8192)
PREVIEW_MB_STEP = 256
PROXY_MB_RANGE = (1024, 32768)
PROXY_MB_STEP = 512
# Sprint 3 item 3: offline map (map tile store budget, config "tile_store_mb")
MAP_MB_RANGE = (256, 8192)
MAP_MB_STEP = 256
MAP_JOB_POLL_MS = 400
# Sprint 4: every continent and country / territory (tile_store.REGIONS, world_regions catalogue)
MAP_CONTINENTS, MAP_COUNTRIES = tile_store.region_ids()
MAP_REGIONS = MAP_CONTINENTS + MAP_COUNTRIES
MAP_DETAILS = ((8, "settings-map-detail-overview"), (11, "settings-map-detail-roads"),
               (14, "settings-map-detail-streets"))
MAP_DEFAULT_REGION = "cz"
RAIL_WIDTH = 214           # lišta kategorií vlevo (logické px)
PANEL_SIZE = (1020, 820)   # list Předvoleb: lišta + obsah


def default_map_region():
    """The system's own country when it is in the catalogue, else Czechia."""
    try:
        from PyQt6.QtCore import QLocale
        code = QLocale.territoryToCode(QLocale.system().territory()).lower()
    except Exception:
        code = ""
    return code if code in tile_store.REGIONS else MAP_DEFAULT_REGION
# Gate 0: AI na pozadí při napájení z baterie (power.py)
BATTERY_CHOICES = [(N_("battery-throttle"), "throttle"), (N_("battery-pause"), "pause"), (N_("battery-full"), "full")]
PERF_CHOICES = [(N_("perf-auto"), "auto"), (N_("perf-high"), "high"), (N_("perf-standard"), "standard"),
                (N_("perf-baseline"), "baseline")]
# Výchozí řazení: uložená hodnota v config.json je identifikátor (historicky český text), popisek z katalogu
SORT_CHOICES = (("Datum pořízení (Nejnovější)", N_("sort-date-taken-newest")),  # i18n-ok: stored config value
                ("Datum pořízení (Nejstarší)", N_("sort-date-taken-oldest")),  # i18n-ok: stored config value
                ("Datum přidání do galerie", N_("sort-date-added")),  # i18n-ok: stored config value
                ("Podle výletů", N_("sort-by-trip")))  # i18n-ok: stored config value


def _choices(pairs):
    """[(message id, value)] -> [(text, value)] v aktuálním jazyce."""
    return [(tr(label), value) for label, value in pairs]


def get_folder_size_mb(path):
    """Vypočítá velikost složky v MB."""
    if not os.path.exists(path):
        return 0.0
    total_size = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total_size += os.path.getsize(fp)
    return total_size / (1024 * 1024)


def _czech_items(n):
    return tr("count-items", count=n)


def _czech_people(n, adjective="unconfirmed"):
    """(3, 'unconfirmed') -> '3 unconfirmed people' / '3 nepotvrzené osoby'."""
    return tr("count-people-unconfirmed", count=n)


class _SearchField(QLineEdit):
    """Pole hledání: Esc s textem ho jen vymaže (zavírá až prázdné pole); placeholder má AA kontrast."""

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape and self.text():
            self.clear()
            e.accept()
            return
        super().keyPressEvent(e)

    def changeEvent(self, e):
        super().changeEvent(e)
        self._fix_placeholder()

    def showEvent(self, e):
        super().showEvent(e)
        self._fix_placeholder()

    def _fix_placeholder(self):
        """Výchozí placeholder Qt je tertiary (pod 4.5 : 1) – použije se secondary."""
        pal = self.palette()
        sec = pal.color(pal.ColorRole.PlaceholderText)
        want = tokens.palette()["text_secondary"]
        if sec.name().lower() != want.lower():
            from PyQt6.QtGui import QColor
            pal.setColor(pal.ColorRole.PlaceholderText, QColor(want))
            self.setPalette(pal)


class _RailItem(KButton):
    """Položka lišty kategorií: vlevo zarovnaný text, vybraná = karta s akcentovým proužkem vlevo.
    Kreslí se z tokenů (text >= 4.5 : 1 na pozadí okna i na kartě), hover z KButton."""

    def __init__(self, text, parent=None):
        super().__init__(text, parent, variant="secondary")
        self.setObjectName("navItem")
        self.setCheckable(True)

    def sizeHint(self):
        return QSize(RAIL_WIDTH, 38)

    def paintEvent(self, _):
        t = palette_for(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        radius = tokens.RADIUS["sm"]
        hover = max(0.0, min(1.0, self._hover.value))
        if self.isChecked():
            p.fillPath(_rounded(rect, radius), _c(t, "surface"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_c(t, "accent"))
            p.drawRoundedRect(QRectF(rect.left() + 1, rect.top() + 9, 3, rect.height() - 18), 1.5, 1.5)
        elif hover:
            fill = _c(t, "hover")
            fill.setAlpha(int(fill.alpha() * hover))
            p.fillPath(_rounded(rect, radius), fill)
        if self._kbd_focus:
            _focus_ring(p, t, rect, radius)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold if self.isChecked() else QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(_c(t, "text") if self.isChecked() or hover > .5 else _c(t, "text_secondary"))
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(rect.width()) - 28)
        p.drawText(rect.adjusted(16, 0, -8, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), text)
        p.end()


class _AutoList(QListWidget):
    """Seznam, který roste s počtem položek (1 až max_rows řádků) místo pevně vyhrazeného bloku;
    prázdný ukáže větu, co dělat dál."""

    def __init__(self, max_rows=6, empty_text="", parent=None):
        super().__init__(parent)
        self._max_rows = max_rows
        self._empty_text = empty_text
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        model = self.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset, model.layoutChanged):
            sig.connect(self._fit)
        self._fit()

    def showEvent(self, e):
        super().showEvent(e)
        self._fit()                  # řádky mají po aplikaci stylu jinou výšku než při stavbě

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == e.Type.StyleChange:
            self._fit()

    def _fit(self, *_):
        n = self.count()
        row_h = self.sizeHintForRow(0) if n else 34
        rows = max(1, min(n, self._max_rows))
        self.setFixedHeight(rows * max(row_h, 28) + 2 * self.frameWidth() + 10)

    def paintEvent(self, e):
        super().paintEvent(e)
        if self.count() or not self._empty_text:
            return
        p = QPainter(self.viewport())
        p.setPen(tokens.qcolor(tokens.palette()["text_secondary"]))
        p.drawText(self.viewport().rect().adjusted(10, 0, -10, 0),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._empty_text)
        p.end()


def _hint(text):
    lbl = QLabel(text)
    lbl.setObjectName("hint")
    lbl.setWordWrap(True)
    return lbl


def _impact(text=""):
    lbl = QLabel(text)
    lbl.setObjectName("impact")
    lbl.setWordWrap(True)
    return lbl


class ConfirmSheet(QDialog):
    """Potvrzovací list: co se stane (dopad), volby a jedno pojmenované tlačítko akce."""

    def __init__(self, title, body, impact_lines=(), options=(), action_text=None,
                 destructive=True, parent=None):
        super().__init__(parent)
        action_text = action_text or tr("action-continue")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        # i18n-ok: markup wrapper around a translated title
        head = QLabel(f"<b style='font-size:15px'>{title}</b>")
        lay.addWidget(head)
        lay.addWidget(_hint(body))
        for line in impact_lines:
            lay.addWidget(_impact("• " + line))
        self.options = {}
        for key, label, checked in options:
            chk = QCheckBox(label)
            chk.setChecked(checked)
            self.options[key] = chk
            lay.addWidget(chk)
        row = QHBoxLayout()
        row.addStretch()
        cancel = KButton(tr("action-cancel"))
        cancel.clicked.connect(self.reject)
        cancel.setDefault(True)                       # Enter = bezpečná volba
        self.btn_action = KButton(action_text)
        self.btn_action.setObjectName("btnDanger" if destructive else "btnSave")
        self.btn_action.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.btn_action)
        lay.addLayout(row)

    def values(self):
        return {k: c.isChecked() for k, c in self.options.items()}


class SettingsDialog(QDialog):
    """Předvolby. as_sheet=True (hlavní okno): list v okně (ui_sheets.PanelSheet) se ztlumeným
    pozadím místo samostatného systémového okna; rozhraní exec() / accept() / reject() a
    výsledné příznaky zůstávají stejné. Bez rodiče (testy) klasický QDialog."""

    def __init__(self, parent=None, as_sheet=False):
        super().__init__(parent)
        self._sheet = None
        self._sheet_done = False
        if as_sheet and parent is not None:
            from ui_sheets import PanelSheet
            self.setWindowFlags(Qt.WindowType.Widget)
            self.setObjectName("settingsSheet")
            self._sheet = PanelSheet(parent, self, max_size=PANEL_SIZE)
            self._sheet.request_close = self.reject       # klik vedle karty = Zrušit
        self.setWindowTitle(tr("settings-window-title", APP_NAME=brand.APP_NAME))
        if self._sheet is None:
            self.resize(1000, 720)
        # Styl z tokenů (tokens.build_qss "settings"): motiv, zvýraznění, fajfka v zaškrtávátku
        tokens.apply_stylesheet(self, "settings")

        self.config = load_config()
        # výsledky pro hlavní okno (co po zavření obnovit)
        self.cache_cleaned = False          # miniatury smazány -> obnova na pozadí
        self.previews_cleared = False
        self.trash_emptied = False
        self.recluster_requested = False
        self.factory_reset_done = False
        self.map_cache_cleared = False      # Předvolby › Mapa offline › Vymazat
        self.guide_requested = False
        self.hidden_requested = False       # Předvolby › Skryté › Otevřít
        self._stats = self._read_library_stats()
        self.init_ui()
        # Founder audit: kolečko nad posuvníkem / seznamem posouvá stránku, dokud prvek nemá fokus
        guard_wheel(self)

    # ── list v okně ──
    @property
    def sheet(self):
        return self._sheet

    def exec(self):
        if self._sheet is None:
            return super().exec()
        loop = QEventLoop()
        self.finished.connect(lambda _r: loop.quit())
        self._sheet.open()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if not self._sheet_done:
            loop.exec()
        return self.result()

    def done(self, r):
        """V listu se karta nesmí schovat hned (prázdná karta při odjezdu): výsledek a signály
        hned, odjezd listu animovaně; list pak smaže i tento widget."""
        if getattr(self, "_map_timer", None) is not None:
            self._map_timer.stop()          # the offline-map job keeps running; only polling stops
        if getattr(self, "_sync_timer", None) is not None:
            self._sync_timer.stop()
        if self._sheet is None:
            return super().done(r)
        if self._sheet_done:
            return None
        self._sheet_done = True
        self.setResult(int(r))
        self._sheet._close(bool(r))
        if r == QDialog.DialogCode.Accepted:
            self.accepted.emit()
        else:
            self.rejected.emit()
        self.finished.emit(int(r))
        return None

    # ── data pro náhled dopadu ──
    def _read_library_stats(self):
        """Čísla pro náhled dopadu. Bez přístupu k DB (testy) se ukáže jen „—“."""
        stats = {"unconfirmed": None, "confirmed": None, "trash_plan": None}
        try:
            from database import get_connection
            import library_tools
            conn = get_connection()
            try:
                row = conn.execute("SELECT COALESCE(SUM(COALESCE(is_confirmed, 0) = 0), 0), "
                                   "COALESCE(SUM(COALESCE(is_confirmed, 0) = 1), 0) FROM people").fetchone()
                stats["unconfirmed"], stats["confirmed"] = int(row[0]), int(row[1])
                stats["trash_plan"] = library_tools.plan_empty_trash(conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Settings] statistiky knihovny: {e}")
        return stats

    def _confirm(self, title, body, impact_lines=(), options=(), action_text=None, destructive=True):
        """Potvrzovací list; vrací (potvrzeno, {volba: bool}). V testech se nahrazuje.
        V listu Předvoleb jako další list v okně (žádné systémové okno)."""
        if self._sheet is not None:
            from ui_sheets import Sheet
            sheet = Sheet(self.parentWidget(), title, body, action_text or tr("action-continue"),
                          destructive=destructive)
            for line in impact_lines:
                sheet.content.addWidget(_impact("• " + line))
            boxes = {}
            for key, label, checked in options:
                chk = QCheckBox(label)
                chk.setChecked(checked)
                boxes[key] = chk
                sheet.content.addWidget(chk)
            values = {}
            sheet.done.connect(lambda _ok: values.update({k: c.isChecked() for k, c in boxes.items()}))
            ok = sheet.exec()
            return ok, values
        sheet = ConfirmSheet(title, body, impact_lines, options, action_text, destructive, self)
        ok = sheet.exec() == QDialog.DialogCode.Accepted
        return ok, sheet.values()

    # ── rozvržení: nahoře název + hledání, vlevo lišta kategorií, vpravo stránky s kartami ──
    PAGE_KEYS = ("library", "appearance", "music", "recognition", "storage", "map", "privacy", "about")

    def init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 16)
        root.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel(tr("nav-settings"))
        title.setObjectName("sheetHeader")
        head.addWidget(title)
        head.addStretch(1)
        self.search = _SearchField()
        self.search.setObjectName("settingsSearch")
        self.search.setPlaceholderText(tr("settings-search-placeholder"))
        self.search.setAccessibleName(tr("settings-search-placeholder"))
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(300)
        self.search.textChanged.connect(self._apply_filter)
        head.addWidget(self.search)
        root.addLayout(head)
        find = QShortcut(QKeySequence.StandardKey.Find, self)
        find.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find.activated.connect(lambda: (self.search.setFocus(), self.search.selectAll()))

        body = QHBoxLayout()
        body.setSpacing(18)
        self.rail = QFrame()
        self.rail.setObjectName("rail")
        self.rail.setFixedWidth(RAIL_WIDTH)
        self._rail_lay = QVBoxLayout(self.rail)
        self._rail_lay.setContentsMargins(0, 2, 0, 0)
        self._rail_lay.setSpacing(2)
        body.addWidget(self.rail)

        col = QVBoxLayout()
        col.setSpacing(8)
        self.lbl_search_empty = QLabel()
        self.lbl_search_empty.setObjectName("hint")
        self.lbl_search_empty.setWordWrap(True)
        self.lbl_search_empty.hide()
        col.addWidget(self.lbl_search_empty)
        self.pages = QStackedWidget()
        col.addWidget(self.pages, 1)
        body.addLayout(col, 1)
        root.addLayout(body, 1)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav = []                   # [(tlačítko, název)] ve stejném pořadí jako stránky
        self._cards = []                 # [(karta, název karty, index stránky)] pro hledání
        self._filter_hidden = set()
        self._page_index = -1
        self._build_pages()
        self._nav[0][0].setChecked(True)
        self.pages.currentChanged.connect(lambda i: self._nav[i][0].setChecked(True))

        bottom_bar = QHBoxLayout()
        bottom_bar.addStretch()
        btn_cancel = KButton(tr("action-cancel"))
        btn_cancel.clicked.connect(self.reject)
        bottom_bar.addWidget(btn_cancel)
        btn_save = KButton(tr("settings-save"))
        btn_save.setObjectName("btnSave")
        btn_save.clicked.connect(self.save_and_close)
        bottom_bar.addWidget(btn_save)
        root.addLayout(bottom_bar)

    def _build_pages(self):
        titles = {"library": tr("settings-nav-library"), "appearance": tr("settings-nav-appearance"),
                  "music": tr("settings-nav-music"), "recognition": tr("settings-nav-recognition"),
                  "storage": tr("settings-nav-storage"), "map": tr("settings-map-offline"),
                  "privacy": tr("settings-nav-privacy"), "about": tr("settings-tab-about")}
        for key in self.PAGE_KEYS:
            if key == "about":
                self._rail_lay.addStretch(1)          # O aplikaci dole, mimo kategorie nastavení
            layout = self._add_page(titles[key])
            getattr(self, f"_page_{key}")(layout)
            layout.addStretch()

    def _add_page(self, title):
        """Nová stránka (nadpis + posuvná plocha) a její tlačítko v liště; vrací layout stránky."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 0, 10, 18)
        lay.setSpacing(14)
        heading = QLabel(title)
        heading.setObjectName("pageHeading")
        lay.addWidget(heading)
        self._page_index = self.pages.addWidget(self._scroll(page))
        btn = _RailItem(title)
        self._nav_group.addButton(btn)
        idx = self._page_index
        btn.clicked.connect(lambda _c=False, i=idx: self.pages.setCurrentIndex(i))
        self._rail_lay.addWidget(btn)
        self._nav.append((btn, title))
        return lay

    def go_to_page(self, key):
        self.pages.setCurrentIndex(self.PAGE_KEYS.index(key))

    def _scroll(self, page):
        page.setObjectName("page")
        area = QScrollArea()
        area.setFrameShape(QFrame.Shape.NoFrame)        # bez tvrdé linky kolem stránky
        area.setWidgetResizable(True)
        area.setWidget(page)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return area

    # ── hledání: schová řádky a karty, které neodpovídají; lišta ukáže počet nálezů ──
    @staticmethod
    def _fold(text):
        text = re.sub(r"<[^>]+>", " ", text or "")
        return "".join(c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c))

    @staticmethod
    def _card_rows(card):
        return [w for w in card.children() if isinstance(w, QWidget) and w.objectName() == "row"]

    def _row_text(self, row):
        parts = []
        for w in row.findChildren(QWidget):
            if isinstance(w, QLabel):
                parts.append(w.text())
            elif isinstance(w, (ToggleSwitch, QPushButton)):
                parts.append(w.text())
            if isinstance(w, InfoDot):
                parts.append(w.info)
        return self._fold(" ".join(parts))

    def _set_filtered(self, widget, show):
        """Schová / vrátí widget; nikdy neodkryje to, co schovala logika stránky (např. zařízení)."""
        if show:
            if widget in self._filter_hidden:
                widget.show()
                self._filter_hidden.discard(widget)
        elif not widget.isHidden():
            widget.hide()
            self._filter_hidden.add(widget)
        return widget not in self._filter_hidden and not widget.isHidden()

    def _set_row_visible(self, row, visible):
        """Logika stránky (prázdný stav, žádné zařízení…) schová / ukáže řádek i s oddělovačem;
        řádek, který právě drží hledání, se nechá být."""
        if row in self._filter_hidden:
            return
        row.setVisible(visible)
        if row._sep is not None:
            row._sep.setVisible(visible)

    def _apply_filter(self, text=""):
        words = self._fold(text).split()
        counts = [0] * len(self._nav)
        for card, title, page in self._cards:
            seps = [w for w in card.children() if isinstance(w, QFrame) and w.objectName() == "rowSep"]
            if not words:
                for row in self._card_rows(card):
                    self._set_filtered(row, True)
                    if row._sep is not None:
                        row._sep.setVisible(not row.isHidden())
                self._set_filtered(card, True)
                continue
            page_hit = all(w in self._fold(self._nav[page][1]) for w in words)
            title_hit = page_hit or all(w in self._fold(title) for w in words)
            hits = 0
            for row in self._card_rows(card):
                ok = title_hit or all(w in self._row_text(row) for w in words)
                hits += 1 if self._set_filtered(row, ok) else 0
            for sep in seps:
                sep.setVisible(False)
            shown = self._set_filtered(card, hits > 0)
            counts[page] += hits if shown else 0
        for (btn, title), n in zip(self._nav, counts):
            btn.setText(f"{title}   {fmt_number(n)}" if words and n else title)
        total = sum(counts)
        self.lbl_search_empty.setVisible(bool(words) and total == 0)
        self.pages.setVisible(not words or total > 0)
        if words and total == 0:
            self.lbl_search_empty.setText(tr("settings-search-empty", query=text.strip()))
        elif words and counts[self.pages.currentIndex()] == 0:
            self.pages.setCurrentIndex(next(i for i, n in enumerate(counts) if n))

    # ── karty a řádky (popisek vlevo, ovládání vpravo, oddělovač mezi řádky) ──
    def _group(self, layout, title, info=None, menu=None):
        """Karta s nadpisem uvnitř. info = vysvětlení v (i) vedle nadpisu; menu = [(atribut, text,
        slot) | None] – nabídka „⋯“ vpravo v záhlaví (nebezpečné akce: vysypat, smazat, obnovit).
        Položka nabídky je QAction uložená jako self.<atribut> (isEnabled / setEnabled / trigger)."""
        frame = QFrame()
        frame.setObjectName("group")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        head = QWidget()
        head.setObjectName("cardHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(18, 12, 12, 8)
        hl.setSpacing(4)
        cap = QLabel(title)
        cap.setObjectName("cardTitle")
        hl.addWidget(cap)
        if info:
            dot = InfoDot(info)
            dot.setObjectName("groupInfo")
            hl.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        hl.addStretch(1)
        if menu:
            hl.addWidget(self._card_menu(frame, menu))
        box.addWidget(head)
        layout.addWidget(frame)
        self._cards.append((frame, title, self._page_index))
        return frame

    def _card_menu(self, card, items):
        btn = KButton("", compact=True, icon="more")
        btn.setObjectName("cardMenu")
        btn.setToolTip(tr("settings-card-menu"))
        btn.setAccessibleName(tr("settings-card-menu"))
        menu = QMenu(btn)
        for item in items:
            if item is None:
                menu.addSeparator()
                continue
            attr, text, slot = item
            action = menu.addAction(text)
            action.triggered.connect(lambda _checked=False, s=slot: s())
            if attr:
                setattr(self, attr, action)
        card._menu = menu

        def pop():
            at = btn.mapToGlobal(QPoint(btn.width() - menu.sizeHint().width(), btn.height() + 2))
            menu.popup(at)
        btn.clicked.connect(lambda _c=False: pop())
        return btn

    @staticmethod
    def _row(group, label=None, control=None, value=None, stacked=None, info=None, desc=None):
        """Řádek karty. label + control = vedle sebe (popisek vlevo, ovládání vpravo); stacked =
        widget přes celou šířku pod popiskem (posuvník, seznam). value = QLabel s hodnotou vpravo
        od popisku. info = vysvětlení v (i) hned za popiskem; desc = krátký popis pod popiskem."""
        box = group.layout()
        sep = None
        if box.count():
            sep = QFrame()
            sep.setObjectName("rowSep")
            sep.setFixedHeight(1)
            box.addWidget(sep)
        row = QWidget()
        row.setObjectName("row")
        row._sep = sep                       # oddělovač nad řádkem se schovává spolu s ním
        lay = QVBoxLayout(row)
        lay.setContentsMargins(18, 11, 18, 11)
        lay.setSpacing(8)
        if label is not None or control is not None or value is not None:
            line = QHBoxLayout()
            line.setSpacing(14)
            if isinstance(label, str):
                label = QLabel(label)
            if label is not None:
                label.setWordWrap(not info)      # s (i) na jednom řádku (jinak se zalomí do úzka)
                label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                text_col = QVBoxLayout()
                text_col.setSpacing(2)
                head = QHBoxLayout()
                head.setSpacing(6)
                head.addWidget(label, 0 if info else 1)
                if info:
                    head.addWidget(InfoDot(info), 0, Qt.AlignmentFlag.AlignVCenter)
                    head.addStretch(1)
                text_col.addLayout(head)
                if desc:
                    text_col.addWidget(_hint(desc))
                line.addLayout(text_col, 1)
            if value is not None:
                value.setObjectName("rowValue")
                line.addWidget(value, 0, Qt.AlignmentFlag.AlignRight)
            if control is not None:
                line.addWidget(control, 0 if label is not None else 1, Qt.AlignmentFlag.AlignRight
                               if label is not None else Qt.AlignmentFlag.AlignLeft)
            lay.addLayout(line)
        if stacked is not None:
            if isinstance(stacked, QHBoxLayout):
                lay.addLayout(stacked)
            else:
                lay.addWidget(stacked)
        box.addWidget(row)
        return row

    # ── stránky ──
    def _page_library(self, layout):
        g = self._group(layout, tr("settings-library-folders"), info=tr("settings-folders-hint"))
        self.list_folders = _AutoList(max_rows=6, empty_text=tr("settings-folders-empty"))
        self.list_folders.setToolTip(tr("settings-watched-tooltip"))
        self.list_folders.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        watched = {os.path.normcase(f) for f in self.config.get("watched_folders", [])}
        for f in self.config.get("library_folders", []):
            self._add_folder_item(f, os.path.normcase(f) in watched)
        btn_box = QHBoxLayout()
        btn_add = KButton(tr("settings-add-folder-action"), variant="primary", compact=True)
        btn_add.setObjectName("btnAddFolder")
        btn_add.clicked.connect(self.add_library_folder)
        btn_remove = KButton(tr("settings-remove-selected"), compact=True)
        btn_remove.clicked.connect(self.remove_library_folder)
        btn_box.addWidget(btn_add)
        btn_box.addWidget(btn_remove)
        btn_box.addStretch()
        self._row(g, stacked=self.list_folders)
        self._row(g, stacked=btn_box)
        self.chk_autoscan = ToggleSwitch(tr("settings-scan-on-start"))
        self.chk_autoscan.setChecked(self.config.get("auto_scan_on_startup", True))
        self._row(g, stacked=self.chk_autoscan)

        g = self._group(layout, tr("settings-photo-library"), info=tr("settings-tile-size-hint"))
        self.combo_default_sort = QComboBox()
        for value, label in SORT_CHOICES:
            self.combo_default_sort.addItem(tr(label), value)
        self._set_sort(self.config.get("default_sort_order", SORT_CHOICES[0][0]))
        self._row(g, tr("settings-default-sort"), self.combo_default_sort)
        self.chk_xmp = ToggleSwitch(tr("settings-xmp"))
        self.chk_xmp.setChecked(bool(self.config.get("xmp_sidecars", True)))
        self._row(g, stacked=self.chk_xmp)
        self.chk_borrow = ToggleSwitch(tr("settings-borrow-location"))
        self.chk_borrow.setChecked(bool(self.config.get("borrow_location", True)))
        self._row(g, stacked=self.chk_borrow)

        g = self._group(layout, tr("nav-trash"), info=tr("settings-trash-hint"),
                        menu=[("btn_empty_trash", tr("settings-empty-trash"), self.empty_trash)])
        self.lbl_trash = QLabel()
        self.lbl_trash.setWordWrap(True)
        self._row(g, stacked=self.lbl_trash)
        self.refresh_trash_label()

    def _page_appearance(self, layout):
        g = self._group(layout, tr("settings-appearance"), info=tr("settings-appearance-hint"))
        # Gate 1: jazyk rozhraní – přepne se hned po uložení, bez restartu
        self.combo_language = QComboBox()
        self.combo_language.setObjectName("combo_language")
        self.combo_language.addItem(tr("settings-language-system", language=i18n.native_name(i18n.system_language())),
                                    i18n.SYSTEM)
        for code, name in i18n.LANGUAGES:
            self.combo_language.addItem(name, code)
        self.set_language_choice(self.config.get("language", i18n.SYSTEM))
        self._row(g, tr("settings-language"), self.combo_language)
        current = self.config.get("theme", theme.DEFAULT_THEME)
        self.theme_switch = SegmentedControl([(tr("theme-system"), "system"), (tr("theme-dark"), "dark"), (tr("theme-light"), "light")],
                                             current=current if current in dict(THEME_CHOICES) else "dark")
        self._row(g, tr("settings-theme"), self.theme_switch)
        self.accent_picker = AccentPicker(self.config.get("accent", tokens.DEFAULT_ACCENT),
                                          self.config.get("accent_custom", ""))
        self._row(g, tr("settings-accent"), self.accent_picker)
        self.chk_adaptive = ToggleSwitch(tr("settings-adaptive"))
        self.chk_adaptive.set_info(tr("settings-adaptive-tooltip"))
        self.chk_adaptive.setChecked(bool(self.config.get("adaptive_accent", True)))
        self._row(g, stacked=self.chk_adaptive)
        self.chk_reduce_motion = ToggleSwitch(tr("settings-reduce-motion"))
        self.chk_reduce_motion.setChecked(bool(self.config.get("reduce_motion", False)))
        self._row(g, stacked=self.chk_reduce_motion)
        self.chk_memories = ToggleSwitch(tr("settings-show-memories"))
        self.chk_memories.setChecked(bool(self.config.get("show_memories", True)))
        self._row(g, stacked=self.chk_memories)

        # Barvy: výchozí = kontrakt Windows (color_manage.MODE_SYSTEM); přepínač = volitelný
        # převod do nativního gamutu panelu (MODE_PANEL)
        g = self._group(layout, tr("settings-colors"))
        import color_manage
        self.chk_color_display = ToggleSwitch(tr("settings-color-display"))
        self.chk_color_display.setChecked(
            color_manage.normalize_mode(self.config.get("color_output")) == color_manage.MODE_PANEL)
        tgt = color_manage.target()
        self.chk_color_display.set_info(
            tr("settings-color-hint", name=tgt['name'], reason=tr(tgt['reason']) if i18n.has(tgt['reason']) else tgt['reason']))
        self._row(g, stacked=self.chk_color_display)

    def _page_music(self, layout):
        # Gate 2 D: hudba pro Cestu - vlastní skladby v %APPDATA%/Kairos/music
        g = self._group(layout, tr("settings-music"), info=tr("settings-music-hint"))
        self.list_music = _AutoList(max_rows=6, empty_text=tr("settings-music-empty"))
        self.list_music.setObjectName("list_music")
        self._reload_music()
        music_box = QHBoxLayout()
        self.btn_add_music = KButton(tr("music-add"), variant="primary", compact=True)
        self.btn_add_music.clicked.connect(lambda _checked=False: self.add_music())
        btn_remove_music = KButton(tr("settings-remove-selected"), compact=True)
        btn_remove_music.clicked.connect(self.remove_music)
        btn_open_music = KButton(tr("music-open-folder"), compact=True)
        btn_open_music.clicked.connect(self.open_music_folder)
        for b in (self.btn_add_music, btn_remove_music, btn_open_music):
            music_box.addWidget(b)
        music_box.addStretch()
        self._row(g, tr("settings-music-own"), stacked=self.list_music)
        self._row(g, stacked=music_box)

    def _page_recognition(self, layout):
        g = self._group(layout, tr("guide-ch-faces"))
        self._saved_threshold = int(round(float(self.config.get("face_similarity_threshold", 0.62)) * 100))
        self.lbl_threshold = QLabel()
        self.slider_threshold = QSlider(Qt.Orientation.Horizontal)
        self.slider_threshold.setRange(30, 90)   # 0.30 – 0.90
        self.slider_threshold.setValue(self._saved_threshold)
        self.slider_threshold.valueChanged.connect(self._update_threshold_impact)
        row = self._row(g, tr("settings-face-threshold"), value=self.lbl_threshold, stacked=self.slider_threshold,
                        info=tr("settings-face-threshold-hint"))
        self.lbl_threshold_impact = _impact()
        row.layout().addWidget(self.lbl_threshold_impact)
        self.btn_recluster = KButton(tr("settings-regroup"), compact=True)
        self.btn_recluster.clicked.connect(self.request_recluster)
        self._row(g, tr("settings-regroup-row"), self.btn_recluster)
        self._update_threshold_impact(self.slider_threshold.value())

        # Sprint 4.0: přesné hledání (SigLIP 2) – volitelné, výchozí lehký CLIP
        g = self._group(layout, tr("settings-search-card"))
        import siglip_engine
        self.chk_precise = ToggleSwitch(tr("settings-precise-search-toggle"))
        self.chk_precise.setChecked(bool(self.config.get("search_precise", False)))
        state = (tr("settings-model-downloaded") if siglip_engine.installed()
                 else tr("settings-model-download-size", value=fmt_number(siglip_engine.DOWNLOAD_MB / 1000, 1)))
        self.chk_precise.set_info(tr("settings-precise-search-hint") + state)
        self._row(g, stacked=self.chk_precise)
        self.chk_ocr = ToggleSwitch(tr("settings-ocr"))
        self.chk_ocr.setChecked(bool(self.config.get("ocr_text", True)))
        self._row(g, stacked=self.chk_ocr)

        # Fáze 1: adaptivní škálování (rozhodnutí C) a chování AI na baterii
        g = self._group(layout, tr("settings-perf-card"))
        tier = self.config.get("performance_tier", "auto")
        self.perf_switch = SegmentedControl(_choices(PERF_CHOICES), current=tier if tier in dict(PERF_CHOICES).values() else "auto")
        detected = perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb())
        tier_label = {k: t for t, k in PERF_CHOICES}.get(detected)
        self._row(g, tr("settings-performance"), self.perf_switch,
                  info=tr("settings-perf-hint", tier=tr(tier_label) if tier_label else detected))
        pol = self.config.get("battery_policy", power.DEFAULT_POLICY)
        self.battery_switch = SegmentedControl(_choices(BATTERY_CHOICES),
                                               current=pol if pol in dict(BATTERY_CHOICES).values() else "throttle")
        self._row(g, tr("settings-battery-ai"), self.battery_switch,
                  info=tr("settings-battery-hint", pct=power.LOW_BATTERY_PCT, now=power.get().describe()))

    def _page_storage(self, layout):
        g = self._group(layout, tr("settings-memory-caches"), info=tr("settings-caches-hint"), menu=[
            ("act_delete_thumbs", tr("settings-delete-thumbs"), self.clean_cache),
            ("btn_clear_previews", tr("settings-delete-previews"), self.clear_previews),
            ("btn_clear_proxies", tr("settings-delete-video-copies"), self.clear_proxies)])
        lru_val = int(self.config.get("thumbnail_lru_capacity", 1200))
        self.lbl_lru = QLabel()
        self.slider_lru = QSlider(Qt.Orientation.Horizontal)
        self.slider_lru.setRange(64, 4000)
        self.slider_lru.setValue(lru_val)
        self.slider_lru.valueChanged.connect(self._update_lru_impact)
        row = self._row(g, tr("settings-ram-thumbs"), value=self.lbl_lru, stacked=self.slider_lru)
        self.lbl_lru_impact = _hint("")
        row.layout().addWidget(self.lbl_lru_impact)
        self._update_lru_impact(lru_val)

        self.lbl_preview_mb = QLabel()
        self.slider_preview_mb = QSlider(Qt.Orientation.Horizontal)
        self.slider_preview_mb.setRange(*PREVIEW_MB_RANGE)
        self.slider_preview_mb.setSingleStep(PREVIEW_MB_STEP)
        self.slider_preview_mb.setPageStep(PREVIEW_MB_STEP * 4)
        self.slider_preview_mb.setValue(int(self.config.get("preview_cache_mb", preview_cache.DEFAULT_BUDGET_MB)))
        self.slider_preview_mb.valueChanged.connect(self._update_preview_impact)
        row = self._row(g, tr("settings-preview-cache"), value=self.lbl_preview_mb, stacked=self.slider_preview_mb)
        self.lbl_preview_impact = _hint("")
        row.layout().addWidget(self.lbl_preview_impact)

        self.lbl_proxy_mb = QLabel()
        self.slider_proxy_mb = QSlider(Qt.Orientation.Horizontal)
        self.slider_proxy_mb.setRange(*PROXY_MB_RANGE)
        self.slider_proxy_mb.setSingleStep(PROXY_MB_STEP)
        self.slider_proxy_mb.setPageStep(PROXY_MB_STEP * 4)
        self.slider_proxy_mb.setValue(int(self.config.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB)))
        self.slider_proxy_mb.valueChanged.connect(self._update_proxy_impact)
        row = self._row(g, tr("settings-video-copies"), value=self.lbl_proxy_mb, stacked=self.slider_proxy_mb)
        self.lbl_proxy_impact = _hint("")
        row.layout().addWidget(self.lbl_proxy_impact)
        self._update_proxy_impact(self.slider_proxy_mb.value())

        self.lbl_cache_size = QLabel()
        self.lbl_cache_size.setWordWrap(True)
        self._row(g, stacked=self.lbl_cache_size)
        self.refresh_cache_size_label()

        # Gate 0: umístění dat a přesun mezipaměti na jiný disk (provede se při dalším startu)
        self._pending_cache_dir = None
        self.lbl_cache_dir = QLabel()
        self.lbl_cache_dir.setWordWrap(True)
        self.lbl_cache_dir.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_move_cache = KButton(tr("settings-move-ellipsis"), compact=True)
        self.btn_move_cache.clicked.connect(self.choose_cache_dir)
        self._row(g, tr("settings-cache-location"), self.btn_move_cache,
                  info=tr("settings-cache-location-hint", DATA_DIR=paths.DATA_DIR, FACE_DIR=paths.FACE_DIR))
        self._row(g, stacked=self.lbl_cache_dir)
        self._update_cache_dir_label()

    def _page_map(self, layout):
        self._build_map_offline_group(layout)

    def _page_privacy(self, layout):
        # Stage 3: složka Skryté
        g = self._group(layout, tr("nav-locked"), info=tr("settings-locked-hint"))
        self.chk_hidden_entry = ToggleSwitch(tr("settings-show-locked"))
        self.chk_hidden_entry.setChecked(bool(self.config.get("show_hidden_folder", True)))
        self._row(g, stacked=self.chk_hidden_entry)
        self.btn_open_hidden = KButton(tr("action-open-ellipsis"), compact=True, icon="lock")
        self.btn_open_hidden.clicked.connect(self.open_hidden)
        self._row(g, tr("settings-locked-row"), self.btn_open_hidden)
        self.btn_hidden_pass = KButton(tr("settings-change-passcode"), compact=True)
        self.btn_hidden_pass.clicked.connect(self.change_hidden_passcode)
        self._row(g, tr("settings-locked-passcode-row"), self.btn_hidden_pass)
        self._build_mobile_sync_group(layout)

    def _page_about(self, layout):
        g = self._group(layout, tr("settings-tab-about"))
        self._row(g, stacked=brand.Wordmark(34))
        self._row(g, stacked=QLabel(tr("settings-version")))
        self._row(g, stacked=_hint(tr("settings-about-body")))
        btn_newsletter = KButton(tr("settings-newsletter"), compact=True)
        btn_newsletter.clicked.connect(self.open_newsletter_signup)
        self._row(g, stacked=self._left(btn_newsletter))

        # Nápověda (Polish P2) – otevře stránku Tipy a průvodce v hlavním okně
        g = self._group(layout, tr("guide-where-help"), info=tr("settings-guide-hint"))
        self.btn_guide = KButton(tr("action-open-ellipsis"), compact=True, icon="sparkle")
        self.btn_guide.clicked.connect(self.open_guide)
        self._row(g, tr("settings-guide-row"), self.btn_guide)

        g = self._group(layout, tr("settings-recovery"), menu=[
            ("act_reset_form", tr("settings-reset-form").lstrip("↺ ").strip(), self.reset_to_defaults),
            None,
            ("btn_factory_reset", tr("settings-factory-reset"), self.factory_reset)])
        self.lbl_factory = _hint("")
        self._row(g, stacked=self.lbl_factory)
        self._update_factory_label()

    @staticmethod
    def _left(widget):
        box = QHBoxLayout()
        box.addWidget(widget)
        box.addStretch()
        return box

    def open_guide(self):
        self.guide_requested = True
        self.reject()

    def open_hidden(self):
        self.hidden_requested = True
        self.reject()

    def change_hidden_passcode(self):
        locked = getattr(self.parent(), "locked", None)
        if locked is None:
            return
        from ui_toast import show_toast
        if locked.change_passcode(self):
            show_toast(self.parent(), tr("settings-passcode-set"), "success")

    # ── Mapa offline (Sprint 3 item 3) ──
    def _build_map_offline_group(self, layout):
        """Map cache usage, its limit, photo places offline, region packs, clearing. Everything
        slow (SQLite, network, file copies) runs in map_precache's job thread; this page only
        reads counters (map_precache.usage, MapJobs.state) from a timer."""
        g = self._group(layout, tr("settings-map-offline"), info=tr("settings-map-offline-hint"), menu=[
            ("btn_map_remove", tr("settings-map-remove"), self.remove_map_pack),
            ("btn_map_clear", tr("settings-map-clear"), self.clear_map_cache)])
        self.lbl_map_usage = QLabel()
        self.bar_map_usage = QProgressBar()
        self.bar_map_usage.setObjectName("usageBar")
        self.bar_map_usage.setRange(0, 1000)
        self.bar_map_usage.setTextVisible(False)
        self.bar_map_usage.setFixedHeight(6)
        self._row(g, tr("settings-map-cache"), value=self.lbl_map_usage, stacked=self.bar_map_usage)

        self.lbl_map_mb = QLabel()
        self.slider_map_mb = QSlider(Qt.Orientation.Horizontal)
        self.slider_map_mb.setRange(*MAP_MB_RANGE)
        self.slider_map_mb.setSingleStep(MAP_MB_STEP)
        self.slider_map_mb.setPageStep(MAP_MB_STEP * 4)
        self.slider_map_mb.setValue(int(self.config.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
        self.slider_map_mb.valueChanged.connect(self._update_map_usage)
        self._row(g, tr("settings-map-limit"), value=self.lbl_map_mb, stacked=self.slider_map_mb)

        self.chk_map_prefetch = ToggleSwitch(tr("settings-map-auto"))
        self.chk_map_prefetch.setChecked(bool(self.config.get("map_prefetch", True)))
        self.chk_map_prefetch.set_info(tr("settings-map-auto-hint"))
        self._row(g, stacked=self.chk_map_prefetch)

        self.btn_map_precache = KButton(tr("settings-map-precache"), compact=True)
        self.btn_map_precache.clicked.connect(self.toggle_map_precache)
        self._row(g, tr("settings-map-precache-row"), self.btn_map_precache)
        self.lbl_map_job = _hint("")
        self._row_map_job = self._row(g, stacked=self.lbl_map_job)

        self.combo_map_region = QComboBox()
        self.combo_map_region.setMaxVisibleItems(18)
        lang = i18n.current()
        collator = QCollator(i18n.qlocale())
        for rid in sorted(MAP_CONTINENTS, key=lambda r: collator.sortKey(tile_store.region_name(r, lang))):
            self.combo_map_region.addItem(tile_store.region_name(rid, lang), rid)
        self.combo_map_region.insertSeparator(self.combo_map_region.count())
        for rid in sorted(MAP_COUNTRIES, key=lambda r: collator.sortKey(tile_store.region_name(r, lang))):
            self.combo_map_region.addItem(tile_store.region_name(rid, lang), rid)
        self.combo_map_region.setCurrentIndex(self.combo_map_region.findData(default_map_region()))
        self.combo_map_detail = QComboBox()
        for z, key in MAP_DETAILS:
            self.combo_map_detail.addItem(tr(key), z)
        self.combo_map_detail.setCurrentIndex(len(MAP_DETAILS) - 1)
        self.combo_map_detail.setToolTip(tr("settings-map-detail"))
        self.combo_map_region.currentIndexChanged.connect(self._on_map_region_changed)
        self.combo_map_detail.currentIndexChanged.connect(self._update_region_estimate)
        self.btn_map_region = KButton(tr("settings-map-region-download"), compact=True)
        self.btn_map_region.clicked.connect(self.download_region_pack)
        line = QHBoxLayout()
        line.addWidget(self.combo_map_region, 3)
        line.addWidget(self.combo_map_detail, 2)
        line.addWidget(self.btn_map_region)
        self._row(g, tr("settings-map-regions"), info=tr("settings-map-regions-hint"), stacked=line)
        self.lbl_map_region = _hint("")
        self._row(g, stacked=self.lbl_map_region)

        self.list_map_packs = _AutoList(max_rows=5)
        self.btn_map_import = KButton(tr("settings-map-import"), compact=True)
        self.btn_map_import.clicked.connect(self.import_map_pack)
        packs_row = QHBoxLayout()
        packs_row.addWidget(self.btn_map_import)
        packs_row.addStretch()
        self._row(g, tr("settings-map-packs"), stacked=self.list_map_packs)
        self._row(g, stacked=packs_row)

        self._map_packs_shown = None
        self._map_job_seen = None           # job "started" stamp whose finish was handled
        self._map_timer = QTimer(self)
        self._map_timer.setInterval(MAP_JOB_POLL_MS)
        self._map_timer.timeout.connect(self.refresh_map_offline)
        self._map_timer.start()
        self._update_region_estimate()
        self.refresh_map_offline()

    # ── Synchronizace s iPhonem (iOS Phase 2) ──
    def _build_mobile_sync_group(self, layout):
        """LAN bridge for Kairos on iPhone. The switch acts at once, like Wi-Fi: it starts or stops
        sync_server and persists `sync_server_enabled` immediately, because pairing needs the
        bridge running while this sheet is still open. Binding runs on sync_server's worker
        thread; this page only reads its state from a timer."""
        import sync_server
        self._sync = sync_server
        g = self._group(layout, tr("settings-mobile-sync"), info=tr("settings-mobile-sync-hint"))
        self.chk_sync = ToggleSwitch(tr("settings-mobile-sync-toggle"))
        self.chk_sync.setChecked(bool(self.config.get("sync_server_enabled", False)))
        self.chk_sync.toggled.connect(self.set_sync_enabled)
        self._row(g, stacked=self.chk_sync)
        self.lbl_sync_status = _hint("")
        self._row(g, stacked=self.lbl_sync_status)
        self.btn_pair_phone = KButton(tr("settings-mobile-pair"), compact=True)
        self.btn_pair_phone.clicked.connect(self.pair_phone)
        self._row(g, tr("settings-mobile-pair-row"), self.btn_pair_phone)
        self.lbl_sync_devices = QLabel()
        self.list_sync_devices = _AutoList(max_rows=4)
        self.btn_sync_forget = KButton(tr("settings-mobile-forget"), compact=True)
        self.btn_sync_forget.clicked.connect(self.forget_phone)
        forget_row = QHBoxLayout()
        forget_row.addWidget(self.btn_sync_forget)
        forget_row.addStretch()
        self._row(g, tr("settings-mobile-devices"), value=self.lbl_sync_devices, stacked=self.list_sync_devices)
        self._sync_forget_row = self._row(g, stacked=forget_row)
        self._sync_starting = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(MAP_JOB_POLL_MS)
        self._sync_timer.timeout.connect(self.refresh_sync_status)
        self._sync_timer.start()
        self.refresh_sync_devices()
        self.refresh_sync_status()

    def set_sync_enabled(self, on):
        on = bool(on)
        cfg = load_config()                     # persist just this key: it took effect already
        cfg["sync_server_enabled"] = on
        save_config(cfg)
        self.config["sync_server_enabled"] = on
        if on:
            if self._sync.service() is None:
                self._sync_starting = True
                self._sync.start_async(on_ready=self._on_sync_ready)
        else:
            self._sync_starting = False
            self._sync.stop()
        self.refresh_sync_status()

    def _on_sync_ready(self, _srv):             # worker thread: only a flag; the timer redraws
        self._sync_starting = False

    def refresh_sync_status(self):
        srv = self._sync.service()
        if srv is not None:
            from phone_pairing import address_of
            addr = address_of(f"kairos://pair?h={self._sync.lan_address()}&p={srv.port}")
            text = tr("settings-mobile-status-on", address=addr)
        elif self._sync_starting:
            text = tr("settings-mobile-status-starting")
        elif self.chk_sync.isChecked() and self._sync.last_error is not None:
            text = tr("settings-mobile-status-failed", port=str(self._sync.DEFAULT_PORT))
        else:
            text = tr("settings-mobile-status-off")
        if self.lbl_sync_status.text() != text:
            self.lbl_sync_status.setText(text)

    def refresh_sync_devices(self):
        try:
            devices = self._sync.paired_devices()
        except Exception as e:                  # noqa: BLE001 – a broken devices.json must not break Settings
            print(f"[Sync] {e}")
            devices = []
        self.list_sync_devices.clear()
        for d in devices:
            when = (d.get("paired_at") or "")[:10]
            name = d.get("name") or tr("pair-device-fallback")
            item = QListWidgetItem(f"{name}  ·  {when}" if when else name)
            item.setData(Qt.ItemDataRole.UserRole, d.get("id"))
            self.list_sync_devices.addItem(item)
        self.lbl_sync_devices.setText(fmt_number(len(devices)) if devices else tr("settings-mobile-devices-none"))
        self.list_sync_devices.setVisible(bool(devices))
        self._set_row_visible(self._sync_forget_row, bool(devices))

    def pair_phone(self):
        """Pairing needs the bridge: the switch turns on (and persists) first."""
        if not self.chk_sync.isChecked():
            self.chk_sync.setChecked(True)      # -> set_sync_enabled(True)
        from phone_pairing import PairPhoneDialog
        dlg = PairPhoneDialog(self)
        dlg.exec()
        self.refresh_sync_devices()
        self.refresh_sync_status()

    def forget_phone(self):
        item = self.list_sync_devices.currentItem()
        if item is None and self.list_sync_devices.count() == 1:
            item = self.list_sync_devices.item(0)
        if item is None:
            return
        name = item.text().split("  ·  ")[0]
        ok, _ = self._confirm(tr("settings-mobile-forget-title", device=name),
                              tr("settings-mobile-forget-body"), action_text=tr("settings-mobile-forget"))
        if not ok:
            return
        self._sync.forget_device(item.data(Qt.ItemDataRole.UserRole))
        self.refresh_sync_devices()

    def _map_budget_mb(self):
        return int(round(self.slider_map_mb.value() / MAP_MB_STEP) * MAP_MB_STEP)

    def _update_map_usage(self, *_):
        u = map_precache.usage()
        budget = self._map_budget_mb() * 1024 * 1024
        self.lbl_map_mb.setText(tr("unit-size", value=fmt_number(budget / 2 ** 30, 2), unit="GB"))
        self.lbl_map_usage.setText(tr("settings-map-usage", used=fmt_number(u["bytes"] / 2 ** 20, 0),
                                      limit=fmt_number(budget / 2 ** 30, 1)))
        self.bar_map_usage.setValue(int(min(1.0, u["bytes"] / max(1, budget)) * 1000))
        return u

    def _installed_packs(self, u):
        """Pack list from the running store (metadata read off-thread), else names and sizes
        from the folder (a directory listing, no SQLite)."""
        if u.get("live"):
            return [(p["name"], self._pack_title(p["name"], p["title"]), p["bytes"]) for p in u["packs"]]
        out = []
        try:
            for n in sorted(os.listdir(tile_store.packs_dir())):
                if n.lower().endswith(tile_store.PACK_SUFFIX):
                    name = os.path.splitext(n)[0]
                    out.append((name, self._pack_title(name, name), os.path.getsize(os.path.join(tile_store.packs_dir(), n))))
        except OSError:
            pass
        return out

    @staticmethod
    def _pack_title(name, title):
        return tr("settings-map-world-pack") if name == tile_store.WORLD_PACK else title

    def refresh_map_offline(self):
        u = self._update_map_usage()
        packs = self._installed_packs(u)
        if packs != self._map_packs_shown:
            self._map_packs_shown = packs
            self.list_map_packs.clear()
            for name, title, size in packs:
                item = QListWidgetItem(tr("settings-map-pack-item", name=title,
                                          mb=fmt_number(size / 2 ** 20, 0)))
                item.setData(Qt.ItemDataRole.UserRole, name)
                self.list_map_packs.addItem(item)
            if not packs:
                item = QListWidgetItem(tr("settings-map-packs-none"))
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                self.list_map_packs.addItem(item)
        st = map_precache.jobs().state()
        busy = map_precache.jobs().running()
        self.lbl_map_job.setText(self._map_job_text(st))
        self._set_row_visible(self._row_map_job, bool(self.lbl_map_job.text()))
        precaching = busy and st.get("kind") == "precache"
        self.btn_map_precache.setText(tr("settings-map-stop") if precaching else tr("settings-map-precache"))
        self.btn_map_precache.setEnabled(precaching or not busy)
        regioning = busy and st.get("kind") == "region"
        self.btn_map_region.setText(tr("settings-map-stop") if regioning else tr("settings-map-region-download"))
        self.btn_map_region.setEnabled(regioning or (not busy and self._region_fits()))
        for b in (self.btn_map_import, self.btn_map_clear):
            b.setEnabled(not busy)
        self.btn_map_remove.setEnabled(not busy and bool(packs))
        stamp = st.get("started")
        if not busy and stamp and stamp != self._map_job_seen and st.get("phase") in map_precache.FINAL:
            self._map_job_seen = stamp
            if st.get("kind") == "clear" and st.get("phase") == map_precache.DONE:
                self.map_cache_cleared = True

    @staticmethod
    def _map_job_text(st):
        kind, phase = st.get("kind"), st.get("phase")
        if not kind or phase == map_precache.IDLE:
            return ""
        mb = fmt_number((st.get("bytes") or 0) / 2 ** 20, 0)
        if kind == "clear":
            return tr("settings-map-clearing") if phase not in map_precache.FINAL else (
                tr("settings-map-cleared") if phase == map_precache.DONE else tr("settings-map-job-failed", reason=st.get("reason", "")))
        if kind == "import":
            if phase == map_precache.DONE:
                return tr("settings-map-imported", name=st.get("pack", ""))
            if phase == map_precache.FAILED:
                return tr("settings-map-import-failed", reason=st.get("reason", ""))
            return tr("settings-map-importing")
        if phase == map_precache.PREPARING:
            return tr("settings-map-preparing")
        if phase == map_precache.WAITING:
            return tr({"busy": "settings-map-wait-busy", "metered": "settings-map-wait-metered",
                       "battery": "settings-map-wait-battery"}.get(st.get("reason"), "settings-map-wait-busy"))
        done = (st.get("done") or 0) + (st.get("skipped") or 0) + (st.get("failed") or 0)
        if phase == map_precache.RUNNING and st.get("stage") == "world":
            return tr("settings-map-world-progress", done=fmt_number(done, 0),
                      total=fmt_number(st.get("total") or 0, 0), mb=mb)
        if phase == map_precache.RUNNING:
            return tr("settings-map-progress", done=fmt_number(done, 0), total=fmt_number(st.get("total") or 0, 0), mb=mb)
        if phase == map_precache.DONE:
            if kind == "region":
                return tr("settings-map-region-done", mb=mb)
            return tr("settings-map-done", count=st.get("done") or 0, mb=mb, skipped=st.get("skipped") or 0)
        return tr({map_precache.OFFLINE: "settings-map-offline-now", map_precache.BUDGET: "settings-map-budget-full",
                   map_precache.CANCELLED: "settings-map-stopped"}.get(phase, "settings-map-job-failed"),
                  reason=st.get("reason", ""))

    def _precache_options(self):
        owner = self.parentWidget()
        opts = owner.precache_options() if owner is not None and hasattr(owner, "precache_options") else {
            "layer": self.config.get("map_layer", "standard"), "dpr": float(self.devicePixelRatioF() or 1.0),
            "hidpi_raster": False, "policy": self.config.get("battery_policy", power.DEFAULT_POLICY)}
        opts["budget_mb"] = self._map_budget_mb()
        return opts

    def toggle_map_precache(self):
        mj = map_precache.jobs()
        if mj.running():
            mj.cancel()
        else:
            mj.start_precache(manual=True, **self._precache_options())
        self.refresh_map_offline()

    def _on_map_region_changed(self, *_):
        """A new region: the detail drops to the deepest level whose pack stays within
        tile_store.MAX_REGION_TILES (a continent at street level is a planet download)."""
        rid = self.combo_map_region.currentData()
        if rid in tile_store.REGIONS:
            best = tile_store.region_max_detail(tile_store.REGIONS[rid], tuple(z for z, _k in MAP_DETAILS))
            want = self.combo_map_detail.currentData()
            if best is not None and (want is None or want > best):
                self.combo_map_detail.blockSignals(True)
                self.combo_map_detail.setCurrentIndex(self.combo_map_detail.findData(best))
                self.combo_map_detail.blockSignals(False)
        self._update_region_estimate()

    def _region_fits(self):
        rid = self.combo_map_region.currentData()
        zmax = self.combo_map_detail.currentData()
        return rid in tile_store.REGIONS and zmax is not None and tile_store.region_fits(tile_store.REGIONS[rid], zmax)

    def _update_region_estimate(self, *_):
        rid = self.combo_map_region.currentData()
        zmax = self.combo_map_detail.currentData() or tile_store.REGION_ZMAX
        tiles, size = tile_store.estimate_region(tile_store.REGIONS[rid], zmax) if rid in tile_store.REGIONS else (0, 0)
        if rid in tile_store.REGIONS and not self._region_fits():
            self.lbl_map_region.setText(tr("settings-map-region-too-large", tiles=fmt_number(tiles, 0)))
        else:
            self.lbl_map_region.setText(tr("settings-map-region-estimate", tiles=fmt_number(tiles, 0),
                                           mb=fmt_number(size / 2 ** 20, 0)))
        if hasattr(self, "list_map_packs"):           # not during construction
            self.refresh_map_offline()

    def download_region_pack(self):
        mj = map_precache.jobs()
        if mj.running():
            mj.cancel()
        elif self._region_fits():
            rid = self.combo_map_region.currentData()
            mj.start_region(rid, zmax=self.combo_map_detail.currentData(), title=self.combo_map_region.currentText(),
                            budget_mb=self._map_budget_mb())
        self.refresh_map_offline()

    def import_map_pack(self, path=None):
        if not path or isinstance(path, bool):          # clicked(bool) is not a path
            path, _ = QFileDialog.getOpenFileName(self, tr("settings-map-import"), "", tr("settings-map-import-filter"))
        if not path:
            return False
        ok = map_precache.jobs().start_import(path, budget_mb=self._map_budget_mb())
        self.refresh_map_offline()
        return ok

    def remove_map_pack(self):
        item = self.list_map_packs.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not name:
            return False
        ok, _ = self._confirm(tr("settings-map-remove-q"), tr("settings-map-remove-body", name=item.text()),
                              action_text=tr("settings-map-remove"))
        if not ok:
            return False
        try:
            map_precache.jobs().remove_pack(name)
        except OSError as e:
            self._notify(str(e), ok=False)
            return False
        self._map_packs_shown = None
        self.refresh_map_offline()
        return True

    def clear_map_cache(self):
        u = map_precache.usage()
        ok, _ = self._confirm(tr("settings-map-clear-q"),
                              tr("settings-map-clear-body", mb=fmt_number(u["bytes"] / 2 ** 20, 0)),
                              action_text=tr("settings-map-clear"))
        if not ok:
            return False
        started = map_precache.jobs().start_clear(budget_mb=self._map_budget_mb())
        self.refresh_map_offline()
        return started

    # ── náhled dopadu ──
    def _set_sort(self, value):
        i = self.combo_default_sort.findData(value)
        self.combo_default_sort.setCurrentIndex(i if i >= 0 else 0)

    def set_language_choice(self, value):
        i = self.combo_language.findData(value or i18n.SYSTEM)
        self.combo_language.setCurrentIndex(i if i >= 0 else 0)

    def selected_language(self):
        return self.combo_language.currentData() or i18n.SYSTEM

    def _update_threshold_impact(self, v):
        self.lbl_threshold.setText(fmt_number(v / 100, 2))
        unconfirmed, confirmed = self._stats.get("unconfirmed"), self._stats.get("confirmed")
        if v == self._saved_threshold:
            self.lbl_threshold_impact.setText("")
            self.lbl_threshold_impact.setVisible(False)     # bez prázdné mezery pod posuvníkem
            return
        self.lbl_threshold_impact.setVisible(True)
        who = _czech_people(unconfirmed) if unconfirmed is not None else tr("settings-threshold-who-unknown")
        split = v > self._saved_threshold
        if confirmed:
            msg = "settings-threshold-impact-split-kept" if split else "settings-threshold-impact-merge-kept"
            self.lbl_threshold_impact.setText(tr(msg, who=who, confirmed=confirmed))
        else:
            msg = "settings-threshold-impact-split" if split else "settings-threshold-impact-merge"
            self.lbl_threshold_impact.setText(tr(msg, who=who))

    def _update_lru_impact(self, v):
        self.lbl_lru.setText(f"{v}")
        self.lbl_lru_impact.setText(tr("settings-ram-hint", value=fmt_number(v * 0.5, 0)))

    def _update_preview_impact(self, v):
        v = int(round(v / PREVIEW_MB_STEP) * PREVIEW_MB_STEP)
        self.lbl_preview_mb.setText(tr("unit-size", value=fmt_number(v / 1024, 2), unit="GB"))
        used = preview_cache.cache_size() / (1024 * 1024)
        text = tr("settings-preview-used", used=fmt_number(used, 0), photos=fmt_number(used / 0.45, 0)) if used else \
            tr("settings-preview-empty")
        if used > v:
            text += " " + tr("settings-preview-freed", mb=fmt_number(used - v, 0))
        self.lbl_preview_impact.setText(text)

    def _update_proxy_impact(self, v):
        v = int(round(v / PROXY_MB_STEP) * PROXY_MB_STEP)
        self.lbl_proxy_mb.setText(tr("unit-size", value=fmt_number(v / 1024, 1), unit="GB"))
        used = video_proxy.cache_size() / (1024 * 1024)
        text = (tr("settings-proxy-used", used=fmt_number(used, 0)) if used else tr("settings-proxy-empty")) + " "
        text += tr("settings-proxy-note")
        if used > v:
            text += " " + tr("settings-proxy-freed", mb=fmt_number(used - v, 0))
        self.lbl_proxy_impact.setText(text)

    def clear_proxies(self):
        """Kopie se samy vytvoří znovu – bez potvrzení, dopad ukazuje štítek u posuvníku."""
        video_proxy.clear_cache()
        self._update_proxy_impact(self.slider_proxy_mb.value())

    def _update_factory_label(self):
        import library_tools
        changed = library_tools.changed_settings(self.config)
        what = tr("settings-changed-count", count=len(changed)) if changed else tr("settings-all-default")
        self.lbl_factory.setText(what + " " + tr("settings-reset-scope"))

    def refresh_trash_label(self):
        plan = self._stats.get("trash_plan")
        if plan is None:
            self.lbl_trash.setText(tr("settings-trash-count-unknown"))
            self.btn_empty_trash.setEnabled(False)
            return
        n_rec, n_rem, n_miss = len(plan["recycle"]), len(plan["removable"]), len(plan["missing"])
        total = n_rec + n_rem + n_miss
        parts = [tr("settings-trash-count", items="<b>" + _czech_items(total) + "</b>")]
        if n_rem:
            parts.append(tr("settings-trash-removable", count=n_rem))
        if n_miss:
            parts.append(tr("settings-trash-missing", count=n_miss))
        self.lbl_trash.setText(" · ".join(parts))
        self.btn_empty_trash.setEnabled(total > 0)

    def refresh_cache_size_label(self):
        thumbs_mb = get_folder_size_mb(THUMB_DIR)
        faces_mb = get_folder_size_mb(FACE_DIR)
        previews_mb = preview_cache.cache_size() / (1024 * 1024)
        self.lbl_cache_size.setText(
            tr("settings-cache-usage", thumbs_mb=fmt_number(thumbs_mb, 1), previews_mb=fmt_number(previews_mb, 1), faces_mb=fmt_number(faces_mb, 1))
        )
        if hasattr(self, "slider_preview_mb"):
            self._update_preview_impact(self.slider_preview_mb.value())

    # ── akce ──
    def clean_cache(self):
        ok, _ = self._confirm(tr("settings-delete-thumbs-q"), tr("settings-delete-thumbs-body"),
                              action_text=tr("settings-delete-thumbs"))
        if not ok:
            return
        ok, message = clear_thumbnail_cache()
        self.refresh_cache_size_label()
        if ok:
            self.cache_cleaned = True
        self._notify(message + (tr("settings-thumbs-regen") if ok else ""), ok)

    def _notify(self, message, ok=True):
        """Výsledek akce: v listu toast v hlavním okně, jinak (samostatné okno) zpráva."""
        if self._sheet is not None:
            from ui_toast import show_toast
            show_toast(self.parentWidget(), message.strip(), "success" if ok else "error")
        elif ok:
            QMessageBox.information(self, tr("common-done"), message)
        else:
            QMessageBox.warning(self, tr("common-error"), message)

    def clear_previews(self):
        """Mezipaměť se sama obnoví – bez potvrzení, dopad ukazuje štítek u tlačítka."""
        preview_cache.clear_cache()
        self.previews_cleared = True
        self.refresh_cache_size_label()

    def empty_trash(self):
        import library_tools
        plan = self._stats.get("trash_plan")
        if not plan or not any(plan.values()):
            return
        n_rec, n_rem, n_miss = len(plan["recycle"]), len(plan["removable"]), len(plan["missing"])
        impact = [tr("settings-trash-impact-recycle", count=n_rec)] if n_rec else []
        if n_miss:
            impact.append(tr("settings-trash-impact-missing", count=n_miss))
        options = []
        if n_rem:
            impact.append(tr("settings-trash-impact-removable", count=n_rem))
            options.append(("permanent", tr("settings-trash-delete-permanently-option", count=n_rem), False))
        ok, values = self._confirm(tr("settings-empty-trash-q"),
                                   tr("settings-empty-trash-body"), impact, options, tr("settings-empty-trash-confirm"))
        if not ok:
            return
        permanent = bool(values.get("permanent"))
        if permanent:
            ok2, _ = self._confirm(tr("settings-delete-permanently-q"),
                                   tr("settings-delete-permanently-body", count=n_rem), (), (),
                                   tr("settings-delete-permanently"))
            permanent = ok2
        stats = library_tools.empty_trash(plan, delete_removable_permanently=permanent)
        self.trash_emptied = True
        self._stats = self._read_library_stats()
        self.refresh_trash_label()
        lines = [tr("settings-trash-recycled", recycled=stats['recycled'])]
        if stats["deleted"]:
            lines.append(tr("settings-trash-deleted", deleted=stats['deleted']))
        if stats["skipped"]:
            lines.append(tr("settings-trash-skipped", skipped=stats['skipped']))
        if stats["failed"]:
            lines.append(tr("settings-trash-failed", failed=stats['failed']))
        self.lbl_trash.setText(self.lbl_trash.text() + "<br>" + " · ".join(lines))

    def request_recluster(self):
        unconfirmed = self._stats.get("unconfirmed")
        impact = [tr("settings-regroup-who", who=_czech_people(unconfirmed)) if unconfirmed is not None
                  else tr("settings-regroup-unconfirmed"),
                  tr("settings-regroup-keep")]
        ok, _ = self._confirm(tr("settings-regroup-q"), tr("settings-regroup-when"), impact,
                              (), tr("settings-regroup-confirm"), destructive=False)
        if ok:
            self.recluster_requested = True
            self.btn_recluster.setText(tr("settings-regroup-scheduled"))

    def factory_reset(self):
        import library_tools
        changed = library_tools.changed_settings(self.config)
        impact = [tr("settings-reset-count", count=len(changed)) if changed
                  else tr("settings-reset-already-default"),
                  tr("settings-reset-keeps")]
        options = [("keep_folders", tr("settings-reset-keep-watched"), True),
                   ("clear_thumbnails", tr("settings-reset-delete-thumbs"), False),
                   ("clear_previews", tr("settings-reset-delete-previews"), False)]
        ok, values = self._confirm(tr("settings-reset-q"), tr("settings-reset-body"),
                                   impact, options, tr("settings-reset-confirm"))
        if not ok:
            return
        self.config = library_tools.factory_reset(values.get("keep_folders", True),
                                                  values.get("clear_thumbnails", False),
                                                  values.get("clear_previews", False))
        self.factory_reset_done = True
        self.cache_cleaned = self.cache_cleaned or values.get("clear_thumbnails", False)
        self.previews_cleared = self.previews_cleared or values.get("clear_previews", False)
        self.accept()

    def _reload_music(self):
        self.list_music.clear()
        for t in ambient_music.user_tracks():
            item = QListWidgetItem(t["title"])
            item.setData(Qt.ItemDataRole.UserRole, t["file"])
            self.list_music.addItem(item)

    def add_music(self, files=None):
        """Add Music… (the main window also shows a toast and refreshes the Journey player)."""
        owner = self.parent()
        if files is None or isinstance(files, bool):  # clicked(bool) must never pose as a file list
            files, _ = QFileDialog.getOpenFileNames(self, tr("music-add-pick"), "", tr("music-file-filter"))
        if not files:
            return []
        if owner is not None and hasattr(owner, "add_music"):
            added = owner.add_music(files, parent=self)
        else:
            added = ambient_music.add_user_tracks(files)
        self._reload_music()
        return added

    def remove_music(self):
        for item in self.list_music.selectedItems():
            try:
                ambient_music.remove_user_track(item.data(Qt.ItemDataRole.UserRole))
            except OSError as e:
                print(f"[Music] {e}")
        self._reload_music()

    def open_music_folder(self):
        os.makedirs(ambient_music.USER_MUSIC_DIR, exist_ok=True)
        os.startfile(ambient_music.USER_MUSIC_DIR)       # i18n-ok: Windows shell call, no text

    def add_library_folder(self):
        folder = QFileDialog.getExistingDirectory(self, tr("settings-pick-folder"))
        if folder:
            existing = [self.list_folders.item(i).text() for i in range(self.list_folders.count())]
            if folder not in existing:
                self._add_folder_item(folder, False)

    def _add_folder_item(self, folder, watched):
        item = QListWidgetItem(folder)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if watched else Qt.CheckState.Unchecked)
        self.list_folders.addItem(item)
        return item

    def remove_library_folder(self):
        current_row = self.list_folders.currentRow()
        if current_row >= 0:
            self.list_folders.takeItem(current_row)

    def open_newsletter_signup(self):
        url = self.config.get("newsletter_url", "https://omniphotovault.com/updates")
        webbrowser.open(url)

    def reset_to_defaults(self):
        """Jen formulář (bez uložení) – proto bez potvrzovacího listu."""
        self.list_folders.clear()
        for f in DEFAULT_CONFIG.get("library_folders", []):
            self._add_folder_item(f, False)
        self.chk_autoscan.setChecked(DEFAULT_CONFIG.get("auto_scan_on_startup", True))
        self.chk_reduce_motion.setChecked(DEFAULT_CONFIG.get("reduce_motion", False))
        self.chk_xmp.setChecked(DEFAULT_CONFIG.get("xmp_sidecars", True))
        self.chk_borrow.setChecked(DEFAULT_CONFIG.get("borrow_location", True))
        self.chk_ocr.setChecked(DEFAULT_CONFIG.get("ocr_text", True))
        self.chk_precise.setChecked(DEFAULT_CONFIG.get("search_precise", False))
        self.chk_color_display.setChecked(DEFAULT_CONFIG.get("color_output") == "panel")
        self.chk_memories.setChecked(DEFAULT_CONFIG.get("show_memories", True))
        self.chk_adaptive.setChecked(DEFAULT_CONFIG.get("adaptive_accent", True))
        self.chk_hidden_entry.setChecked(DEFAULT_CONFIG.get("show_hidden_folder", True))
        self._set_sort(DEFAULT_CONFIG.get("default_sort_order", SORT_CHOICES[0][0]))
        self.set_language_choice(DEFAULT_CONFIG.get("language", i18n.SYSTEM))
        self.slider_threshold.setValue(int(round(float(DEFAULT_CONFIG.get("face_similarity_threshold", 0.62)) * 100)))
        self.slider_lru.setValue(int(DEFAULT_CONFIG.get("thumbnail_lru_capacity", 1200)))
        self.slider_preview_mb.setValue(int(DEFAULT_CONFIG.get("preview_cache_mb", preview_cache.DEFAULT_BUDGET_MB)))
        self.slider_proxy_mb.setValue(int(DEFAULT_CONFIG.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB)))
        self.slider_map_mb.setValue(int(DEFAULT_CONFIG.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
        self.chk_map_prefetch.setChecked(DEFAULT_CONFIG.get("map_prefetch", True))
        self.perf_switch.set_current(DEFAULT_CONFIG.get("performance_tier", "auto"))
        self.battery_switch.set_current(DEFAULT_CONFIG.get("battery_policy", power.DEFAULT_POLICY))
        self.theme_switch.set_current(DEFAULT_CONFIG.get("theme", "dark"))
        self.accent_picker.set_value(DEFAULT_CONFIG.get("accent", tokens.DEFAULT_ACCENT))

    def choose_cache_dir(self):
        folder = QFileDialog.getExistingDirectory(self, tr("settings-pick-cache-folder"))
        if not folder:
            return
        folder = os.path.normpath(folder)
        if os.path.basename(folder).lower() not in ("kairos", "cache", "kairos cache"):
            folder = os.path.join(folder, "Kairos")      # nikdy ne přímo do kořene disku
        if os.path.normcase(folder) == os.path.normcase(paths.CACHE_ROOT):
            self._pending_cache_dir = None
        else:
            self._pending_cache_dir = folder
        self._update_cache_dir_label()

    def _update_cache_dir_label(self):
        text = paths.CACHE_ROOT
        if self._pending_cache_dir:
            text += tr("settings-cache-pending", pending_cache_dir=self._pending_cache_dir)
        self.lbl_cache_dir.setText(text)

    def selected_theme(self):
        return self.theme_switch.current_key() or theme.DEFAULT_THEME

    def save_and_close(self):
        items = [self.list_folders.item(i) for i in range(self.list_folders.count())]
        self.config["library_folders"] = [it.text() for it in items]
        self.config["watched_folders"] = [it.text() for it in items if it.checkState() == Qt.CheckState.Checked]
        self.config["auto_scan_on_startup"] = self.chk_autoscan.isChecked()
        self.config["reduce_motion"] = self.chk_reduce_motion.isChecked()
        self.config["xmp_sidecars"] = self.chk_xmp.isChecked()
        self.config["borrow_location"] = self.chk_borrow.isChecked()
        self.config["ocr_text"] = self.chk_ocr.isChecked()
        self.config["search_precise"] = self.chk_precise.isChecked()
        self.config["color_output"] = "panel" if self.chk_color_display.isChecked() else "system"
        self.config["show_memories"] = self.chk_memories.isChecked()
        self.config["adaptive_accent"] = self.chk_adaptive.isChecked()
        self.config["show_hidden_folder"] = self.chk_hidden_entry.isChecked()
        self.config["default_sort_order"] = self.combo_default_sort.currentData() or SORT_CHOICES[0][0]
        self.config["accent"], self.config["accent_custom"] = self.accent_picker.value()
        self.config["face_similarity_threshold"] = round(self.slider_threshold.value() / 100.0, 2)
        self.config["thumbnail_lru_capacity"] = self.slider_lru.value()
        self.config["preview_cache_mb"] = int(round(self.slider_preview_mb.value() / PREVIEW_MB_STEP) * PREVIEW_MB_STEP)
        self.config["proxy_cache_mb"] = int(round(self.slider_proxy_mb.value() / PROXY_MB_STEP) * PROXY_MB_STEP)
        self.config["tile_store_mb"] = self._map_budget_mb()
        self.config["map_prefetch"] = self.chk_map_prefetch.isChecked()
        self.config["performance_tier"] = self.perf_switch.current_key() or "auto"
        self.config["battery_policy"] = self.battery_switch.current_key() or power.DEFAULT_POLICY
        if self._pending_cache_dir is not None:
            target = self._pending_cache_dir
            same_default = os.path.normcase(target) == os.path.normcase(paths.DEFAULT_CACHE_ROOT)
            self.config["cache_dir"] = "" if same_default else target
            self.config["cache_dir_pending_from"] = paths.CACHE_ROOT      # data_migration.relocate_cache
        self.config["theme"] = self.selected_theme()
        self.config["language"] = self.selected_language()
        if self.slider_threshold.value() != self._saved_threshold:
            self.recluster_requested = True
        save_config(self.config)
        self.accept()
