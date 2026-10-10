"""A CLI tool a user builds is a skill every page can call.

What this closes: there was no way to build a tool deliberately. The forge
exists, but only reactively — the model calls a skill that is not there, and the
forge fires mid-turn. Nothing let a user say "build me a tool for this, I will
need it later", and nothing let them review what was built before it ran.

What is asserted here, in the order the pieces fit together:

1. **The manifest is a contract.** `validate()` refuses the shapes that fail
   silently in practice: a stdlib slug that shadows a module, a placeholder with
   no parameter, a required parameter the template drops, a flag with no
   boolean behind it.
2. **Arguments are rendered without a shell.** A value containing a space stays
   one argument; an absent value takes its flag with it; a boolean flag appears
   only when true.
3. **Running it never raises and never hangs.** A tool that fails, crashes or
   runs forever comes back as a result, not an exception, and a timeout really
   stops it.
4. **Registering it makes it a skill.** It appears in `enabled_list_all()` —
   which is what chat, the Code page, swarm agents, the bots and voice all read
   — and `execute()` routes to the program.
5. **Removing it takes the skill away first.** A window where the skill exists
   but its file is gone fails as a confusing subprocess error.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_tools.py
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


# A working tool, written to disk rather than generated, so this suite does not
# depend on a model being available.
TOOL_SOURCE = '''\
"""Say hello to a name."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="Say hello")
    parser.add_argument("--name", required=True)
    parser.add_argument("--loud", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    message = "hello " + args.name
    if args.loud:
        message = message.upper()
    print(json.dumps({"success": True, "message": message}))


if __name__ == "__main__":
    main()
'''

# Prints nothing, exits nonzero — the "tool is broken" path.
FAILING_SOURCE = '''\
import sys
sys.stderr.write("it broke\\n")
sys.exit(3)
'''

# Prints plain prose, not JSON — the shape a hand-written tool often has before
# anyone adds a --json flag.
PROSE_SOURCE = '''\
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--name", required=True)
args = parser.parse_args()
print("hello " + args.name)
'''

# Never returns — the "tool hangs" path.
HANGING_SOURCE = '''\
import time
time.sleep(600)
'''


def write_tool(root: Path, slug: str, manifest: dict, source: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tool.py").write_text(source, encoding="utf-8")
    (directory / "tool.json").write_text(json.dumps(manifest, indent=2),
                                         encoding="utf-8")
    return directory


def base_manifest(**overrides) -> dict:
    manifest = {
        "slug": "demo",
        "name": "demo",
        "description": "Say hello to a name, optionally loudly",
        "keywords": ["hello", "greet"],
        "entry": ["python", "tool.py"],
        "params": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "loud": {"type": "boolean"},
                "json": {"type": "boolean"},
            },
            "required": ["name"],
        },
        "argv_template": ["--name", "{name}", "--loud", "--json"],
        "aliases": {"name": "who"},
        "requires_approval": False,
        "timeout_s": 20,
        "category": "cli",
    }
    manifest.update(overrides)
    return manifest


def main() -> int:
    from backend.cli_tools.spec import CliToolSpec
    from backend.cli_tools.registry import CliToolRegistry
    from backend.skills.registry import skill_registry

    print("=== the manifest is a contract ===")
    good = CliToolSpec.from_dict(base_manifest())
    check("a well-formed manifest validates", good.validate() == "",
          good.validate())
    check("a manifest round-trips", CliToolSpec.from_dict(good.to_dict()) == good)

    check("a bad slug is refused",
          CliToolSpec.from_dict(base_manifest(slug="Bad Slug!")).validate() != "")
    stdlib = CliToolSpec.from_dict(base_manifest(slug="json", name="json_tool"))
    check("a stdlib module name is refused", stdlib.validate() != "",
          "a tool called 'json' would shadow the stdlib module")
    check("a colliding skill name is refused",
          good.validate(known_skills={"demo"}) != "")
    undeclared = CliToolSpec.from_dict(base_manifest(
        argv_template=["--name", "{name}", "{mystery}"]))
    check("a placeholder with no parameter is refused",
          undeclared.validate() != "")
    missing = CliToolSpec.from_dict(base_manifest(
        argv_template=["--loud", "--json"]))
    check("a required parameter the template drops is refused",
          missing.validate() != "")
    badflag = CliToolSpec.from_dict(base_manifest(
        argv_template=["--name", "{name}", "--nope"]))
    check("a flag with no parameter behind it is refused",
          badflag.validate() != "")
    stringflag = CliToolSpec.from_dict(base_manifest(
        argv_template=["--name", "--loud", "--json"]))
    check("a value parameter passed as a flag is refused",
          stringflag.validate() != "")
    unknown = CliToolSpec.from_dict({**base_manifest(), "field_from_the_future": 1})
    check("an unknown manifest field does not break loading",
          unknown.slug == "demo")

    print("=== arguments are rendered without a shell ===")
    argv, warnings = good.argv_for({"who": "World", "loud": True, "json": True})
    check("an alias is accepted",
          argv == ["--name", "World", "--loud", "--json"], argv)
    argv, _ = good.argv_for({"name": "a b; rm -rf /", "json": True})
    check("a value with a space stays one argument",
          argv == ["--name", "a b; rm -rf /", "--json"], argv)
    argv, _ = good.argv_for({"name": "quiet", "loud": False, "json": False})
    check("a false boolean is omitted, not passed",
          "--loud" not in argv and "--json" not in argv
          and argv == ["--name", "quiet"], argv)
    argv, _ = good.argv_for({"json": True})
    check("an absent value takes its flag with it",
          "--name" not in argv, argv)
    multi = CliToolSpec.from_dict(base_manifest(
        params={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["paths"]},
        argv_template=["--path={paths}"], aliases={}))
    argv, _ = multi.argv_for({"paths": ["a.txt", "b.txt"]})
    check("an array repeats its slot",
          argv == ["--path=a.txt", "--path=b.txt"], argv)
    argv, warnings = good.argv_for({"name": "x", "nonsense": 1})
    check("an undeclared parameter is reported, not passed",
          any("nonsense" in w for w in warnings) and "nonsense" not in argv,
          warnings)

    print("=== running it never raises and never hangs ===")
    from backend.cli_tools.runner import run_cli

    temp = Path(tempfile.mkdtemp(prefix="addled-cli-tools-"))
    try:
        ok_dir = write_tool(temp, "demo", base_manifest(), TOOL_SOURCE)
        result = asyncio.run(run_cli(good, ok_dir, {"name": "World", "json": True}))
        check("a working tool returns its JSON",
              result.get("success") and result.get("message") == "hello World",
              result)
        check("output is marked as parsed", result.get("parsed") is True)

        plain = CliToolSpec.from_dict(base_manifest(
            slug="plain", name="plain_tool",
            argv_template=["--name", "{name}"], aliases={}))
        plain_dir = write_tool(temp, "plain", plain.to_dict(), PROSE_SOURCE)
        result = asyncio.run(run_cli(plain, plain_dir, {"name": "World"}))
        check("a tool that prints no JSON is still usable",
              result.get("success") and result.get("parsed") is False
              and "hello World" in result.get("stdout", ""), result)

        fail_spec = CliToolSpec.from_dict(base_manifest(
            slug="broken", name="broken", argv_template=["--x", "{name}"],
            aliases={}))
        fail_dir = write_tool(temp, "broken", fail_spec.to_dict(), FAILING_SOURCE)
        result = asyncio.run(run_cli(fail_spec, fail_dir, {"name": "z"}))
        check("a failing tool returns a failure, not an exception",
              result.get("success") is False and "it broke" in str(result.get("error")),
              result)

        hang_spec = CliToolSpec.from_dict(base_manifest(
            slug="hang", name="hang", argv_template=["--x", "{name}"],
            aliases={}, timeout_s=2))
        hang_dir = write_tool(temp, "hang", hang_spec.to_dict(), HANGING_SOURCE)
        import time as _time
        started = _time.time()
        result = asyncio.run(run_cli(hang_spec, hang_dir, {"name": "z"}))
        elapsed = _time.time() - started
        check("a hanging tool is stopped by the timeout",
              result.get("success") is False and elapsed < 15,
              f"{elapsed:.1f}s, {result}")
        check("the timeout error names the limit",
              "2s" in str(result.get("error")), result.get("error"))

        # The global timeout is a CEILING for tools that did not set their own.
        # Asserted because a declared setting that nothing reads is worse than
        # no setting: the user changes it and nothing anywhere behaves
        # differently, with no error to notice.
        from backend.config import config as _config
        long_spec = CliToolSpec.from_dict(base_manifest(
            slug="slowpoke", name="slowpoke", argv_template=["--x", "{name}"],
            aliases={}, timeout_s=300))
        long_dir = write_tool(temp, "slowpoke", long_spec.to_dict(),
                              HANGING_SOURCE)
        _config.set("cli_tools", "timeout_s", value=2)
        try:
            started = _time.time()
            result = asyncio.run(run_cli(long_spec, long_dir, {"name": "z"}))
            elapsed = _time.time() - started
        finally:
            _config.set("cli_tools", "timeout_s", value=60)
        check("the configured ceiling shortens a tool that asked for longer",
              result.get("success") is False and elapsed < 15,
              f"{elapsed:.1f}s, {result}")
        check("and the error names the ceiling that applied, not the spec's",
              "2s" in str(result.get("error")), result.get("error"))

        missing_spec = CliToolSpec.from_dict(base_manifest(
            slug="ghost", name="ghost", argv_template=["--x", "{name}"],
            aliases={}, entry=["definitely-not-a-real-interpreter", "tool.py"]))
        ghost_dir = write_tool(temp, "ghost", missing_spec.to_dict(), TOOL_SOURCE)
        result = asyncio.run(run_cli(missing_spec, ghost_dir, {"name": "z"}))
        check("a missing interpreter is reported, not raised",
              result.get("success") is False, result)

        print("=== registering it makes it a skill ===")
        # The SINGLETON, because that is the instance the adapter bumps run
        # stats on and the one the resolution chain consults. A local registry
        # would pass the match checks and silently disagree with the app about
        # the counters.
        from backend.cli_tools.registry import cli_tools as live
        live._tools.clear()
        added = live.add(good, ok_dir, built_by="user")
        check("the tool registers", added.get("success") is True, added)
        skill = skill_registry.get("demo")
        check("it is a skill in the registry", skill is not None)
        check("its category is 'cli'",
              skill is not None and skill.category == "cli")
        check("it is offered to a turn",
              "demo" in [s.name for s in skill_registry.enabled_list_all()])
        check("it matches its own slug",
              live.find_match("demo") is not None)
        check("it matches a keyword",
              live.find_match("greet_someone") is not None)
        # The user's own words carry the intent when the CALL NAME is not itself
        # a keyword. The two signals have to corroborate: a request word that
        # shares nothing with the call is the accidental-collision case, and
        # matching on it would run a tool the model did not ask for.
        check("the turn's own words help a related call name match",
              live.find_match("do_greeting", "please greet the user") is not None)
        check("prose unrelated to the call does not carry a match",
              live.find_match("send_an_email_to_bob",
                              "greet everyone in the company") is None)
        check("an unrelated call does not match it",
              live.find_match("send_an_email_to_bob") is None)

        ran = asyncio.run(skill_registry.execute("demo", {"name": "World", "json": True}))
        check("executing the skill runs the program",
              ran.success and ran.data.get("message") == "hello World", ran.data)
        check("running it records the stats",
              live.get("demo") is not None and live.get("demo").run_count >= 1,
              live.get("demo").stats_dict() if live.get("demo") else "missing")

        print("=== removing it takes the skill away first ===")
        removal = live.remove("demo")
        check("the removal succeeds", removal.get("success") is True, removal)
        check("the skill is gone from the registry",
              skill_registry.get("demo") is None)
        check("the directory is gone", not ok_dir.exists())

        print("=== a broken tool cannot stop the others loading ===")
        registry2 = CliToolRegistry()
        registry2._tools.clear()
        write_tool(temp, "good-one", base_manifest(slug="good-one",
                                                   name="good_one"), TOOL_SOURCE)
        (temp / "bad-one").mkdir(parents=True, exist_ok=True)
        (temp / "bad-one" / "tool.json").write_text("{ not json",
                                                    encoding="utf-8")
        write_tool(temp, "bad-two", base_manifest(slug="bad-two",
                                                 name="bad_two",
                                                 argv_template=["{nope}"]),
                   TOOL_SOURCE)

        import backend.cli_tools.registry as _reg
        original = _reg.CliToolRegistry.tools_dir
        _reg.CliToolRegistry.tools_dir = lambda self: temp
        try:
            loaded = registry2.load()
        finally:
            _reg.CliToolRegistry.tools_dir = original
        check("the good tool still loads", loaded == 1, f"loaded {loaded}")
        check("the good tool is registered",
              skill_registry.get("good_one") is not None)
        skill_registry.unregister("good_one")
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: CLI tools build, run, register and unregister correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
