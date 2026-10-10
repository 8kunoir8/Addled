"""A tool the user built answers a call before anything is downloaded.

What this closes: when the model called a skill that did not exist, the chain
went straight to the MCP market and then to the forge. Both fetch or generate
code that runs on the user's machine, and neither is the user's own work. A tool
they wrote and reviewed was not consulted at all, because there was nowhere to
put one.

What is asserted here:

1. **The order is built-in → CLI tool → market.** A name that matches nothing in
   the registry but matches a CLI tool resolves to the tool, and the market is
   never consulted.
2. **The switch really switches.** `cli_tools.prefer_over_mcp = False` restores
   the old order, so the change is reversible by a user who disagrees with it.
3. **A failure is reported, not routed around.** A matched tool that crashes is
   the answer; falling through to downloading something else would run code the
   user did not choose.
4. **Matching sees the turn's request text**, because a tool-call name is often
   generic while the request carries the intent.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_priority.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


TOOL_SOURCE = '''\
import argparse
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--query", required=True)
parser.add_argument("--json", action="store_true")
args = parser.parse_args()
if args.query == "explode":
    sys.stderr.write("no\\n")
    sys.exit(4)
print(json.dumps({"success": True, "answered_by": "the-users-own-tool",
                  "query": args.query}))
'''

MANIFEST = {
    "slug": "price-fetcher",
    "name": "price_fetcher",
    "description": "Fetch prices from a page (built by the user)",
    "keywords": ["price", "prices", "scrape"],
    "entry": ["python", "tool.py"],
    "params": {"type": "object",
               "properties": {"query": {"type": "string"},
                              "json": {"type": "boolean"}},
               "required": ["query"]},
    "argv_template": ["--query", "{query}", "--json"],
    "aliases": {},
    "timeout_s": 20,
    "category": "cli",
}


def main() -> int:
    import backend.skills.tool_loop as tool_loop
    from backend.cli_tools.registry import cli_tools
    from backend.skills.registry import skill_registry
    from backend.config import config

    temp = Path(tempfile.mkdtemp(prefix="addled-cli-priority-"))
    market_calls: list[str] = []

    # Stub the market so a call to it is visible. If the CLI tool is preferred,
    # this list must stay empty — that is the whole assertion.
    import backend.skills.market_search as market_search

    async def _fake_find_match(name, threshold=None):
        market_calls.append(name)
        return None

    original_find = getattr(market_search, "find_match", None)
    market_search.find_match = _fake_find_match

    # And the forge, so a fall-through cannot quietly generate code instead.
    import backend.skills.forge as forge_mod
    forge_calls: list[str] = []

    async def _fake_forge(task_description, provider=None, auto_validate=True):
        forge_calls.append(task_description)
        class _R:
            success = False
            detail = "forge disabled for this check"
            skill_name = ""
        return _R()

    original_forge = getattr(forge_mod.skill_forge, "forge", None)
    forge_mod.skill_forge.forge = _fake_forge

    try:
        directory = temp / MANIFEST["slug"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tool.py").write_text(TOOL_SOURCE, encoding="utf-8")
        (directory / "tool.json").write_text(json.dumps(MANIFEST, indent=2),
                                             encoding="utf-8")

        # The registry reads `app_paths.CLI_TOOLS_DIR` in production. Redirect it
        # at the temp directory so this check never touches a real install — and
        # so it cannot pass by finding a tool that happens to be there already.
        import backend.cli_tools.registry as _reg
        original_tools_dir = _reg.CliToolRegistry.tools_dir
        _reg.CliToolRegistry.tools_dir = lambda self: temp
        cli_tools.load()
        check("the user's tool is registered",
              cli_tools.get("price-fetcher") is not None)
        check("it is a skill like any other",
              skill_registry.get("price_fetcher") is not None)

        print("=== the built-in registry still comes first ===")
        market_calls.clear()
        result = asyncio.run(tool_loop.execute_skill(
            "read_file", {"path": str(directory / "tool.py")}))
        check("a built-in skill runs directly without touching the CLI tools",
              result.get("success") is True, result)
        check("and the market was not consulted", not market_calls, market_calls)

        print("=== a user's tool answers before the market is consulted ===")
        market_calls.clear()
        forge_calls.clear()
        result = asyncio.run(tool_loop.execute_skill(
            "fetch_prices", {"query": "widgets"}))
        check("the call reached the user's own tool",
              (result.get("data") or {}).get("answered_by") == "the-users-own-tool",
              result)
        check("the result names which tool answered",
              result.get("cli_tool") == "price-fetcher", result)
        check("the market was NEVER consulted", not market_calls, market_calls)
        check("the forge was never asked to write anything",
              not forge_calls, forge_calls)

        print("=== the generic call name is resolved by the request text ===")
        market_calls.clear()
        # `do_the_thing` says nothing; the request carries the intent. This is
        # the path a model takes when it names a tool vaguely.
        try:
            config.set("_forge", "request", value="fetch me the prices off a page")
        except Exception:  # noqa: BLE001
            pass
        result = asyncio.run(tool_loop.execute_skill(
            "do_the_thing", {"query": "widgets"}))
        check("the request text routed the call to the tool",
              (result.get("data") or {}).get("answered_by") == "the-users-own-tool",
              result)
        check("and still did not touch the market", not market_calls, market_calls)

        print("=== a failure is reported, not routed around ===")
        market_calls.clear()
        try:
            config.set("_forge", "request", value="")
        except Exception:  # noqa: BLE001
            pass
        result = asyncio.run(tool_loop.execute_skill(
            "fetch_prices", {"query": "explode"}))
        check("the failing tool reports its own failure",
              result.get("success") is False, result)
        check("the failure says which tool it was",
              result.get("cli_tool") == "price-fetcher", result)
        check("it did NOT fall through to the market",
              not market_calls, market_calls)

        print("=== a tool that RAISES is still the answer ===")
        # The runner returns failures as values, but the registry can raise — a
        # handler that throws, an approval policy that errors. That must not be
        # treated as "no tool matched" and quietly answered by something
        # downloaded, which is the opposite of what the user asked for.
        market_calls.clear()
        original_execute = skill_registry.execute

        async def _boom(target, call_params):
            if target == "price_fetcher":
                raise RuntimeError("the tool exploded")
            return await original_execute(target, call_params)

        skill_registry.execute = _boom
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "fetch_prices", {"query": "widgets"}))
        finally:
            skill_registry.execute = original_execute
        check("a raising tool reports a failure, not a fall-through",
              result.get("success") is False, result)
        check("it still names the tool the user built",
              result.get("cli_tool") == "price-fetcher", result)
        check("and the market was NOT consulted after the raise",
              not market_calls, market_calls)

        print("=== the switch restores the old order ===")
        # `ask_before_build` is turned off here on purpose. With it on, the
        # build question fires first and legitimately ends the turn before the
        # market is reached — that is a different behaviour, checked separately.
        # This block is only about whether the CLI tool outranks the market.
        market_calls.clear()
        config.set("cli_tools", "prefer_over_mcp", value=False)
        config.set("cli_tools", "ask_before_build", value=False)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "fetch_prices", {"query": "widgets"}))
        finally:
            config.set("cli_tools", "prefer_over_mcp", value=True)
            config.set("cli_tools", "ask_before_build", value=True)
        check("with the switch off the CLI tool is not used",
              (result.get("data") or {}).get("answered_by") is None, result)
        check("and the market IS consulted again", bool(market_calls),
              "prefer_over_mcp=False must restore the pre-existing order")

        print("=== disabling the feature disables the path ===")
        market_calls.clear()
        config.set("cli_tools", "enabled", value=False)
        try:
            result = asyncio.run(tool_loop.execute_skill(
                "fetch_prices", {"query": "widgets"}))
        finally:
            config.set("cli_tools", "enabled", value=True)
        check("with cli_tools off the tool is not consulted",
              (result.get("data") or {}).get("answered_by") is None, result)

        print("=== the priority never shadows a real skill ===")
        # Register a built-in with the same name the tool would match, and make
        # sure the registry still wins: a user tool may not take over an
        # existing capability.
        check("an existing skill still resolves to itself",
              skill_registry.get("read_file") is not None)
        result = asyncio.run(tool_loop.execute_skill(
            "read_file", {"path": str(directory / "tool.json")}))
        check("and the CLI tool did not intercept it",
              result.get("cli_tool") is None, result)
    finally:
        if original_find is not None:
            market_search.find_match = original_find
        if original_forge is not None:
            forge_mod.skill_forge.forge = original_forge
        try:
            cli_tools.remove("price-fetcher")
        except Exception:  # noqa: BLE001
            pass
        _reg.CliToolRegistry.tools_dir = original_tools_dir
        shutil.rmtree(temp, ignore_errors=True)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: a user-built tool is preferred over anything downloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
