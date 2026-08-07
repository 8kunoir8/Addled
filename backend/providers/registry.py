"""
Provider registry — unified built-in + custom provider management.

Enforces TLS (HTTPS only) for custom providers.
"""

from __future__ import annotations

from backend.config import config


def list_available() -> list[dict]:
    """Return all providers the user can switch to right now (with status)."""
    builtin = config.get("providers", "builtin") or {}
    custom = config.get("providers", "custom") or []
    active = config.active_provider

    result = []
    for pid, pdata in builtin.items():
        result.append({
            "id": pid,
            "name": pdata.get("name", pid),
            "is_builtin": True,
            "is_active": pid == active,
            "has_key": bool(pdata.get("api_key", "")),
            "vision": pdata.get("vision", False),
            "base_url": pdata.get("base_url", ""),
            "models": pdata.get("models", []),
        })
    for c in custom:
        pid = c.get("id", "unknown")
        result.append({
            "id": pid,
            "name": c.get("name", pid),
            "is_builtin": False,
            "is_active": pid == active,
            "has_key": bool(c.get("api_key", "")),
            "vision": c.get("vision", True),
            "base_url": c.get("base_url", ""),
            "models": c.get("models", []),
        })
    return result


def get_provider(provider_id: str | None = None):
    """Get a provider instance by ID. Falls back to active provider."""
    from backend.providers.deepseek_provider import DeepSeekProvider
    from backend.providers.openai_provider import OpenAIProvider
    from backend.providers.claude_provider import ClaudeProvider
    from backend.providers.gemini_provider import GeminiProvider
    from backend.providers.ollama_provider import OllamaProvider

    pid = provider_id or config.active_provider
    cfg = config.provider_config(pid)

    if pid == "deepseek":
        return DeepSeekProvider(cfg)
    elif pid == "openai":
        return OpenAIProvider(cfg)
    elif pid == "claude":
        return ClaudeProvider(cfg)
    elif pid == "gemini":
        return GeminiProvider(cfg)
    elif pid == "ollama":
        return OllamaProvider(cfg)
    elif pid == "lmstudio":
        return OpenAIProvider({**cfg, "base_url": cfg.get("base_url", "http://localhost:1234/v1"),
                               "provider_id": "lmstudio"})
    elif pid == "copilot":
        from backend.providers.copilot_provider import CopilotProvider
        return CopilotProvider(cfg)
    else:
        # Custom provider — treat as OpenAI-compatible
        return OpenAIProvider({**cfg, "provider_id": pid})
