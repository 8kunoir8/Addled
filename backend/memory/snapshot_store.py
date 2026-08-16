"""
Rolling screenshot memory — keeps the last N masked screenshots for a limited
time so the agent can answer "what was I doing 20 minutes ago?".

Privacy: stores the SAME masked image the observer already captured
(privacy zones blacked out), only in RAM, capped in count and age.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import logging

log = logging.getLogger("addled.snapshots")


@dataclass
class Snapshot:
    ts: float
    image_b64: str
    context: str


class SnapshotStore:
    def __init__(self):
        self._items: deque[Snapshot] = deque()
        self._last_add = 0.0

    def _cfg(self) -> dict:
        try:
            from backend.config import config
            return {
                "enabled": config.get("observation", "snapshot_store_enabled", default=True),
                "max": int(config.get("observation", "snapshot_max", default=30)),
                "min_interval": float(config.get("observation", "snapshot_min_interval_s", default=60)),
                "max_age_h": float(config.get("observation", "snapshot_max_age_h", default=24)),
            }
        except Exception:
            return {"enabled": True, "max": 30, "min_interval": 60, "max_age_h": 24}

    def add(self, image_b64: str, context: str, ts: float | None = None) -> None:
        """Store a (already privacy-masked) screenshot. Throttled + capped."""
        cfg = self._cfg()
        if not cfg["enabled"] or not image_b64:
            return
        now = ts if ts is not None else time.time()
        if now - self._last_add < cfg["min_interval"]:
            return
        self._last_add = now
        self._items.append(Snapshot(now, image_b64, context or "unknown"))
        # Age-based purge (default: drop anything older than 24h)
        cutoff = now - cfg["max_age_h"] * 3600
        while self._items and self._items[0].ts < cutoff:
            self._items.popleft()
        # Count cap
        while len(self._items) > cfg["max"]:
            self._items.popleft()

    def nearest(self, ago_seconds: float | None) -> Snapshot | None:
        """Snapshot closest to (now - ago_seconds). None when empty."""
        if not self._items:
            return None
        target = time.time() - (ago_seconds or 0)
        return min(self._items, key=lambda s: abs(s.ts - target))

    def list(self) -> list[dict]:
        now = time.time()
        return [{"ts": s.ts, "age_s": round(now - s.ts, 1), "context": s.context}
                for s in self._items]

    def count(self) -> int:
        return len(self._items)

    async def describe(self, ago_seconds: float | None = None) -> str | None:
        """Run local vision over a stored snapshot. Returns description."""
        snap = self.nearest(ago_seconds)
        if not snap:
            return None
        try:
            from backend.providers.hf_vision import hf_vision
            result = await hf_vision.analyze(snap.image_b64, "")
            return result.response if result.ok else None
        except Exception as e:
            log.warning("Snapshot description failed: %s", e)
            return None


snapshot_store = SnapshotStore()
