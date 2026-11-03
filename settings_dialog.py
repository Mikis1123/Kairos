"""Předvolby (Patch 11 / dávka E): Standardní a Pokročilé.

Standardní – sledované složky + chytrý import, vzhled (tmavý / světlý / podle systému),
             řazení a dlaždice, vysypání koše (do Koše Windows).
Pokročilé  – práh tváří a přeshlukování, paměť a mezipaměti (miniatury, náhledy 2048 px),
             obnovení továrního nastavení.

Každý zásah ukazuje dopad PŘÍMO pod ovládacím prvkem (kolik osob, kolik MB, kolik položek).
Potvrzovací list jen u nevratných nebo hromadných akcí; nic z tohoto okna nesmaže databázi,
osoby ani originály (koš jde do Koše Windows, tovární nastavení = jen config.json).
"""
from i18n import N_, fmt_number, tr
import os
import webbrowser

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QFileDialog, QSlider, QMessageBox,
    QFrame, QTabWidget, QWidget, QListWidget, QListWidgetItem,
    QCheckBox, QComboBox, QScrollArea
)
from PyQt6.QtCore import QEventLoop, Qt
from ui_controls import AccentPicker, InfoDot, KButton, SegmentedControl, ToggleSwitch, guard_wheel
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

THEME_CHOICES = (("dark", N_("theme-dark")), ("light", N_("theme-light")), ("system", N_("theme-system")))
PREVIEW_MB_RANGE = (512, 8192)
PREVIEW_MB_STEP = 256
PROXY_MB_RANGE = (1024, 32768)
PROXY_MB_STEP = 512
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
            self._sheet = PanelSheet(parent, self)
            self._sheet.request_close = self.reject       # klik vedle karty = Zrušit
        self.setWindowTitle(tr("settings-window-title", APP_NAME=brand.APP_NAME))
        if self._sheet is None:
            self.resize(660, 560)
        # Styl z tokenů (tokens.build_qss "settings"): motiv, zvýraznění, fajfka v zaškrtávátku
        tokens.apply_stylesheet(self, "settings")

        self.config = load_config()
        # výsledky pro hlavní okno (co po zavření obnovit)
        self.cache_cleaned = False          # miniatury smazány -> obnova na pozadí
        self.previews_cleared = False
        self.trash_emptied = False
        self.recluster_requested = False
        self.factory_reset_done = False
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

    # ── rozvržení ──
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self.create_standard_tab()), tr("settings-tab-general"))
        self.tabs.addTab(self._scroll(self.create_advanced_tab()), tr("settings-tab-advanced"))
        self.tabs.addTab(self.create_about_tab(), tr("settings-tab-about"))
        # Kairos: záložky přepíná segmentový přepínač nahoře uprostřed (QTabWidget drží stránky)
        self.tabs.tabBar().hide()
        self.tab_switch = SegmentedControl([(self.tabs.tabText(i), i) for i in range(self.tabs.count())], current=0)
        self.tab_switch.changed.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.tab_switch.set_current)
        if self._sheet is not None:
            title = QLabel(tr("nav-settings"))
            title.setObjectName("sheetHeader")
            main_layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignHCenter)
        main_layout.addWidget(self.tab_switch, alignment=Qt.AlignmentFlag.AlignHCenter)
        main_layout.addWidget(self.tabs)

        bottom_bar = QHBoxLayout()
        btn_reset = KButton(tr("settings-reset-form"))
        btn_reset.setObjectName("btnReset")
        btn_reset.setToolTip(tr("settings-reset-form-tooltip"))
        btn_reset.clicked.connect(self.reset_to_defaults)
        bottom_bar.addWidget(btn_reset)
        bottom_bar.addStretch()
        btn_cancel = KButton(tr("action-cancel"))
        btn_cancel.clicked.connect(self.reject)
        bottom_bar.addWidget(btn_cancel)
        btn_save = KButton(tr("settings-save"))
        btn_save.setObjectName("btnSave")
        btn_save.clicked.connect(self.save_and_close)
        bottom_bar.addWidget(btn_save)
        main_layout.addLayout(bottom_bar)

    def _scroll(self, page):
        page.setObjectName("page")
        area = QScrollArea()
        area.setFrameShape(QFrame.Shape.NoFrame)        # bez tvrdé linky kolem stránky
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    # ── skupiny a řádky (macOS: popisek vlevo, ovládání vpravo, oddělovač odsazený) ──
    @staticmethod
    def _group(layout, title, info=None):
        """Skupina s nadpisem; info = vysvětlení v (i) vedle nadpisu (Founder audit: žádné
        odstavce textu pod skupinou)."""
        cap = QLabel(title.upper())
        cap.setObjectName("groupTitle")
        if info:
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.setSpacing(2)
            head.addWidget(cap)
            dot = InfoDot(info)
            dot.setObjectName("groupInfo")
            head.addWidget(dot, 0, Qt.AlignmentFlag.AlignBottom)
            head.addStretch(1)
            layout.addLayout(head)
        else:
            layout.addWidget(cap)
        frame = QFrame()
        frame.setObjectName("group")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box = QVBoxLayout(frame)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        layout.addWidget(frame)
        return frame

    @staticmethod
    def _row(group, label=None, control=None, value=None, stacked=None, info=None):
        """Řádek skupiny. label + control = vedle sebe; stacked = widget přes celou šířku pod
        popiskem (posuvník, seznam). value = QLabel s hodnotou vpravo od popisku.
        info = vysvětlení v (i) hned za popiskem."""
        box = group.layout()
        if box.count():
            sep = QFrame()
            sep.setObjectName("rowSep")
            sep.setFixedHeight(1)
            box.addWidget(sep)
        row = QWidget()
        row.setObjectName("row")
        lay = QVBoxLayout(row)
        lay.setContentsMargins(14, 9, 14, 9)
        lay.setSpacing(6)
        if label is not None or control is not None or value is not None:
            line = QHBoxLayout()
            line.setSpacing(12)
            if isinstance(label, str):
                label = QLabel(label)
            if label is not None:
                label.setWordWrap(not info)      # s (i) na jednom řádku (jinak se zalomí do úzka)
                if info:
                    line.addWidget(label, 0)
                    line.addWidget(InfoDot(info), 0, Qt.AlignmentFlag.AlignVCenter)
                    line.addStretch(1)
                else:
                    line.addWidget(label, 1)
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

    @staticmethod
    def _tab_layout(tab):
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(20, 14, 20, 18)
        layout.setSpacing(6)
        return layout

    def create_standard_tab(self):
        tab = QWidget()
        layout = self._tab_layout(tab)

        # Knihovna + chytrý import
        g = self._group(layout, tr("settings-library"), info=tr("settings-folders-hint"))
        self.list_folders = QListWidget()
        self.list_folders.setMinimumHeight(96)
        self.list_folders.setToolTip(tr("settings-watched-tooltip"))
        watched = {os.path.normcase(f) for f in self.config.get("watched_folders", [])}
        for f in self.config.get("library_folders", []):
            self._add_folder_item(f, os.path.normcase(f) in watched)
        btn_box = QHBoxLayout()
        btn_add = KButton(tr("settings-add-folder"), compact=True)
        btn_add.clicked.connect(self.add_library_folder)
        btn_remove = KButton(tr("settings-remove-selected"), compact=True)
        btn_remove.clicked.connect(self.remove_library_folder)
        btn_box.addWidget(btn_add)
        btn_box.addWidget(btn_remove)
        btn_box.addStretch()
        self._row(g, tr("settings-library-folders"), stacked=self.list_folders)
        self._row(g, stacked=btn_box)
        self.chk_autoscan = ToggleSwitch(tr("settings-scan-on-start"))
        self.chk_autoscan.setChecked(self.config.get("auto_scan_on_startup", True))
        self._row(g, stacked=self.chk_autoscan)

        # Vzhled
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

        # Gate 2 D: hudba pro Cestu - vlastní skladby v %APPDATA%/Kairos/music
        g = self._group(layout, tr("settings-music"), info=tr("settings-music-hint"))
        self.list_music = QListWidget()
        self.list_music.setObjectName("list_music")
        self.list_music.setMinimumHeight(72)
        self._reload_music()
        music_box = QHBoxLayout()
        self.btn_add_music = KButton(tr("music-add"), compact=True)
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

        # Řazení
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
        self.chk_ocr = ToggleSwitch(tr("settings-ocr"))
        self.chk_ocr.setChecked(bool(self.config.get("ocr_text", True)))
        self._row(g, stacked=self.chk_ocr)

        # Koš
        g = self._group(layout, tr("nav-trash"), info=tr("settings-trash-hint"))
        self.lbl_trash = QLabel()
        self.lbl_trash.setWordWrap(True)
        self.btn_empty_trash = KButton(tr("settings-empty-trash"), compact=True)
        self.btn_empty_trash.setObjectName("btnReset")
        self.btn_empty_trash.clicked.connect(self.empty_trash)
        self._row(g, self.lbl_trash, self.btn_empty_trash)
        self.refresh_trash_label()

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

        # Nápověda (Polish P2) – otevře stránku Tipy a průvodce v hlavním okně
        g = self._group(layout, tr("guide-where-help"), info=tr("settings-guide-hint"))
        self.btn_guide = KButton(tr("action-open-ellipsis"), compact=True, icon="sparkle")
        self.btn_guide.clicked.connect(self.open_guide)
        self._row(g, tr("settings-guide-row"), self.btn_guide)
        layout.addStretch()
        return tab

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

    def create_advanced_tab(self):
        tab = QWidget()
        layout = self._tab_layout(tab)

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

        g = self._group(layout, tr("settings-memory-caches"), info=tr("settings-caches-hint"))
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

        self.lbl_cache_size = QLabel()
        self.lbl_cache_size.setWordWrap(True)
        cache_row = QHBoxLayout()
        btn_clean = KButton(tr("settings-delete-thumbs"), compact=True)
        btn_clean.setObjectName("btnCleanCache")
        btn_clean.clicked.connect(self.clean_cache)
        self.btn_clear_previews = KButton(tr("settings-delete-previews"), compact=True)
        self.btn_clear_previews.setObjectName("btnCleanCache")
        self.btn_clear_previews.clicked.connect(self.clear_previews)
        cache_row.addWidget(btn_clean)
        cache_row.addWidget(self.btn_clear_previews)
        cache_row.addStretch()
        self._row(g, stacked=self.lbl_cache_size)
        self._row(g, stacked=cache_row)
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

        # Fáze 1: adaptivní škálování a proxy videí (rozhodnutí C: limit úložiště v Předvolbách)
        g = self._group(layout, tr("settings-perf-video"))
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
        self.btn_clear_proxies = KButton(tr("settings-delete-video-copies"), compact=True)
        self.btn_clear_proxies.setObjectName("btnCleanCache")
        self.btn_clear_proxies.clicked.connect(self.clear_proxies)
        self._row(g, tr("settings-video-copies-row"), self.btn_clear_proxies)
        self._update_proxy_impact(self.slider_proxy_mb.value())

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

        # Sprint 4.0: přesné hledání (SigLIP 2) – volitelné, výchozí lehký CLIP
        g = self._group(layout, tr("settings-precise-search"))
        import siglip_engine
        self.chk_precise = ToggleSwitch(tr("settings-precise-search-toggle"))
        self.chk_precise.setChecked(bool(self.config.get("search_precise", False)))
        state = (tr("settings-model-downloaded") if siglip_engine.installed()
                 else tr("settings-model-download-size", value=fmt_number(siglip_engine.DOWNLOAD_MB / 1000, 1)))
        self.chk_precise.set_info(tr("settings-precise-search-hint") + state)
        self._row(g, stacked=self.chk_precise)

        g = self._group(layout, tr("settings-recovery"))
        self.lbl_factory = _hint("")
        self.btn_factory_reset = KButton(tr("settings-factory-reset"), compact=True)
        self.btn_factory_reset.setObjectName("btnReset")
        self.btn_factory_reset.clicked.connect(self.factory_reset)
        self._row(g, self.lbl_factory, self.btn_factory_reset)
        self._update_factory_label()
        layout.addStretch()
        return tab

    def create_about_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)
        layout.addWidget(brand.Wordmark(34), alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(QLabel(tr("settings-version")))
        layout.addWidget(_hint(tr("settings-about-body")))
        layout.addSpacing(10)
        btn_newsletter = KButton(tr("settings-newsletter"))
        btn_newsletter.clicked.connect(self.open_newsletter_signup)
        layout.addWidget(btn_newsletter, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return tab

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
