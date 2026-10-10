"""A skill's instructions must be readable WITHOUT calling the skill.

The defect this exists to catch is subtle and was live in Addled: a market skill
stores the SKILL.md body as its `instructions`, and the only way to see it was
to call the skill. But you decide whether to call a skill from its one-line
description in the catalogue - so a skill documenting a five-step procedure
could never inform the decision to use it. The guidance was present on disk and
unreachable in practice, which looks exactly like working software.

Two things therefore have to be true, and they pull in opposite directions:
  1. `skill_view` returns the body.
  2. The prompt catalogue does NOT contain the body - otherwise every installed
     skill's full text rides in every single request, and the fix costs more
     than the problem.

The second is why this check reads the catalogue and asserts the body is absent
from it. A solution that put the body in the prompt would pass (1) and fail the
whole point.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_skill_bodies.py
"""

import asyncio
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []

BODY = ("STEP ONE: check the datum is even set.\n"
        "STEP TWO: normalise before comparing.\n"
        "STEP THREE: never compare floats without a tolerance.")


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")
        fails.append(label)


def main() -> int:
    print("check_skill_bodies")

    try:
        from backend.skills.registry import (
            SkillDefinition, skill_registry)
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the registry: {e}")
        print()
        print("FAILED (1): could not import the registry")
        return 1

    # ------------------------------------------------------------ the field
    check("SkillDefinition carries a body",
          "body" in SkillDefinition.__dataclass_fields__,
          "a skill cannot hold instructions longer than its description")
    check("body defaults to empty",
          SkillDefinition.__dataclass_fields__["body"].default == "",
          "a skill with no body must not need one")

    async def h(params):
        return {"success": True}

    probe = "probe_skill_bodies"
    skill_registry.register(SkillDefinition(
        probe, "A probe skill used only by this check.",
        {"type": "object", "properties": {}}, h, "meta", body=BODY))

    async def run():
        view = skill_registry.get("skill_view")
        search = skill_registry.get("skill_search")
        got = await view.handler({"name": probe})
        miss = await view.handler({"name": probe + "_nope"})
        found = await search.handler({"query": "probe skill used by check"})
        nob = await view.handler({"name": "skill_search"})
        return got, miss, found, nob

    got, miss, found, nob = asyncio.run(run())

    # ------------------------------------------------------------- retrieval
    check("skill_view returns the stored body", got.get("body") == BODY,
          repr(got.get("body"))[:80])
    check("skill_view marks a miss as a failure", miss.get("success") is False)
    check("a miss suggests close matches rather than a dead end",
          any(probe in n for n in (miss.get("close_matches") or [])),
          repr(miss.get("close_matches")))
    check("skill_search finds a skill by its description",
          any(s["name"] == probe for s in (found.get("skills") or [])),
          repr([s["name"] for s in (found.get("skills") or [])][:5]))
    check("a skill with no body says so plainly",
          isinstance(nob.get("note"), str) and "no written instructions"
          in nob.get("note", ""),
          repr(nob.get("note")))
    check("viewing a bodiless skill still succeeds",
          nob.get("success") is True)

    # ------------------------------------------------- the catalogue is small
    # This is the assertion that fails if someone "fixes" reachability by
    # pasting every body into the prompt.
    catalogue = skill_registry.to_prompt_tools()
    check("the prompt catalogue does NOT contain the body",
          "STEP THREE" not in catalogue and "never compare floats" not in catalogue,
          "bodies must be fetched on demand, not carried in every request")
    check("the catalogue still lists the skill by name",
          probe in catalogue,
          "the one-line index is what the model decides from")

    # ------------------------------------------------- and the prompt path too
    native = skill_registry.to_openai_tools()
    check("the native tool schema does not carry the body either",
          "STEP THREE" not in str(native),
          "the body must not leak into the tool schemas")

    # ---------------------------------------------- market skills populate it
    market_src = open(os.path.join(ROOT, "backend", "skills", "market.py"),
                      encoding="utf-8").read()
    check("market skills store their SKILL.md body",
          "body=meta.get(\"instructions\"" in market_src,
          "an installed skill's instructions must reach the body field")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all skill-body checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
