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

# -- conversation history ------------------------------------------------------
# The blocks above are memories about *other* moments. The conversation window
# is the one part of the prompt that describes *this* one, and it was the only
# part not passed through a gate — so a 54-character question about a brand new
# subject went out with 4270 characters of the previous subject attached, and
# the model answered that instead.
#
# The signal that separates the two cases is not length and not similarity. It
# is whether the new message carries any content word of its own:
#
#   * no content words ("continue", "why?", "kenapa?", "lanjutkan", "and then")
#     -> the turn has no topic to stand on, so it is *about* the previous one.
#        Keeping history is mandatory: this is the ellipsis the answer lives in.
#   * any content word ("bro one off admin management itu maksudnya yg gimana ?")
#     -> the turn can stand alone, so it is a candidate new topic.
#
# This is what Arguello & Rose call "Content Contribution" ("Binary-valued, is
# there a non-stopword term in the current contribution?") -- introduced exactly
# because a term-overlap method "wrongly classifies all of these as starts of
# new topics" -- and Kumar & Callan's Cat1 ("self-contained query ... demarcates
# a contextual shift") versus Cat2-imp split. The hardcoded list of follow-up
# words it replaces could only ever be an incomplete guess at this.


def content_words(text: str) -> set[str]:
    """The tokens that carry a topic: the shared tokenizer, minus the stopwords."""
    return _tokens(text)


# Bare continuations that are *shaped* like content words: "continue",
# "explain", "lanjutkan" and "jelaskan" all survive the stopword filter, because
# they are ordinary verbs with no topic attached — the tokenizer cannot tell
# "explain" from "explain how DNS works". The content-word test covers the
# open-ended half of this (function words, bare pronouns, "and then") and this
# covers the closed half. It is the list ws_server already held, widened to the
# same idea: a word that asks for more of the previous turn rather than naming a
# subject. It only ever *keeps* history, so the cost of a word being wrong here
# is one stale window, never a lost question.
_FOLLOWUP_WORDS = frozenset("""
    continue continue? go on keep going more again same next and then
    why why? explain jelaskan how so how come what about
    kenapa kenapa? mengapa bagaimana lanjutkan lalu terus lanjut
    kok kok bisa gimana gimana? apa apa? caranya
    yes yeah yep ok okay oke sip lanjutkan
""".split())


def is_bare_followup(text: str) -> bool:
    """True when the turn has no topic of its own and so inherits the last one.

    A message with no content words cannot be answered in isolation; whatever it
    means is in what came before. A message that is *only* a continuation word
    ("continue", "explain", "lanjutkan") is the same case, and has to be caught
    by name because those words look like content to the tokenizer.

    Both tests err toward keeping history, which is the safe direction: the
    expensive mistake is depriving a follow-up of the turn it depends on.
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    if not content_words(stripped):
        return True
    # A short message made of nothing but continuation words.
    words = [w for w in _WORD.findall(stripped.lower()) if len(w) > 1]
    if words and all(w in _FOLLOWUP_WORDS for w in words):
        return True
    return stripped.lower() in _FOLLOWUP_WORDS

# The assistant's own last turn can be why the user's says almost nothing: it
# is blocked on an approval, it asked a question, or it offered to do
# something. In those cases part of the meaning lives in the window rather than
# in the new message, so no property of the message alone can find it.
#
# The two are not equal, and the distinction is the reported bug: the prior
# turn there ended by OFFERING ("Want me to go ahead and read ...?") while the
# user asked about admin management at a cosine of 0.12. A pending approval
# genuinely gates the next turn; an offer does not.
_AWAITING = re.compile(
    r"\b(needs your approval|waiting on your approval|waiting for your "
    r"approval|pending approval|awaiting (?:your )?approval|"
    r"approve it in the dashboard|perlu persetujuan|setujui|izin)\b", re.I)
# "Did it end by asking something?" -- deliberately cheap, and only ever a
# tie-breaker, because a long reply can end in a question while the user moves
# to a different subject.
_ASKED = re.compile(r"(\?\s*$|\?\s*[\"']?\s*$)", re.M)


def _prior_turn(window: list[dict]) -> str:
    """The assistant's most recent message in the window, or ''."""
    for m in reversed(window or []):
        if (m.get("role") or "") == "assistant":
            content = m.get("content")
            return content if isinstance(content, str) else ""
    return ""


async def continues_topic(message: str, window: list[dict]) -> bool:
    """Should this turn keep the raw conversation window as context?

    Deliberately one-sided: every test here can only *keep* history, and only a
    turn that carries its own subject, shares nothing with the window, and is
    not answering the assistant's last turn is treated as a new topic. The
    expensive mistake is the other direction -- an ACL'22 error analysis puts
    coreference at 44% of the damage done by changing history.

    The semantic half may only *confirm* a continuation, never manufacture one:
    the default embedder is English-centric and scores Indonesian and code-mixed
    turns at 0.23-0.33, so a low cosine is noise here, not a signal.
    """
    if is_bare_followup(message):
        return True
    recent = " ".join(str(m.get("content") or "") for m in (window or [])[-4:])
    if not recent.strip():
        return True
    if lexical(message, recent) >= LEXICAL_MIN:
        return True
    prior = _prior_turn(window)
    # Blocked on an approval: whatever comes next is about that, and the user
    # cannot move on until it clears. This one overrides.
    if prior and _AWAITING.search(prior):
        return True
    score = await semantic(message, recent)
    # The assistant asked something. That is real but much weaker than the two
    # signals above: a long reply can end in a question while the user answers
    # something else entirely. The reported bug is exactly that — the prior turn
    # asked about `skill_read` and the user asked about admin management, at a
    # cosine of 0.12. So this only decides when the two texts are otherwise
    # close; a near-zero cosine beats it.
    if prior and _ASKED.search(prior) and score >= SEMANTIC_MIN:
        return True
    # No shared words and it has its own content: a new topic. The bar is high
    # because the default embedder mis-scores Indonesian and code-mixed turns
    # at 0.23-0.33, so an unremarkable cosine must not pull a new question back
    # into the old subject.
    return score >= SEMANTIC_MIN * 1.5


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
