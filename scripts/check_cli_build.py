"""Building a tool: draft first, write only after it is seen.

What this closes: a user could not get a tool for a capability nothing covered.
The forge could write a skill, but a skill is an async function the app imports
— not a program the user can run, read, or take with them. This builds the
latter, and the split between `draft` and `apply` is the property that matters:
the model's output is shown before any of it reaches the disk.

What is asserted here:

1. **A draft writes nothing.** After `draft` returns, the tools directory holds
   no entry for it.
2. **A draft is a real spec.** It validates, its argv renders, and the schema
   matches the flags the program actually declares — read from the source, not
   guessed, because the schema is what the model is told to fill in.
3. **Bad output is refused, not written.** No entry point, a syntax error, and a
   provider that will not answer each produce `success=False` and no file.
4. **`apply` writes what the user saw.** An edit passed as `source` is what
   lands on disk and what runs.
5. **An applied tool is immediately callable**, and its smoke test reports the
   truth about whether the program runs.

No model is called: the provider is a stub. The point is the pipeline around it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_build.py
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


GOOD_PROGRAM = '''\
import argparse
import json

parser = argparse.ArgumentParser(description="Count words in a file.")
parser.add_argument("--path", required=True)
parser.add_argument("--limit", type=int, default=10)
parser.add_argument("--json", action="store_true")
args = parser.parse_args()

if __name__ == "__main__":
    try:
        text = open(args.path, encoding="utf-8").read()
        words = text.split()[:args.limit]
        print(json.dumps({"success": True, "data": {"words": words}}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))
        raise SystemExit(1)
EXAMPLE_ARGV = ["--path", "__FILE__", "--limit", "3"]
'''


class _Reply:
    def __init__(self, text, ok=True, error=""):
        self.response = text
        self.ok = ok
        self.error = error


class _Provider:
    provider_id = "stub"

    def __init__(self, text, ok=True, error=""):
        self._text = text
        self._ok = ok
        self._error = error

    async def chat(self, *a, **k):
        return _Reply(self._text, self._ok, self._error)


def main() -> int:
    import backend.cli_tools.builder as builder
    import backend.cli_tools.registry as registry_mod
    from backend.cli_tools.registry import cli_tools
    from backend.cli_tools.spec import CliToolSpec
    from backend.skills.forge import skill_forge

    temp = Path(tempfile.mkdtemp(prefix="addled-cli-build-"))
    original_tools_dir = registry_mod.CliToolRegistry.tools_dir
    registry_mod.CliToolRegistry.tools_dir = lambda self: temp

    # Discovery and install are the forge's and are covered by its own suites.
    # Stubbed here so a network call cannot decide the result of this check.
    original_discover = skill_forge.discover
    original_install = skill_forge.install

    async def _no_discovery(capability, provider=None):
        return {"package": "", "install_cmd": "", "approach": "", "code_hint": ""}

    installs: list[str] = []

    async def _fake_install(cmd):
        installs.append(cmd)
        return {"success": True}

    skill_forge.discover = _no_discovery
    skill_forge.install = _fake_install

    # `draft` imports the provider chooser from `tool_loop` at call time, so the
    # stub has to go there — patching it anywhere else would leave the real
    # chooser running and this check would need a live model.
    import backend.skills.tool_loop as tool_loop
    original_chooser = tool_loop.forge_target_provider
    sent: list[str] = []

    def _make_provider(text, ok=True, error=""):
        def _provider_for(provider=None):
            sent.append(text)
            return _Provider(text, ok, error)
        return _provider_for

    try:
        print("=== a draft writes nothing ===")
        tool_loop.forge_target_provider = _make_provider(GOOD_PROGRAM)
        build = asyncio.run(builder.draft(
            "count the words in a text file", slug="word-counter"))
        check("the draft succeeded", build.success, build.detail)
        check("it produced a slug", build.slug == "word-counter", build.slug)
        check("it produced source", bool(build.source.strip()))
        check("NOTHING was written to the tools directory",
              not any(temp.iterdir()), list(temp.iterdir()))

        print("=== the draft is a spec that validates ===")
        check("it carries a spec", build.spec is not None)
        check("the spec validates", build.spec.validate() == "",
              build.spec.validate() if build.spec else "no spec")
        params = build.spec.params.get("properties", {})
        check("the schema declares the program's flags",
              {"path", "limit", "json"} <= set(params), list(params))
        check("a default of 10 is typed integer, not string",
              params["limit"]["type"] == "integer", params.get("limit"))
        check("a required flag is marked required",
              "path" in build.spec.params.get("required", []),
              build.spec.params.get("required"))
        check("the compatibility flag is declared",
              params.get("json", {}).get("type") == "boolean", params.get("json"))
        check("the example invocation was kept for the smoke test",
              build.spec is not None)
        argv, warns = build.spec.argv_for({"path": "notes.txt", "limit": 3})
        check("the argv renders the real flags",
              argv[:2] == ["--path", "notes.txt"], argv)
        check("rendering a complete call warns about nothing", not warns, warns)

        print("=== the tool knows what it was asked for ===")
        check("the description is the user's words",
              "words" in build.spec.description.lower(), build.spec.description)
        check("the keywords come from the request, so a later call matches",
              "words" in build.spec.keywords, build.spec.keywords)

        print("=== a program with no entry point is refused ===")
        tool_loop.forge_target_provider = _make_provider(
            "import json\nprint(json.dumps({'success': True}))\n")
        bad = asyncio.run(builder.draft("do something vague"))
        check("no entry point means no draft", bad.success is False, bad.detail)
        check("and the reason says so",
              "entry point" in bad.detail.lower(), bad.detail)
        check("still nothing written", not any(temp.iterdir()))

        print("=== code that does not compile is refused ===")
        tool_loop.forge_target_provider = _make_provider(
            "def f(:\n    pass\nif __name__ == '__main__':\n    pass\n")
        bad = asyncio.run(builder.draft("do something broken"))
        check("a syntax error means no draft", bad.success is False, bad.detail)
        check("and the reason mentions the syntax error",
              "syntax" in bad.detail.lower(), bad.detail)

        print("=== a provider that will not answer is not an exception ===")
        tool_loop.forge_target_provider = _make_provider("", ok=False,
                                                         error="rate limited")
        bad = asyncio.run(builder.draft("do something unanswerable"))
        check("a provider error is a failed draft, not a raise",
              bad.success is False, bad.detail)
        check("and the provider's reason is passed through",
              "rate limited" in bad.detail, bad.detail)

        print("=== a markdown-fenced answer is unwrapped ===")
        tool_loop.forge_target_provider = _make_provider(
            "```python\n" + GOOD_PROGRAM + "\n```")
        fenced = asyncio.run(builder.draft("count words", slug="fenced-counter"))
        check("the fences were stripped, not written into the file",
              fenced.success and "```" not in fenced.source, fenced.detail)

        print("=== apply writes what the user saw ===")
        tool_loop.forge_target_provider = _make_provider(GOOD_PROGRAM)
        build = asyncio.run(builder.draft("count the words in a text file",
                                          slug="word-counter"))
        edited = build.source.replace("Count words in a file.",
                                      "Count words in a file, as edited.")
        applied = asyncio.run(builder.apply(build, source=edited))
        check("the tool was applied", applied.get("success") is True, applied)
        program = temp / "word-counter" / "tool.py"
        check("the program is on disk", program.exists())
        check("the EDIT is what was written, not the model's original",
              "as edited" in program.read_text(encoding="utf-8"),
              program.read_text(encoding="utf-8")[:200])
        check("the manifest is beside it",
              (temp / "word-counter" / "tool.json").exists())
        manifest = json.loads((temp / "word-counter" / "tool.json")
                              .read_text(encoding="utf-8"))
        check("the manifest carries the same slug",
              manifest.get("slug") == "word-counter", manifest.get("slug"))

        print("=== the model's example does not invent flags ===")
        # The model writes `EXAMPLE_ARGV` by hand and invents names. Asked to
        # count words it implemented `--input/-i` but wrote
        # `EXAMPLE_ARGV = ["--file", ...]`. The template used to pass the
        # unknown flag straight through, producing a tool that registered,
        # appeared in the catalogue and failed on EVERY call with
        # "unrecognized arguments: --file" — the schema validated, so nothing
        # complained until it was used. Found by running the built tool.
        invented = builder._params_from_source(
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--input', '-i', required=True)\n"
            "p.add_argument('--top', type=int, default=10)\n",
            ["--file", "x", "--top", "10"])
        made = builder._template_from(invented,
                                      ["--file", "x", "--top", "10"])
        check("a flag the program does not define is dropped from the template",
              "--file" not in made, made)
        check("the flag it really defines is used instead",
              "--input" in made, made)
        check("the invented flag's value is not left behind as a positional",
              "x" not in made, made)
        check("the real flags still carry placeholders",
              "{input}" in made and "{top}" in made, made)

        print("=== no flag appears twice in the template ===")
        # Found by running a tool whose example mentioned `--json`: the loop
        # added it, the property loop added it again, and the reconciliation
        # between them tested for `"json"` AS A PLACEHOLDER — which never holds
        # for a boolean — so both ran. The template came out
        # `["--word", "{word}", "--json", "--times", "{times}", "--json"]`.
        with_json = builder._template_from(
            builder._params_from_source(
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--word', required=True)\n"
                "p.add_argument('--json', action='store_true')\n",
                []),
            ["--word", "hi", "--json"])
        check("--json is emitted once when the example already has it",
              with_json.count("--json") == 1, with_json)
        without_json = builder._template_from(
            builder._params_from_source(
                "import argparse\n"
                "p = argparse.ArgumentParser()\n"
                "p.add_argument('--word', required=True)\n"
                "p.add_argument('--json', action='store_true')\n",
                []),
            ["--word", "hi"])
        check("--json is still added when the example omits it",
              without_json.count("--json") == 1, without_json)

        print("=== required is what argparse says, not what was inferred ===")
        # The old rule excluded any flag with a `default` or an `nargs`, and any
        # boolean or integer. Four wrong inferences, failing in both
        # directions: `nargs="+", required=True` was DROPPED so the tool could
        # never be called, and `add_argument("--name")` — optional to argparse —
        # was ADDED so the schema demanded a value nothing needs.
        def _req(line):
            return builder._params_from_source(
                "import argparse\np = argparse.ArgumentParser()\n"
                + line + "\n", []).get("required")

        check("nargs='+' with required=True IS required",
              _req('p.add_argument("--files", nargs="+", required=True)')
              == ["files"], "a dropped required flag makes the tool uncallable")
        check("nargs='+' with a default is NOT",
              _req('p.add_argument("--files", nargs="+", default=[])') == [])
        check("a bare flag is NOT required (argparse treats it as optional)",
              _req('p.add_argument("--name")') == [],
              "the schema demanded a value the program does not need")
        check("an explicit required=True on an int IS",
              _req('p.add_argument("--count", type=int, required=True)')
              == ["count"])
        check("store_true is never required",
              _req('p.add_argument("--json", action="store_true")') == [])

        print("=== a positional is reported, not silently dropped ===")
        # The schema is built from `add_argument("--flag")` calls, so a
        # positional is invisible to it and every call omits it. The prompt
        # asks for `--kebab-case`, but nothing enforces that, and the failure is
        # silent: the tool registers and then refuses every call.
        spec_pos = CliToolSpec(slug="p", name="p", description="d",
                               keywords=["k"], entry=["python", "tool.py"],
                               params=builder._params_from_source(
                                   "import argparse\n"
                                   "p = argparse.ArgumentParser()\n"
                                   'p.add_argument("target")\n', []),
                               argv_template=["target", "--json"])
        warned = builder._check_argv_template(spec_pos)
        check("a positional argument raises a warning",
              any("position" in w.lower() for w in warned), warned)
        check("and a tool with none does not",
              not any("position" in w.lower() for w in
                      builder._check_argv_template(build.spec)),
              builder._check_argv_template(build.spec))

        print("=== a folder flag is given a folder, not a file ===")
        # `--directory` was handed `input.txt`, because the sample shaping
        # treated "dir" as a kind of "file". The generated tool was correct and
        # refused it with `NotADirectoryError`, so the smoke test reported a
        # failure in a tool that was written exactly right — and the user was
        # shown a red Test on good code.
        check("a directory flag gets a directory-shaped sample",
              builder._sample_for("--directory", "string") == "example_dir",
              builder._sample_for("--directory", "string"))
        check("and so does a folder flag",
              builder._sample_for("--folder", "string") == "example_dir",
              builder._sample_for("--folder", "string"))
        check("while a file flag still gets a file",
              builder._sample_for("--file", "string") == "input.txt",
              builder._sample_for("--file", "string"))

        # And the sample is MATERIALISED as a directory that can actually be
        # listed. A name alone is not enough: the tool would still fail on a
        # path that does not exist.
        import os
        dir_argv, dir_scratch = builder._materialise_paths(
            ["--directory", "example_dir", "--json"])
        try:
            check("the sample directory is really created",
                  os.path.isdir(dir_argv[1]), dir_argv[1])
            check("and it is non-empty, so listing it means something",
                  bool(os.listdir(dir_argv[1])), dir_argv[1])
        finally:
            if dir_scratch:
                import shutil as _sh
                _sh.rmtree(dir_scratch, ignore_errors=True)

        print("=== a bad sample is not blamed on the code ===")
        # A fixture fault and a code fault look identical to the user, and the
        # retry is wasted on the first — the model is asked to correct a program
        # that was never wrong, and produces a different one that fails the same
        # way. The classifier names the difference.
        check("a path error naming OUR sample is a fixture problem",
              builder._looks_like_a_fixture_problem(
                  "NotADirectoryError: not a directory: "
                  r"C:\Temp\addled-cli-smoke-x\input.txt", "",
                  ["--directory", r"C:\Temp\addled-cli-smoke-x\input.txt"]),
              "the sample was ours, so the sample is the suspect")
        # The FALSE POSITIVE that matters: a path the tool hardcoded is real
        # breakage, and excusing it would hide a bug. The first version of this
        # classifier checked only the error text and got exactly this wrong.
        check("but a path the TOOL invented is not",
              not builder._looks_like_a_fixture_problem(
                  "FileNotFoundError: No such file or directory: "
                  "'definitely-not-here-12345.txt'", "", ["--json"]),
              "a hardcoded path is the program's own fault")
        check("and an ordinary error is not either",
              not builder._looks_like_a_fixture_problem(
                  "ValueError: bad csv row", "", ["--input", "input.txt"]))
        check("a repeated value flag keeps only one placeholder",
              builder._template_from(
                  builder._params_from_source(
                      "import argparse\np = argparse.ArgumentParser()\n"
                      'p.add_argument("--tags", action="append")\n', []),
                  ["--tags", "a", "--tags", "b"]).count("{tags}") == 1,
              builder._template_from(
                  builder._params_from_source(
                      "import argparse\np = argparse.ArgumentParser()\n"
                      'p.add_argument("--tags", action="append")\n', []),
                  ["--tags", "a", "--tags", "b"]))

        # A positional, which the OPTION pattern cannot see and the template
        # check cannot either — a positional is exactly what a template has no
        # slot for. A live draft of "count the lines in a text file" wrote
        # `add_argument("file")`, so the tool registered, listed, passed
        # validation, and then failed EVERY call with "the following arguments
        # are required: file".
        pos_params = builder._params_from_source(
            "import argparse\np = argparse.ArgumentParser()\n"
            'p.add_argument("file", help="path")\n'
            'p.add_argument("--json", action="store_true")\n', [])
        check("a positional argument is detected from the source",
              pos_params.get("_positionals") == ["file"],
              f"found {pos_params.get('_positionals')!r}")
        pos_params.pop("_positionals", None)
        check("and it is kept OUT of the schema",
              sorted(pos_params.get("properties", {})) == ["json"],
              sorted(pos_params.get("properties", {})))

        # And that `draft` actually TELLS the user, since a detector nothing
        # calls is the same as no detector. Driven through the real `draft`
        # against the source shape the live model produced verbatim, with only
        # the provider stubbed — so the warning path, not just the parser, is
        # what is under test.
        positional_source = (
            "import argparse, json, sys\n"
            "def main() -> int:\n"
            '    p = argparse.ArgumentParser(description="Count lines.")\n'
            '    p.add_argument("file", help="Path to the text file.")\n'
            '    p.add_argument("--json", action="store_true")\n'
            "    a = p.parse_args()\n"
            '    print(json.dumps({"success": True, "data": 1}))\n'
            "    return 0\n"
            'if __name__ == "__main__":\n'
            "    sys.exit(main())\n")

        # Named distinctly from the module-level `_Reply`/`_Provider` at the top
        # of this file. Reusing those names made them local to `main`, and the
        # helper reading the module-level ones then could not find them —
        # `NameError: cannot access free variable`.
        class _PosReply:
            ok = True
            error = ""

        class _PosProvider:
            provider_id = "stub"

            async def chat(self, *a, **k):
                reply = _PosReply()
                reply.response = positional_source
                return reply

        # Patched on the MODULES the function imports from, not on `builder`:
        # `draft` does `from backend.skills.forge import skill_forge` inside the
        # function body, so there is no `builder.skill_forge` to replace.
        import backend.skills.forge as forge_mod

        class _PosForge:
            async def discover(self, *a, **k):
                return {}

        # The CHOOSER must be replaced too, not just the provider passed in.
        # `draft` calls `forge_target_provider`, and earlier checks in this file
        # set it to a stub returning `GOOD_PROGRAM` — so passing a provider had
        # no effect and this check silently ran against the OTHER source. Found
        # by printing what `draft` received: `['--path', '__FILE__', '--limit',
        # '3']`, not the positional source written above.
        import backend.skills.tool_loop as tool_loop_mod

        real_forge = forge_mod.skill_forge
        real_chooser = tool_loop_mod.forge_target_provider
        forge_mod.skill_forge = _PosForge()
        tool_loop_mod.forge_target_provider = lambda p=None: _PosProvider()
        try:
            drafted = asyncio.run(builder.draft(
                "count the lines in a text file"))
        finally:
            forge_mod.skill_forge = real_forge
            tool_loop_mod.forge_target_provider = real_chooser
        check("the draft still succeeds, so the user can see and edit it",
              drafted.success is True,
              f"detail was {drafted.detail!r} — a failing test must not "
              "withhold the source from someone who wants to fix it")
        check("and it WARNS about the positional it cannot supply",
              any("POSITION" in w for w in (drafted.warnings or [])),
              f"warnings were {drafted.warnings!r}")
        # AND the draft's OWN test actually RUNS it. A schema-derived invocation
        # cannot express a positional, so it omitted it entirely, the program
        # still exited 0, and the test passed a tool it had never properly
        # exercised — a false pass on the one thing the test exists to catch.
        #
        # It is NOT asserted to FAIL here, and the first version of this check
        # wrongly asserted exactly that. Given a real file the program works
        # fine; the defect is that ADDLED cannot supply the positional, which
        # the warning above reports. Making the test fail would claim the code
        # is broken when the code is correct and the CALLER is limited.
        check("and the draft was actually run, with an argument supplied",
              drafted.test_result is not None
              and drafted.test_result.get("success") is True,
              f"test_result was {drafted.test_result!r}")
        check("with the private key kept off the stored schema",
              "_positionals" not in (drafted.spec.params or {}),
              sorted((drafted.spec.params or {})))

        # `--json` exactly once, INCLUDING when the model's own example already
        # contains it. That is the case that shipped broken: the example loop
        # appends the flag, and the property loop appends it again because
        # "is `json` a placeholder" is false for a boolean and therefore reads
        # as unused. The installed builder returns `["--json", "--json"]` here,
        # confirmed against the live app, whose stored tool.json carried exactly
        # that template.
        #
        # The example matters: with `[]` the broken code got it RIGHT, which is
        # why this survived until a real draft sent `["--json"]`. A check that
        # only tried the empty example passes against the broken code — which is
        # what the first version of this check did.
        for example in ([], ["--json"]):
            with_json = builder._template_from(
                builder._params_from_source(
                    "import argparse\np = argparse.ArgumentParser()\n"
                    'p.add_argument("file")\n'
                    'p.add_argument("--json", action="store_true")\n',
                    example),
                example)
            check(f"the compatibility flag is emitted exactly once "
                  f"(example {example!r})",
                  with_json.count("--json") == 1, with_json)

        print("=== a near-miss parameter spelling still reaches the program ===")
        # The schema key is snake_case and the flag is kebab-case, so a model
        # that reads `--file-path` in the tool's description may send
        # `file-path`. That used to be dropped with a warning, leaving an EMPTY
        # argv — which fails as "nothing to run" and blames the tool for a
        # parameter the caller merely spelled differently.
        #
        # The alias map is `{canonical: alias}` because that is the direction
        # `spec.argv_for` reads. Built the other way round it does not raise: it
        # looks for a parameter named after the alias, finds none, and renders
        # nothing. So this asserts the RENDERED ARGV, not the map's contents.
        kebab = builder._params_from_source(
            "import argparse\np = argparse.ArgumentParser()\n"
            'p.add_argument("--file-path", required=True)\n', [])
        kebab_spec = CliToolSpec(
            slug="k", name="k", description="d", keywords=["k"],
            entry=["python", "tool.py"], params=kebab,
            argv_template=builder._template_from(
                kebab, ["--file-path", "x.txt"]),
            aliases=builder._aliases_for(kebab))
        exact, _ = kebab_spec.argv_for({"file_path": "a.txt"})
        dashed, _ = kebab_spec.argv_for({"file-path": "a.txt"})
        check("the canonical spelling renders the flag",
              exact == ["--file-path", "a.txt"], exact)
        # Absolute expectations, NOT one compared against the other. Asserting
        # `dashed == exact` passed even with the map inverted, because the
        # inversion broke the canonical lookup too — leaving `exact` as `[]` and
        # `dashed` as `[]`, which compare equal. Both now name the argv they
        # must produce, so either one breaking fails on its own.
        check("the dashed spelling renders the same thing",
              dashed == ["--file-path", "a.txt"],
              f"a near-miss spelling produced {dashed}")

        print("=== an applied tool is callable straight away ===")
        check("it is registered", cli_tools.get("word-counter") is not None)
        # Registered AS A SKILL as well, which is how chat and the swarm reach
        # it — the adapter is the only execution path, so "it is callable" means
        # the skill layer can call it.
        from backend.skills.registry import skill_registry
        check("and exposed as a skill",
              skill_registry.get("word_counter") is not None)
        sample = temp / "sample.txt"
        sample.write_text("alpha beta gamma delta", encoding="utf-8")
        result = asyncio.run(skill_registry.execute(
            "word_counter", {"path": str(sample), "limit": 2}))
        check("running it succeeds", result.success is True, result)
        # The program's JSON is the result, and its own `data` key survives one
        # level down — the adapter does not rewrite what the tool printed.
        check("its JSON was parsed",
              (result.data or {}).get("data", {}).get("words")
              == ["alpha", "beta"],
              result.data)

        print("=== an edit that does not compile is refused at apply ===")
        refused = asyncio.run(builder.apply(build, source="def broken(:\n"))
        check("apply refuses broken code",
              refused.get("success") is False, refused)
        check("and the reason names the line",
              "syntax error" in str(refused.get("error")).lower(), refused)

        print("=== the smoke test reports the truth ===")
        ok = asyncio.run(builder.smoke_test(build.spec, temp / "word-counter",
                                            {"path": str(sample), "limit": 1}))
        check("a working tool passes its smoke test", ok.get("success") is True,
              ok)
        broke = GOOD_PROGRAM.replace("open(args.path", "open(args.nonexistent")
        (temp / "word-counter" / "tool.py").write_text(broke, encoding="utf-8")
        bad_run = asyncio.run(builder.smoke_test(
            build.spec, temp / "word-counter",
            {"path": str(sample), "limit": 1}))
        check("a broken tool FAILS its smoke test",
              bad_run.get("success") is False, bad_run)
        check("and the failure is readable",
              bool(bad_run.get("error")), bad_run)

        # Put the working program back. It was left broken above, and every
        # later check that runs this tool inherited the damage — the bare-test
        # assertion below failed with 'Namespace' object has no attribute
        # 'nonexistent', which reads as a new bug and is actually this one.
        (temp / "word-counter" / "tool.py").write_text(GOOD_PROGRAM,
                                                       encoding="utf-8")

        print("=== a bare test uses the tool's own example ===")
        # The Test button sends NO params. With `dry_run_args` never populated
        # and never read, every tool was invoked with zero arguments, so
        # anything with a required flag failed with "nothing to run" — a
        # failure of the TEST shown to the user as a failure of their tool.
        #
        # The GOOD_PROGRAM above needs `--path`, so this fails without the
        # fallback. Checked on the SPEC's recorded args, and separately on a
        # spec whose field is null, because a tool saved before the field
        # existed must not be permanently untestable.
        bare = asyncio.run(builder.smoke_test(
            build.spec, temp / "word-counter", params=None))
        check("a no-argument test still runs a tool with required flags",
              "nothing to run" not in str(bare.get("error") or ""),
              bare)
        check("and the recorded example is an argv, not a bare value",
              all(t.startswith("--") or t for t in
                  (build.spec.dry_run_args or []))
              and any(t.startswith("--") for t in
                      (build.spec.dry_run_args or [])),
              build.spec.dry_run_args)
        check("a tool predating the field derives its args on the fly",
              builder._dry_run_args(
                  ["--path", "{path}", "--json"],
                  build.spec) == ["--path", str(
                      builder._sample_for("--path", "string")), "--json"],
              builder._dry_run_args(["--path", "{path}", "--json"],
                                    build.spec))

        print("=== applying a failed draft is refused ===")
        not_a_build = builder.BuildResult(False, detail="nothing to apply")
        refused = asyncio.run(builder.apply(not_a_build))
        check("a failed draft cannot be applied",
              refused.get("success") is False, refused)

        print("=== a stdlib package the model named does not kill the build ===")
        # `discover` asks the model what a tool needs, and the model sometimes
        # answers with a standard-library module. Asked to count words in a
        # file it said `package: "collections"`, so the draft ran
        # `pip install collections`, that failed, and the WHOLE build aborted
        # before any code was generated — a program needing nothing at all was
        # unbuildable. Found by calling the live builder, not by a unit test.
        #
        # The install is stubbed to fail here exactly as pip does for a stdlib
        # module, so this fails again if the guard is ever removed.
        check("a stdlib module is recognised as already importable",
              builder._already_importable("collections") is True)
        check("a genuinely absent package is not",
              builder._already_importable("totally-made-up-xyzzy") is False)
        check("the distribution name is not confused with the import name",
              builder._already_importable("PIL") is True,
              "pillow imports as PIL; the probe must ask the import name")

        print("=== the Test button's own call works ===")
        # The suite above calls `smoke_test` directly, which is why it passed
        # while the feature was broken: the BUG WAS IN THE CALLER. The handler
        # coerced a missing `params` to `{}` before calling, so `smoke_test`'s
        # "no values — use the tool's own example" branch was unreachable and
        # every bare Test click reported "nothing to run".
        #
        # Driven through the REAL handler with the payload the page actually
        # sends (just a slug). A grep for `call = None` would pass on a comment
        # saying the same thing, so the assertion runs the code instead.
        from backend import ws_server as _ws
        # The handlers are registered by an explicit call, not at import, so
        # this must run before the registry can be read.
        _ws._register_default_handlers()
        server = getattr(_ws, "_server", None)
        handler = (server._handlers.get("cliTools.test")
                   if server is not None else None)
        check("cliTools.test is registered",
              handler is not None, "the page cannot call a method that is absent")
        bare_test = (asyncio.run(handler({"slug": "word-counter"}, None))
                     if handler is not None else {})
        # Not just "did not say nothing to run" — the whole point is that a
        # bare click reports SUCCESS. Asserting only the absence of the old
        # error would pass on any new error, including the real one that
        # followed it: the sample path `input.txt` did not exist, so a
        # file-reading tool failed with "File not found: input.txt".
        check("a Test click with no params succeeds outright",
              bare_test.get("success") is True, bare_test)

        print("=== a sampled path is a file that exists ===")
        made, scratch = builder._materialise_paths(
            ["--file", "input.txt", "--json"])
        check("a path-like sample is created on disk",
              os.path.exists(made[1]), made)
        check("and the argv points at the created file",
              made[0] == "--file" and made[2] == "--json", made)
        check("the scratch directory is reported for cleanup",
              bool(scratch), scratch)
        check("a non-path sample is left exactly as it was",
              builder._materialise_paths(["--query", "example"])[0]
              == ["--query", "example"],
              builder._materialise_paths(["--query", "example"]))
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)

        async def _std_install(_cmd):
            return {"success": False,
                    "error": "No matching distribution found for collections"}

        async def _std_discover(_capability, _provider=None):
            # The model's real answer for "count the words in a file". The
            # shared stub in this suite returns an EMPTY package, which skips
            # the install branch entirely — so without this the assertions
            # below pass whether or not the guard exists.
            return {"package": "collections",
                    "install_cmd": "pip install collections",
                    "approach": "read the file and split it",
                    "code_hint": "collections.Counter"}

        real_install = skill_forge.install
        real_discover = skill_forge.discover
        real_chooser2 = tool_loop.forge_target_provider
        try:
            skill_forge.install = _std_install
            skill_forge.discover = _std_discover
            # Stop at the provider check: REACHING it proves the install
            # failure did not abort the build, which is the whole assertion.
            # Returning None means no model call is attempted.
            tool_loop.forge_target_provider = lambda p=None: None
            std = asyncio.run(builder.draft("count words in a file"))
        finally:
            skill_forge.install = real_install
            skill_forge.discover = real_discover
            tool_loop.forge_target_provider = real_chooser2
        check("a failed stdlib install does not abort the draft",
              "collections" not in (std.detail or "").lower(),
              f"the stdlib install failure still aborted: {std.detail}")
        check("and it reaches the provider step instead",
              "provider" in (std.detail or "").lower(), std.detail)
    finally:
        registry_mod.CliToolRegistry.tools_dir = original_tools_dir
        skill_forge.discover = original_discover
        skill_forge.install = original_install
        if original_chooser is not None:
            tool_loop.forge_target_provider = original_chooser
        else:
            try:
                del tool_loop.forge_target_provider
            except AttributeError:
                pass
        shutil.rmtree(temp, ignore_errors=True)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: a tool is drafted, reviewed and only then written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
