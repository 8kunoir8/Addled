"""
Google Gemini provider.

Uses google-generativeai SDK.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

from backend.providers.base import BaseProvider, ProviderResult


class GeminiProvider(BaseProvider):
    provider_id = "gemini"
    provider_name = "Gemini"
    supports_vision = True
    supports_streaming = True

    def _get_model(self, model_name: str | None = None):
        import google.generativeai as genai
        api_key = self._config.get("api_key", "")
        genai.configure(api_key=api_key)
        model = model_name or self._config.get("default_model", "gemini-2.0-flash")
        return genai.GenerativeModel(model)

    def _messages_to_prompt(self, messages: list[dict]) -> str:
        """Convert chat messages to a single prompt string."""
        parts = []
        for m in messages:
            role = "User" if m["role"] == "user" else "Assistant" if m["role"] == "assistant" else "System"
            parts.append(f"{role}: {m['content']}")
        return "\n\n".join(parts)

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> ProviderResult:
        t0 = time.monotonic()
        try:
            gm = self._get_model(model)
            prompt = self._messages_to_prompt(messages)
            resp = gm.generate_content(
                prompt,
                generation_config={
                    "max_output_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            return ProviderResult(
                ok=True,
                response=resp.text,
                model=gm.model_name,
                tokens_in=resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0,
                tokens_out=resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0,
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
            gm = self._get_model(model)
            prompt = self._messages_to_prompt(messages)
            resp = gm.generate_content(
                prompt,
                generation_config={
                    "max_output_tokens": max_tokens,
                    "temperature": temperature,
                },
                stream=True,
            )
            for chunk in resp:
                if chunk.text:
                    yield chunk.text
        except Exception:
            yield ""

    async def vision(
        self,
        image_b64: str,
        prompt: str,
        model: str | None = None,
    ) -> ProviderResult:
        """Vision analysis using Gemini."""
        import google.generativeai as genai
        from google.generativeai.types import Part

        t0 = time.monotonic()
        try:
            api_key = self._config.get("api_key", "")
            genai.configure(api_key=api_key)
            model_name = model or self._config.get("default_model", "gemini-2.0-flash")
            gm = genai.GenerativeModel(model_name)
            resp = gm.generate_content([Part.from_data(mime_type="image/png", data=image_b64), prompt])
            return ProviderResult(
                ok=True,
                response=resp.text,
                model=model_name,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        except Exception as e:
            return ProviderResult(ok=False, error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
