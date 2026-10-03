"""Does a forged skill get VALIDATED with a real input, and cleaned up if not?

Two faults this covers, both found by driving the real `forge_skill` in the
installed app:

1. Validation called the handler with `{}`. Any skill that takes a parameter
   therefore returned its own "missing parameter" error and FAILED ITS OWN TEST
   while its generated code was correct — a false negative on the most common
   kind of skill. The generator now asks the model for an example call
   (`TEST_PARAMS`), which is passed to `validate`.

2. `generate` registers the skill BEFORE returning, so a skill that then failed
   validation stayed registered and on disk while the forge reported failure.
   The app would load it on every start and the tool loop would call it. It is
   now deleted.

3. The failure message read `validation.get('error')`, which is absent when the
   handler returns `{"success": False}` rather than raising, so the user was
   told "failed validation: None".

The handler is exercised directly with a local test double, so nothing here
needs a model or a network.
"""
import asyncio
import importlib.util
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\Kunoir\Codeground\Clicky\Addled")

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


from backend.skills import forge as forge_mod
from backend.skills.forge import FORGE_DIR, SkillForge, skill_forge
from backend.skills.registry import SkillDefinition, skill_registry

NAME = "addled_check_validate_needs_input"
BOOM = "addled_check_validate_raises"

# The shape the generator actually produces: needs a parameter, returns its own
# error when it is missing. `__NAME__` is substituted rather than using
# str.format, because the source is full of dict braces.
NEEDS_INPUT = '''
from backend.skills.registry import SkillDefinition

async def h(params: dict) -> dict:
    value = params.get("seconds")
    if value is None:
        return {"success": False,
                "error": "Missing required parameter: 'seconds'"}
    return {"success": True, "result": f"{value} seconds"}

SKILL_DEF = SkillDefinition(
    name="__NAME__", description="check double: needs 'seconds'",
    parameters={"type": "object", "properties": {"seconds": {"type": "number"}}},
    handler=h, category="forged")
'''

RAISES = '''
from backend.skills.registry import SkillDefinition

async def h(params: dict) -> dict:
    raise RuntimeError("boom from the handler")

SKILL_DEF = SkillDefinition(
    name="__NAME__", description="check double: raises",
    parameters={}, handler=h, category="forged")
'''


def _install(name, src):
    p = FORGE_DIR / f"{name}.py"
    p.write_text(src.replace("__NAME__", name), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"_chk_{name}", str(p))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    skill_registry.register(m.SKILL_DEF)
    return p


print("=== a skill that needs a parameter ===")
_install(NAME, NEEDS_INPUT)
try:
    # The old call. This must FAIL: it is the bug, and asserting it keeps the
    # test honest about what it is measuring.
    v0 = asyncio.run(skill_forge.validate(NAME))
    check("with no arguments it fails (this was the bug)",
          v0.get("success") is False, str(v0)[:150])
    check("and says why, rather than reporting None",
          bool(v0.get("error")) and "None" not in str(v0.get("error")),
          repr(v0.get("error")))

    # The fixed call: the generator's example input.
    v1 = asyncio.run(skill_forge.validate(NAME, {"seconds": 90}))
    check("with a real input it PASSES", v1.get("success") is True,
          str(v1.get("error"))[:200])
    check("and returns the real result",
          "90 seconds" in str((v1.get("output") or {}).get("result")),
          str(v1.get("output"))[:160])
finally:
    skill_forge.delete(NAME)

print()
print("=== a handler that raises ===")
_install(BOOM, RAISES)
try:
    v2 = asyncio.run(skill_forge.validate(BOOM))
    check("the exception is reported with its message",
          v2.get("success") is False and "boom" in str(v2.get("error")),
          repr(v2.get("error")))
finally:
    (FORGE_DIR / f"{BOOM}.py").unlink(missing_ok=True)
    skill_registry.unregister(BOOM)

print()
print("=== a failed forge leaves nothing behind ===")
# Generation is stubbed so the assertion is about the cleanup, not the model.
# The stub MUST name SKILL_DEF from the derived name, because that is what the
# real build_skill_module does and what the deletion looks up.
STUB = '''
from backend.skills.registry import SkillDefinition

async def h(params: dict) -> dict:
    return {"success": False, "error": "deliberate validation failure"}

SKILL_DEF = SkillDefinition(
    name="__DERIVED__", description="check double: always fails",
    parameters={}, handler=h, category="forged")
'''

_orig_generate, _orig_discover = SkillForge.generate, SkillForge.discover


async def _fake_generate(self, skill_name, task, discovery, provider=None):
    p = FORGE_DIR / f"{skill_name}.py"
    p.write_text(STUB.replace("__DERIVED__", skill_name), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"_s_{skill_name}", str(p))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    self._forged[skill_name] = m.SKILL_DEF
    skill_registry.register(m.SKILL_DEF)
    return forge_mod.ForgeResult(True, skill_name, "generated",
                                 detail="stub", test_params={})


async def _fake_discover(self, task, provider=None):
    return {"package": "", "install_cmd": "", "confidence": "high"}


SkillForge.generate, SkillForge.discover = _fake_generate, _fake_discover
try:
    r = asyncio.run(skill_forge.forge("a task whose skill always fails"))
finally:
    SkillForge.generate, SkillForge.discover = _orig_generate, _orig_discover

derived = r.skill_name
check("it reports failure", r.success is False and r.action == "failed",
      f"success={r.success} action={r.action}")
check("the generated file is removed", not (FORGE_DIR / f"{derived}.py").exists(),
      f"{derived}.py is still on disk, so it stays loadable")
check("it is not left registered", skill_registry.get(derived) is None,
      "still registered, so the tool loop could call a failed skill")
check("the reason is specific, not None",
      "deliberate validation failure" in str(r.detail), str(r.detail)[:200])

# Belt and braces: remove anything the stub wrote under a derived name.
for f in FORGE_DIR.glob("a_task_whose_skill_always_fails*.py"):
    f.unlink(missing_ok=True)
    skill_registry.unregister(f.stem)

print()
print("FAILED: " + ", ".join(fails) if fails
      else "all forge-validation checks passed")
raise SystemExit(1 if fails else 0)
