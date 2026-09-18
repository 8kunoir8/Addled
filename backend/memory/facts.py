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
    # Relate the new fact to any file its text names.
    try:
        from backend.memory.autolink import link_text
        link_text("fact", fact["id"], text, source="auto",
                  extra_note=text[:80])
    except Exception:
        pass
    return fact


def delete_fact(fact_id: int) -> bool:
    facts = _load()
    before = len(facts)
    facts = [f for f in facts if f.get("id") != fact_id]
    if len(facts) == before:
        return False
    _save(facts)
    # Drop its relations too, or the graph keeps a node that is gone.
    try:
        from backend.memory.autolink import forget_ref
        forget_ref("fact", fact_id)
    except Exception:
        pass
    return True


def clear_facts() -> int:
    facts = _load()
    _save([])
    try:
        from backend.memory.autolink import forget_kind
        forget_kind("fact")
    except Exception:
        pass
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


async def auto_extract_facts() -> int:
    """Ask the LLM for durable facts from recent turns. Opt-in only
    (memory.auto_facts). Returns the number of facts added."""
    from backend.config import config
    if not config.get("memory", "auto_facts", default=False):
        return 0

    from backend.memory.chat_history import chat_history
    messages = chat_history.get_context(max_messages=20)
    user_msgs = [str(m["content"])[:300] for m in messages
                 if m.get("role") == "user"]
    if not user_msgs:
        return 0

    from backend.providers.registry import get_provider
    try:
        provider = get_provider()
    except Exception:
        return 0

    transcript = "\n".join(f"- {m}" for m in user_msgs[-10:])
    prompt = ("From this conversation, extract up to 5 durable facts about "
              "the user worth remembering (preferences, projects, decisions). "
              "Return ONLY a JSON array of short strings, or [] if nothing "
              "worth saving.\n\n" + transcript)
    from backend.providers import router
    try:
        result = await provider.chat(
            [{"role": "user", "content": prompt}],
            model=router.for_provider(provider, "utility"),
            max_tokens=200, temperature=0.2)
        if not result.ok or not result.response or \
                result.response.startswith(("[Provider", "[Not connected")):
            return 0
        match = re.search(r"\[.*\]", result.response, re.S)
        if not match:
            return 0
        items = json.loads(match.group(0))
        added = 0
        for item in items:
            if isinstance(item, str):
                fact = add_fact(item, source="agent")
                if fact:
                    added += 1
        if added:
            log.info("Auto-extracted %d fact(s)", added)
        return added
    except Exception as e:
        log.debug("auto facts failed: %s", e)
        return 0
