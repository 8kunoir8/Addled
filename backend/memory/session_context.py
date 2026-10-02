"""
Session context — per-session working memory.

Tracks what the user is working on within a single session.
Persisted to JSON for continuity across restarts.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.session_context")

from backend import app_paths

CONTEXT_PATH = app_paths.MEMORY_DIR / "session_context.json"


class SessionContext:
    """Per-session working memory."""

    def __init__(self):
        self.last_app_context: str = "unknown"
        self.last_action: dict | None = None
        self.active_workspace: str | None = None
        self.open_apps: list[str] = []
        self.session_start_time: float = time.time()
        self.interaction_count: int = 0
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        if CONTEXT_PATH.exists():
            try:
                with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.last_app_context = data.get("last_app_context", "unknown")
                self.last_action = data.get("last_action")
                self.active_workspace = data.get("active_workspace")
                self.open_apps = data.get("open_apps", [])
                self.interaction_count = data.get("interaction_count", 0)
                # Don't restore session_start_time — it's for this session
            except (json.JSONDecodeError, OSError):
                pass
        self._loaded = True

    def save(self):
        """Persist session context to disk."""
        CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONTEXT_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "last_app_context": self.last_app_context,
                "last_action": self.last_action,
                "active_workspace": self.active_workspace,
                "open_apps": self.open_apps,
                "interaction_count": self.interaction_count,
            }, f, indent=2)

    def record_interaction(self):
        self.interaction_count += 1

    def get_welcome_message(self) -> str | None:
        """Generate a welcome-back message based on last session."""
        self._load()
        if self.last_app_context and self.last_app_context != "unknown":
            return f"Welcome back! Last time you were {self.last_app_context}."
        return None


# Singleton
session_context = SessionContext()
