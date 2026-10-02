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

from backend import app_paths

FACTS_PATH = app_paths.MEMORY_DIR / "facts.json"
# The next id to hand out. Persisted so an id is never reused.
#
# `next_id = max(ids) + 1` was computed from the CURRENT list, which is trimmed
# to `facts_max`. So once the cap was reached and the oldest fact dropped, its
# id was immediately handed to the new fact — and `delete_fact(id)` plus
# `autolink.forget_ref("fact", id)` would then act on a different fact than the
# one the user (or the dashboard) meant. Ids have to be monotonic, not "whatever
# is highest right now".
from backend import app_paths

_COUNTER_PATH = app_paths.MEMORY_DIR / "facts_counter.json"


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


def _next_id() -> int:
    """The next unused fact id. Monotonic — never reuses a retired id."""
    nid = 1
    try:
        if _COUNTER_PATH.exists():
            data = json.loads(_COUNTER_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("next_id"), int):
                nid = int(data["next_id"])
    except (json.JSONDecodeError, OSError):
        nid = 1
    if nid <= 1:
        # No usable counter (fresh install, or an upgrade from before it
        # existed): continue past whatever the stored facts already use, so
        # numbering does not restart underneath existing ids.
        try:
            nid = max((int(f.get("id", 0)) for f in _load()), default=0) + 1
        except Exception:  # noqa: BLE001
            nid = 1
    try:
        _COUNTER_PATH.write_text(json.dumps({"next_id": nid + 1}),
                                 encoding="utf-8")
    except OSError as e:
        log.warning("Could not save the facts id counter: %s", e)
    return nid


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
    next_id = _next_id()
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


def compose_facts(items: list[str]) -> str | None:
    """Format already-selected facts, or None when there are none."""
    items = [t for t in items if t]
    if not items:
        return None
    lines = "\n".join(f"- {t}" for t in items)
    return ("[Core memory] Durable facts saved about the user (preferences, "
            f"decisions, context):\n{lines}\n"
            "Treat these as true unless the user contradicts them.")


def build_facts_context(max_facts: int = 20) -> str | None:
    """System-prompt block with every durable fact.

    Callers that have a user message to hand should use
    `relevance.facts_block()` instead, which drops the unrelated ones.
    """
    return compose_facts([f["text"] for f in get_facts(max_facts)])


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
