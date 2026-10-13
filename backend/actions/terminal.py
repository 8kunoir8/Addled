"""
Terminal executor — safe command execution with allowlists.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("addled.terminal")


async def _stop_process_tree(proc, grace: float = 5.0) -> None:
    """Stop a timed-out command, whole.

    On Windows `proc.kill()` is not enough. The command runs through a shell —
    `create_subprocess_shell` starts `cmd.exe`, which starts the real program —
    so killing the shell leaves the program running. Measured here: after
    `proc.kill()`, nine `python.exe` processes were still alive, holding the
    stdout pipe open. That is not only wasted CPU and locked files: a child
    still holding the write end means the reader never sees EOF, so the output
    cannot be collected and the call hangs until its own grace expires.
    `taskkill /F /T` takes the tree, the pipe closes, and the reader finishes.
    That is the whole reason this function is Windows-aware.

    Killing only — the caller owns the reads, which it starts before waiting.
    Reading here would compete with those tasks for the same stream, and the
    loser silently returns nothing.

    Never raises: this runs on the failure path, where a second exception would
    replace a useful timeout message with a confusing one.
    """
    if proc.returncode is None:
        try:
            if sys.platform == "win32":
                # `/T` is the part that matters: without it only the shell dies.
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(proc.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(killer.wait(), timeout=grace)
            else:
                proc.kill()
        except Exception as e:  # noqa: BLE001
            log.debug("could not stop the timed-out process tree: %s", e)
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace)
        except Exception:  # noqa: BLE001
            # Already gone, or refusing to report. Either way the caller's read
            # task decides what we get back.
            pass


async def _task_output(task, grace: float = 5.0) -> str:
    """The decoded text a read task collected, or "" if it cannot be had.

    A read that has not finished is given a short grace rather than dropped: by
    the time this runs the process tree is dead and the pipe closed, so the read
    completes almost immediately. The timeout is the belt to that braces — a
    stream that somehow never ends must not turn a reported failure into a hung
    tool call.
    """
    try:
        chunk = await asyncio.wait_for(task, timeout=grace)
    except Exception:  # noqa: BLE001
        task.cancel()
        return ""
    return (chunk or b"").decode("utf-8", errors="replace")[:50000]



def _console_record(command: str, result: dict, *, argv: list | None = None,
                    cwd: str | None = None, tool: str | None = None,
                    started: float | None = None,
                    conversation: str | None = None) -> None:
    """Report one finished command to the console. Never raises.

    Kept as a module function rather than inlined twice so `execute` and
    `execute_argv` cannot drift in how they report - the two must produce
    identical entry shapes or the console reads differently depending on which
    kind of command ran.
    """
    try:
        from backend.actions import console_log as console
        import time as _t

        ok = bool(result.get("success"))
        if result.get("error") and "timed out" in str(result.get("error", "")).lower():
            status = console.TIMEOUT
        else:
            status = console.OK if ok else console.FAILED

        entry = console.make_entry(
            command=command,
            argv=argv,
            kind="argv" if argv else "shell",
            tool=tool or ("run_command" if not argv else "tool"),
            cwd=cwd,
            status=status,
            conversation=conversation,
        )
        entry["exit_code"] = result.get("exit_code")
        entry["stdout"] = str(result.get("stdout") or "")
        entry["stderr"] = str(result.get("stderr") or "")
        if result.get("error") and not entry["stderr"]:
            entry["stderr"] = str(result.get("error"))
        if started is not None:
            entry["duration_ms"] = int((_t.monotonic() - started) * 1000)
        console.record(entry)
    except Exception as e:  # noqa: BLE001
        log.debug("console capture failed: %s", e)

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

    async def execute(self, command: str, cwd: str | None = None,
                      timeout: int = 30) -> dict:
        """Execute a shell command and return stdout/stderr.

        There is no `allow_dangerous` parameter. One used to sit here, accepted
        and never read, while the executor dutifully passed its
        `_approved_run` flag into it — so the code read as though approving an
        action lifted a guard inside this method, when the only thing that ever
        granted an approved command permission was the executor clearing its
        own gate. Removed rather than honoured: the destruction gate owns this
        decision, and a second, silent switch here would be a second place for
        it to be wrong.
        """
        if not command.strip():
            return {"success": False, "error": "Empty command"}

        # Defensive translation of common bash-isms for Windows PowerShell 5.1
        if sys.platform == "win32":
            command = command.replace(" && ", " ; ")
            command = command.replace("~/", "$HOME/")

        # Timed from here so the console can report a real duration rather than
        # a number invented at display time.
        _t0 = time.monotonic()

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
            # One mechanism, not two. `communicate()` and a direct `read()` on
            # the same stream fight over it, and the loser silently gets
            # nothing - so this reads the pipes itself and never calls
            # `communicate`.
            #
            # The reads are started BEFORE anything is waited on, which is what
            # makes the output survive a timeout: a read begun after the kill
            # races the transport teardown and can find an empty buffer, and
            # `wait_for(proc.communicate())` cannot be used at all, because
            # cancelling it tears down the very pipes it was reading.
            out_task = asyncio.ensure_future(proc.stdout.read())
            err_task = asyncio.ensure_future(proc.stderr.read())
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # The command ran too long. Stop the whole process tree and
                # report what it printed first - see `_stop_process_tree` for
                # why `taskkill /T` rather than `proc.kill()`, and why the
                # partial output is worth recovering.
                timed_out = True
                await _stop_process_tree(proc)
            else:
                timed_out = False

            stdout = await _task_output(out_task)
            stderr = await _task_output(err_task)
            if timed_out:
                out = {
                    "success": False,
                    "error": (f"Command timed out after {timeout}s and was "
                              "stopped."),
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": -1,
                    "timed_out": True,
                    "rewritten": rewritten,
                }
            else:
                out = {
                    "success": proc.returncode == 0,
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": proc.returncode,
                    "rewritten": rewritten,
                }
            _console_record(command, out, cwd=cwd, started=_t0)
            return out
        except Exception as e:
            out = {"success": False, "error": str(e)}
            _console_record(command, out, cwd=cwd, started=_t0)
            return out

    async def execute_argv(self, argv: list[str], cwd: str | None = None,
                           timeout: int = 30) -> dict:
        """Run a program directly with an argument list — NO shell involved.

        `execute()` builds a string and hands it to `powershell -Command`, which
        is right for a command an LLM wrote and wrong for anything carrying a
        caller-supplied argument. Interpolating an argument into that string
        means PowerShell parses it, so a value containing `$(...)`, a backtick,
        or an unbalanced quote runs as code instead of being passed as data.

        This method never involves a shell: the OS receives the argv as separate
        strings, so an argument can only ever be an argument. Anything that
        spawns a program with parameters from the model or the user should use
        it rather than building a command line.
        """
        if not argv or not str(argv[0]).strip():
            return {"success": False, "error": "Empty command"}
        _a0 = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *[str(a) for a in argv],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(),
                                                    timeout=timeout)
            out = {
                "success": proc.returncode == 0,
                "stdout": stdout.decode("utf-8", errors="replace")[:50000],
                "stderr": stderr.decode("utf-8", errors="replace")[:10000],
                "exit_code": proc.returncode,
                "rewritten": False,
            }
            _console_record(" ".join(str(a) for a in argv), out, argv=list(argv),
                            cwd=cwd, started=_a0)
            return out
        except asyncio.TimeoutError:
            out = {"success": False, "error": f"Command timed out after {timeout}s"}
            _console_record(" ".join(str(a) for a in argv), out, argv=list(argv),
                            cwd=cwd, started=_a0)
            return out
        except Exception as e:
            out = {"success": False, "error": str(e)}
            _console_record(" ".join(str(a) for a in argv), out, argv=list(argv),
                            cwd=cwd, started=_a0)
            return out
