"""Which conversation the current turn belongs to.

A tool call arrives at the approval gate carrying only its *own* arguments —
`{"path": "a.txt"}` — with no trace of who asked for it. But an approval has to
be answerable by the chat that raised it, and the chat is a property of the
turn, not of the tool. Passing it down as an argument would mean every skill
signature and every handler in between growing a parameter none of them care
about, so it travels beside the call instead.

A `ContextVar` is the right shape for that: it follows the task, it is already
correct across the `asyncio.create_task` each request runs in, and a turn that
never sets it simply has no origin — which reads as "not answerable from a
chat", the safe default rather than a guess.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

log = logging.getLogger("addled.chat_context")

# {source, conversation}. Empty when nothing set it, so a caller from a script
# or a check has no origin rather than an invented one.
_turn: ContextVar[dict] = ContextVar("addled_turn_origin", default={})

def set_origin(source: object = "", conversation: object = "") -> None:
    """Mark this turn as coming from a particular conversation."""
    try:
        from backend import chat_sources
        _turn.set({
            "source": chat_sources.normalise(source),
            "conversation": str(conversation or "").strip()[:200],
        })
    except Exception as e:  # noqa: BLE001
        log.debug("could not record the turn origin: %s", e)

def origin() -> dict:
    """The turn's origin, or an empty dict when there is none."""
    try:
        return dict(_turn.get() or {})
    except Exception:  # noqa: BLE001
        return {}

def source() -> str:
    return str(origin().get("source") or "")

def conversation() -> str:
    return str(origin().get("conversation") or "")
