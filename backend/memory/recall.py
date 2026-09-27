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
    """The embedder *and model* a row was written with.

    Deliberately not `embedder_kind()`: 'onnx' and 'transformers' can be
    different models, and two different 384-dim models produce incomparable
    vectors. Tagging with the model is what lets a change be detected and the
    old rows migrated instead of silently mixed in.
    """
    try:
        from backend.memory.embedding import embedder_id
        return embedder_id()
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
    """Semantic (MiniLM) recall fused with BM25 keywords via RRF.

    Two similarity bars, not one, because they answer different questions:

    * the *retrieval floor* decides who is allowed into the ranking. It is
      deliberately low — a candidate that does not make the ranking can never
      be considered, so this bar should only exclude genuine noise.
    * the *relevance gate* decides which of the ranked candidates is worth its
      tokens in the prompt, and stays strict.

    Using the gate for both was measured on this machine to admit ~0 semantic
    candidates: English-centric MiniLM scores Indonesian and code-mixed turns at
    0.23-0.33, under the 0.35 gate, so every recall came from BM25 alone and
    "hybrid search" was a description rather than a fact.
    """
    from backend.config import config
    from backend.memory.bm25 import bm25_index
    from backend.memory.embedding import embed_text_async
    from backend.memory.vector_store import vector_store

    gate = float(config.get("memory", "min_similarity", default=0.35))
    floor = float(config.get("memory", "retrieval_min_similarity",
                             default=gate))
    # Never let a misconfigured floor sneak past the gate it feeds.
    floor = min(floor, gate)
    try:
        vec = await embed_text_async(query)
        sem = vector_store.search(
            vec, category="conversation",
            top_k=top_k * 2, min_similarity=floor,
        )
    except Exception as e:
        log.debug("semantic recall failed (%s) — hash fallback", e)
        return build_memory_context(query, top_k=top_k)

    # Appended to the context block so the reason a turn recalled nothing is
    # visible in the chat UI rather than guessed at.
    if not sem:
        log.debug("recall: no semantic candidate cleared %.2f", floor)

    key = []
    if config.get("memory", "hybrid_search", default=True):
        try:
            key = bm25_index.search(query, top_k=top_k * 2)
        except Exception as e:
            log.debug("bm25 search failed: %s", e)

    # Reciprocal rank fusion over semantic + keyword rankings
    fused: dict[int, dict] = {}

    def add(hit_id, score, text, similarity=None):
        text = str(text or "").strip()
        if not text:
            return
        if hit_id not in fused:
            fused[hit_id] = {"score": 0.0, "text": text,
                             "similarity": similarity}
        fused[hit_id]["score"] += score
        # Keep the best similarity seen for this row, so the gate below judges
        # a semantic hit on its own merit rather than on a fused rank.
        if similarity is not None:
            current = fused[hit_id].get("similarity")
            if current is None or similarity > current:
                fused[hit_id]["similarity"] = similarity

    for rank, h in enumerate(sem):
        meta = h.get("metadata") or {}
        add(h["id"], 1.0 / (60 + rank), meta.get("text"),
            h.get("similarity"))

    for rank, k in enumerate(key):
        add(k["id"], 1.0 / (60 + rank), k.get("text"))

    ordered = sorted(fused.values(), key=lambda x: -x["score"])

    # The gate applies to the fused result. A row that reached the ranking on
    # keyword match alone has no similarity and is kept — BM25 already decided
    # it was relevant, and that is the half of hybrid search that works well
    # here. A row that only just cleared the floor is judged on its own score.
    kept = []
    for item in ordered:
        similarity = item.get("similarity")
        if similarity is not None and similarity < gate:
            continue
        kept.append(item)
        if len(kept) >= top_k:
            break

    texts = [f["text"] for f in kept]
    if not texts:
        return None
    return _context_block(texts)
