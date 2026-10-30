"""Modely hledání v ONNX Runtime (Sprint 3) – bez torch / transformers / sentence-transformers.

ImageEncoder: CLIP ViT-B/32 (obraz -> 512). Předzpracování jako CLIPImageProcessor:
  kratší strana na 224 (bikubicky), střed 224×224, /255, normalizace průměrem a odchylkou CLIP.
TextEncoder: multilingvální DistilBERT + průměr přes masku + Dense 768->512 (clip-ViT-B-32-multilingual-v1).

Obě třídy mají .encode(seznam, convert_to_numpy=True, show_progress_bar=False) jako
SentenceTransformer – ai_engine je vymění beze změny ostatního kódu. Výstupy NEJSOU normalizované
(stejně jako původní model; uložené embeddingy fotek zůstávají platné – viz tools/export_onnx.py).

Akcelerace: DirectML (onnxruntime-directml, jakákoli GPU s DirectX 12), jinak CPU. Text běží vždy
na CPU (jeden krátký dotaz – přenos na GPU by trval déle než výpočet).
"""
import os
import threading

import numpy as np

VISION_FILE = "clip_b32_vision.onnx"
TEXT_FILE = "clip_b32_text_multi.onnx"
TEXT_INT8_FILE = "clip_b32_text_multi_int8.onnx"
TOKENIZER_FILE = "clip_b32_text_tokenizer.json"
MAX_SEQ = 128
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
IMAGE_BATCH = 16


def available(models_dir):
    return all(os.path.exists(os.path.join(models_dir, f)) for f in (VISION_FILE, TEXT_FILE, TOKENIZER_FILE))


def providers(prefer_gpu=True):
    import onnxruntime as ort
    have = ort.get_available_providers()
    out = []
    if prefer_gpu and "DmlExecutionProvider" in have and os.environ.get("KAIROS_NO_GPU") != "1":
        out.append("DmlExecutionProvider")
    out.append("CPUExecutionProvider")
    return out


def _session(path, prefer_gpu, threads=0):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if threads:
        so.intra_op_num_threads = threads
    provs = providers(prefer_gpu)
    if "DmlExecutionProvider" in provs:
        so.enable_mem_pattern = False             # DirectML: povinné (dokumentace ORT)
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    try:
        return ort.InferenceSession(path, sess_options=so, providers=provs)
    except Exception as e:                        # ovladač / GPU odmítl -> CPU
        if provs[0] != "CPUExecutionProvider":
            print(f"[ONNX] {provs[0]} nelze použít ({e}), běží CPU")
            return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
        raise


def preprocess(img):
    """PIL RGB -> float32 3×224×224."""
    from PIL import Image
    if img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    # jako transformers get_resize_output_image_size: kratší strana = 224, delší oříznutá int()
    # (zaokrouhlení místo int() posunulo střed o půl pixelu -> kosinus jen 0,991)
    if w <= h:
        nw, nh = SIZE, int(SIZE * h / w)
    else:
        nw, nh = int(SIZE * w / h), SIZE
    img = img.resize((nw, nh), Image.Resampling.BICUBIC)
    left, top = (nw - SIZE) // 2, (nh - SIZE) // 2
    img = img.crop((left, top, left + SIZE, top + SIZE))
    a = np.asarray(img, dtype=np.float32) / 255.0
    a = (a - MEAN) / STD
    return a.transpose(2, 0, 1)


class ImageEncoder:
    def __init__(self, models_dir, prefer_gpu=True, threads=0):
        self.session = _session(os.path.join(models_dir, VISION_FILE), prefer_gpu, threads)
        self.provider = self.session.get_providers()[0]
        self._lock = threading.Lock()

    def encode(self, images, convert_to_numpy=True, show_progress_bar=False, batch_size=IMAGE_BATCH):
        single = not isinstance(images, (list, tuple))
        batch = [images] if single else list(images)
        out = []
        for k in range(0, len(batch), batch_size):
            x = np.stack([preprocess(im) for im in batch[k:k + batch_size]]).astype(np.float32)
            with self._lock:                      # sezení je bezpečné, ale DirectML fronta ne vždy
                out.append(self.session.run(None, {"pixel_values": x})[0])
        vecs = np.concatenate(out, axis=0) if out else np.zeros((0, 512), np.float32)
        return vecs[0] if single else vecs


class TextEncoder:
    def __init__(self, models_dir, quantized=True, threads=0):
        from tokenizers import Tokenizer
        path = os.path.join(models_dir, TEXT_INT8_FILE if quantized else TEXT_FILE)
        if quantized and not os.path.exists(path):
            path = os.path.join(models_dir, TEXT_FILE)
        self.session = _session(path, prefer_gpu=False, threads=threads)
        self.tokenizer = Tokenizer.from_file(os.path.join(models_dir, TOKENIZER_FILE))
        self.tokenizer.enable_truncation(MAX_SEQ)
        self.tokenizer.enable_padding(pad_id=self.tokenizer.token_to_id("[PAD]") or 0, pad_token="[PAD]")
        self._lock = threading.Lock()

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)
        if not batch:
            return np.zeros((0, 512), np.float32)
        enc = self.tokenizer.encode_batch([str(t) for t in batch])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        with self._lock:
            vecs = self.session.run(None, {"input_ids": ids, "attention_mask": mask})[0]
        return vecs[0] if single else vecs
