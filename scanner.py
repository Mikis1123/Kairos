from i18n import tr
import io
import os
import sys
import subprocess
import json
import math
import re
import time
import traceback
from pathlib import Path
from datetime import datetime
from collections import Counter
from PyQt6.QtCore import QThread, pyqtSignal
from database import get_connection, db_transaction, upsert_media_item
import cloud_files
import media_identity
import trips
import reverse_geocoder as rg
from PIL import Image, ImageOps
import color_manage
import fs_atomic
import imaging

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass
import raw_decode  # noqa: E402  ProRAW / camera RAW through every Image.open (raw_decode.py)
raw_decode.register()

try:
    from mutagen.mp4 import MP4
except ImportError:
    MP4 = None

from paths import (
    BASE_DIR,
    THUMB_DIR as CACHE_DIR,
    EXIFTOOL_PATH as DEFAULT_EXIFTOOL,
    FFMPEG_PATH as DEFAULT_FFMPEG,
)

SUPPORTED_EXTS = {'.heic', '.jpg', '.jpeg', '.png', '.mov', '.mp4', '.dng'}
# Stage 2: miniatura = kratší strana 840 px (= 2 × GRID_MAX_CELL při DPR 2). Dřív 360 px na
# DELŠÍ straně -> čtvercová dlaždice nad ~135 logických px se na HiDPI zvětšovala (rozmazaná
# mřížka). Delší strana nejvýš 1680 px (panoramata). Paměť hlídá mřížka: do RAM si bere jen
# tolik pixelů, kolik dlaždice potřebuje (main.thumb_tier).
THUMB_SHORT_EDGE = 840
THUMB_LONG_MAX = 1680
THUMB_QUALITY = 88               # 4:4:4 (imaging.encode_jpeg) – čisté sytě barevné hrany i na velké dlaždici
THUMB_VERSION = 3               # 2 = kratší strana 840 px + převod do profilu displeje (color_manage)
                                # 3 = imaging pipeline: Lanczos3, ostření jasu, 4:4:4, kontrakt Windows
THUMB_VERSION_FILE = "THUMB_VERSION"
MTIME_TOLERANCE_SEC = 2.0   # FAT/exFAT ukládá mtime s přesností 2 s


def haversine_distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return 0.0
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def _path_key(path):
    """Klíč pro porovnání cest: na Windows nezáleží na velikosti písmen ani na lomítkách."""
    return os.path.normcase(os.path.normpath(path))


def _hidden_startupinfo():
    if os.name == 'nt':
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return info
    return None


def _clean_exif_date(raw):
    """Vrátí 'YYYY-MM-DD HH:MM:SS', nebo '' pro prázdné / neplatné hodnoty
    (typicky '0000:00:00 00:00:00' z fotoaparátů bez nastavených hodin)."""
    s = str(raw or "").strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4] == ':' and s[7] == ':':
        s = s.replace(":", "-", 2)
    s = s.replace("T", " ").replace("Z", "")[:19]
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            dt = datetime.strptime(s[:length], fmt)
        except ValueError:
            continue
        if dt.year < 1971:
            return ""
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return ""


# ==============================================================================
# MINIATURY (sdílené skenerem i obnovou po vyčištění mezipaměti)
# ==============================================================================

def _thumb_box(w, h):
    """Rámeček pro Image.thumbnail(): kratší strana THUMB_SHORT_EDGE, delší nejvýš THUMB_LONG_MAX."""
    w, h = max(1, int(w)), max(1, int(h))
    scale = min(1.0, THUMB_SHORT_EDGE / min(w, h), THUMB_LONG_MAX / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def _video_thumb_filter(file_path, ffmpeg_path):
    """Zmenšení snímku videa na kratší stranu miniatury + HDR -> SDR (HLG / Dolby Vision z iPhonu
    by jinak vypadaly vybledle)."""
    s = THUMB_SHORT_EDGE
    vf = f"scale='if(gt(iw,ih),-2,min({s},iw))':'if(gt(iw,ih),min({s},ih),-2)':flags=lanczos+accurate_rnd"
    try:
        import video_proxy
        sdr = video_proxy.sdr_filter(video_proxy.probe(file_path, ffmpeg_path))
    except Exception:
        sdr = ""
    return vf + ("," + sdr if sdr else "") + ",format=yuvj444p"


def _render_still_thumb(file_path):
    """Miniatura fotky přes imaging: dekódování (JPEG v DCT na ≥ 2×), Lanczos3 na kratší stranu
    THUMB_SHORT_EDGE, ostření jasu pro obrazovku, jediný převod do kontraktu displeje."""
    return imaging.render_still(
        file_path, lambda w, h: imaging.fit_short_edge(w, h, THUMB_SHORT_EDGE, THUMB_LONG_MAX),
        sharpen=imaging.SHARPEN_THUMB, need_long=THUMB_LONG_MAX)


def make_thumbnail(file_path, thumb_path, m_type, paired_video, ffmpeg_path=DEFAULT_FFMPEG):
    """Vytvoří miniaturu (kratší strana THUMB_SHORT_EDGE, sRGB). Zápis je atomický. Vrací True při úspěchu."""
    startupinfo = _hidden_startupinfo()
    tmp_path = fs_atomic.unique_tmp(thumb_path)
    is_video_only = (m_type == 'video' and not paired_video)

    def _ffmpeg(extra_before_input):
        if not os.path.exists(ffmpeg_path):
            return
        cmd = [ffmpeg_path, "-loglevel", "quiet", "-y"] + extra_before_input + [
            "-i", file_path, "-map", "0:v:0", "-frames:v", "1",
            "-vf", _video_thumb_filter(file_path, ffmpeg_path),
            "-q:v", "2", tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, startupinfo=startupinfo)

    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if not is_video_only:
            try:
                rgb = _render_still_thumb(file_path)
                with open(tmp_path, "wb") as f:
                    f.write(imaging.encode_jpeg(rgb, THUMB_QUALITY))
            except Exception:
                _ffmpeg([])
        else:
            _ffmpeg(["-ss", "00:00:00.200"])

        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            # cíl může krátce držet čtečka mřížky / antivir (WinError 5) -> retry s backoffem
            return fs_atomic.replace(tmp_path, thumb_path)
    except Exception as e:
        print(f"Chyba miniatury {file_path}: {e}")
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass
    return False


def render_thumbnail_bytes(file_path, m_type, paired_video, ffmpeg_path=DEFAULT_FFMPEG):
    """Stejná miniatura jako make_thumbnail(), ale jen v paměti (JPEG bajty, nebo None) – pro
    složku Skryté, kde nešifrovaná miniatura nesmí na disk ani dočasně."""
    if not file_path or not os.path.exists(file_path):
        return None
    if not (m_type == 'video' and not paired_video):
        try:
            return imaging.encode_jpeg(_render_still_thumb(file_path), THUMB_QUALITY)
        except Exception:
            pass
    if not os.path.exists(ffmpeg_path):
        return None
    seek = ["-ss", "00:00:00.200"] if m_type == 'video' and not paired_video else []
    cmd = [ffmpeg_path, "-loglevel", "quiet"] + seek + [
        "-i", file_path, "-map", "0:v:0", "-frames:v", "1",
        "-vf", _video_thumb_filter(file_path, ffmpeg_path),
        "-q:v", "2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    try:
        out = subprocess.run(cmd, capture_output=True, startupinfo=_hidden_startupinfo(), timeout=60).stdout
    except Exception:
        return None
    return out or None


def _thumb_version_path():
    return os.path.join(CACHE_DIR, THUMB_VERSION_FILE)


def thumb_version_tag():
    """Verze miniatur = algoritmus + otisk výstupního profilu (jiný displej = jiné barvy)."""
    return f"{THUMB_VERSION}:{color_manage.target_fingerprint()}"


def thumb_version_state():
    """(verze, od_kdy) z THUMB_VERSION v cache miniatur. „2:abcd“ = hotovo; „2:abcd pending <t>“
    = převod běží od času t (miniatury starší než t jsou ještě staré verze)."""
    try:
        with open(_thumb_version_path(), encoding="ascii") as f:
            parts = f.read().split()
        since = float(parts[2]) if len(parts) >= 3 and parts[1] == "pending" else None
        return parts[0], since
    except (OSError, ValueError, IndexError):
        return None, None


def _write_thumb_version(text):
    try:
        with open(_thumb_version_path(), "w", encoding="ascii") as f:
            f.write(text)
    except OSError:
        pass


def repair_missing_thumbnails(should_stop=None, progress=None, flush_every=50):
    """Doplní miniatury, které chybí v databázi (NULL) nebo na disku (smazaná cache),
    a převede miniatury starší verze (THUMB_VERSION) – po kouskách, přerušitelně: miniatura
    novější než začátek převodu už je nová; stará zůstává vidět, dokud ji nová nenahradí.

    thumbnail_path == '' znamená "už se to zkoušelo a nešlo" - takové soubory se
    znovu nezkouší, dokud se nezmění (UPSERT při změně velikosti vrátí NULL).
    Vrací počet vytvořených miniatur.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    tag = thumb_version_tag()
    ver, since = thumb_version_state()
    upgrading = ver != tag or since is not None
    if upgrading and (ver != tag or since is None):
        since = time.time()
        _write_thumb_version(f"{tag} pending {since}")
    cache_key = os.path.normcase(os.path.abspath(CACHE_DIR))
    conn = get_connection()
    created = 0
    stopped = False
    try:
        c = conn.cursor()
        # skryté (Stage 3) ne: jejich miniatura existuje jen šifrovaně (locked_folder)
        c.execute("SELECT id, file_path, media_type, paired_video_path, thumbnail_path FROM media_items "
                  "WHERE COALESCE(is_hidden, 0) = 0")
        todo = []
        for item_id, fp, m_type, paired, thumb in c.fetchall():
            if thumb == "":
                continue
            if thumb is None or not os.path.exists(thumb):
                todo.append((item_id, fp, m_type, paired, False))
            elif upgrading and os.path.normcase(os.path.dirname(os.path.abspath(thumb))) == cache_key:
                try:
                    stale = os.path.getmtime(thumb) < since
                except OSError:
                    stale = False
                if stale:
                    todo.append((item_id, fp, m_type, paired, True))

        total = len(todo)
        updates = []

        def flush():
            if updates:
                c.executemany("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", updates)
                conn.commit()
                updates.clear()

        for i, (item_id, fp, m_type, paired, upgrade) in enumerate(todo):
            if should_stop and should_stop():
                stopped = True
                break
            if not cloud_files.local_ok(fp):
                continue  # zdroj chybí (odpojený disk?) / jen v cloudu - nic neznačit, zkusí se příště
            thumb_path = os.path.join(CACHE_DIR, f"thumb_{item_id}.jpg")
            if make_thumbnail(fp, thumb_path, m_type, paired):
                updates.append((thumb_path, item_id))
                created += 1
            elif not upgrade:
                updates.append(("", item_id))  # trvale nečitelné (stará miniatura při převodu zůstává)
            if len(updates) >= flush_every:
                flush()
            if progress:
                progress(i + 1, total)
        flush()
    finally:
        conn.close()
    if upgrading and not stopped:
        _write_thumb_version(tag)
    return created


def with_live_partners(files):
    """Podporované soubory + jejich partneři se stejným názvem ve stejné složce (IMG_1.HEIC
    přetažená bez IMG_1.MOV by přišla o pohyb Live Photo). Pořadí zachová, bez duplicit."""
    out, seen = [], set()

    def add(p):
        key = _path_key(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    for p in files:
        if not p or Path(p).suffix.lower() not in SUPPORTED_EXTS or not os.path.isfile(p):
            continue
        add(os.path.normpath(p))
        folder, stem = os.path.dirname(p), Path(p).stem.lower()
        try:
            siblings = os.listdir(folder)
        except OSError:
            continue
        for name in siblings:
            if Path(name).stem.lower() == stem and Path(name).suffix.lower() in SUPPORTED_EXTS:
                add(os.path.normpath(os.path.join(folder, name)))
    return out


class MediaScannerThread(QThread):
    progress_signal = pyqtSignal(int, int)
    status_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(int)
    # Gate 0: soubory jen v cloudu (OneDrive / iCloud), které sken přeskočil – (počet, bajty, cesty)
    cloud_skipped_signal = pyqtSignal(int, float, list)

    def __init__(self, target_folder, exiftool_path=None, batch_size=50, files=None, allow_cloud=False):
        """target_folder = složka (rekurzivně), nebo None a files = seznam souborů (přetažené
        soubory: indexují se na místě, nic se nekopíruje). allow_cloud = uživatel výslovně
        souhlasil se stažením souborů „jen online“ (jinak se přeskočí a nahlásí)."""
        super().__init__()
        self.target_folder = target_folder
        self.files = list(files) if files is not None else None
        self.allow_cloud = bool(allow_cloud)
        self.cloud_files = []            # [(cesta, velikost)] přeskočené zástupné soubory
        self.reconnected = 0             # Gate 0: přesunuté soubory znovupřipojené k položkám
        self.exiftool_path = exiftool_path if (exiftool_path and os.path.exists(exiftool_path)) else DEFAULT_EXIFTOOL
        self.ffmpeg_path = DEFAULT_FFMPEG
        self.batch_size = batch_size
        self._is_running = True
        self._last_progress_emit = 0.0
        os.makedirs(CACHE_DIR, exist_ok=True)

    def stop(self):
        self._is_running = False

    def _emit_progress(self, value, force=False):
        """Omezí frekvenci signálů do UI (max ~7x za sekundu)."""
        now = time.monotonic()
        if force or now - self._last_progress_emit >= 0.15:
            self._last_progress_emit = now
            self.progress_signal.emit(int(value), 100)

    def run(self):
        processed = 0
        try:
            processed = self._run_pipeline()
        except Exception as e:
            print(f"Kritická chyba skeneru: {e}")
            traceback.print_exc()
        # Při zrušení uživatelem se finished_signal nevysílá (řeší to UI samo);
        # při výjimce ano, aby UI nezůstalo viset v importním overlayi.
        if self._is_running:
            self.finished_signal.emit(processed)

    # ------------------------------------------------------------------
    def _collect_files(self):
        """Podporované soubory. Zástupné soubory cloudu (atributy z výpisu složky – nic se
        neotevírá) jdou bez souhlasu do self.cloud_files místo ke zpracování."""
        self.cloud_files = []
        if self.files is not None:
            out = []
            for p in with_live_partners(self.files):
                if not self.allow_cloud and cloud_files.is_placeholder(p):
                    try:
                        self.cloud_files.append((p, os.stat(p).st_size))
                    except OSError:
                        pass
                    continue
                out.append(p)
            return out
        all_files = []
        for root, dirs, files in os.walk(self.target_folder):
            if not self._is_running:
                return None
            for f in files:
                if Path(f).suffix.lower() in SUPPORTED_EXTS:
                    all_files.append(os.path.join(root, f))
        if self.allow_cloud:
            return all_files
        out = []
        for p in all_files:
            try:
                st = os.stat(p)            # metadata (atributy) – data souboru se nestahují
            except OSError:
                continue
            if cloud_files.stat_is_placeholder(st):
                self.cloud_files.append((p, st.st_size))
            else:
                out.append(p)
        return out

    def _load_known_media(self):
        """path_key -> (uložená cesta, velikost, mtime) pro všechna média v DB."""
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT file_path, file_size, file_mtime FROM media_items")
            return {_path_key(p): (p, s, m) for p, s, m in c.fetchall() if p}
        finally:
            conn.close()

    def _classify(self, all_files, known, missing=None):
        """Rozdělí soubory na nové/změněné (ke zpracování) a beze změny (přeskočit).
        Prvky to_process: (skutečná cesta, cesta v DB, velikost, mtime[, kandidáti, otisk]).
        missing = media_identity.MissingIndex (Gate 0): nový soubor s otiskem chybějící
        položky dostane kandidáty – _insert_item ho po ověření data EXIF připojí k ní."""
        to_process, backfill, skipped = [], [], 0
        for p in all_files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime
            row = known.get(_path_key(p))
            if row is None:
                if missing:
                    fp = media_identity.fingerprint(p, size)
                    cands = missing.candidates(p, size, fp)
                    to_process.append((p, os.path.normpath(p), size, mtime, cands, fp))
                else:
                    to_process.append((p, os.path.normpath(p), size, mtime))
                continue
            stored_path, k_size, k_mtime = row
            if k_size == size and (k_mtime is None or abs(k_mtime - mtime) < MTIME_TOLERANCE_SEC):
                skipped += 1
                if k_mtime is None:
                    backfill.append((mtime, stored_path))  # starší DB: jen doplnit mtime
            else:
                # Existující záznam -> aktualizovat pod UŽ ULOŽENOU cestou (žádné duplicity)
                to_process.append((p, stored_path, size, mtime))
        return to_process, backfill, skipped

    def _run_pipeline(self):
        self.status_signal.emit(tr("scan-finding-files"))
        try:
            all_files = self._collect_files()
        except Exception as e:
            print(f"Chyba při procházení složky: {e}")
            return 0
        if all_files is None:       # zrušeno
            return 0
        if self.cloud_files:
            self.cloud_skipped_signal.emit(len(self.cloud_files), float(sum(sz for _, sz in self.cloud_files)),
                                           [p for p, _ in self.cloud_files])
            print(f"[Cloud] přeskočeno {len(self.cloud_files)} souborů jen v cloudu (bez souhlasu se nestahují)")
        if not all_files:
            return 0

        total_files = len(all_files)
        known = self._load_known_media()
        missing = None
        if any(_path_key(p) not in known for p in all_files):
            conn = get_connection()
            try:
                missing = media_identity.MissingIndex.load(conn)
            finally:
                conn.close()
        to_process, backfill, skipped = self._classify(all_files, known, missing)

        if backfill:
            try:
                with db_transaction() as conn:
                    conn.executemany("UPDATE media_items SET file_mtime = ? WHERE file_path = ?", backfill)
            except Exception as e:
                print(f"Chyba doplnění mtime: {e}")

        # FÁZE 1: Extrakce metadat jen pro nové / změněné soubory (0 % -> 45 %)
        done = skipped
        self.status_signal.emit(tr("scan-reading-metadata"))
        for i in range(0, len(to_process), self.batch_size):
            if not self._is_running:
                return 0
            batch = to_process[i:i + self.batch_size]
            meta_by_key = self._extract_batch_metadata([b[0] for b in batch])
            try:
                with db_transaction() as conn:
                    cursor = conn.cursor()
                    for entry in batch:
                        real_path, db_path, size, mtime = entry[:4]
                        cands, fp = (entry[4], entry[5]) if len(entry) > 4 else (None, None)
                        meta = meta_by_key.get(_path_key(real_path)) or {"SourceFile": real_path}
                        self._insert_item(cursor, meta, db_path, size, mtime, cands=cands, fp=fp,
                                          missing=missing)
            except Exception as e:
                print(f"Chyba při dávkovém zápisu metadat: {e}")

            done += len(batch)
            self._emit_progress((done / total_files) * 45)

        if self.reconnected:
            print(f"[Identita] znovupřipojeno {self.reconnected} přesunutých / přejmenovaných souborů "
                  f"(alba, tváře a oblíbené zůstaly)")
            try:
                import locked_folder
                locked_folder.load_hidden_paths()      # registr Skrytých je podle cesty
            except Exception as e:
                print(f"[Identita] registr Skrytých: {e}")

        # Sprint 2: nové položky – oblíbené a opravená poloha z XMP sidecarů (knihovna po ztrátě DB)
        if to_process:
            self._restore_sidecars([b[1] for b in to_process])

        self._emit_progress(45, force=True)
        if not self._is_running:
            return 0

        conn = get_connection()
        try:
            # FÁZE 2: Párování Live Photos a geokódování (45 % -> 60 %)
            self.status_signal.emit(tr("scan-pairing-live"))
            self._pair_live_photos(conn)
            self._emit_progress(52, force=True)

            if self._is_running:
                self.status_signal.emit(tr("scan-geocoding"))
                self._geocode_locations(conn)
                self._emit_progress(60, force=True)

            # FÁZE 3: Miniatury - nové i chybějící na disku (60 % -> 92 %)
            if self._is_running:
                self.status_signal.emit(tr("scan-thumbnails"))
                repair_missing_thumbnails(
                    should_stop=lambda: not self._is_running,
                    progress=lambda i, n: self._emit_progress(60 + (i / max(n, 1)) * 25),
                )
                self._emit_progress(85, force=True)

            # FÁZE 3b: pHash + ostrost pro detekci duplicit (85 % -> 92 %)
            if self._is_running:
                self.status_signal.emit(tr("scan-fingerprints"))
                try:
                    from ai_engine import refresh_phashes   # líně: táhne cv2/numpy
                    refresh_phashes(
                        should_stop=lambda: not self._is_running,
                        progress=lambda i, n: self._emit_progress(85 + (i / max(n, 1)) * 7),
                    )
                except Exception as e:
                    print(f"Chyba výpočtu otisků: {e}")
                self._emit_progress(92, force=True)

            # FÁZE 4: Shlukování výletů (92 % -> 100 %)
            if self._is_running:
                self.status_signal.emit(tr("scan-clustering"))
                self._cluster_into_events(conn)
                self._borrow_locations(conn)
                self._emit_progress(100, force=True)
        finally:
            conn.close()

        return len(to_process)

    # ------------------------------------------------------------------
    def _restore_sidecars(self, db_paths):
        try:
            import xmp_sync
            with db_transaction() as conn:
                ids = []
                for k in range(0, len(db_paths), 500):
                    chunk = db_paths[k:k + 500]
                    ids += [r[0] for r in conn.execute(
                        f"SELECT id FROM media_items WHERE file_path IN ({','.join('?' * len(chunk))})", chunk)]
                n = xmp_sync.restore_media(conn, ids)
            if n:
                print(f"[XMP] obnoveno ze sidecarů: {n} položek")
        except Exception as e:
            print(f"[XMP] obnova: {e}")

    def _borrow_locations(self, conn):
        """Sprint 2: fotky bez GPS převezmou polohu z telefonu ve stejné události a chvíli."""
        try:
            from config import load_config
            if not load_config().get("borrow_location", True):
                return
            import location_borrow
            import xmp_sync
            ids = location_borrow.borrow(conn)
            conn.commit()
            if ids:
                print(f"[Poloha] vypůjčeno pro {len(ids)} položek")
                self._geocode_locations(conn)
                xmp_sync.touch(ids)
        except Exception as e:
            print(f"Chyba výpůjčky polohy: {e}")

    def _run_exiftool(self, file_paths):
        cmd = [
            self.exiftool_path, "-json", "-n",
            "-DateTimeOriginal", "-CreationDate", "-CreateDate",
            "-SubSecTimeOriginal", "-OffsetTimeOriginal",
            "-GPSLatitude", "-GPSLongitude", "-GPSAltitude",
            "-GPSCoordinates", "-GPSPosition", "-GPSImgDirection",
            "-Make", "-Model", "-AndroidManufacturer", "-AndroidModel",
            "-LensModel", "-FNumber", "-ExposureTime", "-ISO",
            "-FocalLength", "-FocalLengthIn35mmFormat", "-ContentIdentifier",
        ] + list(file_paths)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                startupinfo=_hidden_startupinfo(), timeout=600,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                return data if isinstance(data, list) else []
        except Exception as e:
            print(f"Chyba ExifTool: {e}")
        return []

    def _extract_batch_metadata(self, file_paths):
        """Vrací dict {path_key: meta}. Metadata se k souborům páruji podle SourceFile,
        NE podle pořadí - jinak by jeden nečitelný soubor posunul metadata všech dalších."""
        by_key = {}
        if os.path.exists(self.exiftool_path):
            for meta in self._run_exiftool(file_paths):
                src = meta.get("SourceFile")
                if src:
                    by_key[_path_key(src)] = meta

            missing = [fp for fp in file_paths if _path_key(fp) not in by_key]
            # Jednotlivě zkusit znovu (max 5 na dávku, ať se nespouští desítky procesů)
            for fp in missing[:5]:
                for meta in self._run_exiftool([fp]):
                    if meta.get("SourceFile"):
                        by_key[_path_key(fp)] = meta
                        break
            still_missing = [fp for fp in file_paths if _path_key(fp) not in by_key]
            if still_missing:
                print(f"ExifTool nevrátil metadata pro {len(still_missing)} souborů "
                      f"(např. {still_missing[0]}); uloží se bez EXIF.")

        result = {}
        for fp in file_paths:
            meta = by_key.get(_path_key(fp), {"SourceFile": fp})
            meta["SourceFile"] = fp          # jistota: vždy skutečná cesta k souboru
            result[_path_key(fp)] = meta

        for meta in result.values():
            if meta.get("GPSLatitude") is None:
                coords = meta.get("GPSCoordinates") or meta.get("GPSPosition")
                if coords:
                    parts = str(coords).split()
                    if len(parts) >= 2:
                        try:
                            meta["GPSLatitude"] = float(parts[0].replace(',', '.'))
                            meta["GPSLongitude"] = float(parts[1].replace(',', '.'))
                        except ValueError:
                            pass

            if not meta.get("DateTimeOriginal"):
                dt = meta.get("CreationDate") or meta.get("CreateDate")
                if dt:
                    meta["DateTimeOriginal"] = str(dt)

            if not meta.get("Make"):
                meta["Make"] = meta.get("AndroidManufacturer")
            if not meta.get("Model"):
                meta["Model"] = meta.get("AndroidModel")

        if MP4 is not None:
            for meta in result.values():
                fp = meta.get("SourceFile", "")
                if fp.lower().endswith(('.mov', '.mp4')) and os.path.exists(fp):
                    try:
                        vid = MP4(fp)
                        if meta.get("GPSLatitude") is None and '©xyz' in vid:
                            val = vid['©xyz'][0]
                            if isinstance(val, bytes):
                                val = val.decode('utf-8', 'ignore')
                            match = re.match(r'([+-]\d+\.\d+)([+-]\d+\.\d+)', val)
                            if match:
                                meta["GPSLatitude"] = float(match.group(1))
                                meta["GPSLongitude"] = float(match.group(2))

                        if not meta.get("DateTimeOriginal") and '©day' in vid:
                            val = vid['©day'][0]
                            if isinstance(val, bytes):
                                val = val.decode('utf-8', 'ignore')
                            meta["DateTimeOriginal"] = val.replace('T', ' ').replace('Z', '')[:19]
                    except Exception:
                        pass

        return result

    def _insert_item(self, cursor, meta, db_path, size, mtime, cands=None, fp=None, missing=None):
        """UPSERT jednoho souboru (nikdy nemaže existující řádek). cands = chybějící položky se
        stejným otiskem (Gate 0): při shodě data pořízení se soubor připojí k té položce."""
        file_path = db_path
        if not file_path:
            return
        file_name = os.path.basename(file_path)
        ext = Path(file_path).suffix.lower()
        media_type = "video" if ext in {'.mov', '.mp4'} else "photo"

        date_taken = _clean_exif_date(meta.get("DateTimeOriginal"))
        if not date_taken:
            try:
                date_taken = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                date_taken = ""

        if cands and missing is not None:
            pick = missing.choose(cands, date_taken)
            if pick is not None and media_identity.rekey(cursor, pick, file_path, fp):
                self.reconnected += 1

        try:
            upsert_media_item(cursor, {
                "file_path": file_path, "file_name": file_name,
                "file_size": size, "file_mtime": mtime,
                "file_ext": ext, "media_type": media_type,
                "date_taken": date_taken,
                "subsec_time": str(meta.get("SubSecTimeOriginal", "")),
                "timezone_offset": str(meta.get("OffsetTimeOriginal", "")),
                "latitude": meta.get("GPSLatitude"), "longitude": meta.get("GPSLongitude"),
                "altitude": meta.get("GPSAltitude"), "compass_heading": meta.get("GPSImgDirection"),
                "device_make": str(meta.get("Make", "")), "device_model": str(meta.get("Model", "")),
                "lens_model": str(meta.get("LensModel", "")),
                "f_number": meta.get("FNumber"), "exposure_time": str(meta.get("ExposureTime", "")),
                "iso": meta.get("ISO"),
                "focal_length": meta.get("FocalLength"),
                "focal_length_35mm": meta.get("FocalLengthIn35mmFormat"),
                "content_identifier": str(meta.get("ContentIdentifier", "")),
                "raw_metadata_json": json.dumps(meta, default=str),
            })
            guid, rel = media_identity.identity_of(file_path)
            if fp is None:
                fp = media_identity.fingerprint(file_path, size)
            cursor.execute("UPDATE media_items SET volume_guid = ?, vol_rel_path = ?, fingerprint = ?, "
                           "availability = NULL WHERE file_path = ?", (guid, rel, fp, file_path))
        except Exception as err:
            print(f"Chyba zápisu souboru {file_name}: {err}")

    def _pair_live_photos(self, conn):
        try:
            cursor = conn.cursor()
            cursor.execute("""
            SELECT p.id, p.file_path, v.file_path 
            FROM media_items p
            JOIN media_items v ON 
                substr(p.file_name, 1, instr(p.file_name, '.') - 1) = substr(v.file_name, 1, instr(v.file_name, '.') - 1)
                AND p.media_type = 'photo' 
                AND v.media_type = 'video'
                AND rtrim(p.file_path, p.file_name) = rtrim(v.file_path, v.file_name)
            """)
            pairs = cursor.fetchall()
            for photo_id, _, video_path in pairs:
                cursor.execute("""
                    UPDATE media_items 
                    SET media_type = 'live_photo', paired_video_path = ? 
                    WHERE id = ?
                """, (video_path, photo_id))
            conn.commit()
        except Exception as e:
            print(f"Chyba při párování Live Photos: {e}")

    def _geocode_locations(self, conn):
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT id, latitude, longitude FROM media_items WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND place_city IS NULL")
            rows = cursor.fetchall()
            if not rows:
                return

            coords = [(r[1], r[2]) for r in rows]
            # mode=1: jeden proces. Výchozí mode=2 spouští multiprocessing, což
            # v zabaleném (frozen) Windows buildu bez freeze_support() restartuje aplikaci.
            try:
                results = rg.search(coords, mode=1, verbose=False)
            except TypeError:
                results = rg.search(coords, mode=1)
            updates = [
                (res.get('name', ''), res.get('cc', ''), rows[i][0])
                for i, res in enumerate(results)
            ]
            cursor.executemany("UPDATE media_items SET place_city = ?, place_country = ? WHERE id = ?", updates)
            conn.commit()
        except Exception as e:
            print(f"Chyba při geokódování: {e}")

    def _cluster_into_events(self, conn):
        """Gate 2 B: adaptive trips (home inference, day trips vs. journeys with chapters).
        Event ids stay stable across re-clusters, so covers and links survive (trips.apply)."""
        try:
            stats = trips.recluster(conn)
            conn.commit()
            if stats.get("kinds"):
                print(f"[Events] {stats['kinds']} | kept {stats['kept']}, new {stats['created']}, "
                      f"removed {stats['deleted']}")
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[Events] clustering failed: {e}")

