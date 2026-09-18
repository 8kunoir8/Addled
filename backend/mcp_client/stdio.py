"""
stdio transport: launch a server as a child process and talk JSON-RPC on its
stdin/stdout, one message per line.

This is the lifecycle shape already proven by ``backend/local_llm/manager.py``
(spawn, readiness handshake, timeout, reap) with the one difference that matters:
stdin is a pipe, because MCP needs a bidirectional stream. stderr is drained to
the log so a chatty server cannot deadlock on a full pipe buffer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys

from backend.mcp_client.protocol import (
    JsonRpcSession,
    McpError,
    initialize_params,
)

log = logging.getLogger("addled.mcp.stdio")

DEFAULT_TIMEOUT = 30.0
# CREATE_NO_WINDOW: never flash a console window for a background server.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class McpStdioClient:
    """A running stdio MCP server session."""

    def __init__(self, server_id: str, spec: dict,
                 timeout: float = DEFAULT_TIMEOUT):
        self.server_id = server_id
        self.spec = spec or {}
        self.timeout = float(spec.get("timeout_s") or timeout)
        self.session = JsonRpcSession()
        self.server_info: dict = {}
        self.protocol_version: str = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        # Kept so a server that dies during the handshake can say why: its own
        # stderr is the only place that reason exists.
        self._stderr_tail: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _argv(self) -> list[str]:
        command = self.spec.get("command")
        if isinstance(command, list):
            argv = [str(c) for c in command]
        else:
            argv = [str(command or "")]
        argv += [str(a) for a in (self.spec.get("args") or [])]
        return argv

    def _env(self) -> dict:
        env = dict(os.environ)
        extra = self.spec.get("env") or {}
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
        return env

    async def start(self) -> dict:
        argv = self._argv()
        if not argv or not argv[0]:
            raise McpError(-32602, "no command configured for this server")

        # Resolve the launcher against PATH before spawning. On Windows an npm
        # launcher is 'npx.cmd', not an executable, and
        # create_subprocess_exec does not consult PATHEXT - so an npm-backed
        # server listed by the market failed to start with "command not
        # found: npx". shutil.which does the PATHEXT search.
        from shutil import which
        resolved = which(argv[0])
        if resolved:
            argv[0] = resolved

        cwd = self.spec.get("cwd") or None

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._env(),
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError as e:
            raise McpError(-32603, f"command not found: {argv[0]} ({e})")
        except OSError as e:
            raise McpError(-32603, f"could not start '{argv[0]}': {e}")

        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        result = await self.request("initialize", initialize_params())
        self.protocol_version = str(result.get("protocolVersion") or "")
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        await self.notify("notifications/initialized")
        log.info("MCP server '%s' ready: %s %s", self.server_id,
                 self.server_info.get("name", "?"),
                 self.server_info.get("version", ""))
        return result

    async def close(self, grace: float = 3.0) -> None:
        proc = self._proc
        self.session.fail_all(McpError(-32000, "connection closed"))
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, RuntimeError):
            pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=grace)
            except asyncio.TimeoutError:
                log.debug("MCP server '%s' did not exit; killing it",
                          self.server_id)
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=grace)
                except asyncio.TimeoutError:
                    pass
        self._proc = None

    # -- io ----------------------------------------------------------------

    async def _write(self, message: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError(-32000, "server is not running")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise McpError(-32000, f"server closed its input: {e}")

    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    # Some servers print banners on stdout; ignore anything
                    # that is not a JSON-RPC envelope.
                    log.debug("MCP '%s' non-JSON stdout: %s",
                              self.server_id, text[:200])
                    continue
                reply = self.session.feed(message)
                if reply:
                    try:
                        await self._write(reply)
                    except McpError:
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("MCP '%s' reader stopped: %s", self.server_id, e)
        finally:
            self.session.fail_all(
                McpError(-32000, "the server closed the connection"))

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_tail.append(text)
                    del self._stderr_tail[:-20]
                    log.debug("MCP '%s' stderr: %s", self.server_id,
                              text[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def stderr_tail(self, limit: int = 8) -> str:
        """The last lines the server wrote to stderr."""
        return "\n".join(self._stderr_tail[-limit:])

    # -- requests ----------------------------------------------------------

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None) -> dict:
        if not self.alive:
            raise McpError(-32000, "server is not running")
        message, future = self.session.create(method, params)
        await self._write(message)
        limit = self.timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(future, timeout=limit)
        except asyncio.TimeoutError:
            self.session.abandon(message["id"])
            raise McpError(
                -32001,
                f"'{method}' timed out after {limit:.0f}s")
        except asyncio.CancelledError:
            self.session.abandon(message["id"])
            raise

    async def notify(self, method: str, params: dict | None = None) -> None:
        if not self.alive:
            return
        await self._write(self.session.notification(method, params))


def resolve_command(spec: dict) -> str:
    """Best-effort resolution of a bare command name against PATH.

    Used only to give a clearer error before spawning.
    """
    from shutil import which
    command = spec.get("command")
    if isinstance(command, list):
        command = command[0] if command else ""
    return which(str(command)) or "" if command else ""
