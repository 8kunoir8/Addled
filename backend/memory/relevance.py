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


async def _meetings_by_index(query: str, want: int) -> list[dict]:
    """Candidate meetings from the semantic index, best match first.

    This is what `store.index()` writes its rows FOR. Until this existed the
    index was write-only: both recall paths hardcoded `category="conversation"`
    and nothing searched `category="meeting"`, so meetings were reachable only
    through the file listing -- which finds a meeting when the user's words
    overlap its summary, and misses it when they mean the same thing in
    different words. "When do we need to renew?" against a summary that says
    "renewal deadline" is the case this covers.

    Returns [] for any failure, and for the hash embedder, whose vectors carry
    no meaning to compare. The caller then falls back to the listing, so a
    missing index costs recall quality and never costs recall itself.
    """
    from backend.meetings.store import get
    from backend.memory.embedding import embedder_kind
    if embedder_kind() == "hash":
        return []
    # The RETRIEVAL floor, not SEMANTIC_MIN. They answer different questions:
    # the floor decides who is allowed into the ranking (deliberately low --
    # a candidate that does not make the ranking can never be considered), and
    # the gate decides which ranked candidate earns its tokens (strict).
    #
    # Using the gate for both is the mistake `recall.py` documents at length: it
    # was measured on this machine to admit ~0 semantic candidates, because
    # MiniLM scores a genuine paraphrase at 0.31 -- under the 0.35 gate -- so
    # every result came from keywords and "semantic search" was a description
    # rather than a fact. This function reintroduced exactly that bug: it
    # retrieved at SEMANTIC_MIN, the paraphrase never entered the ranking, and
    # the index looked inert. Keeping the floor below the gate is what makes
    # the gate's semantic half able to see the candidates it is meant to judge.
    from backend.config import config
    floor = float(config.get("memory", "retrieval_min_similarity", default=0.25))
    floor = min(floor, SEMANTIC_MIN)  # never above the gate it feeds
    try:
        from backend.memory.embedding import embed_text
        from backend.memory.vector_store import vector_store
        if not vector_store.available:
            return []
        hits = vector_store.search(
            embed_text(query), category="meeting",
            top_k=max(want * 2, 6), min_similarity=floor)
    except Exception as e:  # noqa: BLE001
        log.debug("meeting index search failed: %s", e)
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for h in hits:
        mid = str((h.get("metadata") or {}).get("meeting_id") or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        try:
            m = get(mid)
        except Exception as e:  # noqa: BLE001
            log.debug("meeting %s unreadable: %s", mid, e)
            continue
        # An index row whose meeting is gone, or has lost its summary, is
        # stale: `unindex` should have removed it, and if it did not, this is
        # where that shows up rather than as a block with a missing body.
        if m and m.get("summary"):
            m.setdefault("id", mid)
            out.append(m)
        if len(out) >= want:
            break
    return out


async def _meetings_by_listing(want: int) -> list[dict]:
    """Recent summarised meetings, newest first -- the fallback path."""
    from backend.meetings.store import list_meetings
    try:
        meetings = list_meetings(limit=max(want * 4, 12))
    except Exception as e:  # noqa: BLE001
        log.debug("could not list meetings: %s", e)
        return []
    return [m for m in meetings if m.get("summary")]


async def meetings_block(query: str, limit: int = 3) -> str | None:
    """Meeting summaries, minus the meetings unrelated to this request.

    Gated for the same reason the session summaries are, and more so: a
    meeting is a long, specific, other-people's-words document, and
    injecting one into a turn that is not about it is how the model ends up
    answering a question nobody asked.

    Two passes, and the order is the point. The semantic index supplies
    meetings that *mean* the same as the question; the file listing supplies
    the recent ones the index cannot see (never indexed, or indexed before the
    embedder was available). The index goes first because it is the only half
    that can match a paraphrase, and the listing then fills the remaining
    slots so a turn still reaches recent meetings when the index contributes
    nothing.
    """
    from backend.meetings.store import compose_meeting_block

    candidates = await _meetings_by_index(query, limit)
    have = {str(m.get("id") or "") for m in candidates}
    if len(candidates) < limit:
        for m in await _meetings_by_listing(limit):
            mid = str(m.get("id") or "")
            if mid and mid in have:
                continue
            have.add(mid)
            candidates.append(m)
            if len(candidates) >= limit * 2:
                break

    kept = []
    for m in candidates:
        # Gate on the FIELDS, not one concatenated blob.
        #
        # `keep` compares the query against whatever string it is handed, and a
        # long unrelated field dilutes a short relevant one: measured on this
        # machine, the query "when does the subscription expire..." scores 0.42
        # against a meeting's summary but only 0.31 against the same summary
        # once the title "Northwind contract review" is prefixed. The title
        # shares no words with the question, so it adds nothing but length --
        # and that was enough to drop a genuinely relevant meeting under the
        # 0.35 gate. Scoring each field asks the right question ("does any part
        # of this meeting bear on the query?") instead of "is the average of
        # this meeting close to the query?", which no real meeting ever is.
        parts = [str(m.get("summary") or ""), str(m.get("title") or "")]
        parts += [str(d) for d in (m.get("decisions") or [])]
        if any([await keep(query, part) for part in parts if part.strip()]):
            kept.append(m)
        if len(kept) >= limit:
            break
    return compose_meeting_block(kept)
