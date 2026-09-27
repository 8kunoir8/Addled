"""
Semantic re-embedding migration.

Two kinds of stale row live in the store, and both need the same fix:

* untagged rows, from before the `embedder` key existed (hashed n-grams);
* rows tagged with a *different* embedder than the one now loaded.

The second case is the dangerous one. all-MiniLM-L6-v2 and
paraphrase-multilingual-MiniLM-L12-v2 both emit 384 floats, so the store accepts
a mixture of the two without complaint. Cosine similarity between vectors from
two different models is meaningless, so a half-migrated store returns confident
nonsense rather than an error. Only the first case used to be handled, which
meant changing the embedder silently corrupted ranking.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.reembed")

BATCH = 100

def stale_rows(rows: list[dict], kind: str) -> list[dict]:
    """Rows whose vector did not come from `kind`.

    A row with no tag is stale by definition; a row tagged with anything other
    than the current embedder is stale too. That second half is what was
    missing: changing the embedder left every existing row tagged with the old
    model, and because both models emit 384 floats the store accepted the mix
    silently. Cosine similarity between two different models' vectors is
    meaningless, so a half-migrated store returns confident nonsense instead of
    an error.
    """
    stale = []
    for row in rows:
        tagged = (row.get("metadata") or {}).get("embedder")
        if tagged != kind:
            stale.append(row)
    return stale

async def reembed_legacy_batch(max_rows: int = BATCH) -> int:
    """Re-embed up to max_rows stale conversation rows. Returns count done."""
    from backend.memory import embedding

    kind = embedding.embedder_id()
    if kind == "hash":
        return 0

    from backend.memory.vector_store import vector_store
    try:
        rows = vector_store.list(category="conversation", limit=5000)
    except Exception as e:
        log.debug("reembed list failed: %s", e)
        return 0

    legacy = stale_rows(rows, kind)
    if not legacy:
        return 0

    done = 0
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
        log.info("Re-embedded %d memory rows with %s", done, kind)
    return done

async def reembed_all(stop_after: int = 20000) -> dict:
    """Migrate the whole store to the current embedder, in batches.

    Returns a report rather than a count so a partial migration is visible: a
    store left half-migrated is worse than one never migrated, because the
    mixture cannot be detected from a similarity score.
    """
    from backend.memory import embedding
    kind = embedding.embedder_id()
    if kind == "hash":
        return {"ok": False, "migrated": 0, "remaining": None,
                "detail": "No semantic embedder is loaded, so there is nothing "
                          "to migrate to."}

    from backend.memory.vector_store import vector_store
    migrated = 0
    while migrated < stop_after:
        done = await reembed_legacy_batch()
        if not done:
            break
        migrated += done

    try:
        rows = vector_store.list(category="conversation", limit=5000)
        remaining = len(stale_rows(rows, kind))
    except Exception as e:
        log.debug("reembed verify failed: %s", e)
        remaining = None

    return {
        "ok": remaining == 0,
        "migrated": migrated,
        "remaining": remaining,
        "embedder": kind,
        "detail": ("%s row(s) still on another embedder" % remaining
                   if remaining else
                   "All conversation memories now use %s." % kind),
    }
