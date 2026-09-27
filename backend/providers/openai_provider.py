"""
OpenAI provider (and OpenAI-compatible endpoints).

Also serves as base for LM Studio and any custom OpenAI-compatible API.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from backend.providers.base import BaseProvider, ProviderResult


_HTTP_ERROR_CHARS = 400


def http_error_detail(exc: httpx.HTTPStatusError) -> str:
    """The upstream's own explanation, not httpx's status line.

    ``str(exc)`` is "Client error '404 Not Found' for url ...", which throws
    away the only useful part. OpenRouter, for one, explains a refusal in the
    body and links to the setting that lifts it — the difference between a
    mystery and a one-click fix.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or "")
            elif isinstance(error, str):
                detail = error
            detail = detail or str(payload.get("message") or "")
    except Exception:
        detail = (response.text or "")[:_HTTP_ERROR_CHARS]
    detail = " ".join(detail.split())[:_HTTP_ERROR_CHARS]
    if not detail:
        return str(exc)
    return f"HTTP {response.status_code}: {detail}"


def _parse_sse_as_completion(text: str, model: str) -> dict:
    """Reassemble a non-requested SSE stream into a normal chat completion dict.

    Some routers always return SSE regardless of whether ``stream`` was sent.
    Collect delta content from every ``data:`` line and rebuild a minimal
    non-streaming response so the rest of the call-path stays unchanged.
    """
    content_parts: list[str] = []
    result_model = model
    usage: dict = {}
    tool_calls: list = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except Exception:
            continue
        result_model = chunk.get("model", result_model)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                while len(tool_calls) <= idx:
                    tool_calls.append({"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls[idx]["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += fn["arguments"]
    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "model": result_model,
        "usage": usage,
    }


class OpenAIProvider(BaseProvider):
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
                try:
                    data = resp.json()
                except Exception:
                    # Server returned SSE even though we didn't request streaming
                    # (some routers/proxies always stream). Reconstruct from chunks.
                    data = _parse_sse_as_completion(resp.text, model)
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
        except httpx.HTTPStatusError as e:
            return ProviderResult(ok=False, error=http_error_detail(e),
                                  duration_ms=int((time.monotonic() - t0) * 1000))
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
