"""
Tells the dashboard that an action is waiting on the user's decision.

An approved shell command has to be answerable while the chat turn that asked
for it is still open, so the request is announced the moment it is queued. The
dashboard answers with `action.approve` / `action.deny` and the executor wakes
the waiting turn.

Kept in its own module because the executor is imported from many places and
must not depend on the WebSocket server; this is the one-directional edge.

The announcement carries enough for the dashboard to write the card without
asking a second question: `kind` says whether this is a skill, a tool or a
plain action, `name` is what to show, and `grantable` says whether "Always
allow" may even be offered. That last one comes from the policy rather than
from the dashboard, so a destructive command cannot be made permanently
permitted by a caller that ignores the field.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.actions.approval_notice")

# Recent announcements, newest last. The broadcast can land before the
# dashboard has connected, so the dashboard can also read this back.
_pending: list[dict] = []
_MAX_REMEMBERED = 20

def _classify(action_type: str) -> str:
    """`skill` / `tool` / `action` — what the dashboard should call this."""
    name = str(action_type or "")
    try:
        from backend.skills.registry import skill_registry
        skill = skill_registry.get(name)
    except Exception:  # noqa: BLE001
        skill = None
    if skill is not None:
        # MCP tools are registered as skills too; the category is what tells
        # them apart in the card, and the user thinks of them as tools.
        return "tool" if getattr(skill, "category", "") == "mcp" else "skill"
    return "action"

def _grantable(action_type: str) -> bool:
    """May the user make this permanent? The policy decides, not the caller."""
    try:
        from backend.approvals import policy
        if policy.is_protected(action_type):
            return False
        return str(_classify(action_type)) in ("skill", "tool")
    except Exception as e:  # noqa: BLE001
        log.debug("could not decide whether %s is grantable: %s", action_type, e)
        return False

def publish(approval_id: str, action_type: str, params: dict | None = None) -> bool:
    """Announce one queued approval. Never raises, returns whether it went out."""
    params = params or {}
    kind = _classify(action_type)
    grantable = _grantable(action_type)
    record = {
        "approval_id": approval_id,
        "action_type": action_type,
        "kind": kind,
        "name": action_type,
        "grantable": grantable,
        "params": params,
    }
    _pending.append(record)
    del _pending[:-_MAX_REMEMBERED]
    try:
        from backend.ws_server import get_server
        server = get_server()
        if server is None:
            return False
        server.broadcast_nowait("action.approvalRequest", {
            "approval_id": approval_id,
            "action_type": action_type,
            "kind": kind,
            "name": action_type,
            "grantable": grantable,
            "command": str(params.get("command", ""))[:500],
        })
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("approval notice broadcast failed: %s", e)
        return False

def remember(approval_id: str) -> None:
    """Drop one announcement once it has been answered."""
    for index, record in enumerate(_pending):
        if record["approval_id"] == approval_id:
            del _pending[index]
            return

def pending() -> list[dict]:
    """Announcements not yet answered, for a dashboard that arrived late.

    `params` is dropped: the dashboard renders the card from `name`, `kind` and
    `grantable`, and the parameters of a queued command are not something the
    restore path needs to hand back.

    Entries the executor no longer holds are dropped first. A request that timed
    out or was answered is gone from the executor while this module's copy can
    outlive it, and restoring that would put a button on the screen that can
    only ever answer "no such approval".
    """
    live = _live_ids()
    if live is None:
        return [{k: v for k, v in record.items() if k != "params"}
                for record in _pending]
    if live is not _ALL:
        kept = [r for r in _pending if r["approval_id"] in live]
        if len(kept) != len(_pending):
            _pending[:] = kept
    return [{k: v for k, v in record.items() if k != "params"}
            for record in _pending]


# Distinguishes "the executor said nothing is queued" from "the executor could
# not be asked", so a failure to reach it never empties the list.
_ALL = object()

def _live_ids():
    """Ids the executor still holds, or `_ALL` if it cannot be asked."""
    try:
        from backend.actions.executor import executor
        return {a["approval_id"] for a in executor.pending_approvals()}
    except Exception as e:  # noqa: BLE001
        log.debug("could not read the executor's queue: %s", e)
        return _ALL
