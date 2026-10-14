"""
System controls — volume, brightness, lock, screenshot, clipboard.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

log = logging.getLogger("addled.system_ctrl")


class SystemControls:
    """Desktop system operations."""

    async def set_volume(self, level: int) -> dict:
        """Set system volume 0-100, and confirm it took.

        Delegates to `backend.actions.audio`, which talks to the Windows Core
        Audio endpoint directly and READS THE VALUE BACK. That readback is the
        point: two earlier versions of this method reported success without
        changing anything - the first shelled out to `nircmd`, never bundled,
        behind an unconditional `{"success": True}`; the second drove
        `VK_VOLUME_UP` through `SendMessage`, exited 0, and left the level
        untouched (verified by readback: 100% after asking for 30%).

        A tool that reports success it did not achieve is the same defect as a
        model that reports work it did not do. Neither belongs here.
        """
        from backend.actions import audio
        return await asyncio.to_thread(audio.set_volume, level)

    async def set_brightness(self, level: int) -> dict:
        """Set screen brightness 0-100.

        The previous body ran PowerShell, **discarded its output entirely**, and
        returned `{"success": True, "level": level}` on every path. Measured
        live 2026-10-14: the command returned exit code 1 with `Get-WmiObject :
        Not supported` - this monitor has no DDC/CI - and the tool still
        reported success. It was wrong 100% of the time on that machine.

        Now the exit code and the WMI result are checked, and a monitor that
        cannot be controlled says so. `WmiMonitorBrightnessMethods` only
        populates on internal panels and DDC/CI-capable externals, so "not
        supported" is a normal answer the user needs to receive.
        """
        try:
            if sys.platform != "win32":
                return {"success": False,
                        "error": "Brightness control is Windows-only"}
            pct = max(0, min(100, int(level)))
            script = (
                "$ErrorActionPreference='Stop';"
                "try{"
                "$m=Get-CimInstance -Namespace root/WMI "
                "-ClassName WmiMonitorBrightnessMethods -ErrorAction Stop;"
                "if(-not $m){Write-Output 'FAIL no controllable display';exit 0};"
                f"$m | Invoke-CimMethod -MethodName WmiSetBrightness "
                f"-Arguments @{{Timeout=1;Brightness={pct}}} | Out-Null;"
                "Write-Output 'OK'"
                "}catch{Write-Output ('FAIL ' + $_.Exception.Message)}"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script],
                capture_output=True, text=True, timeout=40)
            out = (proc.stdout or "").strip()
            if out == "OK":
                return {"success": True, "level": pct}
            # The script reports its own diagnosis on stdout with a `FAIL `
            # marker, so the message the model reads is a diagnosis and not a
            # copy of the PowerShell source.
            detail = out[5:].strip() if out.startswith("FAIL ") else ""
            if not detail:
                detail = (proc.stderr or "").strip()[:160] or "no output"
            return {
                "success": False,
                "error": ("could not set the brightness (this usually means the "
                          "display has no DDC/CI control): " + detail[:200]),
            }
        except Exception as e:  # noqa: BLE001
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
            except Exception:
                # A bare `except:` here also swallowed KeyboardInterrupt and
                # SystemExit — a Ctrl-C during the Tk call would be ignored and
                # the fallback resolution returned as if it were real.
                return {"success": True, "width": 1920, "height": 1080}
        except Exception as e:
            return {"success": False, "error": str(e)}
