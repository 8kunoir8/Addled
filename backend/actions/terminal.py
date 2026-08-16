"""
Terminal executor — safe command execution with allowlists.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

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
            }
        except asyncio.TimeoutError:
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            return {"success": False, "error": str(e)}
