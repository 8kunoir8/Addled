"""
Long-term conversational recall.

Embeddings come from backend/memory/embedding.py — ONNX MiniLM when
available, transformers fp32 next, hashed n-grams last. Retrieval fuses
semantic (cosine) + keyword (BM25) hits with reciprocal rank fusion.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("addled.recall")

DIM = 384  # matches vector_store DEFAULT_DIM


def embed_text(text: str, dim: int = DIM) -> np.ndarray:
    """Legacy hashed n-gram embedding (kept for session summaries)."""
    from backend.memory.embedding import hash_embed
    return hash_embed(text, dim)


def _embedder_kind() -> str:
    try:
        from backend.memory.embedding import embedder_kind
        return embedder_kind()
    except Exception:
        return "hash"


def remember(role: str, text: str) -> None:
    """Store a conversation turn in long-term memory (sync; best embedder)."""
    from backend.memory.embedding import embed_text as semantic_embed
    from backend.memory.vector_store import vector_store
    text = (text or "").strip()
    if len(text) < 4:
        return
    try:
        vector_store.add(
            semantic_embed(text),
            category="conversation",
            metadata={"role": role, "text": text[:2000],
                      "embedder": _embedder_kind()},
        )
    except Exception as e:
        log.debug("remember failed: %s", e)


async def remember_async(role: str, text: str) -> None:
    """Store a conversation turn — offloads embedding off the event loop."""
    from backend.memory.embedding import embed_text_async
    from backend.memory.vector_store import vector_store
    text = (text or "").strip()
    if len(text) < 4:
        return
    try:
        vec = await embed_text_async(text)
        row_id = vector_store.add(
            vec, category="conversation",
            metadata={"role": role, "text": text[:2000],
                      "embedder": _embedder_kind()},
        )
        # Relate the memory to any file it names.
        if row_id:
            from backend.memory.autolink import link_text
            link_text("memory", row_id, text, source="auto",
                      extra_note=f"{role}: {text[:80]}")
    except Exception as e:
        log.debug("remember failed: %s", e)


def recall(query: str, top_k: int = 3, min_similarity: float = 0.05) -> list[str]:
    """Legacy hash-embedding recall (used when semantic recall is off)."""
    from backend.memory.vector_store import vector_store
    try:
        hits = vector_store.search(
            embed_text(query), category="conversation",
            top_k=top_k, min_similarity=min_similarity,
        )
        out = []
        for h in hits:
            meta = h.get("metadata") or {}
            text = str(meta.get("text", "")).strip()
            if text:
                out.append(text)
        return out
    except Exception as e:
        log.debug("recall failed: %s", e)
        return []


def build_memory_context(query: str, top_k: int = 3) -> str | None:
    """Legacy context block built from hash recall."""
    memories = recall(query, top_k=top_k)
    if not memories:
        return None
    return _context_block(memories)


def _context_block(memories: list[str]) -> str:
    lines = "\n".join(f"- {m}" for m in memories)
    return ("[Long-term memory] Things you and the user discussed before, "
            f"relevant to this conversation:\n{lines}\n"
            "Use them naturally if they are relevant; ignore if not.")


async def build_memory_context_hybrid(query: str, top_k: int = 3) -> str | None:
    """Semantic (MiniLM) recall fused with BM25 keywords via RRF."""
    from backend.config import config
    from backend.memory.bm25 import bm25_index
    from backend.memory.embedding import embed_text_async
    from backend.memory.vector_store import vector_store

    min_sim = config.get("memory", "min_similarity", default=0.05)
    try:
        vec = await embed_text_async(query)
        sem = vector_store.search(
            vec, category="conversation",
            top_k=top_k * 2, min_similarity=min_sim,
        )
    except Exception as e:
        log.debug("semantic recall failed (%s) — hash fallback", e)
        return build_memory_context(query, top_k=top_k)

    key = []
    if config.get("memory", "hybrid_search", default=True):
        try:
            key = bm25_index.search(query, top_k=top_k * 2)
        except Exception as e:
            log.debug("bm25 search failed: %s", e)

    # Reciprocal rank fusion over semantic + keyword rankings
    fused: dict[int, dict] = {}

    def add(hit_id, score, text):
        text = str(text or "").strip()
        if not text:
            return
        if hit_id not in fused:
            fused[hit_id] = {"score": 0.0, "text": text}
        fused[hit_id]["score"] += score

    for rank, h in enumerate(sem):
        meta = h.get("metadata") or {}
        add(h["id"], 1.0 / (60 + rank), meta.get("text"))
    for rank, k in enumerate(key):
        add(k["id"], 1.0 / (60 + rank), k.get("text"))

    ordered = sorted(fused.values(), key=lambda x: -x["score"])[:top_k]
    texts = [f["text"] for f in ordered]
    if not texts:
        return None
    return _context_block(texts)
