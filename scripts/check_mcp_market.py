"""Checks for the MCP market and the automatic switch-off.

The market turns registry entries into server specs. Two things matter and are
easy to get wrong: an entry that cannot actually run must say so rather than be
added and left to fail on connect, and only servers the agent added itself may
be disconnected when they go idle, so nothing the user configured by hand is
switched off behind their back.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_mcp_market.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


from contextlib import contextmanager


@contextmanager
def temp_settings():
    """Point the settings singleton at a temporary file.

    Credentials are stored through `config.set`, and this machine's settings.json
    is real: a suite must not write a fake key into it.
    """
    import tempfile
    from pathlib import Path

    import backend.config as config_mod

    original_path = config_mod.SETTINGS_PATH
    original_data = config_mod.config._data
    config_mod.SETTINGS_PATH = Path(tempfile.mkdtemp()) / "settings.json"
    config_mod.config._data = dict(config_mod.DEFAULT_SETTINGS)
    config_mod.config._dirty = False
    try:
        yield config_mod
    finally:
        config_mod.SETTINGS_PATH = original_path
        config_mod.config._data = original_data


def npm_entry(name: str = "acme/thing", version: str = "1.2.3",
              env: list | None = None) -> dict:
    package = {
        "registryType": "npm",
        "identifier": "@acme/thing",
        "version": version,
    }
    if env:
        package["environmentVariables"] = [
            {"name": key, "isRequired": True} for key in env]
    return {"server": {"name": name, "title": "Thing", "version": version,
                       "description": "Does a thing", "packages": [package]}}


def remote_entry(name: str = "acme/remote", headers: list | None = None) -> dict:
    remote = {"type": "streamable-http", "url": "https://example.com/mcp"}
    if headers:
        remote["headers"] = [{"name": h} for h in headers]
    return {"server": {"name": name, "title": "Remote", "version": "2.0.0",
                       "description": "A remote thing", "remotes": [remote]}}


def normalise_checks(market) -> None:
    # Pretend only npx is installed, so the result does not depend on this box.
    with mock.patch.object(market, "_runtime_available",
                           lambda launcher: launcher == "npx"):
        plain = market.normalise(npm_entry())
        check("an npm entry becomes a stdio candidate",
              plain and plain["transport"] == "stdio"
              and plain["command"] == "npx", plain and plain["command"])
        check("the version is pinned into the npx argument",
              plain and plain["args"] == ["-y", "@acme/thing@1.2.3"],
              plain and plain["args"])
        check("a plain npm entry is runnable", plain and plain["runnable"] is True)

        with_env = market.normalise(npm_entry(env=["GCS_BUCKET"]))
        check("a required environment variable blocks it",
              with_env and with_env["runnable"] is False
              and with_env["requires_env"] == ["GCS_BUCKET"],
              with_env and with_env["blocked_reason"])
        check("and the reason names the variable",
              "GCS_BUCKET" in (with_env or {}).get("blocked_reason", ""),
              with_env and with_env["blocked_reason"])

        pypi = market.normalise({"server": {
            "name": "acme/py", "title": "Py", "version": "1.0.0",
            "packages": [{"registryType": "pypi", "identifier": "pything",
                          "version": "1.0.0"}]}})
        check("a pypi entry is blocked when uvx is missing",
              pypi and pypi["command"] == "uvx" and pypi["runnable"] is False
              and "uvx" in pypi["blocked_reason"],
              pypi and pypi["blocked_reason"])

        open_remote = market.normalise(remote_entry())
        check("an open remote is runnable",
              open_remote and open_remote["runnable"] is True
              and open_remote["transport"] == "http",
              open_remote and open_remote["blocked_reason"])

        keyed = market.normalise(remote_entry(headers=["Authorization"]))
        check("a remote needing a key is blocked",
              keyed and keyed["runnable"] is False
              and keyed["requires_headers"] == ["Authorization"],
              keyed and keyed["blocked_reason"])

        check("an entry with nothing runnable is dropped",
              market.normalise({"server": {"name": "acme/oci", "packages": [
                  {"registryType": "oci", "identifier": "acme/img"}]}}) is None)
        check("an entry without a name is dropped",
              market.normalise({"server": {"packages": []}}) is None)

        spec = market.to_spec(market.normalise(npm_entry()), auto=True)
        check("the spec carries an auto marker", spec["auto"] is True, spec)
        check("the spec keeps the registry name for later",
              spec["market_name"] == "acme/thing", spec["market_name"])
        # Ids end up in settings.json and in tool names, so they follow the
        # same alphanumeric-underscore convention as hand-added servers.
        check("the spec id is safe to store",
              re.fullmatch(r"[A-Za-z0-9_]+", spec["id"]) is not None,
              spec["id"])
        check("a market spec is not trusted by default",
              spec["trusted"] is False)


def search_checks(market) -> None:
    versions = [npm_entry(version="1.0.0"), npm_entry(version="1.1.0"),
                npm_entry(name="other/pkg", version="2.0.0")]

    with mock.patch.object(market, "_fetch", lambda q, l: versions), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True):
        results = asyncio.run(market.search("thing", limit=10))
    check("one candidate per server, not per version", len(results) == 2,
          [r["name"] for r in results])

    def boom(query, limit):
        raise OSError("network down")

    with mock.patch.object(market, "_fetch", boom):
        failed = asyncio.run(market.search("thing"))
    check("a registry failure returns nothing rather than raising",
          failed == [], failed)

    check("an empty query is not sent", asyncio.run(market.search("  ")) == [])

    # The registry lists every published version. An old release that crashes
    # on startup was installed while a fixed one existed, because whichever
    # came back first was kept.
    def with_latest(version: str, latest: bool) -> dict:
        entry = npm_entry(version=version)
        entry["_meta"] = {
            "io.modelcontextprotocol.registry/official": {"isLatest": latest}}
        return entry

    with mock.patch.object(market, "_fetch",
                           lambda q, l: [with_latest("0.1.0", False),
                                         with_latest("0.1.9", True)]), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True):
        picked = asyncio.run(market.search("thing", limit=10))
    check("the version the registry marks as latest is chosen",
          len(picked) == 1 and picked[0]["version"] == "0.1.9",
          [c["version"] for c in picked])

    # Positive control: flip the marker and the other one must win, so the
    # check above cannot pass just because the list happened to be in order.
    with mock.patch.object(market, "_fetch",
                           lambda q, l: [with_latest("0.1.0", True),
                                         with_latest("0.1.9", False)]), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True):
        flipped = asyncio.run(market.search("thing", limit=10))
    check("and the marker decides it, not list order",
          len(flipped) == 1 and flipped[0]["version"] == "0.1.0",
          [c["version"] for c in flipped])


def suggest_checks(market) -> None:
    calls: list[str] = []
    payload = [npm_entry()]

    def fake_fetch(query, limit):
        calls.append(query)
        # The registry matches keywords: the three-word form finds nothing and
        # the two-word one does, which is the case this has to survive.
        return payload if query == "convert pdf" else []

    with mock.patch.object(market, "_fetch", fake_fetch), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True), \
         mock.patch("backend.skills.market_search._search_terms",
                    lambda q: ["convert", "pdf", "text"]):
        results = asyncio.run(market.suggest("convert a pdf to text"))

    check("a query the registry cannot match is retried with fewer words",
          calls == ["convert pdf text", "convert pdf"], calls)
    check("and the narrower query's results are returned",
          bool(results), results)


async def _resolved(value):
    return value


def install_checks(market) -> None:
    blocked = market.normalise(npm_entry(env=["NEEDED"]))
    with mock.patch.object(market, "_runtime_available", lambda launcher: True), \
         mock.patch.object(market, "search",
                           lambda name, limit=20: _resolved([blocked])):
        out = asyncio.run(market.install("acme/thing"))
    check("a server that cannot run is refused, not added",
          out.get("success") is False and "NEEDED" in str(out.get("error")),
          out.get("error"))

    usable = market.normalise(npm_entry())
    added: list[dict] = []

    class FakeManager:
        def add(self, spec):
            added.append(spec)
            return {"success": True}

        async def connect(self, server_id):
            return {"success": True}

        def status(self):
            return {"servers": []}

    with mock.patch.object(market, "_runtime_available", lambda launcher: True), \
         mock.patch.object(market, "search",
                           lambda name, limit=20: _resolved([usable])), \
         mock.patch("backend.mcp_client.manager.mcp_manager", FakeManager()):
        out = asyncio.run(market.install("acme/thing", auto=True))

    check("a usable server is added and connected",
          out.get("success") is True
          and re.fullmatch(r"[A-Za-z0-9_]+", str(out.get("server_id")))
          is not None,
          out.get("server_id"))
    check("the spec handed to the manager pins the package",
          bool(added) and added[0]["args"] == ["-y", "@acme/thing@1.2.3"], added)

    # Registry entries routinely leave required arguments undeclared - the
    # filesystem servers want a directory and only say so on stderr - so the
    # caller has to be able to supply them.
    added.clear()
    with mock.patch.object(market, "_runtime_available", lambda launcher: True), \
         mock.patch.object(market, "search",
                           lambda name, limit=20: _resolved([usable])), \
         mock.patch("backend.mcp_client.manager.mcp_manager", FakeManager()):
        asyncio.run(market.install("acme/thing", auto=True,
                                   extra_args='--root "/tmp/a b"'))
    check("extra arguments are appended, quotes and all",
          bool(added) and added[0]["args"][-2:] == ["--root", "/tmp/a b"],
          added and added[0]["args"])

    # Pressing Add twice, or re-adding after a failed first attempt, must work.
    updated: list[tuple] = []

    class ExistingManager:
        def add(self, spec):
            return {"success": False,
                    "error": "an MCP server with id 'acme_thing' exists"}

        def update(self, server_id, patch):
            updated.append((server_id, patch))
            return {"success": True}

        async def connect(self, server_id):
            return {"success": True}

        def status(self):
            return {"servers": []}

    with mock.patch.object(market, "_runtime_available", lambda launcher: True), \
         mock.patch.object(market, "search",
                           lambda name, limit=20: _resolved([usable])), \
         mock.patch("backend.mcp_client.manager.mcp_manager", ExistingManager()):
        again = asyncio.run(market.install("acme/thing", auto=True))

    check("re-adding an existing server updates it instead of failing",
          again.get("success") is True and len(updated) == 1, updated)
    check("and the existing configuration is overwritten",
          bool(updated) and updated[0][1]["args"] == ["-y", "@acme/thing@1.2.3"],
          updated)


def sweep_checks() -> None:
    """The automatic switch-off must only ever touch auto servers."""
    from backend.mcp_client.manager import McpManager, McpServerState

    class FakeConfig:
        def __init__(self, minutes: float) -> None:
            self.minutes = minutes

        def get(self, section: str, key: str, default=None):
            if (section, key) == ("mcp", "auto_deactivate_minutes"):
                return self.minutes
            return default

    def sweep(minutes: float):
        manager = McpManager()
        disconnected: list[str] = []
        for sid, auto, idle in (("auto-idle", True, 3600),
                                ("auto-fresh", True, 5),
                                ("manual-idle", False, 3600)):
            item = McpServerState({"id": sid, "auto": auto})
            item.state = "ready"
            item.last_used = time.time() - idle
            manager._servers[sid] = item

        async def fake_disconnect(server_id):
            disconnected.append(server_id)
            manager._servers.pop(server_id, None)
            return {"success": True}

        manager.disconnect = fake_disconnect
        with mock.patch("backend.mcp_client.manager.config",
                        FakeConfig(minutes)):
            result = asyncio.run(manager.sweep_idle())
        return result, disconnected, sorted(manager._servers)

    result, disconnected, remaining = sweep(30)
    check("an idle auto server is disconnected",
          disconnected == ["auto-idle"], disconnected)
    check("a recently used auto server is kept",
          "auto-fresh" in remaining, remaining)
    check("a server the user added is never switched off",
          "manual-idle" not in disconnected, disconnected)
    check("the sweep reports what it did", result.get("swept") == 1, result)

    result, disconnected, remaining = sweep(0)
    check("zero minutes turns the sweep off",
          disconnected == [] and "skipped" in result, result)
    check("and leaves every server connected", len(remaining) == 3, remaining)


def stderr_checks() -> None:
    """A server that dies on startup must say why.

    Its stderr is the only place the reason exists, and without it a failed
    install reads "the server closed the connection" and gives the user nothing
    to act on.
    """
    from backend.mcp_client.manager import McpManager
    from backend.mcp_client.protocol import McpError

    class DyingClient:
        closed = False

        async def start(self):
            raise McpError(-32000, "the server closed the connection")

        async def close(self):
            self.closed = True

        def stderr_tail(self, limit: int = 8) -> str:
            return "Usage: mcp-server-filesystem --allowed-directories <dir>"

    spec = {"id": "s1", "name": "fs", "command": "npx", "args": []}
    manager = McpManager()
    manager._config_servers = lambda: [spec]
    with mock.patch.object(manager, "_build_client",
                           lambda sid, sp: DyingClient()), \
         mock.patch.object(manager, "_register_tools", lambda state: None):
        out = asyncio.run(manager.connect("s1"))

    check("a dead server's own message is kept",
          "Usage:" in str(out.get("error")), out.get("error"))
    check("and the handshake failure is still named",
          "closed the connection" in str(out.get("error")), out.get("error"))
    check("the server is not left claiming to be connected",
          out.get("server", {}).get("state") == "error",
          (out.get("server") or {}).get("state"))


LISTING = ('Add this to your config: {"command": "npx", "args": ["-y", '
           '"reportflow-mcp"]} That is the whole setup. No env vars, no API '
           'keys, no secrets to manage.')


def _smithery_entry(market, payload: dict, available: bool = True):
    with mock.patch.object(market, "_runtime_available",
                           lambda launcher: available):
        return market._smithery_normalise(payload)


def smithery_checks(market) -> None:
    """The one other directory with a usable, key-free API.

    Glama answers 401 without a key, mcp.so answers 500, PulseMCP retired its
    API, and MCP Market and FreeMCPLab publish none - so this is the only other
    source there is. Its listings often embed the launch command; when one does
    not, the entry has to say so rather than be given a guessed launcher.
    """
    command, args, env = market._command_from_text(LISTING)
    check("a launch command is read out of the listing",
          command == "npx" and args == ["-y", "reportflow-mcp"], (command, args))
    check("prose about API keys does not invent an env requirement",
          env == [], env)
    check("an unrelated command is not accepted as a launcher",
          market._command_from_text('{"command": "curl", "args": []}')[0] == "")
    usable = _smithery_entry(market, {"qualifiedName": "acme/tool",
                                      "displayName": "Tool",
                                      "description": LISTING,
                                      "verified": True, "useCount": 12})
    check("a Smithery listing with a command is runnable",
          usable and usable["runnable"] is True
          and usable["source"] == "smithery",
          usable and usable.get("blocked_reason"))
    check("and carries its title and popularity",
          usable and usable["title"] == "Tool" and usable["uses"] == 12,
          usable)

    vague = _smithery_entry(market, {"qualifiedName": "acme/vague",
                                     "displayName": "Vague",
                                     "description": "No config published."})
    check("a listing with no command is blocked, not guessed at",
          vague and vague["runnable"] is False
          and "no launch command" in vague["blocked_reason"],
          vague and vague["blocked_reason"])

    keyed = _smithery_entry(market, {
        "qualifiedName": "acme/keyed", "displayName": "Keyed",
        "description": '{"command": "npx", "args": ["x"], '
                       '"env": {"ACME_KEY": "..."}}'})
    check("an env the listing declares blocks it",
          keyed and keyed["runnable"] is False
          and keyed["requires_env"] == ["ACME_KEY"],
          keyed and keyed["blocked_reason"])

    hosted = _smithery_entry(market, {"qualifiedName": "acme/hosted",
                                     "displayName": "Hosted",
                                     "description": "No config here.",
                                     "remote": True})
    # A listing says it is remote and nothing else, so the missing endpoint is
    # the blocker. The key is asked for once there is an address to send it to,
    # which `hosted_reach_checks` covers.
    check("a hosted Smithery entry reports the missing endpoint",
          hosted and hosted["runnable"] is False
          and "hosted" in hosted["blocked_reason"]
          and "endpoint" in hosted["blocked_reason"],
          hosted and hosted["blocked_reason"])

    # The value of a second source is the servers only it has...
    with mock.patch.object(market, "_fetch", lambda q, l: []), \
         mock.patch.object(market, "_fetch_smithery",
                           lambda q, l: [{"qualifiedName": "smi/only",
                                          "displayName": "Only",
                                          "description": LISTING}]), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True):
        only = asyncio.run(market.search("thing", limit=10))
    check("a server only Smithery has is still offered",
          len(only) == 1 and only[0]["source"] == "smithery",
          [c["source"] for c in only])

    # ...and never overriding the official registry on the same name.
    with mock.patch.object(market, "_fetch",
                           lambda q, l: [npm_entry(name="same/name")]), \
         mock.patch.object(market, "_fetch_smithery",
                           lambda q, l: [{"qualifiedName": "same/name",
                                          "displayName": "Same",
                                          "description": LISTING}]), \
         mock.patch.object(market, "_runtime_available", lambda launcher: True):
        both = asyncio.run(market.search("thing", limit=10))
    check("the registry wins when both carry the same server",
          len(both) == 1 and both[0]["source"] == "registry",
          [c["source"] for c in both])


def credential_checks(market) -> None:
    """A listing that needs a key becomes addable once one is stored.

    This is what the market could not do before: it named the variable and there
    was nowhere to put a value, so those entries were permanently unaddable. The
    gate has to open for a value the user supplied, and stay shut for a server
    blocked on something a value cannot fix.
    """
    import tempfile
    from pathlib import Path

    import backend.config as config_mod
    from backend.mcp_client import credentials

    with temp_settings():
        entry = {
            "name": "acme/keyed", "title": "Keyed", "version": "1.0.0",
            "packages": [{
                "registryType": "npm", "identifier": "keyed-mcp",
                "version": "1.0.0",
                "environmentVariables": [
                    {"name": "ACME_API_KEY", "isRequired": True}],
            }],
        }
        with mock.patch.object(market, "_runtime_available", lambda l: True):
            before = market.normalise(entry)
            check("an entry needing an unheld variable is blocked",
                  before["runnable"] is False
                  and before["blocked_kinds"] == ["env"],
                  str(before.get("blocked_kinds")))
            check("and the reason names the variable",
                  "ACME_API_KEY" in before["blocked_reason"],
                  before["blocked_reason"])
            check("and its spec carries no value",
                  market.to_spec(before)["env"] == {},
                  str(market.to_spec(before)["env"]))

            credentials.save({"ACME_API_KEY": "secret-value"})
            after = market._finish(dict(before))
            check("storing the value makes it runnable",
                  after["runnable"] is True and after["blocked_kinds"] == [],
                  str(after)[:200])
            check("and the value reaches the spec",
                  market.to_spec(after)["env"] == {
                      "ACME_API_KEY": "secret-value"},
                  str(market.to_spec(after)["env"]))
            check("while the declared requirement is left intact",
                  after["requires_env"] == ["ACME_API_KEY"],
                  str(after.get("requires_env")))

        # Only names come back, never values: the dashboard has no business
        # reading a credential it just stored.
        names = credentials.known()
        check("known() reports names only",
              names == {"env": ["ACME_API_KEY"], "headers": [], "params": []},
              str(names))
        check("and a stored value is never returned",
              "secret-value" not in str(names), "a value was echoed")

        # A server blocked for a reason no value can fix stays blocked.
        hosted = market._finish({
            "name": "hosted/thing", "source": "smithery", "transport": "stdio",
            "command": "", "args": [], "requires_env": [],
            "requires_headers": [], "blocked_hint":
                "hosted by Smithery; needs their API key"})
        check("a hosted entry is not unblocked by storing something",
              hosted["runnable"] is False
              and "hosted" in hosted["blocked_kinds"],
              str(hosted.get("blocked_kinds")))

        headers_entry = {
            "name": "acme/remote", "title": "Remote",
            "remotes": [{"url": "https://example.com/mcp",
                         "headers": [{"name": "Authorization"}]}],
        }
        keyed = market.normalise(headers_entry)
        check("a remote needing a header is blocked for a header",
              keyed["runnable"] is False and keyed["blocked_kinds"] == ["headers"],
              str(keyed.get("blocked_kinds")))
        credentials.save(None, {"Authorization": "Bearer xyz"})
        check("and its value reaches the spec's headers",
              market.to_spec(market._finish(dict(keyed)))["headers"]
              == {"Authorization": "Bearer xyz"},
              str(market.to_spec(market._finish(dict(keyed)))["headers"]))

        # An empty string clears, which is the only way to remove one.
        credentials.save({"ACME_API_KEY": ""})
        check("an empty value clears the stored one",
              credentials.known()["env"] == [], str(credentials.known()))


async def keyed_install_checks(market) -> None:
    """install() accepts the values and judges the entry against them."""
    specs: list[dict] = []

    class RecordingManager:
        def add(self, spec):
            specs.append(spec)
            return {"success": True}

        async def connect(self, server_id):
            return {"success": True}

        def status(self):
            return {"servers": []}

    with temp_settings(), \
         mock.patch.object(market, "_runtime_available", lambda l: True), \
         mock.patch.object(market, "search",
                           lambda *a, **k: _keyed_candidates()), \
         mock.patch("backend.mcp_client.manager.mcp_manager",
                    RecordingManager()):
        refused = await market.install("acme/keyed")
        check("without a value, install refuses and says why",
              refused.get("success") is False
              and "ACME_API_KEY" in str(refused.get("error")),
              str(refused)[:200])
        check("and nothing was added", specs == [], str(specs)[:120])

        out = await market.install("acme/keyed",
                                   env={"ACME_API_KEY": "given-now"})
        check("with one supplied, install proceeds",
              out.get("success") is True, str(out)[:200])
        check("and the value reaches the server it was given for",
              bool(specs)
              and specs[-1].get("env", {}).get("ACME_API_KEY") == "given-now",
              str(specs[-1].get("env")) if specs else "nothing added")

        # The whole point of storing it: the next pass — the agent's own —
        # finds the entry addable without being handed the value again.
        again = await market.install("acme/keyed")
        check("and a later pass needs no value at all",
              again.get("success") is True, str(again)[:200])


async def _keyed_candidates():
    """The same listing, judged fresh, with no credentials yet stored."""
    from backend.mcp_client import market
    entry = {
        "name": "acme/keyed", "title": "Keyed", "version": "1.0.0",
        "packages": [{
            "registryType": "npm", "identifier": "keyed-mcp",
            "version": "1.0.0",
            "environmentVariables": [
                {"name": "ACME_API_KEY", "isRequired": True}],
        }],
    }
    return [market.normalise(entry)]


async def smithery_endpoint_checks(market) -> None:
    """A hosted Smithery listing gets the endpoint the registry publishes.

    Smithery's own registry marks a server remote without saying where to reach
    it, so the entry said "hosted by Smithery; needs their API key" and a key had
    nowhere to go. The official registry publishes the same server's endpoint,
    with the header shape — "Bearer {smithery_api_key}" — so the entry becomes an
    ordinary HTTP server that wants an Authorization value.
    """
    from backend.mcp_client import credentials

    twin = market.normalise({
        "name": "ai.smithery/hasdata-duckduckgo-mcp", "title": "DuckDuckGo",
        "remotes": [{
            "type": "streamable-http",
            "url": "https://server.smithery.ai/@hasdata/duckduckgo-mcp/mcp",
            "headers": [{"name": "Authorization",
                         "value": "Bearer {smithery_api_key}",
                         "description": "Bearer token for Smithery authentication"}],
        }],
    })
    check("the published header shape is kept",
          twin["header_hints"].get("Authorization") == "Bearer {smithery_api_key}",
          str(twin.get("header_hints")))

    hosted = market._smithery_normalise({
        "qualifiedName": "hasdata/duckduckgo-mcp", "displayName": "DuckDuckGo MCP",
        "description": "no config block in this listing", "remote": True,
    })
    check("a hosted listing on its own is blocked with no endpoint",
          hosted["blocked_kinds"] == ["hosted"] and not hosted["url"],
          str(hosted.get("blocked_kinds")))

    with mock.patch.object(market, "_registry_candidates", lambda q, l: [twin]), \
         mock.patch.object(market, "_smithery_candidates", lambda q, l: [hosted]):
        hits = await market.search("duckduckgo")
    merged = next((c for c in hits if c["name"] == "hasdata/duckduckgo-mcp"), None)
    check("the hosted entry adopts the published endpoint",
          merged and merged["transport"] == "http"
          and merged["url"].endswith("/mcp"),
          str(merged)[:220] if merged else "entry missing")
    check("and asks for the gateway key as a parameter, not a header",
          merged and merged["blocked_kinds"] == ["params"]
          and merged["requires_params"] == ["api_key"]
          and merged["requires_headers"] == [],
          str(merged.get("blocked_reason")) if merged else "entry missing")
    check("because a bearer token is what that gateway rejects",
          merged and "Authorization" not in (merged.get("header_hints") or {}),
          str(merged.get("header_hints")) if merged else "entry missing")
    check("and it says which entry the endpoint came from",
          merged and merged.get("endpoint_from")
          == "ai.smithery/hasdata-duckduckgo-mcp",
          str(merged.get("endpoint_from")) if merged else "entry missing")

    # With the key stored, it is addable — the whole point of the feature.
    with temp_settings():
        credentials.save(None, None, {"api_key": "sk-test"})
        check("and storing the key makes it addable",
              market._finish(dict(merged or {}))["runnable"] is True,
              str((merged or {}).get("blocked_reason")))
        spec = market.to_spec(merged)
        check("with the key in the spec's query parameters",
              spec["params"].get("api_key") == "sk-test",
              str(spec.get("params")))
        check("and nowhere in its headers",
              "Authorization" not in (spec.get("headers") or {}),
              str(spec.get("headers")))

    # A twin on somebody else's host keeps what the registry declared for it:
    # the parameter form is about Smithery's gateway, not about headers in
    # general.
    other = market.normalise({
        "name": "ai.smithery/acme-own-host", "title": "Own host",
        "remotes": [{"type": "streamable-http",
                     "url": "https://mcp.example.com/mcp",
                     "headers": [{"name": "X-Api-Key",
                                  "value": "opaque value",
                                  "description": "from the provider"}]}],
    })
    elsewhere = market._smithery_normalise({
        "qualifiedName": "acme/own-host", "remote": True, "description": "",
        "isDeployed": True})
    with mock.patch.object(market, "_registry_candidates", lambda q, l: [other]), \
         mock.patch.object(market, "_smithery_candidates", lambda q, l: [elsewhere]):
        hits = await market.search("ownhost")
    hosted_elsewhere = next((c for c in hits if c["name"] == "acme/own-host"), None)
    check("a twin on the provider's own host keeps its declared header",
          bool(hosted_elsewhere)
          and hosted_elsewhere["url"] == "https://mcp.example.com/mcp"
          and hosted_elsewhere["requires_headers"] == ["X-Api-Key"]
          and hosted_elsewhere["requires_params"] == [],
          str(hosted_elsewhere)[:200] if hosted_elsewhere else "entry missing")

    # A listing with no twin in the official registry stays honestly blocked.
    lonely = market._smithery_normalise({
        "qualifiedName": "nobody/private-server", "remote": True,
        "description": "",
    })
    with mock.patch.object(market, "_registry_candidates", lambda q, l: [twin]), \
         mock.patch.object(market, "_smithery_candidates", lambda q, l: [lonely]):
        hits = await market.search("privatethatdoesnotexist")
    kept = next((c for c in hits if c["name"] == "nobody/private-server"), None)
    check("a hosted entry with no published endpoint stays blocked",
          kept and kept["blocked_kinds"] == ["hosted"],
          str(kept.get("blocked_kinds")) if kept else "entry missing")


def _hasdata_listing() -> dict:
    """A hosted listing as Smithery's list endpoint gives it: remote, no more."""
    return {"qualifiedName": "hasdata/duckduckgo-mcp",
            "displayName": "DuckDuckGo MCP Server",
            "description": "DuckDuckGo search as structured JSON. Runs on "
                           "HasData's hosted API.",
            "remote": True, "isDeployed": True}


# What their detail endpoint publishes for it - the shape was copied from a live
# response, including the schema key that says the value is a header.
_HASDATA_DETAIL = {
    "qualifiedName": "hasdata/duckduckgo-mcp",
    "deploymentUrl": "https://duckduckgo-mcp--hasdata.run.tools",
    "connections": [{
        "type": "http",
        "deploymentUrl": "https://duckduckgo-mcp--hasdata.run.tools",
        "configSchema": {
            "type": "object",
            "properties": {
                "x-api-key": {
                    "type": "string",
                    "x-from": {"header": "x-api-key"},
                    "description": "Enter your API key from the HasData dashboard.",
                },
            },
        },
    }],
}


async def _hosted_search(market, listing: dict, record: dict | None) -> list[dict]:
    """Search with only this one listing present, and this one detail record."""
    candidate = market._smithery_normalise(listing)
    with mock.patch.object(market, "_registry_candidates", lambda q, l: []), \
         mock.patch.object(market, "_smithery_candidates",
                           lambda q, l: [candidate]), \
         mock.patch.object(market, "_fetch_smithery_detail", lambda name: record):
        return await market.search("duckduckgo")


async def hosted_reach_checks(market) -> None:
    """A hosted listing becomes reachable with the key the user already has.

    Smithery's registry marks a server remote and publishes neither an address
    nor the values it wants, so "hosted by Smithery" was the end of the road and
    there was nowhere to put a key. Their gateway serves a deployed server at its
    qualified name - a bad bearer answers "Invalid token", not 404 - and their
    detail record names the values the server itself asks for. That is what turns
    the note into a field.
    """
    from backend.mcp_client import credentials

    market._detail_cache.clear()
    hits = await _hosted_search(market, _hasdata_listing(), _HASDATA_DETAIL)
    hit = next((c for c in hits if c["name"] == "hasdata/duckduckgo-mcp"), None)
    check("a deployed hosted listing gets Smithery's gateway address",
          bool(hit) and hit.get("url")
          == "https://server.smithery.ai/hasdata/duckduckgo-mcp/mcp",
          (hit or {}).get("url"))
    check("and asks for the key its own schema declares, beside the gateway's",
          bool(hit) and hit.get("requires_params") == ["api_key"]
          and hit.get("requires_headers") == ["x-api-key"],
          str((hit or {}).get("requires_params")) + " / "
          + str((hit or {}).get("requires_headers")))
    check("the gateway key is sent as a parameter, never as a header",
          bool(hit) and not (hit.get("header_hints") or {})
          and "Authorization" not in (hit.get("requires_headers") or []),
          str((hit or {}).get("header_hints")))
    check("the server's own key is explained in the provider's words",
          bool(hit) and (hit.get("key_help") or {}).get("x-api-key")
          == "Enter your API key from the HasData dashboard.",
          (hit or {}).get("key_help"))
    check("and the gateway key says where to get one",
          bool(hit) and "smithery.ai"
          in (hit.get("key_help") or {}).get("api_key", ""),
          (hit or {}).get("key_help"))
    check("so the card waits on values, not on hosting",
          bool(hit) and hit.get("blocked_kinds") == ["params", "headers"]
          and "hosted" not in (hit or {}).get("blocked_reason", ""),
          (hit or {}).get("blocked_reason"))
    check("and names exactly what it is still waiting for",
          bool(hit) and hit.get("missing_values") == ["api_key", "x-api-key"],
          str((hit or {}).get("missing_values")))

    with temp_settings():
        credentials.save(None, None, {"api_key": "sk-test"})
        check("the gateway key alone is not enough when the server wants its own",
              market._finish(dict(hit or {}))["runnable"] is False,
              str((hit or {}).get("blocked_reason")))
        credentials.save(None, {"x-api-key": "hd-123"})
        ready = market._finish(dict(hit or {}))
        check("both values make it addable", ready["runnable"] is True,
              ready.get("blocked_reason"))
        spec = market.to_spec(ready)
        check("and each reaches the place it belongs",
              spec["params"] == {"api_key": "sk-test"}
              and spec["headers"] == {"x-api-key": "hd-123"},
              str(spec["params"]) + " / " + str(spec["headers"]))
        check("and the card stops asking for what it already holds",
              ready.get("missing_values") == [],
              str(ready.get("missing_values")))

    # A server whose schema declares nothing still needs the gateway key: asked
    # without one, Smithery answers "Missing Authorization header" rather than
    # serving it. The address does not depend on the detail record at all.
    shares = await _hosted_search(market, {"qualifiedName": "mcp-hive/hive-servers",
                                          "displayName": "MCP Hive",
                                          "description": "", "remote": True,
                                          "isDeployed": True}, None)
    hive = next((c for c in shares if c["name"] == "mcp-hive/hive-servers"), None)
    check("a hosted server that declares no values is still asked for the key",
          bool(hive) and hive.get("requires_params") == ["api_key"]
          and hive.get("blocked_kinds") == ["params"],
          str((hive or {}).get("blocked_reason")))
    check("because the address does not need the detail record to exist",
          bool(hive) and hive.get("url")
          == "https://server.smithery.ai/mcp-hive/hive-servers/mcp",
          (hive or {}).get("url"))

    # A server with no slug of its own has just its namespace in the address.
    slim = await _hosted_search(market, {"qualifiedName": "motherduck",
                                        "displayName": "MotherDuck",
                                        "description": "", "remote": True,
                                        "isDeployed": True}, None)
    duck = next((c for c in slim if c["name"] == "motherduck"), None)
    check("a slug-less server still gets an address",
          bool(duck) and duck.get("url")
          == "https://server.smithery.ai/motherduck/mcp",
          (duck or {}).get("url"))

    # Not deployed on their gateway and no host published: honest, as before.
    stranded = await _hosted_search(market, {"qualifiedName": "nobody/private",
                                            "displayName": "Private",
                                            "description": "",
                                            "remote": True}, None)
    stay = next((c for c in stranded if c["name"] == "nobody/private"), None)
    check("a hosted server with nowhere to send it stays blocked",
          bool(stay) and stay.get("blocked_kinds") == ["hosted"]
          and not stay.get("url"),
          (stay or {}).get("blocked_kinds"))

    # A value that belongs in an environment variable is not a header, so no
    # field may be offered for it: it would be sent to the wrong place.
    envonly = await _hosted_search(market, {"qualifiedName": "acme/envonly",
                                           "displayName": "Env", "description": "",
                                           "remote": True, "isDeployed": True},
                                  {"connections": [{"type": "http", "configSchema": {
                                      "properties": {"TOKEN": {
                                          "x-from": {"env": "TOKEN"},
                                          "description": "a variable"}}}}]})
    env_hit = next((c for c in envonly if c["name"] == "acme/envonly"), None)
    check("a value meant for a variable is not shown as a header",
          bool(env_hit) and env_hit.get("requires_headers") == [],
          (env_hit or {}).get("requires_headers"))

def param_checks(market) -> None:
    """A value whose destination is the URL, not a header.

    Smithery's gateway reads its key as `api_key` in the query string and
    refuses the same value as a bearer token, so a credential must be able to
    travel in the URL. Two traps are worth pinning by name: `manager.validate()`
    drops any spec field it does not explicitly list, and the key must be added
    per request rather than written into the spec's url, where it would be
    printed on the market card and in every log line about that server.
    """
    from backend.mcp_client import credentials
    from backend.mcp_client.http import McpHttpClient
    from backend.mcp_client.manager import mcp_manager

    with temp_settings():
        credentials.save(None, None, {"api_key": "sk-param"})
        check("a URL parameter is stored and reported by name",
              credentials.known()["params"] == ["api_key"],
              str(credentials.known()))
        check("and reaches a spec that wants it",
              credentials.subset_params(["api_key"]) == {"api_key": "sk-param"},
              str(credentials.subset_params(["api_key"])))
        check("while a name we hold nothing for stays missing",
              credentials.missing_params(["api_key", "other"]) == ["other"],
              str(credentials.missing_params(["api_key", "other"])))
        credentials.save(None, None, {"api_key": ""})
        check("an empty value clears it",
              credentials.known()["params"] == [], str(credentials.known()))

    clean, error = mcp_manager.validate({
        "name": "Param Server", "transport": "http",
        "url": "https://server.smithery.ai/acme/thing/mcp",
        "params": {"api_key": "sk-param"}})
    check("the spec keeps its query parameters through validation",
          error is None and (clean or {}).get("params") == {"api_key": "sk-param"},
          str(error or clean))

    client = McpHttpClient("sid", {
        "url": "https://server.smithery.ai/acme/thing/mcp",
        "params": {"api_key": "sk-param"}, "headers": {"X-A": "1"}})
    check("the address a spec shows stays free of the key",
          client.url == "https://server.smithery.ai/acme/thing/mcp", client.url)
    check("and the request URL carries the parameter",
          client._target()
          == "https://server.smithery.ai/acme/thing/mcp?api_key=sk-param",
          client._target())
    check("while headers still travel as headers",
          client._headers().get("X-A") == "1"
          and "api_key" not in client._headers(),
          str(sorted(client._headers())))
    existing = McpHttpClient("sid", {"url": "https://x.test/mcp?keep=1",
                                     "params": {"api_key": "k"}})
    check("a parameter is appended to an address that already has one",
          existing._target() == "https://x.test/mcp?keep=1&api_key=k",
          existing._target())

    # The upgrade path. A key the old market collected as a bearer header has to
    # end up where the gateway actually reads it, or every server added before
    # the fix keeps answering "401 invalid token" while holding a good key.
    import copy

    from backend.config import DEFAULT_SETTINGS, _Config

    with temp_settings():
        probe = _Config()
        probe._data = copy.deepcopy(DEFAULT_SETTINGS)
        probe._data["mcp"]["credentials"] = {
            "env": {}, "headers": {"Authorization": "Bearer sk-old"}, "params": {}}
        probe._migrate()
        stored = probe._data["mcp"]["credentials"]
        check("an upgrade moves the old bearer into the api_key parameter",
              stored["params"].get("api_key") == "sk-old", str(stored))
        check("and leaves the header alone for whatever else may use it",
              stored["headers"].get("Authorization") == "Bearer sk-old",
              str(stored["headers"]))
        probe._migrate()
        check("running it again changes nothing",
              probe._data["mcp"]["credentials"]["params"]["api_key"] == "sk-old",
              str(probe._data["mcp"]["credentials"]["params"]))


async def detail_cache_checks(market) -> None:
    """Their detail endpoint is asked once per server, not once per search.

    `install` re-searches by name, so a host that has already answered must not
    be asked again - and neither must it be asked for every keystroke in the
    search box. The network is what is faked here rather than the function:
    patching `_fetch_smithery_detail` would replace the cache with the stub and
    prove nothing about it, which is exactly what the first version of this
    check did.
    """
    market._detail_cache.clear()
    asked: list[str] = []

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._body = json.dumps(payload).encode()

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def fake_urlopen(request, timeout=None):
        asked.append(str(getattr(request, "full_url", request)))
        return FakeResponse(_HASDATA_DETAIL)

    with mock.patch.object(market, "_registry_candidates", lambda q, l: []), \
         mock.patch.object(market, "_smithery_candidates",
                           lambda q, l: [market._smithery_normalise(_hasdata_listing())]), \
         mock.patch.object(market.urllib.request, "urlopen", fake_urlopen):
        first = await market.search("duckduckgo")
        await market.search("duckduckgo")
    check("the detail record is fetched once and then remembered",
          len(asked) == 1 and "hasdata/duckduckgo-mcp" in asked[0], asked)
    check("and the remembered record is the one that was used",
          bool(first) and first[0].get("key_help", {}).get("x-api-key")
          == "Enter your API key from the HasData dashboard.",
          first and first[0].get("key_help"))


def main() -> int:
    from backend.mcp_client import market

    # Nothing here may touch the network: the sources are stubbed, and the
    # checks that are about a source patch it themselves. The detail endpoint is
    # stubbed too - it is one call per hosted listing, and a suite that reached
    # for a live host would be slow, flaky and dependent on someone else's
    # uptime.
    with mock.patch.object(market, "_fetch", lambda q, l: []), \
         mock.patch.object(market, "_fetch_smithery", lambda q, l: []), \
         mock.patch.object(market, "_fetch_smithery_detail", lambda name: None):
        normalise_checks(market)
        search_checks(market)
        suggest_checks(market)
        install_checks(market)
        sweep_checks()
        stderr_checks()
        smithery_checks(market)
        credential_checks(market)
        asyncio.run(keyed_install_checks(market))
        asyncio.run(smithery_endpoint_checks(market))
        asyncio.run(hosted_reach_checks(market))
        param_checks(market)
    # Deliberately outside the stubs above: this check has to run the real
    # function to observe its cache, so it fakes the network itself instead.
    asyncio.run(detail_cache_checks(market))
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: MCP market normalisation, acquisition and idle switch-off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
