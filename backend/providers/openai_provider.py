"""
OpenAI provider (and OpenAI-compatible endpoints).

Also serves as base for LM Studio and any custom OpenAI-compatible API.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


class OpenAIProvider(BaseProvider):
    provider_id = "openai"
    provider_name = "OpenAI"
    supports_vision = True
    supports_streaming = True

    def __init__(self, config: dict):
        super().__init__(config)
        # Allow override for LM Studio / custom
        self.provider_id = config.get("provider_id", "openai")

    def _get_client(self) -> httpx.AsyncClient:
        api_key = self._config.get("api_key", "")
        base_url = self._config.get("base_url", "https://api.openai.com/v1")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0)

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "gpt-4o")
        try:
            payload: dict = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if tools:
                payload["tools"] = tools
            async with self._get_client() as client:
                resp = await client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                return ProviderResult(
                    ok=True,
                    response=message.get("content") or "",
                    model=data.get("model", model),
                    tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                    tokens_out=data.get("usage", {}).get("completion_tokens", 0),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    tool_calls=message.get("tool_calls") or None,
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
        model = model or self._config.get("default_model", "gpt-4o")
        try:
            async with self._get_client() as client:
                async with client.stream("POST", "/chat/completions", json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                }) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            import json
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
        except Exception:
            yield ""

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """Vision analysis using GPT-4o or compatible."""
        model = model or self._config.get("default_model", "gpt-4o")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        }]
        return await self.chat(messages, model=model, max_tokens=1024)
