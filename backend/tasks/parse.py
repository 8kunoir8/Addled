"""Heuristic natural-language task parser (LLM-free fallback).

Understands:
  "remind me to <payload> at 3pm"            → one-shot today (or tomorrow
                                               if the time already passed)
  "remind me to <payload> tomorrow at 9am"
  "every friday at 9am <payload>"            → weekly recurrence
  "every day at 9am <payload>"               → daily recurrence
  "schedule <payload> at 14:30"

Used by the voice path when the LLM provider is unavailable, and offered
to chat as a fallback. Returns a task dict compatible with tasks.schedule.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

WEEKDAY_MAP = {
    "mon": 0, "monday": 0, "tue": 1, "tuesday": 1, "wed": 2,
    "wednesday": 2, "thu": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

_PREFIXES = ("remind me to ", "remind me ", "reminder ",
             "schedule a task to ", "schedule ", "set a reminder to ",
             "set reminder to ", "create a task to ", "create task ")


def _time_date(time_phrase: str) -> tuple[str, str] | None:
    """Parse a time phrase into ('YYYY-MM-DD', 'HH:MM')."""
    from backend.integrations.calendar_integration import _normalize_dt
    norm = _normalize_dt(time_phrase.strip())
    if " " in norm:
        return tuple(norm.split(" ", 1))  # type: ignore[return-value]
    return None


def _rule_to_weekdays(rule: str) -> list[int]:
    if rule in ("weekday", "weekdays"):
        return [0, 1, 2, 3, 4]
    return [WEEKDAY_MAP.get(rule, 4)]


def _finalize(parsed: tuple[str, str], payload: str,
              recurrence: dict) -> dict | None:
    date, hhmm = parsed
    now = datetime.now()
    if date == now.strftime("%Y-%m-%d") and hhmm <= now.strftime("%H:%M"):
        date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    payload = payload.strip(" .!?")
    if not payload:
        return None
    return {
        "title": payload[:60],
        "payload": payload,
        "time": hhmm,
        "date": date,
        "recurrence": recurrence,
    }


def parse_natural_task(text: str) -> dict | None:
    """Best-effort task extraction; returns None when nothing matched."""
    low = (text or "").strip()
    if not low:
        return None
    # strip wake-word / name prefixes: "hey addled remind me to ..."
    low = re.sub(r"(?i)^\s*(hey\s+|ok\s+)?(addled|fox)[,.!\s]+", "", low)
    for prefix in _PREFIXES:
        if low.lower().startswith(prefix):
            low = low[len(prefix):]
            break

    # recurring: "every <rule> at <time> <payload>"
    m = re.match(
        r"(?i)every\s+(day|weekday|weekdays|mon|tue|wed|thu|fri|sat|sun|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+"
        r"at\s+(.+?)\s+(\S.*)$", low)
    if m:
        rule = m.group(1).lower()
        parsed = _time_date(m.group(2))
        if parsed is None:
            return None
        recurrence = {"type": "daily", "weekdays": []} if rule == "day" \
            else {"type": "weekly", "weekdays": _rule_to_weekdays(rule)}
        payload = re.sub(r"(?i)^(remind\s+(?:me\s+)?to\s+|remind\s+)",
                         "", m.group(3))
        return _finalize(parsed, payload, recurrence)

    # one-shot: "<payload> at <time phrase>"
    idx = low.rfind(" at ")
    if idx < 0:
        # trailing time without "at": "remind me to X 3pm"
        m2 = re.search(
            r"(?i)\s(\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight)$", low)
        if m2:
            parsed = _time_date(m2.group(1))
            if parsed is not None:
                return _finalize(parsed, low[:m2.start()],
                                 {"type": "none", "weekdays": []})
        return None
    payload = low[:idx].strip()
    time_phrase = low[idx + 4:]
    # trailing day words belong to the time phrase
    tm = re.search(r"(?i)\s+(tomorrow|today|day after tomorrow)$", payload)
    if tm:
        time_phrase = tm.group(1) + " " + time_phrase
        payload = payload[:tm.start()]
    parsed = _time_date(time_phrase)
    if parsed is None:
        return None
    return _finalize(parsed, payload, {"type": "none", "weekdays": []})
