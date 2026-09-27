"""Probe: does the forge work end to end with a scripted provider?

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\_forge_probe.py

Not part of check_all.py. It writes a real file into forged_skills and removes
it again, so it needs no network and no API key.
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.providers.base import ProviderResult
from backend.skills.registry import skill_registry
from backend.skills import forge as forge_mod


class ScriptedProvider:
    """Answers the two prompts the forge sends, in order."""

    provider_id = "openai"
    provider_name = "scripted"

    def __init__(self):
        self.calls = 0
        self.seen = []

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None):
        self.calls += 1
        text = " ".join(
            str(m.get("content", "")) for m in messages if isinstance(m, dict))
        self.seen.append(text)
        if "Suggest the BEST Python library" in text:
            return ProviderResult(ok=True, model="scripted", response=json.dumps({
                "package": "socket",
                "install_cmd": "",
                "approach": "use the stdlib socket module",
                "confidence": "high",
                "import_statement": "import socket",
                "code_hint": "socket.gethostname()",
            }))
        return ProviderResult(ok=True, model="scripted", response=(
            "async def probe_machine_name(params: dict) -> dict:\n"
            "    try:\n"
            "        import socket\n"
            "        return {\"success\": True,\n"
            "                \"hostname\": socket.gethostname()}\n"
            "    except Exception as e:\n"
            "        return {\"success\": False, \"error\": str(e)}\n"
        ))


async def main() -> int:
    fails = []

    def check(label, ok, detail=""):
        print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            fails.append(label)

    # The browser path is optional and would hit the network; skip it so the
    # probe is offline and deterministic.
    async def no_discover(task_description, provider=None):
        return {"package": "socket", "install_cmd": "",
                "approach": "stdlib", "confidence": "high",
                "import_statement": "import socket", "code_hint": ""}

    forge_mod.skill_forge.discover = no_discover

    before = len(skill_registry.list_all())
    result = await forge_mod.skill_forge.forge(
        "get this machine's hostname",
        provider=ScriptedProvider(),
        auto_validate=True,
    )
    print("forge:", result.success, result.action, "|", result.detail[:140])

    check("forge reports success", result.success, result.detail[:160])
    check("forge names the generated skill", bool(result.skill_name),
          result.skill_name)

    name = result.skill_name
    on_disk = forge_mod.FORGE_DIR / f"{name}.py"
    check("the module was written to disk", on_disk.exists(), str(on_disk))

    skill = skill_registry.get(name)
    check("the skill is registered", skill is not None, name)
    check("it is a forged skill",
          getattr(skill, "category", "") == "forged",
          str(getattr(skill, "category", "")))

    after = len(skill_registry.list_all())
    check("the catalogue grew", after == before + 1, f"{before} -> {after}")

    # The part that matters: the agent can now actually use it.
    res = await skill_registry.execute(name, {})
    print("execute:", res.success, json.dumps(res.data)[:160])
    check("the forged skill runs", res.success, str(res.error))
    check("and returns a real value", bool(res.data.get("hostname")),
          json.dumps(res.data)[:160])

    # Reload from disk the way startup does.
    skill_registry.unregister(name)
    reloaded = forge_mod.SkillForge()
    check("a restart reloads it from disk",
          reloaded._forged.get(name) is not None,
          ", ".join(sorted(reloaded._forged)[:6]) or "(none)")

    # Clean up so the probe leaves nothing behind.
    forge_mod.skill_forge.delete(name)
    check("cleanup removed the file", not on_disk.exists(), str(on_disk))

    print()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
