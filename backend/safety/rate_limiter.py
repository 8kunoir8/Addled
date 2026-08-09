"""
Rate limiter — token bucket per action type.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("addled.rate_limiter")

DEFAULT_LIMITS = {
    "chat.send": (30, 60),      # 30/min
    "click": (60, 60),
    "type": (30, 60),
    "key_press": (60, 60),
    "read_file": (120, 60),
    "write_file": (30, 60),
    "delete_file": (10, 60),
    "launch": (20, 60),
    "run_command": (20, 60),
    "screenshot": (10, 60),
    "default": (30, 60),
}


class RateLimiter:
    """Token bucket per action. Returns ALLOWED, QUEUED, or BLOCKED."""

    def __init__(self):
        self._buckets: dict[str, tuple[float, int]] = {}  # action -> (last_refill, tokens)
        self._limits = dict(DEFAULT_LIMITS)

    def check(self, action_type: str) -> str:
        limit = self._limits.get(action_type, self._limits["default"])
        max_tokens, window = limit
        now = time.monotonic()
        last_refill, tokens = self._buckets.get(action_type, (now, max_tokens))

        # Refill
        elapsed = now - last_refill
        refill = int(elapsed / window * max_tokens)
        tokens = min(max_tokens, tokens + refill)
        last_refill = now if refill > 0 else last_refill

        if tokens > 0:
            self._buckets[action_type] = (last_refill, tokens - 1)
            return "ALLOWED"
        if tokens > -3:
            self._buckets[action_type] = (last_refill, tokens - 1)
            return "QUEUED"
        return "BLOCKED"
