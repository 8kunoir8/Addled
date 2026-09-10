"""
Goal executor — runs goal steps sequentially with checkpointing.
"""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("addled.goals.executor")


class GoalExecutor:
    """Executes goal steps with checkpointing and recovery."""

    def __init__(self, action_executor=None, store=None):
        self._executor = action_executor
        self._store = store
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_flags: dict[str, bool] = {}

    def set_executor(self, executor):
        self._executor = executor

    def set_store(self, store):
        self._store = store

    async def run_goal(self, goal_id: str) -> dict:
        """Execute all steps of a goal. Returns final status."""
        if not self._store:
            return {"status": "failed", "error": "No goal store configured"}
        if not self._executor:
            return {"status": "failed", "error": "No action executor configured"}

        goal = self._store.load(goal_id)
        if not goal:
            return {"status": "failed", "error": f"Goal not found: {goal_id}"}

        self._cancel_flags[goal_id] = False
        plan = goal.get("plan", {})
        steps = plan.get("steps", [])

        # Sort steps by index
        steps.sort(key=lambda s: s.get("index", 0))

        # Checkpoint recovery: steps left 'in_progress' by a crashed run
        for step in steps:
            if step.get("status") == "in_progress":
                step["status"] = "pending"
                step.pop("error", None)
        self._store.save(goal)

        completed = set()
        attempts = 0
        max_attempts = max(8, 2 * len(steps) + 4)  # stuck guard

        def _progress(status: str, extra: dict | None = None):
            try:
                from backend.ws_server import get_server
                get_server().broadcast_nowait("goal.progress", {
                    "goalId": goal_id,
                    "status": status,
                    "step": len(completed),
                    "total": len(steps),
                    **(extra or {}),
                })
            except Exception:
                pass

        _progress("running")

        for step in steps:
            if self._cancel_flags.get(goal_id):
                self._store.update_status(goal_id, "cancelled")
                return {"status": "cancelled", "completed": len(completed), "total": len(steps)}

            step_idx = step.get("index", 0)
            deps = step.get("dependencies", [])

            # Check dependencies
            if deps and not all(d in completed for d in deps):
                step["status"] = "blocked"
                self._store.save(goal)
                continue

            if step.get("status") == "completed":
                completed.add(step_idx)
                continue

            attempts += 1
            if attempts > max_attempts:
                self._store.update_status(goal_id, "failed")
                _progress("failed", {"error": "too many attempts (stuck)"})
                return {"status": "failed", "error": "stuck",
                        "completed": len(completed), "total": len(steps)}

            # Execute step
            step["status"] = "in_progress"
            self._store.save(goal)

            try:
                from backend.actions.executor import ActionRequest
                request = ActionRequest(
                    action_type=step.get("action_type", "run_command"),
                    params=step.get("params", {}),
                )
                result = await self._executor.execute(request)

                if result.success:
                    step["status"] = "completed"
                    step["result"] = {"summary": result.summary, "data": result.data}
                    completed.add(step_idx)
                    _progress("step_completed")
                else:
                    step["status"] = "failed"
                    step["error"] = result.error
                    _progress("step_failed", {"error": str(result.error)[:120]})
                    # Retry once with modified approach
                    if step.get("retry_count", 0) < 1:
                        step["retry_count"] = step.get("retry_count", 0) + 1
                        step["status"] = "pending"
                        self._store.save(goal)
                        continue
                    self._store.update_status(goal_id, "failed")
                    try:
                        from backend.character.mood import mood_engine
                        mood_engine.event("goal_stuck")
                    except Exception:
                        pass
                    return {"status": "failed", "step": step_idx, "error": result.error,
                            "completed": len(completed), "total": len(steps)}
            except Exception as e:
                step["status"] = "failed"
                step["error"] = str(e)
                self._store.update_status(goal_id, "failed")
                _progress("failed", {"error": str(e)[:120]})
                return {"status": "failed", "step": step_idx, "error": str(e)}

            self._store.save(goal)
            await asyncio.sleep(0.5)  # Brief pause between steps

        self._store.update_status(goal_id, "completed")
        _progress("completed")
        try:
            from backend.character.mood import mood_engine
            mood_engine.event("goal_done")
        except Exception:
            pass
        return {"status": "completed", "completed": len(completed), "total": len(steps)}

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
