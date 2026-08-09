"""
Input simulator — mouse and keyboard synthetic input.

Uses pyautogui for cross-platform compatibility.
Typing has human-like cadence with random delays.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

log = logging.getLogger("addled.input")


class InputSimulator:
    """Keyboard and mouse synthetic input with human-like cadence."""

    def __init__(self):
        self._cancel_flag = False

    def cancel(self):
        self._cancel_flag = True

    # ---- keyboard ------------------------------------------------------------

    async def type_text(self, text: str, cadence_ms: float = 80.0) -> dict:
        """Type text with realistic delays."""
        try:
            import pyautogui
            self._cancel_flag = False
            started = time.monotonic()
            chars = 0
            for i, char in enumerate(text):
                if self._cancel_flag:
                    return {"success": False, "error": "cancelled", "chars": chars}
                prev = text[i - 1] if i > 0 else None
                delay = self._char_delay(char, prev, cadence_ms)
                await asyncio.sleep(delay / 1000)
                pyautogui.write(char, interval=0)
                chars += 1
            elapsed = int((time.monotonic() - started) * 1000)
            return {"success": True, "chars": chars, "duration_ms": elapsed}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    def _char_delay(char: str, prev: str | None, base: float) -> float:
        fast_bigrams = {"th","er","in","an","on","at","en","nd","ti","es","or","te","ed","is","it","al","ar","st","to","nt","ng","le","re","ve","de","co","me"}
        if prev and prev.lower() + char.lower() in fast_bigrams:
            delay = base * 0.4
        elif char in ".!?\n":
            delay = base * 2.5
        elif char in ",;:":
            delay = base * 1.5
        elif char == " ":
            delay = base * 1.2
        else:
            delay = base
        return delay * random.uniform(0.8, 1.2)

    async def press_keys(self, combo: str) -> dict:
        """Press a key combination like 'ctrl+s', 'alt+tab'."""
        try:
            import pyautogui
            pyautogui.hotkey(*combo.split("+"))
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ---- mouse ---------------------------------------------------------------

    async def click(self, x: int, y: int, button: str = "left") -> dict:
        try:
            import pyautogui
            pyautogui.click(x, y, button=button)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def double_click(self, x: int, y: int) -> dict:
        try:
            import pyautogui
            pyautogui.doubleClick(x, y)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def move(self, x: int, y: int) -> dict:
        try:
            import pyautogui
            pyautogui.moveTo(x, y, duration=0.3)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        try:
            import pyautogui
            delta = amount * (1 if direction == "up" else -1)
            pyautogui.scroll(delta * 40)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> dict:
        try:
            import pyautogui
            pyautogui.moveTo(x1, y1)
            pyautogui.drag(x2 - x1, y2 - y1, duration=0.5)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyautogui not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}
