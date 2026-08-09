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

                def find_window(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetForegroundWindow(hwnd)
                            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                            return False
                    return True

                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(find_window), 0)
                return {"success": True, "summary": f"Focused: {title_substring}"}
            return {"success": False, "error": "Window focus only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def resize(self, title_substring: str, width: int, height: int) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                SWP_NOZORDER = 0x0004
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetWindowPos(hwnd, 0, 0, 0, width, height, SWP_NOZORDER)
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                return {"success": True}
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
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.SetWindowPos(hwnd, 0, x, y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                return {"success": True}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def minimize(self, title_substring: str) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                return {"success": True}
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
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                return {"success": True}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _show_window(self, title_substring: str, cmd: int) -> dict:
        try:
            if sys.platform == "win32":
                import ctypes
                user32 = ctypes.windll.user32
                def cb(hwnd, _):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        if title_substring.lower() in buf.value.lower():
                            user32.ShowWindow(hwnd, cmd)
                            return False
                    return True
                WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
                user32.EnumWindows(WNDENUMPROC(cb), 0)
                return {"success": True}
            return {"success": False, "error": "Only supported on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}
