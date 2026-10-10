"""Some endpoints accept a `system` message and quietly drop it.

Found on 2026-10-09: 9router (a local OpenAI-compatible proxy, v0.5.95) removes
the `system` role before forwarding upstream. Measured directly — a 200-word
system message added exactly 0 `prompt_tokens` while the same text in a user
turn added 403 — and read in its shipped bundle, where the message splitter
hoists system text into a separate variable and never re-attaches it. Neither
of its token-saver toggles (RTK, Headroom) changes this; both were tested.

The consequence is large and silent: every system-prompt feature is inert
behind such an endpoint. That is the assistant's instructions, the tool
catalogue, and every memory block — which is why, on that configuration, the
model answered without tools and invented dates that were sitting in the
context it never received.

This module does two things:

  * `drops_system_role()` — decides, from evidence, whether an endpoint keeps
    the system role.
  * `fold_into_user()` — moves system text into the first user turn, for the
    endpoints that do not.

Both are provider-scoped and opt-in through a verdict, so a correct provider is
never touched. The detected verdict is remembered per provider: a wrong guess
costs one bad turn, not a bad turn every time.
"""

from __future__ import annotations

import logging
import re
import threading

log = logging.getLogger("addled.providers.system_role")

# The detector's "how much smaller than expected" threshold.
#
# `prompt_tokens` counts the whole request, so this compares the observed count
# against the tokens the system prompt alone should account for. Tokenisation
# differs between endpoints, so the estimate is deliberately generous: a system
# prompt of N characters is at LEAST N/6 tokens even for a token-hungry
# tokeniser, and real ones give roughly N/4. If the total request came in under
# half the system prompt's minimum, the system text is not in there.
_CHARS_PER_TOKEN_CONSERVATIVE = 6.0

# Verdicts, and what they mean:
#   None      — unknown; nothing has been observed yet
#   True      — the endpoint demonstrably keeps the system role
#   False     — the endpoint demonstrably drops it
_KEEP, _DROP = True, False

_verdicts: dict[str, bool] = {}
_lock = threading.Lock()

# Settings is read once and reused; a dict lookup per turn is fine but a file
# read per turn is not, and this is consulted on every provider call.
_memo: dict | None = None


def _provider_key(provider) -> str:
    """Stable identity for a provider, so a verdict survives across instances."""
    pid = getattr(provider, "provider_id", None)
    if pid:
        return str(pid)
    cfg = getattr(provider, "_config", {}) or {}
    return str(cfg.get("id") or cfg.get("base_url")
               or type(provider).__name__)


def _stored() -> dict:
    """Verdicts from previous runs, kept in settings.

    Persisted because the cost of forgetting is paid on the FIRST turn after
    every restart: that turn runs unfolded, so its instructions are dropped and
    (worse) it can overrule nothing — the detection cannot fire again until a
    turn where the system prompt is still present, which is exactly the turn
    that just went wrong. One bad turn per launch, forever, is not acceptable
    when the answer is one line in settings.

    Best-effort: a settings file that cannot be read must not take the provider
    path down with it, so every failure degrades to "unknown".
    """
    if _memo is not None:
        return _memo
    try:
        from backend.config import config
        stored = config.get("providers", "system_role_verdicts", default={})
        return dict(stored) if isinstance(stored, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def verdict_for(provider) -> bool | None:
    """The remembered verdict for this provider, or None if not yet known."""
    key = _provider_key(provider)
    with _lock:
        if key in _verdicts:
            return _verdicts[key]
    stored = _stored()
    if key in stored:
        val = bool(stored[key])
        with _lock:
            _verdicts[key] = val
        return val
    return None


def record(provider, tokens_in: int, messages: list[dict]) -> bool | None:
    """Learn from a completed request whether the system role survived.

    Returns the verdict (True kept, False dropped), or None when this request
    cannot tell — no usage reported, or the system prompt too small for the
    difference to be measurable. Callers should ignore None rather than treat
    it as a negative: most requests are not evidence.

    The signal is the token count, not the model's behaviour. Asking the model
    to echo a canary costs a call and assumes the model obeys; the token count
    is reported by the endpoint itself and cannot be talked out of it.
    """
    if not tokens_in or tokens_in <= 0:
        return None
    system_chars = sum(len(str(m.get("content") or ""))
                       for m in messages if m.get("role") == "system")
    if not system_chars:
        # Nothing to detect: a system-free request tells us nothing.
        return None
    minimum = system_chars / _CHARS_PER_TOKEN_CONSERVATIVE
    # Too small to distinguish from noise in the user turn's own tokens.
    if minimum < 40:
        return None
    kept = tokens_in >= minimum
    key = _provider_key(provider)
    with _lock:
        _verdicts[key] = kept
    _persist(key, kept)
    return kept


def _persist(key: str, kept: bool) -> None:
    """Write one verdict to settings, best-effort.

    Failure is logged and swallowed: a settings write must never be the reason
    a chat turn fails, and the in-memory verdict still works for this run.
    """
    global _memo
    try:
        from backend.config import config
        stored = dict(_stored())
        if stored.get(key) == kept:
            return
        stored[key] = kept
        config.set("providers", "system_role_verdicts", value=stored)
        _memo = stored
        log.info("provider %r %s the system role", key,
                 "keeps" if kept else "DROPS")
    except Exception as e:  # noqa: BLE001
        log.debug("could not persist system-role verdict for %r: %s", key, e)


def drops_system_role(provider) -> bool:
    """True when this provider is known to discard the system role.

    Unknown is treated as "keeps it". Folding when it is not needed is a
    modification of the prompt for no reason; not folding when it was needed is
    the status quo, which a provider that honours system messages handles fine.
    Only a positive detection flips the behaviour.
    """
    return verdict_for(provider) is _DROP


# The user's own words must stay FIRST in the turn.
#
# An earlier version prepended the instructions, which is the obvious placement
# and the wrong one. `check_reply_language.py` asserts that the turn being
# answered *starts with* the user's question, and `_with_directive` appends the
# reply-language line to it. Prepending put a wall of instructions in front of
# the question, so the turn stopped starting with it. The instructions are
# appended instead — after the question, as the last thing the model reads.
#
# Nothing here needs to be first. The catalogue sets the format, the question
# states the task, and the language line pins the reply language; instructions
# that arrive after all three are still instructions.


def _append_instructions(content: str, preamble: str) -> str:
    """`content` with `preamble` appended after it, or the preamble alone."""
    content = str(content or "").strip()
    if not content:
        return preamble
    return content + "\n\n" + preamble


def fold_into_user(messages: list[dict]) -> list[dict]:
    """Move any system message into the user turn, after the user's own words.

    After, not before — see `_append_instructions` for why the position is
    load-bearing. The text stays visibly quoted as instructions rather than
    blending into the request, because a model reading "the user said this"
    about its own instructions is a different failure.

    The system message is removed, not copied — an endpoint that drops it would
    otherwise leave the caller thinking it was delivered, and one that keeps it
    would receive it twice.

    Returns a NEW list. The caller's messages are shared with the tool loop and
    the history writer, and folding in place would quietly rewrite the stored
    conversation.
    """
    system_text = "\n\n".join(
        str(m.get("content") or "") for m in messages
        if m.get("role") == "system" and str(m.get("content") or "").strip())
    kept = [m for m in messages if m.get("role") != "system"]
    if not system_text.strip():
        return kept

    preamble = (
        "The following are your standing instructions and context for this "
        "request. Treat them as authoritative.\n\n" + system_text)
    out: list[dict] = []
    for i, m in enumerate(kept):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list):
                # Multimodal content: keep the parts, add text after them so
                # the user's image and words still come first.
                out.append({**m, "content": [*content,
                                             {"type": "text", "text": preamble}]})
            else:
                out.append({**m,
                            "content": _append_instructions(str(content or ""),
                                                            preamble)})
            out.extend(kept[i + 1:])
            return out
        out.append(m)
    # No user turn at all: the instructions must still be delivered, so a user
    # turn is created rather than dropping them on the floor.
    out.append({"role": "user", "content": preamble})
    return out


def prepare(provider, messages: list[dict], *, enabled: bool = True
            ) -> list[dict]:
    """The one call sites need: fold iff this provider is known to need it.

    Takes the messages the caller built. Returns them unchanged when the
    provider honours system messages, or when `enabled` is False.
    """
    if not enabled or not drops_system_role(provider):
        return messages
    return fold_into_user(messages)


def reset(*, persisted: bool = False) -> None:
    """Forget the in-memory verdicts.

    `persisted=True` also clears what was written to settings, which is what a
    test wants and what a user changing provider endpoint wants. The default
    keeps the durable copy so a restart does not throw away what was learned.
    """
    global _memo
    with _lock:
        _verdicts.clear()
    _memo = None
    if persisted:
        try:
            from backend.config import config
            config.set("providers", "system_role_verdicts", value={})
        except Exception as e:  # noqa: BLE001
            log.debug("could not clear stored verdicts: %s", e)
