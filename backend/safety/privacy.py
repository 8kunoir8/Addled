"""
Privacy guard — screen region blackout and app exclusion.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.privacy")


class PrivacyGuard:
    """Manages privacy zones and app exclusions."""

    def __init__(self):
        self._zones: list[dict] = []  # [{x, y, w, h}]
        self._excluded_apps: list[str] = []

    def add_zone(self, x: int, y: int, w: int, h: int):
        self._zones.append({"x": x, "y": y, "w": w, "h": h})

    def remove_zone(self, index: int):
        if 0 <= index < len(self._zones):
            self._zones.pop(index)

    def exclude_app(self, app_name: str):
        if app_name not in self._excluded_apps:
            self._excluded_apps.append(app_name)

    def is_excluded(self, window_title: str) -> bool:
        title_lower = window_title.lower()
        return any(app.lower() in title_lower for app in self._excluded_apps)

    def mask_zones(self, width: int, height: int) -> list[dict]:
        """Return list of zone rects to black out."""
        return [z for z in self._zones if z["x"] < width and z["y"] < height]

    def clear(self):
        self._zones.clear()
        self._excluded_apps.clear()
