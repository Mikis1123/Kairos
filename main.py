"""Kairos – hlavní okno aplikace (Patch 3).

P3.1 Asynchronní karty osob | P3.2 Debounce vyhledávání | P3.3 Přesun obličeje
P3.4 Mrtvý kód odstraněn    | P3.5 Duplicity dialog     | P3.6 Borderline Review
P3.7 Nastavení prahu        | P3.8 Konfig. práh         | P3.9 Dynamická LRU cache
"""
import sys, os, time, json, math, subprocess, unicodedata
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

os.environ["QT_LOGGING_RULES"] = "qt.multimedia*=false"
os.environ["AV_LOG_FORCE_NOCOLOR"] = "1"
# Patch 11: rovnou D3D11VA (výstup už je D3D11) – bez zkoušení dalších HW dekodérů (~40 ms na start)
if sys.platform == "win32":
    os.environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "d3d11va")
# --log-level=3: nativní logy Chromia (GPU proces) jen fatální – varování WebGL za každý snímek
# mapy zaplavovala terminál; zprávy konzole stránek filtruje web_console.QuietWebPage
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
    "--ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy --log-level=3"
)

# Kairos dávka 5: při spuštění jako skript se nejdřív ukáže úvodní obrazovka – těžké importy
# níže proběhnou až pod ní (splash.boot pak modul načte znovu jako „main“ a zavolá main())
if __name__ == "__main__" and not os.environ.get("KAIROS_NO_SPLASH"):
    import splash
    sys.exit(splash.boot())

from paths import (
    BASE_DIR, RESOURCE_DIR, THUMB_DIR as CACHE_DIR, FFMPEG_PATH as DEFAULT_FFMPEG, resolve_face_path,
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QListWidget, QListWidgetItem, QLabel,
    QProgressBar, QStackedWidget, QComboBox, QButtonGroup, QLineEdit,
    QAbstractItemView, QInputDialog, QListView, QStyledItemDelegate,
    QStyleOptionViewItem, QStyle, QSlider, QFrame, QDialog, QScrollArea,
    QMenu, QMessageBox, QSizePolicy, QLayout,
)
from PyQt6.QtCore import (
    Qt, QSize, QObject, pyqtSlot, QTimer, QThread, pyqtSignal,
    QUrl, QRect, QRectF, QCoreApplication, QAbstractListModel,
    QModelIndex, QThreadPool, QRunnable, QPoint, QPointF, QEvent,
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QImage, QFont, QPainter, QColor, QKeyEvent,
    QPainterPath, QLinearGradient, QPen, QKeySequence, QShortcut, QCursor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import (
    QWebEngineSettings, QWebEngineProfile, QWebEngineUrlRequestInterceptor, QWebEnginePage,
)
from PyQt6.QtWebChannel import QWebChannel
from web_console import QuietWebPage

from database import (
    init_db, get_connection, save_image_embedding, get_all_embeddings, get_media_needing_embedding,
    get_media_pending_face_scan, save_faces_for_media, relocate_face_paths,
    visible_media_sql, query_geo_bounds, sync_geo_index, trash_media, restore_media,
    dismiss_duplicate_pair, undismiss_duplicate_pair, purge_orphan_test_rows,
    get_search_vectors, get_videos_needing_frames, save_video_frame_embeddings,
)
from scanner import MediaScannerThread, repair_missing_thumbnails
from face_cluster import (
    strict_recluster_faces, get_borderline_faces_for_person,
    assign_face_to_person, invalidate_centroid_cache,
    merge_people, undo_merge, confirm_person, suggest_person_merges, dismiss_person_merge,
    refresh_person_covers, count_quarantined_assigned,
)
from viewer import MediaViewerWidget
from ai_engine import (
    compute_image_embedding, search_by_text, migrate_legacy_embeddings, refresh_phashes,
)
from config import load_config, save_config
from settings_dialog import SettingsDialog
from ui_nav import NavigationController, DetailPage
from ui_toast import ToastManager, show_toast
from viewer_hud import SlideshowView, load_image_file, MediaWarmUp, ImagePipeline
from events_view import (EventCard, BentoLayout, fetch_event_cards, split_event_title, COVER_MAX_EDGE,
                         AmbientCycler, AmbientFramesJob)
import preview_cache
from preview_cache import PREVIEW_EDGE
import theme
import tokens
import motion
from ui_controls import (ActivityStatus, EmptyState, HintPill, KButton, MenuSelect, NavItem, NavSection,
                         SegmentedControl, SidebarNav, pin_overlay,
                         smooth_rect_path)
from ui_sheets import InWindowDialog
import icons
import brand
import drop_import
import folder_watch
import guide
import journey
import live_hover
import memories
import tint
import window_effects
import moments_view
from moments_view import MomentsView
import library_tools
import frame_meter
import governor
import perf_profile
import scrub_strip
import search_engine
import video_proxy
import video_frames
HOVER_PRELOAD_MS = 350      # setrvání kurzoru nad videem v mřížce před přednačtením
SMART_IMPORT_DELAY_MS = 4000 # kontrola nových fotek až po usazení startu
import face_engine

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass


# ── helpers ──────────────────────────────────────────────────────────────────

def strip_accents(text):
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text))
        if unicodedata.category(c) != "Mn"
    ).lower().strip()


def get_local_thumb_url(thumb_path):
    if thumb_path and os.path.exists(thumb_path):
        return QUrl.fromLocalFile(os.path.abspath(thumb_path)).toString()
    return ""


# ── Záznamy médií (společný kontrakt pro ApplePhotoModel i MediaViewerWidget) ─

# 0 id | 1 file_name | 2 file_path | 3 media_type | 4 paired_video_path
# 5 thumbnail_path | 6 date_taken | 7 is_favorite
MEDIA_COLUMNS = ("m.id, m.file_name, m.file_path, m.media_type, m.paired_video_path, "
                 "m.thumbnail_path, m.date_taken, m.is_favorite")

# Pohybové video Live Photo patří k fotce, v mřížce nesmí být jako samostatná položka
NOT_MOTION_CLIP = ("NOT EXISTS (SELECT 1 FROM media_items lp "
                   "WHERE lp.paired_video_path = m.file_path)")


def fetch_media_records(where="", params=(), order_by="m.date_taken DESC, m.id DESC",
                        trashed=False):
    """Vrátí záznamy ve tvaru MEDIA_COLUMNS bez pohybových videí Live Photos.
    trashed=False: knihovna (bez koše), trashed=True: jen položky v koši."""
    trash_cond = "COALESCE(m.is_trashed, 0) = 1" if trashed else "COALESCE(m.is_trashed, 0) = 0"
    sql = f"SELECT {MEDIA_COLUMNS} FROM media_items m WHERE {NOT_MOTION_CLIP} AND {trash_cond}"
    if where:
        sql += f" AND ({where})"
    sql += f" ORDER BY {order_by}"
    conn = get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def preview_build_order():
    """[(cesta, typ)] pro tvorbu náhledů: nejdřív to, co se zobrazí „ostře hned“ – kandidáti
    duplicit a obálky událostí – pak celá viditelná knihovna od nejnovějších. Bez opakování."""
    seen, order = set(), []

    def add(path, mtype):
        if path and path not in seen:
            seen.add(path)
            order.append((path, mtype))

    conn = get_connection()
    try:
        try:
            from ai_engine import get_duplicate_pairs
            for _, path_a, _, path_b, _ in get_duplicate_pairs(conn):
                add(path_a, "photo")
                add(path_b, "photo")
        except Exception as e:
            print(f"[Previews] duplicity: {e}")
        try:
            for card in fetch_event_cards(conn):
                cover = card.get("cover") or {}
                add(cover.get("path"), "photo")
        except Exception as e:
            print(f"[Previews] události: {e}")
        for path, mtype in conn.execute(f"SELECT m.file_path, m.media_type FROM media_items m "
                                        f"WHERE {visible_media_sql('m')} ORDER BY m.date_taken DESC, m.id DESC"):
            add(path, mtype)
    finally:
        conn.close()
    return order


def with_favorite(record, is_fav):
    """Kopie záznamu (MEDIA_COLUMNS) se změněným příznakem oblíbené."""
    return tuple(record[:7]) + (int(is_fav),) + tuple(record[8:])


def create_media_viewer(records, index=0, parent=None):
    """Prohlížeč nad seznamem záznamů (šipky = předchozí/další) jako běžný widget.

    Nevytváří samostatné okno: MainWindow ho vkládá do svého zásobníku stránek
    (NavigationController) a „‹ Zpět“ / Esc vrací do mřížky na stejné místo."""
    viewer = MediaViewerWidget(parent)
    viewer.web_profile_factory = map_web_profile   # minimapa sdílí diskovou cache dlaždic s mapou
    records = list(records)
    viewer.set_media_data(records, index)
    return viewer


def parse_search_results(results):
    """search_by_text vrací [(media_id, skóre), ...] od nejlepší shody -> [media_id, ...]."""
    ids = []
    for r in results or []:
        try:
            ids.append(int(r[0] if isinstance(r, (tuple, list)) else r))
        except (TypeError, ValueError, IndexError):
            continue
    return ids


def order_records_by_ids(records, ids):
    """Vybere záznamy s danými id a seřadí je podle pořadí v ids (relevance)."""
    rank = {}
    for i, media_id in enumerate(ids):
        rank.setdefault(media_id, i)
    hits = [r for r in records if int(r[0]) in rank]
    hits.sort(key=lambda r: rank[int(r[0])])
    return hits


# ── Mapa ──────────────────────────────────────────────────────────────────────

def build_map_markers(geo_rows):
    """Řádky z query_geo_bounds (id, lat, lon, file_name, city, date, thumb, type) ->
    data pro window.setMarkers() v map_vault.html. Neplatné souřadnice se vynechají."""
    markers = []
    for row in geo_rows:
        media_id, lat, lon, name, city, date, thumb = row[:7]
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)) \
                or not (-90 <= lat <= 90 and -180 <= lon <= 180) \
                or (lat == 0 and lon == 0):          # "Null Island" = chybějící GPS
            continue
        markers.append({
            "id": int(media_id), "lat": lat, "lon": lon,
            "name": name or "", "city": city or "", "date": (date or "")[:16],
            "thumb": get_local_thumb_url(thumb),
        })
    return markers


# Jediné povolené vzdálené zdroje webových stránek aplikace (mapa i minimapa prohlížeče):
# dlaždice podkladů z přepínače mapy (OpenFreeMap, Esri, OpenTopoMap). Dlaždice prozrazují
# jen zobrazenou oblast mapy, nikdy id ani metadata fotek.
ALLOWED_WEB_HOSTS = frozenset({
    "server.arcgisonline.com",       # Základní (Esri World Street Map) + Satelitní + záloha minimapy (Dark Gray)
    "tile.opentopomap.org",          # Turistická: vrstevnice a stínování terénu
    "tile.waymarkedtrails.org",      # Turistická: značené pěší a cyklistické trasy
    "tiles.openfreemap.org",         # vektorové podklady mapy i minimapy (styl, dlaždice, písma, ikony)
    "elevation-tiles-prod.s3.amazonaws.com",   # Batch 6: 3D terén (AWS Terrain Tiles, jen tento bucket)
})
MAP_HTTP_CACHE_MB = 512              # dlaždice a styl na disku – mapa se po restartu nestahuje znovu
MAP_LAYERS = ("standard", "satellite", "outdoor")
DEFAULT_MAP_LAYER = "standard"
LEGACY_MAP_LAYERS = {"dark": "standard", "topo": "outdoor"}   # hodnoty z config.json starší verze
VIEWER_PREWARM_DELAY_MS = 2500   # prohlížeč + video se připraví v klidu po startu
LOCAL_WEB_SCHEMES = frozenset({"file", "qrc", "data", "blob", "about"})


def is_allowed_web_url(url):
    """True pro místní obsah a povolené mapové servery; vše ostatní (CDN, Google, ...) se blokuje,
    aby poloha fotek neodcházela na nepovolené servery."""
    u = url if isinstance(url, QUrl) else QUrl(str(url))
    scheme = u.scheme().lower()
    if scheme in LOCAL_WEB_SCHEMES:
        return True
    return scheme == "https" and u.host().lower() in ALLOWED_WEB_HOSTS


class WebUrlInterceptor(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if not is_allowed_web_url(info.requestUrl()):
            info.block(True)


_map_profile = None


def release_map_profile():
    """Profil mapy zrušit AŽ po stránkách, které ho používají (QtWebEngine jinak hlásí
    „Release of profile requested but WebEnginePage still not deleted“ a při ukončení
    občas spadne). Volá se z shutdown_app po smazání hlavního okna."""
    global _map_profile
    profile, _map_profile = _map_profile, None
    if profile is not None:
        from PyQt6 import sip
        if not sip.isdeleted(profile):
            sip.delete(profile)


_parked_threads = []
PARKED_THREAD_WAIT_MS = 20000


def park_thread(thread):
    """Vlákno, které při zavírání okna nestihlo doběhnout: bez rodiče (smazání okna ho nesmí
    zrušit) a s odkazem tady, ať ho Python neuvolní. shutdown_app na něj počká."""
    try:
        thread.setParent(None)
    except RuntimeError:
        return
    if thread not in _parked_threads:
        _parked_threads.append(thread)


def join_parked_threads(timeout_ms=PARKED_THREAD_WAIT_MS):
    """Počká na odložená vlákna (sdílený limit). Vrací počet těch, která stále běží."""
    deadline = time.monotonic() + timeout_ms / 1000.0
    alive = []
    for thread in _parked_threads:
        left = max(0, int((deadline - time.monotonic()) * 1000))
        try:
            if thread.isRunning() and not thread.wait(left):
                alive.append(thread)
        except RuntimeError:
            continue
    _parked_threads[:] = alive
    return len(alive)


def shutdown_app(app, window):
    """Deterministické pořadí úklidu po app.exec() – bez něj ruší Python obaly Qt objektů při
    ukončení interpretu v náhodném pořadí a asi každé třetí zavření aplikace skončilo pádem
    v sip (0xC0000005, naměřeno). Nejdřív okno (stránka mapy, přehrávače), pak globální
    filtr motivu, profil mapy a generátor náhledových pásů."""
    from PyQt6 import sip
    if join_parked_threads():
        print("[Shutdown] vlákno na pozadí nedoběhlo včas")
    if window is not None and not sip.isdeleted(window):
        window.deleteLater()
    # deleteLater se po skončení app.exec() samo neprovede – výslovně jen DeferredDelete
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
    theme.uninstall()
    icons.clear_cache()                     # Kairos: QPixmap ikon nesmí přežít QApplication
    release_map_profile()
    try:
        import scrub_strip
        scrub_strip.manager().shutdown()
    except Exception as e:
        print(f"[Shutdown] {e}")
    try:
        import media_engine
        media_engine.shutdown_all()         # vlákna přehrávačů před zánikem QApplication
    except Exception as e:
        print(f"[Shutdown] {e}")
    if frame_meter.enabled_logging():       # KAIROS_FRAMESTATS=1: statistika snímků do perf_logs/
        path = frame_meter.write_log(os.path.join(BASE_DIR, "perf_logs"))
        if path:
            print(f"[FrameStats] {path}")
    app.processEvents()


def map_web_profile():
    """Trvalý profil mapy (jeden za běh): disková HTTP cache v .cache/web, žádné cookies
    navíc, stejný filtr povolených serverů jako výchozí profil. None, když ho nelze vytvořit."""
    global _map_profile
    if _map_profile is None:
        try:
            import paths
            root = paths.WEB_CACHE_DIR           # čteno za běhu (testy ho přesměrují)
            os.makedirs(root, exist_ok=True)
            # rodič = aplikace: profil musí přežít všechny své stránky
            profile = QWebEngineProfile("omni_map", QApplication.instance())
            profile.setPersistentStoragePath(os.path.join(root, "storage"))
            profile.setCachePath(os.path.join(root, "http"))
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            profile.setHttpCacheMaximumSize(MAP_HTTP_CACHE_MB * 1024 * 1024)
            profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            profile._interceptor = WebUrlInterceptor(profile)
            profile.setUrlRequestInterceptor(profile._interceptor)
            _map_profile = profile
        except Exception as e:
            print(f"[Map] trvalý profil nelze vytvořit, použije se výchozí: {e}")
            return None
    return _map_profile


# ── P3.9 LRU cache ───────────────────────────────────────────────────────────

def pixmap_bytes(pix):
    """Paměť pixmapy v RAM (šířka × výška × bajty na pixel)."""
    try:
        return max(1, pix.width() * pix.height() * max(1, pix.depth() // 8))
    except (AttributeError, RuntimeError):
        return 1


THUMB_RAM_BUDGET_MB = 384       # strop paměti miniatur v RAM (kromě počtu z Předvoleb)


class LRUCache:
    """LRU s limitem počtu a volitelně i bajtů (Sprint R: 1200 miniatur na displeji s DPR 2
    je přes 1,5 GB – samotný počet paměť nehlídal). sizer(value) -> bajty."""

    def __init__(self, capacity: int = 1200, max_bytes=None, sizer=None):
        self.cache = OrderedDict()
        self.capacity = max(64, int(capacity))
        self.max_bytes = int(max_bytes) if max_bytes else None
        self.sizer = sizer
        self._sizes = {}
        self.bytes = 0

    def get(self, key):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key, value):
        if key in self.cache:
            self.bytes -= self._sizes.pop(key, 0)
        self.cache[key] = value
        self.cache.move_to_end(key)
        if self.sizer is not None:
            size = int(self.sizer(value))
            self._sizes[key] = size
            self.bytes += size
        self._evict()

    def _evict(self):
        while len(self.cache) > self.capacity or (
                self.max_bytes is not None and self.bytes > self.max_bytes and len(self.cache) > 1):
            key, _ = self.cache.popitem(last=False)
            self.bytes -= self._sizes.pop(key, 0)

    def clear(self):
        self.cache.clear()
        self._sizes.clear()
        self.bytes = 0

    def resize(self, new_capacity: int):
        self.capacity = max(64, int(new_capacity))
        self._evict()


# ── Thumbnail worker ──────────────────────────────────────────────────────────

THUMB_LOADING = "loading"
THUMB_OK = "ok"
THUMB_MISSING_FILE = "missing"        # originál na disku není (smazaný / odpojený disk)
THUMB_UNAVAILABLE = "unavailable"     # soubor je, ale náhled se nepodařilo načíst


class ThumbnailWorkerSignals(QObject):
    loaded = pyqtSignal(int, QImage)
    failed = pyqtSignal(int, str)


class ThumbnailWorker(QRunnable):
    """Načte náhled mimo GUI vlákno. Pracuje jen s QImage (QPixmap mimo GUI vlákno
    není bezpečný); na QPixmap ho převede až model v hlavním vlákně."""

    def __init__(self, item_id: int, thumb_path: str, fallback_path: str = None):
        super().__init__()
        self.item_id = item_id
        self.thumb_path = thumb_path
        self.fallback_path = fallback_path
        self.signals = ThumbnailWorkerSignals()
        self.setAutoDelete(True)

    def run(self):
        img = QImage()
        for path in (self.thumb_path, self.fallback_path):
            if path and os.path.exists(path):
                img = self._load(path)
                if not img.isNull():
                    break
        if img.isNull():
            original = self.fallback_path
            state = (THUMB_UNAVAILABLE if original and os.path.exists(original)
                     else THUMB_MISSING_FILE)
            self.signals.failed.emit(self.item_id, state)
            return
        if img.width() > 360 or img.height() > 360:
            img = img.scaled(360, 360, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)
        self.signals.loaded.emit(self.item_id, img)

    @staticmethod
    def _load(path):
        ext = os.path.splitext(path)[1].lower()
        if ext in {".heic", ".dng"}:
            try:
                from PIL import Image, ImageOps
                with Image.open(path) as img:
                    img = ImageOps.exif_transpose(img)
                    img.thumbnail((360, 360))
                    img = img.convert("RGB")
                    w, h = img.size
                    return QImage(img.tobytes("raw", "RGB"), w, h, w * 3,
                                  QImage.Format.Format_RGB888).copy()
            except Exception:
                pass
        return QImage(path)


# ── ApplePhotoModel ───────────────────────────────────────────────────────────

class ApplePhotoModel(QAbstractListModel):
    ThumbRole = Qt.ItemDataRole.UserRole + 1
    MetaRole  = Qt.ItemDataRole.UserRole + 2
    StateRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, lru_capacity: int = 1200, parent=None):
        super().__init__(parent)
        self.records = []
        self.possible = set()
        self.id_to_row = {}
        self.ram_cache = LRUCache(lru_capacity, max_bytes=THUMB_RAM_BUDGET_MB * 1024 * 1024, sizer=pixmap_bytes)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)
        self.loading_ids = set()
        self.failed = {}          # item_id -> THUMB_MISSING_FILE | THUMB_UNAVAILABLE
        self._meta = {}           # řádek -> slovník MetaRole (delegát ho čte při každém snímku)

    def set_records(self, db_records, possible=()):
        """possible = id položek z pásma „možné shody“ hledání (kreslí se tlumeně)."""
        self.beginResetModel()
        self.records = list(db_records)
        self.possible = {int(i) for i in possible}
        self._meta = {}
        self.id_to_row = {int(r[0]): i for i, r in enumerate(self.records)}
        self.loading_ids.clear()
        self.failed.clear()       # soubor se mohl mezitím objevit (připojený disk, opravený náhled)
        self.endResetModel()

    def invalidate_cache(self):
        self.ram_cache.clear()
        self.loading_ids.clear()
        self.failed.clear()

    def thumb_state(self, item_id: int):
        if self.ram_cache.get(item_id) is not None:
            return THUMB_OK
        return self.failed.get(item_id, THUMB_LOADING)

    def set_favorite(self, item_id: int, is_fav: int):
        """Aktualizuje srdíčko jedné položky bez resetu modelu (zachová výběr i scroll)."""
        row = self.id_to_row.get(int(item_id))
        if row is None:
            return
        self.records[row] = with_favorite(self.records[row], is_fav)
        self._meta.pop(row, None)
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.MetaRole])

    def resize_cache(self, cap: int):
        self.ram_cache.resize(cap)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.records)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self.records):
            return None
        rec = self.records[row]
        item_id = int(rec[0])

        if role == self.ThumbRole:
            pix = self.ram_cache.get(item_id)
            if pix is not None:
                return pix
            if item_id not in self.loading_ids and item_id not in self.failed:
                self.loading_ids.add(item_id)
                w = ThumbnailWorker(item_id, rec[5], rec[2])
                w.signals.loaded.connect(self._on_thumb_loaded)
                w.signals.failed.connect(self._on_thumb_failed)
                self.thread_pool.start(w)
            return None

        if role == self.StateRole:
            return self.thumb_state(item_id)

        if role == self.MetaRole:
            meta = self._meta.get(row)
            if meta is None:
                name = rec[1] or (os.path.basename(rec[2]) if rec[2] else "")
                meta = self._meta[row] = {"id": item_id, "name": name, "type": rec[3],
                                          "date": rec[6], "is_fav": bool(rec[7]),
                                          "possible": item_id in self.possible}
            return meta
        return None

    def _emit_row_changed(self, item_id: int, roles):
        row = self.id_to_row.get(item_id)
        if row is not None and 0 <= row < len(self.records):
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, roles)

    def _on_thumb_loaded(self, item_id: int, image: QImage):
        self.ram_cache.put(item_id, QPixmap.fromImage(image))   # QPixmap až v GUI vlákně
        self.loading_ids.discard(item_id)
        self.failed.pop(item_id, None)
        self._emit_row_changed(item_id, [self.ThumbRole, self.StateRole])

    def _on_thumb_failed(self, item_id: int, state: str):
        self.loading_ids.discard(item_id)
        self.failed[item_id] = state      # už se nezkouší znovu při každém překreslení
        self._emit_row_changed(item_id, [self.ThumbRole, self.StateRole])


# ── ApplePhotoDelegate ────────────────────────────────────────────────────────

def paint_media_glyph(p, rect, is_video=False, crossed=False):
    """Jednoduchá vektorová ikona (fotka = hory se sluncem, video = filmový pás)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    stroke = max(1.5, rect.width() * 0.055)
    color = theme.qcolor("text_secondary")
    p.setPen(QPen(color, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                  Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    radius = rect.width() * 0.12
    p.drawRoundedRect(rect, radius, radius)
    w, h = rect.width(), rect.height()
    if is_video:
        tri = QPainterPath()
        cx, cy = rect.center().x(), rect.center().y()
        tri.moveTo(cx - w * 0.12, cy - h * 0.22)
        tri.lineTo(cx + w * 0.17, cy)
        tri.lineTo(cx - w * 0.12, cy + h * 0.22)
        tri.closeSubpath()
        p.setBrush(color)
        p.drawPath(tri)
    else:
        hills = QPainterPath()
        hills.moveTo(rect.left() + w * 0.12, rect.bottom() - h * 0.18)
        hills.lineTo(rect.left() + w * 0.40, rect.top() + h * 0.45)
        hills.lineTo(rect.left() + w * 0.58, rect.top() + h * 0.65)
        hills.lineTo(rect.left() + w * 0.70, rect.top() + h * 0.55)
        hills.lineTo(rect.right() - w * 0.12, rect.bottom() - h * 0.18)
        p.drawPath(hills)
        r = w * 0.07
        p.drawEllipse(QRectF(rect.right() - w * 0.30 - r, rect.top() + h * 0.22 - r, 2 * r, 2 * r))
    if crossed:
        p.setPen(QPen(QColor("#FF453A"), stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(rect.topLeft() + QPoint(-2, -2).toPointF(), rect.bottomRight() + QPoint(2, 2).toPointF())
    p.restore()


TILE_CACHE_BYTES = 64 * 1024 * 1024       # hotové dlaždice mřížky (~500 dlaždic 180 px)


class ApplePhotoDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell_size = 180
        self.corner_radius = 10.0
        # Hladké zmenšení + kreslení přes vyhlazenou ořezovou cestu (zaoblené rohy) stálo při
        # posouvání ~0,5 ms za dlaždici a snímek (25–45 ms na snímek). Klidová dlaždice se proto
        # vykreslí jednou do pixmapy (LRU podle bajtů) a při posouvání se jen zkopíruje.
        self._tiles = OrderedDict()           # (pixmap.cacheKey(), w, h, poloměr, překryv, dpr) -> QPixmap
        self._tiles_bytes = 0

    def _paint_photo(self, painter, frame, path, pixmap, zoom, meta, live_frame=None, index=None):
        """Fotka (vyplní rám) nebo zástupná plocha, snímek Live Photo a ztmavení pod odznaky –
        oříznuté na path."""
        painter.save()
        painter.setClipPath(path)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        # Ve fyzických pixelech: zvednutá (najetá) dlaždice se dřív škálovala na logické pixely
        # a na HiDPI displeji (DPR 2) se při najetí rozmazala na polovinu rozlišení
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        if pixmap and not pixmap.isNull():
            frame_r = frame.toAlignedRect()
            target = QSize(round(frame_r.width() * zoom * dpr), round(frame_r.height() * zoom * dpr))
            scaled = pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            scaled.setDevicePixelRatio(dpr)
            size = scaled.deviceIndependentSize()
            painter.drawPixmap(QPointF(frame.center().x() - size.width() / 2,
                                       frame.center().y() - size.height() / 2), scaled)
        else:
            self._paint_placeholder(painter, frame.toAlignedRect(), index)
        if live_frame is not None:
            pm, opacity = live_frame
            size = pm.deviceIndependentSize() * zoom          # stejné zvednutí jako statický snímek
            painter.setOpacity(opacity)
            painter.drawPixmap(QRectF(frame.center().x() - size.width() / 2, frame.center().y() - size.height() / 2,
                                      size.width(), size.height()), pm, QRectF(pm.rect()))
            painter.setOpacity(1.0)
        self._paint_badge_shade(painter, frame, meta)
        if meta.get("possible"):
            # pásmo „možné shody“: fotka pod závojem barvy pozadí – jisté shody vystoupí
            veil = theme.qcolor("bg")
            veil.setAlpha(120)
            painter.fillRect(frame, veil)
        painter.restore()

    @staticmethod
    def _paint_badge_shade(painter, frame, meta):
        if meta.get("type") in ("live_photo", "video") or meta.get("is_fav"):
            grad = QLinearGradient(0, frame.top(), 0, frame.top() + 40)
            grad.setColorAt(0.0, QColor(0, 0, 0, 90))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(QRectF(frame.left(), frame.top(), frame.width(), 40), grad)
            grad = QLinearGradient(0, frame.bottom() - 36, 0, frame.bottom())
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(1.0, QColor(0, 0, 0, 70 if meta.get("is_fav") else 0))
            painter.fillRect(QRectF(frame.left(), frame.bottom() - 36, frame.width(), 36), grad)

    @staticmethod
    def _paint_badges(painter, frame, meta):
        """Odznak LIVE / VIDEO vlevo nahoře a srdce oblíbené vlevo dole."""
        white = QColor("#FFFFFF")
        m_type = meta.get("type")
        if m_type in ("live_photo", "video") and frame.width() >= 64:
            label, icon = ("LIVE", "live") if m_type == "live_photo" else ("VIDEO", "play_fill")
            f = QFont(tokens.ui_family())
            f.setPixelSize(10)
            f.setWeight(QFont.Weight.DemiBold)
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 106)
            painter.setFont(f)
            tw = painter.fontMetrics().horizontalAdvance(label)
            badge = QRectF(frame.left() + 7, frame.top() + 7, tw + 26, 19)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(12, 12, 14, 130))
            painter.drawRoundedRect(badge, 6, 6)
            icons.paint(painter, icon, QRectF(badge.left() + 5, badge.top() + 3.5, 12, 12), white)
            painter.setPen(white)
            painter.drawText(badge.adjusted(20, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, label)
        if meta.get("is_fav") and frame.width() >= 48:
            icons.paint(painter, "heart_fill", QRectF(frame.left() + 7, frame.bottom() - 23, 16, 16), white)

    def _tile(self, pixmap, size, radius, meta, dpr):
        """Hotová zaoblená dlaždice velikosti size (logické px) z mezipaměti – i s odznaky,
        takže klidová dlaždice je při posouvání jediné drawPixmap (žádný text ani cesty)."""
        shade = (meta.get("type") in ("live_photo", "video"), bool(meta.get("is_fav")),
                 theme.current() if meta.get("possible") else None)     # závoj má barvu motivu
        key = (pixmap.cacheKey(), size.width(), size.height(), round(radius, 2), shade, dpr,
               meta.get("type"))
        tile = self._tiles.get(key)
        if tile is not None:
            self._tiles.move_to_end(key)
            return tile
        tile = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
        tile.setDevicePixelRatio(dpr)
        tile.fill(Qt.GlobalColor.transparent)
        p = QPainter(tile)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        frame = QRectF(0, 0, size.width(), size.height())
        self._paint_photo(p, frame, smooth_rect_path(frame, radius), pixmap, 1.0, meta)
        self._paint_badges(p, frame, meta)
        p.end()
        self._tiles[key] = tile
        self._tiles_bytes += tile.width() * tile.height() * 4
        while self._tiles_bytes > TILE_CACHE_BYTES and len(self._tiles) > 1:
            _, old = self._tiles.popitem(last=False)
            self._tiles_bytes -= old.width() * old.height() * 4
        return tile

    @property
    def placeholder_color(self):
        return theme.qcolor("placeholder")       # podle motivu (tmavý / světlý)

    def set_cell_size(self, size: int):
        self.cell_size = size

    def sizeHint(self, option, index):
        return QSize(self.cell_size, self.cell_size)

    def _paint_placeholder(self, painter, rect, index):
        """Místo černé díry: načítání = tichá šedá plocha, chyba = ikona + důvod + název."""
        state = index.data(ApplePhotoModel.StateRole) or THUMB_LOADING
        if state == THUMB_LOADING:
            painter.fillRect(rect, self.placeholder_color)
            return
        painter.fillRect(rect, theme.qcolor("surface"))
        meta = index.data(ApplePhotoModel.MetaRole) or {}
        is_video = meta.get("type") == "video"
        side = min(rect.width(), rect.height())
        icon = side * 0.28
        show_text = side >= 110
        cy = rect.center().y() - (side * 0.10 if show_text else 0)
        icon_rect = QRectF(rect.center().x() - icon / 2, cy - icon * 0.4, icon, icon * 0.8)
        paint_media_glyph(painter, icon_rect, is_video, crossed=(state == THUMB_MISSING_FILE))
        if not show_text:
            return
        if state == THUMB_MISSING_FILE:
            reason = "Soubor nedostupný"
        else:
            reason = "Video bez náhledu" if is_video else "Náhled nedostupný"
        text_w = rect.width() - 16
        f = QFont("Segoe UI")
        f.setPixelSize(max(10, int(side * 0.065)))
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(theme.qcolor("text_muted"))
        y = int(icon_rect.bottom() + side * 0.06)
        h = painter.fontMetrics().height()
        painter.drawText(QRect(rect.left() + 8, y, text_w, h), Qt.AlignmentFlag.AlignCenter, reason)
        f.setWeight(QFont.Weight.Normal)
        painter.setFont(f)
        painter.setPen(theme.qcolor("text_secondary"))
        name = painter.fontMetrics().elidedText(meta.get("name") or "", Qt.TextElideMode.ElideMiddle, text_w)
        painter.drawText(QRect(rect.left() + 8, y + h + 2, text_w, h), Qt.AlignmentFlag.AlignCenter, name)

    def _blit_resting(self, painter, option, index):
        """Klidová dlaždice s náhledem = jedno drawPixmap hotové dlaždice: žádné save/restore,
        cesty, škálování ani text. False = je potřeba plné kreslení (bez náhledu, hraje Live)."""
        live = getattr(self.parent(), "live_preview", None)
        if live is not None and live.playing_row == index.row():
            return False
        pixmap = index.data(ApplePhotoModel.ThumbRole)
        if pixmap is None or pixmap.isNull():
            return False
        rect = option.rect.adjusted(4, 4, -4, -4)
        if rect.width() <= 0 or rect.height() <= 0:
            return True
        meta = index.data(ApplePhotoModel.MetaRole) or {}
        radius = max(6.0, min(14.0, rect.width() * 0.07))
        dev = painter.device()
        dpr = dev.devicePixelRatioF() if dev is not None else 1.0
        # dlaždice má DPR zařízení = kopie 1:1; vyhlazování by ji vedlo pomalou cestou (2,5×)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawPixmap(rect.topLeft(), self._tile(pixmap, rect.size(), radius, meta, dpr))
        return True

    def _hover(self, index):
        view = self.parent()
        fn = getattr(view, "hover_progress", None)
        return fn(index.row()) if fn else 0.0

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Kairos dávka 4: plynulé rohy, při najetí dlaždice „povyskočí“ do mezery (a fotka se
        jemně přiblíží), výběr = prstenec zvýraznění s vnořenou fotkou a fajfkou."""
        hover = self._hover(index)
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if hover <= 0.01 and not is_selected and self._blit_resting(painter, option, index):
            return                                      # posouvání: čisté zkopírování hotové dlaždice

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        inset = 4.0 - 3.0 * hover                       # najetí: dlaždice doroste do mezery
        rect_f = QRectF(option.rect).adjusted(inset, inset, -inset, -inset)
        rect = rect_f.toAlignedRect()
        if rect.width() <= 0 or rect.height() <= 0:
            painter.restore()
            return
        t = tokens.palette()
        radius = max(6.0, min(14.0, rect_f.width() * 0.07))

        if hover > 0.01 and not is_selected:          # měkký stín pod zvednutou dlaždicí
            for i, alpha in enumerate((34, 20, 10)):
                grow = 1.5 + i * 2.0
                shadow = QColor(0, 0, 0, round(alpha * hover))
                painter.fillPath(smooth_rect_path(rect_f.adjusted(-grow, -grow + 3, grow, grow + 4),
                                                  radius + grow), shadow)

        frame = rect_f
        if is_selected:                                 # vybraná fotka se vnoří do prstence
            painter.fillPath(smooth_rect_path(rect_f, radius), tokens.qcolor(t["accent_soft"]))
            frame = rect_f.adjusted(5, 5, -5, -5)
        path_radius = radius - (3 if is_selected else 0)

        pixmap = index.data(ApplePhotoModel.ThumbRole)
        frame_r = frame.toAlignedRect()
        meta = index.data(ApplePhotoModel.MetaRole) or {}
        m_type = meta.get("type")
        live = getattr(self.parent(), "live_preview", None)      # Polish P4: Live Photo při najetí
        live_frame = live.frame_for(index.row()) if live is not None else None
        zoom = 1.0 + 0.04 * hover * (0 if is_selected else 1)
        if pixmap and not pixmap.isNull() and zoom == 1.0 and live_frame is None \
                and QRectF(frame_r) == frame:                  # klidová dlaždice: hotová z mezipaměti
            dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
            painter.drawPixmap(frame_r.topLeft(), self._tile(pixmap, frame_r.size(), path_radius, meta, dpr))
        else:                                                  # zvedaná / přehrávaná / bez náhledu
            self._paint_photo(painter, frame, smooth_rect_path(frame, path_radius), pixmap, zoom, meta,
                              live_frame, index)
            self._paint_badges(painter, frame, meta)

        white = QColor("#FFFFFF")

        if is_selected:
            accent = tokens.qcolor(t["accent"])
            painter.setPen(QPen(accent, 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(smooth_rect_path(rect_f.adjusted(1.25, 1.25, -1.25, -1.25), radius))
            chk = QRectF(frame.right() - 25, frame.bottom() - 25, 20, 20)
            painter.setPen(QPen(white, 1.6))
            painter.setBrush(tokens.qcolor(t["fill"]))
            painter.drawEllipse(chk)
            icons.paint(painter, "check", chk.adjusted(3, 3, -3, -3), tokens.qcolor(t["on_accent"]))
        elif hover > 0.01:
            edge = QColor(255, 255, 255, round(70 * hover)) if theme.current() == "dark" \
                else QColor(0, 0, 0, round(40 * hover))
            painter.setPen(QPen(edge, 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(smooth_rect_path(rect_f.adjusted(.5, .5, -.5, -.5), radius))

        painter.restore()


# ── AppleGridView ─────────────────────────────────────────────────────────────

# Nejméně 3 sloupce: 2 obří dlaždice na širokém displeji působily jako rozbitá mřížka (test v praxi)
GRID_MIN_COLUMNS, GRID_MAX_COLUMNS = 3, 14
GRID_MIN_CELL, GRID_MAX_CELL = 90, 420
SCROLL_IDLE_MS = 140               # tak dlouho po posledním posunu se vrací najetí myši
PINCH_SETTLE_MS = 160              # touchpad (Ctrl + jemné kolečko): po tak dlouhém klidu se dosedne
FROZEN_MAX_TILES = 600             # pojistka: tolik dlaždic nejvýš v jednom zmrazeném snímku


# ── Zoom mřížky bez přepočtu rozvržení (Fáze 1) ──────────────────────────────
# Dřív každý krok animace zoomu volal setGridSize + layoutChanged + doItemsLayout (rozvržení celé
# knihovny) a každou viditelnou dlaždici znovu rastroval v nové velikosti (~0,5 ms / dlaždice) –
# 50+ ms na snímek proti rozpočtu 8,3 ms při 120 Hz, a mezivelikosti navíc vytlačily klidové
# dlaždice z cache. Teď je rozvržení během gesta „zmrazené“: rozvržení jsou jen funkce
# index -> obdélník, kreslí se hotové klidové dlaždice škálované do mezilehlých obdélníků
# a skutečné rozvržení se nastaví JEDNOU, když pohyb doběhne.

class _GridLayout:
    """Rovnoměrná mřížka QListView (IconMode, spacing 0): aritmetika místo rozvržení."""

    def __init__(self, cell, cols, ox, oy, scroll, count):
        self.cell, self.cols = float(cell), max(1, int(cols))
        self.ox, self.oy, self.scroll, self.count = ox, oy, float(scroll), count

    def rect(self, i):
        r, c = divmod(i, self.cols)
        return QRectF(self.ox + c * self.cell, self.oy + r * self.cell - self.scroll, self.cell, self.cell)

    def content_height(self):
        rows = (self.count + self.cols - 1) // self.cols
        return self.oy + rows * self.cell

    def index_range(self, vp):
        if not self.count:
            return 0, -1
        r0 = int((vp.top() + self.scroll - self.oy) // self.cell)
        r1 = int((vp.bottom() + self.scroll - self.oy) // self.cell)
        first = max(0, r0 * self.cols)
        last = min(self.count - 1, (r1 + 1) * self.cols - 1)
        return first, last


class _ScaledLayout:
    """Mřížka zvětšená o s kolem kotvy (gesto prstů / touchpad: 1:1 s pohybem)."""

    def __init__(self, base, s, anchor):
        self.base, self.s, self.anchor = base, float(s), QPointF(anchor)
        self.count = base.count

    def rect(self, i):
        r, a, s = self.base.rect(i), self.anchor, self.s
        return QRectF(a.x() + (r.x() - a.x()) * s, a.y() + (r.y() - a.y()) * s, r.width() * s, r.height() * s)

    def index_range(self, vp):
        a, s = self.anchor, max(1e-3, self.s)
        inv = QRectF(a.x() + (vp.left() - a.x()) / s, a.y() + (vp.top() - a.y()) / s,
                     vp.width() / s, vp.height() / s)
        return self.base.index_range(inv)


class _BlendLayout:
    """Přechod mezi dvěma rozvrženími (e = 0 … 1)."""

    def __init__(self, a, b, e=0.0):
        self.a, self.b, self.e = a, b, float(e)
        self.count = b.count

    def rect(self, i):
        ra, rb, e = self.a.rect(i), self.b.rect(i), self.e
        return QRectF(ra.x() + (rb.x() - ra.x()) * e, ra.y() + (rb.y() - ra.y()) * e,
                      ra.width() + (rb.width() - ra.width()) * e, ra.height() + (rb.height() - ra.height()) * e)

    def index_range(self, vp):
        a0, a1 = self.a.index_range(vp)
        b0, b1 = self.b.index_range(vp)
        if a1 < a0:
            return b0, b1
        if b1 < b0:
            return a0, a1
        return min(a0, b0), max(a1, b1)

    def frozen(self):
        """Aktuální stav jako samostatné rozvržení (nový cíl uprostřed animace navazuje odsud)."""
        return _BlendLayout(self.a, self.b, self.e)
_SCROLL_SWALLOWED_EVENTS = (QEvent.Type.HoverMove, QEvent.Type.HoverEnter, QEvent.Type.HoverLeave,
                            QEvent.Type.MouseMove)


class AppleGridView(QListView):
    item_activated = pyqtSignal(int)
    zoom_changed = pyqtSignal(int)          # nová velikost dlaždice (px) po Ctrl + kolečku / gestu

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(28)
        # posuvník vždy místo zabraný (tenký, průhledná dráha): šířka mřížky se pak nemění,
        # když se objeví – jinak by poslední sloupec přeskočil na další řádek
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setMouseTracking(True)
        self.photo_model = ApplePhotoModel(1200, self)
        self.photo_delegate = ApplePhotoDelegate(self)
        self.setModel(self.photo_model)
        self.setItemDelegate(self.photo_delegate)
        self.doubleClicked.connect(self._on_double_clicked)
        self.cell_size = self.photo_delegate.cell_size
        # najetí: jedna dlaždice se zvedá, předchozí klesá (animace jen při pohybu myši)
        self._hover_in_row, self._hover_out_row = -1, -1
        self._hover_in = motion.Tween(0.0, lambda v: self._repaint_row(self._hover_in_row), self)
        self._hover_out = motion.Tween(0.0, lambda v: self._repaint_row(self._hover_out_row), self)
        self.entered.connect(lambda idx: self._set_hover_row(idx.row()))
        # zoom (Fáze 1): rozvržení zmrazené během pohybu, kotva = fotka pod kurzorem / prsty
        self._zoom_t = motion.Tween(0.0, self._on_zoom_progress, self)
        self._zoom_t.finished.connect(self._on_zoom_finished)
        self._frozen = None                 # zobrazené rozvržení během zoomu (None = QListView)
        self._zoom_target = None            # cílové _GridLayout běžícího přechodu
        self._pinch_base = None             # gesto: rozvržení na začátku, měřítko, kotva
        self._pinch_scale = 1.0
        self._pinch_anchor = QPointF()
        self._pinch_settle = QTimer(self)   # touchpad: dosednutí po krátkém klidu
        self._pinch_settle.setSingleShot(True)
        self._pinch_settle.setInterval(PINCH_SETTLE_MS)
        self._pinch_settle.timeout.connect(self._pinch_end)
        self.photo_model.modelReset.connect(self._finish_zoom)   # nová data = zoom dosedne hned
        self._wheel_accum = 0
        self._cols = None                   # počet sloupců drží šířku: dlaždice vyplní řádek celý
        self.zoom_hint = HintPill(self)
        self.grabGesture(Qt.GestureType.PinchGesture)
        # Polish P4: Live Photo se po ~350 ms pod kurzorem rozpohybuje (pás snímků scrub_strip)
        self.live_preview = live_hover.LivePreview(self)
        # Během posouvání žádné najetí: zvedání dlaždic, Live Photo ani sledování myši – každá
        # dlaždice je jen kopie hotové pixmapy. Najetí se obnoví SCROLL_IDLE_MS po zastavení.
        self._scrolling = False
        self._scroll_idle = QTimer(self)
        self._scroll_idle.setSingleShot(True)
        self._scroll_idle.setInterval(SCROLL_IDLE_MS)
        self._scroll_idle.timeout.connect(self._on_scroll_idle)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    # ── velikost dlaždic ──
    def set_cell_size(self, px: int):
        # strop tří sloupců až u rozvržené mřížky: při startu je viewport ~100 px a strop by
        # zapamatovanou velikost natrvalo zmenšil (sloupce pak hlídá resizeEvent)
        laid_out = self.isVisible() and self.viewport().width() >= GRID_MIN_COLUMNS * GRID_MIN_CELL
        top = self._max_cell() if laid_out else GRID_MAX_CELL
        px = int(max(GRID_MIN_CELL, min(top, px)))
        self._finish_zoom()
        self._cols = None                  # sloupce se dopočítají z velikosti při dalším rozvržení
        self.cell_size = px
        self.photo_delegate.set_cell_size(px)
        self.setGridSize(QSize(px, px))
        self.photo_model.layoutChanged.emit()

    def columns(self, cell=None):
        if cell is None and self._cols:
            return self._cols                # držený počet sloupců (zoom, Dny)
        width = max(1, self.viewport().width())
        return max(GRID_MIN_COLUMNS, width // max(1, int(cell or self.cell_size)))

    def _max_cell(self):
        """Největší dlaždice: GRID_MAX_CELL, ale vždy aspoň GRID_MIN_COLUMNS sloupců."""
        return min(GRID_MAX_CELL, self._cell_for_columns(GRID_MIN_COLUMNS))

    def _cell_for_columns(self, cols):
        # o 2 px méně než celá šířka: QListView jinak poslední sloupec zalomí na další řádek
        return int(max(GRID_MIN_CELL, min(GRID_MAX_CELL, (self.viewport().width() - 2) // max(1, cols))))

    def set_columns(self, cols, animate=True):
        """Pevný počet sloupců (Dny = 4); velikost dlaždic se dopočítá z šířky."""
        self._cols = max(GRID_MIN_COLUMNS, min(GRID_MAX_COLUMNS, int(cols)))
        if animate:
            self._animate_to(self._cols, QPointF(4, 4))
        else:
            self._finish_zoom()
            self._apply_zoom_step(self._cell_for_columns(self._cols))

    def resizeEvent(self, event):
        self._finish_zoom()                  # okno se mění: rozpracovaný zoom dosednout hned
        super().resizeEvent(event)
        if self.viewport().width() <= 2:
            return
        if self._cols is None:
            self._cols = max(GRID_MIN_COLUMNS, (self.viewport().width() - 2) // max(1, self.cell_size))
        cell = self._cell_for_columns(self._cols)
        if cell != self.cell_size:
            self._apply_zoom_step(cell)

    def zoom_by(self, steps, anchor_pos=None):
        """steps > 0 = přiblížit (méně sloupců). Plynule a s fotkou pod kurzorem na místě."""
        current = self._cols or self.columns()
        cols = max(GRID_MIN_COLUMNS, min(GRID_MAX_COLUMNS, current - steps))
        if cols == current and (self._zoom_target is None or self._zoom_target.cols == cols):
            return
        self._cols = cols
        pos = QPointF(anchor_pos if anchor_pos is not None else self.viewport().rect().center())
        self._animate_to(cols, pos)
        self.zoom_hint.flash(_czech_columns(cols))
        self.zoom_changed.emit(self._cell_for_columns(cols))

    def _apply_zoom_step(self, value):
        """Okamžité skutečné rozvržení (změna velikosti okna, dosednutí zoomu)."""
        px = int(round(float(value)))
        if px == self.cell_size:
            return
        self.cell_size = px
        self.photo_delegate.set_cell_size(px)
        self.setGridSize(QSize(px, px))
        self.photo_model.layoutChanged.emit()

    # ── zmrazené rozvržení ──
    def _current_grid(self):
        """Skutečné rozvržení QListView jako aritmetická mřížka."""
        n = self.photo_model.rowCount()
        sb = self.verticalScrollBar().value()
        ox = oy = 0.0
        if n:
            r0 = self.visualRect(self.photo_model.index(0, 0))
            ox, oy = float(r0.x()), float(r0.y() + sb)
        cols = max(1, int(self.viewport().width() // max(1, self.cell_size)))
        return _GridLayout(self.cell_size, cols, ox, oy, sb, n)

    def _shown_layout(self):
        if isinstance(self._frozen, _BlendLayout):
            return self._frozen.frozen()
        return self._frozen if self._frozen is not None else self._current_grid()

    def _index_at(self, layout, pos):
        first, last = layout.index_range(QRectF(self.viewport().rect()))
        best, best_d = None, None
        for i in range(first, min(last, first + FROZEN_MAX_TILES) + 1):
            r = layout.rect(i)
            if r.contains(pos):
                return i
            d = (r.center() - pos).manhattanLength()
            if best_d is None or d < best_d:
                best, best_d = i, d
        return best

    def _target_grid(self, cols, anchor, start):
        """Cílová mřížka pro počet sloupců; posun tak, aby fotka pod kotvou zůstala na místě."""
        base = self._current_grid()
        cell = float(self._cell_for_columns(cols))
        target = _GridLayout(cell, cols, base.ox, base.oy, 0.0, base.count)
        idx = self._index_at(start, anchor) if base.count else None
        scroll = 0.0
        if idx is not None:
            src = start.rect(idx)
            frac = (anchor.y() - src.top()) / max(1.0, src.height())
            scroll = target.rect(idx).top() + frac * cell - anchor.y()
        max_scroll = max(0.0, target.content_height() - self.viewport().height())
        target.scroll = min(max(0.0, scroll), max_scroll)
        return target

    def _animate_to(self, cols, anchor, kind="emphasized"):
        start = self._shown_layout()
        self._zoom_target = self._target_grid(cols, anchor, start)
        self._begin_freeze(_BlendLayout(start, self._zoom_target, 0.0))
        self._zoom_t._anim.stop()
        self._zoom_t.value = self._zoom_t.target = 0.0
        self._zoom_t.to(1.0, kind)          # skrytá mřížka / omezený pohyb = dosedne hned

    def _begin_freeze(self, layout):
        if self._frozen is None:
            governor.get().hold("gesture")  # práce na pozadí počká, ať zoom má celý snímek
            self.live_preview.stop()
            self._drop_hover()
        self._frozen = layout
        self.viewport().update()

    def _on_zoom_progress(self, e):
        if isinstance(self._frozen, _BlendLayout):
            self._frozen.e = float(e)
            self.viewport().update()

    def _on_zoom_finished(self):
        if self._zoom_target is not None and self._pinch_base is None:
            self._commit_zoom(self._zoom_target)

    def _finish_zoom(self):
        """Rozpracovaný zoom / gesto dosednout hned (posun, změna velikosti, nová data)."""
        if self._pinch_base is not None:
            self._pinch_settle.stop()
            self._pinch_end(animate=False)
        elif self._zoom_target is not None:
            self._zoom_t._anim.stop()
            self._commit_zoom(self._zoom_target)

    def _commit_zoom(self, target):
        """Jediné skutečné rozvržení na konci pohybu (a jediné rastrování dlaždic v nové velikosti)."""
        was_frozen = self._frozen is not None
        self._zoom_target = None
        self._frozen = None
        self._apply_zoom_step(target.cell)
        self.doItemsLayout()
        self.verticalScrollBar().setValue(int(round(target.scroll)))
        if was_frozen:
            governor.get().release("gesture")
        self.viewport().update()

    def paintEvent(self, event):
        if self._frozen is not None:
            self._paint_frozen()
        else:
            super().paintEvent(event)
        frame_meter.tick("grid")            # Fáze 0: doba snímku mřížky (Ctrl+Shift+F)

    def _paint_frozen(self):
        """Klidové dlaždice (z cache delegáta) škálované do mezilehlých obdélníků: žádné
        rozvržení, žádné rastrování – jen kopie pixmap."""
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        vp = QRectF(self.viewport().rect())
        layout, model, delegate = self._frozen, self.photo_model, self.photo_delegate
        first, last = layout.index_range(vp)
        last = min(last, first + FROZEN_MAX_TILES)
        dpr = self.viewport().devicePixelRatioF()
        rest = QSize(max(1, self.cell_size - 8), max(1, self.cell_size - 8))
        radius = max(6.0, min(14.0, rest.width() * 0.07))
        placeholder = delegate.placeholder_color
        for i in range(max(0, first), last + 1):
            r = layout.rect(i).adjusted(4, 4, -4, -4)
            if r.width() <= 1 or not r.intersects(vp):
                continue
            idx = model.index(i, 0)
            pix = model.data(idx, ApplePhotoModel.ThumbRole)
            if pix is None or pix.isNull():
                p.fillRect(r, placeholder)
                continue
            tile = delegate._tile(pix, rest, radius, model.data(idx, ApplePhotoModel.MetaRole) or {}, dpr)
            p.drawPixmap(r, tile, QRectF(tile.rect()))
        p.end()

    # ── gesto prstů / touchpad: 1:1, pak dosednutí na celé sloupce ──
    def _pinch_begin(self, anchor):
        self._zoom_t._anim.stop()
        self._pinch_base = self._shown_layout()
        self._pinch_scale = 1.0
        self._pinch_anchor = QPointF(anchor)

    def _pinch_update(self, scale):
        base = self._pinch_base
        if base is None:
            return
        base_cell = max(1.0, base.rect(0).width()) if base.count else float(self.cell_size)
        cell = base_cell * scale
        # gumové meze: za nejmenší / největší velikostí (i GRID_MIN_COLUMNS) jen zlomek pohybu
        top = self._max_cell()
        if cell > top:
            cell = top + (cell - top) * 0.3
        elif cell < GRID_MIN_CELL:
            cell = GRID_MIN_CELL - (GRID_MIN_CELL - cell) * 0.3
        self._pinch_scale = cell / base_cell
        self._begin_freeze(_ScaledLayout(base, self._pinch_scale, self._pinch_anchor))

    def _pinch_end(self, animate=True):
        base = self._pinch_base
        if base is None:
            return
        base_cell = max(1.0, base.rect(0).width()) if base.count else float(self.cell_size)
        width = max(1, self.viewport().width() - 2)
        cols = int(round(width / max(1.0, base_cell * self._pinch_scale)))
        cols = max(GRID_MIN_COLUMNS, min(GRID_MAX_COLUMNS, cols))
        shown = _ScaledLayout(base, self._pinch_scale, self._pinch_anchor)
        self._pinch_base = None
        self._cols = cols
        self._zoom_target = self._target_grid(cols, self._pinch_anchor, shown)
        if animate:
            self._begin_freeze(_BlendLayout(shown, self._zoom_target, 0.0))
            self._zoom_t._anim.stop()
            self._zoom_t.value = self._zoom_t.target = 0.0
            self._zoom_t.to(1.0, "spring")       # dosednutí s jemným přetočením
        else:
            self._commit_zoom(self._zoom_target)
        self.zoom_hint.flash(_czech_columns(cols))
        self.zoom_changed.emit(self._cell_for_columns(cols))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            dy = event.angleDelta().y()
            if event.pixelDelta().y() or (dy and dy % 120):
                # touchpad / sevření prstů (jemné kroky): plynule 1:1, po klidu dosednout
                if self._pinch_base is None:
                    self._pinch_begin(event.position())
                self._pinch_update(self._pinch_scale * math.exp(dy / 600.0))
                self._pinch_settle.start()
            else:
                # kolečko myši – po celých krocích (sloupec za zářez)
                self._wheel_accum += dy
                steps = int(self._wheel_accum / 120)
                if steps:
                    self._wheel_accum -= steps * 120
                    self.zoom_by(steps, event.position())
            event.accept()
            return
        self._finish_zoom()                  # běžný posun: zoom dosedne a mřížka jede dál
        super().wheelEvent(event)

    def event(self, e):
        if e.type() == QEvent.Type.Gesture:
            pinch = e.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                center = QPointF(self.viewport().mapFromGlobal(pinch.centerPoint().toPoint()))
                state = pinch.state()
                if state == Qt.GestureState.GestureStarted:
                    self._pinch_begin(center)
                elif state == Qt.GestureState.GestureUpdated:
                    if self._pinch_base is None:
                        self._pinch_begin(center)
                    self._pinch_update(pinch.totalScaleFactor())
                elif state in (Qt.GestureState.GestureFinished, Qt.GestureState.GestureCanceled):
                    self._pinch_end()
                return True
        return super().event(e)

    # ── posouvání ──
    def is_scrolling(self):
        return self._scrolling

    def _on_scroll(self, _value):
        if not self._scrolling:
            self._scrolling = True
            self.live_preview.stop()
            self._drop_hover()
        self._scroll_idle.start()

    def _drop_hover(self):
        """Zvednutá dlaždice okamžitě dolů (bez animace) – při posunu by jinak animovala
        a kreslila se plnou cestou se škálováním."""
        rows = (self._hover_in_row, self._hover_out_row)
        self._hover_in_row = self._hover_out_row = -1
        self._hover_in.set(0.0)
        self._hover_out.set(0.0)
        for row in rows:
            self._repaint_row(row)

    def _on_scroll_idle(self):
        self._scrolling = False
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(pos) and self.isVisible():
            idx = self.indexAt(pos)
            self._set_hover_row(idx.row() if idx.isValid() else -1)

    def viewportEvent(self, event):
        # myš během posouvání: žádné hover / entered / přepočty pod kurzorem
        # (Qt volá už během konstrukce – atribut ještě nemusí existovat; výjimka = qFatal)
        if getattr(self, "_scrolling", False) and event.type() in _SCROLL_SWALLOWED_EVENTS \
                and (event.type() != QEvent.Type.MouseMove or not event.buttons()):
            return True
        return super().viewportEvent(event)

    # ── najetí ──
    def hover_progress(self, row):
        if self._scrolling:
            return 0.0
        if row == self._hover_in_row:
            return float(self._hover_in.value)
        if row == self._hover_out_row:
            return float(self._hover_out.value)
        return 0.0

    def _repaint_row(self, row):
        if 0 <= row < self.photo_model.rowCount():
            self.viewport().update(self.visualRect(self.photo_model.index(row, 0)))

    def _set_hover_row(self, row):
        if row == self._hover_in_row or self._scrolling:
            return
        old_row, old_value = self._hover_in_row, float(self._hover_in.value)
        if self._hover_out_row >= 0 and self._hover_out_row != row:
            self._hover_out.set(0.0)
            self._repaint_row(self._hover_out_row)
        self._hover_out_row = old_row
        self._hover_out.set(old_value)
        self._hover_out.to(0.0, "standard")
        self._hover_in_row = row
        self._hover_in.set(0.0)
        if row >= 0:
            self._hover_in.to(1.0, "micro")
        self.live_preview.hover(row)

    def leaveEvent(self, event):
        self._set_hover_row(-1)
        self.live_preview.stop()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self.live_preview.stop()
        super().hideEvent(event)

    def _on_double_clicked(self, index: QModelIndex):
        if index.isValid():
            self.item_activated.emit(index.row())

    def selected_row_indices(self):
        return [idx.row() for idx in self.selectionModel().selectedIndexes()]

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and key in (
                Qt.Key.Key_Plus, Qt.Key.Key_Equal, Qt.Key.Key_Minus):
            self.zoom_by(1 if key != Qt.Key.Key_Minus else -1)
            event.accept(); return
        if key == Qt.Key.Key_F:
            self.main_window.toggle_favorite_on_selection(self)
            event.accept(); return
        if key == Qt.Key.Key_S and self.photo_model.rowCount() > 0:
            self.main_window.start_slideshow_from_grid(self)
            event.accept(); return
        if key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            rows = self.selected_row_indices()
            if rows:
                self.item_activated.emit(rows[0])
                event.accept(); return
        if key == Qt.Key.Key_Escape:
            if self.selectionModel().hasSelection():
                self.clearSelection()
                event.accept(); return
            event.ignore(); return      # bez výběru Esc patří stránce (podstránka = zpět)
        super().keyPressEvent(event)


def _czech_columns(n):
    return f"{n} sloupec" if n == 1 else (f"{n} sloupce" if 2 <= n <= 4 else f"{n} sloupců")


# ── P3.1 Async person cards ───────────────────────────────────────────────────

UNKNOWN_PERSON = "Neznámá osoba"
PERSON_CARD_W, PERSON_CARD_H = 176, 228
PERSON_AVATAR_RATIO = 0.78           # průměr avataru / šířka karty
PERSON_GRID_SPACING = 16


def _czech_photo_count(n):
    n = int(n or 0)
    if n == 1:
        return "1 fotka"
    if 2 <= n <= 4:
        return f"{n} fotky"
    return f"{n} fotek"


def _czech_item_count(n):
    n = int(n or 0)
    if n == 1:
        return "1 položka"
    if 2 <= n <= 4:
        return f"{n} položky"
    return f"{n} položek"


def _czech_result_count(n):
    n = int(n or 0)
    if n == 1:
        return "1 shoda"
    if 2 <= n <= 4:
        return f"{n} shody"
    return f"{n} shod"


def _format_date_range(start, end):
    """'2024-07-01 10:00' / '2024-07-03' -> '1. 7. 2024 – 3. 7. 2024' (jeden den = jedno datum)."""
    def fmt(value):
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
        return f"{d.day}. {d.month}. {d.year}"
    a, b = fmt(start), fmt(end)
    if a and b and a != b:
        return f"{a} – {b}"
    return a or b


def _person_initials(name):
    if not name or name.startswith("Neznám"):
        return ""
    parts = [p for p in name.split() if p[:1].isalnum()]
    return "".join(p[0] for p in parts[:2]).upper()


def _load_avatar_image(path, px):
    """Načte ořez obličeje jako kruhový QImage px×px; při chybě vrátí prázdný QImage.

    Pracuje jen s QImage, takže je bezpečné ho volat z QRunnable (QPixmap mimo GUI vlákno ne).
    """
    src = QImage(path) if path else QImage()
    if src.isNull() or px <= 0:
        return QImage()
    side = min(src.width(), src.height())
    src = src.copy((src.width() - side) // 2, (src.height() - side) // 2, side, side)
    src = src.scaled(px, px, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    out = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, px, px)
    p.setClipPath(clip)
    p.drawImage(0, 0, src)
    p.end()
    return out


def _paint_avatar_placeholder(p, rect, name):
    """Šedý kruh ve stylu Kontaktů: iniciály pojmenované osoby, jinak silueta."""
    grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    grad.setColorAt(0.0, QColor("#8E8E93"))
    grad.setColorAt(1.0, QColor("#636366"))
    circle = QPainterPath()
    circle.addEllipse(rect)
    p.fillPath(circle, grad)
    initials = _person_initials(name)
    d = rect.width()
    if initials:
        f = QFont("Segoe UI")
        f.setPixelSize(max(8, int(d * 0.36)))
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        p.setPen(QColor("#FFFFFF"))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials)
        return
    p.save()
    p.setClipPath(circle)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 200))
    cx = rect.center().x()
    head_r = d * 0.19
    p.drawEllipse(QRectF(cx - head_r, rect.top() + d * 0.20, head_r * 2, head_r * 2))
    p.drawEllipse(QRectF(cx - d * 0.34, rect.top() + d * 0.64, d * 0.68, d * 0.62))
    p.restore()


def paint_person_card(p, rect, avatar, name, count, hovered=False, pressed=False, selected=False,
                      hover_progress=None):
    """Společné vykreslení karty osoby (widget PersonCard i offscreen _make_person_card).
    selected = vybraná ke sloučení (prstenec ve zvýraznění + fajfka). hover_progress 0–1 plynule
    (bez něj platí hovered)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    rect = QRectF(rect)
    w = rect.width()
    t = tokens.palette()
    hover = float(hover_progress) if hover_progress is not None else (1.0 if hovered else 0.0)

    card = rect.adjusted(0.5, 0.5, -0.5, -0.5)
    radius = tokens.RADIUS["lg"]
    bg = tokens.mix(tokens.qcolor(t["surface"]), tokens.qcolor(t["surface_hover"]), hover)
    if pressed:
        bg = tokens.qcolor(t["surface_pressed"])
    path = smooth_rect_path(card, radius)
    p.fillPath(path, bg)
    p.setPen(QPen(tokens.qcolor(t["hairline"]), 1.0))
    p.drawPath(path)

    d = w * PERSON_AVATAR_RATIO
    pad = (w - d) / 2
    av = QRectF(rect.left() + pad, rect.top() + pad, d, d)
    if avatar is not None and not avatar.isNull():
        zoom = 1.0 + 0.03 * hover                       # najetí: tvář se jemně přiblíží
        p.save()
        circle = QPainterPath()
        circle.addEllipse(av)
        p.setClipPath(circle)
        dz = d * zoom
        p.drawImage(QRectF(av.center().x() - dz / 2, av.center().y() - dz / 2, dz, dz), avatar)
        p.restore()
    else:
        _paint_avatar_placeholder(p, av, name)
    p.setPen(QPen(QColor(255, 255, 255, 26) if theme.current() == "dark" else QColor(0, 0, 0, 20), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(av)
    if selected:
        ring = max(2.5, w * 0.022)
        p.setPen(QPen(tokens.qcolor(t["accent"]), ring))
        p.drawEllipse(av.adjusted(-ring, -ring, ring, ring))
        badge = w * 0.17
        b = QRectF(av.right() - badge * 0.9, av.bottom() - badge * 0.95, badge, badge)
        p.setPen(QPen(bg, max(2.0, w * 0.014)))
        p.setBrush(tokens.qcolor(t["fill"]))
        p.drawEllipse(b)
        icons.paint(p, "check", b.adjusted(badge * 0.2, badge * 0.2, -badge * 0.2, -badge * 0.2),
                    tokens.qcolor(t["on_accent"]))

    text_w = int(w - 2 * pad * 0.6)
    text_x = rect.left() + (w - text_w) / 2
    name_font = QFont(tokens.ui_family())
    name_font.setPixelSize(max(9, round(w * 0.075)))
    name_font.setWeight(QFont.Weight.DemiBold)
    p.setFont(name_font)
    unnamed = not name or name == UNKNOWN_PERSON
    p.setPen(tokens.qcolor(t["text_secondary"] if unnamed else t["text"]))
    name_h = p.fontMetrics().height()
    name_y = av.bottom() + w * 0.07
    shown = p.fontMetrics().elidedText(name or UNKNOWN_PERSON, Qt.TextElideMode.ElideRight, text_w)
    p.drawText(QRectF(text_x, name_y, text_w, name_h), Qt.AlignmentFlag.AlignCenter, shown)

    sub_font = QFont(tokens.ui_family())
    sub_font.setPixelSize(max(8, round(w * 0.063)))
    sub_font.setFeature(QFont.Tag("tnum"), 1)
    p.setFont(sub_font)
    p.setPen(tokens.qcolor(t["text_tertiary"] if unnamed else t["text_secondary"]))
    sub_h = p.fontMetrics().height()
    p.drawText(QRectF(text_x, name_y + name_h + 1, text_w, sub_h),
               Qt.AlignmentFlag.AlignCenter, _czech_photo_count(count))
    p.restore()


class PersonCardSignals(QObject):
    ready = pyqtSignal(int, QImage, str, int)


class PersonCardWorker(QRunnable):
    """Načte avatar osoby mimo GUI vlákno; target_size = průměr avataru v pixelech."""

    def __init__(self, person_id, name, cover_path, count, target_size=170):
        super().__init__()
        self.person_id = person_id
        self.name = name
        self.cover_path = cover_path
        self.count = count
        self.target_size = target_size
        self.signals = PersonCardSignals()
        self.setAutoDelete(True)

    def run(self):
        try:
            path = resolve_face_path(self.cover_path)
            if path is None:
                # Chybí cover -> vezmeme (nebo z originálu vyřízneme) nejlepší obličej osoby
                path = face_engine.reconstruct_best_crop_for_person(self.person_id)
            img = _load_avatar_image(path, self.target_size)
            self.signals.ready.emit(self.person_id, img, self.name, self.count)
        except Exception as e:
            print(f"[PersonCard] {e}")


def _make_person_card(crop_path, name, photo_count, target_size=PERSON_CARD_W):
    """Offscreen render karty osoby (2× HiDPI) – stejný vzhled jako widget PersonCard."""
    scale = 2
    w = int(target_size)
    h = round(w * PERSON_CARD_H / PERSON_CARD_W)
    avatar = _load_avatar_image(resolve_face_path(crop_path),
                                round(w * PERSON_AVATAR_RATIO * scale))
    result = QPixmap(w * scale, h * scale)
    result.setDevicePixelRatio(scale)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    paint_person_card(painter, QRectF(0, 0, w, h), avatar, name, photo_count)
    painter.end()
    return result


class PersonCard(QWidget):
    """Karta osoby: tmavé zaoblené pozadí, kruhový avatar, jméno a počet fotek.
    Klik = otevřít, Ctrl/Shift+klik = vybrat (sloučení), pravé tlačítko = nabídka."""
    clicked = pyqtSignal(int, str)
    selection_toggled = pyqtSignal(int, bool)        # (person_id, additive) – Ctrl/Shift+klik
    context_requested = pyqtSignal(int, QPoint)      # (person_id, globální pozice)

    def __init__(self, person_id, name, count, parent=None):
        super().__init__(parent)
        self.person_id = person_id
        self.name = name or UNKNOWN_PERSON
        self.count = count
        self.selected = False
        self._avatar = None
        self._hovered = False
        self._pressed = False
        self._hover_anim = motion.Tween(0.0, lambda v: self.update(), self)
        self.setFixedSize(PERSON_CARD_W, PERSON_CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{self.name}\n{_czech_photo_count(count)}\n"
                        "Ctrl+klik = vybrat více osob ke sloučení")
        self.setAccessibleName(f"{self.name}, {_czech_photo_count(count)}")

    def set_avatar(self, image: QImage):
        self._avatar = image
        self.update()

    def set_selected(self, on: bool):
        if bool(on) != self.selected:
            self.selected = bool(on)
            self.update()

    def contextMenuEvent(self, event):
        self.context_requested.emit(self.person_id, event.globalPos())
        event.accept()

    def enterEvent(self, event):
        self._hovered = True
        self._hover_anim.to(1.0, "micro")
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = self._pressed = False
        self._hover_anim.to(0.0, "standard")
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        was_pressed = self._pressed
        self._pressed = False
        self.update()
        if (event.button() == Qt.MouseButton.LeftButton and was_pressed
                and self.rect().contains(event.position().toPoint())):
            mods = event.modifiers()
            if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
                self.selection_toggled.emit(self.person_id, True)
                return
            self._hovered = False   # podstránka "spolkne" leaveEvent
            self.clicked.emit(self.person_id, self.name)
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        paint_person_card(p, QRectF(self.rect()), self._avatar, self.name, self.count,
                          self._hovered, self._pressed, self.selected, hover_progress=self._hover_anim.value)
        p.end()


class MergeSuggestionBar(QFrame):
    """„Je to stejná osoba?“ – dva avatary vedle sebe, Ano (sloučit) / Ne / Přeskočit."""
    accepted = pyqtSignal(dict)
    rejected = pyqtSignal(dict)

    AVATAR = 64

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MergeSuggestion")
        self.setStyleSheet("""
            QFrame#MergeSuggestion { background:#2C2C2E; border:1px solid rgba(255,255,255,0.08);
                                     border-radius:14px; }
            QLabel { background:transparent; }
        """)
        self.suggestions = []
        self.index = 0
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(12)
        self.lbl_a = QLabel()
        self.lbl_b = QLabel()
        for lbl in (self.lbl_a, self.lbl_b):
            lbl.setFixedSize(self.AVATAR, self.AVATAR)
        row.addWidget(self.lbl_a)
        row.addWidget(self.lbl_b)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.lbl_question = QLabel("Je to stejná osoba?")
        self.lbl_question.setStyleSheet("font-size:15px; font-weight:600; color:#F5F5F7;")
        self.lbl_detail = QLabel()
        self.lbl_detail.setStyleSheet("font-size:12px; color:#8E8E93;")
        texts.addWidget(self.lbl_question)
        texts.addWidget(self.lbl_detail)
        row.addLayout(texts, 1)
        self.btn_yes = QPushButton("Ano, sloučit")
        self.btn_yes.setObjectName("btn_primary")
        self.btn_no = QPushButton("Ne")
        self.btn_skip = QPushButton("Další")
        self.btn_skip.setToolTip("Rozhodnout později")
        for b in (self.btn_yes, self.btn_no, self.btn_skip):
            b.setMinimumHeight(30)
            row.addWidget(b)
        self.btn_yes.clicked.connect(lambda: self._emit(self.accepted))
        self.btn_no.clicked.connect(lambda: self._emit(self.rejected))
        self.btn_skip.clicked.connect(self._skip)
        self.hide()

    def current(self):
        if 0 <= self.index < len(self.suggestions):
            return self.suggestions[self.index]
        return None

    def set_suggestions(self, suggestions, avatar_loader):
        self.suggestions = list(suggestions)
        self.index = 0
        self._avatar_loader = avatar_loader
        self._show_current()

    def _show_current(self):
        s = self.current()
        if s is None:
            self.hide()
            return
        for lbl, pid in ((self.lbl_a, s["a"]), (self.lbl_b, s["b"])):
            img = self._avatar_loader(pid, self.AVATAR * 2)
            pix = QPixmap.fromImage(img) if img is not None and not img.isNull() else QPixmap()
            if pix.isNull():
                pix = QPixmap(self.AVATAR * 2, self.AVATAR * 2)
                pix.fill(Qt.GlobalColor.transparent)
                p = QPainter(pix)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                _paint_avatar_placeholder(p, QRectF(0, 0, pix.width(), pix.height()), "")
                p.end()
            pix.setDevicePixelRatio(2)
            lbl.setPixmap(pix)
        name_a = s["name_a"] or UNKNOWN_PERSON
        name_b = s["name_b"] or UNKNOWN_PERSON
        self.lbl_detail.setText(
            f"{name_a} ({s['faces_a']}) a {name_b} ({s['faces_b']}) · "
            f"podobnost {round(s['similarity'] * 100)} % · návrh {self.index + 1} z {len(self.suggestions)}")
        self.show()

    def _emit(self, signal):
        s = self.current()
        if s is not None:
            signal.emit(s)

    def _skip(self):
        self.index += 1
        self._show_current()

    def drop_current(self):
        """Po „Ne“: návrh zmizí a ukáže se další (po sloučení se návrhy načítají znovu,
        protože se změnil centroid sloučené osoby)."""
        if self.current() is None:
            return
        del self.suggestions[self.index]
        self.index = min(self.index, len(self.suggestions))
        self._show_current()


class FlowLayout(QLayout):
    """Řadí položky zleva doprava a zalamuje je podle dostupné šířky (responzivní mřížka)."""

    def __init__(self, parent=None, spacing=PERSON_GRID_SPACING):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect, apply):
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        for item in self._items:
            if item.isEmpty():          # skrytý widget (filtr vyhledávání)
                continue
            hint = item.sizeHint()
            if line_h and x + hint.width() > area.right() + 1:
                x = area.x()
                y += line_h + self._spacing
                line_h = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def create_event_card(cover_thumb_path, title, date_str, item_count, target_w=290, target_h=215):
    result = QPixmap(target_w, target_h)
    result.fill(Qt.GlobalColor.transparent)
    cover = QPixmap(cover_thumb_path) if (cover_thumb_path and os.path.exists(cover_thumb_path)) else QPixmap()
    if cover.isNull():
        cover = QPixmap(target_w, target_h)
        cover.fill(QColor("#27272A"))
    scaled = cover.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation)
    x = max(0, (scaled.width() - target_w) // 2)
    y = max(0, (scaled.height() - target_h) // 2)
    cropped = scaled.copy(x, y, target_w, target_h)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    card_path = QPainterPath()
    card_path.addRoundedRect(0, 0, target_w, target_h, 14, 14)
    painter.setClipPath(card_path)
    painter.drawPixmap(0, 0, cropped)
    grad = QLinearGradient(0, target_h * 0.45, 0, target_h)
    grad.setColorAt(0.0, QColor(0, 0, 0, 0))
    grad.setColorAt(1.0, QColor(0, 0, 0, 220))
    painter.fillRect(0, int(target_h * 0.35), target_w, int(target_h * 0.65), grad)
    painter.setPen(QColor("#FFFFFF"))
    painter.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
    painter.drawText(16, target_h - 44, target_w - 32, 22, Qt.AlignmentFlag.AlignLeft, title)
    painter.setPen(QColor("#D1D1D6"))
    painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
    painter.drawText(16, target_h - 22, target_w - 32, 18, Qt.AlignmentFlag.AlignLeft,
                     f"{date_str}  •  {item_count} položek")
    painter.end()
    return result


# ── CircularProgressWidget ────────────────────────────────────────────────────

class CircularProgressWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(220, 260)
        self._progress = 0
        self._status_text = "Načítání..."
        self._elapsed_sec = 0
        self._total_est_sec = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 185, 15, 15)
        self.btn_cancel = QPushButton("Zrušit import")
        self.btn_cancel.setStyleSheet("""
            QPushButton { background-color:#3A3A3C; color:#FFFFFF; border:none;
                border-radius:8px; font-size:12px; font-weight:bold; padding:8px; }
            QPushButton:hover { background-color:#FF453A; }
        """)
        layout.addWidget(self.btn_cancel)

    def set_progress(self, current, total):
        self._progress = int((current / total) * 100) if total > 0 else 0
        self.update()

    def set_status(self, text):
        self._status_text = text
        self.update()

    def set_time_info(self, elapsed_sec, total_est_sec):
        self._elapsed_sec = elapsed_sec
        self._total_est_sec = total_est_sec
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(22, 22, 24, 245))
        p.setPen(QPen(QColor(58, 58, 60), 1))
        p.drawRoundedRect(self.rect(), 22, 22)
        cx, cy, r, pw = self.width() // 2, 85, 44, 7
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(44, 44, 46), pw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        span = int(-360 * (self._progress / 100.0) * 16)
        p.setPen(QPen(QColor(10, 132, 255), pw, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(cx - r, cy - r, r * 2, r * 2, 90 * 16, span)
        p.setPen(QColor(255, 255, 255))
        p.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        p.drawText(QRect(cx - r, cy - r, r * 2, r * 2), Qt.AlignmentFlag.AlignCenter, f"{self._progress}%")
        em_m, em_s = divmod(self._elapsed_sec, 60)
        et_m, et_s = divmod(self._total_est_sec, 60)
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(QRect(12, cy + r + 4, self.width() - 24, 20), Qt.AlignmentFlag.AlignCenter,
                   f"{em_m}:{em_s:02d} / {et_m}:{et_s:02d}")
        p.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        p.setPen(QColor(174, 174, 178))
        st = self._status_text if len(self._status_text) < 28 else self._status_text[:25] + "..."
        p.drawText(QRect(12, cy + r + 24, self.width() - 24, 20), Qt.AlignmentFlag.AlignCenter, st)


# ── P3.2 AI Search worker ─────────────────────────────────────────────────────

class AISearchWorker(QThread):
    """Hledání 2.0 mimo GUI vlákno: search_engine.run_search (fazety + obraz, dvě pásma)."""
    search_finished = pyqtSignal(object, str)     # SearchResult, dotaz

    def __init__(self, query_text, embeddings_cache, universe=None):
        super().__init__()
        self.query_text = query_text
        self.embeddings_cache = embeddings_cache
        self.universe = universe if universe is not None else list(embeddings_cache or ())

    def run(self):
        # Výjimka unikající z QThread.run = qFatal -> pád celé aplikace (PyQt6). Chyba modelu,
        # sítě (HF Hub) nebo dat hledání se projeví jen jako „žádné výsledky“.
        query = str(self.query_text or "").strip()
        try:
            result = search_engine.run_search(query, self.universe, self.embeddings_cache)
        except Exception as e:
            print(f"[AI Hledání] {type(e).__name__}: {e}")
            result = search_engine.SearchResult(query, [], [], [], False)
        self.search_finished.emit(result, self.query_text)


SEARCH_WARMUP_DELAY_MS = 5000   # textový model hledání se načte v klidu po startu, ne při 1. dotazu


class SearchModelWarmup(QThread):
    """Načte textový model (sentence-transformers, ~5 s) dřív, než ho uživatel potřebuje."""

    def run(self):
        try:
            from ai_engine import get_text_model, concept_bank_vectors
            concept_bank_vectors(get_text_model())    # i banka pojmů kalibrace hledání
        except Exception as e:          # bez modelu hledání vrátí prázdný výsledek, nespadne
            print(f"[AI Hledání] Model se nenačetl: {type(e).__name__}: {e}")


# ── Map bridge ────────────────────────────────────────────────────────────────

MAP_HOLD_TTL_S = 20.0       # nejdelší pohyb kamery mapy bez nového hlášení


class MapBridge(QObject):
    def __init__(self, main_window):
        super().__init__()
        self.main_window = main_window

    @pyqtSlot(int)
    def openPhotoFromMap(self, item_id):
        QTimer.singleShot(30, lambda: self.main_window.open_viewer_by_id(int(item_id)))

    @pyqtSlot(str, bool)
    def setInteraction(self, reason, active):
        """Mapa hlásí pohyb kamery / běžící Cestu -> práce na pozadí počká (governor).
        Pohyb kamery má pojistku: bez „konec“ se hold sám uvolní (dřív visel navždy, když
        moveend přišel během Cesty, a démon pak do restartu stál)."""
        if reason in ("map", "journey"):
            ttl = MAP_HOLD_TTL_S if reason == "map" else None
            governor.get().set_active(reason, bool(active), ttl_s=ttl)

    def release_all(self):
        """Stránka mapy zmizela / načetla se znovu: její holdy neplatí."""
        for reason in ("map", "journey"):
            governor.get().set_active(reason, False)

    @pyqtSlot(str)
    def setMapLayer(self, name):
        """Přepínač podkladů v mapě -> zapamatovat volbu (config.json)."""
        if name in MAP_LAYERS and hasattr(self.main_window, "set_map_layer"):
            self.main_window.set_map_layer(name)

    @pyqtSlot(str)
    def openPhotosFromMap(self, ids_json):
        """Shluk na stejném místě: prohlížeč nad všemi jeho fotkami."""
        try:
            ids = [int(i) for i in json.loads(ids_json)]
        except (TypeError, ValueError):
            return
        QTimer.singleShot(30, lambda: self.main_window.open_viewer_for_ids(ids))

    @pyqtSlot(int)
    def requestJourney(self, event_id):
        """Výběr Cesty v mapě -> data zastávek zpět do window.playJourney()."""
        if hasattr(self.main_window, "play_journey"):
            QTimer.singleShot(0, lambda: self.main_window.play_journey(int(event_id)))

    @pyqtSlot()
    def journeyClosed(self):
        """Uživatel Cestu ukončil (×, Hotovo, Esc) -> případný návrat na stránku, odkud ji spustil."""
        if hasattr(self.main_window, "journey_closed"):
            QTimer.singleShot(0, self.main_window.journey_closed)


# ── Background AI daemon ──────────────────────────────────────────────────────

PROXY_WORK_SHARE = 0.8      # u videa s převodem: podíl práce na proxy (zbytek = náhledový pás)
_housekeeping_done = False  # záloha + úklid jen při prvním průchodu démona za běh

class BackgroundAIDaemon(QThread):
    people_updated     = pyqtSignal()
    embeddings_updated = pyqtSignal()          # vše znovu (migrace starého formátu)
    embeddings_added   = pyqtSignal(list)      # jen tato id (průběžné doplňování)
    thumbs_repaired    = pyqtSignal()
    face_crops_repaired = pyqtSignal(int)
    status_changed     = pyqtSignal(str)

    def __init__(self, face_threshold: float = 0.62, parent=None, merge_threshold=None):
        super().__init__(parent)
        self._running = True
        self._last_status = 0.0
        self.face_threshold = face_threshold
        self.merge_threshold = merge_threshold     # None = face_cluster.MERGE_THRESHOLD
        self.preview_budget_mb = preview_cache.DEFAULT_BUDGET_MB
        self.proxy_budget_mb = video_proxy.DEFAULT_BUDGET_MB

    def stop(self):
        self._running = False

    def _yield(self):
        """should_stop pro každou položku práce: během interakce (video, gesto, Cesta) čeká
        na klid (governor), na slabším PC dává UI pauzu. True = skončit."""
        return governor.get().checkpoint(lambda: not self._running)

    def _status(self, text, force=False):
        now = time.monotonic()
        if force or now - self._last_status >= 0.25:
            self._last_status = now
            self.status_changed.emit(text)

    def _housekeeping(self):
        """Jednou za běh aplikace: denní záloha knihovny a úklid polotovarů po pádu (Sprint R)."""
        global _housekeeping_done
        if _housekeeping_done:
            return
        _housekeeping_done = True
        try:
            import database as _db
            import paths as _paths
            if _db.backup_database(_paths.BACKUP_DIR):
                print("[Backup] denní záloha knihovny hotová")
            swept = video_proxy.sweep_temp()
            if swept:
                print(f"[Housekeeping] smazáno {swept} rozpracovaných souborů po přerušeném běhu")
        except Exception as e:
            print(f"[Housekeeping] {e}")

    def run(self):
        try:
            self._housekeeping()
            self._repair_thumbnails()
            if self._running:
                self._migrate_embeddings()
            if self._running:
                self._refresh_embeddings()       # hledání: miniatury už jsou, embeddingy hned po nich
            if self._running:
                self._repair_face_crops()
            if self._running:
                self._compute_phashes()
            if self._running:
                self._build_previews()
            if self._running:
                self._analyze_faces()
            if self._running:
                self._build_video_derivatives()
            if self._running:
                self._index_video_frames()           # až po proxy: snímky z rychlé SDR kopie
            if self._running:
                self._improve_person_covers()
        except Exception as e:
            print(f"AI Daemon error: {e}")
        self.status_changed.emit("")

    def _refresh_embeddings(self):
        """Embeddingy hledání pro položky bez platného (chybí / bez podpisu zdroje / změněný
        soubor). Zdroj = miniatura (už otočená podle EXIF, 360 px – CLIP pracuje s 224 px), jinak
        originál fotky. Průběžně hlásí embeddings_updated, ať hledání zlepšuje hned."""
        try:
            rows = get_media_needing_embedding()
        except Exception as e:
            print(f"[Embeddings] {e}")
            return
        total, batch = len(rows), []
        for i, (media_id, file_path, thumb_path, media_type, sig) in enumerate(rows):
            if self._yield():
                return
            self._status(f"Připravuji chytré hledání ({i + 1}/{total})...")
            src = thumb_path if thumb_path and os.path.exists(thumb_path) else (
                file_path if media_type != "video" else None)
            emb = compute_image_embedding(src) if src else None
            if emb is None:
                continue
            try:
                save_image_embedding(media_id, emb, sig)
            except Exception as e:
                print(f"[Embeddings] {media_id}: {e}")
                continue
            batch.append(media_id)
            if len(batch) >= 25:
                self.embeddings_added.emit(batch)    # jen tyto položky – ne celá knihovna znovu
                batch = []
        if batch:
            self.embeddings_added.emit(batch)

    def _build_video_derivatives(self):
        """Videa předem: 1080p SDR proxy pro 4K / HDR (tažení, náhledy, Cesta, slabší PC) a
        náhledový pás z ní. Nikdy během přehrávání – ffmpeg stojí, dokud je rušno (governor)."""
        prof = perf_profile.current()
        try:
            conn = get_connection()
            try:
                videos = [r[0] for r in conn.execute(f"""
                    SELECT m.file_path FROM media_items m
                    WHERE m.media_type = 'video' AND {visible_media_sql('m')}
                    ORDER BY m.date_taken DESC, m.id DESC""")]
            finally:
                conn.close()
        except Exception as e:
            print(f"[Proxy] {e}")
            return
        # Jen videa, kterým něco chybí – a jejich délky předem: průběh je vážený délkou (dřív
        # „(i/n)“ přes VŠECHNA videa, kde hotová přeskočila hned a jeden 4K HDR převod pak
        # minuty stál na stejném čísle bez známky života)
        pending = [p for p in videos if p and os.path.exists(p)
                   and not (video_proxy.is_settled(p) and scrub_strip.has_valid_strip(p))]
        if not pending:
            return
        weights = []
        for p in pending:
            if not self._running:
                return
            weights.append(scrub_strip.probe_duration_ms(p))
        prep = video_proxy.PrepProgress(weights)
        gov = governor.get()

        def report(k, frac):
            self._status(prep.update(k, frac, paused=gov.busy()))

        for k, path in enumerate(pending):
            report(k, 0.0)
            if self._yield():
                return
            needs_proxy = not video_proxy.is_settled(path)
            share = PROXY_WORK_SHARE if needs_proxy else 0.0      # převod je většina práce
            if needs_proxy:
                made = video_proxy.build_proxy(path, gov, self._yield, threads=prof.bg_threads,
                                               progress=lambda f, k=k: report(k, share * f))
                if made is False:
                    return                                    # zrušeno (konec aplikace)
                if made:
                    video_proxy.enforce_budget(self.proxy_budget_mb, keep=[made])
            if self._yield():
                return
            if not scrub_strip.has_valid_strip(path):
                scrub_strip.build_strip_now(path, self._yield, dense=prof.dense_strips,
                                            threads=max(1, prof.bg_threads),
                                            progress=lambda f, k=k: report(k, share + (1 - share) * f))
            report(k, 1.0)
        self._status("", force=True)

    def _index_video_frames(self):
        """Hledání ve videích podle celého klipu: FRAMES_MAX snímků -> CLIP vektory
        (video_frame_embeddings). Plakát ukazuje jen začátek – vodopád v polovině klipu
        dřív hledání nenašlo."""
        try:
            rows = get_videos_needing_frames()
        except Exception as e:
            print(f"[VideoFrames] {e}")
            return
        total, batch = len(rows), []
        for i, (media_id, path, sig) in enumerate(rows):
            if self._yield():
                return
            self._status(f"Připravuji hledání ve videích ({i + 1}/{total})...")
            import ai_engine
            # ffmpeg ve strážci: během přehrávání / gest stojí (dřív běžel naplno vedle videa)
            frames = video_frames.extract_frames(path, should_stop=lambda: not self._running,
                                                 governor=governor.get())
            if not self._running:
                return
            try:
                vecs = ai_engine.embed_pil([img for _ms, img in frames]) if frames else []
                save_video_frame_embeddings(media_id, list(zip([ms for ms, _ in frames], vecs)), sig)
            except Exception as e:
                print(f"[VideoFrames] {media_id}: {e}")
                continue
            batch.append(media_id)
            if len(batch) >= 5:
                self.embeddings_added.emit(batch)
                batch = []
        if batch:
            self.embeddings_added.emit(batch)

    def _build_previews(self):
        """Náhledy 2048 px a plakáty videí na pozadí, v pořadí podle užitku: kandidáti duplicit,
        obálky událostí, pak celá knihovna od nejnovějších. Nakonec LRU úklid na rozpočet."""
        try:
            for path, mtype in preview_build_order():
                if self._yield():
                    return
                if preview_cache.get_preview(path, touch=False) is None:
                    self._status("Připravuji náhledy fotek...")
                    preview_cache.build_preview(path, mtype)
            preview_cache.enforce_budget(self.preview_budget_mb)
        except Exception as e:
            print(f"[Previews] {e}")

    def _improve_person_covers(self):
        """Jednorázově doplní natočení hlavy starším obličejům a vybere osobám nejlepší portrét
        (čelní, ostrý, typický, bez dalšího obličeje v záběru)."""
        try:
            pending = face_engine.count_faces_without_frontality()
            if pending:
                face_engine.ensure_ready()
                self._status(f"Kontroluji obličeje ({pending})...", force=True)
                face_engine.analyze_face_crops(should_stop=self._yield)
            # Anatomická brána poslala do karantény obličej, který patří osobě (týl hlavy, kámen)
            # -> přeshlukovat, aby se z nich nestaly „osoby“
            if self._running and count_quarantined_assigned():
                self._status("Přeshlukovávám obličeje...", force=True)
                strict_recluster_faces(threshold=self.face_threshold, merge_threshold=self.merge_threshold)
                self.people_updated.emit()
            if self._running and refresh_person_covers():
                self.face_crops_repaired.emit(0)     # stejné obnovení avatarů jako po opravě ořezů
        except Exception as e:
            print(f"[Covers] {e}")

    def _repair_face_crops(self):
        """Jednorázově přeřízne deformované (protažené) ořezy obličejů u okraje fotek."""
        try:
            self._status("Kontroluji ořezy obličejů...", force=True)
            fixed = face_engine.repair_stretched_face_crops(should_stop=self._yield)
            if fixed and self._running:
                self.face_crops_repaired.emit(fixed)
        except Exception as e:
            print(f"[FaceCrops] {e}")

    def _repair_thumbnails(self):
        created = repair_missing_thumbnails(
            should_stop=self._yield,
            progress=lambda i, n: self._status(f"Obnovuji náhledy ({i}/{n})..."),
        )
        if created and self._running:
            self.thumbs_repaired.emit()

    def _compute_phashes(self):
        """Otisky pro duplicity: nové položky i přepočet zastaralých otisků (PHASH_VERSION)."""
        try:
            refresh_phashes(
                should_stop=self._yield,
                progress=lambda i, n: self._status(f"Počítám otisky pro duplicity ({i}/{n})..."),
            )
        except Exception as e:
            print(f"[pHash] {e}")

    def _migrate_embeddings(self):
        converted, dropped = migrate_legacy_embeddings()
        if (converted or dropped) and self._running:
            self.embeddings_updated.emit()

    def _analyze_faces(self):
        pending = get_media_pending_face_scan()
        if not pending:
            return
        try:
            face_engine.ensure_ready()
        except Exception as e:
            print(f"Rozpoznávání tváří není dostupné: {e}")
            return
        new_faces_found = False
        total = len(pending)
        for idx, (media_id, file_path) in enumerate(pending):
            if self._yield():
                return
            if not file_path or not os.path.exists(file_path):
                continue
            self._status(f"Analýza tváří ({idx+1}/{total})...")
            try:
                faces = face_engine.detect_and_extract_faces(media_id, file_path)
                save_faces_for_media(media_id, faces)
                if faces:
                    new_faces_found = True
                # embedding hledání už nevzniká tady (dřív jen jednou s obličeji a bez podpisu
                # zdroje – po re-importu pod novým id zůstal u cizí fotky): _refresh_embeddings
            except Exception as e:
                print(f"[AI] {file_path}: {e}")
        if new_faces_found and self._running:
            self._status("Shlukovávám obličeje...", force=True)
            try:
                strict_recluster_faces(threshold=self.face_threshold,
                                       merge_threshold=self.merge_threshold)
            except Exception as e:
                print(f"[Cluster] {e}")
            self.people_updated.emit()


# ── Kontroly (duplicity, hraniční obličeje) jako stránky v okně ───────────────

class ScaledImageLabel(QLabel):
    """Obrázek přizpůsobený velikosti štítku (celoplošné stránky místo pevných 280 px)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_source(self, pixmap):
        self._source = pixmap if pixmap is not None else QPixmap()
        if self._source.isNull():
            self.setPixmap(QPixmap())
        self._rescale()

    def setText(self, text):
        self._source = QPixmap()
        super().setText(text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self):
        if self._source.isNull():
            return
        dpr = self.devicePixelRatioF()
        scaled = self._source.scaled(int(self.width() * dpr), int(self.height() * dpr),
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(dpr)
        super().setPixmap(scaled)


REVIEW_IMAGE_MIN_EDGE = 1600     # porovnání duplicit: ostrý obraz i na velkém / HiDPI displeji
REVIEW_PREFETCH_PAIRS = 2        # kolik dalších párů dekódovat dopředu
REVIEW_CACHE_SIZE = 12


class ReviewView(QWidget):
    """Základ kontrolního průvodce: běžný widget vkládaný do okna (DetailPage), ne QDialog.
    accept() ohlásí dokončení signálem finished_review – hlavní okno se pak vrátí zpět.

    Vzhled podle Kairos: kreslená tlačítka KButton (primary / secondary / ghost / danger_outline)
    místo sytě zelených a červených QSS ploch, barvy a písmo z tokens (i ve světlém motivu)."""
    finished_review = pyqtSignal()
    MARGINS = (32, 24, 32, 24)
    ACTION_HEIGHT = 36

    def accept(self):
        self.finished_review.emit()

    def _review_button(self, text, variant, tooltip=None, icon=None):
        btn = KButton(text, variant=variant, icon=icon)
        btn.setFixedHeight(self.ACTION_HEIGHT)
        btn.setMinimumWidth(120)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tooltip:
            btn.setToolTip(tooltip)
        return btn

    def _review_label(self, role, color_token, align=Qt.AlignmentFlag.AlignCenter):
        lbl = QLabel()
        lbl.setAlignment(align)
        lbl.setFont(tokens.font(role))
        lbl.setStyleSheet(f"color:{tokens.palette()[color_token]}; background:transparent;")
        return lbl

    @staticmethod
    def _media_well_style():
        t = tokens.palette()
        return (f"background:{t['surface_2']}; border:1px solid {t['hairline']}; "
                f"border-radius:{tokens.RADIUS['lg']}px;")

    @staticmethod
    def _action_row(*buttons, stretch=True):
        row = QHBoxLayout()
        row.setSpacing(8)
        if stretch:
            row.addStretch(1)
        for b in buttons:
            row.addWidget(b)
        if stretch:
            row.addStretch(1)
        return row


# ── P3.5 Duplicate Review ─────────────────────────────────────────────────────

class DuplicateReviewView(ReviewView):
    title = "Duplicitní fotografie"

    def __init__(self, pairs, parent=None):
        super().__init__(parent)
        # Očekává seznam n-tic (id_a, path_a, id_b, path_b, hamming) z get_duplicate_pairs();
        # cokoli jiného (např. int z detect_duplicates_in_db) = žádné páry, ne pád.
        self.pairs = [p for p in pairs if len(p) == 5] if isinstance(pairs, (list, tuple)) else []
        self.current_idx = 0
        self.changed = False      # něco se přesunulo do koše -> hlavní okno obnoví data
        self._trashed = set()     # id přesunutá do koše v této kontrole (jejich páry se přeskočí)
        self._history = []        # [(index páru, (druh, data))] pro "Zpět"
        self._load_token = 0      # pozdě doběhlé načtení předchozího páru se zahodí
        self._label_path = {}     # štítek -> cesta, jejíž ostrý snímek čeká
        # Dekódování mimo GUI vlákno + cache + přednačtení dalších párů (porovnání je pak hned ostré)
        self._images = ImagePipeline(self._review_size, capacity=REVIEW_CACHE_SIZE, threads=3, parent=self)
        self._images.loaded.connect(self._on_pipeline_image)
        self._build_ui()
        self._load_pair()

    def trashed_count(self):
        return len(self._trashed)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(*self.MARGINS)
        root.setSpacing(16)
        self.lbl_title = self._review_label("headline", "text")
        root.addWidget(self.lbl_title)
        img_row = QHBoxLayout()
        img_row.setSpacing(20)
        self.lbl_a = ScaledImageLabel()
        self.lbl_a.setStyleSheet(self._media_well_style())
        self.lbl_b = ScaledImageLabel()
        self.lbl_b.setStyleSheet(self._media_well_style())
        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        self.lbl_info_a = self._review_label("callout", "text_secondary")
        left_col.addWidget(self.lbl_a, 1)
        left_col.addWidget(self.lbl_info_a)
        right_col = QVBoxLayout()
        right_col.setSpacing(8)
        self.lbl_info_b = self._review_label("callout", "text_secondary")
        right_col.addWidget(self.lbl_b, 1)
        right_col.addWidget(self.lbl_info_b)
        img_row.addLayout(left_col)
        img_row.addLayout(right_col)
        root.addLayout(img_row, 1)
        # Doporučené ponechání (A = nejostřejší) je jediná plná akce; ostatní tlumené sklo
        self.btn_keep_a = self._review_button("Ponechat levé", "primary", "Pravou položku přesune do koše")
        self.btn_keep_b = self._review_button("Ponechat pravé", "secondary", "Levou položku přesune do koše")
        self.btn_keep_both = self._review_button("Ponechat obě", "secondary",
                                                 "Nejsou to duplicity – pár se už nenabídne")
        self.btn_skip = self._review_button("Přeskočit", "ghost")
        self.btn_keep_a.clicked.connect(lambda: self._action("keep_a"))
        self.btn_keep_b.clicked.connect(lambda: self._action("keep_b"))
        self.btn_keep_both.clicked.connect(lambda: self._action("keep_both"))
        self.btn_skip.clicked.connect(lambda: self._action("skip"))
        root.addLayout(self._action_row(self.btn_keep_a, self.btn_keep_b, self.btn_keep_both, self.btn_skip))
        self.lbl_hint = self._review_label("callout", "text_tertiary")
        self.lbl_hint.setText("Nechtěná položka se přesune do koše (Fotografie › Smazané). "
                              "Soubor na disku zůstává a lze ji obnovit. "
                              "Klávesy: ← / → předchozí / další pár, Ctrl+Z vrátit, Esc zavřít.")
        self.lbl_hint.setWordWrap(True)
        root.addWidget(self.lbl_hint)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        self.btn_undo = KButton("Vrátit", variant="ghost", compact=True)
        self.btn_undo.setToolTip("Vrátí poslední rozhodnutí (Ctrl+Z)")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self._undo)
        nav_row.addWidget(self.btn_undo)
        nav_row.addStretch(1)
        self.btn_prev = KButton("‹ Předchozí", variant="secondary", compact=True)
        self.btn_next = KButton("Další ›", variant="secondary", compact=True)
        self.lbl_counter = self._review_label("callout", "text_secondary")
        self.lbl_counter.setMinimumWidth(64)
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next.clicked.connect(self._next)
        nav_row.addWidget(self.btn_prev)
        nav_row.addWidget(self.lbl_counter)
        nav_row.addWidget(self.btn_next)
        nav_row.addStretch(1)
        nav_row.addSpacing(self.btn_undo.sizeHint().width())   # počítadlo přesně uprostřed
        root.addLayout(nav_row)
        # Klávesy (šipky, Ctrl+Z, Esc) dostává celá kontrola, ne naposledy kliknuté tlačítko
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for btn in self.findChildren(QPushButton):
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _load_pair(self):
        if not self.pairs:
            self.lbl_title.setText("Žádné duplicity nenalezeny.")
            return
        n = len(self.pairs)
        i = self.current_idx
        self.lbl_counter.setText(f"{i+1} / {n}")
        self.btn_prev.setEnabled(self._neighbor(-1) is not None)
        self.btn_next.setEnabled(self._neighbor(+1) is not None)
        pair = self.pairs[i]
        id_a, path_a, id_b, path_b, hamming = pair
        self.lbl_title.setText(f"Možný duplikát  (Hammingova vzdálenost: {hamming})")
        self._load_token += 1
        self._set_thumb(self.lbl_a, id_a, path_a)
        self._set_thumb(self.lbl_b, id_b, path_b)
        # A je z get_duplicate_pairs nejostřejší (pak největší) položka skupiny
        self.lbl_info_a.setText((os.path.basename(path_a) if path_a else "?") + "  •  doporučeno ponechat")
        self.lbl_info_b.setText(os.path.basename(path_b) if path_b else "?")

    def _set_thumb(self, label, media_id, path):
        """Okamžitě miniatura z DB (náhled), pak na pozadí plné rozlišení originálu.
        Dřív: HEIC/DNG Qt nenačetl -> zůstala rozmazaná 360px miniatura a porovnání ostrosti
        dvou téměř stejných fotek nemělo smysl."""
        label.full_res = False
        thumb = None
        try:
            conn = get_connection()
            try:
                row = conn.execute("SELECT thumbnail_path FROM media_items WHERE id=?", (media_id,)).fetchone()
            finally:
                conn.close()
            thumb = row[0] if row and row[0] and os.path.exists(row[0]) else None
        except Exception as e:
            print(f"[DupDialog] thumb: {e}")
        has_original = bool(path) and os.path.exists(path)
        self._label_path[label] = path if has_original else None
        cached = self._images.request(path) if has_original else None
        if cached is not None:                       # přednačteno -> rovnou ostré
            label.set_source(QPixmap.fromImage(cached))
            label.full_res = True
        elif thumb:
            label.set_source(QPixmap(thumb))
        elif has_original:
            label.setText("Načítám…")
        else:
            label.setText("Obrázek nenalezen")
        if label is self.lbl_b:
            self._prefetch_following()

    def _review_size(self):
        # Velikost náhledové vrstvy: z cache se čte ~25 ms; chybí-li, první dekód ji zároveň vytvoří
        return QSize(PREVIEW_EDGE, PREVIEW_EDGE)

    def _prefetch_following(self):
        """Další páry (originály) se dekódují na pozadí, než se k nim uživatel dostane."""
        paths = []
        i = self.current_idx
        for _ in range(REVIEW_PREFETCH_PAIRS):
            i = self._neighbor_from(i, +1)
            if i is None:
                break
            paths += [p for p in (self.pairs[i][1], self.pairs[i][3]) if p]
        self._images.prefetch(paths)

    def _neighbor_from(self, idx, step):
        i = idx + step
        while 0 <= i < len(self.pairs) and self._is_resolved(i):
            i += step
        return i if 0 <= i < len(self.pairs) else None

    def _on_pipeline_image(self, path, image):
        for label in (self.lbl_a, self.lbl_b):
            if self._label_path.get(label) == path:
                label.set_source(QPixmap.fromImage(image))
                label.full_res = True

    def _on_full_image(self, token, key, image):
        """Zpětná kompatibilita (starší rozhraní testů): výsledek pro pár s tokenem."""
        if token != self._load_token or image.isNull():
            return
        label = self.lbl_a if key == "a" else self.lbl_b
        label.set_source(QPixmap.fromImage(image))
        label.full_res = True

    def wait_for_images(self, msecs=10000):
        """Testy / skripty: počkat na plné rozlišení (i přednačtených párů)."""
        self._images.wait(msecs)

    def _action(self, action):
        """Ponechat levé/pravé = druhou položku do koše; Ponechat obě = pár už nenabízet."""
        if not self.pairs:
            return
        id_a, _, id_b, _, _ = self.pairs[self.current_idx]
        try:
            if action == "keep_a":
                trash_media([id_b])
                entry = ("trash", id_b)
            elif action == "keep_b":
                trash_media([id_a])
                entry = ("trash", id_a)
            elif action == "keep_both":
                dismiss_duplicate_pair(id_a, id_b)
                entry = ("dismiss", (id_a, id_b))
            else:
                entry = ("skip", None)
        except Exception as e:
            show_toast(self, f"Akci se nepodařilo uložit: {e}", "error")
            return
        if entry[0] == "trash":
            self._trashed.add(entry[1])
        if entry[0] != "skip":
            self.changed = True
        self._history.append((self.current_idx, entry))
        self.btn_undo.setEnabled(True)
        self._next()

    def _undo(self):
        if not self._history:
            return
        idx, (kind, data) = self._history.pop()
        try:
            if kind == "trash":
                restore_media([data])
                self._trashed.discard(data)
            elif kind == "dismiss":
                undismiss_duplicate_pair(*data)
        except Exception as e:
            show_toast(self, f"Akci se nepodařilo vrátit: {e}", "error")
            return
        self.current_idx = idx
        self.btn_undo.setEnabled(bool(self._history))
        self._load_pair()

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Left:
            self._prev()
        elif key == Qt.Key.Key_Right:
            if self._neighbor(+1) is not None:
                self._next()
        elif key == Qt.Key.Key_Z and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._undo()
        else:
            super().keyPressEvent(event)     # Esc propadne do DetailPage = zpět
            return
        event.accept()

    def _is_resolved(self, i):
        """Pár, jehož položka už je v koši, se nenabízí (např. 2. pár téže skupiny)."""
        id_a, _, id_b, _, _ = self.pairs[i]
        return id_a in self._trashed or id_b in self._trashed

    def _neighbor(self, step):
        i = self.current_idx + step
        while 0 <= i < len(self.pairs) and self._is_resolved(i):
            i += step
        return i if 0 <= i < len(self.pairs) else None

    def _prev(self):
        i = self._neighbor(-1)
        if i is not None:
            self.current_idx = i
            self._load_pair()

    def _next(self):
        i = self._neighbor(+1)
        if i is not None:
            self.current_idx = i
            self._load_pair()
        else:
            self.accept()


DuplicateReviewDialog = DuplicateReviewView   # původní název (testy P3.5 / C7)


# ── P3.6 Borderline Review ────────────────────────────────────────────────────

class BorderlineReviewView(ReviewView):
    def __init__(self, person_id, person_name, borderline_faces, parent=None):
        super().__init__(parent)
        self.person_id = person_id
        self.person_name = person_name
        self.faces = list(borderline_faces)
        self.current_idx = 0
        self.assigned = 0          # kolik obličejů uživatel potvrdil (hlavní okno obnoví osoby)
        self.title = f"Hraniční obličeje – {person_name}"
        self._build_ui()
        self._load_face()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(*self.MARGINS)
        root.setSpacing(16)
        root.addStretch(1)
        self.lbl_title = self._review_label("title", "text")
        root.addWidget(self.lbl_title)
        img_row = QHBoxLayout()
        img_row.setSpacing(56)
        img_row.addStretch(1)
        ring = (f"background:{tokens.palette()['surface_2']}; border:1px solid {tokens.palette()['hairline']}; "
                f"border-radius:100px;")
        left = QVBoxLayout()
        left.setSpacing(10)
        self.lbl_profile = self._review_label("caption", "text_tertiary")
        self.lbl_profile.setText("PROFIL OSOBY")
        self.img_profile = QLabel()
        self.img_profile.setFixedSize(200, 200)
        self.img_profile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_profile.setStyleSheet(ring)
        left.addWidget(self.img_profile)
        left.addWidget(self.lbl_profile)
        right = QVBoxLayout()
        right.setSpacing(10)
        self.lbl_candidate_info = self._review_label("caption", "text_tertiary")
        self.lbl_candidate_info.setText("KANDIDÁT")
        self.img_candidate = QLabel()
        self.img_candidate.setFixedSize(200, 200)
        self.img_candidate.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_candidate.setStyleSheet(ring)
        right.addWidget(self.img_candidate)
        right.addWidget(self.lbl_candidate_info)
        img_row.addLayout(left)
        img_row.addLayout(right)
        img_row.addStretch(1)
        root.addLayout(img_row)
        self.lbl_sim = self._review_label("body", "text_secondary")
        root.addWidget(self.lbl_sim)
        root.addSpacing(4)
        self.btn_yes = self._review_button("Ano, je to stejná osoba", "primary")
        self.btn_no = self._review_button("Ne, jiná osoba", "danger_outline")
        self.btn_skip = self._review_button("Přeskočit", "ghost")
        self.btn_yes.clicked.connect(self._assign_yes)
        self.btn_no.clicked.connect(self._assign_no)
        self.btn_skip.clicked.connect(self._next)
        root.addLayout(self._action_row(self.btn_yes, self.btn_no, self.btn_skip))
        self.lbl_counter = self._review_label("callout", "text_tertiary")
        root.addWidget(self.lbl_counter)
        root.addStretch(1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _load_face(self):
        if not self.faces:
            self.lbl_title.setText("Žádné hraniční obličeje.")
            return
        n = len(self.faces)
        i = self.current_idx
        self.lbl_counter.setText(f"{i+1} / {n}")
        face = self.faces[i]
        face_id = face.get("face_id")
        crop_path = face.get("crop_path", "")
        similarity = face.get("similarity", 0.0)
        self.lbl_title.setText(f"Je tento obličej {self.person_name}?")
        self.lbl_sim.setText(f"Podobnost {round(similarity * 100)} %  ·  hraniční pásmo")
        self.lbl_candidate_info.setText(f"Obličej #{face_id}")
        self._set_round_thumb(self.img_candidate, crop_path)
        try:
            from face_engine import reconstruct_best_crop_for_person
            profile_path = reconstruct_best_crop_for_person(self.person_id)
            self._set_round_thumb(self.img_profile, profile_path)
        except Exception:
            self.img_profile.setText("?")

    def _set_round_thumb(self, label, path):
        img = _load_avatar_image(path, 200) if path and os.path.exists(path) else QImage()
        if not img.isNull():                       # stejný středový čtvercový ořez jako karty Osob
            label.setPixmap(QPixmap.fromImage(img))
        else:
            label.setText("?")

    def _assign_yes(self):
        if not self.faces:
            return
        face = self.faces[self.current_idx]
        face_id = face.get("face_id")
        try:
            assign_face_to_person(face_id, self.person_id)
            self.assigned += 1
        except Exception as e:
            show_toast(self, f"Obličej se nepodařilo přiřadit: {e}", "error")
            return
        self._next()

    def _assign_no(self):
        self._next()

    def _next(self):
        if self.current_idx < len(self.faces) - 1:
            self.current_idx += 1
            self._load_face()
        else:
            self.accept()


BorderlineReviewDialog = BorderlineReviewView  # původní název (testy P3.6)


# ── Fáze 0: překryv se statistikou snímků ─────────────────────────────────────

class FrameStatsOverlay(QLabel):
    """Ctrl+Shift+F: p50 / p95 / p99 a vynechané snímky všech měřáků (mřížka, prohlížeč, video,
    mapa, Cesta), výkonnostní profil a co právě drží práci na pozadí (governor)."""

    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        f = QFont("Consolas")
        f.setPixelSize(12)
        self.setFont(f)
        self.setStyleSheet("QLabel { background: rgba(10, 10, 14, 215); color: #E8E8F0; "
                           "border-radius: 8px; padding: 8px 10px; }")
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self.hide()

    def toggle(self):
        if self.isVisible():
            self._timer.stop()
            self.hide()
        else:
            self._refresh()
            self.show()
            self.raise_()
            self._timer.start()

    def _refresh(self):
        view = getattr(self._window, "map_view", None)
        if view is not None and getattr(self._window, "_map_loaded", False):
            view.page().runJavaScript("window.getFrameStats ? window.getFrameStats() : null", self._on_map_stats)
        prof = perf_profile.current()
        lines = [f"Kairos · {prof.tier} · {prof.refresh_hz:.0f} Hz (rozpočet {prof.frame_budget_ms:.1f} ms)"]
        lines += [frame_meter.format_stats(s) for s in frame_meter.all_stats()]
        held = governor.get().reasons()
        lines.append(f"pozadí: {'pozastaveno (' + ', '.join(held) + ')' if held else ('klid' if not governor.get().busy() else 'čeká na klid')}")
        self.setText("\n".join(lines))
        self.adjustSize()
        self.move(self._window.width() - self.width() - 16, 56)

    @staticmethod
    def _on_map_stats(stats):
        for name, s in (stats or {}).items():
            if isinstance(s, dict):
                frame_meter.record_external(name, s)


# ── MainWindow ────────────────────────────────────────────────────────────────

# Styl hlavního okna se generuje z tokenů pro aktuální režim a zvýraznění (tokens.build_qss).


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.setWindowTitle(brand.APP_NAME)
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        # Adaptivní škálování: profil stroje (strop kvality, zátěž pozadí, meze mapy)
        self.perf = perf_profile.detect(self.config.get("performance_tier"))
        governor.get().set_item_pause(self.perf.bg_item_pause_s)

        # State
        self._all_records = []
        self._trash_records = []
        self._filtered_records = []
        self._map_loaded = False
        self._pending_journey = None     # data Cesty čekající na načtení mapy
        self._journey_return = False     # mapa s Cestou vložená nad událost (návrat po konci)
        self._tint_job = None            # tint.TintJob – odstíny obálek událostí na pozadí
        self._current_filter = "all"
        self._current_sort = "date_desc"
        self._search_query = ""
        self._embeddings_cache = {}
        self._scan_start_time = None
        self._ai_daemon = None
        self._scanner = None
        self._search_workers = set()  # běžící AISearchWorker (nesmí zaniknout dřív než vlákno)
        self._search_warmup = None    # SearchModelWarmup (jednou po startu)
        self._search_result = None    # search_engine.SearchResult aktuálního dotazu
        self._search_sort_override = False
        self._person_card_pool = QThreadPool()
        self._person_card_pool.setMaxThreadCount(4)
        self._person_cards = {}  # person_id -> PersonCard
        self._people_rows = {}   # person_id -> (name, cover_path, počet fotek)
        self._selected_people = set()   # karty vybrané ke sloučení (Ctrl+klik)
        self._avatar_cache = LRUCache(512)  # (person_id, cover_path) -> QImage avatar
        self._viewer = None          # jediný prohlížeč v okně (vytvoří se při prvním otevření)
        self._viewer_model = None    # model mřížky, ze které se prohlížeč otevřel
        self._immersive = set()      # důvody celé obrazovky ("viewer", "slideshow")
        self._media_warmup = None    # jednorázové zahřátí FFmpeg po startu (MediaWarmUp)
        self._import_probe = None    # Patch 11: chytrý import (NewMediaProbe)
        self._background_import = []
        self._background_current = None    # položka fronty, kterou právě skenuje _scanner
        self._background_imported = 0
        self._was_maximized = False

        # P3.2 debounce timer
        self._search_debounce = QTimer()
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(400)
        self._search_debounce.timeout.connect(self._run_ai_search)

        self._build_ui()
        # Podstránky (prohlížeč, osoba, událost, kontroly) v témže okně místo dialogů
        self.nav = NavigationController(self.stack, self)
        self.toasts = ToastManager(self)
        self._back_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Back), self)
        self._back_shortcut.activated.connect(self._nav_back)
        self._help_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F1), self)
        self._help_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._help_shortcut.activated.connect(lambda: self.open_guide())
        # Fáze 0: měření doby snímku (Ctrl+Shift+F), rozpočet podle obnovovací frekvence displeje
        frame_meter.set_refresh(self.perf.refresh_hz)
        self._frame_stats = FrameStatsOverlay(self)
        self._frame_shortcut = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        self._frame_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._frame_shortcut.activated.connect(self._frame_stats.toggle)
        self._spotlight = None
        # Polish P3: přetažení složek / fotek kamkoli do okna + sledované složky
        self.drop_router = drop_import.DropRouter(self, self.centralWidget())
        self.drop_router.dropped.connect(self.import_dropped)
        self.folder_watcher = folder_watch.FolderWatcher(self)
        self.folder_watcher.changed.connect(self._on_watched_changed)
        self.folder_watcher.set_roots(self._watched_roots())
        self._apply_stylesheet()
        self._refresh_theme_icons()
        self._init_db_and_load()
        QTimer.singleShot(SEARCH_WARMUP_DELAY_MS, self._warm_search_model)

    def _nav_back(self):
        if self.nav.current() is self._viewer and self._viewer is not None:
            self._viewer.request_close()      # zastaví přehrávání a pak se vrátí
        else:
            self.nav.pop()

    def _apply_stylesheet(self):
        tokens.apply_stylesheet(self, "main")

    def _build_ui(self):
        central = QWidget()
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = self._build_sidebar()
        self.sidebar = sidebar
        root_layout.addWidget(sidebar)

        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)

        self.page_photos = self._build_photos_page()
        self.page_people = self._build_people_page()
        self.page_events = self._build_events_page()
        self.page_map = self._build_map_page()
        self.page_import = self._build_import_page()

        self.stack.addWidget(self.page_photos)   # 0
        self.stack.addWidget(self.page_people)   # 1
        self.stack.addWidget(self.page_events)   # 2
        self.stack.addWidget(self.page_map)      # 3
        self.stack.addWidget(self.page_import)   # 4

        self.stack.setCurrentIndex(0)
        self.stack.currentChanged.connect(self._sync_map_active)
        guide.mark(self.page_people, "people")         # kotvy tipů (guide.py) na celé stránky
        guide.mark(self.page_map, "map")

    # Postranní panel (Kairos dávka 3): stránky stacku a kolekce = filtry mřížky Fotografie
    SIDEBAR_PAGES = ("photos", "people", "events", "map")
    COLLECTION_FILTERS = {"favorites": "favorites", "videos": "video", "trash": "trash"}
    FILTER_TITLES = {"all": "Fotografie", "favorites": "Oblíbené", "photo": "Fotografie",
                     "video": "Videa", "trash": "Smazané"}

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setFixedWidth(212)
        # styl z tokenů (#sidebar): o stupeň tmavší než obsah, jemná dělicí linka
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 14, 10, 12)
        layout.setSpacing(2)

        self.brand_mark = brand.BrandMark()
        layout.addWidget(self.brand_mark)
        layout.addSpacing(6)

        self.sidebar_nav = SidebarNav([
            NavSection("Knihovna"),
            ("photos", "Fotografie", "photos"), ("people", "Osoby", "people"),
            ("events", "Události", "events"), ("map", "Mapa", "map"),
            NavSection("Kolekce"),
            ("favorites", "Oblíbené", "heart"), ("videos", "Videa", "video"), ("trash", "Smazané", "trash"),
        ])
        # Kairos Spark: kliknutí na hlavní záložku vyšle z jiskry loga světelný puls (~180 ms);
        # spojení před přepnutím stránky, ať animace startuje bez čekání na novou stránku
        self.spark_pulse = brand.SparkPulse(sidebar, self.brand_mark)
        self.sidebar_nav.current_changed.connect(
            lambda key: key in self.SIDEBAR_PAGES and self.spark_pulse.fire(self.sidebar_nav.item(key)))
        self.sidebar_nav.current_changed.connect(self._on_sidebar)
        self._nav_buttons = [self.sidebar_nav.item(k) for k in self.SIDEBAR_PAGES]   # index = stránka stacku
        self._nav_group = self.sidebar_nav.group
        layout.addWidget(self.sidebar_nav)

        self._nav_buttons[0].setChecked(True)
        layout.addStretch()

        self.btn_import = KButton("Importovat", icon="plus")
        self.btn_import.setObjectName("btn_primary")
        self.btn_import.clicked.connect(self._start_import)
        layout.addWidget(guide.mark(self.btn_import, "import"))
        layout.addSpacing(4)

        self.btn_settings = NavItem("Předvolby", icon="settings")
        self.btn_settings.setCheckable(False)
        self.btn_settings.clicked.connect(self._open_settings)
        layout.addWidget(self.btn_settings)
        self.btn_guide = NavItem("Tipy a průvodce", icon="sparkle")
        self.btn_guide.setCheckable(False)
        self.btn_guide.setToolTip("Zkratky, mapa, tváře a hledání (F1)")
        self.btn_guide.clicked.connect(lambda: self.open_guide())
        layout.addWidget(guide.mark(self.btn_guide, "guide"))

        # Kairos dávka 5: práce na pozadí s animovaným indikátorem (setText jako dřív QLabel)
        self.lbl_status = ActivityStatus()
        layout.addWidget(self.lbl_status)

        return sidebar

    @staticmethod
    def _make_toolbar(title, subtitle_label):
        """Lišta stránky: titulek a podtitulek vlevo, ovládání vpravo (styl #toolbar z tokenů)."""
        toolbar = QWidget()
        toolbar.setObjectName("toolbar")
        toolbar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        toolbar.setFixedHeight(64)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(20, 8, 16, 8)
        tb_layout.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        lbl_title = QLabel(title)
        lbl_title.setObjectName("pageTitle")
        subtitle_label.setObjectName("pageSubtitle")
        titles.addWidget(lbl_title)
        titles.addWidget(subtitle_label)
        tb_layout.addLayout(titles)
        tb_layout.addStretch()
        return toolbar, tb_layout, lbl_title

    @staticmethod
    def _make_search(placeholder):
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedHeight(30)
        edit.setMinimumWidth(200)
        edit.setMaximumWidth(300)
        icon_action = edit.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        return edit, icon_action

    def _build_photos_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.lbl_photo_count = QLabel("")
        toolbar, tb_layout, self.lbl_photos_title = self._make_toolbar("Fotografie", self.lbl_photo_count)

        # Kairos dávka 4: pohled podle času (Roky / Měsíce = karty, Dny = větší dlaždice s datem)
        self._time_view = "all"
        self._cell_before_days = None
        self.time_switch = SegmentedControl([("Roky", "years"), ("Měsíce", "months"), ("Dny", "days"),
                                             ("Vše", "all")], current="all")
        self.time_switch.changed.connect(self._set_time_view)
        tb_layout.addWidget(guide.mark(self.time_switch, "time_switch"))
        tb_layout.addSpacing(10)

        # P3.2 Search bar
        self.search_bar, self._search_icon = self._make_search("Hledat – třeba „moře“ nebo „pes“")
        self.search_bar.textChanged.connect(self._on_search_changed)
        tb_layout.addWidget(guide.mark(self.search_bar, "search"))

        self.btn_clear_search = KButton("", variant="ghost", compact=True, icon="x")
        self.btn_clear_search.setToolTip("Vymazat hledání")
        self.btn_clear_search.clicked.connect(self._clear_search)
        self.btn_clear_search.setVisible(False)
        tb_layout.addWidget(self.btn_clear_search)

        tb_layout.addSpacing(6)
        self.btn_slideshow = KButton("Prezentace", compact=True, icon="slideshow")
        self.btn_slideshow.setToolTip("Prezentace přes celou obrazovku od vybrané fotky (S)")
        self.btn_slideshow.clicked.connect(lambda: self.start_slideshow_from_grid(self.grid_view))
        tb_layout.addWidget(self.btn_slideshow)

        # Sort combo
        # Kairos kapsle s nabídkou místo nativního QComboBox (stejné API)
        self.sort_combo = MenuSelect(["Nejnovější", "Nejstarší", "Název A–Z", "Velikost"], icon="sort")
        self.sort_combo.setAccessibleName("Řazení")
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        tb_layout.addWidget(self.sort_combo)

        layout.addWidget(toolbar)

        # Polish P2: Vzpomínky „Před 3 lety“ – tichá karta nad mřížkou (jen Vše, bez hledání)
        self.memory_box = QWidget(page)
        box = QVBoxLayout(self.memory_box)
        box.setContentsMargins(20, 12, 20, 4)
        self.memory_card = memories.MemoryCard(self.memory_box)
        self.memory_card.opened.connect(self.open_viewer_for_ids)
        self.memory_card.dismissed.connect(self._dismiss_memory)
        box.addWidget(self.memory_card)
        self.memory_box.hide()
        layout.addWidget(self.memory_box)
        self._memory = None
        self._memory_job = None
        self._memory_midnight = QTimer(self)
        self._memory_midnight.setSingleShot(True)
        self._memory_midnight.timeout.connect(self._refresh_memory)

        # Grid view
        self.grid_view = AppleGridView(self, page)
        self.grid_view.item_activated.connect(self._on_photo_activated)
        self.grid_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid_view.customContextMenuRequested.connect(self._show_photo_context_menu)
        # Patch 11: najetí myší na video v mřížce (chvíli setrvat) = přednačíst do prohlížeče
        self._hover_row = -1
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(HOVER_PRELOAD_MS)
        self._hover_timer.timeout.connect(self._preload_hovered)
        self.grid_view.entered.connect(self._on_grid_hover)
        self.grid_view.verticalScrollBar().valueChanged.connect(self._update_day_capsule)
        self.day_capsule = HintPill(self.grid_view)
        # Kairos dávka 4: velikost dlaždic Ctrl + kolečkem / sevřením prstů (bez posuvníku)
        self.grid_view.set_cell_size(int(self.config.get("default_thumbnail_size", 180)))
        self.grid_view.zoom_changed.connect(self._remember_cell_size)
        self.moments_view = MomentsView()
        self.moments_view.card_clicked.connect(self._on_moment_clicked)
        self.photo_views = QStackedWidget()
        self.photo_views.addWidget(self.grid_view)
        self.photo_views.addWidget(self.moments_view)
        layout.addWidget(self.photo_views, 1)

        # Prázdný stav přes mřížku: koš, oblíbené, videa, hledání bez výsledků, prázdná knihovna.
        # Řídí ho signály modelu – filtr, hledání i mazání ho aktualizují samy.
        self._photos_empty_action = None
        self.photos_empty = EmptyState(parent=self.grid_view)
        self.photos_empty.action_triggered.connect(self._on_photos_empty_action)
        self.photos_empty.hide()
        pin_overlay(self.photos_empty, self.grid_view)
        model = self.grid_view.photo_model
        for signal in (model.modelReset, model.rowsInserted, model.rowsRemoved):
            signal.connect(self._update_photos_empty)
        model.modelReset.connect(self._refresh_moments)

        return page

    def _build_people_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.lbl_people_count = QLabel("")
        toolbar, tb_layout, _ = self._make_toolbar("Osoby", self.lbl_people_count)

        self.people_search, self._people_search_icon = self._make_search("Hledat osoby")
        self.people_search.textChanged.connect(self._filter_people)
        tb_layout.addWidget(self.people_search)
        tb_layout.addSpacing(6)

        # Sloučení vybraných karet (Ctrl+klik) – vidět jen při výběru 2+ osob
        self.btn_merge = KButton("Sloučit osoby", variant="primary", icon="merge")
        self.btn_merge.setToolTip("Sloučí vybrané osoby do jedné (Ctrl+klik vybírá karty)")
        self.btn_merge.clicked.connect(lambda: self._merge_people_ids(sorted(self._selected_people)))
        self.btn_merge.setVisible(False)
        tb_layout.addWidget(self.btn_merge)

        self.btn_recluster = KButton("Přeshlukovat", compact=True, icon="refresh")
        self.btn_recluster.setToolTip("Znovu seskupit nepotvrzené tváře (pojmenované osoby zůstanou)")
        self.btn_recluster.clicked.connect(self._recluster_faces)
        tb_layout.addWidget(guide.mark(self.btn_recluster, "faces_recluster"))

        self.btn_borderline = KButton("Hraniční", compact=True, icon="face_question")
        self.btn_borderline.setToolTip("Tváře, u kterých si rozpoznávání není jisté")
        self.btn_borderline.clicked.connect(self._open_borderline_review)
        tb_layout.addWidget(guide.mark(self.btn_borderline, "faces_borderline"))

        self.btn_duplicates = KButton("Duplicity", compact=True, icon="duplicates")
        self.btn_duplicates.setToolTip("Najít téměř stejné fotky")
        self.btn_duplicates.clicked.connect(self._open_duplicate_review)
        tb_layout.addWidget(guide.mark(self.btn_duplicates, "faces_duplicates"))

        layout.addWidget(toolbar)

        # Scroll area for person cards
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border:none; background:#1C1C1E; }")

        self._people_container = QWidget()
        self._people_container.setStyleSheet("background:#1C1C1E;")
        content = QVBoxLayout(self._people_container)
        content.setContentsMargins(24, 20, 24, 24)
        content.setSpacing(16)

        # „Je to stejná osoba?“ – návrhy sloučení roztříštěných osob
        self.merge_bar = MergeSuggestionBar()
        self.merge_bar.accepted.connect(self._on_suggestion_accepted)
        self.merge_bar.rejected.connect(self._on_suggestion_rejected)
        content.addWidget(self.merge_bar)

        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), page)
        esc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        esc.activated.connect(self._clear_person_selection)

        # Karty se zalamují podle šířky okna (dřív QHBoxLayout = jedna nekonečná řada)
        self._people_grid = QWidget()
        self._people_grid.setStyleSheet("background:transparent;")
        self._people_flow = FlowLayout(self._people_grid)
        self._people_flow.setContentsMargins(0, 0, 0, 0)
        content.addWidget(self._people_grid)

        self.people_empty = EmptyState("people", "Zatím žádné osoby",
                                       "Tváře se rozpoznávají na pozadí po importu fotek. "
                                       "Vše probíhá v počítači – nic se neodesílá.")
        self.people_empty.hide()
        content.addWidget(self.people_empty, 1)
        content.addStretch(1)

        scroll.setWidget(self._people_container)
        layout.addWidget(scroll, 1)

        return page

    def _build_events_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.lbl_events_sub = QLabel("")
        toolbar, _, _ = self._make_toolbar("Události", self.lbl_events_sub)
        layout.addWidget(toolbar)

        # Bento m\u0159\u00ed\u017eka karet (events_view.py) ve scrollovac\u00ed oblasti
        self._events_scroll = QScrollArea()
        self._events_scroll.setWidgetResizable(True)
        self._events_scroll.setStyleSheet("QScrollArea { border:none; background:#1C1C1E; }")
        self._events_grid = QWidget()
        self._events_grid.setStyleSheet("background:#1C1C1E;")
        self._events_layout = BentoLayout(self._events_grid)
        self._events_layout.setContentsMargins(24, 12, 24, 24)
        self._events_scroll.setWidget(self._events_grid)
        layout.addWidget(self._events_scroll, 1)

        self.lbl_events_empty = EmptyState("events", "Zatím žádné události",
                                           "Výlety a dny se poskládají samy podle data a místa, "
                                           "jakmile importujete fotky.")
        self.lbl_events_empty.setVisible(False)
        layout.addWidget(self.lbl_events_empty, 1)

        self._event_cards = []
        self.event_cards = []
        self._event_images = ImagePipeline(lambda: QSize(COVER_MAX_EDGE, COVER_MAX_EDGE), capacity=24,
                                           threads=2, parent=self)
        self._event_images.loaded.connect(self._on_event_cover_loaded)
        # Patch 11: viditelné karty v klidu prolínají několik fotek události (events_view)
        self._events_gen = 0
        self._ambient = AmbientCycler(self._events_scroll.viewport(), lambda: self.event_cards,
                                      self._event_images.request, self)
        return page

    def _build_map_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        try:
            # Platí pro výchozí profil = mapa i minimapa prohlížeče (viz is_allowed_web_url)
            self._web_interceptor = WebUrlInterceptor(self)
            QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(self._web_interceptor)

            self.map_view = QWebEngineView()
            # Patch 11: vlastní trvalý profil s diskovou HTTP cache (výchozí profil drží jen RAM)
            profile = map_web_profile()
            # QuietWebPage: konzole WebGL / MapLibre bez opakovaných hlášení za každý snímek
            self.map_view.setPage(QuietWebPage(profile, self.map_view, tag="Mapa") if profile is not None
                                  else QuietWebPage(self.map_view, tag="Mapa"))
            # Žádné menu Chromia (Zpět / Znovu načíst / Zobrazit zdroj) na pravém tlačítku
            self.map_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.map_view.page().setBackgroundColor(QColor("#1C1C1E"))
            settings = self.map_view.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

            self.map_channel = QWebChannel()
            self.map_bridge = MapBridge(self)
            self.map_channel.registerObject("bridge", self.map_bridge)
            self.map_view.page().setWebChannel(self.map_channel)
            self.map_view.loadStarted.connect(self.map_bridge.release_all)   # nová stránka = staré holdy pryč
            self.map_view.loadFinished.connect(self._on_map_loaded)

            map_html = os.path.join(RESOURCE_DIR, "map_vault.html")
            if os.path.exists(map_html):
                self.map_view.load(QUrl.fromLocalFile(os.path.abspath(map_html)))
            layout.addWidget(self.map_view)
        except Exception as e:
            layout.addWidget(EmptyState("map", "Mapa není dostupná",
                                        f"Zobrazení mapy se nepodařilo spustit ({e}). Fotky ani jejich "
                                        f"polohy se tím nemění."), 1)
            self.map_view = None

        return page

    def _build_import_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress_widget = CircularProgressWidget()
        self.progress_widget.btn_cancel.clicked.connect(self._cancel_import)
        layout.addWidget(self.progress_widget, 0, Qt.AlignmentFlag.AlignCenter)

        return page

    # ── DB init & data loading ────────────────────────────────────────────────

    def _init_db_and_load(self):
        try:
            init_db()
        except Exception as e:
            QMessageBox.critical(self, "Chyba DB", f"Nelze inicializovat databázi:\n{e}")
            return
        try:
            fixed = relocate_face_paths()
            if fixed:
                print(f"[Init] Opraveno {fixed} cest k ořezům obličejů (přesunutá složka aplikace)")
        except Exception as e:
            print(f"[Init] Oprava cest obličejů selhala: {e}")
        try:
            purged = purge_orphan_test_rows()
            if purged:
                print(f"[Init] Odstraněno {purged} osiřelých testovacích záznamů (černé karty v mřížce)")
        except Exception as e:
            print(f"[Init] Úklid osiřelých záznamů selhal: {e}")
        self._load_photos()
        self._load_people()
        self._load_events()
        self._load_embeddings()
        self._refresh_memory()
        self._start_ai_daemon()
        QTimer.singleShot(VIEWER_PREWARM_DELAY_MS, self._prewarm_viewer)
        QTimer.singleShot(SMART_IMPORT_DELAY_MS, self._smart_import_check)

    # ── Chytrý import (Patch 11) ──────────────────────────────────────────────

    def _smart_import_check(self):
        """Po startu jen SPOČÍTAT nové fotky ve sledovaných složkách a nabídnout import."""
        folders = [f for f in self.config.get("library_folders", []) if f and os.path.isdir(f)]
        if not self.isVisible() or not self.config.get("auto_scan_on_startup", True) or not folders \
                or (self._scanner and self._scanner.isRunning()) or self._import_probe is not None:
            return
        self._import_probe = library_tools.NewMediaProbe(folders, self)
        self._import_probe.found.connect(self._on_new_media_found)
        self._import_probe.start()

    def _on_new_media_found(self, count, folders):
        if count <= 0 or not folders:
            return
        noun = "nová fotka" if count == 1 else ("nové fotky" if 2 <= count <= 4 else "nových fotek")
        show_toast(self, f"Nalezeno {count} {noun}", "info", action_text="Importovat na pozadí",
                   on_action=lambda: self._import_in_background(folders), timeout_ms=None,
                   dismiss_text="Přeskočit")

    def _import_in_background(self, folders):
        """Import bez přepnutí na stránku průběhu – složky jedna po druhé, na konci toast."""
        self._enqueue_background_import([f for f in folders if f])

    @staticmethod
    def _queue_key(item):
        if isinstance(item, tuple):          # ("files", [cesty]) – přetažené soubory
            return ("files",) + tuple(os.path.normcase(p) for p in item[1])
        return os.path.normcase(os.path.normpath(item))

    def _enqueue_background_import(self, items, skip_running=True):
        """Fronta importu na pozadí: složky (str) nebo ("files", [cesty]). Běží-li sken, jen se
        zařadí – navazuje _on_background_scan_done, po importu přes stránku průběhu _on_scan_finished.
        skip_running: znovu přetažená složka, která se právě skenuje, se nezařadí podruhé (změny
        ze sledované složky ano – sken mohl soubor minout)."""
        queued = {self._queue_key(i) for i in self._background_import}
        busy = bool(self._scanner and self._scanner.isRunning())
        if busy and skip_running and self._background_current is not None:
            queued.add(self._queue_key(self._background_current))
        if not busy and not self._background_import:
            self._background_imported = 0
        for item in items:
            if self._queue_key(item) not in queued:
                self._background_import.append(item)
                queued.add(self._queue_key(item))
        if not busy:
            self._next_background_import()

    def _next_background_import(self):
        if not self._background_import:
            sync_geo_index()
            self._load_photos()
            self._load_events()
            self._refresh_memory()
            self._start_ai_daemon()
            if self._background_imported:
                show_toast(self, f"Import dokončen: {_czech_item_count(self._background_imported)}", "success")
            self._background_imported = 0
            return
        if self._scanner and self._scanner.isRunning():
            return                           # naváže se po doběhnutí běžícího skenu
        item = self._background_import.pop(0)
        self._background_current = item
        if isinstance(item, tuple):
            scanner = MediaScannerThread(None, files=item[1])
        else:
            scanner = MediaScannerThread(item)
        self._scanner = scanner
        scanner.status_signal.connect(self.lbl_status.setText)
        scanner.finished_signal.connect(lambda n, s=scanner, it=item: self._on_background_scan_done(s, it, n))
        scanner.finished.connect(lambda: self.lbl_status.setText(""))
        scanner.start()

    def _on_background_scan_done(self, scanner, item, processed):
        scanner.wait(5000)                   # finished_signal přichází těsně před koncem vlákna
        self._background_current = None
        self._background_imported += int(processed or 0)
        self._next_background_import()

    # ── Přetažení a sledované složky (Polish P3) ──────────────────────────────

    def _is_watched(self, folder):
        key = os.path.normcase(os.path.normpath(folder))
        return any(os.path.normcase(os.path.normpath(f)) == key for f in self.config.get("watched_folders", []))

    def _watched_roots(self):
        """Sledují se jen složky knihovny (Předvolby › Knihovna), které mají sledování zapnuté."""
        lib = {os.path.normcase(os.path.normpath(f)) for f in self.config.get("library_folders", []) if f}
        return [f for f in self.config.get("watched_folders", [])
                if f and os.path.normcase(os.path.normpath(f)) in lib]

    def import_dropped(self, folders, files):
        """Přetažené složky a soubory: import na pozadí, soubory se indexují na místě."""
        items = list(folders) + ([("files", list(files))] if files else [])
        if not items:
            return
        self._enqueue_background_import(items)
        unwatched = [f for f in folders if not self._is_watched(f)]
        what = drop_import.describe(folders, files)
        if unwatched:
            label = "Sledovat složku" if len(unwatched) == 1 else "Sledovat složky"
            show_toast(self, f"Importuji {what} na pozadí", "info", action_text=label,
                       on_action=lambda: self.watch_folders(unwatched), timeout_ms=8000)
        else:
            show_toast(self, f"Importuji {what} na pozadí", "info")

    def watch_folders(self, folders):
        """Trvale sledovat: složka se přidá do knihovny a změny se naimportují samy."""
        lib = list(self.config.get("library_folders", []))
        watched = list(self.config.get("watched_folders", []))
        for f in folders:
            if not any(os.path.normcase(x) == os.path.normcase(f) for x in lib):
                lib.append(f)
            if not self._is_watched(f):
                watched.append(f)
        self.config["library_folders"], self.config["watched_folders"] = lib, watched
        save_config(self.config)
        self.folder_watcher.set_roots(self._watched_roots())
        names = ", ".join(f"„{os.path.basename(f) or f}“" for f in folders)
        show_toast(self, f"Sleduji {names} – nové fotky se přidají samy", "success")

    def _on_watched_changed(self, roots):
        # sken je inkrementální: známé soubory přeskočí
        self._enqueue_background_import(roots, skip_running=False)

    def _resume_background_queue(self):
        """Po importu přes stránku průběhu pokračovat frontou (přetažení během importu)."""
        if self._scanner is not None:
            self._scanner.wait(5000)
        if self._background_import:
            self._next_background_import()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() \
                and getattr(self, "folder_watcher", None) is not None:
            self.folder_watcher.window_activated()

    def _load_photos(self):
        try:
            self._all_records = fetch_media_records()
            self._trash_records = fetch_media_records(trashed=True)
        except Exception as e:
            print(f"[Load] {e}")
            self._all_records = []
            self._trash_records = []
        self._apply_filter_and_sort()

    def _load_people(self):
        try:
            conn = get_connection()
            c = conn.cursor()
            c.execute("""
                SELECT p.id, p.name, p.cover_face_path,
                       COUNT(f.id) as face_count
                FROM people p
                LEFT JOIN face_instances f ON f.person_id = p.id AND COALESCE(f.is_valid, 1) = 1
                     AND f.media_id IN (SELECT id FROM media_items WHERE COALESCE(is_trashed, 0) = 0)
                GROUP BY p.id
                ORDER BY p.name COLLATE NOCASE
            """)
            people = c.fetchall()
        except Exception as e:
            print(f"[People] {e}")
            people = []
        self._render_people_cards(people)

    def _render_people_cards(self, people):
        while self._people_flow.count():
            item = self._people_flow.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._person_cards.clear()
        self._people_rows = {pid: (name, cover, count) for pid, name, cover, count in people}
        self._selected_people &= set(self._people_rows)     # sloučené / smazané osoby z výběru ven

        avatar_px = max(1, round(PERSON_CARD_W * PERSON_AVATAR_RATIO * self.devicePixelRatioF()))
        for pid, name, cover_path, count in people:
            card = PersonCard(pid, name or UNKNOWN_PERSON, count, self._people_grid)
            card.clicked.connect(self._on_person_card_clicked)
            card.selection_toggled.connect(self._toggle_person_selection)
            card.context_requested.connect(self._show_person_context_menu)
            card.set_selected(pid in self._selected_people)
            self._person_cards[pid] = card
            self._people_flow.addWidget(card)

            # P3.1 asynchronní načtení avataru (cache = bez blikání při opakovaném načtení)
            card.setProperty("cover_path", cover_path)
            cached = self._avatar_cache.get((pid, cover_path))
            if cached is not None:
                card.set_avatar(cached)
                continue
            worker = PersonCardWorker(pid, card.name, cover_path, count, target_size=avatar_px)
            worker.signals.ready.connect(self._on_person_card_ready)
            self._person_card_pool.start(worker)

        self._filter_people()
        self._update_merge_button()
        if self.stack.currentWidget() is self.page_people:
            self._refresh_merge_suggestions()

    # ── Výběr a slučování osob ────────────────────────────────────────────────

    def _toggle_person_selection(self, person_id: int, additive: bool = True):
        if person_id in self._selected_people:
            self._selected_people.discard(person_id)
        else:
            if not additive:
                self._selected_people.clear()
            self._selected_people.add(person_id)
        for pid, card in self._person_cards.items():
            card.set_selected(pid in self._selected_people)
        self._update_merge_button()

    def _clear_person_selection(self):
        self._selected_people.clear()
        for card in self._person_cards.values():
            card.set_selected(False)
        self._update_merge_button()

    def _update_merge_button(self):
        n = len(self._selected_people)
        self.btn_merge.setVisible(n >= 2)
        self.btn_merge.setText(f"Sloučit osoby ({n})")

    def _show_person_context_menu(self, person_id: int, global_pos):
        if person_id not in self._selected_people:
            # pravé tlačítko na nevybrané kartě = pracuje se s ní (jako ve Finderu)
            self._selected_people = {person_id}
            for pid, card in self._person_cards.items():
                card.set_selected(pid == person_id)
            self._update_merge_button()
        name = (self._people_rows.get(person_id) or (UNKNOWN_PERSON,))[0] or UNKNOWN_PERSON
        n = len(self._selected_people)
        menu = QMenu(self)
        act_open = menu.addAction("Otevřít")
        act_rename = menu.addAction("Přejmenovat…")
        menu.addSeparator()
        act_merge = menu.addAction(f"Sloučit osoby ({n})" if n >= 2 else "Sloučit osoby")
        act_merge.setEnabled(n >= 2)
        if n < 2:
            act_merge.setToolTip("Ctrl+klikem vyberte další osobu")
        act_clear = menu.addAction("Zrušit výběr")
        act_clear.setEnabled(n > 0)
        action = menu.exec(global_pos)
        if action == act_open:
            self._clear_person_selection()
            self._on_person_card_clicked(person_id, name)
        elif action == act_rename:
            self._rename_person(person_id, name)
        elif action == act_merge:
            self._merge_people_ids(sorted(self._selected_people))
        elif action == act_clear:
            self._clear_person_selection()

    def _merge_target(self, ids):
        """Cíl sloučení: pojmenovaná osoba, jinak ta s nejvíce fotkami."""
        def key(pid):
            name, _, count = self._people_rows.get(pid, (None, None, 0))
            named = bool(name) and name != UNKNOWN_PERSON
            return (named, int(count or 0), -pid)
        return max(ids, key=key)

    def _merge_people_ids(self, ids, ask_name=True):
        ids = [int(i) for i in ids if int(i) in self._people_rows]
        if len(ids) < 2:
            show_toast(self, "Ke sloučení vyberte alespoň dvě osoby (Ctrl+klik)", "info")
            return None
        target = self._merge_target(ids)
        names = []
        for pid in ids:
            name = self._people_rows[pid][0]
            if name and name != UNKNOWN_PERSON and name not in names:
                names.append(name)
        chosen = names[0] if names else None
        if len(names) > 1 and ask_name:
            chosen, ok = InWindowDialog.getItem(self, "Sloučit osoby",
                                              "Osoby mají různá jména. Které ponechat?", names, 0, False)
            if not ok:
                return None
        try:
            result = merge_people(target, [i for i in ids if i != target], name=chosen)
        except Exception as e:
            show_toast(self, f"Osoby se nepodařilo sloučit: {e}", "error")
            return None
        if not result:
            return None
        self._selected_people.clear()
        self._avatar_cache.clear()
        self._load_people()
        if self.stack.currentWidget() is not self.page_people:
            self._refresh_merge_suggestions()
        label = f"„{result['name']}“" if result["name"] and result["name"] != UNKNOWN_PERSON else "jedné osoby"
        extra = f", nově nalezeno: {result['pulled_in']}" if result["pulled_in"] else ""
        show_toast(self, f"Sloučeno do {label} – přesunuto obličejů: {result['moved_faces']}{extra}",
                   "success", action_text="Vrátit", on_action=lambda u=result["undo"]: self._undo_people_merge(u))
        return result

    def _undo_people_merge(self, undo):
        try:
            undo_merge(undo)
        except Exception as e:
            show_toast(self, f"Sloučení se nepodařilo vrátit: {e}", "error")
            return
        self._avatar_cache.clear()
        self._load_people()

    def _suggestion_avatar(self, person_id, px):
        _, cover, _ = self._people_rows.get(person_id, (None, None, 0))
        path = resolve_face_path(cover)
        return _load_avatar_image(path, px) if path else QImage()

    def _refresh_merge_suggestions(self):
        try:
            suggestions = suggest_person_merges()
        except Exception as e:
            print(f"[People] návrhy sloučení: {e}")
            suggestions = []
        self.merge_bar.set_suggestions(suggestions, self._suggestion_avatar)

    def _on_suggestion_accepted(self, suggestion):
        # _load_people po sloučení načte návrhy znovu (centroid sloučené osoby se změnil)
        self._merge_people_ids([suggestion["a"], suggestion["b"]])

    def _on_suggestion_rejected(self, suggestion):
        try:
            dismiss_person_merge(suggestion["a"], suggestion["b"])
        except Exception as e:
            show_toast(self, f"Rozhodnutí se nepodařilo uložit: {e}", "error")
            return
        self.merge_bar.drop_current()

    def _on_person_card_ready(self, person_id: int, image: QImage, name: str, count: int):
        card = self._person_cards.get(person_id)
        if card is None or image.isNull():
            return
        self._avatar_cache.put((person_id, card.property("cover_path")), image)
        card.set_avatar(image)

    def _filter_people(self):
        """Filtr bez nového dotazu do DB: jen skryje karty, FlowLayout je přeskočí."""
        search_text = strip_accents(self.people_search.text())
        visible = 0
        for card in self._person_cards.values():
            match = not search_text or search_text in strip_accents(card.name)
            card.setVisible(match)
            visible += match
        self.lbl_people_count.setText(f"{visible} osob")
        self._update_people_empty(visible)

    def _load_events(self):
        try:
            conn = get_connection()
            try:
                cards = fetch_event_cards(conn)
                trips = {t["id"] for t in journey.list_journeys(conn)}
                tint_todo = tint.stale_items(conn, cards)      # odstíny obálek (uložené se jen načtou)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Events] {e}")
            cards, trips, tint_todo = [], set(), []
        for ev in cards:
            ev["journey"] = int(ev["id"]) in trips
        self._event_cards = cards
        self._render_event_cards(cards)
        if tint_todo:
            job = tint.TintJob(tint_todo, get_connection)
            job.signals.done.connect(lambda tints, gen=self._events_gen: self._on_event_tints(gen, tints))
            self._tint_job = job             # drží signály naživu do konce výpočtu
            self._event_images.pool.start(job)

    def _on_event_tints(self, gen, tints):
        self._tint_job = None
        if gen != self._events_gen:
            return
        for ev in self._event_cards:
            if int(ev["id"]) in tints:
                ev["tint"] = tints[int(ev["id"])]
        for card in self.event_cards:
            card.update()

    def _render_event_cards(self, cards):
        while self._events_layout.count():
            item = self._events_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.event_cards = []
        for ev in cards:
            card = EventCard(ev, self._events_grid)
            card.clicked.connect(self._open_event)
            card.journey_requested.connect(self._request_journey)
            cover = ev.get("cover") or {}
            full = self._event_images.request(cover.get("path")) if cover.get("path") else None
            if full is not None:
                card.set_cover(QPixmap.fromImage(full), full_res=True)
            elif cover.get("thumb") and os.path.exists(cover["thumb"]):
                card.set_cover(QPixmap(cover["thumb"]))       # hned náhled, ostrá obálka dorazí
            self._events_layout.addWidget(card)
            self.event_cards.append(card)
        n = len(cards)
        self.lbl_events_sub.setText("" if not n else
                                    f"{n} {'událost' if n == 1 else 'události' if n < 5 else 'událostí'}")
        self.lbl_events_empty.setVisible(n == 0)
        self._events_scroll.setVisible(n > 0)
        self._events_scroll.setVisible(n > 0)
        self._events_gen += 1
        if cards and self._ambient.animations:
            job = AmbientFramesJob(cards, get_connection)
            job.signals.done.connect(lambda frames, gen=self._events_gen: self._on_ambient_frames(gen, frames))
            self._event_images.pool.start(job)

    def _on_ambient_frames(self, gen, frames):
        if gen != self._events_gen:
            return                          # mezitím se karty znovu vykreslily
        for card in self.event_cards:
            card.set_frames(frames.get(int(card.event["id"]), []))
        self._ambient.sync()

    def _on_event_cover_loaded(self, path, image):
        for card in getattr(self, "event_cards", []):
            if card.cover_path() == path:
                card.set_cover(QPixmap.fromImage(image), full_res=True)

    def _load_embeddings(self):
        try:
            self._embeddings_cache = get_search_vectors()       # videa: plakát + snímky celého klipu
        except Exception as e:
            print(f"[Embeddings] {e}")
            self._embeddings_cache = {}
        # démon opravil / doplnil embeddingy -> běžící hledání se zpřesní samo
        if self._search_query.strip() and self._search_result is not None:
            self._search_debounce.start()

    def _merge_embeddings(self, ids):
        """Démon doplnil vektory těchto položek: jen je přidat do cache. Dřív se každých 25
        položek načítala celá knihovna v GUI vlákně (desítky MB, sekundové záseky)."""
        try:
            fresh = get_search_vectors(ids)
        except Exception as e:
            print(f"[Embeddings] {e}")
            return
        cache = getattr(self, "_embeddings_cache", None)
        if cache is None:
            self._embeddings_cache = cache = {}
        cache.update(fresh)
        if self._search_query.strip() and self._search_result is not None:
            self._search_debounce.start()

    def _start_ai_daemon(self):
        """Spustí průchod démona. Běží-li už, průchod se po doběhnutí zopakuje: seznamy práce
        (nové importy, změněné prahy z Předvoleb) si démon bere na začátku a nové položky by
        jinak čekaly do dalšího spuštění aplikace (Sprint R)."""
        daemon = self._ai_daemon
        if daemon is not None and daemon.isRunning():
            self._ai_daemon_rerun = True
            return
        self._ai_daemon_rerun = False
        if getattr(self, "_shutting_down", False):
            return
        threshold = float(self.config.get("face_similarity_threshold", 0.62))
        self._ai_daemon = BackgroundAIDaemon(face_threshold=threshold,
                                             merge_threshold=self.config.get("face_merge_threshold"))
        self._ai_daemon.preview_budget_mb = int(self.config.get("preview_cache_mb", preview_cache.DEFAULT_BUDGET_MB))
        self._ai_daemon.proxy_budget_mb = int(self.config.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB))
        self._ai_daemon.people_updated.connect(self._load_people)
        self._ai_daemon.embeddings_updated.connect(self._load_embeddings)
        self._ai_daemon.embeddings_added.connect(self._merge_embeddings)
        self._ai_daemon.thumbs_repaired.connect(self._load_photos)
        self._ai_daemon.face_crops_repaired.connect(self._on_face_crops_repaired)
        self._ai_daemon.status_changed.connect(self._on_daemon_status)
        self._ai_daemon.finished.connect(self._on_ai_daemon_finished)
        self._ai_daemon.start()

    def _on_ai_daemon_finished(self):
        if getattr(self, "_ai_daemon_rerun", False) and not getattr(self, "_shutting_down", False):
            daemon = self._ai_daemon
            if daemon is not None:
                daemon.wait(1000)            # finished přichází těsně před koncem run()
            QTimer.singleShot(0, self._start_ai_daemon)

    def _on_face_crops_repaired(self, count: int):
        # cache avatarů je klíčovaná cestou, ta zůstává stejná -> jinak by visel starý obrázek
        self._avatar_cache.clear()
        self._load_people()

    def _on_daemon_status(self, text: str):
        self.lbl_status.setText(text)

    # ── Filter / Sort ─────────────────────────────────────────────────────────

    def _collection_records(self):
        """Záznamy aktuální kolekce (knihovna / oblíbené / fotky / videa / koš) v pořadí mřížky."""
        f = self._current_filter
        records = list(self._trash_records if f == "trash" else self._all_records)
        if f == "favorites":
            records = [r for r in records if r[7]]
        elif f == "photo":
            records = [r for r in records if r[3] in ("photo", "live_photo")]
        elif f == "video":
            records = [r for r in records if r[3] == "video"]
        return records

    def _sorted(self, records):
        s = self._current_sort
        if s == "date_asc":
            records.sort(key=lambda r: r[6] or "")
        elif s == "date_desc":
            records.sort(key=lambda r: r[6] or "", reverse=True)
        elif s == "name_az":
            records.sort(key=lambda r: strip_accents(r[1] or ""))
        elif s == "size":
            records.sort(key=lambda r: r[0], reverse=True)
        return records

    def _apply_filter_and_sort(self):
        f = self._current_filter
        records = self._collection_records()
        possible = ()
        if self._search_query.strip():
            result = self._search_result
            if result is None or result.query != self._search_query.strip():
                self._sync_memory_card()   # hledání běží: mřížka drží předchozí stav (žádné
                return                     # probliknutí), karta Vzpomínek ale hned zmizí
            # Hledání v AKTUÁLNÍ kolekci, v pořadí relevance: jisté shody, pak možné. Řazení
            # zvolené během hledání platí uvnitř pásem; jinak relevance.
            confident = order_records_by_ids(records, result.confident)
            maybe = order_records_by_ids(records, result.possible)
            if self._search_sort_override:
                confident, maybe = self._sorted(confident), self._sorted(maybe)
            records = confident + maybe
            possible = {int(r[0]) for r in maybe}
        else:
            records = self._sorted(records)

        self._filtered_records = records
        self.grid_view.photo_model.set_records(records, possible)
        self._sync_memory_card()
        if hasattr(self, 'lbl_photo_count'):
            self.lbl_photo_count.setText(self._count_text(len(records), len(possible)))

    def _count_text(self, count, possible=0):
        query = self._search_query.strip()
        if query:
            if not count:
                return f"Nic neodpovídá „{query}“"
            sure = count - possible
            if not possible:
                return f"{_czech_result_count(sure)} pro „{query}“"
            if not sure:
                return f"Žádná jistá shoda · {possible} možných pro „{query}“"
            return f"{_czech_result_count(sure)} · {possible} možných pro „{query}“"
        if self._current_filter == "trash":
            return f"{_czech_item_count(count)} · obnovíte je pravým tlačítkem" if count else ""
        return _czech_item_count(count)

    def _set_filter(self, key: str):
        self._current_filter = key
        self.lbl_photos_title.setText(self.FILTER_TITLES.get(key, "Fotografie"))
        if self._search_query.strip():
            self._search_result = None       # jiná kolekce = jiné prostředí hledání
            self._run_ai_search()
        self._apply_filter_and_sort()
        if self.stack.currentIndex() == 0:
            self._sync_sidebar()

    def _on_sidebar(self, key):
        if key in self.COLLECTION_FILTERS:
            self._open_collection(key)
            return
        if key == "photos" and self._current_filter != "all":
            self._set_filter("all")
        self._switch_page(self.SIDEBAR_PAGES.index(key))

    def _open_collection(self, key):
        """Kolekce = mřížka Fotografie s filtrem (Oblíbené, Videa, Smazané)."""
        self._set_filter(self.COLLECTION_FILTERS[key])
        self._switch_page(0)

    def _sync_sidebar(self):
        """Jezdec v postranním panelu podle stránky a filtru (i po programové změně)."""
        idx = self.stack.currentIndex()
        if not 0 <= idx < len(self.SIDEBAR_PAGES):
            idx = self.stack.indexOf(self.nav.root())    # podstránka: podle kořene pod ní
        if idx == 0:
            key = next((k for k, f in self.COLLECTION_FILTERS.items() if f == self._current_filter), "photos")
        elif 0 <= idx < len(self.SIDEBAR_PAGES):
            key = self.SIDEBAR_PAGES[idx]
        else:
            return
        self.sidebar_nav.set_current(key)

    def _update_photos_empty(self, *_):
        if self.grid_view.photo_model.rowCount() > 0:
            self.photos_empty.hide()
            self._update_day_capsule()
            return
        query, f = self._search_query.strip(), self._current_filter
        action = None
        if query:
            content = ("search", f"Žádné výsledky pro „{query}“",
                       "Zkuste jiné slovo nebo popis – třeba „moře“, „pes“ nebo „západ slunce“.")
            action = ("clear", "Vymazat hledání")
        elif f == "trash":
            content = ("trash", "Koš je prázdný",
                       "Smazané fotky a videa tu počkají, dokud koš nevysypete v Předvolbách.")
        elif f == "favorites":
            content = ("heart", "Zatím žádné oblíbené",
                       "Označte fotku srdíčkem – klávesou F v mřížce nebo v prohlížeči – a najdete ji tady.")
        elif f == "video":
            content = ("video", "Žádná videa", "Videa z vašich složek se tu objeví hned po importu.")
        elif f == "photo":
            content = ("photos", "Žádné fotky", "Fotky z vašich složek se tu objeví hned po importu.")
        else:
            content = ("photos", "Zatím tu nejsou žádné fotky",
                       "Přidejte složku s fotkami. Import poběží na pozadí a nic se nepřesouvá ani nemaže.")
            action = ("import", "Importovat")
        self._photos_empty_action = action[0] if action else None
        self.photos_empty.set_content(*content, action[1] if action else None)
        if self.photos_empty.isHidden():
            self.photos_empty.show()
            self.photos_empty.raise_()

    # ── pohled podle času (Roky / Měsíce / Dny / Vše) ──
    def _set_time_view(self, mode):
        prev, self._time_view = self._time_view, mode
        grid = self.grid_view
        if mode == "days" and prev != "days":
            self._cell_before_days = grid._cols or grid.columns()    # Dny = větší dlaždice (4 sloupce)
            grid.set_columns(4)
        elif mode != "days" and prev == "days" and self._cell_before_days:
            grid.set_columns(self._cell_before_days)
        self._refresh_moments()
        self._update_day_capsule()

    def _refresh_moments(self):
        mode = self._time_view
        if mode in ("years", "months") and self._filtered_records:
            self.moments_view.set_groups(mode, moments_view.group_records(self._filtered_records, mode))
            self.photo_views.setCurrentWidget(self.moments_view)
        else:
            self.photo_views.setCurrentWidget(self.grid_view)

    def _on_moment_clicked(self, mode, key):
        """Rok -> jeho měsíce; měsíc -> mřížka Vše na prvním snímku toho měsíce."""
        if mode == "years":
            self.time_switch.set_current("months")
            self._set_time_view("months")
            self.moments_view.scroll_to_key_prefix(key)
            return
        self.time_switch.set_current("all")
        self._set_time_view("all")
        row = next((i for i, r in enumerate(self.grid_view.photo_model.records)
                    if (str(r[6] or "").startswith(key) if key else not r[6])), None)
        if row is not None:
            self.grid_view.scrollTo(self.grid_view.photo_model.index(row, 0),
                                    QAbstractItemView.ScrollHint.PositionAtTop)

    def _update_day_capsule(self, *_):
        model = self.grid_view.photo_model
        if self._time_view != "days" or not model.rowCount():
            self.day_capsule.hide_sticky()
            return
        idx = self.grid_view.indexAt(QPoint(12, 12))
        if idx.isValid():
            text = moments_view.czech_day(model.records[idx.row()][6])
            if text:
                self.day_capsule.show_sticky(text)

    def _on_photos_empty_action(self):
        if self._photos_empty_action == "clear":
            self._clear_search()
        elif self._photos_empty_action == "import":
            self._start_import()

    def _update_people_empty(self, visible):
        if not self._person_cards:
            self.people_empty.set_content("people", "Zatím žádné osoby",
                                          "Tváře se rozpoznávají na pozadí po importu fotek. "
                                          "Vše probíhá v počítači – nic se neodesílá.")
        else:
            q = self.people_search.text().strip()
            self.people_empty.set_content("search", f"Nikdo neodpovídá „{q}“",
                                          "Zkuste jiné jméno, nebo pojmenujte osobu na její kartě.")
        self.people_empty.setVisible(visible == 0)

    def _refresh_theme_icons(self):
        """Ikony kreslené mimo vlastní widgety (akce v polích hledání) podle motivu."""
        col = tokens.palette()["text_tertiary"]
        for action in (self._search_icon, self._people_search_icon):
            action.setIcon(icons.qicon("search", col, 16))

    def _on_sort_changed(self, idx: int):
        mapping = {0: "date_desc", 1: "date_asc", 2: "name_az", 3: "size"}
        self._current_sort = mapping.get(idx, "date_desc")
        if self._search_query.strip():
            self._search_sort_override = True   # zvoleno během hledání: platí uvnitř pásem
        self._apply_filter_and_sort()

    def _remember_cell_size(self, px: int):
        """Zoom mřížky se pamatuje (config) a platí i pro mřížky osob a událostí."""
        self.config["default_thumbnail_size"] = int(px)
        save_config(self.config)

    # ── P3.2 Search ───────────────────────────────────────────────────────────

    def _on_search_changed(self, text: str):
        self.btn_clear_search.setVisible(bool(text))
        self._search_query = text
        self._search_sort_override = False       # nový dotaz = zase podle relevance
        if not text.strip():
            self._search_result = None
            self._apply_filter_and_sort()
            return
        self._search_debounce.start()

    def _clear_search(self):
        self.search_bar.clear()
        self._search_query = ""
        self._search_result = None
        self._apply_filter_and_sort()

    def _warm_search_model(self):
        import ai_engine
        if self._search_warmup is not None or not self._embeddings_cache or ai_engine._text_model is not None:
            return
        self._search_warmup = SearchModelWarmup()
        self.lbl_status.set_task("search_model", "Připravuji chytré hledání…")
        self._search_warmup.finished.connect(lambda: self.lbl_status.set_task("search_model", ""))
        self._search_warmup.start()

    def _run_ai_search(self):
        query = self._search_query.strip()
        if not query:
            self._apply_filter_and_sort()
            return
        status = getattr(self, "lbl_status", None)
        if status is not None:
            import ai_engine
            status.set_task("search", f"Hledám „{query}“…" if ai_engine._text_model is not None
                            else "Připravuji chytré hledání…")
        # QThread.quit() vlákno bez event loopu nezastaví a přepsání reference by zničilo
        # běžící QThread (pád). Starší hledání doběhnou samy, jejich výsledky se zahodí.
        # Hledá se v aktuální kolekci (i v koši); fazety (místo, čas, typ) fungují i bez modelu.
        universe = [int(r[0]) for r in self._collection_records()]
        worker = AISearchWorker(self._search_query, self._embeddings_cache, universe)
        worker.search_finished.connect(self._on_search_results)
        worker.finished.connect(lambda w=worker: self._release_search_worker(w))
        self._search_workers.add(worker)
        worker.start()

    def _release_search_worker(self, worker):
        worker.wait()   # finished se vysílá těsně před koncem vlákna
        # Poslední referenci pustit až v dalším průchodu smyčky: zrušení QThread uprostřed
        # doručování jeho vlastního signálu finished je use-after-free (0xC0000005)
        QTimer.singleShot(0, lambda: self._drop_search_worker(worker))

    def _drop_search_worker(self, worker):
        self._search_workers.discard(worker)
        status = getattr(self, "lbl_status", None)
        if status is not None and not self._search_workers:
            status.set_task("search", "")

    def _on_search_results(self, result, query: str):
        if query != self._search_query:
            return  # stale result
        if not isinstance(result, search_engine.SearchResult):     # starý tvar [(id, skóre)]
            result = search_engine.SearchResult(query, parse_search_results(result), [], [], True)
        self._search_result = result
        # pod prahem relevance nic -> prázdný výsledek a věta „Nic neodpovídá …“, ne náhodné fotky
        self._apply_filter_and_sort()

    # ── Navigation ────────────────────────────────────────────────────────────

    def _switch_page(self, idx: int):
        self.nav.reset()          # postranní panel zavře otevřené podstránky (prohlížeč, osobu, ...)
        self.stack.setCurrentIndex(idx)
        self._sync_sidebar()
        if idx == 1:
            self._load_people()
        elif idx == 2:
            self._load_events()
        elif idx == 3:
            self._refresh_map()

    def _on_map_loaded(self, ok: bool):
        self._map_loaded = ok
        if not ok:
            self._pending_journey = None
            print("[Map] Stránku mapy se nepodařilo načíst")
            return
        layer = self.config.get("map_layer", DEFAULT_MAP_LAYER)
        layer = LEGACY_MAP_LAYERS.get(layer, layer)
        if layer not in MAP_LAYERS:
            layer = DEFAULT_MAP_LAYER
        page = self.map_view.page()
        # adaptivní škálování mapy: meze rozlišení, LOD dlaždic a terénu podle profilu stroje
        page.runJavaScript(f"window.setPerfProfile && window.setPerfProfile({json.dumps(self.perf.for_map())});")
        page.runJavaScript(f"window.setBaseLayer && window.setBaseLayer({json.dumps(layer)});")
        self._sync_accent()
        self._sync_map_active()
        self._refresh_map()
        data, self._pending_journey = self._pending_journey, None
        if data is not None:                # Cesta vyžádaná z Událostí dřív, než se mapa načetla
            page.runJavaScript(f"window.playJourney && window.playJourney({json.dumps(data)});")

    @staticmethod
    def _event_glow(event, mode=None):
        """Kontextový odstín události pro kreslené plochy, nebo None (bez odstínu / vypnuto)."""
        hue = (event or {}).get("tint")
        if not isinstance(hue, str) or not hue or not tokens.adaptive():
            return None
        return tokens.context_glow(hue, mode)

    def _sync_accent(self):
        """Zvýraznění do míst mimo styly Qt (plátno mapy) – po načtení mapy i po změně v Předvolbách."""
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return                          # nenačtená mapa dostane aktuální barvu v _on_map_loaded
        self.map_view.page().runJavaScript(
            f"window.setAccent && window.setAccent({json.dumps(tokens.accent('dark')['accent'])});")

    def _sync_map_active(self, *_):
        """Mapa ví, jestli je vidět: skrytá se neotáčí a Cesta se pozastaví."""
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return
        active = self.stack.currentWidget() is self.page_map
        self.map_view.page().runJavaScript(f"window.setMapActive && window.setMapActive({json.dumps(active)});")

    def play_journey(self, event_id: int, return_here: bool = False):
        """Cesta: zastávky události -> kinematické přehrávání na mapě (journey.py).
        Nenačtená mapa si data podrží (_pending_journey) a přehraje je v _on_map_loaded.
        return_here: mapa se vloží jako podstránka nad aktuální stránku (detail / přehled
        událostí) a konec Cesty nebo Zpět vrátí přesně tam – místo resetu na kořen Mapy."""
        if getattr(self, "map_view", None) is None:
            return False
        try:
            conn = get_connection()
            try:
                data = journey.build_journey(
                    conn, event_id, get_local_thumb_url,
                    image_url=lambda p: get_local_thumb_url(preview_cache.get_preview(p, touch=False)))
                row = conn.execute("SELECT tint FROM events WHERE id = ?", (int(event_id),)).fetchone()
            finally:
                conn.close()
        except Exception as e:
            print(f"[Map] Cesta: {e}")
            return False
        if data is None:
            return False
        glow = MainWindow._event_glow({"tint": row[0] if row else None}, mode="dark")
        if glow:
            data["accent"] = glow            # trasa v odstínu výletu; po konci Cesty zpět zvýraznění
        if self.stack.currentWidget() is not self.page_map:
            if return_here:
                # Drobečková navigace: NavigationController si pamatuje stránku pod mapou
                # i její posunutí; podstránka se nezničí jako při _switch_page
                self.nav.push(self.page_map, on_pop=self._on_journey_return, dispose=False)
                self._journey_return = True
                self._sync_sidebar()
                self._refresh_map()
            else:
                self._switch_page(3)
        if not self._map_loaded:
            self._pending_journey = data
            return True
        self.map_view.page().runJavaScript(f"window.playJourney && window.playJourney({json.dumps(data)});")
        return True

    def _request_journey(self, event_id: int):
        """Tlačítko Cesta (karta / hlavička události). Odloženě: přepnutí stránky ruší podstránku,
        ze které klik přišel."""
        def go():
            if not self.play_journey(int(event_id), return_here=True):
                show_toast(self, "Tahle událost nemá dost míst pro Cestu", "info")
        QTimer.singleShot(0, go)

    def journey_closed(self):
        """Konec Cesty z mapy: spuštěná z události -> zpět na ni (jen když je mapa navrchu;
        nad ní může být prohlížeč fotky z Cesty)."""
        if self._journey_return and self.nav.current() is self.page_map:
            self.nav.pop()

    def _on_journey_return(self):
        """Mapa sundaná ze zásobníku (konec Cesty, Zpět, postranní panel): Cesta skončí tiše."""
        self._journey_return = False
        self._pending_journey = None
        if getattr(self, "map_view", None) is not None and self._map_loaded:
            self.map_view.page().runJavaScript("window.endJourney && window.endJourney(true);")
        self._sync_sidebar()

    def set_map_layer(self, name: str):
        if name not in MAP_LAYERS or self.config.get("map_layer") == name:
            return
        self.config["map_layer"] = name
        try:
            save_config(self.config)
        except Exception as e:
            print(f"[Map] Volbu podkladu se nepodařilo uložit: {e}")

    def _refresh_map(self):
        """Pošle do mapy všechny viditelné fotky s polohou (R*Tree index přes query_geo_bounds)."""
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return   # stránka ještě není načtená -> data pošle _on_map_loaded
        try:
            markers = build_map_markers(query_geo_bounds(-90, 90, -180, 180))
        except Exception as e:
            print(f"[Map] {e}")
            return
        self.map_view.page().runJavaScript(
            f"window.setMarkers && window.setMarkers({json.dumps(markers)});")
        try:
            conn = get_connection()
            try:
                trips = journey.list_journeys(conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Map] Cesty: {e}")
            return
        self.map_view.page().runJavaScript(f"window.setJourneys && window.setJourneys({json.dumps(trips)});")

    def open_viewer_for_ids(self, ids):
        """Prohlížeč nad konkrétními fotkami (shluk z mapy), seřazenými podle data."""
        ids = [int(i) for i in ids][:900]   # limit parametrů SQLite
        if not ids:
            return
        try:
            records = fetch_media_records(f"m.id IN ({','.join('?' * len(ids))})", tuple(ids),
                                          order_by="m.date_taken, m.id")
        except Exception as e:
            show_toast(self, f"Fotky nelze otevřít: {e}", "error")
            return
        self._open_viewer(records, 0)

    # ── Photo actions ─────────────────────────────────────────────────────────

    def _on_photo_activated(self, row: int):
        self._open_viewer(self._filtered_records, row, self.grid_view.photo_model)

    def open_viewer_by_id(self, item_id: int):
        """Mapa a jiné vstupy podle id: v kontextu aktuální mřížky, jinak samostatně."""
        for row, rec in enumerate(self._filtered_records):
            if int(rec[0]) == int(item_id):
                self._open_viewer(self._filtered_records, row, self.grid_view.photo_model)
                return
        try:
            records = fetch_media_records("m.id = ?", (int(item_id),))
        except Exception as e:
            show_toast(self, f"Soubor nelze otevřít: {e}", "error")
            return
        self._open_viewer(records, 0)

    def _on_grid_hover(self, index: QModelIndex):
        self._hover_row = index.row() if index.isValid() else -1
        self._hover_timer.start()

    def _preload_hovered(self):
        """Video pod kurzorem do záložního přehrávače – dvojklik ho pak spustí bez dekódování."""
        row = self._hover_row
        if self._viewer is None or self.nav.depth() != 0 or not 0 <= row < len(self._filtered_records):
            return
        rec = self._filtered_records[row]
        if rec[3] == "video" and rec[2] and os.path.exists(rec[2]):
            self._viewer.preload(rec[2])

    def _prewarm_viewer(self):
        """Po startu v klidu: vytvořit prohlížeč (~0,6 s) a zahřát přehrávání videa (~0,15 s),
        aby první otevření fotky ani první video nezasekly rozhraní."""
        if not self.isVisible():
            return                      # zavřené / skryté okno nic nepřipravuje
        if self._viewer is None and self.nav.depth() == 0:
            self._ensure_viewer()
        if self._media_warmup is None:
            video = next((r[2] for r in self._all_records
                          if r[3] == "video" and r[2] and os.path.exists(r[2])), None)
            if video:
                # o chvíli později, ať se obě jednorázové ceny nesečtou do jednoho zaseknutí
                QTimer.singleShot(1000, lambda p=video: self._start_media_warmup(p))

    def _start_media_warmup(self, path):
        if self._media_warmup is None and self.isVisible():
            self._media_warmup = MediaWarmUp(path, self)
            self._media_warmup.start()

    def _ensure_viewer(self):
        """Jeden prohlížeč na celé okno – přehrávač a minimapa se nevytvářejí při každém otevření."""
        if self._viewer is None:
            self._viewer = create_media_viewer([], 0)
            self._viewer.close_requested.connect(self._close_viewer)
            self._viewer.favorite_changed_signal.connect(
                lambda media_id, is_fav: self._on_favorite_changed(media_id, is_fav, self._viewer_model))
            self._viewer.fullscreen_toggled.connect(lambda on: self._set_immersive("viewer", on))
            self._viewer.slideshow_requested.connect(
                lambda records, index: self._start_slideshow(records, index, self._viewer_model))
            self._viewer.transition_provider = self._viewer_transition
        return self._viewer

    # ── Polish P5: přechody prohlížeč <-> dlaždice ────────────────────────────

    @staticmethod
    def _grid_cell(grid, row, page, selected):
        """(obdélník fotky v dlaždici v souřadnicích page, poloměr rohů) jako ho kreslí delegát,
        nebo None, když dlaždice není na stránce vidět."""
        rect = QRectF(grid.visualRect(grid.photo_model.index(row, 0)))
        if rect.isEmpty():
            return None
        hover = max(0.0, min(1.0, grid.hover_progress(row)))   # ApplePhotoDelegate.paint
        inset = 4.0 - 3.0 * hover + (5.0 if selected else 0.0)
        radius = max(6.0, min(14.0, (rect.width() - 8.0) * 0.07)) - (3.0 if selected else 0.0)
        top_left = grid.viewport().mapTo(page, rect.topLeft().toPoint())
        cell = QRectF(top_left.x(), top_left.y(), rect.width(), rect.height()).adjusted(inset, inset, -inset, -inset)
        viewport = QRectF(QRect(grid.viewport().mapTo(page, QPoint(0, 0)), grid.viewport().size()))
        if not viewport.intersects(cell):
            return None
        return cell, radius

    def _viewer_transition(self, media_id):
        """Pro stažení dolů: (snímek stránky pod prohlížečem, cílová dlaždice, poloměr).
        Dlaždice se nejdřív označí a posune do pohledu – jako po zavření (_reveal_in_grid)."""
        model, viewer = self._viewer_model, self._viewer
        page = self.nav.previous(viewer) if viewer is not None else None
        grid = model.parent() if model is not None else None
        row = model.id_to_row.get(int(media_id)) if (model is not None and media_id is not None) else None
        if page is None or not isinstance(grid, AppleGridView) or row is None:
            return (page.grab() if page is not None else None), None, 0.0
        idx = model.index(row, 0)
        grid.setCurrentIndex(idx)
        grid.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)
        cell = self._grid_cell(grid, row, page, selected=True)
        backdrop = page.grab()
        if cell is None:
            return backdrop, None, 0.0
        return backdrop, cell[0], cell[1]

    def _hero_source(self, model, media_id):
        """Pro otevření z mřížky: (náhled dlaždice, obdélník, poloměr, snímek stránky), jinak None."""
        grid = model.parent() if model is not None else None
        page = self.stack.currentWidget()
        if not isinstance(grid, AppleGridView) or not grid.isVisible() or motion.reduced():
            return None
        row = model.id_to_row.get(int(media_id))
        thumb = model.data(model.index(row, 0), ApplePhotoModel.ThumbRole) if row is not None else None
        if thumb is None or thumb.isNull():
            return None
        selected = grid.selectionModel().isRowSelected(row, QModelIndex())
        cell = self._grid_cell(grid, row, page, selected)
        if cell is None:
            return None
        return thumb, cell[0], cell[1], page.grab()

    # ── Celá obrazovka a prezentace ───────────────────────────────────────────

    def _set_immersive(self, reason: str, on: bool):
        """Celá obrazovka bez postranního panelu; drží se, dokud ji chce prohlížeč nebo prezentace."""
        was = bool(self._immersive)
        (self._immersive.add if on else self._immersive.discard)(reason)
        now = bool(self._immersive)
        if now == was:
            return
        self.sidebar.setVisible(not now)
        if now:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        elif self._was_maximized:
            self.showMaximized()
        else:
            self.showNormal()

    def is_immersive(self):
        return bool(self._immersive)

    def start_slideshow_from_grid(self, grid):
        """S v mřížce: prezentace od vybrané (jinak první) položky aktuální mřížky."""
        records = grid.photo_model.records
        rows = sorted(grid.selected_row_indices())
        self._start_slideshow(records, rows[0] if rows else 0, grid.photo_model)

    def _start_slideshow(self, records, index=0, model=None):
        records = list(records)
        if not records:
            show_toast(self, "Prezentace nemá co ukázat – žádné položky", "info")
            return None
        from_viewer = self._viewer is not None and self.nav.current() is self._viewer
        view = SlideshowView(records, index)
        self.nav.push(view, on_pop=lambda v=view: self._on_slideshow_done(v, from_viewer, model))
        self._set_immersive("slideshow", True)
        view.start()
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        return view

    def _on_slideshow_done(self, view, from_viewer, model):
        view.stop()
        self._set_immersive("slideshow", False)
        if not view.records:
            return
        current = view.records[view.index]
        if from_viewer and self._viewer is not None and self.nav.current() is self._viewer:
            # zpět v prohlížeči na snímku, u kterého prezentace skončila
            self._viewer.set_media_data(view.records, view.index)
            self._viewer.setFocus(Qt.FocusReason.OtherFocusReason)
        elif model is not None:
            QTimer.singleShot(0, lambda: self._reveal_in_grid(model, int(current[0])))

    def _open_viewer(self, records, index: int, model=None, show_info=False):
        """Otevře prohlížeč v okně nad aktuální stránkou; ‹ Zpět / Esc vrátí na stejné místo."""
        if not (0 <= index < len(records)):
            return
        viewer = self._ensure_viewer()
        hero = self._hero_source(model, records[index][0]) if model is not None else None
        try:
            viewer.set_media_data(list(records), index)
        except Exception as e:
            show_toast(self, f"Soubor nelze otevřít: {e}", "error")
            return
        self._viewer_model = model
        if show_info and not viewer.btn_info.isChecked():
            viewer.btn_info.setChecked(True)
            viewer.toggle_info_panel()
        self.nav.push(viewer, on_pop=self._on_viewer_popped, dispose=False)
        if hero is not None:                   # Polish P5: fotka vyletí z dlaždice
            if viewer.size() != self.stack.size():
                viewer.setGeometry(self.stack.rect())
            viewer.layout().activate()
            viewer.play_hero_open(*hero)
        viewer.setFocus(Qt.FocusReason.OtherFocusReason)

    def _close_viewer(self):
        if self._viewer is not None and self.nav.contains(self._viewer):
            self.nav.push(self._viewer)   # odstraní případné stránky nad prohlížečem
            self.nav.pop()

    def _on_viewer_popped(self):
        viewer = self._viewer
        viewer.cleanup_playback()          # zvuk videa / Live Photo nesmí hrát v mřížce
        viewer.set_fullscreen(False)       # mřížka se vrací v normálním okně
        model = self._viewer_model
        if model is None or not viewer.media_list:
            return
        current_id = int(viewer.media_list[viewer.current_index][0])
        # Po listování šipkami označit v mřížce naposledy prohlíženou fotku (bez posunu, je-li vidět)
        QTimer.singleShot(0, lambda: self._reveal_in_grid(model, current_id))

    def _reveal_in_grid(self, model, media_id: int):
        grid = model.parent()
        row = model.id_to_row.get(media_id)
        if not isinstance(grid, AppleGridView) or row is None or not grid.isVisible():
            return
        idx = model.index(row, 0)
        grid.setCurrentIndex(idx)
        grid.scrollTo(idx, QAbstractItemView.ScrollHint.EnsureVisible)
        grid.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_favorite_changed(self, media_id: int, is_fav: int, model=None):
        for rows in (self._all_records, self._filtered_records):
            for i, rec in enumerate(rows):
                if int(rec[0]) == media_id:
                    rows[i] = with_favorite(rec, is_fav)
        self.grid_view.photo_model.set_favorite(media_id, is_fav)
        if model is not None and model is not self.grid_view.photo_model:
            model.set_favorite(media_id, is_fav)

    def _show_photo_context_menu(self, pos):
        rows = self.grid_view.selected_row_indices()
        if not rows:
            return
        menu = QMenu(self)
        act_fav = menu.addAction("Přidat/odebrat oblíbené (F)")
        act_open = menu.addAction("Otevřít")
        menu.addSeparator()
        act_info = menu.addAction("Informace o souboru")
        menu.addSeparator()
        in_trash = self._current_filter == "trash"
        act_trash = menu.addAction("Obnovit" if in_trash else "Přesunout do koše")
        action = menu.exec(self.grid_view.mapToGlobal(pos))
        if action == act_trash:
            self._set_trashed_for_rows(rows, not in_trash)
        elif action == act_fav:
            self.toggle_favorite_on_selection()
        elif action == act_open and rows:
            self._on_photo_activated(rows[0])
        elif action == act_info and rows:
            self._show_file_info(rows[0])

    def _set_trashed_for_rows(self, rows, trashed: bool):
        ids = [int(self._filtered_records[r][0]) for r in rows
               if 0 <= r < len(self._filtered_records)]
        try:
            (trash_media if trashed else restore_media)(ids)
        except Exception as e:
            show_toast(self, f"Akci se nepodařilo provést: {e}", "error")
            return
        self._on_library_changed()
        n = len(ids)
        if trashed:
            show_toast(self, f"Přesunuto do koše: {_czech_item_count(n)}", "success",
                       action_text="Vrátit", on_action=lambda: self._undo_trash(ids))
        else:
            show_toast(self, f"Obnoveno z koše: {_czech_item_count(n)}", "success")

    def _undo_trash(self, ids):
        try:
            restore_media(ids)
        except Exception as e:
            show_toast(self, f"Obnovení se nepodařilo: {e}", "error")
            return
        self._on_library_changed()

    def _on_library_changed(self):
        """Po koši / obnovení: mřížka, události, osoby i mapa musí sedět s DB."""
        self._load_photos()
        self._load_events()
        self._load_people()
        if self.stack.currentWidget() is self.page_map:
            self._refresh_map()

    def _show_trash(self):
        self._switch_page(0)
        self._open_collection("trash")

    def toggle_favorite_on_selection(self, grid=None):
        """F v mřížce: přepne srdíčko u výběru – v hlavní mřížce i v mřížce osoby / události."""
        from database import toggle_favorite
        grid = grid or self.grid_view
        records = grid.photo_model.records
        for row in grid.selected_row_indices():
            if 0 <= row < len(records):
                item_id = int(records[row][0])
                try:
                    new_fav = toggle_favorite(item_id)
                except Exception as e:
                    print(f"[Fav] {e}")
                    continue
                if grid is not self.grid_view:
                    grid.photo_model.set_favorite(item_id, new_fav)
        self._load_photos()

    def _show_file_info(self, row: int):
        """Informace o souboru = panel Info v prohlížeči (místo modálního okna s cestou)."""
        if row < 0 or row >= len(self._filtered_records):
            return
        self._open_viewer(self._filtered_records, row, self.grid_view.photo_model, show_info=True)

    # ── People actions ────────────────────────────────────────────────────────

    def _on_person_card_clicked(self, person_id: int, name: str):
        try:
            # IN místo JOIN: dva obličeje téže osoby na jedné fotce = fotka jen jednou
            records = fetch_media_records(
                "m.id IN (SELECT media_id FROM face_instances WHERE person_id = ?)", (person_id,))
        except Exception as e:
            print(f"[PersonClick] {e}")
            records = []

        # Fotky osoby = podstránka v okně (dřív modální dialog)
        page = DetailPage(name, _czech_photo_count(len(records)))
        page.add_action("Přejmenovat", lambda: self._rename_person(person_id, page.lbl_title.text(), page))
        page.add_action("Hraniční obličeje…",
                        lambda: self._open_borderline_for_person(person_id, page.lbl_title.text()),
                        "Projít obličeje, které se této osobě jen trochu podobají")
        grid = self._make_collection_grid(records, page)
        self._add_slideshow_action(page, grid)
        page.set_content(grid)
        self.nav.push(page)

    def _add_slideshow_action(self, page, grid):
        btn = page.add_action("▶  Prezentace", lambda: self.start_slideshow_from_grid(grid),
                              "Prezentace přes celou obrazovku (S)")
        btn.setObjectName("btn_primary")
        btn.setEnabled(grid.photo_model.rowCount() > 0)
        return btn

    def _make_collection_grid(self, records, parent):
        grid = AppleGridView(self, parent)
        grid.set_cell_size(self.grid_view.cell_size)
        grid.photo_model.set_records(records)
        grid.item_activated.connect(
            lambda row, g=grid: self._open_viewer(g.photo_model.records, row, g.photo_model))
        return grid

    def _rename_person(self, person_id: int, current_name: str, page=None):
        new_name, ok = InWindowDialog.getText(
            self, "Přejmenovat osobu", "Nové jméno:", text=current_name)
        if ok and new_name.strip():
            try:
                from database import db_transaction
                with db_transaction() as conn:
                    conn.execute("UPDATE people SET name=? WHERE id=?",
                                 (new_name.strip(), person_id))
                confirm_person(person_id)    # přejmenovaná osoba = kotva, přeshlukování ji nesmaže
                self._load_people()
                if isinstance(page, DetailPage):
                    page.set_title(new_name.strip())
            except Exception as e:
                show_toast(self, f"Osobu nelze přejmenovat: {e}", "error")

    def _recluster_faces(self):
        threshold = float(self.config.get("face_similarity_threshold", 0.62))
        self.lbl_status.setText("Přeshlukovávám obličeje...")
        QCoreApplication.processEvents()
        try:
            stats = strict_recluster_faces(threshold=threshold,
                                           merge_threshold=self.config.get("face_merge_threshold"))
            invalidate_centroid_cache()
            self._load_people()
            if stats:
                persons = int(stats.get("anchors", 0)) + int(stats.get("new_people", 0))
                show_toast(self, f"Přeshlukování dokončeno – {persons} osob, "
                                 f"{stats.get('faces', 0)} obličejů", "success")
            else:
                show_toast(self, "Žádné obličeje k přeshlukování", "info")
        except Exception as e:
            show_toast(self, f"Přeshlukování selhalo: {e}", "error")
        finally:
            self.lbl_status.setText("")

    def _open_borderline_review(self):
        try:
            conn = get_connection()
            try:
                persons = conn.execute(
                    "SELECT id, name FROM people WHERE name != ? ORDER BY name", (UNKNOWN_PERSON,)
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            show_toast(self, f"Osoby nelze načíst: {e}", "error")
            return
        if not persons:
            show_toast(self, "Hraniční obličeje se hledají u pojmenovaných osob – "
                             "nejdřív některou osobu pojmenujte", "info")
            return
        # První osoba, která má hraniční obličeje
        for pid, pname in persons:
            faces = get_borderline_faces_for_person(pid)
            if faces:
                self._push_borderline_review(pid, pname, faces)
                return
        show_toast(self, "Žádné hraniční obličeje nenalezeny", "info")

    def _open_borderline_for_person(self, person_id: int, name: str):
        faces = get_borderline_faces_for_person(person_id)
        if not faces:
            show_toast(self, f"Žádné hraniční obličeje pro {name}", "info")
            return
        self._push_borderline_review(person_id, name, faces)

    def _push_borderline_review(self, person_id: int, name: str, faces):
        view = BorderlineReviewView(person_id, name, faces)
        page = DetailPage("Hraniční obličeje", name)
        page.set_content(view)
        view.finished_review.connect(page.back_requested.emit)
        self.nav.push(page, on_pop=lambda v=view: self._on_borderline_done(v))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        return view

    def _on_borderline_done(self, view):
        if view.assigned:
            self._load_people()
            show_toast(self, f"{view.person_name}: potvrzeno obličejů – {view.assigned}", "success")

    def _open_duplicate_review(self):
        self.lbl_status.setText("Hledám duplicity...")
        QCoreApplication.processEvents()
        try:
            from ai_engine import get_duplicate_pairs
            # Doplní chybějící / zastaralé otisky (jinak by nové importy v detekci chyběly)
            refresh_phashes(progress=self._on_phash_progress)
            conn = get_connection()
            try:
                pairs = get_duplicate_pairs(conn)
            finally:
                conn.close()
        except Exception as e:
            show_toast(self, f"Duplicity nelze vyhledat: {e}", "error")
            self.lbl_status.setText("")
            return
        self.lbl_status.setText("")
        if not pairs:
            show_toast(self, "Žádné duplicitní fotografie nenalezeny", "success")
            return
        self._push_duplicate_review(pairs)

    def _push_duplicate_review(self, pairs):
        view = DuplicateReviewView(pairs)
        n = len(view.pairs)
        page = DetailPage("Duplicity", f"{n} {'možný pár' if n == 1 else 'možné páry' if n < 5 else 'možných párů'}")
        page.set_content(view)
        view.finished_review.connect(page.back_requested.emit)
        self.nav.push(page, on_pop=lambda v=view: self._on_duplicates_done(v))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        return view

    def _on_duplicates_done(self, view):
        if not view.changed:
            return
        self._on_library_changed()
        trashed = view.trashed_count()
        if trashed:
            show_toast(self, f"Přesunuto do koše: {_czech_item_count(trashed)}", "success",
                       action_text="Zobrazit koš", on_action=self._show_trash)

    def _on_phash_progress(self, i: int, n: int):
        if i == n or i % 25 == 0:
            self.lbl_status.setText(f"Počítám otisky ({i}/{n})...")
            QCoreApplication.processEvents()

    # ── Events ────────────────────────────────────────────────────────────────

    def _open_event(self, event_id: int):
        """Karta události -> podstránka s mřížkou jejích fotek (DetailPage + AppleGridView)."""
        event = next((e for e in self._event_cards if int(e["id"]) == int(event_id)), None)
        try:
            records = fetch_media_records("m.event_id = ?", (event_id,), order_by="m.date_taken, m.id")
        except Exception as e:
            print(f"[Event] {e}")
            records = []
        if not records:
            show_toast(self, "Událost neobsahuje žádné viditelné položky", "info")
            return
        title = split_event_title((event or {}).get("title") or "Událost")[1]
        start, end = (event or {}).get("start"), (event or {}).get("end")
        location = (event or {}).get("location")
        page = DetailPage(title, " · ".join(filter(None, [location, _format_date_range(start, end),
                                                          _czech_item_count(len(records))])))
        page.set_ambient(self._event_glow(event))               # Polish P4: odstín obálky v hlavičce
        grid = self._make_collection_grid(records, page)
        if (event or {}).get("journey"):
            page.add_action("Cesta", lambda: self._request_journey(event_id), "Přehrát cestu událostí na mapě")
        self._add_slideshow_action(page, grid)
        page.set_content(grid)
        self.nav.push(page)

    # ── Import ────────────────────────────────────────────────────────────────

    def _start_import(self):
        folder = QFileDialog.getExistingDirectory(self, "Vyberte složku s fotografiemi")
        if not folder:
            return
        self.config["library_folders"] = list(set(
            self.config.get("library_folders", []) + [folder]
        ))
        save_config(self.config)
        self._run_scan(folder)

    def _run_scan(self, folder: str):
        if self._scanner and self._scanner.isRunning():
            show_toast(self, "Import už probíhá", "info")
            return
        self.nav.reset()
        self.stack.setCurrentIndex(4)
        self._scan_start_time = time.monotonic()
        self.progress_widget.set_progress(0, 1)
        self.progress_widget.set_status("Spouštím import...")

        self._scanner = MediaScannerThread(folder)
        self._scanner.progress_signal.connect(self._on_scan_progress)
        self._scanner.status_signal.connect(self.progress_widget.set_status)
        self._scanner.finished_signal.connect(self._on_scan_finished)
        self._scanner.start()

    def _on_scan_progress(self, current: int, total: int):
        self.progress_widget.set_progress(current, total)
        elapsed = time.monotonic() - (self._scan_start_time or time.monotonic())
        est = (elapsed / current * total) if current > 0 else 0
        self.progress_widget.set_time_info(int(elapsed), int(est))

    def _on_scan_finished(self, processed: int = 0):
        self.stack.setCurrentIndex(0)
        self._nav_buttons[0].setChecked(True)
        sync_geo_index()   # R*Tree se jinak plnil jen při startu -> nové fotky by na mapě chyběly
        self._load_photos()
        self._refresh_memory()
        self._load_events()
        self._start_ai_daemon()
        if processed:
            show_toast(self, f"Import dokončen: {_czech_item_count(processed)}", "success")
        if self._background_import:
            QTimer.singleShot(0, self._resume_background_queue)

    def _cancel_import(self):
        # MediaScannerThread má stop(), ne cancel() (AttributeError ve slotu = pád PyQt6).
        # Při zrušení se finished_signal nevysílá -> po doběhnutí vlákna načteme, co se stihlo uložit.
        if self._scanner and self._scanner.isRunning():
            self._scanner.stop()
            self._scanner.finished.connect(sync_geo_index)
            self._scanner.finished.connect(self._load_photos)
            self._scanner.finished.connect(self._load_events)
            self._scanner.finished.connect(lambda: QTimer.singleShot(0, self._resume_background_queue))
        self.stack.setCurrentIndex(0)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        old_theme = self.config.get("theme", theme.DEFAULT_THEME)
        old_accent = (self.config.get("accent"), self.config.get("accent_custom"))
        old_memories = self.config.get("show_memories", True)
        dlg = SettingsDialog(self)
        accepted = dlg.exec()
        if dlg.trash_emptied:                  # proběhlo i při Zrušit – knihovna musí sedět s DB
            self._on_library_changed()
        if getattr(dlg, "guide_requested", False) is True:   # Předvolby › Nápověda › Tipy a průvodce
            QTimer.singleShot(0, self.open_guide)
        if not accepted:
            return
        self.config = load_config()
        motion.configure(self.config)       # Omezit animace (Předvolby)
        if bool(self.config.get("adaptive_accent", True)) != tokens.adaptive():
            tokens.set_adaptive(self.config.get("adaptive_accent", True))    # Přizpůsobit obsahu
            for card in getattr(self, "event_cards", []):
                card.update()
            self.memory_card.update()
        # P3.9 resize LRU cache
        cap = int(self.config.get("thumbnail_lru_capacity", 1200))
        self.grid_view.photo_model.resize_cache(cap)
        new_theme = self.config.get("theme", theme.DEFAULT_THEME)
        new_accent = (self.config.get("accent"), self.config.get("accent_custom"))
        if new_accent != old_accent:
            tokens.configure(self.config)
        if new_theme != old_theme or new_accent != old_accent:
            theme.apply(new_theme, self)
            window_effects.style_for_theme(self)
            self._refresh_theme_icons()
            self._sync_accent()                 # trasa Cesty, odznaky a tlačítka mapy bez obnovení
            self._load_people()                 # karty osob se kreslí offscreen v barvách motivu
            self.grid_view.viewport().update()
        if dlg.recluster_requested:
            self._recluster_faces()
        self.folder_watcher.set_roots(self._watched_roots())   # sledování zapnuté / vypnuté u složek
        if self.config.get("show_memories", True) != old_memories:
            self._refresh_memory()             # Vzpomínky zapnuté / vypnuté v Předvolbách
        # Restart démona s novým prahem / rozpočtem: zastavit a spustit znovu, až doběhne.
        # Dřív wait(2000) vypršel (rozdělaná položka), _start_ai_daemon se vrátil hned a démon
        # zůstal do restartu aplikace mrtvý (Sprint R).
        if self._ai_daemon and self._ai_daemon.isRunning():
            self._ai_daemon.stop()
        # snížený limit kopií videí platí hned (jen mazání nejdéle nepoužitých souborů)
        video_proxy.enforce_budget(int(self.config.get("proxy_cache_mb", video_proxy.DEFAULT_BUDGET_MB)))
        self._start_ai_daemon()

    # ── Vzpomínky (Polish P2) ─────────────────────────────────────────────────

    def _refresh_memory(self, today=None):
        """Vzpomínka na dnešek na pozadí (memories.MemoryJob); další přepočet těsně po půlnoci."""
        self._memory_midnight.start(memories.ms_to_midnight())
        if not self.config.get("show_memories", True):
            self._on_memory_ready((None, []))
            return
        job = memories.MemoryJob(get_connection, today)
        job.signals.done.connect(self._on_memory_ready)
        self._memory_job = job               # drží signály naživu, dokud výpočet neskončí
        QThreadPool.globalInstance().start(job)

    def _on_memory_ready(self, result):
        memory, images = result
        self._memory_job = None
        self._memory = memory
        self.memory_card.set_memory(memory, images)
        self._sync_memory_card()

    def _sync_memory_card(self):
        box = getattr(self, "memory_box", None)
        if box is None:
            return
        show = (self._memory is not None and self.config.get("show_memories", True)
                and self.config.get("memories_dismissed") != datetime.now().date().isoformat()
                and self._current_filter == "all" and not self._search_query.strip())
        box.setVisible(bool(show))

    def _dismiss_memory(self):
        """✕ na kartě: skrýt jen pro dnešek (zítra může přijít jiná vzpomínka)."""
        self.config["memories_dismissed"] = datetime.now().date().isoformat()
        save_config(self.config)
        self._sync_memory_card()
        show_toast(self, "Vzpomínka se zítra objeví znovu", "info")

    # ── Tipy a průvodce (Polish P2) ───────────────────────────────────────────

    def open_guide(self, chapter_id=None):
        """Stránka Tipy a průvodce (F1, postranní panel, Předvolby). Otevřená se jen přepne na kapitolu."""
        self._close_spotlight()
        page = next((p for p in self.stack.findChildren(guide.GuidePage) if self.nav.contains(p)), None)
        if page is None:
            page = guide.GuidePage(chapter_id)
            page.show_requested.connect(self.show_tip)
            self.nav.push(page)
        else:
            self.nav.push(page)              # už je v zásobníku -> zavře, co je nad ní
            if chapter_id:
                page.show_chapter(chapter_id)
        return page

    def show_tip(self, tip):
        """„Ukázat“: zavře podstránky, přejde na stránku kotvy a zvýrazní ji (SpotlightOverlay)."""
        if tip.page is not None:
            self._switch_page(tip.page)
        else:
            self.nav.reset()
        QTimer.singleShot(0, lambda: self._spotlight_tip(tip))   # po přepnutí stránky a rozložení

    def _spotlight_tip(self, tip):
        target = guide.find_anchor(self, tip.anchor)
        if target is None:
            show_toast(self, f"{tip.title}: {tip.body}", "info")
            return None
        self._close_spotlight()
        self._spotlight = guide.SpotlightOverlay(self.centralWidget(), target, tip.title, tip.body)
        self._spotlight.closed.connect(self._on_spotlight_closed)
        return self._spotlight

    def _on_spotlight_closed(self):
        self._spotlight = None

    def _close_spotlight(self):
        spot, self._spotlight = self._spotlight, None
        if spot is not None:
            try:
                spot.close()
            except RuntimeError:
                pass

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._shutting_down = True           # démon se už znovu nespustí (_start_ai_daemon)
        self.drop_router.detach()            # filtr tažení je na celé aplikaci
        self.folder_watcher.set_roots([])
        self._background_import = []         # rozpracovaný sken doběhne, další se už nespustí
        if self._viewer is not None:
            self._viewer.cleanup_playback()
        if self._media_warmup is not None:
            self._media_warmup.wait(3000)
        if self._import_probe is not None and self._import_probe.isRunning():
            self._import_probe.stop()
            self._import_probe.wait(3000)
        # Dekódování na pozadí: zahodit čekající úlohy a počkat na běžící – ukončení procesu
        # uprostřed dekódování HEIC nekončí čistě (ověřeno: exit code 5)
        pipelines = [self._event_images] + ([self._viewer._images] if self._viewer is not None else [])
        pipelines += [v._images for v in self.findChildren(DuplicateReviewView)]
        for pipe in pipelines:
            pipe.pool.clear()
        for pipe in pipelines:
            pipe.pool.waitForDone(3000)
        # Démon a sken: zastavit a počkat. Kdo do limitu nedoběhne (rozdělaná položka), přejde
        # do úschovy a shutdown_app na něj počká před zánikem QApplication – dřív se okno
        # smazalo s běžícím QThread a proces při ukončení padal (Sprint R).
        for thread in (self._ai_daemon, self._scanner):
            if thread is not None and thread.isRunning():
                thread.stop()
                if not thread.wait(3000):
                    park_thread(thread)
        for worker in list(self._search_workers):
            worker.wait()   # hledání vždy doběhne; zničení běžícího QThread = pád
        self._search_workers.clear()
        if self._search_warmup is not None:
            self._search_warmup.wait()          # načítání modelu nejde přerušit; zničení běžícího QThread = pád
        self._person_card_pool.waitForDone(2000)
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def main(app=None, splash=None):
    """Hlavní okno. app/splash předává splash.boot(); bez nich (KAIROS_NO_SPLASH) se vytvoří tady."""
    if app is None:
        app = QApplication(sys.argv)
        app.setApplicationName(brand.APP_NAME)
        app.setOrganizationName(brand.APP_NAME)
        brand.set_windows_app_id()
        app.setWindowIcon(brand.app_icon())
    app.setStyle("Fusion")
    ui_font = app.font()
    ui_font.setFamily(tokens.ui_family())     # Segoe UI Variable (Windows 11), jinak Segoe UI
    app.setFont(ui_font)

    # Paleta a styly podle motivu (theme.py) a zvýraznění (tokens.py); nové widgety přestyluje filtr
    cfg = load_config()
    tokens.configure(cfg)
    motion.configure(cfg)
    theme.apply(cfg.get("theme", theme.DEFAULT_THEME))

    window = MainWindow()
    window_effects.style_for_theme(window)    # titulkový pruh v barvě aplikace (Windows 11)
    window.show()
    if splash is not None:
        splash.finish(window)
    ret = app.exec()
    shutdown_app(app, window)                # pevné pořadí úklidu (pád v sip při ukončení)
    del window
    if _parked_threads:
        # vlákno visí i po limitu: zničení běžícího QThread při ukončení = qFatal (pád).
        # Data jsou v transakcích SQLite, ffmpeg zabije Job Object -> tvrdý konec je bezpečný.
        sys.stdout.flush()
        os._exit(ret)
    sys.exit(ret)


if __name__ == "__main__":
    main()
