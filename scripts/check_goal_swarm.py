"""Checks for a goal actually being done by the swarm.

A goal used to be a list of shell commands. Every step went to the
ActionExecutor, so "research X and write it up" planned into `run_command`
steps that could neither research nor write. And when the model returned
nothing usable, the fallback produced three `echo` steps that printed the
goal's title and reported success -- a goal that did nothing looked like a goal
that worked.

The directions that matter here are the ones that would be silently wrong:

* an agent step must reach the swarm, not the shell;
* an action step must still reach the shell, because routing "pip install X"
  through a model only adds a call that decides to do what the step says;
* the fallback must be work, not an echo that reports success; and
* a goal that does not finish must re-plan or report failure, never success.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_goal_swarm.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import backend.swarm.orchestrator as swarm_mod  # noqa: E402
from backend.goals import executor as goal_executor_module  # noqa: E402
from backend.goals import planner  # noqa: E402
from backend.goals.executor import GoalExecutor  # noqa: E402

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

class StubProvider:
    """A provider returning a fixed reply, so planning is testable."""

    def __init__(self, reply):
        self.reply = reply
        self.calls: list[str] = []

    class _Result:
        def __init__(self, ok, response):
            self.ok = ok
            self.response = response

    async def chat(self, messages, **kwargs):
        self.calls.append(messages[-1]["content"])
        return self._Result(True, self.reply)

class StubSwarm:
    """Records what the executor asked the swarm to do."""

    def __init__(self, reply="the agent did the work"):
        self.reply = reply
        self.flows: list[dict] = []
        self.spawned: list[str] = []

    def list_agents(self):
        return [{"id": "agent_researcher", "name": "Researcher",
                 "type": "researcher", "role": "researcher"},
                {"id": "agent_coder", "name": "Coder",
                 "type": "coder", "role": "coder"}]

    def spawn(self, name, agent_type="general", **kwargs):
        self.spawned.append(agent_type)

        class _A:
            id = "agent_" + agent_type
        return _A()

    async def run_flow(self, steps, provider=None, goal=""):
        self.flows.append({"steps": steps, "goal": goal})
        return {"success": True, "flowId": "flow_test",
                "steps": [{"agent": "Researcher", "task": steps[0]["task"],
                           "success": True, "output": self.reply}]}

class StubActions:
    """Records what the executor asked the shell to do."""

    def __init__(self, ok=True):
        self.ok = ok
        self.actions: list[str] = []

    async def execute(self, request):
        self.actions.append(request.action_type)

        class _R:
            def __init__(self, ok):
                self.success = ok
                self.summary = "ran"
                self.error = None if ok else "nope"
        return _R(self.ok)

class StubStore:
    def __init__(self, goal):
        self.goal = goal
        self.saved = 0

    def load(self, goal_id):
        return self.goal if goal_id == self.goal.get("id") else None

    def save(self, goal):
        self.goal = goal
        self.saved += 1

    def update_status(self, goal_id, status):
        self.goal["status"] = status
        self.saved += 1

GOOD_PLAN = (
    '[{"kind":"agent","role":"researcher","task":"find out what the filing '
    'deadline is","dependencies":[]},'
    ' {"kind":"agent","role":"writer","task":"write it up",'
    '"dependencies":[1]},'
    ' {"kind":"action","action_type":"run_command",'
    '"params":{"command":"git add -A"},"dependencies":[2]}]')

def test_planner(loop):
    plan = loop.run_until_complete(
        planner.plan_goal("file the report", "before Friday",
                          StubProvider(GOOD_PLAN)))
    steps = plan.get("steps") or []
    check("a planned goal has steps", len(steps) == 3, str(len(steps)))
    if steps:
        check("the first step is agent work",
              steps[0].get("kind") == "agent", str(steps[0]))
        check("it names the role the plan asked for",
              steps[0].get("role") == "researcher", str(steps[0]))
        check("the task text survives",
              "deadline" in str(steps[0].get("task")), str(steps[0]))
        check("dependencies are kept",
              steps[1].get("dependencies") == [1], str(steps[1]))
    if len(steps) > 2:
        check("the command step stays an action",
              steps[2].get("kind") == "action", str(steps[2]))
        check("and keeps its action_type",
              steps[2].get("action_type") == "run_command", str(steps[2]))
    check("the plan reports the roles it needs",
          "researcher" in (plan.get("roles") or []), str(plan.get("roles")))

    fenced = StubProvider("Here is the plan:\n```json\n" + GOOD_PLAN
                          + "\n```\nHope that helps.")
    check("a fenced reply is parsed",
          len(loop.run_until_complete(
              planner.plan_goal("x", "", fenced)).get("steps") or []) == 3,
          "the fence broke parsing")

    inferred = StubProvider(
        '[{"description":"run the tests","action_type":"run_command",'
        '"params":{"command":"pytest"}},'
        ' {"description":"read the results and decide what to fix",'
        '"role":"analyst"}]')
    s3 = (loop.run_until_complete(
        planner.plan_goal("green tests", "", inferred)).get("steps") or [])
    check("an action step is inferred from action_type",
          bool(s3) and s3[0].get("kind") == "action", str(s3[:1]))
    check("a step with no action_type is inferred as agent work",
          len(s3) > 1 and s3[1].get("kind") == "agent", str(s3[1:2]))

    sloppy = StubProvider(
        '[{"kind":"agent","role":"wizard","task":"do magic"},'
        ' {"kind":"action","action_type":"frobnicate","params":{}}]')
    s4 = (loop.run_until_complete(
        planner.plan_goal("sloppy", "", sloppy)).get("steps") or [])
    check("an unknown role falls back to general",
          bool(s4) and s4[0].get("role") == "general", str(s4[:1]))
    check("an unknown action falls back to run_command",
          len(s4) > 1 and s4[1].get("action_type") == "run_command",
          str(s4[1:2]))

    loopdep = StubProvider(
        '[{"kind":"agent","role":"general","task":"one","dependencies":[1]},'
        ' {"kind":"agent","role":"general","task":"two","dependencies":[9]}]')
    s5 = (loop.run_until_complete(
        planner.plan_goal("bad deps", "", loopdep)).get("steps") or [])
    check("a step cannot depend on itself",
          bool(s5) and 1 not in (s5[0].get("dependencies") or []), str(s5[:1]))
    check("a dependency on a step that does not exist is dropped",
          len(s5) > 1 and 9 not in (s5[1].get("dependencies") or []),
          str(s5[1:2]))

    # The bug worth naming: the old fallback ran `echo Analyzing: <title>`
    # three times and reported success.
    fallback = loop.run_until_complete(
        planner.plan_goal("something hard", "", StubProvider("not json")))
    f_steps = fallback.get("steps") or []
    check("an unusable reply falls back to a plan", bool(f_steps), str(fallback))
    check("the fallback is agent work, not a shell echo",
          bool(f_steps) and f_steps[0].get("kind") == "agent",
          str(f_steps[:1]))
    check("the fallback is not a command plan",
          all(s.get("action_type") != "run_command" for s in f_steps),
          str(f_steps))
    # The task itself has to ask for work. Checking only the `kind` would pass
    # for a fallback that is an agent step whose task is `echo` -- which is the
    # shape the old bug had, so this asserts on the text too.
    fallback_task = str(f_steps[0].get("task") or "") if f_steps else ""
    check("the fallback's task asks for the work, not for an echo",
          "Work on this goal" in fallback_task
          and "echo" not in fallback_task.lower(),
          fallback_task[:160])
    check("the fallback tells the agent to admit what it cannot do",
          "missing" in fallback_task, fallback_task[:160])

def test_executor(loop):
    swarm = StubSwarm()
    actions = StubActions()
    ex = GoalExecutor(action_executor=actions)

    store = StubStore({
        "id": "goal_1", "title": "t", "description": "",
        "status": "pending", "findings": [],
        "plan": {"steps": [
            {"index": 1, "kind": "agent", "role": "researcher",
             "task": "research it", "dependencies": [], "status": "pending"},
            {"index": 2, "kind": "action", "action_type": "run_command",
             "params": {"command": "echo hi"}, "dependencies": [1],
             "status": "pending"},
        ]},
    })
    ex.set_store(store)

    real_swarm = swarm_mod.swarm
    swarm_mod.swarm = swarm
    try:
        out = loop.run_until_complete(ex.run_goal("goal_1"))
        check("the goal completes when every step succeeds",
              out.get("status") == "completed", str(out.get("status")))
        check("the agent step went to the swarm",
              len(swarm.flows) == 1, str(len(swarm.flows)))
        check("and carried its task through",
              bool(swarm.flows)
              and "research it" in swarm.flows[0]["steps"][0]["task"],
              str(swarm.flows[:1]))
        check("the command step went to the shell",
              actions.actions == ["run_command"], str(actions.actions))
        check("the agent's answer is kept on the goal",
              any("the agent did the work" in str(f.get("output") or "")
                  for f in store.goal.get("findings") or []),
              str(store.goal.get("findings"))[:200])
        check("the goal is marked completed",
              store.goal.get("status") == "completed",
              str(store.goal.get("status")))

        aid, problem = ex._resolve("researcher")
        check("a role resolves to the desk that matches it",
              aid == "agent_researcher" and not problem, f"{aid} / {problem}")
        aid2, _ = ex._resolve("nobody-has-this-role")
        check("an unmatched role falls back to some desk rather than "
              "failing the goal", bool(aid2), aid2)

        # ---- a failing action step -------------------------------------
        # Patched so the assertion is about the failure being recorded, not
        # about what a re-plan did afterwards.
        saved_plan2 = planner.plan_goal

        async def no_replan2(*a, **k):
            return {"steps": [], "count": 0, "kind": "planned", "roles": []}

        planner.plan_goal = no_replan2
        try:
            ex2 = GoalExecutor(action_executor=StubActions(ok=False))
            store2 = StubStore({
                "id": "goal_2", "title": "t", "description": "",
                "status": "pending", "findings": [],
                "plan": {"steps": [
                    {"index": 1, "kind": "action", "action_type": "run_command",
                     "params": {}, "dependencies": [], "status": "pending"},
                ]},
            })
            ex2.set_store(store2)
            out2 = loop.run_until_complete(ex2.run_goal("goal_2"))
            check("a failed step does not report the goal as completed",
                  out2.get("status") != "completed", str(out2.get("status")))
            check("a failed step is recorded with a reason",
                  bool(store2.goal["plan"]["steps"][0].get("error")),
                  str(store2.goal["plan"]["steps"][0]))
        finally:
            planner.plan_goal = saved_plan2

        # Re-planning is patched out for this one: the test is about the
        # blocked marking, and the planner would otherwise replace the very
        # plan being inspected with a fresh one.
        saved_plan = planner.plan_goal

        async def no_replan(*a, **k):
            return {"steps": [], "count": 0, "kind": "planned", "roles": []}

        planner.plan_goal = no_replan
        try:
            ex3 = GoalExecutor(action_executor=StubActions(ok=False))
            store3 = StubStore({
                "id": "goal_3", "title": "t", "description": "",
                "status": "pending", "findings": [],
                "plan": {"steps": [
                    {"index": 1, "kind": "action", "action_type": "run_command",
                     "params": {}, "dependencies": [], "status": "pending"},
                    {"index": 2, "kind": "agent", "role": "general",
                     "task": "waits on the failed one", "dependencies": [1],
                     "status": "pending"},
                ]},
            })
            ex3.set_store(store3)
            loop.run_until_complete(ex3.run_goal("goal_3"))
            second = store3.goal["plan"]["steps"][1]
            check("a step waiting on a failed step is marked blocked",
                  second.get("status") == "blocked", str(second))
            check("and says what it was waiting for",
                  "1" in str(second.get("error")), str(second))
        finally:
            planner.plan_goal = saved_plan

        replan_calls: list[str] = []
        real_plan_goal = planner.plan_goal

        async def fake_plan(title, description="", provider=None,
                            *, roles=None, feedback=""):
            replan_calls.append(feedback)
            if len(replan_calls) == 1:
                return {"steps": [{"index": 1, "kind": "action",
                                   "action_type": "run_command",
                                   "params": {}, "dependencies": [],
                                   "status": "pending"}],
                        "count": 1, "kind": "planned", "roles": []}
            return {"steps": [{"index": 1, "kind": "agent", "role": "general",
                               "task": "finish it", "dependencies": [],
                               "status": "pending"}],
                    "count": 1, "kind": "planned", "roles": ["general"]}

        planner.plan_goal = fake_plan
        try:
            ex4 = GoalExecutor(action_executor=StubActions(ok=False))
            store4 = StubStore({
                "id": "goal_4", "title": "loopy", "description": "",
                "status": "pending", "findings": [],
                "plan": {"steps": [
                    {"index": 1, "kind": "action",
                     "action_type": "run_command", "params": {},
                     "dependencies": [], "status": "pending"},
                ]},
            })
            ex4.set_store(store4)
            out4 = loop.run_until_complete(ex4.run_goal("goal_4"))
            check("a goal that fails is re-planned",
                  len(replan_calls) >= 1, str(len(replan_calls)))
            check("the re-plan is told what happened",
                  any("FAILED" in f for f in replan_calls),
                  str(replan_calls)[:300])
            # The plan has to actually be replaced, not just re-requested. A
            # re-plan whose steps are discarded leaves the goal running the
            # same failing plan until the round budget runs out -- which looks
            # like the loop working while it is doing nothing.
            check("the re-planned steps are adopted",
                  any(str(s.get("task") or "") == "finish it"
                      for s in (store4.goal.get("plan") or {}).get("steps") or []),
                  str(store4.goal.get("plan"))[:300])
            check("there is a bound on the attempts",
                  int(out4.get("rounds") or 0)
                  <= goal_executor_module.MAX_ROUNDS,
                  str(out4.get("rounds")))
        finally:
            planner.plan_goal = real_plan_goal

        empty = StubSwarm()
        empty.list_agents = lambda: []
        swarm_mod.swarm = empty
        aid3, problem3 = ex._resolve("writer")
        check("a role with no desk spawns one instead of failing",
              bool(aid3) and not problem3, f"{aid3} / {problem3}")
        check("and it spawns the role that was asked for",
              empty.spawned == ["writer"], str(empty.spawned))

        # ---- a goal with no steps must not be reported complete --------
        # Patched out, because the fallback planner would supply a step and
        # this is testing what happens when a plan is genuinely empty.
        saved_plan3 = planner.plan_goal

        async def still_empty(*a, **k):
            return {"steps": [], "count": 0, "kind": "planned", "roles": []}

        planner.plan_goal = still_empty
        try:
            ex5 = GoalExecutor(action_executor=StubActions())
            store5 = StubStore({"id": "goal_5", "title": "empty",
                                "description": "", "status": "pending",
                                "findings": [], "plan": {"steps": []}})
            ex5.set_store(store5)
            out5 = loop.run_until_complete(ex5.run_goal("goal_5"))
            check("a goal with no steps is not called complete",
                  out5.get("status") != "completed", str(out5.get("status")))
        finally:
            planner.plan_goal = saved_plan3
    finally:
        swarm_mod.swarm = real_swarm

def run():
    loop = asyncio.new_event_loop()
    try:
        test_planner(loop)
        test_executor(loop)
    finally:
        loop.close()

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: goal -> swarm -- reasoning steps go to a desk, commands stay commands, and a goal that is not done says so")
    return 0

if __name__ == "__main__":
    sys.exit(main())
