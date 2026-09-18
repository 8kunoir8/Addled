"""
Fetch, cache and refresh the guideline pack documents.

The text is stored under ``backend/memory/guidelines/<pack>.md`` with an
``index.json`` recording where each copy came from, so the Settings panel can
show the real source URL and the user can read the full file. Nothing here may
raise into a caller: a pack that cannot be fetched simply is not injected, and
chat keeps working offline with whatever is already cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from backend.config import SETTINGS_PATH, config
from backend.guidelines import packs

log = logging.getLogger("addled.guidelines")

DIR = SETTINGS_PATH.parent / "guidelines"
INDEX_PATH = DIR / "index.json"
TIMEOUT = 20.0
DEFAULT_TTL_DAYS = 7

_fetch_lock = asyncio.Lock()


# ---- paths -------------------------------------------------------------------


def text_path(pack_id: str):
    return DIR / f"{pack_id}.md"


def text(pack_id: str) -> str:
    """The cached ruleset text, or "" when it has not been fetched yet."""
    try:
        return text_path(pack_id).read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_index() -> dict:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_index(data: dict) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = INDEX_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(INDEX_PATH)
    except OSError as e:
        log.debug("could not write guidelines index: %s", e)


def meta(pack_id: str) -> dict:
    return _read_index().get(pack_id) or {}


def ttl_days() -> float:
    try:
        return float(config.get("guidelines", "ttl_days",
                                default=DEFAULT_TTL_DAYS)
                     or DEFAULT_TTL_DAYS)
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def is_stale(pack_id: str) -> bool:
    """True when the cached copy is missing or older than the TTL.

    A recorded failure stays stale so the next opportunity retries it.
    """
    if not text(pack_id):
        return True
    entry = meta(pack_id)
    if entry.get("error"):
        return True
    ts = entry.get("fetched_at")
    if not ts:
        return True
    try:
        age = time.time() - datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return True
    return age > ttl_days() * 86400


# ---- fetch -------------------------------------------------------------------


def _strip_frontmatter(body: str) -> str:
    """Drop a leading YAML ``---`` block (upstream SKILL.md files carry one)."""
    if not body.startswith("---"):
        return body
    end = body.find("\n---", 3)
    if end == -1:
        return body
    rest = body[end + 4:]
    return rest.lstrip("\r\n")


def _write(pack_id: str, body: str, source_url: str) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = text_path(pack_id).with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(text_path(pack_id))
    index = _read_index()
    index[pack_id] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": len(body),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
        "source_url": source_url,
        "error": None,
    }
    _write_index(index)


async def fetch(pack_id: str, force: bool = False) -> dict:
    """Download one pack, trying each upstream URL in turn. Never raises."""
    spec = packs.PACKS.get(pack_id)
    if not spec:
        return {"ok": False, "pack": pack_id, "error": "unknown pack"}
    if not force and not is_stale(pack_id):
        return {"ok": True, "pack": pack_id, "skipped": "fresh"}

    import httpx
    errors: list[str] = []
    for url in spec.get("sources", ()):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT,
                                         follow_redirects=True) as client:
                resp = await client.get(
                    url, headers={"User-Agent": "Addled"})
            if resp.status_code != 200:
                errors.append(f"{url} -> HTTP {resp.status_code}")
                continue
            body = _strip_frontmatter(resp.text).strip()
            if not body:
                errors.append(f"{url} -> empty document")
                continue
            _write(pack_id, body + "\n", url)
            log.info("Guideline pack '%s' updated from %s (%d bytes)",
                     pack_id, url, len(body))
            return {"ok": True, "pack": pack_id, "source": url,
                    "bytes": len(body)}
        except Exception as e:
            errors.append(f"{url} -> {type(e).__name__}: {str(e)[:120]}")

    detail = "; ".join(errors)[:400] or "no sources configured"
    index = _read_index()
    entry = dict(index.get(pack_id) or {})
    entry["error"] = detail
    entry["checked_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    index[pack_id] = entry
    _write_index(index)
    log.debug("Guideline pack '%s' unavailable: %s", pack_id, detail)
    return {"ok": False, "pack": pack_id, "error": detail}


async def refresh_if_stale(force: bool = False) -> dict:
    """Refresh whichever packs are missing or past their TTL."""
    async with _fetch_lock:
        targets = [pid for pid in packs.PACKS
                   if force or is_stale(pid)]
        results = {}
        for pid in targets:
            results[pid] = await fetch(pid, force=True)
        if not targets:
            return {"skipped": "fresh", "packs": {}}
        return {"refreshed": len(targets), "packs": results}


def docs() -> dict:
    """Disk state per pack — the Settings panel merges this with the config."""
    out: dict = {}
    for pid, spec in packs.PACKS.items():
        entry = meta(pid)
        body = text(pid)
        out[pid] = {
            "title": spec.get("title", pid),
            "subtitle": spec.get("subtitle", ""),
            "summary": spec.get("summary", ""),
            "home": spec.get("home", ""),
            "license": spec.get("license", ""),
            "levels": list(spec.get("levels", ())),
            "default_level": spec.get("default_level", "full"),
            "sources": list(spec.get("sources", ())),
            "chars": len(body),
            "fetched_at": entry.get("fetched_at"),
            "source_url": entry.get("source_url") or "",
            "error": entry.get("error"),
            "stale": is_stale(pid),
            "cached": bool(body),
            "path": str(text_path(pid)),
        }
    return out
