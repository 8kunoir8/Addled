"""
Destruction gate — classifies actions and gates destructive ones.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.gate")

DESTRUCTIVE_ACTIONS = {
    "delete_file", "close_app", "close_window",
    "run_command",  # depends on command content
}


class DestructionGate:
    """Classifies actions as safe or destructive."""

    def classify(self, action_type: str) -> str:
        if action_type in DESTRUCTIVE_ACTIONS:
            return "destructive"
        return "safe"

    def requires_approval(self, action_type: str, params: dict) -> bool:
        if action_type == "delete_file":
            return True
        if action_type == "run_command":
            cmd = params.get("command", "").lower()
            dangerous = {"del ", "rm ", "format", "shutdown", "restart", "rmdir"}
            return any(cmd.startswith(d) for d in dangerous)
        return action_type in DESTRUCTIVE_ACTIONS
