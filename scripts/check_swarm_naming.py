"""A swarm agent can be named, renamed, and called by that name.

What this closes: naming existed in `roster.json`, but only the Swarm page could
write a definition — no skill reached it. So "make me an agent called Scout" was
a refusal with a trip to another page, and there was no way to rename one from
the chat at all.

Three things have to hold, and each is checked here:

1. **One name means one agent.** `roster.find` takes an id, an exact name, or a
   loose name ("the Scout agent"), and answers ambiguity by refusing rather than
   picking by list order. This used to be copy-pasted into two skills, which is
   how one ends up accepting a name the other rejects.

2. **A duplicate name is refused.** Two desks with one name make every later
   lookup ambiguous, so a duplicate created once becomes unusable everywhere.

3. **A rename keeps the agent.** Learned rules live in `feedback/<agent_id>.md`,
   so editing the name on the SAME id carries them across. A rename that minted
   a new id would look identical to the user and silently discard everything the
   desk had learned — the failure this suite exists to catch.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_swarm_naming.py
"""

import asyncio
import os
import sys

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


async def main() -> int:
    from backend.skills.registry import skill_registry
    from backend.skills.tool_loop import execute_skill
    from backend.swarm import roster

    print("=== the tools exist and are offered to a turn ===")
    check("swarm_create is registered",
          skill_registry.get("swarm_create") is not None)
    check("swarm_rename is registered",
          skill_registry.get("swarm_rename") is not None)
    check("swarm_delegate is still registered",
          skill_registry.get("swarm_delegate") is not None)

    descriptions = {
        "swarm_delegate": str(skill_registry.get("swarm_delegate").description),
        "swarm_create": str(skill_registry.get("swarm_create").description),
        "swarm_rename": str(skill_registry.get("swarm_rename").description),
    }
    check("delegate says a named agent should be passed as `agent`",
          "agent" in descriptions["swarm_delegate"]
          and "name" in descriptions["swarm_delegate"].lower(),
          descriptions["swarm_delegate"][:160])
    check("delegate mentions swarm_roster for an unknown desk",
          "swarm_roster" in descriptions["swarm_delegate"])

    print()
    print("=== one name means one agent ===")
    defs = roster.definitions()
    check("there is a roster to test against", len(defs) > 0,
          "seed the roster first")
    if not defs:
        return 1

    target = next((e for e in defs if str(e.get("name")).lower() == "qa"), defs[0])
    name = str(target.get("name"))
    agent_id = str(target.get("id"))

    check("find resolves by id", (roster.find(agent_id) or {}).get("id") == agent_id)
    check("find resolves by exact name",
          (roster.find(name) or {}).get("id") == agent_id)
    check("find resolves case-insensitively",
          (roster.find(name.upper()) or {}).get("id") == agent_id)
    # The wording a model actually produces. This is the case that used to fail
    # with "No swarm agent called 'QA agent'" while printing QA in the same list.
    check(f"find resolves a loose name: 'the {name} agent'",
          (roster.find(f"the {name} agent") or {}).get("id") == agent_id,
          str(roster.find(f"the {name} agent")))
    check("find returns None for an unknown name",
          roster.find("NoSuchDeskAnywhere") is None)
    check("find returns None for an empty handle", roster.find("") is None)

    print()
    print("=== create ===")
    # A name nothing else uses, so the test cannot collide with the real roster.
    probe = "ProbeDeskZz"
    created_id = None
    try:
        made = await execute_skill("swarm_create",
                                   {"name": probe, "type": "general",
                                    "brief": "A desk created by the check suite."})
        created_id = (made.get("data") or {}).get("agentId")
        check("swarm_create made an agent", bool(created_id),
              str(made.get("data"))[:200] or str(made.get("error"))[:200])
        check("the new name resolves to it",
              (roster.find(probe) or {}).get("id") == created_id)
        check("it is now in the roster",
              any(str(e.get("name")) == probe for e in roster.definitions()))

        # A duplicate must be refused, and must name the agent in the way.
        again = await execute_skill("swarm_create", {"name": probe})
        data = again.get("data") or {}
        check("a duplicate name is refused", not data.get("success"),
              str(data)[:200])
        check("the refusal names the agent already using it",
              probe in str(data.get("error") or ""),
              str(data.get("error"))[:200])

        print()
        print("=== rename keeps the agent ===")
        # A rule is the thing a careless rename would lose. Recorded first, so
        # the check can prove it survives.
        rule = "Keep replies to one paragraph."
        roster.add_rule(created_id, rule)
        before = roster.rules_for(created_id)
        check("a rule was recorded", rule in before, str(before))

        new_name = "ProbeDeskZzRenamed"
        renamed = await execute_skill("swarm_rename",
                                      {"agent": probe, "name": new_name})
        rdata = renamed.get("data") or {}
        check("swarm_rename succeeded", bool(rdata.get("success")),
              str(rdata.get("error"))[:200])
        check("the id is unchanged", str(rdata.get("agentId")) == created_id,
              f"{rdata.get('agentId')} vs {created_id}")
        check("the new name resolves",
              (roster.find(new_name) or {}).get("id") == created_id)
        check("the old name no longer resolves", roster.find(probe) is None)
        # The regression that matters: same id means the feedback file is the
        # same file, so the learned rule must still be readable.
        after = roster.rules_for(created_id)
        check("its learned rule survived the rename", rule in after, str(after))
        check("the reply says the rules were kept",
              "1" in str(rdata.get("message") or "")
              or "rule" in str(rdata.get("message") or "").lower(),
              str(rdata.get("message"))[:200])

        # Renaming onto a live name must be refused, or two desks share one name.
        taken = await execute_skill("swarm_rename",
                                    {"agent": new_name, "name": name})
        check("renaming onto an existing agent is refused",
              not (taken.get("data") or {}).get("success"),
              str(taken.get("data"))[:200])

        print()
        print("=== delegating to a name reaches that agent ===")
        # The whole point of naming. Resolve a name to an id the same way
        # `swarm_delegate` does, and confirm it lands on the named desk.
        resolved = roster.find(f"the {new_name} agent")
        check("a loose reference to the renamed desk resolves",
              (resolved or {}).get("id") == created_id,
              str(resolved))
    finally:
        # Never leave the test desk behind: a roster littered by a suite is a
        # user-visible bug, and this one runs on every `check_all`.
        for handle in (probe, "ProbeDeskZzRenamed"):
            entry = roster.find(handle)
            if entry:
                roster.remove(str(entry.get("id")))
        leftover = roster.find(probe) or roster.find("ProbeDeskZzRenamed")
        check("the test agent was cleaned up", leftover is None, str(leftover))

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all swarm-naming checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
