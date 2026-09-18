"""
OpenRouter provider — OpenAI-compatible aggregator.

Adds OpenRouter's attribution headers and a clear error when no API key is set,
then reuses the OpenAI-compatible request path.
"""

from __future__ import annotations

import httpx

from backend.providers.base import ProviderResult
from backend.providers.openai_provider import OpenAIProvider

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    provider_id = "openrouter"
    provider_name = "OpenRouter"
    supports_vision = True
    supports_streaming = True

    def __init__(self, config: dict):
        super().__init__({**config, "provider_id": "openrouter"})
        if not self._config.get("base_url"):
            self._config["base_url"] = DEFAULT_BASE_URL

    def _get_client(self) -> httpx.AsyncClient:
        client = super()._get_client()
        extra = self._config.get("extra_headers") or {}
        for key, value in extra.items():
            if value:
                client.headers[key] = str(value)
        return client

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ProviderResult:
        if not self._config.get("api_key"):
            return ProviderResult(
                ok=False,
                error="OpenRouter API key not set. Add it in Settings — Providers "
                      "(openrouter.ai/keys).",
            )
        return await super().chat(
            messages, model=model, max_tokens=max_tokens,
            temperature=temperature, tools=tools,
        )

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """Vision via a multimodal OpenRouter model (default_model may be text-only)."""
        if not self._config.get("api_key"):
            return ProviderResult(
                ok=False,
                error="OpenRouter API key not set. Add it in Settings — Providers.",
            )
        model = model or self._config.get("vision_model") \
            or self._config.get("default_model")
        return await super().vision(image_b64, prompt, model=model)
