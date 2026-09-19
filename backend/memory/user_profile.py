"""User model — a typed, lightweight profile of the person Addled lives with.

Learned heuristically from existing signals (no LLM required):
  - preferences  ← durable facts (first N)
  - rituals      ← recurring scheduler tasks (weekly/daily)
  - hours        ← inverse of safety quiet hours (when the user is around)

Injected into every chat as a stable system-prompt block so the companion
feels like it knows you.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.user_profile")

PROFILE_PATH = Path(__file__).resolve().parent / "user_profile.json"

_DEFAULTS = {
    "tone": "friendly",
    "hours": {"start": "09:00", "end": "21:00"},
    "rituals": [],
    "preferences": [],
    "updated": 0.0,
}


def _load() -> dict:
    if PROFILE_PATH.exists():
        try:
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULTS)


def _save(profile: dict) -> None:
    try:
        PROFILE_PATH.write_text(json.dumps(profile, indent=2,
                                           ensure_ascii=False),
                                encoding="utf-8")
    except OSError as e:
        log.warning("profile save failed: %s", e)


def get_profile() -> dict:
    return _load()


def update_profile(fields: dict) -> dict:
    """Partial update (dashboard). Returns the new profile."""
    profile = _load()
    for key, value in fields.items():
        if key in _DEFAULTS:
            profile[key] = value
    profile["updated"] = time.time()
    _save(profile)
    return profile


def auto_learn() -> dict:
    """Heuristic profile learning from facts + scheduler + quiet hours."""
    profile = _load()
    changed = False

    # preferences are no longer copied from facts. The facts block already
    # injects every fact verbatim, so the copy put each one in the prompt twice
    # — and, being part of an always-on block, it kept a stale fact alive no
    # matter how unrelated it was. Older builds wrote them here; prune them.
    try:
        facts = _fact_texts()
        current = list(profile.get("preferences") or [])
        pruned = [p for p in current if p and _norm(p) not in facts]
        if pruned != current:
            profile["preferences"] = pruned
            changed = True
    except Exception:
        pass

    # rituals ← recurring scheduler tasks
    try:
        from backend.tasks.store import task_store
        rituals = []
        for t in task_store.list_all(enabled=True):
            rtype = (t.recurrence or {}).get("type", "none")
            if rtype in ("daily", "weekly"):
                rituals.append(f"{t.time} — {t.title}")
        if rituals and rituals != profile.get("rituals"):
            profile["rituals"] = rituals
            changed = True
    except Exception:
        pass

    # hours ← inverse of quiet hours
    try:
        from backend.config import config
        quiet_start = config.get("safety", "quiet_hours_start",
                                 default="22:00")
        quiet_end = config.get("safety", "quiet_hours_end", default="07:00")
        profile["hours"] = {"start": quiet_end, "end": quiet_start}
        changed = True
    except Exception:
        pass

    if changed:
        profile["updated"] = time.time()
        _save(profile)
    return profile


def _norm(text: str) -> str:
    """Whitespace-and-case-insensitive form, for comparing a preference to a fact."""
    return " ".join((text or "").split()).lower()


def _fact_texts() -> set[str]:
    """Normalised text of every fact, so the profile can drop the copies."""
    try:
        from backend.memory.facts import get_facts
        return {_norm(f.get("text", "")) for f in get_facts(200)}
    except Exception:
        return set()


def build_profile_context() -> str | None:
    """System-prompt block, or None when there is nothing to say."""
    profile = _load()
    parts = []
    # A preference that is really a fact would be injected twice; the facts
    # block owns those, so skip any whose text matches one.
    facts = _fact_texts()
    prefs = [p for p in profile.get("preferences") or []
             if p and _norm(p) not in facts][:10]
    rituals = [r for r in profile.get("rituals") or [] if r][:8]
    if prefs:
        parts.append("Preferences: " + "; ".join(prefs))
    if rituals:
        parts.append("Regular rituals: " + "; ".join(rituals))
    hours = profile.get("hours") or {}
    if hours.get("start") and hours.get("end"):
        parts.append(f"Usually around between {hours['start']} and "
                     f"{hours['end']} (outside quiet hours).")
    tone = profile.get("tone")
    if tone and tone != "friendly":
        parts.append(f"User prefers a {tone} tone.")
    if not parts:
        return None
    return ("[User model] What the agent has learned about its user:\n"
            + "\n".join(f"- {p}" for p in parts)
            + "\nUse this to personalize replies, but update it when "
              "contradicted.")
