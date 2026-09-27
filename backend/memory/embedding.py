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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

log = logging.getLogger("addled.embedding")

DIM = 384

# The thread pool embedding runs on. Deliberately small and PRIVATE to this
# module: `embed_text_async` used the loop's default executor, which is shared
# with every other `run_in_executor`/`to_thread` caller, so a long re-embed
# sweep could occupy all of it and make unrelated work queue behind a CPU-bound
# ONNX call. Two workers is enough to overlap embedding with the event loop
# without monopolising the machine.
_EMBED_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")

MODEL_DIR = Path(__file__).parent / "models" / "minilm-l6-v2"
ONNX_PATH = MODEL_DIR / "model.onnx"
TRANSFORMERS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# The multilingual alternative, same 384 dimensions so the store's schema and
# every stored blob remain valid.
#
# It exists because all-MiniLM-L6-v2 is English-centric, and measured on this
# machine it scored the user's Indonesian and code-mixed turns at 0.23-0.33 —
# under the relevance gate, so semantic recall contributed almost nothing and
# keyword search carried every result. A multilingual encoder is the actual fix
# for that; the retrieval floor only stops the symptom.
#
# NOT bundled, and NOT downloaded automatically. It is ~470 MB, this is a
# setting a user has to choose, and fetching a model nobody asked for would be
# exactly the kind of surprise the rest of the app avoids.
MULTILINGUAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDER_MODELS = {
    "minilm": TRANSFORMERS_MODEL,
    "multilingual": MULTILINGUAL_MODEL,
}

# Identifies the vectorization scheme, not the model. Bump on any change to
# tokenization, pooling or normalization that alters the vectors themselves.
# It is part of the row tag because such a change leaves the model name
# identical while making every stored vector incomparable.
#   v1 — masked mean pooling (see the note in embed_text)
POOL_VERSION = "v1"

_session = None       # onnxruntime.InferenceSession OR transformers AutoModel
_tokenizer = None     # transformers tokenizer (used by both backends)
_fast_tokenizer = None  # tokenizers.Tokenizer — works without transformers
_backend = "uninitialized"   # "onnx" | "transformers" | "hash"
_source = "minilm"    # which model produced the vectors — recorded per row
# Set the first time an embedding falls back to hashing. Reported so a broken
# semantic path is visible rather than inferred from poor recall.
_degraded = False

def configured_source() -> str:
    """Which embedding model the user asked for: 'minilm' or 'multilingual'."""
    try:
        from backend.config import config
        value = str(config.get("memory", "embedder", default="minilm") or "")
        return value if value in EMBEDDER_MODELS else "minilm"
    except Exception:
        return "minilm"

def source() -> str:
    """The model actually in use. Named so rows can record where they came from."""
    return _source

def model_available(name: str) -> bool:
    """Whether `name`'s weights are present locally, without downloading.

    Only a local check. A false answer here is why the multilingual option
    cannot simply be switched on: the weights are not on this machine, and
    fetching them is a deliberate, visible action rather than a side effect of
    changing a setting.
    """
    try:
        if name == "multilingual":
            from transformers.utils import cached_file
            cached_file(MULTILINGUAL_MODEL, "config.json")
            return True
        return ONNX_PATH.exists()
    except Exception:
        return False


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

    # 2) transformers fp32 (torch + transformers ship with the app bundle).
    # The configured source decides which model: `minilm` here, or the
    # multilingual encoder when the user has chosen it *and* it is cached.
    global _source
    wanted = configured_source()
    candidates = [wanted, "minilm"] if wanted != "minilm" else ["minilm"]
    for name in candidates:
        if name != "minilm" and not model_available(name):
            log.info("Embedder: '%s' requested but its weights are not cached "
                     "— staying on minilm", name)
            continue
        try:
            from transformers import AutoTokenizer, AutoModel
            _tokenizer = AutoTokenizer.from_pretrained(EMBEDDER_MODELS[name])
            _session = AutoModel.from_pretrained(EMBEDDER_MODELS[name])
            _session.eval()
            _backend = "transformers"
            _source = name
            log.info("Embedder: transformers %s loaded", EMBEDDER_MODELS[name])
            return True
        except Exception as e:
            log.warning("transformers embedder '%s' failed (%s)", name, e)

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
                    # `tokenizer.json` pads to a fixed length (128 here), so
                    # `ids` is mostly `[PAD]`. The mask must come from the
                    # tokenizer — building it as all-ones marked every padding
                    # position as real content, and pooling then averaged the
                    # `[PAD]` embedding into the sentence vector. Because short
                    # sentences are mostly padding, that pulled every vector
                    # toward the same point: unrelated text scored 0.87 and
                    # gibberish ("zzzz qqqq" vs "aaaaaaaa") scored 0.96, above
                    # genuinely related pairs. Dense is not the same as
                    # discriminative, and these vectors were only dense.
                    mask = enc.attention_mask[:256]
                    inputs = {
                        "input_ids": np.array([ids], dtype=np.int64),
                        "attention_mask": np.array([mask], dtype=np.int64),
                    }
                # only pass tensors the exported model actually expects
                need = {i.name for i in _session.get_inputs()}
                # Supply any input the model requires but the tokenizer did not
                # produce — `token_type_ids` for this MiniLM export. Filtering
                # alone cannot do this: it removes extras, and a *missing
                # required* input made `session.run` raise, which the handler
                # below swallowed into the hashed fallback. The result was an
                # embedder that reported "onnx" while every vector it produced
                # was a hash, so semantic recall silently did nothing.
                for required in need:
                    if required not in inputs:
                        inputs[required] = np.zeros(
                            (1, len(inputs.get("input_ids", [[]])[0])),
                            dtype=np.int64)
                inputs = {k: v for k, v in inputs.items() if k in need}
                out = _session.run(None, inputs)
                hs = out[0]
                # Masked mean pooling, matching the Sentence-Transformers model
                # card. Plain `hs.mean(axis=1)` averages the padding positions
                # too, which flattens the vector (see the note above the mask).
                if hs.ndim == 3:
                    m = inputs["attention_mask"].astype(np.float32)[..., None]
                    vec = (hs * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
                    vec = vec.squeeze(0)
                else:
                    vec = hs
                vec = np.asarray(vec, dtype=np.float32)
            else:
                import torch
                tok = _tokenizer(text, return_tensors="pt",
                                 truncation=True, max_length=256)
                with torch.inference_mode():
                    out = _session(**tok).last_hidden_state
                # Same masked pooling as the ONNX branch above; transformers
                # pads to the longest item in the batch, so `dim=1` over the
                # raw states would fold padding in here as well.
                mask = tok["attention_mask"].unsqueeze(-1).to(out.dtype)
                summed = (out * mask).sum(dim=1)
                counts = mask.sum(dim=1).clamp(min=1e-9)
                vec = (summed / counts).squeeze(0).numpy().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm > 0:
                vec = vec / norm
            return vec.astype(np.float32)
        except Exception as e:
            # Warning, not debug. Falling back to hashed n-grams means every
            # vector this function returns from now on is meaningless for
            # similarity, while `embedder_kind()` keeps reporting the semantic
            # backend. That mismatch hid a real failure for an unknown length of
            # time, so it is said at a level that reaches the log by default.
            global _degraded
            if not _degraded:
                _degraded = True
                log.warning("embed_text failed (%s: %s) — falling back to hashed "
                            "embeddings; semantic recall is now inactive",
                            type(e).__name__, e)
    return hash_embed(text)


async def embed_text_async(text: str) -> np.ndarray:
    """Offload embedding to a worker thread — never blocks the event loop.

    Uses a small DEDICATED pool rather than the loop's default executor.
    `run_in_executor(None, ...)` queues on the shared pool, whose worker count
    is `min(32, cpu+4)`; a re-embed sweep (100s of rows) or the quadratic
    duplicate scan in `autolink.link_near_duplicates` occupies every worker with
    a CPU-bound ONNX call, and unrelated work queued on that same pool —
    `search_in_files`, static assets in the remote gateway, Kokoro TTS — waits
    behind it. Two workers keep embedding busy without starving everything else.
    """
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(
        _EMBED_POOL, embed_text, text)


def embedder_kind() -> str:
    """Which backend is active: 'onnx', 'transformers' or 'hash'."""
    _load()
    return _backend

def embedder_id() -> str:
    """The embedder *and its model*, for tagging stored rows.

    `embedder_kind()` alone is not enough to keep a store consistent: 'onnx' and
    'transformers' can be different models, and two different 384-dim models
    produce incomparable vectors. Rows are tagged with this instead, so a model
    change is detectable and `reembed.stale_rows` can migrate them.

    The `POOL_VERSION` suffix exists because the vectors changed meaning without
    the model changing. Until it was added, this file mean-pooled the padding
    positions into every vector, which flattened them (unrelated text scored
    0.87; gibberish 0.96). Fixing the pooling left the id at `onnx:minilm`, so
    every row written under the broken scheme looked current and no migration
    would have run. Bump this whenever the numbers a row is built from change.
    """
    _load()
    if _backend == "hash":
        return "hash"
    if _backend == "onnx":
        return f"onnx:minilm:{POOL_VERSION}"
    return f"transformers:{_source}:{POOL_VERSION}"
