"""Importing MCP servers other tools have already been told about.

What this covers, and why each part earned a check:

* **All three tools are read.** Cursor and VS Code keep `mcpServers`/`servers`
  in a project file; Claude Code keeps one global section and one per project
  in `~/.claude.json`. All four shapes appear in the wild, and a reader that
  handles three of them silently loses a quarter of a user's servers.
* **`${env:NAME}` is expanded, and a missing one is named.** A config that
  references a variable nobody set must say so rather than import a server that
  connects and fails — the failure would look like Addled's.
* **A secret from another tool's file never comes back.** Those headers are
  credentials; they are stored encrypted and the scan result reports names only.
  A scan result goes over a socket to a dashboard, so anything in it is shown.
* **`~/.claude.json` is not read twice.** It is one file holding a global
  section and per-project ones; the project walk must not wander into it, or a
  server appears under a project's name as well as its own.
* **An `sse` server is imported and then refused, not silently turned into
  HTTP.** That coercion was F10, and an import path is where it would creep
  back in.
* **An import is converted through the same `validate` as a typed server.**
  If it were not, an import could be a way to smuggle in a field the user
  cannot see or edit.

Every file is written into a temporary directory used as that tool's home, so
these run without touching the real ones.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

fails: list[str] = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def _fixture(base: Path) -> None:
    """A home directory holding one config per tool, with the real shapes."""
    (base / ".cursor").mkdir(parents=True, exist_ok=True)
    (base / ".vscode").mkdir(parents=True, exist_ok=True)
    (base / ".cursor" / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            "filesystem": {"command": "npx",
                           "args": ["-y", "@modelcontextprotocol/server-filesystem"]},
            "remote-thing": {
                "url": "https://mcp.example.com/v1",
                "headers": {"Authorization": "Bearer cursor-canary-9977"}},
        }}), encoding="utf-8")
    (base / ".vscode" / "mcp.json").write_text(json.dumps({
        "servers": {
            "vscode-fs": {"type": "stdio", "command": "uvx",
                          "args": ["mcp-server-git"]},
            "vscode-remote": {"type": "http", "url": "https://vs.example/mcp"},
            "vscode-sse": {"type": "sse", "url": "https://sse.example/mcp"},
        }}), encoding="utf-8")
    (base / ".claude.json").write_text(json.dumps({
        "mcpServers": {"global-one": {"command": "node", "args": ["g.js"]}},
        "projects": {"P": {"mcpServers": {
            "proj-needs-env": {"command": "node", "args": ["p.js"],
                               "env": {"API_KEY": "${env:IMPORT_CANARY_MISSING}"}},
            "global-one": {"command": "node", "args": ["g-again.js"]},
        }}}}), encoding="utf-8")


def _with_fixture(fn):
    """Run `fn(base)` against a throwaway home directory."""
    base = Path(tempfile.mkdtemp(prefix="addled-mcp-import-"))
    try:
        _fixture(base)
        with mock.patch.object(Path, "home", staticmethod(lambda: base)):
            return fn(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_scan_reads_every_tool():
    """All three tools, and all four config shapes, are read."""
    from backend.mcp_client import configimport

    def run(base):
        out = configimport.scan()
        names = {f["name"] for f in out["found"]}
        check("a Cursor project file is read", "filesystem" in names, str(names))
        check("a VS Code file using `servers` is read",
              "vscode-fs" in names, str(names))
        check("Claude Code's global section is read",
              "global-one" in names, str(names))
        check("Claude Code's per-project section is read",
              "proj-needs-env" in names, str(names))
        check("every tool that was found is reported as a source",
              {s["source"] for s in out["sources"]} >= {"cursor", "vscode",
                                                       "claude"},
              str(out["sources"]))

        # The transports have to survive, or `remote-thing` would be imported
        # as a stdio server with a URL for a command.
        by_name = {f["name"]: f for f in out["found"]}
        check("a command becomes a stdio spec",
              by_name["filesystem"]["transport"] == "stdio",
              str(by_name["filesystem"]))
        check("a url becomes an http spec",
              by_name["remote-thing"]["transport"] == "http",
              str(by_name["remote-thing"]))
        check("the command and its arguments are both kept",
              by_name["filesystem"]["command"] == "npx"
              and by_name["filesystem"]["args"] ==
              ["-y", "@modelcontextprotocol/server-filesystem"],
              str(by_name["filesystem"]))
        check("an sse server keeps its declared transport, not http",
              by_name["vscode-sse"]["transport"] == "sse",
              str(by_name["vscode-sse"]))

    _with_fixture(run)


def test_env_placeholders_are_expanded_and_named():
    """`${env:NAME}` expands; a missing variable is named, not guessed."""
    import os
    from backend.mcp_client import configimport

    def run(base):
        os.environ["IMPORT_CANARY_SET"] = "the-real-value"
        try:
            entry = {"command": "node",
                     "args": ["--token=${env:IMPORT_CANARY_SET}",
                              "--other=${env:IMPORT_CANARY_MISSING}"],
                     "env": {"KEY": "${env:IMPORT_CANARY_MISSING}"}}
            spec, error = configimport._server_from_entry("t", entry, "cursor")
            check("a placeholder is expanded from the environment",
                  error == "" and "--token=the-real-value" in spec["args"],
                  f"{error} {spec}")
            check("a missing variable is reported by name",
                  spec["_missing"] == ["IMPORT_CANARY_MISSING"],
                  str(spec.get("_missing")))
            check("a missing variable is not filled with a placeholder",
                  not any("IMPORT_CANARY_MISSING" in a for a in spec["args"]),
                  str(spec["args"]))
        finally:
            os.environ.pop("IMPORT_CANARY_SET", None)

        # And it is surfaced on the scan result, where a card can act on it.
        out = configimport.scan()
        needs = [f for f in out["found"] if f["name"] == "proj-needs-env"]
        check("the scan reports which variables are missing",
              needs and needs[0]["missing"] == ["IMPORT_CANARY_MISSING"],
              str(needs))

    _with_fixture(run)


def test_a_secret_from_another_file_never_comes_back():
    """Another tool's header value is a secret, and does not leave the module.

    The scan result is sent to the dashboard and drawn on a card. A key taken
    from Cursor's file is exactly as secret as one the user typed, so the rule
    is the same: the names are reported, the values are not.
    """
    from backend.config import config
    from backend.mcp_client import configimport, credentials

    saved_creds = config.get("mcp", "credentials", default={})
    saved_servers = config.get("mcp", "servers", default=[])

    def run(base):
        try:
            config.set("mcp", "servers", value=[])
            out = configimport.scan()
            check("a header value from another file is not returned",
                  "cursor-canary-9977" not in json.dumps(out),
                  "the value appears in the scan result")
            check("and no private field is returned either",
                  not any(k.startswith("_") for f in out["found"] for k in f),
                  str([list(f) for f in out["found"]])[:200])
            entry = [f for f in out["found"] if f["name"] == "remote-thing"]
            check("but the header name is reported, so a card can show it",
                  entry and entry[0]["header_names"] == ["Authorization"],
                  str(entry))

            # Importing it stores the value as a credential, encrypted — so the
            # server definition carries no key at all.
            specs, errors = configimport.make_specs({"remote_thing": True})
            check("the import produced no error", errors == [], str(errors))
            check("the imported server carries no header value",
                  specs and not specs[0].get("headers"),
                  str(specs))
            check("the value went into the credential store",
                  credentials.known()["headers"] == ["Authorization"],
                  str(credentials.known()["headers"]))
            check("and it is encrypted at rest",
                  "cursor-canary-9977" not in
                  json.dumps(config.get("mcp", "credentials", default={})),
                  "the imported secret is readable in the config file")
        finally:
            config.set("mcp", "servers", value=saved_servers)
            config.set("mcp", "credentials", value=saved_creds)

    _with_fixture(run)


def test_one_file_is_not_read_twice():
    """A server named in one file appears once, not once per section.

    `~/.claude.json` holds a global `mcpServers` and a `projects` map, and the
    project walk looks in parent directories. Reaching the home file that way
    would list the global servers again under a project's identity — and an
    import of the second copy would overwrite the first user's settings.
    """
    from backend.mcp_client import configimport

    def run(base):
        out = configimport.scan()
        names = [f["name"] for f in out["found"]]
        check("a name in both the global and a project section appears once",
              names.count("global-one") == 1,
              f"appeared {names.count('global-one')} times: {names}")
        ids = [f["id"] for f in out["found"] if f.get("id")]
        check("no id is offered twice",
              len(ids) == len(set(ids)), str(ids))
        # The global section is the one that wins, because it is read first and
        # is the one the user thinks of as their setting.
        winner = [f for f in out["found"] if f["name"] == "global-one"][0]
        check("the global definition is the one kept",
              winner["args"] == ["g.js"], str(winner))
        # And the file is not reported twice as a source.
        paths = [s["path"] for s in out["sources"]]
        check("a config file is read once",
              len(paths) == len(set(paths)), str(paths))

        # The project walk looks in parent directories, and the home directory
        # is an ancestor of anything under it. Reading from a nested path must
        # not reach the global file a second time.
        nested = base / "projects" / "deep" / "deeper"
        nested.mkdir(parents=True, exist_ok=True)
        from_deep = configimport.scan(str(nested))
        deep_names = [f["name"] for f in from_deep["found"]]
        check("working deep inside home does not re-read the global file",
              deep_names.count("global-one") <= 1,
              f"appeared {deep_names.count('global-one')} times: {deep_names}")

    _with_fixture(run)


def test_import_goes_through_validate():
    """An import is checked exactly as a typed server is."""
    from backend.config import config
    from backend.mcp_client import configimport

    saved_servers = config.get("mcp", "servers", default=[])
    saved_creds = config.get("mcp", "credentials", default={})

    def run(base):
        try:
            config.set("mcp", "servers", value=[])
            specs, errors = configimport.make_specs(
                {"vscode_sse": True, "filesystem": True})
            check("an sse server is refused rather than coerced to http",
                  not any(s.get("transport") == "http"
                          and s.get("id") == "vscode_sse" for s in specs),
                  str(specs))
            check("and the refusal says what is wrong with it",
                  any("sse" in e for e in errors), str(errors))
            check("the sound servers in the same batch still import",
                  any(s["id"] == "filesystem" for s in specs),
                  str([s["id"] for s in specs]))

            # Importing twice must not duplicate, and must say so.
            config.set("mcp", "servers", value=specs)
            again, errors2 = configimport.make_specs({"filesystem": True})
            check("importing an already-configured server is refused",
                  again == [] and any("already" in e for e in errors2),
                  f"{again} {errors2}")
        finally:
            config.set("mcp", "servers", value=saved_servers)
            config.set("mcp", "credentials", value=saved_creds)

    _with_fixture(run)


def main():
    for name, fn in [
        ("scan reads every tool", test_scan_reads_every_tool),
        ("env placeholders", test_env_placeholders_are_expanded_and_named),
        ("a foreign secret stays foreign",
         test_a_secret_from_another_file_never_comes_back),
        ("one file is not read twice", test_one_file_is_not_read_twice),
        ("import goes through validate", test_import_goes_through_validate),
    ]:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            fails.append(f"{name} raised: {type(e).__name__}: {e}")

    print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
