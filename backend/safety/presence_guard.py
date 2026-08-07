"""
Presence guard — detects when the agent should stay quiet.

Detects meetings, gaming, focus mode, quiet hours, and user away.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("addled.presence_guard")


class PresenceGuard:
    """Monitors desktop context to determine if agent should sleep."""

    def __init__(self):
        self._in_meeting = False
        self._in_gaming = False
        self._in_quiet_hours = False
        self._last_input_time = time.time()
        self._away_timeout = 300  # 5 minutes default

    def check(self) -> tuple[bool, str]:
        """
        Check if agent should be blocked.
        Returns (blocked, reason).
        """
        from backend.config import config

        # Check quiet hours
        quiet_start = config.get("safety", "quiet_hours_start", default="22:00")
        quiet_end = config.get("safety", "quiet_hours_end", default="07:00")
        self._in_quiet_hours = self._is_quiet_hours(quiet_start, quiet_end)

        # Check meeting detection (window title scan)
        if config.get("safety", "meeting_auto_sleep", default=True):
            self._in_meeting = self._detect_meeting()

        # Check gaming detection
        if config.get("safety", "gaming_auto_sleep", default=True):
            self._in_gaming = self._detect_gaming()

        # Check user away
        away_timeout = config.get("safety", "away_timeout_minutes", default=5) * 60
        if self._has_user_input():
            self._last_input_time = time.time()

        user_away = (time.time() - self._last_input_time) > away_timeout

        if self._in_meeting:
            return True, "meeting"
        if self._in_gaming:
            return True, "gaming"
        if self._in_quiet_hours:
            return True, "quiet_hours"
        if user_away:
            return True, "away"

        return False, ""

    def _is_quiet_hours(self, start_str: str, end_str: str) -> bool:
        """Check if current time is within quiet hours."""
        try:
            now = time.localtime()
            current_minutes = now.tm_hour * 60 + now.tm_min

            sh, sm = map(int, start_str.split(":"))
            eh, em = map(int, end_str.split(":"))
            start_m = sh * 60 + sm
            end_m = eh * 60 + em

            if start_m <= end_m:
                return start_m <= current_minutes <= end_m
            else:  # Spans midnight
                return current_minutes >= start_m or current_minutes <= end_m
        except (ValueError, AttributeError):
            return False

    def _detect_meeting(self) -> bool:
        """Check for meeting apps in foreground window title."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return False
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()

            meeting_keywords = [
                "zoom meeting", "zoom - ", "microsoft teams", "teams meeting",
                "google meet", "webex", "discord", "skype", "slack huddle",
                "gotomeeting", "bluejeans", "whereby",
            ]
            return any(kw in title for kw in meeting_keywords)
        except Exception:
            return False

    def _detect_gaming(self) -> bool:
        """Check for fullscreen gaming."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()

            # Check if fullscreen
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)

            if width >= screen_w and height >= screen_h:
                return True
            return False
        except Exception:
            return False

    def _has_user_input(self) -> bool:
        """Check for recent mouse/keyboard activity."""
        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            idle_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return idle_ms < 5000  # Activity within last 5 seconds
        except Exception:
            return True  # Assume user is present if we can't check
