"""Přesné hledání (Sprint 4.0): SigLIP 2 jako volitelný druhý model hledání.

Výchozí zůstává lehký CLIP ViT-B/32 v ONNX (ai_onnx) – běží na každém stroji s 8 GB. Po zapnutí
„Přesné hledání“ v Předvolbách › Pokročilé se stáhne SigLIP 2 (google/siglip2-base-patch16-256,
ONNX export onnx-community) do models/siglip2 vedle aplikace, démon na pozadí přeindexuje
knihovnu (governor: během videa, gest a Cesty stojí) a hledání se přepne, jakmile je index
téměř úplný (main.py). Do té doby, a kdykoli po vypnutí, hledá CLIP – oba indexy žijí vedle sebe
(image_embeddings 512 rozměrů, siglip_embeddings 768), takže přepnutí nic nepřepočítává.

Naměřeno (eval/eval_siglip.py, zlatá sada 277 fotek): čisté pořadí R@20 0,815 -> 0,941.
Obraz: shoda ONNX s PyTorch kosinus 1,0000 (předzpracování = SiglipImageProcessor: 256×256
bilineárně bez ořezu, (x/255 - 0,5) / 0,5). Text: fp32 (int8 export onnx-community dává u češtiny
kosinus jen 0,80–0,87 proti PyTorch – viz tests).
"""
import hashlib
import os
import threading
import urllib.request

import numpy as np

import paths

MODEL_ID = "onnx-community/siglip2-base-patch16-256-ONNX"
BASE_URL = f"https://huggingface.co/{MODEL_ID}/resolve/main/"
DIM = 768
SIZE = 256
MAX_LEN = 64                  # SigLIP je trénovaný s výplní na 64 tokenů – kratší výplň zhorší shodu
IMAGE_BATCH = 8

VISION_FILE = "vision_model.onnx"
TEXT_FILE = "text_model.onnx"
TOKENIZER_FILE = "tokenizer.json"
# (vzdálená cesta, místní soubor, velikost v bajtech, SHA-256) – stažené soubory se ověří celé
FILES = [
    ("onnx/vision_model.onnx", VISION_FILE, 371992072,
     "f5cb16728a704703f05516ded628397e11dbca4de2eb5db04b0c0bcee988aa7a"),
    ("onnx/text_model.onnx", TEXT_FILE, 1129469657,
     "d3de4a6bbbfcb429b6615ac496790353cf4a4fc0f19fbbe7179e523ae60daaef"),
    ("tokenizer.json", TOKENIZER_FILE, 34363039,
     "cb9140fae3ac5122c972d37adf83e1248471a38147ad76f8215c8872c6fd8322"),
]
DOWNLOAD_MB = round(sum(f[2] for f in FILES) / 1e6)
CHUNK = 1 << 20


def model_dir():
    """Zapisovatelná složka vedle aplikace (ne _internal balíčku – ten může být jen pro čtení)."""
    return os.path.join(paths.BASE_DIR, "models", "siglip2")


def installed(folder=None):
    folder = folder or model_dir()
    return all(os.path.exists(os.path.join(folder, local)) and os.path.getsize(os.path.join(folder, local)) == size
               for _remote, local, size, _sha in FILES)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK * 4), b""):
            h.update(block)
    return h.hexdigest()


class DownloadError(Exception):
    pass


def download(progress=None, should_stop=None, folder=None, base_url=BASE_URL, opener=None):
    """Stáhne chybějící soubory modelu (pokračuje v přerušeném .part). progress(hotovo_B, celkem_B).
    True = model je kompletní, False = zrušeno; chyba sítě / kontrolního součtu -> DownloadError
    (vadný soubor se smaže, příště se stahuje znovu)."""
    folder = folder or model_dir()
    os.makedirs(folder, exist_ok=True)
    opener = opener or urllib.request.urlopen
    total = sum(f[2] for f in FILES)
    done = sum(f[2] for f in FILES if os.path.exists(os.path.join(folder, f[1]))
               and os.path.getsize(os.path.join(folder, f[1])) == f[2])
    for remote, local, size, sha in FILES:
        target = os.path.join(folder, local)
        if os.path.exists(target) and os.path.getsize(target) == size:
            continue
        part = target + ".part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have > size:
            os.remove(part)
            have = 0
        req = urllib.request.Request(base_url + remote, headers={"User-Agent": "Kairos"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            resp = opener(req, timeout=60)
        except Exception as e:
            raise DownloadError(f"{local}: {e}") from e
        with resp:
            if have and getattr(resp, "status", 206) != 206:      # server neumí pokračovat -> od nuly
                have = 0
            with open(part, "ab" if have else "wb") as out:
                got = have
                while True:
                    if should_stop and should_stop():
                        return False
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    got += len(block)
                    if progress:
                        progress(done + got, total)
        if os.path.getsize(part) != size:
            raise DownloadError(f"{local}: neúplné stažení ({os.path.getsize(part)} z {size} B)")
        if sha and len(sha) == 64 and _sha256(part) != sha:
            os.remove(part)
            raise DownloadError(f"{local}: nesouhlasí kontrolní součet")
        os.replace(part, target)
        done += size
    return installed(folder)


def remove(folder=None):
    """Smaže stažený model (vypnutí přesného hledání s úklidem místa)."""
    folder = folder or model_dir()
    for _remote, local, _size, _sha in FILES:
        for p in (os.path.join(folder, local), os.path.join(folder, local) + ".part"):
            if os.path.exists(p):
                os.remove(p)


def preprocess(img):
    """PIL -> float32 3×256×256 (SiglipImageProcessor: bilineárně na 256×256 bez ořezu)."""
    from PIL import Image
    if img.mode != "RGB":
        img = img.convert("RGB")
    img = img.resize((SIZE, SIZE), Image.Resampling.BILINEAR)
    a = np.asarray(img, dtype=np.float32) * (1.0 / 255.0)
    return ((a - 0.5) / 0.5).transpose(2, 0, 1)


class ImageEncoder:
    """Rozhraní jako SentenceTransformer / ai_onnx.ImageEncoder: .encode(seznam PIL) -> (n, 768)."""
    dim = DIM

    def __init__(self, folder=None, prefer_gpu=True, threads=0):
        import ai_onnx
        self.session = ai_onnx._session(os.path.join(folder or model_dir(), VISION_FILE), prefer_gpu, threads)
        self.provider = self.session.get_providers()[0]
        self._lock = threading.Lock()

    def encode(self, images, convert_to_numpy=True, show_progress_bar=False, batch_size=IMAGE_BATCH):
        single = not isinstance(images, (list, tuple))
        batch = [images] if single else list(images)
        out = []
        for k in range(0, len(batch), batch_size):
            x = np.stack([preprocess(im) for im in batch[k:k + batch_size]]).astype(np.float32)
            with self._lock:
                out.append(self.session.run(["pooler_output"], {"pixel_values": x})[0])
        vecs = np.concatenate(out, axis=0) if out else np.zeros((0, DIM), np.float32)
        return vecs[0] if single else vecs


class TextEncoder:
    dim = DIM

    def __init__(self, folder=None, threads=0, text_file=TEXT_FILE):
        import ai_onnx
        from tokenizers import Tokenizer
        folder = folder or model_dir()
        self.session = ai_onnx._session(os.path.join(folder, text_file), prefer_gpu=False, threads=threads)
        self.tokenizer = Tokenizer.from_file(os.path.join(folder, TOKENIZER_FILE))
        self.tokenizer.enable_truncation(MAX_LEN)
        self.tokenizer.enable_padding(pad_id=0, pad_token="<pad>", length=MAX_LEN)
        self._lock = threading.Lock()

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        if not batch:
            return np.zeros((0, DIM), np.float32)
        enc = self.tokenizer.encode_batch([str(t).lower() for t in batch])   # SigLIP: malá písmena
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        with self._lock:
            vecs = self.session.run(["pooler_output"], {"input_ids": ids})[0]
        return vecs[0] if single else vecs
