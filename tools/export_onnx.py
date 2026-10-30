"""Vývojový nástroj (Sprint 3): převod modelů hledání do ONNX – aplikace pak torch nepotřebuje.

    python tools/export_onnx.py            # vytvoří models/clip_b32_*.onnx + tokenizer
    python tools/export_onnx.py --check    # jen ověří shodu ONNX s původním modelem

Potřebuje torch + sentence-transformers (jen tady, na vývojovém stroji). Výsledek:
  models/clip_b32_vision.onnx          obraz 224×224 -> 512 (CLIP ViT-B/32, get_image_features)
  models/clip_b32_text_multi.onnx      tokeny -> 512 (DistilBERT multilingual, průměr přes masku, Dense)
  models/clip_b32_text_multi_int8.onnx totéž, váhy int8 (dotazy hledání; uložené vektory fotek se netýká)
  models/clip_b32_text_tokenizer.json  tokenizer (knihovna tokenizers, bez transformers)
Shoda: kosinus ≥ 0.9999 obraz (uložené embeddingy zůstávají platné), ≥ 0.999 text.
"""
import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

import ai_engine  # noqa: E402
import ai_onnx  # noqa: E402

OPSET = 17


def export(models_dir):
    import torch
    from sentence_transformers import SentenceTransformer

    img = SentenceTransformer(ai_engine.IMAGE_MODEL_NAME, device="cpu")
    clip = img[0].model.eval()

    class Vision(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, pixel_values):
            out = self.m.vision_model(pixel_values=pixel_values)
            return self.m.visual_projection(out.pooler_output)

    txt = SentenceTransformer(ai_engine.TEXT_MODEL_NAME, device="cpu")
    bert = txt[0].auto_model.eval()
    dense = txt[2].linear.eval()

    class Text(torch.nn.Module):
        def __init__(self, b, d):
            super().__init__()
            self.b, self.d = b, d

        def forward(self, input_ids, attention_mask):
            h = self.b(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            m = attention_mask.unsqueeze(-1).to(h.dtype)
            pooled = (h * m).sum(1) / m.sum(1).clamp(min=1e-9)
            return self.d(pooled)

    os.makedirs(models_dir, exist_ok=True)
    vpath = os.path.join(models_dir, ai_onnx.VISION_FILE)
    tpath = os.path.join(models_dir, ai_onnx.TEXT_FILE)
    with torch.no_grad():
        torch.onnx.export(Vision(clip), (torch.randn(2, 3, 224, 224),), vpath, opset_version=OPSET,
                          input_names=["pixel_values"], output_names=["embeds"],
                          dynamic_axes={"pixel_values": {0: "batch"}, "embeds": {0: "batch"}}, dynamo=False)
        ids = torch.ones(2, 16, dtype=torch.long)
        torch.onnx.export(Text(bert, dense), (ids, torch.ones_like(ids)), tpath, opset_version=OPSET,
                          input_names=["input_ids", "attention_mask"], output_names=["embeds"],
                          dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
                                        "embeds": {0: "batch"}}, dynamo=False)
    snap = os.path.dirname(txt.tokenizer.name_or_path) if os.path.isfile(txt.tokenizer.name_or_path) else None
    tok_src = None
    for cand in (snap, getattr(txt[0], "tokenizer", None) and txt[0].tokenizer.name_or_path):
        if cand and os.path.exists(os.path.join(cand, "tokenizer.json")):
            tok_src = os.path.join(cand, "tokenizer.json")
    if tok_src is None:
        from huggingface_hub import hf_hub_download
        tok_src = hf_hub_download(ai_engine.TEXT_MODEL_NAME, "tokenizer.json")
    shutil.copyfile(tok_src, os.path.join(models_dir, ai_onnx.TOKENIZER_FILE))

    from onnxruntime.quantization import QuantType, quantize_dynamic
    quantize_dynamic(tpath, os.path.join(models_dir, ai_onnx.TEXT_INT8_FILE), weight_type=QuantType.QInt8)
    for f in (ai_onnx.VISION_FILE, ai_onnx.TEXT_FILE, ai_onnx.TEXT_INT8_FILE, ai_onnx.TOKENIZER_FILE):
        print(f"  {f}: {os.path.getsize(os.path.join(models_dir, f)) / 2**20:.1f} MB")


def check(models_dir):
    """Srovnání s původními modely sentence-transformers na fotkách z knihovny a dotazech golden setu."""
    import json
    from PIL import Image
    from sentence_transformers import SentenceTransformer
    st_img = SentenceTransformer(ai_engine.IMAGE_MODEL_NAME, device="cpu")
    st_txt = SentenceTransformer(ai_engine.TEXT_MODEL_NAME, device="cpu")
    ox_img = ai_onnx.ImageEncoder(models_dir, prefer_gpu=False)
    queries = [q["q"] for q in json.load(open(os.path.join(ROOT, "eval", "search_golden.json"), encoding="utf-8"))["queries"]]
    imgs = []
    thumbs = os.path.join(ROOT, ".cache", "thumbnails")
    for name in sorted(os.listdir(thumbs))[:24] if os.path.isdir(thumbs) else []:
        try:
            imgs.append(Image.open(os.path.join(thumbs, name)).convert("RGB"))
        except Exception:
            pass
    if not imgs:
        rng = np.random.default_rng(1)
        imgs = [Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8)) for _ in range(6)]

    def cos(a, b):
        a = a / np.linalg.norm(a, axis=1, keepdims=True)
        b = b / np.linalg.norm(b, axis=1, keepdims=True)
        return (a * b).sum(1)

    ci = cos(np.asarray(st_img.encode(imgs, convert_to_numpy=True)), ox_img.encode(imgs))
    print(f"obraz  ({len(imgs)}): kosinus min {ci.min():.6f}  průměr {ci.mean():.6f}")
    ref = np.asarray(st_txt.encode(queries, convert_to_numpy=True))
    for label, quant in (("text fp32", False), ("text int8", True)):
        ct = cos(ref, ai_onnx.TextEncoder(models_dir, quantized=quant).encode(queries))
        print(f"{label} ({len(queries)}): kosinus min {ct.min():.6f}  průměr {ct.mean():.6f}")
    return float(ci.min())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--models", default=os.path.join(ROOT, "models"))
    a = ap.parse_args()
    if not a.check:
        export(a.models)
    check(a.models)
