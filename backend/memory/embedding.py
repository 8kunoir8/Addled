"""
Local semantic embeddings for Addled's memory.

Backend chain (first available wins, all fully offline once downloaded):
  1. ONNX-quantized MiniLM-L6-v2  (backend/memory/models/minilm-l6-v2/model.onnx)
  2. transformers MiniLM fp32     (torch + transformers already bundled)
  3. hashed n-gram vectors        (works everywhere, no model needed)

Always returns 384-dim L2-normalized float32 vectors, matching
vector_store.DEFAULT_DIM, so old and new rows coexist in vectors.db.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import numpy as np

log = logging.getLogger("addled.embedding")

DIM = 384

MODEL_DIR = Path(__file__).parent / "models" / "minilm-l6-v2"
ONNX_PATH = MODEL_DIR / "model.onnx"
TRANSFORMERS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

_session = None       # onnxruntime.InferenceSession OR transformers AutoModel
_tokenizer = None     # transformers tokenizer (used by both backends)
_fast_tokenizer = None  # tokenizers.Tokenizer — works without transformers
_backend = "uninitialized"   # "onnx" | "transformers" | "hash"


def hash_embed(text: str, dim: int = DIM) -> np.ndarray:
    """Hashed n-gram embedding — stable, cheap, works offline."""
    vec = np.zeros(dim, dtype=np.float32)
    text = (text or "").lower()
    tokens = re.findall(r"[\w']+", text)
    grams = tokens + [a + "_" + b for a, b in zip(tokens, tokens[1:])]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


def _load() -> bool:
    """Initialize the best available backend. Returns False for hash-only."""
    global _session, _tokenizer, _backend
    if _backend != "uninitialized":
        return _backend != "hash"

    # 1) ONNX quantized model (fast, small — preferred)
    if ONNX_PATH.exists():
        try:
            import onnxruntime as ort
            _session = ort.InferenceSession(
                str(ONNX_PATH), providers=["CPUExecutionProvider"])
        except Exception as e:
            log.warning("ONNX session failed (%s) — trying transformers", e)
            _session = None
        else:
            # Tokenizer: transformers when available, else the lightweight
            # `tokenizers` lib (no torch dependency — fresh installs)
            try:
                from transformers import AutoTokenizer
                global _tokenizer
                _tokenizer = AutoTokenizer.from_pretrained(str(MODEL_DIR))
            except Exception:
                _tokenizer = None
            if _tokenizer is None:
                try:
                    from tokenizers import Tokenizer
                    global _fast_tokenizer
                    _fast_tokenizer = Tokenizer.from_file(
                        str(MODEL_DIR / "tokenizer.json"))
                except Exception as e:
                    log.warning("No tokenizer available (%s) — hash fallback", e)
                    _backend = "hash"
                    return False
            _backend = "onnx"
            log.info("Embedder: ONNX MiniLM loaded from %s", MODEL_DIR)
            return True

    # 2) transformers fp32 (torch + transformers ship with the app bundle)
    try:
        from transformers import AutoTokenizer, AutoModel
        _tokenizer = AutoTokenizer.from_pretrained(TRANSFORMERS_MODEL)
        _session = AutoModel.from_pretrained(TRANSFORMERS_MODEL)
        _session.eval()
        _backend = "transformers"
        log.info("Embedder: transformers MiniLM loaded")
        return True
    except Exception as e:
        log.warning("transformers embedder failed (%s) — using hash "
                    "embeddings", e)

    _backend = "hash"
    return False


def embed_text(text: str) -> np.ndarray:
    """384-dim L2-normalized vector; degrades to hash embeddings."""
    if _load():
        try:
            if _backend == "onnx":
                if _tokenizer is not None:
                    tok = _tokenizer(text, return_tensors="np",
                                     truncation=True, max_length=256)
                    inputs = {k: v for k, v in tok.items()}
                else:
                    enc = _fast_tokenizer.encode(text)
                    ids = enc.ids[:256]
                    inputs = {
                        "input_ids": np.array([ids], dtype=np.int64),
                        "attention_mask": np.ones((1, len(ids)), dtype=np.int64),
                    }
                # only pass tensors the exported model actually expects
                need = {i.name for i in _session.get_inputs()}
                inputs = {k: v for k, v in inputs.items() if k in need}
                out = _session.run(None, inputs)
                hs = out[0]
                vec = hs.mean(axis=1).squeeze(0) if hs.ndim == 3 else hs
                vec = np.asarray(vec, dtype=np.float32)
            else:
                import torch
                tok = _tokenizer(text, return_tensors="pt",
                                 truncation=True, max_length=256)
                with torch.inference_mode():
                    out = _session(**tok).last_hidden_state
                vec = out.mean(dim=1).squeeze(0).numpy().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            return vec.astype(np.float32)
        except Exception as e:
            log.debug("embed_text failed (%s) — hash fallback", e)
    return hash_embed(text)


async def embed_text_async(text: str) -> np.ndarray:
    """Offload embedding to a worker thread — never blocks the event loop."""
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(
        None, embed_text, text)


def embedder_kind() -> str:
    """Which backend is active: 'onnx', 'transformers' or 'hash'."""
    _load()
    return _backend
