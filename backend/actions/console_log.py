"""The console: what Addled ran, what it printed, and what came back.

Why this exists
---------------
Asked to check for whisper, Addled replied "On it - checking now", ran nothing,
and the NEXT turn invented "I asked to run two commands and they're still waiting
on your approval". Nothing had run. Nothing was pending. The truth was only in a
log file, and a lie and a truth are indistinguishable when neither is on screen.

So this is not a log viewer. It records the commands that ran AND the ones that
did not - `awaiting` and `denied` are the states that make that lie visible. A
panel showing only output would have shown nothing at that moment, which is
exactly what the model was counting on.

Design
------
`id` is the spine. One command is ONE row, however many places observe it:

    the gate queues it          -> status "awaiting"   (same id)
    the user approves it        -> the terminal runs it
    the terminal finishes it    -> status "ok"         (same id, updated)

Everything is keyed on that id, so a row is never duplicated and never split in
two. Both the live badge and a replay after a page reload read the same list.

Redaction happens HERE, before the entry is ever broadcast. A secret that never
leaves the process cannot leak through the renderer, a screenshot, or a
devtools panel. `raw(id)` is the one deliberate exception, and it is asked for
by id and by the user, not handed out with the list.

Bounded on purpose: a long session with a chatty command must not grow without
limit, and an unbounded buffer is a slow memory leak that only shows up in the
session that has been open longest.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid

log = logging.getLogger("addled.console")

# How many entries to keep. Generous enough to cover a working session, small
# enough that the replay payload stays sane. A chatty command cannot push this
# over, because an entry is capped too (_STREAM_CAP).
_MAX_ENTRIES = 500

# Per-stream cap on what is KEPT and SENT. The terminal itself truncates at
# 50k/10k for the MODEL's benefit; this is a separate, tighter budget because
# this payload goes over a websocket to a browser and is rendered. Truncation is
# always flagged rather than silent - a cut-off output that reads as complete
# would be worse than no output.
_STREAM_CAP = 8000

# The statuses a row can hold. Ordered roughly by "how much you need to look".
RUNNING, OK, FAILED, TIMEOUT, DENIED, AWAITING = (
    "running", "ok", "failed", "timeout", "denied", "awaiting")

_entries: list[dict] = []
_by_id: dict[str, dict] = {}
_lock = threading.Lock()

# Broadcast is injected by ws_server at import time (it owns the socket). Kept
# as a plain callable so this module has no import cycle with the server, and so
# every call site here can stay best-effort: a console that cannot broadcast
# must never be a reason a command fails.
_broadcast = None


def set_broadcast(fn) -> None:
    """Wire the websocket broadcast. Called once by ws_server."""
    global _broadcast
    _broadcast = fn


# --- redaction --------------------------------------------------------------
#
# Ordered, and each pattern is deliberately specific. A redactor that is too
# greedy mangles ordinary output ("token" appears in lots of innocent text and
# users read it as a broken terminal); one that is too shy prints the secret.
# These target the shapes secrets actually take.
_REDACTIONS: tuple[tuple[re.Pattern, str], ...] = (
    # Authorization headers: keep the scheme, drop the credential.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
     r"\1 [redacted]"),
    # Named secrets in env dumps, config prints, connection strings.
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|pwd|token|"
                r"access[_-]?key|client[_-]?secret|private[_-]?key)\b"
                r"\s*[:=]\s*[\"']?([^\s\"';,]{6,})"),
     r"\1=[redacted]"),
    # Provider-shaped keys, matched by their own prefixes.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "sk-[redacted]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "ghp_[redacted]"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "gho_[redacted]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github_pat_[redacted]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[redacted]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "xox[redacted]"),
    # JWTs - three base64url segments. Specific enough not to catch prose.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "[redacted-jwt]"),
    # A credential embedded in a URL: scheme://user:pass@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+):[^/\s@]+@"),
     r"\1:[redacted]@"),
)


def redact(text: str) -> str:
    """Mask anything that looks like a credential.

    Applied to the command AND to its output: a secret can be typed in (a curl
    with a token) or printed out (an env dump), and both end up on the same
    screen.
    """
    if not text:
        return text
    out = str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


def looks_secret(text: str) -> bool:
    """Whether redaction would change `text`. Used by checks, not by the UI."""
    return bool(text) and redact(text) != text


# --- entry construction -----------------------------------------------------

def new_id() -> str:
    """A fresh entry id.

    Random-suffixed for the same reason approval ids are (see Fix 2): a counter
    alone repeats across restarts, and the dashboard matches by id, so a reused
    id makes a stale row collide with a live one.
    """
    return f"cmd_{uuid.uuid4().hex[:12]}"


def _cap(text: str) -> tuple[str, bool]:
    """Trim to the stream cap, reporting whether anything was dropped."""
    if not text:
        return "", False
    s = str(text)
    if len(s) <= _STREAM_CAP:
        return s, False
    return s[:_STREAM_CAP], True


def make_entry(*, command: str = "", argv: list | None = None,
               kind: str = "shell", tool: str = "run_command",
               cwd: str | None = None, status: str = RUNNING,
               conversation: str | None = None,
               approval_id: str | None = None) -> dict:
    """Build an entry with a fresh id, before redaction and capping.

    `approval_id` is carried separately from `id` so a gated command keeps the
    id the approval queue already knows it by. Without that the row would be
    split: one entry when queued, a different one when it finally ran.

    Text fields are left as given: `record` is the single place that redacts and
    caps, because it is the single place that knows the entry's id - and the id
    is what tells the UI a value was masked.
    """
    return {
        "id": approval_id or new_id(),
        "approval_id": approval_id,
        "kind": kind,
        "tool": tool,
        "command": command or "",
        "argv": [str(a) for a in (argv or [])] or None,
        "cwd": cwd,
        "status": status,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "duration_ms": None,
        "conversation": conversation,
        "ts": time.time(),
    }


# Which fields carry text that must be redacted and capped. Everything else
# (status, ids, exit codes, timings) is machine-made and passes through.
_TEXT_FIELDS = {"command", "stdout", "stderr"}


# Fields whose value redaction actually changed, per entry id. Recorded at WRITE
# time because it cannot be recovered later: redacting already-redacted text is
# a no-op, so the marker is the only evidence a secret was ever there. Without
# this the badge would report a clean command that plainly is not.
_masked: dict[str, set] = {}


def _sanitise(key: str, value, entry_id: str | None = None):
    """Redact and cap one field on its way into the store.

    Applied on EVERY write, not only when an entry is built. Redacting at
    construction and then storing `update(stdout=...)` verbatim left the
    largest, most secret-prone field - a command's output - completely
    unprotected, which is the opposite of what this module is for.

    Records which fields were masked against `entry_id`, so `snapshot` can tell
    the UI that a value was replaced rather than genuinely absent.
    """
    if key not in _TEXT_FIELDS or not isinstance(value, str):
        return value
    masked = redact(value)
    if masked != value and entry_id:
        _masked.setdefault(entry_id, set()).add(key)
    capped, _ = _cap(masked)
    return capped


# --- recording --------------------------------------------------------------

def record(entry: dict, *, announce: bool = True) -> dict:
    """Store an entry (or update the one with this id) and broadcast it.

    An update MERGES rather than replaces: the gate writes the command when it
    queues, and the terminal later writes the output. Both must survive, so a
    later write fills in fields rather than resetting the ones it did not touch.

    Best-effort throughout. Recording is observation; it must never be able to
    break the thing it observes.
    """
    try:
        with _lock:
            existing = _by_id.get(entry.get("id"))
            if existing is not None:
                # Updated IN PLACE, deliberately. `_entries` and `_by_id` hold
                # the SAME dict, and `snapshot` reads `_entries` while an
                # update looks up `_by_id`. Building a merged copy and storing
                # it only in `_by_id` left `_entries` pointing at the original,
                # so a row read `running` forever while the update reported
                # success - the update was real and the list never saw it.
                for k, v in entry.items():
                    # A partial write must not blank a field an earlier
                    # observation already filled: the gate records the command,
                    # the terminal later records output, and neither knows
                    # about the other's fields.
                    if v is None and existing.get(k) not in (None, ""):
                        continue
                    existing[k] = _sanitise(k, v, existing["id"])
                # Computed, not taken from the caller. An entry whose output was
                # trimmed must SAY so, and trusting a caller to pass the flag
                # means one forgetful call site ships a silently cut-off result.
                for field in ("stdout", "stderr"):
                    if len(str(existing.get(field) or "")) >= _STREAM_CAP:
                        existing["truncated"] = True
                existing["ts_updated"] = time.time()
                target = existing
            else:
                created = dict(entry)
                for k in list(created):
                    created[k] = _sanitise(k, created[k], created.get("id"))
                for field in ("stdout", "stderr"):
                    if len(str(created.get(field) or "")) >= _STREAM_CAP:
                        created["truncated"] = True
                _by_id[created["id"]] = created
                _entries.append(created)
                target = created
                if len(_entries) > _MAX_ENTRIES:
                    for old in _entries[:len(_entries) - _MAX_ENTRIES]:
                        _by_id.pop(old.get("id"), None)
                        _masked.pop(old.get("id"), None)
                    del _entries[:len(_entries) - _MAX_ENTRIES]
        if announce:
            _emit(target)
        return target
    except Exception as e:  # noqa: BLE001
        log.debug("could not record console entry: %s", e)
        return entry


def _emit(entry: dict) -> None:
    """Send one entry over the socket. Never raises."""
    fn = _broadcast
    if fn is None:
        return
    try:
        fn("chat.command", dict(entry))
    except Exception as e:  # noqa: BLE001
        log.debug("console broadcast failed: %s", e)


def update(entry_id: str, **fields) -> dict | None:
    """Update an existing entry by id. No-op (None) when it is unknown.

    Unknown is not an error: a command can be recorded by the terminal in a
    build where the gate never saw it, and vice versa.
    """
    with _lock:
        target = _by_id.get(entry_id)
        if target is None:
            return None
        patch = {"id": entry_id}
        patch.update(fields)
    return record(patch)


# --- reading ----------------------------------------------------------------

def snapshot(conversation: str | None = None,
             limit: int | None = None) -> dict:
    """The stored entries, oldest first, for a dashboard that just opened.

    A broadcast reaches only a dashboard that is ALREADY open, so a reload would
    otherwise show an empty console beside a conversation full of work. Same
    recovery `approvals.list` performs, same reason.

    There is deliberately no `raw=True`. Redaction happens at capture, so the
    unredacted text is not retained anywhere to be returned - offering a flag
    that promised it would be a lie in the signature. The UI shows which fields
    were masked (`redacted_fields`) so a reader knows a value was replaced
    rather than lost.
    """
    with _lock:
        items = [dict(e) for e in _entries]
    if conversation:
        items = [e for e in items if e.get("conversation") == conversation]
    if limit is not None and limit > 0:
        items = items[-limit:]
    for e in items:
        masked = redacted_fields(e)
        if masked:
            e["redacted"] = masked
    return {"entries": items, "count": len(items),
            "capped": len(items) >= _MAX_ENTRIES,
            "redaction": "credentials are masked at capture"}


def clear(conversation: str | None = None) -> int:
    """Forget entries (all, or one conversation). Returns how many went."""
    global _entries
    with _lock:
        before = len(_entries)
        if conversation:
            _entries = [e for e in _entries
                        if e.get("conversation") != conversation]
            for eid in [k for k, v in _by_id.items()
                        if v.get("conversation") == conversation]:
                _by_id.pop(eid, None)
                _masked.pop(eid, None)
        else:
            _entries = []
            _by_id.clear()
            _masked.clear()
        return before - len(_entries)


def reset() -> None:
    """Forget everything, including wiring. Used by checks."""
    global _entries, _broadcast
    with _lock:
        _entries = []
        _by_id.clear()
        _masked.clear()
    _broadcast = None


def redacted_fields(entry: dict) -> list[str]:
    """Which fields of this entry had a value masked. For the UI's badge.

    Read from what `_sanitise` recorded at write time, NOT re-derived here.
    Re-deriving was the bug: it tested whether redacting the STORED text would
    change it, and the stored text is already redacted, so the answer was always
    no and every entry claimed to be clean.
    """
    with _lock:
        return sorted(_masked.get(str(entry.get("id") or ""), ()))
