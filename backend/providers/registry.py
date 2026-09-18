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
    selected = config.selected_provider

    local_status: dict = {}
    try:
        from backend.local_llm.manager import local_llm
        local_status = local_llm.status()
    except Exception:
        local_status = {}

    result = []
    for pid, pdata in builtin.items():
        is_local = bool(pdata.get("local", False))
        entry = {
            "id": pid,
            "name": pdata.get("name", pid),
            "is_builtin": True,
            "is_active": pid == active,
            "is_selected": pid == selected,
            "has_key": bool(pdata.get("api_key", "")),
            "vision": pdata.get("vision", False),
            "base_url": pdata.get("base_url", ""),
            "models": pdata.get("models", []),
            "local": is_local,
            "requires_key": not is_local and pid != "copilot",
        }
        if pid == "local" and local_status:
            entry["installed"] = bool(local_status.get("installed"))
            entry["running"] = bool(local_status.get("running"))
            entry["needs_download"] = not bool(local_status.get("installed"))
        result.append(entry)
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
    from backend.providers.lmstudio_provider import LMStudioProvider

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
        return LMStudioProvider(cfg)
    elif pid == "local":
        from backend.providers.local_provider import LocalProvider
        return LocalProvider(cfg)
    elif pid == "openrouter":
        from backend.providers.openrouter_provider import OpenRouterProvider
        return OpenRouterProvider(cfg)
    elif pid == "huggingface":
        from backend.providers.huggingface_local_provider import (
            HuggingFaceLocalProvider,
        )
        return HuggingFaceLocalProvider(cfg)
    elif pid == "copilot":
        from backend.providers.copilot_provider import CopilotProvider
        return CopilotProvider(cfg)
    else:
        # Custom provider — treat as OpenAI-compatible
        return OpenAIProvider({**cfg, "provider_id": pid})
