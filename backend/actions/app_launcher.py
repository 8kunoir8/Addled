"""
App launcher — start, stop, and find applications.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("addled.launcher")


class AppLauncher:
    """Launch, close, and find applications."""

    async def launch(self, name: str, path: str | None = None, args: list | None = None) -> dict:
        """Launch an application by name or path."""
        try:
            if path:
                cmd = [path] + (args or [])
                subprocess.Popen(cmd, shell=False)
                return {"success": True, "summary": f"Launched {name}"}
            # Try common launch methods
            if sys.platform == "win32":
                subprocess.Popen(["start", "", name], shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", name])
            else:
                subprocess.Popen([name], shell=False)
            return {"success": True, "summary": f"Launched {name}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def close(self, name: str, pid: int | None = None) -> dict:
        """Close an application by name or PID."""
        try:
            if pid:
                os.kill(pid, 15 if sys.platform != "win32" else 0)  # SIGTERM or terminate
                return {"success": True, "summary": f"Closed PID {pid}"}
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
            elif sys.platform == "darwin":
                subprocess.run(["pkill", "-f", name], capture_output=True)
            else:
                subprocess.run(["pkill", "-f", name], capture_output=True)
            return {"success": True, "summary": f"Closed {name}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def find(self, name: str) -> dict:
        """Find an application's executable path."""
        try:
            if sys.platform == "win32":
                result = subprocess.run(["where", name], capture_output=True,
                                        text=True, encoding="utf-8",
                                        errors="replace")
                paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
                return {"success": True, "paths": paths, "found": len(paths) > 0}
            else:
                result = subprocess.run(["which", name], capture_output=True,
                                        text=True, encoding="utf-8",
                                        errors="replace")
                return {"success": True, "path": result.stdout.strip(), "found": result.returncode == 0}
        except Exception as e:
            return {"success": False, "error": str(e)}
