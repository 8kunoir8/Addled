"""
DeepSeek AI provider (OpenAI-compatible).

Uses https://api.deepseek.com with Bearer token auth.
Models: deepseek-v4-pro, deepseek-v4-flash, deepseek-chat.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


class DeepSeekProvider(BaseProvider):
    provider_id = "deepseek"
    provider_name = "DeepSeek"
    supports_streaming = True
    supports_vision = True  # via DeepSeek-VL2 (self-hosted or compatible endpoint)

    def _get_client(self) -> httpx.AsyncClient:
        api_key = self._config.get("api_key", "")
        base_url = self._config.get("base_url", "https://api.deepseek.com")
        return httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """
        Analyze an image using the LOCAL vision model via Hugging Face
        transformers (default: microsoft/Florence-2-base).

        DeepSeek's cloud API does not serve a vision model, so the
        provider goes straight to the local HF model — no cloud vision
        call, no API key needed for this path.
        """
        from backend.providers.hf_vision import hf_vision
        return await hf_vision.analyze(image_b64, prompt)

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "deepseek-v4-pro")
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
                    reasoning_content=message.get("reasoning_content") or "",
                )
        except Exception as e:
            # Surface the API's own message: a bare "400 Bad Request" from
            # httpx hides causes like a rejected model or message shape.
            detail = str(e)
            body = getattr(getattr(e, "response", None), "text", "")
            if body:
                detail = f"{detail} — {body[:300].strip()}"
            return ProviderResult(ok=False, error=detail,
                                  duration_ms=int((time.monotonic() - t0) * 1000))

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        model = model or self._config.get("default_model", "deepseek-v4-pro")
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
            yield ""  # Signal error
