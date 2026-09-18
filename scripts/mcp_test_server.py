"""A tiny MCP stdio server, used to verify Addled's MCP client.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout and implements just enough
of the protocol: ``initialize``, ``notifications/initialized``, ``tools/list``
and ``tools/call``. Nothing may be written to stdout except protocol messages,
so diagnostics go to stderr.

    .\\python-bundle\\python.exe -s .\\scripts\\mcp_test_server.py

Tools it exposes:
  echo(text)   -> the text back
  add(a, b)    -> the sum
  boom()       -> always reports a tool error
  slow(secs)   -> sleeps, for exercising timeouts
"""

from __future__ import annotations

import json
import sys
import time

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to echo."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First addend."},
                "b": {"type": "number", "description": "Second addend."},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "boom",
        "description": "Always fails. Used to check error handling.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow",
        "description": "Sleep for the given number of seconds.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "secs": {"type": "number", "description": "Seconds to sleep."},
            },
        },
    },
]


def write(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def log(text: str) -> None:
    sys.stderr.write(f"[test-server] {text}\n")
    sys.stderr.flush()


def result(req_id, payload: dict) -> None:
    write({"jsonrpc": "2.0", "id": req_id, "result": payload})


def fail(req_id, code: int, message: str) -> None:
    write({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def text_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, arguments: dict) -> dict:
    if name == "echo":
        return text_result(str(arguments.get("text", "")))
    if name == "add":
        try:
            total = float(arguments.get("a", 0)) + float(arguments.get("b", 0))
        except (TypeError, ValueError):
            return text_result("a and b must be numbers", is_error=True)
        return text_result(str(int(total) if total.is_integer() else total))
    if name == "boom":
        return text_result("boom failed on purpose", is_error=True)
    if name == "slow":
        time.sleep(float(arguments.get("secs", 1)))
        return text_result("slept")
    return text_result(f"unknown tool '{name}'", is_error=True)


def main() -> None:
    log(f"ready on pid {__import__('os').getpid()}")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "addled-test-server", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            log("initialized")
        elif method == "tools/list":
            result(req_id, {"tools": TOOLS})
        elif method == "tools/call":
            result(req_id, call_tool(str(params.get("name") or ""),
                                     params.get("arguments") or {}))
        elif req_id is not None:
            fail(req_id, -32601, f"Method not found: {method}")
    log("stdin closed; exiting")


if __name__ == "__main__":
    main()
