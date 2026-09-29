"""
Destruction gate — classifies actions and gates destructive ones.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.gate")

DESTRUCTIVE_ACTIONS = {
    "delete_file", "close_app", "close_window",
    "run_command",  # depends on command content
    # Both of these can replace a file, and `move` can put a folder inside
    # another one. Gated for the same reason `delete_file` is: the user is told
    # before something they already have is overwritten. `requires_approval=True`
    # on the skill is what raises the card; naming them here is what makes the
    # gate's own answer agree with the card, so a grant works and the dashboard
    # is told the truth about what can ask.
    "move_file", "copy_file",
}

# Compound commands whose danger is in the whole name rather than in a single
# dangerous word, so the prefix rule cannot see them without also gating
# harmless names that happen to share a stem.
#
# `restart-computer` and `format-report` are structurally identical — a bare
# entry followed by a hyphen — and no punctuation rule can separate a cmdlet
# from a filename. Rather than guess, the ambiguous few are listed, and the
# punctuation rule stays conservative (a hyphen does NOT end a command word)
# so `format-report`, `cipher-check` and `logoff-report` are not gated as
# `format`, `cipher` and `logoff`.
COMPOUND_DANGEROUS = {
    "restart-computer", "stop-computer", "format-volume", "clear-disk",
    "remove-partition", "remove-item -recurse -force", "stop-process -force",
    "reset-computer", "start-process -verb runas",
}

# Characters that can follow a command word. Everything else is treated as part
# of the word, so `delete` is not `del` and `format-report` is not `format`.
# Defined once because the prefix rule and the compound rule must agree about
# where a command ends.
WORD_BOUNDARY = " \t/\\:=;|&>\"'"

def command_stem(entry: str) -> str:
    """The bare name of a dangerous-command entry, separator removed ONCE.

    Shared with `backend.approvals.policy` so the gate that decides what to ask
    about and the policy that decides what may be remembered cannot drift into
    disagreeing about what a dangerous command is.

    Deliberately not `rstrip("/\\\\")`: that strips the whole character set, so
    `"del "` / `"del/"` / `"del\\t"` all became `"del"` and swept in `delete`.
    """
    return str(entry or "").strip().rstrip("/\\").strip()

def matches_dangerous(cmd: str, entry: str) -> bool:
    """Does this command begin with this dangerous-command entry?

    `entry` comes from `terminal.DANGEROUS_COMMANDS`, which is a set of
    *prefixes* written the way a person types them: `"del "`, `"del/"`,
    `"rm -rf"`, `"reg delete"`.

    The obvious implementation — `cmd.startswith(entry.rstrip("/\\"))` — is
    wrong, and was the cause of safe commands being gated. `str.rstrip` takes a
    CHARACTER SET, not a suffix, so it strips every trailing slash and
    backslash, and the set contains `"del "`, `"del\\t"` and `"del/"`, which all
    collapse to `"del"`. The gate then flagged anything starting with `del` —
    `delete`, `delete-item` — and `"rd "` collapsed to `"rd"`, taking `rdp` with
    it. A safe command was reported as destructive, so the turn ended asking for
    permission the user should never have been asked for.

    Matching is therefore done on the entry with its own trailing separator
    removed ONCE, and the comparison insists on a word boundary: the command
    must either equal the stem or continue with something that cannot be part
    of the same word (a space, a separator, or a punctuated flag).
    """
    entry = str(entry or "").strip()
    if not entry:
        return False
    stem = command_stem(entry)
    if not stem or not cmd.startswith(stem):
        return False
    rest = cmd[len(stem):]
    if not rest:
        return True  # exactly the command
    # `del/q` and `del /q` are the command; `delete` is not.
    #
    # Two characters are deliberately NOT boundaries:
    #   `-` joins one word — `format-report`, `cipher-check` and
    #       `restart-computer-notes.txt` are names, not commands.
    #   `.` introduces a file extension far more often than it separates a
    #       command, so `restart.json` is a filename and not a reboot.
    # The compounds this would otherwise miss are named in COMPOUND_DANGEROUS,
    # which is the honest answer for the ambiguous few rather than a punctuation
    # rule that guesses between a cmdlet and a filename.
    return rest[0] in WORD_BOUNDARY

def is_compound_dangerous(cmd: str) -> bool:
    """Is this one of the named compound commands?

    Matched as a whole word for the same reason as everything else: the
    command must equal the name or continue past it at a boundary, so
    `restart-computer-notes.txt` is a filename and not a reboot.
    """
    for name in COMPOUND_DANGEROUS:
        stem = command_stem(name)
        if cmd == stem:
            return True
        if cmd.startswith(stem):
            rest = cmd[len(stem):]
            if rest and rest[0] in WORD_BOUNDARY:
                return True
    return False


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
            # Named compounds first: they are dangerous as a whole name and the
            # prefix rule below cannot see them without gating harmless names
            # that share a stem.
            if is_compound_dangerous(cmd):
                return True
            # Import the canonical dangerous-command set from terminal.py
            # so there is exactly one list to keep in sync.  Previously
            # this hardcoded 6 patterns while terminal.py had 18+; any
            # of the gap commands (diskpart, cipher, reg delete, net user,
            # takeown, icacls, logoff, erase, rd, …) slipped past the
            # gate, then terminal.py caught them with NO approval_id,
            # making them un-approvable: the dashboard could never allow
            # them because the executor never queued them.
            from backend.actions.terminal import DANGEROUS_COMMANDS
            return any(matches_dangerous(cmd, c) for c in DANGEROUS_COMMANDS)
        return action_type in DESTRUCTIVE_ACTIONS
