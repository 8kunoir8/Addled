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

# Names approved this session. Kept as a set because it is the fast path read on
# every tool call, and because a session approval is deliberately cheaper than a
# standing one: it costs nothing to read and disappears on restart.
_approved: set[str] = set()


def _key(server_id: str, tool: str) -> str:
    return f"{server_id}::{tool}"


def key(server_id: str, tool: str) -> str:
    """The stored spelling of one tool's approval, for callers that need it.

    Public because the dashboard hands the key back when revoking, and the
    value has to match what `is_approved` looks up or the revoke silently does
    nothing.
    """
    return _key(server_id, tool)


def is_approved(server_id: str, tool: str) -> bool:
    """Approved this session, or granted standing permission.

    The persistent half is what makes "Always allow" on a tool card mean
    something after a restart. It is read second because the in-memory set is
    the common case and this runs on every forwarded call.
    """
    key = _key(server_id, tool)
    if key in _approved:
        return True
    try:
        from backend.approvals import policy
        return policy.is_always_allowed(policy.TOOL, key)
    except Exception as e:  # noqa: BLE001
        log.debug("could not read standing MCP approval for %s: %s", key, e)
        return False


def approve(server_id: str, tool: str) -> None:
    """Approve for this session."""
    _approved.add(_key(server_id, tool))


def is_standing(server_id: str, tool: str) -> bool:
    """Granted for good, rather than approved for this session."""
    try:
        from backend.approvals import policy
        return policy.is_always_allowed(policy.TOOL, _key(server_id, tool))
    except Exception as e:  # noqa: BLE001
        log.debug("could not read standing approval for %s::%s: %s",
                  server_id, tool, e)
        return False


def approve_always(server_id: str, tool: str) -> dict:
    """Approve for good, so the prompt does not return after a restart."""
    key = _key(server_id, tool)
    try:
        from backend.approvals import policy
        result = policy.always_allow(policy.TOOL, key)
    except Exception as e:  # noqa: BLE001
        log.warning("could not save standing MCP approval for %s: %s", key, e)
        return {"success": False, "error": str(e)}
    if result.get("success"):
        _approved.add(key)
    return result


def revoke(server_id: str, tool: str | None = None) -> None:
    """Forget approvals for one tool, or every tool on a server.

    Both halves are cleared. Dropping only the session set would leave a
    standing grant in place and the tool would keep running without a prompt —
    which is the opposite of what revoking is for.
    """
    try:
        from backend.approvals import policy
    except Exception as e:  # noqa: BLE001
        log.debug("approval policy unavailable while revoking: %s", e)
        policy = None

    if tool is None:
        prefix = f"{server_id}::"
        for key in [k for k in _approved if k.startswith(prefix)]:
            _approved.discard(key)
        if policy is not None:
            for key in policy.list_allowed()["tools"]:
                if key.startswith(prefix):
                    policy.revoke(policy.TOOL, key)
    else:
        key = _key(server_id, tool)
        _approved.discard(key)
        if policy is not None:
            policy.revoke(policy.TOOL, key)


def approved_list() -> list[str]:
    """Every approved tool, session and standing, without duplicates."""
    standing: list[str] = []
    try:
        from backend.approvals import policy
        standing = policy.list_allowed()["tools"]
    except Exception as e:  # noqa: BLE001
        log.debug("could not read standing approvals: %s", e)
    return sorted(_approved | set(standing))


def standing_list() -> list[str]:
    """Only the approvals that survive a restart, for the MCP cards."""
    try:
        from backend.approvals import policy
        return list(policy.list_allowed()["tools"])
    except Exception as e:  # noqa: BLE001
        log.debug("could not read standing approvals: %s", e)
        return []


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
            "to allow it. If they agree, call this exact tool again with "
            "the same arguments plus confirm=true. Do not call a separate "
            "trust-check or verification tool unless the user specifically "
            "asked for one; the confirm flag is the approval."),
    }
