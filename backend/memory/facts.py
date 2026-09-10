"""
Self-editable core memory — durable facts about the user (Letta-style).

The agent (via the memory_set/memory_get tools) or the user (via the
dashboard Memory page) can save one-line facts that persist across
sessions. Current facts are injected into the system prompt of every chat.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("addled.facts")

FACTS_PATH = Path(__file__).parent / "facts.json"


def _load() -> list[dict]:
    if FACTS_PATH.exists():
        try:
            data = json.loads(FACTS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save(facts: list[dict]) -> None:
    try:
        FACTS_PATH.write_text(
            json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.warning("Could not save facts: %s", e)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def get_facts(limit: int = 50) -> list[dict]:
    """Most recent facts."""
    return _load()[-limit:]


def add_fact(text: str, source: str = "manual") -> dict | None:
    """Add a fact; skips empty text and duplicates. Returns the fact or None."""
    from backend.config import config
    text = (text or "").strip()
    if len(text) < 4:
        return None
    facts = _load()
    norm = _norm(text)
    if any(_norm(f.get("text", "")) == norm for f in facts):
        return None
    next_id = max((int(f.get("id", 0)) for f in facts), default=0) + 1
    fact = {"id": next_id, "text": text, "ts": time.time(), "source": source}
    facts.append(fact)
    max_facts = int(config.get("memory", "facts_max", default=200))
    _save(facts[-max_facts:])
    return fact


def delete_fact(fact_id: int) -> bool:
    facts = _load()
    before = len(facts)
    facts = [f for f in facts if f.get("id") != fact_id]
    if len(facts) == before:
        return False
    _save(facts)
    return True


def clear_facts() -> int:
    facts = _load()
    _save([])
    return len(facts)


def build_facts_context(max_facts: int = 20) -> str | None:
    """System-prompt block with the durable facts, or None when empty."""
    facts = get_facts(max_facts)
    if not facts:
        return None
    lines = "\n".join(f"- {f['text']}" for f in facts)
    return ("[Core memory] Durable facts saved about the user (preferences, "
            f"decisions, context):\n{lines}\n"
            "Treat these as true unless the user contradicts them.")
