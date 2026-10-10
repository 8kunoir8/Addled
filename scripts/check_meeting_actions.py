"""Check that a meeting's action items are PROPOSED, and only created on ask.

The behaviour under test is a refusal as much as a feature. An action item is
`{what, who}` with `who` frequently a third party, so the danger is not that
`meeting_actions` fails to create a task — it is that it creates one nobody
agreed to. Every check below is written so that a version of the skill that
writes to the task store on its own initiative FAILS.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATA = Path(tempfile.mkdtemp(prefix="addled_ma_"))
os.environ["ADDLED_DATA_DIR"] = str(DATA)

_failures: list[str] = []
_passed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _passed
    if ok:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failures.append(name + (f" — {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}")


from backend.meetings import store  # noqa: E402
from backend.skills.registry import skill_registry  # noqa: E402
from backend.tasks.store import task_store  # noqa: E402

ADDLED = "Steru to review the pricing cap before the renewal."
THIRD = "Tom to send the migration plan to the platform team."
NOBODY = "Book the offsite venue."


def _meeting(title: str, actions: list[dict]) -> str:
    m = store.create(title=title)
    store.set_summary(m["id"], "Discussed the renewal and the migration.",
                      decisions=["Renew at the capped price."],
                      actions=actions,
                      open_questions=[])
    return m["id"]


def _tool(name: str):
    return skill_registry.get(name)


def call(skill, **params):
    return asyncio.run(skill.handler(params))


# ---------------------------------------------------------------------------
section("The skill is registered")

skill = _tool("meeting_actions")
check("meeting_actions is registered", skill is not None)
if skill is None:
    print("\nCannot continue without the skill.")
    raise SystemExit(1)

check("it is given a description the model can act on",
      "propose" in (skill.description or "").lower(),
      "the description must say it proposes before it creates")


# ---------------------------------------------------------------------------
section("Proposing writes nothing")

meeting_id = _meeting("Northwind contract review",
                      [{"what": "Review the pricing cap", "who": "Steru"},
                       {"what": "Send the migration plan", "who": "Tom"},
                       {"what": "Book the offsite venue", "who": ""}])

before = len(task_store.list_all())
result = call(skill, id=meeting_id)
check("a bare call succeeds", result.get("success"), str(result)[:200])
check("and comes back as a proposal",
      result.get("mode") == "proposal", str(result.get("mode")))
check("nothing was scheduled",
      len(task_store.list_all()) == before,
      f"the task store grew from {before} to {len(task_store.list_all())} "
      "on a proposal call")
check("every action item is offered",
      len(result.get("actions", [])) == 3, str(result.get("actions")))
check("the indexes are present to accept by",
      [a["index"] for a in result.get("actions", [])] == [0, 1, 2],
      str([a.get("index") for a in result.get("actions", [])]))
check("the owner travels with the item",
      result["actions"][1]["who"] == "Tom", str(result["actions"][1]))
check("an item with no owner is not given one",
      result["actions"][2]["who"] == "", str(result["actions"][2]))
check("the note says nothing was scheduled",
      "proposal" in (result.get("note") or "").lower()
      or "nothing was scheduled" in (result.get("note") or "").lower(),
      str(result.get("note"))[:160])


# ---------------------------------------------------------------------------
section("A bare true is refused rather than guessed")

result = call(skill, id=meeting_id, accept=True)
check("accept=true does not create anything",
      not result.get("success"), str(result)[:160])
check("and says why", "list of indexes" in (result.get("error") or ""),
      str(result.get("error")))
check("the task store is still untouched",
      len(task_store.list_all()) == before)


# ---------------------------------------------------------------------------
section("Confirming creates only what was named")

result = call(skill, id=meeting_id, accept=[0])
check("the confirmed item is created", result.get("success"), str(result)[:200])
check("exactly one task was created",
      len(result.get("created", [])) == 1, str(result.get("created")))
check("and the store grew by exactly one",
      len(task_store.list_all()) == before + 1,
      f"{before} -> {len(task_store.list_all())}")
check("the item NOT named was not created",
      not any("Tom" in t.title or "migration" in t.title
              for t in task_store.list_all()),
      "an unaccepted action item was scheduled")
check("the created task carries the owner",
      any("Steru" in t.title for t in task_store.list_all()),
      str([t.title for t in task_store.list_all()]))
check("the created task has a real next_run",
      all(t.next_run > 0 for t in task_store.list_all()),
      "a task with next_run 0 never fires")
check("the result reports the task id",
      result["created"][0].get("task_id"), str(result["created"][0]))


# ---------------------------------------------------------------------------
section("A third party's item is kept, not silently made the user's")

result = call(skill, id=meeting_id, accept=[1])
check("the third-party item can be created when the user asks",
      result.get("success"), str(result)[:160])
titles = [t.title for t in task_store.list_all()]
check("its owner is preserved in the title",
      any(t.startswith("Tom:") for t in titles),
      "the owner was dropped: " + str(titles))
check("the text is preserved too",
      any("migration plan" in t for t in titles), str(titles))


# ---------------------------------------------------------------------------
section("Bad input is refused, not half-applied")

grew = len(task_store.list_all())
for bad, why in [([99], "an out-of-range index"),
                 (["x"], "a non-number"),
                 ([0.5, "y"], "junk in the list")]:
    result = call(skill, id=meeting_id, accept=bad)
    if bad == [99]:
        # It must be REPORTED, not quietly dropped: an index that vanishes
        # looks identical to one that was created, and the user is told
        # nothing about the item they asked for.
        reported = result.get("skipped") or result.get("failed") \
            or result.get("error")
        check("an out-of-range index is reported, not silently dropped",
              bool(reported), str(result)[:200])
        check("and no task is claimed for it",
              not result.get("created"), str(result.get("created")))
    else:
        check(f"{why} is refused", not result.get("success"), str(result)[:160])
check("no task was created by the bad input",
      len(task_store.list_all()) == grew,
      f"{grew} -> {len(task_store.list_all())}")


# ---------------------------------------------------------------------------
section("A meeting with no actions, or no meeting at all")

plain = store.create(title="Standup")
store.set_summary(plain["id"], "Nothing decided.")
result = call(skill, id=plain["id"])
check("a meeting with no action items does not succeed",
      not result.get("success"), str(result)[:160])
check("and points at summarising it",
      "summaris" in (result.get("error") or "").lower()
      or "summaris" in (result.get("hint") or "").lower(),
      str(result)[:200])

result = call(skill, id="meeting_does_not_exist")
check("an unknown meeting id is refused",
      not result.get("success"), str(result)[:160])
result = call(skill, id="")
check("a missing id is refused", not result.get("success"), str(result)[:160])

check("the whole run created only the two confirmed tasks",
      len(task_store.list_all()) == 2,
      f"{len(task_store.list_all())} tasks: "
      f"{[t.title for t in task_store.list_all()]}")


# ---------------------------------------------------------------------------
section("The skill does not reach into the task store on import")

import inspect  # noqa: E402
from backend.skills import registry as reg_mod  # noqa: E402

source = inspect.getsource(reg_mod)
start = source.index("async def meeting_actions")
body = source[start:start + 6000]
check("the task store is only written inside the accept branch",
      body.index("task_store.add") > body.index("if not accept:"),
      "task_store.add appears before the proposal early-return")


# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"FAILED {len(_failures)} check(s)")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All {_passed} meeting-action checks passed.")
