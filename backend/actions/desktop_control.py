"""
Gated desktop input control — mouse/keyboard with permission + guardrails.

Default-OFF (config desktop.allow_input). When enabled:
  - optional session grant (config desktop.require_session_approval):
    first use broadcasts desktop.permissionRequest; the user approves
    from the dashboard (desktop.grant), valid for session_timeout_min.
  - coordinate bounds (inside the primary screen),
  - typed-text length cap (max_type_chars),
  - hotkey whitelist (+ optional extended combos),
  - pyautogui FAILSAFE enabled (mouse to top-left corner aborts input),
  - every action logged to the egress monitor (text content as length only).
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("addled.desktop")

WHITELIST_HOTKEYS = {
    "ctrl+c", "ctrl+v", "ctrl+z", "ctrl+a", "ctrl+s", "ctrl+f", "ctrl+shift+s",
}
EXTENDED_HOTKEYS = {
    "alt+tab", "alt+f4", "win+d", "win+e", "win+r", "win+l",
}


class DesktopControl:
    """Permission-gated synthetic mouse/keyboard control."""

    def __init__(self):
        self._granted_at = 0.0

    def _cfg(self):
        from backend.config import config
        return config

    def grant(self) -> None:
        self._granted_at = time.time()
        log.info("Desktop control granted (session)")

    def revoke(self) -> None:
        self._granted_at = 0.0
        log.info("Desktop control revoked")

    def granted(self) -> bool:
        cfg = self._cfg()
        if not cfg.get("desktop", "allow_input", default=False):
            return False
        if not cfg.get("desktop", "require_session_approval", default=True):
            return True
        timeout = int(cfg.get("desktop", "session_timeout_min",
                              default=15)) * 60
        return self._granted_at > 0 and time.time() - self._granted_at < timeout

    # ---- guards ------------------------------------------------------------

    def _guard(self, action: str) -> dict | None:
        """Error dict when the action is not permitted, else None."""
        cfg = self._cfg()
        if not cfg.get("desktop", "allow_input", default=False):
            return {"success": False,
                    "error": "Desktop control is disabled "
                             "(Settings → Desktop Control)"}
        if not self.granted():
            return {"success": False, "requires_grant": True,
                    "error": "Desktop control needs permission — approve it "
                             "in the dashboard"}
        return None

    async def _request_grant(self) -> None:
        try:
            from backend.ws_server import get_server
            await get_server().broadcast("desktop.permissionRequest",
                                         {"ts": time.time()})
        except Exception as e:
            log.debug("permission broadcast failed: %s", e)

    def _log(self, action: str, detail: dict) -> None:
        try:
            from backend.safety.egress_monitor import egress
            egress.record(f"desktop.{action}", detail)
        except Exception:
            pass

    def _screen_bounds(self) -> tuple[int, int]:
        try:
            import pyautogui
            w, h = pyautogui.size()
            return int(w), int(h)
        except Exception:
            return 32767, 32767

    def _in_bounds(self, x, y) -> bool:
        w, h = self._screen_bounds()
        return 0 <= int(x) <= w and 0 <= int(y) <= h

    # ---- input actions ------------------------------------------------------

    async def click(self, x: int, y: int, button: str = "left") -> dict:
        err = self._guard("click")
        if err:
            await self._request_grant()
            return err
        if not self._in_bounds(x, y):
            return {"success": False, "error": "Coordinates outside the screen"}
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().click(int(x), int(y), button)
        self._log("click", {"x": int(x), "y": int(y), "button": button})
        return res

    async def move_mouse(self, x: int, y: int) -> dict:
        err = self._guard("move_mouse")
        if err:
            await self._request_grant()
            return err
        if not self._in_bounds(x, y):
            return {"success": False, "error": "Coordinates outside the screen"}
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().move(int(x), int(y))
        self._log("move_mouse", {"x": int(x), "y": int(y)})
        return res

    async def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        err = self._guard("scroll")
        if err:
            await self._request_grant()
            return err
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().scroll(direction, min(int(amount), 20))
        self._log("scroll", {"direction": direction})
        return res

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        err = self._guard("drag")
        if err:
            await self._request_grant()
            return err
        if not (self._in_bounds(x1, y1) and self._in_bounds(x2, y2)):
            return {"success": False, "error": "Coordinates outside the screen"}
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().drag(int(x1), int(y1), int(x2), int(y2))
        self._log("drag", {"from": [int(x1), int(y1)],
                           "to": [int(x2), int(y2)]})
        return res

    async def type_text(self, text: str) -> dict:
        err = self._guard("type")
        if err:
            await self._request_grant()
            return err
        text = str(text or "")
        cap = int(self._cfg().get("desktop", "max_type_chars", default=500))
        if len(text) > cap:
            return {"success": False,
                    "error": f"Text exceeds the {cap}-character typing limit"}
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().type_text(text)
        self._log("type", {"chars": len(text)})  # content never logged
        return res

    async def hotkey(self, combo: str) -> dict:
        err = self._guard("hotkey")
        if err:
            await self._request_grant()
            return err
        combo = (combo or "").strip().lower().replace(" ", "")
        allow_ext = bool(self._cfg().get("desktop", "allow_extended_hotkeys",
                                          default=False))
        if combo not in WHITELIST_HOTKEYS and not (
                allow_ext and combo in EXTENDED_HOTKEYS):
            return {"success": False,
                    "error": f"Hotkey not allowed: {combo}"}
        from backend.actions.input_simulator import InputSimulator
        res = await InputSimulator().press_keys(combo)
        self._log("hotkey", {"combo": combo})
        return res


# Singleton
desktop_control = DesktopControl()
