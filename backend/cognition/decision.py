"""
Decision engine — determines agent response after each observation.

Returns: "stay_quiet", "nudge", "suggest", "offer_action".
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.decision")


class DecisionEngine:
    """Decides what to do after each observation."""

    def __init__(self):
        self._in_meeting = False
        self._in_gaming = False
        self._in_quiet_hours = False
        self._stuck_counter: dict[str, int] = {}
        self._last_context: str = "unknown"

    def track_context(self, context: str):
        """Increment stuck counter for repeated contexts."""
        if context == self._last_context:
            self._stuck_counter[context] = self._stuck_counter.get(context, 0) + 1
        else:
            self._stuck_counter[context] = 1
        self._last_context = context

    def set_meeting(self, active: bool):
        self._in_meeting = active

    def set_gaming(self, active: bool):
        self._in_gaming = active

    def set_quiet_hours(self, active: bool):
        self._in_quiet_hours = active

    def evaluate(self, context: str, detail: str | None = None,
                 novel: bool = False, is_error: bool = False,
                 budget_pct: float = 100.0) -> str:
        """Evaluate observation and return decision."""

        # Hard gates — no proactive behavior
        if self._in_meeting or self._in_gaming:
            return "stay_quiet"

        if self._in_quiet_hours:
            if is_error:
                return "nudge"
            return "stay_quiet"

        # Priority decisions
        if is_error:
            return "suggest"

        # Novel context with detail → offer proactive help
        if novel and detail and context not in ("idle", "other", "unknown"):
            return "nudge"

        # User appears stuck on same context for a while
        if self._stuck_counter.get(context, 0) >= 6 and context not in ("idle", "other"):
            return "suggest"

        # Budget-conscious
        if budget_pct < 10:
            return "stay_quiet"

        return "stay_quiet"
