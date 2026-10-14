"""
Keep a request inside the context window it is going to.

Nothing did this before: the tool catalogue was appended to the user's message
and the whole thing was sent, whatever its size. That works against a 64k-token
cloud model and silently fails against the local one, whose context is 8192 and
whose reply budget was a fixed 4096 — leaving 4096 tokens for a prompt whose tool
block alone measured ~4700.

The estimate is deliberately crude (characters over four). A real tokenizer would
mean shipping one per provider, and being approximately right here is what keeps
a request from being rejected outright. Every function is total: a caller that
cannot budget the request should send it anyway rather than drop the user's
message.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger("addled.providers.budget")

# Characters per token. English prose lands near four, but Indonesian, code and
# JSON are denser, and this estimate decides whether a request is trimmed. An
# UNDER-estimate is the unsafe direction: the check passes, nothing is trimmed,
# and the provider rejects the whole request — which is what "request (8470
# tokens) exceeds the available context size (8192)" was, seen in a real chat.
# Three errs the other way and only costs some trimming.
CHARS_PER_TOKEN = 3

# Never ask for fewer than this, or the model cannot answer at all.
MIN_REPLY_TOKENS = 256

# Context windows, by provider id. Used only when the provider does not report
# its own, and only to decide how much to trim.
CONTEXT_LIMITS = {
    "global": 65536,
    "deepseek": 65536,
    "openai": 128000,
    "copilot": 128000,
    "openrouter": 128000,
    "claude": 200000,
    "gemini": 1000000,
    "ollama": 32768,
    "lmstudio": 32768,
    "huggingface": 8192,
    "local": 8192,
    "huggingface_local": 8192,
}
# Used only when the provider published nothing and is not in the table above.
# This was 32768 — the smallest *local* window in the table — which meant a
# cloud gateway that advertised 200000 was assumed to hold 32768 and every long
# conversation was trimmed to fit a window that did not exist. A proxy is a
# cloud endpoint; the cheapest cloud windows in the table are 128000, so that is
# the honest guess, and it is logged rather than silent. A too-small window
# degrades every long conversation invisibly; a too-large one fails one request
# visibly, and this is still far below what a modern proxy reports.
FALLBACK_CONTEXT = 128000

# Local runtimes are the exception: they serve a fixed, usually small window and
# assuming 128k against them produces rejected requests.
LOCAL_FALLBACK_CONTEXT = 32768

# -- prompt accounting ---------------------------------------------------------

# The system prompt is assembled by ~20 `sys_prompt = sys_prompt + ...` calls
# spread over 300 lines, each in its own try/except. Nothing could say what any
# block cost, which is why the largest single contributor went unnoticed: the
# wiki block measured 5553 characters against a 2592-character tool catalogue,
# and producing that number took an offline harness rather than reading the log.
#
# PromptBuild wraps the concatenation without changing its type. Everything
# downstream still receives a `str`, every `if block:` guard still works, and a
# caller that ignores the breakdown pays one `len()` per block.
#
# The two-tier split is the other half of the point. Providers cache an
# unchanged *prefix*: Anthropic needs an explicit breakpoint, OpenAI, DeepSeek
# and Gemini cache automatically, and all of them require the prefix to be
# byte-identical from the start. Addled appends per-turn relevance-gated memory
# onto the front, so the prefix changed on essentially every turn and nothing
# could ever hit. Marking a block `stable` and keeping the volatile ones after
# it is the precondition for caching anything, and it costs nothing when
# caching is not in play.


class PromptBuild:
    """A prompt string that remembers what each block cost.

    ``add()`` appends. ``stable`` marks a block as belonging to the cacheable
    prefix; the first volatile block closes the prefix, and ``prefix()`` returns
    it. Ordering is what makes a cache reachable, so a volatile block added
    before a stable one is a programming error and is logged rather than
    silently tolerated.
    """

    __slots__ = ("_parts", "_blocks", "_prefix_end", "_sealed", "_name")

    def __init__(self, initial: str = "", label: str = "base",
                 *, name: str = "prompt"):
        self._parts: list[str] = []
        self._blocks: list[tuple[str, int, bool]] = []
        self._prefix_end: int | None = None
        self._sealed = False
        self._name = name
        if initial:
            self._parts.append(initial)
            self._blocks.append((label, len(initial), True))

    def add(self, label: str, block: str | None, *, stable: bool = False,
            newline: bool = True) -> "PromptBuild":
        """Append one labelled block. Tolerates None, so callers drop the `if`."""
        text = block or ""
        if newline and text:
            text = "\n\n" + text
        if stable and self._sealed:
            log.warning("prompt '%s': stable block '%s' added after a volatile "
                        "one; the cache prefix cannot include it", self._name,
                        label)
        if not stable and self._prefix_end is None:
            self._prefix_end = len(self._parts)
            self._sealed = True
        self._parts.append(text)
        self._blocks.append((label, len(text), stable))
        return self

    def extend(self, other: "PromptBuild") -> "PromptBuild":
        """Append another build's text and block records, separators and all."""
        for label, _chars, stable in other._blocks:
            self.add(label, other._text_of(label), stable=stable)
        return self

    def _text_of(self, label: str) -> str:
        """The text recorded under `label`, separators stripped back off."""
        for i, (name, _chars, _stable) in enumerate(self._blocks):
            if name == label and i < len(self._parts):
                return self._parts[i].lstrip("\n")
        return ""

    def __str__(self) -> str:
        return "".join(self._parts)

    def __len__(self) -> int:
        return sum(len(p) for p in self._parts)

    def prefix(self) -> str:
        """The byte-stable part a provider can cache, up to the first volatile block."""
        end = self._prefix_end if self._prefix_end is not None else len(self._parts)
        return "".join(self._parts[:end])

    def breakdown(self, chars_per_token: int | None = None) -> list[dict]:
        """Per-block sizes, biggest first -- the `/context` view."""
        cpt = chars_per_token or CHARS_PER_TOKEN
        rows = [{"label": label, "chars": chars, "stable": stable,
                 "tokens": chars // cpt if chars else 0}
                for (label, chars, stable) in self._blocks]
        return sorted(rows, key=lambda r: r["chars"], reverse=True)

    def report(self, threshold_chars: int = 0) -> str:
        """One line per block, for the log or the dashboard."""
        return "\n".join(
            f"  {r['label']:<20}{'S' if r['stable'] else ' '} "
            f"{r['chars']:>7,} chars  ~{r['tokens']:>6,} tok"
            for r in self.breakdown() if r["chars"] >= threshold_chars)

    def summary(self) -> dict:
        """The numbers an operator or the UI wants, without the text."""
        stable_chars = sum(c for _l, c, s in self._blocks if s)
        return {
            "chars": len(self),
            "tokens": estimate_tokens(str(self)),
            "blocks": len(self._blocks),
            "stable_chars": stable_chars,
            "prefix_chars": len(self.prefix()),
            "volatile_chars": len(self) - len(self.prefix()),
            "breakdown": self.breakdown(),
        }




def estimate_tokens(text: str) -> int:
    """Approximate tokens for one string. Never negative, never zero for text."""
    if not text:
        return 0
    return max(1, len(str(text)) // CHARS_PER_TOKEN)


def message_tokens(messages: list[dict]) -> int:
    """Approximate tokens for a whole message list."""
    total = 0
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False) if content else ""
        total += estimate_tokens(content) + 4        # per-message overhead
        for call in message.get("tool_calls") or []:
            total += estimate_tokens(json.dumps(call, ensure_ascii=False))
    return total


def context_limit(provider_id: str, model: str | None = None) -> int:
    """Context window to assume for a provider and model.

    Order: what the provider itself published on ``GET /models``, then a
    user override, then the static table, then a last-resort default. The
    discovered value wins because it is the only one that can be right for a
    gateway serving models with windows from 200k to 1.05M — one number per
    provider cannot describe that.
    """
    if provider_id == "local":
        try:
            from backend.config import config
            configured = int(config.get("local_llm", "ctx", default=0) or 0)
            if configured > 0:
                return configured
        except Exception:
            pass
    published = _published_limit(provider_id, model)
    if published:
        return published
    if provider_id in CONTEXT_LIMITS:
        return CONTEXT_LIMITS[provider_id]
    fallback = _fallback_for(provider_id)
    # Logged at info, not debug: this defect survived a whole research pass
    # because the wrong number was silent.
    log.info("assuming a %d-token context for '%s': the provider published "
             "none (set providers.%s.context_limit to override)",
             fallback, provider_id or "?", provider_id or "?")
    return fallback


def _fallback_for(provider_id: str) -> int:
    """Last-resort window for a provider that published and declared nothing."""
    pid = (provider_id or "").lower()
    if pid in ("ollama", "lmstudio", "huggingface", "huggingface_local",
               "local"):
        return LOCAL_FALLBACK_CONTEXT
    return FALLBACK_CONTEXT


def _published_limit(provider_id: str, model: str | None) -> int:
    """The window the provider advertised for this model, or 0 if unknown.

    Best-effort by design: discovery is a cache read and a provider that never
    answered must not break budgeting.
    """
    pid = provider_id or ""
    if not pid:
        return 0
    override = _configured_limit(pid)
    if override:
        return override
    try:
        from backend.providers import model_catalog
        limits = model_catalog.cached_limits(pid)
    except Exception:  # never let discovery break a request
        return 0
    if not limits:
        return 0
    if model:
        exact = limits.get(str(model))
        if exact:
            return exact
        # The model is named and the provider did not publish a window for it.
        # Borrowing a sibling's number would size the request for a different
        # model — on this gateway that spans 200k to 1.05M, so it is not a
        # rounding error. Fall through to the caller's default instead.
        return 0
    # No model named. Ask the provider which model it would actually use —
    # that is the one a request without an explicit model will go to.
    default = _default_model(pid)
    if default and limits.get(default):
        return limits[default]
    # Still nothing: one entry is unambiguous, and with several the smallest is
    # the only defensible read, because assuming a window larger than the model
    # has causes rejected requests rather than merely short ones.
    if len(limits) == 1:
        return next(iter(limits.values()))
    return min(limits.values())


def _default_model(provider_id: str) -> str:
    """The model this provider serves when none is named, or ''."""
    try:
        from backend.config import config
        cfg = config.provider_config(provider_id) or {}
        return str(cfg.get("default_model") or "").strip()
    except Exception:
        return ""


def _configured_limit(provider_id: str) -> int:
    """A ``providers.<id>.context_limit`` the user set by hand, or 0."""
    try:
        from backend.config import config
        cfg = config.provider_config(provider_id) or {}
        value = int(cfg.get("context_limit") or 0)
        return value if value > 0 else 0
    except Exception:
        return 0


def fit_messages(messages: list[dict], limit: int, reserve: int,
                 keep_recent: int = 4) -> tuple[list[dict], int]:
    """Trim history until the request fits. Returns (messages, dropped).

    The system prompt and the most recent turns are never dropped — losing the
    system prompt changes the agent's behaviour, and losing the latest turn
    loses the question. Older middle turns go first. If it still does not fit,
    the caller is told how much it overruns rather than having its message
    silently deleted.
    """
    if not messages:
        return messages, 0

    room = limit - reserve
    if message_tokens(messages) <= room:
        return messages, 0

    # Keep the system prompt and the newest turns. The system message is
    # selected BY ROLE, not by identity: `[m for m in messages if m is not
    # head[0]]` relied on the object being the same one, so a caller that
    # passed two system dicts — or a copy — would have both silently dropped
    # with the head. Only the first system message is pinned; the rest are
    # ordinary middle content.
    pinned_system: list[dict] = []
    rest: list[dict] = []
    system_taken = False
    for m in messages:
        if not system_taken and m.get("role") == "system":
            pinned_system.append(m)
            system_taken = True
            continue
        rest.append(m)
    if len(rest) <= keep_recent:
        return messages, 0

    tail = rest[-keep_recent:]
    middle = rest[:-keep_recent]
    dropped = 0
    while middle and message_tokens(pinned_system + middle + tail) > room:
        middle.pop(0)                 # oldest first
        dropped += 1

    # Hand back the smaller list even when it still does not fit: the caller can
    # then report that the request is too large, which is better than sending
    # the whole thing and getting an opaque rejection from the provider.
    return pinned_system + middle + tail, dropped


def reply_budget(prompt_tokens: int, limit: int,
                 want: int = 4096) -> int:
    """How many tokens the reply may use without overrunning the window."""
    room = limit - prompt_tokens - 64            # small safety margin
    if room <= 0:
        return 0
    return max(MIN_REPLY_TOKENS, min(want, room)) if room >= MIN_REPLY_TOKENS \
        else room

def fit_single_prompt(prompt: str, provider_id: str,
                      want_reply: int = 1024) -> tuple[str, int]:
    """Clip one self-contained prompt to a window, and say how much reply room is left.

    Background jobs — the skill forge's discovery and code generation calls, in
    particular — build their own one-message prompt and hand it straight to
    ``provider.chat``. That bypasses the tool loop and therefore all of the
    trimming above, so on the 8k local model the forge's prompt plus a 2000-token
    generation request overran the window and came back as an opaque provider
    error. The prompt is what gets clipped here: unlike a conversation, it is
    reproducible and the tail carries the instruction.

    Returns ``(prompt, max_tokens)``; ``max_tokens`` is 0 when even the clipped
    prompt cannot fit, which the caller should treat as "do not send this".
    """
    limit = context_limit(provider_id)
    tokens = estimate_tokens(prompt)
    room = limit - want_reply - 64
    if tokens > room and room > 0:
        keep_chars = max(0, room * CHARS_PER_TOKEN)
        prompt = prompt[:keep_chars]
        log.info("Clipped a background prompt to fit %s's %d-token context",
                 provider_id or "provider", limit)
    return prompt, reply_budget(estimate_tokens(prompt), limit, want=want_reply)
