"""
Semantic re-embedding migration.

Legacy memory rows were embedded with hashed n-grams (no 'embedder' key in
metadata). This migrates them in batches to the current semantic embedder
(ONNX MiniLM or transformers) in the background, so old memories become
meaning-searchable too. Idempotent: tagged rows are skipped.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.reembed")

BATCH = 100


async def reembed_legacy_batch(max_rows: int = BATCH) -> int:
    """Re-embed up to max_rows legacy conversation rows. Returns count done."""
    from backend.memory import embedding

    if embedding.embedder_kind() == "hash":
        return 0  # nothing better to embed with — leave legacy rows alone

    from backend.memory.vector_store import vector_store
    try:
        rows = vector_store.list(category="conversation", limit=5000)
    except Exception as e:
        log.debug("reembed list failed: %s", e)
        return 0

    legacy = [r for r in rows if not (r.get("metadata") or {}).get("embedder")]
    if not legacy:
        return 0

    done = 0
    kind = embedding.embedder_kind()
    for r in legacy[:max_rows]:
        text = str((r.get("metadata") or {}).get("text", "")).strip()
        if not text:
            continue
        try:
            vec = await embedding.embed_text_async(text)
            if vector_store.reembed(int(r["id"]), vec, kind):
                done += 1
        except Exception as e:
            log.debug("reembed row %s failed: %s", r.get("id"), e)
    if done:
        log.info("Re-embedded %d legacy memory rows (%s)", done, kind)
    return done
