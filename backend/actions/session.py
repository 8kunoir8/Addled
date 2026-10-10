"""
Interactive sessions — one shell that stays alive between tool calls.

Why this exists
---------------
`run_command` runs `powershell.exe -Command "<one command>"`. That is the right
default: it is stateless, it cannot leak a half-finished state into the next
call, and a command that hangs dies with the call. But it cannot do the things a
developer actually does in a terminal:

    cd src; npm install        # same shell, so cwd persists
    $env:API_KEY = "..."       # an env var the next command can read
    python -i                  # a REPL you keep typing into
    ssh host                   # an open connection you send many lines to

Each of those needs a process that is still running when the next command
arrives. This module owns those processes.

Design
------
- **Named sessions.** `session` is a key like `"main"`. Opening the same name
  twice returns the running one rather than spawning a second shell — a model
  that "opens a shell" on every step would otherwise leave a trail of processes.
- **A real PTY-free pipe shell.** On Windows this is `powershell.exe -NoExit
  -Command -`, which reads commands from stdin and keeps running; on POSIX it is
  `bash -i` with stdin piped. Output is read on a background task so a command
  that prints and keeps running does not block the next call.
- **A sentinel per command.** Each write is followed by an echo of a unique
  marker; the reader collects output until it sees the marker, then returns.
  That is how "the command finished" is known without a timeout guess.
- **Bounded lifetime.** A session idle for `IDLE_TTL_S` is closed, and at most
  `MAX_SESSIONS` live at once, so a forgotten session cannot accumulate.

This is deliberately NOT the default path. It is offered as
`session_open` / `session_send` / `session_close` so the model chooses it for
the work that needs it, and `run_command` stays the safe one-shot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("addled.actions.session")

IDLE_TTL_S = 30 * 60      # a session nobody has touched in 30 min is closed
MAX_SESSIONS = 4          # live shells at once; each is a real process
OUTPUT_CAP = 60_000       # per-command capture ceiling
DEFAULT_WAIT_S = 30.0     # how long one command may take before we stop waiting

# A per-command marker printed after the command so the reader knows it ended.
# It must not collide with real output, so it is a UUID-derived token and
# matched on its own line.
_MARK = "__ADDLED_DONE_{}__"

@dataclass
class Session:
    name: str
    proc: asyncio.subprocess.Process
    shell: str
    cwd: str
    started: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    buffer: str = ""
    reader: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    def info(self) -> dict:
        return {
            "name": self.name,
            "shell": self.shell,
            "cwd": self.cwd,
            "alive": (not self.closed and self.proc.returncode is None),
            "idleSeconds": int(time.time() - self.last_used),
        }

def _console_record(command: str, *, session: str, status: str,
                    stdout: str = "", stderr: str = "",
                    exit_code: int | None = None,
                    duration_ms: int | None = None) -> None:
    """Report a session command to the console. Never raises.

    A session is the same act as `run_command` - a command on the user's
    machine, with input and output - so it belongs on the same panel, in the
    same shape. Reported as kind="session" so the panel can say which of the
    two it was without guessing from the text.
    """
    try:
        from backend.actions import console_log as console
        entry = console.make_entry(
            command=command, kind="session", tool="session_send",
            status=status,
        )
        entry["session"] = session
        entry["stdout"] = stdout or ""
        entry["stderr"] = stderr or ""
        entry["exit_code"] = exit_code
        entry["duration_ms"] = duration_ms
        console.record(entry)
    except Exception as e:  # noqa: BLE001
        log.debug("console session capture failed: %s", e)


class SessionManager:
    """Owns the live shells. One per process; see the module-level `sessions`."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _shell_argv(self) -> tuple[list[str], str]:
        """The command that gives a shell reading from stdin, and its name.

        `-NoExit -Command -` makes PowerShell read commands from stdin and stay
        up after each one, which is exactly the interactive behaviour wanted.
        `-NoProfile` keeps a user's profile from printing banners into the first
        command's output.
        """
        if sys.platform == "win32":
            return (["powershell.exe", "-NoLogo", "-NoProfile",
                     "-ExecutionPolicy", "Bypass", "-Command", "-"],
                    "powershell")
        return (["bash", "-i"], "bash")

    async def open(self, name: str = "main", cwd: str | None = None) -> dict:
        """Start (or return) a session called `name`."""
        name = (name or "main").strip() or "main"
        existing = self._sessions.get(name)
        if existing and not existing.closed and existing.proc.returncode is None:
            existing.last_used = time.time()
            return {"success": True, "session": existing.info(),
                    "reused": True}
        if existing:
            self._sessions.pop(name, None)

        await self._reap()
        if len(self._sessions) >= MAX_SESSIONS:
            return {"success": False,
                    "error": (f"{MAX_SESSIONS} sessions are already open. "
                              "Close one first (session_close).")}

        argv, shell = self._shell_argv()
        workdir = cwd or os.path.expanduser("~")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=workdir,
            )
        except (FileNotFoundError, OSError) as e:
            return {"success": False, "error": f"Could not start {argv[0]}: {e}"}

        sess = Session(name=name, proc=proc, shell=shell, cwd=workdir)
        sess.reader = asyncio.create_task(self._read_loop(sess))
        self._sessions[name] = sess
        log.info("Session '%s' opened (%s, pid %s)", name, shell, proc.pid)
        return {"success": True, "session": sess.info(), "reused": False}

    async def send(self, name: str, command: str,
                   wait_s: float = DEFAULT_WAIT_S) -> dict:
        """Write one command and collect its output up to the end marker."""
        sess = self._sessions.get((name or "main").strip() or "main")
        if sess is None or sess.closed or sess.proc.returncode is not None:
            return {"success": False,
                    "error": (f"No live session '{name}'. Open one with "
                              "session_open first.")}
        if not command.strip():
            return {"success": False, "error": "Empty command."}

        token = uuid.uuid4().hex[:12]
        marker = _MARK.format(token)
        # Print the marker on its own line after the command, capturing the exit
        # code first so the result can carry it. `$?`/`$LASTEXITCODE` differ
        # between cmdlets and native programs, so both are reported.
        if sess.shell == "powershell":
            trailer = (f"; Write-Output \"{marker} $LASTEXITCODE\"")
        else:
            trailer = f"; echo \"{marker} $?\""
        payload = command.rstrip("\n") + "\n" + trailer.lstrip("; ") + "\n"

        _t0 = time.time()
        async with sess.lock:
            sess.last_used = time.time()
            sess.buffer = ""
            try:
                sess.proc.stdin.write(payload.encode("utf-8", errors="replace"))
                await sess.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                sess.closed = True
                return {"success": False,
                        "error": f"The session's shell exited: {e}"}

            deadline = time.time() + max(1.0, min(wait_s, 600.0))
            while time.time() < deadline:
                if marker in sess.buffer:
                    break
                if sess.proc.returncode is not None:
                    break
                await asyncio.sleep(0.05)

        if marker in sess.buffer:
            before, _, after = sess.buffer.partition(marker)
            out = before
            exit_part = after.splitlines()[0].strip() if after else ""
            try:
                exit_code = int(exit_part.split()[0]) if exit_part else 0
            except (ValueError, IndexError):
                exit_code = 0
            stdout = out[:OUTPUT_CAP]
            _console_record(command, session=name,
                            status="ok" if exit_code in (0, None) else "failed",
                            stdout=stdout, exit_code=exit_code,
                            duration_ms=int((time.time() - _t0) * 1000))
            return {
                "success": True,
                "stdout": stdout,
                "stderr": "",
                "exit_code": exit_code,
                "truncated": len(out) > OUTPUT_CAP,
                "session": sess.info(),
            }

        # No marker: either it is slow, or it is an interactive program waiting
        # for input. Say which, so the model does not just retry blindly.
        got = sess.buffer[:OUTPUT_CAP]
        _console_record(command, session=name, status="running", stdout=got,
                        duration_ms=int((time.time() - _t0) * 1000))
        return {
            "success": True,
            "stdout": got,
            "stderr": "",
            "exit_code": None,
            "running": True,
            "session": sess.info(),
            "note": ("The command has not finished — likely waiting for input, "
                     "or long-running. Send more input with session_send, or "
                     "read again with session_read."),
        }

    async def read(self, name: str, wait_s: float = 2.0) -> dict:
        """Return whatever the session has printed since the last read."""
        sess = self._sessions.get((name or "main").strip() or "main")
        if sess is None:
            return {"success": False, "error": f"No session '{name}'."}
        await asyncio.sleep(max(0.0, min(wait_s, 30.0)))
        async with sess.lock:
            out = sess.buffer
            sess.buffer = ""
        if out.strip():
            _console_record("(read)", session=name, status="ok",
                            stdout=out[:OUTPUT_CAP])
        return {"success": True, "stdout": out[:OUTPUT_CAP],
                "session": sess.info()}

    async def close(self, name: str) -> dict:
        """Terminate a session and forget it."""
        key = (name or "main").strip() or "main"
        sess = self._sessions.pop(key, None)
        if sess is None:
            return {"success": False, "error": f"No session '{key}'."}
        await self._terminate(sess)
        return {"success": True, "closed": key}

    def list_sessions(self) -> list[dict]:
        return [s.info() for s in self._sessions.values()]

    async def close_all(self) -> None:
        for name in list(self._sessions):
            await self.close(name)

    # ── internals ─────────────────────────────────────────────────────────

    async def _terminate(self, sess: Session) -> None:
        sess.closed = True
        if sess.reader:
            sess.reader.cancel()
        try:
            if sess.proc.returncode is None:
                sess.proc.terminate()
                try:
                    await asyncio.wait_for(sess.proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    sess.proc.kill()
        except ProcessLookupError:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug("closing session %s: %s", sess.name, e)

    async def _read_loop(self, sess: Session) -> None:
        """Drain the shell's stdout into its buffer, forever.

        Runs as a task so `send` can poll the buffer instead of awaiting the
        read itself — a command that never ends must not wedge the session.
        """
        try:
            while True:
                chunk = await sess.proc.stdout.read(4096)
                if not chunk:
                    break
                sess.buffer += chunk.decode("utf-8", errors="replace")
                if len(sess.buffer) > OUTPUT_CAP * 4:
                    sess.buffer = sess.buffer[-OUTPUT_CAP * 2:]
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("session %s reader stopped: %s", sess.name, e)

    async def _reap(self) -> None:
        """Close sessions that have gone idle or whose shell has exited."""
        now = time.time()
        for name, sess in list(self._sessions.items()):
            if sess.closed or sess.proc.returncode is not None:
                self._sessions.pop(name, None)
                continue
            if now - sess.last_used > IDLE_TTL_S:
                log.info("Session '%s' idle %.0fs — closing", name,
                         now - sess.last_used)
                self._sessions.pop(name, None)
                await self._terminate(sess)

sessions = SessionManager()

def looks_interactive(command: str) -> bool:
    """Whether a one-shot command probably needed a live session.

    Used only to nudge: when `run_command` fails in a way that says "this wanted
    a terminal" (a REPL that exited at once, a `cd` that did not stick), the
    result can suggest `session_open` instead of the model retrying the same
    stateless call.
    """
    if not command:
        return False
    low = command.lower().strip()
    if re.match(r"^(python|node|irb|psql|sqlite3|mysql|redis-cli|ssh|telnet|"
                r"mysql|gdb)\b(?!.*\s-(c|e|m)\b)", low):
        return True
    # `cd` on its own, or a `set`/`$env:` that must outlive the call.
    if low.startswith("cd ") and ";" not in low and "&&" not in low:
        return True
    return bool(re.match(r"^(set|export|\$env:)", low)) and ";" not in low
