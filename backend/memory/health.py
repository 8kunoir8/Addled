"""
Memory health — what the RAG stack is actually doing, on this machine.

Written because the interesting failures here are silent. `embedder_kind()`
falling back to `hash` turns semantic recall into meaningless vector comparison
with no visible symptom; a strict similarity gate can admit zero candidates so
"hybrid search" quietly becomes keyword-only; and a store can be empty while the
UI still offers a Memory page. None of that raises, and none of it is visible
from the outside.

This reports facts a caller can act on, and says plainly when a reading is
unavailable rather than guessing.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.memory.health")

# Below this many conversation rows, recall is not really being exercised. Not
# an error — a fresh install starts at zero — but worth saying.
THIN_STORE_ROWS = 25

def report() -> dict:
    """Everything worth knowing about the memory stack, in one call."""
    out: dict = {
        "embedder": _embedder(),
        "store": _store(),
        "index": _index(),
        "settings": _settings(),
        "warnings": [],
    }
    out["warnings"] = _warnings(out)
    return out

def _embedder() -> dict:
    try:
        from backend.memory import embedding
        kind = embedding.embedder_kind()
    except Exception as e:
        return {"kind": "unavailable", "semantic": False,
                "detail": f"{type(e).__name__}: {e}"}
    return {
        "kind": kind,
        # The one that matters: with `hash` there is no meaning in the vectors,
        # and relevance.py deliberately skips semantic scoring because of it.
        "semantic": kind != "hash",
        "detail": {
            "onnx": "ONNX MiniLM-L6-v2 (quantized)",
            "transformers": "transformers MiniLM fp32",
            "hash": ("hashed n-grams — no semantic meaning; semantic recall and "
                     "the semantic half of the relevance gate are inactive"),
        }.get(kind, kind),
    }

def _store() -> dict:
    try:
        from backend.memory.vector_store import vector_store
    except Exception as e:
        return {"available": False, "detail": f"{type(e).__name__}: {e}"}
    if not vector_store.available:
        return {"available": False, "detail": "vectors.db could not be opened"}
    categories: dict[str, int] = {}
    try:
        rows = vector_store._conn.execute(
            "SELECT category, COUNT(*) FROM vectors GROUP BY category"
        ).fetchall()
        categories = {str(cat): int(n) for cat, n in rows}
    except Exception as e:
        return {"available": True, "detail": f"count failed: {e}",
                "categories": {}}
    return {
        "available": True,
        "categories": categories,
        "total": sum(categories.values()),
        "conversations": categories.get("conversation", 0),
        "dim": getattr(vector_store, "_dim", None),
    }

def _index() -> dict:
    """The BM25 side — it carries recall when embeddings are weak."""
    try:
        from backend.memory.bm25 import bm25_index
        before = len(bm25_index._docs)
        try:
            bm25_index.sync()
        except Exception as e:
            return {"docs": before, "detail": f"sync failed: {e}"}
        return {"docs": len(bm25_index._docs)}
    except Exception as e:
        return {"docs": 0, "detail": f"{type(e).__name__}: {e}"}

def _settings() -> dict:
    try:
        from backend.config import config
        get = lambda k, d: config.get("memory", k, default=d)  # noqa: E731
        gate = get("min_similarity", 0.35)
        floor = get("retrieval_min_similarity", gate)
        return {
            "semantic_embeddings": bool(get("semantic_embeddings", True)),
            "hybrid_search": bool(get("hybrid_search", True)),
            "recall_top_k": int(get("recall_top_k", 3) or 3),
            "min_similarity": float(gate),
            "retrieval_min_similarity": float(floor),
            # A floor at or above the gate means every candidate that survives
            # retrieval is kept, which is the opposite failure to the one the
            # floor was added for.
            "floor_below_gate": float(floor) < float(gate),
        }
    except Exception as e:
        return {"detail": f"{type(e).__name__}: {e}"}

def _warnings(report: dict) -> list[str]:
    """Plain-language problems, in the order a user would want to fix them."""
    out: list[str] = []

    embedder = report.get("embedder") or {}
    if embedder.get("semantic") is False and embedder.get("kind") == "hash":
        out.append("No semantic embedder loaded — recall is keyword-only and the "
                   "semantic half of the relevance gate is off.")
    elif embedder.get("kind") == "unavailable":
        out.append(f"The embedder could not be inspected: {embedder.get('detail')}")

    store = report.get("store") or {}
    if store.get("available") is False:
        out.append(f"Memory store is not readable: {store.get('detail')}")
    else:
        conversations = int(store.get("conversations") or 0)
        if conversations == 0:
            out.append("No conversation memories stored yet, so recall has "
                       "nothing to find.")
        elif conversations < THIN_STORE_ROWS:
            out.append(f"Only {conversations} conversation memories — recall "
                       f"will improve as more turns are recorded.")

    index = report.get("index") or {}
    if store.get("available") and int(index.get("docs") or 0) == 0 \
            and int(store.get("conversations") or 0) > 0:
        out.append("BM25 index is empty while memories exist — the keyword half "
                   "of hybrid search is inactive.")

    settings = report.get("settings") or {}
    if settings.get("floor_below_gate") is False and "min_similarity" in settings:
        out.append("retrieval_min_similarity is not below min_similarity, so the "
                   "retrieval floor is doing nothing.")

    return out

async def probe(query: str = "what did we work on", top_k: int = 3) -> dict:
    """Run one real recall and report what came back, and from where.

    This is the check that would have caught the silent degradation: component
    status can look perfect while every result arrives through one half of a
    "hybrid" search.
    """
    from backend.memory.embedding import embed_text_async
    from backend.memory.vector_store import vector_store

    try:
        vec = await embed_text_async(query)
    except Exception as e:
        return {"ok": False, "detail": f"embedding failed: {e}"}

    settings = _settings()
    floor = float(settings.get("retrieval_min_similarity", 0.25))
    top = max(1, top_k)
    try:
        sem = vector_store.search(vec, category="conversation",
                                  top_k=top * 2, min_similarity=floor)
    except Exception as e:
        return {"ok": False, "detail": f"search failed: {e}"}

    key: list[dict] = []
    try:
        from backend.memory.bm25 import bm25_index
        key = bm25_index.search(query, top_k=top * 2)
    except Exception as e:
        log.debug("probe bm25 failed: %s", e)

    out = {
        "ok": True,
        "query": query,
        "retrieval_min_similarity": floor,
        "semantic_candidates": len(sem),
        "keyword_candidates": len(key),
        "top_semantic": [
            {"similarity": round(float(h.get("similarity") or 0), 3),
             "text": str((h.get("metadata") or {}).get("text", ""))[:80]}
            for h in sem[:3]
        ],
    }
    if not sem:
        out["detail"] = ("No semantic candidate cleared the retrieval floor — "
                         "recall is running on keywords alone.")
    return out
