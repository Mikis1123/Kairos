from i18n import tr
import os
import time
import sqlite3
import subprocess
import threading
import cv2
import numpy as np
from PIL import Image, ImageOps
from database import get_connection
import cv_io
from paths import FACE_DIR as FACE_CACHE_DIR, FFMPEG_PATH, resolve_face_path

os.makedirs(FACE_CACHE_DIR, exist_ok=True)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass
import raw_decode  # noqa: E402  ProRAW / camera RAW through every Image.open (raw_decode.py)
raw_decode.register()

# Moduly InsightFace: detekce + rozpoznání stačí (3D landmarky a genderage se nepoužívají).
# Pro budoucí filtr podle natočení hlavy (yaw) přidej "landmark_3d_68".
FACE_MODULES = ["detection", "recognition"]

_app = None
_face_align = None
_app_lock = threading.Lock()

# Referenční body ArcFace (112×112): oči, nos, koutky úst – stejné jako insightface.utils.face_align
ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)


def umeyama_similarity(src, dst):
    """Podobnostní transformace (rotace + měřítko + posun) metodou nejmenších čtverců (Umeyama 1991).

    Matematicky totéž jako skimage SimilarityTransform.estimate, které InsightFace volá a které
    scikit-image 2.2 odstraní (FutureWarning „estimate is deprecated“) – pak by přestalo fungovat
    zarovnání obličejů i embeddingy. Vrací matici 2×3 pro cv2.warpAffine."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n = src.shape[0]
    mu_s, mu_d = src.mean(axis=0), dst.mean(axis=0)
    s0, d0 = src - mu_s, dst - mu_d
    cov = d0.T @ s0 / n
    U, S, Vt = np.linalg.svd(cov)
    D = np.eye(2)
    if np.linalg.det(U) * np.linalg.det(Vt) < 0:
        D[1, 1] = -1.0
    R = U @ D @ Vt
    var_s = (s0 ** 2).sum() / n
    scale = (S * np.diag(D)).sum() / var_s if var_s > 0 else 1.0
    t = mu_d - scale * (R @ mu_s)
    return np.hstack([scale * R, t[:, None]])


def estimate_norm(lmk, image_size=112, mode="arcface"):
    """Náhrada insightface.utils.face_align.estimate_norm bez scikit-image (stejné výsledky)."""
    lmk = np.asarray(lmk, dtype=np.float64)
    assert lmk.shape == (5, 2)
    if image_size % 112 == 0:
        ratio, diff_x = image_size / 112.0, 0.0
    else:
        ratio = image_size / 128.0
        diff_x = 8.0 * ratio
    dst = ARCFACE_DST * ratio
    dst[:, 0] += diff_x
    return umeyama_similarity(lmk, dst)


def norm_crop(img, landmark, image_size=112):
    M = estimate_norm(landmark, image_size)
    return cv2.warpAffine(img, M, (image_size, image_size), borderValue=0.0)


def _install_own_alignment(face_align_module):
    """InsightFace (detekce i rozpoznání) volá face_align.norm_crop -> estimate_norm;
    podstrčíme vlastní implementaci, takže scikit-image se pro zarovnání vůbec nepoužije."""
    face_align_module.estimate_norm = estimate_norm
    face_align_module.norm_crop = lambda img, landmark, image_size=112, mode="arcface": norm_crop(
        img, landmark, image_size)


def _get_app():
    """Líně inicializuje InsightFace (až v démonu, ne při importu modulu při startu aplikace)."""
    global _app, _face_align
    with _app_lock:
        if _app is None:
            import onnxruntime as ort
            from insightface.app import FaceAnalysis
            from insightface.utils import face_align
            _install_own_alignment(face_align)

            # MODUL 1.1: Automatická detekce DirectX 12 GPU akcelerace (DirectML) s fallbackem na CPU
            providers = []
            if "DmlExecutionProvider" in ort.get_available_providers():
                providers.append("DmlExecutionProvider")
            providers.append("CPUExecutionProvider")

            try:
                face_app = FaceAnalysis(name="buffalo_l", providers=providers, allowed_modules=FACE_MODULES)
            except TypeError:   # starší insightface bez allowed_modules
                face_app = FaceAnalysis(name="buffalo_l", providers=providers)
            face_app.prepare(ctx_id=0, det_size=(640, 640))
            _app, _face_align = face_app, face_align
        return _app


def ensure_ready():
    """Načte modely předem; vyhodí výjimku, pokud to nejde (např. chybí model)."""
    _get_app()


def _discard_crops(detections):
    for det in detections:
        try:
            os.remove(det["crop_path"])
        except OSError:
            pass


def _get_images_from_media(file_path):
    if not file_path or not os.path.exists(file_path):
        return []

    ext = os.path.splitext(file_path)[1].lower()

    if ext in {'.mov', '.mp4'}:
        frames = []
        try:
            cap = cv2.VideoCapture(file_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()

            if fps > 0 and frame_count > 0:
                duration = frame_count / fps
            else:
                duration = 5.0
        except Exception:
            duration = 5.0

        interval = 1.5
        timestamps = []
        t = 0.5
        while t < duration:
            timestamps.append(t)
            t += interval

        if len(timestamps) > 10:
            timestamps = [i * (duration / 10) for i in range(10)]
        if not timestamps:
            timestamps = [0.5]

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        for t in timestamps:
            hours = int(t // 3600)
            minutes = int((t % 3600) // 60)
            seconds = t % 60
            ts_str = f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"

            temp_frame = os.path.join(FACE_CACHE_DIR, f"temp_{os.getpid()}_{int(t*1000)}.jpg")
            cmd = [
                (FFMPEG_PATH if os.path.exists(FFMPEG_PATH) else "ffmpeg"), "-loglevel", "quiet", "-y",
                "-ss", ts_str,
                "-i", file_path,
                "-vframes", "1",
                "-q:v", "2",
                temp_frame
            ]
            subprocess.run(cmd, capture_output=True, startupinfo=startupinfo)
            if os.path.exists(temp_frame):
                img_bgr = cv_io.imread(temp_frame)
                try:
                    os.remove(temp_frame)
                except Exception:
                    pass
                if img_bgr is not None and img_bgr.size > 0:
                    frames.append(img_bgr)
        return frames
    else:
        try:
            import imaging                      # stejný dekodér jako zbytek aplikace
            arr = np.asarray(imaging.flatten(imaging.open_oriented(file_path).image))
            return [cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)]
        except Exception:
            img = cv_io.imread(file_path)
            return [img] if img is not None else []


def _is_valid_face_geometry(face):
    if not hasattr(face, 'kps') or face.kps is None:
        return False
    kps = face.kps
    if len(kps) < 3:
        return False
    rx, ry = kps[0]
    lx, ly = kps[1]
    nx, ny = kps[2]

    if ry >= ny or ly >= ny:
        return False
    return True


# Verze algoritmu ořezu; starší ořezy u obličejů u okraje fotky byly deformované (viz níže).
# 3 = opravené i obličeje z videí (verze 2 je jen označila – protažené avatary v Osobách zůstaly)
CROP_VERSION = 3
# Avatar v rozlišení, které stačí i největšímu použití (profil osoby 200 log. px × DPR 2 ≈ 400,
# karta 137 × 2 = 275) – dřív 256 px (a staré 112/224 px), takže se na HiDPI zvětšoval = rozmazaný
AVATAR_PX = 384
FACE_RENDER_VERSION = 1        # 1 = imaging pipeline: Lanczos3 384 px, ostření jasu, 4:4:4
FACE_RENDER_MARKER = "FACE_RENDER"
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}
CROP_MATCH_TOL = 22.0          # průměrný rozdíl jasu (0–255) na 64×64: uložený ořez pochází z tohoto snímku


def square_crop_window(img_w, img_h, bbox, pad_ratio=0.50):
    """Čtvercové okno (x, y, strana) kolem obličeje, které se VŽDY vejde do fotky.

    Dřív se okno u obličeje blízko okraje (selfie) ořízlo okrajem na obdélník a ten se pak
    roztáhl na čtverec -> svisle / vodorovně protažené avatary. Teď se okno posune dovnitř
    fotky (a případně zmenší na kratší stranu), takže poměr stran zůstává přesně 1:1."""
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    side = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * pad_ratio)
    side = int(round(min(side, img_w, img_h)))
    if side <= 0:
        return None
    x = int(round(min(max(cx - side / 2.0, 0), img_w - side)))
    y = int(round(min(max(cy - side / 2.0, 0), img_h - side)))
    return x, y, side


def _extract_apple_avatar_crop(img, bbox, target_size=AVATAR_PX, pad_ratio=0.50):
    """
    MODUL 2.1: Dynamický ořez obličeje rozšířený o 50% okraj (hlava, vlasy a ramena jako v Apple Photos).
    Vždy čtvercový výřez -> zmenšení na target_size beze změny poměru stran.
    """
    h, w = img.shape[:2]
    window = square_crop_window(w, h, bbox, pad_ratio)
    if window is None:
        return None
    x, y, side = window
    crop = img[y:y + side, x:x + side]
    if crop is None or crop.size == 0 or crop.shape[0] != crop.shape[1]:
        return None
    # imaging: Lanczos3 + ostření jasu (barvy zdroje; převod do kontraktu displeje dělá
    # _avatar_to_output, u videí žádný)
    import imaging
    rgb = Image.fromarray(cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2RGB))
    out = imaging.render_avatar(rgb, int(target_size), output=False)
    return cv2.cvtColor(np.asarray(out), cv2.COLOR_RGB2BGR)


def _write_avatar(path, avatar_bgr):
    """Uloží hotový avatar (BGR) jako cache JPEG 4:4:4 (imaging) – atomicky. True = OK."""
    if avatar_bgr is None or avatar_bgr.size == 0:
        return False
    import imaging
    rgb = Image.fromarray(cv2.cvtColor(np.ascontiguousarray(avatar_bgr), cv2.COLOR_BGR2RGB))
    return imaging.write_jpeg(rgb, path)


# ── barvy avatarů ────────────────────────────────────────────────────────────
# Ořezy tváří z FOTEK jsou v kontraktu displeje (color_manage), stejně jako miniatury; z videí ne
# (video se kreslí bez správy barev). Jediný převod: ze zdrojového profilu hotového ořezu.
# Změna kontraktu = nový ořez z originálu (rerender_face_crops), nikdy převod převodu.


def _source_icc(path):
    try:
        with Image.open(path) as im:
            return im.info.get("icc_profile") or None
    except Exception:
        return None


def _avatar_to_output(avatar_bgr, source_path, src_icc=False):
    """Ořez (BGR) z fotky převedený z profilu fotky do výstupního profilu. Video beze změny."""
    if avatar_bgr is None or os.path.splitext(source_path or "")[1].lower() in VIDEO_EXTS:
        return avatar_bgr
    try:
        import color_manage
        icc = _source_icc(source_path) if src_icc is False else src_icc
        rgb = Image.fromarray(cv2.cvtColor(avatar_bgr, cv2.COLOR_BGR2RGB))
        out = color_manage.pil_to_output(rgb, icc)
        return cv2.cvtColor(np.asarray(out), cv2.COLOR_RGB2BGR) if out is not rgb else avatar_bgr
    except Exception:
        return avatar_bgr


def _face_render_key():
    import color_manage
    return {"v": FACE_RENDER_VERSION, "fp": color_manage.target_fingerprint()}


def _rerender_video_faces(path, media_rows, size=AVATAR_PX, pad_ratio=0.50):
    """Ořezy z videa znovu (nová pipeline): snímek, ze kterého ořez vznikl, se najde porovnáním
    uloženého ořezu s ořezem každého snímku (stejné časy jako při skenování). Bez shody se ořez
    nechá být. Video se barevně nepřevádí. Vrací počet přeřezaných."""
    todo = []
    for fid, crop_path, bx, by, bw, bh, _, media_id in media_rows:
        stored = resolve_face_path(crop_path, FACE_CACHE_DIR)
        if stored and None not in (bx, by, bw, bh):
            sig = _crop_signature(cv_io.imread(stored))
            if sig is not None:
                todo.append((stored, (bx, by, bw, bh), sig))
    if not todo:
        return 0
    frames = _get_images_from_media(path)
    done = 0
    for stored, (bx, by, bw, bh), sig in todo:
        best = None
        for frame in frames:
            fh, fw = frame.shape[:2]
            bbox = (int(bx * fw), int(by * fh), int((bx + bw) * fw), int((by + bh) * fh))
            cand = _extract_apple_avatar_crop(frame, bbox, size, pad_ratio)
            csig = _crop_signature(cand)
            if csig is None:
                continue
            d = float(np.mean(np.abs(csig - sig)))
            if best is None or d < best[0]:
                best = (d, cand)
        if best is not None and best[0] <= CROP_MATCH_TOL and _write_avatar(stored, best[1]):
            done += 1
    return done


def rerender_face_crops(should_stop=None):
    """Přeřízne všechny ořezy tváří z ORIGINÁLŮ, když se změnila pipeline ořezů
    (FACE_RENDER_VERSION) nebo kontrakt barev displeje – nikdy nepřevádí hotový ořez podruhé.
    Fotka se dekóduje jednou pro všechny své tváře. Obličeje z videí jen při nové verzi pipeline
    (barevně se nepřevádějí). Přerušitelné: hotová id se průběžně zapisují do značky.
    Vrací počet přeřezaných ořezů."""
    import json
    import imaging
    marker = os.path.join(FACE_CACHE_DIR, FACE_RENDER_MARKER)
    cur = _face_render_key()
    try:
        with open(marker, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    last = state.get("done_key")
    if last == cur:
        return 0
    if state.get("pending") != cur:
        state = {"done_key": last, "pending": cur, "ids": []}
    videos_too = (last or {}).get("v") != FACE_RENDER_VERSION
    done = set(state.get("ids") or [])

    def save_state(final=False):
        os.makedirs(FACE_CACHE_DIR, exist_ok=True)
        data = ({"done_key": cur, "pending": None, "ids": []} if final
                else dict(state, ids=sorted(done)))
        tmp = marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, marker)

    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT f.id, f.face_crop_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, m.file_path, f.media_id
            FROM face_instances f JOIN media_items m ON m.id = f.media_id
            WHERE f.face_crop_path IS NOT NULL ORDER BY f.media_id
        """).fetchall()
    finally:
        conn.close()
    by_media = {}
    for row in rows:
        if row[0] not in done:
            by_media.setdefault(row[7], []).append(row)
    rendered = 0
    for k, media_rows in enumerate(by_media.values()):
        if should_stop and should_stop():
            save_state()
            return rendered
        path = media_rows[0][6]
        ext = os.path.splitext(path or "")[1].lower()
        try:
            if not path or not os.path.exists(path):
                pass
            elif ext in VIDEO_EXTS:
                if videos_too:
                    rendered += _rerender_video_faces(path, media_rows)
            else:
                dec = imaging.open_oriented(path)
                w, h = dec.image.size
                for fid, crop_path, bx, by, bw, bh, _, media_id in media_rows:
                    if None in (bx, by, bw, bh):
                        continue
                    window = square_crop_window(w, h, (bx * w, by * h, (bx + bw) * w, (by + bh) * h), 0.50)
                    if window is None:
                        continue
                    x, y, side = window
                    crop = dec.image.crop((x, y, x + side, y + side)).convert("RGB")
                    avatar = imaging.render_avatar(crop, AVATAR_PX, dec.icc)
                    target = resolve_face_path(crop_path, FACE_CACHE_DIR) or os.path.join(
                        FACE_CACHE_DIR, f"face_{media_id}_rec_{fid}.jpg")
                    if imaging.write_jpeg(avatar, target):
                        rendered += 1
                        if target != crop_path:
                            c = get_connection()
                            try:
                                c.execute("UPDATE face_instances SET face_crop_path = ? WHERE id = ?", (target, fid))
                                c.execute("UPDATE people SET cover_face_path = ? WHERE key_face_id = ?", (target, fid))
                                c.commit()
                            finally:
                                c.close()
        except Exception as e:
            print(f"[FaceCrops] {os.path.basename(path or '')}: {e}")
        done.update(r[0] for r in media_rows)
        if k % 10 == 9:
            save_state()
    save_state(final=True)
    return rendered


PURGE_MIN_REFS_PRESENT = 0.8     # aspoň tolik odkazovaných ořezů musí v té složce opravdu ležet


def purge_orphan_face_files(min_age_s=86400):
    """Smaže soubory ořezů, na které už neodkazuje žádný obličej ani obálka osoby (pozůstatky
    opakovaných skenů). Jen face_*.jpg / temp_*.jpg starší než min_age_s.

    POJISTKA: maže jen tehdy, když databáze a složka prokazatelně patří k sobě – DB má odkazy
    a aspoň PURGE_MIN_REFS_PRESENT z nich v této složce existuje. Jiná / prázdná / dočasná DB
    proti skutečné složce (testy, rozbitá migrace) = nic se nesmaže. Vrací počet smazaných."""
    conn = get_connection()
    try:
        refs = {os.path.basename(str(p).replace("\\", "/")).lower()
                for (p,) in conn.execute(
                    "SELECT face_crop_path FROM face_instances WHERE face_crop_path IS NOT NULL "
                    "UNION SELECT cover_face_path FROM people WHERE cover_face_path IS NOT NULL") if p}
    finally:
        conn.close()
    try:
        entries = list(os.scandir(FACE_CACHE_DIR))
    except OSError:
        return 0
    names = {e.name.lower() for e in entries}
    present = len(refs & names)
    if not refs or present < PURGE_MIN_REFS_PRESENT * len(refs):
        print(f"[FaceCrops] úklid ořezů přeskočen: DB a složka k sobě nepatří ({present}/{len(refs)})")
        return 0
    now = time.time()
    removed = 0
    for e in entries:
        name = e.name.lower()
        if not (name.endswith(".jpg") and (name.startswith("face_") or name.startswith("temp_"))):
            continue
        if name in refs:
            continue
        try:
            if not e.is_file() or now - e.stat().st_mtime < min_age_s:
                continue
            os.remove(e.path)
            removed += 1
        except OSError:
            pass
    return removed


def restore_missing_video_face_crops(should_stop=None, min_similarity=0.55):
    """Samoléčení: ořezy obličejů z VIDEÍ, jejichž soubor chybí, se vyříznou znovu. Snímek se
    vytáhne ve stejných časech jako při skenování, detektor najde tváře a ta pravá se pozná
    podle uloženého ArcFace embeddingu (+ překryv rámečku) – tedy přesně ta tvář z přesně
    toho snímku, ne odhad. Uloží se na původní cestu. Vrací počet obnovených ořezů."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT f.id, f.face_crop_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.embedding,
                   m.file_path, f.media_id
            FROM face_instances f JOIN media_items m ON m.id = f.media_id
            WHERE f.face_crop_path IS NOT NULL ORDER BY f.media_id
        """).fetchall()
    finally:
        conn.close()
    import json
    marker = os.path.join(FACE_CACHE_DIR, "FACE_RESTORE")      # id, které se obnovit nepodařilo
    try:
        with open(marker, encoding="utf-8") as f:
            failed = set(json.load(f).get("failed") or [])
    except (OSError, ValueError):
        failed = set()
    by_media = {}
    for row in rows:
        if row[0] in failed:
            continue                                     # nezkoušet při každém startu znovu
        if os.path.splitext(row[7] or "")[1].lower() in VIDEO_EXTS and not resolve_face_path(row[1], FACE_CACHE_DIR):
            by_media.setdefault(row[8], []).append(row)
    if not by_media:
        return 0
    app = _get_app()
    attempted = set()
    restored = 0
    for media_rows in by_media.values():
        if should_stop and should_stop():
            break
        path = media_rows[0][7]
        if not path or not os.path.exists(path):
            continue
        try:
            detections = []                                   # (frame, bbox px, normed emb, bbox norm)
            for frame in _get_images_from_media(path):
                fh, fw = frame.shape[:2]
                for face in app.get(frame) or []:
                    emb = getattr(face, "normed_embedding", None)
                    if emb is None:
                        continue
                    x1, y1, x2, y2 = [float(v) for v in face.bbox]
                    detections.append((frame, (max(0, int(x1)), max(0, int(y1)), min(fw, int(x2)), min(fh, int(y2))),
                                       np.asarray(emb, dtype=np.float32),
                                       (x1 / fw, y1 / fh, x2 / fw, y2 / fh)))
            for fid, crop_path, bx, by, bw, bh, blob, _, media_id in media_rows:
                attempted.add(fid)
                if not blob:
                    continue
                ref = np.frombuffer(bytes(blob), dtype=np.float32)
                ref = ref / (np.linalg.norm(ref) or 1.0)
                best = None
                for frame, bbox, emb, nb in detections:
                    if emb.shape != ref.shape:
                        continue
                    sim = float(np.dot(ref, emb))
                    if None not in (bx, by, bw, bh):
                        ix = max(0.0, min(bx + bw, nb[2]) - max(bx, nb[0]))
                        iy = max(0.0, min(by + bh, nb[3]) - max(by, nb[1]))
                        inter = ix * iy
                        union = bw * bh + (nb[2] - nb[0]) * (nb[3] - nb[1]) - inter
                        sim += 0.1 * (inter / union if union > 0 else 0.0)
                    if best is None or sim > best[0]:
                        best = (sim, frame, bbox)
                if best is None or best[0] < min_similarity:
                    continue
                avatar = _extract_apple_avatar_crop(best[1], best[2], AVATAR_PX, 0.50)
                target = os.path.join(FACE_CACHE_DIR, os.path.basename(str(crop_path).replace("\\", "/")))
                if avatar is not None and _write_avatar(target, avatar):
                    attempted.discard(fid)
                    restored += 1
                    if os.path.normcase(target) != os.path.normcase(str(crop_path)):
                        c = get_connection()
                        try:
                            c.execute("UPDATE face_instances SET face_crop_path = ? WHERE id = ?", (target, fid))
                            c.execute("UPDATE people SET cover_face_path = ? WHERE key_face_id = ?", (target, fid))
                            c.commit()
                        finally:
                            c.close()
        except Exception as e:
            print(f"[FaceCrops] obnova {os.path.basename(path)}: {e}")
    if attempted:
        try:
            with open(marker, "w", encoding="utf-8") as f:
                json.dump({"failed": sorted(failed | attempted)}, f)
        except OSError:
            pass
    return restored


def _is_stretched_window(img_w, img_h, bbox, pad_ratio=0.50, tolerance=0.03):
    """Byl by ořez starým algoritmem (okno oříznuté okrajem) deformovaný?"""
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    half = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * pad_ratio) / 2.0
    cw = min(img_w, cx + half) - max(0, cx - half)
    ch = min(img_h, cy + half) - max(0, cy - half)
    return ch > 0 and abs(cw / ch - 1.0) > tolerance


def _legacy_avatar_crop(img, bbox, target_size=256, pad_ratio=0.50):
    """Původní (CROP_VERSION 1) ořez: okno oříznuté okrajem a roztažené na čtverec.
    Jen pro rozpoznání starých ořezů – nové se tak nikdy nedělají."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    half = max(x2 - x1, y2 - y1) * (1.0 + 2.0 * pad_ratio) / 2.0
    ax, ay = int(max(0, cx - half)), int(max(0, cy - half))
    bx, by = int(min(w, cx + half)), int(min(h, cy + half))
    if bx - ax < 2 or by - ay < 2:
        return None
    return cv2.resize(img[ay:by, ax:bx], (target_size, target_size), interpolation=cv2.INTER_AREA)


def _crop_signature(img):
    if img is None or img.size == 0:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    return cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)


def _video_size(path):
    try:
        cap = cv2.VideoCapture(path)
        try:
            w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        finally:
            cap.release()
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def _repair_video_faces(path, media_rows, target_size=AVATAR_PX, pad_ratio=0.50):
    """Protažené ořezy obličejů z videa: snímek, ze kterého ořez vznikl, se najde znovu (stejné
    časy jako při skenování, _get_images_from_media) porovnáním uloženého ořezu se starým
    algoritmem. Přeřízne se jen tam, kde uložený ořez opravdu odpovídá starému protaženému –
    správné ořezy (nový algoritmus) i neověřitelné se nechají být. Vrací počet opravených."""
    size = _video_size(path)
    if size is None:
        return 0
    w, h = size
    todo = []
    for fid, crop_path, bx, by, bw, bh, _, media_id in media_rows:
        if None in (bx, by, bw, bh):
            continue
        # rotace videa (metadata) prohazuje strany snímku – stačí, že okraj hrozí v jedné z nich
        if not any(_is_stretched_window(fw, fh, (bx * fw, by * fh, (bx + bw) * fw, (by + bh) * fh), pad_ratio)
                   for fw, fh in ((w, h), (h, w))):
            continue
        stored = resolve_face_path(crop_path, FACE_CACHE_DIR)
        sig = _crop_signature(cv_io.imread(stored)) if stored else None
        if sig is not None:
            todo.append((fid, stored, (bx, by, bw, bh), sig, media_id))
    if not todo:
        return 0
    frames = _get_images_from_media(path)
    repaired = 0
    for fid, stored, (bx, by, bw, bh), sig, media_id in todo:
        best = None                                        # (rozdíl se starým ořezem, nový ořez)
        new_diff = float("inf")
        for frame in frames:
            fh, fw = frame.shape[:2]
            bbox = (bx * fw, by * fh, (bx + bw) * fw, (by + bh) * fh)
            old = _crop_signature(_legacy_avatar_crop(frame, bbox, target_size, pad_ratio))
            new = _extract_apple_avatar_crop(frame, tuple(int(v) for v in bbox), target_size, pad_ratio)
            if old is None or new is None:
                continue
            d_old = float(np.mean(np.abs(old - sig)))
            new_diff = min(new_diff, float(np.mean(np.abs(_crop_signature(new) - sig))))
            if best is None or d_old < best[0]:
                best = (d_old, new)
        if best is None or best[0] > CROP_MATCH_TOL or new_diff <= best[0]:
            continue                                       # už správný / snímek nenalezen
        if _write_avatar(stored, best[1]):
            repaired += 1
    return repaired


# ── Anatomická brána (Patch 11) ───────────────────────────────────────────────
# Kalibrováno na skutečné knihovně (kontaktní archy 486 obličejů): nejsilnějším signálem je
# NORMA surového ArcFace embeddingu – týl hlavy, kámen, tričko mají ~8–15, skutečné obličeje
# i ve špatném světle ~16–26. Jistota detektoru a věrohodnost bodů (oči nad nosem nad ústy,
# rozumné vzdálenosti) jsou podpůrné; samotná re-detekce na ořezu spolehlivá NENÍ (těsný
# ořez detailu obličeje detektor často nenajde).
GATE_VERSION = 1
FACE_NORM_LOW, FACE_NORM_HIGH = 12.0, 19.0
FACE_DET_LOW, FACE_DET_HIGH = 0.45, 0.80
FACE_VALID_THRESHOLD = 0.45      # pod tím = karanténa (is_valid = 0), vratné přepočtem


def landmark_plausibility(kps, bbox=None):
    """1.0 = anatomicky věrohodné rozložení 5 bodů, nižší = nesmyslná geometrie (textura).

    V souřadnicích obličeje (osa očí): ústa musí být pod očima, nos mezi očima a ústy,
    šířka úst i vzdálenost očí v lidských poměrech. Profil a natočení hlavy tolerují."""
    if kps is None:
        return 0.5
    k = np.asarray(kps, dtype=np.float64)
    if k.shape != (5, 2):
        return 0.5
    le, re, nose, ml, mr = k
    d = np.linalg.norm(re - le)
    if d < 1e-3:
        return 0.0
    ax = (re - le) / d
    perp = np.array([-ax[1], ax[0]])
    em, mm = (le + re) / 2.0, (ml + mr) / 2.0
    mouth_v = float((mm - em) @ perp) / d          # ústa pod očima (≈ 1)
    nose_v = float((nose - em) @ perp) / d         # nos mezi (≈ 0.6)
    mouth_w = float(np.linalg.norm(mr - ml)) / d   # šířka úst (≈ 0.9)
    checks = [0.45 <= mouth_v <= 2.6, 0.05 <= nose_v < mouth_v, 0.35 <= mouth_w <= 1.6]
    if bbox is not None:
        bw = max(1.0, float(bbox[2] - bbox[0]))
        checks.append(0.08 <= d / bw <= 0.75)
    return sum(checks) / len(checks)


def face_validity(embedding_norm, det_score, kps=None, bbox=None, redetected=True):
    """Skóre „je to lidský obličej“ 0..1 a důvody. det_score None = neznámé (neutrální)."""
    why = []
    s_norm = float(np.clip((float(embedding_norm or 0.0) - FACE_NORM_LOW) / (FACE_NORM_HIGH - FACE_NORM_LOW), 0, 1))
    if s_norm < 0.3:
        why.append(tr("face-why-weak-embedding"))
    if det_score is None or not redetected:
        s_det = 0.5
    else:
        s_det = float(np.clip((float(det_score) - FACE_DET_LOW) / (FACE_DET_HIGH - FACE_DET_LOW), 0, 1))
        if s_det < 0.3:
            why.append(tr("face-why-low-confidence"))
    s_geo = landmark_plausibility(kps, bbox) if (kps is not None and redetected) else 0.75
    if s_geo < 0.75:
        why.append(tr("face-why-landmarks"))
    score = 0.60 * s_norm + 0.25 * s_det + 0.15 * s_geo
    return float(score), why


FRONTAL_MAX_OFFSET = 0.5     # posun nosu od středu očí (v násobcích jejich vzdálenosti) = profil
CROWD_MIN_RELATIVE = 0.35    # jiný obličej aspoň takto velký v ořezu = „portrét se dvěma lidmi“


def frontality_from_kps(kps):
    """Jak moc je obličej natočený čelně: 1 = přímo do objektivu, 0 = profil.

    Z 5 bodů InsightFace (oči, nos, koutky úst): u čelního pohledu leží nos (a střed úst)
    uprostřed mezi očima; při natočení hlavy se posouvají k jednomu oku."""
    if kps is None or len(kps) < 3:
        return None
    k = np.asarray(kps, dtype=np.float64)
    le, re, nose = k[0], k[1], k[2]
    d = np.linalg.norm(re - le)
    if d < 1e-3:
        return 0.0
    axis = (re - le) / d
    eye_mid = (le + re) / 2.0
    offsets = [abs(float(np.dot(nose - eye_mid, axis))) / d]
    if len(k) >= 5:
        offsets.append(abs(float(np.dot((k[3] + k[4]) / 2.0 - eye_mid, axis))) / d)
    return float(max(0.0, 1.0 - max(offsets) / FRONTAL_MAX_OFFSET))


def _box_size(face):
    b = face.bbox
    return max(float(b[2] - b[0]), float(b[3] - b[1]))


def _crowded(face, others, window):
    """Je ve výřezu portrétu ještě jiný výrazný obličej?"""
    x, y, side = window
    main_size = _box_size(face)
    for other in others:
        if other is face or float(getattr(other, "det_score", 1.0)) < 0.5:
            continue
        b = other.bbox
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        if x <= cx <= x + side and y <= cy <= y + side and _box_size(other) >= CROWD_MIN_RELATIVE * main_size:
            return 1
    return 0


_PENDING_ANALYSIS_SQL = "frontality IS NULL OR COALESCE(gate_version, 0) < ?"


def count_faces_without_frontality():
    """Obličeje čekající na analýzu ořezu (natočení / anatomická brána)."""
    conn = get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM face_instances WHERE {_PENDING_ANALYSIS_SQL}",
                            (GATE_VERSION,)).fetchone()[0]
    finally:
        conn.close()


def analyze_face_crops(should_stop=None, batch=50):
    """Jednorázově zanalyzuje uložené ořezy starších obličejů (Patch 10 + 11).

    Detektor se pustí na ořez: nejbližší obličej ke středu dá natočení (frontality) a další
    výrazné obličeje = portrét s více lidmi (crowded). Z normy uloženého embeddingu, jistoty
    re-detekce a věrohodnosti bodů se spočítá anatomická brána -> is_valid (karanténa = 0).
    Žádný obličej v ořezu = frontality 0 (o platnosti pak rozhoduje hlavně norma – těsný ořez
    detailu detektor často nenajde). Vrací počet zpracovaných obličejů."""
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT id, face_crop_path, embedding FROM face_instances "
                            f"WHERE {_PENDING_ANALYSIS_SQL}", (GATE_VERSION,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    app = _get_app()
    done, pending = 0, []

    def flush():
        if pending:
            c = get_connection()
            try:
                c.executemany("UPDATE face_instances SET frontality = ?, crowded = ?, is_valid = ?, face_score = ?, "
                              "det_score = COALESCE(det_score, ?), gate_version = ? WHERE id = ?", pending)
                c.commit()
            finally:
                c.close()
            pending.clear()

    for fid, crop, blob in rows:
        if should_stop and should_stop():
            break
        norm = float(np.linalg.norm(np.frombuffer(bytes(blob), dtype=np.float32))) if blob else 0.0
        path = resolve_face_path(crop)
        img = cv_io.imread(path) if path else None
        front, crowded, det, kps, bbox, found = 0.0, 0, None, None, None, False
        if img is not None and img.size > 0:
            h, w = img.shape[:2]
            faces = app.get(img)
            if faces:
                centre = np.array([w / 2.0, h / 2.0])
                main = min(faces, key=lambda f: np.linalg.norm(
                    np.array([(f.bbox[0] + f.bbox[2]) / 2.0, (f.bbox[1] + f.bbox[3]) / 2.0]) - centre))
                f = frontality_from_kps(getattr(main, "kps", None))
                front = f if f is not None else 0.0
                crowded = _crowded(main, faces, (0, 0, max(w, h)))
                det, kps, bbox, found = float(getattr(main, "det_score", 0.0)), main.kps, main.bbox, True
        score, _ = face_validity(norm, det, kps, bbox, redetected=found)
        pending.append((front, crowded, 1 if score >= FACE_VALID_THRESHOLD else 0, score, det, GATE_VERSION, fid))
        done += 1
        if len(pending) >= batch:
            flush()
    flush()
    return done


def _oriented_size(path):
    """Rozměry fotky po aplikaci EXIF orientace – jen z hlavičky (bez dekódování pixelů)."""
    with Image.open(path) as im:
        w, h = im.size
        if im.getexif().get(274) in (5, 6, 7, 8):
            w, h = h, w
    return w, h


def repair_stretched_face_crops(should_stop=None, target_size=AVATAR_PX, pad_ratio=0.50):
    """Jednorázově přeřízne deformované ořezy (CROP_VERSION < 2) z originálních fotek.

    Kontroluje jen hlavičky fotek; originál dekóduje jen tam, kde byl ořez opravdu
    deformovaný. Obličeje z videí: snímek se najde znovu (_repair_video_faces).
    Vrací počet opravených ořezů."""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT f.id, f.face_crop_path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, m.file_path, f.media_id
            FROM face_instances f JOIN media_items m ON m.id = f.media_id
            WHERE COALESCE(f.crop_version, 1) < ?
            ORDER BY f.media_id
        """, (CROP_VERSION,)).fetchall()
    finally:
        conn.close()

    repaired, checked = 0, []
    by_media = {}
    for row in rows:
        by_media.setdefault(row[7], []).append(row)
    for media_rows in by_media.values():
        if should_stop and should_stop():
            break
        path = media_rows[0][6]
        ext = os.path.splitext(path or "")[1].lower()
        if not path or not os.path.exists(path):
            checked.extend(r[0] for r in media_rows)
            continue
        if ext in VIDEO_EXTS:
            try:
                repaired += _repair_video_faces(path, media_rows, target_size, pad_ratio)
            except Exception as e:
                print(f"[FaceCrops] video {os.path.basename(path)}: {e}")
            checked.extend(r[0] for r in media_rows)
            continue
        try:
            w, h = _oriented_size(path)
        except Exception:
            checked.extend(r[0] for r in media_rows)
            continue
        img_bgr = None
        for fid, crop_path, bx, by, bw, bh, _, media_id in media_rows:
            checked.append(fid)
            if None in (bx, by, bw, bh):
                continue
            bbox = (bx * w, by * h, (bx + bw) * w, (by + bh) * h)
            if not _is_stretched_window(w, h, bbox, pad_ratio):
                continue
            if img_bgr is None:
                try:
                    with Image.open(path) as pil_img:
                        pil_img = ImageOps.exif_transpose(pil_img)
                        img_bgr = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
                except Exception:
                    break
            avatar = _extract_apple_avatar_crop(img_bgr, tuple(int(v) for v in bbox), target_size, pad_ratio)
            if avatar is None:
                continue
            target = resolve_face_path(crop_path, FACE_CACHE_DIR) or os.path.join(
                FACE_CACHE_DIR, f"face_{media_id}_rec_{fid}.jpg")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if _write_avatar(target, _avatar_to_output(avatar, path)):
                repaired += 1
                if target != crop_path:
                    conn = get_connection()
                    try:
                        conn.execute("UPDATE face_instances SET face_crop_path = ? WHERE id = ?", (target, fid))
                        conn.execute("UPDATE people SET cover_face_path = ? WHERE key_face_id = ?", (target, fid))
                        conn.commit()
                    finally:
                        conn.close()
    if checked:
        conn = get_connection()
        try:
            conn.executemany("UPDATE face_instances SET crop_version = ? WHERE id = ?",
                             [(CROP_VERSION, fid) for fid in checked])
            conn.commit()
        finally:
            conn.close()
    return repaired


def reconstruct_face_crop(face_instance_id, target_size=AVATAR_PX, pad_ratio=0.50):
    """
    PATCH 2.1 – Lazy rekonstrukce ořezu obličeje z původní fotky.

    Pokud soubor ořezu chybí na disku (smazaná cache), vyřízne obličej znovu
    z originální fotografie podle souřadnic uložených v databázi a zapíše ho
    zpět na původní cestu (nebo vygeneruje novou, pokud cesta v DB chybí).

    Vrací cestu k ořezu (str) nebo None při selhání.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT fi.face_crop_path, fi.bbox_x, fi.bbox_y, fi.bbox_w, fi.bbox_h,
                   m.file_path, fi.media_id
            FROM face_instances fi
            JOIN media_items m ON fi.media_id = m.id
            WHERE fi.id = ?
        """, (face_instance_id,))
        row = c.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    crop_path, bx, by, bw, bh, file_path, media_id = row

    # Ořez existuje (případně po přesunu složky aplikace) → není třeba rekonstruovat
    existing = resolve_face_path(crop_path, FACE_CACHE_DIR)
    if existing:
        return existing

    # Nemáme souřadnice → nelze rekonstruovat
    if None in (bx, by, bw, bh) or not file_path or not os.path.exists(file_path):
        return None

    # Načteme originál (jen fotky, ne videa)
    ext = os.path.splitext(file_path)[1].lower()
    if ext in {'.mov', '.mp4'}:
        return None

    try:
        from PIL import Image, ImageOps
        with Image.open(file_path) as pil_img:
            pil_img = ImageOps.exif_transpose(pil_img)
            img_rgb = pil_img.convert("RGB")
            img_arr = np.array(img_rgb)
            img_bgr = cv2.cvtColor(img_arr, cv2.COLOR_RGB2BGR)
    except Exception:
        img_bgr = cv_io.imread(file_path)

    if img_bgr is None or img_bgr.size == 0:
        return None

    h, w = img_bgr.shape[:2]
    # Souřadnice jsou normalizované (0–1) → převod na pixely
    x1 = int(bx * w)
    y1 = int(by * h)
    x2 = int((bx + bw) * w)
    y2 = int((by + bh) * h)

    avatar = _extract_apple_avatar_crop(img_bgr, (x1, y1, x2, y2),
                                        target_size=target_size, pad_ratio=pad_ratio)
    if avatar is None:
        return None

    # Cesta: použijeme původní, nebo vygenerujeme novou (i když původní složka už neexistuje,
    # jinak by se po přesunu aplikace znovu vytvořila stará složka mimo FACE_CACHE_DIR)
    if not crop_path or not os.path.isdir(os.path.dirname(crop_path)):
        crop_filename = f"face_{media_id}_rec_{face_instance_id}.jpg"
        crop_path = os.path.join(FACE_CACHE_DIR, crop_filename)

    os.makedirs(os.path.dirname(crop_path), exist_ok=True)
    if not _write_avatar(crop_path, _avatar_to_output(avatar, file_path)):
        return None

    # Aktualizujeme cestu v DB (pokud se změnila nebo chyběla)
    conn2 = get_connection()
    try:
        c2 = conn2.cursor()
        c2.execute("UPDATE face_instances SET face_crop_path = ? WHERE id = ?",
                   (crop_path, face_instance_id))
        # Pokud je tato instance klíčovým obličejem osoby, aktualizujeme i cover_face_path
        c2.execute("""
            UPDATE people SET cover_face_path = ?
            WHERE key_face_id = ? AND (cover_face_path IS NULL OR cover_face_path != ?)
        """, (crop_path, face_instance_id, crop_path))
        conn2.commit()
    finally:
        conn2.close()

    return crop_path


def reconstruct_best_crop_for_person(person_id, target_size=AVATAR_PX, pad_ratio=0.50):
    """
    PATCH 2.1 – Najde nejkvalitnější obličej osoby a rekonstruuje jeho ořez.

    Vrací cestu k ořezu (str) nebo None.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT fi.id, fi.face_crop_path
            FROM face_instances fi
            WHERE fi.person_id = ?
            ORDER BY COALESCE(fi.quality_score, 0) DESC
        """, (person_id,))
        instances = c.fetchall()
    finally:
        conn.close()

    for fi_id, crop_path in instances:
        existing = resolve_face_path(crop_path, FACE_CACHE_DIR)
        if existing:
            return existing
        # Zkusíme rekonstruovat
        result = reconstruct_face_crop(fi_id, target_size=target_size, pad_ratio=pad_ratio)
        if result:
            return result

    return None


def detect_and_extract_faces(media_id, image_path, preloaded_image=None):
    """
    Detekuje obličeje, spočítá komplexní skóre kvality, uloží 256x256 Apple avatar s 50% okrajem
    a extrahuje normalizovaný 512d biometrický vektor.

    PATCH 2.3 – Decode Once: pokud volající předá ``preloaded_image`` (BGR ndarray),
    originál se nečte z disku znovu. Platí jen pro fotky (ne videa).
    """
    if not image_path or not os.path.exists(image_path):
        return []

    if preloaded_image is not None and isinstance(preloaded_image, np.ndarray) and preloaded_image.size > 0:
        images = [preloaded_image]
    else:
        images = _get_images_from_media(image_path)
    if not images:
        return []

    raw_detections = []
    detection_errors = 0

    for img in images:
        h, w = img.shape[:2]
        try:
            faces = _get_app().get(img)
        except Exception as e:
            detection_errors += 1
            print(f"[Face] Detekce selhala ({os.path.basename(image_path)}): {e}")
            continue

        if not faces:
            continue

        for face in faces:
            if not _is_valid_face_geometry(face):
                continue

            bbox = face.bbox.astype(int)
            x1, y1, x2, y2 = max(0, bbox[0]), max(0, bbox[1]), min(w, bbox[2]), min(h, bbox[3])
            bw = x2 - x1
            bh = y2 - y1
            if bw < 24 or bh < 24:
                continue

            try:
                aligned_face = _face_align.norm_crop(img, face.kps, image_size=224)
            except Exception:
                continue

            if aligned_face is None or aligned_face.size == 0:
                continue

            sharpness = get_image_sharpness(aligned_face)
            if sharpness < 18.0:
                continue

            det_score = float(getattr(face, 'det_score', 0.8))
            quality_score = float(sharpness * det_score)

            # Uložíme estetický portrét s 50% paddingem (celá hlava a krk)
            avatar_crop = _extract_apple_avatar_crop(img, (x1, y1, x2, y2), target_size=AVATAR_PX, pad_ratio=0.50)
            if avatar_crop is None:
                avatar_crop = aligned_face

            crop_filename = f"face_{media_id}_{int(time.time())}_{np.random.randint(1000,9999)}.jpg"
            crop_path = os.path.join(FACE_CACHE_DIR, crop_filename)
            if not _write_avatar(crop_path, _avatar_to_output(avatar_crop, image_path)):
                # cv2.imwrite by tady selhal TIŠE (např. diakritika v cestě) a v DB by zůstala
                # cesta k neexistujícímu souboru. Raději hlasitě selhat - fotka se zkusí příště.
                _discard_crops(raw_detections)
                raise OSError(f'Couldn’t write the face crop: {crop_path}')

            embedding = face.embedding
            if embedding is None:
                continue

            # Podklad pro výběr portrétu osoby: natočení hlavy + další obličej ve výřezu
            window = square_crop_window(w, h, (x1, y1, x2, y2), 0.50)
            frontality = frontality_from_kps(face.kps)
            crowded = _crowded(face, faces, window) if window else 0
            # Anatomická brána: textura / týl hlavy jde rovnou do karantény (is_valid = 0)
            face_score, _ = face_validity(float(np.linalg.norm(embedding)), det_score, face.kps, face.bbox)

            bbox_str = f"{x1},{y1},{bw},{bh}"
            raw_detections.append({
                "media_id": media_id,
                "crop_path": crop_path,
                "embedding": embedding.astype(np.float32),
                "sharpness": sharpness,
                "quality_score": quality_score,
                "crop_version": CROP_VERSION,
                "frontality": frontality,
                "crowded": crowded,
                "is_valid": 1 if face_score >= FACE_VALID_THRESHOLD else 0,
                "face_score": face_score,
                "det_score": det_score,
                "gate_version": GATE_VERSION,
                "bbox": bbox_str,
                "bbox_x": float(x1) / max(1, w),
                "bbox_y": float(y1) / max(1, h),
                "bbox_w": float(bw) / max(1, w),
                "bbox_h": float(bh) / max(1, h)
            })

    if not raw_detections:
        if detection_errors and detection_errors == len(images):
            # Nic se nepodařilo zpracovat -> NE "žádné tváře" (fotka se nesmí označit za hotovou)
            raise RuntimeError('Face detection failed for every image')
        return []

    unique_people_in_media = []
    for det in raw_detections:
        vec_curr = det["embedding"]
        vec_curr = vec_curr / (np.linalg.norm(vec_curr) + 1e-7)

        matched_group = None
        for group in unique_people_in_media:
            sim = float(np.dot(vec_curr, group["rep_vec"]))
            if sim >= 0.48:
                matched_group = group
                break

        if matched_group is not None:
            matched_group["detections"].append(det)
            if det["quality_score"] > matched_group["best_det"]["quality_score"]:
                matched_group["best_det"] = det
        else:
            unique_people_in_media.append({
                "rep_vec": vec_curr,
                "best_det": det,
                "detections": [det]
            })

    # Ořezy detekcí, které dedup zahodil, by zůstaly na disku navždy jako sirotci.
    kept_crops = {g["best_det"]["crop_path"] for g in unique_people_in_media}
    _discard_crops([d for d in raw_detections if d["crop_path"] not in kept_crops])

    final_results = []
    for group in unique_people_in_media:
        best = group["best_det"]
        final_results.append({
            "media_id": best["media_id"],
            "crop_path": best["crop_path"],
            "embedding": sqlite3.Binary(best["embedding"].tobytes()),
            "sharpness": best["sharpness"],
            "quality_score": best["quality_score"],
            "bbox": best["bbox"],
            "bbox_x": best["bbox_x"],
            "bbox_y": best["bbox_y"],
            "bbox_w": best["bbox_w"],
            "bbox_h": best["bbox_h"]
        })

    return final_results


def get_image_sharpness(image_input):
    from pathlib import Path
    if isinstance(image_input, (str, Path)):
        if not image_input or not os.path.exists(image_input):
            return -1.0
        img = cv_io.imread(str(image_input), cv2.IMREAD_GRAYSCALE)
    elif isinstance(image_input, np.ndarray):
        img = image_input
        if img.size == 0:
            return -1.0
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        return -1.0

    if img is None or img.size == 0:
        return -1.0
    return cv2.Laplacian(img, cv2.CV_64F).var()


def update_person_key_photo(person_id, forced_face_id=None):
    """
    Aktualizuje klíčový portrét osoby. Pokud uživatel zvolil konkrétní fotku (forced_face_id),
    použije ji přednostně; jinak vybere nejostřejší portrét.
    """
    conn = get_connection()
    c = conn.cursor()

    if forced_face_id is not None:
        c.execute("SELECT face_crop_path FROM face_instances WHERE id = ?", (forced_face_id,))
        row = c.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            # ruční volba uživatele = zamčený portrét (automatický výběr ho nepřepíše)
            c.execute(
                "UPDATE people SET cover_face_path = ?, key_face_id = ?, cover_locked = 1 WHERE id = ?",
                (row[0], forced_face_id, person_id)
            )
            conn.commit()
            conn.close()
            return
    conn.close()

    # Patch 10: nejtypičtější ostrý čelní obličej (podobnost s průměrnou tváří osoby,
    # kvalita, velikost) – ne prostě nejostřejší, který bývá i profil nebo týl hlavy
    from face_cluster import refresh_person_covers
    refresh_person_covers([person_id])


def get_borderline_faces_for_person(person_id, min_sim=0.32, max_sim=0.47):
    """
    MODUL 1.1 & 2.2: Najde dosud nepřiřazené tváře v databázi, které se podobají dané osobě
    v hraničním pásmu jistoty, pro dialog 'Zkontrolovat další fotky (Je toto stejná osoba?)'.
    """
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT embedding FROM face_instances WHERE person_id = ?", (person_id,))
    person_embs = [np.frombuffer(r[0], dtype=np.float32) for r in c.fetchall() if r[0]]
    if not person_embs:
        conn.close()
        return []

    arr = np.array(person_embs)
    centroid = np.mean(arr, axis=0)
    centroid = centroid / (np.linalg.norm(centroid) + 1e-7)

    c.execute("""
        SELECT f.id, f.media_id, f.face_crop_path, f.embedding, m.file_name, m.thumbnail_path
        FROM face_instances f
        JOIN media_items m ON f.media_id = m.id
        WHERE f.person_id IS NULL AND f.face_crop_path IS NOT NULL
    """)
    unassigned = c.fetchall()
    conn.close()

    candidates = []
    for f_id, m_id, crop_path, emb_blob, file_name, thumb_path in unassigned:
        if not emb_blob or not os.path.exists(crop_path):
            continue
        vec = np.frombuffer(emb_blob, dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) + 1e-7)
        sim = float(np.dot(vec, centroid))
        if min_sim <= sim <= max_sim:
            candidates.append({
                "face_id": f_id,
                "media_id": m_id,
                "crop_path": crop_path,
                "similarity": sim,
                "file_name": file_name,
                "thumb_path": thumb_path
            })

    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates


def cluster_faces_into_people(match_threshold=0.42):
    conn = get_connection()
    c = conn.cursor()

    c.execute("SELECT f.id, f.embedding, f.face_crop_path FROM face_instances f WHERE f.person_id IS NULL")
    unassigned = c.fetchall()

    if not unassigned:
        conn.close()
        return

    c.execute("SELECT p.id, f.embedding FROM people p JOIN face_instances f ON p.id = f.person_id")
    assigned_rows = c.fetchall()

    clusters = {}
    centroids = {}

    for p_id, emb_blob in assigned_rows:
        if p_id not in clusters:
            clusters[p_id] = []
        clusters[p_id].append(np.frombuffer(emb_blob, dtype=np.float32))

    for p_id, embs in clusters.items():
        arr = np.array(embs)
        centroid = np.mean(arr, axis=0)
        centroids[p_id] = centroid / (np.linalg.norm(centroid) + 1e-7)

    remaining_unassigned = []
    for face_id, emb_blob, crop_path in unassigned:
        vec = np.frombuffer(emb_blob, dtype=np.float32)
        vec = vec / (np.linalg.norm(vec) + 1e-7)

        best_person_id = None
        best_sim = -1.0

        for p_id, centroid in centroids.items():
            sim = float(np.dot(vec, centroid))
            if sim > best_sim:
                best_sim = sim
                best_person_id = p_id

        if best_sim >= match_threshold and best_person_id is not None:
            c.execute("UPDATE face_instances SET person_id = ? WHERE id = ?", (best_person_id, face_id))
            clusters[best_person_id].append(vec)
            arr = np.array(clusters[best_person_id])
            new_centroid = np.mean(arr, axis=0)
            centroids[best_person_id] = new_centroid / (np.linalg.norm(new_centroid) + 1e-7)
        else:
            remaining_unassigned.append((face_id, vec, crop_path))

    if remaining_unassigned:
        n = len(remaining_unassigned)
        adj = {i: [] for i in range(n)}
        vecs = [item[1] for item in remaining_unassigned]

        for i in range(n):
            for j in range(i + 1, n):
                sim = float(np.dot(vecs[i], vecs[j]))
                if sim >= 0.48:
                    adj[i].append(j)
                    adj[j].append(i)

        visited = set()
        for i in range(n):
            if i not in visited:
                comp = []
                queue = [i]
                while queue:
                    curr = queue.pop(0)
                    if curr not in visited:
                        visited.add(curr)
                        comp.append(curr)
                        queue.extend([neighbor for neighbor in adj[curr] if neighbor not in visited])

                if len(comp) >= 2:
                    c.execute(
                        "INSERT INTO people (name, cover_face_path) VALUES (?, ?)",
                        # i18n-ok: SQL default person name
                        ("Neznámá osoba", remaining_unassigned[comp[0]][2])
                    )
                    new_p_id = c.lastrowid
                    for idx in comp:
                        f_id, _, _ = remaining_unassigned[idx]
                        c.execute("UPDATE face_instances SET person_id = ? WHERE id = ?", (new_p_id, f_id))
                    conn.commit()
                    update_person_key_photo(new_p_id)

    conn.commit()
    conn.close()
    for p_id in clusters.keys():
        update_person_key_photo(p_id)