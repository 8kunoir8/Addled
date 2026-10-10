"""Meetings — a recording's transcript, summary, decisions and actions.

A meeting is not a chat turn and not a journal entry. It has a title, a start
and an end, one transcript, and the things that came out of it: what was
decided, who has to do what, and what was left open. This module is the shape
of that record, and the file it lives in.

One file per meeting, under ``memory/meetings/<id>.json``, rather than one big
list. A meeting holds a full transcript — tens of thousands of characters — and
rewriting every meeting in order to add one would make saving O(all meetings)
and risk losing the lot to a partial write.

What is deliberately NOT here: transcription and summarisation. Those are
pipeline steps that know about models; this only knows about meetings.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("addled.meetings")

from backend import app_paths

MEETINGS_DIR = app_paths.subdir("meetings")

# Serialises the read-modify-write in `update`, for the same reason the journal
# has one: a summary finishing while the user renames the meeting would
# otherwise have each write the copy it read.
_LOCK = threading.Lock()

# A transcript is the user's own words and gets kept whole; this only stops a
# runaway file. 2 MB is roughly a 12-hour meeting, so a real one never hits it.
MAX_TRANSCRIPT_CHARS = 2_000_000
MAX_MEETINGS = 500


def _slug(text: str, fallback: str = "meeting") -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:60] or fallback)


def _new_id(title: str, started_at: float | None = None) -> str:
    """A sortable, human-recognisable id: ``2026-10-09-1432-roadmap-sync``.

    Time first so a plain `sort()` is chronological — a directory listing then
    reads like a calendar, which is what someone looking for "Tuesday's
    meeting" actually wants.
    """
    when = time.localtime(started_at or time.time())
    stamp = time.strftime("%Y-%m-%d-%H%M", when)
    slug = _slug(title)
    base = f"{stamp}-{slug}"
    path = MEETINGS_DIR / f"{base}.json"
    n = 2
    while path.exists():
        base = f"{stamp}-{slug}-{n}"
        path = MEETINGS_DIR / f"{base}.json"
        n += 1
    return base


def _path(meeting_id: str) -> Path:
    # Reject anything that could climb out of the directory. The id reaches
    # here from a tool argument, and a value like `../../settings` would
    # otherwise read or write outside the store.
    clean = re.sub(r"[^A-Za-z0-9._-]", "", str(meeting_id or ""))
    if not clean or clean != str(meeting_id):
        raise ValueError(f"invalid meeting id: {meeting_id!r}")
    # The character filter above is not on its own a containment guarantee —
    # `..` passes it (dots are allowed) and produced `...json`, which happens to
    # land inside the directory but is not obviously safe to reason about. So
    # the resolved path is checked against the real directory, which is the
    # claim that actually matters and does not depend on the filter being
    # complete. (Kept here rather than pasted from the register, because the
    # first attempt inserted it into `create()`'s dict literal and broke the
    # file — a reminder that a patched line number is not a claim about content.)
    resolved = (MEETINGS_DIR / f"{clean}.json").resolve()
    try:
        resolved.relative_to(MEETINGS_DIR.resolve())
    except ValueError:
        raise ValueError(f"invalid meeting id: {meeting_id!r}") from None
    return resolved


def _read_raw(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("meeting file unreadable (%s): %s", path.name, e)
        return None


def _write_raw(meeting: dict) -> None:
    """Write via a temp file and replace, so a crash cannot truncate one."""
    mid = meeting["id"]
    path = _path(mid)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meeting, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(path)


def create(title: str = "", *, source: str = "file", started_at: float | None = None,
           ended_at: float | None = None) -> dict:
    """Start a meeting record. Returns the stored dict."""
    started = float(started_at or time.time())
    title = (title or "").strip() or time.strftime("Meeting %Y-%m-%d %H:%M",
                                                   time.localtime(started))
    meeting = {
        "id": _new_id(title, started),
        "title": title,
        "source": source,
        "started_at": started,
        "ended_at": float(ended_at) if ended_at else None,
        "created_at": time.time(),
        "attendees": [],
        "segments": [],
        "transcript": "",
        "language": "",
        "summary": "",
        "decisions": [],
        "actions": [],
        "open_questions": [],
        "summarised_at": None,
    }
    with _LOCK:
        _write_raw(meeting)
        _prune()
    return meeting


def get(meeting_id: str) -> dict | None:
    try:
        return _read_raw(_path(meeting_id))
    except ValueError:
        return None


def update(meeting_id: str, **fields) -> dict | None:
    """Merge fields into a stored meeting.

    Only known keys are written: a typo in a caller would otherwise add a field
    that never gets read and looks like it saved something.
    """
    allowed = {"title", "ended_at", "attendees", "segments", "transcript",
               "language", "summary", "decisions", "actions", "open_questions",
               "summarised_at", "source", "vector_id"}
    unknown = set(fields) - allowed
    if unknown:
        log.warning("ignoring unknown meeting field(s): %s", sorted(unknown))
    with _LOCK:
        meeting = get(meeting_id)
        if meeting is None:
            return None
        for k, v in fields.items():
            if k in allowed:
                meeting[k] = v
        if meeting.get("transcript"):
            meeting["transcript"] = meeting["transcript"][:MAX_TRANSCRIPT_CHARS]
        _write_raw(meeting)
    return meeting


def set_transcript(meeting_id: str, transcript: str,
                   segments: list[dict] | None = None,
                   language: str = "", ended_at: float | None = None) -> dict | None:
    """Attach a finished transcript. The common case, named."""
    return update(meeting_id,
                  transcript=str(transcript or ""),
                  segments=list(segments or []),
                  language=str(language or ""),
                  ended_at=float(ended_at or time.time()))


def set_summary(meeting_id: str, summary: str, *, decisions=None, actions=None,
                open_questions=None) -> dict | None:
    """Attach a summary and whatever was extracted with it.

    A field left as None is *kept*, not cleared: a second summarise pass that
    returns no decisions should not erase decisions found the first time.
    """
    fields: dict = {"summary": str(summary or ""), "summarised_at": time.time()}
    if decisions is not None:
        fields["decisions"] = list(decisions)
    if actions is not None:
        fields["actions"] = list(actions)
    if open_questions is not None:
        fields["open_questions"] = list(open_questions)
    return update(meeting_id, **fields)


# -- semantic recall -----------------------------------------------------------
#
# A meeting summary is exactly the kind of thing "what did we decide about X?"
# needs to reach, and the vector store is how that question gets answered. The
# summary goes in under its own category so a meeting can be found without
# every chat turn having to be searched, and the row id is kept ON the meeting
# so deleting one meeting removes exactly one row. (`session_summary` used
# `delete_category` once and deleted every other summary's row too; not again.)

def _recall_text(meeting: dict) -> str | None:
    """What gets embedded for a meeting, or None when there is nothing to embed.

    The title, the summary, and the decisions — not the transcript. The
    transcript is long enough to swamp the vector and make every meeting look
    similar to every query; the summary is the distilled form of it, which is
    what the question is actually about. Decisions are appended because they
    are short, high-signal, and often phrased differently from the summary's
    prose.

    A title alone is NOT enough. Indexing an unsummarised meeting would put a
    row in the index whose text is "Budget review" — it matches queries about
    the words in its own name and can never be composed into a block (there is
    no summary to show), so it is a row that can be found but not used. That is
    exactly the kind of junk the gating in `relevance` exists to keep out, so
    the summary is the gate here.
    """
    summary = str(meeting.get("summary") or "").strip()
    if not summary:
        return None
    parts = [str(meeting.get("title") or "").strip(), summary]
    parts.extend(str(d).strip() for d in (meeting.get("decisions") or []) if d)
    return "\n".join(p for p in parts if p).strip()


def index(meeting_id: str) -> int | None:
    """Put this meeting's summary into semantic recall. Returns the row id.

    Safe to call twice: the previous row is removed first, so re-summarising a
    meeting replaces its entry instead of leaving two versions of it in the
    index to compete with each other.

    Returns None when there is nothing to index or no embedder — never raises,
    because this runs at the end of a summarise the user already waited for.
    """
    meeting = get(meeting_id)
    if meeting is None:
        return None
    text = _recall_text(meeting)
    if not text:
        return None
    unindex(meeting_id)
    try:
        # `backend.memory.embedding`, NOT `backend.memory.recall`. recall.py
        # defines its own `embed_text` as the LEGACY HASH embedder ("kept for
        # session summaries", it says). Importing it here wrote hash vectors
        # for every meeting while `relevance._meetings_by_index` searched with
        # MiniLM -- both 384-dim, so nothing raised and nothing matched. Every
        # meeting row was silently unsearchable: measured, the stored vector
        # scored cosine 0.109 against a fresh embed of its OWN text.
        # Tag the row with the embedder that wrote it, as `recall.remember`
        # does. Without the tag a later model change is undetectable: every row
        # is 384-dim whatever wrote it, so a mixed store answers confidently
        # and wrongly. The tag is what lets a future migration find the rows
        # that need re-embedding instead of silently ranking nonsense.
        from backend.memory.embedding import embed_text, embedder_id
        from backend.memory.vector_store import vector_store
        row_id = vector_store.add(
            embed_text(text), category="meeting",
            metadata={"text": text, "meeting_id": meeting_id,
                      "title": meeting.get("title") or "",
                      "started_at": meeting.get("started_at"),
                      "embedder": embedder_id()})
    except Exception as e:  # noqa: BLE001
        log.debug("could not index meeting %s: %s", meeting_id, e)
        return None
    if isinstance(row_id, int):
        update(meeting_id, vector_id=row_id)
        return row_id
    return None


def unindex(meeting_id: str) -> bool:
    """Remove this meeting's vector row. True when a row was removed.

    Only the row this meeting recorded is touched. A meeting with no
    `vector_id` (never summarised, or indexed by an older build) removes
    nothing rather than guessing — guessing is what deleted the wrong rows the
    last time.
    """
    meeting = get(meeting_id)
    if meeting is None:
        return False
    vid = meeting.get("vector_id")
    if not isinstance(vid, int):
        return False
    try:
        from backend.memory.vector_store import vector_store
        vector_store.delete(vid)
    except Exception as e:  # noqa: BLE001
        log.debug("could not remove vector row for %s: %s", meeting_id, e)
        return False
    update(meeting_id, vector_id=None)
    return True


def list_meetings(limit: int = 50, *, summaries_only: bool = True) -> list[dict]:
    """Recent meetings, newest first.

    ``summaries_only`` drops the transcript from each entry. It is the whole
    point of the flag: a listing of 50 meetings with their transcripts is
    megabytes of JSON to render a sidebar.
    """
    if not MEETINGS_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(MEETINGS_DIR.glob("*.json"), reverse=True):
        m = _read_raw(path)
        if not m:
            continue
        if summaries_only:
            m = {k: v for k, v in m.items() if k != "transcript"}
            m["segment_count"] = len(m.get("segments") or [])
            m.pop("segments", None)
        out.append(m)
        if len(out) >= limit:
            break
    return out


def delete(meeting_id: str) -> bool:
    try:
        path = _path(meeting_id)
    except ValueError:
        return False
    # Drop its recall row FIRST, while the file is still there to read the id
    # from. Deleting the file and then trying to find the row means the id is
    # gone and the meeting stays answerable long after the user deleted it.
    unindex(meeting_id)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:  # noqa: BLE001
        log.warning("could not delete meeting %s: %s", meeting_id, e)
        return False


def _prune() -> None:
    """Drop the oldest meetings past the cap. Caller holds the lock."""
    if not MEETINGS_DIR.exists():
        return
    files = sorted(MEETINGS_DIR.glob("*.json"))
    for path in files[:-MAX_MEETINGS] if len(files) > MAX_MEETINGS else []:
        try:
            path.unlink()
            log.info("pruned old meeting %s", path.name)
        except OSError:
            pass


def stats() -> dict:
    meetings = list_meetings(limit=MAX_MEETINGS)
    return {
        "count": len(meetings),
        "summarised": sum(1 for m in meetings if m.get("summary")),
        "with_actions": sum(1 for m in meetings if m.get("actions")),
        "latest": meetings[0]["id"] if meetings else None,
    }


def compose_meeting_block(items: list[dict]) -> str | None:
    """Format already-selected meetings for injection, or None when empty.

    The wording lives here rather than in `relevance.py` so there is one copy
    of it, matching how `compose_session_block` and `compose_timeline` work.

    `items` are meeting dicts (as `list_meetings` returns them). The block says
    where the content came from, because a model handed a meeting summary
    without being told what it is will treat other people's words as the user's.
    """
    lines = []
    for m in items:
        summary = (m.get("summary") or "").strip()
        if not summary:
            continue
        title = (m.get("title") or "untitled meeting").strip()
        stamp = ""
        when = m.get("started_at")
        if when:
            import datetime as _dt
            stamp = _dt.date.fromtimestamp(when).isoformat() + " — "
        lines.append(f"- {stamp}{title}: {summary}")
        for d in (m.get("decisions") or [])[:5]:
            lines.append(f"    decision: {d}")
        for a in (m.get("actions") or [])[:5]:
            if isinstance(a, dict):
                what = str(a.get("what") or "").strip()
                who = str(a.get("who") or "").strip()
                if what:
                    lines.append(f"    action: {what}"
                                 + (f" ({who})" if who else ""))
            elif a:
                lines.append(f"    action: {a}")
    if not lines:
        return None
    return ("[Meeting notes] What was said and decided in recent meetings the "
            "user recorded:\n" + "\n".join(lines) + "\n"
            "These are other people's words, not the user's, and they are "
            "history — mention them only when relevant.")
