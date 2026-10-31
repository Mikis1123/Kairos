"""Předvolby (Patch 11 / dávka E): Standardní a Pokročilé.

Standardní – sledované složky + chytrý import, vzhled (tmavý / světlý / podle systému),
             řazení a dlaždice, vysypání koše (do Koše Windows).
Pokročilé  – práh tváří a přeshlukování, paměť a mezipaměti (miniatury, náhledy 2048 px),
             obnovení továrního nastavení.

Každý zásah ukazuje dopad PŘÍMO pod ovládacím prvkem (kolik osob, kolik MB, kolik položek).
Potvrzovací list jen u nevratných nebo hromadných akcí; nic z tohoto okna nesmaže databázi,
osoby ani originály (koš jde do Koše Windows, tovární nastavení = jen config.json).
"""
import os
import webbrowser

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QFileDialog, QSlider, QMessageBox,
    QFrame, QTabWidget, QWidget, QListWidget, QListWidgetItem,
    QCheckBox, QComboBox, QScrollArea
)
from PyQt6.QtCore import Qt
from ui_controls import AccentPicker, KButton, SegmentedControl, ToggleSwitch
from config import load_config, save_config, DEFAULT_CONFIG
from paths import THUMB_DIR, FACE_DIR
from cache_tools import clear_thumbnail_cache
import perf_profile
import preview_cache
import theme
import video_proxy
import tokens
import brand

THEME_CHOICES = (("dark", "Tmavý"), ("light", "Světlý"), ("system", "Podle systému"))
PREVIEW_MB_RANGE = (512, 8192)
PREVIEW_MB_STEP = 256
PROXY_MB_RANGE = (1024, 32768)
PROXY_MB_STEP = 512
PERF_CHOICES = [("Automaticky", "auto"), ("Plný", "high"), ("Vyvážený", "standard"), ("Úsporný", "baseline")]


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
    return "1 položka" if n == 1 else (f"{n} položky" if 2 <= n <= 4 else f"{n} položek")


def _czech_people(n, adjective):
    """(3, 'nepotvrzen') -> '3 nepotvrzené osoby'."""
    if n == 1:
        return f"1 {adjective}á osoba"
    if 2 <= n <= 4:
        return f"{n} {adjective}é osoby"
    return f"{n} {adjective}ých osob"


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

    def __init__(self, title, body, impact_lines=(), options=(), action_text="Pokračovat",
                 destructive=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
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
        cancel = KButton("Zrušit")
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Předvolby – {brand.APP_NAME}")
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

    def _confirm(self, title, body, impact_lines=(), options=(), action_text="Pokračovat", destructive=True):
        """Potvrzovací list; vrací (potvrzeno, {volba: bool}). V testech se nahrazuje."""
        sheet = ConfirmSheet(title, body, impact_lines, options, action_text, destructive, self)
        ok = sheet.exec() == QDialog.DialogCode.Accepted
        return ok, sheet.values()

    # ── rozvržení ──
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self.create_standard_tab()), "Standardní")
        self.tabs.addTab(self._scroll(self.create_advanced_tab()), "Pokročilé")
        self.tabs.addTab(self.create_about_tab(), "O aplikaci")
        # Kairos: záložky přepíná segmentový přepínač nahoře uprostřed (QTabWidget drží stránky)
        self.tabs.tabBar().hide()
        self.tab_switch = SegmentedControl([(self.tabs.tabText(i), i) for i in range(self.tabs.count())], current=0)
        self.tab_switch.changed.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.tab_switch.set_current)
        main_layout.addWidget(self.tab_switch, alignment=Qt.AlignmentFlag.AlignHCenter)
        main_layout.addWidget(self.tabs)

        bottom_bar = QHBoxLayout()
        btn_reset = KButton("↺ Výchozí hodnoty ve formuláři")
        btn_reset.setObjectName("btnReset")
        btn_reset.setToolTip("Vrátí ovládací prvky na výchozí hodnoty; uloží se až tlačítkem Uložit.")
        btn_reset.clicked.connect(self.reset_to_defaults)
        bottom_bar.addWidget(btn_reset)
        bottom_bar.addStretch()
        btn_cancel = KButton("Zrušit")
        btn_cancel.clicked.connect(self.reject)
        bottom_bar.addWidget(btn_cancel)
        btn_save = KButton("Uložit změny")
        btn_save.setObjectName("btnSave")
        btn_save.clicked.connect(self.save_and_close)
        bottom_bar.addWidget(btn_save)
        main_layout.addLayout(bottom_bar)

    def _scroll(self, page):
        page.setObjectName("page")
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(page)
        return area

    # ── skupiny a řádky (macOS: popisek vlevo, ovládání vpravo, oddělovač odsazený) ──
    @staticmethod
    def _group(layout, title):
        cap = QLabel(title.upper())
        cap.setObjectName("groupTitle")
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
    def _row(group, label=None, control=None, value=None, stacked=None):
        """Řádek skupiny. label + control = vedle sebe; stacked = widget přes celou šířku pod
        popiskem (posuvník, seznam). value = QLabel s hodnotou vpravo od popisku."""
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
                label.setWordWrap(True)
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
        g = self._group(layout, "Knihovna")
        self.list_folders = QListWidget()
        self.list_folders.setMinimumHeight(96)
        self.list_folders.setToolTip("Zaškrtnutá složka se sleduje: nové fotky v ní se naimportují samy")
        watched = {os.path.normcase(f) for f in self.config.get("watched_folders", [])}
        for f in self.config.get("library_folders", []):
            self._add_folder_item(f, os.path.normcase(f) in watched)
        btn_box = QHBoxLayout()
        btn_add = KButton("Přidat složku…", compact=True)
        btn_add.clicked.connect(self.add_library_folder)
        btn_remove = KButton("Odebrat vybranou", compact=True)
        btn_remove.clicked.connect(self.remove_library_folder)
        btn_box.addWidget(btn_add)
        btn_box.addWidget(btn_remove)
        btn_box.addStretch()
        self._row(g, "Sledované složky", stacked=self.list_folders)
        self._row(g, stacked=btn_box)
        self.chk_autoscan = ToggleSwitch("Při spuštění hledat nové fotky")
        self.chk_autoscan.setChecked(self.config.get("auto_scan_on_startup", True))
        self._row(g, stacked=self.chk_autoscan)
        layout.addWidget(_hint("Odebráním složky se fotky z knihovny nesmažou. Chytrý import nové soubory "
                               "jen spočítá a nabídne – bez vašeho souhlasu nic neimportuje. Výjimkou jsou "
                               "zaškrtnuté složky: ty Kairos sleduje a nové fotky z nich přidá sám. Složky "
                               "i fotky můžete také přetáhnout přímo do okna."))

        # Vzhled
        g = self._group(layout, "Vzhled")
        current = self.config.get("theme", theme.DEFAULT_THEME)
        self.theme_switch = SegmentedControl([("Systém", "system"), ("Tmavý", "dark"), ("Světlý", "light")],
                                             current=current if current in dict(THEME_CHOICES) else "dark")
        self._row(g, "Motiv", self.theme_switch)
        self.accent_picker = AccentPicker(self.config.get("accent", tokens.DEFAULT_ACCENT),
                                          self.config.get("accent_custom", ""))
        self._row(g, "Barva zvýraznění", self.accent_picker)
        self.chk_adaptive = ToggleSwitch("Přizpůsobit obsahu")
        self.chk_adaptive.setToolTip("Záře karet, hlavička události a trasa Cesty se jemně přizpůsobí barvám "
                                     "fotek. Tlačítka a ovládací prvky zůstávají ve zvolené barvě.")
        self.chk_adaptive.setChecked(bool(self.config.get("adaptive_accent", True)))
        self._row(g, stacked=self.chk_adaptive)
        self.chk_reduce_motion = ToggleSwitch("Omezit animace")
        self.chk_reduce_motion.setChecked(bool(self.config.get("reduce_motion", False)))
        self._row(g, stacked=self.chk_reduce_motion)
        self.chk_memories = ToggleSwitch("Vzpomínky „Před 3 lety“ nad Fotografiemi")
        self.chk_memories.setChecked(bool(self.config.get("show_memories", True)))
        self._row(g, stacked=self.chk_memories)
        layout.addWidget(_hint("Prohlížeč a prezentace zůstávají vždy tmavé – fotky v nich vyniknou. "
                               "Vlastní barvu Kairos doladí, aby byla čitelná ve tmavém i světlém motivu. "
                               "Když jsou ve Windows vypnuté Animační efekty, animace se omezí samy."))

        # Řazení
        g = self._group(layout, "Knihovna fotek")
        self.combo_default_sort = QComboBox()
        self.combo_default_sort.addItems([
            "Datum pořízení (Nejnovější)",
            "Datum pořízení (Nejstarší)",
            "Datum přidání do galerie",
            "Podle výletů"
        ])
        self.combo_default_sort.setCurrentText(self.config.get("default_sort_order", "Datum pořízení (Nejnovější)"))
        self._row(g, "Výchozí řazení", self.combo_default_sort)
        self.chk_xmp = ToggleSwitch("Ukládat oblíbené, jména a polohu do souborů XMP vedle fotek")
        self.chk_xmp.setChecked(bool(self.config.get("xmp_sidecars", True)))
        self._row(g, stacked=self.chk_xmp)
        self.chk_borrow = ToggleSwitch("Doplnit polohu fotek bez GPS z telefonu ve stejné chvíli")
        self.chk_borrow.setChecked(bool(self.config.get("borrow_location", True)))
        self._row(g, stacked=self.chk_borrow)
        self.chk_ocr = ToggleSwitch("Číst text na fotkách (cedule, dokumenty) pro hledání – offline")
        self.chk_ocr.setChecked(bool(self.config.get("ocr_text", True)))
        self._row(g, stacked=self.chk_ocr)
        layout.addWidget(_hint("Velikost dlaždic měníte přímo v mřížce: Ctrl + kolečko myši nebo sevření prstů."))

        # Koš
        g = self._group(layout, "Koš")
        self.lbl_trash = QLabel()
        self.lbl_trash.setWordWrap(True)
        self.btn_empty_trash = KButton("Vysypat koš…", compact=True)
        self.btn_empty_trash.setObjectName("btnReset")
        self.btn_empty_trash.clicked.connect(self.empty_trash)
        self._row(g, self.lbl_trash, self.btn_empty_trash)
        layout.addWidget(_hint("Soubory se přesunou do Koše Windows – dají se odtamtud obnovit."))
        self.refresh_trash_label()

        # Stage 3: složka Skryté
        g = self._group(layout, "Skryté 🔒")
        self.chk_hidden_entry = ToggleSwitch("Zobrazit Skryté v postranním panelu")
        self.chk_hidden_entry.setChecked(bool(self.config.get("show_hidden_folder", True)))
        self._row(g, stacked=self.chk_hidden_entry)
        self.btn_open_hidden = KButton("Otevřít…", compact=True, icon="lock")
        self.btn_open_hidden.clicked.connect(self.open_hidden)
        self._row(g, "Fotky mimo knihovnu, hledání, tváře, mapu a sestřihy", self.btn_open_hidden)
        self.btn_hidden_pass = KButton("Změnit heslo…", compact=True)
        self.btn_hidden_pass.clicked.connect(self.change_hidden_passcode)
        self._row(g, "Heslo ke Skrytým (záloha k Windows Hello)", self.btn_hidden_pass)
        layout.addWidget(_hint("Skryté se zamknou po 2 minutách nečinnosti, při minimalizaci okna, zamčení "
                               "počítače a při odchodu ze složky. Dokud jsou odemčené, okno se neukáže ve "
                               "snímcích obrazovky, sdílení ani v náhledech Alt+Tab."))

        # Nápověda (Polish P2) – otevře stránku Tipy a průvodce v hlavním okně
        g = self._group(layout, "Nápověda")
        self.btn_guide = KButton("Otevřít…", compact=True, icon="sparkle")
        self.btn_guide.clicked.connect(self.open_guide)
        self._row(g, "Tipy a průvodce – zkratky, mapa, tváře a hledání", self.btn_guide)
        layout.addWidget(_hint("Kdykoli také klávesou F1. Neuložené změny v Předvolbách se zahodí."))
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
            show_toast(self.parent(), "Heslo ke Skrytým je nastavené.", "success")

    def create_advanced_tab(self):
        tab = QWidget()
        layout = self._tab_layout(tab)

        g = self._group(layout, "Rozpoznávání tváří")
        self._saved_threshold = int(round(float(self.config.get("face_similarity_threshold", 0.62)) * 100))
        self.lbl_threshold = QLabel()
        self.slider_threshold = QSlider(Qt.Orientation.Horizontal)
        self.slider_threshold.setRange(30, 90)   # 0.30 – 0.90
        self.slider_threshold.setValue(self._saved_threshold)
        self.slider_threshold.valueChanged.connect(self._update_threshold_impact)
        row = self._row(g, "Práh podobnosti tváří", value=self.lbl_threshold, stacked=self.slider_threshold)
        self.lbl_threshold_impact = _impact()
        row.layout().addWidget(self.lbl_threshold_impact)
        row.layout().addWidget(_hint("Nižší = víc obličejů v jedné osobě (víc záměn). Vyšší = přísnější shoda "
                                     "(víc menších skupin). Doporučeno 0,55 – 0,70."))
        self.btn_recluster = KButton("Přeskupit osoby znovu…", compact=True)
        self.btn_recluster.clicked.connect(self.request_recluster)
        self._row(g, "Seskupit nepotvrzené tváře znovu", self.btn_recluster)
        self._update_threshold_impact(self.slider_threshold.value())

        g = self._group(layout, "Paměť a mezipaměti")
        lru_val = int(self.config.get("thumbnail_lru_capacity", 1200))
        self.lbl_lru = QLabel()
        self.slider_lru = QSlider(Qt.Orientation.Horizontal)
        self.slider_lru.setRange(64, 4000)
        self.slider_lru.setValue(lru_val)
        self.slider_lru.valueChanged.connect(self._update_lru_impact)
        row = self._row(g, "Miniatury v paměti (RAM)", value=self.lbl_lru, stacked=self.slider_lru)
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
        row = self._row(g, "Mezipaměť náhledů 2048 px", value=self.lbl_preview_mb, stacked=self.slider_preview_mb)
        self.lbl_preview_impact = _hint("")
        row.layout().addWidget(self.lbl_preview_impact)

        self.lbl_cache_size = QLabel()
        self.lbl_cache_size.setWordWrap(True)
        cache_row = QHBoxLayout()
        btn_clean = KButton("Smazat miniatury", compact=True)
        btn_clean.setObjectName("btnCleanCache")
        btn_clean.clicked.connect(self.clean_cache)
        self.btn_clear_previews = KButton("Smazat náhledy 2048 px", compact=True)
        self.btn_clear_previews.setObjectName("btnCleanCache")
        self.btn_clear_previews.clicked.connect(self.clear_previews)
        cache_row.addWidget(btn_clean)
        cache_row.addWidget(self.btn_clear_previews)
        cache_row.addStretch()
        self._row(g, stacked=self.lbl_cache_size)
        self._row(g, stacked=cache_row)
        layout.addWidget(_hint("Obě mezipaměti se na pozadí samy vytvoří znovu. Ořezy tváří, osoby, "
                               "databáze a vaše fotografie se tím nemění."))
        self.refresh_cache_size_label()

        # Fáze 1: adaptivní škálování a proxy videí (rozhodnutí C: limit úložiště v Předvolbách)
        g = self._group(layout, "Výkon a videa")
        tier = self.config.get("performance_tier", "auto")
        self.perf_switch = SegmentedControl(PERF_CHOICES, current=tier if tier in dict(PERF_CHOICES) else "auto")
        self._row(g, "Výkon", self.perf_switch)
        detected = perf_profile.classify(os.cpu_count() or 4, perf_profile.total_ram_gb())
        self._row(g, stacked=_hint(
            f"Automaticky podle počítače – tento je „{dict(PERF_CHOICES).get(detected, detected)}“. Plný: 4K HDR "
            "videa přímo, nejostřejší 3D mapa. Úsporný: videa přes 1080p kopie, lehčí mapa a méně práce na pozadí. "
            "Projeví se po restartu."))

        self.lbl_proxy_mb = QLabel()
        self.slider_proxy_mb = QSlider(Qt.Orientation.Horizontal)
        self.slider_proxy_mb.setRange(*PROXY_MB_RANGE)
        self.slider_proxy_mb.setSingleStep(PROXY_MB_STEP)
        self.slider_proxy_mb.setPageStep(PROXY_MB_STEP * 4)
        self.slider_proxy_mb.setValue(int(self.config.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB)))
        self.slider_proxy_mb.valueChanged.connect(self._update_proxy_impact)
        row = self._row(g, "Kopie videí 1080p (4K a HDR)", value=self.lbl_proxy_mb, stacked=self.slider_proxy_mb)
        self.lbl_proxy_impact = _hint("")
        row.layout().addWidget(self.lbl_proxy_impact)
        self.btn_clear_proxies = KButton("Smazat kopie videí", compact=True)
        self.btn_clear_proxies.setObjectName("btnCleanCache")
        self.btn_clear_proxies.clicked.connect(self.clear_proxies)
        self._row(g, "Kopie se na pozadí vytvoří znovu, když je potřeba", self.btn_clear_proxies)
        self._update_proxy_impact(self.slider_proxy_mb.value())

        # Stage 2: správa barev – výstupní profil fotek (Display P3 z iPhonu -> displej)
        g = self._group(layout, "Barvy")
        import color_manage
        self.chk_color_display = ToggleSwitch("Barvy fotek podle displeje")
        self.chk_color_display.setChecked(self.config.get("color_output", "auto") != color_manage.MODE_SRGB)
        self._row(g, stacked=self.chk_color_display)
        tgt = color_manage.target()
        self._row(g, stacked=_hint(
            f"Teď: {tgt['name']} – {tgt['reason']}. Vypnuto = vždy sRGB (pro displeje, které barvy "
            "převádějí samy). Po změně se náhledy na pozadí přegenerují."))

        # Sprint 4.0: přesné hledání (SigLIP 2) – volitelné, výchozí lehký CLIP
        g = self._group(layout, "Přesné hledání")
        import siglip_engine
        self.chk_precise = ToggleSwitch("Přesné hledání (SigLIP 2)")
        self.chk_precise.setChecked(bool(self.config.get("search_precise", False)))
        self._row(g, stacked=self.chk_precise)
        state = ("Model je stažený." if siglip_engine.installed()
                 else f"Po zapnutí se jednou stáhne model (~{siglip_engine.DOWNLOAD_MB / 1000:.1f} GB).")
        self._row(g, stacked=_hint(
            "Přesnější hledání popisem: na zkušební sadě je správná fotka mezi prvními 20 v 94 % případů "
            "místo 82 % (výsledky aplikace 92 % místo 89 %). Knihovna se na pozadí přeindexuje – do té doby hledá dosavadní "
            "model. Potřebuje asi o 1,6 GB paměti víc; doporučeno od 16 GB RAM. " + state))

        g = self._group(layout, "Obnovení")
        self.lbl_factory = _hint("")
        self.btn_factory_reset = KButton("Obnovit tovární nastavení…", compact=True)
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
        layout.addWidget(QLabel("Verze 1.0.0 (Release Candidate)"))
        layout.addWidget(_hint("Plně offline desktopová fotogalerie s lokálním rozpoznáváním tváří "
                               "a nativní podporou Apple Live Photos."))
        layout.addSpacing(10)
        btn_newsletter = KButton("Odebírat aktualizace a novinky (web)")
        btn_newsletter.clicked.connect(self.open_newsletter_signup)
        layout.addWidget(btn_newsletter, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        return tab

    # ── náhled dopadu ──
    def _update_threshold_impact(self, v):
        self.lbl_threshold.setText(f"{v / 100:.2f}".replace(".", ","))
        unconfirmed, confirmed = self._stats.get("unconfirmed"), self._stats.get("confirmed")
        if v == self._saved_threshold:
            self.lbl_threshold_impact.setText("")
            self.lbl_threshold_impact.setVisible(False)     # bez prázdné mezery pod posuvníkem
            return
        self.lbl_threshold_impact.setVisible(True)
        who = _czech_people(unconfirmed, "nepotvrzen") if unconfirmed is not None else "nepotvrzené osoby"
        keep = (f"; potvrzené zůstanou beze změny ({confirmed})" if confirmed
                else "; potvrzené osoby zůstanou")
        direction = "rozdělí na víc skupin" if v > self._saved_threshold else "sloučí do méně skupin"
        self.lbl_threshold_impact.setText(f"Po uložení se znovu seskupí: {who} – spíš se {direction}{keep}.")

    def _update_lru_impact(self, v):
        self.lbl_lru.setText(f"{v}")
        self.lbl_lru_impact.setText(f"Až ~{v * 0.5:.0f} MB RAM při dlaždicích 360 px. Vyšší = plynulejší "
                                    f"posouvání, nižší = úspornější. Doporučeno 800 – 2000.")

    def _update_preview_impact(self, v):
        v = int(round(v / PREVIEW_MB_STEP) * PREVIEW_MB_STEP)
        self.lbl_preview_mb.setText(f"{v / 1024:.2f} GB".replace(".", ","))
        used = preview_cache.cache_size() / (1024 * 1024)
        text = f"Nyní obsazeno {used:.0f} MB (~{used / 0.45:.0f} fotek okamžitě ostrých)." if used else \
            "Zatím prázdná – plní se na pozadí."
        if used > v:
            text += f" Po uložení se uvolní ~{used - v:.0f} MB nejdéle nepoužitých náhledů."
        self.lbl_preview_impact.setText(text)

    def _update_proxy_impact(self, v):
        v = int(round(v / PROXY_MB_STEP) * PROXY_MB_STEP)
        self.lbl_proxy_mb.setText(f"{v / 1024:.1f} GB".replace(".", ","))
        used = video_proxy.cache_size() / (1024 * 1024)
        text = (f"Nyní obsazeno {used:.0f} MB. " if used else "Zatím prázdné – plní se na pozadí, nikdy během "
                "přehrávání. ")
        text += "Kopie slouží k plynulému posouvání časové osy, náhledům a Cestě; originál zůstává beze změny."
        if used > v:
            text += f" Po uložení se uvolní ~{used - v:.0f} MB nejdéle nepoužitých kopií."
        self.lbl_proxy_impact.setText(text)

    def clear_proxies(self):
        """Kopie se samy vytvoří znovu – bez potvrzení, dopad ukazuje štítek u posuvníku."""
        video_proxy.clear_cache()
        self._update_proxy_impact(self.slider_proxy_mb.value())

    def _update_factory_label(self):
        import library_tools
        changed = library_tools.changed_settings(self.config)
        what = f"Změněno {len(changed)} nastavení." if changed else "Všechna nastavení jsou výchozí."
        self.lbl_factory.setText(f"{what} Obnovení vrátí jen předvolby (config.json); databáze, osoby, "
                                 f"obličeje a originály zůstanou.")

    def refresh_trash_label(self):
        plan = self._stats.get("trash_plan")
        if plan is None:
            self.lbl_trash.setText("V koši: —")
            self.btn_empty_trash.setEnabled(False)
            return
        n_rec, n_rem, n_miss = len(plan["recycle"]), len(plan["removable"]), len(plan["missing"])
        total = n_rec + n_rem + n_miss
        parts = [f"V koši: <b>{_czech_items(total)}</b>"]
        if n_rem:
            parts.append(f"{n_rem} na vyjímatelném / síťovém disku (výchozí: přeskočí se)")
        if n_miss:
            parts.append(f"{n_miss} bez souboru (jen se zapomene)")
        self.lbl_trash.setText(" · ".join(parts))
        self.btn_empty_trash.setEnabled(total > 0)

    def refresh_cache_size_label(self):
        thumbs_mb = get_folder_size_mb(THUMB_DIR)
        faces_mb = get_folder_size_mb(FACE_DIR)
        previews_mb = preview_cache.cache_size() / (1024 * 1024)
        self.lbl_cache_size.setText(
            f"Miniatury (lze smazat): <b>{thumbs_mb:.1f} MB</b><br>"
            f"Náhledy 2048 px (lze smazat): <b>{previews_mb:.1f} MB</b><br>"
            f"Ořezy tváří (trvalá data, nemažou se): <b>{faces_mb:.1f} MB</b>"
        )
        if hasattr(self, "slider_preview_mb"):
            self._update_preview_impact(self.slider_preview_mb.value())

    # ── akce ──
    def clean_cache(self):
        reply = QMessageBox.question(
            self,
            "Smazat miniatury?",
            "Smažou se pouze dočasné miniatury fotografií.\n"
            "Ořezy tváří, rozpoznané osoby, databáze a originály zůstanou nedotčeny.\n"
            "Miniatury se na pozadí vytvoří znovu.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        ok, message = clear_thumbnail_cache()
        self.refresh_cache_size_label()
        if ok:
            self.cache_cleaned = True
            QMessageBox.information(self, "Hotovo", message + "\nNáhledy se budou dogenerovat na pozadí.")
        else:
            QMessageBox.warning(self, "Chyba", message)

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
        impact = [f"{_czech_items(n_rec)} se přesune do Koše Windows (lze obnovit)"] if n_rec else []
        if n_miss:
            impact.append(f"{_czech_items(n_miss)} už nemá soubor – jen se odstraní z knihovny")
        options = []
        if n_rem:
            impact.append(f"{_czech_items(n_rem)} leží na vyjímatelném nebo síťovém disku – tam Koš Windows "
                          f"není, proto se ve výchozím stavu přeskočí a zůstanou v koši aplikace")
            options.append(("permanent", f"Smazat i těchto {n_rem} TRVALE (nelze vrátit)", False))
        ok, values = self._confirm("Vysypat koš?",
                                   "Položky zmizí z knihovny i ze složek na disku. Rozpoznané osoby "
                                   "ostatních fotek zůstanou.", impact, options, "Vysypat koš")
        if not ok:
            return
        permanent = bool(values.get("permanent"))
        if permanent:
            ok2, _ = self._confirm("Trvale smazat soubory?",
                                   f"{_czech_items(n_rem)} na vyjímatelném / síťovém disku se smaže bez "
                                   f"možnosti obnovení.", (), (), "Smazat trvale")
            permanent = ok2
        stats = library_tools.empty_trash(plan, delete_removable_permanently=permanent)
        self.trash_emptied = True
        self._stats = self._read_library_stats()
        self.refresh_trash_label()
        lines = [f"Do Koše Windows: {stats['recycled']}"]
        if stats["deleted"]:
            lines.append(f"Trvale smazáno: {stats['deleted']}")
        if stats["skipped"]:
            lines.append(f"Přeskočeno (vyjímatelný / síťový disk): {stats['skipped']}")
        if stats["failed"]:
            lines.append(f"Nepodařilo se: {stats['failed']} (zůstávají v koši)")
        self.lbl_trash.setText(self.lbl_trash.text() + "<br>" + " · ".join(lines))

    def request_recluster(self):
        unconfirmed = self._stats.get("unconfirmed")
        impact = [f"Znovu se seskupí: {_czech_people(unconfirmed, 'nepotvrzen')}" if unconfirmed is not None
                  else "Nepotvrzené osoby se seskupí znovu",
                  "Pojmenované a potvrzené osoby zůstanou (slouží jako kotvy)"]
        ok, _ = self._confirm("Přeskupit osoby?", "Proběhne na pozadí po zavření předvoleb.", impact,
                              (), "Přeskupit", destructive=False)
        if ok:
            self.recluster_requested = True
            self.btn_recluster.setText("Přeskupení naplánováno ✓")

    def factory_reset(self):
        import library_tools
        changed = library_tools.changed_settings(self.config)
        impact = [f"Vrátí se {len(changed)} nastavení na výchozí hodnoty" if changed
                  else "Nastavení už jsou výchozí",
                  "Databáze, osoby, obličeje ani originální fotky se NEsmažou"]
        options = [("keep_folders", "Ponechat sledované složky", True),
                   ("clear_thumbnails", "Smazat i miniatury (vytvoří se znovu)", False),
                   ("clear_previews", "Smazat i náhledy 2048 px (vytvoří se znovu)", False)]
        ok, values = self._confirm("Obnovit tovární nastavení?", "Předvolby se vrátí do stavu po instalaci.",
                                   impact, options, "Obnovit")
        if not ok:
            return
        self.config = library_tools.factory_reset(values.get("keep_folders", True),
                                                  values.get("clear_thumbnails", False),
                                                  values.get("clear_previews", False))
        self.factory_reset_done = True
        self.cache_cleaned = self.cache_cleaned or values.get("clear_thumbnails", False)
        self.previews_cleared = self.previews_cleared or values.get("clear_previews", False)
        self.accept()

    def add_library_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Vyberte složku s fotkami")
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
        self.chk_color_display.setChecked(DEFAULT_CONFIG.get("color_output", "auto") != "srgb")
        self.chk_memories.setChecked(DEFAULT_CONFIG.get("show_memories", True))
        self.chk_adaptive.setChecked(DEFAULT_CONFIG.get("adaptive_accent", True))
        self.chk_hidden_entry.setChecked(DEFAULT_CONFIG.get("show_hidden_folder", True))
        self.combo_default_sort.setCurrentText(DEFAULT_CONFIG.get("default_sort_order", "Datum pořízení (Nejnovější)"))
        self.slider_threshold.setValue(int(round(float(DEFAULT_CONFIG.get("face_similarity_threshold", 0.62)) * 100)))
        self.slider_lru.setValue(int(DEFAULT_CONFIG.get("thumbnail_lru_capacity", 1200)))
        self.slider_preview_mb.setValue(int(DEFAULT_CONFIG.get("preview_cache_mb", preview_cache.DEFAULT_BUDGET_MB)))
        self.slider_proxy_mb.setValue(int(DEFAULT_CONFIG.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB)))
        self.perf_switch.set_current(DEFAULT_CONFIG.get("performance_tier", "auto"))
        self.theme_switch.set_current(DEFAULT_CONFIG.get("theme", "dark"))
        self.accent_picker.set_value(DEFAULT_CONFIG.get("accent", tokens.DEFAULT_ACCENT))

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
        self.config["color_output"] = "auto" if self.chk_color_display.isChecked() else "srgb"
        self.config["show_memories"] = self.chk_memories.isChecked()
        self.config["adaptive_accent"] = self.chk_adaptive.isChecked()
        self.config["show_hidden_folder"] = self.chk_hidden_entry.isChecked()
        self.config["default_sort_order"] = self.combo_default_sort.currentText()
        self.config["accent"], self.config["accent_custom"] = self.accent_picker.value()
        self.config["face_similarity_threshold"] = round(self.slider_threshold.value() / 100.0, 2)
        self.config["thumbnail_lru_capacity"] = self.slider_lru.value()
        self.config["preview_cache_mb"] = int(round(self.slider_preview_mb.value() / PREVIEW_MB_STEP) * PREVIEW_MB_STEP)
        self.config["proxy_cache_mb"] = int(round(self.slider_proxy_mb.value() / PROXY_MB_STEP) * PROXY_MB_STEP)
        self.config["performance_tier"] = self.perf_switch.current_key() or "auto"
        self.config["theme"] = self.selected_theme()
        if self.slider_threshold.value() != self._saved_threshold:
            self.recluster_requested = True
        save_config(self.config)
        self.accept()
