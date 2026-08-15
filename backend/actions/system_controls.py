"""
System controls — volume, brightness, lock, screenshot, clipboard.
"""

from __future__ import annotations

import logging
import sys
import subprocess

log = logging.getLogger("addled.system_ctrl")


class SystemControls:
    """Desktop system operations."""

    async def set_volume(self, level: int) -> dict:
        """Set system volume 0-100."""
        try:
            if sys.platform == "win32":
                import ctypes
                # Send volume up/down keys via keybd_event
                # Simple approach: use nircmd or sndvol
                subprocess.run(["nircmd", "setsysvolume", str(int(level * 655.35))],
                             capture_output=True)
                return {"success": True, "level": level}
            return {"success": False, "error": "Volume control only on Windows with nircmd"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_brightness(self, level: int) -> dict:
        """Set screen brightness 0-100."""
        try:
            if sys.platform == "win32":
                import ctypes
                # WMI approach
                import subprocess
                cmd = f'powershell (Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})'
                subprocess.run(cmd, shell=True, capture_output=True)
                return {"success": True, "level": level}
            return {"success": False, "error": "Brightness control only on Windows"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def lock(self) -> dict:
        """Lock the workstation."""
        try:
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.LockWorkStation()
            elif sys.platform == "darwin":
                subprocess.run(["pmset", "displaysleepnow"])
            else:
                subprocess.run(["xdg-screensaver", "lock"])
            return {"success": True, "summary": "Workstation locked"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def screenshot(self, monitor: int | None = None, region: dict | None = None,
                         mask_zones: list | None = None) -> dict:
        """Capture a screenshot. Returns base64 PNG.
        mask_zones: list of {x, y, w, h} rects to black out (privacy)."""
        try:
            import mss
            import base64
            from io import BytesIO
            with mss.mss() as sct:
                if monitor is not None:
                    mon = sct.monitors[min(monitor, len(sct.monitors) - 1)]
                    img = sct.grab(mon)
                elif region:
                    img = sct.grab(region)
                else:
                    img = sct.grab(sct.monitors[1])  # Primary monitor
                from PIL import Image, ImageDraw
                pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                for zone in mask_zones or []:
                    try:
                        x, y = int(zone.get("x", 0)), int(zone.get("y", 0))
                        w, h = int(zone.get("w", 0)), int(zone.get("h", 0))
                        if w > 0 and h > 0:
                            ImageDraw.Draw(pil_img).rectangle(
                                [x, y, x + w, y + h], fill=(0, 0, 0))
                    except (TypeError, ValueError):
                        continue
                buf = BytesIO()
                pil_img.save(buf, format="PNG")
                return {"success": True, "image_b64": base64.b64encode(buf.getvalue()).decode(),
                        "width": img.width, "height": img.height}
        except ImportError as e:
            return {"success": False, "error": f"Missing dependency: {e}. Install mss + Pillow."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_clipboard(self) -> dict:
        try:
            import pyperclip
            text = pyperclip.paste()
            from backend.config import config
            if config.get("safety", "clipboard_filter", default=True):
                from backend.safety.clipboard_filter import redact
                text, hits = redact(text)
                return {"success": True, "text": text, "redacted": hits}
            return {"success": True, "text": text}
        except ImportError:
            return {"success": False, "error": "pyperclip not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_clipboard(self, text: str) -> dict:
        try:
            import pyperclip
            pyperclip.copy(text)
            return {"success": True}
        except ImportError:
            return {"success": False, "error": "pyperclip not installed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_screen_size(self) -> dict:
        try:
            import mss
            with mss.mss() as sct:
                mon = sct.monitors[1]
                return {"success": True, "width": mon["width"], "height": mon["height"]}
        except ImportError:
            try:
                import tkinter
                root = tkinter.Tk()
                w, h = root.winfo_screenwidth(), root.winfo_screenheight()
                root.destroy()
                return {"success": True, "width": w, "height": h}
            except:
                return {"success": True, "width": 1920, "height": 1080}
        except Exception as e:
            return {"success": False, "error": str(e)}
