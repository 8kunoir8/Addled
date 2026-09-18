"""
JSON-RPC 2.0 plumbing shared by both MCP transports.

The only subtle part is direction. MCP is bidirectional: the client sends
requests (``tools/list``, ``tools/call``) and the server may also send its own
requests back (``sampling/createMessage``, ``roots/list``). A message with a
``method`` is therefore a request from the peer and must be answered even when it
is one Addled does not implement — a client that silently drops it leaves the
server waiting.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("addled.mcp")

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "Addled"
CLIENT_VERSION = "1.0"

# JSON-RPC 2.0 standard codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class McpError(Exception):
    """A transport or protocol failure, or an error returned by a server."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


class JsonRpcSession:
    """Correlates outgoing requests with incoming responses."""

    def __init__(self):
        self._pending: dict = {}
        self._counter = 0
        self.notifications: list[dict] = []

    # -- outgoing ----------------------------------------------------------

    def create(self, method: str, params: dict | None = None):
        """Build a request envelope and the future that will receive its result."""
        self._counter += 1
        req_id = self._counter
        message = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            message["params"] = params
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        return message, future

    def notification(self, method: str, params: dict | None = None) -> dict:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        return message

    def abandon(self, req_id) -> None:
        """Forget a request that timed out, so a late reply is ignored."""
        self._pending.pop(req_id, None)

    def fail_all(self, error: Exception) -> None:
        """Fail every in-flight request (used when the transport dies)."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -- incoming ----------------------------------------------------------

    def feed(self, message: dict) -> dict | None:
        """Route one incoming message.

        Returns a reply envelope when the peer made a request we do not
        implement (so the server is not left waiting), otherwise ``None``.
        """
        if not isinstance(message, dict):
            return None

        if "method" in message:
            req_id = message.get("id")
            if req_id is None:
                self.notifications.append(message)
                # Keep the buffer bounded; notifications are informational.
                if len(self.notifications) > 50:
                    del self.notifications[:-50]
                return None
            log.debug("MCP server request ignored: %s", message.get("method"))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": METHOD_NOT_FOUND,
                    "message": f"Method not found: {message.get('method')}",
                },
            }

        req_id = message.get("id")
        future = self._pending.pop(req_id, None)
        if future is None or future.done():
            return None

        error = message.get("error")
        if error:
            future.set_exception(McpError(
                int(error.get("code") or INTERNAL_ERROR),
                str(error.get("message") or "server error"),
                error.get("data"),
            ))
        else:
            result = message.get("result")
            future.set_result(result if isinstance(result, dict) else
                              {"value": result})
        return None


def initialize_params() -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
    }


def parse_sse(body: str) -> list[dict]:
    """Extract JSON-RPC messages from a ``text/event-stream`` body."""
    import json
    messages = []
    for block in body.split("\n\n"):
        payload = []
        for line in block.splitlines():
            if line.startswith("data:"):
                payload.append(line[5:].lstrip())
        if not payload:
            continue
        chunk = "\n".join(payload).strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            decoded = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            messages.append(decoded)
        elif isinstance(decoded, list):
            messages.extend(m for m in decoded if isinstance(m, dict))
    return messages
