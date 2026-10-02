"""Task persistence — JSON store mirroring calendar_integration.py."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("addled.tasks.store")

_now = time.time  # module-level alias: avoids class-body field shadowing

from backend import app_paths

STORE_PATH = app_paths.subdir("integrations") \
    / "scheduled_tasks.json"


@dataclass
class ScheduledTask:
    id: str
    title: str
    kind: str = "task"              # "task" | "reminder"
    action: str = "notify"          # "notify" | "chat"
    payload: str = ""
    time: str = "09:00"             # local HH:MM
    date: str = ""                  # one-shot anchor YYYY-MM-DD
    recurrence: dict = field(default_factory=lambda: {
        "type": "none",             # none | daily | weekly | monthly
        "weekdays": [],             # 0=Mon..6=Sun for weekly
    })
    next_run: float = 0.0           # epoch seconds
    enabled: bool = True
    last_run: float = 0.0
    source: str = "ui"              # ui | llm | voice
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduledTask":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


class TaskStore:
    """Persistent list of scheduled tasks with cap + duplicate rejection."""

    def __init__(self, max_scheduled: int = 20):
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.max_scheduled = max_scheduled
        self._tasks: dict[str, ScheduledTask] = self._load()

    def _load(self) -> dict[str, ScheduledTask]:
        if not STORE_PATH.exists():
            return {}
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            return {t["id"]: ScheduledTask.from_dict(t)
                    for t in raw if t.get("id")}
        except (json.JSONDecodeError, OSError, TypeError):
            return {}

    def _save(self) -> None:
        try:
            STORE_PATH.write_text(
                json.dumps([t.to_dict() for t in self._tasks.values()],
                           indent=2, ensure_ascii=False),
                encoding="utf-8")
        except OSError as e:
            log.warning("task save failed: %s", e)

    # ---- CRUD ----------------------------------------------------------------

    def add(self, task: ScheduledTask) -> tuple[ScheduledTask | None, str]:
        """Add a task. Enforces max_scheduled and duplicate rejection."""
        active = [t for t in self._tasks.values() if t.enabled]
        if len(active) >= self.max_scheduled:
            return None, f"schedule full ({self.max_scheduled} tasks max)"
        dup_key = (task.title.strip().lower(), task.time, task.date)
        for t in self._tasks.values():
            if (t.title.strip().lower(), t.time, t.date) == dup_key \
                    and t.enabled:
                return None, "duplicate task already scheduled"
        if not task.id:
            task.id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self._tasks[task.id] = task
        self._save()
        return task, ""

    def get(self, task_id: str) -> ScheduledTask | None:
        return self._tasks.get(task_id)

    def list_all(self, enabled: bool | None = None) -> list[ScheduledTask]:
        tasks = list(self._tasks.values())
        if enabled is not None:
            tasks = [t for t in tasks if t.enabled == enabled]
        return sorted(tasks, key=lambda t: t.next_run)

    def update(self, task_id: str, fields: dict) -> ScheduledTask | None:
        """Partial update. Caller recomputes next_run for time changes."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        for key, value in fields.items():
            if hasattr(task, key):
                setattr(task, key, value)
        self._save()
        return task

    def delete(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False


# Module-level singleton.
task_store = TaskStore()


def refresh_max_scheduled(max_scheduled: int) -> None:
    """Re-read the cap from config (used by the scheduler)."""
    task_store.max_scheduled = max_scheduled
