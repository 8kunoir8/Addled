"""
Questions waiting on the user, and where each one came from.

The shape follows `backend/approvals/pending.py` deliberately, because the two
problems are the same problem: something raised inside a turn needs an answer
from outside it, possibly from a different surface, possibly later. The lessons
already paid for there are reused rather than rediscovered:

* **Origin is recorded.** A question asked in one conversation must be answered
  by that conversation. Without this, an answer typed in a Telegram group would
  settle a question raised on the dashboard, and two chats running at once
  would answer each other's prompts.
* **The list survives a late dashboard.** The broadcast can land before the
  dashboard has connected, so the queue is readable and `publish` is not the
  only way to learn about a question.
* **Entries the owner no longer holds are dropped.** A question that expired is
  gone from the live set while this module's copy can outlive it; restoring
  that would put a card on screen whose only possible answer is "too late".

What differs from approvals is expiry, because a question is answered on a
clock rather than eventually. The queue is also the only place that decides
what "answered" means, so the same question cannot be settled twice by two
surfaces racing.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("addled.questions.pending")

# Conversations are keys here, so they are normalised the same way the
# approvals module does it: a bounded string, never the raw input.
_MAX_CONVERSATION = 200

_lock = threading.RLock()
_open: dict[str, dict] = {}
_counter = 0

def _conversation(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text[:_MAX_CONVERSATION]

def _next_id() -> str:
    global _counter
    _counter += 1
    return f"q_{int(time.time())}_{_counter}"

def _expire_locked(now: float | None = None) -> int:
    """Drop lapsed questions. Caller holds the lock. Returns how many went.

    Expiry is applied on every read rather than by a timer. A background sweep
    would need its own lifecycle and a way to survive a restart; doing it on
    access means an expired question can never be answered, because any path
    that could answer has already looked and found it gone.
    """
    from backend.questions import policy
    moment = time.time() if now is None else now
    dead = [qid for qid, entry in _open.items()
            if policy.is_expired(entry.get("created", 0.0), moment)]
    for qid in dead:
        entry = _open.pop(qid, None)
        if entry:
            # Remembered so the next turn can be told it went unanswered. A
            # question that silently vanished would be re-asked verbatim, and
            # the user would have no idea their earlier answer never landed.
            _record_outcome(qid, "expired", entry)
    return len(dead)

# Outcomes of finished questions, newest last, bounded. Read by the pipeline so
# a follow-up turn knows the question it asked was never answered.
_finished: list[dict] = []
_MAX_FINISHED = 40

def _record_outcome(question_id: str, outcome: str, entry: dict) -> None:
    _finished.append({
        "question_id": question_id,
        "outcome": outcome,
        "question": entry.get("question", ""),
        "source": entry.get("source", ""),
        "conversation": entry.get("conversation", ""),
        "answer": entry.get("answer", ""),
        "finished": time.time(),
    })
    del _finished[:-_MAX_FINISHED]

def open_questions(source: object = None, conversation: object = None) -> list[dict]:
    """Questions still answerable, oldest first, optionally for one chat.

    Expiry runs first, so a caller can never see (or answer) a lapsed question.
    """
    from backend import chat_sources
    with _lock:
        _expire_locked()
        wanted_src = chat_sources.normalise(source) if source is not None else None
        want_conv = _conversation(conversation) if conversation is not None else None
        out = []
        for entry in sorted(_open.values(), key=lambda e: e.get("created", 0.0)):
            if wanted_src is not None and entry.get("source") != wanted_src:
                continue
            if want_conv is not None and entry.get("conversation") != want_conv:
                continue
            out.append(dict(entry))
        return out

def get(question_id: str) -> dict | None:
    """One open question, or None if it is unknown or has expired."""
    with _lock:
        _expire_locked()
        entry = _open.get(str(question_id or "").strip())
        return dict(entry) if entry else None

def ask(question: str, *, options: list[str] | None = None,
        source: object = "", conversation: object = "",
        context: str = "") -> dict:
    """Queue a question. Returns the record, or an error explaining the refusal.

    Never raises: every failure is a result the caller can put in front of the
    model, because a raised exception here would surface as "the tool failed"
    and invite the model to invent a cause — the fault the approval path
    already documents at length.
    """
    from backend import chat_sources
    from backend.questions import policy

    text = " ".join(str(question or "").split())
    if not text:
        return {"success": False,
                "error": "A question needs wording. Pass it as 'question'."}
    if len(text) > 600:
        # Long enough for a real question, short enough that the card is a
        # prompt rather than an essay the user has to scroll.
        return {"success": False,
                "error": ("That question is too long to put on a card (600 "
                          "character limit). Ask the shortest version that "
                          "makes the decision clear.")}

    cleaned_options = []
    for option in (options or [])[:6]:
        value = " ".join(str(option or "").split())
        if value and value not in cleaned_options:
            cleaned_options.append(value[:120])

    allowed, reason = policy.may_ask(source)
    if not allowed:
        return {"success": False, "unattended": True, "error": reason}

    origin = chat_sources.normalise(source)
    conv = _conversation(conversation)

    with _lock:
        _expire_locked()
        mine = [e for e in _open.values()
                if e.get("source") == origin and e.get("conversation") == conv]
        if len(mine) >= policy.MAX_OPEN:
            return {"success": False,
                    "error": ("There are already several questions waiting. "
                              "Answer those first, or proceed with the most "
                              "likely reading and state your assumption.")}
        if policy.is_duplicate(mine, text, cleaned_options):
            return {"success": False,
                    "error": ("That question has already been asked and is "
                              "still waiting. Do not ask again — the user has "
                              "seen it. Continue with the most likely reading "
                              "and say which assumption you made.")}
        question_id = _next_id()
        entry = {
            "question_id": question_id,
            "question": text,
            "options": cleaned_options,
            "context": " ".join(str(context or "").split())[:400],
            "source": origin,
            "conversation": conv,
            "created": time.time(),
        }
        _open[question_id] = entry
    return {"success": True, **dict(entry),
            "ttl": policy.describe_ttl()}

def answer(question_id: str, text: str) -> dict:
    """Settle a question. Returns the answer, or an error saying why not.

    The answer is recorded as untrusted user input and is *not* merged into any
    system text by this module. Callers that put it in a prompt must label it;
    see the module docstring in `policy.py` for why.
    """
    ident = str(question_id or "").strip()
    if not ident:
        return {"success": False, "error": "Which question? No id given."}
    reply = str(text or "").strip()
    if not reply:
        return {"success": False,
                "error": "An answer needs something in it, even a word."}
    with _lock:
        _expire_locked()
        entry = _open.get(ident)
        if entry is None:
            # Told apart from "never existed": one is a race the user should
            # understand, the other is a bug.
            return {"success": False, "expired": True,
                    "error": ("That question is no longer open — it expired "
                              "before the answer arrived, or another surface "
                              "already answered it.")}
        settled = dict(entry)
        settled["answer"] = reply[:4000]
        _open.pop(ident, None)
        _record_outcome(ident, "answered", settled)
    return {"success": True, **settled}

def dismiss(question_id: str) -> dict:
    """Give up on a question. Returns whether one was actually dropped.

    Distinct from answering with an empty string, so the transcript can say
    "you dismissed this" rather than showing a blank answer the model would try
    to interpret.
    """
    ident = str(question_id or "").strip()
    with _lock:
        _expire_locked()
        entry = _open.pop(ident, None)
        if entry is None:
            return {"success": False, "expired": True,
                    "error": "That question is not open."}
        _record_outcome(ident, "dismissed", entry)
    return {"success": True, "question_id": ident, "dismissed": True}

def recent_outcomes(source: object = None, conversation: object = None,
                    within_s: float = 900.0) -> list[dict]:
    """Questions that finished recently, for the follow-up turn to be told.

    Scoped to the conversation that asked, so a question that expired in one
    chat is not reported as background noise in another, and bounded by time so
    an old outcome is not replayed into a new request days later.
    """
    from backend import chat_sources
    cutoff = time.time() - max(1.0, float(within_s))
    wanted_src = chat_sources.normalise(source) if source is not None else None
    want_conv = _conversation(conversation) if conversation is not None else None
    with _lock:
        out = []
        for entry in _finished:
            if entry.get("finished", 0.0) < cutoff:
                continue
            if wanted_src is not None and entry.get("source") != wanted_src:
                continue
            if want_conv is not None and entry.get("conversation") != want_conv:
                continue
            out.append(dict(entry))
        return out

def forget_outcomes(source: object = None, conversation: object = None) -> int:
    """Consume the finished list for one conversation.

    Called once the outcome has been reported into a turn, so the same expiry
    is not announced twice. Returns how many were dropped.
    """
    from backend import chat_sources
    wanted_src = chat_sources.normalise(source) if source is not None else None
    want_conv = _conversation(conversation) if conversation is not None else None
    with _lock:
        keep, dropped = [], 0
        for entry in _finished:
            if ((wanted_src is None or entry.get("source") == wanted_src)
                    and (want_conv is None
                         or entry.get("conversation") == want_conv)):
                dropped += 1
            else:
                keep.append(entry)
        _finished[:] = keep
        return dropped

def clear() -> None:
    """Drop everything. For tests, and for a restart that resets state."""
    global _counter
    with _lock:
        _open.clear()
        _finished.clear()
        _counter = 0
