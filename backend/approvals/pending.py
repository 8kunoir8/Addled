"""Which conversation raised an approval, so only that one can answer it.

An approval used to be a bare id in a flat queue. That was fine while the only
way to answer was a dashboard card in front of the person who asked. It is not
fine once the same question can be answered from a chat app: a "yes" typed in a
WhatsApp group must not release a destructive command that was asked for in
Telegram, or by a scheduled task, or by a swarm agent. The three are not the
same person and not the same decision.

So every queued approval records its origin, and an answer has to name either
the exact id or the conversation it came from. The dashboard names the id —
it is showing one card — and a bot names its conversation, which is how a typed
"yes" finds the request it belongs to.

Kept separate from the executor because the executor is about running things and
this is about who is allowed to decide, and separate from `approval_notice`
because that one is a courtesy broadcast with a bounded history and this one is
the record of what is still genuinely answerable.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("addled.approvals.pending")

# approval_id -> {source, conversation, action_type, name, grantable,
#                 command, created}
_origins: dict[str, dict] = {}

# Older entries are dropped when the queue grows past this. The executor's own
# queue is the real bound; this only exists so a request nothing answered
# cannot accumulate here forever.
_MAX = 200

def _conversation(value: object) -> str:
    """Normalise a conversation id. Empty means "not answerable by chat"."""
    text = str(value or "").strip()
    return text[:200]

def record(approval_id: str, *, source: object = "", conversation: object = "",
           action_type: str = "", kind: str = "", grantable: bool = False,
           command: str = "") -> None:
    """Note where an approval came from. Never raises."""
    try:
        ident = str(approval_id or "").strip()
        if not ident:
            return
        from backend import chat_sources
        _origins[ident] = {
            "approval_id": ident,
            "source": chat_sources.normalise(source),
            "conversation": _conversation(conversation),
            "action_type": str(action_type or ""),
            "kind": str(kind or ""),
            "grantable": bool(grantable),
            "command": str(command or "")[:500],
            "created": time.time(),
        }
        if len(_origins) > _MAX:
            oldest = sorted(_origins, key=lambda k: _origins[k]["created"])
            for key in oldest[:len(_origins) - _MAX]:
                _origins.pop(key, None)
    except Exception as e:  # noqa: BLE001
        log.debug("could not record the origin of %s: %s", approval_id, e)

def forget(approval_id: str) -> None:
    """Drop one entry once it has been answered or denied."""
    _origins.pop(str(approval_id or "").strip(), None)

def get(approval_id: str) -> dict | None:
    """The recorded origin, or None when nothing is known about this id."""
    entry = _origins.get(str(approval_id or "").strip())
    return dict(entry) if entry else None

def for_conversation(source: object, conversation: object) -> list[dict]:
    """Every approval raised in one conversation, oldest first.

    This is what a bot asks when the user types "yes": the answer belongs to the
    request *this chat* asked for, not to whichever request happens to be
    first in the queue.
    """
    try:
        from backend import chat_sources
        want_source = chat_sources.normalise(source)
    except Exception:  # noqa: BLE001
        want_source = str(source or "")
    want_conv = _conversation(conversation)
    if not want_conv:
        return []
    found = [dict(e) for e in _origins.values()
             if e["source"] == want_source and e["conversation"] == want_conv]
    found.sort(key=lambda e: e["created"])
    return found

def all_pending() -> list[dict]:
    return sorted((dict(e) for e in _origins.values()),
                  key=lambda e: e["created"])

def clear() -> None:
    """Drop everything. Used by the checks."""
    _origins.clear()
