"""
Terminal executor — safe command execution with allowlists.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

log = logging.getLogger("addled.terminal")

# Commands that are always safe to run
SAFE_COMMANDS = {
    "dir", "ls", "echo", "type", "cat", "find", "grep", "where", "which",
    "cd", "pwd", "date", "time", "whoami", "hostname", "ipconfig", "ping",
    "nslookup", "netstat", "tasklist", "systeminfo", "ver",
    "git status", "git log", "git diff", "git branch",
    "python --version", "node --version", "npm list", "pip list",
}

# Commands that require user approval
APPROVAL_COMMANDS = {
    "git push", "git commit", "npm install", "pip install",
    "del", "rm", "rmdir", "move", "copy", "xcopy", "robocopy",
    "mkdir", "chmod", "attrib", "schtasks", "sc",
    "net start", "net stop",
}


class TerminalExecutor:
    """Execute shell commands safely."""

    async def execute(self, command: str, cwd: str | None = None, timeout: int = 30) -> dict:
        """Execute a shell command and return stdout/stderr."""
        if not command.strip():
            return {"success": False, "error": "Empty command"}

        # Check against approval list
        cmd_lower = command.lower().strip()
        requires_approval = any(cmd_lower.startswith(c) for c in APPROVAL_COMMANDS)
        is_safe = any(cmd_lower.startswith(c) for c in SAFE_COMMANDS)

        if requires_approval and not is_safe:
            return {"success": False, "error": "This command requires approval. Use the dashboard to confirm.",
                    "requires_approval": True}

        try:
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
