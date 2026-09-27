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
            cmd = params.get("command", "").lower().strip()
            # Import the canonical dangerous-command set from terminal.py
            # so there is exactly one list to keep in sync.  Previously
            # this hardcoded 6 patterns while terminal.py had 18+; any
            # of the gap commands (diskpart, cipher, reg delete, net user,
            # takeown, icacls, logoff, erase, rd, …) slipped past the
            # gate, then terminal.py caught them with NO approval_id,
            # making them un-approvable: the dashboard could never allow
            # them because the executor never queued them.
            from backend.actions.terminal import DANGEROUS_COMMANDS
            return any(cmd.startswith(c.rstrip()) for c in DANGEROUS_COMMANDS)
        return action_type in DESTRUCTIVE_ACTIONS
