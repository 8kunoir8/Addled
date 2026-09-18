"""
MCP market — find servers in the official registry.

The Model Context Protocol registry (``registry.modelcontextprotocol.io``) is a
real API returning machine-readable specs: each entry advertises either
``packages`` (something to run locally, with the runtime it wants) or
``remotes`` (an HTTP endpoint). Addled already knows how to speak to both, so a
listing can be turned straight into a server spec.

What this module deliberately will not do is pretend a server is ready when it
is not. An entry that needs an API key, a required environment variable, or a
runtime that is not installed is reported with the reason rather than added and
left to fail on connect, which is the difference between a market and a list of
broken links.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import urllib.parse
import urllib.request

log = logging.getLogger("addled.mcp.market")

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
UA = {"User-Agent": "Addled/1.0 (mcp market)", "Accept": "application/json"}
TIMEOUT = 15.0

# package registryType -> (launcher, is it installed here)
_LAUNCHERS = {
    "npm": ("npx", ["-y"]),
    "pypi": ("uvx", []),
}


def _runtime_available(launcher: str) -> bool:
    return bool(shutil.which(launcher))


def _clean_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""


def _pick_remote(entry: dict) -> dict | None:
    for remote in entry.get("remotes") or []:
        if not isinstance(remote, dict):
            continue
        url = _clean_url(remote.get("url"))
        if not url:
            continue
        headers = [str(h.get("name") or "").strip()
                   for h in (remote.get("headers") or [])
                   if isinstance(h, dict) and h.get("name")]
        return {"url": url, "requires_headers": [h for h in headers if h]}
    return None


def _pick_package(entry: dict) -> dict | None:
    """The best locally-runnable package, npm first (npx ships with Node)."""
    packages = [p for p in (entry.get("packages") or [])
                if isinstance(p, dict) and p.get("identifier")]
    for wanted in ("npm", "pypi"):
        for package in packages:
            if str(package.get("registryType") or "").lower() == wanted:
                return package
    return None


def normalise(raw: dict) -> dict | None:
    """Turn one registry entry into a candidate Addled can present.

    Returns None for entries with nothing runnable in them: an ``oci`` package
    is a container image, which Addled has no way to launch.
    """
    entry = raw.get("server") if isinstance(raw.get("server"), dict) else raw
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    if not name:
        return None

    candidate = {
        "name": name,
        "title": str(entry.get("title") or "").strip(),
        "description": " ".join(str(entry.get("description") or "").split()),
        "version": str(entry.get("version") or ""),
        "repository": str((entry.get("repository") or {}).get("url") or ""),
        "transport": "",
        "command": "",
        "args": [],
        "url": "",
        "requires_env": [],
        "requires_headers": [],
        "runnable": False,
        "blocked_reason": "",
        "latest": bool(((raw.get("_meta") or {})
                        .get("io.modelcontextprotocol.registry/official") or {})
                       .get("isLatest", True)),
    }

    package = _pick_package(entry)
    remote = _pick_remote(entry)

    if package is not None:
        kind = str(package.get("registryType") or "").lower()
        launcher, prefix = _LAUNCHERS.get(kind, ("", []))
        identifier = str(package.get("identifier"))
        version = str(package.get("version") or "")
        arg = identifier
        # npx needs the version on the package spec; uvx takes the name alone
        # and resolves it itself.
        if kind == "npm" and version:
            arg = f"{identifier}@{version}"
        candidate.update({
            "transport": "stdio",
            "command": launcher,
            "args": prefix + [arg],
            "requires_env": sorted(
                str(v.get("name")) for v in (package.get("environmentVariables")
                                             or [])
                if isinstance(v, dict) and v.get("name") and v.get("isRequired")),
            "runtime": kind,
        })
    elif remote is not None:
        candidate.update({
            "transport": "http",
            "url": remote["url"],
            "requires_headers": remote["requires_headers"],
            "runtime": "remote",
        })
    else:
        return None

    # Why it cannot be used yet, if it cannot.
    reasons: list[str] = []
    if candidate["transport"] == "stdio":
        launcher = candidate["command"]
        if not _runtime_available(launcher):
            reasons.append(
                f"needs '{launcher}' on PATH"
                + (" (install Node.js)" if launcher == "npx"
                   else " (install uv)" if launcher == "uvx" else ""))
    if candidate["requires_env"]:
        reasons.append("needs " + ", ".join(candidate["requires_env"]))
    if candidate["requires_headers"]:
        reasons.append("needs an API key for "
                       + ", ".join(candidate["requires_headers"]))
    candidate["blocked_reason"] = "; ".join(reasons)
    candidate["runnable"] = not reasons
    return candidate


def to_spec(candidate: dict, *, trusted: bool = False,
            auto: bool = False) -> dict:
    """The server definition to hand to the manager."""
    from backend.mcp_client.manager import _sanitise, skill_name  # noqa: F401

    name = candidate.get("name") or ""
    short = (candidate.get("title") or name.split("/")[-1] or "server").strip()
    sid = _sanitise(name.replace("/", "-").replace(".", "-"))
    spec = {
        "id": sid,
        "name": short[:60] or sid,
        "transport": candidate.get("transport") or "stdio",
        "command": candidate.get("command") or "",
        "args": list(candidate.get("args") or []),
        "url": candidate.get("url") or "",
        "env": {},
        "headers": {},
        "enabled": True,
        "trusted": bool(trusted),
        # Marks a server nobody chose by hand, so the idle sweep knows which
        # ones it is allowed to switch off again.
        "auto": bool(auto),
        "market_name": name,
    }
    return spec


def _version_key(version: str) -> tuple:
    """Sort key for a version string; anything non-numeric is ignored."""
    import re

    parts = re.findall(r"\d+", str(version or ""))
    return tuple(int(p) for p in parts[:4]) or (0,)


def _fetch(query: str, limit: int) -> list[dict]:
    url = (f"{REGISTRY_URL}?search={urllib.parse.quote(query)}"
           f"&limit={max(1, min(50, limit))}")
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        import json

        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload.get("servers") or []


async def search(query: str, limit: int = 12) -> list[dict]:
    """Candidate servers matching a query. Returns [] rather than raising."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        raw = await asyncio.get_running_loop().run_in_executor(
            None, _fetch, query, limit)
    except Exception as e:  # noqa: BLE001
        log.debug("MCP registry search failed for %r: %s", query, e)
        return []

    candidates: dict[str, dict] = {}
    for item in raw:
        candidate = normalise(item)
        if candidate is None:
            continue
        name = candidate["name"]
        kept = candidates.get(name)
        if kept is None:
            candidates[name] = candidate
        elif candidate["latest"] and not kept["latest"]:
            # The registry lists every published version. Keeping whichever
            # came back first installed a release that crashed on startup while
            # a fixed one existed, so the version the registry marks as latest
            # wins, and the highest number breaks ties.
            candidates[name] = candidate
        elif candidate["latest"] == kept["latest"] and \
                _version_key(candidate["version"]) > _version_key(kept["version"]):
            candidates[name] = candidate
    return list(candidates.values())


async def suggest(task: str, limit: int = 8) -> list[dict]:
    """Candidates for a task, most usable first.

    Usable entries come before ones needing a key, because the caller is
    usually looking for something it can start using now.

    The registry matches keywords, not questions - "convert a pdf to text"
    matched nothing while "pdf" matched servers - so a query that finds nothing
    is retried with fewer of its words.
    """
    from backend.skills.market_search import _search_terms

    terms = _search_terms(task)
    candidates: list[dict] = []
    tried: set[str] = set()
    for count in (3, 2, 1):
        query = " ".join(terms[:count]) if terms else task
        if not query or query in tried:
            continue
        tried.add(query)
        candidates = await search(query, limit=limit * 2)
        if candidates:
            if query != task:
                log.debug("MCP market: %r searched as %r (%d hit(s))",
                          task, query, len(candidates))
            break
    if not candidates and task not in tried:
        candidates = await search(task, limit=limit * 2)

    candidates.sort(key=lambda c: (not c["runnable"], not c["latest"],
                                   c["name"]))
    return candidates[:limit]


async def install(name: str, *, trusted: bool = False, auto: bool = False,
                  extra_args: object = None) -> dict:
    """Add a market server by registry name and connect it.

    ``extra_args`` exists because registry entries often leave required
    command-line arguments undeclared: the filesystem servers want a directory
    to serve and say so only on stderr once they start. Without this the entry
    is added and then fails with no way to fix it from the UI.
    """
    from backend.mcp_client.manager import mcp_manager

    candidates = await search(name, limit=20)
    exact = next((c for c in candidates if c["name"] == name), None)
    if exact is None:
        exact = next((c for c in candidates
                      if c["name"].endswith(name) or name.endswith(c["name"])),
                     None)
    if exact is None:
        return {"success": False,
                "error": f"'{name}' is not in the MCP registry"}
    if not exact["runnable"]:
        return {"success": False, "candidate": exact,
                "error": f"'{name}' cannot run yet: {exact['blocked_reason']}"}

    spec = to_spec(exact, trusted=trusted, auto=auto)
    if extra_args:
        import shlex

        try:
            # Quoted paths survive; a bare string is split on whitespace.
            spec["args"] = list(spec["args"]) + shlex.split(str(extra_args))
        except ValueError:
            return {"success": False, "candidate": exact,
                    "error": f"could not read the extra arguments: {extra_args!r}"}
    added = mcp_manager.add(spec)
    if isinstance(added, dict) and added.get("success") is False:
        if "exists" in str(added.get("error") or "").lower():
            # Already configured. Update it in place: pressing Add twice, or
            # adding again with different arguments after a failed first try,
            # has to work rather than be refused as a duplicate.
            added = mcp_manager.update(spec["id"], spec)
        if isinstance(added, dict) and added.get("success") is False:
            return {**added, "candidate": exact}

    result = await mcp_manager.connect(spec["id"])
    return {"success": bool(result.get("success", True)),
            "server_id": spec["id"],
            "candidate": exact,
            "connect": result,
            "status": mcp_manager.status()}


async def acquire_for(task: str) -> dict:
    """Find, add and connect a server that can do ``task``, if one is obvious.

    Only entries that need nothing from the user are taken automatically: a
    server behind an API key would otherwise be added and then fail on every
    call, which is worse than saying no.
    """
    candidates = await suggest(task, limit=8)
    if not candidates:
        return {"success": False, "error": "no MCP server matched that task"}
    usable = [c for c in candidates if c["runnable"]]
    if not usable:
        names = "; ".join(f"{c['name']} ({c['blocked_reason']})"
                          for c in candidates[:3])
        return {"success": False, "candidate": candidates[0],
                "error": f"nothing usable without more setup: {names}"}

    best = usable[0]
    result = await install(best["name"], auto=True)
    result["candidates"] = [c["name"] for c in candidates[:5]]
    return result
