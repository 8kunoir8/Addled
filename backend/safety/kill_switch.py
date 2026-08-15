"""
Kill switch — global hotkey that immediately stops the agent.

Uses the Win32 RegisterHotKey API (pywin32, already bundled), so it works
even when the app is not focused. Default: ctrl+shift+alt+k (configurable
in settings via safety.kill_switch_hotkey).
"""

from __future__ import annotations

import ctypes
import logging
import threading

log = logging.getLogger("addled.kill_switch")

MODS = {
    "ctrl": 0x0002, "control": 0x0002,
    "shift": 0x0004,
    "alt": 0x0001,
    "win": 0x0008,
}

VK_KEYS = {
    "space": 0x20,
    "escape": 0x1B, "esc": 0x1B,
    "tab": 0x09, "enter": 0x0D, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "pause": 0x13, "printscreen": 0x2C,
}

WM_HOTKEY = 0x0312


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse 'ctrl+shift+alt+k' → (modifiers_mask, virtual_key)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty hotkey")
    mods = 0
    key = None
    for p in parts:
        if p in MODS:
            mods |= MODS[p]
        else:
            key = p
    if key is None:
        raise ValueError(f"Hotkey '{spec}' has no key")
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    elif key in VK_KEYS:
        vk = VK_KEYS[key]
    else:
        raise ValueError(f"Unsupported hotkey key: '{key}'")
    return mods, vk


class KillSwitch:
    """Background thread listening for the global kill hotkey."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._callbacks: list = []
        self._hotkey_id = 0xBADD  # arbitrary WM_HOTKEY id
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def on_activated(self, callback) -> None:
        self._callbacks.append(callback)

    def _trigger(self) -> None:
        log.warning("KILL SWITCH ACTIVATED — stopping agent")
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception as e:
                log.warning("Kill switch callback failed: %s", e)

    def start(self, hotkey_spec: str = "ctrl+shift+alt+k") -> bool:
        """Start the listener thread. The OS hotkey is registered inside the
        pump thread, because WM_HOTKEY is delivered to the registering
        thread's message queue."""
        if self._running:
            return True
        try:
            mods, vk = parse_hotkey(hotkey_spec)
        except ValueError as e:
            log.warning("Invalid kill switch hotkey '%s': %s", hotkey_spec, e)
            return False

        self._running = True
        self._thread = threading.Thread(target=self._pump, args=(mods, vk),
                                        daemon=True, name="kill-switch")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread:
            try:
                # Wake the pump thread so it exits and unregisters the hotkey
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread.ident or 0, 0x0400, 0, 0)
            except Exception:
                pass

    def _pump(self, mods: int, vk: int) -> None:
        try:
            import win32gui
            user32 = ctypes.windll.user32
            if not user32.RegisterHotKey(None, self._hotkey_id, mods, vk):
                log.warning("RegisterHotKey failed (already registered?)")
                return
            self._active = True
            log.info("Kill switch armed")
            while self._running:
                raw = win32gui.GetMessage(None, 0, 0)
                if not raw or not raw[0]:
                    continue
                # pywin32 GetMessage → [result, (hwnd, msg, wParam, lParam, time, pt)]
                msg_tuple = raw[1]
                if (len(msg_tuple) > 2 and msg_tuple[1] == WM_HOTKEY
                        and msg_tuple[2] == self._hotkey_id):
                    self._trigger()
        except Exception as e:
            log.warning("Kill switch message pump stopped: %s", e)
        finally:
            if self._active:
                try:
                    ctypes.windll.user32.UnregisterHotKey(None, self._hotkey_id)
                except Exception:
                    pass
            self._running = False
            self._active = False


kill_switch = KillSwitch()
