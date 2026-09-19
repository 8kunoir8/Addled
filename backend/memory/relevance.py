"""
Relevance gate for the context blocks that describe *other* moments.

Facts, timeline days, session summaries and rolling compaction summaries are all
about the past. Injecting them into every prompt regardless of what was asked is
how a bare "yoo apakabar ?" came back as a screen resolution: the model was handed
a wall of stale context and no question, so it echoed the most recent thing it
recognised.

A memory earns its place in the prompt only when it is close to what the user just
said. Word overlap settles most cases for free; the embedder covers the paraphrase
overlap misses, and is skipped when only the hash fallback is loaded, because
those vectors carry no meaning to compare. Neither half loads anything of its own
— it reuses the embedder recall already needs for the turn.

The bias is deliberate: dropping a memory that would have helped costs the agent a
little context, while keeping one that does not fit changes the answer.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("addled.relevance")

# A memory has to clear one of these to be injected. The semantic bar is the one
# the procedure matcher settled on ("a recipe for something else is worse than no
# recipe"); the lexical bar is the same idea in word-overlap units.
SEMANTIC_MIN = 0.35
LEXICAL_MIN = 0.34

# Words that turn up in almost every memory, so sharing one proves nothing.
_STOP = frozenset(
    "about after again against all also and any are because been before being "
    "between both but came can cannot could did does doing done down during each "
    "few for from further get give given got had has have having here how into "
    "its itself just know like made make many may might more most much must need "
    "now only other our out over own please same should some such tell than that "
    "the their them then there these they this those through too under until "
    "very want was were what when where which while who why will with within "
    "would you your yourself user users assistant addled".split())

_WORD = re.compile(r"[a-z0-9_]+")

# Embedded text is cached: the same fact is scored against every turn, and the
# embedder is the only slow part. Bounded so a long session cannot grow it without
# limit.
_CACHE: dict[str, object] = {}
_CACHE_MAX = 512


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOP}


def lexical(query: str, text: str) -> float:
    """Share of the query's content words that the text contains."""
    q = _tokens(query)
    if not q:
        return 0.0
    return len(q & _tokens(text)) / len(q)


async def _vector(text: str):
    """Cached embedding, or None when only the meaningless hash one is loaded."""
    from backend.memory.embedding import embed_text_async, embedder_kind
    if embedder_kind() == "hash":
        return None
    hit = _CACHE.get(text)
    if hit is not None:
        return hit
    vec = await embed_text_async(text)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[text] = vec
    return vec


async def semantic(query: str, text: str) -> float:
    """Cosine between the two embeddings, or 0.0 when there is no real embedder."""
    try:
        a = await _vector(query)
        b = await _vector(text)
    except Exception as e:
        log.debug("relevance: embedder unavailable (%s)", e)
        return 0.0
    if a is None or b is None:
        return 0.0
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


async def keep(query: str, text: str) -> bool:
    """True when this memory is close enough to the query to be worth its tokens.

    A query of nothing but filler ("hi", "yoo apakabar ?") matches nothing by
    design: there is no topic to relate a memory to, so the reply is built from
    the conversation alone.
    """
    if not text:
        return False
    if lexical(query, text) >= LEXICAL_MIN:
        return True
    return await semantic(query, text) >= SEMANTIC_MIN


async def select(query: str, items: list[str]) -> list[str]:
    """Keep the items that relate to the query, in their original order."""
    kept = []
    for item in items:
        if await keep(query, item):
            kept.append(item)
    return kept


# -- gated blocks --------------------------------------------------------------
# Each mirrors the corresponding build_*_context() but drops the unrelated
# entries; the wording itself lives in the store modules so there is one copy.


async def facts_block(query: str, max_facts: int = 20) -> str | None:
    """Durable facts, minus the ones unrelated to what was just said."""
    from backend.memory.facts import compose_facts, get_facts
    items = [f.get("text", "") for f in get_facts(max_facts)]
    return compose_facts(await select(query, items))


async def timeline_block(query: str, days: int = 3) -> str | None:
    """Recent day summaries, minus the days unrelated to this request."""
    from backend.memory.journal import (JOURNAL_DIR, compose_timeline,
                                        list_days)
    if not JOURNAL_DIR.exists():
        return None
    kept: list[tuple[str, str]] = []
    for meta in list_days(limit=days):
        summary = meta.get("summary") or ""
        if await keep(query, summary):
            kept.append((meta["date"], summary))
    return compose_timeline(kept)


async def summaries_block(query: str, limit: int = 3) -> str | None:
    """Session summaries, minus the sessions unrelated to this request."""
    from backend.memory.session_summary import (compose_session_block,
                                                get_recent_summaries)
    summaries = [s.get("summary", "") for s in get_recent_summaries(limit)]
    return compose_session_block(await select(query, summaries))
