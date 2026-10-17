"""
Presence guard — detects when the agent should stay quiet.

Detects meetings, gaming, focus mode, quiet hours, and user away.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("addled.presence_guard")

# ---------------------------------------------------------------------------
# Gaming detection: the decision, separated from the reading of it.
#
# `classify_window` is a PURE function - facts in, verdict out. Everything that
# talks to Windows lives in `_window_facts`, so the decision can be tested
# without a desktop, and `check_gaming_pause.py` calls the shipping code
# directly instead of keeping a copy of the rules that could drift from it.
#
# The rules exist because of a measured failure. On this machine the old test
# ("covers the work area and is frameless") called three windows a game, and
# none of them was one: the Windows desktop (`Progman`), the text-input flyout,
# and a cursor overlay. They are indistinguishable from a borderless game by
# size and framelessness alone, so the decision also reads the window CLASS,
# its extended style, and the binary behind it.
# ---------------------------------------------------------------------------

# Windows shell and input surfaces. These are never games however they are
# sized. `Windows.UI.Core.CoreWindow` is the one that matters: it is the class
# of the text-input flyout, and it is ALSO the class of six hidden
# `DesktopWindowXamlSource` windows, so a class-name lookup is not a safe way to
# reach one of them (see `_window_facts`).
SHELL_CLASSES = frozenset({
    "Progman",                      # the desktop itself
    "WorkerW",                      # the desktop's wallpaper layer
    "Shell_TrayWnd",                # the taskbar
    "Windows.UI.Core.CoreWindow",   # text-input flyout, XAML islands
    "ApplicationFrameWindow",       # desktop-bridge host
    "MultitaskingViewFrame",        # task view
    "ForegroundStaging",            # the shell's foreground-handoff window
    "XamlExplorerHostIslandWindow",  # shell XAML host
    "Cua.AgentCursorOverlay",       # cursor overlay agents
})

# A binary under the Windows directory is never a game. This is a rejection
# only: it can never ACCEPT something, because a game and a tool under D:\Games
# are indistinguishable by path.
SYSTEM_ROOTS = ("%SystemRoot%", "%SystemRoot%\\SystemApps")

# A pixel or two of slack. A borderless window can be placed at -1 or sized one
# pixel short by its own window procedure, and one pixel under must not mean
# "not a game".
SLACK = 2

WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000


def _under_system(roots, path: str) -> bool:
    """Whether `path` is inside one of `roots`.

    Normalised and case-folded, with exactly one separator on both sides. A
    plain prefix test is wrong twice: `C:\\WindowsApps` would match `C:\\Windows`,
    and a doubled separator matches nothing at all - and matching nothing here
    FAILS OPEN, because the window is then treated as a possible game. Both were
    hit while testing this.
    """
    if not path:
        return False
    p = os.path.normpath(str(path)).lower()
    for root in roots:
        r = os.path.expandvars(str(root))
        if "%" in r:          # an unset variable - do not guess
            continue
        r = os.path.normpath(r).rstrip("\\").lower() + "\\"
        if p.startswith(r):
            return True
    return False


def classify_window(*, class_name: str, width: int, height: int,
                    style: int, exstyle: int, zoomed: bool,
                    work_w: int, work_h: int,
                    pid: int = 0, own_pid: int = -1,
                    image_path: str = "",
                    monitor_w: int = 0, monitor_h: int = 0,
                    shell_classes=SHELL_CLASSES,
                    system_roots=SYSTEM_ROOTS,
                    noactivate_rejects: bool = True) -> tuple[bool, str]:
    """Is the described window a game taking the screen? Returns (verdict, why).

    Pure: no ctypes, no HWND, no I/O. The reason string names the FIRST rule
    that fired, which is what makes a wrong verdict diagnosable from a log - and
    what lets the tests assert a row failed for the right reason rather than by
    luck.
    Order is deliberate. Size and identity come first because they are decisive
    and cheap; the style rules follow. `IsZoomed` comes before the caption test
    because a maximised window is the specific thing that looks like a
    full-screen app without being one. The monitor test comes after both,
    because it is the only thing that recognises a game which keeps its caption.
    """
    if width < work_w - SLACK or height < work_h - SLACK:
        return False, "too small"

    if own_pid >= 0 and pid == own_pid:
        # The character is frameless and always-on-top. A large one would
        # otherwise pause the model because of its own window.
        return False, "own process"

    if class_name in shell_classes:
        return False, "shell class"

    if _under_system(system_roots, image_path):
        return False, "system binary"

    if noactivate_rejects and (exstyle & WS_EX_NOACTIVATE):
        # A window that never takes activation is an overlay, not something
        # being played.
        return False, "no-activate overlay"

    if (exstyle & WS_EX_TOOLWINDOW) and not (exstyle & WS_EX_APPWINDOW):
        return False, "tool window"

    if (exstyle & WS_EX_LAYERED) and (exstyle & WS_EX_TRANSPARENT):
        return False, "click-through overlay"

    if zoomed:
        return False, "maximised"

    has_caption = bool(style & WS_CAPTION)
    has_frame = bool(style & WS_THICKFRAME)
    is_popup = bool(style & WS_POPUP)

    # A window LARGER than the monitor is not a normal window: the user cannot
    # see the edges or reach the title bar. It is how borderless fullscreen is
    # implemented - draw past the screen edge so no border shows. Measured on
    # Delta Force in windowed mode: 2576x1479 at (-8,-31) on a 2560x1440
    # monitor, with WS_CAPTION set, which the rules below would otherwise call a
    # dialog. Not zoomed, so `IsZoomed` does not catch it either.
    #
    # This is placed AFTER the shell and overlay rules on purpose: the two
    # other windows that match on this desktop are `Progman` and a cursor
    # overlay, and both are rejected before reaching here.
    if monitor_w and monitor_h:
        if width > monitor_w + SLACK and height > monitor_h + SLACK:
            return True, "larger than the monitor"

    # A popup with a caption and no frame is a dialog or a splash.
    if has_caption and not has_frame:
        return False, "captioned popup"

    # An ordinary window keeps its caption and resize frame even when
    # maximised. A borderless game has neither.
    if (has_caption or has_frame) and not is_popup:
        return False, "framed"

    return True, "borderless fullscreen"


def _image_path(pid: int) -> str:
    """The executable behind a process, or "" if it cannot be read.

    Best effort by design. This feature only ever REJECTS - a window whose
    binary is under the Windows directory is never a game - so failing to read
    a path must leave the decision exactly as it was, not reject the window.
    That is why every failure returns "" rather than raising.

    `PROCESS_QUERY_LIMITED_INFORMATION` is enough, and does not need
    elevation: that is the whole reason to use it over the stronger query
    rights, which would fail for processes owned by other users or elevated.
    """
    if not pid:
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                      pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


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

        This reads the window and hands the facts to `classify_window`, which
        owns the decision. The split matters: the rules are the part that was
        wrong before, and they are now testable without a desktop.

        Measured on this machine is both the failure and the target. A real
        borderless game (Delta Force) is a popup with no caption, no frame,
        and the work area behind it. So are three things that are NOT games,
        and the old "big and frameless" test called all of them one:

          * the Windows desktop (`Progman`) - the shell's own window
          * the text-input flyout (`Windows.UI.Core.CoreWindow`)
          * a cursor overlay (`Cua.AgentCursorOverlay`)

        Size and framelessness cannot separate those from a game; only the
        window class, its extended style and the binary behind it can. The
        reason for the verdict is logged, so a surprising pause can be traced
        to the rule that caused it.
        """
        try:
            import ctypes
            facts = self._window_facts()
            if facts is None:
                # Could not read the window. NOT a game: a failed style read
                # returns 0, and 0 is frameless, and frameless is what a game
                # looks like - so guessing here fails open.
                return False

            verdict, why = classify_window(
                class_name=facts["class_name"],
                width=facts["width"], height=facts["height"],
                style=facts["style"], exstyle=facts["exstyle"],
                zoomed=facts["zoomed"],
                work_w=facts["work_w"], work_h=facts["work_h"],
                monitor_w=facts["monitor_w"], monitor_h=facts["monitor_h"],
                pid=facts["pid"], own_pid=os.getpid(),
                image_path=facts["image_path"],
            )
            if self._in_gaming != verdict:
                log.info("gaming=%s (%s) window=%r", verdict, why,
                         facts["class_name"])
            return verdict
        except Exception as exc:
            log.debug("gaming detection failed: %s", exc)
            return False

    @staticmethod
    def _window_facts():
        """Read everything the classifier needs from the FOREGROUND window.

        Returns None on any failed read. None must not be treated as "no
        style bits": `GetWindowLongW` on a dead handle returns 0, which reads
        as frameless, which reads as a game. Every read is guarded, and the
        handle is re-checked after reading the styles in case it died between
        the two calls.

        The foreground HWND is used directly, and that is deliberate: several
        windows share the class `Windows.UI.Core.CoreWindow` (the text-input
        flyout plus several hidden `DesktopWindowXamlSource` windows), so a
        lookup by class name returns whichever came first, not necessarily the
        one that is in front. Do not replace this with `FindWindowW`.
        """
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        hwnd = user32.GetForegroundWindow()
        if not hwnd or not user32.IsWindow(hwnd):
            return None

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        GWL_STYLE, GWL_EXSTYLE = -16, -20
        style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF

        cls = ctypes.create_unicode_buffer(256)
        if not user32.GetClassNameW(hwnd, cls, 256):
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        # Re-check: the handle can die between the reads above, and the bytes
        # already gathered would then describe a window that no longer exists.
        if not user32.IsWindow(hwnd):
            return None

        # The work area excludes the taskbar. A borderless game that does not
        # cover the taskbar is this size, and it is still taking the screen.
        work = RECT()
        got_work = bool(user32.SystemParametersInfoW(
            0x0030, 0, ctypes.byref(work), 0))     # SPI_GETWORKAREA
        if got_work:
            work_w = work.right - work.left
            work_h = work.bottom - work.top
        else:
            work_w = user32.GetSystemMetrics(0)
            work_h = user32.GetSystemMetrics(1)

        # The monitor the window sits on, for the "larger than the screen"
        # test. MONITOR_DEFAULTTONEAREST (2) always returns a monitor, so there
        # is no failure case to handle here.
        mon = (ctypes.c_long * 4)()
        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong),
                        ("rcMonitor", (ctypes.c_long * 4)),
                        ("rcWork", (ctypes.c_long * 4)),
                        ("dwFlags", ctypes.c_ulong)]
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        monitor_w = monitor_h = 0
        if user32.GetMonitorInfoW(
                user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):
            monitor_w = mi.rcMonitor[2] - mi.rcMonitor[0]
            monitor_h = mi.rcMonitor[3] - mi.rcMonitor[1]

        return {
            "class_name": cls.value,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            "style": style,
            "exstyle": exstyle,
            "zoomed": bool(user32.IsZoomed(hwnd)),
            "work_w": work_w,
            "work_h": work_h,
            "monitor_w": monitor_w,
            "monitor_h": monitor_h,
            "pid": pid.value,
            "image_path": _image_path(pid.value),
        }

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
