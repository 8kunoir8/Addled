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

    def _get_vision_client(self) -> httpx.AsyncClient:
        """Client for DeepSeek-VL2 — same API key, optionally different endpoint."""
        api_key = self._config.get("api_key", "")
        vision_base_url = self._config.get("vision_base_url") or \
            self._config.get("base_url", "https://api.deepseek.com")
        return httpx.AsyncClient(
            base_url=vision_base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=90.0,
        )

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """
        Analyze an image using DeepSeek-VL2.

        Uses the OpenAI-compatible vision format with the SAME API key as chat.
        If the endpoint doesn't serve DeepSeek-VL2 (e.g. the standard cloud API),
        this returns a clear error — set vision_base_url to a VL2 endpoint
        (vLLM / Ollama / LM Studio) in settings.
        """
        model = model or self._config.get("vision_model", "deepseek-vl2")
        t0 = time.monotonic()
        try:
            async with self._get_vision_client() as client:
                resp = await client.post("/chat/completions", json={
                    "model": model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/png;base64,{image_b64}"}},
                        ],
                    }],
                    "max_tokens": 1024,
                    "temperature": 0.3,
                })
                if resp.status_code == 404:
                    return ProviderResult(
                        ok=False,
                        error=f"Model '{model}' not found on this endpoint. "
                              f"Set vision_base_url to a DeepSeek-VL2 endpoint.",
                    )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                return ProviderResult(
                    ok=True,
                    response=choice["message"]["content"],
                    model=data.get("model", model),
                    tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                    tokens_out=data.get("usage", {}).get("completion_tokens", 0),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
        except httpx.HTTPStatusError as e:
            api_error = (f"Vision request failed ({e.response.status_code}): "
                         f"{e.response.text[:200]}")
        except Exception as e:
            api_error = str(e)

        # ── Fall back to local DeepSeek-VL2-tiny via Hugging Face ──────────
        log.warning("DeepSeek cloud vision failed (%s) — falling back to HF %s",
                    api_error, "deepseek-ai/deepseek-vl2-tiny")
        from backend.providers.hf_vision import hf_vision
        result = await hf_vision.analyze(image_b64, prompt)
        if not result.ok:
            result.error = f"Cloud vision failed: {api_error} | HF fallback: {result.error}"
        return result

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        t0 = time.monotonic()
        model = model or self._config.get("default_model", "deepseek-v4-pro")
        try:
            async with self._get_client() as client:
                resp = await client.post("/chat/completions", json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                })
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                return ProviderResult(
                    ok=True,
                    response=choice["message"]["content"],
                    model=data.get("model", model),
                    tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                    tokens_out=data.get("usage", {}).get("completion_tokens", 0),
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
