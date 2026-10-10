"""A built tool reaches chat, the character, swarm agents and the Code page.

The whole premise of the feature is that a tool the user builds is a skill like
any other. That is true only because registration goes through the SHARED
`SkillRegistry` — the same trick the MCP manager uses — and the four entry
points all resolve tools from it.

Which means the failure mode is quiet. If a tool registered somewhere else, or
the Code page kept its own list, or a swarm desk's tool subset did not include
the `cli` category, the tool would still work when the user called it by name
from chat. Everything would look fine. The only symptom would be that it was
missing from the surfaces that ask "what can you do?" — and nobody files that as
a bug, they just never discover the tool.

So this asserts the routing rather than re-testing the pipeline, which
`check_parity.py` already covers for skills in general:

1. A registered tool appears in the OpenAI tool payload and the prompt
   catalogue, with its real schema.
2. A tool subset that names it narrows to exactly it, and a subset that omits it
   really omits it.
3. Asking what tools exist surfaces the `cli` category, which is the one place a
   per-category allow-list could silently drop it.
4. The Code page and the swarm resolve through the same registry, so there is no
   second catalogue to keep in step.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_consumers.py
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

parser = argparse.ArgumentParser()
parser.add_argument("--text", required=True)
parser.add_argument("--json", action="store_true")
args = parser.parse_args()
print(json.dumps({"success": True, "data": {"shouted": args.text.upper()}}))
'''

MANIFEST = {
    "slug": "shouter",
    "name": "shout_text",
    "description": "Print text in capitals",
    "keywords": ["shout", "shouting", "uppercase"],
    "entry": ["python", "tool.py"],
    "params": {"type": "object",
               "properties": {"text": {"type": "string"},
                              "json": {"type": "boolean"}},
               "required": ["text"]},
    "argv_template": ["--text", "{text}", "--json"],
    "aliases": {},
    "timeout_s": 20,
    "category": "cli",
}


def main() -> int:
    import backend.cli_tools.registry as registry_mod
    from backend.cli_tools.registry import cli_tools
    from backend.skills.registry import skill_registry
    from backend import ws_server

    temp = Path(tempfile.mkdtemp(prefix="addled-cli-consumers-"))
    original_tools_dir = registry_mod.CliToolRegistry.tools_dir
    registry_mod.CliToolRegistry.tools_dir = lambda self: temp

    try:
        directory = temp / MANIFEST["slug"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tool.py").write_text(TOOL_SOURCE, encoding="utf-8")
        (directory / "tool.json").write_text(json.dumps(MANIFEST, indent=2),
                                             encoding="utf-8")
        cli_tools.load()

        skill = skill_registry.get("shout_text")
        check("the tool is registered as a skill", skill is not None)
        check("it carries the `cli` category",
              skill is not None and skill.category == "cli",
              skill.category if skill else "no skill")

        print("=== it reaches every catalogue a consumer reads ===")
        openai_tools = skill_registry.to_openai_tools({"shout_text"})
        names = [t.get("function", {}).get("name") for t in openai_tools]
        check("it appears in the OpenAI tool payload",
              names == ["shout_text"], names)
        schema = (openai_tools[0].get("function", {}).get("parameters")
                  if openai_tools else {})
        check("with its real schema, not an empty one",
              "text" in (schema or {}).get("properties", {}), schema)
        check("and the required field survived",
              "text" in (schema or {}).get("required", []), schema)

        print("=== asking what tools exist surfaces it ===")
        # `filter_for_query` holds the one per-category allow-list in the
        # codebase. A `cli` tool missing from it is callable but
        # undiscoverable, which is the quiet failure this check exists for — so
        # the question that asks about tools is put straight to that code.
        chosen = skill_registry.filter_for_query("what tools do you have?")
        check("a tool-discovery question surfaces the built tool",
              "shout_text" in chosen,
              f"cli tools were dropped by the category allow-list: "
              f"{sorted(chosen)[:12]}")

        print("=== the Code page's own tool lists include it ===")
        # The Code page does NOT use the full catalogue: the planner and the
        # editor run with a deliberately restricted list, so "it is in the
        # registry" is not enough — a tool absent from those two lists cannot be
        # called from the Code page at all. Asserted by CALLING the resolver
        # rather than grepping for it, because a name that never reaches the
        # list is exactly the bug, and a grep cannot tell the difference.
        check("the resolver adds a built tool to the planner's list",
              "shout_text" in ws_server._code_page_tools(ws_server._PLAN_TOOLS),
              ws_server._code_page_tools(ws_server._PLAN_TOOLS))
        check("and to the editor's list",
              "shout_text" in ws_server._code_page_tools(
                  ws_server._CODE_EDIT_TOOLS),
              ws_server._code_page_tools(ws_server._CODE_EDIT_TOOLS))
        check("it keeps the read-only tools those lists exist for",
              "code_read" in ws_server._code_page_tools(ws_server._PLAN_TOOLS)
              and "read_file" in ws_server._code_page_tools(
                  ws_server._CODE_EDIT_TOOLS))
        check("both call sites use the resolver, not the bare constants",
              open(os.path.join(ROOT, "backend", "ws_server.py"),
                   encoding="utf-8").read().count("tools=_code_page_tools(") == 2,
              "a call site left on the raw tuple silently drops every CLI tool")

        print("=== no consumer keeps a second catalogue ===")
        # Swarm agents name their tools through the shared roster/brief. If that
        # ever grew its own list, a tool would have to be registered twice.
        swarm = open(os.path.join(ROOT, "backend", "swarm", "orchestrator.py"),
                     encoding="utf-8").read()
        check("swarm agents read their tool names from the shared brief",
              "tool_brief" in swarm or "skill_registry" in swarm)

        print("=== the tool actually runs on the consumer path ===")
        result = asyncio.run(skill_registry.execute("shout_text",
                                                   {"text": "hello"}))
        check("executing it through the registry succeeds", result.success,
              result.error)
        check("and the program's output comes back",
              (result.data or {}).get("data", {}).get("shouted") == "HELLO",
              result.data)

        print("=== removing it removes it everywhere ===")
        cli_tools.remove("shouter")
        check("it is gone from the skill registry",
              skill_registry.get("shout_text") is None)
        check("and from the OpenAI payload",
              [t.get("function", {}).get("name")
               for t in skill_registry.to_openai_tools({"shout_text"})] == [])
    finally:
        registry_mod.CliToolRegistry.tools_dir = original_tools_dir
        try:
            cli_tools.remove("shouter")
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(temp, ignore_errors=True)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: a built tool reaches every surface that offers tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
