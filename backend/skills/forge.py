"""
Skill Forge — self-extending agent capabilities.

When Addled encounters a task it can't handle, the Forge:
  1. Searches the web for a solution (library, tool, approach)
  2. Installs required packages via pip/npm
  3. Generates a Python skill wrapper using the LLM
  4. Validates the new skill with a test call
  5. Registers it in the SkillRegistry for immediate use
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from backend.skills.registry import SkillDefinition, skill_registry

log = logging.getLogger("addled.forge")

from backend import app_paths

FORGE_DIR = app_paths.subdir("forged_skills")
FORGE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class ForgeResult:
    success: bool
    skill_name: str
    action: str  # "found_existing", "installed", "generated", "failed"
    detail: str = ""
    skill_code: str = ""
    # The example call the generator asked the model for, carried here so
    # `forge` can validate the skill with a real input instead of `{}`. Without
    # it, a skill that takes a parameter fails its own test.
    test_params: dict | None = None


def pip_argv(install_cmd: str) -> list[str] | None:
    """The command line that installs a package, or None if it is not one.

    `pip install X` has to become `python -m pip install X` for the interpreter
    that is running, so the package lands where the generated skill will import
    it from. This used to drop the wrong word and run `python -m install X`,
    which fails with "No module named install" — so every forge dependency
    install failed and generation carried on without the package it had just
    decided it needed.

    It also has to target a WRITABLE directory. A plain `pip install` writes to
    the interpreter's own site-packages, which is `C:\\Program Files\\...` for an
    installed Addled and therefore read-only — so every forge dependency failed
    with `WinError 5: Access is denied` and the skill was written without it.
    The browser and vision installers already solved this with
    `--target <PYLIBS_DIR>`; the forge simply was not updated when the app moved
    under Program Files.

    `-s` matches how the app itself runs. Without it pip can write to a
    user-site directory the app does not search, and the package is installed
    but invisible.
    """
    cmd = (install_cmd or "").strip()
    if not cmd.startswith(("pip ", "pip3 ", "python -m pip ")):
        return None
    args = cmd.split()
    body = args[1:] if args[0] in ("pip", "pip3") else args[3:]

    try:
        from backend import app_paths
        # Make packages installed here importable straight away, and idempotent
        # so a second forge does not stack duplicate path entries.
        app_paths.add_pylibs_to_path()
        target = str(app_paths.PYLIBS_DIR)
    except Exception as e:  # noqa: BLE001
        # Fall back to the old behaviour rather than refusing to install: an
        # unreadable path config is a reason to try, not a reason to give up.
        log.debug("could not resolve the pylibs directory: %s", e)
        return [sys.executable, "-s", "-m", "pip"] + body

    return [sys.executable, "-s", "-m", "pip"] + body + [
        "--target", target, "--no-warn-script-location",
    ]

def importable_name(install_cmd: str) -> str:
    """The import name a package is checked under, best effort.

    Used to VERIFY an install rather than trusting pip's exit code: a `--target`
    install into a directory the process does not search still exits 0, which is
    the failure the browser installer's own comment warns about.
    """
    cmd = (install_cmd or "").strip()
    if not cmd.startswith(("pip ", "pip3 ", "python -m pip ")):
        return ""
    args = cmd.split()
    body = args[1:] if args[0] in ("pip", "pip3") else args[3:]
    for token in body:
        if token.startswith("-") or token in ("install", "upgrade", "-U"):
            continue
        # `package==1.2` / `package[extra]` -> `package`
        return token.split("==")[0].split("[")[0].split(">")[0].strip()
    return ""

def _import_probe(name: str) -> str:
    """Whether a package is importable, and why not if it is not.

    Run in a SUBPROCESS, because importing is not something this process can
    undo: a package whose import has a side effect, or one that fails partway,
    would be felt by the running app. A fresh interpreter answers the only
    question that matters — can anything import this now — without touching
    this process's state.

    The directory to search is passed as an ARGUMENT, not imported.

    This used to have the child do `from backend import app_paths`, which fails:
    `-c` puts '' (the cwd) on `sys.path`, not the install's `resources`, and the
    `except` swallowed the ModuleNotFoundError. The probe therefore never put
    `pylibs` on its path and could only see the packages bundled in the app's
    own site-packages. So a dependency correctly installed into `pylibs` was
    reported MISSING, and `install()` returned failure for a SUCCESSFUL
    install — the opposite of the bug this probe exists to prevent. Found by
    running the real install path against the installed app, where the two
    directories actually differ.

    A probe that cannot run reports "no problem found" (""), so a broken probe
    can never block a working install.
    """
    probe = (
        "import importlib.util, sys\n"
        "target = sys.argv[1]\n"
        "if target and target not in sys.path:\n"
        "    sys.path.insert(0, target)\n"
        f"print('OK' if importlib.util.find_spec({name!r}) else 'MISSING')\n"
    )
    try:
        pylibs = str(app_paths.PYLIBS_DIR)
    except Exception:  # noqa: BLE001
        # Without a directory to add, the probe would test the wrong paths and
        # could report a false MISSING — the exact failure described above.
        # Answering "no problem found" leaves the exit-code result standing.
        return ""
    try:
        result = subprocess.run(
            [sys.executable, "-s", "-c", probe, pylibs],
            capture_output=True, text=True, timeout=90,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        # A probe that cannot run is not proof of failure. Reporting it as one
        # would block a working install, so this answers "no problem found".
        return ""
    if result.returncode == 0 and "OK" in (result.stdout or ""):
        return ""
    return (f"pip reported success but '{name}' still cannot be imported, so "
            f"the skill would fail on its first call. "
            f"{(result.stdout or result.stderr or '').strip()[:200]}")


def defined_async_functions(code: str) -> list[str]:
    """The names of the top-level async functions a generated module defines.

    The forge asks the model for a function named after the skill, but the name
    is a slug of the task text ("get_this_machine_hostname") while a model will
    naturally write something of its own ("probe_machine_name"). Binding
    ``SKILL_DEF.handler`` to the requested name then produced a module that
    raised `NameError: name 'get_this_machine_hostname' is not defined` at
    import — so the skill was written, failed to load, and was deleted again.
    Reading the name back out of the code is what makes the binding true.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    return [node.name for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)]


def build_skill_module(skill_name: str, package: str, task_description: str,
                       code: str, params_schema: dict) -> str:
    """The complete generated module, ready to write and import.

    Assembled from parts rather than one indented template. `textwrap.dedent`
    removes only the indentation every line shares, and generated code starts at
    column 0, so a template's own imports kept their 16 spaces and the module
    would not parse — every forged skill failed with "IndentationError:
    unexpected indent" on the template's `from dataclasses import dataclass`.

    The purpose is collapsed to a single line first. It is model-written text, so
    it can contain a newline, and a newline in the header comment ends the
    comment and turns the rest of the sentence into code — the module then loads
    as `NameError: name 'newline' is not defined`. A one-line description is what
    the prompt catalogue needs as well.

    The handler is bound to the function the code actually defines, falling back
    to the requested name only when there is nothing to bind to.
    """
    purpose = " ".join(str(task_description or "").split())[:200]
    defined = defined_async_functions(code)
    handler = defined[0] if defined else skill_name
    return "\n".join([
        f"# Auto-generated skill: {skill_name}",
        f"# Package: {package}",
        f"# Purpose: {purpose}",
        "",
        "from backend.skills.registry import SkillDefinition",
        "",
        code.strip(),
        "",
        "SKILL_DEF = SkillDefinition(",
        f"    name={skill_name!r},",
        f"    description={purpose!r},",
        f"    parameters={params_schema!r},",
        f"    handler={handler},",
        '    category="forged",',
        ")",
        "",
    ])


class SkillForge:
    """
    Self-extending capability layer.

    Flow:
      User asks for something → no matching skill →
      forge.discover(task) → forge.install(library) →
      forge.generate(task, library, provider) → skill_registry.register() →
      forge.validate(skill) → execute!
    """

    def __init__(self):
        self._forged: dict[str, SkillDefinition] = {}
        self._load_forged()

    def _missing_dependency(self, path) -> str:
        """The package a forged skill declares but cannot import, if any.

        A skill is written with a `# Package: <name>` header. When the forge
        could not install that package it used to carry on anyway and register
        the skill regardless — so the file imports cleanly (the `import` is
        inside the handler), loads on every start, and fails only when someone
        calls it:

            {'success': False, 'error': "Missing required package 'webcolors':
             No module named 'webcolors'"}

        That is inherited state, not a new bug: a skill forged by an older build
        stays broken after an upgrade. Checking the declared package here is what
        lets a bad skill be reported instead of quietly loading, and it makes an
        existing broken skill discoverable rather than invisible.

        Returns "" when the dependency is present or none is declared.
        """
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            return ""
        m = re.search(r"^#\s*Package:\s*(\S+)", head, re.MULTILINE)
        if not m:
            return ""
        package = m.group(1).strip()
        if not package or package.lower() in ("unknown", "none", "stdlib", "-"):
            return ""
        # The import name is usually the package name; a wheel can differ, so a
        # miss here is only reported when the plain name is also absent.
        mod = package.replace("-", "_").split("==")[0].split("[")[0]
        try:
            if importlib.util.find_spec(mod) is not None:
                return ""
        except (ImportError, ModuleNotFoundError, ValueError):
            pass
        return package

    def _load_forged(self):
        """Load previously forged skills from disk.

        A skill whose declared package is missing is NOT registered. It cannot
        work, so registering it only means every future start loads something
        that will fail when called, and the failure looks like a broken skill
        rather than the missing dependency it is.
        """
        for f in FORGE_DIR.glob("*.py"):
            try:
                name = f.stem
                missing = self._missing_dependency(f)
                if missing:
                    log.warning(
                        "Forged skill %s needs '%s', which is not installed — "
                        "not loading it. Forge it again now that the forge "
                        "installs dependencies into a writable location.",
                        name, missing)
                    continue
                # Load the skill module dynamically
                spec = importlib.util.spec_from_file_location(
                    f"forged_{name}", str(f))
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    if hasattr(mod, "SKILL_DEF"):
                        skill = mod.SKILL_DEF
                        self._forged[name] = skill
                        skill_registry.register(skill)
                        log.info("Loaded forged skill: %s", name)
            except Exception as e:
                log.warning("Failed to load forged skill %s: %s", f.name, e)

    def delete(self, name: str) -> bool:
        """Remove a forged skill from disk + registry."""
        p = FORGE_DIR / f"{name}.py"
        ok = False
        if p.exists():
            p.unlink()
            ok = True
        self._forged.pop(name, None)
        skill_registry.unregister(name)
        if ok:
            log.info("Deleted forged skill: %s", name)
        return ok

    # ── Discovery ────────────────────────────────────────────────────────

    async def discover(self, task_description: str, provider=None) -> dict:
        """
        Search for a solution to a task the agent can't handle.
        Returns: {package, install_cmd, approach, confidence}
        """
        # Try web search first
        search_error = ""
        try:
            from backend.browser.browser_engine import browser
            import urllib.parse
            query = f"python library {task_description} pip install"
            nav = await browser.navigate(
                f"https://duckduckgo.com/?q={urllib.parse.quote(query)}")
            if nav.get("success"):
                extract = await browser.extract()
                snippet = extract.get("text", "")[:3000]
            else:
                snippet = ""
                search_error = str(nav.get("error") or "navigation failed")
        except Exception as e:
            snippet = ""
            search_error = f"{type(e).__name__}: {e}"
            # Playwright is an optional install, so its absence is expected on
            # many machines — but a *silent* empty snippet made a broken
            # browser indistinguishable from a search that found nothing, and
            # discovery then fell through to the web-less fallback with no
            # explanation anywhere.
            log.info("Forge discovery web search skipped: %s", search_error)

        # Ask the LLM to suggest a solution
        if provider:
            try:
                prompt = (
                    f"The user wants to: {task_description}\n\n"
                    f"Web search results:\n{snippet[:1500]}\n\n"
                    f"Suggest the BEST Python library to accomplish this task. "
                    f"Respond with a JSON object:\n"
                    f'{{"package": "package_name", "install_cmd": "pip install X", '
                    f'"approach": "brief description of how to use it", '
                    f'"confidence": "high|medium|low", '
                    f'"import_statement": "from X import Y", '
                    f'"code_hint": "example usage code"}}'
                )
                from backend.providers import router
                from backend.providers import budget
                pid = str(getattr(provider, "provider_id", "") or "")
                prompt, max_tokens = budget.fit_single_prompt(
                    prompt, pid, want_reply=1000)
                if max_tokens <= 0:
                    fallback = self._discovery_fallback(task_description)
                    fallback["note"] = (
                        f"{pid or 'The model'} has no room for the discovery "
                        "prompt.")
                    return fallback
                result = await provider.chat(
                    [{"role": "user", "content": prompt}],
                    model=router.for_provider(provider, "reasoning"),
                    max_tokens=max_tokens, temperature=0.3,
                )
                if result.ok:
                    text = result.response.strip()
                    if "```" in text:
                        text = text.split("```")[1]
                        if text.startswith("json"):
                            text = text[4:]
                        text = text.strip()
                    return json.loads(text)
            except Exception as e:
                # A model that answered with prose instead of JSON used to be
                # indistinguishable from one that was never asked.
                log.info("Forge discovery via model failed: %s", e)

        # Fallback: no usable answer from the web or the model
        fallback = self._discovery_fallback(task_description)
        if search_error:
            fallback["note"] = f"web search unavailable ({search_error})"
        return fallback

    @staticmethod
    def _discovery_fallback(task_description: str) -> dict:
        """What discovery returns when no model could be consulted."""
        return {
            "package": "unknown",
            "install_cmd": "",
            "approach": f"Search the web for: python {task_description}",
            "confidence": "low",
            "import_statement": "",
            "code_hint": "",
        }

    # ── Installation ─────────────────────────────────────────────────────

    async def install(self, install_cmd: str) -> dict:
        """Install a Python package into the app's own writable package dir.

        Verified rather than trusted. pip's exit code alone is not proof: a
        `--target` install into a directory the running process does not search
        still exits 0, and the package is then installed but unusable — which is
        worse than a failure, because the skill generates and only breaks on its
        first real call.
        """
        if not install_cmd:
            return {"success": False, "error": "No install command provided"}

        try:
            argv = pip_argv(install_cmd)
            if argv is None:
                return {"success": False,
                        "error": f"Unsafe install command: {install_cmd.strip()}"}
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=300,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                return {"success": False, "error": result.stderr[-500:]}

            # Prove it can actually be imported before reporting success.
            name = importable_name(install_cmd)
            if name:
                problem = _import_probe(name)
                if problem:
                    return {"success": False, "error": problem}

            log.info("Package installed: %s", " ".join(argv[1:]))
            return {"success": True, "output": result.stdout[-500:]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Install timed out"}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}

    # ── Generation ───────────────────────────────────────────────────────

    async def generate(
        self,
        skill_name: str,
        task_description: str,
        discovery: dict,
        provider=None,
    ) -> ForgeResult:
        """
        Generate a new skill definition from task description + discovery.
        Uses the LLM to write the handler function.
        """
        package = discovery.get("package", "unknown")
        import_stmt = discovery.get("import_statement", "")
        code_hint = discovery.get("code_hint", "")
        approach = discovery.get("approach", "")

        if not provider:
            return ForgeResult(False, skill_name, "failed",
                              "No AI provider available for code generation")

        # A slug of the task text can be long and unlovely — "python library
        # implement a function called scrape_website pip install" truncates to
        # "a_tool_named_scrape_website_th". Asked for that, a model writes
        # something of its own anyway, so the name is a preference and the
        # module binds to whatever was actually defined.
        requested = skill_name if len(skill_name) <= 40 else ""
        name_line = (f"            - Be async: `async def {requested}(params: dict) -> dict:`"
                     if requested else
                     "            - Be async: `async def <a_short_snake_case_name>(params: dict) -> dict:`")
        start_line = (f"            Start with: async def {requested}(params: dict) -> dict:"
                      if requested else
                      "            Start with: async def <a_short_snake_case_name>(params: dict) -> dict:")

        prompt = textwrap.dedent(f"""
            Generate a Python async function that wraps the capability: {task_description}

            Package to use: {package}
            Import: {import_stmt}
            Approach: {approach}
            Example: {code_hint}

            The function must:
{name_line}
            - Accept a `params` dict with relevant parameters
            - Return a dict with at least {{"success": True/False}}
            - Handle errors gracefully with try/except
            - Be self-contained (imports inside the function)

            Output ONLY the function code. No markdown, no explanation.
{start_line}

            Then, after the function, on its own line, add an example call:

            TEST_PARAMS = {{"<param>": <a realistic value>}}

            This is used to TEST the skill after it is written, so it must be a
            complete, valid call that would succeed. If the function takes no
            parameters, write: TEST_PARAMS = {{}}
        """)

        skill_path = None
        try:
            from backend.providers import router
            from backend.providers import budget
            pid = str(getattr(provider, "provider_id", "") or "")
            # The reserve covers REASONING as well as the answer.
            #
            # It was 2000, which is enough for a model that answers directly and
            # not for one that thinks first. The local qwen3-8b spent all 2000
            # tokens reasoning and returned no content at all
            # (finish_reason "length", 9063 chars of reasoning, 0 of code); at
            # 6000 the same prompt produced the function in 637 tokens. So the
            # budget was the whole problem, and the forge reported it as a
            # provider error it could not explain.
            #
            # 6000 rather than 4000 for headroom: the reasoning length varies
            # with the task, and a generation that runs out mid-answer wastes
            # the entire call. `reply_budget` still caps this at whatever the
            # context window allows, so a small-context model is unaffected.
            prompt, max_tokens = budget.fit_single_prompt(
                prompt, pid, want_reply=6000)
            if max_tokens <= 0:
                return ForgeResult(
                    False, skill_name, "failed",
                    f"{pid or 'The model'} cannot hold the code-generation "
                    f"prompt. Switch provider, or forge with a larger model.")
            result = await provider.chat(
                [{"role": "user", "content": prompt}],
                model=router.for_provider(provider, "reasoning"),
                max_tokens=max_tokens, temperature=0.3,
            )
            if not result.ok:
                return ForgeResult(False, skill_name, "failed",
                                  f"Provider error: {result.error}")

            code = result.response.strip()
            if "```" in code:
                code = code.split("```")[1]
                if code.startswith("python"):
                    code = code[6:]
                code = code.strip()

            # Split off the example call the prompt asks for, if the model
            # supplied one. It is not part of the handler, so it must not go
            # into the module's function code — but it IS the only sane input
            # to validate the skill with.
            #
            # Without it, `validate()` called the handler with `{}` and any
            # skill that needs a parameter failed its own test, reporting
            # "failed validation: None" while the generated code was perfectly
            # correct. That is a false negative on the most common kind of
            # skill, and it left the skill registered-but-marked-failed.
            test_params: dict | None = None
            m = re.search(r"^\s*TEST_PARAMS\s*=\s*(.+?)\s*$", code,
                          re.MULTILINE | re.DOTALL)
            if m:
                try:
                    parsed = ast.literal_eval(m.group(1).strip())
                    if isinstance(parsed, dict):
                        test_params = parsed
                except (ValueError, SyntaxError):
                    log.debug("could not read TEST_PARAMS from generated code")
                # Remove it from the code either way: a stray assignment is
                # harmless but it is not part of the function.
                code = code[:m.start()] + code[m.end():]
                code = code.strip()

            # Validate syntax
            try:
                ast.parse(code)
            except SyntaxError as e:
                return ForgeResult(False, skill_name, "failed",
                                  f"Generated code has syntax error: {e}")

            # Generate parameter schema from function
            params_schema = self._infer_params(code, task_description)

            # Make sure optional packages are importable BEFORE the generated
            # module is loaded.
            #
            # The app calls this at startup, so it is normally already done —
            # but a forged skill's whole point is that it imports a package
            # which was fetched on demand, and the forge is the one place that
            # must not assume the path is ready. Without it, validation of a
            # skill whose dependency IS installed fails with "No module named
            # 'humanize'", which reads as a broken dependency rather than a
            # missing path entry.
            try:
                from backend import app_paths
                app_paths.add_pylibs_to_path()
            except Exception as e:  # noqa: BLE001
                log.debug("could not add pylibs to the path: %s", e)

            # Create the full skill module
            module_code = build_skill_module(skill_name, package,
                                             task_description, code,
                                             params_schema)

            # Save to disk
            skill_path = FORGE_DIR / f"{skill_name}.py"
            skill_path.write_text(module_code, encoding="utf-8")

            # Load and register
            spec = importlib.util.spec_from_file_location(
                f"forged_{skill_name}", str(skill_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            if hasattr(mod, "SKILL_DEF"):
                skill = mod.SKILL_DEF
                self._forged[skill_name] = skill
                skill_registry.register(skill)
                log.info("Forged new skill: %s (package: %s)", skill_name, package)
                return ForgeResult(True, skill_name, "generated",
                                  skill_code=code,
                                  detail=f"Generated and registered skill '{skill_name}' using {package}",
                                  test_params=test_params)
            # Nothing can use it, so it must not stay where every start will try
            # to load it.
            skill_path.unlink(missing_ok=True)
            return ForgeResult(False, skill_name, "failed",
                              "Generated code has no SKILL_DEF")

        except Exception as e:
            log.exception("Skill generation failed: %s", e)
            # A file that does not compile has to go. It is loaded on every
            # start, so leaving it behind logs a failure forever and hands the
            # user a skill they never asked for — which is exactly what a tool
            # name like `get_screen_size()` used to produce.
            if skill_path is not None:
                try:
                    skill_path.unlink(missing_ok=True)
                    log.info("Removed %s: it did not load", skill_path.name)
                except OSError:
                    pass
            return ForgeResult(False, skill_name, "failed", str(e))

    # ── Validation ───────────────────────────────────────────────────────

    async def validate(self, skill_name: str, test_params: dict | None = None) -> dict:
        """Test a newly forged skill to ensure it works.

        `test_params` is the example call the generator asked the model for.
        Passing it matters: called with `{}`, any skill that takes a parameter
        returns its own "missing parameter" error, so the skill fails its own
        test while being correct. See the TEST_PARAMS handling in `generate`.
        """
        skill = skill_registry.get(skill_name)
        if not skill:
            return {"success": False, "error": f"Skill {skill_name} not found"}

        try:
            result = await skill.handler(test_params or {})
            if isinstance(result, dict):
                if result.get("success", False):
                    return {"success": True, "output": result,
                            "skill": skill_name}
                # The handler ran and said no. That is a normal failure, not an
                # exception, so it has no `error` on the exception path — and
                # `forge` read `validation.get('error')`, which was absent,
                # producing the empty "failed validation: None". Say the real
                # reason instead.
                reason = str(result.get("error") or "the skill reported "
                             "failure with no reason")
                if test_params is None:
                    reason += (" (it was tested with no arguments; if it "
                               "needs input, that is why)")
                return {"success": False, "error": reason,
                        "output": result, "skill": skill_name}
            return {"success": False, "error": "Handler returned non-dict"}
        except Exception as e:
            # Remove broken skill
            skill_path = FORGE_DIR / f"{skill_name}.py"
            if skill_path.exists():
                skill_path.unlink()
            log.warning("Forged skill %s failed validation, removed: %s",
                       skill_name, e)
            return {"success": False, "error": str(e)}

    # ── Full Forge Pipeline ──────────────────────────────────────────────

    async def forge(
        self,
        task_description: str,
        provider=None,
        auto_validate: bool = True,
    ) -> ForgeResult:
        """
        Full pipeline: discover → install → generate → validate.
        Called when agent can't find a matching skill for a user task.
        """
        log.info("Forging skill for: %s", task_description[:80])

        # 1. Discover
        discovery = await self.discover(task_description, provider)
        if discovery.get("confidence") == "low":
            # Still try, but note uncertainty
            pass

        package = discovery.get("package", "")
        install_cmd = discovery.get("install_cmd", "")
        note = str(discovery.get("note") or "")

        # 2. Install the dependency it needs.
        #
        # A failure STOPS the forge. It used to log and carry on, on the theory
        # that the package might already be present — which produced a skill
        # whose first real call failed on an ImportError, in a way the user
        # could not trace back to a forge that had quietly given up. A skill
        # that cannot run is worse than an honest "I could not install what it
        # needs", so the reason is returned instead.
        if install_cmd and package not in ("unknown", ""):
            install_result = await self.install(install_cmd)
            if not install_result["success"]:
                log.warning("Forge dependency install failed: %s",
                            install_result.get("error"))
                return ForgeResult(
                    False, self._make_skill_name(task_description), "failed",
                    f"Could not install the '{package}' package this needs: "
                    f"{str(install_result.get('error') or '')[:300]}")

        # 3. Generate skill name from task
        skill_name = self._make_skill_name(task_description)

        # 4. Generate
        result = await self.generate(skill_name, task_description, discovery, provider)
        if not result.success:
            if note:
                result.detail = f"{result.detail} (discovery: {note})"
            return result

        # 5. Validate
        if auto_validate:
            # Tested with the model's own example call, not `{}`. Called with
            # nothing, a skill that needs a parameter reports its own "missing
            # parameter" error and fails a test its code would have passed.
            validation = await self.validate(skill_name, result.test_params)
            if not validation["success"]:
                # A skill the forge calls failed must NOT stay registered and on
                # disk. `generate` registers it before returning, so without
                # this the app carries a live skill that every future start
                # loads, while the forge reported failure — and the tool loop
                # would happily call it. Removed, so "failed" is the whole
                # truth rather than half of it.
                self.delete(skill_name)
                return ForgeResult(
                    False, skill_name, "failed",
                    f"Generated skill failed validation: "
                    f"{validation.get('error') or 'no reason given'}")

        if note:
            result.detail = f"{result.detail} (discovery: {note})"
        return result

    # ── Helpers ──────────────────────────────────────────────────────────

    def _make_skill_name(self, task: str) -> str:
        """Generate a unique, safe skill name from a task description."""
        # Simple slugify
        name = "".join(c if c.isalnum() else "_" for c in task.lower()[:40])
        name = name.strip("_")[:30]
        if not name:
            name = "forged_skill"
        # Ensure uniqueness
        base = name
        i = 1
        while name in skill_registry._skills:
            name = f"{base}_{i}"
            i += 1
        return name

    def _infer_params(self, code: str, task: str) -> dict:
        """Infer JSON Schema parameters from generated function code.

        Every parameter used to be declared `"type": "string"` — hardcoded — so
        a `params.get("seconds", 0)` came out as a string whose default was the
        STRING "0". The schema is not decoration: it is sent verbatim as the
        function-calling `parameters` (OpenAI/DeepSeek) or `input_schema`
        (Claude), so it tells the model to send "3661" where the handler does
        `int(seconds)`. The type is now inferred from the default value and from
        how the value is used.
        """
        schema = {
            "type": "object",
            "properties": {},
            "required": [],
        }

        def _type_of(value) -> str | None:
            """A JSON Schema type for a Python literal, if it maps cleanly."""
            if isinstance(value, bool):
                return "boolean"
            if isinstance(value, int):
                return "integer"
            if isinstance(value, float):
                return "number"
            if isinstance(value, str):
                return "string"
            if isinstance(value, (list, tuple)):
                return "array"
            if isinstance(value, dict):
                return "object"
            return None

        def _type_from_use(node, names: set[str]) -> str | None:
            """The type implied by how a value is consumed.

            Matched on the VARIABLES the value flowed into, not the parameter
            name: a handler binds `n = params.get("count")` and then does
            `int(n)`, so searching for `int(count)` finds nothing. `names` is
            therefore the set of names to follow, seeded with the variable the
            call was assigned to.

            Weaker than a literal default but better than lying: `int(x)` means
            a number, `len(x)`/`sorted(x)` a list, `x == True` a boolean. Only
            consulted when the default gives nothing away.
            """
            if not names:
                return None
            for sub in ast.walk(node):
                # `int(x)` / `float(x)` / `str(x)` / `bool(x)` / `len(x)`
                if isinstance(sub, ast.Call):
                    f = sub.func
                    fname = (getattr(f, "id", None)
                             or getattr(f, "attr", None))
                    if sub.args and _names(sub.args[0]) & names:
                        if fname in ("int", "float"):
                            return "number"
                        if fname == "str":
                            return "string"
                        if fname == "bool":
                            return "boolean"
                        if fname in ("list", "tuple", "set", "sorted", "len"):
                            # A container: len() only applies to one, and
                            # list()/sorted() produce one.
                            return "array"
                    # `x.strip()` / `x.split()` mean a string.
                    if fname in ("strip", "split", "lower", "upper", "replace",
                                 "startswith", "endswith", "join"):
                        recv = getattr(f, "value", None)
                        if _names(recv) & names:
                            return "string"
                    # `x.get(...)` / `x.items()` mean a mapping.
                    if fname in ("items", "keys", "values"):
                        recv = getattr(f, "value", None)
                        if _names(recv) & names:
                            return "object"
                # Comparisons like `x == True`
                elif isinstance(sub, ast.Compare) and \
                        _names(sub.left) & names:
                    if any(isinstance(c, ast.Constant)
                           and isinstance(c.value, bool)
                           for c in sub.comparators):
                        return "boolean"
                # Iterating it means a container.
                elif isinstance(sub, (ast.For, ast.comprehension)):
                    target = getattr(sub, "iter", None)
                    if _names(target) & names:
                        return "array"
            return None

        def _assigned_to(child) -> set[str]:
            """The variable names a `params.get(...)` call is bound to.

            `x = params.get("k")`            -> {"x"}
            `a, b = params.get("k", ())`     -> {"a", "b"}
            Anything else (a bare call, or a call nested in an expression) has
            no name to follow, so the set is empty and only the default can
            decide the type.
            """
            out: set[str] = set()
            for n in ast.walk(node):
                for sub in ast.walk(n):
                    if not isinstance(sub, (ast.Assign, ast.AnnAssign)):
                        continue
                    value = sub.value
                    if value is not child:
                        continue
                    targets = (sub.targets if isinstance(sub, ast.Assign)
                               else [sub.target])
                    for t in targets:
                        if isinstance(t, ast.Name):
                            out.add(t.id)
                        elif isinstance(t, (ast.Tuple, ast.List)):
                            out |= {e.id for e in t.elts
                                    if isinstance(e, ast.Name)}
            return out

        def _names(node) -> set:
            """Every bare name mentioned under `node`."""
            return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} \
                if node is not None else set()

        def _is_params(node) -> bool:
            """Whether an expression refers to the handler's `params` dict.

            Accepts the bare name and anything ending in `.params`, so a handler
            that renames or wraps it is still read.
            """
            return (isinstance(node, ast.Name) and node.id == "params") or \
                   (isinstance(node, ast.Attribute) and node.attr == "params")

        def _param_reads(node) -> list[tuple[str, ast.AST | None, ast.AST]]:
            """Every named parameter the handler reads, and its default node.

            Returns `(name, default_node, read_node)`. Three shapes are found,
            because a model writes any of them and all three are correct:

              params.get("seconds", 0)     -> default 0
              params["seconds"]            -> no default
              "seconds" not in params      -> no default (a guard)

            Only `params.get` used to be recognised. A handler written with
            `params['seconds']` — which the local qwen3-8b produced — therefore
            yielded NO parameters at all, and the skill was registered with the
            dummy `query`/`input` fallback schema while its real parameter went
            undeclared. The skill still ran, because it reads `params` itself,
            but every caller was told the wrong interface.
            """
            found: dict[str, tuple[ast.AST | None, ast.AST]] = {}

            def note(name, default_node, read_node):
                # Keep the first read that has a default; a bare read never
                # replaces one that carried a literal.
                if name not in found:
                    found[name] = (default_node, read_node)
                elif found[name][0] is None and default_node is not None:
                    found[name] = (default_node, read_node)

            for sub in ast.walk(node):
                # params.get("k", default)
                if isinstance(sub, ast.Call) and \
                        isinstance(sub.func, ast.Attribute) and \
                        sub.func.attr == "get" and _is_params(sub.func.value) and \
                        sub.args and isinstance(sub.args[0], ast.Constant) and \
                        isinstance(sub.args[0].value, str):
                    note(sub.args[0].value,
                         sub.args[1] if len(sub.args) >= 2 else None, sub)
                # params["k"]
                elif isinstance(sub, ast.Subscript) and _is_params(sub.value):
                    sl = sub.slice
                    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                        note(sl.value, None, sub)
                # "k" in params  /  "k" not in params
                elif isinstance(sub, ast.Compare) and \
                        any(o in (ast.In, ast.NotIn) for o in sub.ops) and \
                        any(_is_params(c) for c in sub.comparators) and \
                        isinstance(sub.left, ast.Constant) and \
                        isinstance(sub.left.value, str):
                    note(sub.left.value, None, sub)

            return [(n, d, r) for n, (d, r) in found.items()]

        # Parse function signature
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    # Look for the ways a handler reads its parameters.
                    for param_name, default_node, child in _param_reads(node):
                        if param_name in schema["properties"]:
                            continue

                        entry = {
                            "description": f"Parameter: {param_name}",
                        }
                        default_value = None
                        if default_node is not None:
                            try:
                                default_value = ast.literal_eval(default_node)
                            except (ValueError, SyntaxError):
                                default_value = None

                        # 1. The default value is the strongest signal.
                        inferred = _type_of(default_value)
                        # 2. Otherwise, how the value is used. Follow the
                        #    variable the value was bound to, and also the
                        #    parameter's own name in case it is read inline.
                        if inferred is None:
                            inferred = _type_from_use(
                                node, _assigned_to(child) | {param_name})
                        if inferred:
                            entry["type"] = inferred

                        # A default is only worth publishing when it is not
                        # None: JSON Schema accepts `null`, but a `None` here
                        # usually means "no default set".
                        if default_value is not None:
                            entry["default"] = default_value
                        elif default_node is not None and \
                                isinstance(default_node, ast.Constant) and \
                                default_node.value is None:
                            entry["default"] = None

                        # `required` is NOT inferred. `normalise` ENFORCES it
                        # (it reports "missing required parameter(s)" and treats
                        # ""/None as absent), so listing a parameter here can
                        # make a skill that works — one whose handler copes with
                        # a missing key — refuse to be called at all. The old
                        # code never set it, and a wrong guess breaks a working
                        # skill, so nothing is added.

                        # An `array` without `items` is still usable, but
                        # `normalise` splits a scalar string into a list only
                        # when `items.type` is `string`. Declaring `items` keeps
                        # that coercion working.
                        if entry.get("type") == "array":
                            entry["items"] = {"type": "string"}

                        schema["properties"][param_name] = entry
        except Exception:  # noqa: BLE001
            pass

        if not schema["properties"]:
            schema["properties"] = {
                "query": {"type": "string", "description": "Task input"},
                "input": {"type": "string", "description": "Additional input"},
            }

        return schema

    def list_forged(self) -> list[dict]:
        """List all dynamically forged skills."""
        return [
            {"name": name, "description": s.description, "category": s.category}
            for name, s in self._forged.items()
        ]


# ── Singleton ────────────────────────────────────────────────────────────

skill_forge = SkillForge()
