"""
Rolling mid-session compaction — keeps long conversations in context.

When the current conversation outgrows the configured threshold, the oldest
third is summarized (LLM when available, heuristic otherwise) into
rolling_summary.json. Every future chat injects the recent rolling summaries
as an "[Earlier in this conversation]" block, so early context survives even
though the raw turns fall out of the short-term window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.compaction")

from backend import app_paths

ROLLING_PATH = app_paths.MEMORY_DIR / "rolling_summary.json"
MAX_ENTRIES = 10
PROVIDER_BACKOFF_S = 15 * 60

# Compaction summarizes with the *same* provider chat uses. A local model serves
# one generation at a time, so summarizing while the user was mid-conversation
# put their next message behind a large summarization prompt — the bot bridge
# saw that as "Request chat.send timed out" on the second message. Background
# work yields to the user: a turn marks activity, and compaction waits for a
# quiet gap before spending the model.
QUIET_WINDOW_S = 15.0
QUIET_WAIT_MAX_S = 600.0
# Cap what a single summarization sends. The chunk used to be a third of the
# whole backlog, which on a long conversation is thousands of tokens of prompt
# on a CPU model; folding it in smaller pieces keeps each job short.
MAX_CHUNK = 20

# How much of the conversation is kept word-for-word behind the summary.
#
# Compaction rewrites the live list, so this is the one number that decides how
# much the model can still quote exactly after a rewrite. A summary is lossy,
# and the turns a user is most likely to refer back to are the recent ones.
KEEP_TAIL = 12

# A long message is worth keeping in full only while it is recent. Older ones are
# truncated in place rather than dropped, which preserves the fact that the turn
# happened without paying for the whole payload. This is Claude Code's "clear
# older tool outputs first" step, adapted: Addled stores a tool result inside the
# assistant's prose rather than as a separate `tool` message, so the unit here is
# a long message rather than a tool-result block.
TRUNCATE_AFTER_KEEP = 400

# Thrashing guard. Rewriting is what makes the prompt smaller, but rewriting on
# every turn would summarize a conversation to death — each pass loses detail the
# next cannot recover, and the visible history never settles.
#
# The signal is recurrence (how many turns were added since the last rewrite),
# not wall-clock: a conversation that has added two turns since the last
# compaction has nothing new to fold, however long the user sat there thinking.
MIN_TURNS_BETWEEN_COMPACTIONS = 8

_locked = False           # single-flight guard
_last_provider_error = 0.0
_last_activity = 0.0


def note_activity() -> None:
    """Record an interactive turn so compaction keeps out of the way."""
    global _last_activity
    _last_activity = time.time()


def _idle_for() -> float:
    return time.time() - _last_activity


async def _wait_for_quiet(max_wait: float | None = None) -> bool:
    """Wait until no interactive turn has run for QUIET_WINDOW_S seconds.

    The window and the cap are read from the module at call time (not bound as
    defaults) so a test can shorten them instead of waiting out the real ones.
    """
    if max_wait is None:
        max_wait = QUIET_WAIT_MAX_S
    deadline = time.time() + max_wait
    while _idle_for() < QUIET_WINDOW_S:
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(1.0, remaining))
    return True


def _load() -> dict:
    if ROLLING_PATH.exists():
        try:
            state = json.loads(ROLLING_PATH.read_text(encoding="utf-8"))
            if isinstance(state, dict) and "entries" in state:
                return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"conversation_id": None, "entries": [], "summarized": 0}


def _save(state: dict) -> None:
    try:
        ROLLING_PATH.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not save rolling summary: %s", e)


async def _summarize_chunk(messages: list[dict]) -> str:
    """Summarize a chunk of turns — LLM first, heuristic fallback."""
    from backend.providers.registry import get_provider
    global _last_provider_error

    provider = None
    try:
        provider = get_provider()
    except Exception:
        provider = None

    if provider is not None and time.time() - _last_provider_error > PROVIDER_BACKOFF_S:
        from backend.providers import router
        try:
            transcript = "\n".join(
                f"{m['role']}: {str(m['content'])[:400]}" for m in messages)
            prompt = ("Summarize this part of an ongoing conversation in "
                      "2-3 short bullets: topics discussed, decisions made, "
                      "user preferences. Be concise and factual.\n\n"
                      + transcript)
            result = await provider.chat(
                [{"role": "user", "content": prompt}],
                model=router.for_provider(provider, "utility"),
                max_tokens=300, temperature=0.3)
            if result.ok and result.response and not result.response.startswith("["):
                summary = result.response.strip()
                if summary:
                    return summary
        except Exception as e:
            log.debug("LLM compaction failed, heuristic: %s", e)
            _last_provider_error = time.time()

    # Heuristic fallback: recent exchanges in the chunk (both user intent and agent key action)
    bullets: list[str] = []
    seen: set[str] = set()
    for m in messages:
        role = m.get("role", "")
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        first_line = content.split("\n", 1)[0].strip()[:120]
        if not first_line or first_line in seen:
            continue
        seen.add(first_line)
        if role == "user":
            bullets.append(f"User: {first_line}")
        elif role == "assistant" and bullets and not bullets[-1].startswith("Agent:"):
            bullets.append(f"Agent: {first_line}")
        if len(bullets) >= 6:
            break
    if not bullets:
        return ""
    return "Earlier exchanges: " + " | ".join(bullets)

def _rewritten(all_msgs: list[dict], keep_from: int) -> list[dict]:
    """The message list to store after folding everything before `keep_from`.

    Two reductions, in the order Claude Code applies them:

      1. **Truncate** long messages older than the verbatim tail. The message
         stays — a user reading the history still sees that the turn happened
         and how it opened — but the payload stops being re-sent every turn.
      2. **Drop** what remains before `keep_from`, because it has just been
         summarized and is the part the rolling summary will speak for.

    Returns a new list of new dicts. The caller's list came from
    `get_context()`, which returns slices of the live store's own dicts, so
    editing in place would rewrite the store before the summary is saved.
    """
    out: list[dict] = []
    for i, m in enumerate(all_msgs):
        if i < keep_from:
            continue
        msg = dict(m)
        if i < len(all_msgs) - KEEP_TAIL:
            content = str(msg.get("content") or "")
            if len(content) > TRUNCATE_AFTER_KEEP:
                msg["content"] = (content[:TRUNCATE_AFTER_KEEP].rstrip()
                                  + "\n[...truncated]")
                msg["truncated"] = True
        out.append(msg)
    return out


def _turns_since(state: dict, total: int) -> int:
    """Messages added since the last rewrite, or a large number if unknown."""
    since = state.get("rewritten_at")
    if not isinstance(since, int):
        # No rewrite has happened yet. A summary-only bookmark may exist from an
        # older build, and those conversations are exactly the oversized ones,
        # so an unknown count must not block the first rewrite.
        return total
    return total - since


async def maybe_compact() -> bool:
    """Summarize the oldest third of the conversation once it outgrows the
    threshold. Fire-and-forget safe; single-flight. Returns True if compacted."""
    from backend.config import config
    from backend.memory.chat_history import chat_history
    global _locked

    if _locked:
        return False
    _locked = True
    try:
        threshold = int(config.get("memory", "compaction_threshold",
                                   default=12))
        state = _load()
        conv_id = chat_history.current_conversation_id
        if conv_id and state.get("conversation_id") != conv_id:
            # New conversation — start a fresh rolling summary
            state = {"conversation_id": conv_id, "entries": [], "summarized": 0}

        all_msgs = chat_history.get_context(max_messages=100000)
        total = len(all_msgs)
        if total < threshold:
            return False

        # The thrashing guard runs BEFORE the quiet wait, so a conversation
        # that has barely moved is rejected without spending fifteen seconds
        # waiting for a gap it was never going to use.
        if _turns_since(state, total) < MIN_TURNS_BETWEEN_COMPACTIONS:
            log.debug("compaction skipped: fewer than %d messages since the "
                      "last rewrite", MIN_TURNS_BETWEEN_COMPACTIONS)
            return False

        # What is left to fold is everything in the live list EXCEPT the
        # verbatim tail. It is no longer a positional bookmark into the
        # original list: a rewrite removes the summarized turns, so a bookmark
        # would drift past the shortened list and the conversation would stop
        # compacting while it was still growing.
        foldable = max(0, total - KEEP_TAIL)
        if foldable < threshold:
            return False  # the tail alone is all that remains — nothing stale

        # Everything above is cheap. Only now is it worth spending the model,
        # so wait for a gap in the conversation first.
        if not await _wait_for_quiet():
            log.debug("compaction skipped: conversation never went quiet")
            return False

        # The user may have chatted while we waited — re-read and re-check.
        all_msgs = chat_history.get_context(max_messages=100000)
        total = len(all_msgs)
        foldable = max(0, total - KEEP_TAIL)
        if foldable < threshold:
            return False

        # A third of the foldable region, capped: the older half of a long
        # conversation, not the part the user just said.
        chunk = all_msgs[: min(MAX_CHUNK, max(1, foldable // 3))]
        summary = await _summarize_chunk(chunk)
        if not summary:
            return False

        entries = list(state.get("entries", []))
        entries.append({"ts": time.time(), "summary": summary})
        state["entries"] = entries[-MAX_ENTRIES:]
        state["conversation_id"] = conv_id

        # The summarized chunk leaves the live list; the tail stays
        # word-for-word. `keep_from` advances by the chunk that was actually
        # summarized rather than jumping to `total - KEEP_TAIL` in one pass: the
        # head-jump reaches the steady state immediately and drops far more, but
        # on the 12-message default threshold it discarded most of a short
        # conversation before the summary had been shown to be any good.
        #
        # Bounded by the tail, which is never summarized, so a pass can never
        # remove messages a later pass would then have to describe without ever
        # having read them.
        keep_from = min(len(chunk), max(0, total - KEEP_TAIL))
        rewritten = _rewritten(all_msgs, keep_from)
        stored = chat_history.replace_context(rewritten, conv_id)
        if stored != len(rewritten):
            # The store refused the rewrite (unknown conversation, or a write
            # error). Recording progress now would mark turns as summarized
            # while they were still sitting in the list, and the next pass would
            # skip them. Leave the state alone.
            log.warning("compaction: rewrite not applied (%d of %d stored); "
                        "leaving the summary state unchanged",
                        stored, len(rewritten))
            return False

        # `summarized` is a running total of turns folded so far, not a
        # bookmark: the rewrite removes them, so a bookmark would point past the
        # end of the shortened list and the conversation would stop compacting
        # while still growing — measured, on a 240-message conversation, as
        # compacting once and then never again.
        state["summarized"] = int(state.get("summarized", 0)) + len(chunk)
        # `rewritten_at` is what the thrashing guard compares against: the size
        # of the list as this pass left it.
        state["rewritten_at"] = stored
        _save(state)
        log.info("Compacted %d messages into a summary; %d kept (%d before, "
                 "%d truncated)",
                 len(chunk), stored, total,
                 sum(1 for m in rewritten if m.get("truncated")))
        return True
    except Exception as e:
        log.debug("compaction failed: %s", e)
        return False
    finally:
        _locked = False


def compose_rolling(items: list[str]) -> str | None:
    """Format already-selected summaries, or None when there are none."""
    lines = [f"- {s}" for s in items if s]
    if not lines:
        return None
    return ("[Earlier in this conversation] Summaries of earlier parts of "
            f"this conversation:\n" + "\n".join(lines) + "\n"
            "Use them for continuity — mention only when relevant.")


def build_rolling_context(max_entries: int = 5) -> str | None:
    """Continuity block for long conversations, or None when empty.

    Only the conversation that produced these entries may see them. They are
    *mid-session* continuity, and `maybe_compact()` already treats a change of
    conversation as "start a fresh rolling summary" — but this reader never
    checked, so a compaction of a thread that had ended weeks earlier was
    announced to every new chat as "[Earlier in this conversation]".
    """
    state = _load()
    entries = state.get("entries", [])
    if not entries:
        return None
    from backend.memory.chat_history import chat_history
    current = chat_history.current_conversation_id
    if current is None or state.get("conversation_id") != current:
        return None
    return compose_rolling([e["summary"] for e in entries[-max_entries:]])
