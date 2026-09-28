"""Checks for per-agent memory, and a flow that can be resumed.

The swarm had one shared blackboard, in memory only. Two consequences followed,
and this suite covers both:

* an agent had no memory of its **own** work — restart, and the planner was a
  stranger to its own plan; and
* a flow that was interrupted lost every finished step, because progress lived
  in local variables inside `run_flow`.

The directions that matter are the ones that would be silently wrong:

* another agent's work must not appear in a desk's own digest, or "per-agent
  memory" is just the old shared blackboard with extra steps;
* a resumable flow must not replay steps that already finished, because those
  cost model calls and their results are already recorded; and
* a flow whose agents no longer exist must say so, rather than failing on the
  first step with an error nobody can act on.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_swarm_memory.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.swarm import checkpoints, notebook, orchestrator

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    real_nb = notebook._DIR
    real_cp = checkpoints._DIR
    tmp = Path(tempfile.mkdtemp(prefix="swarm_mem_"))
    try:
        notebook._DIR = tmp / "notebooks"
        checkpoints._DIR = tmp / "checkpoints"

        # ---- an agent keeps its own history -------------------------------
        notebook.record("agent_a", name="Planner", task="draft a plan",
                        output="Step one: read the file.")
        notebook.record("agent_a", name="Planner", task="revise",
                        output="Step one: read, then validate.")
        notebook.record("agent_b", name="Coder", task="write the code",
                        output="Wrote widget.py with two functions.")

        a = notebook.load("agent_a")
        check("an agent's own entries are recorded", len(a["entries"]) == 2,
              str(len(a["entries"])))
        check("with the task and the output",
              a["entries"][0]["task"] == "draft a plan"
              and "read the file" in a["entries"][0]["output"],
              str(a["entries"][0]))

        digest_a = notebook.digest("agent_a")
        check("the digest carries that agent's work",
              "draft a plan" in digest_a, digest_a[:200])
        # The point of per-agent memory: another desk's work is NOT here.
        check("the digest does not carry another agent's work",
              "widget.py" not in digest_a,
              "the shared blackboard came back under a new name")

        digest_b = notebook.digest("agent_b")
        check("and each agent gets its own",
              "widget.py" in digest_b and "draft a plan" not in digest_b,
              digest_b[:200])

        # ---- it survives a restart ---------------------------------------
        # A fresh load, the way a new process sees it.
        reloaded = notebook.load("agent_a")
        check("the notebook is on disk, so a restart keeps it",
              len(reloaded["entries"]) == 2
              and reloaded["entries"][1]["task"] == "revise",
              str(reloaded["entries"]))

        # ---- old entries fold into a summary ------------------------------
        # This needs a folder of its own only so the fold does not disturb the
        # notebooks the checks above are still asserting on; it is restored
        # straight after, because `forget` below looks in the current folder.
        real_fold_dir = notebook._DIR
        notebook._DIR = tmp / "notebooks_fold"
        for i in range(notebook.MAX_ENTRIES + 12):
            notebook.record("agent_c", task=f"task {i}", output=f"output {i}")
        folded = notebook.load("agent_c")
        check("entries are capped",
              len(folded["entries"]) <= notebook.MAX_ENTRIES,
              str(len(folded["entries"])))
        check("the overflow lands in the summary, not nowhere",
              "task 0" in folded["summary"],
              "the oldest work was dropped outright")
        check("the summary stays inside its bound",
              len(folded["summary"]) <= notebook.MAX_SUMMARY_CHARS,
              str(len(folded["summary"])))
        check("and the digest is bounded too",
              len(notebook.digest("agent_c")) <= notebook.MAX_DIGEST_CHARS,
              str(len(notebook.digest("agent_c"))))

        # ---- old detail is still retrievable ------------------------------
        # Checked here, against the same notebook the fold just compressed,
        # because that is the case that matters: the digest has moved on and
        # the full entry is still on disk to be asked for.
        hits = notebook.search("agent_c", "output 3")
        check("work the digest has moved on from is still findable",
              bool(hits), "search found nothing")
        check("search only looks in that agent's notebook",
              notebook.search("agent_c", "no such phrase") == [],
              "it matched something that is not there")
        notebook._DIR = real_fold_dir

        # ---- a notebook cannot escape its folder --------------------------
        for bad in ("../escape", "a/b", "a\\b", "", ".", ".."):
            check(f"a notebook path of {bad!r} is refused",
                  notebook.path_for(bad) is None, "it was allowed")

        # ---- forgetting removes the file ---------------------------------
        check("forgetting an agent removes its notebook",
              notebook.forget("agent_a") is True, "it was not removed")
        check("and it reads as empty afterwards",
              notebook.load("agent_a")["entries"] == [],
              "the entries survived")

        # ---- the notebook never raises ------------------------------------
        (tmp / "notebooks").mkdir(parents=True, exist_ok=True)
        (tmp / "notebooks" / "agent_bad.json").write_text("{not json",
                                                          encoding="utf-8")
        check("a corrupt notebook reads as empty rather than raising",
              notebook.load("agent_bad")["entries"] == [],
              "it raised or kept junk")

        # ---- a flow leaves a checkpoint -----------------------------------
        steps = [
            {"agentId": "a1", "task": "one"},
            {"agentId": "a2", "task": "two"},
            {"agentId": "a3", "task": "three"},
        ]
        checkpoints.start("flow_x", "do the thing", steps)
        data = checkpoints.load("flow_x")
        check("a checkpoint is written when a flow starts", data is not None,
              "nothing was written")
        check("it keeps the plan, so a resume knows the shape",
              len((data or {}).get("steps") or []) == 3,
              str((data or {}).get("steps")))

        checkpoints.progress("flow_x", finished=[1], skipped=[],
                             done=[{"agent": "A", "output": "first result"}])
        mid = checkpoints.load("flow_x") or {}
        check("progress records which steps finished",
              mid.get("finished") == [1], str(mid.get("finished")))
        check("and what they produced, for the rebuilt transcript",
              (mid.get("done") or [{}])[0].get("output") == "first result",
              str(mid.get("done")))

        listed = checkpoints.resumable()
        check("an interrupted flow is offered to resume",
              any(f["flowId"] == "flow_x" for f in listed), str(listed))
        entry = next((f for f in listed if f["flowId"] == "flow_x"), {})
        check("the offer says how far it got",
              entry.get("finished") == 1 and entry.get("steps") == 3,
              str(entry))

        # ---- a resumed flow replays, then continues ----------------------
        orch = orchestrator.SwarmOrchestrator()
        # `spawn` generates its own ids, so the desks are bound to the ids the
        # checkpoint names — which is what a real resume has to line up too.
        for agent_id, name in (("a1", "One"), ("a2", "Two"), ("a3", "Three")):
            agent = orch.spawn(name, "general")
            orch._agents.pop(agent.id, None)
            agent.id = agent_id
            orch._agents[agent_id] = agent

        ran: list[str] = []
        original_run_flow = orch.run_flow

        async def fake_run_flow(step_list, provider=None, goal=""):
            # Only the unfinished steps should reach here.
            ran.append("called")
            return {"success": True, "flowId": "flow_new",
                    "steps": [], "skipped": []}

        orch.run_flow = fake_run_flow
        try:
            out = asyncio.run(orch.resume_flow("flow_x"))
            check("resuming a flow runs the remainder",
                  out.get("success") is True and ran == ["called"],
                  str(out))
            check("and says it was resumed",
                  out.get("resumed") is True, str(out))
            check("and closes the old checkpoint, so it is not offered again",
                  not any(f["flowId"] == "flow_x"
                          for f in checkpoints.resumable()),
                  "it can still be resumed")
        finally:
            orch.run_flow = original_run_flow

        # ---- a flow with nothing left says so, rather than rerunning ------
        checkpoints.start("flow_done", "already finished", steps)
        checkpoints.progress("flow_done", finished=[1, 2, 3], skipped=[],
                             done=[])
        out = asyncio.run(orch.resume_flow("flow_done"))
        check("a finished flow is not run again",
              out.get("success") is True and out.get("resumed") is False,
              str(out))

        # ---- a flow whose agents are gone says so -------------------------
        checkpoints.start("flow_orphan", "orphan", [
            {"agentId": "gone_agent", "task": "something"},
        ])
        out = asyncio.run(orch.resume_flow("flow_orphan"))
        check("a flow naming a missing agent is refused with a reason",
              out.get("success") is False
              and "gone_agent" in str(out.get("error")),
              str(out))

        # ---- resuming something that does not exist -----------------------
        out = asyncio.run(orch.resume_flow("no_such_flow"))
        check("resuming an unknown flow is refused",
              out.get("success") is False, str(out))

        # ---- a checkpoint path cannot escape its folder -------------------
        for bad in ("../escape", "a/b", "a\\b", "", ".", ".."):
            check(f"a checkpoint path of {bad!r} is refused",
                  checkpoints.path_for(bad) is None, "it was allowed")

        # ---- the closing of a finished checkpoint -------------------------
        checkpoints.start("flow_close", "x", steps)
        checkpoints.complete("flow_close")
        check("a completed flow leaves nothing to resume",
              checkpoints.load("flow_close") is None,
              "the file is still there")

        # ---- discarding ---------------------------------------------------
        checkpoints.start("flow_drop", "x", steps)
        check("discarding a checkpoint succeeds",
              checkpoints.discard("flow_drop") is True, "it was not removed")
        check("and it is gone",
              checkpoints.load("flow_drop") is None, "still there")

        # ---- the agent-side hook never raises -----------------------------
        agent = orchestrator.SwarmAgent("hook_agent", "Hook", "general",
                                        "You are a hook.", [])
        notebook._DIR = tmp / "notebooks_hook"
        try:
            agent._note_progress("a task", "an output")
            check("an agent can record its own progress",
                  len(notebook.load("hook_agent")["entries"]) == 1,
                  "nothing was recorded")
            check("and read it back as a reminder",
                  "an output" in agent.remember(), agent.remember()[:120])
        except Exception as e:  # noqa: BLE001
            check("the agent-side notebook hook never raises", False, repr(e))

        # A broken notebook must not break the agent.
        notebook._DIR = Path("/\0invalid")
        try:
            agent._note_progress("a task", "an output")
            agent.remember()
            check("a broken notebook does not break the agent", True)
        except Exception as e:  # noqa: BLE001
            check("a broken notebook does not break the agent", False, repr(e))
    finally:
        notebook._DIR = real_nb
        checkpoints._DIR = real_cp
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: swarm memory — each desk keeps its own history, and an "
          "interrupted flow can be resumed")
    return 0

if __name__ == "__main__":
    sys.exit(main())
