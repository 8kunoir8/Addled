"""
Session summaries — continuity across sessions.

On shutdown the agent writes a short summary of the session (what you were
working on, decisions, preferences, open tasks) into long-term memory.
Every future chat automatically receives the recent summaries so the agent
remembers the long-running context.

Persistence: backend/memory/session_summaries.json (last 50) + vector store
(category 'session_summary') for semantic recall.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.session_memory")

SUMMARIES_PATH = Path(__file__).parent / "session_summaries.json"
MAX_SUMMARIES = 50


def _load() -> list[dict]:
    if SUMMARIES_PATH.exists():
        try:
            return json.loads(SUMMARIES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save(entries: list[dict]) -> None:
    try:
        SUMMARIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUMMARIES_PATH.write_text(
            json.dumps(entries[-MAX_SUMMARIES:], indent=2, ensure_ascii=False),
            encoding="utf-8")
    except OSError as e:
        log.warning("Could not save session summaries: %s", e)


async def summarize_session(provider=None) -> str:
    """Summarize the current conversation. LLM when available, heuristic
    fallback otherwise. Returns "" when there is nothing to summarize."""
    from backend.memory.chat_history import chat_history
    messages = chat_history.get_context(max_messages=60)
    if not messages:
        return ""

    if provider is None:
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception:
            provider = None

    if provider is not None:
        from backend.providers import router
        try:
            transcript = "\n".join(
                f"{m['role']}: {str(m['content'])[:400]}" for m in messages)
            prompt = (
                "Summarize this working session in 3-5 short bullets: what the "
                "user was working on, decisions made, preferences, and open "
                "tasks. Be concise and factual.\n\n" + transcript)
            result = await provider.chat(
                [{"role": "user", "content": prompt}],
                model=router.for_provider(provider, "utility"),
                max_tokens=300, temperature=0.3)
            if result.ok and result.response and not result.response.startswith("["):
                summary = result.response.strip()
                if summary:
                    save_session_summary(summary)
                    log.info("Session summary saved (%d chars)", len(summary))
                    return summary
        except Exception as e:
            log.debug("LLM summary failed, using heuristic: %s", e)

    # Heuristic fallback: recent user topics
    user_topics = [str(m["content"])[:120] for m in messages
                   if m.get("role") == "user"][-5:]
    if not user_topics:
        return ""
    summary = "Recent topics: " + " | ".join(user_topics)
    save_session_summary(summary)
    return summary


def save_session_summary(summary: str) -> None:
    """Persist a summary (file + vector store for semantic recall)."""
    summary = (summary or "").strip()
    if not summary:
        return
    entries = _load()
    entries.append({"ts": time.time(), "summary": summary})
    _save(entries)
    try:
        from backend.memory.recall import embed_text
        from backend.memory.vector_store import vector_store
        vector_store.add(embed_text(summary), category="session_summary",
                         metadata={"text": summary})
    except Exception as e:
        log.debug("Vector summary store failed: %s", e)


def get_recent_summaries(limit: int = 3) -> list[dict]:
    return _load()[-limit:]


def delete_summary(index: int) -> bool:
    """Delete one summary by index (0 = oldest in the list)."""
    entries = _load()
    if not (0 <= index < len(entries)):
        return False
    entries.pop(index)
    _save(entries)
    return True


def clear_summaries() -> int:
    """Remove all summaries (file + vector rows). Returns count removed."""
    entries = _load()
    _save([])
    try:
        from backend.memory.vector_store import vector_store
        vector_store.delete_category("session_summary")
    except Exception:
        pass
    return len(entries)


def build_session_context(limit: int = 3) -> str | None:
    """Block injected into chat for long-run continuity."""
    summaries = get_recent_summaries(limit)
    if not summaries:
        return None
    lines = "\n".join(f"- {s['summary']}" for s in summaries)
    return ("[Session memory] What you and the user have been working on in "
            f"recent sessions:\n{lines}\n"
            "Use this for continuity — mention it only when relevant.")
