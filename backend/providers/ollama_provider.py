"""
Ollama local provider.

Uses the ollama Python SDK for local LLM inference.
Supports vision models (minicpm-v, llava).
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from backend.providers.base import BaseProvider, ProviderResult


class OllamaProvider(BaseProvider):
    provider_id = "ollama"
    provider_name = "Ollama (Local)"
    supports_vision = True
    supports_streaming = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._host = config.get("base_url", "http://localhost:11434").rstrip("/")

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "minicpm-v:8b")
        try:
            import ollama
            client = ollama.AsyncClient(host=self._host)
            resp = await client.chat(
                model=model,
                messages=messages,
                options={
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
            )
            return ProviderResult(
                ok=True,
                response=resp["message"]["content"],
                model=model,
                tokens_in=resp.get("prompt_eval_count", 0),
                tokens_out=resp.get("eval_count", 0),
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ProviderResult(ok=False, error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        model = model or self._config.get("default_model", "minicpm-v:8b")
        try:
            import ollama
            client = ollama.AsyncClient(host=self._host)
            stream = await client.chat(
                model=model,
                messages=messages,
                options={
                    "num_predict": max_tokens,
                    "temperature": temperature,
                },
                stream=True,
            )
            async for chunk in stream:
                content = chunk.get("message", {}).get("content", "")
                if content:
                    yield content
        except Exception:
            yield ""

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """Vision analysis using an Ollama vision model."""
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "minicpm-v:8b")
        try:
            import ollama
            client = ollama.AsyncClient(host=self._host)
            resp = await client.chat(
                model=model,
                messages=[{
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                }],
            )
            return ProviderResult(
                ok=True,
                response=resp["message"]["content"],
                model=model,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ProviderResult(ok=False, error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))

    async def list_models(self) -> list[str]:
        """List locally installed Ollama models."""
        try:
            import ollama
            client = ollama.AsyncClient(host=self._host)
            models = await client.list()
            return [m["name"] for m in models.get("models", [])]
        except Exception:
            return self._config.get("models", [])

    async def validate(self) -> dict:
        """Test Ollama connection."""
        try:
            models = await self.list_models()
            return {"ok": True, "models": models}
        except Exception as e:
            return {"ok": False, "error": str(e), "models": []}
