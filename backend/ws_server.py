"""
WebSocket JSON-RPC 2.0 server.

Handles all communication between Dashboard/Bots and the Python backend.
Runs on ws://127.0.0.1:9876.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
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
        return {"settings": dict(config._data)}

    async def settings_set(params: dict, ws) -> dict:
        from backend.config import config
        section = params.get("section")
        key = params.get("key")
        value = params.get("value")
        if section and key is not None:
            config.set(section, key, value)
        return {"success": True}

    _server.register("system.status", system_status)
    _server.register("system.getProviders", system_get_providers)
    _server.register("settings.get", settings_get)
    _server.register("settings.set", settings_set)
