"""
browser-use framework backend — open-ended multi-step web tasks.

Optional dependency (pip install browser-use). Never imported until needed;
every entry point degrades cleanly when the package is missing or when no
LLM is available (the llm_available gate).
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("addled.framework")

_available_cache: bool | None = None
_last_llm_error = 0.0
LLM_BACKOFF_S = 10 * 60

OPEN_ENDED_HINTS = (
    "find", "search for", "cheapest", "book", "login", "sign in",
    "fill out", "navigate and", "compare", "order", "summarize the",
)


def available() -> bool:
    """True when the browser-use package is importable."""
    global _available_cache
    if _available_cache is None:
        try:
            import browser_use  # noqa: F401
            _available_cache = True
        except Exception:
            _available_cache = False
    return _available_cache


def llm_available() -> bool:
    """True when an LLM usable by browser-use is configured and healthy.

    Local providers (ollama/lmstudio) count as available; cloud providers
    require an api key; recent provider failures trigger a backoff window."""
    global _last_llm_error
    from backend.config import config
    pid = config.active_provider
    if pid in ("ollama", "lmstudio"):
        return True
    try:
        cfg = config.provider_config(pid)
    except Exception:
        return False
    if not cfg.get("api_key", ""):
        return False
    if time.time() - _last_llm_error < LLM_BACKOFF_S:
        return False
    return True


def note_llm_error() -> None:
    global _last_llm_error
    _last_llm_error = time.time()


def _make_llm():
    """Build an OpenAI-compatible LLM client for the active provider.

    Tries the import paths browser-use uses across versions; returns None
    when none work (the router treats that as unavailable)."""
    from backend.config import config
    pid = config.active_provider
    if pid not in ("deepseek", "openai", "ollama", "lmstudio"):
        return None
    try:
        cfg = config.provider_config(pid)
    except Exception:
        return None
    kwargs = {
        "model": cfg.get("default_model", ""),
        "api_key": cfg.get("api_key", "") or "local",
        "base_url": cfg.get("base_url", ""),
    }
    for path in (("langchain_openai", "ChatOpenAI"),
                 ("browser_use.llm", "ChatOpenAI"),
                 ("browser_use", "ChatOpenAI")):
        try:
            mod = __import__(path[0], fromlist=[path[1]])
            cls = getattr(mod, path[1])
            return cls(**kwargs)
        except Exception:
            continue
    return None


async def run_task(task: str, max_steps: int = 10) -> dict:
    """Run one open-ended task through the browser-use agent.

    Returns {"success", "result_text", "steps", "error"} — never raises."""
    if not available():
        return {"success": False, "error": "browser-use is not installed"}
    if not llm_available():
        return {"success": False, "error": "no LLM available for browser-use"}
    llm = _make_llm()
    if llm is None:
        return {"success": False,
                "error": "could not build an LLM for the active provider"}
    try:
        from browser_use import Agent
        agent = Agent(task=task, llm=llm, use_vision=False)
        result = await agent.run(max_steps=int(max_steps))
        text = str(result) if result is not None else ""
        return {"success": True, "result_text": text[:4000],
                "steps": int(max_steps), "error": None}
    except Exception as e:
        note_llm_error()
        log.warning("browser-use task failed: %s", e)
        return {"success": False, "error": str(e)}
