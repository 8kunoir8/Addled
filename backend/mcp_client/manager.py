"""
MCP server manager: owns the connections, discovers their tools, and exposes
each one to Addled as an ordinary skill.

Registered tools land in the shared ``SkillRegistry`` under the ``mcp`` category
with a prefixed name (``mcp__<server>__<tool>``), which means they show up in
``skills.list``, in every provider's tool payload, and behind the existing
per-skill enable/disable switch without any extra plumbing.

Every failure path is soft: a server that is missing, crashed, slow, or
misconfigured records an error and returns a readable tool failure. Nothing here
may hang the tool loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from backend.config import config
from backend.mcp_client import approval
from backend.mcp_client.protocol import McpError

log = logging.getLogger("addled.mcp")

CONNECT_GRACE = 25.0
MAX_RESULT_CHARS = 8000


def _sanitise(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))[:48]


def skill_name(server_id: str, tool_name: str) -> str:
    return f"mcp__{_sanitise(server_id)}__{_sanitise(tool_name)}"


def _transport_of(spec: dict) -> str:
    declared = str(spec.get("transport") or "").strip().lower()
    if declared in ("stdio", "http"):
        return declared
    if spec.get("url") and not spec.get("command"):
        return "http"
    return "stdio"


def _normalise(result: dict, tool_name: str) -> dict:
    """Turn an MCP ``tools/call`` result into Addled's skill-result shape."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "image":
            parts.append("[image content returned by the tool]")
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text")
                             or resource.get("uri")
                             or "[resource content]"))
        else:
            try:
                parts.append(json.dumps(item)[:500])
            except (TypeError, ValueError):
                parts.append(f"[{kind or 'content'}]")
    text = "\n".join(p for p in parts if p).strip()
    truncated = len(text) > MAX_RESULT_CHARS
    body = text[:MAX_RESULT_CHARS]

    base = {"tool": tool_name}
    structured = result.get("structuredContent")
    if structured is not None:
        base["structured"] = structured

    if result.get("isError"):
        return {"success": False,
                "error": body or f"'{tool_name}' reported an error",
                **base}
    return {"success": True, "result": body, "text": body,
            "truncated": truncated, **base}


class McpServerState:
    """Runtime state for one configured server."""

    def __init__(self, spec: dict):
        self.spec = spec or {}
        self.id = str(spec.get("id") or "")
        self.client = None
        self.tools: list[dict] = []
        self.registered: list[str] = []
        self.state = "disconnected"
        self.last_error: str | None = None
        # When a tool on this server last ran, for the automatic switch-off.
        self.last_used: float = 0.0

    @property
    def server_info(self) -> dict:
        return getattr(self.client, "server_info", {}) if self.client else {}

    @property
    def protocol_version(self) -> str:
        return getattr(self.client, "protocol_version", "") if self.client else ""


class McpManager:
    def __init__(self):
        self._servers: dict[str, McpServerState] = {}
        self._lock = asyncio.Lock()
        self._started = False

    # -- config -----------------------------------------------------------

    def enabled(self) -> bool:
        return bool(config.get("mcp", "enabled", default=True))

    def _config_servers(self) -> list[dict]:
        raw = config.get("mcp", "servers", default=[]) or []
        return [s for s in raw if isinstance(s, dict) and s.get("id")]

    def _spec(self, server_id: str) -> dict | None:
        for spec in self._config_servers():
            if str(spec.get("id")) == server_id:
                return spec
        return None

    def save_servers(self, servers: list[dict]) -> None:
        config.set("mcp", "servers", value=servers)

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> dict:
        """Connect the enabled servers at startup.

        The autoconnect setting is about starting the app, not about refusing an
        explicit request — see `reload`.
        """
        if not self.enabled():
            return {"skipped": "mcp is disabled"}
        if bool(config.get("mcp", "autoconnect", default=True)) is False:
            return {"skipped": "autoconnect is off"}
        self._started = True
        return await self._connect_enabled()

    async def _connect_enabled(self) -> dict:
        """Connect every enabled server, whatever the autoconnect preference."""
        if not self.enabled():
            return {"skipped": "mcp is disabled"}
        results = {}
        for spec in self._config_servers():
            if not bool(spec.get("enabled", True)):
                continue
            sid = str(spec.get("id"))
            try:
                results[sid] = await self.connect(sid)
            except Exception as e:  # never let one server stop the others
                log.debug("MCP server '%s' failed to start: %s", sid, e)
                results[sid] = {"success": False, "error": str(e)}
        ready = sum(1 for r in results.values() if r.get("success"))
        if results:
            log.info("MCP: %d/%d server(s) ready", ready, len(results))
        return {"servers": len(results), "ready": ready, "results": results}

    async def stop(self) -> None:
        """Close every connection and remove the tools they contributed."""
        self._started = False
        for sid in list(self._servers):
            try:
                await self.disconnect(sid)
            except Exception as e:
                log.debug("MCP '%s' disconnect failed: %s", sid, e)

    async def reload(self) -> dict:
        """Reconnect everything enabled, whatever the autoconnect preference.

        This is an explicit request, so it must not be gated the way `start()`
        is: with autoconnect off, reload used to reach `start()`, skip, and
        return — so "Reconnect all" did nothing and reported success.
        """
        await self.stop()
        self._started = True
        return await self._connect_enabled()

    async def connect(self, server_id: str) -> dict:
        spec = self._spec(server_id)
        if spec is None:
            return {"success": False,
                    "error": f"unknown MCP server '{server_id}'"}
        if not bool(spec.get("enabled", True)):
            return {"success": False,
                    "error": f"MCP server '{server_id}' is disabled"}
        if not self.enabled():
            return {"success": False, "error": "MCP support is turned off"}

        async with self._lock:
            existing = self._servers.get(server_id)
            if existing is not None and existing.state == "ready":
                return {"success": True, "server": self.server_status(server_id)}

            state = McpServerState(spec)
            self._servers[server_id] = state
            state.state = "connecting"
            try:
                state.client = self._build_client(server_id, spec)
                await asyncio.wait_for(state.client.start(),
                                       timeout=CONNECT_GRACE)
                state.state = "ready"
                state.last_error = None
            except asyncio.TimeoutError:
                state.state = "error"
                state.last_error = (f"handshake timed out after "
                                    f"{CONNECT_GRACE:.0f}s")
            except Exception as e:
                state.state = "error"
                state.last_error = f"{type(e).__name__}: {str(e)[:200]}"

            if state.state != "ready":
                if state.client is not None:
                    # Whatever it printed on the way out is the only real
                    # reason available, so keep it in the error.
                    getter = getattr(state.client, "stderr_tail", None)
                    if callable(getter):
                        try:
                            detail = (getter() or "").strip()
                        except Exception:
                            detail = ""
                        if detail:
                            state.last_error = (
                                f"{state.last_error}\n{detail[-400:]}")
                    try:
                        await state.client.close()
                    except Exception:
                        pass
                state.client = None
                self._register_tools(state)
                log.debug("MCP '%s' unavailable: %s", server_id,
                          state.last_error)
                return {"success": False, "error": state.last_error,
                        "server": self.server_status(server_id)}

            try:
                await self.discover(server_id)
            except Exception as e:
                # Connected but tool listing failed: keep the connection and
                # report it, since tools/list can succeed on a retry.
                state.last_error = f"tool discovery failed: {str(e)[:200]}"
            state.last_used = time.time()
            return {"success": True, "server": self.server_status(server_id)}

    async def disconnect(self, server_id: str) -> dict:
        state = self._servers.pop(server_id, None)
        if state is None:
            return {"success": True, "state": "disconnected"}
        client, state.client = state.client, None
        if client is not None:
            try:
                await client.close()
            except Exception as e:
                log.debug("MCP '%s' close failed: %s", server_id, e)
        state.state = "disconnected"
        # Drop the discovered tools first: _register_tools re-registers
        # whatever is still in state.tools, which would otherwise leave a
        # disconnected server's tools callable.
        state.tools = []
        self._register_tools(state)
        return {"success": True, "state": "disconnected"}

    async def ensure_connected(self, server_id: str) -> bool:
        state = self._servers.get(server_id)
        if state is not None and state.state == "ready":
            return True
        result = await self.connect(server_id)
        return bool(result.get("success"))

    async def sweep_idle(self) -> dict:
        """Disconnect servers that were added automatically and then went quiet.

        A server found for one task would otherwise hold its child process and
        keep its tools in every prompt for the rest of the session. Only
        ``auto`` servers are touched, so nothing the user chose by hand is
        switched off behind their back, and the configuration stays either way -
        reconnecting is instant.
        """
        minutes = float(config.get("mcp", "auto_deactivate_minutes",
                                   default=30) or 0)
        if minutes <= 0:
            return {"swept": 0, "skipped": "auto deactivation is off"}
        cutoff = time.time() - minutes * 60
        swept: list[str] = []
        for server_id, state in list(self._servers.items()):
            if not bool(state.spec.get("auto")) or state.state != "ready":
                continue
            if state.last_used and state.last_used > cutoff:
                continue
            try:
                await self.disconnect(server_id)
                swept.append(server_id)
            except Exception as e:  # noqa: BLE001
                log.debug("MCP idle sweep failed for '%s': %s", server_id, e)
        if swept:
            log.info("MCP: deactivated idle server(s): %s", ", ".join(swept))
        return {"swept": len(swept), "servers": swept}

    def _build_client(self, server_id: str, spec: dict):
        transport = _transport_of(spec)
        if transport == "http":
            from backend.mcp_client.http import McpHttpClient
            return McpHttpClient(server_id, spec)
        from backend.mcp_client.stdio import McpStdioClient
        return McpStdioClient(server_id, spec)

    # -- discovery --------------------------------------------------------

    async def discover(self, server_id: str) -> list[dict]:
        """List a server's tools and (re)register them as Addled skills."""
        state = self._servers.get(server_id)
        if state is None or state.client is None:
            raise McpError(-32000, "server is not connected")

        tools: list[dict] = []
        cursor: str | None = None
        # Bounded: a server that returns the same cursor forever must not loop.
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            result = await state.client.request("tools/list", params)
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and tool.get("name"):
                    tools.append(tool)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        state.tools = tools
        self._register_tools(state)
        log.debug("MCP '%s' exposes %d tool(s)", server_id, len(tools))
        return tools

    def _register_tools(self, state: McpServerState) -> None:
        try:
            from backend.skills.registry import skill_registry, SkillDefinition
        except Exception as e:
            log.debug("skill registry unavailable for MCP tools: %s", e)
            return

        for name in state.registered:
            skill_registry.unregister(name)
        state.registered = []

        server_name = str(state.spec.get("name") or state.id)
        trusted = bool(state.spec.get("trusted"))
        for tool in state.tools:
            tool_name = str(tool.get("name") or "").strip()
            if not tool_name:
                continue
            schema = tool.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            if not trusted:
                schema = approval.decorate_schema(schema)

            description = str(tool.get("description") or "").strip()
            prefix = f"[{server_name}] "
            suffix = f" (MCP server '{state.id}'"
            suffix += ")" if trusted else "; needs the user's approval first)"
            text = (prefix + description).strip() if description else \
                f"Call the '{tool_name}' tool on the MCP server '{server_name}'."
            text += suffix

            name = skill_name(state.id, tool_name)
            skill_registry.register(SkillDefinition(
                name,
                text[:900],
                schema,
                self._make_handler(state.id, tool_name),
                category="mcp",
            ))
            state.registered.append(name)

    def _make_handler(self, server_id: str, tool_name: str):
        async def handler(params: dict) -> dict:
            state = self._servers.get(server_id)
            if state is None or state.state != "ready":
                if not await self.ensure_connected(server_id):
                    state = self._servers.get(server_id)
                    detail = (state.last_error if state else None) or \
                        "not connected"
                    return {"success": False,
                            "error": (f"MCP server '{server_id}' is "
                                      f"unavailable: {detail}")}
                state = self._servers.get(server_id)
            if state is None or state.client is None:
                return {"success": False,
                        "error": f"MCP server '{server_id}' is unavailable"}

            # Idle accounting: this is what lets an automatically added server
            # be switched off again once nothing is using it.
            state.last_used = time.time()

            call_params = dict(params or {})
            trusted = bool(state.spec.get("trusted"))
            if not trusted and not approval.is_approved(server_id, tool_name):
                if not bool(call_params.get(approval.CONFIRM_PARAM)):
                    return approval.refusal(
                        server_id,
                        str(state.spec.get("name") or server_id),
                        tool_name)
                approval.approve(server_id, tool_name)
            call_params = approval.strip(call_params)

            try:
                result = await state.client.request(
                    "tools/call",
                    {"name": tool_name, "arguments": call_params})
            except McpError as e:
                state.last_error = str(e)
                return {"success": False,
                        "error": f"MCP tool '{tool_name}' failed: {e}"}
            except Exception as e:
                state.last_error = f"{type(e).__name__}: {str(e)[:200]}"
                return {"success": False,
                        "error": f"MCP tool '{tool_name}' failed: {e}"}
            return _normalise(result, tool_name)

        return handler

    # -- introspection -----------------------------------------------------

    def server_status(self, server_id: str) -> dict:
        spec = self._spec(server_id) or {}
        state = self._servers.get(server_id)
        tools = state.tools if state else []
        return {
            "id": server_id,
            "name": spec.get("name") or server_id,
            "transport": _transport_of(spec),
            "command": spec.get("command"),
            "args": spec.get("args") or [],
            "url": spec.get("url") or "",
            # Key names only: header/env values are routinely secrets.
            "header_keys": sorted((spec.get("headers") or {}).keys()),
            "env_keys": sorted((spec.get("env") or {}).keys()),
            "timeout_s": spec.get("timeout_s", 30.0),
            "enabled": bool(spec.get("enabled", True)),
            "trusted": bool(spec.get("trusted")),
            # True when the market found it rather than the user; the idle
            # sweep only ever disconnects these.
            "auto": bool(spec.get("auto")),
            "market_name": str(spec.get("market_name") or ""),
            "idle_seconds": (int(time.time() - state.last_used)
                             if state and state.last_used else None),
            "state": state.state if state else "disconnected",
            "last_error": state.last_error if state else None,
            "server_info": state.server_info if state else {},
            "protocol_version": state.protocol_version if state else "",
            "tool_count": len(tools),
            "tools": [{"name": t.get("name"),
                       "description": str(t.get("description") or "")[:200]}
                      for t in tools],
            "approved": [t.get("name") for t in tools
                         if approval.is_approved(server_id, str(t.get("name")))],
        }

    def status(self) -> dict:
        specs = self._config_servers()
        servers = [self.server_status(str(s.get("id"))) for s in specs]
        return {
            "enabled": self.enabled(),
            "autoconnect": bool(config.get("mcp", "autoconnect", default=True)),
            "servers": servers,
            "tool_count": sum(s["tool_count"] for s in servers),
            "approved": approval.approved_list(),
        }

    # -- config edits ------------------------------------------------------

    def validate(self, spec: dict, *, server_id: str | None = None):
        """Return ``(spec, error)`` for a server definition from the UI."""
        if not isinstance(spec, dict):
            return None, "server definition must be an object"
        name = str(spec.get("name") or "").strip()
        sid = str(server_id or spec.get("id") or "").strip()
        if not sid:
            sid = _sanitise(name.lower().replace(" ", "-")) or "server"
        sid = _sanitise(sid)
        transport = str(spec.get("transport") or "").strip().lower()
        if transport not in ("stdio", "http"):
            transport = "http" if spec.get("url") and not spec.get("command") \
                else "stdio"

        command = spec.get("command")
        if isinstance(command, str):
            command = command.strip()
        args = spec.get("args") or []
        if isinstance(args, str):
            args = [a for a in args.split() if a]
        url = str(spec.get("url") or "").strip()

        if transport == "stdio":
            if not command and not isinstance(command, list):
                return None, "stdio servers need a 'command'"
            url = ""
        else:
            if not url.lower().startswith(("http://", "https://")):
                return None, "http servers need an http(s) 'url'"
            command, args = None, []

        headers = spec.get("headers") or {}
        env = spec.get("env") or {}
        clean = {
            "id": sid,
            "name": name or sid,
            "transport": transport,
            "command": command,
            "args": [str(a) for a in args] if isinstance(args, list) else [],
            "env": {str(k): str(v) for k, v in env.items()}
            if isinstance(env, dict) else {},
            "cwd": str(spec.get("cwd") or ""),
            "url": url,
            "headers": {str(k): str(v) for k, v in headers.items()}
            if isinstance(headers, dict) else {},
            # Query parameters a spec carries (Smithery's gateway wants its key
            # as `api_key`). Whitelisted like every other field: anything not
            # named here is silently dropped on save.
            "params": {str(k): str(v)
                       for k, v in (spec.get("params") or {}).items()}
            if isinstance(spec.get("params"), dict) else {},
            "enabled": bool(spec.get("enabled", True)),
            "trusted": bool(spec.get("trusted")),
            "auto": bool(spec.get("auto")),
            "market_name": str(spec.get("market_name") or ""),
            "timeout_s": float(spec.get("timeout_s") or 30.0),
        }
        return clean, None

    def add(self, spec: dict) -> dict:
        clean, error = self.validate(spec)
        if error:
            return {"success": False, "error": error}
        servers = self._config_servers()
        if any(str(s.get("id")) == clean["id"] for s in servers):
            return {"success": False,
                    "error": f"an MCP server with id '{clean['id']}' exists"}
        servers.append(clean)
        self.save_servers(servers)
        return {"success": True, "server": clean,
                "status": self.status()}

    def update(self, server_id: str, patch: dict) -> dict:
        servers = self._config_servers()
        for index, existing in enumerate(servers):
            if str(existing.get("id")) != server_id:
                continue
            merged = {**existing, **(patch or {})}
            clean, error = self.validate(merged, server_id=server_id)
            if error:
                return {"success": False, "error": error}
            servers[index] = clean
            self.save_servers(servers)
            # Trust changes what the schema advertises; a rename changes labels;
            # a new timeout must reach the live connection, not just the config.
            state = self._servers.get(server_id)
            if state is not None:
                state.spec = clean
                if state.client is not None:
                    state.client.timeout = clean["timeout_s"]
                self._register_tools(state)
            return {"success": True, "server": clean, "status": self.status()}
        return {"success": False, "error": f"unknown MCP server '{server_id}'"}

    async def remove(self, server_id: str) -> dict:
        await self.disconnect(server_id)
        servers = [s for s in self._config_servers()
                   if str(s.get("id")) != server_id]
        self.save_servers(servers)
        approval.revoke(server_id)
        return {"success": True, "status": self.status()}

    def tools(self, server_id: str) -> dict:
        state = self._servers.get(server_id)
        if state is None:
            return {"success": False, "tools": [],
                    "error": f"MCP server '{server_id}' is not connected"}
        return {"success": True, "server": server_id, "tools": state.tools}


mcp_manager = McpManager()
