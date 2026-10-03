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
        """Check for a game covering the screen, windowed-borderless included.

        Two shapes have to be recognised, and measurement on this machine is
        what set the thresholds:

        * **Exclusive fullscreen** — the window is the SCREEN (2560x1440 here).
          The original test caught this.
        * **Borderless-windowed** — the app draws itself over the desktop with
          no frame. Commonly the same size as the screen (also caught), but
          some titles size to the WORK AREA instead (2560x1392 here), because
          they do not cover the taskbar. Those were MISSED: the old test asked
          whether the window covered the whole screen, and 1392 < 1440.

        The work-area case cannot be settled by size alone, because a MAXIMISED
        ordinary window is also exactly the work area. What separates them is
        the window STYLE: a borderless game is a popup with no caption and no
        resize frame, while a maximised app keeps its caption and thick frame.
        Measured on this machine: VS Code maximised reports caption=True,
        thickframe=True; a borderless game reports popup=True and neither.

        So the rule is: big enough to cover the work area, AND frameless. The
        frameless half is what stops every maximised window from being called a
        game, and the work-area half is what stops a borderless game from being
        missed.
        """
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top

            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)

            # The usable area, which excludes the taskbar. A borderless window
            # that does not cover the taskbar is this size, and it is still a
            # game taking the screen.
            work = RECT()
            SPI_GETWORKAREA = 0x0030
            got_work = bool(user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(work), 0))
            work_w = (work.right - work.left) if got_work else screen_w
            work_h = (work.bottom - work.top) if got_work else screen_h

            # A pixel or two of slack: a borderless window can be positioned at
            # -1 or sized 1px short by its own window procedure, and being one
            # pixel under must not mean "not a game".
            SLACK = 2
            covers = (width >= work_w - SLACK and height >= work_h - SLACK)
            if not covers:
                return False

            # Frameless is the half that excludes ordinary maximised windows.
            #
            # Read the styles in one place rather than stacking conditions: an
            # earlier draft of this function tried to be clever with several
            # overlapping checks and ended up with two branches that could never
            # be reached. One decision, expressed once, is the only version that
            # can be reasoned about.
            GWL_STYLE = -16
            WS_POPUP = 0x80000000
            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
            has_caption = bool(style & WS_CAPTION)
            has_frame = bool(style & WS_THICKFRAME)
            is_popup = bool(style & WS_POPUP)

            # A normal window keeps its caption and its resize frame even when
            # maximised — that is what a maximised editor is. A borderless game
            # has neither: the app draws its own frame or none at all.
            looks_like_a_window = has_caption or has_frame
            if looks_like_a_window and not is_popup:
                return False

            # A popup with a caption is a dialog or a splash, not a game.
            if has_caption and not has_frame:
                return False

            return True
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
