"""
Addled Local provider — calls the llamafile-hosted model over HTTP.

The local server is started on demand by ``backend.local_llm.manager``; this
provider only translates "not installed / still starting" into an actionable
message instead of an opaque connection error.
"""

from __future__ import annotations

from backend.providers.base import ProviderResult
from backend.providers.openai_provider import OpenAIProvider


class LocalProvider(OpenAIProvider):
    provider_id = "local"
    provider_name = "Addled Local (llamafile)"
    supports_vision = False
    supports_streaming = True

    def __init__(self, config: dict):
        super().__init__({**config, "provider_id": "local"})
        from backend.config import config as app_config
        port = app_config.get("local_llm", "port", default=8090)
        base_url = self._config.get("base_url") or f"http://127.0.0.1:{port}/v1"
        # The port can change if 8090 was busy, so always re-read the live one.
        self._config["base_url"] = base_url
        self._config.setdefault("api_key", "sk-local")

    def _manager(self):
        from backend.local_llm.manager import local_llm
        return local_llm

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
    ) -> ProviderResult:
        manager = self._manager()
        problem = await manager.ensure_running()
        if problem:
            return ProviderResult(ok=False, error=problem)
        self._config["base_url"] = manager.api_base()
        if model is None:
            model = manager.model_id()
        return await super().chat(
            messages, model=model, max_tokens=max_tokens,
            temperature=temperature, tools=tools,
        )

    async def list_models(self) -> list[str]:
        manager = self._manager()
        if manager.is_running():
            return [manager.model_id()]
        return self._config.get("models", [])

    async def validate(self) -> dict:
        manager = self._manager()
        status = manager.status()
        if not status.get("installed"):
            return {
                "ok": False,
                "models": [],
                "error": "Local model not downloaded yet.",
            }
        problem = await manager.ensure_running()
        if problem:
            return {"ok": False, "models": [], "error": problem}
        return {"ok": True, "models": await self.list_models()}
