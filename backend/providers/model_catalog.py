"""
Model discovery — ask each provider which models it actually offers.

``providers.<id>.models`` is a hand-written list, so it goes stale the moment a
provider ships a new model. This module queries the real endpoints, caches the
answer, and merges it with the configured list.

The merged list is what the Settings dropdown offers and what the router
validates role overrides against. The cache lives in
``backend/memory/models_catalog.json`` — deliberately separate from
settings.json so a config reset does not throw away the discovery result.

Everything here is best-effort: a provider that is offline, rate-limited or
misconfigured records an error and keeps its previous list rather than losing
it. Nothing in this module may raise into a caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.config import SETTINGS_PATH, config

log = logging.getLogger("addled.providers.catalog")

CATALOG_PATH: Path = SETTINGS_PATH.parent / "models_catalog.json"
TIMEOUT = 12.0
DEFAULT_TTL_DAYS = 7

# Providers served by an OpenAI-compatible ``GET /models``.
_OPENAI_COMPATIBLE = ("openai", "deepseek", "openrouter", "local", "lmstudio")

# Local servers serve /models without an API key.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0", "::1", "[::1]")


def _is_loopback(base_url: str) -> bool:
    low = (base_url or "").lower()
    return any(h in low for h in _LOOPBACK_HOSTS)

_refresh_lock = asyncio.Lock()


# ---- cache -------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> dict:
    try:
        raw = CATALOG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    try:
        CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CATALOG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(CATALOG_PATH)
    except OSError as e:
        log.debug("could not write model catalog: %s", e)


def _provider_ids() -> list[str]:
    ids = list((config.get("providers", "builtin", default={}) or {}).keys())
    for c in config.get("providers", "custom", default=[]) or []:
        cid = (c or {}).get("id")
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def cached(pid: str) -> list[str]:
    """Models discovered from the provider's API (may be empty)."""
    entry = _load().get(pid) or {}
    models = entry.get("models")
    return [str(m) for m in models] if isinstance(models, list) else []


def cached_meta(pid: str) -> dict:
    entry = _load().get(pid) or {}
    return {
        "fetched_at": entry.get("fetched_at"),
        "checked_at": entry.get("checked_at"),
        "error": entry.get("error"),
        "status": entry.get("status"),
        "source": entry.get("source"),
    }


def merged(pid: str) -> list[str]:
    """Configured models first, then anything new the provider reports.

    A union rather than a replacement: a model the user pinned by hand must
    never silently disappear because an API response omitted it.
    """
    cfg = config.provider_config(pid) or {}
    configured = [str(m) for m in (cfg.get("models") or []) if m]
    out = list(configured)
    for m in cached(pid):
        if m not in out:
            out.append(m)
    return out


def ttl_days() -> float:
    try:
        return float(config.get("providers", "catalog_ttl_days",
                                default=DEFAULT_TTL_DAYS) or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def is_stale(pid: str) -> bool:
    """Should this provider be queried again?

    A recorded failure always counts as stale so it is retried at the next
    opportunity. A definitive answer (discovered, static, or nothing to query)
    is good for the whole TTL — otherwise providers that need no lookup would be
    re-checked on every single startup.
    """
    entry = _load().get(pid) or {}
    if entry.get("status") == "error":
        return True
    ts = entry.get("fetched_at") or entry.get("checked_at")
    if not ts:
        return True
    try:
        age = time.time() - datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return True
    return age > ttl_days() * 86400


def enabled() -> bool:
    return bool(config.get("providers", "catalog_auto", default=True))


# ---- discovery ---------------------------------------------------------------


async def _fetch_openai_compatible(pid: str, cfg: dict) -> dict:
    import httpx
    base = str(cfg.get("base_url") or "").rstrip("/")
    if not base:
        return {"status": "skipped", "models": [],
                "error": "no base_url configured"}
    key = str(cfg.get("api_key") or "").strip()
    if not key and not (cfg.get("local") or _is_loopback(base)):
        return {"status": "skipped", "models": [],
                "error": "no API key configured"}
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    extra = cfg.get("extra_headers") or {}
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    async with httpx.AsyncClient(timeout=TIMEOUT,
                                 follow_redirects=True) as client:
        resp = await client.get(f"{base}/models", headers=headers)
        if resp.status_code != 200:
            return {"status": "error", "models": [],
                    "error": f"HTTP {resp.status_code}: "
                             f"{resp.text[:160].strip()}"}
        data = resp.json()
    items = None
    if isinstance(data, dict):
        items = data.get("data", data.get("models"))
    elif isinstance(data, list):
        items = data
    models = []
    for item in items or []:
        mid = item.get("id") if isinstance(item, dict) else item
        if mid:
            models.append(str(mid))
    return {"status": "ok", "models": sorted(set(models)), "source": "api"}


async def _fetch_ollama(pid: str, cfg: dict) -> dict:
    import httpx
    base = str(cfg.get("base_url") or "http://localhost:11434").rstrip("/")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{base}/api/tags")
        if resp.status_code != 200:
            return {"status": "error", "models": [],
                    "error": f"HTTP {resp.status_code}"}
        data = resp.json()
    models = [str(m.get("name")) for m in (data.get("models") or [])
              if isinstance(m, dict) and m.get("name")]
    return {"status": "ok", "models": sorted(set(models)), "source": "api"}


async def _fetch_claude(pid: str, cfg: dict) -> dict:
    import httpx
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        return {"status": "skipped", "models": [],
                "error": "no API key configured"}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    async with httpx.AsyncClient(timeout=TIMEOUT,
                                 follow_redirects=True) as client:
        resp = await client.get(
            "https://api.anthropic.com/v1/models?limit=100", headers=headers)
        if resp.status_code != 200:
            return {"status": "error", "models": [],
                    "error": f"HTTP {resp.status_code}: "
                             f"{resp.text[:160].strip()}"}
        data = resp.json()
    models = [str(m.get("id")) for m in (data.get("data") or [])
              if isinstance(m, dict) and m.get("id")]
    return {"status": "ok", "models": sorted(set(models)), "source": "api"}


async def _fetch_gemini(pid: str, cfg: dict) -> dict:
    key = str(cfg.get("api_key") or "").strip()
    if not key:
        return {"status": "skipped", "models": [],
                "error": "no API key configured"}

    def _list() -> list[str]:
        import google.generativeai as genai
        genai.configure(api_key=key)
        names = []
        for m in genai.list_models():
            methods = getattr(m, "supported_generation_methods", None) or []
            if "generateContent" not in methods:
                continue
            name = str(getattr(m, "name", "") or "")
            names.append(name.split("/")[-1] if name else "")
        return names

    names = await asyncio.to_thread(_list)
    return {"status": "ok", "models": sorted({n for n in names if n}),
            "source": "api"}


async def fetch(pid: str) -> dict:
    """Query one provider. Never raises."""
    cfg = config.provider_config(pid) or {}
    if not cfg:
        return {"status": "error", "models": [],
                "error": "unknown provider"}
    try:
        if pid in ("huggingface", "local"):
            # Managed local models. llamafile reports the model as the path of
            # the file Addled downloaded, which is both useless in a dropdown
            # and redundant — the configured name is authoritative here.
            return {"status": "static", "models": list(cfg.get("models") or []),
                    "source": "static", "error": None}
        if pid == "copilot":
            return {"status": "static", "models": list(cfg.get("models") or []),
                    "source": "static", "error": None}
        if pid == "ollama":
            return await _fetch_ollama(pid, cfg)
        if pid == "claude":
            return await _fetch_claude(pid, cfg)
        if pid == "gemini":
            return await _fetch_gemini(pid, cfg)
        if pid in _OPENAI_COMPATIBLE or cfg.get("base_url"):
            return await _fetch_openai_compatible(pid, cfg)
        return {"status": "static", "models": list(cfg.get("models") or []),
                "source": "static", "error": None}
    except Exception as e:  # offline, DNS, malformed JSON, missing SDK, ...
        return {"status": "error", "models": [],
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


async def refresh(force: bool = False, only: list[str] | None = None) -> dict:
    """Query providers and update the cache. Never raises.

    A failed lookup keeps the models already cached for that provider and
    leaves ``fetched_at`` untouched, so it stays stale and is retried later.
    """
    if not force and not enabled():
        return {"skipped": "catalog_auto is off", "providers": 0}

    async with _refresh_lock:
        data = _load()
        targets = only if only is not None else _provider_ids()
        counts = {"ok": 0, "static": 0, "error": 0, "skipped": 0, "new": 0}
        for pid in targets:
            result = await fetch(pid)
            status = result.get("status") or "error"
            counts[status if status in counts else "error"] += 1
            previous = data.get(pid) or {}
            entry = dict(previous)
            entry["status"] = status
            entry["source"] = result.get("source") or previous.get("source")
            entry["error"] = result.get("error")
            entry["checked_at"] = _now_iso()
            discovered = result.get("models") or []
            if discovered:
                if sorted(discovered) != sorted(previous.get("models") or []):
                    counts["new"] += 1
                entry["models"] = discovered
                entry["fetched_at"] = _now_iso()
            else:
                # Keep whatever we had; only record why this attempt failed.
                entry.setdefault("models", [])
            data[pid] = entry
            if result.get("error"):
                log.debug("model discovery for '%s': %s (%s)", pid,
                          result["error"], status)
        _save(data)
        if counts["new"]:
            log.info("Model catalog refreshed: %s provider(s) changed",
                     counts["new"])
        return {**counts, "catalog": catalog()}


async def refresh_if_stale(force: bool = False) -> dict:
    """Refresh only where the cache is missing or older than the TTL.

    Called by the weekly housekeeping job, which also fires once at startup —
    the staleness check is what makes that first fire a no-op.
    """
    if not enabled():
        return {"skipped": "catalog_auto is off", "providers": 0}
    if force:
        return await refresh(force=True)
    stale = [pid for pid in _provider_ids() if is_stale(pid)]
    if not stale:
        return {"skipped": "fresh", "providers": 0}
    return await refresh(force=True, only=stale)


def catalog() -> dict:
    """Everything the Settings panel needs, per provider."""
    ttl = ttl_days()
    out: dict = {}
    for pid in _provider_ids():
        meta = cached_meta(pid)
        out[pid] = {
            "models": merged(pid),
            "discovered": cached(pid),
            "fetched_at": meta["fetched_at"],
            "checked_at": meta["checked_at"],
            "error": meta["error"],
            "status": meta["status"],
            "stale": is_stale(pid),
        }
    return {"providers": out, "ttl_days": ttl, "enabled": enabled()}
