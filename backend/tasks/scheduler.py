"""Scheduler — tick-driven execution of tasks, reminders and housekeeping.

Polled from the engine tick (backend/engine.py). Due tasks fire their
registered action fire-and-forget; next_run is advanced BEFORE execution so
a crash mid-task never replays it. Quiet hours defer `notify` actions until
the window ends (the task stays due and fires right after).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from backend.tasks import recurrence
from backend.tasks.actions import ACTION_REGISTRY, in_quiet_hours
from backend.tasks.store import task_store

log = logging.getLogger("addled.tasks.scheduler")

STATE_PATH = Path(__file__).resolve().parent.parent / "memory" / "integrations" \
    / "scheduler_state.json"

MISSED_GRACE_S = 300  # beyond this, missed-policy applies


class Scheduler:
    def __init__(self):
        self._running: set[str] = set()
        self._housekeeping: dict[str, dict] = {}
        self._h_state: dict[str, float] = self._load_state()
        self._last_poll = 0.0

    # ---- state ---------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            if STATE_PATH.exists():
                return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_state(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps(self._h_state, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass

    # ---- registration --------------------------------------------------------

    def set_engine_emit(self, emit) -> None:
        from backend.tasks.actions import set_engine_emit
        set_engine_emit(emit)

    def register_housekeeping(self, name: str, fn, interval_s: float) -> None:
        """Register a periodic internal job (e.g. calendar reminders)."""
        self._housekeeping[name] = {"fn": fn, "interval_s": interval_s}
        self._h_state.setdefault(name, 0.0)

    # ---- polling -------------------------------------------------------------

    def enabled(self) -> bool:
        try:
            from backend.config import config
            return bool(config.get("scheduling", "enabled", default=True))
        except Exception:
            return True

    async def poll(self) -> dict:
        """Run due housekeeping jobs and dispatch due user tasks."""
        if not self.enabled():
            return {"scheduler": "disabled"}
        now = time.time()

        for name, job in list(self._housekeeping.items()):
            next_due = self._h_state.get(name, 0.0) + job["interval_s"]
            if now >= next_due:
                self._h_state[name] = now
                self._save_state()
                try:
                    if asyncio.iscoroutinefunction(job["fn"]):
                        asyncio.create_task(job["fn"]())
                    else:
                        job["fn"]()
                except Exception as e:
                    log.warning("housekeeping %s failed: %s", name, e)

        dispatched = 0
        for task in task_store.list_all(enabled=True):
            if task.id in self._running:
                continue
            if task.next_run <= now:
                self._dispatch(task, now)
                dispatched += 1
        return {"dispatched": dispatched}

    def _dispatch(self, task, now: float) -> None:
        # Quiet hours: keep notify tasks due but silent until window ends.
        if task.action == "notify" and in_quiet_hours():
            return
        overdue = now - task.next_run
        if overdue > MISSED_GRACE_S:
            policy = "catchup_once"
            try:
                from backend.config import config
                policy = config.get("scheduling", "missed_policy",
                                    default="catchup_once")
            except Exception:
                pass
            if policy == "skip":
                self._advance(task, now)
                task_store.update(task.id, {"last_run": now})
                log.info("task %s skipped (missed policy)", task.id)
                return
            log.info("task %s overdue by %.0fs — catch-up run",
                     task.id, overdue)
        self._running.add(task.id)
        asyncio.create_task(self._fire(task))

    async def _fire(self, task) -> None:
        try:
            self._advance(task, time.time())  # BEFORE executing: no replays
            action = ACTION_REGISTRY.get(task.action)
            if action is None:
                log.warning("task %s has unknown action %r",
                            task.id, task.action)
                return
            await action(task)
        except Exception as e:
            log.warning("task %s failed: %s", task.id, e)
        finally:
            self._running.discard(task.id)

    def _advance(self, task, now: float) -> None:
        rtype = (task.recurrence or {}).get("type", "none")
        if rtype == "none":
            task_store.update(task.id, {
                "enabled": False,
                "last_run": now,
            })
        else:
            nxt = recurrence.next_run(task, after=now)
            task_store.update(task.id, {
                "next_run": nxt,
                "last_run": now,
            })

    # ---- introspection -------------------------------------------------------

    def status(self) -> dict:
        tasks = [{
            "id": t.id, "title": t.title, "kind": t.kind,
            "action": t.action, "payload": t.payload[:80],
            "time": t.time, "date": t.date,
            "recurrence": t.recurrence, "next_run": t.next_run,
            "enabled": t.enabled, "last_run": t.last_run,
            "source": t.source, "rule": recurrence.humanize(t),
        } for t in task_store.list_all()]
        housekeeping = [{
            "name": name,
            "interval_s": job["interval_s"],
            "next_run": self._h_state.get(name, 0.0) + job["interval_s"],
        } for name, job in self._housekeeping.items()]
        return {"enabled": self.enabled(), "tasks": tasks,
                "housekeeping": housekeeping}


# Module-level singleton used by the engine tick and ws_server.
scheduler = Scheduler()
