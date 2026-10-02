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

        summarized = int(state.get("summarized", 0))
        pending = all_msgs[summarized:]
        if len(pending) < threshold:
            return False  # only the fresh tail remains — nothing stale to fold

        # Everything above is cheap. Only now is it worth spending the model,
        # so wait for a gap in the conversation first.
        if not await _wait_for_quiet():
            log.debug("compaction skipped: conversation never went quiet")
            return False

        # The user may have chatted while we waited — re-read and re-check.
        all_msgs = chat_history.get_context(max_messages=100000)
        summarized = int(state.get("summarized", 0))
        pending = all_msgs[summarized:]
        if len(pending) < threshold:
            return False

        chunk = pending[: min(MAX_CHUNK, max(1, len(pending) // 3))]
        summary = await _summarize_chunk(chunk)
        if not summary:
            return False

        entries = list(state.get("entries", []))
        entries.append({"ts": time.time(), "summary": summary})
        state["entries"] = entries[-MAX_ENTRIES:]
        state["summarized"] = summarized + len(chunk)
        state["conversation_id"] = conv_id
        _save(state)
        log.info("Compacted %d messages (%d summarized total)",
                 len(chunk), state["summarized"])
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
