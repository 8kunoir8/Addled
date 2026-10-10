"""Turn a capability the user asked for into a CLI tool they can review.

The forge writes a *skill*: an async Python function the app imports and calls.
That is not what this builds. A CLI tool is a standalone program with its own
entry point, its own `--help`, and a JSON contract on stdout, so that the same
thing the agent runs is the thing the user can run from a terminal and debug.
The skill layer is only an adapter over it (`adapter.py`), which is why a tool
kept working after the forge's interface changed and why a tool the user wrote
by hand is indistinguishable from one built here.

Drafting and applying are deliberately separate steps, and the separation is the
safety property that matters:

- `draft()` calls the model and returns source text. It touches nothing.
- `apply()` writes that source to disk and registers the tool. It is only
  reached once a human has seen the code on the Settings page.

Nothing here invents a new way to call a model. Discovery and dependency
installation are the forge's, because a tool that needs `Pillow` needs it
installed into the app's own writable package directory by the same verified
code path the forge already uses.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.cli_tools.spec import CliToolSpec

log = logging.getLogger("addled.cli_tools")


@dataclass
class BuildResult:
    """A drafted tool, or the reason there is not one.

    `source` is text and `spec` is metadata. Neither has been written anywhere
    until `apply()` is called with this object.
    """

    success: bool
    slug: str = ""
    spec: CliToolSpec | None = None
    source: str = ""
    detail: str = ""
    # What discovery decided the tool needs. Carried so the Settings page can
    # say "this needs the `pillow` package" before the user approves anything.
    package: str = ""
    install_cmd: str = ""
    # The example invocation the model supplied, so a smoke test can use a real
    # input instead of an empty one.
    test_params: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # What happened when the draft was actually run, before the user saw it.
    # `None` when the draft never got that far. A reviewer reading `success:
    # true` with no test is reading a claim; with this, they are reading a
    # demonstration.
    test_result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "slug": self.slug,
            "source": self.source,
            "detail": self.detail,
            "package": self.package,
            "install_cmd": self.install_cmd,
            "test_params": self.test_params,
            "warnings": list(self.warnings),
            "spec": self.spec.to_dict() if self.spec else None,
            "test_result": self.test_result,
        }


_PROMPT = """\
Write a standalone Python command-line program that does this:

{capability}

{context}
It must follow this contract exactly, because a separate program invokes it and
parses its output:

- Use only the standard library, unless a package is named above.
- Use `argparse`. Every option is `--kebab-case`.
- Read every input from NAMED OPTIONS. Nothing from stdin, nothing interactive,
  no prompts, and **no positional arguments**: write `add_argument("--file")`,
  never `add_argument("file")`. The caller builds the command line from option
  names, so a positional cannot be supplied and the tool fails every call with
  `the following arguments are required: file`.
- On success print ONE JSON object to stdout and exit 0. The object MUST
  include `"success": true`, plus any data under a `"data"` key.
- On failure print `{{"success": false, "error": "<what went wrong>"}}` to
  stdout and exit non-zero. A human must be able to read the error and know
  what to fix.
- Never print anything to stdout except that single JSON object. Logging, if
  any, goes to stderr.
- Add a short `--json` flag that is accepted for compatibility even if the
  output is always JSON.
- Guard the entry point with `if __name__ == "__main__":`.

Output ONLY the Python source. No markdown fences, no explanation, no notes.

After the program, on its own final line, add an example invocation:

EXAMPLE_ARGV = ["--example-flag", "a realistic value"]

It is used to smoke-test the program, so every value must be one that would
actually succeed. If the program takes no options, write: EXAMPLE_ARGV = []
"""


def _installed(name: str) -> bool:
    """Whether the interpreter that will RUN the tool can import `name`.

    Checked rather than assumed. Discovery suggests a package from what the open
    web says about the task, which is a good guess at the right library and no
    evidence at all that it is installed here. A live draft was told pandas
    "will already be installed", imported it, and the tool died on
    `ModuleNotFoundError` the first time it was tested — after a full generation
    had been spent writing code around it.
    """
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _context_block(package: str, approach: str, code_hint: str) -> str:
    """What discovery learned, phrased for the generation prompt."""
    lines = []
    if package and package not in ("unknown", ""):
        if _installed(package):
            lines.append(f"- The task is best done with the `{package}` package, "
                         f"which IS available in the interpreter that runs you. "
                         f"Import it normally.")
        else:
            # Saying "not installed" without saying what to do instead invites
            # the model to import it anyway. Name the fallback explicitly.
            lines.append(f"- `{package}` is NOT available here and importing it "
                         f"will crash the tool at startup. Use the standard "
                         f"library instead.")
    if approach:
        lines.append(f"- Suggested approach: {approach}")
    if code_hint:
        lines.append(f"- Reference: {code_hint}")
    return ("\n".join(lines) + "\n\n") if lines else ""


def _extract_argv(source: str) -> tuple[str, list[str]]:
    """Split the trailing EXAMPLE_ARGV line off the program source.

    Mirrors what the forge does with `TEST_PARAMS`, and for the same reason: the
    example is not part of the program, but it is the only honest input to test
    it with. A program that requires `--path` cannot be checked with no
    arguments, and would report a failure its own code would have passed.
    """
    import ast
    import re

    match = re.search(r"^\s*EXAMPLE_ARGV\s*=\s*(.+?)\s*$", source,
                      re.MULTILINE | re.DOTALL)
    if not match:
        return source, []
    argv: list[str] = []
    try:
        parsed = ast.literal_eval(match.group(1).strip())
        if isinstance(parsed, (list, tuple)):
            argv = [str(v) for v in parsed]
    except (ValueError, SyntaxError, TypeError):
        log.debug("could not read EXAMPLE_ARGV from the generated program")
    # Removed from the source either way: a stray assignment is harmless but it
    # is not part of what runs.
    return (source[:match.start()] + source[match.end():]).strip(), argv


def _slug_from(text: str, *, taken: set[str] | None = None) -> str:
    """A valid `CliToolSpec` slug for a capability description.

    The spec's own regex is the contract, so this produces something that passes
    it rather than something that merely looks right. Uniqueness is handled here
    rather than by the caller because two capabilities with the same first few
    words are common ("convert a video to …").
    """
    import re

    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    # Drop the filler a request is usually padded with, so "please fetch me the
    # prices" does not become `please-fetch-me-the-prices`.
    stop = {"a", "an", "the", "me", "my", "i", "to", "for", "of", "and", "or",
            "please", "can", "you", "it", "this", "that", "some", "with", "on",
            "in", "is", "be", "do", "then"}
    keep = [w for w in words if w not in stop] or words
    slug = "-".join(keep[:4])[:40].strip("-")
    if not slug or not slug[0].isalnum():
        slug = f"tool-{slug}".strip("-")[:40]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", slug):
        slug = re.sub(r"[^a-z0-9_-]", "-", slug)[:40].strip("-") or "tool"
    if taken:
        base, i = slug, 2
        while slug in taken:
            suffix = f"-{i}"
            slug = (base[:40 - len(suffix)] + suffix)
            i += 1
    return slug


def _name_from(slug: str) -> str:
    """The skill-facing name for a slug.

    The rest of the app addresses tools by an identifier, and the spec requires
    a valid Python-ish name — so the slug's dashes become underscores rather
    than there being a second naming scheme to keep in step.
    """
    import re

    name = slug.replace("-", "_")
    name = re.sub(r"[^a-z0-9_]", "", name.lower())[:40]
    if not name or not name[0].isalpha():
        name = f"tool_{name}"
    return name


def _already_importable(package: str) -> bool:
    """Can `package` be imported right now, without installing anything?

    Used to tell a real missing dependency from a spurious one. `discover` asks
    the model what a tool needs and the model sometimes answers with a
    standard-library module (`collections`, `json`); `pip install` those cannot
    succeed, so the failed install must not be read as "this tool cannot be
    built".

    Resolved in a SUBPROCESS, like the forge's own probe, so nothing here
    mutates this process's `sys.path` — but unlike that probe the answer is
    advisory: a probe that cannot run returns False, which leaves the install's
    own verdict standing rather than inventing a pass.

    `-s` matches how the app runs and keeps a user site-packages directory out
    of the answer; the app's own libs are appended so a package installed into
    `PYLIBS_DIR` earlier in this same build is still found.
    """
    import subprocess
    import sys

    if not package or not package.isidentifier():
        return False
    paths: list[str] = []
    try:
        from backend import app_paths
        paths.append(str(app_paths.PYLIBS_DIR))
    except Exception:  # noqa: BLE001
        pass
    probe = (
        "import importlib.util, sys\n"
        "for p in sys.argv[2:]:\n"
        "    if p not in sys.path:\n"
        "        sys.path.insert(0, p)\n"
        f"print('OK' if importlib.util.find_spec({package!r}) else 'NO')\n"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-s", "-c", probe] + paths,
            capture_output=True, text=True, timeout=20)
    except Exception as e:  # noqa: BLE001
        log.debug("import probe for %r could not run: %s", package, e)
        return False
    return out.stdout.strip().endswith("OK")

async def draft(
    capability: str,
    *,
    slug: str | None = None,
    provider=None,
    name_hint: str | None = None,
) -> BuildResult:
    """Write a CLI program for `capability`, and RUN IT before returning.

    One retry, not a loop. The first attempt is the model's ordinary work; if it
    runs and fails, the error is real evidence about code that exists, and a
    second attempt told about it is likely to be right. Beyond that the failures
    stop being slips and start being "this capability does not fit the shape",
    where more attempts burn generations without improving the answer — so the
    second result is shown to the user whether or not it passes.

    A failing draft is STILL RETURNED for review. The point of testing here is
    to replace "this might work" with "here is what happened", not to withhold
    the source from someone who may want to fix it by hand.
    """
    result = await _draft_once(capability, slug=slug, provider=provider,
                               name_hint=name_hint)
    if not result.success or not result.test_result:
        return result
    if result.test_result.get("success"):
        return result
    # `untested` means the harness could not run it at all, so the failure says
    # nothing about the code and feeding it back would only mislead the model.
    if result.test_result.get("untested"):
        return result
    # A fixture problem is a fault in the SAMPLE, not in the program. Retrying
    # asks the model to correct code that was never wrong, and it will oblige —
    # producing a different program that fails on the same bad sample. So the
    # draft is returned as-is and the reason is reported where the user can see
    # it, rather than spent on a generation that cannot help.
    if result.test_result.get("fixture"):
        result.warnings.append(
            "This looks like a problem with the sample value used to test it, "
            "not with the code — the tool was handed a path it could not use. "
            "It may well work when called properly.")
        return result

    failure = str(result.test_result.get("error") or "")
    log.info("draft for %r failed its own test; retrying once: %s",
             capability[:60], failure[:200])
    retried = await _draft_once(capability, slug=slug, provider=provider,
                                name_hint=name_hint, last_failure=failure)
    if not retried.success:
        # The retry produced nothing usable. Show the FIRST draft, which at
        # least exists, rather than replacing a reviewable tool with an error.
        return result
    if retried.test_result and retried.test_result.get("success"):
        retried.warnings.append(
            "The first attempt failed its test; this is the second, which ran "
            "successfully.")
    else:
        retried.warnings.append(
            "This is the second attempt, after the first failed its test. It "
            "also failed — the error is above.")
    return retried


async def _draft_once(
    capability: str,
    *,
    slug: str | None = None,
    provider=None,
    name_hint: str | None = None,
    last_failure: str | None = None,
) -> BuildResult:
    """Write a CLI program for `capability`. Writes nothing to disk.

    Returns a `BuildResult` whose `success` is False with a readable `detail`
    when the model is unavailable, the code does not compile, or nothing usable
    came back — never raising into a websocket handler.
    """
    from backend.skills.forge import skill_forge
    # The provider chooser lives with the forge CALL SITE rather than in
    # `forge.py`, and it decides which model writes code. Imported here rather
    # than at module scope because `tool_loop` imports the CLI tool helpers.
    from backend.skills.tool_loop import forge_target_provider

    capability = str(capability or "").strip()
    if not capability:
        return BuildResult(False, detail="No capability was described.")
    if len(capability) > 2000:
        capability = capability[:2000]

    provider = forge_target_provider(provider)

    # Discovery and dependency install are the forge's, unchanged. A tool that
    # needs a package needs it in the app's own writable directory, and that
    # path is already verified (pip's exit code alone does not prove an import
    # will work) — re-implementing it here would be a second, unverified copy.
    discovery: dict = {}
    try:
        # `web=False` because the web step OPENS A VISIBLE CHROMIUM WINDOW.
        # The user clicks "Write the tool" expecting a wait, not a browser
        # appearing over their desktop with no explanation. It also mostly
        # hurt: it is what suggested `collections` as a pip package.
        discovery = await skill_forge.discover(capability, provider,
                                               web=False) or {}
    except Exception as e:  # noqa: BLE001
        log.debug("discovery failed for %r: %s", capability[:60], e)
        discovery = {}

    package = str(discovery.get("package") or "")
    install_cmd = str(discovery.get("install_cmd") or "")
    if install_cmd and package not in ("unknown", ""):
        # `discover` asks the MODEL what this needs, and the model sometimes
        # names a standard-library module: asked to count words it answered
        # `package: "collections"`. `pip install collections` cannot succeed, so
        # treating that as fatal made a program needing nothing at all
        # unbuildable — the whole draft died before any code was generated.
        #
        # So a failed install is only fatal when the module is genuinely
        # missing. If it already imports, there was nothing to install and the
        # failure is spurious. This deliberately does not second-guess a real
        # missing dependency: that install DID need to work.
        try:
            installed = await skill_forge.install(install_cmd)
        except Exception as e:  # noqa: BLE001
            installed = {"success": False, "error": str(e)}
        if not installed.get("success") and not _already_importable(package):
            return BuildResult(
                False,
                detail=("Could not install the "
                        f"'{package}' package this needs: "
                        f"{str(installed.get('error') or '')[:300]}"),
                package=package, install_cmd=install_cmd)
        if not installed.get("success"):
            log.info("no install needed for %r: it already imports", package)

    feedback = ""
    if last_failure:
        # Appended to the CONTEXT rather than woven into the contract, because
        # the contract is the stable part of the prompt and this is one run's
        # correction. The previous attempt ran and FAILED, so the error is a
        # fact about real code rather than a guess about what might go wrong —
        # which is what makes one retry worth a second generation.
        feedback = ("\nYour previous attempt FAILED when it was actually run. "
                    "Here is exactly what happened:\n\n"
                    f"{last_failure[:1200]}\n\n"
                    "Write a corrected program that does not have this "
                    "problem.\n\n")

    prompt = _PROMPT.format(
        capability=capability,
        context=_context_block(package, str(discovery.get("approach") or ""),
                               str(discovery.get("code_hint") or ""))
        + feedback)

    if provider is None:
        return BuildResult(
            False, detail="No AI provider is available to write the tool.",
            package=package, install_cmd=install_cmd)

    # Budgeting is the forge's too: a small-context model must be told when the
    # prompt cannot fit, rather than truncating and generating nonsense.
    try:
        # `budget` and `router` are MODULES, not classes — `from ... import
        # budget` here would bind the function rather than the module and every
        # call would fail with an AttributeError.
        from backend.providers import budget, router

        pid = str(getattr(provider, "provider_id", "") or "")
        prompt, max_tokens = budget.fit_single_prompt(prompt, pid,
                                                      want_reply=6000)
        if max_tokens <= 0:
            return BuildResult(
                False,
                detail=(f"{pid or 'The model'} cannot hold the prompt for this "
                        "tool. Switch provider, or build it with a larger "
                        "model."),
                package=package, install_cmd=install_cmd)
        result = await provider.chat(
            [{"role": "user", "content": prompt}],
            model=router.for_provider(provider, "reasoning"),
            max_tokens=max_tokens, temperature=0.3)
    except Exception as e:  # noqa: BLE001
        return BuildResult(False, detail=f"Provider error: {e}",
                           package=package, install_cmd=install_cmd)

    if not getattr(result, "ok", False):
        return BuildResult(
            False, detail=f"Provider error: {getattr(result, 'error', '')}",
            package=package, install_cmd=install_cmd)

    source = str(getattr(result, "response", "") or "").strip()
    if "```" in source:
        source = source.split("```")[1] if len(source.split("```")) > 1 else source
        if source.startswith("python"):
            source = source[6:]
        source = source.strip()

    source, example_argv = _extract_argv(source)

    # It has to compile before a human is asked to read it. Handing over source
    # with a syntax error wastes the review, and `apply` would write a file that
    # cannot run at all.
    import ast

    try:
        ast.parse(source)
    except SyntaxError as e:
        return BuildResult(
            False, detail=f"The generated program has a syntax error: {e}",
            source=source, package=package, install_cmd=install_cmd)

    if not source.strip():
        return BuildResult(False, detail="The model returned no code.",
                           package=package, install_cmd=install_cmd)

    # A program with no entry point would register, appear in the catalogue and
    # then do nothing when called.
    if "__main__" not in source:
        return BuildResult(
            False,
            detail=("The generated program has no `if __name__ == "
                    "'__main__':` entry point, so running it would do nothing."),
            source=source, package=package, install_cmd=install_cmd)

    chosen_slug = slug or _slug_from(name_hint or capability)
    name = _name_from(chosen_slug)

    params = _params_from_source(source, example_argv)
    # Taken off the schema here: everything downstream treats `params` as a
    # plain JSON Schema, and this is deliberately not one of its keys.
    positionals = params.pop("_positionals", [])
    template = _template_from(params, example_argv)
    spec = CliToolSpec(
        slug=chosen_slug,
        name=name,
        description=_describe(capability),
        keywords=_keywords(capability),
        entry=["python", "tool.py"],
        params=params,
        argv_template=template,
        # The spellings a model may plausibly send for each parameter, so a
        # near-miss resolves instead of being dropped. `argv_for` already knew
        # how to use aliases; nothing ever populated them, so any key that was
        # not the exact snake_case name was discarded with a warning and the
        # call went out with an EMPTY argv — which then failed as "nothing to
        # run", blaming the tool for a parameter the caller spelled differently.
        #
        # Only forms the flag itself implies: `--file-path` means `file_path`,
        # `file-path` and `filePath` all address the same argument. No guessing
        # at semantic synonyms — `path` is NOT added for `--file-path`, because
        # that is a different word and silently accepting it would let a wrong
        # value through looking correct.
        aliases=_aliases_for(params),
        requires_approval=False,
        category="cli",
    )

    # A runnable example invocation, stored ON THE SPEC so it survives to disk
    # and reaches every caller. `dry_run_args` was declared for exactly this
    # ("args that exercise the tool without side effects, for the build-time
    # smoke test") and then never populated or read by anything — so the Test
    # button invoked every tool with NO arguments, and a tool with a required
    # flag failed its own test with "nothing to run". That reads as "your tool
    # is broken" when the tool was fine and the call was empty.
    #
    # Read back out of the TEMPLATE rather than the model's raw example, so a
    # flag the schema rejected cannot come back in through the test.
    spec.dry_run_args = _dry_run_args(template, spec) or None
    warnings = _check_argv_template(spec)
    if positionals:
        # Reported HERE, from the parsed source, because the template-based
        # check cannot see a positional at all — it is precisely the thing a
        # template has no slot for. Left unreported, the tool registers, lists,
        # passes validation, and then fails every call with
        # `the following arguments are required: <name>`.
        warnings.append(
            "This tool requires a value by POSITION "
            f"({', '.join(positionals)}), which Addled cannot supply — every "
            "call will fail. Rename it to an option, like `--input`, or edit "
            "the code below before applying.")
    # Run it, HERE, before a human is asked to review it. Validation only says
    # the manifest is well-formed; this says the program actually works. The
    # difference matters, because a tool that fails its first real call was
    # previously indistinguishable from a working one on the review screen: it
    # was saved, listed in the catalogue, offered to chat and the swarm, and
    # failed every time it was used. The user found out from a chat reply, long
    # after the draft that caused it was cheap to regenerate.
    #
    # Run from a TEMPORARY directory, so a draft still writes nothing
    # permanent — the promise in this function's docstring.
    # A positional cannot be reached by a schema-derived invocation at all, so
    # running one would PASS a tool that fails every real call — the exact false
    # pass this test exists to prevent. The model's `EXAMPLE_ARGV` is used when
    # it exists, since it can supply a positional; when it does not, a sample is
    # substituted for each positional so the program is still driven properly
    # and argparse is given the chance to complain.
    if positionals:
        override = list(example_argv) if example_argv else _positional_argv(
            example_argv, positionals)
        test = await _test_draft(source, spec, argv_override=override)
    else:
        test = await _test_draft(source, spec)
    if test.get("success"):
        warnings.append("Ran it: the test invocation succeeded.")
    else:
        warnings.append(
            "Ran it and it FAILED: "
            f"{test.get('error') or 'no output was produced'}")
    return BuildResult(True, slug=chosen_slug, spec=spec, source=source,
                       detail="Ready to review.", package=package,
                       install_cmd=install_cmd, warnings=warnings,
                       test_result=test)


def _describe(capability: str) -> str:
    """One line for the catalogue. Truncated, because the brief lists them."""
    text = " ".join(str(capability or "").split())
    return text[:200] if text else "A tool built for this capability"


def _keywords(capability: str) -> list[str]:
    """The words a later call can be matched on.

    Taken from the capability rather than the code: the user asked for this in
    their own words, and those are the words they will use again.
    """
    import re

    stop = {"a", "an", "the", "me", "my", "i", "to", "for", "of", "and", "or",
            "please", "can", "you", "it", "this", "that", "some", "with", "on",
            "in", "is", "be", "do", "from", "into", "then", "get", "make"}
    words = [w for w in re.findall(r"[a-z0-9]+", str(capability or "").lower())
             if w not in stop and len(w) > 2]
    seen: list[str] = []
    for word in words:
        if word not in seen:
            seen.append(word)
    return seen[:12]


def _params_from_source(source: str, example_argv: list[str]) -> dict:
    """A JSON Schema for the program's options, read from its argparse setup.

    Read from the source rather than guessed, because the schema is what the
    model is told to fill in: a flag that exists but is not declared here can
    never be set by the agent, and the tool would be permanently mis-called.
    """
    import re

    properties: dict[str, dict] = {}
    required: list[str] = []
    # Positional arguments, detected SEPARATELY because the option pattern below
    # requires a leading `--` and so cannot see them at all. A positional is
    # then invisible to everything downstream: it is absent from the schema, so
    # the caller never supplies it, and every call dies with
    # `the following arguments are required: file`. One live draft produced
    # exactly that, from code that was otherwise correct.
    #
    # Returned alongside the schema rather than raised, so `draft` can report it
    # to the person reviewing the tool while still showing them the source.
    positionals: list[str] = []
    for match in re.finditer(
            r"add_argument\(\s*[\"']([A-Za-z_][A-Za-z0-9_-]*)[\"']", source):
        name = match.group(1)
        if name not in positionals:
            positionals.append(name)


    for match in re.finditer(
            r"add_argument\(\s*[\"']--([a-z0-9-]+)[\"']([^)]*)\)",
            source, re.IGNORECASE | re.DOTALL):
        flag, rest = match.group(1), match.group(2)
        key = flag.replace("-", "_")

        if re.search(r"action\s*=\s*[\"']store_true[\"']", rest):
            kind = "boolean"
        elif re.search(r"action\s*=\s*[\"']count[\"']", rest):
            kind = "integer"
        else:
            # `type=int` is the common way a program says "this is a number",
            # and getting it wrong tells the model to send "10" where the code
            # does arithmetic on it. Read from the source rather than assumed.
            if re.search(r"type\s*=\s*(int|float)", rest):
                kind = "number"
            elif re.search(r"nargs\s*=\s*[\"']?\+|action\s*=\s*[\"']append[\"']",
                           rest):
                kind = "array"
            else:
                kind = "string"

        entry: dict = {"type": kind, "description": f"--{flag}"}
        # `required` is read from the source, NOT inferred from what is
        # missing. argparse decides this itself and says so: an option is
        # required only when it was declared `required=True`.
        #
        # The old rule excluded any flag with a `default` or an `nargs`, and
        # any boolean or integer. All four inferences are wrong, and each fails
        # in a different direction:
        #   `nargs="+", required=True`  was dropped  → the tool could not be
        #       called at all, because a flag it needs could never be set
        #   `add_argument("--name")`    was ADDED    → argparse treats it as
        #       optional, so the schema demanded a value nothing needs
        # It also made `--count required=True` required only by accident of its
        # type. Reading the one keyword argparse reads removes the guessing.
        explicit_required = re.search(r"\brequired\s*=\s*True\b", rest) is not None
        default = re.search(r"\bdefault\s*=\s*([^,)]+)", rest)
        if default is not None:
            literal = _literal(default.group(1).strip())
            entry["default"] = literal
            # A default of `10` is an integer even when the program forgot
            # `type=int`, and the schema is what the model reads.
            entry["type"] = _widen(kind, literal)
        # A flag that takes no value cannot be "required" in a useful sense —
        # there is nothing to supply — so it stays out even if declared so.
        properties[key] = entry
        if explicit_required and kind != "boolean":
            required.append(key)

    # `--json` is in the contract the prompt asks for and is always safe to
    # send, so it is declared even if the model omitted the line. Without it a
    # template containing `--json` fails spec validation outright.
    properties.setdefault("json", {"type": "boolean",
                                   "description": "Accepted for compatibility; "
                                                  "output is always JSON."})

    # Carried on the schema under a private key so the signature stays put and
    # callers can read it without another parameter to thread through. Removed
    # again before the schema reaches anything that would reject a stray key.
    return {"type": "object", "properties": properties, "required": required,
            "_positionals": positionals}


def _literal(text: str):
    """A default value as Python, or the text if it is not a literal.

    `default=None` must become a real None and not the string "None", because it
    is copied into the schema the model reads.
    """
    import ast

    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text


def _widen(kind: str, default) -> str:
    """The schema type a default value implies, if it is more specific.

    A `default=10` is an integer whatever `type=` says, and a schema that calls
    it a `number` still tells the model to send `10.0` where the program does
    list indexing. The default is a value read from the real source, so it is
    the more trustworthy of the two signals and it wins.
    """
    if isinstance(default, bool):
        return "boolean"
    if isinstance(default, int):
        return "integer"
    if isinstance(default, float):
        # A float default under `type=int` is contradictory; the integer reading
        # is the one that will not silently change the value's meaning.
        return "integer" if kind == "integer" else "number"
    if isinstance(default, (list, tuple)):
        return "array"
    if isinstance(default, dict):
        return "object"
    return kind


def _positional_argv(example_argv: list[str],
                     positionals: list[str]) -> list[str]:
    """An invocation that feeds the POSITIONAL slots, from `example_argv`.

    Non-flag tokens are taken from the example in order and handed back as the
    positional values, so a program the model demonstrated as
    `["a.txt", "--verbose"]` is tested as `["a.txt"]` rather than with the
    positional silently dropped.

    A file-shaped sample is created by `smoke_test`'s existing path
    materialisation, so a positional naming a path is not reported as missing.
    If nothing usable is found, the parameter NAME is passed instead: that is
    not a valid path, and the tool failing on it is the honest outcome. What
    must not happen is the argument being ABSENT — which is how a program that
    requires a positional came to pass its own smoke test.
    """
    values = [t for t in (example_argv or []) if not str(t).startswith("-")]
    out: list[str] = []
    for index, name in enumerate(positionals):
        if index < len(values):
            out.append(str(values[index]))
        else:
            lowered = str(name).lower()
            out.append(f"{name}.txt" if lowered.endswith(("file", "path"))
                       else str(name))
    return out


def _template_from(params: dict, example_argv: list[str]) -> list[str]:
    """The argv skeleton, built from the example the model supplied.

    The example is a real, working invocation, so its shape is the shape the
    template should have. Flags it names become placeholders; anything else in
    the schema becomes a placeholder too, so nothing is unreachable.

    A flag in the example that the SCHEMA does not declare is dropped. The
    schema is read from the program's own `add_argument` calls, so it is the
    only authority on what the program accepts; the example is written by the
    model and can be wrong.
    """
    template: list[str] = []
    index = 0
    while index < len(example_argv):
        item = example_argv[index]
        if item.startswith("--"):
            key = item.lstrip("-").replace("-", "_")
            if key in params.get("properties", {}):
                if (params["properties"][key].get("type") == "boolean"
                        or index + 1 >= len(example_argv)
                        or example_argv[index + 1].startswith("--")):
                    template.append(item)
                else:
                    template.extend([item, "{" + key + "}"])
                    index += 1
            else:
                # The schema comes from the program's real `add_argument` calls,
                # which are authoritative. This flag is not among them, so the
                # program does not accept it — which happens because the model
                # writes `EXAMPLE_ARGV` by hand and invents a name: asked to
                # count words it implemented `--input/-i` but wrote
                # `EXAMPLE_ARGV = ["--file", ...]`.
                #
                # Passing it through (the old behaviour) produced a tool that
                # registered, appeared in the catalogue, and failed on every
                # call with `unrecognized arguments: --file` — the schema
                # validated, so nothing complained until it was used.
                # The flag itself is dropped and any value after it with it, so
                # the stray token cannot be left behind as a positional.
                log.debug("dropping unknown flag %r from the argv template",
                          item)
                if (index + 1 < len(example_argv)
                        and not example_argv[index + 1].startswith("-")):
                    index += 1
        index += 1

    used = {t.strip("{}") for t in template if t.startswith("{")}
    for key, schema in params.get("properties", {}).items():
        if key in used:
            continue
        flag = "--" + key.replace("_", "-")
        if schema.get("type") == "boolean":
            # Only the compatibility flag is added blindly: it is documented as
            # always-safe. Anything else might change behaviour, and a wrong
            # default flag is worse than a missing one.
            continue
        template.extend([flag, "{" + key + "}"])
    # `--json` is appended AFTER the dedupe, never during the property loop
    # above. Adding it in that loop put the flag past the point where duplicates
    # were removed, and the `not in seen_flags` guard at the end then added it
    # again — a live draft produced `["--json", "--json"]` exactly that way.
    # Harmless to argparse for a store_true flag, but it is a malformed
    # template, and the same ordering mistake on a flag that takes a VALUE is a
    # real mis-call.
    # A repeated flag is dropped WITH ITS VALUE. Skipping only the flag left
    # its placeholder behind — `["--tags", "{tags}", "{tags}"]` — which is a
    # second positional argument the program never asked for, and the mis-call
    # the earlier version of this comment predicted. The program wants one
    # value for `--tags`; an example that repeats the flag to show "this can be
    # given more than once" does not mean the schema can express it.
    #
    # Deduplicating by FIRST APPEARANCE keeps the model's own ordering, which is
    # the one that reads like the `--help` text it wrote.
    deduped: list[str] = []
    seen_flags: set[str] = set()
    skip_value = False
    for token in template:
        if token.startswith("-"):
            skip_value = token in seen_flags
            if skip_value:
                continue
            seen_flags.add(token)
            deduped.append(token)
            continue
        if skip_value:
            # The placeholder belonging to the repeated flag.
            skip_value = False
            continue
        skip_value = False
        deduped.append(token)
    template = deduped
    if "--json" not in seen_flags:
        template.append("--json")
    return template


def _aliases_for(params: dict) -> dict:
    """Alias spellings per parameter, keyed `{canonical: alias}`.

    The direction is `spec.argv_for`'s: it reads `self.aliases` as
    `{real: alt}` and rewrites `alt` into `real` before rendering. Getting it
    backwards does NOT raise — it looks for a parameter named after the alias,
    finds none, and the call renders as an EMPTY argv, which then fails as
    "nothing to run" and blames the tool.

    Only forms the flag NAME itself implies. A model that has read
    `--file-path` in the tool's description may send `file-path` or `filePath`,
    and both unambiguously mean `file_path`. It may not send `path`, and mapping
    that would be inventing a synonym — a wrong value would then be accepted and
    look correct.
    """
    aliases: dict[str, str] = {}
    for key in (params.get("properties") or {}):
        if key == "json":
            continue
        if "_" in key:
            aliases.setdefault(key, key.replace("_", "-"))
    return aliases


def _dry_run_args(template: list[str], spec: CliToolSpec) -> list[str]:
    """A concrete argv to smoke-test with, read back out of `template`.

    The template is `["--input", "{input}", "--top", "{top}", "--json"]`, so the
    placeholder names pair it with the flag immediately before them. Reading the
    values out of the TEMPLATE rather than the model's raw example matters: the
    template has already had every flag the schema rejected removed, so a
    parameter that no longer exists cannot be passed back in.

    Only REQUIRED parameters get a value. A value for an optional flag is a
    guess — `--top 10` is not obviously better than the program's own default —
    and inventing one changes what the test exercises. Required parameters have
    no default to fall back on, which is exactly why they must be filled.

    A sample value is chosen by type and shaped like the flag it belongs to, so
    `--input` gets a path and `--url` gets a URL. That is not cosmetic: an
    `--input` that wants a file and receives `"example"` fails inside `open()`,
    and the user is shown an error from a tool that was never broken.
    """
    required = set(spec.params.get("required") or [])
    props = spec.params.get("properties") or {}
    # Walk the template as `("--flag", "{key}")` pairs. A placeholder is
    # meaningless without the flag in front of it, and handling them together
    # is what keeps the two from drifting apart.
    out: list[str] = []
    index = 0
    while index < len(template):
        token = template[index]
        nxt = template[index + 1] if index + 1 < len(template) else ""
        takes_value = (token.startswith("--")
                       and nxt.startswith("{") and nxt.endswith("}"))
        if takes_value:
            key = nxt.strip("{}")
            # Optional parameters are left to the program's own defaults:
            # inventing `--top 10` would change what the test exercises.
            if key in required:
                kind = (props.get(key) or {}).get("type", "string")
                sample = _sample_for(token, kind)
                out.append(token)
                if kind == "array":
                    out.extend(str(v) for v in sample)
                else:
                    out.append(str(sample))
            index += 2
            continue
        # A flag with no placeholder after it takes no value (`--json`), so it
        # is safe and useful to send.
        if token.startswith("--"):
            out.append(token)
        index += 1
    return out


def _materialise_paths(argv: list[str]) -> tuple[list[str], str | None]:
    """Create real files for any argument that looks like a path to one.

    Returns the argv with those arguments repointed, and the scratch directory
    to remove afterwards (or None if nothing was created).

    The sample values in `_dry_run_args` are shapes, not files: `--file` gets
    `"input.txt"`, which lets the program parse its arguments and then dies with
    "File not found". The user sees a red Test on a tool that is perfectly fine,
    because the test handed it a filename that was never going to exist.

    Recognised by a set of extensions rather than by the flag name, because the
    flag says what the value is FOR and this needs to know what it IS. A tool
    taking `--input` as a literal string must not get a file, and one taking
    `--source` as a path must.
    """
    import os
    import tempfile

    path_like = (".txt", ".csv", ".json", ".jsonl", ".log", ".md", ".yaml",
                 ".yml", ".xml", ".html", ".tsv")
    scratch: str | None = None
    out: list[str] = []
    for index, arg in enumerate(argv):
        previous = argv[index - 1] if index else ""
        # Only the VALUE of a flag is a candidate, never the flag itself.
        if not previous.startswith("-"):
            out.append(arg)
            continue

        # A DIRECTORY sample is created as a directory, with a file and a
        # subdirectory inside it, so a tool that lists a folder has something to
        # list. Recognised by the sample itself rather than by extension,
        # because a directory has no extension to match on and "does it end in
        # .txt" is the wrong question for a value meant to be a folder. Missed
        # here, a correct tool failed its own smoke test with
        # `NotADirectoryError: not a directory: ...\input.txt`.
        if arg == "example_dir":
            if scratch is None:
                scratch = tempfile.mkdtemp(prefix="addled-cli-smoke-")
            created = os.path.join(scratch, "example_dir")
            try:
                os.makedirs(os.path.join(created, "subdir"), exist_ok=True)
                with open(os.path.join(created, "sample.txt"), "w",
                          encoding="utf-8") as handle:
                    handle.write("alpha\nbeta\ngamma\n")
                out.append(created)
                continue
            except OSError as e:  # noqa: BLE001
                log.debug("could not create %r for the smoke test: %s",
                          created, e)

        if arg.lower().endswith(path_like):
            if scratch is None:
                scratch = tempfile.mkdtemp(prefix="addled-cli-smoke-")
            created = os.path.join(scratch, os.path.basename(arg) or "input.txt")
            try:
                with open(created, "w", encoding="utf-8") as handle:
                    # Content the tool can count, parse or summarise. Small
                    # enough to be harmless, non-empty so a tool that reports
                    # "0 rows" is not mistaken for one that did nothing.
                    handle.write("alpha\nbeta\ngamma\n")
                out.append(created)
                continue
            except OSError as e:  # noqa: BLE001
                log.debug("could not create %r for the smoke test: %s",
                          created, e)
        out.append(arg)
    return out, scratch


def _sample_for(flag: str, kind: str):
    """A plausible value for `flag`, by its declared type and name."""
    name = flag.lstrip("-").lower()
    if kind == "boolean":
        return True
    if kind in ("integer", "number"):
        return 1
    if kind == "array":
        return ["example"]
    if any(w in name for w in ("url", "uri", "endpoint", "host")):
        return "https://example.com"
    # Directories BEFORE files, because "dir" is not a kind of "file" and
    # `input.txt` is not a directory. A live draft asked to list a folder's
    # contents declared `--directory` and was handed `input.txt`; the program
    # correctly refused it with `NotADirectoryError`, and the smoke test
    # reported a failure in a tool that was written exactly right. The sample
    # was the only thing that was wrong.
    if any(w in name for w in ("dir", "folder")):
        return "example_dir"
    if any(w in name for w in ("path", "file", "input", "source")):
        return "input.txt"
    return "example"


def _check_argv_template(spec: CliToolSpec) -> list[str]:
    """Validate the drafted spec, and report rather than reject.

    A draft is shown to a human, so a validation complaint is information they
    need — but a draft that fails validation must not look like a draft that
    passed. The warning list is the honest middle.
    """
    warnings: list[str] = []
    try:
        error = spec.validate()
        if error:
            warnings.append(error)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"could not validate the drafted tool: {e}")

    # A positional argument cannot be expressed in the schema, which is built
    # from `add_argument("--flag")` calls. The prompt asks for `--kebab-case`
    # options, but nothing enforces it, and when the model ignores that the tool
    # is silently uncallable: the required positional is invisible, so every
    # call from chat or the swarm omits it and argparse refuses.
    #
    # Reported rather than rejected. The draft is shown to a human who can fix
    # the flag, and refusing outright would throw the work away over something
    # they may prefer to edit themselves.
    positionals = _positionals(spec)
    if positionals:
        warnings.append(
            f"This tool takes a value that must be passed by position "
            f"({', '.join(positionals)}), which Addled cannot supply — calls "
            "will fail. Give it an option name instead, like `--input`.")
    return warnings


def _positionals(spec: CliToolSpec) -> list[str]:
    """Describe any positional slots the template contains.

    Detected from the TEMPLATE rather than the schema, because a positional is
    precisely what the schema cannot contain. A slot that is neither a flag nor
    a `{placeholder}` is one.
    """
    names: list[str] = []
    for index, token in enumerate(spec.argv_template or []):
        if token.startswith("-") or (token.startswith("{")
                                     and token.endswith("}")):
            continue
        names.append(f"slot {index + 1} ({token!r})")
    return names


async def apply(build: BuildResult, *, source: str | None = None,
                built_by: str = "user") -> dict:
    """Write a drafted tool to disk and register it.

    `source` overrides the drafted text, which is how an edit made on the
    Settings page is respected: the user's version is what is written, not the
    model's. Everything is validated before the first byte is written, so a
    rejected tool leaves no half-made directory behind.
    """
    import ast
    import json
    import os

    from backend.cli_tools.registry import cli_tools

    if not build or not build.success or build.spec is None:
        return {"success": False,
                "error": build.detail if build else "Nothing to apply."}

    text = (source if source is not None else build.source) or ""
    if not text.strip():
        return {"success": False, "error": "There is no code to write."}

    # The edited text is what runs, so it is what has to compile.
    try:
        ast.parse(text)
    except SyntaxError as e:
        return {"success": False,
                "error": f"The code has a syntax error on line {e.lineno}: "
                         f"{e.msg}"}

    spec = build.spec
    try:
        error = spec.validate()
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"The tool is not valid: {e}"}
    if error:
        return {"success": False, "error": error}

    try:
        directory = cli_tools.tools_dir() / spec.slug
        directory.mkdir(parents=True, exist_ok=True)
        # Written to a temporary name first and moved into place, so a tool is
        # never half-registered: the manifest appearing is what `load()` will
        # treat as "this tool exists".
        program = directory / "tool.py"
        staged = directory / "tool.py.tmp"
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, program)

        manifest = directory / "tool.json"
        staged_manifest = directory / "tool.json.tmp"
        staged_manifest.write_text(json.dumps(spec.to_dict(), indent=2),
                                   encoding="utf-8")
        os.replace(staged_manifest, manifest)
    except Exception as e:  # noqa: BLE001
        log.warning("could not write the built tool: %s", e)
        return {"success": False, "error": f"Could not write the tool: {e}"}

    try:
        cli_tools.add(spec, directory, built_by=built_by)
    except Exception as e:  # noqa: BLE001
        log.warning("could not register the built tool: %s", e)
        return {"success": False, "error": f"Could not register the tool: {e}"}

    log.info("Built CLI tool %s (%s)", spec.slug, spec.name)
    return {"success": True, "slug": spec.slug, "name": spec.name,
            "directory": str(directory)}


async def _test_draft(source: str, spec: CliToolSpec,
                      argv_override: list[str] | None = None) -> dict:
    """Run a NOT-YET-SAVED draft, from a temporary directory.

    A thin wrapper over `smoke_test`, which already knows how to pick an
    invocation, materialise the file paths it names, and interpret the result.
    The only thing it needs that a draft does not have is a directory holding
    `tool.py`, so one is made, used, and removed.

    Kept SEPARATE from `smoke_test` rather than folded into it, because the two
    ask different questions. This one asks "is this draft worth showing?", which
    is a question about text and has to tolerate a tool that has not been
    written anywhere. `smoke_test` asks "does the saved tool work?", which is
    what the Test button means and what the user is told afterwards.
    """
    import shutil
    import tempfile
    from pathlib import Path

    scratch = Path(tempfile.mkdtemp(prefix="addled-cli-draft-"))
    try:
        (scratch / "tool.py").write_text(source, encoding="utf-8")
        return await smoke_test(spec, scratch, params=None,
                                argv_override=argv_override)
    except Exception as e:  # noqa: BLE001
        # A failure to test is not a failure of the tool. Reported as such, so
        # the draft is not rejected over a problem in the harness.
        return {"success": False, "error": f"could not test the draft: {e}",
                "untested": True}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def smoke_test(spec: CliToolSpec, source_dir,
                     params: dict | None = None,
                     argv_override: list[str] | None = None) -> dict:
    """Run a built tool once, so "it works" is demonstrated and not assumed.

    Deliberately separate from validation: validation says the manifest is
    well-formed, this says the program actually runs. A tool can pass every
    static check and still fail on its first real call — an import that is not
    installed, a flag the template sends that argparse rejects — and the user
    should learn that here, while they are looking at the code, rather than
    later from a chat reply.
    """
    from backend.cli_tools.runner import run_cli

    # With no params, fall back to the example invocation recorded on the spec.
    # Without this the Test button called every tool with NO arguments, so any
    # tool with a required flag reported "nothing to run" — a failure of the
    # TEST, shown to the user as a failure of their tool. `dry_run_args` is
    # exactly what that field is for; it simply was never read.
    #
    # An explicit `params` always wins, including an empty dict: a caller that
    # deliberately passes nothing is asking for a no-argument run.
    #
    # `dry_run_args` is the recorded answer, but a tool saved BEFORE that field
    # was populated has it as null and would otherwise be permanently
    # untestable. It is derived from the spec's own `required` list in that
    # case, which is what says which flags have no default to fall back on.
    # A caller-supplied `argv_override` WINS, and is not clobbered here. It is
    # how the draft-time test runs a program whose invocation the schema cannot
    # express — a positional, which has no template slot and therefore never
    # reaches `dry_run_args`. Overwriting it silently turned every positional
    # tool into a passing draft that failed every real call.
    if argv_override is None:
        if params is not None:
            argv_override = None
        else:
            params = {}
            argv_override = (
                list(spec.dry_run_args) if spec.dry_run_args
                else _dry_run_args(spec.argv_template, spec)) or None
    # A sample that names a file must name a file that EXISTS, or the tool
    # fails its smoke test with "File not found: input.txt" — which is the test
    # catching its own placeholder, not a fault in the tool. Found by running
    # the built tool: the flags and the values were right, and the only thing
    # wrong was that nothing had been created at the path.
    #
    # Written into a temp directory rather than the working tree, and cleaned up
    # afterwards, so a smoke test leaves nothing behind.
    scratch: str | None = None
    if argv_override:
        argv_override, scratch = _materialise_paths(argv_override)
    try:
        result = await run_cli(spec, source_dir, params or {},
                               argv_override=argv_override)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"the tool raised: {e}"}
    finally:
        if scratch:
            import shutil as _shutil
            _shutil.rmtree(scratch, ignore_errors=True)

    # `run_cli` reports a tool's own verdict directly: on success it merges the
    # program's JSON into the result and sets nothing else, and `exit_code` is
    # only present on the failure path. Requiring it here failed every working
    # tool, which is exactly the false negative a smoke test must not have.
    ok = bool(result.get("success"))
    error = "" if ok else (result.get("error")
                           or str(result.get("stderr") or "")[:400]
                           or "the tool reported a failure")
    detail = (str(result.get("stdout") or "")
              + str(result.get("stderr") or ""))
    return {
        "success": ok,
        "exit_code": result.get("exit_code"),
        "stdout": str(result.get("stdout") or "")[:4000],
        "stderr": str(result.get("stderr") or "")[:2000],
        "parsed": result.get("parsed"),
        "error": error,
        # Whether the failure is about the SAMPLE rather than the program.
        #
        # A tool can be refused by its own test because the fixture it was
        # handed is wrong — a file where a directory was wanted, an extension
        # the sample creator knows and the tool does not. From the outside that
        # looks exactly like broken code: the retry runs, fails the same way,
        # and the user is told twice that their tool is broken when the tool is
        # correct and the FIXTURE is not.
        #
        # A retry cannot fix a bad fixture, and asking the model to correct a
        # program that was never wrong wastes a generation and teaches it
        # nothing. Naming the distinction does not repair the fixture either,
        # but it stops the user being sent to look in the wrong place.
        "fixture": "" if ok else _looks_like_a_fixture_problem(
            detail, error, list(argv_override or [])),
    }


def _looks_like_a_fixture_problem(output: str, error: str,
                                  argv: list[str]) -> bool:
    """Is this failure about the sample we supplied rather than the program?

    Requires BOTH a path-shaped error and one of the paths we actually passed,
    named in the message. The first version of this checked only the error,
    which flagged a tool that hardcoded `open("definitely-not-here.txt")` — real
    breakage in the program — as a problem with the test fixture. Excusing a
    genuine fault is worse than the vagueness it removes, so the match is made
    against OUR OWN sample values rather than the wording alone.
    """
    text = f"{output}\n{error}".lower()
    markers = (
        "notadirectoryerror", "isadirectoryerror", "filenotfounderror",
        "no such file or directory", "cannot find the path",
        "cannot find the file", "not a directory", "is a directory",
    )
    if not any(marker in text for marker in markers):
        return False
    for value in argv:
        text_value = str(value)
        if len(text_value) < 3:
            continue
        # Compare on the basename: the tool may report the path as it received
        # it or as it resolved it, and only the name is stable across the two.
        name = text_value.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if name and name in text.replace("\\", "/").lower():
            return True
    return False
