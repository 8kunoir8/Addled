"""
MCP config import — read the servers other tools have already been told about.

Setting Addled up with MCP servers meant re-typing what the user had already
typed somewhere else. Cursor, VS Code and Claude Code all keep a JSON file of
MCP servers, and they agree on the shape closely enough that one reader can
serve all three: a ``mcpServers`` object keyed by name, each value carrying
either a ``command`` (stdio) or a ``url`` (remote).

Two things this module will not do.

It will not *execute* anything found here. A config file is a text file
somebody else wrote; importing from it is reading, and the servers are added
disabled-by-default-approval like any other, so a tool from an imported server
still asks before it runs.

It will not invent a value. A config that references ``${env:FOO}`` or leaves a
key empty is reported as needing that value, not filled with a placeholder that
connects and fails. Finding a server is only useful if it can then be used.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("addled.mcp.configimport")

# Where each tool keeps its file. Relative paths are resolved against the home
# directory; the two project-local ones are looked for relative to a given
# directory as well, because that is how they are actually used.
_HOME_FILES = (
    # Claude Code keeps one file with a global section and one per project.
    (".claude.json", "claude"),
    # Claude Desktop, which is a different file in a different place.
    ("Library/Application Support/Claude/claude_desktop_config.json", "claude"),
    ("AppData/Roaming/Claude/claude_desktop_config.json", "claude"),
    (".codeium/windsurf/mcp_config.json", "windsurf"),
)
_PROJECT_FILES = (
    (".cursor/mcp.json", "cursor"),
    (".vscode/mcp.json", "vscode"),
    (".mcp.json", "claude"),
)

# A `${env:NAME}` or `${NAME}` placeholder, and the `$NAME` form VS Code uses.
_VAR = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _read_json(path: Path) -> dict | None:
    """The parsed file, or None. A malformed config is not an error here."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except Exception as e:  # noqa: BLE001
        log.debug("could not read %s: %s", path, e)
        return None
    return data if isinstance(data, dict) else None


def _substitute(text: str) -> tuple[str, list[str]]:
    """Expand `${env:NAME}` from the environment; report what is missing.

    A missing variable yields an empty string and its name in the missing list.
    Guessing at a value would produce a server that connects and fails, which is
    harder to diagnose than one that says which variable it wants.
    """
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        value = os.environ.get(name)
        if value is None or value == "":
            missing.append(name)
            return ""
        return value

    return _VAR.sub(repl, text), missing


def _server_from_entry(name: str, entry: dict,
                       source: str) -> tuple[dict | None, str]:
    """One `mcpServers` value as an Addled spec, or a reason it cannot be.

    The three tools spell the same idea slightly differently — ``type`` may be
    ``stdio``/``http``/``sse``, or absent, in which case a ``command`` means
    stdio and a ``url`` means remote. ``sse`` is imported and then rejected by
    `validate`, which is where that message belongs.
    """
    if not isinstance(entry, dict):
        return None, "not an object"

    declared = str(entry.get("type") or entry.get("transport") or "").strip()
    command = entry.get("command")
    url = str(entry.get("url") or "").strip()
    missing: list[str] = []

    spec: dict = {"name": name, "market_name": name}

    if command:
        cmd = command if isinstance(command, list) else [command]
        cmd = [str(c) for c in cmd]
        # A list command is a pre-quoted argv; a string command is a line that
        # may carry arguments, which is how Claude Desktop writes it.
        if len(cmd) == 1 and " " in cmd[0] and not entry.get("args"):
            parts = cmd[0].split()
            cmd = parts
        argv = cmd + [str(a) for a in (entry.get("args") or [])]
        expanded: list[str] = []
        for part in argv:
            text, miss = _substitute(part)
            missing += miss
            expanded.append(text)
        spec.update({"transport": "stdio", "command": expanded[0],
                     "args": expanded[1:]})
        env: dict[str, str] = {}
        for key, value in (entry.get("env") or {}).items():
            text, miss = _substitute(str(value))
            missing += miss
            if text:
                env[str(key)] = text
        if env:
            spec["env"] = env
        spec["_missing"] = sorted(set(missing))
        return spec, ""

    if url:
        text, miss = _substitute(url)
        missing += miss
        headers: dict[str, str] = {}
        for key, value in (entry.get("headers") or {}).items():
            htext, hmiss = _substitute(str(value))
            missing += hmiss
            if htext:
                headers[str(key)] = htext
        spec.update({"transport": "http", "url": text})
        if headers:
            spec["headers"] = headers
        spec["_missing"] = sorted(set(missing))
        if declared.lower() == "sse":
            spec["transport"] = "sse"
        return spec, ""

    return None, "no command and no url"


def _collect(data: dict, source: str, *, project: str = "") -> list[dict]:
    """Every server in one parsed file, with where it came from."""
    found: list[dict] = []
    blocks: list[tuple[str, dict]] = []
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        blocks.append(("", servers))
    # VS Code nests them under `servers`, and sometimes a `mcp` wrapper.
    if isinstance(data.get("servers"), dict):
        blocks.append(("", data["servers"]))
    if isinstance(data.get("mcp"), dict) and \
            isinstance(data["mcp"].get("servers"), dict):
        blocks.append(("", data["mcp"]["servers"]))
    # Claude Code: one section per project, which is where most of them live.
    projects = data.get("projects")
    if isinstance(projects, dict):
        for path, cfg in projects.items():
            if isinstance(cfg, dict) and isinstance(cfg.get("mcpServers"), dict):
                blocks.append((str(path), cfg["mcpServers"]))

    for where, block in blocks:
        for name, entry in block.items():
            spec, why = _server_from_entry(str(name), entry, source)
            if spec is None:
                found.append({"name": str(name), "source": source,
                              "error": why, "spec": None, "project": where})
                continue
            spec["source"] = source
            spec["project"] = where
            found.append({"name": str(name), "source": source, "error": "",
                          "spec": spec, "project": where})
    return found


def sources(cwd: str = "") -> list[tuple[Path, str]]:
    """Every config file that exists and is worth reading, with its tool name.

    Home files first, then the ones belonging to a working directory, since a
    project-local file is the more specific answer and should win a tie.
    """
    out: list[tuple[Path, str]] = []
    home = Path.home()
    for rel, source in _HOME_FILES:
        path = home / rel
        if path.is_file():
            out.append((path, source))
    if cwd:
        base = Path(cwd)
        for rel, source in _PROJECT_FILES:
            path = base / rel
            if path.is_file():
                out.append((path, source))
        # `.cursor/mcp.json` is also kept in parent directories in practice.
        # The walk stops before the home directory: `~/.claude.json` is a
        # *global* file read above, and reaching it here would make any deep
        # working path pick it up a second time under a project's identity.
        home_resolved = home.resolve()
        for parent in list(base.resolve().parents)[:4]:
            if parent == home_resolved or home_resolved in parent.parents:
                break
            for rel, source in _PROJECT_FILES:
                path = parent / rel
                if path.is_file() and path not in [p for p, _ in out]:
                    out.append((path, source))
    # And the two project-local ones under the home directory, which is where a
    # global Cursor or VS Code setting lives.
    for rel, source in _PROJECT_FILES:
        path = home / rel
        if path.is_file() and path not in [p for p, _ in out]:
            out.append((path, source))
    return out


def scan(cwd: str = "") -> dict:
    """Every importable server, safe to hand to the dashboard.

    The private items are reduced by `_public`, which drops the header values
    taken from another tool's file. Those are secrets, and this result is sent
    over a socket and drawn on a card.
    """
    out = _scan_private(cwd)
    out["found"] = [_public(item) for item in out["found"]]
    return out


def _scan_private(cwd: str = "") -> dict:
    """Every importable server, with the specs and values still attached.

    Returns ``{"found": [...], "sources": [...], "already": [...]}``. A server
    whose id is already configured is reported as ``already`` rather than
    offered again — re-importing one would either duplicate it or silently
    overwrite the settings the user has since changed.
    """
    from backend.mcp_client import credentials
    from backend.mcp_client.manager import McpManager, _sanitise

    existing = {str(s.get("id")) for s in McpManager()._config_servers()}
    found: list[dict] = []
    seen_sources: list[dict] = []
    seen_ids: dict[str, str] = {}
    already: list[dict] = []

    for path, source in sources(cwd):
        data = _read_json(path)
        if data is None:
            seen_sources.append({"path": str(path), "source": source,
                                 "readable": False, "count": 0})
            continue
        entries = _collect(data, source)
        seen_sources.append({"path": str(path), "source": source,
                             "readable": True, "count": len(entries)})
        for item in entries:
            spec = item.get("spec")
            if spec is None:
                item["id"] = ""
                found.append(item)
                continue
            sid = _sanitise(str(spec["name"]).replace("/", "-")
                            .replace(".", "-")) or "server"
            item["id"] = sid
            # Two files naming the same server: the first wins, and the second
            # is reported as a duplicate rather than offered twice. The order
            # of `sources()` puts home files first and project files after, so
            # the global setting is what carries over.
            if sid in seen_ids:
                item["duplicate_of"] = seen_ids[sid]
                continue
            seen_ids[sid] = str(path)
            if sid in existing:
                already.append({"id": sid, "name": item["name"],
                                "source": source, "path": str(path)})
                continue
            item["missing"] = spec.pop("_missing", [])
            item["headers_known"] = sorted(
                (spec.get("headers") or {}).keys())
            # Header values are secrets from somebody else's file. They are
            # reported by name and handed to `credentials`, never echoed.
            if spec.get("headers"):
                item["_header_values"] = dict(spec["headers"])
                spec.pop("headers", None)
            item["_spec"] = spec
            item.pop("spec", None)
            found.append(item)

    return {
        "found": found,
        "sources": seen_sources,
        "already": already,
        "held": credentials.known(),
    }


def _public(item: dict) -> dict:
    """One scan result with everything secret and everything private removed.

    The spec carries header *values* taken from another tool's file, and those
    are as much a secret as one the user typed. They are handed to `credentials`
    at import time and never returned — the same rule the rest of this package
    follows, and the reason `scan()` can be sent straight to a dashboard.
    """
    out = {k: v for k, v in item.items()
           if not k.startswith("_")}
    out["header_names"] = sorted((item.get("_header_values") or {}).keys())
    spec = item.get("_spec") or {}
    out["transport"] = spec.get("transport") or ""
    out["url"] = spec.get("url") or ""
    out["command"] = spec.get("command") or ""
    out["args"] = list(spec.get("args") or [])
    out["env_names"] = sorted((spec.get("env") or {}).keys())
    return out



def make_specs(picked: dict, *, cwd: str = "") -> tuple[list[dict], list[str]]:
    """Turn chosen imports into server specs Addled can save.

    Header values found in another tool's file are stored as credentials, the
    same as any other, so they are encrypted at rest and never appear in the
    server definition. Without this an imported key would sit in the clear in
    a file Addled writes, which would undo 5.2 for exactly the servers most
    likely to have one.
    """
    from backend.mcp_client import credentials
    from backend.mcp_client.manager import McpManager

    wanted = {str(k) for k in (picked or {}).keys()}
    if not wanted:
        return [], []

    manager = McpManager()
    existing = {str(s.get("id")) for s in manager._config_servers()}
    # The *private* scan: this is the one path that needs the specs and the
    # header values, and it is the path that stores them as credentials.
    scan_result = _scan_private(cwd)
    specs: list[dict] = []
    errors: list[str] = []
    header_values: dict[str, str] = {}

    # A server that is already configured is not in `found` at all — the scan
    # files it under `already` so it is not offered twice. That means a caller
    # asking for one by name gets nothing back and no reason, which reads as a
    # silent failure. Say which ones those are instead.
    offered = set()
    for item in scan_result["found"]:
        if item.get("id") in wanted:
            offered.add(item["id"])
    for stale in scan_result.get("already") or []:
        if stale.get("id") in wanted and stale.get("id") not in offered:
            errors.append(f"{stale.get('name')}: already configured")

    for item in scan_result["found"]:
        if item.get("id") not in wanted:
            continue
        source_spec = dict(item.get("_spec") or {})
        if not source_spec:
            errors.append(f"{item.get('name')}: nothing to import")
            continue
        for key, value in (item.get("_header_values") or {}).items():
            header_values[key] = value
        clean, error = manager.validate(source_spec)
        if error:
            errors.append(f"{item.get('name')}: {error}")
            continue
        specs.append(clean)

    if header_values:
        credentials.save(header_values=header_values)
    return specs, errors
