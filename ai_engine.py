import io
import math
import os
import pickle
import threading
import cv2
import numpy as np
from PIL import Image
from datetime import datetime

import cv_io

try:
    import faiss
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

TEXT_MODEL_NAME = "sentence-transformers/clip-ViT-B-32-multilingual-v1"
IMAGE_MODEL_NAME = "sentence-transformers/clip-ViT-B-32"

EMBED_DIM = 512                 # CLIP ViT-B/32
EMBED_BYTES = EMBED_DIM * 4     # float32

_text_model = None
_image_model = None
_model_lock = threading.Lock()
_index_lock = threading.RLock()

# Paměťová cache pro okamžité vektorové vyhledávání (< 2 ms)
_cached_index_state = {
    "count": -1,
    "media_ids": [],
    "matrix": None,
    "faiss_index": None
}


# ==============================================================================
# BEZPEČNÝ FORMÁT EMBEDDINGŮ: surový little-endian float32 (512 * 4 = 2048 B)
# Dřív pickle.dumps(); pickle.loads() na datech z databáze umožňuje spuštění cizího
# kódu, pokud někdo DB podstrčí. Staré záznamy se čtou přes omezený unpickler.
# ==============================================================================

def encode_embedding(vec):
    """ndarray -> bytes (float32, normalizovaný, little-endian)."""
    arr = np.asarray(vec, dtype=np.float32).flatten()
    if arr.size != EMBED_DIM:
        raise ValueError(f"Embedding má {arr.size} dimenzí, očekáváno {EMBED_DIM}")
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = arr / norm
    return arr.astype("<f4").tobytes()


_ALLOWED_PICKLE_GLOBALS = {
    ("numpy", "ndarray"), ("numpy", "dtype"),
    ("numpy.core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"), ("numpy._core.numeric", "_frombuffer"),
}


class _RestrictedUnpickler(pickle.Unpickler):
    """Povolí jen rekonstrukci numpy pole; cokoli jiného (os.system, ...) odmítne."""
    def find_class(self, module, name):
        if (module, name) in _ALLOWED_PICKLE_GLOBALS:
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"Zakázaný objekt v pickle: {module}.{name}")


def decode_embedding(raw):
    """bytes -> float32 ndarray (EMBED_DIM,), nebo None při neplatných datech."""
    if not raw:
        return None
    try:
        raw = bytes(raw)
        if len(raw) == EMBED_BYTES:
            vec = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        elif raw[:1] == b"\x80":   # starý formát (pickle)
            vec = _RestrictedUnpickler(io.BytesIO(raw)).load()
            vec = np.asarray(vec, dtype=np.float32).flatten()
        else:
            return None
        if vec.size != EMBED_DIM or not np.all(np.isfinite(vec)):
            return None
        return vec
    except Exception:
        return None


def migrate_legacy_embeddings():
    """Jednorázově převede staré pickle záznamy na float32 bytes.
    Nečitelné / podezřelé záznamy smaže (přegenerují se). Vrací (převedeno, smazáno)."""
    from database import get_connection
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT media_id, embedding FROM image_embeddings WHERE length(embedding) != ?",
                  (EMBED_BYTES,))
        rows = c.fetchall()
        if not rows:
            return 0, 0
        updates, deletes = [], []
        for media_id, raw in rows:
            vec = decode_embedding(raw)
            if vec is None:
                deletes.append((media_id,))
            else:
                updates.append((encode_embedding(vec), media_id))
        if updates:
            c.executemany("UPDATE image_embeddings SET embedding = ? WHERE media_id = ?", updates)
        if deletes:
            c.executemany("DELETE FROM image_embeddings WHERE media_id = ?", deletes)
        conn.commit()
        return len(updates), len(deletes)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[AI Chyba] Migrace embeddingů selhala: {e}")
        return 0, 0
    finally:
        conn.close()


def get_text_model():
    """Textový model (vyhledávání) - načítá se samostatně a jen jednou."""
    global _text_model
    with _model_lock:
        if _text_model is None:
            from sentence_transformers import SentenceTransformer
            _text_model = SentenceTransformer(TEXT_MODEL_NAME)
        return _text_model


def get_image_model():
    """Obrazový model (výpočet embeddingů) - načítá se samostatně a jen jednou."""
    global _image_model
    with _model_lock:
        if _image_model is None:
            from sentence_transformers import SentenceTransformer
            _image_model = SentenceTransformer(IMAGE_MODEL_NAME)
        return _image_model


def get_models():
    """Zpětná kompatibilita: vrací (text_model, image_model)."""
    return get_text_model(), get_image_model()


def compute_image_embedding(image_path):
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        img_model = get_image_model()
        with Image.open(image_path) as img:
            img_rgb = img.convert("RGB")
            embedding = img_model.encode(img_rgb, convert_to_numpy=True, show_progress_bar=False)
            return encode_embedding(embedding)
    except Exception as e:
        print(f"[AI Chyba] Selhal výpočet vektoru pro {image_path}: {e}")
        return None


def _rebuild_vector_index(photo_embeddings_dict):
    """Sestaví normalizovanou matici (a FAISS IndexFlatIP, pokud je dostupný) v RAM."""
    global _cached_index_state
    media_ids = []
    vectors = []

    for m_id, raw_bytes in photo_embeddings_dict.items():
        vec = decode_embedding(raw_bytes)
        if vec is None:
            continue
        norm = np.linalg.norm(vec)
        if norm > 0:
            media_ids.append(int(m_id))
            vectors.append(vec / norm)

    if not vectors:
        _cached_index_state = {"count": 0, "media_ids": [], "matrix": None, "faiss_index": None}
        return

    matrix = np.vstack(vectors).astype(np.float32)
    faiss_idx = None
    if HAS_FAISS:
        try:
            dim = matrix.shape[1]
            faiss_idx = faiss.IndexFlatIP(dim)
            faiss_idx.add(matrix)
        except Exception:
            faiss_idx = None

    _cached_index_state = {
        "count": len(photo_embeddings_dict),
        "media_ids": media_ids,
        "matrix": matrix,
        "faiss_index": faiss_idx
    }


# ZÁLOHA (bez banky pojmů – viz select_calibrated níže, který se používá přednostně).
# Práh relevance CLIP (text multilingual-v1 × obraz ViT-B-32). Kosinová podobnost textu a fotky
# leží pro celou knihovnu v úzkém pásmu ~0,15–0,32: nesouvisející fotky mají ~0,18–0,22, skutečné
# shody ~0,25+. Starý práh (0,18 a 75 % nejlepší) proto pouštěl skoro celou knihovnu.
# Fotka projde, jen když splní VŠECHNY podmínky:
#   1) absolutní minimum SEARCH_ABS_MIN,
#   2) vyčnívá z rozložení skóre dotazu v knihovně (průměr + SEARCH_Z_STRICT × směrodatná odchylka),
#   3) je v pásmu SEARCH_TOP_BAND od nejlepší shody.
# Adaptivní záloha: když přísné podmínky nesplní nic, ale nejlepší fotky výrazně vyčnívají
# (z ≥ SEARCH_Z_FALLBACK nad SEARCH_SOFT_MIN), vrátí se těch pár nejlepších. Jinak prázdný výsledek.
SEARCH_ABS_MIN = 0.235
SEARCH_SOFT_MIN = 0.215
SEARCH_Z_STRICT = 1.6
SEARCH_Z_FALLBACK = 2.6
SEARCH_TOP_BAND = 0.045
SEARCH_FALLBACK_MAX = 12
SEARCH_MIN_STATS = 24          # pod tolik fotkami je rozptyl nespolehlivý -> jen absolutní práh a pásmo


def select_relevant(scores, abs_min=SEARCH_ABS_MIN, soft_min=SEARCH_SOFT_MIN, z_strict=SEARCH_Z_STRICT,
                    z_fallback=SEARCH_Z_FALLBACK, top_band=SEARCH_TOP_BAND, fallback_max=SEARCH_FALLBACK_MAX):
    """Indexy relevantních skóre od nejlepšího (viz komentář u SEARCH_ABS_MIN)."""
    scores = np.asarray(scores, dtype=np.float32)
    if scores.size == 0 or not np.all(np.isfinite(scores)):
        return []
    order = np.argsort(-scores, kind="stable")
    top = float(scores[order[0]])
    use_stats = scores.size >= SEARCH_MIN_STATS
    mu = float(scores.mean()) if use_stats else 0.0
    sd = float(scores.std()) if use_stats else 0.0
    if use_stats and sd < 1e-6:
        return []                                   # všechny fotky stejně (ne)podobné = nic nevyčnívá
    cutoff = max(abs_min, top - top_band)
    if use_stats:
        cutoff = max(cutoff, mu + z_strict * sd)
    hits = [int(i) for i in order if scores[i] >= cutoff]
    if hits:
        return hits
    # záloha jen s důkazem z rozložení – v malé knihovně nejde poznat, zda 0,22 „vyčnívá“
    if not use_stats or top < soft_min or (top - mu) / sd < z_fallback:
        return []
    soft_cut = max(soft_min, top - top_band / 2, mu + z_fallback * sd)
    return [int(i) for i in order[:fallback_max] if scores[i] >= soft_cut]


# ── Kalibrace na obraz (hubness) ──────────────────────────────────────────────
# Změřeno na skutečné knihovně (277 fotek z výletu): globální práh nemůže fungovat. Některé fotky
# („huby“ – např. jezero s nebem) mají vysokou podobnost s ČÍMKOLI; „pes“, „letadlo“, „slon“
# i „les“ končí se skóre 0,24–0,26 a z knihovny vyčnívají stejně (z ≈ 2,3–3,7). Starý výběr
# proto vracel 5–17 fotek i pro pojmy, které v knihovně vůbec nejsou.
# Řešení: každou fotku porovnat s ní samotnou. Pro fotku se spočte rozložení podobností k bance
# běžných pojmů (CONCEPT_BANK); dotaz je shoda, jen když z tohoto rozložení vyčnívá
# (z_obraz ≥ SEARCH_Z_IMAGE) – hub má vysoké skóre ke všem pojmům, takže ho „pes“ nepřeskočí.
# Navíc musí být nad průměrem knihovny (SEARCH_Z_LIBRARY) a nejlepší shoda musí být jistá
# (SEARCH_Z_IMAGE_TOP), jinak je výsledek PRÁZDNÝ. Výsledek se řadí podle z_obraz.
# Na knihovně: nepřítomné pojmy 19/19 -> 4/19 s výsledkem, přítomné nalezeny 13/16.
CONCEPT_BANK = (
    "pes kočka kůň kráva pták ryba hmyz auto autobus vlak letadlo loď kolo motorka silnice most "
    "budova dům město ulice kostel hrad zámek věž zeď okno dveře pokoj kuchyně postel stůl židle "
    "počítač telefon televize kniha papír jídlo pizza dort ovoce zelenina nápoj káva pivo víno "
    "člověk muž žena dítě dav obličej ruka noha oblečení boty hora hory kopec skála kámen písek "
    "pláž moře jezero řeka voda vodopád les strom tráva louka květiny zahrada park pole nebe mraky "
    "slunce noc sníh led déšť oheň světlo tma fotbal sport hudba koncert oslava "
    "obraz socha muzeum obchod restaurace kancelář škola nemocnice letiště nádraží stan "
    "dřevo kov sklo plast hračka zbraň peníze text dokument").split() + ["západ slunce", "snímek obrazovky"]
SEARCH_Z_IMAGE = 2.5
SEARCH_Z_IMAGE_TOP = 2.7
SEARCH_Z_LIBRARY = 1.0
SEARCH_RAW_FLOOR = 0.20
# Kalibrováno na 277 fotkách. Ve větší knihovně překročí pevný práh náhodou víc fotek
# (maximum šumu roste s počtem) -> prahy mírně rostou s log10 velikosti (10 000 fotek: +0,38).
SEARCH_CALIBRATED_N = 300
SEARCH_Z_PER_DECADE = 0.25

_bank_cache = {"model": None, "vectors": None}


def concept_bank_vectors(text_model):
    """Normalizované embeddingy banky pojmů (jednou na textový model)."""
    if _bank_cache["model"] is not text_model or _bank_cache["vectors"] is None:
        vecs = np.asarray(text_model.encode(list(CONCEPT_BANK), convert_to_numpy=True,
                                            show_progress_bar=False), dtype=np.float32)
        if vecs.ndim != 2 or vecs.shape[0] != len(CONCEPT_BANK):
            vecs = None                         # mock / jiný model: kalibrace nejde -> záloha
        else:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            vecs = vecs / np.where(norms > 0, norms, 1.0)
        _bank_cache["model"], _bank_cache["vectors"] = text_model, vecs
    return _bank_cache["vectors"]


def image_concept_stats(matrix, bank):
    """Průměr a směrodatná odchylka podobnosti každé fotky k bance pojmů, nebo None."""
    if bank is None or matrix is None or bank.shape[1] != matrix.shape[1]:
        return None
    sims = matrix @ bank.T
    mu, sd = sims.mean(axis=1), sims.std(axis=1)
    if not np.all(np.isfinite(sd)) or float(sd.min()) < 1e-6:
        return None                             # degenerovaná banka (všechny pojmy stejné)
    return mu.astype(np.float32), sd.astype(np.float32)


def select_calibrated(scores, img_mu, img_sd, z_image=SEARCH_Z_IMAGE, z_top=SEARCH_Z_IMAGE_TOP,
                      z_library=SEARCH_Z_LIBRARY, raw_floor=SEARCH_RAW_FLOOR):
    """Indexy shod podle z-skóre vůči vlastní bance pojmů fotky (od nejjistější); prázdný
    seznam, když ani nejlepší kandidát nedosáhne z_top."""
    scores = np.asarray(scores, dtype=np.float32)
    if scores.size == 0 or not np.all(np.isfinite(scores)):
        return []
    grow = SEARCH_Z_PER_DECADE * max(0.0, math.log10(scores.size / SEARCH_CALIBRATED_N))
    z_image, z_top = z_image + grow, z_top + grow
    zi = (scores - img_mu) / img_sd
    ok = (zi >= z_image) & (scores >= raw_floor)
    if scores.size >= SEARCH_MIN_STATS:
        sd = float(scores.std())
        if sd < 1e-6:
            return []
        ok &= (scores - float(scores.mean())) / sd >= z_library
    if not ok.any() or float(zi[ok].max()) < z_top:
        return []                               # žádná jistá shoda = prázdný výsledek, ne šum
    hits = np.flatnonzero(ok)
    return [int(i) for i in hits[np.argsort(-zi[hits], kind="stable")]]


def search_by_text(query_text, photo_embeddings_dict, **thresholds):
    """
    MODUL 1.2: Sémantické vyhledávání – skalární součin přes celou knihovnu (< 2 ms / 10 000 fotek),
    výběr podle kalibrace na obraz (select_calibrated); bez použitelné banky pojmů záloha
    select_relevant. Vrací [(media_id, skóre), ...] od nejlepší.
    """
    query_text = str(query_text or "").strip()
    if not query_text or not photo_embeddings_dict:
        return []

    with _index_lock:
        # klíč i podle identity slovníku: po načtení embeddingů se stejným počtem (jedna fotka
        # smazaná, jiná přidaná) by jinak hledání vracelo id ze staré matice
        if (_cached_index_state["count"] != len(photo_embeddings_dict)
                or _cached_index_state.get("source") is not photo_embeddings_dict
                or _cached_index_state["matrix"] is None):
            _rebuild_vector_index(photo_embeddings_dict)
            _cached_index_state["source"] = photo_embeddings_dict
        state = dict(_cached_index_state)   # lokální snímek, ať ho jiné vlákno nepřepíše

    matrix = state["matrix"]
    media_ids = state["media_ids"]
    if matrix is None or len(media_ids) == 0:
        return []

    text_model = get_text_model()
    q_vec = text_model.encode(query_text, convert_to_numpy=True, show_progress_bar=False)
    q_vec = np.asarray(q_vec, dtype=np.float32).flatten()
    if q_vec.size != matrix.shape[1] or not np.all(np.isfinite(q_vec)):
        return []                       # jiný model / poškozený výstup: žádné NaN skóre ani pád np.dot
    q_norm = np.linalg.norm(q_vec)
    if q_norm > 0:
        q_vec = q_vec / q_norm

    # Skóre celé knihovny (ne jen top-k z FAISS): adaptivní práh potřebuje rozložení všech skóre
    scores = np.dot(matrix, q_vec)
    stats = _image_stats(matrix, text_model)
    if stats is not None and not thresholds:
        picked = select_calibrated(scores, *stats)
    else:
        picked = select_relevant(scores, **thresholds)
    return [(media_ids[i], float(scores[i])) for i in picked]


def _image_stats(matrix, text_model):
    """Statistika fotek vůči bance pojmů – počítá se jednou na index (knihovnu) a model."""
    try:
        bank = concept_bank_vectors(text_model)
    except Exception:
        return None
    with _index_lock:
        cached = _cached_index_state.get("concept_stats")
        if cached is not None and cached[0] is matrix and cached[1] is bank:
            return cached[2]
    stats = image_concept_stats(matrix, bank)
    with _index_lock:
        if _cached_index_state.get("matrix") is matrix:
            _cached_index_state["concept_stats"] = (matrix, bank, stats)
    return stats


def compute_phash_and_sharpness(image_path):
    """
    MODUL 1.3: Vypočítá 64-bitový perceptuální hash (DCT pHash) pro detekci duplicit
    a Laplaciánské skóre ostrosti pro kurátorství.
    """
    if not image_path or not os.path.exists(image_path):
        return None, 0.0
    try:
        img_gray = cv_io.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img_gray is None:
            with Image.open(image_path) as pil_img:
                img_gray = np.array(pil_img.convert("L"))
        if img_gray is None or img_gray.size == 0:
            return None, 0.0

        # 1. Laplaciánské skóre ostrosti
        sharpness = float(cv2.Laplacian(img_gray, cv2.CV_64F).var())

        # 2. 64-bitový DCT Perceptual Hash (pHash)
        resized = cv2.resize(img_gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
        dct_matrix = cv2.dct(resized)
        dct_low = dct_matrix[:8, :8]
        # Průměr bez DC složky [0,0]
        med = np.median(dct_low.flatten()[1:])
        bits = (dct_low > med).flatten()
        hash_val = 0
        for b in bits:
            hash_val = (hash_val << 1) | int(b)
        phash_hex = f"{hash_val:016x}"
        return phash_hex, sharpness
    except Exception:
        return None, 0.0


# Zvýšit při změně algoritmu nebo zdroje otisku -> refresh_phashes() přepočítá všechno.
# Verze 2: otisky z dřívějších verzí byly zastaralé (různé fotky se stejným pHash
# I STEJNOU ostrostí = spočítané ze stejného obrázku), proto se nedůvěřuje NULL verzi.
PHASH_VERSION = 2
_phash_lock = threading.Lock()


def _phash_source(media_id, file_path, thumb_path, media_type):
    """Obrázek pro otisk: vlastní miniatura položky (rychlá, už otočená podle EXIF),
    jinak originál fotky. Miniatura s cizím id se nepoužije (příčina starých kolizí)."""
    if thumb_path and os.path.exists(thumb_path) \
            and os.path.basename(thumb_path).lower() == f"thumb_{media_id}.jpg":
        return thumb_path
    if media_type != "video" and file_path and os.path.exists(file_path):
        return file_path
    return None


def refresh_phashes(should_stop=None, progress=None, flush_every=50):
    """Spočítá pHash + ostrost pro položky bez platného otisku (nové importy, změněné
    soubory, staré verze) a spolehlivě je zapíše do DB. Vrací počet zpracovaných položek.

    Běží ze skeneru (po miniaturách) i z démona; zámek zajistí, že se průchody nepřekrývají
    a druhý průchod zpracuje vše, co mezitím přibylo.
    """
    from database import get_connection, db_transaction, visible_media_sql
    with _phash_lock:
        conn = get_connection()
        try:
            rows = conn.execute(f"""
                SELECT m.id, m.file_path, m.thumbnail_path, m.media_type
                FROM media_items m
                WHERE {visible_media_sql('m')}
                  AND (m.phash_version IS NULL OR m.phash_version != ?)
                ORDER BY m.id
            """, (PHASH_VERSION,)).fetchall()
        finally:
            conn.close()

        total, done, updates = len(rows), 0, []

        def flush():
            if updates:
                with db_transaction() as wconn:
                    wconn.executemany(
                        "UPDATE media_items SET phash = ?, sharpness_score = ?, phash_version = ? "
                        "WHERE id = ?", updates)
                updates.clear()

        for i, (media_id, file_path, thumb_path, media_type) in enumerate(rows):
            if should_stop and should_stop():
                break
            src = _phash_source(media_id, file_path, thumb_path, media_type)
            if src is None:
                continue   # zdroj zatím chybí (miniatura se teprve vytvoří / odpojený disk)
            phash, sharpness = compute_phash_and_sharpness(src)
            # Nečitelný soubor: phash NULL + aktuální verze = nezkouší se znovu, dokud se nezmění
            updates.append((phash, float(sharpness or 0.0), PHASH_VERSION, media_id))
            done += 1
            if len(updates) >= flush_every:
                flush()
            if progress:
                progress(i + 1, total)
        flush()
        return done


def hamming_distance_hex(hash1, hash2):
    """Spočítá Hammingovu vzdálenost dvou 64-bitových hexadecimálních pHash otisků."""
    if not hash1 or not hash2 or len(hash1) != 16 or len(hash2) != 16:
        return 64
    try:
        xor_val = int(hash1, 16) ^ int(hash2, 16)
        return xor_val.bit_count()
    except Exception:
        return 64


def detect_duplicates_in_db(conn, max_hamming=3, max_time_diff_sec=120):
    """
    MODUL 1.3: Najde duplicitní snímky na základě pHash (Hammingova vzdálenost <= 3)
    a časové blízkosti, a označí je v databázi.

    Porovnávají se jen viditelné položky s aktuální verzí otisku (bez pohybových videí
    Live Photos a koše) a jen fotky s fotkami, videa s videi.
    """
    from database import visible_media_sql
    c = conn.cursor()
    c.execute(f"""
        SELECT m.id, m.phash, m.date_taken, m.file_size, m.sharpness_score, m.media_type
        FROM media_items m
        WHERE m.phash IS NOT NULL AND m.phash != '' AND m.phash_version = ?
          AND {visible_media_sql('m')}
        ORDER BY m.date_taken ASC, m.id ASC
    """, (PHASH_VERSION,))
    rows = c.fetchall()
    if not rows:
        return 0

    c.execute("UPDATE media_items SET is_duplicate = 0, duplicate_group_id = NULL")

    parsed = []
    for r in rows:
        dt_obj = None
        if r[2]:
            try:
                dt_obj = datetime.strptime(str(r[2])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        parsed.append({
            "id": r[0],
            "phash": r[1],
            "dt": dt_obj,
            "size": r[3] or 0,
            "sharpness": r[4] or 0.0,
            "kind": "video" if r[5] == "video" else "photo",
        })

    visited = set()
    group_counter = 1
    duplicate_Updates = []

    n = len(parsed)
    for i in range(n):
        if parsed[i]["id"] in visited:
            continue
        group = [parsed[i]]
        for j in range(i + 1, min(n, i + 40)):
            if parsed[j]["id"] in visited or parsed[j]["kind"] != parsed[i]["kind"]:
                continue
            dist = hamming_distance_hex(parsed[i]["phash"], parsed[j]["phash"])
            if dist <= max_hamming:
                if parsed[i]["dt"] and parsed[j]["dt"]:
                    tdiff = abs((parsed[j]["dt"] - parsed[i]["dt"]).total_seconds())
                    if tdiff <= max_time_diff_sec or dist <= 1:
                        group.append(parsed[j])
                        visited.add(parsed[j]["id"])
                elif dist <= 2:
                    group.append(parsed[j])
                    visited.add(parsed[j]["id"])

        if len(group) > 1:
            visited.add(parsed[i]["id"])
            for member in group:
                duplicate_Updates.append((1, group_counter, member["id"]))
            group_counter += 1

    if duplicate_Updates:
        c.executemany(
            "UPDATE media_items SET is_duplicate = ?, duplicate_group_id = ? WHERE id = ?",
            duplicate_Updates
        )
    conn.commit()
    return len(duplicate_Updates)


def get_duplicate_pairs(conn, max_hamming=3, max_time_diff_sec=120):
    """
    Spustí detekci duplicit a vrátí páry pro DuplicateReviewDialog.

    detect_duplicates_in_db() vrací jen POČET označených snímků (int); dialog potřebuje
    seznam n-tic (id_a, path_a, id_b, path_b, hamming). A je nejostřejší (pak největší)
    snímek skupiny, B postupně každý další člen téže skupiny. Páry, u kterých uživatel
    zvolil "Ponechat obě", se vynechají.
    """
    from database import get_dismissed_duplicate_pairs
    detect_duplicates_in_db(conn, max_hamming=max_hamming, max_time_diff_sec=max_time_diff_sec)
    dismissed = get_dismissed_duplicate_pairs(conn)
    c = conn.cursor()
    c.execute("""
        SELECT duplicate_group_id, id, file_path, phash
        FROM media_items
        WHERE is_duplicate = 1 AND duplicate_group_id IS NOT NULL
        ORDER BY duplicate_group_id,
                 COALESCE(sharpness_score, 0) DESC, COALESCE(file_size, 0) DESC, id
    """)
    groups = {}
    for group_id, media_id, path, phash in c.fetchall():
        groups.setdefault(group_id, []).append((media_id, path, phash))

    pairs = []
    for members in groups.values():
        best_id, best_path, best_hash = members[0]
        for media_id, path, phash in members[1:]:
            if (min(best_id, media_id), max(best_id, media_id)) in dismissed:
                continue
            pairs.append((best_id, best_path, media_id, path,
                          hamming_distance_hex(best_hash, phash)))
    return pairs