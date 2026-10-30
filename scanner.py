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
import reverse_geocoder as rg
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

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
THUMB_SIZE = 360
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

def make_thumbnail(file_path, thumb_path, m_type, paired_video, ffmpeg_path=DEFAULT_FFMPEG):
    """Vytvoří miniaturu (max. 360 px). Zápis je atomický. Vrací True při úspěchu."""
    startupinfo = _hidden_startupinfo()
    tmp_path = os.path.splitext(thumb_path)[0] + ".tmp.jpg"
    is_video_only = (m_type == 'video' and not paired_video)

    def _ffmpeg(extra_before_input):
        if not os.path.exists(ffmpeg_path):
            return
        cmd = [ffmpeg_path, "-loglevel", "quiet", "-y"] + extra_before_input + [
            "-i", file_path, "-map", "0:v:0", "-frames:v", "1",
            "-vf", f"scale='min({THUMB_SIZE},iw)':-2,format=yuvj420p",
            "-q:v", "3", tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, startupinfo=startupinfo)

    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if not is_video_only:
            try:
                with Image.open(file_path) as img:
                    # JPEG: dekodér umí rovnou zmenšit (DCT scaling), ušetří paměť i čas.
                    try:
                        img.draft("RGB", (THUMB_SIZE * 2, THUMB_SIZE * 2))
                    except Exception:
                        pass
                    # Nejdřív zmenšit, teprve potom otáčet / konvertovat (žádné kopie v plné velikosti).
                    img.thumbnail((THUMB_SIZE * 2, THUMB_SIZE * 2))
                    oriented = ImageOps.exif_transpose(img)
                    rgb = oriented.convert("RGB")
                    rgb.thumbnail((THUMB_SIZE, THUMB_SIZE))
                    rgb.save(tmp_path, "JPEG", quality=85)
            except Exception:
                _ffmpeg([])
        else:
            _ffmpeg(["-ss", "00:00:00.200"])

        if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            os.replace(tmp_path, thumb_path)
            return True
    except Exception as e:
        print(f"Chyba miniatury {file_path}: {e}")
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except Exception:
        pass
    return False


def repair_missing_thumbnails(should_stop=None, progress=None, flush_every=50):
    """Doplní miniatury, které chybí v databázi (NULL) nebo na disku (smazaná cache).

    thumbnail_path == '' znamená "už se to zkoušelo a nešlo" - takové soubory se
    znovu nezkouší, dokud se nezmění (UPSERT při změně velikosti vrátí NULL).
    Vrací počet vytvořených miniatur.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = get_connection()
    created = 0
    try:
        c = conn.cursor()
        c.execute("SELECT id, file_path, media_type, paired_video_path, thumbnail_path FROM media_items")
        todo = []
        for item_id, fp, m_type, paired, thumb in c.fetchall():
            if thumb == "":
                continue
            if thumb is None or not os.path.exists(thumb):
                todo.append((item_id, fp, m_type, paired))

        total = len(todo)
        updates = []

        def flush():
            if updates:
                c.executemany("UPDATE media_items SET thumbnail_path = ? WHERE id = ?", updates)
                conn.commit()
                updates.clear()

        for i, (item_id, fp, m_type, paired) in enumerate(todo):
            if should_stop and should_stop():
                break
            if not fp or not os.path.exists(fp):
                continue  # zdroj chybí (odpojený disk?) - nic neznačit, zkusí se příště
            thumb_path = os.path.join(CACHE_DIR, f"thumb_{item_id}.jpg")
            if make_thumbnail(fp, thumb_path, m_type, paired):
                updates.append((thumb_path, item_id))
                created += 1
            else:
                updates.append(("", item_id))  # trvale nečitelné
            if len(updates) >= flush_every:
                flush()
            if progress:
                progress(i + 1, total)
        flush()
    finally:
        conn.close()
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

    def __init__(self, target_folder, exiftool_path=None, batch_size=50, files=None):
        """target_folder = složka (rekurzivně), nebo None a files = seznam souborů (přetažené
        soubory: indexují se na místě, nic se nekopíruje)."""
        super().__init__()
        self.target_folder = target_folder
        self.files = list(files) if files is not None else None
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
        if self.files is not None:
            return with_live_partners(self.files)
        all_files = []
        for root, _, files in os.walk(self.target_folder):
            if not self._is_running:
                return None
            for f in files:
                if Path(f).suffix.lower() in SUPPORTED_EXTS:
                    all_files.append(os.path.join(root, f))
        return all_files

    def _load_known_media(self):
        """path_key -> (uložená cesta, velikost, mtime) pro všechna média v DB."""
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute("SELECT file_path, file_size, file_mtime FROM media_items")
            return {_path_key(p): (p, s, m) for p, s, m in c.fetchall() if p}
        finally:
            conn.close()

    def _classify(self, all_files, known):
        """Rozdělí soubory na nové/změněné (ke zpracování) a beze změny (přeskočit)."""
        to_process, backfill, skipped = [], [], 0
        for p in all_files:
            try:
                st = os.stat(p)
            except OSError:
                continue
            size, mtime = st.st_size, st.st_mtime
            row = known.get(_path_key(p))
            if row is None:
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
        self.status_signal.emit("Hledám soubory...")
        try:
            all_files = self._collect_files()
        except Exception as e:
            print(f"Chyba při procházení složky: {e}")
            return 0
        if all_files is None:       # zrušeno
            return 0
        if not all_files:
            return 0

        total_files = len(all_files)
        known = self._load_known_media()
        to_process, backfill, skipped = self._classify(all_files, known)

        if backfill:
            try:
                with db_transaction() as conn:
                    conn.executemany("UPDATE media_items SET file_mtime = ? WHERE file_path = ?", backfill)
            except Exception as e:
                print(f"Chyba doplnění mtime: {e}")

        # FÁZE 1: Extrakce metadat jen pro nové / změněné soubory (0 % -> 45 %)
        done = skipped
        self.status_signal.emit("Načítám metadata...")
        for i in range(0, len(to_process), self.batch_size):
            if not self._is_running:
                return 0
            batch = to_process[i:i + self.batch_size]
            meta_by_key = self._extract_batch_metadata([b[0] for b in batch])
            try:
                with db_transaction() as conn:
                    cursor = conn.cursor()
                    for real_path, db_path, size, mtime in batch:
                        meta = meta_by_key.get(_path_key(real_path)) or {"SourceFile": real_path}
                        self._insert_item(cursor, meta, db_path, size, mtime)
            except Exception as e:
                print(f"Chyba při dávkovém zápisu metadat: {e}")

            done += len(batch)
            self._emit_progress((done / total_files) * 45)

        # Sprint 2: nové položky – oblíbené a opravená poloha z XMP sidecarů (knihovna po ztrátě DB)
        if to_process:
            self._restore_sidecars([b[1] for b in to_process])

        self._emit_progress(45, force=True)
        if not self._is_running:
            return 0

        conn = get_connection()
        try:
            # FÁZE 2: Párování Live Photos a geokódování (45 % -> 60 %)
            self.status_signal.emit("Slučuji Live Photos...")
            self._pair_live_photos(conn)
            self._emit_progress(52, force=True)

            if self._is_running:
                self.status_signal.emit("Zjišťuji polohu a města...")
                self._geocode_locations(conn)
                self._emit_progress(60, force=True)

            # FÁZE 3: Miniatury - nové i chybějící na disku (60 % -> 92 %)
            if self._is_running:
                self.status_signal.emit("Vytvářím náhledy...")
                repair_missing_thumbnails(
                    should_stop=lambda: not self._is_running,
                    progress=lambda i, n: self._emit_progress(60 + (i / max(n, 1)) * 25),
                )
                self._emit_progress(85, force=True)

            # FÁZE 3b: pHash + ostrost pro detekci duplicit (85 % -> 92 %)
            if self._is_running:
                self.status_signal.emit("Počítám otisky pro duplicity...")
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
                self.status_signal.emit("Shlukuji výlety...")
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

    def _insert_item(self, cursor, meta, db_path, size, mtime):
        """UPSERT jednoho souboru (nikdy nemaže existující řádek)."""
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
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, date_taken, latitude, longitude, place_city 
                FROM media_items 
                WHERE date_taken IS NOT NULL AND date_taken != ''
                ORDER BY date_taken ASC
            """)
            items = cursor.fetchall()
            if not items:
                return

            # POŘADÍ JE DŮLEŽITÉ: nejdřív zrušit odkazy z media_items, teprve pak mazat events.
            # (Cizí klíč media_items.event_id -> events.id je zapnutý; dřív to fungovalo jen
            # proto, že INSERT OR REPLACE předtím smazal všechny řádky s odkazem.)
            cursor.execute("UPDATE media_items SET event_id = NULL")
            cursor.execute("DELETE FROM events")

            events_clusters = []
            current_cluster = []
            last_dt, last_lat, last_lon = None, None, None

            for item_id, dt_str, lat, lon, city in items:
                try:
                    dt = datetime.strptime(dt_str[:19], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue

                item_dict = {"id": item_id, "dt": dt, "lat": lat, "lon": lon, "city": city or "Neznámé místo"}

                if not current_cluster:
                    current_cluster.append(item_dict)
                    last_dt, last_lat, last_lon = dt, lat, lon
                    continue

                time_diff_hours = (dt - last_dt).total_seconds() / 3600.0
                if time_diff_hours > 26.0:
                    events_clusters.append(current_cluster)
                    current_cluster = [item_dict]
                else:
                    if lat is not None and lon is not None and last_lat is not None and last_lon is not None:
                        dist_km = haversine_distance_km(last_lat, last_lon, lat, lon)
                        if dist_km > 45.0 and time_diff_hours > 8.0:
                            events_clusters.append(current_cluster)
                            current_cluster = [item_dict]
                        else:
                            current_cluster.append(item_dict)
                    else:
                        if time_diff_hours > 14.0:
                            events_clusters.append(current_cluster)
                            current_cluster = [item_dict]
                        else:
                            current_cluster.append(item_dict)

                last_dt = dt
                if lat is not None and lon is not None:
                    last_lat, last_lon = lat, lon

            if current_cluster:
                events_clusters.append(current_cluster)

            for cluster in events_clusters:
                self._save_smart_event(cursor, cluster)

            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"Chyba při shlukování událostí: {e}")

    def _save_smart_event(self, cursor, cluster):
        if not cluster:
            return
        start_dt = cluster[0]["dt"]
        end_dt = cluster[-1]["dt"]

        if start_dt.date() == end_dt.date():
            date_label = start_dt.strftime("%d. %m. %Y")
        else:
            date_label = f"{start_dt.strftime('%d. %m.')} – {end_dt.strftime('%d. %m. %Y')}"

        cities = [c["city"] for c in cluster if c["city"] != "Neznámé místo"]
        if cities:
            dominant_city, _ = Counter(cities).most_common(1)[0]
            unique_cities = list(dict.fromkeys(cities))
            title = f"Výlet: {dominant_city} a okolí ({date_label})" if len(unique_cities) > 1 else f"Výlet: {dominant_city} ({date_label})"
            location_name = dominant_city
        else:
            title = f"Výlet ({date_label})"
            location_name = "Neznámé místo"

        cursor.execute("""
            INSERT INTO events (title, location_name, start_date, end_date, item_count)
            VALUES (?, ?, ?, ?, ?)
        """, (title, location_name, start_dt.strftime("%Y-%m-%d %H:%M"), end_dt.strftime("%Y-%m-%d %H:%M"), len(cluster)))
        event_id = cursor.lastrowid

        cursor.executemany(
            "UPDATE media_items SET event_id = ? WHERE id = ?",
            [(event_id, c["id"]) for c in cluster],
        )