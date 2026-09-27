"""Swarm roster checks — agents that persist, brief and remember.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_swarm_roster.py

Pins the things that make a swarm agent a named desk rather than a one-shot
call: it survives a restart, its brief and learned rules reach its prompt, one
agent can pin its own model, and a correction is remembered. Everything runs
against a throwaway roster folder, so nothing here touches the real one.
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.swarm import roster

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    tmp = Path(tempfile.mkdtemp(prefix="roster_"))
    real_dir, real_roster, real_feedback = (
        roster._DIR, roster.ROSTER_PATH, roster.FEEDBACK_DIR)
    try:
        roster._DIR = tmp
        roster.ROSTER_PATH = tmp / "roster.json"
        roster.FEEDBACK_DIR = tmp / "feedback"

        # ---- default types carry skills --------------------------------
        types = {t["type"]: t for t in roster.type_catalogue()}
        check("every built-in type is described",
              len(types) >= 8, str(list(types)))
        for name in ("coder", "reviewer", "researcher", "planner", "qa"):
            check(f"the '{name}' desk has default skills",
                  bool(types.get(name, {}).get("skills")),
                  str(types.get(name)))
        check("the coder desk is offered a verifier",
              "verify_code" in types["coder"]["skills"],
              str(types["coder"]["skills"]))
        check("the reviewer desk is offered the karpathy guideline",
              any(s.startswith("guidelines:") for s in types["reviewer"]["skills"]),
              str(types["reviewer"]["skills"]))

        # ---- persistence ------------------------------------------------
        check("an empty roster reads as empty, not an error",
              roster.definitions() == [], str(roster.definitions()))

        entry = roster.upsert({"id": "agent_rev", "name": "REVIEWER",
                               "type": "reviewer",
                               "brief": "Be blunt. Must-fix first."})
        check("upsert returns the stored shape", entry["id"] == "agent_rev",
              str(entry))
        check("the type default prompt is filled in",
              bool(entry["prompt"]), str(entry))
        check("and the type's default skills", bool(entry["skills"]),
              str(entry))

        # Read back from disk — this is what "survives a restart" means.
        again = roster.get("agent_rev")
        check("it is readable after a reload", again is not None, "gone")
        check("the brief survived", "blunt" in again["brief"].lower(),
              again["brief"])

        # ---- an unknown type falls back rather than failing --------------
        odd = roster.upsert({"id": "agent_x", "name": "X",
                             "type": "not_a_type"})
        check("an unknown type falls back to general",
              odd["type"] == "general", str(odd))
        check("and still gets a usable prompt", bool(odd["prompt"]), str(odd))

        # ---- brief and learned rules reach the persona --------------------
        class FakeAgent:
            def __init__(self):
                self.id = "agent_rev"
                self.name = "REVIEWER"
                self.role = "Code Reviewer"
                self.does = "Reviews changes."
                self.system_prompt = "You are a reviewer."
                self.brief = "Be blunt. Must-fix first."
                self.skills = []

        persona = roster.build_persona(FakeAgent())
        check("the persona names the agent",
              "REVIEWER" in persona, persona[:200])
        check("and carries its brief",
              "Be blunt" in persona, persona[:300])
        check("and its prompt", "You are a reviewer." in persona,
              persona[:300])

        # ---- corrections become standing rules ----------------------------
        roster.add_rule("agent_rev", "Proposals are always one page.")
        roster.add_rule("agent_rev", "Never invent a price.")
        rules = roster.rules_for("agent_rev")
        check("a standing rule is remembered", len(rules) == 2, str(rules))
        check("newest last", rules[-1] == "Never invent a price.", str(rules))

        persona2 = roster.build_persona(FakeAgent())
        check("learned rules reach the persona",
              "one page" in persona2, persona2[:400])
        check("and are framed as corrections",
              "corrections" in persona2.lower(), persona2[:400])

        one = roster.add_one_off("agent_rev", "For the Harbourside one.")
        check("a one-off is recorded", one.get("success"), str(one))
        check("but is NOT carried into future tasks",
              not any("Harbourside" in r for r in roster.rules_for("agent_rev")),
              str(roster.rules_for("agent_rev")))

        # ---- removing -----------------------------------------------------
        check("an agent can be forgotten", roster.remove("agent_x"),
              "remove failed")
        check("and is gone from the roster",
              roster.get("agent_x") is None, "still there")

        # ---- the orchestrator uses it --------------------------------------
        from backend.swarm import orchestrator as orch_mod
        swarm = orch_mod.SwarmOrchestrator()
        agent = swarm.spawn_from_roster("agent_rev")
        check("an agent can be spawned from the roster", agent is not None,
              "spawn_from_roster returned None")
        if agent:
            check("with its brief intact", "blunt" in agent.brief.lower(),
                  agent.brief)
            check("and its name", agent.name == "REVIEWER", agent.name)
            check("and its composed persona carries the brief",
                  "Be blunt" in agent.persona(), agent.persona()[:300])

        # A fresh orchestrator loads the roster — this is the restart case.
        fresh = orch_mod.SwarmOrchestrator()
        restored = fresh.load_roster()
        check("a new session restores the saved agents", restored >= 1,
              f"restored={restored}")

        # ---- per-agent model -----------------------------------------------
        pinned = roster.upsert({"id": "agent_fast", "name": "FAST",
                                "type": "general", "model": "qwen3-8b",
                                "provider": ""})
        check("a model can be pinned per agent",
              pinned["model"] == "qwen3-8b", str(pinned))

        # ---- mid-flight notes ----------------------------------------------
        m = orch_mod.SwarmOrchestrator()
        m.clear_notes()
        r = m.note("planner", "The API returns snake_case.", to="")
        check("a note is recorded", r.get("success"), str(r))
        m.note("coder", "I renamed helper to util.", to="lead")
        # Read as `qa` — a third party. A note addressed to `lead` is not
        # addressed to qa, so it should be labelled with who it *is* for.
        third_party = m.notes_text(exclude="qa")
        check("a broadcast note reaches another agent",
              "snake_case" in third_party, third_party)
        check("a note addressed elsewhere is labelled as such",
              "coder → lead" in third_party, third_party)
        # Read as `lead` — the addressee. The arrow would be noise there.
        addressed = m.notes_text(exclude="lead")
        check("a note addressed TO you is not labelled with an arrow",
              "→" not in addressed, addressed)
        check("and its text still arrives", "renamed helper" in addressed,
              addressed)
        check("an agent does not see its own note",
              "snake_case" not in m.notes_text(exclude="planner"),
              m.notes_text(exclude="planner"))
        check("a note is delivered once, not repeatedly",
              "snake_case" not in m.notes_text(exclude="qa"),
              m.notes_text(exclude="qa"))
        check("notes can be cleared", m.clear_notes() >= 2, "clear failed")

        # ---- seeding ------------------------------------------------------
        seed_dir = tmp / "seed"
        roster._DIR = seed_dir
        roster.ROSTER_PATH = seed_dir / "roster.json"
        roster.FEEDBACK_DIR = seed_dir / "feedback"
        made = roster.seed_if_empty()
        check("a fresh roster is seeded with default desks", made >= 5,
              f"seeded {made}")
        seeded = roster.definitions()
        check("every seeded desk has a prompt and skills",
              all(e["prompt"] and e["skills"] for e in seeded),
              str([(e["name"], bool(e["skills"])) for e in seeded]))
        check("seeding twice does not duplicate",
              roster.seed_if_empty() == 0, "it seeded again")
        check("a deleted roster is not re-seeded over the user's choice",
              (roster.remove(seeded[0]["id"])
               and roster.seed_if_empty() == 0),
              "it re-added a desk the user removed")

        # ---- the skills exist ------------------------------------------------
        from backend.skills.registry import skill_registry as reg
        reg._register_all()
        names = [s.name for s in reg.list_all()]
        for skill in ("swarm_note", "swarm_notes", "swarm_roster",
                      "swarm_learn"):
            check(f"the '{skill}' tool is registered", skill in names,
                  "missing")
    finally:
        roster._DIR, roster.ROSTER_PATH, roster.FEEDBACK_DIR = (
            real_dir, real_roster, real_feedback)
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: swarm roster — persistent desks, briefs, learned rules, notes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
