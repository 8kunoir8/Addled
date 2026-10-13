"""The trajectory: what the agent DID on a turn, step by step.

Why this exists
---------------
The console answers "what did Addled run?" for shell commands. It cannot answer
"what did Addled do on that turn?" - a turn that searched the web, read two
files and then wrote one has no single command to show, and the console only
ever sees the terminal. The reply text says what the model *claims*, and the
timeline says what the *user* said. Neither says what actually happened between
the two.

This records that middle: one row per tool call, in order, with what it cost and
what came back. It is the difference between "I checked that for you" and
`read_file(config.json, 41ms) -> 200 OK`.

Design
------
A turn is a LIST of steps sharing a `turn_id`. The turn id is minted once, by
the pipeline, and threaded down to the tool loop, so every step of one turn
groups together without the caller having to reassemble anything.

`seq` is per-turn and zero-based: the order the steps ran. The list is already
append-ordered, so `seq` is a convenience for a reader, not the sort key - but
it makes a dropped step visible, which a bare list does not.

Bounded like the console, for the same reason: an unbounded buffer is a slow
memory leak that only shows up in the session that has been open longest. It is
a ring over ROWS, not turns, because a turn can be one step or twelve and the
memory cost tracks the rows.

Redaction is REUSED from `console_log`, not re-implemented. Two secret-scrubbers
would drift, and the one that drifted would be the one that leaked. Tool
arguments are exactly where a key gets typed.

This is a record, not a claim. An estimate is marked as one, a failure is stored
as a failure, and a step that never ran is absent rather than implied - the same
rule the console learned the hard way.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from backend.actions.console_log import redact

log = logging.getLogger("addled.trajectory")

# Rows, not turns. A turn costs one to twelve rows, and the payload is rendered
# in a browser, so the budget that matters is the row count.
_MAX_ENTRIES = 500

# Per-field cap on what is kept and sent. A tool result can be a whole file;
# this payload goes over a websocket and is rendered, so it is trimmed - and
# always flagged when it is, because a cut-off result that reads as complete is
# worse than no result at all.
_FIELD_CAP = 8000

_entries: list[dict] = []
_by_id: dict[str, dict] = {}
_lock = threading.Lock()

# Injected by ws_server at import time, exactly as the console does, so this
# module has no import cycle with the server and a failure to broadcast can
# never break a turn.
_broadcast = None


def set_broadcast(fn) -> None:
    """Wire the websocket broadcast. Called once by ws_server."""
    global _broadcast
    _broadcast = fn


def new_turn_id() -> str:
    """A fresh turn id.

    Random-suffixed for the same reason the console's entry ids are: a counter
    repeats across restarts, and the dashboard matches by id, so a reused id
    makes a stale turn collide with a live one.
    """
    return f"turn_{uuid.uuid4().hex[:12]}"


def _cap(text: str) -> tuple[str, bool]:
    """Trim to the field cap, reporting whether anything was dropped."""
    if not text:
        return "", False
    s = str(text)
    if len(s) <= _FIELD_CAP:
        return s, False
    return s[:_FIELD_CAP], True


def _text(value) -> str:
    """A field's text form, redacted and capped. Non-strings pass through.

    Structured values are rendered rather than dropped: a tool's arguments are
    usually a dict, and a record that said "arguments: <dict>" would be useless
    for the one question this exists to answer.
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value  # type: ignore[return-value]
    if isinstance(value, str):
        text = value
    else:
        try:
            import json
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            text = str(value)
    masked = redact(text)
    capped, _ = _cap(masked)
    return capped


def make_step(*, turn_id: str, tool: str, seq: int = 0,
              arguments=None, conversation: str | None = None) -> dict:
    """Build a step with a fresh id, before the call runs.

    `status` starts `running` so a step that never completes is still visible.
    A step built only on success would be missing exactly the cases worth
    looking at: the crash, the denial, the timeout.
    """
    return {
        "id": f"step_{uuid.uuid4().hex[:12]}",
        "turn_id": turn_id,
        "seq": seq,
        "tool": tool,
        "arguments": _text(arguments) if arguments is not None else None,
        "status": "running",
        "success": None,
        "result": None,
        "error": None,
        "duration_ms": None,
        "forged": False,
        "requires_approval": False,
        "conversation": conversation,
        "ts": time.time(),
    }


def record(step: dict, *, announce: bool = True) -> dict:
    """Store a step and broadcast it. Never raises.

    An update MERGES rather than replaces, and an update to a known id updates
    the SAME row rather than appending a second. This is the console's rule and
    it is load-bearing for the same reason: a step is observed more than once —
    `finish` writes the result and the duration — and two rows for one call
    would show a tool that ran twice, once stuck on "running". `_entries` and
    `_by_id` hold the SAME dict so a reader of either sees the update.

    Best-effort throughout: recording is observation, and observation must never
    be able to break the thing it observes. A turn does not fail because its
    trace did.
    """
    try:
        with _lock:
            existing = _by_id.get(step.get("id"))
            if existing is not None:
                # Merged IN PLACE. A later write fills in fields rather than
                # resetting the ones it does not carry, and it must not blank a
                # field an earlier observation already recorded.
                for key, value in step.items():
                    if value is None and existing.get(key) not in (None, ""):
                        continue
                    existing[key] = _text(value) if key in ("arguments", "result") else value
                for key in ("arguments", "result"):
                    if isinstance(existing.get(key), str) and len(existing[key]) >= _FIELD_CAP:
                        existing["truncated"] = True
                existing["ts_updated"] = time.time()
                target = existing
            else:
                created = dict(step)
                for key, value in list(created.items()):
                    if key in ("arguments", "result"):
                        created[key] = _text(value)
                created["truncated"] = any(
                    isinstance(created.get(k), str) and len(created[k]) >= _FIELD_CAP
                    for k in ("arguments", "result"))
                created.setdefault("ts", time.time())
                _by_id[created["id"]] = created
                _entries.append(created)
                if len(_entries) > _MAX_ENTRIES:
                    for old in _entries[:len(_entries) - _MAX_ENTRIES]:
                        _by_id.pop(old.get("id"), None)
                    del _entries[:len(_entries) - _MAX_ENTRIES]
                target = created
        if announce:
            _emit(target)
        return target
    except Exception as e:  # noqa: BLE001
        log.debug("could not record trajectory step: %s", e)
        return step


def finish(step_id: str, *, success: bool, result=None, error=None,
           duration_ms: int | None = None, forgot: bool = False) -> dict | None:
    """Complete a step started by `make_step`. None when the id is unknown.

    Unknown is not an error - a step can be finished by a path built before the
    store existed, and a record that threw here would turn a finished tool call
    into a failed turn.
    """
    with _lock:
        target = _by_id.get(step_id)
        if target is None:
            return None
        target["status"] = "ok" if success else "failed"
        target["success"] = bool(success)
        if result is not None:
            target["result"] = _text(result)
        if error:
            target["error"] = _text(error)
        if duration_ms is not None:
            target["duration_ms"] = int(duration_ms)
        target["ts_done"] = time.time()
        snapshot = dict(target)
    _emit(snapshot)
    return snapshot


def _emit(step: dict) -> None:
    """Send one step over the socket. Never raises."""
    fn = _broadcast
    if fn is None:
        return
    try:
        fn("chat.trajectory", dict(step))
    except Exception as e:  # noqa: BLE001
        log.debug("trajectory broadcast failed: %s", e)


def snapshot(turn_id: str | None = None,
             conversation: str | None = None,
             limit: int | None = None) -> dict:
    """The stored steps, oldest first, for a dashboard that just opened.

    A broadcast reaches only a dashboard that is ALREADY open, so a reload would
    otherwise show an empty trajectory beside a conversation full of work. Same
    recovery `console.list` performs, same reason.

    Grouped into turns: the reader's unit is a turn, and handing them a flat
    list would make them do the grouping the store already knows.
    """
    with _lock:
        items = [dict(e) for e in _entries]
    if turn_id:
        items = [e for e in items if e.get("turn_id") == turn_id]
    if conversation:
        items = [e for e in items if e.get("conversation") == conversation]
    if limit is not None and limit > 0:
        items = items[-limit:]

    turns: list[dict] = []
    index: dict[str, dict] = {}
    for e in items:
        tid = str(e.get("turn_id") or "unknown")
        group = index.get(tid)
        if group is None:
            group = {"turn_id": tid, "steps": [], "ts": e.get("ts")}
            index[tid] = group
            turns.append(group)
        group["steps"].append(e)
    for group in turns:
        steps = group["steps"]
        group["count"] = len(steps)
        group["duration_ms"] = sum(
            int(s["duration_ms"]) for s in steps
            if isinstance(s.get("duration_ms"), (int, float)))
        group["failed"] = sum(1 for s in steps if s.get("success") is False)

    return {"turns": turns, "entries": items, "count": len(items),
            "capped": len(items) >= _MAX_ENTRIES,
            "redaction": "credentials are masked at capture"}


def clear(conversation: str | None = None) -> int:
    """Forget steps (all, or one conversation). Returns how many went."""
    global _entries
    with _lock:
        before = len(_entries)
        if conversation:
            _entries = [e for e in _entries
                        if e.get("conversation") != conversation]
            for sid in [k for k, v in _by_id.items()
                        if v.get("conversation") == conversation]:
                _by_id.pop(sid, None)
        else:
            _entries = []
            _by_id.clear()
        return before - len(_entries)


def reset() -> None:
    """Forget everything, including wiring. Used by checks."""
    global _entries, _broadcast
    with _lock:
        _entries = []
        _by_id.clear()
    _broadcast = None
