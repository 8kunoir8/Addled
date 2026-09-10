"""Tasks — the Addled scheduler (store, recurrence, actions, scheduler).

In-process, tick-driven scheduling for:
  - user scheduled tasks (from UI, chat, voice)
  - calendar due-event reminders
  - internal housekeeping jobs (unified with memory maintenance)

No APScheduler/croniter — the engine tick polls `scheduler.poll()`
(backend/tasks/scheduler.py) and due tasks fire their registered action.
"""

from backend.tasks.store import ScheduledTask, TaskStore, task_store  # noqa: F401
from backend.tasks.recurrence import (  # noqa: F401
    next_run,
    expand_month,
    humanize,
    time_to_epoch,
)
