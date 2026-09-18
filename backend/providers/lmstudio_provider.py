"""
LM Studio provider — local LLM via OpenAI-compatible API.

LM Studio exposes an OpenAI-compatible endpoint at http://localhost:1234/v1
Works with any GGUF model loaded in LM Studio.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


class LMStudioProvider(BaseProvider):
    provider_id = "lmstudio"
    provider_name = "LM Studio"
    supports_vision = False  # LM Studio local models typically text-only
    supports_streaming = True

    def _get_client(self) -> httpx.AsyncClient:
        base_url = self._config.get("base_url", "http://localhost:1234/v1")
        return httpx.AsyncClient(
            base_url=base_url,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ProviderResult:
        started = time.monotonic()
        try:
            client = self._get_client()
            payload = {
                "messages": messages,
                "model": model or self._config.get("default_model")
                or self._config.get("model", "local-model"),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            return await self._sync_chat(client, payload, started)
        except httpx.ConnectError:
            return ProviderResult(
                ok=False,
                error="Cannot connect to LM Studio. Make sure it's running on http://localhost:1234",
            )
        except Exception as e:
            return ProviderResult(ok=False, error=str(e))

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        """Stream a chat completion response — yields content chunks."""
        try:
            client = self._get_client()
            payload = {
                "messages": messages,
                "model": model or self._config.get("default_model")
                or self._config.get("model", "local-model"),
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
            }
            async with client.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"[Error {resp.status_code}: {body.decode()[:200]}]"
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except httpx.ConnectError:
            yield "[Error: Cannot connect to LM Studio]"
        except Exception as e:
            yield f"[Error: {e}]"

    async def _sync_chat(self, client: httpx.AsyncClient, payload: dict,
                         started: float) -> ProviderResult:
        resp = await client.post("/chat/completions", json=payload)
        if resp.status_code != 200:
            return ProviderResult(
                ok=False,
                error=f"LM Studio returned {resp.status_code}: {resp.text[:200]}",
            )

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return ProviderResult(
            ok=True,
            response=message.get("content", ""),
            model=data.get("model", payload.get("model", "local-model")),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def list_models(self) -> list[str]:
        """List models currently loaded in LM Studio."""
        try:
            client = self._get_client()
            resp = await client.get("/models")
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("id", "unknown") for m in data.get("data", [])]
            return []
        except Exception:
            return []
