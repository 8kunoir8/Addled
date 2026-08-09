"""
Goal store — JSON persistence for goal state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("addled.goals.store")

STORE_DIR = Path(__file__).parent.parent / "memory" / "goals"


class GoalStore:
    """Persists goals as JSON files."""

    def __init__(self):
        STORE_DIR.mkdir(parents=True, exist_ok=True)

    def create(self, title: str, description: str = "", priority: str = "normal",
               plan: dict | None = None) -> str:
        """Create a new goal, return its ID."""
        import uuid
        goal_id = f"goal_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        goal = {
            "id": goal_id,
            "title": title,
            "description": description,
            "status": "pending",
            "priority": priority,
            "created_at": time.time(),
            "updated_at": time.time(),
            "plan": plan or {"steps": []},
            "checkpoints": [],
        }
        self.save(goal)
        return goal_id

    def save(self, goal: dict):
        goal["updated_at"] = time.time()
        path = STORE_DIR / f"{goal['id']}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(goal, f, indent=2, ensure_ascii=False)

    def load(self, goal_id: str) -> dict | None:
        path = STORE_DIR / f"{goal_id}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_all(self, status: str | None = None) -> list[dict]:
        goals = []
        if not STORE_DIR.exists():
            return goals
        for f in STORE_DIR.glob("*.json"):
            try:
                goal = json.loads(f.read_text(encoding="utf-8"))
                if status and goal.get("status") != status:
                    continue
                goals.append(goal)
            except (json.JSONDecodeError, OSError):
                pass
        goals.sort(key=lambda g: g.get("created_at", 0), reverse=True)
        return goals

    def update_status(self, goal_id: str, status: str):
        goal = self.load(goal_id)
        if goal:
            goal["status"] = status
            self.save(goal)

    def delete(self, goal_id: str):
        path = STORE_DIR / f"{goal_id}.json"
        if path.exists():
            path.unlink()

    def resume_interrupted(self) -> list[str]:
        """Find goals that were in_progress and resume them."""
        interrupted = []
        for goal in self.list_all():
            if goal.get("status") == "in_progress":
                interrupted.append(goal["id"])
        return interrupted


# Singleton
goal_store = GoalStore()
