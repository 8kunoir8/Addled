"""
The rules for the model asking the user a question.

`ask_user` is an escape hatch, not a conversation feature. It exists for the
case a turn genuinely cannot resolve — which of two same-named files, which
account, which of three plausible readings of a one-line request — and where
guessing wrong would waste more time than asking. Everything here exists to
keep it that narrow, because the failure mode of an over-eager `ask_user` is
worse than the problem it solves: a model that finds asking cheap stops making
reasonable assumptions, and the user ends up answering more questions than they
would have typed instructions.

Three rules, each guarding a specific way this goes wrong:

1. **Only ask when someone can answer.** `chat_sources.is_attended` decides.
   A scheduled task, a swarm desk or a voice turn has nobody watching, so a
   question there parks the work forever. Those surfaces are told to decide,
   state their assumption, and carry on — which is what a competent colleague
   does rather than emailing a question into an empty room.

2. **A question expires.** It is answered or it is not, and a card that can
   still be answered an hour later is worse than one that lapsed: the turn it
   belonged to is long gone, and the answer would arrive with no context. An
   expired question is recorded, so the next turn can be told it went
   unanswered rather than the model re-asking the same thing forever.

3. **One question at a time, per conversation.** A confused model can ask the
   same thing repeatedly; two prompts for one decision trains the user to
   dismiss them unread, which is exactly the habit that makes the real one
   useless. A repeat of an open question in the same conversation is refused
   with a reason the model can act on.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("addled.questions.policy")

# How long a question stays answerable. Ten minutes is the working estimate for
# "the user stepped away and came back": long enough to cover a coffee, short
# enough that the answer still lands near the turn that asked. The card shows
# the remaining time, so this is a visible promise rather than a hidden timer.
DEFAULT_TTL_S = 600.0

# A hard ceiling on anything configured, so a bad settings value cannot leave a
# question answerable for a day. Five minutes at the bottom: below that the
# prompt can expire before the user has finished reading it.
MIN_TTL_S = 60.0
MAX_TTL_S = 3600.0

# Bound on remembered questions. The queue is short-lived by construction, but
# an unbounded dict fed by a looping model is a leak in any case.
MAX_OPEN = 20

_lock = threading.Lock()

def ttl_seconds() -> float:
    """The configured lifetime of a question, clamped to a sane range.

    Read from settings so it can be tuned without a build, but clamped here
    rather than trusted: a 0 would make every question expire instantly and a
    huge value would defeat rule 2. Failing closed means falling back to the
    default, not to "no expiry".
    """
    try:
        from backend.config import config
        raw = config.get("chat", "question_ttl_s", default=DEFAULT_TTL_S)
        value = float(raw)
    except Exception as e:  # noqa: BLE001
        log.debug("could not read question_ttl_s (%s); using the default", e)
        return DEFAULT_TTL_S
    if value <= 0:
        return DEFAULT_TTL_S
    return max(MIN_TTL_S, min(MAX_TTL_S, value))

def may_ask(source: object) -> tuple[bool, str]:
    """Return ``(allowed, reason)``. The reason is written for the model.

    The message matters as much as the verdict. A bare refusal gives the model
    nothing to do next, and a model with nothing to do tends to stop and ask
    anyway — in prose this time, which is the behaviour this exists to prevent.
    So the refusal says what to do instead.
    """
    try:
        from backend import chat_sources
    except Exception as e:  # noqa: BLE001
        return False, (f"Clarification is unavailable ({e}). Decide what is "
                       "most likely and say which assumption you made.")
    name = chat_sources.normalise(source)
    if chat_sources.is_attended(name):
        return True, ""
    label = chat_sources.label(name)
    return False, (
        f"You are running from {label}, where nobody is watching to answer. "
        "Do not wait for clarification: choose the most likely reading, make "
        "the change, and state plainly which assumption you made so it can be "
        "corrected."
    )

def describe_ttl() -> str:
    """A phrase for the card: "10 minutes".

    Rounded to whole minutes because the exact second is noise on a prompt, and
    kept in one place so the card and the log agree on the promise being made.
    """
    seconds = ttl_seconds()
    minutes = max(1, int(round(seconds / 60.0)))
    return f"{minutes} minute" + ("" if minutes == 1 else "s")

def is_expired(created: float, now: float | None = None) -> bool:
    """Whether a question raised at ``created`` has lapsed."""
    moment = time.time() if now is None else now
    return (moment - float(created or 0.0)) > ttl_seconds()

def _words(text: object) -> set[str]:
    """Significant words in a question, for comparison rather than display.

    Punctuation is stripped and short filler dropped, so the two spellings a
    looping model produces — "Which file should I edit?" and "Which file should
    I edit? alpha.py or beta.py?" — are recognised as the same question. Held
    below three characters, which removes "the", "a", "or" and "is" while
    keeping "file", "codename" and "server".
    """
    import re as _re
    raw = _re.findall(r"[a-z0-9_.]+", str(text or "").lower())
    return {w for w in raw if len(w) >= 3}

def is_duplicate(existing: list[dict], question: str,
                 options: list[str] | None = None) -> bool:
    """Whether this question repeats one already open in the conversation.

    A *similarity* test, not equality — which is what it has to be, because the
    failure it guards is a model looping on one decision and rephrasing it each
    time. Observed live: asked "Which file should I edit?" with two options, the
    model was re-entered with an answer and asked "Which file should I edit?
    alpha.py or beta.py?" — the same decision, and an equality test let it
    through. The second card replaced the first, the answer was never used, and
    the turn ended on a question the user had already answered.

    Two questions match when they share most of their significant words and
    neither adds a substantively different one. The threshold is deliberately
    forgiving in the direction that refuses: refusing a genuine second question
    costs the model one round of "decide and state your assumption", while
    letting a loop through costs the user a card they cannot usefully answer.
    """
    def norm(text: object) -> str:
        return " ".join(str(text or "").lower().split())

    wanted = norm(question)
    if not wanted:
        return False
    want_words = _words(question)
    want_opts = {norm(o) for o in (options or []) if str(o).strip()}
    for entry in existing or []:
        have_words = _words(entry.get("question"))
        if not have_words or not want_words:
            continue
        # One wording contained in the other: the model extended or trimmed its
        # own sentence without changing the decision.
        contained = (want_words <= have_words) or (have_words <= want_words)
        shared = len(want_words & have_words)
        overlap = shared / max(1, min(len(want_words), len(have_words)))
        if not contained and overlap < 0.7:
            continue
        # When both carry choices, the choices are part of the question: the
        # same words offering different options is a different decision.
        have_opts = {norm(o) for o in (entry.get("options") or [])
                     if str(o).strip()}
        if want_opts and have_opts and want_opts != have_opts:
            continue
        return True
    return False
