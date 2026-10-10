"""
GitHub Copilot provider.

Uses device-flow OAuth for authentication.
Free tier with rate limits.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


class CopilotProvider(BaseProvider):
    provider_id = "copilot"
    provider_name = "GitHub Copilot"
    supports_vision = True
    supports_streaming = True

    def __init__(self, config: dict):
        super().__init__(config)
        self._token: str | None = None
        self._token_expiry: float = 0.0

    async def _ensure_token(self) -> str:
        """Get or refresh the Copilot token via device flow."""
        if self._token and time.time() < self._token_expiry:
            return self._token
        # Device flow: prompt user to visit URL and enter code
        # For now, use environment variable or config
        token = self._config.get("api_key", "")
        if not token:
            import os
            token = os.environ.get("COPILOT_TOKEN", "")
        if token:
            self._token = token
            self._token_expiry = time.time() + 3600
            return token
        raise ValueError("No Copilot token configured. Run device-flow login first.")

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        t0 = time.monotonic()
        try:
            token = await self._ensure_token()
            async with httpx.AsyncClient(
                base_url="https://api.githubcopilot.com",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=60.0,
            ) as client:
                resp = await client.post("/chat/completions", json={
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
                    model="copilot",
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
        try:
            token = await self._ensure_token()
            async with httpx.AsyncClient(
                base_url="https://api.githubcopilot.com",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
            ) as client:
                async with client.stream("POST", "/chat/completions", json={
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
                                # A native tool call shares this stream; the
                                # sentinel is the only channel a `str` iterator
                                # has. See STREAM_TOOL_CALL.
                                if delta.get("tool_calls"):
                                    from backend.skills.tool_loop import (
                                        STREAM_TOOL_CALL)
                                    yield STREAM_TOOL_CALL
                            except json.JSONDecodeError:
                                continue
        except Exception:
            yield ""
