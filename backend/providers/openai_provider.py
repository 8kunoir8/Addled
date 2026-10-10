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
        # `str(exc)` can itself be empty for some httpx errors, and returning it
        # put a bare "Provider error: " in front of the user.
        detail = str(exc) or f"HTTP {response.status_code} with no detail"
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
        return httpx.AsyncClient(base_url=base_url, headers=headers,
                                 timeout=self._timeout())

    def _timeout(self) -> float:
        """How long to wait for a completion, in seconds.

        A flat 60s is right for a hosted API and wrong for a local model. A
        local 8B produced 2000 tokens in ~45s, so a 6000-token generation
        reliably exceeded 60s — and `httpx.ReadTimeout` has an EMPTY `str()`,
        so the caller saw `Provider error: ` with no reason at all. That blank
        message is what made the forge look broken instead of slow.
        """
        import os
        env = os.environ.get("ADDLED_HTTP_TIMEOUT")
        if env:
            try:
                return float(env)
            except ValueError:
                pass
        # Local servers are slower and are not billed by the second.
        if self.provider_id in ("local", "ollama", "lmstudio"):
            return 600.0
        return 120.0

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
                content = message.get("content") or ""
                # Thinking models put their reasoning in a SEPARATE field. Two
                # things go wrong when it is dropped, and both did:
                #
                # 1. `tool_loop` must echo `reasoning_content` back on the
                #    follow-up request, or the model repeats what it already
                #    reasoned about. `deepseek_provider` already passed it; this
                #    one did not, so every OpenAI-shaped thinking model lost it —
                #    including `LocalProvider`, which subclasses this.
                # 2. A thinking model can spend the WHOLE token budget reasoning
                #    and return no content at all. The local qwen3-8b did
                #    exactly that at 2000 tokens (finish_reason "length",
                #    9063 chars of reasoning, 0 of content), so the forge was
                #    handed an empty string and reported a failure that named
                #    neither the cause nor the fix.
                reasoning = (message.get("reasoning_content")
                             or message.get("reasoning") or "")
                finish = choice.get("finish_reason") or ""
                # A tool call with no prose is a COMPLETE answer, not an
                # empty one. `content` is nullable in the OpenAI schema and a
                # model that has nothing to say before acting sends
                # `content: null` with `finish_reason: "tool_calls"`. Measured
                # on the live gateway (deepseek-v4.1-flash via 9router):
                #
                #     RAW: finish='tool_calls' content=None tools=2
                #
                # The check below used to run before this one, so it discarded
                # those tool calls and reported "The model returned an empty
                # response. Check that the model is loaded" -- blaming the
                # model for a parser that had thrown its answer away. That is
                # the intermittent failure that killed 2 of 8 live runs at
                # round 2, after the model had already called meeting_list.
                # A tool call with no prose is a COMPLETE answer, not an
                # empty one. `content` is nullable in the OpenAI schema and a
                # model that has nothing to say before acting sends
                # `content: null` with `finish_reason: "tool_calls"`. Measured
                # on the live gateway (deepseek-v4.1-flash via 9router):
                #
                #     RAW: finish='tool_calls' content=None tools=2
                #
                # The check below used to run before this one, so it discarded
                # those tool calls and reported "The model returned an empty
                # response. Check that the model is loaded" -- blaming the
                # model for a parser that had thrown its answer away. That is
                # the intermittent failure that killed 2 of 8 live runs at
                # round 2, after the model had already called meeting_list.
                if not content.strip() and (message.get("tool_calls") or []):
                    return ProviderResult(
                        ok=True,
                        response=content or "",
                        model=data.get("model", model),
                        tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                        tokens_out=data.get("usage", {}).get("completion_tokens", 0),
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        tool_calls=message.get("tool_calls") or None,
                        reasoning_content=reasoning,
                    )

                if not content.strip():
                    # An empty reply is a FAILURE, not an empty success, and the
                    # reasoning is NOT substituted for the answer.
                    #
                    # Substituting it looked helpful and is not: the reasoning
                    # is prose ("Okay, so the user is asking..."), so a code
                    # consumer like the forge feeds it to ast.parse and reports
                    # a syntax error about a sentence — the same misleading
                    # symptom this change exists to remove. The reasoning is
                    # still carried on the result so `tool_loop` can echo it
                    # back, and the message says what actually happened.
                    if finish == "length":
                        why = (f"The model spent its entire output limit "
                               f"({max_tokens} tokens) thinking and returned no "
                               "answer. Raise the limit, use a model without a "
                               "thinking mode, or disable thinking.")
                    elif reasoning:
                        why = ("The model returned reasoning but no answer, so "
                               "there is nothing usable here. It may have hit "
                               f"the output limit ({max_tokens} tokens).")
                    else:
                        why = ("The model returned an empty response. Check "
                               "that the model is loaded, and that the prompt "
                               "is not being rejected.")
                    return ProviderResult(
                        ok=False, error=why,
                        model=data.get("model", model),
                        tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                        tokens_out=data.get("usage", {}).get("completion_tokens", 0),
                        duration_ms=int((time.monotonic() - t0) * 1000),
                        reasoning_content=reasoning,
                    )
                return ProviderResult(
                    ok=True,
                    response=content,
                    model=data.get("model", model),
                    tokens_in=data.get("usage", {}).get("prompt_tokens", 0),
                    tokens_out=data.get("usage", {}).get("completion_tokens", 0),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    tool_calls=message.get("tool_calls") or None,
                    reasoning_content=reasoning,
                )
        except httpx.HTTPStatusError as e:
            return ProviderResult(ok=False, error=http_error_detail(e),
                                  duration_ms=int((time.monotonic() - t0) * 1000))
        except httpx.TimeoutException:
            # `str(httpx.ReadTimeout())` is EMPTY, so raising it produced
            # "Provider error: " with nothing after it — the caller blamed the
            # model for a timeout it could not see. Timeouts are named now, and
            # local servers get a longer one (see `_timeout`).
            waited = time.monotonic() - t0
            return ProviderResult(
                ok=False,
                error=(f"The model did not finish within {self._timeout():.0f}s "
                       f"(waited {waited:.0f}s). A local model generating a long "
                       "reply can exceed the limit: raise ADDLED_HTTP_TIMEOUT, "
                       "or reduce the requested length."),
                model=model,
                duration_ms=int(waited * 1000),
            )
        except Exception as e:
            # Never an empty reason. An exception with no message is still a
            # failure, and the caller needs something to act on.
            reason = str(e) or f"{type(e).__name__} (no message)"
            return ProviderResult(ok=False,
                                  error=f"{type(e).__name__}: {reason}",
                                  model=model,
                                  duration_ms=int((time.monotonic() - t0) * 1000))

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
