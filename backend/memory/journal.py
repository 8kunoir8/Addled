"""Episodic timeline — a daily journal of what happened.

Every chat turn (user + assistant) is appended to the day's journal file.
A nightly job summarizes each finished day into 2-3 sentences (LLM when a
provider is up, heuristic otherwise) so "what did we do yesterday?" has a
real answer — and so the agent can reference its own past (persistent
identity).

Files: backend/memory/journal/journal_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("addled.journal")

# Serialises the read-modify-write in `record`.
#
# `record` loads the whole day, appends one entry, and writes the day back. Two
# callers doing that at once both read the same file, so the second write wins
# and the first turn is silently lost. That is reachable, not theoretical: the
# journal is written from the shared chat pipeline, which the SWARM runs for
# several agents at the same time (`asyncio.gather` in `_run_parallel_step`),
# and a scheduled task can fire while a chat turn is being journaled. The
# docstring used to claim the caller was a single loop — it stopped being true
# when swarm workers began recording.
#
# A plain `threading.Lock`, not an asyncio one: the write path is also reached
# from worker threads, and the lock is only ever held for a local file write.
_JOURNAL_LOCK = threading.Lock()

from backend import app_paths

JOURNAL_DIR = app_paths.subdir("journal")
MAX_ENTRIES_PER_DAY = 200
KEEP_DAYS = 60


def _day_path(date_str: str) -> Path:
    return JOURNAL_DIR / f"journal_{date_str}.json"


def _load_day(date_str: str) -> dict:
    path = _day_path(date_str)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"date": date_str, "entries": [], "summary": "", "summarized": False}


def _save_day(day: dict) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        _day_path(day["date"]).write_text(
            json.dumps(day, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("journal save failed: %s", e)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def record(role: str, text: str) -> None:
    """Append a chat turn to today's journal.

    The read-modify-write is done under a lock. Several callers reach this at
    the same time — swarm agents finish together and a scheduled task can land
    mid-turn — and without the lock the slower writer overwrote the faster one,
    losing an entry with no error anywhere.
    """
    if not text or not text.strip():
        return
    with _JOURNAL_LOCK:
        day = _load_day(_today())
        day["entries"].append({
            "ts": time.time(),
            "role": role,
            "text": text.strip()[:500],
        })
        if len(day["entries"]) > MAX_ENTRIES_PER_DAY:
            day["entries"] = day["entries"][-MAX_ENTRIES_PER_DAY:]
        _save_day(day)


def get_day(date_str: str | None = None) -> dict:
    return _load_day(date_str or _today())


def list_days(limit: int = 14) -> list[dict]:
    """Recent journal days, newest first (summary + entry count only)."""
    if not JOURNAL_DIR.exists():
        return []
    days = []
    for path in sorted(JOURNAL_DIR.glob("journal_*.json"), reverse=True):
        try:
            day = json.loads(path.read_text(encoding="utf-8"))
            days.append({
                "date": day.get("date", ""),
                "entries": len(day.get("entries", [])),
                "summary": day.get("summary", ""),
                "summarized": day.get("summarized", False),
            })
        except (json.JSONDecodeError, OSError):
            continue
        if len(days) >= limit:
            break
    return days


async def nightly_summarize() -> dict:
    """Summarize yesterday (and any older unsummarized day). Provider
    when available, heuristic fallback otherwise."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return await _summarize_day(yesterday)


async def _summarize_day(date_str: str) -> dict:
    day = _load_day(date_str)
    if day.get("summarized"):
        return {"date": date_str, "skipped": "already summarized"}
    entries = day.get("entries", [])
    if not entries:
        return {"date": date_str, "skipped": "no entries"}

    summary = ""
    provider_text = ""
    try:
        from backend.providers.registry import get_provider
        provider = get_provider()
        transcript = "\n".join(
            f"{e['role']}: {e['text'][:200]}" for e in entries[-40:])
        from backend.providers import router
        resp = await provider.chat([
            {"role": "system", "content":
             "Summarize this day of a user+desktop-AI working together in "
             "2-3 plain sentences (no lists, no meta-commentary)."},
            {"role": "user", "content": transcript},
        ], model=router.for_provider(provider, "utility"))
        # provider.chat returns a ProviderResult, not a dict.
        if resp.ok:
            provider_text = (resp.response or "").strip()
        else:
            log.debug("journal LLM summary failed: %s", resp.error)
    except Exception as e:
        log.debug("journal LLM summary unavailable: %s", e)

    if provider_text:
        summary = provider_text[:500]
    else:
        # heuristic: first and last entries, truncated
        first = entries[0]["text"][:160]
        last = entries[-1]["text"][:160]
        summary = (f"The day started with: \"{first}\" ... and ended with: "
                   f"\"{last}\" ({len(entries)} turns).")

    day["summary"] = summary
    day["summarized"] = True
    _save_day(day)
    _prune()
    return {"date": date_str, "summary": summary[:120]}


def compose_timeline(items: list[tuple[str, str]]) -> str | None:
    """Format already-selected (date, summary) pairs, or None when empty."""
    lines = [f"- {date}: {summary}" for date, summary in items if summary]
    if not lines:
        return None
    return ("[Recent days] What happened recently, in the agent's own "
            "timeline:\n" + "\n".join(lines) +
            "\nReference these naturally when relevant (e.g. 'yesterday "
            "we...').")


def build_timeline_context(days: int = 3) -> str | None:
    """System-prompt block with recent day summaries (newest first)."""
    if not JOURNAL_DIR.exists():
        return None
    return compose_timeline([(meta["date"], meta.get("summary") or "")
                             for meta in list_days(limit=days)])


def _prune() -> None:
    """Delete journal files older than KEEP_DAYS."""
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    for path in JOURNAL_DIR.glob("journal_*.json"):
        try:
            if path.stem.removeprefix("journal_") < cutoff:
                path.unlink()
        except OSError:
            pass
