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


def main() -> int:
    from backend.mcp_client import market

    normalise_checks(market)
    search_checks(market)
    suggest_checks(market)
    install_checks(market)
    sweep_checks()
    stderr_checks()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: MCP market normalisation, acquisition and idle switch-off")
    return 0


if __name__ == "__main__":
    sys.exit(main())
