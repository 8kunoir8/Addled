"""
WebSocket JSON-RPC 2.0 server.

Handles all communication between Dashboard/Bots and the Python backend.
Runs on ws://127.0.0.1:9876.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets
from websockets.server import WebSocketServerProtocol

log = logging.getLogger("addled.ws")

# ---- message types ----------------------------------------------------------

HandlerFunc = Callable[[dict, WebSocketServerProtocol], Awaitable[dict | None]]


class WSServer:
    """JSON-RPC 2.0 WebSocket server with method dispatch."""

    def __init__(self):
        self._handlers: dict[str, HandlerFunc] = {}
        self._connections: set[WebSocketServerProtocol] = set()
        self._server = None

    def register(self, method: str, handler: HandlerFunc):
        """Register a JSON-RPC method handler."""
        self._handlers[method] = handler

    async def start(self, host: str = "127.0.0.1", port: int = 9876):
        """Start the WebSocket server."""
        self._server = await websockets.serve(
            self._handle_connection, host, port,
            max_size=10 * 1024 * 1024,  # 10MB max message
        )
        log.info("WebSocket server listening on ws://%s:%d", host, port)

    async def stop(self):
        """Stop the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("WebSocket server stopped")

    async def broadcast(self, method: str, params: dict | None = None):
        """Send a notification to all connected clients."""
        msg = json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })
        dead = set()
        for ws in self._connections:
            try:
                await ws.send(msg)
            except websockets.ConnectionClosed:
                dead.add(ws)
        self._connections -= dead

    async def _handle_connection(self, ws: WebSocketServerProtocol):
        """Handle a single WebSocket connection."""
        self._connections.add(ws)
        peer = ws.remote_address
        log.info("Client connected: %s", peer)

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    response = await self._dispatch(msg, ws)
                    if response is not None:
                        await ws.send(json.dumps(response))
                except json.JSONDecodeError:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }))
                except Exception:
                    log.exception("Error handling message from %s", peer)
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": msg.get("id") if isinstance(msg, dict) else None,
                        "error": {"code": -32603, "message": "Internal error"},
                    }))
        except websockets.ConnectionClosed:
            log.info("Client disconnected: %s", peer)
        finally:
            self._connections.discard(ws)

    async def _dispatch(self, msg: dict, ws: WebSocketServerProtocol) -> dict | None:
        """Route a JSON-RPC message to the appropriate handler."""
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Notifications have no id — don't respond
        is_notification = msg_id is None and method != ""

        handler = self._handlers.get(method)
        if handler is None:
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

        try:
            result = await handler(params, ws)
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": result or {},
            }
        except Exception as e:
            log.exception("Handler error for %s", method)
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32000, "message": str(e)},
            }


# ---- singleton ---------------------------------------------------------------

_server = WSServer()


def get_server() -> WSServer:
    return _server


async def start_ws_server(host: str = "127.0.0.1", port: int = 9876):
    """Start the singleton WebSocket server."""
    _register_default_handlers()
    await _server.start(host, port)


def _register_default_handlers():
    """Register built-in JSON-RPC handlers."""

    async def system_status(params: dict, ws) -> dict:
        from backend.config import config
        return {
            "engineState": "running",
            "provider": config.active_provider,
            "agentName": config.agent_name,
            "characterState": "idle",
            "uptime": 0,
        }

    async def system_get_providers(params: dict, ws) -> dict:
        from backend.providers.registry import list_available
        return {"providers": list_available()}

    async def settings_get(params: dict, ws) -> dict:
        from backend.config import config
        section = params.get("section")
        if section:
            return {"settings": {section: config.get(section, default={})}}
        # Ensure config is loaded before accessing private _data
        config._ensure_loaded()
        return {"settings": dict(config._data)}

    async def settings_set(params: dict, ws) -> dict:
        from backend.config import config
        section = params.get("section")
        key = params.get("key")
        value = params.get("value")
        if section:
            if key is not None and key != "":
                config.set(section, key, value=value)
            else:
                # Top-level setting (e.g. agent_name)
                config.set(section, value=value)
        return {"success": True}

    _server.register("system.status", system_status)
    _server.register("system.getProviders", system_get_providers)
    _server.register("settings.get", settings_get)
    _server.register("settings.set", settings_set)

    # ---- Phase 3: Chat with prompt guard + provider integration ---------------

    async def chat_send(params: dict, ws) -> dict:
        message = params.get("message", "")
        if not message:
            return {"response": "I didn't catch that.", "tokens": 0, "conversationId": None}

        # Prompt guard check
        from backend.config import config
        if config.get("safety", "prompt_guard", default=True):
            from backend.safety.prompt_guard import sanitize
            sanitized, blocked = sanitize(message)
            if blocked:
                return {"response": sanitized, "tokens": 0, "conversationId": None}

        try:
            from backend.providers.registry import get_provider
            from backend.memory.chat_history import chat_history
            provider = get_provider()
            sys_prompt = config.get("chat", "system_prompt",
                default="You are Addled, a helpful AI desktop companion.")
            context = chat_history.get_context(max_messages=config.get("chat", "context_messages", default=20))

            messages = [{"role": "system", "content": sys_prompt}] + [
                {"role": m["role"], "content": m["content"]} for m in context
            ] + [{"role": "user", "content": message}]

            result = await provider.chat(messages, max_tokens=config.get("chat", "max_tokens", default=4096),
                                         temperature=config.get("chat", "temperature", default=0.7))
            if result.ok:
                chat_history.add_message("user", message)
                chat_history.add_message("assistant", result.response,
                    tokens={"in": result.tokens_in, "out": result.tokens_out})
                return {"response": result.response,
                        "tokens": result.tokens_in + result.tokens_out,
                        "conversationId": chat_history._data.get("current_conversation")}
            return {"response": f"[Provider: {result.error}] Check API key in Settings → Providers.",
                    "tokens": 0, "conversationId": None}
        except Exception as e:
            return {"response": f"[Not connected: {e}] Configure an AI provider in Settings.", "tokens": 0, "conversationId": None}

    # ---- Phase 3: Action execution --------------------------------------------

    async def action_execute(params: dict, ws) -> dict:
        from backend.actions.executor import executor, ActionRequest
        action_type = params.get("type", "")
        action_params = params.get("params", {})
        if not action_type:
            return {"success": False, "error": "No action type specified"}
        result = await executor.execute(ActionRequest(action_type=action_type, params=action_params))
        return {"success": result.success, "action_type": result.action_type,
                "summary": result.summary, "duration_ms": result.duration_ms,
                "error": result.error, "data": result.data}

    # ---- Phase 3: Voice TTS --------------------------------------------------

    async def voice_speak(params: dict, ws) -> dict:
        from backend.voice.tts import speak
        text = params.get("text", "")
        voice = params.get("voice", "en-US-JennyNeural")
        if not text:
            return {"success": False, "error": "No text to speak"}
        result = await speak(text, voice)
        return result

    # ---- Phase 3: Observer status ---------------------------------------------

    async def observer_status(params: dict, ws) -> dict:
        return {"tier": "light", "active": True, "context": "unknown",
                "message": "Observer running. Full perception in Phase 3."}

    _server.register("chat.send", chat_send)
    _server.register("action.execute", action_execute)
    _server.register("voice.speak", voice_speak)
    _server.register("observer.status", observer_status)

    # ---- Phase 5/6 stubs (code, goals, swarm, browser) ------------------------

    async def code_bind(params: dict, ws) -> dict:
        """Stub: code.bind — full implementation in Phase 5."""
        folder = params.get("folderPath", "")
        if not folder:
            return {"workspaceId": None, "files": [], "error": "No folder path provided"}
        import os
        if not os.path.isdir(folder):
            return {"workspaceId": folder, "files": [], "error": f"Folder not found: {folder}"}
        # Return a basic file listing
        files = []
        try:
            for root, dirs, filenames in os.walk(folder):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules','__pycache__','venv','.git','.next')]
                for f in filenames[:200]:
                    fp = os.path.join(root, f)
                    ext = os.path.splitext(f)[1].lower()
                    lang_map = {'.py':'python','.js':'javascript','.ts':'typescript','.tsx':'typescript','.json':'json','.md':'markdown','.html':'html','.css':'css','.rs':'rust','.go':'go','.java':'java','.cs':'csharp','.rb':'ruby','.php':'php','.sql':'sql','.yaml':'yaml','.yml':'yaml','.sh':'shell'}
                    files.append({"name": f, "path": os.path.relpath(fp, folder), "language": lang_map.get(ext, os.path.splitext(f)[1][1:] or 'text'), "size": os.path.getsize(fp)})
                if len(files) >= 200: break
        except Exception as e:
            return {"workspaceId": folder, "files": [], "error": str(e)}
        return {"workspaceId": folder, "files": files}

    async def code_read(params: dict, ws) -> dict:
        """Stub: code.read — full implementation in Phase 5."""
        wp = params.get("workspaceId", "")
        fp = params.get("filePath", "")
        import os
        full = os.path.join(wp, fp) if wp else fp
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            ext = os.path.splitext(fp)[1].lower()
            lang_map = {'.py':'python','.js':'javascript','.ts':'typescript','.tsx':'typescript','.json':'json','.md':'markdown','.html':'html','.css':'css'}
            return {"content": content, "language": lang_map.get(ext, 'text')}
        except Exception as e:
            return {"content": f"// Error reading file: {e}", "language": "text"}

    async def goal_create(params: dict, ws) -> dict:
        """Stub: goal.create — full implementation in Phase 5."""
        title = params.get("title", "Untitled Goal")
        description = params.get("description", "")
        priority = params.get("priority", "normal")
        import time, uuid
        goal_id = f"goal_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        return {"goalId": goal_id, "plan": {"steps": [
            {"index": 0, "description": f"Analyze: {title}", "status": "pending"},
            {"index": 1, "description": f"Execute: {title}", "status": "pending"},
            {"index": 2, "description": f"Verify: {title}", "status": "pending"},
        ]}}

    async def swarm_spawn(params: dict, ws) -> dict:
        """Stub: swarm.spawn — full implementation in Phase 5."""
        import uuid
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        return {"agentId": agent_id, "status": "spawned",
                "message": f"Agent '{params.get('name','unnamed')}' spawned. Full swarm execution coming in Phase 5."}

    async def browser_navigate(params: dict, ws) -> dict:
        """Stub: browser.navigate — full implementation in Phase 3 (Playwright)."""
        return {"success": True, "url": params.get("url", ""),
                "message": "Browser control requires Playwright. Install with: pip install playwright && playwright install chromium"}

    async def browser_action(params: dict, ws) -> dict:
        """Stub: browser.* actions — full implementation in Phase 3."""
        return {"success": True, "message": "Browser actions coming in Phase 3."}

    async def code_edit(params: dict, ws) -> dict:
        """Stub: code.edit — full implementation in Phase 5."""
        wp = params.get("workspaceId", "")
        instruction = params.get("instruction", "")
        # Return a placeholder diff indicating what would happen
        return {"diffs": [{
            "file": params.get("filePath", "unknown"),
            "instruction": instruction,
            "status": "pending",
            "message": "Code editing engine coming in Phase 5. Instruction saved."
        }]}

    _server.register("code.bind", code_bind)
    _server.register("code.read", code_read)
    _server.register("code.edit", code_edit)
    _server.register("goal.create", goal_create)
    _server.register("swarm.spawn", swarm_spawn)
    _server.register("browser.navigate", browser_navigate)
    _server.register("browser.go_back", browser_action)
    _server.register("browser.go_forward", browser_action)
    _server.register("browser.click", browser_action)
    _server.register("browser.type", browser_action)
    _server.register("browser.screenshot", browser_action)
    _server.register("browser.extract", browser_action)
    _server.register("browser.close", browser_action)
