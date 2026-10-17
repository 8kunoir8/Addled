"""A halt latch the kill switch actually sets.

The kill hotkey used to call `engine.stop()` and `executor.cancel_current()`.
The first ends the current tick, and the tick loop restarts on the next one; the
second sets a flag that is cleared at the top of every `execute()` and read by
nothing. So the panic key finished the current turn and then the agent carried
on as if nothing had happened — the next `chat.send`, or the next queued goal
step, ran exactly as before.

This is the missing half: a process-wide latch, checked before any tool runs and
before any mutating request is dispatched, which survives until the user says
otherwise. It is persisted to disk because a panic that a restart silently
clears is not a panic button — the failure people act on is the one they press
*while* restarting.

It is deliberately a refusal, not a kill. The user's unsaved conversation is
worth more than the agent, and a paused agent is easier to trust than one that
has to be restored from a crash.
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("addled.halt")

_STATE: dict = {"halted": False, "since": 0.0, "reason": ""}


def state_path():
    """Where the latch is persisted. Read lazily so tests can redirect it."""
    from backend import app_paths
    return app_paths.MEMORY_DIR / "halted.json"


def halt(reason: str = "") -> None:
    """Stop the agent doing anything until `resume()`."""
    _STATE["halted"] = True
    _STATE["since"] = time.time()
    _STATE["reason"] = str(reason or "")
    log.warning("AGENT HALTED: %s", _STATE["reason"] or "(no reason given)")
    _write()


def resume() -> None:
    """Clear the latch. Only ever called for an explicit, local decision."""
    was = _STATE.get("halted")
    _STATE["halted"] = False
    _STATE["since"] = 0.0
    _STATE["reason"] = ""
    if was:
        log.info("Agent resumed")
    _clear()


def is_halted() -> bool:
    return bool(_STATE.get("halted"))


def reason() -> str:
    return str(_STATE.get("reason") or "")


def status() -> dict:
    return {"halted": is_halted(), "since": _STATE.get("since") or 0.0,
            "reason": reason()}


def _write() -> None:
    try:
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status()), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        # The latch stays set in memory even when it cannot be written. A halt
        # that is forgotten on restart is worse than one that is, but a halt
        # that refuses to engage because a disk is full is worse than both.
        log.warning("could not persist the halt latch: %s", e)


def _clear() -> None:
    try:
        state_path().unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        log.debug("could not clear the halt latch file: %s", e)


def _reload() -> None:
    """Read the latch from disk. Called at startup and by the tests."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        _STATE.update({"halted": False, "since": 0.0, "reason": ""})
        return
    except Exception as e:  # noqa: BLE001
        log.warning("could not read the halt latch: %s", e)
        return
    if isinstance(data, dict) and data.get("halted"):
        _STATE["halted"] = True
        _STATE["since"] = float(data.get("since") or 0.0)
        _STATE["reason"] = str(data.get("reason") or "")
        log.warning("The agent was halted before this start and has NOT been "
                    "resumed: %s", _STATE["reason"] or "(no reason given)")
