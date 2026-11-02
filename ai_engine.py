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
SIGLIP_DIM = 768                # Sprint 4.0: SigLIP 2 (přesné hledání, siglip_engine) – vlastní tabulka

_text_model = None
_image_model = None
_siglip_text = None
_siglip_image = None
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
    if arr.size not in _dims():
        raise ValueError(f'Embedding has {arr.size} dimensions, expected {EMBED_DIM}')
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
        raise pickle.UnpicklingError(f'Forbidden object in pickle: {module}.{name}')


def _dims():
    return {EMBED_DIM, SIGLIP_DIM}


def decode_embedding(raw):
    """bytes -> float32 ndarray (EMBED_DIM nebo SIGLIP_DIM,), nebo None při neplatných datech."""
    if not raw:
        return None
    try:
        raw = bytes(raw)
        if len(raw) in {d * 4 for d in _dims()}:
            vec = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        elif raw[:1] == b"\x80":   # starý formát (pickle)
            vec = _RestrictedUnpickler(io.BytesIO(raw)).load()
            vec = np.asarray(vec, dtype=np.float32).flatten()
        else:
            return None
        if vec.size not in _dims() or not np.all(np.isfinite(vec)):
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


# Sprint 3: modely hledání v ONNX Runtime (ai_onnx, models/clip_b32_*.onnx z tools/export_onnx.py) –
# aplikace bez torch (~1 GB paměti a ~2 GB balíčku méně). sentence-transformers zůstává jen jako
# záloha vývojového stroje bez exportovaných modelů (KAIROS_TORCH=1 ho vynutí pro srovnání).
def onnx_models_dir():
    import paths
    return os.path.join(paths.RESOURCE_DIR, "models")


def _use_onnx():
    if os.environ.get("KAIROS_TORCH") == "1":
        return False
    import ai_onnx
    return ai_onnx.available(onnx_models_dir())


def _quantized_text():
    """int8 textový model (-385 MB, golden set P@10 0,844 -> 0,834) jen na nejslabším profilu."""
    env = os.environ.get("KAIROS_TEXT_MODEL")
    if env in ("int8", "fp32"):
        return env == "int8"
    try:
        import perf_profile
        return perf_profile.current().tier == "baseline"
    except Exception:
        return False


def get_text_model():
    """Textový model (vyhledávání) - načítá se samostatně a jen jednou."""
    global _text_model
    with _model_lock:
        if _text_model is None:
            if _use_onnx():
                import ai_onnx
                _text_model = ai_onnx.TextEncoder(onnx_models_dir(), quantized=_quantized_text())
            else:
                from sentence_transformers import SentenceTransformer
                _text_model = SentenceTransformer(TEXT_MODEL_NAME)
        return _text_model


def get_image_model():
    """Obrazový model (výpočet embeddingů) - načítá se samostatně a jen jednou."""
    global _image_model
    with _model_lock:
        if _image_model is None:
            if _use_onnx():
                import ai_onnx
                _image_model = ai_onnx.ImageEncoder(onnx_models_dir())
                print(f"[AI] obrazový model ONNX: {_image_model.provider}")
            else:
                from sentence_transformers import SentenceTransformer
                _image_model = SentenceTransformer(IMAGE_MODEL_NAME)
        return _image_model


# ── Sprint 4.0: přesné hledání (SigLIP 2) ──
def siglip_available():
    import siglip_engine
    return siglip_engine.installed()


def get_siglip_text_model():
    global _siglip_text
    with _model_lock:
        if _siglip_text is None:
            import siglip_engine
            _siglip_text = siglip_engine.TextEncoder()
        return _siglip_text


def get_siglip_image_model():
    global _siglip_image
    with _model_lock:
        if _siglip_image is None:
            import siglip_engine
            _siglip_image = siglip_engine.ImageEncoder()
            print(f"[AI] SigLIP 2 obrazový model: {_siglip_image.provider}")
        return _siglip_image


def release_siglip():
    """Vypnuté přesné hledání: modely z paměti (~1,6 GB)."""
    global _siglip_text, _siglip_image
    with _model_lock:
        _siglip_text = _siglip_image = None


def text_model_for_dim(dim):
    """Textový model ke stejnému prostoru jako index: 768 = SigLIP 2, jinak CLIP. Index tak určuje
    model a dotaz CLIP se nikdy neporovná s vektory SigLIP (ani naopak)."""
    if dim == SIGLIP_DIM and EMBED_DIM != SIGLIP_DIM:
        return get_siglip_text_model()
    return get_text_model()


def siglip_embed_pil(images):
    """PIL obrázky -> [bytes 768 float32] pro siglip_embeddings."""
    batch = list(images)
    if not batch:
        return []
    vecs = np.asarray(get_siglip_image_model().encode(batch), dtype=np.float32).reshape(len(batch), -1)
    return [encode_embedding(v) for v in vecs]


def get_models():
    """Zpětná kompatibilita: vrací (text_model, image_model)."""
    return get_text_model(), get_image_model()


def compute_image_embedding(image_path):
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with Image.open(image_path) as img:
            return embed_pil(img.convert("RGB"))
    except Exception as e:
        print(f"[AI Chyba] Selhal výpočet vektoru pro {image_path}: {e}")
        return None


def embed_pil(images):
    """PIL obrázek (nebo seznam) -> bytes embeddingu (nebo seznam). Snímky videa jdou jednou dávkou."""
    single = not isinstance(images, (list, tuple))
    batch = [images] if single else list(images)
    if not batch:
        return []
    vecs = get_image_model().encode(batch, convert_to_numpy=True, show_progress_bar=False)
    vecs = np.asarray(vecs, dtype=np.float32).reshape(len(batch), -1)
    out = [encode_embedding(v) for v in vecs]
    return out[0] if single else out


def _rebuild_vector_index(photo_embeddings_dict):
    """Sestaví normalizovanou matici (a FAISS IndexFlatIP, pokud je dostupný) v RAM.

    Hodnota slovníku je bytes (jeden vektor) nebo n-tice bytes (video: plakát + snímky z celého
    klipu, video_frames). Řádky jedné položky leží v matici za sebou; starts[k] = první řádek
    položky media_ids[k] (pro np.maximum.reduceat v rank_semantic)."""
    global _cached_index_state
    media_ids = []
    vectors = []
    starts = []

    for m_id, raw in photo_embeddings_dict.items():
        rows = []
        for raw_bytes in (raw if isinstance(raw, (tuple, list)) else (raw,)):
            vec = decode_embedding(raw_bytes)
            if vec is None or (vectors and vec.size != vectors[0].size):   # jeden index = jeden model
                continue
            norm = np.linalg.norm(vec)
            if norm > 0:
                rows.append(vec / norm)
        if rows:
            media_ids.append(int(m_id))
            starts.append(len(vectors))
            vectors.extend(rows)

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

    multi = len(vectors) > len(media_ids)
    starts = np.asarray(starts, dtype=np.int64)
    _cached_index_state = {
        "count": len(photo_embeddings_dict),
        "media_ids": media_ids,
        "matrix": matrix,
        "faiss_index": faiss_idx,
        # vícesnímková videa: začátky řádků položek a počet řádků (None = řádek = položka)
        "starts": starts if multi else None,
        "rows_per_item": np.diff(np.append(starts, len(vectors))) if multi else None,
    }


# ── Hledání 2.0: řazení obrazem ve dvou pásmech (search_engine.run_search) ────────
# Každá fotka se porovná sama se sebou: z_obraz = jak moc dotaz vyčnívá z podobností fotky k bance
# běžných pojmů (CONCEPT_BANK) – „hub“ (fotka podobná čemukoli) tak nepřeskočí skutečné shody.
# z_knihovna = jak moc fotka vyčnívá v rámci celé knihovny. Řadí se podle z_obraz + z_knihovna.
#
# Dřívější výběr měl jednu tvrdou hranu („nejlepší pod z 2,7 = nic“) a „voda“ s nejlepším z 2,61
# vracela nulu. Teď žádná hrana mezi „něco“ a „nic“ není: jisté shody jsou pásmo nad
# SEARCH_Z_CONFIDENT, možné shody pásmo nad SEARCH_Z_POSSIBLE. Prázdno jen když nic v knihovně
# nedosáhne SEARCH_Z_SHOW (nic nevyčnívá) – pak to UI řekne rovnou.
#
# Prahy laděné proti zlaté sadě eval/search_golden.json (45 dotazů, 152 viditelných položek):
# R@20 0,85, P@10 0,84 jen obrazem; jisté pásmo bez jediné falešné shody u 8 nepřítomných
# pojmů; polovina nepřítomných pojmů prázdná. Ověřit znovu: python eval/eval_search.py
SEARCH_Z_CONFIDENT = 2.6
SEARCH_Z_LIB_CONFIDENT = 1.0
SEARCH_Z_POSSIBLE = 1.2
SEARCH_Z_SHOW = 1.8
SEARCH_POSSIBLE_MAX = 24
SEARCH_LIB_WEIGHT = 1.0          # váha z_knihovna v řazení
SEARCH_FACE_BONUS = 0.5          # „lidi u jezera“: rozpoznaná tvář řadí výš
SEARCH_MIN_STATS = 24            # pod tolik položkami je rozptyl knihovny nespolehlivý
# Kalibrováno na ~150–300 položkách. Ve větší knihovně překročí pevný práh náhodou víc fotek
# (maximum šumu roste s počtem) -> prahy mírně rostou s log10 velikosti (10 000 fotek: +0,38).
SEARCH_CALIBRATED_N = 300
SEARCH_Z_PER_DECADE = 0.25
# Sprint 4.0: SigLIP 2 má jiné rozložení podobností k bance pojmů -> vlastní prahy (CLIP čte
# konstanty výše). Kalibrace: python eval/sweep_siglip_z.py (zlatá sada, mřížka prahů):
# aplikace R@20 0,909 -> 0,920, P@10 0,850 -> 0,859, jisté shody u nepřítomných pojmů 1/8 -> 0/8
# (CLIP na téže sadě: R@20 0,885, P@10 0,839). Jisté 3,4–4,2 vychází stejně, bere se nejnižší.
Z_PROFILES = {
    SIGLIP_DIM: {"confident": 3.4, "lib": 1.0, "possible": 0.8, "show": 1.8},
}


def z_profile(dim):
    """Prahy pro index dané dimenze: {confident, lib, possible, show}."""
    if dim in Z_PROFILES and dim != EMBED_DIM:
        return Z_PROFILES[dim]
    return {"confident": SEARCH_Z_CONFIDENT, "lib": SEARCH_Z_LIB_CONFIDENT,
            "possible": SEARCH_Z_POSSIBLE, "show": SEARCH_Z_SHOW}
# z za zdvojnásobení počtu vektorů videa (frame_penalty). Kalibrace eval/eval_search.py --frames --videos:
# 0 = 2 nepřítomné pojmy s jistou shodou; 0,25 = žádná, videa R@3 0,40 -> 0,79, MRR 0,48 -> 0,87
SEARCH_FRAME_Z_PENALTY = 0.25

CONCEPT_BANK = (
    # i18n-ok: search vocabulary (CLIP prompts)
    "pes kočka kůň kráva pták ryba hmyz auto autobus vlak letadlo loď kolo motorka silnice most "
    "budova dům město ulice kostel hrad zámek věž zeď okno dveře pokoj kuchyně postel stůl židle "
    "počítač telefon televize kniha papír jídlo pizza dort ovoce zelenina nápoj káva pivo víno "
    "člověk muž žena dítě dav obličej ruka noha oblečení boty hora hory kopec skála kámen písek "
    "pláž moře jezero řeka voda vodopád les strom tráva louka květiny zahrada park pole nebe mraky "
    "slunce noc sníh led déšť oheň světlo tma fotbal sport hudba koncert oslava "
    "obraz socha muzeum obchod restaurace kancelář škola nemocnice letiště nádraží stan "
    # i18n-ok: search vocabulary (CLIP prompts)
    "dřevo kov sklo plast hračka zbraň peníze text dokument").split() + ["západ slunce", "snímek obrazovky"]

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


def tier_scores(zi, zl, key, n_library, profile=None):
    """Indexy (jisté, možné) od nejlepšího. zi = z vůči bance pojmů fotky, zl = z v knihovně
    (None u malé knihovny), key = pořadí. Prázdné obojí = nic nevyčnívá (viz SEARCH_Z_SHOW)."""
    zi = np.asarray(zi, dtype=np.float32)
    if zi.size == 0 or not np.all(np.isfinite(zi)):
        return [], []
    zp = profile or z_profile(EMBED_DIM)
    grow = SEARCH_Z_PER_DECADE * max(0.0, math.log10(max(1, n_library) / SEARCH_CALIBRATED_N))
    if float(zi.max()) < zp["show"] + grow:
        return [], []
    lib_ok = np.ones_like(zi, dtype=bool) if zl is None else zl >= zp["lib"]
    possible_ok = np.ones_like(zi, dtype=bool) if zl is None else zl >= 0.0
    order = np.argsort(-np.asarray(key, dtype=np.float32), kind="stable")
    confident = [int(i) for i in order if zi[i] >= zp["confident"] + grow and lib_ok[i]]
    taken = set(confident)
    possible = [int(i) for i in order
                if i not in taken and zi[i] >= zp["possible"] + grow and possible_ok[i]]
    return confident, possible[:SEARCH_POSSIBLE_MAX]


def query_vector(text, text_model):
    """Soubor (ensemble) vektorů dotazu: původní text + anglický protějšek ze slovníku
    (search_lexicon) – „les“ je pro model i francouzský člen, „forest“ ne. None = vadný výstup."""
    from search_engine import fold, _same_word
    from search_lexicon import english_for
    words = str(text).split()
    texts = [text]
    english = english_for(words, fold, _same_word)
    if english:
        texts.append(english)
    vecs = np.asarray(text_model.encode(texts, convert_to_numpy=True, show_progress_bar=False),
                      dtype=np.float32)
    if vecs.ndim == 1:                                  # model vrátil jeden vektor (mock)
        vecs = vecs[None, :]
    if not np.all(np.isfinite(vecs)):
        return None
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    vec = (vecs / np.where(norms > 0, norms, 1.0)).mean(axis=0)
    n = np.linalg.norm(vec)
    return vec / n if n > 0 else None


def _index_for(photo_embeddings_dict):
    with _index_lock:
        # klíč i podle identity slovníku: po načtení embeddingů se stejným počtem (jedna fotka
        # smazaná, jiná přidaná) by jinak hledání vracelo id ze staré matice
        if (_cached_index_state["count"] != len(photo_embeddings_dict)
                or _cached_index_state.get("source") is not photo_embeddings_dict
                or _cached_index_state["matrix"] is None):
            _rebuild_vector_index(photo_embeddings_dict)
            _cached_index_state["source"] = photo_embeddings_dict
        return dict(_cached_index_state)   # lokální snímek, ať ho jiné vlákno nepřepíše


def rank_semantic(text, pool_ids, photo_embeddings_dict, face_ids=None):
    """Obrazové řazení dotazu v rámci pool_ids (aktuální kolekce / výsledek fazet).
    Statistiky (z_knihovna, kalibrace) jsou přes celou knihovnu, výběr jen z poolu.
    Vrací (jisté, možné) – seznamy media_id od nejlepšího."""
    text = str(text or "").strip()
    if not text or not photo_embeddings_dict:
        return [], []
    state = _index_for(photo_embeddings_dict)
    matrix, media_ids = state["matrix"], state["media_ids"]
    if matrix is None or not media_ids:
        return [], []
    text_model = text_model_for_dim(matrix.shape[1])
    q_vec = query_vector(text, text_model)
    if q_vec is None or q_vec.size != matrix.shape[1]:
        return [], []                   # jiný model / poškozený výstup: žádné NaN skóre ani pád np.dot

    row_scores = matrix @ q_vec
    starts = state.get("starts")
    # Video s víc vektory (plakát + snímky celého klipu): hodnotí se nejlepším snímkem
    scores = np.maximum.reduceat(row_scores, starts) if starts is not None else row_scores
    pool = set(pool_ids) if pool_ids is not None else None
    rows = np.array([i for i, m in enumerate(media_ids) if pool is None or m in pool], dtype=np.int64)
    if rows.size == 0:
        return [], []
    n = len(media_ids)
    zl_all = None
    if n >= SEARCH_MIN_STATS:
        sd = float(scores.std())
        if sd < 1e-6:
            return [], []                               # všechny fotky stejně (ne)podobné = nic nevyčnívá
        zl_all = (scores - float(scores.mean())) / sd
    stats = _image_stats(matrix, text_model)

    def image_z(rs):                                    # každý snímek vůči své bance pojmů
        z = (rs - stats[0]) / stats[1]
        if starts is not None:
            z = np.maximum.reduceat(z, starts) - frame_penalty(state["rows_per_item"])
        return z

    if stats is not None:
        zi_all = image_z(row_scores)
        zi_all = soft_and(text, zi_all, lambda t: _component_scores(t, text_model, matrix), image_z)
    elif zl_all is not None:
        zi_all = zl_all                                 # bez banky pojmů (mock model): jen knihovna
    else:
        return [], []
    zi, zl = zi_all[rows], (zl_all[rows] if zl_all is not None else None)
    key = zi + (SEARCH_LIB_WEIGHT * zl if zl is not None else 0.0)
    if face_ids:
        key = key + SEARCH_FACE_BONUS * np.array([media_ids[i] in face_ids for i in rows], dtype=np.float32)
    confident, possible = tier_scores(zi, zl, key, n, z_profile(matrix.shape[1]))
    return [media_ids[rows[i]] for i in confident], [media_ids[rows[i]] for i in possible]


# ── Sprint 3: měkké AND přes složky dotazu ──
# „západ slunce oheň smích“ jako jeden vektor = průměr pojmů: fotka silná v jednom (jen oheň)
# předběhne fotku, kde je všechno trochu. Dotaz se proto rozloží na slova (bez výplňových),
# každé se ohodnotí samostatně a z se smíchá s měkkým minimem přes složky (log-sum-exp):
# vyhrává fotka, která splní všechny. Složka, kterou knihovna vůbec nezná (max z < SHOW –
# „smích“ není vidět), se z AND vynechá, jinak by shodila celý dotaz.
# Naměřeno (eval/eval_softand.py, 30 dvojic pojmů ze zlaté sady): váha 0 -> P@k 0,734, 0,3 -> 0,734,
# 0,5 -> 0,701, 0,7 -> 0,681 (τ 0,25–2 stejně); jednoslovné dotazy beze změny. Celý dotaz jako jeden
# vektor CLIP zvládá dvojice pojmů sám -> VYPNUTO, mechanismus zůstává pro větší knihovny.
SOFTAND_WEIGHT = 0.0
SOFTAND_TAU = 0.5
SOFTAND_MAX_PARTS = 4
SOFTAND_FILLER = {"and", "with", "or", "a", "i", "s", "se", "u", "v", "ve", "na", "z", "ze", "the", "of", "in", "at",
                  "do", "pod", "nad", "pred", "za", "k", "ke", "pri", "po", "od"}


def query_parts(text):
    """Obsahová slova dotazu (2–4), jinak [] – jednoslovný dotaz se nerozkládá."""
    from search_engine import fold
    words = [w for w in str(text or "").replace(",", " ").replace("+", " ").split()
             if fold(w) not in SOFTAND_FILLER and len(fold(w)) > 1]
    return words if 2 <= len(words) <= SOFTAND_MAX_PARTS else []


def _component_scores(word, text_model, matrix):
    v = query_vector(word, text_model)
    if v is None or v.size != matrix.shape[1]:
        return None
    return matrix @ v


def soft_and(text, zi_whole, scores_for, image_z, weight=None):
    """Smíchá z celého dotazu s měkkým minimem z jeho složek (viz výše). Bez složek beze změny."""
    weight = SOFTAND_WEIGHT if weight is None else weight
    parts = query_parts(text) if weight > 0 else []
    if not parts:
        return zi_whole
    zs = []
    for w in parts:
        rs = scores_for(w)
        if rs is None:
            continue
        z = image_z(rs)
        if float(np.max(z)) >= SEARCH_Z_SHOW:            # pojem v knihovně je – patří do AND
            zs.append(z)
    if len(zs) < 2:
        return zi_whole
    Z = np.stack(zs).astype(np.float64)
    soft_min = -SOFTAND_TAU * np.log(np.mean(np.exp(-Z / SOFTAND_TAU), axis=0))
    return ((1 - weight) * zi_whole + weight * soft_min).astype(np.float32)


def frame_penalty(rows_per_item):
    """Maximum z k snímků vyčnívá náhodou víc než jeden vektor (víc pokusů). Srážka z podle
    počtu vektorů položky drží falešné shody videí na úrovni fotek (eval: nepřítomné pojmy)."""
    k = np.maximum(1, np.asarray(rows_per_item, dtype=np.float32))
    return (SEARCH_FRAME_Z_PENALTY * np.log2(k)).astype(np.float32)


def search_by_text(query_text, photo_embeddings_dict):
    """Obrazové hledání v celé knihovně: [(media_id, skóre), ...] – jisté shody, pak možné.
    (Aplikace hledá přes search_engine.run_search; tohle je jednoduché rozhraní pro skripty.)"""
    query_text = str(query_text or "").strip()
    if not query_text or not photo_embeddings_dict:
        return []
    confident, possible = rank_semantic(query_text, None, photo_embeddings_dict)
    return [(m, 1.0) for m in confident] + [(m, 0.5) for m in possible]


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
        # Stage 2: miniatury jsou větší (kratší strana 840 px) – ostrost (Laplacián) závisí na
        # měřítku, proto vždy ze stejné velikosti jako dřív (delší strana 360 px)
        h, w = img_gray.shape[:2]
        if max(h, w) > PHASH_EDGE:
            k = PHASH_EDGE / float(max(h, w))
            img_gray = cv2.resize(img_gray, (max(1, round(w * k)), max(1, round(h * k))),
                                  interpolation=cv2.INTER_AREA)

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
PHASH_VERSION = 3               # 3 = z normalizované velikosti (PHASH_EDGE) po zvětšení miniatur
PHASH_EDGE = 360
_phash_lock = threading.Lock()


def _phash_source(media_id, file_path, thumb_path, media_type):
    """Obrázek pro otisk: vlastní miniatura položky (rychlá, už otočená podle EXIF),
    jinak originál fotky. Miniatura s cizím id se nepoužije (příčina starých kolizí)."""
    if thumb_path and os.path.exists(thumb_path) \
            and os.path.basename(thumb_path).lower() == f"thumb_{media_id}.jpg":
        return thumb_path
    import cloud_files
    if media_type != "video" and cloud_files.local_ok(file_path):   # Gate 0: ne jen-v-cloudu
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