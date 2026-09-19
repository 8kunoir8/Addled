"""
Terminal executor — safe command execution with allowlists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("addled.terminal")

# Commands that are always safe to run
SAFE_COMMANDS = {
    "dir", "ls", "echo", "type", "cat", "find", "grep", "where", "which",
    "cd", "pwd", "date", "time", "whoami", "hostname", "ipconfig", "ping",
    "nslookup", "netstat", "tasklist", "systeminfo", "ver",
    "git status", "git log", "git diff", "git branch",
    "git push", "git commit", "git pull", "git clone",
    "python --version", "node --version", "npm list", "pip list",
    "pip install", "npm install", "npm i ", "winget", "choco",
    "mkdir", "move", "copy", "xcopy", "robocopy",
    "curl", "iwr", "invoke-webrequest", "invoke-restmethod",
    "net start", "net stop", "schtasks", "sc", "chmod", "attrib",
}

# Commands that ALWAYS require user approval (never run autonomously)
DANGEROUS_COMMANDS = {
    "del ", "del\t", "del/", "erase ", "rm ", "rmdir", "rd ",
    "format", "shutdown", "restart", "logoff",
    "diskpart", "cipher", "reg delete", "reg add",
    "net user", "net localgroup", "takeown", "icacls",
    "rm -rf", "rmdir /s",
}


# ---- RTK (Rust Token Killer) integration -----------------------------------
# When rtk.exe is available, eligible single commands are rewritten to their RTK
# equivalents so the agent sees compact output (60-90% fewer tokens) instead of
# raw dumps. Purely optional — raw passthrough otherwise, and the binary is
# something the user installs from Settings → Tools rather than something the
# installer carries (see backend/tools/rtk.py).

RTK_PREFIXES = (
    "git ", "pip ", "python -m pip", "pytest", "python -m pytest",
    "npm ", "ruff ", "gh ", "docker ", "kubectl ", "cargo ",
)


def _find_rtk() -> str | None:
    """Locate rtk.exe: PATH first, then the copies the app installed."""
    from backend.tools import rtk
    return rtk.find("rtk.exe")


def _rtk_rewrite(command: str, rtk: str | None) -> tuple[str, bool]:
    """Rewrite `git status` → `& 'C:\\...\\rtk.exe' git status` when eligible."""
    if not rtk:
        return command, False
    try:
        from backend.config import config
        if not config.get("tools", "rtk_enabled", default=True):
            return command, False
    except Exception:
        pass
    stripped = command.strip()
    if ";" in stripped or stripped.startswith("rtk"):
        return command, False  # chained or already-rtk commands — passthrough
    if not any(stripped.startswith(p) for p in RTK_PREFIXES):
        return command, False
    log.info("RTK: %s", stripped)
    return f"& '{rtk}' {stripped}", True


class TerminalExecutor:
    """Execute shell commands safely."""

    async def execute(self, command: str, cwd: str | None = None, timeout: int = 30,
                      allow_dangerous: bool = False) -> dict:
        """Execute a shell command and return stdout/stderr."""
        if not command.strip():
            return {"success": False, "error": "Empty command"}

        # Check against the danger list (matches DestructionGate)
        cmd_lower = command.lower().strip()
        is_dangerous = any(cmd_lower.startswith(c) for c in DANGEROUS_COMMANDS)
        if is_dangerous and not allow_dangerous:
            return {"success": False, "error": "This command requires approval. Use the dashboard to confirm.",
                    "requires_approval": True}

        # Defensive translation of common bash-isms for Windows PowerShell 5.1
        if sys.platform == "win32":
            command = command.replace(" && ", " ; ")
            command = command.replace("~/", "$HOME/")

        # Optional RTK compression for high-output commands (graceful fallback)
        rtk_path = _find_rtk()
        command, rewritten = _rtk_rewrite(command, rtk_path)
        env = None
        if rewritten and rtk_path:
            # make rg.exe (ripgrep) visible to rtk.exe, wherever it was installed
            rtk_dir = str(Path(rtk_path).parent)
            env = {**os.environ,
                   "PATH": rtk_dir + os.pathsep + os.environ.get("PATH", "")}

        try:
            if sys.platform == "win32":
                # PowerShell is the natural shell for Windows desktop automation
                # (LLMs almost always emit PowerShell syntax).
                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace")[:50000],
                "stderr": stderr.decode("utf-8", errors="replace")[:10000],
                "exit_code": proc.returncode,
                "rewritten": rewritten,
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}
