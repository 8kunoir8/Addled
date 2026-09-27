"""
Tells the dashboard that an action is waiting on the user's decision.

An approved shell command has to be answerable while the chat turn that asked
for it is still open, so the request is announced the moment it is queued. The
dashboard answers with `action.approve` / `action.deny` and the executor wakes
the waiting turn.

Kept in its own module because the executor is imported from many places and
must not depend on the WebSocket server; this is the one-directional edge.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.actions.approval_notice")

# Recent announcements, newest last. The broadcast can land before the
# dashboard has connected, so the dashboard can also read this back.
_pending: list[dict] = []
_MAX_REMEMBERED = 20

def publish(approval_id: str, action_type: str, params: dict | None = None) -> bool:
    """Announce one queued approval. Never raises, returns whether it went out."""
    params = params or {}
    record = {"approval_id": approval_id, "action_type": action_type,
              "params": params}
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
    """Announcements not yet answered, for a dashboard that arrived late."""
    return list(_pending)
