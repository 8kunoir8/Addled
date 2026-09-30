"""
Window manager — enumerate, focus, resize, move, minimize windows.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("addled.window_mgr")


class WindowManager:
    """Window operations."""

    async def list_windows(self) -> dict:
        """List all visible windows with titles and positions."""
        try:
            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                windows = []
                def enum_callback(hwnd, _):
                    if user32.IsWindowVisible(hwnd):
                        length = user32.GetWindowTextLengthW(hwnd)
                        if length > 0:
                            buf = ctypes.create_unicode_buffer(length + 1)
                            user32.GetWindowTextW(hwnd, buf, length + 1)
                            title = buf.value
                            if title and title.strip():
                                rect = ctypes.wintypes.RECT()
                                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                                windows.append({
                                    "title": title,
                                    "hwnd": hwnd,
                                    "x": rect.left, "y": rect.top,
                                    "width": rect.right - rect.left,
                                    "height": rect.bottom - rect.top,
                                })
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
                return {"success": True, "windows": windows, "count": len(windows)}
            return {"success": True, "windows": [], "count": 0, "summary": "Window listing only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def focus(self, title_substring: str) -> dict:
        """Bring a window to front by title substring."""
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                # See close(): a match that finds nothing must say so rather
                # than report success. This mattered less before the live check
                # proved a model will trust "success" and tell the user a
                # window was focused when nothing had moved.
                found = {"title": ""}

                def find_window(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetForegroundWindow(hwnd)
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            found["title"] = buf.value
                            return False
                    return True

                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(find_window), 0)
                if not found["title"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'.")}
                return {"success": True,
                        "summary": f"Focused: {found['title']}"}
            return {"success": False, "error": "Window focus only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def resize(self, title_substring: str, width: int, height: int) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                SWP_NOZORDER = 0x0004
                found = {"title": ""}
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetWindowPos(hwnd, 0, 0, 0, width, height, SWP_NOZORDER)
                            found["title"] = buf.value
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                if not found["title"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'.")}
                return {"success": True, "summary": f"Resized {found['title']}"}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def move(self, title_substring: str, x: int, y: int) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                SWP_NOSIZE = 0x0001
                SWP_NOZORDER = 0x0004
                found = {"title": ""}
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
                            found["title"] = buf.value
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                if not found["title"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'.")}
                return {"success": True, "summary": f"Moved {found['title']}"}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def minimize(self, title_substring: str) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                found = {"title": ""}
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                            found["title"] = buf.value
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                if not found["title"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'.")}
                return {"success": True, "summary": f"Minimized {found['title']}"}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def maximize(self, title_substring: str) -> dict:
        return await self._show_window(title_substring, 3)  # SW_MAXIMIZE

    async def restore(self, title_substring: str) -> dict:
        return await self._show_window(title_substring, 9)  # SW_RESTORE

    async def close(self, title_substring: str) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                WM_CLOSE = 0x0010
                # Whether a window actually matched. The callback's own return
                # value cannot carry this: it is the enumeration protocol's
                # "keep going?" flag, so returning False there means "stop
                # looking", not "I found it". Reading it as a result is how this
                # came to report success on a title that matched nothing — a
                # live check closed "Notepad" that was not open, was told
                # `success: True`, and the window list was unchanged.
                found = {"title": "", "count": 0}

                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                            found["count"] += 1
                            if not found["title"]:
                                found["title"] = buf.value
                            return False
                    return True

                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                if not found["count"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'. Nothing was "
                                      f"closed.")}
                return {"success": True,
                        "summary": f"Closed {found['title']}",
                        "closed": found["title"], "count": found["count"]}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _show_window(self, title_substring: str, cmd: int) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                found = {"title": ""}
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.ShowWindow(hwnd, cmd)
                            found["title"] = buf.value
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                if not found["title"]:
                    return {"success": False,
                            "error": (f"No open window matched "
                                      f"'{title_substring}'.")}
                return {"success": True, "summary": f"Updated {found['title']}"}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}
