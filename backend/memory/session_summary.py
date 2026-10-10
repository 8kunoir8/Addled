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

from backend import app_paths

SUMMARIES_PATH = app_paths.MEMORY_DIR / "session_summaries.json"
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
    # A stable, monotonic id — NOT the list position.
    #
    # `_save` keeps only the newest MAX_SUMMARIES, so a positional index stopped
    # identifying the row it was computed for the moment the list started
    # rotating: `delete_summary(index)` then popped a different summary than the
    # one the dashboard showed, and every `memory_related("summary", n)` link
    # pointed at the wrong note. `facts.py` had the same defect and now uses the
    # same fix.
    nid = max((int(e.get("id", 0)) for e in entries), default=0) + 1
    entries.append({"id": nid, "ts": time.time(), "summary": summary})
    _save(entries)
    # Provenance: this summary came out of today's session, and it may name files.
    try:
        import datetime as _dt
        from backend.memory.autolink import link_provenance, link_text
        link_provenance("summary", nid, "journal",
                        _dt.date.today().isoformat(), note=summary[:80])
        link_text("summary", nid, summary, source="auto",
                  extra_note=summary[:80])
    except Exception as e:
        log.debug("summary auto-link failed: %s", e)
    try:
        # `backend.memory.embedding`, not `backend.memory.recall`: recall.py's
        # `embed_text` is the LEGACY HASH embedder. Writing hash vectors here
        # while recall searches with MiniLM produced rows that are 384-dim and
        # therefore accepted, but whose cosine against a MiniLM query is
        # meaningless -- `reembed.py` describes this exact failure ("cosine
        # similarity between vectors from two different models is meaningless,
        # so a half-migrated store returns confident nonsense rather than an
        # error"). Session summaries were never semantically searchable.
        from backend.memory.embedding import embed_text
        from backend.memory.vector_store import vector_store
        # Keep the vector row's id on the entry, so deleting the summary can
        # remove exactly that row. `delete_category` was used before, which
        # dropped the vector rows of every OTHER summary too — so deleting one
        # silently removed the rest from semantic recall while their text
        # stayed in the JSON.
        row_id = vector_store.add(embed_text(summary),
                                  category="session_summary",
                                  metadata={"text": summary, "summary_id": nid})
        if isinstance(row_id, int):
            entries = _load()
            for entry in entries:
                if int(entry.get("id", -1)) == nid:
                    entry["vector_id"] = row_id
                    break
            _save(entries)
    except Exception as e:
        log.debug("Vector summary store failed: %s", e)


def get_recent_summaries(limit: int = 3) -> list[dict]:
    return _load()[-limit:]


def delete_summary(ref: int) -> bool:
    """Delete one summary.

    `ref` is the summary's **id** (what `get_recent_summaries` returns). A bare
    list position is still accepted for older callers, but ids are what survive
    the 50-entry rotation — a position did not, which is how this deleted the
    wrong summary once the list started turning over.
    """
    entries = _load()
    doomed = [e for e in entries if int(e.get("id", -1)) == int(ref)]
    if doomed:
        entries = [e for e in entries if int(e.get("id", -1)) != int(ref)]
        _save(entries)
        # Remove only this summary's vector row. `delete_category` was used
        # before, which also erased the semantic-recall row of every summary
        # that is still here.
        vid = doomed[0].get("vector_id")
        if isinstance(vid, int):
            try:
                from backend.memory.vector_store import vector_store
                vector_store.delete(vid)
            except Exception as e:  # noqa: BLE001
                log.debug("could not delete summary vector row: %s", e)
        return True
    # No id matched: treat it as a position, the old contract.
    if not (0 <= int(ref) < len(entries)):
        return False
    entries.pop(int(ref))
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


def compose_session_block(items: list[str]) -> str | None:
    """Format already-selected summaries, or None when there are none."""
    lines = [f"- {s}" for s in items if s]
    if not lines:
        return None
    return ("[Session memory] What you and the user have been working on in "
            f"recent sessions:\n" + "\n".join(lines) + "\n"
            "Use this for continuity — mention it only when relevant.")


def build_session_context(limit: int = 3) -> str | None:
    """Block injected into chat for long-run continuity."""
    return compose_session_block([s["summary"]
                                  for s in get_recent_summaries(limit)])
