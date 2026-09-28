"""
Goal executor — runs goal steps, delegating the reasoning ones to the swarm.

A goal used to be a list of shell commands. Every step went to the
ActionExecutor, so a goal like "research X and write it up" planned into
`run_command` steps that could neither research nor write — the only path was a
shell, and the planner was asked to express the goal as one.

Now a step says what kind it is:

- `agent` steps go to a swarm desk through `run_flow`, so they get the same
  pipeline chat uses: tools, memory, the agent's own notebook, and the
  worked-on-by-others transcript.
- `action` steps still go to the ActionExecutor untouched, because `pip install
  X` is a command and routing it through a model would only add a call that
  decides to do what the step already says.

The executor also runs the goal as a **loop**: a plan that does not finish the
goal is re-planned with what happened fed back in, up to a bounded number of
attempts. That is the difference between "the steps ran" and "the goal is done"
— the old executor could only ever report the former.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("addled.goals.executor")

# How many times a goal may be re-planned after an attempt that did not finish
# it. Bounded because each attempt is real model work: an unbounded loop on a
# goal that cannot be met would spend money forever.
MAX_ROUNDS = 3

# Characters of the previous attempt carried into a re-plan. Enough to say what
# happened; the agents' notebooks hold the detail.
MAX_FEEDBACK_CHARS = 2000


class GoalExecutor:
    """Runs a goal's steps, and loops until the goal is met or the budget ends."""

    def __init__(self, action_executor=None, store=None):
        self._executor = action_executor
        self._store = store
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_flags: dict[str, bool] = {}

    def set_executor(self, executor):
        self._executor = executor

    def set_store(self, store):
        self._store = store

    # -- roles -> real desks --------------------------------------------------
    #
    # A plan names roles ("researcher"), not ids, because the user's desks are
    # theirs to rename. Resolving happens here so the plan stays readable and a
    # goal created before a desk was renamed still runs.

    def _resolve(self, role: str):
        """Find or make a desk for a role. Returns (agent_id, error)."""
        from backend.swarm.orchestrator import swarm
        wanted = str(role or "general").strip().lower() or "general"
        agents = swarm.list_agents()
        for a in agents:
            if str(a.get("role") or "").strip().lower() == wanted:
                return a["id"], ""
        for a in agents:
            if str(a.get("type") or "").strip().lower() == wanted:
                return a["id"], ""
        for a in agents:
            if str(a.get("type") or "") == "general":
                return a["id"], ""
        if agents:
            return agents[0]["id"], ""
        try:
            agent = swarm.spawn(f"{wanted.title()} (goal)", wanted)
            return agent.id, ""
        except Exception as e:  # noqa: BLE001
            return "", f"could not create a '{wanted}' desk: {e}"

    def _progress(self, goal_id: str, status: str, extra: dict | None = None):
        """Announce progress to the Goals page. Never raises."""
        try:
            from backend.ws_server import get_server
            server = get_server()
            if server is not None:
                server.broadcast_nowait("goal.progress",
                                        {"goalId": goal_id, "status": status,
                                         **(extra or {})})
        except Exception as e:  # noqa: BLE001
            log.debug("could not announce goal progress: %s", e)

    def _save(self, goal: dict) -> bool:
        """Write the goal, tolerating a store that will not take it.

        Every write used to be a bare `self._store.save(goal)`, so a full disk
        or a read-only folder raised straight out of `run_goal` and killed the
        goal with an unhandled exception — a crash rather than a failure the
        user can see. A goal that cannot record its progress is still worth
        finishing, so the write is attempted, logged, and reported.
        """
        try:
            self._store.save(goal)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("could not save goal %s: %s",
                        goal.get("id", "?"), e)
            return False

    def _set_status(self, goal_id: str, status: str) -> bool:
        """Set a goal's status, tolerating a store that will not take it."""
        try:
            self._store.update_status(goal_id, status)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("could not set goal %s to %s: %s", goal_id, status, e)
            return False

    # -- one step -------------------------------------------------------------

    async def _run_agent_step(self, goal_id: str, step: dict,
                              provider=None) -> tuple[bool, str, str]:
        """Run one agent step through the swarm. Returns (ok, output, error)."""
        from backend.swarm.orchestrator import swarm
        role = str(step.get("role") or "general")
        agent_id, problem = self._resolve(role)
        if not agent_id:
            return False, "", problem

        task = str(step.get("task") or step.get("description") or "").strip()
        expects = str(step.get("expects") or "").strip()
        if expects:
            task += f"\n\nProduce: {expects}"

        # A flow of one, so this goes down exactly the path a multi-step flow
        # does: the agent gets its own notebook, the shared transcript and the
        # same pipeline chat uses. Calling run_agent directly would skip the
        # flow's bookkeeping and make a goal behave differently from a flow.
        try:
            result = await swarm.run_flow(
                [{"agentId": agent_id, "task": task}],
                provider=provider,
                goal=str(step.get("description") or ""))
        except Exception as e:  # noqa: BLE001
            log.warning("goal %s agent step failed: %s", goal_id, e)
            return False, "", f"{type(e).__name__}: {e}"

        if not isinstance(result, dict):
            return False, "", "the swarm returned no result"
        if not result.get("success"):
            return False, "", str(result.get("error") or "the step failed")

        output = ""
        for entry in (result.get("steps") or []):
            if entry.get("output"):
                output = str(entry["output"])
        return True, output, ""

    async def _run_action_step(self, step: dict) -> tuple[bool, str, str]:
        """Run one action step. Returns (ok, output, error)."""
        from backend.actions.executor import ActionRequest
        try:
            request = ActionRequest(
                action_type=step.get("action_type", "run_command"),
                params=step.get("params", {}),
            )
            result = await self._executor.execute(request)
        except Exception as e:  # noqa: BLE001
            return False, "", f"{type(e).__name__}: {e}"
        if result.success:
            return True, str(result.summary or ""), ""
        return False, "", str(result.error or "the action failed")

    async def _run_step(self, goal_id: str, step: dict,
                        provider=None) -> tuple[bool, str, str]:
        """Dispatch one step by its kind."""
        kind = str(step.get("kind") or "").strip().lower()
        if kind not in ("agent", "action"):
            # Steps written before kinds existed carry an action_type. Reading
            # that keeps a stored goal runnable instead of failing validation
            # after an update.
            kind = "action" if step.get("action_type") else "agent"
        if kind == "agent":
            return await self._run_agent_step(goal_id, step, provider)
        return await self._run_action_step(step)

    # -- the goal loop --------------------------------------------------------

    def _feedback(self, steps: list[dict]) -> str:
        """What happened last time, for the re-plan prompt."""
        lines = []
        for step in steps:
            status = str(step.get("status") or "pending")
            marker = {"completed": "done", "failed": "FAILED",
                      "blocked": "blocked", "skipped": "skipped"}.get(
                          status, status)
            line = f"- [{marker}] {step.get('description') or step.get('task')}"
            if step.get("error"):
                line += f" -- {step['error']}"
            elif (step.get("result") or {}).get("summary"):
                line += f" -- {str(step['result']['summary'])[:200]}"
            lines.append(line)
        return "\n".join(lines)[:MAX_FEEDBACK_CHARS]

    async def run_goal(self, goal_id: str, provider=None) -> dict:
        """Run a goal, re-planning while it is not finished. Returns status."""
        if not self._store:
            return {"status": "failed", "error": "No goal store configured"}
        if not self._executor:
            return {"status": "failed", "error": "No action executor configured"}

        goal = self._store.load(goal_id)
        if not goal:
            return {"status": "failed", "error": f"Goal not found: {goal_id}"}

        self._cancel_flags[goal_id] = False
        plan = goal.get("plan", {}) or {}
        steps = plan.get("steps", []) or []
        rounds = 0
        findings: list[dict] = list(goal.get("findings") or [])

        while rounds < MAX_ROUNDS:
            rounds += 1
            if self._cancel_flags.get(goal_id):
                self._set_status(goal_id, "cancelled")
                self._progress(goal_id, "cancelled")
                return {"status": "cancelled", "rounds": rounds}

            # A step left `in_progress` by a crashed run is reset, so a restart
            # resumes rather than reporting a step that will never finish.
            for step in steps:
                if step.get("status") == "in_progress":
                    step["status"] = "pending"
                    step.pop("error", None)
            self._save(goal)

            self._progress(goal_id, "running",
                           {"round": rounds, "step": 0, "total": len(steps)})

            completed: set[int] = set()
            attempts = 0
            max_attempts = max(8, 2 * len(steps) + 4)
            stopped = None

            for step in steps:
                if self._cancel_flags.get(goal_id):
                    self._set_status(goal_id, "cancelled")
                    self._progress(goal_id, "cancelled")
                    return {"status": "cancelled", "rounds": rounds}

                index = int(step.get("index") or 0)
                deps = step.get("dependencies") or []

                if step.get("status") == "completed":
                    completed.add(index)
                    continue
                if deps and not all(d in completed for d in deps):
                    # Waiting on something that failed means this can never
                    # run. Marked blocked rather than skipped, so the report
                    # says why instead of the step silently vanishing.
                    step["status"] = "blocked"
                    step["error"] = ("depends on step(s) "
                                     + ", ".join(str(d) for d in deps
                                                 if d not in completed))
                    self._save(goal)
                    continue

                attempts += 1
                if attempts > max_attempts:
                    stopped = "too many attempts (stuck)"
                    break

                step["status"] = "in_progress"
                self._save(goal)
                self._progress(goal_id, "step_started",
                               {"round": rounds, "step": index,
                                "total": len(steps),
                                "description": str(step.get("description")
                                                   or step.get("task")
                                                   or "")[:160]})

                ok, output, error = await self._run_step(goal_id, step,
                                                         provider)
                if ok:
                    step["status"] = "completed"
                    step["result"] = {"summary": output[:2000]}
                    completed.add(index)
                    if output.strip():
                        # The agent's answer is the point of the goal, so it is
                        # kept on the goal itself. Leaving it only in the
                        # swarm's memory would mean the Goals page could show
                        # that a step ran, never what it produced.
                        findings.append({
                            "step": index,
                            "role": str(step.get("role")
                                        or step.get("kind") or ""),
                            "description": str(step.get("description")
                                               or step.get("task") or "")[:300],
                            "output": output[:4000],
                            "round": rounds,
                            "timestamp": time.time(),
                        })
                    self._progress(goal_id, "step_completed",
                                   {"round": rounds, "step": index,
                                    "total": len(steps)})
                else:
                    step["status"] = "failed"
                    step["error"] = error
                    self._progress(goal_id, "step_failed",
                                   {"round": rounds, "step": index,
                                    "total": len(steps),
                                    "error": str(error)[:160]})
                    stopped = error or "the step failed"
                    # Deliberately NOT a `break`. The remaining steps are still
                    # walked so those that depended on this one are marked
                    # blocked, with a reason. Breaking here left them saying
                    # "pending" forever, which reads as "still to do".
                    self._save(goal)
                    continue

                self._save(goal)
                await asyncio.sleep(0.2)

            goal["findings"] = findings[-40:]
            goal["rounds"] = rounds
            self._save(goal)

            done_count = sum(1 for s in steps if s.get("status") == "completed")
            # `steps` must be non-empty: a plan with no steps has not done the
            # goal, and reporting it complete would be the same lie the old
            # echo fallback told.
            if stopped is None and steps and done_count == len(steps):
                self._set_status(goal_id, "completed")
                self._progress(goal_id, "completed",
                               {"rounds": rounds, "step": done_count,
                                "total": len(steps)})
                try:
                    from backend.character.mood import mood_engine
                    mood_engine.event("goal_done")
                except Exception:  # noqa: BLE001
                    pass
                return {"status": "completed", "rounds": rounds,
                        "completed": done_count, "total": len(steps),
                        "findings": len(findings), "steps": steps}

            if rounds >= MAX_ROUNDS:
                self._set_status(goal_id, "failed")
                reason = stopped or ("the goal was not finished in "
                                     f"{MAX_ROUNDS} attempts")
                self._progress(goal_id, "failed",
                               {"error": str(reason)[:160], "rounds": rounds})
                try:
                    from backend.character.mood import mood_engine
                    mood_engine.event("goal_stuck")
                except Exception:  # noqa: BLE001
                    pass
                return {"status": "failed", "error": reason, "rounds": rounds,
                        "completed": done_count, "total": len(steps),
                        "findings": len(findings), "steps": steps}

            # Not finished. Re-plan with what happened and try again — this is
            # the difference between "the steps ran" and "the goal is done".
            self._progress(goal_id, "replanning",
                           {"round": rounds, "error": str(stopped or "")[:160]})
            try:
                from backend.goals.planner import plan_goal
                replanned = await plan_goal(
                    goal.get("title", ""), goal.get("description", ""),
                    provider, feedback=self._feedback(steps))
                new_steps = replanned.get("steps") or []
            except Exception as e:  # noqa: BLE001
                log.warning("re-planning goal %s failed: %s", goal_id, e)
                new_steps = []

            if not new_steps:
                self._set_status(goal_id, "failed")
                reason = stopped or "the goal did not finish"
                self._progress(goal_id, "failed",
                               {"error": str(reason)[:160], "rounds": rounds})
                return {"status": "failed", "error": reason, "rounds": rounds,
                        "completed": done_count, "total": len(steps),
                        "findings": len(findings), "steps": steps}

            steps = new_steps
            goal["plan"] = replanned
            goal["rounds"] = rounds
            self._save(goal)
            log.info("Goal %s re-planned (round %d): %d step(s)",
                     goal_id, rounds + 1, len(new_steps))

        self._set_status(goal_id, "failed")
        return {"status": "failed", "rounds": rounds, "steps": steps}

    def cancel(self, goal_id: str):
        """Cancel a running goal."""
        self._cancel_flags[goal_id] = True
        if goal_id in self._running:
            self._running[goal_id].cancel()

    @property
    def is_running(self) -> bool:
        return any(not t.done() for t in self._running.values())


# Singleton
goal_executor = GoalExecutor()
