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

# ---- delegation depth --------------------------------------------------------

# Whether this turn is ALREADY the work of a delegated task, and how far down.
#
# Why this exists: a swarm agent runs its task through `run_chat_pipeline`
# (`SwarmAgent.run_task`), so an agent IS a chat turn with a persona and a tool
# list. That makes `swarm_delegate` recursive by construction — an agent holding
# the skill could hand a task to another agent, which holds the skill too. On a
# cloud model that is merely expensive; on the local model it is worse, because
# flows are serialised to one generation at a time, so nested delegation queues
# behind itself and looks like a hang.
#
# Depth is a ContextVar for the same reason the origin is: it follows the task
# and is correct across `asyncio.create_task`. It is deliberately NOT a module
# global — a global would leak across concurrent turns and one user's delegation
# would refuse another's.
_depth: ContextVar[int] = ContextVar("addled_delegation_depth", default=0)

# One level: a chat or code turn may delegate, but the agent it delegates to may
# not delegate again. Two independent runs are each allowed their own level,
# because depth is per-turn rather than per-process.
MAX_DELEGATION_DEPTH = 1


def delegation_depth() -> int:
    """How many delegations deep this turn is. 0 for an ordinary turn."""
    try:
        return int(_depth.get() or 0)
    except Exception:  # noqa: BLE001
        return 0


def in_delegation() -> bool:
    """True when this turn is itself the work of a delegated task."""
    return delegation_depth() > 0


def enter_delegation() -> None:
    """Mark the current turn as delegated work.

    Called by whatever *starts* the agent's turn, before `run_chat_pipeline`, so
    that every tool call inside it sees the raised depth.
    """
    try:
        _depth.set(delegation_depth() + 1)
    except Exception as e:  # noqa: BLE001
        log.debug("could not raise the delegation depth: %s", e)


def exit_delegation() -> None:
    """Undo `enter_delegation`.

    Paired rather than token-based on purpose. The delegated run happens in an
    `asyncio.create_task`, which COPIES the current context — so a `ContextVar`
    token taken here would not be usable from inside that task, and the raise
    has to happen in the task anyway. A plain decrement is what the pair
    actually needs, and it is clamped at zero so a stray call cannot make the
    depth negative and silently permit a nested delegation.
    """
    try:
        _depth.set(max(0, delegation_depth() - 1))
    except Exception as e:  # noqa: BLE001
        log.debug("could not lower the delegation depth: %s", e)

