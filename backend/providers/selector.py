"""
Default-provider resolution.

Addled ships a local model as the no-API-key default. This module decides which
provider should actually serve a request: the user's explicit ``providers.active``
choice always wins when it can really run, otherwise we fall back sensibly instead
of failing with a 401.
"""

from __future__ import annotations

import logging

from backend.config import config

log = logging.getLogger("addled.providers")

# Providers that speak to a server the user may already run locally.
_EXTERNAL_LOCAL = ("ollama", "lmstudio")


def _raw_active() -> str:
    return config.get("providers", "active", default="local") or "local"


def _has_key(pid: str) -> bool:
    value = config.get("providers", "builtin", pid, "api_key", default="") or ""
    return bool(str(value).strip())


def _is_local(pid: str) -> bool:
    return bool(config.get("providers", "builtin", pid, "local", default=False))


def local_installed() -> bool:
    try:
        from backend.local_models import paths
        return paths.installed()
    except Exception:
        return False


def hf_ready() -> bool:
    """True when the HF (Local) model snapshot is on disk (cheap check)."""
    try:
        from backend.providers.huggingface_local_provider import snapshot_present
        model_id = config.get("providers", "builtin", "huggingface",
                              "default_model", default="") or ""
        root = config.get("providers", "builtin", "huggingface",
                          "model_root", default="") or ""
        return bool(model_id) and snapshot_present(model_id, root)
    except Exception:
        return False


def is_usable(pid: str) -> bool:
    """Can this provider actually answer a request right now?

    ``ollama``/``lmstudio`` return True so an explicit user choice is respected,
    but they are never *fallen back to* (see ``resolve_default_provider``).
    """
    if not pid:
        return False
    if pid == "local":
        return local_installed()
    if pid == "huggingface":
        return hf_ready()
    if pid == "copilot":
        import os
        return bool(_has_key("copilot") or os.environ.get("COPILOT_TOKEN"))
    if pid in _EXTERNAL_LOCAL:
        return True
    return _has_key(pid)


def resolve_default_provider() -> str:
    """The provider id Addled should use right now."""
    explicit = _raw_active()
    if not bool(config.get("providers", "smart_default", default=True)):
        return explicit
    if is_usable(explicit):
        return explicit
    if local_installed():
        return "local"
    if _has_key("openrouter"):
        return "openrouter"
    if bool(config.get("local_llm", "declined", default=False)):
        return "openrouter"
    for pid in config.get("providers", "priority", default=[]) or []:
        if pid != explicit and pid not in _EXTERNAL_LOCAL and is_usable(pid):
            log.info("Provider '%s' unavailable — using '%s'", explicit, pid)
            return pid
    # Nothing is usable. If the user has no cloud key at all, the local model is
    # the intended default (and the download prompt is the useful next step);
    # otherwise keep their choice so the real auth error surfaces.
    if any(_has_key(p) for p in ("openrouter", "deepseek", "openai", "claude",
                                 "gemini", "copilot")):
        return explicit
    if bool(config.get("local_llm", "declined", default=False)):
        return "openrouter"
    return "local"
