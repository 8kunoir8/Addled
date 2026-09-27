"""
Base provider interface.

All AI providers implement this abstract base.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ProviderResult:
    ok: bool
    response: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    duration_ms: int = 0
    error: str | None = None
    tool_calls: list | None = None  # raw API tool_calls (OpenAI format)
    # Thinking-mode models (e.g. DeepSeek v4) return this alongside tool_calls
    # and require it to be echoed back on the follow-up request.
    reasoning_content: str = ""


# Provider ids that serve ONE generation at a time.
#
# Kept here rather than in the swarm orchestrator so every caller agrees on the
# same answer. The orchestrator already treats these as single-generation when
# it decides whether a flow's steps may run concurrently, and the Code page needs
# the same fact to decide how many diffs to request at once — two copies of this
# list would drift, and drifting here means firing parallel requests at a server
# that can only queue them.
SINGLE_GENERATION_PROVIDERS = frozenset(
    {"local", "huggingface", "huggingface_local", "lmstudio", "ollama"}
)

# The widest a caller should go when the provider CAN serve several at once.
# A burst of twenty model calls is its own kind of rude — it hits rate limits,
# and on a metered provider it costs real money — so this is a ceiling, not a
# target.
MAX_CONCURRENCY = 3


def concurrency_width(provider, wanted: int = MAX_CONCURRENCY) -> int:
    """How many model requests may be in flight at once for `provider`.

    Returns 1 for a single-generation provider, whatever the caller asked for.
    That is the honest answer rather than a failure: the local model queues
    concurrent requests, so firing several would look parallel and behave
    serially, which is worse than saying so.

    An UNKNOWN provider is also width 1. A missing or unrecognised id is not
    permission to fan out — the cost of guessing wrong is a burst of requests at
    something that may queue, rate-limit, or bill per call, and the cost of
    being conservative is only that the work happens in order.

    Never fewer than 1, so a caller can treat the result as a width without
    guarding against zero.
    """
    pid = str(getattr(provider, "provider_id", "") or "").strip().lower()
    if not pid:
        return 1                       # unknown: assume it queues
    if pid in SINGLE_GENERATION_PROVIDERS:
        return 1
    try:
        return max(1, min(int(wanted), MAX_CONCURRENCY))
    except (TypeError, ValueError):
        return 1


class BaseProvider(ABC):
    """Abstract base for all AI providers."""

    provider_id: str
    provider_name: str
    supports_vision: bool = False
    supports_streaming: bool = True

    def __init__(self, config: dict):
        self._config = config

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        """Send a chat completion request."""
        ...

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream a chat completion response."""
        ...

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """Analyze an image. Override if provider supports vision."""
        return ProviderResult(ok=False, error="Vision not supported by this provider")

    async def list_models(self) -> list[str]:
        """List available models for this provider."""
        return self._config.get("models", [])

    async def validate(self) -> dict:
        """Test connection. Returns {"ok": True/False, "models": [...], "error": "..."}."""
        return {"ok": True, "models": await self.list_models()}
