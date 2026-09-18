"""
Approval gate for MCP tool calls.

MCP servers are third-party code with real side effects, so a tool from a server
the user has not marked trusted needs explicit approval. There is no modal
dialog in the chat flow, so the gate works in-band:

* the tool's advertised schema gains a ``confirm`` argument, which makes the
  requirement discoverable to the model rather than hidden;
* the first call is refused with a message the model can relay to the user;
* once the user agrees and the model calls again with ``confirm: true``, the
  server is recorded as approved for the rest of the session.

``confirm`` is stripped before the call is forwarded, so a server never sees an
argument it did not declare.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.mcp")

CONFIRM_PARAM = "confirm"

_approved: set[str] = set()


def _key(server_id: str, tool: str) -> str:
    return f"{server_id}::{tool}"


def is_approved(server_id: str, tool: str) -> bool:
    return _key(server_id, tool) in _approved


def approve(server_id: str, tool: str) -> None:
    _approved.add(_key(server_id, tool))


def revoke(server_id: str, tool: str | None = None) -> None:
    """Forget approvals for one tool, or every tool on a server."""
    if tool is None:
        prefix = f"{server_id}::"
        for key in [k for k in _approved if k.startswith(prefix)]:
            _approved.discard(key)
    else:
        _approved.discard(_key(server_id, tool))


def approved_list() -> list[str]:
    return sorted(_approved)


def decorate_schema(schema: dict) -> dict:
    """Add the ``confirm`` argument to a tool's JSON Schema."""
    out = dict(schema or {})
    if not out.get("type"):
        out["type"] = "object"
    properties = dict(out.get("properties") or {})
    properties[CONFIRM_PARAM] = {
        "type": "boolean",
        "description": (
            "Set to true only after the user has agreed to run this tool. "
            "Addled does not run an unapproved tool from this server."),
    }
    out["properties"] = properties
    return out


def strip(params: dict) -> dict:
    """Remove the approval argument before forwarding to the server."""
    out = dict(params or {})
    out.pop(CONFIRM_PARAM, None)
    return out


def refusal(server_id: str, server_name: str, tool: str) -> dict:
    """The result returned for an unapproved call."""
    return {
        "success": False,
        "requires_approval": True,
        "server": server_id,
        "tool": tool,
        "error": (f"'{tool}' on the MCP server '{server_name}' needs the "
                  "user's approval before it can run."),
        "message": (
            f"'{tool}' from the MCP server '{server_name}' has not been "
            "approved yet, so it did not run. Ask the user whether they want "
            "to allow it. If they agree, call this tool again with "
            "confirm=true; if they decline, do not call it again and tell "
            "them it was skipped."),
    }
