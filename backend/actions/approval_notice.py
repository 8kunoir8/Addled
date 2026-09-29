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

def _kind_for(action_type: str) -> str | None:
    """The `kind` the dashboard will send back for this action, or None.

    This is the value that reaches `policy._valid_kind`, so it is computed once
    and used by every grantability answer. Two cases have to be told apart:

    A name that is not a registered skill but IS gated by the destruction gate
    (`close_app`) must still resolve to something the policy accepts, because
    the executor's own grant check reads the `skill` bucket first. Classifying
    it as `action` made it un-grantable in both scopes and left the card with no
    memory control, which is the dead end the session control exists to remove.

    A name nothing recognises at all is different: it is None, so it is not
    advertised as grantable. Returning `skill` for any unknown string would have
    made every typo a permanently permittable name.
    """
    kind = str(_classify(action_type))
    if kind in ("skill", "tool"):
        return kind
    try:
        from backend.safety.destruction_gate import DESTRUCTIVE_ACTIONS
        if str(action_type) in DESTRUCTIVE_ACTIONS:
            return "skill"
    except Exception as e:  # noqa: BLE001
        log.debug("could not read the gate's actions: %s", e)
    return None

def _grantable(action_type: str) -> bool:
    """May the user make this permanent? The policy decides, not the caller."""
    try:
        from backend.approvals import policy
        if policy.is_protected(action_type):
            return False
        return policy._valid_kind(_kind_for(action_type)) is not None
    except Exception as e:  # noqa: BLE001
        log.debug("could not decide whether %s is grantable: %s", action_type, e)
        return False

def _session_grantable(action_type: str) -> bool:
    """May the user allow this until restart?

    True for everything that can ask, including the actions a permanent grant
    refuses — `run_command` is the motivating case, and a session grant is the
    only one it can have. The card uses this to offer the session control where
    a permanent switch would be refused, so the button it shows is one that
    actually works.

    The test is "can the policy store a grant for this", not "did it classify
    as a skill or a tool". Classifying on the kind answered a narrower question
    than it looked: an action the gate really does gate but registers as a bare
    executor handler — `close_app` — classifies as `action`, so the card offered
    neither switch and the user's only answer was "Allow once" and Deny, which
    is exactly the dead end the session control exists to remove.

    The policy is the authority on what it will accept, so it is asked rather
    than predicted. `_kind_for` resolves the same name the dashboard will send,
    so the button cannot be shown for an answer the backend would reject.
    """
    try:
        from backend.approvals import policy
        return policy._valid_kind(_kind_for(action_type)) is not None
    except Exception as e:  # noqa: BLE001
        log.debug("could not decide whether %s is session-grantable: %s",
                  action_type, e)
        return False

def publish(approval_id: str, action_type: str, params: dict | None = None) -> bool:
    """Announce one queued approval. Never raises, returns whether it went out.

    The `kind` sent here is the one resolved by `_kind_for`, not the raw
    classification. The dashboard echoes this value straight back as the `kind`
    of its answer, so sending `action` for a gated handler like `close_app` meant
    the answer arrived under a kind `policy._valid_kind` rejects and the grant
    was refused — the button would have failed had the card offered it at all.
    """
    params = params or {}
    kind = _kind_for(action_type)
    grantable = _grantable(action_type)
    session_grantable = _session_grantable(action_type)
    record = {
        "approval_id": approval_id,
        "action_type": action_type,
        "kind": kind,
        "name": action_type,
        "grantable": grantable,
        "session_grantable": session_grantable,
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
            # Sent, not left to the dashboard's default. The card treats a
            # missing field as "offer the session button", so omitting it
            # promised a control the backend would have refused.
            "session_grantable": session_grantable,
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
