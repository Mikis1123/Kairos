"""Kairos – hlavní okno aplikace (Patch 3).

P3.1 Asynchronní karty osob | P3.2 Debounce vyhledávání | P3.3 Přesun obličeje
P3.4 Mrtvý kód odstraněn    | P3.5 Duplicity dialog     | P3.6 Borderline Review
P3.7 Nastavení prahu        | P3.8 Konfig. práh         | P3.9 Dynamická LRU cache
"""
import boot_trace  # noqa: E402,F401 – first: cold-boot clock (pillar 5)
import sys, os, time, json, math, subprocess, unicodedata
if __name__ == "__main__":
    # An exception in a Qt slot must not kill the app silently (PyQt6 default = qFatal, exit 127
    # without a word under pythonw): log it to logs/crash.log and keep running.
    import crash_guard
    import paths as _paths
    crash_guard.install(_paths.LOG_DIR)
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
def _launch_intro_wanted():
    """Pillar 5: the 1.5 s brand intro gates the window, so it is opt-in (config
    "launch_intro"); a normal launch goes straight to the grid. KAIROS_SPLASH=1 forces it."""
    if os.environ.get("KAIROS_NO_SPLASH"):
        return False
    if os.environ.get("KAIROS_SPLASH") == "1":
        return True
    try:
        import data_migration
        data_migration.ensure_ready()           # config lives in AppData (Gate 0)
        from config import load_config
        return bool(load_config().get("launch_intro", False))
    except Exception:
        return False


if __name__ == "__main__" and _launch_intro_wanted():
    import splash
    sys.exit(splash.boot())
if __name__ == "__main__":
    # Gate 0 bez úvodní obrazovky: migrace dat do AppData dřív, než importy níže převezmou cesty
    import data_migration
    data_migration.ensure_ready()

import i18n
from i18n import N_, event_title, fmt_number, tr

from paths import (
    BASE_DIR, RESOURCE_DIR, THUMB_DIR as CACHE_DIR, FFMPEG_PATH as DEFAULT_FFMPEG, resolve_face_path,
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QListWidget, QListWidgetItem, QLabel,
    QProgressBar, QStackedWidget, QComboBox, QButtonGroup, QLineEdit,
    QAbstractItemView, QInputDialog, QListView, QStyledItemDelegate,
    QStyleOptionViewItem, QStyle, QSlider, QFrame, QDialog, QScrollArea,
    QMenu, QMessageBox, QSizePolicy, QLayout, QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import (
    Qt, QSize, QObject, pyqtSlot, QTimer, QThread, pyqtSignal,
    QUrl, QRect, QRectF, QCoreApplication, QAbstractListModel,
    QModelIndex, QThreadPool, QRunnable, QPoint, QPointF, QEvent, QSizeF,
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QImage, QFont, QPainter, QColor, QKeyEvent,
    QPainterPath, QLinearGradient, QRadialGradient, QPen, QKeySequence, QShortcut, QCursor, QImageReader,
    QImageIOHandler, QPolygonF, QPaintEvent,
)
# QtWebEngine (Qt6WebEngineCore.dll, ~200 MB) is imported at first use only – see web_engine.py
import web_engine

from database import (
    init_db, get_connection, save_image_embedding, get_all_embeddings, get_media_needing_embedding,
    get_media_pending_face_scan, save_faces_for_media, relocate_face_paths,
    visible_media_sql, query_geo_bounds, sync_geo_index, trash_media, restore_media,
    dismiss_duplicate_pair, undismiss_duplicate_pair, purge_orphan_test_rows,
    get_search_vectors, get_videos_needing_frames, save_video_frame_embeddings, db_transaction,
    get_media_needing_ocr, save_ocr_results,
    get_media_needing_siglip, save_siglip_embeddings, siglip_coverage, get_siglip_vectors,
)
from scanner import MediaScannerThread, repair_missing_thumbnails
import cloud_files
import media_identity
import power
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
                         AmbientCycler, AmbientFramesJob, CoverVirtualizer)
import preview_cache
import theme
import tokens
import motion
import ui_controls
import kinetic_scroll
import decode_queue
import micro_previews
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
import ambient_music
import threading
import card_media
import tile_store
import map_precache
import map_host
import route_corridor
import mood
import gpx_tracks
import trips
import video_proxy
import video_frames
import xmp_sync
import albums
import locked_folder
from locked_ui import LockedFolderController
import command_palette
from command_palette import Command
HOVER_PRELOAD_MS = 350      # setrvání kurzoru nad videem v mřížce před přednačtením
SMART_IMPORT_DELAY_MS = 4000 # kontrola nových fotek až po usazení startu
import face_engine

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass
import raw_decode  # noqa: E402  ProRAW / camera RAW through every Image.open (raw_decode.py)
raw_decode.register()


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
                        trashed=False, hidden=False):
    """Vrátí záznamy ve tvaru MEDIA_COLUMNS bez pohybových videí Live Photos.
    trashed=False: knihovna (bez koše), trashed=True: jen položky v koši.
    hidden=True: jen obsah složky Skryté (Stage 3) – jinak se skryté nevracejí nikdy."""
    trash_cond = "COALESCE(m.is_trashed, 0) = 1" if trashed else "COALESCE(m.is_trashed, 0) = 0"
    hidden_cond = "COALESCE(m.is_hidden, 0) = 1" if hidden else "COALESCE(m.is_hidden, 0) = 0"
    sql = (f"SELECT {MEDIA_COLUMNS} FROM media_items m WHERE {NOT_MOTION_CLIP} AND {trash_cond} "
           f"AND {hidden_cond}")
    if where:
        sql += f" AND ({where})"
    sql += f" ORDER BY {order_by}"
    conn = get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def query_people():
    """Rows (id, name, cover, face count) of the People page. Opens its own connection, so it
    runs on any thread (bg_jobs)."""
    conn = get_connection()
    try:
        # i18n-ok: SQL
        return conn.execute("""
            SELECT p.id, p.name, p.cover_face_path,
                   COUNT(f.id) as face_count
            FROM people p
            LEFT JOIN face_instances f ON f.person_id = p.id AND COALESCE(f.is_valid, 1) = 1
                 AND f.media_id IN (SELECT id FROM media_items WHERE COALESCE(is_trashed, 0) = 0
                                    AND COALESCE(is_hidden, 0) = 0)
            GROUP BY p.id
            HAVING COUNT(f.id) > 0             -- osoba jen ze Skrytých / z koše se neukáže
            ORDER BY p.name COLLATE NOCASE
        """).fetchall()
    finally:
        conn.close()


def query_events():
    """(cards, trip ids, covers without a stored tint) of the Events page; any thread."""
    conn = get_connection()
    try:
        cards = fetch_event_cards(conn)
        trips = {t["id"] for t in journey.list_journeys(conn)}
        tint_todo = tint.stale_items(conn, cards)      # odstíny obálek (uložené se jen načtou)
        return cards, trips, tint_todo
    finally:
        conn.close()


def query_library(unlocked):
    """(edits, library, trash, hidden) for the grids; any thread."""
    import quick_edit
    conn = get_connection()
    try:
        edits = quick_edit.load_many(conn)
    finally:
        conn.close()
    return (edits, fetch_media_records(), fetch_media_records(trashed=True),
            fetch_media_records(hidden=True) if unlocked else [])


def siglip_ready_state(wanted):
    """Sprint 4.0: is the SigLIP 2 index complete enough to answer searches? Any thread."""
    import ai_engine
    if not (wanted and ai_engine.siglip_available()):
        return False
    have, need = siglip_coverage()
    return need > 0 and have >= SIGLIP_READY_SHARE * need


def load_search_state(wanted):
    """(siglip ready, {id: vectors}) – the search index the app should use; any thread."""
    try:
        ready = siglip_ready_state(wanted)
    except Exception as e:
        print(f"[SigLIP] {e}")
        ready = False
    return ready, (get_siglip_vectors() if ready else get_search_vectors())


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
    viewer.tile_service = mini_map_tile_service    # Sprint 3: native mini-map reads the tile store
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


def map_refresh_payload():
    """(setMarkers JS, setJourneys JS) for the map page; any thread (own DB connections). A part
    that fails is None (logged), the other is still sent."""
    markers_js = trips_js = None
    try:
        markers = build_map_markers(query_geo_bounds(-90, 90, -180, 180))
        markers_js = f"window.setMarkers && window.setMarkers({json.dumps(markers)});"
    except Exception as e:
        print(f"[Map] {e}")
        return None, None
    try:
        conn = get_connection()
        try:
            trips = journey.list_journeys(conn)
        finally:
            conn.close()
        trips_js = f"window.setJourneys && window.setJourneys({json.dumps(trips)});"
    except Exception as e:
        print(f"[Map] Cesty: {e}")
    return markers_js, trips_js


# Jediné povolené vzdálené zdroje webových stránek aplikace (mapa; minimapa prohlížeče je od Sprintu 3 nativní – mini_map.py):
# dlaždice podkladů z přepínače mapy (OpenFreeMap, Esri, OpenTopoMap). Dlaždice prozrazují
# jen zobrazenou oblast mapy, nikdy id ani metadata fotek.
ALLOWED_WEB_HOSTS = frozenset({
    "server.arcgisonline.com",       # Základní (Esri World Street Map) + Satelitní + nativní minimapa prohlížeče
    "tile.opentopomap.org",          # Turistická: vrstevnice a stínování terénu
    "tile.waymarkedtrails.org",      # Turistická: značené pěší a cyklistické trasy
    "tiles.openfreemap.org",         # vektorové podklady mapy (styl, dlaždice, písma, ikony)
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
    if scheme == "http":                # C7e: jen místní úložiště dlaždic této aplikace
        srv = tile_store.service()
        return srv is not None and srv.owns(u.host(), u.port())
    return scheme == "https" and u.host().lower() in ALLOWED_WEB_HOSTS


_interceptor_cls = None


def _web_url_interceptor_class():
    """QWebEngineUrlRequestInterceptor subclass, defined on first use (lazy QtWebEngine)."""
    global _interceptor_cls
    if _interceptor_cls is None:
        from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

        class WebUrlInterceptor(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):
                if not is_allowed_web_url(info.requestUrl()):
                    info.block(True)

        _interceptor_cls = WebUrlInterceptor
    return _interceptor_cls


def __getattr__(name):                     # main.WebUrlInterceptor for callers outside this module
    if name == "WebUrlInterceptor":
        return _web_url_interceptor_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    tile_store.stop()                       # C7e: místní server dlaždic + dopsání fronty zápisů
    if "sync_server" in sys.modules:
        sys.modules["sync_server"].stop()   # iOS LAN bridge (only imported when enabled)
    try:
        xmp_sync.uninstall()                # dopsat čekající XMP sidecary
    except Exception as e:
        print(f"[Shutdown] {e}")
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
        import paths as _paths_mod
        path = frame_meter.write_log(os.path.join(_paths_mod.LOG_DIR, "perf_logs"))
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
            from PyQt6.QtWebEngineCore import QWebEngineProfile
            # rodič = aplikace: profil musí přežít všechny své stránky
            profile = QWebEngineProfile("omni_map", QApplication.instance())
            profile.setPersistentStoragePath(os.path.join(root, "storage"))
            profile.setCachePath(os.path.join(root, "http"))
            profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
            profile.setHttpCacheMaximumSize(MAP_HTTP_CACHE_MB * 1024 * 1024)
            profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
            profile._interceptor = _web_url_interceptor_class()(profile)
            profile.setUrlRequestInterceptor(profile._interceptor)
            _map_profile = profile
        except Exception as e:
            print(f"[Map] trvalý profil nelze vytvořit, použije se výchozí: {e}")
            return None
    return _map_profile


def install_tile_store(page, budget_mb=tile_store.DEFAULT_BUDGET_MB):
    """C7e: spustí místní úložiště dlaždic a stránce ho ohlásí PŘED jejími skripty
    (window.TILE_STORE_CONFIG) – i první dlaždice nově vzniklé mapy jdou přes něj.
    Bez úložiště zůstává mapa u přímých adres serverů. Vrací službu nebo None."""
    srv = tile_store.start(budget_mb)
    if srv is None:
        return None
    return _inject_tile_store(page, srv)


def mini_map_tile_service():
    """Native mini-map of the viewer (mini_map.py, worker thread): the running tile store, which
    keeps its budget; when the Map tab wasn't opened this session it is started with the budget
    from Preferences. None = no store (the mini-map shows its background and the pin)."""
    srv = tile_store.service()
    if srv is None:
        srv = tile_store.start(int(load_config().get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
    return srv


def _inject_tile_store(page, srv):
    from PyQt6.QtWebEngineCore import QWebEngineScript
    script = QWebEngineScript()
    script.setName("kairos-tile-store")
    script.setSourceCode(f"window.TILE_STORE_CONFIG = {json.dumps(srv.config())};")
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    page.scripts().insert(script)
    return srv


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

    def peek(self, key):
        """Like get() without refreshing the entry (prefetch checks must not reorder the LRU)."""
        return self.cache.get(key)

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

    def discard(self, keys):
        for key in keys:
            if self.cache.pop(key, None) is not None:
                self.bytes -= self._sizes.pop(key, 0)

    def resize(self, new_capacity: int):
        self.capacity = max(64, int(new_capacity))
        self._evict()


# ── Thumbnail worker ──────────────────────────────────────────────────────────

THUMB_LOADING = "loading"
THUMB_OK = "ok"
THUMB_MISSING_FILE = "missing"        # originál na disku není (smazaný / odpojený disk)
THUMB_UNAVAILABLE = "unavailable"     # soubor je, ale náhled se nepodařilo načíst


# Stage 2: miniatury na disku mají kratší stranu 840 px (scanner.THUMB_SHORT_EDGE). Do RAM se
# berou jen v rozlišení, které dlaždice potřebuje (kratší strana ve fyzických px) – hustá
# mřížka (14 sloupců) tak nedrží 4 MB na dlaždici, velké dlaždice (3 sloupce) nejsou rozmazané.
THUMB_TIERS = (256, 512, 840)
PRETILE_BYTES = 96 * 1024 * 1024      # grid tiles pre-rendered by the thumbnail workers
DECODE_THREADS_REST = 4               # thumbnail decoders while the grid rests
DECODE_THREADS_FLING = 2              # ...and while it glides (the GUI thread keeps its frame budget)


def thumb_tier(cell_px_physical):
    """Nejmenší úroveň, která pokryje dlaždici (fyzické px), jinak největší."""
    need = int(math.ceil(max(1.0, float(cell_px_physical))))
    return next((t for t in THUMB_TIERS if t >= need), THUMB_TIERS[-1])


def cache_decode_divisor(w, h, tier, headroom=1.5):
    """Largest JPEG DCT scale divisor (1, 2, 4, 8) that still leaves the short edge >= headroom×tier."""
    short = min(int(w), int(h))
    for d in (8, 4, 2):
        if short / d >= tier * headroom:
            return d
    return 1


def _shrink_short_edge(img, short):
    """QImage zmenšený (Lanczos3, imaging) tak, aby kratší strana byla `short` (menší se nezvětšuje)."""
    import imaging
    return imaging.shrink_qimage_short_edge(img, short)


# Pillar 2: Quick Edits shown in every grid. media id -> quick_edit.Adjustments (edited items
# only), filled by MainWindow._load_edits and updated when the viewer saves an edit. Workers read
# it when they are created, so a tile is always rendered with the edit that was current then.
EDITS = {}
import weakref  # noqa: E402
_LIVE_MODELS = weakref.WeakSet()


def edit_for(item_id):
    return EDITS.get(int(item_id)) if EDITS else None


def tile_shade(meta):
    """What the badge shade of a tile depends on (part of every tile cache key)."""
    return (meta.get("type") in ("live_photo", "video"), bool(meta.get("is_fav")))


def compose_tile_image(img, w_px, h_px, radius, dpr, shade):
    """The resting grid tile without its badge icons, as a QImage at device resolution: the
    thumbnail cover-scaled (Qt smooth), clipped to the smooth rounded rect, plus the badge
    shade. Thread-safe (QImage + QPainter only), so thumbnail workers build it off the GUI
    thread. ApplePhotoDelegate._tile draws the same steps when no pre-rendered tile matches."""
    out = QImage(max(1, int(w_px)), max(1, int(h_px)), QImage.Format.Format_ARGB32_Premultiplied)
    out.fill(Qt.GlobalColor.transparent)
    out.setDevicePixelRatio(dpr)
    w, h = w_px / dpr, h_px / dpr
    frame = QRectF(0, 0, w, h)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.setClipPath(smooth_rect_path(frame, radius))
    scaled = img.scaled(QSize(int(w_px), int(h_px)), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    size = scaled.deviceIndependentSize()
    p.drawImage(QPointF(w / 2 - size.width() / 2, h / 2 - size.height() / 2), scaled)
    ApplePhotoDelegate._paint_badge_shade(p, frame, {"type": "video" if shade[0] else "photo", "is_fav": shade[1]})
    p.end()
    return out


THUMB_ABORT_RETRIES = 2              # decode exceptions retried before a tile shows "unavailable"


class ThumbnailWorkerSignals(QObject):
    loaded = pyqtSignal(int, QImage, QImage)     # id, thumbnail (tier), pre-rendered tile (or null)
    failed = pyqtSignal(int, str)
    source_short = pyqtSignal(int, int)      # kratší strana zdroje (víc z něj nevytěžíme)
    aborted = pyqtSignal(int)                # an exception in the decode: retry later (bounded)


class ThumbnailWorker(QRunnable):
    """Načte náhled mimo GUI vlákno. Pracuje jen s QImage (QPixmap mimo GUI vlákno
    není bezpečný); na QPixmap ho převede až model v hlavním vlákně."""

    def __init__(self, item_id: int, thumb_path: str, fallback_path: str = None, tier: int = 512, signals=None):
        super().__init__()
        self.item_id = item_id
        self.thumb_path = thumb_path
        self.fallback_path = fallback_path
        self.tier = int(tier)
        # the model hands every worker ONE shared signals object (connected once): a QObject plus
        # three connects per tile cost ~0.3 ms of GUI time per request during a fling
        self.signals = signals if signals is not None else ThumbnailWorkerSignals()
        self.tile_geom = None       # (w_px, h_px, radius, dpr, shade): pre-render the grid tile too
        self.started_ids = None     # Gate 2 A: model's set of dequeued ids (stale-queue cancel)
        self._source_short = None   # true short edge of the decoded source (before scaled decode)
        self.adjustments = None     # pillar 2: quick_edit.Adjustments applied before the tier shrink
        self.queue = None           # Sprint 2: decode_queue.DecodeQueue – run() decodes its best job
        self.micro_store = None     # Sprint 2: micro_previews.MicroStore fed as a by-product
        self.setAutoDelete(True)

    def _apply_edit(self, img):
        if self.adjustments is None or img.isNull():
            return img
        try:
            import quick_edit
            out = quick_edit.render_qimage(img, self.adjustments)
            if self._source_short:
                x, y, cw, ch = self.adjustments.crop
                self._source_short = max(1, int(self._source_short * min(cw, ch)))
            return out
        except Exception:
            return img

    def run(self):
        """Sprint 2: a pool slot. With a decode queue the worker decodes whichever queued job is
        best NOW (nearest the viewport centre, then ahead of the scroll), which may be another
        item's; jobs for rows that scrolled away were already dropped from the queue."""
        q = self.queue
        job = self if q is None else q.pop()
        if job is not None:
            job._safe_decode()

    def _safe_decode(self):
        """Torture test 2026-11-06: an exception in _decode (MemoryError on a huge source under
        pressure, a locked micro-preview store, a bad edit) emitted neither loaded nor failed:
        the id stayed in loading_ids for the session and the tile was never requested again (a
        permanent placeholder). Now the model hears `aborted` and retries it later, bounded."""
        try:
            self._decode()
        except Exception as e:                         # noqa: BLE001
            print(f"[Thumb] {self.item_id}: {type(e).__name__}: {e}")
            try:
                self.signals.aborted.emit(self.item_id)
            except RuntimeError:                        # the model went away meanwhile
                pass

    def _decode(self):
        if self.started_ids is not None:
            self.started_ids.add(self.item_id)
        img = QImage()
        for path in (self.thumb_path, self.fallback_path):
            # Gate 0: originál jen v cloudu se kvůli mřížce nestahuje (posun = stovky souborů)
            if path and os.path.exists(path) and (path is self.thumb_path or cloud_files.local_ok(path)):
                img = self._load(path)
                if not img.isNull():
                    break
        if img.isNull():
            original = self.fallback_path
            state = (THUMB_UNAVAILABLE if original and os.path.exists(original)
                     else THUMB_MISSING_FILE)
            self.signals.failed.emit(self.item_id, state)
            return
        img = self._apply_edit(img)
        self.signals.source_short.emit(self.item_id, self._source_short or min(img.width(), img.height()))
        thumb = _shrink_short_edge(img, self.tier)
        st = self.micro_store
        if st is not None and not st.has(self.item_id):
            st.put_image(self.item_id, thumb)          # Sprint 2: the micro preview for free
        self.signals.loaded.emit(self.item_id, thumb, self._tile(thumb))

    def _tile(self, thumb):
        if self.tile_geom is None or thumb.isNull():
            return QImage()
        try:
            return compose_tile_image(thumb, *self.tile_geom)
        except Exception:                              # the GUI path renders it instead
            return QImage()

    def _load(self, path):
        import color_manage
        import imaging
        if not color_manage.is_cache_file(path):
            # záložně originál (miniatura chybí): stejná pipeline jako miniatury (imaging),
            # dekódovaná rovnou v DCT na ≥ 2× dlaždici
            try:
                dec = imaging.open_oriented(path, need_long=self.tier * 3)
                self._source_short = min(dec.full_size)
                img = dec.image
                target = imaging.fit_short_edge(*img.size, self.tier)
                if target != img.size:
                    img = imaging.sharpen_luma(imaging.resample(img, target), *imaging.SHARPEN_THUMB)
                return imaging.pil_to_qimage(imaging.to_display(img, dec.icc))
            except Exception:
                pass
        else:
            # miniatura z cache, Lanczos zmenšení v tomto vlákně. Pillar 5: when the tier is far
            # below the 840 px file, libjpeg decodes at an exact 1/2 or 1/4 in the DCT (no Qt
            # resampling – the requested size is exactly libjpeg's output), keeping >= 1.5× the
            # tier for the Lanczos step. Measured at boot: 10 + 13 ms -> ~3 + 3 ms per tile.
            reader = QImageReader(path)
            src = reader.size()
            div = cache_decode_divisor(src.width(), src.height(), self.tier) if src.isValid() else 1
            if div > 1:
                reader.setScaledSize(QSize(-(-src.width() // div), -(-src.height() // div)))
            img = reader.read()
            if not img.isNull():
                self._source_short = min(src.width(), src.height()) if div > 1 else min(img.width(), img.height())
                return img
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        # Gate 2 A: decode straight at the tier (JPEG scales in the DCT - far cheaper than a
        # full decode + shrink); the true source size is reported so later upgrades still work
        src = reader.size()
        if src.isValid() and min(src.width(), src.height()) > 0:
            short = min(src.width(), src.height())
            self._source_short = short
            if short > self.tier * 1.05:
                k = self.tier / short
                reader.setScaledSize(QSize(max(1, round(src.width() * k)), max(1, round(src.height() * k))))
        img = reader.read()
        if not img.isNull() and not color_manage.is_cache_file(path):
            img = color_manage.qimage_to_output(img)     # záložně originál: kontrakt displeje
        return img


class VaultThumbnailWorker(ThumbnailWorker):
    """Miniatura položky ze složky Skryté: dešifruje se z trezoru (locked_folder) jen do RAM;
    chybí-li, vytvoří se v paměti z originálu a rovnou zašifruje. Zamčeno = žádný obraz."""

    def __init__(self, item_id, rec, tier, vault, signals=None):
        super().__init__(item_id, None, rec[2], tier, signals)
        self.media_type, self.paired = rec[3], rec[4]
        self.vault = vault

    def _decode(self):
        if self.started_ids is not None:
            self.started_ids.add(self.item_id)
        try:
            data = self.vault.open_thumbnail(self.item_id)
            if data is None:
                data = locked_folder.render_sealed_thumbnail(self.item_id, self.fallback_path, self.media_type,
                                                             self.paired, self.vault)
        except locked_folder.VaultError:
            data = None
        img = QImage.fromData(data) if data else QImage()
        if img.isNull():
            original = self.fallback_path
            self.signals.failed.emit(self.item_id, THUMB_UNAVAILABLE if original and os.path.exists(original)
                                     else THUMB_MISSING_FILE)
            return
        img = self._apply_edit(img)
        self.signals.source_short.emit(self.item_id, min(img.width(), img.height()))
        thumb = _shrink_short_edge(img, self.tier)
        self.signals.loaded.emit(self.item_id, thumb, self._tile(thumb))


# ── ApplePhotoModel ───────────────────────────────────────────────────────────

class ApplePhotoModel(QAbstractListModel):
    ThumbRole = Qt.ItemDataRole.UserRole + 1
    MetaRole  = Qt.ItemDataRole.UserRole + 2
    StateRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, lru_capacity: int = 1200, parent=None):
        super().__init__(parent)
        _LIVE_MODELS.add(self)            # pillar 2: an edit saved in the viewer refreshes every grid
        self.records = []
        self.possible = set()
        self.id_to_row = {}
        self.ram_cache = LRUCache(lru_capacity, max_bytes=THUMB_RAM_BUDGET_MB * 1024 * 1024, sizer=pixmap_bytes)
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(DECODE_THREADS_REST)
        self.tile_geom = None     # (w_px, h_px, radius, dpr) of a resting tile, from the view
        self._pretiles = OrderedDict()   # item_id -> (QPixmap, (w_px, h_px, radius, dpr, shade))
        self._pretile_bytes = 0
        self._signals = ThumbnailWorkerSignals(self)     # shared by every worker (one set of connects)
        self._signals.source_short.connect(self._on_source_short)
        self._signals.loaded.connect(self._on_thumb_loaded)
        self._signals.failed.connect(self._on_thumb_failed)
        self._signals.aborted.connect(self._on_thumb_aborted)
        self._aborts = {}         # item_id -> decode exceptions so far (THUMB_ABORT_RETRIES)
        self.loading_ids = set()
        self.failed = {}          # item_id -> THUMB_MISSING_FILE | THUMB_UNAVAILABLE
        self._meta = {}           # řádek -> slovník MetaRole (delegát ho čte při každém snímku)
        self.tier = THUMB_TIERS[1]                # kratší strana v RAM (podle velikosti dlaždic)
        self._src_short = {}      # item_id -> kratší strana miniatury na disku (strop zlepšení)
        self.vault = None         # Stage 3: mřížka složky Skryté (miniatury z trezoru)
        self._vault_ids = set()   # id, jejichž (dešifrované) miniatury drží ram_cache
        # Gate 2 A: speed-aware decoding. During a fast fling, new tiles decode at the smallest
        # tier and sharper upgrades wait; when the fling settles, queued decodes for rows that
        # flew past are cancelled so the rows now on screen go first.
        self.fast = False
        self._queued = set()      # ids handed to the pool
        self._started = set()     # ids the pool has dequeued (written from worker threads)
        # Sprint 2: decodes wait in a viewport-prioritised queue (decode_queue); the view reports
        # the visible rows and scroll direction on every scroll step (set_viewport)
        self.decode_queue = decode_queue.DecodeQueue()
        self._viewport = None     # (first, last, direction) last reported by the view
        # Sprint 2: micro previews (LQIP) – a soft preview instead of an empty tile
        self.micro = micro_previews.store()
        self._micro_requested = False
        self._micro_poll = QTimer(self)
        self._micro_poll.setInterval(120)
        self._micro_poll.timeout.connect(self._check_micro_loaded)
        if not self.micro.loaded.is_set():
            self._micro_poll.start()

    def set_fast(self, fast: bool):
        fast = bool(fast)
        if fast == self.fast:
            return False
        self.fast = fast
        # measured (eval/bench_massive_scale.py, 2 000 items, fling): with 4 decode threads the GUI
        # thread's paint took 6.4 ms (CPU + GIL contention), with 2 threads 4.3 ms. Glide with 2,
        # settle with 4.
        self.thread_pool.setMaxThreadCount(DECODE_THREADS_FLING if fast else DECODE_THREADS_REST)
        if fast:
            self.micro.pause_until = time.monotonic() + 0.6
        else:
            self.cancel_stale()
        return True

    def cancel_stale(self):
        """Drops decodes that have not started yet for rows outside the keep window (they scrolled
        away); visible rows re-request on the next paint. With no viewport reported yet: all."""
        if self._viewport is None:
            self.thread_pool.clear()
            dropped = set(self.decode_queue.clear())
            stale = (self._queued - self._started) | dropped
            self.loading_ids.difference_update(stale)
            self._queued.clear()
            self._started.clear()
            return len(stale)
        first, last, direction = self._viewport
        return len(self.set_viewport(first, last, direction))

    # ── Sprint 2: viewport-prioritised decoding ──
    def set_viewport(self, first, last, direction=0):
        """The view's visible rows and scroll direction (+1 down / -1 up / 0). Queued decodes
        outside the keep window are dropped at once (their ids stop counting as loading, so a
        row that comes back re-requests). Returns the dropped ids."""
        self._viewport = (int(first), int(last), int(direction))
        dropped = self.decode_queue.set_viewport(first, last, direction, fast=self.fast)
        if dropped:
            self.loading_ids.difference_update(dropped)
            self._queued.difference_update(dropped)
        return dropped

    def prefetch_ahead(self, budget=None):
        """Predictive prefetch: queue decodes for rows ahead of the scroll (decode_queue's keep
        window), nearest to the leading edge first. Returns how many were queued."""
        n = 0
        rows = self.decode_queue.prefetch_rows()
        count = len(self.records)
        for row in rows:
            if budget is not None and n >= budget:
                break
            if not 0 <= row < count:
                continue
            rec = self.records[row]
            item_id = int(rec[0])
            if item_id in self.loading_ids or item_id in self.failed or self.ram_cache.peek(item_id) is not None:
                continue
            self._start_worker(item_id, rec)
            n += 1
        return n

    # ── Sprint 2: micro previews ──
    def has_micro(self, item_id):
        return self.vault is None and self.micro.has(int(item_id))

    def micro_pixmap(self, item_id):
        """The soft micro preview of an item still decoding (GUI thread), or None."""
        if self.vault is not None:
            return None                 # hidden items never have one
        return self.micro.pixmap(int(item_id))

    def _check_micro_loaded(self):
        if not self.micro.loaded.is_set():
            return
        self._micro_poll.stop()
        self.request_micro_backfill()
        if self.records:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.records) - 1, 0), [self.ThumbRole])

    def request_micro_backfill(self):
        """Missing micro previews are built on a low-priority thread from the on-disk thumbnails,
        starting at the current viewport. Never for the Hidden folder."""
        if self.vault is not None or not self.records or not self.micro.loaded.is_set():
            return 0
        start = self._viewport[0] if self._viewport else 0
        recs = self.records[start:] + self.records[:start]
        jobs = [(int(r[0]), r[5]) for r in recs if r[3] != "video" or r[5]]
        return self.micro.request(jobs, edit_lookup=edit_for)

    def set_tier(self, tier: int):
        """Velikost dlaždic se změnila: větší úroveň se dotáhne líně (data() vrací dosavadní
        pixmapu a na pozadí načte ostřejší); menší nic nestojí – stávající se jen zmenší."""
        self.tier = int(tier)

    def _needs_upgrade(self, item_id, pix):
        want = min(self.tier, self._src_short.get(item_id, self.tier))
        return min(pix.width(), pix.height()) < want - 2

    def _start_worker(self, item_id, rec):
        self.loading_ids.add(item_id)
        tier = min(self.tier, THUMB_TIERS[0]) if self.fast else self.tier
        if self.vault is not None:
            self._vault_ids.add(item_id)
            w = VaultThumbnailWorker(item_id, rec, tier, self.vault, self._signals)
        else:
            w = ThumbnailWorker(item_id, rec[5], rec[2], tier, self._signals)
        w.started_ids = self._started
        if rec[3] != "video":
            w.adjustments = edit_for(item_id)
        if self.tile_geom is not None and self.vault is None:
            w.tile_geom = self.tile_geom + (tile_shade({"type": rec[3], "is_fav": rec[7]}),)
        if self.vault is None:
            w.micro_store = self.micro                  # hidden items never get a micro preview
        self._queued.add(item_id)
        # Sprint 2: the job waits in the viewport-prioritised queue; the pool slot started here
        # decodes whichever job is best when a thread picks it up (ThumbnailWorker.run)
        w.queue = self.decode_queue
        self.decode_queue.submit(item_id, self.id_to_row.get(item_id, 0), w)
        self.thread_pool.start(w)

    def set_tile_geom(self, w_px, h_px, radius, dpr):
        """Resting tile size in device px (the view's cell minus its inset): workers pre-render
        tiles of exactly this size. A new size drops the stale pre-rendered tiles."""
        geom = (int(w_px), int(h_px), round(float(radius), 2), float(dpr))
        if geom != self.tile_geom:
            self.tile_geom = geom
            self._pretiles.clear()
            self._pretile_bytes = 0

    def pretile(self, item_id, w_px, h_px, radius, dpr, shade):
        """The pre-rendered tile (QPixmap) when it matches this geometry and shade, else None."""
        hit = self._pretiles.get(item_id)
        if hit is None or hit[1] != (int(w_px), int(h_px), round(float(radius), 2), float(dpr), shade):
            return None
        self._pretiles.move_to_end(item_id)
        return hit[0]

    def _keep_pretile(self, item_id, tile):
        if tile.isNull() or self.tile_geom is None:
            return
        rec = self.records[self.id_to_row[item_id]] if item_id in self.id_to_row else None
        if rec is None:
            return
        geom = (tile.width(), tile.height(), self.tile_geom[2], tile.devicePixelRatio(),
                tile_shade({"type": rec[3], "is_fav": rec[7]}))
        if geom[:2] != self.tile_geom[:2] or abs(geom[3] - self.tile_geom[3]) > 1e-6:
            return                                   # rendered for an older cell size
        old = self._pretiles.pop(item_id, None)
        if old is not None:
            self._pretile_bytes -= old[0].width() * old[0].height() * 4
        pm = QPixmap.fromImage(tile)
        self._pretiles[item_id] = (pm, geom)
        self._pretile_bytes += pm.width() * pm.height() * 4
        while self._pretile_bytes > PRETILE_BYTES and len(self._pretiles) > 1:
            _, (old_pm, _g) = self._pretiles.popitem(last=False)
            self._pretile_bytes -= old_pm.width() * old_pm.height() * 4

    def _on_source_short(self, item_id: int, short: int):
        self._src_short[item_id] = int(short)

    def set_records(self, db_records, possible=()):
        """possible = id položek z pásma „možné shody“ hledání (kreslí se tlumeně)."""
        records = list(db_records)
        possible = {int(i) for i in possible}
        if records == self.records and possible == self.possible:
            # Torture test 2026-11-06: daemon refreshes mostly change nothing, yet each one reset
            # the 20k-row model on the GUI thread (50-160 ms), dropped the viewport's queued
            # decodes mid-fling and re-decoded in-flight tiles. Unchanged = keep everything;
            # only retry the failed tiles (a drive may have come back), as the reset did.
            if self.failed:
                retry = list(self.failed)
                self.failed.clear()
                for item_id in retry:
                    self._emit_row_changed(item_id, [self.ThumbRole, self.StateRole])
            return
        self.beginResetModel()
        self.records = records
        self.possible = possible
        self._meta = {}
        self.id_to_row = {int(r[0]): i for i, r in enumerate(self.records)}
        self.loading_ids.clear()
        self.failed.clear()       # soubor se mohl mezitím objevit (připojený disk, opravený náhled)
        self.decode_queue.clear()             # rows changed meaning: queued jobs are stale
        self._queued.clear()
        self._viewport = None
        self.endResetModel()
        self.request_micro_backfill()          # new items (an import) get their micro previews

    def refresh_items(self, ids):
        """Pillar 2: these items' pixels changed (Quick Edit saved): drop their thumbnails and
        pre-rendered tiles and repaint – the next paint decodes them with the new edit."""
        ids = {int(i) for i in ids}
        self.ram_cache.discard(ids)
        self.micro.invalidate(ids)             # Sprint 2: the micro preview shows the edit too
        QTimer.singleShot(0, self.request_micro_backfill)
        for item_id in ids:
            old = self._pretiles.pop(item_id, None)
            if old is not None:
                self._pretile_bytes -= old[0].width() * old[0].height() * 4
            self._src_short.pop(item_id, None)
            self.failed.pop(item_id, None)
        self.loading_ids.difference_update(ids)
        for item_id in ids:
            row = self.id_to_row.get(item_id)
            if row is not None:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx)

    def invalidate_cache(self):
        self.ram_cache.clear()
        self.loading_ids.clear()
        self.failed.clear()
        self._src_short.clear()
        self._vault_ids.clear()

    def purge_vault(self):
        """Zamčení: dešifrované miniatury Skrytých pryč z RAM, model bez trezoru."""
        self.vault = None
        ids = self._vault_ids
        self.ram_cache.discard(ids)
        for item_id in ids:
            self.failed.pop(item_id, None)
            self._src_short.pop(item_id, None)
        self.loading_ids.difference_update(ids)
        self._vault_ids = set()

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
                if not self.fast and item_id not in self.loading_ids and self._needs_upgrade(item_id, pix):
                    self._start_worker(item_id, rec)     # ostřejší úroveň; do té doby dosavadní
                return pix
            if item_id not in self.loading_ids and item_id not in self.failed:
                self._start_worker(item_id, rec)
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

    def _on_thumb_loaded(self, item_id: int, image: QImage, tile: QImage = None):
        self._queued.discard(item_id)
        self._started.discard(item_id)
        if item_id in self._vault_ids and self.vault is None:
            self.loading_ids.discard(item_id)       # dorazilo až po zamčení: zahodit
            return
        old = self.ram_cache.get(item_id)
        if old is not None and min(old.width(), old.height()) >= min(image.width(), image.height()):
            # zlepšení nepřišlo (menší zdroj): zapamatovat strop, ať se to nezkouší pořád
            self._src_short[item_id] = min(self._src_short.get(item_id, 1 << 30), min(old.width(), old.height()))
            self.loading_ids.discard(item_id)
            return
        self.ram_cache.put(item_id, QPixmap.fromImage(image))   # QPixmap až v GUI vlákně
        if tile is not None:
            self._keep_pretile(item_id, tile)
        self.loading_ids.discard(item_id)
        self.failed.pop(item_id, None)
        self._emit_row_changed(item_id, [self.ThumbRole, self.StateRole])

    def _on_thumb_aborted(self, item_id: int):
        """A decode raised: forget it was in flight so the next paint requests it again; after
        THUMB_ABORT_RETRIES it is marked unavailable (a deterministic error never loops)."""
        n = self._aborts.get(item_id, 0) + 1
        self._aborts[item_id] = n
        if n > THUMB_ABORT_RETRIES:
            self._on_thumb_failed(item_id, THUMB_UNAVAILABLE)
            return
        self._queued.discard(item_id)
        self._started.discard(item_id)
        self.loading_ids.discard(item_id)
        self._emit_row_changed(item_id, [self.ThumbRole, self.StateRole])

    def _on_thumb_failed(self, item_id: int, state: str):
        self._queued.discard(item_id)
        self._started.discard(item_id)
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


BADGE_D = 20.0                            # průměr skleněného kroužku ikony Live / video
BADGE_INSET = 6.0
BADGE_GLASS_ALPHA = 92
TILE_CACHE_BYTES = 64 * 1024 * 1024       # hotové dlaždice mřížky (~500 dlaždic 180 px)


class ApplePhotoDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = parent                   # plain Python ref: no parent() round trip per tile
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
        """Jen jemné ztmavení tam, kde leží ikona (roh vpravo nahoře / srdce vlevo dole) – žádný
        pruh přes celou šířku dlaždice."""
        if meta.get("type") in ("live_photo", "video") and frame.width() >= 64:
            c = QPointF(frame.right() - BADGE_INSET - BADGE_D / 2, frame.top() + BADGE_INSET + BADGE_D / 2)
            grad = QRadialGradient(c, BADGE_D * 1.6)
            grad.setColorAt(0.0, QColor(0, 0, 0, 46))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(QRectF(c.x() - BADGE_D * 1.6, frame.top(), BADGE_D * 3.2, BADGE_D * 3.2) & frame, grad)
        if meta.get("is_fav"):
            grad = QLinearGradient(0, frame.bottom() - 36, 0, frame.bottom())
            grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            grad.setColorAt(1.0, QColor(0, 0, 0, 70))
            painter.fillRect(QRectF(frame.left(), frame.bottom() - 36, frame.width(), 36), grad)

    @staticmethod
    def badge_rect(frame):
        """Skleněný kroužek ikony Live / video v pravém horním rohu dlaždice (logické px)."""
        return QRectF(frame.right() - BADGE_INSET - BADGE_D, frame.top() + BADGE_INSET, BADGE_D, BADGE_D)

    @staticmethod
    def _paint_badges(painter, frame, meta):
        """Founder audit: místo textových štítků LIVE / VIDEO tichá skleněná mikro-ikona v rohu
        (soustředné kruhy = Live Photo, trojúhelník = video) a srdce oblíbené vlevo dole."""
        white = QColor("#FFFFFF")
        m_type = meta.get("type")
        if m_type in ("live_photo", "video") and frame.width() >= 64:
            disc = ApplePhotoDelegate.badge_rect(frame)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(QColor(20, 20, 24, BADGE_GLASS_ALPHA))      # „sklo“ nad fotkou
            painter.setPen(QPen(QColor(255, 255, 255, 64), 0.8))            # vlasová hrana
            painter.drawEllipse(disc)
            inner = disc.adjusted(4.5, 4.5, -4.5, -4.5)
            if m_type == "video":
                play = disc.adjusted(3.5, 3.5, -3.5, -3.5).translated(0.7, 0)    # opticky na střed
                icons.paint(painter, "play_fill", play, QColor(255, 255, 255, 240))
            else:
                icons.paint(painter, "live", inner, QColor(255, 255, 255, 240))
            painter.restore()
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
        w_px, h_px = max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr))
        frame = QRectF(0, 0, size.width(), size.height())
        model = self.parent().model() if self.parent() is not None else None
        pre = None
        if not meta.get("possible") and model is not None and hasattr(model, "pretile") and "id" in meta:
            pre = model.pretile(meta["id"], w_px, h_px, radius, dpr, tile_shade(meta))
        if pre is not None:
            # composed off the GUI thread (compose_tile_image); only the badge icons are added here
            if tile_shade(meta) == (False, False):
                tile = pre
            else:
                tile = QPixmap(pre)
                p = QPainter(tile)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                self._paint_badges(p, frame, meta)
                p.end()
        else:
            tile = QPixmap(w_px, h_px)
            tile.setDevicePixelRatio(dpr)
            tile.fill(Qt.GlobalColor.transparent)
            p = QPainter(tile)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
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
            micro = self._micro_pixmap_for(index) if index is not None else None
            if micro is not None:
                self._draw_micro(painter, QRectF(rect), micro)     # clipped by the caller's path
            else:
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
            reason = tr("grid-file-unavailable")
        else:
            reason = tr("grid-video-no-thumb") if is_video else tr("grid-thumb-unavailable")
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
            if (index.data(ApplePhotoModel.StateRole) or THUMB_LOADING) != THUMB_LOADING:
                return False                            # failure card (icon + text): full paint
            rect = option.rect.adjusted(4, 4, -4, -4)
            if rect.width() > 0 and rect.height() > 0:
                dev = painter.device()
                dpr = dev.devicePixelRatioF() if dev is not None else 1.0
                radius = max(6.0, min(14.0, rect.width() * 0.07))
                micro = self._micro_pixmap_for(index)
                if micro is not None:
                    # Sprint 2: the soft micro preview until the sharp thumbnail lands: one
                    # bilinear draw, then only the four corner squares of the mask (blending the
                    # whole, mostly transparent mask cost about as much as the photo)
                    self._draw_micro(painter, QRectF(rect), micro)
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                    self._blit_corners(painter, rect, radius, dpr)
                    return True
                # a loading tile during a fling: one blit of a cached rounded placeholder
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                painter.drawPixmap(rect.topLeft(), self._placeholder_tile(rect.size(), radius, dpr))
            return True
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

    # ── Sprint 2: micro previews (LQIP) ──
    @staticmethod
    def _draw_micro(painter, rect, micro):
        """The micro preview cover-scaled into rect (bilinear: the softness is the point). Measured
        ~0.1-0.25 ms per tile at 4x CPU throttle, and the opaque viewport only repaints the strip
        a scroll exposes, so each tile is drawn about once."""
        sw, sh = micro.width(), micro.height()
        if sw <= 0 or sh <= 0 or rect.width() <= 0 or rect.height() <= 0:
            return
        k = max(rect.width() / sw, rect.height() / sh)
        cw, ch = rect.width() / k, rect.height() / k
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(rect, micro, QRectF((sw - cw) / 2, (sh - ch) / 2, cw, ch))

    def _blit_corners(self, painter, rect, radius, dpr):
        """The page background outside the rounded rect, corner squares only."""
        mask = self._corner_mask(rect.size(), radius, dpr)
        c = int(math.ceil(radius)) + 1
        w, h = rect.width(), rect.height()
        if c * 2 > min(w, h):
            painter.drawPixmap(rect.topLeft(), mask)
            return
        for x, y in ((0, 0), (w - c, 0), (0, h - c), (w - c, h - c)):
            painter.drawPixmap(QRectF(rect.left() + x, rect.top() + y, c, c), mask,
                               QRectF(x * dpr, y * dpr, c * dpr, c * dpr))

    def _micro_pixmap_for(self, index):
        """The decoded micro preview of a loading item, or None (no preview / Hidden grid).

        Measured (probe: 4 000 items, throttled CPU, wheel flings, interleaved A/B). The grid
        repaints ~13 tiles per frame, most still loading. Composing a rounded tile pixmap per
        item (alloc + fill + painter + mask) cost ~0.5 ms each and raised the grid paint p50
        from 3.4 to 7.7 ms. Drawing the micro preview straight into the viewport plus one cached
        corner-mask blit costs ~0.12 ms. Lookup is ONE Qt call (index.row()) and pure-Python
        dict reads: while decoders run, every Python <-> Qt round trip on the GUI thread waits
        for the GIL."""
        view = self.parent()
        model = getattr(view, "photo_model", None) if view is not None else None
        if model is None:
            model = view.model() if view is not None else None
        micro_store = getattr(model, "micro", None)
        if micro_store is None or getattr(model, "vault", None) is not None:
            return None
        try:
            item_id = int(model.records[index.row()][0])
        except (AttributeError, IndexError, TypeError):
            return None
        return micro_store.pixmap(item_id)

    def _corner_mask(self, size, radius, dpr):
        """The page background outside the tile's rounded rect: blitted over a square micro
        preview, it gives the same corners as a finished tile without an antialiased clip path
        per tile (the grid viewport is opaque, painted in theme "bg")."""
        key = ("corner-mask", size.width(), size.height(), round(radius, 2), dpr, theme.current())
        mask = self._tiles.get(key)
        if mask is None:
            mask = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
            mask.setDevicePixelRatio(dpr)
            mask.fill(Qt.GlobalColor.transparent)
            frame = QRectF(0, 0, size.width(), size.height())
            outer = QPainterPath()
            outer.addRect(frame.adjusted(-1, -1, 1, 1))
            outer = outer.subtracted(smooth_rect_path(frame, radius))
            p = QPainter(mask)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.fillPath(outer, theme.qcolor("bg"))
            p.end()
            self._tiles[key] = mask
            self._tiles_bytes += mask.width() * mask.height() * 4
        else:
            self._tiles.move_to_end(key)
        return mask

    def _placeholder_tile(self, size, radius, dpr):
        key = ("placeholder", size.width(), size.height(), round(radius, 2), dpr, theme.current())
        tile = self._tiles.get(key)
        if tile is None:
            tile = QPixmap(max(1, round(size.width() * dpr)), max(1, round(size.height() * dpr)))
            tile.setDevicePixelRatio(dpr)
            tile.fill(Qt.GlobalColor.transparent)
            p = QPainter(tile)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.fillPath(smooth_rect_path(QRectF(0, 0, size.width(), size.height()), radius), self.placeholder_color)
            p.end()
            self._tiles[key] = tile
            self._tiles_bytes += tile.width() * tile.height() * 4
        else:
            self._tiles.move_to_end(key)
        return tile

    def _hover(self, index):
        view = self.parent()
        fn = getattr(view, "hover_progress", None)
        return fn(index.row()) if fn else 0.0

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        """Kairos dávka 4: plynulé rohy, při najetí dlaždice „povyskočí“ do mezery (a fotka se
        jemně přiblíží), výběr = prstenec zvýraznění s vnořenou fotkou a fajfkou."""
        region = getattr(self._view, "_paint_region", None)
        if region is not None and not region.intersects(option.rect):
            return                                      # Sprint 2: inside a span, but not dirty
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
FLING_ROWS_PER_S = 6               # Gate 2 A: faster than this = cheap decodes, upgrades wait
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
    # Founder audit: první „hotový“ snímek – všechny viditelné dlaždice mají náhled (nebo trvale
    # selhaly), případně je knihovna prázdná. Splash do té doby drží obraz a pak se prolne.
    firstFrameRendered = pyqtSignal()

    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window
        self._first_frame_pending = True
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSpacing(0)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(28)
        # Opaque viewport (paintEvent fills the dirty rect with the page background): Qt then
        # scrolls by blitting the backing store and repaints only the newly exposed strip.
        # Measured (eval/bench_massive_scale.py, 20 000 items): a transparent viewport made
        # every glide step repaint all ~25 visible tiles and flush the whole viewport (12 ms
        # per frame, so vsync halved the glide to 60 fps).
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
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
        self.live_preview.edit_for = edit_for       # pillar 2: hover loop with the still's edit
        # Během posouvání žádné najetí: zvedání dlaždic, Live Photo ani sledování myši – každá
        # dlaždice je jen kopie hotové pixmapy. Najetí se obnoví SCROLL_IDLE_MS po zastavení.
        self._scrolling = False
        self._scroll_idle = QTimer(self)
        self._scroll_idle.setSingleShot(True)
        self._scroll_idle.setInterval(SCROLL_IDLE_MS)
        self._scroll_idle.timeout.connect(self._on_scroll_idle)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        # Sprint 2: the model learns the visible rows + scroll direction (decode priority, drop of
        # rows scrolled away, predictive prefetch)
        self._last_scroll_value = None
        self.photo_model.modelReset.connect(lambda: QTimer.singleShot(0, lambda: self._report_viewport(0)))
        # Gate 2 A: pixel-smooth kinetic scrolling (spring glide per notch, touchpad momentum)
        self._kinetic = kinetic_scroll.KineticScroller(self, reduced=motion.reduced)
        self._kinetic.velocity_changed.connect(self._on_scroll_speed)
        self._kinetic.settled.connect(lambda: self._on_scroll_speed(0.0))

    def _on_scroll_speed(self, px_s):
        """Fling = more than FLING_ROWS_PER_S rows a second: decode cheap, upgrade later."""
        fast = px_s > FLING_ROWS_PER_S * max(1, self.cell_size)
        if self.photo_model.set_fast(fast) and not fast:
            self.viewport().update()            # visible rows ask for full quality now

    # ── velikost dlaždic ──
    def _update_thumb_tier(self):
        """Rozlišení miniatur v RAM podle dlaždice ve fyzických px (Stage 2)."""
        model = getattr(self, "photo_model", None)
        if model is not None and hasattr(model, "set_tier"):
            model.set_tier(thumb_tier(self.cell_size * self.viewport().devicePixelRatioF()))
        if model is not None and hasattr(model, "set_tile_geom"):
            dpr = self.viewport().devicePixelRatioF()
            side = self.cell_size - 8                # option.rect.adjusted(4, 4, -4, -4)
            if side > 0:
                model.set_tile_geom(round(side * dpr), round(side * dpr), max(6.0, min(14.0, side * 0.07)), dpr)

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
        self._update_thumb_tier()
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
        QTimer.singleShot(0, lambda: self._report_viewport(0))

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
        self._update_thumb_tier()
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
        self._report_viewport(0)

    def paintEvent(self, event):
        bg = QPainter(self.viewport())        # opaque viewport: the gaps between tiles are ours to paint
        bg.fillRect(event.rect(), theme.qcolor("bg"))
        bg.end()
        if self._frozen is not None:
            self._paint_frozen()
        elif not self._paint_dirty_rows(event):
            super().paintEvent(event)
        frame_meter.tick("grid")            # Fáze 0: doba snímku mřížky (Ctrl+Shift+F)
        if self._first_frame_pending and self.visible_tiles_ready():
            self._first_frame_pending = False
            QTimer.singleShot(0, self._emit_first_frame)     # až po dokončení tohoto snímku

    def _emit_first_frame(self):
        # A method (not the bound signal) as the slot: PyQt drops the queued call when this view
        # is destroyed first. The bound signal raised AttributeError in a Qt slot (qFatal) when a
        # detail / album grid closed right after its first paint.
        self.firstFrameRendered.emit()

    def _paint_dirty_rows(self, event):
        """Sprint 2: QListView repaints every item inside the BOUNDING rect of the dirty region.
        While flinging, the region is the strip a scroll exposed plus the tiles whose thumbnails
        just landed, anywhere on screen, so nearly every frame repainted every visible tile (probe:
        ~24 per frame). Only the tiles the region really touches are repainted, one span per grid
        row (PyQt6 cannot list a region's rects, but it can test whether a rect intersects).
        False = a plain region, so QListView paints it."""
        region = event.region()
        if region.rectCount() <= 1 or self.photo_model.rowCount() == 0:
            return False
        layout = self._current_grid()
        vp = self.viewport().rect()
        first, last = layout.index_range(QRectF(vp))
        if last < first:
            return False
        cols = layout.cols
        spans = []
        for r0 in range(first - first % cols, last + 1, cols):
            hit = [i for i in range(max(r0, first), min(r0 + cols, last + 1))
                   if region.intersects(layout.rect(i).toAlignedRect())]
            if hit:
                span = layout.rect(hit[0]).united(layout.rect(hit[-1])).toAlignedRect() & vp
                if not span.isEmpty():
                    spans.append(span)
        self._paint_region = region     # the delegate skips span tiles the region misses
        try:
            for span in spans:          # the painter's system clip is still the real region
                super().paintEvent(QPaintEvent(span))
        finally:
            self._paint_region = None
        self.dirty_spans = len(spans)
        return True

    def visible_tiles_ready(self, cap=240):
        """True, když žádná viditelná dlaždice už nečeká na náhled (prázdná mřížka = hotovo)."""
        model = self.model()
        if model is None or model.rowCount() == 0:
            return True
        vp = self.viewport().rect()
        first = self.indexAt(QPoint(vp.left() + 6, vp.top() + 6))
        row = first.row() if first.isValid() else 0
        for _ in range(cap):
            if row >= model.rowCount():
                break
            idx = model.index(row, 0)
            r = self.visualRect(idx)
            if r.top() > vp.bottom():
                break
            if r.intersects(vp) and model.data(idx, ApplePhotoModel.StateRole) == THUMB_LOADING:
                return False
            row += 1
        return True

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
                micro = model.micro_pixmap(int(model.records[i][0]))
                if micro is not None:
                    delegate._draw_micro(p, r, micro)
                else:
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
        if self._kinetic.wheel(event):
            event.accept()
            return
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
        self._report_viewport()

    def _report_viewport(self, direction=None):
        """Sprint 2: visible rows + scroll direction to the model: queued decodes for rows that
        scrolled away are dropped, the rest are served centre-first, and rows ahead of the
        scroll are queued (predictive prefetch)."""
        model = self.photo_model
        if self._frozen is not None or model.rowCount() == 0 or self.viewport().height() <= 0:
            return
        value = self.verticalScrollBar().value()
        prev = self._last_scroll_value
        if direction is None:
            direction = 0 if prev is None or value == prev else (1 if value > prev else -1)
        self._last_scroll_value = value
        first, last = self._current_grid().index_range(QRectF(self.viewport().rect()))
        if last < first:
            return
        model.set_viewport(first, last, direction)
        model.prefetch_ahead()

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
        self._report_viewport(0)            # at rest: warm a screen above and below
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
    return tr("count-columns", count=n)


# ── P3.1 Async person cards ───────────────────────────────────────────────────

# i18n-ok: DB default person name (localized at display)
UNKNOWN_PERSON = "Neznámá osoba"
PERSON_CARD_W, PERSON_CARD_H = 176, 228
PERSON_AVATAR_RATIO = 0.78           # průměr avataru / šířka karty
PERSON_GRID_SPACING = 16


def _czech_photo_count(n):
    return tr("count-photos", count=int(n or 0))


def _czech_item_count(n):
    return tr("count-items", count=int(n or 0))


def _czech_result_count(n):
    return tr("count-results", count=int(n or 0))


def _format_date_range(start, end):
    """'2024-07-01 10:00' / '2024-07-03' -> '7/1/2024 – 7/3/2024' / '1. 7. 2024 – 3. 7. 2024' (jeden den = jedno datum)."""
    def fmt(value):
        try:
            d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return ""
        return i18n.fmt_date(d, "short")
    a, b = fmt(start), fmt(end)
    if a and b and a != b:
        return f"{a} – {b}"
    return a or b


def _person_initials(name):
    # i18n-ok: DB default person name check
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
    import imaging
    src = imaging.scale_qimage(src, px, px)              # Lanczos3, ne bilineární Qt
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
        self.setToolTip(f"{self.name}\n{_czech_photo_count(count)}\n" + tr("people-card-merge-hint"))
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
        self.lbl_question = QLabel(tr("faces-same-person-q"))
        self.lbl_question.setStyleSheet("font-size:15px; font-weight:600; color:#F5F5F7;")
        self.lbl_detail = QLabel()
        self.lbl_detail.setStyleSheet("font-size:12px; color:#8E8E93;")
        texts.addWidget(self.lbl_question)
        texts.addWidget(self.lbl_detail)
        row.addLayout(texts, 1)
        self.btn_yes = QPushButton(tr("faces-yes-merge"))
        self.btn_yes.setObjectName("btn_primary")
        self.btn_no = QPushButton(tr("common-no"))
        self.btn_skip = QPushButton(tr("common-next"))
        self.btn_skip.setToolTip(tr("faces-decide-later"))
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
            tr("faces-merge-suggestion", name_a=name_a, faces_a=s['faces_a'], name_b=name_b, faces_b=s['faces_b'], value=round(s['similarity'] * 100), value2=self.index + 1, count=len(self.suggestions)))
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


class FlowLayout(ui_controls.FlowLayout):
    """Responzivní mřížka karet osob (ui_controls.FlowLayout s rozestupem mřížky osob)."""

    def __init__(self, parent=None, spacing=PERSON_GRID_SPACING):
        super().__init__(parent, spacing)


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
                     f"{date_str}  •  " + tr("count-items", count=item_count))
    painter.end()
    return result


# ── CircularProgressWidget ────────────────────────────────────────────────────

class CircularProgressWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(220, 260)
        self._progress = 0
        self._status_text = tr("common-loading")
        self._elapsed_sec = 0
        self._total_est_sec = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 185, 15, 15)
        self.btn_cancel = QPushButton(tr("import-cancel"))
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


REVEAL_FALLBACK_MS = 900      # direct launch: the window appears by then even if tiles are still decoding
PHOTO_PRECACHE_DELAY_MS = 90000   # Sprint 3 item 3: photo places offline, once start-up has settled
DEFERRED_SERVICES_MS = 500     # pillar 5: daemons / side pages start this long after the first grid frame
DEFERRED_FALLBACK_MS = 2500    # … or after this, when the window opened somewhere without a grid frame
MAP_PREBUILD_DELAY_MS = 1500   # map host (WebView2) built in the background after the deferred tick
SEARCH_WARMUP_DELAY_MS = 5000   # textový model hledání se načte v klidu po startu, ne při 1. dotazu
SIGLIP_READY_SHARE = 0.98       # přesné hledání se zapne, až má SigLIP 2 vektor ≥ 98 % knihovny
SIGLIP_BATCH = 8


class SiglipDownload(QThread):
    """Sprint 4.0: stažení modelu přesného hledání (~1,5 GB) na pozadí, s pokračováním po přerušení."""
    progress = pyqtSignal(float, float)     # MB hotovo, MB celkem
    done = pyqtSignal(bool, str)            # úspěch, chyba

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        import siglip_engine
        last = [0.0]

        def report(got, total):
            now = time.monotonic()
            if now - last[0] >= 0.5:
                last[0] = now
                self.progress.emit(got / 1e6, total / 1e6)
        try:
            ok = siglip_engine.download(report, should_stop=lambda: not self._running)
            self.done.emit(bool(ok), "" if ok else DOWNLOAD_INTERRUPTED)
        except Exception as e:
            self.done.emit(False, str(e))


class SearchModelWarmup(QThread):
    """Načte textový model (ONNX, ~1 s; dřív sentence-transformers ~15 s) dřív, než ho uživatel potřebuje."""

    def __init__(self, precise=False, parent=None):
        super().__init__(parent)
        self.precise = precise              # Sprint 4.0: přesné hledání = textový model SigLIP 2

    def run(self):
        try:
            from ai_engine import get_text_model, get_siglip_text_model, concept_bank_vectors
            # i banka pojmů kalibrace hledání
            concept_bank_vectors(get_siglip_text_model() if self.precise else get_text_model())
        except Exception as e:          # bez modelu hledání vrátí prázdný výsledek, nespadne
            print(f"[AI Hledání] Model se nenačetl: {type(e).__name__}: {e}")


# ── Map bridge ────────────────────────────────────────────────────────────────

MAP_HOLD_TTL_S = 20.0       # nejdelší pohyb kamery mapy bez nového hlášení
MAP_POWER_POLL_MS = 5000    # map page open: power source / Battery Saver re-checked (frame cap)
MAP_REFRESH_AFTER_SWITCH_MS = 40     # markers re-sent this long after the Map tab is shown


class ClipRelay(QObject):
    """Hotový klip karty Cesty z vlákna card_media.ClipWorker -> GUI (fronta signálů Qt)."""
    ready = pyqtSignal(int, int, str)       # id Cesty (události), index zastávky, URL klipu


class CorridorRouter(QThread):
    """Gate 2 C: routes a Journey's ground legs along their whole corridor (route_corridor.py)
    off the GUI thread, nearest leg first; each finished leg goes straight to the page."""
    leg_ready = pyqtSignal(int, int, str)       # journey (event) id, leg index, JSON path

    def __init__(self, journey_id, requests, fetch, parent=None):
        super().__init__(parent)
        self.journey_id, self.requests, self.fetch = int(journey_id), requests, fetch
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for leg, wps, mode in self.requests:
            if self._stop:
                return
            try:
                path = route_corridor.route_leg(wps, mode, self.fetch)
            except Exception as e:                  # one bad tile / leg never ends the Journey
                print(f"[Journey] corridor leg {leg}: {e}")
                path = None
            if path and not self._stop:
                self.leg_ready.emit(self.journey_id, int(leg), json.dumps(path))


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

    @pyqtSlot(int, str)
    def routeJourney(self, journey_id, tile_template):
        """Gate 2 C: the Journey started -> route its legs along real roads in the background."""
        if hasattr(self.main_window, "route_journey"):
            QTimer.singleShot(0, lambda: self.main_window.route_journey(int(journey_id), str(tile_template or "")))

    @pyqtSlot(int, int, str)
    def routeJourneyDay(self, journey_id, day, tile_template):
        """A single day of a multi-day Journey started: route that day's legs (day -1 = whole)."""
        if hasattr(self.main_window, "route_journey"):
            QTimer.singleShot(0, lambda: self.main_window.route_journey(
                int(journey_id), str(tile_template or ""), day=int(day)))

    @pyqtSlot()
    def journeyClosed(self):
        """Uživatel Cestu ukončil (×, Hotovo, Esc) -> případný návrat na stránku, odkud ji spustil."""
        if hasattr(self.main_window, "journey_closed"):
            QTimer.singleShot(0, self.main_window.journey_closed)

    @pyqtSlot(bool)
    def setJourneyMusic(self, enabled):
        """Tlačítko hudby v Cestě -> zapamatovat volbu (config.json, C6)."""
        mw = self.main_window
        if hasattr(mw, "config"):
            mw.config["cesta_music"] = bool(enabled)
            save_config(mw.config)


# ── Background AI daemon ──────────────────────────────────────────────────────

PROXY_WORK_SHARE = 0.8      # u videa s převodem: podíl práce na proxy (zbytek = náhledový pás)
_housekeeping_done = False  # záloha + úklid jen při prvním průchodu démona za běh
_reconcile_done = False     # Gate 0: kontrola dostupnosti originálů jen jednou za běh

class BackgroundAIDaemon(QThread):
    people_updated     = pyqtSignal()
    embeddings_updated = pyqtSignal()          # vše znovu (migrace starého formátu)
    embeddings_added   = pyqtSignal(list)      # jen tato id (průběžné doplňování)
    thumbs_repaired    = pyqtSignal()
    face_crops_repaired = pyqtSignal(int)
    status_changed     = pyqtSignal(str)
    siglip_added       = pyqtSignal(list)      # Sprint 4.0: nové vektory přesného hledání
    siglip_progress    = pyqtSignal(int, int)  # (hotovo, celkem) – kdy přepnout hledání
    identity_checked   = pyqtSignal(dict)      # Gate 0: statistika media_identity.reconcile

    def __init__(self, face_threshold: float = 0.62, parent=None, merge_threshold=None):
        super().__init__(parent)
        self._running = True
        self._last_status = 0.0
        self.face_threshold = face_threshold
        self.merge_threshold = merge_threshold     # None = face_cluster.MERGE_THRESHOLD
        self.preview_budget_mb = preview_cache.DEFAULT_BUDGET_MB
        self.proxy_budget_mb = video_proxy.DEFAULT_BUDGET_MB
        self.ocr_enabled = True                    # Sprint 3: text na fotkách (Předvolby › ocr_text)
        self.siglip_enabled = False                # Sprint 4.0: přesné hledání (Předvolby › search_precise)
        self._heavy = False                        # Gate 0: právě běží těžký krok (power.py)
        self.last_reconcile = None

    def stop(self):
        self._running = False

    def _yield(self):
        """should_stop pro každou položku práce: během interakce (video, gesto, Cesta) čeká
        na klid (governor), na slabším PC dává UI pauzu. True = skončit."""
        if power.get().action(self._heavy) == power.PAUSE:
            self._status(power.get().describe(), force=True)     # „baterie 15 %: AI pozastavena“
        return governor.get().checkpoint(lambda: not self._running, heavy=self._heavy)

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
        # Gate 0: (krok, těžký?) – těžké kroky na baterii zpomalí / počkají na síť (power.py),
        # lehké (miniatury, opravy) jen zpomalí: knihovna musí zůstat prohlížitelná
        steps = (
            (self._housekeeping, False),
            (self._reconcile_identity, False),
            (self._repair_thumbnails, False),
            (self._migrate_embeddings, False),
            (self._refresh_embeddings, True),        # hledání: miniatury už jsou, embeddingy hned po nich
            (self._repair_face_crops, False),
            (self._backfill_fingerprints, False),
            (self._compute_phashes, True),
            (self._build_previews, False),
            (self._analyze_faces, True),
            (self._build_video_derivatives, True),
            (self._index_video_frames, True),         # až po proxy: snímky z rychlé SDR kopie
            (self._improve_person_covers, True),
            (self._score_smiles, True),
            (self._index_ocr, True),
            (self._index_siglip, True),
        )
        try:
            for step, heavy in steps:
                if not self._running:
                    break
                self._heavy = heavy
                step()
        except Exception as e:
            print(f"AI Daemon error: {e}")
        power.set_thread_eco(False)
        self.status_changed.emit("")

    def _reconcile_identity(self):
        """Gate 0: dostupnost originálů – jiné písmeno disku = přepsat cesty, odpojený disk =
        'offline' (nic se nemaže), soubory jen v cloudu = 'cloud'. Jednou za běh."""
        global _reconcile_done
        if _reconcile_done:
            return
        _reconcile_done = True
        try:
            t0 = time.perf_counter()
            conn = get_connection()
            try:
                # zjišťování bez zámku, zápis změn na konci (krátká transakce)
                stats = media_identity.reconcile(conn, should_stop=lambda: not self._running)
                conn.commit()
            finally:
                conn.close()
            stats["ms"] = round((time.perf_counter() - t0) * 1000)
            self.last_reconcile = stats
            print(f"[Identita] {stats}")
            if stats["remapped"]:
                locked_folder.load_hidden_paths()      # registr Skrytých je podle cesty
            if stats["remapped"] or stats["offline"]:
                self.identity_checked.emit(stats)
        except Exception as e:
            print(f"[Identita] {e}")

    def _backfill_fingerprints(self):
        """Gate 0: obsahové otisky položek ze starších verzí (64 kB na soubor, jen lokální)."""
        try:
            conn = get_connection()
            try:
                n = media_identity.backfill_fingerprints(
                    conn, should_stop=self._yield, exists=cloud_files.local_ok)
            finally:
                conn.close()
            if n:
                print(f"[Identita] doplněno {n} otisků")
        except Exception as e:
            print(f"[Identita] otisky: {e}")

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
            self._status(tr("status-preparing-search", done=i + 1, total=total))
            src = thumb_path if thumb_path and os.path.exists(thumb_path) else (
                file_path if media_type != "video" and cloud_files.local_ok(file_path) else None)
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
        pending = [p for p in videos if cloud_files.local_ok(p)
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
            if not cloud_files.local_ok(path):
                continue                      # Gate 0: odpojený disk / jen v cloudu
            self._status(tr("status-preparing-video-search", done=i + 1, total=total))
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
        """Náhledy v rozlišení displeje a plakáty videí na pozadí, v pořadí podle užitku: kandidáti duplicit,
        obálky událostí, pak celá knihovna od nejnovějších. Nakonec LRU úklid na rozpočet."""
        try:
            import imaging
            imaging.purge_retired_caches()                  # tónové křivky Live, značky přebarvení
            removed = preview_cache.sweep_legacy()          # Stage 2: náhledy staré verze / profilu
            if removed:
                print(f"[Previews] smazáno {removed} náhledů staré verze")
            for path, mtype in preview_build_order():
                if self._yield():
                    return
                if preview_cache.get_preview(path, touch=False) is None and cloud_files.local_ok(path):
                    self._status(tr("status-preparing-thumbs"))
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
                self._status(tr("status-checking-faces", count=pending), force=True)
                face_engine.analyze_face_crops(should_stop=self._yield)
            # Anatomická brána poslala do karantény obličej, který patří osobě (týl hlavy, kámen)
            # -> přeshlukovat, aby se z nich nestaly „osoby“
            if self._running and count_quarantined_assigned():
                self._status(tr("people-regrouping"), force=True)
                strict_recluster_faces(threshold=self.face_threshold, merge_threshold=self.merge_threshold)
                self.people_updated.emit()
            if self._running and refresh_person_covers():
                self.face_crops_repaired.emit(0)     # stejné obnovení avatarů jako po opravě ořezů
        except Exception as e:
            print(f"[Covers] {e}")

    def _repair_face_crops(self):
        """Jednorázově přeřízne deformované (protažené) ořezy obličejů u okraje fotek."""
        try:
            self._status(tr("status-checking-face-crops"), force=True)
            fixed = face_engine.repair_stretched_face_crops(should_stop=self._yield)
            if self._running:                      # pipeline ořezů / kontrakt barev se změnil
                fixed += face_engine.rerender_face_crops(should_stop=self._yield)
            if self._running:                      # samoléčení: chybějící ořezy z videí
                fixed += face_engine.restore_missing_video_face_crops(should_stop=self._yield)
            if self._running:                      # jen když DB a složka k sobě patří (pojistka)
                face_engine.purge_orphan_face_files()
            if fixed and self._running:
                self.face_crops_repaired.emit(fixed)
        except Exception as e:
            print(f"[FaceCrops] {e}")

    def _repair_thumbnails(self):
        created = repair_missing_thumbnails(
            should_stop=self._yield,
            progress=lambda i, n: self._status(tr("status-refreshing-thumbs", i=i, n=n)),
        )
        if created and self._running:
            self.thumbs_repaired.emit()

    def _compute_phashes(self):
        """Otisky pro duplicity: nové položky i přepočet zastaralých otisků (PHASH_VERSION)."""
        try:
            refresh_phashes(
                should_stop=self._yield,
                progress=lambda i, n: self._status(tr("status-fingerprints", i=i, n=n)),
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
        scanned = []
        total = len(pending)
        for idx, (media_id, file_path) in enumerate(pending):
            if self._yield():
                return
            if not cloud_files.local_ok(file_path):
                continue        # odpojený disk / jen v cloudu – zkusí se příště, nic se nestahuje
            self._status(tr("status-analyzing-faces", done=idx + 1, total=total))
            try:
                faces = face_engine.detect_and_extract_faces(media_id, file_path)
                save_faces_for_media(media_id, faces)
                if faces:
                    new_faces_found = True
                    scanned.append(media_id)
                # embedding hledání už nevzniká tady (dřív jen jednou s obličeji a bez podpisu
                # zdroje – po re-importu pod novým id zůstal u cizí fotky): _refresh_embeddings
            except Exception as e:
                print(f"[AI] {file_path}: {e}")
        if new_faces_found and self._running:
            self._status(tr("status-clustering-faces"), force=True)
            try:
                strict_recluster_faces(threshold=self.face_threshold,
                                       merge_threshold=self.merge_threshold)
            except Exception as e:
                print(f"[Cluster] {e}")
            self._apply_sidecar_faces(scanned)
            self.people_updated.emit()

    def _index_ocr(self):
        """Sprint 3: text na fotkách (OCR Windows, offline) pro hledání slovem. Poslední krok
        démona – nejpomalejší (~0,5 s na fotku HEIC) a nejméně naléhavý."""
        import ocr_engine
        if not self.ocr_enabled or not ocr_engine.available():
            return
        try:
            rows = get_media_needing_ocr()
        except Exception as e:
            print(f"[OCR] {e}")
            return
        total, batch = len(rows), []
        for i, (media_id, path, sig) in enumerate(rows):
            if self._yield():
                break
            self._status(tr("status-reading-text", done=i + 1, total=total))
            if path and os.path.exists(path) and not cloud_files.local_ok(path):
                continue                      # jen v cloudu: neukládat prázdný text, zkusit později
            text = ocr_engine.recognize_file(path) if path and os.path.exists(path) else ""
            batch.append((media_id, text, sig))
            if len(batch) >= 25:
                save_ocr_results(batch)
                batch = []
        save_ocr_results(batch)

    def _score_smiles(self):
        """Sprint 4 (Reels): úsměv na nových tvářích (smile.py – CLIP na ořezu, ~10 ms na tvář)."""
        import smile
        try:
            conn = get_connection()
            try:
                if smile.pending_faces(conn, limit=1):
                    self._status(tr("status-smiles"))
                    smile.score_faces(conn, should_stop=self._yield)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Smile] {e}")

    def _index_siglip(self):
        """Sprint 4.0: přesné hledání – vektory SigLIP 2 pro celou knihovnu (stejné zdroje jako CLIP).
        Poslední krok: ~8× pomalejší než CLIP, a do dokončení hledá CLIP. Každá dávka čeká na klid
        (governor) – během videa, gest a Cesty indexování stojí."""
        import ai_engine
        from PIL import Image
        if not self.siglip_enabled or not ai_engine.siglip_available():
            return
        try:
            rows = get_media_needing_siglip()
        except Exception as e:
            print(f"[SigLIP] {e}")
            return
        total = len(rows)
        for k in range(0, total, SIGLIP_BATCH):
            if self._yield():
                return
            self._status(tr("status-refining-search", done=min(total, k + SIGLIP_BATCH), total=total))
            images, meta = [], []
            for media_id, file_path, thumb_path, media_type, sig in rows[k:k + SIGLIP_BATCH]:
                src = thumb_path if thumb_path and os.path.exists(thumb_path) else (
                    file_path if media_type != "video" and cloud_files.local_ok(file_path) else None)
                if not src:
                    continue
                try:
                    with Image.open(src) as im:
                        images.append(im.convert("RGB"))
                    meta.append((media_id, sig))
                except Exception:
                    continue
            if not images:
                continue
            try:
                vecs = ai_engine.siglip_embed_pil(images)
                save_siglip_embeddings([(mid, v, sig) for (mid, sig), v in zip(meta, vecs)])
            except Exception as e:
                print(f"[SigLIP] {e}")
                return
            self.siglip_added.emit([mid for mid, _ in meta])
            if (k // SIGLIP_BATCH) % 8 == 0:
                self.siglip_progress.emit(*siglip_coverage())
        self.siglip_progress.emit(*siglip_coverage())
        self._status("", force=True)

    def _apply_sidecar_faces(self, media_ids):
        """Sprint 2: jména tváří z XMP sidecarů (knihovna po ztrátě databáze) a pak zápis
        sidecarů položkám, kterým shlukování přidalo pojmenovanou osobu."""
        if not media_ids:
            return
        try:
            with db_transaction() as conn:
                named = sum(xmp_sync.apply_faces(conn, mid) for mid in media_ids)
            if named:
                invalidate_centroid_cache()
                print(f"[XMP] jména ze sidecarů: {named} tváří")
        except Exception as e:
            print(f"[XMP] {e}")
        xmp_sync.touch(media_ids)


# ── Kontroly (duplicity, hraniční obličeje) jako stránky v okně ───────────────

class ScaledImageLabel(QLabel):
    """Obrázek přizpůsobený velikosti štítku (celoplošné stránky místo pevných 280 px).
    radius > 0 = obraz se kreslí se zaoblenými rohy (karty duplicit)."""
    source_changed = pyqtSignal()

    def __init__(self, parent=None, radius=0):
        super().__init__(parent)
        self.radius = radius
        self._source = QPixmap()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_source(self, pixmap):
        self._source = pixmap if pixmap is not None else QPixmap()
        if self._source.isNull():
            self.setPixmap(QPixmap())
        self._rescale()
        self.source_changed.emit()

    def source(self):
        return self._source

    def setText(self, text):
        self._source = QPixmap()
        super().setText(text)
        self.source_changed.emit()

    def paintEvent(self, event):
        pix = self.pixmap()
        if self.radius <= 0 or pix is None or pix.isNull():
            return super().paintEvent(event)
        size = pix.deviceIndependentSize()
        r = QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                   size.width(), size.height())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        clip = QPainterPath()
        clip.addRoundedRect(r, self.radius, self.radius)
        p.setClipPath(clip)
        p.drawPixmap(r, pix, QRectF(pix.rect()))
        p.end()

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


class SplitCompare(QWidget):
    """Founder audit: porovnání duplicit posuvníkem – obě fotky přes sebe ve stejném výřezu,
    vlevo od dělítka levá, vpravo pravá. Tažení / klik posune dělítko (i za letu ostré –
    škálované snímky se drží v mezipaměti podle velikosti)."""
    HANDLE = 30

    def __init__(self, label_a, label_b, parent=None):
        super().__init__(parent)
        self.a, self.b = label_a, label_b
        self.split = 0.5
        self._scaled = {}
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SplitHCursor)
        for lbl in (label_a, label_b):
            lbl.source_changed.connect(self._invalidate)
        self.tag_a = tr("dup-side-left")
        self.tag_b = tr("dup-side-right")

    def _invalidate(self):
        self._scaled.clear()
        self.update()

    def image_rect(self):
        """Společný výřez: poměr stran levé fotky (duplicity mají stejný záběr)."""
        src = self.a.source() if not self.a.source().isNull() else self.b.source()
        if src.isNull():
            return QRectF(self.rect())
        size = QSizeF(src.size()).scaled(QSizeF(self.size()), Qt.AspectRatioMode.KeepAspectRatio)
        return QRectF((self.width() - size.width()) / 2, (self.height() - size.height()) / 2,
                      size.width(), size.height())

    def _fitted(self, key, src, rect):
        if src.isNull():
            return None
        dpr = self.devicePixelRatioF()
        k = (key, src.cacheKey(), round(rect.width()), round(rect.height()), dpr)
        pm = self._scaled.get(k)
        if pm is None:
            pm = src.scaled(QSize(round(rect.width() * dpr), round(rect.height() * dpr)),
                            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            self._scaled = {kk: v for kk, v in self._scaled.items() if kk[0] != key}
            self._scaled[k] = pm
        return pm

    def set_split(self, v):
        self.split = max(0.0, min(1.0, float(v)))
        self.update()

    def _from_pos(self, x):
        r = self.image_rect()
        if r.width() > 0:
            self.set_split((x - r.left()) / r.width())

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._from_pos(e.position().x())
        e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._from_pos(e.position().x())
        e.accept()

    def paintEvent(self, _):
        t = tokens.palette()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self.image_rect()
        clip = QPainterPath()
        clip.addRoundedRect(r, 12, 12)
        p.setClipPath(clip)
        p.fillRect(r, tokens.qcolor(t["surface_2"]))
        x = r.left() + r.width() * self.split
        pa = self._fitted("a", self.a.source(), r)
        pb = self._fitted("b", self.b.source(), r)
        if pa is not None:
            p.save()
            p.setClipRect(QRectF(r.left(), r.top(), x - r.left(), r.height()), Qt.ClipOperation.IntersectClip)
            p.drawPixmap(r.topLeft(), pa)
            p.restore()
        if pb is not None:
            p.save()
            p.setClipRect(QRectF(x, r.top(), r.right() - x, r.height()), Qt.ClipOperation.IntersectClip)
            p.drawPixmap(r.topLeft(), pb)
            p.restore()
        # štítky stran
        f = QFont(tokens.ui_family())
        f.setPixelSize(11)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        fm = p.fontMetrics()
        for text, left in ((self.tag_a, True), (self.tag_b, False)):
            w = fm.horizontalAdvance(text) + 16
            pill = QRectF(r.left() + 10 if left else r.right() - 10 - w, r.top() + 10, w, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(12, 12, 14, 150))
            p.drawRoundedRect(pill, 10, 10)
            p.setPen(QColor("#FFFFFF"))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        p.setClipping(False)
        # dělítko s úchytem
        p.setPen(QPen(QColor(255, 255, 255, 235), 2))
        p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        h = QRectF(x - self.HANDLE / 2, r.center().y() - self.HANDLE / 2, self.HANDLE, self.HANDLE)
        p.setPen(QPen(QColor(0, 0, 0, 40), 1))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(h)
        p.setPen(QPen(QColor("#1C1C1E"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        c = h.center()
        for d in (-1, 1):
            p.drawPolyline(QPolygonF([QPointF(c.x() + d * 3, c.y() - 4.5), QPointF(c.x() + d * 7.5, c.y()),
                                      QPointF(c.x() + d * 3, c.y() + 4.5)]))
        p.end()


def _badge(text, kind="neutral"):
    """Štítek srovnání kvality (accent = lepší hodnota / doporučeno, neutral = údaj)."""
    t = tokens.palette()
    lbl = QLabel(text)
    lbl.setObjectName("dupBadge")
    lbl.setProperty("kind", kind)
    if kind == "accent":
        fg, bg, border = t["accent"], t["accent_soft"], "transparent"
    elif kind == "good":
        fg, bg, border = t["text"], t["surface_2"], t["hairline_strong"]
    else:
        fg, bg, border = t["text_secondary"], "transparent", t["hairline_strong"]
    lbl.setStyleSheet(f"QLabel#dupBadge {{ color:{fg}; background:{bg}; border:1px solid {border}; "
                      f"border-radius:9px; padding:1px 8px; font-size:11px; font-weight:600; }}")
    return lbl


def media_facts(path, file_size=None):
    """(šířka, výška, bajty, PŘÍPONA) z hlavičky souboru – bez dekódování obrazu. Chybějící = None."""
    w = h = None
    if path and os.path.exists(path):
        try:
            reader = QImageReader(path)
            size = reader.size()
            if size.isValid():
                w, h = size.width(), size.height()
                if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
                    w, h = h, w
        except Exception:
            pass
        if w is None:
            try:
                from PIL import Image as _PIL
                with _PIL.open(path) as im:
                    w, h = im.size
            except Exception:
                pass
        if file_size is None:
            try:
                file_size = os.path.getsize(path)
            except OSError:
                file_size = None
    ext = os.path.splitext(path or "")[1].lstrip(".").upper() or None
    return w, h, file_size, ext


def compare_facts(fa, fb):
    """Které straně patří štítky „vyšší rozlišení“ / „větší soubor“: {'a': [...], 'b': [...]}."""
    out = {"a": [], "b": []}
    pa = (fa[0] or 0) * (fa[1] or 0)
    pb = (fb[0] or 0) * (fb[1] or 0)
    if pa and pb:
        if pa > pb * 1.01:
            out["a"].append("res")
        elif pb > pa * 1.01:
            out["b"].append("res")
        else:
            out["a"].append("same_res")
            out["b"].append("same_res")
    sa, sb = fa[2] or 0, fb[2] or 0
    if sa and sb and abs(sa - sb) > 0.03 * max(sa, sb):
        out["a" if sa > sb else "b"].append("size")
    return out


class CandidateCard(QFrame):
    """Vyvýšená zaoblená karta kandidáta: štítky nahoře, fotka, pod ní název a údaje."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("dupCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        t = tokens.palette()
        self.setStyleSheet(f"QFrame#dupCard {{ background:{t['surface']}; border:1px solid {t['hairline']}; "
                           f"border-radius:{tokens.RADIUS['lg']}px; }}")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 70 if theme.current() == "dark" else 38))
        self.setGraphicsEffect(shadow)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        badge_row = QWidget()
        badge_row.setFixedHeight(22)              # i bez štítků stejná výška -> fotky obou karet zarovnané
        badge_row.setStyleSheet("background:transparent; border:none;")
        self.badges = QHBoxLayout(badge_row)
        self.badges.setSpacing(6)
        self.badges.setContentsMargins(2, 0, 2, 0)
        lay.addWidget(badge_row)
        self.image = ScaledImageLabel(radius=10)
        self.image.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(self.image, 1)
        self.name = QLabel()
        self.name.setFont(tokens.font("headline"))
        self.name.setStyleSheet(f"color:{t['text']}; background:transparent; border:none;")
        self.facts = QLabel()
        self.facts.setFont(tokens.font("callout"))
        self.facts.setStyleSheet(f"color:{t['text_secondary']}; background:transparent; border:none;")
        lay.addWidget(self.name)
        lay.addWidget(self.facts)

    def set_badges(self, badges):
        while self.badges.count():
            item = self.badges.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for text, kind in badges:
            self.badges.addWidget(_badge(text, kind))
        self.badges.addStretch(1)


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
    @property
    def title(self):
        return tr("duplicates-title")

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
        head = QHBoxLayout()
        self.lbl_title = self._review_label("headline", "text", Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.lbl_title, 1)
        # Founder audit: vedle sebe (karty) / posuvník (obě fotky přes sebe)
        self.mode_switch = SegmentedControl([(tr("dup-mode-side"), "side"), (tr("dup-mode-split"), "split")],
                                            current="side")
        self.mode_switch.changed.connect(self.set_mode)
        self.mode_switch.setToolTip(tr("dup-mode-tooltip"))
        head.addWidget(self.mode_switch, 0, Qt.AlignmentFlag.AlignRight)
        root.addLayout(head)
        self.card_a = CandidateCard()
        self.card_b = CandidateCard()
        self.lbl_a, self.lbl_b = self.card_a.image, self.card_b.image
        self.lbl_info_a, self.lbl_info_b = self.card_a.name, self.card_b.name
        side = QWidget()
        img_row = QHBoxLayout(side)
        img_row.setContentsMargins(6, 4, 6, 10)        # místo pro stín karet
        img_row.setSpacing(24)
        img_row.addWidget(self.card_a, 1)
        img_row.addWidget(self.card_b, 1)
        self.split_view = SplitCompare(self.lbl_a, self.lbl_b)
        self.compare_stack = QStackedWidget()
        self.compare_stack.addWidget(side)
        self.compare_stack.addWidget(self.split_view)
        root.addWidget(self.compare_stack, 1)
        # Doporučené ponechání (A = nejostřejší) je jediná plná akce; ostatní tlumené sklo
        self.btn_keep_a = self._review_button(tr("duplicates-keep-left"), "primary", tr("duplicates-keep-left-tooltip"))
        self.btn_keep_b = self._review_button(tr("duplicates-keep-right"), "secondary", tr("duplicates-keep-right-tooltip"))
        self.btn_keep_both = self._review_button(tr("duplicates-keep-both"), "secondary",
                                                 tr("duplicates-keep-both-tooltip"))
        self.btn_skip = self._review_button(tr("common-skip"), "ghost")
        self.btn_keep_a.clicked.connect(lambda: self._action("keep_a"))
        self.btn_keep_b.clicked.connect(lambda: self._action("keep_b"))
        self.btn_keep_both.clicked.connect(lambda: self._action("keep_both"))
        self.btn_skip.clicked.connect(lambda: self._action("skip"))
        root.addLayout(self._action_row(self.btn_keep_a, self.btn_keep_b, self.btn_keep_both, self.btn_skip))
        self.lbl_hint = self._review_label("callout", "text_tertiary")
        self.lbl_hint.setText(tr("duplicates-hint"))
        self.lbl_hint.setWordWrap(True)
        root.addWidget(self.lbl_hint)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(8)
        self.btn_undo = KButton(tr("action-undo"), variant="ghost", compact=True)
        self.btn_undo.setToolTip(tr("action-undo-decision-tooltip"))
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self._undo)
        nav_row.addWidget(self.btn_undo)
        nav_row.addStretch(1)
        self.btn_prev = KButton(tr("common-previous-arrow"), variant="secondary", compact=True)
        self.btn_next = KButton(tr("common-next-arrow"), variant="secondary", compact=True)
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
            self.lbl_title.setText(tr("duplicates-none"))
            return
        n = len(self.pairs)
        i = self.current_idx
        self.lbl_counter.setText(f"{i+1} / {n}")
        self.btn_prev.setEnabled(self._neighbor(-1) is not None)
        self.btn_next.setEnabled(self._neighbor(+1) is not None)
        pair = self.pairs[i]
        id_a, path_a, id_b, path_b, hamming = pair
        self.lbl_title.setText(tr("duplicates-possible", hamming=hamming))
        self._load_token += 1
        self._set_thumb(self.lbl_a, id_a, path_a)
        self._set_thumb(self.lbl_b, id_b, path_b)
        # A je z get_duplicate_pairs nejostřejší (pak největší) položka skupiny
        self.lbl_info_a.setText(os.path.basename(path_a) if path_a else "?")
        self.lbl_info_b.setText(os.path.basename(path_b) if path_b else "?")
        self._show_facts(id_a, path_a, id_b, path_b)

    def set_mode(self, mode):
        """'side' = dvě karty vedle sebe, 'split' = posuvné porovnání přes sebe."""
        self.compare_stack.setCurrentIndex(1 if mode == "split" else 0)
        if self.mode_switch.current_key() != mode:
            self.mode_switch.set_current(mode)

    def _show_facts(self, id_a, path_a, id_b, path_b):
        """Rozlišení, velikost a formát pod kartami + štítky toho, co je lepší."""
        sizes = {}
        try:
            conn = get_connection()
            try:
                for mid, size in conn.execute("SELECT id, file_size FROM media_items WHERE id IN (?, ?)",
                                              (id_a, id_b)).fetchall():
                    sizes[int(mid)] = size
            finally:
                conn.close()
        except Exception:
            pass
        fa = media_facts(path_a, sizes.get(int(id_a)))
        fb = media_facts(path_b, sizes.get(int(id_b)))
        self.facts = (fa, fb)
        better = compare_facts(fa, fb)
        for card, facts, side in ((self.card_a, fa, "a"), (self.card_b, fb, "b")):
            w, h, size, ext = facts
            bits = []
            if w and h:
                bits.append(f"{w} × {h}")
                bits.append(tr("dup-megapixels", mp=fmt_number(w * h / 1e6, 1)))
            if size:
                bits.append(tr("dup-file-size", mb=fmt_number(size / (1024 * 1024), 1)))
            if ext:
                bits.append(ext)
            card.facts.setText("  ·  ".join(bits))
            badges = [(tr("dup-badge-recommended"), "accent")] if side == "a" else []
            for key in better[side]:
                badges.append({"res": (tr("dup-badge-higher-res"), "good"),
                               "same_res": (tr("dup-badge-same-res"), "neutral"),
                               "size": (tr("dup-badge-larger"), "good")}[key])
            card.set_badges(badges)

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
            label.setText(tr("common-loading"))
        else:
            label.setText(tr("viewer-image-not-found"))
        if label is self.lbl_b:
            self._prefetch_following()

    def _review_size(self):
        # Velikost náhledové vrstvy: z cache se čte ~25 ms; chybí-li, první dekód ji zároveň vytvoří
        return QSize(preview_cache.PREVIEW_EDGE, preview_cache.PREVIEW_EDGE)

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
            show_toast(self, tr("error-action-save", e=e), "error")
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
            show_toast(self, tr("error-action-undo", e=e), "error")
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
        elif key == Qt.Key.Key_S and not event.modifiers():
            self.set_mode("side" if self.compare_stack.currentIndex() == 1 else "split")
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
        self.title = tr("faces-borderline-title-person", person_name=person_name)
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
        self.lbl_profile.setText(tr("faces-profile-eyebrow"))
        self.img_profile = QLabel()
        self.img_profile.setFixedSize(200, 200)
        self.img_profile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_profile.setStyleSheet(ring)
        left.addWidget(self.img_profile)
        left.addWidget(self.lbl_profile)
        right = QVBoxLayout()
        right.setSpacing(10)
        self.lbl_candidate_info = self._review_label("caption", "text_tertiary")
        self.lbl_candidate_info.setText(tr("faces-candidate-eyebrow"))
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
        self.btn_yes = self._review_button(tr("faces-yes-same"), "primary")
        self.btn_no = self._review_button(tr("faces-no-different"), "danger_outline")
        self.btn_skip = self._review_button(tr("common-skip"), "ghost")
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
            self.lbl_title.setText(tr("faces-borderline-none-short"))
            return
        n = len(self.faces)
        i = self.current_idx
        self.lbl_counter.setText(f"{i+1} / {n}")
        face = self.faces[i]
        face_id = face.get("face_id")
        crop_path = face.get("crop_path", "")
        similarity = face.get("similarity", 0.0)
        self.lbl_title.setText(tr("faces-is-this-person", person_name=self.person_name))
        self.lbl_sim.setText(tr("faces-similarity-borderline", value=round(similarity * 100)))
        self.lbl_candidate_info.setText(tr("faces-face-number", face_id=face_id))
        self._set_round_thumb(self.img_candidate, crop_path)
        try:
            from face_engine import reconstruct_best_crop_for_person
            profile_path = reconstruct_best_crop_for_person(self.person_id)
            self._set_round_thumb(self.img_profile, profile_path)
        except Exception:
            self.img_profile.setText("?")

    def _set_round_thumb(self, label, path):
        dpr = max(1.0, label.devicePixelRatioF())
        px = round(200 * dpr)                      # HiDPI: avatar ve fyzických px, ne zvětšený 2×
        img = _load_avatar_image(path, px) if path and os.path.exists(path) else QImage()
        if not img.isNull():                       # stejný středový čtvercový ořez jako karty Osob
            pix = QPixmap.fromImage(img)
            pix.setDevicePixelRatio(px / 200.0)
            label.setPixmap(pix)
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
            show_toast(self, tr("error-face-assign", e=e), "error")
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
        self.setProperty(map_host.AIRSPACE_PROP, "native")     # own HWND above the WebView2 map
        self.setProperty("kairos_airspace_radius", 8)
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
        lines = [f'Kairos · {prof.tier} · {fmt_number(prof.refresh_hz, 0)} Hz (budget {fmt_number(prof.frame_budget_ms, 1)} ms)'
                 f' · map {getattr(self._window, "map_host_kind", "?")}',
                 f'GPU {prof.gpu_name or "?"} · {prof.gpu_class}' + (' · lite map' if prof.gpu_lite else '')]
        lines += [frame_meter.format_stats(s) for s in frame_meter.all_stats()]
        held = governor.get().reasons()
        state = ("paused (" + ", ".join(held) + ")" if held else ("idle" if not governor.get().busy() else "waiting for idle"))
        lines.append(f"background: {state}")          # vývojářský překryv výkonu (F12) – anglicky
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


DOWNLOAD_INTERRUPTED = "interrupted"   # stavový kód stahování (ne text pro uživatele)


class _SyncBridge(QObject):
    """sync_server request thread -> GUI thread (AutoConnection queues across threads)."""
    changed = pyqtSignal(str, int)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config = load_config()
        i18n.notifier().changed.connect(self._on_language_changed)   # Gate 1: přepnutí jazyka za běhu
        # Sprint 2: kurátorská data i do XMP sidecarů (zápis na pozadí, vypínatelné v Předvolbách)
        xmp_sync.install(get_connection, lambda: bool(self.config.get("xmp_sidecars", True)))
        self._sync_bridge = None
        if self.config.get("sync_server_enabled", False):
            import sync_server              # iOS Phase 1: schema, certificate and bind on a worker thread
            self._install_sync_bridge()
            sync_server.start_async()
        self.setWindowTitle(brand.APP_NAME)
        self.setMinimumSize(1100, 700)
        self.resize(1280, 800)
        # Adaptivní škálování: profil stroje (strop kvality, zátěž pozadí, meze mapy)
        self.perf = perf_profile.detect(self.config.get("performance_tier"), self.config.get("gpu_profile"))
        governor.get().set_item_pause(self.perf.bg_item_pause_s)

        # State
        self._all_records = []
        self._trash_records = []
        self._hidden_records = []        # Stage 3: obsah Skrytých – jen po dobu odemčení
        self._vault_seen = set()         # id Skrytých zobrazená od odemčení (úklid prohlížeče)
        self._filtered_records = []
        self._map_loaded = False
        self._pending_journey = None     # data Cesty čekající na načtení mapy
        self._pending_place = None       # mini-map click waiting for the map page (show_place_on_map)
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

        self._reserve_rhi_surface()
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
        # Sprint 2: paleta příkazů (Ctrl + K) – stránky, osoby, události, alba, akce, hledání
        self.palette = command_palette.install(self, self._palette_commands, self._palette_search)
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
        # Stage 3: složka Skryté – odemčení, auto-zamčení, ochrana obrazovky
        self.locked = LockedFolderController(self)
        self.locked.locked.connect(self._on_hidden_locked)
        self.locked.unlocked.connect(self._on_hidden_unlocked)
        self.stack.currentChanged.connect(self._check_hidden_context)
        self._sync_hidden_entry()
        self._init_db_and_load()
        QTimer.singleShot(0, self._hidden_startup_sweep)
        QTimer.singleShot(SEARCH_WARMUP_DELAY_MS, self._warm_search_model)

    def start_deferred_services(self):
        """Pillar 5: non-critical start-up work, DEFERRED_SERVICES_MS after the grid's first frame."""
        if getattr(self, "_deferred_started", False):
            return
        self._deferred_started = True
        QTimer.singleShot(DEFERRED_SERVICES_MS, self._run_deferred_services)

    def _run_deferred_services(self):
        if getattr(self, "_shutting_down", False):
            return
        boot_trace.mark("deferred_services")
        # 120 Hz pass: zero-stall GUI thread. The reads run on bg_jobs' pool and arrive as
        # finished results (the vector tables are tens of MB; they used to stall the first frames).
        self._load_people_async()
        self._load_events_async()
        if not getattr(self, "_embeddings_loaded", False):
            self._load_embeddings_async()   # + Sprint 4.0 SigLIP readiness, in the same worker
        else:
            self._check_siglip_ready_async()
        self._start_ai_daemon()
        QTimer.singleShot(VIEWER_PREWARM_DELAY_MS, self._prewarm_viewer)
        QTimer.singleShot(SMART_IMPORT_DELAY_MS, self._smart_import_check)
        QTimer.singleShot(MAP_PREBUILD_DELAY_MS, self._ensure_map_view)     # map instant on first visit
        QTimer.singleShot(PHOTO_PRECACHE_DELAY_MS, self._start_photo_precache)

    def precache_options(self):
        """Arguments for map_precache.MapJobs.start_precache from this window (GUI thread)."""
        try:
            dpr = float(self.devicePixelRatioF()) or 1.0
        except RuntimeError:
            dpr = 1.0
        layer = self.config.get("map_layer", DEFAULT_MAP_LAYER)
        try:
            hidpi = bool(self.perf.for_map().get("map_hidpi_raster"))
        except Exception:
            hidpi = False
        return {"layer": LEGACY_MAP_LAYERS.get(layer, layer), "dpr": dpr, "hidpi_raster": hidpi,
                "budget_mb": int(self.config.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)),
                "policy": self.config.get("battery_policy", power.DEFAULT_POLICY),
                # Sprint 4: the world z0-6 overview pack first (once), so the globe is never blank offline
                "world": bool(self.config.get("map_world_base", True))}

    def _start_photo_precache(self, manual=False):
        """Sprint 3 item 3: the library's photo places into the tile store (map_precache).
        Automatic runs only with "map_prefetch" on; the job itself waits while the user
        interacts, on a metered connection and (automatic runs) on battery. Nothing here
        touches the DB or the network: both happen in the job's threads."""
        if getattr(self, "_shutting_down", False):
            return False
        if not manual and not self.config.get("map_prefetch", True):
            return False
        return map_precache.jobs().start_precache(manual=manual, **self.precache_options())

    def _nav_back(self):
        if self.nav.current() is self._viewer and self._viewer is not None:
            self._viewer.request_close()      # zastaví přehrávání a pak se vrátí
        else:
            self.nav.pop()

    def _reserve_rhi_surface(self):
        """The window is born with a Direct3D (RHI) surface instead of switching to it later.

        Qt 6 recreates a top-level window – and every native child HWND in it – the first time an
        RHI widget (the viewer's video QQuickWidget, formerly also the minimap's QWebEngineView) is added. That
        happened on the first photo open: the window flashed as if it restarted, and WebView2
        disposed the map's controller with its destroyed parent HWND (blank Map / Journey). A hidden
        1x1 QQuickWidget, created before the window exists, flags the window as RHI from birth."""
        try:
            from PyQt6.QtQuickWidgets import QQuickWidget
        except Exception as e:                 # no QtQuick: map_host still recovers the controller
            print(f"[Okno] RHI surface not reserved: {e}")
            self._rhi_anchor = None
            return
        anchor = QQuickWidget(self)
        anchor.setObjectName("kairos_rhi_anchor")
        anchor.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        anchor.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        anchor.setGeometry(0, 0, 1, 1)
        anchor.hide()
        self._rhi_anchor = anchor

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
    COLLECTION_FILTERS = {"favorites": "favorites", "videos": "video", "trash": "trash", "hidden": "hidden"}
    FILTER_TITLES = {"all": N_("nav-library"), "favorites": N_("nav-favorites"), "photo": N_("nav-library"),
                     "video": N_("nav-videos"), "trash": N_("nav-trash"), "hidden": N_("nav-locked")}

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
            NavSection(tr("nav-section-browse")),
            ("photos", tr("nav-library"), "photos"), ("people", tr("nav-people"), "people"),
            ("events", tr("nav-events"), "events"), ("map", tr("nav-places"), "map"),
            NavSection(tr("nav-section-collections")),
            ("favorites", tr("nav-favorites"), "heart"), ("videos", tr("nav-videos"), "video"),
            ("albums", tr("nav-albums"), "album"),
            ("trash", tr("nav-trash"), "trash"),
            ("hidden", tr("nav-locked"), "lock"),
            NavSection(tr("nav-section-for-you")),
            ("reels", tr("nav-reels"), "reel"), ("echoes", tr("nav-echoes"), "echo"),
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

        self.btn_import = KButton(tr("nav-import"), icon="plus")
        self.btn_import.setObjectName("btn_primary")
        self.btn_import.clicked.connect(self._start_import)
        layout.addWidget(guide.mark(self.btn_import, "import"))
        layout.addSpacing(4)

        self.btn_settings = NavItem(tr("nav-settings"), icon="settings")
        self.btn_settings.setCheckable(False)
        self.btn_settings.clicked.connect(self._open_settings)
        layout.addWidget(self.btn_settings)
        self.btn_guide = NavItem(tr("guide-title"), icon="sparkle")
        self.btn_guide.setCheckable(False)
        self.btn_guide.setToolTip(tr("nav-guide-tooltip"))
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
        toolbar, tb_layout, self.lbl_photos_title = self._make_toolbar(tr("nav-library"), self.lbl_photo_count)

        # Kairos dávka 4: pohled podle času (Roky / Měsíce = karty, Dny = větší dlaždice s datem)
        self._time_view = "all"
        self._cell_before_days = None
        self.time_switch = SegmentedControl([(tr("zoom-years"), "years"), (tr("zoom-months"), "months"), (tr("zoom-days"), "days"),
                                             (tr("zoom-all"), "all")], current="all")
        self.time_switch.changed.connect(self._set_time_view)
        tb_layout.addWidget(guide.mark(self.time_switch, "time_switch"))
        tb_layout.addSpacing(10)

        # P3.2 Search bar
        self.search_bar, self._search_icon = self._make_search(tr("search-placeholder"))
        self.search_bar.textChanged.connect(self._on_search_changed)
        tb_layout.addWidget(guide.mark(self.search_bar, "search"))

        self.btn_clear_search = KButton("", variant="ghost", compact=True, icon="x")
        self.btn_clear_search.setToolTip(tr("search-clear"))
        self.btn_clear_search.clicked.connect(self._clear_search)
        self.btn_clear_search.setVisible(False)
        tb_layout.addWidget(self.btn_clear_search)

        tb_layout.addSpacing(6)
        self.btn_slideshow = KButton(tr("slideshow"), compact=True, icon="slideshow")
        self.btn_slideshow.setToolTip(tr("slideshow-tooltip"))
        self.btn_slideshow.clicked.connect(lambda: self.start_slideshow_from_grid(self.grid_view))
        tb_layout.addWidget(self.btn_slideshow)

        # Sort combo
        # Kairos kapsle s nabídkou místo nativního QComboBox (stejné API)
        self.sort_combo = MenuSelect([tr("sort-newest"), tr("sort-oldest"), tr("sort-name"), tr("sort-size")], icon="sort")
        self.sort_combo.setAccessibleName(tr("sort-a11y"))
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
        toolbar, tb_layout, _ = self._make_toolbar(tr("nav-people"), self.lbl_people_count)

        self.people_search, self._people_search_icon = self._make_search(tr("people-search-placeholder"))
        self.people_search.textChanged.connect(self._filter_people)
        tb_layout.addWidget(self.people_search)
        tb_layout.addSpacing(6)

        # Sloučení vybraných karet (Ctrl+klik) – vidět jen při výběru 2+ osob
        self.btn_merge = KButton(tr("people-merge"), variant="primary", icon="merge")
        self.btn_merge.setToolTip(tr("people-merge-tooltip"))
        self.btn_merge.clicked.connect(lambda: self._merge_people_ids(sorted(self._selected_people)))
        self.btn_merge.setVisible(False)
        tb_layout.addWidget(self.btn_merge)

        self.btn_recluster = KButton(tr("people-regroup"), compact=True, icon="refresh")
        self.btn_recluster.setToolTip(tr("people-regroup-tooltip"))
        self.btn_recluster.clicked.connect(self._recluster_faces)
        tb_layout.addWidget(guide.mark(self.btn_recluster, "faces_recluster"))

        self.btn_borderline = KButton(tr("people-borderline"), compact=True, icon="face_question")
        self.btn_borderline.setToolTip(tr("people-borderline-tooltip"))
        self.btn_borderline.clicked.connect(self._open_borderline_review)
        tb_layout.addWidget(guide.mark(self.btn_borderline, "faces_borderline"))

        self.btn_duplicates = KButton(tr("people-duplicates"), compact=True, icon="duplicates")
        self.btn_duplicates.setToolTip(tr("people-duplicates-tooltip"))
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

        self.people_empty = EmptyState("people", tr("people-empty-title"),
                                       tr("people-empty-body"))
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
        toolbar, tb_layout, _ = self._make_toolbar(tr("nav-events"), self.lbl_events_sub)
        # Gate 2 C: recorded tracks (GPX) give a trip's Journey its true line
        self.btn_import_gpx = KButton(tr("gpx-import"), variant="secondary", compact=True, icon="route")
        self.btn_import_gpx.setToolTip(tr("gpx-import-tooltip"))
        self.btn_import_gpx.clicked.connect(lambda _checked=False: self._import_gpx())
        tb_layout.addWidget(self.btn_import_gpx)
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

        self.lbl_events_empty = EmptyState("events", tr("events-empty-title"),
                                           tr("events-empty-body"))
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
        self._event_covers = CoverVirtualizer(self._events_scroll, lambda: self.event_cards,
                                              self._event_images.request, self)
        return page

    def _build_map_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._map_layout = layout
        self.map_host_kind = "qtwebengine"
        self.map_bridge = MapBridge(self)
        # Pillar 5: the map host (WebView2 + pythonnet / Chromium, ~620 ms) is NOT built during
        # start-up – _ensure_map_view() builds it on first visit, for a Journey, or in
        # start_deferred_services() shortly after the grid's first frame.
        self.map_view = None
        self._map_view_built = False
        return page

    def _ensure_map_view(self):
        """Builds the map host once (WebView2, else QtWebEngine). True when a view exists."""
        if getattr(self, "_map_view_built", True):          # absent = built eagerly elsewhere
            return getattr(self, "map_view", None) is not None
        self._map_view_built = True
        layout = self._map_layout
        if map_host.available():
            try:
                self._build_map_view_webview2(layout)
                return True
            except Exception as e:
                print(f"[Map] WebView2 host failed, using QtWebEngine: {e}")
                self.map_view = None
        self._build_map_view_qtwebengine(layout)
        return self.map_view is not None

    def _map_html_path(self):
        return os.path.join(RESOURCE_DIR, "map_vault.html")

    def _build_map_view_webview2(self, layout):
        """Gate 2: the map in WebView2 (paced 120 Hz; QtWebEngine is locked to 60 fps) – map_host.py."""
        import paths as _paths
        view = map_host.WebView2Host(user_data_dir=os.path.join(_paths.WEB_CACHE_DIR, "wv2"), tag="Mapa")
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        view.page().setBackgroundColor(QColor(map_host.MAP_BACKGROUND))
        srv = tile_store.start(int(self.config.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
        if srv is not None:                  # C7e: like install_tile_store, before the page's scripts
            view.add_init_script(f"window.TILE_STORE_CONFIG = {json.dumps(srv.config())};")
        # Sprint 4: the machine profile before the page's scripts – a low-spec GPU's map is created
        # without MSAA (a WebGL context attribute, fixed at creation) and with flat buildings
        view.add_init_script(f"window.KAIROS_PERF_BOOT = {json.dumps(self.perf.for_map())};")
        names = map_host.bridge_slot_names(MapBridge)
        view.add_init_script(map_host.BRIDGE_JS % json.dumps(names))
        self._map_messages = map_host.WebMessageBridge(self.map_bridge, names, view)
        view.message.connect(self._map_messages.dispatch)
        view.filesDropped.connect(self._on_map_files_dropped)
        view.loadStarted.connect(self.map_bridge.release_all)      # nová stránka = staré holdy pryč
        view.loadFinished.connect(self._on_map_loaded)
        view.failed.connect(self._on_map_host_failed)
        self.map_view = view
        self.map_host_kind = "webview2"
        if os.path.exists(self._map_html_path()):
            view.load(QUrl.fromLocalFile(os.path.abspath(self._map_html_path())))
        layout.addWidget(view)

    def _on_map_host_failed(self, why):
        """WebView2 could not start (runtime broken / blocked by policy) -> QtWebEngine instead."""
        view = self.map_view
        if not isinstance(view, map_host.WebView2Host):
            return
        self._map_loaded = False
        self._map_layout.removeWidget(view)
        view.close_webview()
        view.hide()
        view.deleteLater()
        self.map_view = None
        self._build_map_view_qtwebengine(self._map_layout)

    def _on_map_files_dropped(self, paths_list):
        """Files / folders dropped onto the WebView2 map (Qt never sees that drag)."""
        folders, files = drop_import.classify_paths(paths_list)
        if folders or files:
            self.import_dropped(folders, files)

    def _build_map_view_qtwebengine(self, layout):
        self.map_host_kind = "qtwebengine"
        try:
            # Platí pro výchozí profil = mapa (viz is_allowed_web_url)
            from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
            from PyQt6.QtWebEngineWidgets import QWebEngineView
            from PyQt6.QtWebChannel import QWebChannel
            from web_console import QuietWebPage
            self._web_interceptor = _web_url_interceptor_class()(self)
            QWebEngineProfile.defaultProfile().setUrlRequestInterceptor(self._web_interceptor)

            self.map_view = QWebEngineView()
            # Patch 11: vlastní trvalý profil s diskovou HTTP cache (výchozí profil drží jen RAM)
            profile = map_web_profile()
            # QuietWebPage: konzole WebGL / MapLibre bez opakovaných hlášení za každý snímek
            self.map_view.setPage(QuietWebPage(profile, self.map_view, tag="Mapa") if profile is not None
                                  else QuietWebPage(self.map_view, tag="Mapa"))
            # Žádné menu Chromia (Zpět / Znovu načíst / Zobrazit zdroj) na pravém tlačítku
            self.map_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self.map_view.page().setBackgroundColor(QColor(map_host.MAP_BACKGROUND))
            install_tile_store(self.map_view.page(),
                               int(self.config.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
            settings = self.map_view.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            # C6: hudba Cesty startuje z tlačítka v aplikaci (ne gestem ve stránce) – bez tohohle
            # by ji Chromium zablokoval (autoplay policy)
            settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)

            self.map_channel = QWebChannel()
            self.map_channel.registerObject("bridge", self.map_bridge)
            self.map_view.page().setWebChannel(self.map_channel)
            self.map_view.loadStarted.connect(self.map_bridge.release_all)   # nová stránka = staré holdy pryč
            self.map_view.loadFinished.connect(self._on_map_loaded)

            map_html = self._map_html_path()
            if os.path.exists(map_html):
                self.map_view.load(QUrl.fromLocalFile(os.path.abspath(map_html)))
            layout.addWidget(self.map_view)
        except Exception as e:
            layout.addWidget(EmptyState("map", tr("map-unavailable-title"),
                                        tr("map-unavailable-body", e=e)), 1)
            self.map_view = None

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
            QMessageBox.critical(self, tr("error-db-title"), tr("error-db-init", e=e))
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
        self._migrate_trips()
        self._load_photos()
        self._refresh_memory()
        # Pillar 5: People / Events / search vectors / the AI daemon are not needed for the first
        # grid frame – start_deferred_services() runs them DEFERRED_SERVICES_MS after it paints
        # (pages load their own data on visit; a search loads the vectors on demand).
        QTimer.singleShot(DEFERRED_FALLBACK_MS, self.start_deferred_services)   # no grid frame (other page)

    def _migrate_trips(self):
        """A library clustered by an older trip algorithm is re-clustered once (stable ids keep
        covers, tints and links); before this, recluster only ran when an import found new files."""
        try:
            with db_transaction() as conn:
                t0 = time.perf_counter()
                stats = trips.ensure_current(conn)
            if stats is not None:
                print(f"[Events] trips v{trips.ALGO_VERSION}: {stats.get('kinds')} | kept {stats['kept']}, "
                      f"new {stats['created']}, removed {stats['deleted']} "
                      f"({(time.perf_counter() - t0) * 1000:.0f} ms)")
        except Exception as e:
            print(f"[Events] trip migration failed: {e}")

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
        show_toast(self, tr("import-found-new", count=count), "info", action_text=tr("import-in-background"),
                   on_action=lambda: self._import_in_background(folders), timeout_ms=None,
                   dismiss_text=tr("common-skip"))

    def _import_in_background(self, folders):
        """Import bez přepnutí na stránku průběhu – složky jedna po druhé, na konci toast."""
        self._enqueue_background_import([f for f in folders if f])

    @staticmethod
    def _queue_key(item):
        if isinstance(item, tuple):          # ("files" | "cloud", [cesty]) – přetažené / ze cloudu
            return (item[0],) + tuple(os.path.normcase(p) for p in item[1])
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
                show_toast(self, tr("import-done", items=_czech_item_count(self._background_imported)), "success")
            self._background_imported = 0
            return
        if self._scanner and self._scanner.isRunning():
            return                           # naváže se po doběhnutí běžícího skenu
        item = self._background_import.pop(0)
        self._background_current = item
        if isinstance(item, tuple):
            # ("cloud", …) = uživatel souhlasil se stažením souborů „jen online“ (Gate 0)
            scanner = MediaScannerThread(None, files=item[1], allow_cloud=item[0] == "cloud")
        else:
            scanner = MediaScannerThread(item)
        self._scanner = scanner
        scanner.cloud_skipped_signal.connect(self._on_cloud_skipped)
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
            label = tr("watch-folders-action", count=len(unwatched))
            show_toast(self, tr("import-importing-background", what=what), "info", action_text=label,
                       on_action=lambda: self.watch_folders(unwatched), timeout_ms=8000)
        else:
            show_toast(self, tr("import-importing-background", what=what), "info")

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
        show_toast(self, tr("watch-started", names=names), "success")

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
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized() \
                and getattr(self, "locked", None) is not None and self.locked.is_unlocked:
            self.locked.lock("minimize")
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() \
                and getattr(self, "folder_watcher", None) is not None:
            self.folder_watcher.window_activated()

    def _load_edits(self):
        """Pillar 2: Quick Edits of the library into the shared EDITS map (one small query)."""
        try:
            import quick_edit
            conn = get_connection()
            try:
                edits = quick_edit.load_many(conn)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Edit] {e}")
            edits = {}
        EDITS.clear()
        EDITS.update(edits)

    # ── iPhone LAN bridge -> grid (iOS Phase 2) ──
    def _install_sync_bridge(self):
        """Edits and favourites written by the phone (sync_server request threads) reach the grid
        through a queued Qt signal, the same refresh the viewer's own save uses. Installed when
        the bridge module is loaded: at startup (enabled) or after Settings switched it on."""
        if self._sync_bridge is not None or "sync_server" not in sys.modules:
            return
        bridge = _SyncBridge(self)
        bridge.changed.connect(self._on_remote_change)
        self._sync_bridge = bridge
        sys.modules["sync_server"].set_remote_change_handler(bridge.changed.emit)
        sys.modules["sync_server"].set_search_provider(self._phone_search)

    def _phone_search(self, query, limit):
        """/v1/search from the iPhone (a sync_server request thread): the same warm search as
        the desktop's field, over the vectors already in memory. None = not loaded yet, the bridge
        then loads them itself."""
        vectors = self._embeddings_cache
        if not vectors:
            return None
        conn = get_connection()
        try:
            universe = [r[0] for r in conn.execute(
                f"SELECT m.id FROM media_items m WHERE {visible_media_sql('m')} "
                "ORDER BY m.date_taken DESC, m.id DESC")]
        finally:
            conn.close()
        r = search_engine.run_search(query, universe, vectors)
        return {"ids": r.ids[:limit], "confident": len(r.confident), "semantic": r.semantic, "labels": r.labels}

    def _on_remote_change(self, kind, media_id):
        if kind == "import":
            self._on_phone_import()
        elif kind == "edit":
            self._on_edit_saved(media_id)
        elif kind == "favorite":
            try:
                conn = get_connection()
                try:
                    row = conn.execute("SELECT is_favorite FROM media_items WHERE id = ?", (int(media_id),)).fetchone()
                finally:
                    conn.close()
            except Exception as e:
                print(f"[Sync] {e}")
                return
            if row is not None:
                self._on_favorite_changed(int(media_id), int(row[0] or 0))

    def _on_phone_import(self):
        """Camera-roll uploads from the phone landed in the inbox (Pictures/Kairos iPhone). A
        batch arrives as many quick uploads, so they are coalesced into one incremental scan.
        The first import makes the inbox a watched library folder; from then on it is an
        ordinary folder, and the new media reach the phone through the journal like any other."""
        timer = getattr(self, "_phone_import_timer", None)
        if timer is None:
            timer = self._phone_import_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(1500)
            timer.timeout.connect(self._scan_phone_inbox)
        timer.start()

    def _scan_phone_inbox(self):
        srv = sys.modules["sync_server"].service() if "sync_server" in sys.modules else None
        if srv is None or not os.path.isdir(srv.inbox_dir):
            return
        inbox = srv.inbox_dir
        if not self._is_watched(inbox):
            self.watch_folders([inbox])
        self._enqueue_background_import([inbox], skip_running=False)

    def _on_edit_saved(self, media_id):
        """The viewer saved / reverted a Quick Edit: refresh that tile in every grid."""
        try:
            import quick_edit
            conn = get_connection()
            try:
                adj = quick_edit.load(conn, int(media_id))
            finally:
                conn.close()
        except Exception as e:
            print(f"[Edit] {e}")
            adj = None
        if adj is None or adj.is_identity():
            EDITS.pop(int(media_id), None)
        else:
            EDITS[int(media_id)] = adj
        from PyQt6 import sip
        for model in list(_LIVE_MODELS):
            if sip.isdeleted(model):               # its view (and C++ model) closed: the weak
                _LIVE_MODELS.discard(model)        # wrapper outlived it -> RuntimeError in a slot
                continue
            model.refresh_items([int(media_id)])

    def _load_photos_async(self, *_):
        """Daemon-triggered library refresh: the three grid queries (~46 ms at 20k items) and
        the edits map are read on bg_jobs' pool; the grid re-filters when they land."""
        unlocked = bool(self.locked.is_unlocked)

        def done(state):
            edits, records, trash, hidden = state
            EDITS.clear()
            EDITS.update(edits)
            self._all_records, self._trash_records = records, trash
            self._hidden_records = hidden if self.locked.is_unlocked else []
            self._apply_filter_and_sort()
        self._bg_loads().run("photos", lambda: query_library(unlocked), done, lambda e: print(f"[Load] {e}"))

    def _load_photos(self):
        self._cancel_bg("photos")
        self._load_edits()
        try:
            self._all_records = fetch_media_records()
            self._trash_records = fetch_media_records(trashed=True)
            self._hidden_records = fetch_media_records(hidden=True) if self.locked.is_unlocked else []
        except Exception as e:
            print(f"[Load] {e}")
            self._all_records = []
            self._trash_records = []
            self._hidden_records = []
        self._apply_filter_and_sort()

    def _bg_loads(self):
        """bg_jobs.BackgroundLoads of this window (created on first use)."""
        bg = getattr(self, "_bg", None)
        if bg is None:
            import bg_jobs
            bg = self._bg = bg_jobs.BackgroundLoads(self)
        return bg

    def _cancel_bg(self, key):
        bg = getattr(self, "_bg", None)
        if bg is not None:
            bg.cancel(key)

    def _load_people(self):
        """Synchronous (an explicit edit the user is waiting for); background refreshes use
        _load_people_async."""
        self._cancel_bg("people")               # a slower background read must not overwrite this
        try:
            people = query_people()
        except Exception as e:
            print(f"[People] {e}")
            people = []
        self._render_people_cards(people)

    def _load_people_async(self, *_):
        self._bg_loads().run("people", query_people, self._render_people_cards,
                             lambda e: print(f"[People] {e}"))

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
        self.btn_merge.setText(tr("people-merge-count", n=n))

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
        act_open = menu.addAction(tr("action-open"))
        act_rename = menu.addAction(tr("action-rename-ellipsis"))
        menu.addSeparator()
        act_merge = menu.addAction(tr("people-merge-count", n=n) if n >= 2 else tr("people-merge"))
        act_merge.setEnabled(n >= 2)
        if n < 2:
            act_merge.setToolTip(tr("people-select-more-tooltip"))
        act_clear = menu.addAction(tr("guide-deselect"))
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
            show_toast(self, tr("people-merge-need-two"), "info")
            return None
        target = self._merge_target(ids)
        names = []
        for pid in ids:
            name = self._people_rows[pid][0]
            if name and name != UNKNOWN_PERSON and name not in names:
                names.append(name)
        chosen = names[0] if names else None
        if len(names) > 1 and ask_name:
            chosen, ok = InWindowDialog.getItem(self, tr("people-merge"),
                                              tr("people-merge-which-name"), names, 0, False)
            if not ok:
                return None
        try:
            result = merge_people(target, [i for i in ids if i != target], name=chosen)
        except Exception as e:
            show_toast(self, tr("error-people-merge", e=e), "error")
            return None
        if not result:
            return None
        self._selected_people.clear()
        self._avatar_cache.clear()
        self._load_people()
        if self.stack.currentWidget() is not self.page_people:
            self._refresh_merge_suggestions()
        named = bool(result["name"]) and result["name"] != UNKNOWN_PERSON
        msg = tr("people-merged-into", name=result["name"], faces=result["moved_faces"]) if named else \
            tr("people-merged", faces=result["moved_faces"])
        if result["pulled_in"]:
            msg += tr("people-merged-pulled-in", count=result["pulled_in"])
        show_toast(self, msg,
                   "success", action_text=tr("action-undo"), on_action=lambda u=result["undo"]: self._undo_people_merge(u))
        return result

    def _undo_people_merge(self, undo):
        try:
            undo_merge(undo)
        except Exception as e:
            show_toast(self, tr("error-merge-undo", e=e), "error")
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
            show_toast(self, tr("error-decision-save", e=e), "error")
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
        self.lbl_people_count.setText(tr("count-people", count=visible))
        self._update_people_empty(visible)

    def _load_events(self):
        self._cancel_bg("events")
        try:
            cards, trips, tint_todo = query_events()
        except Exception as e:
            print(f"[Events] {e}")
            cards, trips, tint_todo = [], set(), []
        self._apply_events(cards, trips, tint_todo)

    def _load_events_async(self, *_):
        self._bg_loads().run("events", query_events, lambda d: self._apply_events(*d),
                             lambda e: print(f"[Events] {e}"))

    def _apply_events(self, cards, trips, tint_todo):
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
            # covers load only near the viewport of a visible Events page (CoverVirtualizer):
            # each card holding its 1600 px cover cost ~1 GB at 138 events, from start-up on
            self._events_layout.addWidget(card)
            self.event_cards.append(card)
        n = len(cards)
        self.lbl_events_sub.setText("" if not n else tr("count-events", count=n))
        self.lbl_events_empty.setVisible(n == 0)
        self._events_scroll.setVisible(n > 0)
        self._events_scroll.setVisible(n > 0)
        self._events_gen += 1
        self._event_covers.schedule()
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

    # ── Sprint 4.0: přesné hledání (SigLIP 2, volitelné) ──
    def _precise_wanted(self):
        return bool(self.config.get("search_precise", False))

    def _precise_active(self):
        """Hledá se SigLIP 2: volba zapnutá, model stažený a index téměř úplný (jinak CLIP)."""
        return self._precise_wanted() and getattr(self, "_siglip_ready", False)

    def _check_siglip_ready(self):
        import ai_engine
        ready = False
        if self._precise_wanted() and ai_engine.siglip_available():
            try:
                have, need = siglip_coverage()
                ready = need > 0 and have >= SIGLIP_READY_SHARE * need
            except Exception as e:
                print(f"[SigLIP] {e}")
        changed = ready != getattr(self, "_siglip_ready", False)
        self._siglip_ready = ready
        return changed

    def _on_siglip_progress(self, have, need):
        if getattr(self, "_siglip_ready", False) or not self._precise_wanted():
            return
        if need > 0 and have >= SIGLIP_READY_SHARE * need:
            self._siglip_ready = True
            self._load_embeddings_async()
            show_toast(self, tr("search-precise-ready"), "success")

    def _merge_siglip(self, ids):
        if self._precise_active():
            self._merge_embeddings(ids)

    def _apply_precise_setting(self, was_on):
        """Po Předvolbách: zapnuto -> stáhnout model (je-li třeba), indexovat; vypnuto -> zpět na CLIP."""
        import ai_engine
        now_on = self._precise_wanted()
        if now_on and not ai_engine.siglip_available():
            self._start_siglip_download()
        if was_on and not now_on:
            dl = getattr(self, "_siglip_download", None)
            if dl is not None and dl.isRunning():
                dl.stop()
            ai_engine.release_siglip()
        self._load_embeddings_async()          # readiness + vectors off the GUI thread

    def _start_siglip_download(self):
        import siglip_engine
        dl = getattr(self, "_siglip_download", None)
        if dl is not None and dl.isRunning():
            return
        dl = SiglipDownload(self)
        dl.progress.connect(lambda got, total: self.lbl_status.set_task(
            "siglip", tr("search-precise-downloading", got=fmt_number(got, 0), total=fmt_number(total, 0))))
        dl.done.connect(self._on_siglip_downloaded)
        self._siglip_download = dl
        self.lbl_status.set_task("siglip", tr("search-precise-downloading-start", DOWNLOAD_MB=siglip_engine.DOWNLOAD_MB))
        dl.start()

    def _on_siglip_downloaded(self, ok, error):
        self.lbl_status.set_task("siglip", "")
        if not ok:
            if error != DOWNLOAD_INTERRUPTED:
                show_toast(self, tr("search-precise-download-failed", error=error), "error")
            return
        show_toast(self, tr("search-precise-downloaded"), "info")
        if self._ai_daemon and self._ai_daemon.isRunning():
            self._ai_daemon_rerun = True
        else:
            self._start_ai_daemon()

    def _search_vectors(self, ids=None):
        return get_siglip_vectors(ids) if self._precise_active() else get_search_vectors(ids)

    def _load_embeddings(self):
        self._cancel_bg("embeddings")
        try:
            self._embeddings_cache = self._search_vectors()     # videa: plakát + snímky celého klipu
        except Exception as e:
            print(f"[Embeddings] {e}")
            self._embeddings_cache = {}
        self._embeddings_loaded = True
        self._after_embeddings_loaded()

    def _load_embeddings_async(self, *_):
        """Vectors (and SigLIP readiness) off the GUI thread; the search reruns when they land."""
        wanted = self._precise_wanted()
        self._bg_loads().run("embeddings", lambda: load_search_state(wanted), self._on_embeddings_loaded,
                             lambda e: self._on_embeddings_loaded((getattr(self, "_siglip_ready", False), {})))

    def _on_embeddings_loaded(self, state):
        ready, vectors = state
        self._siglip_ready = ready
        self._embeddings_cache = vectors
        self._embeddings_loaded = True
        self._after_embeddings_loaded()

    def _check_siglip_ready_async(self):
        wanted = self._precise_wanted()

        def done(ready):
            if ready != getattr(self, "_siglip_ready", False):
                self._siglip_ready = ready
                self._load_embeddings_async()          # the other index now answers searches
        self._bg_loads().run("siglip_ready", lambda: siglip_ready_state(wanted), done)

    def _after_embeddings_loaded(self):
        if getattr(self, "_search_after_embeddings", False):
            self._search_after_embeddings = False
            self._run_ai_search()                      # typed before the vectors arrived
            return
        # démon opravil / doplnil embeddingy -> běžící hledání se zpřesní samo
        if self._search_query.strip() and self._search_result is not None:
            self._search_debounce.start()

    def _merge_embeddings(self, ids):
        """Démon doplnil vektory těchto položek: jen je přidat do cache. Dřív se každých 25
        položek načítala celá knihovna v GUI vlákně (desítky MB, sekundové záseky)."""
        ids = [int(i) for i in ids]
        precise = self._precise_active()
        self._merge_seq = getattr(self, "_merge_seq", 0) + 1       # every batch lands (no latest-wins)
        self._bg_loads().run(f"merge-{self._merge_seq}",
                             lambda: get_siglip_vectors(ids) if precise else get_search_vectors(ids),
                             lambda fresh: self._apply_merged_vectors(fresh, precise),
                             lambda e: print(f"[Embeddings] {e}"))

    def _apply_merged_vectors(self, fresh, precise):
        if precise != self._precise_active():
            return                                     # the index switched meanwhile: a full load follows
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
        self._ai_daemon.ocr_enabled = bool(self.config.get("ocr_text", True))
        self._ai_daemon.siglip_enabled = self._precise_wanted()
        self._ai_daemon.siglip_added.connect(self._merge_siglip)
        self._ai_daemon.siglip_progress.connect(self._on_siglip_progress)
        self._ai_daemon.people_updated.connect(self._load_people_async)
        self._ai_daemon.embeddings_updated.connect(self._load_embeddings_async)
        self._ai_daemon.embeddings_added.connect(self._merge_embeddings)
        self._ai_daemon.thumbs_repaired.connect(self._load_photos_async)
        self._ai_daemon.face_crops_repaired.connect(self._on_face_crops_repaired)
        self._ai_daemon.status_changed.connect(self._on_daemon_status)
        self._ai_daemon.identity_checked.connect(self._on_identity_checked)
        self._ai_daemon.finished.connect(self._on_ai_daemon_finished)
        power.get().set_policy(self.config.get("battery_policy", power.DEFAULT_POLICY))
        self._ai_daemon.start()

    def _on_cloud_skipped(self, count, size_bytes, files):
        """Gate 0: sken přeskočil soubory „jen online“ (OneDrive / iCloud). Bez výslovného
        souhlasu se nic nestahuje – nabídka stáhnout a naimportovat je na pozadí."""
        if count <= 0 or not files:
            return
        size = cloud_files.human_size(size_bytes)
        show_toast(self, tr("cloud-only-files", count=count, size=size), "info",
                   action_text=tr("cloud-download-import", size=size),
                   on_action=lambda f=list(files): self._enqueue_background_import([("cloud", f)]),
                   timeout_ms=None, dismiss_text=tr("common-not-now"))

    def _on_identity_checked(self, stats):
        """Gate 0: výsledek kontroly dostupnosti – přesunuté disky se tiše přepojily, odpojené
        jsou Offline (alba, tváře a oblíbené zůstávají; nic se nemaže)."""
        remapped, offline = int(stats.get("remapped", 0)), int(stats.get("offline", 0))
        if remapped:
            self._load_photos()
        if offline:
            vols = len(stats.get("offline_volumes") or {}) or 1
            show_toast(self, tr("offline-items", count=offline, disks=vols), "info")

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
        if f == "hidden":
            return list(self._hidden_records) if self.locked.is_unlocked else []
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
        if self._search_query.strip() and f != "hidden":     # Skryté nejsou v indexu hledání
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
                return tr("search-nothing-matches", query=query)
            sure = count - possible
            if not possible:
                return tr("search-results-for", results=_czech_result_count(sure), query=query)
            if not sure:
                return tr("search-possible-only", possible=possible, query=query)
            return tr("search-results-and-possible", results=_czech_result_count(sure), possible=possible, query=query)
        if self._current_filter == "trash":
            return tr("trash-count-subtitle", items=_czech_item_count(count)) if count else ""
        if self._current_filter == "hidden":
            return tr("locked-count-subtitle", items=_czech_item_count(count)) if count else ""
        return _czech_item_count(count)

    def _set_filter(self, key: str):
        if self._current_filter == "hidden" and key != "hidden":
            self.locked.lock("navigation")           # odchod ze Skrytých = zamknout
        self._current_filter = key
        model = self.grid_view.photo_model
        if key == "hidden":
            model.vault = self.locked.vault if self.locked.is_unlocked else None
        elif model.vault is not None:
            model.purge_vault()
        self.lbl_photos_title.setText(tr(self.FILTER_TITLES.get(key, "nav-library")))
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
        if key == "echoes":
            self.open_echoes()
            return
        if key == "reels":
            self.open_reels()
            return
        if key == "albums":
            self.open_albums()
            return
        if key == "photos" and self._current_filter != "all":
            self._set_filter("all")
        self._switch_page(self.SIDEBAR_PAGES.index(key))

    def _open_collection(self, key):
        """Kolekce = mřížka Fotografie s filtrem (Oblíbené, Videa, Smazané, Skryté)."""
        if key == "hidden" and not self.locked.is_unlocked:
            if not self.locked.request_unlock(self):
                self._sync_sidebar()                 # zrušeno: jezdec zpět na předchozí položku
                return
        self._set_filter(self.COLLECTION_FILTERS[key])
        self._switch_page(0)

    def _sync_sidebar(self):
        """Jezdec v postranním panelu podle stránky a filtru (i po programové změně)."""
        idx = self.stack.currentIndex()
        if not 0 <= idx < len(self.SIDEBAR_PAGES):
            for sub_key in ("echoes", "albums", "reels"):   # Pro vás / Alba i podstránky
                if any(p.property("sidebar_key") == sub_key for p in self.nav.pages()):
                    self.sidebar_nav.set_current(sub_key)
                    return
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
            content = ("search", tr("search-no-results", query=query),
                       tr("search-no-results-body"))
            action = ("clear", tr("search-clear"))
        elif f == "trash":
            content = ("trash", tr("trash-empty-title"),
                       tr("trash-empty-body"))
        elif f == "hidden" and not self.locked.is_unlocked:
            content = ("lock", tr("locked-locked-title"),
                       tr("locked-locked-body"))
            action = ("unlock", "Odemknout")
        elif f == "hidden":
            content = ("lock", tr("locked-empty-title"),
                       tr("locked-empty-body"))
        elif f == "favorites":
            content = ("heart", tr("favorites-empty-title"),
                       tr("favorites-empty-body"))
        elif f == "video":
            content = ("video", tr("videos-empty-title"), tr("videos-empty-body"))
        elif f == "photo":
            content = ("photos", tr("photos-empty-title"), tr("photos-empty-body"))
        else:
            content = ("photos", tr("library-empty-title"),
                       tr("library-empty-body"))
            action = ("import", tr("nav-import"))
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
        if self._photos_empty_action == "unlock":
            if self.locked.request_unlock(self):
                self._set_filter("hidden")
        elif self._photos_empty_action == "clear":
            self._clear_search()
        elif self._photos_empty_action == "import":
            self._start_import()

    def _update_people_empty(self, visible):
        if not self._person_cards:
            self.people_empty.set_content("people", tr("people-empty-title"),
                                          tr("people-empty-body"))
        else:
            q = self.people_search.text().strip()
            self.people_empty.set_content("search", tr("people-nobody-matches", q=q),
                                          tr("people-nobody-matches-body"))
        self.people_empty.setVisible(visible == 0)

    # ── Gate 1: jazyk rozhraní za běhu ────────────────────────────────────────
    def apply_language(self, setting):
        """Předvolby › Jazyk: přepne katalog, přeloží viditelné texty a obnoví pohledy (bez restartu)."""
        return i18n.set_language(i18n.resolve(setting))

    def _on_language_changed(self, code):
        """Texty skládané z dat (počty, data, názvy) se přepočítají; statické přeložil i18n.Retranslator."""
        steps = (
            lambda: self.lbl_photos_title.setText(tr(self.FILTER_TITLES.get(self._current_filter, "nav-library"))),
            self._apply_filter_and_sort, self._refresh_moments, self._load_people, self._load_events,
            self._push_map_strings, self._refresh_memory,
        )
        for step in steps:
            try:
                step()
            except Exception as e:          # jeden pohled nesmí zastavit přepnutí ostatních
                print(f"[i18n] refresh after language change: {e}")

    def _push_map_strings(self):
        """Texty stránky mapy (map_vault.html) v aktuálním jazyce – posílají se i po každém načtení."""
        view = getattr(self, "map_view", None)
        if view is None:
            return
        strings = i18n.page_strings("map-")
        view.page().runJavaScript(
            f"window.setStrings && window.setStrings({json.dumps(strings, ensure_ascii=False)}, "
            f"{json.dumps(i18n.current())});")

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
        precise = self._precise_active()
        loaded = ai_engine._siglip_text if precise else ai_engine._text_model
        if self._search_warmup is not None or not self._embeddings_cache or loaded is not None:
            return
        self._search_warmup = SearchModelWarmup(precise)
        self.lbl_status.set_task("search_model", tr("search-preparing"))
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
            status.set_task("search", tr("search-searching", query=query) if ai_engine._text_model is not None
                            else tr("search-preparing"))
        # QThread.quit() vlákno bez event loopu nezastaví a přepsání reference by zničilo
        # běžící QThread (pád). Starší hledání doběhnou samy, jejich výsledky se zahodí.
        # Hledá se v aktuální kolekci (i v koši); fazety (místo, čas, typ) fungují i bez modelu.
        if not getattr(self, "_embeddings_loaded", False):
            # typed before the deferred start-up tick: the vectors are read in the background
            # (tens of MB – never on the GUI thread). With nothing cached yet the search waits
            # for them; otherwise it runs now and refines itself when they land.
            # Decide BEFORE starting the load: it may deliver at once (cached / SYNC) and its
            # _after_embeddings_loaded must already see the flag, else the search never runs
            # and the "search" activity indicator sticks.
            if not self._embeddings_cache:
                self._search_after_embeddings = True
                if not self._bg_loads().pending("embeddings"):
                    self._load_embeddings_async()          # may rerun this search inline
                return
            if not self._bg_loads().pending("embeddings"):
                self._load_embeddings_async()
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
            self._load_people_async()           # the tab shows at once; fresh cards follow
        elif idx == 2:
            self._load_events_async()
        elif idx == 3:
            self._ensure_map_view()
            # after the tab has painted: the marker query + JSON grows with the library and would
            # hold the switch (the warm map surface is already showing the pins it had)
            QTimer.singleShot(MAP_REFRESH_AFTER_SWITCH_MS, self._refresh_shown_map)

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
        self._map_cap_pushed = self._map_fps_cap()
        page.runJavaScript(f"window.setFrameCap && window.setFrameCap({self._map_cap_pushed});")
        page.runJavaScript(f"window.setBaseLayer && window.setBaseLayer({json.dumps(layer)});")
        page.runJavaScript(f"window.setJourneyMusic && window.setJourneyMusic({json.dumps(self._music_config())});")
        self._push_map_strings()
        self._sync_accent()
        self._sync_map_active()
        self._refresh_map()
        place, self._pending_place = getattr(self, "_pending_place", None), None
        if place is not None:               # mini-map click before the map page had loaded
            page.runJavaScript(f"window.focusPlace && window.focusPlace({place[0]!r}, {place[1]!r});")
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
        if active:                           # the power source may have changed since the last visit
            self._push_map_fps_cap(force=True)
        else:
            self._map_web_qos(False)
        timer = getattr(self, "_map_power_timer", None)
        if active and timer is None:         # unplugged / Battery Saver while the map is open
            timer = self._map_power_timer = QTimer(self)
            timer.setInterval(MAP_POWER_POLL_MS)
            timer.timeout.connect(self._push_map_fps_cap)
        if timer is not None:
            timer.start() if active else timer.stop()

    def _map_web_qos(self, high):
        """Map shown: the web processes that draw it run at full speed even when Windows would
        throttle them (battery, Energy Saver, best power efficiency); hidden: system default.
        Re-applied on the 5 s power poll because Chromium rewrites the flags itself (power.py)."""
        if self.config.get("map_high_qos", True) is False:
            high = False
        if not high and not getattr(self, "_map_qos_on", False):
            return
        self._map_qos_on = bool(high)
        # the process-table walk costs ~12 ms – not on the GUI thread during a tab switch.
        # One worker keeps the on/off order.
        pool = getattr(self, "_qos_pool", None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = self._qos_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="map-qos")
        pool.submit(power.set_web_processes_interactive, high)

    def _push_map_fps_cap(self, force=False):
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return
        if self.stack.currentWidget() is self.page_map:
            self._map_web_qos(True)
        cap = self._map_fps_cap()
        if force or cap != getattr(self, "_map_cap_pushed", None):
            self._map_cap_pushed = cap
            self.map_view.page().runJavaScript(f"window.setFrameCap && window.setFrameCap({cap});")

    def _map_fps_cap(self):
        """Map frame cap: 0 = the panel's own rate. Only Windows Battery Saver (an explicit "save
        power" from the user) caps the map at 60; plain battery keeps the panel rate – the founder's
        bar is a fluid map on battery, and the panel already drops itself to 60 Hz there on most
        laptops. CPU pressure is handled in the page (even-cadence pacing governor), not here.
        Sprint 4: a low-spec GPU (perf_profile.map_fps_cap) holds a steady 60 on a faster panel –
        an even 60 looks smoother than a 120 Hz target that drops every other frame.
        Battery policy "full" lifts only the battery cap; config "map_fps_cap" overrides everything."""
        try:
            cap = int(self.config.get("map_fps_cap", 0) or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            return cap
        gpu_cap = getattr(getattr(self, "perf", None), "map_fps_cap", 0)
        gpu_cap = gpu_cap if isinstance(gpu_cap, int) and gpu_cap > 0 else 0
        if self.config.get("battery_policy", power.DEFAULT_POLICY) == "full":
            return gpu_cap
        try:
            st = power.get().state()
            return 60 if (st.on_battery and st.saver) else gpu_cap
        except Exception:
            return gpu_cap

    def _map_warm_allowed(self):
        """Background tile warm-up (fidelity pass): on unless config "map_prefetch" is off;
        on battery only with the "full" battery policy (it is network and disk work)."""
        if not self.config.get("map_prefetch", True):
            return False
        if self.config.get("battery_policy", power.DEFAULT_POLICY) == "full":
            return True
        try:
            return not power.get().state().on_battery
        except Exception:
            return True

    def _warm_event_region(self, event_id: int):
        """An event was opened: its places (bounding box z10-13, around the photos z14-16) go
        into the tile store in the background, so a later zoom into them is never blank."""
        if getattr(self, "map_view", None) is None or not self._map_loaded or not self._map_warm_allowed():
            return
        try:
            conn = get_connection()
            try:
                rows = conn.execute(f"""
                    SELECT DISTINCT ROUND(m.longitude, 3), ROUND(m.latitude, 3) FROM media_items m
                    WHERE m.event_id = ? AND m.latitude IS NOT NULL AND m.longitude IS NOT NULL
                      AND {visible_media_sql('m')}
                    LIMIT 400""", (int(event_id),)).fetchall()
            finally:
                conn.close()
        except Exception as e:
            print(f"[Map] warm-up: {e}")
            return
        pts = [[float(lon), float(lat)] for lon, lat in rows
               if lon is not None and lat is not None and abs(lat) <= 85 and abs(lon) <= 180
               and (lat, lon) != (0.0, 0.0)]
        if not pts:
            return
        lons, lats = [p[0] for p in pts], [p[1] for p in pts]
        spec = {"bbox": [min(lons), min(lats), max(lons), max(lats)], "points": pts[:160], "zmin": 10, "zmax": 16}
        self.map_view.page().runJavaScript(f"window.warmRegion && window.warmRegion({json.dumps(spec)});")

    def play_journey(self, event_id: int, return_here: bool = False):
        """Cesta: zastávky události -> kinematické přehrávání na mapě (journey.py).
        Nenačtená mapa si data podrží (_pending_journey) a přehraje je v _on_map_loaded.
        return_here: mapa se vloží jako podstránka nad aktuální stránku (detail / přehled
        událostí) a konec Cesty nebo Zpět vrátí přesně tam – místo resetu na kořen Mapy."""
        if not MainWindow._ensure_map_view(self):
            return False
        try:
            conn = get_connection()
            try:
                data = journey.build_journey(
                    conn, event_id, get_local_thumb_url,
                    image_url=lambda p: get_local_thumb_url(preview_cache.get_preview(p, touch=False)))
                row = conn.execute("SELECT tint FROM events WHERE id = ?", (int(event_id),)).fetchone()
                clips = MainWindow._journey_clips(self, conn, data) if data is not None else []
                if data is not None:
                    MainWindow._journey_music(self, conn, data)
            finally:
                conn.close()
        except Exception as e:
            print(f"[Map] Cesta: {e}")
            return False
        if data is None:
            return False
        MainWindow._start_clip_worker(self, data["id"], clips)
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
        self._journey_data = data            # Gate 2 C: corridor routing reads the stops from here
        if not self._map_loaded:
            self._pending_journey = data
            return True
        self.map_view.page().runJavaScript(f"window.playJourney && window.playJourney({json.dumps(data)});")
        return True

    def route_journey(self, journey_id: int, tile_template: str, day: int = -1):
        """Starts CorridorRouter for the playing Journey (legs with a GPX line are skipped).
        day >= 0: one day of a multi-day Journey is playing; its own stops are routed."""
        old = getattr(self, "_corridor_router", None)
        if old is not None:
            old.stop()
            try:
                old.leg_ready.disconnect()
            except TypeError:
                pass
        self._corridor_router = None
        data = getattr(self, "_journey_data", None)
        srv = tile_store.service()
        if not data or int(data.get("id", -1)) != int(journey_id) or not tile_template or srv is None:
            return False
        film = data
        if day >= 0:
            days = data.get("days") or []
            if day >= len(days) or len(days[day].get("stops") or []) < 2:
                return False
            film = days[day]
        skip = {int(k) for k in (film.get("tracks") or {})}
        reqs = [r for r in route_corridor.leg_requests(film["stops"]) if r[0] not in skip]
        if not reqs:
            return False
        worker = CorridorRouter(journey_id, reqs, route_corridor.store_fetcher(tile_template, srv.resolve), self)
        worker.leg_ready.connect(lambda jid, leg, path, d=int(day): self._on_corridor_leg(jid, leg, path, d))
        worker.finished.connect(lambda w=worker: self._forget_router(w))
        worker.finished.connect(worker.deleteLater)
        self._corridor_router = worker
        worker.start(QThread.Priority.LowPriority)
        return True

    def _import_gpx(self, files=None):
        """Gate 2 C: GPX tracks -> library; each binds to the trip it overlaps in time."""
        if files is None or isinstance(files, bool):  # clicked(bool) must never pose as a file list
            files, _ = QFileDialog.getOpenFileNames(self, tr("gpx-import-pick"), "", tr("gpx-file-filter"))
        if not files:
            return []
        results, failed = [], []
        conn = get_connection()
        try:
            for path in files:
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                    results.append(gpx_tracks.import_gpx(conn, data, os.path.basename(path)))
                except (OSError, gpx_tracks.GpxError) as e:
                    print(f"[GPX] {path}: {e}")
                    failed.append(os.path.basename(path))
            conn.commit()
        finally:
            conn.close()
        bound = [r for r in results if r.get("event_id")]
        if bound and len(results) == 1:
            show_toast(self, tr("gpx-imported-bound", event=event_title(bound[0]["event_title"] or "")), "success",
                       action_text=tr("journey"),
                       on_action=lambda eid=bound[0]["event_id"]: self._request_journey(eid))
        elif results:
            show_toast(self, tr("gpx-imported", count=len(results), bound=len(bound)),
                       "success" if bound else "info")
        if failed:
            show_toast(self, tr("gpx-import-failed", files=", ".join(failed)), "error")
        return results

    def _forget_router(self, worker):
        if getattr(self, "_corridor_router", None) is worker:
            self._corridor_router = None

    def _on_corridor_leg(self, journey_id, leg, path_json, day=-1):
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return
        self.map_view.page().runJavaScript(
            f"window.corridorRoute && window.corridorRoute({int(journey_id)}, {int(leg)}, {path_json}, {int(day)});")

    def _journey_music(self, conn, data):
        """Gate 2 D: the track whose energy fits the trip's mood (mood.py) -> data["music"]."""
        try:
            tracks = ambient_music.tracks()
            if not tracks:
                return None
            for t in tracks:
                t["profile"] = ambient_music.track_profile(t, compute=False)
            m = mood.trip_mood(conn, data["id"])
            best = mood.rank_tracks(tracks, m, salt=data["id"])[0]
            data["music"] = {"file": best["file"], "energy": m["energy"], "brightness": m["brightness"]}
            if self.config.get("cesta_music", False) and (
                    any(t["profile"] is None for t in tracks) or not mood.load_prompt_bank()):
                MainWindow._warm_music(self)          # next Journey matches on every signal
            return data["music"]
        except Exception as e:
            print(f"[Music] mood: {e}")
            return None

    def _warm_music(self):
        """Background: prompt bank for the vibe signal + profiles of user tracks (once)."""
        if getattr(self, "_music_warming", False):
            return
        self._music_warming = True

        def work():
            try:
                for t in ambient_music.tracks():
                    ambient_music.track_profile(t)
                mood.warm_prompt_bank()
            except Exception as e:
                print(f"[Music] warm-up: {e}")
            finally:
                self._music_warming = False
        threading.Thread(target=work, name="music-warm", daemon=True).start()

    def add_music(self, files=None, parent=None):
        """Gate 2 D: Add Music… -> copies into %APPDATA%/Kairos/music; Journeys use it next."""
        if files is None:
            files, _ = QFileDialog.getOpenFileNames(parent or self, tr("music-add-pick"), "", tr("music-file-filter"))
        if not files:
            return []
        try:
            added = ambient_music.add_user_tracks(files)
        except OSError as e:
            print(f"[Music] {e}")
            show_toast(self, tr("music-add-failed"), "error")
            return []
        if added:
            show_toast(self, tr("music-added", count=len(added)), "success")
            MainWindow._warm_music(self)
            if getattr(self, "map_view", None) is not None and self._map_loaded:
                self.map_view.page().runJavaScript(
                    f"window.setJourneyMusic && window.setJourneyMusic({json.dumps(self._music_config())});")
        return added

    def _music_config(self):
        """C6: přibalené skladby pro Cestu + volba uživatele (výchozí vypnuto)."""
        # Sprint 4: s mapou dob – Director přichytí příjezdy na zastávky na doby (director.js)
        return {"enabled": bool(self.config.get("cesta_music", False)),
                "tracks": [{"title": t["title"], "url": QUrl.fromLocalFile(t["path"]).toString(),
                            "file": t["file"], "user": t["user"],
                            "beats": ambient_music.beat_map(t, compute=not t["user"])}
                           for t in ambient_music.tracks()]}

    def _journey_clips(self, conn, data):
        """Cesta 3.0: klipy karet (Live Photo, 3 s videa) – jen vysoký profil. Hotové se přidají
        rovnou do dat zastávek (best.clip), chybějící vyrobí _start_clip_worker za běhu."""
        if getattr(self, "perf", None) is None or self.perf.tier != "high":
            return []
        try:
            clips = card_media.plan(conn, data)
        except Exception as e:
            print(f"[Map] Cesta – klipy: {e}")
            return []
        for c in clips:
            if c["ready"]:
                data["stops"][c["stop"]]["best"]["clip"] = QUrl.fromLocalFile(c["path"]).toString()
        return clips

    def _start_clip_worker(self, journey_id, clips):
        old = getattr(self, "_clip_worker", None)
        if old is not None:
            old.stop()
        self._clip_worker = None
        if not any(not c["ready"] for c in clips):
            return
        if getattr(self, "_clip_relay", None) is None:
            self._clip_relay = ClipRelay(self)
            self._clip_relay.ready.connect(self._on_clip_ready)
        relay = self._clip_relay
        worker = card_media.ClipWorker(
            clips, lambda item, path: relay.ready.emit(int(journey_id), int(item["stop"]),
                                                       QUrl.fromLocalFile(path).toString()))
        self._clip_worker = worker
        worker.start()

    def _on_clip_ready(self, journey_id, stop, url):
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return
        self.map_view.page().runJavaScript(
            f"window.journeyClip && window.journeyClip({int(journey_id)}, {int(stop)}, {json.dumps(url)});")

    def _request_journey(self, event_id: int):
        """Tlačítko Cesta (karta / hlavička události). Odloženě: přepnutí stránky ruší podstránku,
        ze které klik přišel."""
        def go():
            if not self.play_journey(int(event_id), return_here=True):
                show_toast(self, tr("journey-not-enough-places"), "info")
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
        if getattr(self, "_corridor_router", None) is not None:
            self._corridor_router.stop()          # Journey over: no more legs to route
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
        self._start_photo_precache()          # photo places for the new base layer (map_precache)

    def _refresh_shown_map(self):
        """Deferred refresh of a tab switch: only while the map is still what the user sees."""
        if self.isVisible() and self.stack.currentWidget() is self.page_map:
            self._refresh_map()

    def _refresh_map(self):
        """Pošle do mapy všechny viditelné fotky s polohou (R*Tree index přes query_geo_bounds)."""
        if getattr(self, "map_view", None) is None or not self._map_loaded:
            return   # stránka ještě není načtená -> data pošle _on_map_loaded
        # Torture test 2026-11-06: at 20k items this ran on the GUI thread (geo query, 20k file
        # stats + URLs, a 4.8 MB json.dumps, the journeys) and froze the grid 2-7 s mid-fling
        # when the background-warmed map finished loading. The payload is built on bg_jobs'
        # pool (latest wins: a burst of refreshes sends one); only runJavaScript stays here.
        self._bg_loads().run("map-markers", map_refresh_payload, self._send_map_payload,
                             lambda e: print(f"[Map] {e}"))

    def _send_map_payload(self, payload):
        view = getattr(self, "map_view", None)
        if view is None or not self._map_loaded:
            return
        markers_js, trips_js = payload
        if markers_js is not None:
            view.page().runJavaScript(markers_js)
        if trips_js is not None:
            view.page().runJavaScript(trips_js)

    def open_viewer_for_ids(self, ids):
        """Prohlížeč nad konkrétními fotkami (shluk z mapy), seřazenými podle data."""
        ids = [int(i) for i in ids][:900]   # limit parametrů SQLite
        if not ids:
            return
        try:
            records = fetch_media_records(f"m.id IN ({','.join('?' * len(ids))})", tuple(ids),
                                          order_by="m.date_taken, m.id")
        except Exception as e:
            show_toast(self, tr("error-open-photos", e=e), "error")
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
            show_toast(self, tr("error-open-file", e=e), "error")
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
            self._viewer.edits_changed.connect(self._on_edit_saved)
            self._viewer.export_requested.connect(lambda mid: self.export_media_ids([mid], tr("export-web-default-title")))
            self._viewer.favorite_changed_signal.connect(
                lambda media_id, is_fav: self._on_favorite_changed(media_id, is_fav, self._viewer_model))
            self._viewer.fullscreen_toggled.connect(lambda on: self._set_immersive("viewer", on))
            self._viewer.slideshow_requested.connect(
                lambda records, index: self._start_slideshow(records, index, self._viewer_model))
            self._viewer.show_on_map_requested.connect(self.show_place_on_map)
            self._viewer.transition_provider = self._viewer_transition
        return self._viewer

    def show_place_on_map(self, lat: float, lon: float):
        """Mini-map click in the viewer: the Map tab, flown to the photo's place. A map page that
        hasn't loaded yet keeps the place (_pending_place) and flies there in _on_map_loaded."""
        if not MainWindow._ensure_map_view(self):
            return False
        self._switch_page(self.SIDEBAR_PAGES.index("map"))      # closes the viewer like the sidebar
        if not self._map_loaded:
            self._pending_place = (float(lat), float(lon))
            return True
        self.map_view.page().runJavaScript(f"window.focusPlace && window.focusPlace({float(lat)!r}, {float(lon)!r});")
        return True

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
            show_toast(self, tr("slideshow-nothing"), "info")
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
            show_toast(self, tr("error-open-file", e=e), "error")
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
        if self._current_filter == "hidden":
            self._show_hidden_context_menu(rows, pos)
            return
        menu = QMenu(self)
        act_fav = menu.addAction(tr("grid-toggle-favorite"))
        act_open = menu.addAction(tr("action-open"))
        ids = [int(self._filtered_records[r][0]) for r in rows if 0 <= r < len(self._filtered_records)]
        act_hide = act_export = None
        if self._current_filter != "trash":
            self._add_album_menu(menu, ids)
            act_export = menu.addAction(tr("action-export-ellipsis"))
        menu.addSeparator()
        act_info = menu.addAction(tr("grid-file-info"))
        menu.addSeparator()
        in_trash = self._current_filter == "trash"
        if not in_trash:
            act_hide = menu.addAction(tr("locked-move-to"))
        act_trash = menu.addAction(tr("trash-restore") if in_trash else tr("trash-move-to"))
        action = menu.exec(self.grid_view.mapToGlobal(pos))
        if action is not None and action == act_hide:
            self.hide_media_ids(ids)
        elif action is not None and action == act_export:
            self.export_media_ids(ids, self._search_query.strip() or "Export")
        elif action == act_trash:
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
            show_toast(self, tr("error-action", e=e), "error")
            return
        self._on_library_changed()
        n = len(ids)
        if trashed:
            show_toast(self, tr("trash-moved", items=_czech_item_count(n)), "success",
                       action_text=tr("action-undo"), on_action=lambda: self._undo_trash(ids))
        else:
            show_toast(self, tr("trash-restored", items=_czech_item_count(n)), "success")

    def _undo_trash(self, ids):
        try:
            restore_media(ids)
        except Exception as e:
            show_toast(self, tr("error-restore", e=e), "error")
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

    # ── Stage 3: složka Skryté ───────────────────────────────────────────────

    def _show_hidden_context_menu(self, rows, pos):
        ids = [int(self._filtered_records[r][0]) for r in rows if 0 <= r < len(self._filtered_records)]
        menu = QMenu(self)
        act_open = menu.addAction(tr("action-open"))
        act_info = menu.addAction(tr("grid-file-info"))
        menu.addSeparator()
        act_unhide = menu.addAction(tr("locked-return-to-library"))
        menu.addSeparator()
        act_lock = menu.addAction(tr("locked-lock"))
        action = menu.exec(self.grid_view.mapToGlobal(pos))
        if action == act_unhide:
            self.unhide_media_ids(ids)
        elif action == act_open and rows:
            self._on_photo_activated(rows[0])
        elif action == act_info and rows:
            self._show_file_info(rows[0])
        elif action == act_lock:
            self.locked.lock("manual")

    def hide_media_ids(self, ids):
        """Přesune položky do Skrytých (poprvé nejdřív nastavení hesla)."""
        ids = [int(i) for i in ids]
        if not ids or not self.locked.ensure_configured(self):
            return 0
        try:
            n = locked_folder.hide_items(ids, self.locked.vault)
        except locked_folder.VaultLocked:
            if not self.locked.request_unlock(self):        # bez klíče účtu Windows: heslem
                return 0
            n = locked_folder.hide_items(ids, self.locked.vault)
        except Exception as e:
            show_toast(self, tr("error-lock", e=e), "error")
            return 0
        self.grid_view.photo_model.ram_cache.discard(ids)    # nešifrované miniatury z RAM pryč
        self._embeddings_cache = {k: v for k, v in (self._embeddings_cache or {}).items() if k not in set(ids)}
        self._search_result = None
        self._on_library_changed()
        if n:
            show_toast(self, tr("locked-moved", items=_czech_item_count(n)), "success")
        return n

    def unhide_media_ids(self, ids):
        try:
            n = locked_folder.unhide_items(ids, self.locked.vault)
        except locked_folder.VaultLocked:
            show_toast(self, tr("locked-is-locked-unlock"), "error")
            return 0
        except Exception as e:
            show_toast(self, tr("error-unlock-return", e=e), "error")
            return 0
        self.grid_view.photo_model.ram_cache.discard(int(i) for i in ids)
        self._on_library_changed()
        if n:
            show_toast(self, tr("locked-returned", items=_czech_item_count(n)), "success")
        return n

    def _in_hidden_context(self):
        """Je vidět obsah Skrytých? Mřížka Skryté, prohlížeč nebo prezentace otevřené z ní."""
        if self._current_filter != "hidden":
            return False
        w = self.stack.currentWidget()
        if w is self.page_photos:
            return True
        return (self.nav.root() is self.page_photos and isinstance(w, (MediaViewerWidget, SlideshowView))
                and self._viewer_model is self.grid_view.photo_model)

    def _check_hidden_context(self, *_):
        # až po doběhnutí přepnutí: nav.reset() při vstupu do Skrytých krátce ukáže jinou stránku
        QTimer.singleShot(0, self._enforce_hidden_context)

    def _enforce_hidden_context(self):
        if self.locked.is_unlocked and not self._in_hidden_context():
            self.locked.lock("navigation")

    def _on_hidden_unlocked(self):
        try:
            self._hidden_records = fetch_media_records(hidden=True)
            self._vault_seen = {int(r[0]) for r in self._hidden_records}
        except Exception as e:
            print(f"[Skryté] {e}")
            self._hidden_records = []
        if self._current_filter == "hidden":
            self.grid_view.photo_model.vault = self.locked.vault
            self._apply_filter_and_sort()

    def _on_hidden_locked(self, reason):
        self._hidden_records = []
        self.grid_view.photo_model.purge_vault()
        viewer = self._viewer
        if self._current_filter == "hidden" and self.nav.depth() > 0 and self.nav.root() is self.page_photos:
            if viewer is not None:
                viewer.cleanup_playback()
            self.nav.reset()                        # prohlížeč / prezentaci skryté fotky zavřít
        if viewer is not None and viewer.media_list and self._viewer_model is self.grid_view.photo_model                 and any(r[0] in self._vault_seen for r in viewer.media_list):
            viewer.forget_media()                   # dekódovaný obraz ani seznam v paměti nezůstanou
        self._vault_seen = set()
        if self._current_filter == "hidden":
            self._apply_filter_and_sort()           # mřížka prázdná, stav „zamčeno“
            if reason in ("idle", "session", "minimize"):
                show_toast(self, tr("locked-did-lock"), "info")

    def _hidden_startup_sweep(self):
        try:
            locked_folder.startup_sweep(self.locked.vault)
        except Exception as e:
            print(f"[Skryté] úklid při startu: {e}")

    def _sync_hidden_entry(self):
        """Předvolby › Zobrazit Skryté v postranním panelu."""
        item = self.sidebar_nav.item("hidden")
        if item is not None:
            item.setVisible(bool(self.config.get("show_hidden_folder", True)))

    def open_hidden(self):
        self.nav.reset()
        self.sidebar_nav.set_current("hidden")
        self._on_sidebar("hidden")

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
        page.add_action(tr("action-rename"), lambda: self._rename_person(person_id, page.lbl_title.text(), page))
        page.add_action(tr("faces-borderline-ellipsis"),
                        lambda: self._open_borderline_for_person(person_id, page.lbl_title.text()),
                        tr("faces-borderline-action-tooltip"))
        grid = self._make_collection_grid(records, page)
        self._add_slideshow_action(page, grid)
        page.set_content(grid)
        self.nav.push(page)

    def _add_slideshow_action(self, page, grid):
        btn = page.add_action(tr("slideshow-play"), lambda: self.start_slideshow_from_grid(grid),
                              tr("slideshow-fullscreen-tooltip"))
        btn.setObjectName("btn_primary")
        btn.setEnabled(grid.photo_model.rowCount() > 0)
        return btn

    def _make_collection_grid(self, records, parent, album_id=None):
        grid = AppleGridView(self, parent)
        grid.set_cell_size(self.grid_view.cell_size)
        grid.photo_model.set_records(records)
        grid.item_activated.connect(lambda row, g=grid: self._on_collection_activated(g, row))
        grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        grid.customContextMenuRequested.connect(
            lambda pos, g=grid: self._collection_context_menu(g, pos, album_id))
        return grid

    def _on_collection_activated(self, grid, row):
        pick = getattr(grid, "_cover_pick_cb", None)     # Alba v2: režim výběru obálky
        if pick is not None:
            grid._cover_pick_cb = None
            pick(row)
            return
        self._open_viewer(grid.photo_model.records, row, grid.photo_model)

    # ── Sprint 2: Paleta příkazů ──────────────────────────────────────────────

    def _palette_search(self, query):
        def run():
            self.nav.reset()
            if self._current_filter == "trash":
                self._set_filter("all")
            self._switch_page(0)
            self.search_bar.setText(query)
            self._sync_sidebar()
        return Command(tr("palette-search-library", query=query), run, "search", tr("palette-smart-search-sub"),
                       "search")

    def _palette_commands(self):
        """Všechno, kam paleta umí skočit. Volá se při každém otevření (aktuální data)."""
        def nav(key):                            # jako klik do postranního panelu
            self.nav.reset()
            self.sidebar_nav.set_current(key)
            self._on_sidebar(key)
        cmds = [
            Command(tr("nav-library"), lambda: nav("photos"), "page", icon="photos", keywords=tr("palette-kw-library")),
            Command(tr("nav-people"), lambda: nav("people"), "page", icon="people", keywords=tr("palette-kw-people")),
            Command(tr("nav-events"), lambda: nav("events"), "page", icon="events", keywords=tr("palette-kw-events")),
            Command(tr("nav-places"), lambda: nav("map"), "page", icon="map", keywords=tr("palette-kw-places")),
            Command(tr("nav-favorites"), lambda: nav("favorites"), "page", icon="heart", keywords="srdce"),
            Command(tr("nav-videos"), lambda: nav("videos"), "page", icon="video", keywords=tr("palette-kw-videos")),
            Command(tr("nav-albums"), lambda: nav("albums"), "page", icon="album", keywords=tr("palette-kw-albums")),
            Command(tr("nav-trash"), lambda: nav("trash"), "page", icon="trash", keywords=tr("palette-kw-trash")),
        ] + ([Command(tr("nav-locked"), lambda: nav("hidden"), "page", icon="lock", keywords=tr("palette-kw-locked"))]
             if self.config.get("show_hidden_folder", True) else []) + [
            Command(tr("nav-echoes"), lambda: nav("echoes"), "page", icon="echo", keywords=tr("palette-kw-echoes")),
            Command(tr("nav-reels"), lambda: nav("reels"), "page", icon="reel",
                    keywords=tr("palette-kw-reels")),
            Command(tr("palette-import"), self._start_import, "action", icon="import", keywords=tr("palette-kw-import")),
            Command(tr("nav-settings"), self._open_settings, "action", icon="settings", keywords=tr("palette-kw-settings")),
            Command(tr("albums-new-ellipsis"), lambda: self._new_album(), "action", icon="plus", keywords=tr("palette-kw-new-album")),
            Command(tr("palette-help"), self.open_guide, "action", icon="info", keywords=tr("palette-kw-help")),
        ]
        if self._search_query.strip():
            cmds.append(Command(tr("palette-save-smart", value=self._search_query.strip()),
                                self.save_search_as_smart_album, "action", icon="sparkle",
                                keywords=tr("palette-kw-save-smart")))
        conn = get_connection()
        try:
            albums.ensure_tables(conn)
            for a in albums.list_albums(conn):
                sub_text = (tr("albums-search-label", query=a['query']) if a["kind"] == "smart"
                            else _czech_item_count(a["count"] or 0))
                cmds.append(Command(a["name"], lambda a=a: self._open_album(a), "album", sub_text,
                                    "sparkle" if a["kind"] == "smart" else "album"))
        finally:
            conn.close()
        for pid, (name, _cover, count) in sorted(getattr(self, "_people_rows", {}).items(),
                                                 key=lambda kv: -(kv[1][2] or 0)):
            # i18n-ok: DB default person name check
            if not name or name.lower().startswith(("neznámá osoba", "unknown")):
                continue
            cmds.append(Command(name, lambda pid=pid, name=name: self._palette_person(pid, name), "person",
                                _czech_photo_count(count or 0), "people"))
        for ev in getattr(self, "_event_cards", []):
            when = (ev.get("start") or "")[:10]
            cmds.append(Command(ev.get("title") or tr("event-default-title"), lambda eid=ev["id"]: self._palette_event(eid), "event",
                                " · ".join(x for x in (ev.get("location"), when) if x), "events",
                                keywords=f"{ev.get('location') or ''} {when[:4]}"))
        return cmds

    def _palette_person(self, person_id, name):
        self.nav.reset()
        self.sidebar_nav.set_current("people")
        self._on_sidebar("people")
        self._on_person_card_clicked(person_id, name)

    def _palette_event(self, event_id):
        self.nav.reset()
        self.sidebar_nav.set_current("events")
        self._on_sidebar("events")
        self._open_event(event_id)

    # ── Sprint 2: Alba ────────────────────────────────────────────────────────

    def _grid_ids(self, grid):
        recs = grid.photo_model.records
        return [int(recs[r][0]) for r in grid.selected_row_indices() if 0 <= r < len(recs)]

    def _collection_context_menu(self, grid, pos, album_id=None):
        ids = self._grid_ids(grid)
        if not ids:
            return
        menu = QMenu(self)
        act_fav = menu.addAction(tr("grid-toggle-favorite"))
        self._add_album_menu(menu, ids, exclude=album_id)
        act_export = menu.addAction(tr("action-export-ellipsis"))
        act_remove = act_cover = None
        if album_id is not None:
            menu.addSeparator()
            act_cover = menu.addAction(tr("albums-set-cover")) if len(ids) == 1 else None
            act_remove = menu.addAction(tr("albums-remove-from"))
        chosen = menu.exec(grid.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_fav:
            self.toggle_favorite_on_selection(grid)
        elif chosen == act_export:
            self.export_media_ids(ids)
        elif chosen == act_remove:
            self._album_edit(lambda conn: albums.remove_items(conn, album_id, ids))
            gone = set(ids)
            grid.photo_model.set_records([r for r in grid.photo_model.records if int(r[0]) not in gone])
            show_toast(self, tr("albums-removed-from", items=_czech_item_count(len(ids))), "success")
            self._refresh_albums_panel()
        elif chosen == act_cover:
            self._album_edit(lambda conn: albums.set_cover(conn, album_id, ids[0]))
            self._refresh_albums_panel()

    def _add_album_menu(self, menu, ids, exclude=None):
        """Podnabídka „Přidat do alba“: existující ruční alba (s cestou složek) + nové album z výběru."""
        sub = menu.addMenu(tr("albums-add-to"))
        conn = get_connection()
        try:
            albums.ensure_tables(conn)
            manual = [a for a in albums.list_albums(conn) if a["kind"] == "manual" and a["id"] != exclude]
        finally:
            conn.close()
        manual.sort(key=lambda a: a["path"].casefold())
        act_new = sub.addAction(tr("albums-new-ellipsis"))
        act_new.triggered.connect(lambda: self._new_album(ids))
        if manual:
            sub.addSeparator()
        for a in manual:
            act = sub.addAction(a["path"])
            act.triggered.connect(lambda _=False, a=a: self._add_to_album(a, ids))
        return sub

    def _album_edit(self, fn):
        try:
            with db_transaction() as conn:
                albums.ensure_tables(conn)
                result = fn(conn)
            return True if result is None else result
        except Exception as e:
            show_toast(self, tr("error-album-edit", e=e), "error")
            return None

    def _add_to_album(self, album, ids):
        added = self._album_edit(lambda conn: albums.add_items(conn, album["id"], ids))
        if added is None:
            return
        text = (tr("albums-added-to", name=album['name'], items=_czech_item_count(added)) if added
                else tr("albums-all-already-in", name=album['name']))
        show_toast(self, text, "success", action_text=tr("action-open"),
                   on_action=lambda: self._open_album_by_id(album["id"]))
        self._refresh_albums_panel()

    def _new_album(self, ids=(), parent_id=None):
        name, ok = InWindowDialog.getText(self, tr("albums-new"), tr("albums-name-prompt"), text="")
        if not ok or not name.strip():
            return None
        aid = self._album_edit(lambda conn: albums.create_album(conn, name, ids, parent_id))
        if aid is None:
            return None
        if ids:
            show_toast(self, tr("albums-created", name=name.strip(), items=_czech_item_count(len(ids))), "success",
                       action_text=tr("action-open"), on_action=lambda: self._open_album_by_id(aid))
        self._refresh_albums_panel()
        return aid

    def _new_folder(self, parent_id=None):
        name, ok = InWindowDialog.getText(self, tr("albums-new-folder"), tr("albums-folder-name-prompt"), text="", action_text=tr("action-create"))
        if not ok or not name.strip():
            return None
        fid = self._album_edit(lambda conn: albums.create_folder(conn, name, parent_id))
        if fid is not None:
            show_toast(self, tr("albums-folder-created", value=' '.join(name.split())), "success")
            self._refresh_albums_panel()
        return fid

    def save_search_as_smart_album(self):
        """Aktuální hledání (a kolekce) jako chytré album – samo se doplňuje novými fotkami."""
        query = self._search_query.strip()
        if not query:
            show_toast(self, tr("albums-smart-need-search"), "info")
            return None
        flt = self._current_filter if self._current_filter in albums.SMART_FILTERS else "all"
        name, ok = InWindowDialog.getText(self, tr("albums-kind-smart"), tr("albums-name-prompt"), text=query[:1].upper() + query[1:])
        if not ok or not name.strip():
            return None
        aid = self._album_edit(lambda conn: albums.create_smart_album(conn, name, query, flt))
        if aid is not None:
            show_toast(self, tr("albums-smart-saved", value=name.strip()), "success")
            self._refresh_albums_panel()
        return aid

    def _refresh_albums_panel(self):
        """Všechny otevřené úrovně Alb (kořen i podsložky v zásobníku stránek)."""
        alive = []
        for panel, page in getattr(self, "_albums_panels", []):
            try:
                n = panel.load()
                if panel.folder_id is None:
                    page.set_title(tr("nav-albums"), albums.czech_albums(n) if n else tr("albums-subtitle"))
                alive.append((panel, page))
            except RuntimeError:                 # stránka už zavřená (C++ objekt smazán)
                pass
        self._albums_panels = alive

    def open_albums(self, folder_id=None):
        """Postranní panel Kolekce › Alba: složky, ruční a chytrá alba (albums.py). folder_id = podsložka."""
        import albums_view
        name, subtitle = None, tr("albums-subtitle")
        if folder_id is None:
            self.nav.reset()
        else:
            conn = get_connection()
            try:
                albums.ensure_tables(conn)
                node = albums.get_album(conn, folder_id)
                crumbs = albums.ancestors(conn, folder_id)
            finally:
                conn.close()
            if not node:
                return
            name = node["name"]
            subtitle = " › ".join([tr("nav-albums")] + [n for _i, n in crumbs[:-1]])
        page = DetailPage(name or tr("nav-albums"), subtitle)
        page.setProperty("sidebar_key", "albums")
        panel = albums_view.AlbumsPanel(get_connection, page, folder_id=folder_id, edit=self._album_edit)
        panel.opened.connect(self._open_album)
        panel.rename_requested.connect(self._rename_album)
        panel.delete_requested.connect(self._delete_album)
        panel.export_requested.connect(self._export_album)
        panel.cover_requested.connect(lambda a: self._open_album(a, pick_cover=True))
        panel.cover_reset_requested.connect(self._reset_album_cover)
        panel.changed.connect(self._refresh_albums_panel)
        page.add_action(tr("albums-new-folder"), lambda: self._new_folder(folder_id), tr("albums-new-folder-tooltip"))
        page.add_action(tr("albums-new"), lambda: self._new_album(parent_id=folder_id))
        if folder_id is None:
            btn = page.add_action(tr("albums-save-search"), self.save_search_as_smart_album,
                                  tr("albums-save-search-tooltip"))
            btn.setEnabled(bool(self._search_query.strip()))
        else:
            page.add_action(tr("action-export"), lambda: self._export_album({"id": folder_id, "name": name}),
                            tr("albums-export-folder-tooltip"))
        page.set_content(panel)
        if folder_id is None:
            self._albums_panels = []
        self._albums_panels = getattr(self, "_albums_panels", []) + [(panel, page)]
        self.nav.push(page, on_pop=self._sync_sidebar)
        self._sync_sidebar()
        n = panel.load()
        if n and folder_id is None:
            page.set_title(tr("nav-albums"), albums.czech_albums(n))

    def _reset_album_cover(self, album):
        if self._album_edit(lambda conn: albums.set_cover(conn, album["id"], None)) is not None:
            self._refresh_albums_panel()

    def _open_album_by_id(self, album_id):
        conn = get_connection()
        try:
            albums.ensure_tables(conn)
            album = albums.get_album(conn, album_id)
        finally:
            conn.close()
        if album:
            self._open_album(album)

    def _open_album(self, album, pick_cover=False):
        if album["kind"] == "folder":
            self.open_albums(album["id"])
            return
        if album["kind"] == "smart":             # chytré album = hledání v mřížce Fotografie
            self.nav.reset()
            self._set_filter(album.get("filter") or "all")
            self._switch_page(0)
            self.search_bar.setText(album["query"])
            self._sync_sidebar()
            return
        import album_reorder
        conn = get_connection()
        try:
            ids = albums.item_ids(conn, album["id"])
            info = albums.get_album(conn, album["id"]) or album
        finally:
            conn.close()
        records = fetch_media_records(f"m.id IN ({','.join('?' * len(ids))})", tuple(ids)) if ids else []
        rank = {mid: i for i, mid in enumerate(ids)}
        records.sort(key=lambda r: rank.get(int(r[0]), 1 << 30))   # pořadí alba (datum / vlastní)
        page = DetailPage(album["name"], _czech_photo_count(len(records)))
        page.setProperty("sidebar_key", "albums")
        grid = self._make_collection_grid(records, page, album_id=album["id"])
        sort = SegmentedControl([(tr("albums-order-by-date"), "date"), (tr("albums-order-custom"), "custom")], current=info.get("sort_mode", "date"))
        sort.setToolTip(tr("albums-custom-order-tooltip"))
        sort.changed.connect(lambda mode: self._set_album_sort(album["id"], grid, mode))
        page.actions_layout.addWidget(sort)
        page.add_action(tr("albums-cover-ellipsis"), lambda: self._begin_cover_pick(album, grid), tr("albums-cover-tooltip"))
        page.add_action(tr("action-export"), lambda: self._export_album(album), tr("albums-export-tooltip"))
        page.add_action(tr("action-rename"), lambda: self._rename_album(album, page))

        def on_move(moved, before):
            order = self._album_edit(lambda conn: albums.move_items(conn, album["id"], moved, before))
            if not isinstance(order, list):
                return
            self._apply_album_order(grid, order)
            sel = grid.selectionModel()
            for mid in moved:
                row = grid.photo_model.id_to_row.get(int(mid))
                if row is not None:
                    sel.select(grid.photo_model.index(row, 0), sel.SelectionFlag.Select)
            sort.set_current("custom")
        grid._reorder = album_reorder.GridReorder(grid, on_move)
        self._add_slideshow_action(page, grid)
        page.set_content(grid)
        self.nav.push(page, on_pop=self._sync_sidebar)
        self._sync_sidebar()
        if pick_cover:
            self._begin_cover_pick(album, grid)

    @staticmethod
    def _apply_album_order(grid, order):
        pos = {mid: i for i, mid in enumerate(order)}
        grid.photo_model.set_records(sorted(grid.photo_model.records, key=lambda r: pos.get(int(r[0]), 1 << 30)))

    def _set_album_sort(self, album_id, grid, mode):
        if self._album_edit(lambda conn: albums.set_sort_mode(conn, album_id, mode)) is None:
            return
        conn = get_connection()
        try:
            order = albums.item_ids(conn, album_id)
        finally:
            conn.close()
        self._apply_album_order(grid, order)

    def _begin_cover_pick(self, album, grid):
        """Režim výběru obálky: další aktivace dlaždice (dvojklik / Enter) nastaví obálku místo otevření."""
        def pick(row):
            recs = grid.photo_model.records
            if not (0 <= row < len(recs)):
                return
            if self._album_edit(lambda conn: albums.set_cover(conn, album["id"], int(recs[row][0]))) is None:
                return
            self._refresh_albums_panel()
            show_toast(self, tr("albums-cover-set", name=album['name']), "success")
        grid._cover_pick_cb = pick
        show_toast(self, tr("albums-cover-pick-toast"), "info",
                   action_text=tr("action-cancel"), on_action=lambda: setattr(grid, "_cover_pick_cb", None))

    def _rename_album(self, album, page=None):
        name, ok = InWindowDialog.getText(self, tr("albums-rename"), tr("albums-new-name-prompt"), text=album["name"])
        if not ok or not name.strip():
            return
        if self._album_edit(lambda conn: albums.rename_album(conn, album["id"], name)) is None:
            return
        album["name"] = " ".join(name.split())
        if isinstance(page, DetailPage):
            page.set_title(album["name"])
        self._refresh_albums_panel()

    def _delete_album(self, album):
        if self._album_edit(lambda conn: albums.delete_album(conn, album["id"])) is None:
            return
        text = (tr("albums-folder-deleted", name=album['name']) if album.get("kind") == "folder"
                else tr("albums-album-deleted", name=album['name']))
        show_toast(self, text, "success")
        self._refresh_albums_panel()

    # ── Stage 4: čistý soukromý export ────────────────────────────────────────

    def export_media_ids(self, ids, title="Export"):
        """List s volbami soukromí → cíl → export ve vlákně (privacy_export, export_sheet)."""
        import export_sheet
        import privacy_export as pe
        job = getattr(self, "_privacy_export", None)
        if job is not None and job.isRunning():
            show_toast(self, tr("export-already-running"), "info", action_text=tr("action-cancel"), on_action=job.stop)
            return None
        conn = get_connection()
        try:
            items, skipped = pe.collect(conn, ids)
            names = pe.person_names(conn)
        finally:
            conn.close()
        if not items:
            show_toast(self, tr("export-error-nothing"), "info")
            return None
        opts = export_sheet.ask_options(self, len(items), export_sheet.options_from_config(self.config, title),
                                        pe.exiftool_available())
        if opts is None:
            return None
        self.config["export_options"] = export_sheet.options_to_config(opts)
        start_dir = self.config.get("export_last_dir") or os.path.join(os.path.expanduser("~"), "Pictures")
        base = pe.default_name(title)
        if opts.package == "zip":
            dest, _ = QFileDialog.getSaveFileName(self, tr("export-save-dialog"), os.path.join(start_dir, base + ".zip"),
                                                  tr("export-zip-filter"))
        else:
            parent = QFileDialog.getExistingDirectory(self, tr("export-pick-folder"), start_dir)
            dest = os.path.join(parent, base) if parent else ""
        if not dest:
            return None
        self.config["export_last_dir"] = os.path.dirname(dest)
        save_config(self.config)
        job = export_sheet.PrivacyExportJob(items, opts, dest, names, self)
        job.progress.connect(lambda f: self.lbl_status.set_task("privacy_export", tr("export-progress", value=fmt_number(f * 100, 0))))

        def done(res):
            self.lbl_status.set_task("privacy_export", "")
            for name, why in res.excluded + skipped:
                print(f"[Export] vynecháno: {name} – {why}")
            text = tr("export-done", value=res.summary())
            if res.excluded:
                text += " – " + tr("export-excluded-reason")
            show_toast(self, text, "success" if not res.excluded else "info", action_text=tr("action-show-in-folder"),
                       on_action=lambda: subprocess.Popen(["explorer", "/select,", os.path.normpath(res.path)]))

        def failed(err):
            self.lbl_status.set_task("privacy_export", "")
            show_toast(self, tr("export-failed", err=err), "error")

        def cancelled():
            self.lbl_status.set_task("privacy_export", "")
            show_toast(self, tr("export-cancelled-nothing"), "info")
        job.finished_ok.connect(done)
        job.failed.connect(failed)
        job.cancelled.connect(cancelled)
        self._privacy_export = job
        job.start()
        show_toast(self, tr("export-exporting", items=_czech_item_count(len(items))), "info", action_text=tr("action-cancel"),
                   on_action=job.stop)
        return job

    def _export_album(self, album):
        conn = get_connection()
        try:
            albums.ensure_tables(conn)
            ids = albums.export_item_ids(conn, album["id"])
        finally:
            conn.close()
        self.export_media_ids(ids, album["name"])

    def _rename_person(self, person_id: int, current_name: str, page=None):
        new_name, ok = InWindowDialog.getText(
            self, tr("people-rename"), tr("people-new-name-prompt"), text=current_name)
        if ok and new_name.strip():
            try:
                from database import db_transaction
                with db_transaction() as conn:
                    conn.execute("UPDATE people SET name=? WHERE id=?",
                                 (new_name.strip(), person_id))
                confirm_person(person_id)    # přejmenovaná osoba = kotva, přeshlukování ji nesmaže
                xmp_sync.touch_person(person_id)   # nové jméno do sidecarů všech jejích fotek
                self._load_people()
                if isinstance(page, DetailPage):
                    page.set_title(new_name.strip())
            except Exception as e:
                show_toast(self, tr("error-person-rename", e=e), "error")

    def _recluster_faces(self):
        threshold = float(self.config.get("face_similarity_threshold", 0.62))
        self.lbl_status.setText(tr("people-regrouping"))
        QCoreApplication.processEvents()
        try:
            stats = strict_recluster_faces(threshold=threshold,
                                           merge_threshold=self.config.get("face_merge_threshold"))
            invalidate_centroid_cache()
            self._load_people()
            if stats:
                persons = int(stats.get("anchors", 0)) + int(stats.get("new_people", 0))
                show_toast(self, tr("people-regroup-done", people=persons, faces=stats.get('faces', 0)), "success")
            else:
                show_toast(self, tr("people-regroup-none"), "info")
        except Exception as e:
            show_toast(self, tr("people-regroup-failed", e=e), "error")
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
            show_toast(self, tr("error-people-load", e=e), "error")
            return
        if not persons:
            show_toast(self, tr("faces-borderline-need-named"), "info")
            return
        # První osoba, která má hraniční obličeje
        for pid, pname in persons:
            faces = get_borderline_faces_for_person(pid)
            if faces:
                self._push_borderline_review(pid, pname, faces)
                return
        show_toast(self, tr("faces-borderline-none"), "info")

    def _open_borderline_for_person(self, person_id: int, name: str):
        faces = get_borderline_faces_for_person(person_id)
        if not faces:
            show_toast(self, tr("faces-borderline-none-for", name=name), "info")
            return
        self._push_borderline_review(person_id, name, faces)

    def _push_borderline_review(self, person_id: int, name: str, faces):
        view = BorderlineReviewView(person_id, name, faces)
        page = DetailPage(tr("faces-borderline"), name)
        page.set_content(view)
        view.finished_review.connect(page.back_requested.emit)
        self.nav.push(page, on_pop=lambda v=view: self._on_borderline_done(v))
        view.setFocus(Qt.FocusReason.OtherFocusReason)
        return view

    def _on_borderline_done(self, view):
        if view.assigned:
            self._load_people()
            show_toast(self, tr("faces-confirmed-count", person_name=view.person_name, assigned=view.assigned), "success")

    def _open_duplicate_review(self):
        self.lbl_status.setText(tr("duplicates-searching"))
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
            show_toast(self, tr("error-duplicates", e=e), "error")
            self.lbl_status.setText("")
            return
        self.lbl_status.setText("")
        if not pairs:
            show_toast(self, tr("duplicates-none-photos"), "success")
            return
        self._push_duplicate_review(pairs)

    def _push_duplicate_review(self, pairs):
        view = DuplicateReviewView(pairs)
        n = len(view.pairs)
        page = DetailPage(tr("people-duplicates"), tr("count-possible-pairs", count=n))
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
            show_toast(self, tr("trash-moved", items=_czech_item_count(trashed)), "success",
                       action_text=tr("trash-show"), on_action=self._show_trash)

    def _on_phash_progress(self, i: int, n: int):
        if i == n or i % 25 == 0:
            self.lbl_status.setText(tr("status-fingerprints-short", done=i, total=n))
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
            show_toast(self, tr("events-no-visible-items"), "info")
            return
        title = split_event_title((event or {}).get("title") or tr("event-default-title"))[1]
        start, end = (event or {}).get("start"), (event or {}).get("end")
        location = (event or {}).get("location")
        page = DetailPage(title, " · ".join(filter(None, [location, _format_date_range(start, end),
                                                          _czech_item_count(len(records))])))
        page.set_ambient(self._event_glow(event))               # Polish P4: odstín obálky v hlavičce
        grid = self._make_collection_grid(records, page)
        if (event or {}).get("journey"):
            page.add_action(tr("journey"), lambda: self._request_journey(event_id), tr("journey-play-event-tooltip"))
        self._add_slideshow_action(page, grid)
        page.set_content(grid)
        self.nav.push(page)
        self._warm_event_region(event_id)          # dlaždice oblasti události dopředu (tile store)

    # ── Ozvěny (tehdy a teď) ──────────────────────────────────────────────────

    def open_echoes(self):
        """Postranní panel Pro vás › Ozvěny: místa, kam se uživatel vrátil po letech (echoes.py).
        Podstránka nad aktuální stránkou; hledání běží na pozadí (EchoJob)."""
        import echoes_view
        self.nav.reset()
        page = DetailPage(tr("nav-echoes"), tr("echoes-subtitle"))
        page.setProperty("sidebar_key", "echoes")
        panel = echoes_view.EchoesPanel(get_connection, page)
        panel.opened.connect(self._open_echo)
        panel.loaded.connect(lambda n: page.set_title(
            tr("nav-echoes"), tr("echoes-places-returned", count=n)
            if n else tr("echoes-subtitle")))
        page.set_content(panel)
        self._echoes_panel = panel
        self.nav.push(page, on_pop=self._sync_sidebar)
        self._sync_sidebar()
        panel.load()

    def _open_echo(self, echo):
        import echoes
        import echoes_view
        first, last = echo["items"][0], echo["items"][-1]
        page = DetailPage(echo.get("place") or tr("echoes-echo"), echoes.subtitle(echo))
        page.setProperty("sidebar_key", "echoes")
        view = echoes_view.ThenNowView(echo, page)
        view.open_photo.connect(lambda mid: self._open_echo_photo(echo, mid))     # dvojklik na polovinu
        page.add_action(tr("echoes-then"), lambda: self._open_echo_photo(echo, echo["items"][view.then_i]["id"]),
                        tr("echoes-shot-in-viewer", year=first['year']))
        page.add_action(tr("echoes-now"), lambda: self._open_echo_photo(echo, last["id"]),
                        tr("echoes-shot-in-viewer", year=last['year']))
        page.set_content(view)
        self.nav.push(page)
        view.setFocus()

    # ── Throwback Reels (Sprint 4) ────────────────────────────────────────────

    def open_reels(self):
        """Pro vás › Sestřihy: návrhy filmových sestřihů (reels.py) – výpočet na pozadí."""
        import reels_view
        self.nav.reset()
        page = DetailPage(tr("nav-reels"), tr("reels-subtitle"))
        page.setProperty("sidebar_key", "reels")
        panel = reels_view.ReelsPanel(get_connection, page)
        panel.opened.connect(self._open_reel)
        panel.loaded.connect(lambda n: page.set_title(
            tr("nav-reels"), tr("reels-ready", count=n)
            if n else tr("reels-subtitle")))
        page.set_content(panel)
        self._reels_panel = panel
        self.nav.push(page, on_pop=self._sync_sidebar)
        self._sync_sidebar()
        panel.load()

    def _open_reel(self, suggestion):
        import reels_view
        track = reels_view._music_for(suggestion["key"], ambient_music.tracks())
        page = DetailPage(suggestion["title"], suggestion.get("subtitle", ""))
        page.setProperty("sidebar_key", "reels")
        body = reels_view.ReelPage(page)
        page.set_content(body)
        btn_play = page.add_action(tr("reels-pause"), lambda: body.player and body.player.toggle(), tr("viewer-play-pause"))
        btn_export = page.add_action(tr("reels-export-mp4"), lambda: self._export_reel(body), tr("reels-export-mp4-tooltip"))
        btn_export.setEnabled(False)

        def sync_play():
            if body.player is not None:
                btn_play.setText(tr("reels-pause") if body.player.playing else tr("guide-play"))

        def ready(renderer):
            body.set_renderer(renderer)
            btn_export.setEnabled(renderer is not None)
            if renderer is not None:
                plan = renderer.plan
                music = (plan.get("music") or {}).get("title")
                page.set_title(suggestion["title"], " · ".join(x for x in (
                    suggestion.get("subtitle", ""), tr("count-shots", count=plan['shots']),
                    tr("unit-seconds", value=fmt_number(plan['total'], 0)),
                    tr("reels-music", music=music) if music else "") if x))
                body.player.ended.connect(sync_play)
                timer = QTimer(page)
                timer.timeout.connect(sync_play)
                timer.start(300)
        job = reels_view.BuildJob(get_connection, suggestion, track)
        job.signals.done.connect(ready)
        self._reel_job = job
        QThreadPool.globalInstance().start(job)
        self.nav.push(page, on_pop=lambda: body.player and body.player.stop())
        self._sync_sidebar()

    def _export_reel(self, body):
        import reel_export
        import reels_view
        if body.player is None:
            return
        plan = body.player.plan
        body.player.pause()
        start = os.path.join(os.path.expanduser("~"), "Videos", reel_export.default_name(plan))
        path, _ = QFileDialog.getSaveFileName(self, tr("reels-save-dialog"), start, tr("reels-mp4-filter"))
        if not path:
            return
        job = reels_view.ExportJob(plan, path, self)
        job.progress.connect(lambda f: self.lbl_status.set_task("reel_export", tr("reels-exporting", value=fmt_number(f * 100, 0))))

        def done(out, stats):
            self.lbl_status.set_task("reel_export", "")
            print(f"[Reels] export {stats}")
            show_toast(self, tr("reels-saved", mb=fmt_number(stats['mb'], 0)), "success", action_text=tr("action-show-in-folder"),
                       on_action=lambda: subprocess.Popen(["explorer", "/select,", os.path.normpath(out)]))

        def failed(err):
            self.lbl_status.set_task("reel_export", "")
            show_toast(self, tr("export-failed", err=err), "error")
        job.finished_ok.connect(done)
        job.failed.connect(failed)
        self._reel_export = job
        job.start()

    def _open_echo_photo(self, echo, media_id):
        ids = [it["id"] for it in echo["items"]]
        try:
            records = fetch_media_records(f"m.id IN ({','.join('?' * len(ids))})", tuple(ids),
                                          order_by="m.date_taken, m.id")
        except Exception as e:
            show_toast(self, tr("error-open-photos", e=e), "error")
            return
        index = next((i for i, r in enumerate(records) if int(r[0]) == int(media_id)), 0)
        self._open_viewer(records, index)

    # ── Import ────────────────────────────────────────────────────────────────

    def _start_import(self):
        folder = QFileDialog.getExistingDirectory(self, tr("import-pick-folder"))
        if not folder:
            return
        self.config["library_folders"] = list(set(
            self.config.get("library_folders", []) + [folder]
        ))
        save_config(self.config)
        self._run_scan(folder)

    def _run_scan(self, folder: str):
        if self._scanner and self._scanner.isRunning():
            show_toast(self, tr("import-already-running"), "info")
            return
        self.nav.reset()
        self.stack.setCurrentIndex(4)
        self._scan_start_time = time.monotonic()
        self.progress_widget.set_progress(0, 1)
        self.progress_widget.set_status(tr("import-starting"))

        self._scanner = MediaScannerThread(folder)
        self._scanner.cloud_skipped_signal.connect(self._on_cloud_skipped)
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
            show_toast(self, tr("import-done", items=_czech_item_count(processed)), "success")
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
        old_precise = self._precise_wanted()
        old_color = self.config.get("color_output", "system")
        old_language = self.config.get("language", i18n.SYSTEM)
        old_prefetch = bool(self.config.get("map_prefetch", True))
        dlg = SettingsDialog(self, as_sheet=True)     # Founder audit: list v okně, ne okno Win32
        accepted = dlg.exec()
        self._install_sync_bridge()            # iOS Phase 2: the iPhone switch acts live in Settings
        if dlg.trash_emptied:                  # proběhlo i při Zrušit – knihovna musí sedět s DB
            self._on_library_changed()
        if getattr(dlg, "hidden_requested", False) is True:   # Předvolby › Skryté › Otevřít
            QTimer.singleShot(0, self.open_hidden)
        if getattr(dlg, "guide_requested", False) is True:   # Předvolby › Nápověda › Tipy a průvodce
            QTimer.singleShot(0, self.open_guide)
        if not accepted:
            return
        self.config = load_config()
        if self.config.get("language", i18n.SYSTEM) != old_language:
            self.apply_language(self.config.get("language", i18n.SYSTEM))    # Gate 1: bez restartu
        motion.configure(self.config)       # Omezit animace (Předvolby)
        self._sync_hidden_entry()
        if bool(self.config.get("adaptive_accent", True)) != tokens.adaptive():
            tokens.set_adaptive(self.config.get("adaptive_accent", True))    # Přizpůsobit obsahu
            for card in getattr(self, "event_cards", []):
                card.update()
            self.memory_card.update()
        if self.config.get("color_output", "system") != old_color:
            configure_display_pipeline(QApplication.instance(), self.config)   # démon přegeneruje cache
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
        srv = tile_store.service()            # Sprint 3 item 3: map cache budget applies now
        if srv is not None:
            srv.store.set_budget(int(self.config.get("tile_store_mb", tile_store.DEFAULT_BUDGET_MB)))
        if self.config.get("map_prefetch", True) and not old_prefetch:
            self._start_photo_precache()      # switched on: keep photo places offline now
        self._apply_precise_setting(old_precise)     # Sprint 4.0: přesné hledání zapnuto / vypnuto
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
        show_toast(self, tr("memories-dismissed"), "info")

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
        map_precache.jobs().cancel()         # offline map job: workers stop before the store does
        self.locked.shutdown()               # klíč Skrytých pryč z paměti, štít dolů
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
        router = getattr(self, "_corridor_router", None)      # Gate 2 C: corridor routing thread
        if router is not None and router.isRunning():
            router.stop()
            if not router.wait(3000):
                park_thread(router)
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
        bg = getattr(self, "_bg", None)              # 120 Hz pass: background library reads
        if bg is not None:
            bg.pool.clear()
            bg.pool.waitForDone(3000)
        if isinstance(getattr(self, "map_view", None), map_host.WebView2Host):
            self.map_view.close_webview()       # WebView2 browser process ends with its controller
        super().closeEvent(event)


# ── Entry point ───────────────────────────────────────────────────────────────

def configure_display_pipeline(app, cfg):
    """Stage 2: výstupní barevný profil (color_manage) a velikost náhledů podle displeje.
    Volat z GUI vlákna před prvním náhledem / démonem."""
    import color_manage
    try:
        tgt = color_manage.configure(cfg.get("color_output", color_manage.MODE_SYSTEM))
        print(f"[Barvy] výstup: {tgt['name']} ({tgt['reason']})")
    except Exception as e:
        print(f"[Barvy] {e}")
    screen = app.primaryScreen() if app is not None else None
    if screen is not None:
        dpr = screen.devicePixelRatio()
        size = screen.size()
        preview_cache.configure_for_display(round(max(size.width(), size.height()) * dpr))


def main(app=None, splash=None):
    """Hlavní okno. app/splash předává splash.boot(); bez nich (KAIROS_NO_SPLASH) se vytvoří tady."""
    import crash_guard
    import paths as _paths
    crash_guard.install(_paths.LOG_DIR)          # idempotent; also covers splash.boot() / embedding
    if app is None:
        web_engine.prepare_app()                 # QtWebEngine may then be imported later (lazily)
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
    i18n.init_from_config(cfg)            # Gate 1: jazyk dřív, než vznikne první widget
    tokens.configure(cfg)
    motion.configure(cfg)
    configure_display_pipeline(app, cfg)
    theme.apply(cfg.get("theme", theme.DEFAULT_THEME))

    boot_trace.mark("main_ready")
    window = MainWindow()
    boot_trace.mark("window_built")
    window_effects.style_for_theme(window)    # titulkový pruh v barvě aplikace (Windows 11)
    grid = getattr(window, "grid_view", None)
    reveal = splash is not None and not motion.reduced()
    # Pillar 5 direct launch: no splash, the window stays transparent until the grid's first
    # finished frame (no blank-tile flash) and then appears at once; REVEAL_FALLBACK_MS caps it.
    direct = splash is None and grid is not None and grid.isVisibleTo(window)
    if reveal or direct:
        # Founder audit: okno se ukáže neviditelné (žádné šedé probliknutí prázdné mřížky),
        # splash drží obraz do prvního hotového snímku mřížky a pak se oba prolnou (250 ms).
        window.setWindowOpacity(0.0)
    window.show()
    boot_trace.mark("window_shown")
    if direct:
        def _appear():
            if window.windowOpacity() < 1.0:
                window.setWindowOpacity(1.0)
        grid.firstFrameRendered.connect(_appear)
        QTimer.singleShot(REVEAL_FALLBACK_MS, _appear)
    if grid is not None:
        grid.firstFrameRendered.connect(lambda: boot_trace.finish(app))
        grid.firstFrameRendered.connect(window.start_deferred_services)
    if splash is not None:
        if reveal and grid is not None and grid.isVisible():
            splash.hand_off(window, grid.firstFrameRendered)
        else:
            splash.finish(window)              # úvod jinde než v mřížce / omezený pohyb
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
