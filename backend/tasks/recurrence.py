"""Recurrence math — local-time scheduling rules (no cron library).

Rules supported: one-shot (date + time), daily, weekly (weekdays), monthly.
All computations use local time and produce epoch seconds for next_run.
"""

from __future__ import annotations

import calendar
import time
from datetime import datetime, timedelta

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    try:
        h, m = hhmm.strip().split(":")
        return int(h) % 24, int(m) % 60
    except (ValueError, AttributeError):
        return 9, 0


def time_to_epoch(date_str: str, hhmm: str = "09:00") -> float:
    """Combine YYYY-MM-DD + HH:MM (local) into epoch seconds."""
    h, m = _parse_hhmm(hhmm)
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        dt = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    return dt.timestamp()


def _next_daily(hhmm: str, after: float) -> float:
    now = datetime.fromtimestamp(after)
    h, m = _parse_hhmm(hhmm)
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _next_weekly(weekdays: list[int], hhmm: str, after: float) -> float:
    if not weekdays:
        weekdays = [datetime.fromtimestamp(after).weekday()]
    now = datetime.fromtimestamp(after)
    h, m = _parse_hhmm(hhmm)
    for offset in range(0, 8):
        day = now + timedelta(days=offset)
        if day.weekday() in weekdays:
            candidate = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate.timestamp() > after:
                return candidate.timestamp()
    # unreachable (week repeats within 7 days)
    return after + 7 * 24 * 3600


def _next_monthly(anchor_date: str, hhmm: str, after: float) -> float:
    h, m = _parse_hhmm(hhmm)
    day_of_month = 1
    try:
        day_of_month = int(datetime.strptime(anchor_date, "%Y-%m-%d").day)
    except (ValueError, TypeError):
        pass
    now = datetime.fromtimestamp(after)
    year, month = now.year, now.month
    for _ in range(14):  # covers short months safely
        last_day = calendar.monthrange(year, month)[1]
        dom = min(day_of_month, last_day)
        candidate = datetime(year, month, dom, h, m)
        if candidate.timestamp() > after:
            return candidate.timestamp()
        month += 1
        if month > 12:
            month = 1
            year += 1
    return after + 30 * 24 * 3600


def next_run(task, after: float | None = None) -> float:
    """Next execution epoch for a task, strictly after `after` (default now)."""
    after = time.time() if after is None else after
    rtype = (task.recurrence or {}).get("type", "none")
    hhmm = task.time or "09:00"
    if rtype == "daily":
        return _next_daily(hhmm, after)
    if rtype == "weekly":
        weekdays = list((task.recurrence or {}).get("weekdays") or [])
        return _next_weekly(weekdays, hhmm, after)
    if rtype == "monthly":
        return _next_monthly(task.date or "", hhmm, after)
    # one-shot: the anchor is date+time
    anchor = task.date or datetime.fromtimestamp(after).strftime("%Y-%m-%d")
    ts = time_to_epoch(anchor, hhmm)
    return ts if ts > after else ts  # caller decides missed-task policy


def expand_month(task, year: int, month: int) -> list[str]:
    """Dates (YYYY-MM-DD) on which this task runs within a calendar month."""
    dates: list[str] = []
    start = datetime(year, month, 1).timestamp()
    end = datetime(year + 1, 1, 1).timestamp() if month == 12 \
        else datetime(year, month + 1, 1).timestamp()
    cursor = start
    seen: set[str] = set()
    for _ in range(62):  # safety bound
        ts = next_run(task, after=cursor - 1)
        if ts >= end or ts < start:
            break
        day_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if day_str in seen:
            break
        seen.add(day_str)
        dates.append(day_str)
        cursor = ts + 1
    return dates


def humanize(task) -> str:
    """Short human label, e.g. '09:00 · every weekday'."""
    rtype = (task.recurrence or {}).get("type", "none")
    if rtype == "daily":
        rule = "every day"
    elif rtype == "weekly":
        weekdays = (task.recurrence or {}).get("weekdays") or []
        if weekdays:
            names = [WEEKDAY_NAMES[d][:3] for d in weekdays]
            rule = "every " + ", ".join(names)
        else:
            rule = "weekly"
    elif rtype == "monthly":
        rule = "monthly"
    else:
        rule = "one-shot"
    return f"{task.time} · {rule}"
