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
