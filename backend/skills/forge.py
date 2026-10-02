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


def pip_argv(install_cmd: str) -> list[str] | None:
    """The command line that installs a package, or None if it is not one.

    `pip install X` has to become `python -m pip install X` for the interpreter
    that is running, so the package lands where the generated skill will import
    it from. This used to drop the wrong word and run `python -m install X`,
    which fails with "No module named install" — so every forge dependency
    install failed and generation carried on without the package it had just
    decided it needed.
    """
    cmd = (install_cmd or "").strip()
    if not cmd.startswith(("pip ", "pip3 ", "python -m pip ")):
        return None
    args = cmd.split()
    if args[0] in ("pip", "pip3"):
        return [sys.executable, "-m", "pip"] + args[1:]
    return args


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

    def _load_forged(self):
        """Load previously forged skills from disk."""
        for f in FORGE_DIR.glob("*.py"):
            try:
                name = f.stem
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
        """Install a Python package via pip."""
        if not install_cmd:
            return {"success": False, "error": "No install command provided"}

        try:
            argv = pip_argv(install_cmd)
            if argv is None:
                return {"success": False,
                        "error": f"Unsafe install command: {install_cmd.strip()}"}
            result = subprocess.run(
                argv,
                capture_output=True, text=True, timeout=120,
                encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                log.info("Package installed: %s", " ".join(argv[1:]))
                return {"success": True, "output": result.stdout[-500:]}
            return {"success": False, "error": result.stderr[-500:]}
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Install timed out"}
        except Exception as e:
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
        """)

        skill_path = None
        try:
            from backend.providers import router
            from backend.providers import budget
            pid = str(getattr(provider, "provider_id", "") or "")
            # The generated function is what is being paid for here, so the
            # reply reserve is generous; the prompt is what gives way.
            prompt, max_tokens = budget.fit_single_prompt(
                prompt, pid, want_reply=2000)
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

            # Validate syntax
            try:
                ast.parse(code)
            except SyntaxError as e:
                return ForgeResult(False, skill_name, "failed",
                                  f"Generated code has syntax error: {e}")

            # Generate parameter schema from function
            params_schema = self._infer_params(code, task_description)

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
                                  detail=f"Generated and registered skill '{skill_name}' using {package}")
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
        """Test a newly forged skill to ensure it works."""
        skill = skill_registry.get(skill_name)
        if not skill:
            return {"success": False, "error": f"Skill {skill_name} not found"}

        try:
            result = await skill.handler(test_params or {})
            if isinstance(result, dict):
                return {"success": result.get("success", False),
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

        # 2. Install if needed
        if install_cmd and package not in ("unknown", ""):
            install_result = await self.install(install_cmd)
            if not install_result["success"]:
                # Non-fatal: try generation anyway (package might already be installed)
                log.warning("Install failed, trying without: %s", install_result.get("error"))

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
            validation = await self.validate(skill_name)
            if not validation["success"]:
                return ForgeResult(False, skill_name, "failed",
                                  f"Generated skill failed validation: {validation.get('error')}")

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
        """Infer JSON Schema parameters from generated function code."""
        schema = {
            "type": "object",
            "properties": {},
            "required": [],
        }

        # Parse function signature
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    # Look for params.get() calls in the function body
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            if (isinstance(child.func, ast.Attribute) and
                                child.func.attr == "get" and
                                len(child.args) >= 1 and
                                isinstance(child.args[0], ast.Constant)):
                                param_name = child.args[0].value
                                if param_name not in schema["properties"]:
                                    default = ""
                                    if len(child.args) >= 2:
                                        if isinstance(child.args[1], ast.Constant):
                                            default = str(child.args[1].value)
                                    schema["properties"][param_name] = {
                                        "type": "string",
                                        "description": f"Parameter: {param_name}",
                                    }
                                    if default:
                                        schema["properties"][param_name]["default"] = default
        except Exception:
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
