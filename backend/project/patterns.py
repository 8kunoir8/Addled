"""Predictive proactivity — heuristic pattern mining (no LLM required).

Mines recurring scheduler tasks + calendar events for weekly rhythms and
surfaces gentle suggestions ("you usually commit on Friday afternoons —
want me to prepare a summary?").
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("addled.project.patterns")

STATE_PATH = Path(__file__).resolve().parent.parent / "memory" / "integrations" \
    / "patterns_state.json"

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def detect_patterns() -> list[dict]:
    """Recurring weekly tasks → patterns [{weekday, time, title}]."""
    patterns = []
    try:
        from backend.tasks.store import task_store
        for t in task_store.list_all(enabled=True):
            rtype = (t.recurrence or {}).get("type", "none")
            weekdays = (t.recurrence or {}).get("weekdays") or []
            if rtype == "weekly" and weekdays:
                for wd in weekdays:
                    patterns.append({"weekday": wd, "time": t.time,
                                     "title": t.title})
            elif rtype == "daily":
                patterns.append({"weekday": None, "time": t.time,
                                 "title": t.title})
    except Exception:
        pass

    # calendar events on the same weekday count as a rhythm too
    try:
        from backend.integrations.calendar_integration import calendar
        from collections import Counter
        weekday_counts = Counter()
        weekday_titles: dict[int, list[str]] = {}
        for ev in calendar.get_events():
            try:
                start = datetime.strptime(str(ev.get("start", ""))[:16],
                                          "%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
            weekday_counts[start.weekday()] += 1
            weekday_titles.setdefault(start.weekday(), []).append(
                str(ev.get("title", ""))[:40])
        for wd, count in weekday_counts.items():
            if count >= 2:
                patterns.append({
                    "weekday": wd,
                    "time": None,
                    "title": " · ".join(weekday_titles[wd][:3])
                             or "something",
                    "calendar": True,
                })
    except Exception:
        pass
    return patterns


def _suggestion_text(p: dict) -> str | None:
    if p.get("calendar"):
        return (f"You often have events on {WEEKDAY_NAMES[p['weekday']]}s — "
                f"like {p['title']}. Want me to check tomorrow's schedule?")
    if p.get("weekday") is not None:
        return (f"You usually run '{p['title']}' every "
                f"{WEEKDAY_NAMES[p['weekday']]} at {p['time']}. I'll keep it "
                "on the calendar.")
    return None


def maybe_suggest_patterns(emit) -> int:
    """Emit at most one new pattern suggestion per day. Returns count."""
    patterns = detect_patterns()
    if not patterns:
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8")) \
            if STATE_PATH.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    if state.get("last_suggest") == today:
        return 0
    for p in patterns:
        text = _suggestion_text(p)
        if not text:
            continue
        try:
            emit(text)
        except Exception:
            pass
        state["last_suggest"] = today
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            pass
        return 1
    return 0
