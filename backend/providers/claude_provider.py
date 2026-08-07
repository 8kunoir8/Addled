"""
Anthropic Claude provider.

Uses Anthropic Messages API.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


class ClaudeProvider(BaseProvider):
    provider_id = "claude"
    provider_name = "Claude"
    supports_vision = True
    supports_streaming = True

    def _get_client(self) -> httpx.AsyncClient:
        api_key = self._config.get("api_key", "")
        return httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "claude-sonnet-4-20250514")

        # Convert OpenAI-format messages to Anthropic format
        system = ""
        anthropic_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthropic_messages.append({"role": m["role"], "content": m["content"]})

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_messages,
        }
        if system:
            body["system"] = system

        try:
            async with self._get_client() as client:
                resp = await client.post("/v1/messages", json=body)
                resp.raise_for_status()
                data = resp.json()
                content = data["content"][0]["text"]
                return ProviderResult(
                    ok=True,
                    response=content,
                    model=data.get("model", model),
                    tokens_in=data.get("usage", {}).get("input_tokens", 0),
                    tokens_out=data.get("usage", {}).get("output_tokens", 0),
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
        model = model or self._config.get("default_model", "claude-sonnet-4-20250514")

        system = ""
        anthropic_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                anthropic_messages.append({"role": m["role"], "content": m["content"]})

        body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_messages,
            "stream": True,
        }
        if system:
            body["system"] = system

        try:
            async with self._get_client() as client:
                async with client.stream("POST", "/v1/messages", json=body) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            import json
                            try:
                                event = json.loads(data)
                                if event.get("type") == "content_block_delta":
                                    delta = event.get("delta", {})
                                    text = delta.get("text", "")
                                    if text:
                                        yield text
                            except json.JSONDecodeError:
                                continue
        except Exception:
            yield ""
