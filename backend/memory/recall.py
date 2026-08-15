"""
Long-term conversational recall.

Embeds text with hashed character n-grams (no external model needed) and
uses the existing VectorStore for cosine retrieval. Wired into chat.send so
the assistant sees relevant things from past conversations automatically.
"""

from __future__ import annotations

import hashlib
import logging
import re

import numpy as np

log = logging.getLogger("addled.recall")

DIM = 384  # matches vector_store DEFAULT_DIM


def embed_text(text: str, dim: int = DIM) -> np.ndarray:
    """Hash n-gram embedding: stable, cheap, works offline."""
    vec = np.zeros(dim, dtype=np.float32)
    text = (text or "").lower()
    tokens = re.findall(r"[\w']+", text)
    # unigrams + bigrams, hashed into buckets
    grams = tokens + [a + "_" + b for a, b in zip(tokens, tokens[1:])]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    return vec


def remember(role: str, text: str) -> None:
    """Store a conversation turn in long-term memory."""
    from backend.memory.vector_store import vector_store
    text = (text or "").strip()
    if len(text) < 4:
        return
    try:
        vector_store.add(
            embed_text(text),
            category="conversation",
            metadata={"role": role, "text": text[:2000]},
        )
    except Exception as e:
        log.debug("remember failed: %s", e)


def recall(query: str, top_k: int = 3, min_similarity: float = 0.05) -> list[str]:
    """Retrieve the most relevant past conversation turns."""
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
    """Return a context block of recalled memories, or None."""
    memories = recall(query, top_k=top_k)
    if not memories:
        return None
    lines = "\n".join(f"- {m}" for m in memories)
    return ("[Long-term memory] Things you and the user discussed before, "
            f"relevant to this conversation:\n{lines}\n"
            "Use them naturally if they are relevant; ignore if not.")
