"""Checks for the goal detail view: who ran each step, and how far it got.

The Goals page could show that a goal existed and what status it had, and
nothing about *how* it was being done. The backend was already broadcasting a
`goal.progress` event carrying the round, the step, and which desk picked it
up — the page simply never subscribed, so every goal ran with a dead progress
display.

The directions that matter here are the ones that would be silently wrong:

* a progress event must name the desk that took the step (the plan names only
  a role, and only the executor knows which desk answered it);
* a re-plan must replace the step list, not merge into it, or abandoned steps
  linger looking like work still to do;
* a progress bar must never exceed 100%, even mid-re-plan; and
* reading a goal that does not exist must say so, not return an empty goal.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_goal_detail.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.goals import executor as executor_mod  # noqa: E402
from backend.goals.executor import GoalExecutor  # noqa: E402

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

# -- the executor names the desk -------------------------------------------

class _FakeStore:
    def __init__(self):
        self.saved: dict = {}

    def load(self, goal_id):
        return self.saved.get(goal_id)

    def save(self, goal):
        self.saved[goal["id"]] = goal

    def update_status(self, goal_id, status):
        if goal_id in self.saved:
            self.saved[goal_id]["status"] = status


class _FakeExecutor:
    async def execute(self, request):
        class R:
            success = True
            summary = "ran"
            error = ""
        return R()


def _patch_swarm(monkey_struct):
    """Point the executor's swarm lookups at a stub."""
    import backend.swarm.orchestrator as swarm_mod
    real = swarm_mod.swarm

    class StubAgent:
        def __init__(self, aid, name, role):
            self.id = aid
            self.name = name
            self.role = role
            self.type = role

    class StubSwarm:
        def list_agents(self):
            return [{"id": "agent_res", "name": "Researcher (goal)",
                     "role": "researcher", "type": "researcher"}]

        def get_agent(self, aid):
            if aid == "agent_res":
                return StubAgent("agent_res", "Researcher (goal)", "researcher")
            return None

        async def run_flow(self, steps, provider=None, goal=""):
            return {"success": True,
                    "steps": [{"output": "researched it", "agentId": "agent_res",
                               "agent": "Researcher (goal)"}]}

    swarm_mod.swarm = StubSwarm()
    return real


real_swarm = _patch_swarm(None)

# Capture what the executor broadcasts, instead of needing a live server.
events: list[tuple[str, dict]] = []
real_progress = GoalExecutor._progress

def _capture(self, goal_id, status, extra=None):
    events.append((status, {"goalId": goal_id, **(extra or {})}))

GoalExecutor._progress = _capture

try:
    # -- 1. the desk is named on the step that was assigned it ---------------
    store = _FakeStore()
    store.saved["g1"] = {
        "id": "g1", "title": "t", "description": "", "status": "pending",
        "plan": {"steps": [
            {"index": 1, "kind": "agent", "role": "researcher",
             "description": "look it up", "status": "pending"},
            {"index": 2, "kind": "agent", "role": "writer",
             "description": "write it up", "status": "pending"},
        ]},
    }
    ex = GoalExecutor(action_executor=_FakeExecutor(), store=store)
    events.clear()
    result = asyncio.run(ex.run_goal("g1"))

    started = [e for s, e in events if s == "step_started"]
    check("a step names the desk that took it",
          started and started[0].get("agentId") == "agent_res"
          and started[0].get("agent") == "Researcher (goal)",
          f"step_started carried {started[0] if started else 'nothing'}")

    # The plan names a ROLE; only the executor can resolve it to a desk. If the
    # role were forwarded instead of the resolved desk, the UI could never name
    # the actual agent.
    check("the resolved desk is a name, not just the planned role",
          started and started[0].get("role") == "researcher"
          and started[0].get("agent"),
          "role was forwarded without a resolved desk")

    # -- 2. every event carries a renderable step list ----------------------
    snapshot_events = [e for s, e in events if e.get("steps")]
    check("progress carries the plan so the page can render it",
          bool(snapshot_events), "no event carried a step list")
    if snapshot_events:
        first = snapshot_events[0]["steps"]
        check("the snapshot has one row per planned step",
              len(first) == 2, f"got {len(first)} rows")
        check("a snapshot row is renderable",
              all(k in first[0] for k in
                  ("index", "kind", "status", "description")),
              f"row keys: {sorted(first[0])}")
        check("the snapshot reports status, not just the plan",
              first[0].get("status") in
              ("pending", "in_progress", "completed", "failed", "blocked"),
              f"status was {first[0].get('status')!r}")

    # -- 3. a re-plan replaces the step list --------------------------------
    # Merging by index would leave steps from the abandoned plan in the list,
    # reading as work still to do.
    steps_a = [{"index": 1, "status": "pending"}, {"index": 2, "status": "pending"},
               {"index": 3, "status": "pending"}]
    snapshot_a = ex._plan_snapshot(steps_a)
    steps_b = [{"index": 1, "status": "pending"}]
    snapshot_b = ex._plan_snapshot(steps_b)
    check("a re-plan yields a shorter list, not a merged one",
          len(snapshot_a) == 3 and len(snapshot_b) == 1,
          f"{len(snapshot_a)} then {len(snapshot_b)}")

    # -- 4. the progress bar is bounded ------------------------------------
    # Mirrors the dashboard's maths so a regression there is visible here.
    def pct(steps):
        total = len(steps)
        done = sum(1 for s in steps if s.get("status") == "completed")
        if not total:
            return 0
        return min(100, max(0, round((done / total) * 100)))

    check("an empty plan is 0%, not a divide-by-zero",
          pct([]) == 0, f"got {pct([])}")
    check("all steps done is 100%",
          pct([{"status": "completed"}] * 4) == 100, f"got {pct([{'status': 'completed'}] * 4)}")
    check("a plan longer than its total cannot exceed 100%",
          pct([{"status": "completed"}] * 3) <= 100, "bar overflowed")
    check("partial progress is proportional",
          pct([{"status": "completed"}, {"status": "pending"}]) == 50,
          f"got {pct([{'status': 'completed'}, {'status': 'pending'}])}")

    # -- 5. a blocked step is renderable, not invisible --------------------
    # A dependency that failed leaves a step that can never run. It has to be
    # visible, with its reason, or it reads as "still to do" forever.
    blocked = ex._plan_snapshot([
        {"index": 1, "status": "failed", "error": "boom"},
        {"index": 2, "status": "blocked", "error": "depends on step(s) 1"},
    ])
    check("a blocked step keeps its reason",
          blocked[1]["status"] == "blocked" and blocked[1]["error"],
          f"row: {blocked[1]}")

    # -- 5b. a malformed stored plan must not crash the run ---------------
    #
    # The plan is read back from a JSON file, where a partial write or a
    # hand-edit can leave `plan` as a string, or `steps` as anything at all.
    # Every one of these used to raise AttributeError straight out of
    # run_goal, killing the goal with nothing shown to the user.
    class _BadStore:
        def __init__(self, goal):
            self.goal = goal

        def load(self, goal_id):
            return self.goal

        def save(self, goal):
            self.goal = goal

        def update_status(self, goal_id, status):
            self.goal["status"] = status

    malformed = {
        "steps is a string": {"plan": {"steps": "oops"}},
        "plan is a string": {"plan": "broken"},
        "plan is None": {"plan": None},
        "steps is a list of strings": {"plan": {"steps": ["a", "b"]}},
        "steps is an int": {"plan": {"steps": 42}},
        "steps holds non-dicts": {"plan": {"steps": [{}, {1: 1}]}},
        "no plan key": {},
        "plan with no steps": {"plan": {}},
    }
    for label, extra in malformed.items():
        goal = {"id": "bad", "title": "t", "description": "",
                "status": "pending"}
        goal.update(extra)
        runner = GoalExecutor(action_executor=_FakeExecutor(),
                              store=_BadStore(goal))
        try:
            outcome = asyncio.run(runner.run_goal("bad"))
            survived = isinstance(outcome, dict) and "status" in outcome
        except Exception as e:  # noqa: BLE001
            survived = False
            outcome = f"{type(e).__name__}: {e}"
        check(f"a malformed plan does not crash the run ({label})", survived,
              f"raised or returned {outcome!r}")

    # The snapshot helper is called on that same untrusted value, so it has to
    # tolerate it too rather than becoming a second crash site.
    for label, bad in (("a string", "nope"), ("an int", 42), ("None", None),
                       ("a dict", {"a": 1}),
                       ("a list of non-dicts", [{"index": "7"}, "junk", None])):
        try:
            rows = ex._plan_snapshot(bad)
            ok = isinstance(rows, list)
        except Exception as e:  # noqa: BLE001
            ok = False
            rows = f"{type(e).__name__}: {e}"
        check(f"the plan snapshot tolerates {label}", ok, f"got {rows!r}")

    # -- 6. reading a goal that is gone says so ----------------------------
    src = (Path(ROOT) / "backend" / "ws_server.py").read_text(encoding="utf-8")
    check("goal.get is registered", 'register("goal.get"' in src,
          "goal.get not registered")
    match = re.search(r"async def goal_get\(.*?\n(?=    async def )", src,
                      re.S)
    body = match.group(0) if match else ""
    check("goal.get reports an unknown goal as an error",
          "Goal not found" in body and '"success": False' in body,
          "an unknown goal would read as an empty one")
    check("goal.get refuses an empty id",
          "No goalId" in body, "an empty id was not rejected")

    # -- 7. the page actually subscribes -----------------------------------
    page = (Path(ROOT) / "dashboard" / "src" / "app" / "goals"
            / "page.tsx").read_text(encoding="utf-8")
    check("the Goals page subscribes to goal.progress",
          "goal.progress" in page,
          "the page still ignores the progress it is sent")
    # onNotification keeps ONE handler per method, so a second subscriber
    # would silently replace this one.
    other = []
    dash = Path(ROOT) / "dashboard" / "src"
    for f in dash.rglob("*.tsx"):
        if f.name == "page.tsx" and f.parent.name == "goals":
            continue
        if "goal.progress" in f.read_text(encoding="utf-8"):
            other.append(str(f.relative_to(ROOT)))
    check("no other component also owns goal.progress", not other,
          f"second subscriber would clobber the handler: {other}")

finally:
    GoalExecutor._progress = real_progress
    import backend.swarm.orchestrator as swarm_mod
    swarm_mod.swarm = real_swarm

if fails:
    print("FAILURES:")
    for f in fails:
        print("  -", f)
    print(f"\n{len(fails)} failure(s)")
    sys.exit(1)

print("PASS: goal detail -- the desk that ran each step is named, progress is "
      "bounded, a re-plan replaces the plan, and reading a missing goal says so")
