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
FALLBACK_CONTEXT = 32768


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


def context_limit(provider_id: str) -> int:
    """Context window to assume for a provider."""
    if provider_id == "local":
        try:
            from backend.config import config
            configured = int(config.get("local_llm", "ctx", default=0) or 0)
            if configured > 0:
                return configured
        except Exception:
            pass
    return CONTEXT_LIMITS.get(provider_id or "", FALLBACK_CONTEXT)


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

    head = [m for m in messages if m.get("role") == "system"][:1]
    rest = [m for m in messages if m is not head[0]] if head else list(messages)
    if len(rest) <= keep_recent:
        return messages, 0

    tail = rest[-keep_recent:]
    middle = rest[:-keep_recent]
    dropped = 0
    while middle and message_tokens(head + middle + tail) > room:
        middle.pop(0)                 # oldest first
        dropped += 1

    # Hand back the smaller list even when it still does not fit: the caller can
    # then report that the request is too large, which is better than sending
    # the whole thing and getting an opaque rejection from the provider.
    return head + tail, dropped


def reply_budget(prompt_tokens: int, limit: int,
                 want: int = 4096) -> int:
    """How many tokens the reply may use without overrunning the window."""
    room = limit - prompt_tokens - 64            # small safety margin
    if room <= 0:
        return 0
    return max(MIN_REPLY_TOKENS, min(want, room)) if room >= MIN_REPLY_TOKENS \
        else room
