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
import json
import logging
import shutil
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("addled.mcp.market")

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
# Smithery's own registry: public, key-free and keyword searchable, which is not
# true of the other directories people ask about. Glama's API answers 401
# without a key, mcp.so answers 500, PulseMCP retired its API (410 Gone) and now
# publishes into the official registry instead, and MCP Market and FreeMCPLab
# serve no API at all - only HTML, which is not something to build on.
SMITHERY_URL = "https://registry.smithery.ai/servers"
# Their gateway, which is where a *hosted* server actually answers. The route is
# the qualified name, and a call with a bad token answers "Invalid token" rather
# than 404 - so the path is theirs, for every server they deploy.
SMITHERY_GATEWAY = "https://server.smithery.ai"
UA = {"User-Agent": "Addled/1.0 (mcp market)", "Accept": "application/json"}
TIMEOUT = 15.0

# Hosted servers are reached through that gateway, which authenticates the
# caller with the user's own Smithery key sent as a QUERY PARAMETER. Not as a
# header: the official registry publishes `Authorization: Bearer
# {smithery_api_key}` for these endpoints, and sending that is a guaranteed
# "401 invalid_token" - measured against five hosted servers and both URL forms
# (with and without the leading `@`). A server whose config schema declares
# nothing still needs the key.
SMITHERY_KEY_PARAM = "api_key"
SMITHERY_KEY_HELP = ("Your Smithery API key. Hosted servers are reached through "
                     "Smithery's gateway, which takes it as the api_key "
                     "parameter. smithery.ai -> Account -> API keys")

# One detail record per hosted listing is wanted per search, so twelve results
# must not cost twelve round trips in series - and a re-search, which `install`
# does, must not pay for them again.
_DETAIL_TTL = 900.0
_DETAIL_MISS_TTL = 60.0
_DETAIL_WORKERS = 6
_detail_cache: dict[str, tuple[float, dict | None]] = {}

# package registryType -> (launcher, is it installed here)
_LAUNCHERS = {
    "npm": ("npx", ["-y"]),
    "pypi": ("uvx", []),
}


def _runtime_available(launcher: str) -> bool:
    if shutil.which(launcher):
        return True
    # `uvx` is on almost nothing. Addled can install it, and when it has, a
    # server launched with it runs even though a plain shell would not find it:
    # stdio.py puts that directory on PATH for the child process.
    if launcher in ("uv", "uvx"):
        try:
            from backend.tools import uv
            return bool(uv.find(f"{launcher}.exe"))
        except Exception as e:  # noqa: BLE001
            log.debug("Could not consult the uv installer: %s", e)
    return False


def _clean_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""


def _gateway_url(name: str) -> str:
    """Smithery's own address for a hosted server, from its qualified name."""
    path = "/".join(part for part in str(name or "").split("/") if part)
    return f"{SMITHERY_GATEWAY}/{path}/mcp" if path else ""


def _is_gateway(url: str) -> bool:
    """Whether Smithery's own gateway serves this endpoint.

    It does not authenticate the way their registry says it does: `api_key` in
    the query string is accepted, `Authorization: Bearer <that same key>` is
    not. Who serves the endpoint therefore decides how the key is sent.
    """
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return False
    return host.lower() == "server.smithery.ai"


def _fetch_smithery_detail(name: str) -> dict | None:
    """What a Smithery listing declares about itself.

    The list endpoint says a server is remote and nothing more, which is why
    these cards could only report that and stop. The detail endpoint publishes
    the deployment *and* a config schema naming every value the server wants, in
    the server's own words ("Enter your API key from the HasData dashboard") -
    the only place that is ever said.
    """
    now = time.monotonic()
    cached = _detail_cache.get(name)
    if cached and cached[0] > now:
        return cached[1]
    record: dict | None = None
    try:
        url = f"{SMITHERY_URL}/{urllib.parse.quote(name)}"
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA),
                timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        record = payload if isinstance(payload, dict) else None
    except Exception as e:  # noqa: BLE001
        log.debug("Smithery had no detail for %r: %s", name, e)
    # A miss is remembered briefly: it is the lasting answer for a server that is
    # simply not there, and re-asking on every search would be rude to a host
    # that has already declined.
    _detail_cache[name] = (now + (_DETAIL_TTL if record else _DETAIL_MISS_TTL),
                           record)
    return record


def _http_schema(record: dict) -> dict:
    """The config schema of the connection Smithery would serve over HTTP."""
    for connection in record.get("connections") or []:
        if not isinstance(connection, dict):
            continue
        if str(connection.get("type") or "").lower() == "http":
            schema = connection.get("configSchema")
            if isinstance(schema, dict):
                return schema
    schema = record.get("configSchema")
    return schema if isinstance(schema, dict) else {}


def _declared_keys(schema: object) -> tuple[list[str], dict[str, str]]:
    """The headers a server's config schema asks for, and what each one is.

    Every property says where its value belongs - ``x-from: {"header":
    "x-api-key"}`` - and describes it in the provider's own words, which is what
    tells a user that this key is HasData's and not Smithery's. A schema with no
    ``required`` list still means the values are wanted: that is how the
    DuckDuckGo entry reads, and its server refuses a call without one.
    """
    properties = (schema or {}).get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return [], {}
    names: list[str] = []
    help_text: dict[str, str] = {}
    for spec in properties.values():
        spec = spec if isinstance(spec, dict) else {}
        source = spec.get("x-from")
        header = ""
        if isinstance(source, dict):
            header = str(source.get("header") or "").strip()
        if not header:
            continue
        if header not in names:
            names.append(header)
        text = " ".join(str(spec.get("description") or "").split())
        if text:
            help_text[header] = text
    return names, help_text


def _pick_remote(entry: dict) -> dict | None:
    for remote in entry.get("remotes") or []:
        if not isinstance(remote, dict):
            continue
        url = _clean_url(remote.get("url"))
        if not url:
            continue
        declared = [h for h in (remote.get("headers") or [])
                    if isinstance(h, dict) and h.get("name")]
        # The value is usually a template — Smithery publishes
        # "Bearer {smithery_api_key}" — and it is the only thing that says what
        # shape the credential has. Without it the field accepts a bare key and
        # the connection fails with a 401 nobody can explain.
        hints = {str(h.get("name")).strip():
                 str(h.get("value") or h.get("description") or "").strip()
                 for h in declared}
        return {"url": url,
                "requires_headers": [str(h.get("name")).strip()
                                     for h in declared],
                "header_hints": hints}
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


def _finish(candidate: dict, hint: str = "") -> dict:
    """Decide whether an entry can be used, and say why not if it cannot.

    The env/header reasons are recomputed from the declared names every time,
    minus whatever values are already held, so storing a key is enough to turn a
    blocked entry into an addable one — no re-fetch, and the agent's automatic
    pass sees exactly what the user sees.
    """
    from backend.mcp_client import credentials

    # Kept on the candidate so a later recompute (after a key is saved) still
    # knows this one was hosted rather than merely undocumented.
    if hint:
        candidate["blocked_hint"] = hint
    hint = str(candidate.get("blocked_hint") or "")

    reasons: list[str] = []
    # Why it is blocked, as categories: the dashboard needs to tell "waiting for
    # a value you can type" from "nothing you can do here" so it can offer the
    # input and enable the button. Reason strings are for reading, these are for
    # deciding.
    kinds: list[str] = []
    if hint:
        reasons.append(hint)
        kinds.append("hosted")
    elif candidate.get("transport") == "stdio":
        launcher = str(candidate.get("command") or "")
        if not launcher:
            reasons.append("no launch command published")
            kinds.append("command")
        elif not _runtime_available(launcher):
            reasons.append(
                f"needs '{launcher}' on PATH"
                + (" (install Node.js)" if launcher == "npx"
                   else " (install uv)" if launcher == "uvx" else ""))
            kinds.append("runtime")
    missing_params = credentials.missing_params(
        candidate.get("requires_params"))
    if missing_params:
        reasons.append("needs " + ", ".join(missing_params))
        kinds.append("params")
    missing_env = credentials.missing_env(candidate.get("requires_env"))
    if missing_env:
        reasons.append("needs " + ", ".join(missing_env))
        kinds.append("env")
    missing_headers = credentials.missing_headers(
        candidate.get("requires_headers"))
    if missing_headers:
        reasons.append("needs an API key for " + ", ".join(missing_headers))
        kinds.append("headers")
    candidate["blocked_reason"] = "; ".join(reasons)
    candidate["blocked_kinds"] = kinds
    # Which names are still missing, so a card can offer a field for those and
    # only those. A value already held does not need asking for again, and
    # showing the box anyway reads as "this is still not set up".
    candidate["missing_values"] = missing_params + missing_headers + missing_env
    candidate["runnable"] = not reasons
    return candidate


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
        "source": "registry",
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
        "requires_params": [],
        "header_hints": {},
        "runnable": False,
        "blocked_reason": "",
        "blocked_kinds": [],
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
            "header_hints": remote.get("header_hints") or {},
            "runtime": "remote",
        })
    else:
        return None

    # Why it cannot be used yet, if it cannot.
    return _finish(candidate)


def to_spec(candidate: dict, *, trusted: bool = False,
            auto: bool = False) -> dict:
    """The server definition to hand to the manager.

    Required env vars and headers are filled from the stored credentials, which
    is what lets this be one path for the user's click and the agent's automatic
    install: by the time a spec is built, the values are already held.
    """
    from backend.mcp_client import credentials
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
        "env": credentials.subset_env(candidate.get("requires_env")),
        "headers": credentials.subset_headers(
            candidate.get("requires_headers")),
        "params": credentials.subset_params(
            candidate.get("requires_params")),
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
    """Registry entries for a query, with one retry.

    Its search endpoint is slow and does time out. A single retry costs nothing
    a user notices and keeps the whole source out of the bin — dropping it
    silently is what made a search look like it contained nothing but
    Smithery-hosted servers, and took away the endpoints some of those entries
    need (see the adoption in `search`).
    """
    url = (f"{REGISTRY_URL}?search={urllib.parse.quote(query)}"
           f"&limit={max(1, min(50, limit))}")
    request = urllib.request.Request(url, headers=UA)
    last: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8",
                                                            errors="replace"))
            return payload.get("servers") or []
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 0:
                time.sleep(0.8)
    # Warning rather than debug: a source that failed changes what the user sees,
    # and "why is every result blocked" is otherwise unanswerable from the log.
    log.warning("The MCP registry did not answer for %r: %s", query, last)
    raise last


def _registry_candidates(query: str, limit: int) -> list[dict]:
    """Registry entries for a query, one candidate per server."""
    best: dict[str, dict] = {}
    for item in _fetch(query, limit):
        candidate = normalise(item)
        if candidate is None:
            continue
        name = candidate["name"]
        kept = best.get(name)
        if kept is None:
            best[name] = candidate
        elif candidate["latest"] and not kept["latest"]:
            # The registry lists every published version. Keeping whichever
            # came back first installed a release that crashed on startup while
            # a fixed one existed, so the version the registry marks as latest
            # wins, and the highest number breaks ties.
            best[name] = candidate
        elif candidate["latest"] == kept["latest"] and \
                _version_key(candidate["version"]) > _version_key(kept["version"]):
            best[name] = candidate
    return list(best.values())


def _fetch_smithery(query: str, limit: int) -> list[dict]:
    url = (f"{SMITHERY_URL}?q={urllib.parse.quote(query)}"
           f"&pageSize={max(1, min(50, limit))}")
    request = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    return payload.get("servers") or []


_LAUNCH_COMMANDS = ("npx", "uvx", "node", "python", "python3")


def _command_from_text(text: object) -> tuple[str, list[str], list[str]]:
    """Pull a launch command out of a listing that embeds one.

    Smithery publishes the client config inside the description - literally
    ``"command": "npx", "args": ["-y", "reportflow-mcp"]``, sometimes followed
    by "no env vars, no API keys". Returns (command, args, required_env); an
    empty command means nothing usable was published, and the entry is then
    reported as needing manual setup rather than being given an invented
    launcher.
    """
    import re

    block = str(text or "").replace("\\n", "\n")
    found = re.search(r'"command"\s*:\s*"([^"]+)"', block)
    if not found:
        return "", [], []
    command = found.group(1).strip()
    if command not in _LAUNCH_COMMANDS:
        return "", [], []

    args: list[str] = []
    args_block = re.search(r'"args"\s*:\s*\[([^\]]*)\]', block)
    if args_block:
        args = [a for a in re.findall(r'"([^"]*)"', args_block.group(1))
                if a.strip()]

    env: list[str] = []
    env_block = re.search(r'"env"\s*:\s*\{([^}]*)\}', block)
    if env_block:
        env = [k for k in re.findall(r'"([^"]+)"\s*:', env_block.group(1))
               if k.strip()]
    return command, args, env


def _smithery_normalise(item: dict) -> dict | None:
    """Turn one Smithery listing into a candidate."""
    if not isinstance(item, dict):
        return None
    name = str(item.get("qualifiedName") or "").strip()
    if not name:
        return None
    command, args, required_env = _command_from_text(item.get("description"))
    # Most Smithery servers are hosted on their gateway, and their listings say
    # so only by being remote. Until the endpoint is worked out (see
    # `_adopt_hosted`), the honest thing to report is the missing endpoint -
    # the key is asked for once there is somewhere to send it.
    hint = ""
    if not command and item.get("remote"):
        hint = "hosted by Smithery; no endpoint published for it"
    return _finish({
        "name": name,
        "source": "smithery",
        "title": str(item.get("displayName") or "").strip(),
        "description": " ".join(str(item.get("description") or "").split())[:400],
        "version": "",
        "repository": str(item.get("homepage") or ""),
        "transport": "stdio",
        "command": command,
        "args": args,
        "url": "",
        "requires_env": sorted(required_env),
        "requires_headers": [],
        "requires_params": [],
        "header_hints": {},
        "runtime": command,
        # Whether Smithery actually deploys it, which is what lets the entry be
        # given their gateway address without asking about it again.
        "deployed": bool(item.get("isDeployed")),
        "verified": bool(item.get("verified")),
        "uses": int(item.get("useCount") or 0),
        "latest": True,
    }, hint)


def _smithery_candidates(query: str, limit: int) -> list[dict]:
    out: dict[str, dict] = {}
    for item in _fetch_smithery(query, limit):
        candidate = _smithery_normalise(item)
        if candidate is not None:
            out.setdefault(candidate["name"], candidate)
    return list(out.values())


def _hosted_updates(candidate: dict, twin: dict | None,
                    record: dict | None) -> dict | None:
    """What a hosted entry needs to become an ordinary HTTP server.

    Returns None when there is nowhere to send it, which is the only case where
    "hosted" is the honest answer.
    """
    record = record or {}
    if not (twin and twin.get("transport") == "http" and twin.get("url")):
        twin = None

    # The official registry publishes the endpoint outright, so it wins over
    # anything derived here.
    url = _clean_url(twin.get("url")) if twin else ""
    if not url and candidate.get("deployed"):
        url = _gateway_url(str(candidate.get("name") or ""))
    if not url:
        # Not deployed on their gateway: their detail record may still name a
        # host, which is the only other place it can be.
        url = _clean_url(record.get("deploymentUrl"))
    if not url:
        return None

    headers: list[str] = []
    params: list[str] = []
    hints: dict[str, str] = {}
    help_text: dict[str, str] = {}
    origin = twin["name"] if twin else ""
    if _is_gateway(url):
        # Their gateway, their key, as the parameter it reads. Whatever header
        # the registry declared for this endpoint is dropped, because a bearer
        # token is exactly what it rejects.
        params = [SMITHERY_KEY_PARAM]
        help_text[SMITHERY_KEY_PARAM] = SMITHERY_KEY_HELP
        origin = origin or "smithery"
    elif twin:
        # Somebody else's host, so trust what the registry declared for it.
        headers = list(twin.get("requires_headers") or [])
        hints = dict(twin.get("header_hints") or {})
        help_text = dict(twin.get("key_help") or {})

    # A hosted server may still want its own provider key beside that, which its
    # config schema names in the provider's own words.
    declared, declared_help = _declared_keys(_http_schema(record))
    for name in declared:
        if name not in headers:
            headers.append(name)
    help_text.update(declared_help)
    return {"url": url, "requires_headers": headers, "requires_params": params,
            "header_hints": hints, "key_help": help_text,
            "endpoint_from": origin}


def _adopt_hosted(candidates: list[dict], official: dict[str, dict]) -> None:
    """Give every "hosted, no way in" entry an endpoint and a place for its key.

    A Smithery listing marks a server remote and says nothing else, so these
    cards could only report that and stop. Two things are knowable: their gateway
    serves a deployed server at its qualified name, and their detail endpoint
    says which values the server itself wants. The gateway holds for every
    listing, so a detail call that fails costs the extra keys but never leaves
    the card dead.
    """
    hosted = [c for c in candidates if "hosted" in (c.get("blocked_kinds") or [])]
    if not hosted:
        return
    records: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as pool:
        for candidate, record in zip(hosted, pool.map(
                lambda c: _fetch_smithery_detail(c["name"]), hosted)):
            records[candidate["name"]] = record

    for candidate in hosted:
        twin = official.get(f"ai.smithery/{candidate['name'].replace('/', '-')}")
        updates = _hosted_updates(candidate, twin, records.get(candidate["name"]))
        if not updates:
            continue
        candidate.update({"transport": "http", "runtime": "remote", **updates})
        candidate.pop("blocked_hint", None)
        # Re-judged rather than assumed: with the endpoint in place the missing
        # values are now the only thing standing between this and a click.
        _finish(candidate)


async def search(query: str, limit: int = 12,
                 sources: tuple[str, ...] = ("registry", "smithery")) -> list[dict]:
    """Candidate servers matching a query, from every source that has one."""
    query = (query or "").strip()
    if not query:
        return []

    loop = asyncio.get_running_loop()
    jobs = []
    if "registry" in sources:
        jobs.append(loop.run_in_executor(None, _registry_candidates, query, limit))
    if "smithery" in sources:
        jobs.append(loop.run_in_executor(None, _smithery_candidates, query, limit))
    batches = await asyncio.gather(*jobs, return_exceptions=True)

    merged: dict[str, dict] = {}
    for batch in batches:
        if isinstance(batch, Exception):
            log.debug("MCP market source failed for %r: %s", query, batch)
            continue
        for candidate in batch:
            # First source listed wins a name clash, so the official registry
            # stays authoritative when both carry the same server.
            merged.setdefault(candidate["name"], candidate)

    official = {c["name"]: c for c in merged.values()
                if c.get("source") == "registry"}
    _adopt_hosted(list(merged.values()), official)

    return list(merged.values())


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
                  extra_args: object = None, env: object = None,
                  headers: object = None, params: object = None) -> dict:
    """Add a market server by registry name and connect it.

    ``extra_args`` exists because registry entries often leave required
    command-line arguments undeclared: the filesystem servers want a directory
    to serve and say so only on stderr once they start. Without this the entry
    is added and then fails with no way to fix it from the UI.

    ``env``, ``headers`` and ``params`` are the values for the variables the
    listing declares, named the way the listing names them. They are stored
    first, so an entry that was blocked only for want of a key becomes addable —
    and stays addable for the agent's own pass later, and for any other listing
    that wants the same variable.
    """
    from backend.mcp_client import credentials
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

    if env or headers or params:
        credentials.save(env, headers, params)
        _finish(exact)

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
