"""Skill telemetry — usage/failure counters for the reflection loop."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.skills.telemetry")

TELEMETRY_PATH = Path(__file__).resolve().parent.parent / "memory" \
    / "integrations" / "skill_telemetry.json"


def _load() -> dict:
    try:
        if TELEMETRY_PATH.exists():
            return json.loads(TELEMETRY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {"skills": {}}


def _save(data: dict) -> None:
    try:
        TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        TELEMETRY_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def record(skill_name: str, ok: bool) -> None:
    data = _load()
    entry = data["skills"].setdefault(skill_name, {
        "uses": 0, "failures": 0, "last_use": 0.0})
    entry["uses"] += 1
    if not ok:
        entry["failures"] += 1
    entry["last_use"] = time.time()
    _save(data)


def stats() -> dict:
    """Top skills by use + skills with failures."""
    data = _load()
    skills = data.get("skills", {})
    top = sorted(skills.items(), key=lambda kv: -kv[1].get("uses", 0))[:8]
    failing = [(n, s["failures"]) for n, s in skills.items()
               if s.get("failures", 0) > 0]
    failing.sort(key=lambda kv: -kv[1])
    return {
        "top": [{"name": n, **s} for n, s in top],
        "failing": failing[:8],
        "total_uses": sum(s.get("uses", 0) for s in skills.values()),
    }
