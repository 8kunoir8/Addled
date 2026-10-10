"""Dangerous-command classification and the gated-turn reply.

Four defects motivated these, all reported from a local model that appeared to
fail at running PowerShell commands:

  * `str.rstrip` takes a CHARACTER SET, not a suffix. `entry.rstrip("/\\")`
    collapsed `"del "`, `"del\\t"` and `"del/"` to `"del"`, so the gate flagged
    anything starting with `del` (`delete`, `delete-item`) and `"rd "` to
    `"rd"`, sweeping in `rdp`. Safe commands were gated.
  * the same `rstrip` in `approvals.policy` made those names permanently
    un-grantable.
  * `terminal.execute` accepted `allow_dangerous` and never read it, while the
    executor passed its `_approved_run` flag into it.
  * a permission request returns `success=False`, so `tool_loop`'s all-failed
    branch rendered it as "I couldn't run the tool for that" — the failed
    attempt the user saw, where a permission prompt had been raised.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_command_gate.py
"""

import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import asyncio  # noqa: E402

fails = []

def check(label, ok, detail=""):
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        fails.append(label)

print("Dangerous-command classification")

from backend.safety.destruction_gate import (  # noqa: E402
    DestructionGate, command_stem, matches_dangerous)
from backend.actions.terminal import DANGEROUS_COMMANDS  # noqa: E402

gate = DestructionGate()
gated = lambda c: gate.requires_approval("run_command", {"command": c})

# The regression: things that merely START with a dangerous stem must be safe.
# Every one of these was gated by the rstrip bug.
print("\n  -- safe commands must NOT ask --")
for cmd in [
    "delete-item foo",            # starts with 'del'
    "Get-ChildItem C:\\delete",   # contains 'del', does not start with it
    "rdp",
    "reg query HKLM\\Software",
    "format-report",              # starts with 'format'
    "erasure-check",              # starts with 'erase'
    "shutdownwatch",              # starts with 'shutdown'
    "net use",                     # 'net user'/'net localgroup' are dangerous, not 'net'
    "cipher-check",
    "logoff-report",
    "directions.md",
    "tasklist",
    "echo deleting nothing",
    "git status",
    # `Remove-Item` USED to sit here, marked safe with the note "PowerShell
    # spelling, not on the list". That was the bug, written down as the
    # expected answer: it is the shell Addled runs, and it deletes files. It is
    # now asserted as destructive further down, with the rest of the PowerShell
    # verbs, and the fact a passing test enshrined the hole is the reason that
    # block exists.
    "restart-computer-notes.txt",     # a filename, not a reboot
    "restart.json",
    "format-report-2026.md",
]:
    check(f"safe: {cmd!r}", not gated(cmd), "was gated but should not ask")

print("\n  -- destructive commands MUST ask --")
for cmd in [
    "del /q file.txt",
    "del/q file.txt",
    "del file.txt",
    "del\tfile.txt",
    "erase file.txt",
    "rmdir /s /q folder",
    "rd /s folder",
    "rm -rf /tmp/x",
    "rm file.txt",
    "format C:",
    "shutdown /s /t 0",
    "restart-computer",
    "logoff",
    "diskpart",
    "cipher /w:C",
    "reg delete HKLM\\Software\\Foo /f",
    "reg add HKLM\\Software\\Foo /v Bar /d 1",
    "net user hacker /add",
    "net localgroup Administrators hacker /add",
    "takeown /f C:\\Windows\\System32",
    "icacls C:\\ /grant Everyone:F",
    "Restart-Computer -Force",        # compound, named explicitly
    "Stop-Computer",
    "Clear-Disk -Number 1",
]:
    check(f"asks: {cmd!r}", gated(cmd), "was NOT gated but must ask")

print("\n  -- the rstrip bug itself --")
check("'del ' does not stem to 'del'",
      command_stem("del ").strip() == "del")
check("'delete' is not matched by the 'del ' entry",
      not matches_dangerous("delete-item x", "del "))
check("'del /q x' IS matched by the 'del ' entry",
      matches_dangerous("del /q x", "del "))
check("'rdp' is not matched by the 'rd ' entry",
      not matches_dangerous("rdp", "rd "))
check("'rd /s' IS matched by the 'rd ' entry",
      matches_dangerous("rd /s", "rd "))
check("'rm -rf' still matched", matches_dangerous("rm -rf /", "rm -rf"))
check("'rmdir /s' still matched", matches_dangerous("rmdir /s x", "rmdir /s"))
# A command that merely contains a dangerous word is not a dangerous command.
check("'echo del file' is not matched at the start",
      not matches_dangerous("echo del file", "del "))

print("\nGrant policy agrees with the gate")

from backend.approvals import policy  # noqa: E402

check("run_command is never permanently grantable",
      policy.is_protected("run_command"))
check("delete_file is not protected by the 'del' prefix",
      not policy.is_protected("delete_file"))
check("a bare 'del' name is protected", policy.is_protected("del"))
check("'rd' name is protected", policy.is_protected("rd"))
names = policy._command_names()
check("no 'delete' name leaked into the command set",
      "delete" not in names, f"names={sorted(names)}")
check("no 'delete-item' name leaked in", "delete-item" not in names)
check("'rdp' did not become a protected name", "rdp" not in names)

print("\nDead parameter is gone")

import inspect  # noqa: E402
from backend.actions.terminal import TerminalExecutor  # noqa: E402

sig = inspect.signature(TerminalExecutor.execute)
check("execute() has no allow_dangerous",
      "allow_dangerous" not in sig.parameters, str(sig))
check("execute() still takes command/cwd/timeout",
      {"command", "cwd", "timeout"} <= set(sig.parameters), str(sig))

# -- every route into a shell is gated ---------------------------------------
#
# `session_send` types a line into a live PowerShell - the same shell
# `run_command` runs - and it was registered with `requires_approval` left
# False. So a command was reachable through a door that never asked, while the
# door beside it asked every time. Measured before the fix: sending
# `Remove-Item <path> -Force` into a session deleted the file, with
# `requires_approval` False and no prompt.
#
# Asserted as a property over every skill rather than as a name check, because
# the failure is "a skill that runs a command is not gated", and naming the two
# that do it today would not catch the third written tomorrow.
#
# `session_open` is deliberately NOT gated: it starts a shell and runs nothing,
# and its description says so. Asking twice to do one thing is how a prompt
# teaches a user to click through it - the cost this whole gate exists to avoid.
print("\nEvery route that runs a command is gated")

from backend.skills.registry import skill_registry as _reg  # noqa: E402

# A skill executes a command if it drives a session, calls the terminal
# executor, or exports the terminal action. Read from the source because that
# is what the handler closes over - a declared flag can be absent while the
# code still runs.
#
# `sessions.open` is not a marker: opening a shell runs nothing, and gating it
# would put a prompt in front of a no-op.
_SHELL_MARKERS = ("TerminalExecutor", "sessions.send", "session_send",
                  "run_command")


def _runs_a_command(name: str) -> bool:
    """Does this skill's handler run a command in a shell?

    Read from the source, because the declared flag is exactly what was
    missing: the handler can reach a shell while `requires_approval` sits at
    its default of False, which is the bug this exists to catch.
    """
    import inspect
    skill = _reg.get(name)
    if skill is None:
        return False
    try:
        src = inspect.getsource(skill.handler)
    except Exception:  # noqa: BLE001
        return False
    return any(m in src for m in _SHELL_MARKERS)


_runner_skills = [s.name for s in _reg.list_all() if _runs_a_command(s.name)]
check("the command-running skills were found at all",
      {"run_command", "session_send"} <= set(_runner_skills),
      f"found {sorted(_runner_skills)}")
check("a skill that only opens a shell is not treated as a runner",
      "session_open" not in _runner_skills,
      "opening a shell runs nothing; gating it is a prompt for a no-op")

_ungated = [n for n in _runner_skills
            if not getattr(_reg.get(n), "requires_approval", False)]
check("no skill that can run a command is left ungated",
      not _ungated,
      f"these run a command without asking: {sorted(_ungated)}")

# Its danger is the command it carries, not its name, so a permanent grant
# cannot tell a safe line from `Remove-Item -Recurse -Force`. It must be
# session-only, exactly as `run_command` is - otherwise the dashboard offers an
# "always allow" switch that un-gates the shell for the rest of time.
from backend.approvals import policy as _policy  # noqa: E402

check("session_send is classified by its command, not its name",
      "session_send" in _policy.CONTENT_CLASSIFIED,
      "a permanent grant would disarm the shell for every future command")
check("session_send cannot be permanently granted",
      not _policy.is_permanently_grantable("session_send"),
      "the dashboard would offer a permanent switch")
check("run_command is still content-classified",
      "run_command" in _policy.CONTENT_CLASSIFIED,
      "the existing entry must not have been replaced")

# The dashboard reads these two fields to decide which control to draw. If the
# flag is set but the policy is not, the card shows an "always allow" switch
# that the backend will refuse - a control that only fails.
_live = _reg.get("session_send")
check("the dashboard will draw a session switch, not a permanent one",
      bool(getattr(_live, "requires_approval", False))
      and not _policy.is_permanently_grantable("session_send"),
      "requires_approval drives the badge; is_permanently_grantable picks the control")

# Both routes into a skill must give the same answer.
#
# This is the check that was missing, and its absence is why the first fix
# looked complete and was not. A skill is reached two ways:
#
#   chat turn        -> registry.execute     -> the skill's `requires_approval`
#   action.execute   -> executor.execute     -> DestructionGate.DESTRUCTIVE_ACTIONS
#
# The skill flag and the gate's set are separate lists, and setting only the
# flag made the chat path ask while the socket path - the one the dashboard and
# a bot use - ran the command anyway. In-process tests passed; the installed app
# still deleted the file. Asserting the two agree is what closes it.
from backend.safety.destruction_gate import DestructionGate  # noqa: E402

# `run_command` is content-classified: an empty command is harmless, so the gate
# answers False for bare params and True once the command would do damage. The
# assertion must hand it something destructive, or it tests the wrong question
# and reports a false failure. `session_send` is gated on the NAME, so bare
# params are enough for it - and both are covered, because a door that asks only
# for some shapes of command is the bug, not the fix.
_PROBE_PARAMS = {
    "run_command": {"command": "Remove-Item C:\\somewhere -Recurse -Force"},
    "session_send": {},
}
_gate = DestructionGate()
for _name in sorted(_runner_skills):
    check(f"the gate also treats '{_name}' as destructive",
          _gate.requires_approval(_name, _PROBE_PARAMS.get(_name, {})),
          f"registry asks but executor.execute would run '{_name}' unattended")

# The shell Addled actually runs. The prefix table is written the way cmd.exe
# spells things, so `del` was gated and the `Remove-Item` a model is far more
# likely to write was not - on the socket path, where `run_command`'s own
# DESTRUCTIVE_ACTIONS membership was skipped by an early return.
print("\nPowerShell's own destructive verbs are classified")

_PS_DANGEROUS = [
    "Remove-Item C:\\x -Force",
    "Remove-Item C:\\x -Recurse -Force",
    "Clear-Content C:\\x",
    "Set-ExecutionPolicy Bypass",
    "Stop-Service -Name WinDefend",
    "Set-MpPreference -DisableRealtimeMonitoring $true",
    "New-LocalUser -Name a -NoPassword",
    "Remove-LocalGroupMember -Group Administrators -Member a",
    "iex 'Remove-Item C:\\x'",
    "Invoke-Expression \"Remove-Item C:\\x\"",
    "& 'Remove-Item' C:\\x",
    "cmd /c del C:\\x",
]
_PS_SAFE = [
    "Write-Output hello",
    "Get-Location",
    "dir C:\\",
    "git status",
    "Test-Path C:\\x",
    "Remove-NotesFromFile x",   # a name that merely starts with a verb
    "Get-ItemProperty HKLM:\\x",
]
for _c in _PS_DANGEROUS:
    check(f"asks: {_c!r}", _gate.requires_approval("run_command", {"command": _c}),
          "was NOT gated but must ask")
for _c in _PS_SAFE:
    check(f"safe: {_c!r}", not _gate.requires_approval("run_command", {"command": _c}),
          "was gated but should not ask")

# And the two lists must not drift: everything the gate calls destructive that
# is also a skill has to carry the flag.
from backend.safety.destruction_gate import DESTRUCTIVE_ACTIONS  # noqa: E402

_flagless = [n for n in sorted(DESTRUCTIVE_ACTIONS)
             if _reg.get(n) is not None
             and not getattr(_reg.get(n), "requires_approval", False)]
check("no skill in the gate's destructive set lacks the skill flag",
      not _flagless,
      f"the gate waits but the card says nothing for: {_flagless}")

# The registry handler is how a chat turn reaches the terminal. If it still
# passed the removed kwarg this raises TypeError at call time, not import time.
from backend.actions.executor import ActionExecutor  # noqa: E402
ex = ActionExecutor()
ex._lazy_init()
check("executor registered a run_command handler",
      "run_command" in ex._handlers)
# The handler is a lambda wrapping `t.execute(...)`. Calling it with a safe
# command is the real test that the removed kwarg is not still being passed —
# a stale `allow_dangerous=` would raise TypeError here.
try:
    out = asyncio.run(ex._handlers["run_command"]({"command": "echo gate-probe"}))
    check("handler runs without the removed kwarg",
          isinstance(out, dict) and "success" in out, f"returned {out!r}")
except TypeError as e:
    check("handler runs without the removed kwarg", False, f"TypeError: {e}")
except Exception as e:  # noqa: BLE001
    # A non-TypeError means the call was accepted and the shell did something.
    check("handler runs without the removed kwarg", True,
          f"accepted (shell said: {type(e).__name__})")

print("\nA gated turn is not reported as a failure")
# Drive the real tool_loop branch by calling it with a gated-shaped result.
# The all-failed branch is only reached when no tool succeeded, so a pending
# approval plus no successes must take the approval path instead.
import asyncio  # noqa: E402
import backend.skills.tool_loop as tl  # noqa: E402

SOURCE = Path(ROOT) / "backend" / "skills" / "tool_loop.py"
src = SOURCE.read_text(encoding="utf-8", errors="replace")
check("approval is separated before the all-failed branch",
      src.index("requires_approval\")]") < src.index(
          "if all(not tr[\"success\"] for tr in tool_results):"),
      "the pending-approval check must come first")
check("the stale 'waits in-band' comment is gone",
      "waits in-band for the user's decision" not in src)
check("the approval reply does not say 'couldn't run'",
      "This needs your approval before it can run" in src)

print("\nNo remaining rstrip misuse")
# The one legitimate use is inside `command_stem`, which strips a separator
# from a single entry (not a character set applied to a command). Any OTHER
# rstrip on the dangerous-command set is the bug returning. Comments and
# docstrings describe the old defect and are not code, so they are skipped by
# tracking whether we are inside a triple-quoted block.
for rel in ["backend/safety/destruction_gate.py",
            "backend/approvals/policy.py"]:
    bad = []
    in_doc = False
    for i, ln in enumerate((Path(ROOT) / rel).read_text(
            encoding="utf-8", errors="replace").splitlines(), 1):
        if ln.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or ln.strip().startswith("#"):
            continue
        code = ln.split("#", 1)[0]
        if "rstrip(" in code and rel.endswith("policy.py"):
            bad.append(i)
    check(f"{rel} has no rstrip on the command set", not bad, f"lines {bad}")

# And the one shared implementation must exist and behave.
from backend.safety.destruction_gate import command_stem as _stem  # noqa: E402
check("command_stem('del/') strips one separator -> 'del'",
      _stem("del/") == "del", _stem("del/"))
check("command_stem('rmdir /s') keeps the inner space",
      _stem("rmdir /s") == "rmdir /s", _stem("rmdir /s"))

print("\nFile operations are reachable from a chat turn")
# `FileOps.move`/`copy` existed and were wired into the ActionExecutor, but
# neither was registered as a SKILL — so the capability was invisible to the
# model. Asked to rename a file it correctly reported "none of the listed tools
# can rename files" and fell back to shelling out to `Rename-Item`. Renaming is
# a move, so move_file is the one entry point; a separate rename_file would be a
# second name for the same operation to keep in step.
from backend.skills.registry import skill_registry  # noqa: E402

for name in ("move_file", "copy_file"):
    skill = skill_registry.get(name)
    check(f"{name} is registered as a skill", skill is not None,
          "the model cannot see or call it")
    if skill:
        check(f"{name} asks before it can clobber",
              bool(skill.requires_approval),
              "a move or copy can replace a file and must be gated")
        props = (skill.parameters or {}).get("properties", {})
        check(f"{name} declares the source/dest names FileOps takes",
              {"source", "dest"} <= set(props),
              f"got {sorted(props)}")
check("move_file is described as the way to rename",
      "rename" in (skill_registry.get("move_file").description or "").lower(),
      "the model has to be told a move within a folder IS the rename")
check("there is no separate rename_file skill",
      skill_registry.get("rename_file") is None,
      "a second entry point for the same operation would drift")

# The gate must agree with the skill, or a grant cannot take effect: the card is
# raised from `requires_approval`, and `_gated` consults THIS set.
from backend.safety.destruction_gate import DESTRUCTIVE_ACTIONS  # noqa: E402
for name in ("move_file", "copy_file"):
    check(f"the gate owns '{name}' too", name in DESTRUCTIVE_ACTIONS,
          "the card would ask but the gate would not agree")
    check(f"'{name}' can be answered and remembered",
          gate.requires_approval(name, {}) is True,
          "an un-gated name cannot be granted")

print("\nThe rename actually works, end to end on disk")
# Not a mock: a real file, moved by the real FileOps, checked on the filesystem.
import tempfile  # noqa: E402

from backend.actions.file_ops import FileOps  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="move_check_"))
(tmp / "probe").mkdir()
src, dst = tmp / "probe" / "alpha.txt", tmp / "probe" / "beta.txt"
src.write_text("hello from the lifecycle test\n", encoding="utf-8")

import backend.workspace as workspace  # noqa: E402

_saved_root = None
try:
    from backend.config import config as _cfg
    _saved_root = _cfg.get("workspace", "root", default="")
except Exception:  # noqa: BLE001
    _cfg = None

# Point the workspace at the probe dir for the duration. The guard resolves
# relative paths against it and refuses anything outside.
try:
    if _cfg is not None:
        _cfg.set("workspace", "root", value=str(tmp))
    res = asyncio.run(FileOps().move(str(src), str(dst)))
    check("move reports success", bool(res.get("success")), str(res)[:200])
    check("the original name is gone", not src.exists())
    check("the new name is there", dst.exists())
    if dst.exists():
        check("and the contents survived the move",
              "lifecycle test" in dst.read_text(encoding="utf-8"))
finally:
    if _cfg is not None and _saved_root is not None:
        _cfg.set("workspace", "root", value=_saved_root)
    import shutil as _sh
    _sh.rmtree(tmp, ignore_errors=True)

print("\nParameter aliases and validation")
# The reported failure: a local model called write_file({"file_path": ...});
# the skill declares `path`, so the handler read "" and returned "No path
# given." — true, and useless. Nothing told the model WHICH name it got wrong,
# so it retried the same shape or gave up. Two things are checked: a plausible
# alias now resolves, and a call that cannot succeed says so without the user
# being asked to approve it first.
_w = skill_registry.get("write_file")
_norm, _err = _w.normalise({"file_path": "a.txt", "text": "hi"})
check("file_path is accepted as an alias for path",
      _norm.get("path") == "a.txt", f"got {_norm}")
check("text is accepted as an alias for content",
      _norm.get("content") == "hi", f"got {_norm}")
check("and the alias call raises no complaint", not _err, _err)

_norm2, _err2 = _w.normalise({"path": "a.txt", "content": "hi"})
check("a correct call is untouched", _norm2.get("path") == "a.txt" and not _err2,
      f"{_norm2} / {_err2}")
check("an unknown extra key does not block a valid call",
      not _w.normalise({"path": "a.txt", "content": "x", "junk": 1})[1])
check("an empty required value counts as missing",
      bool(_w.normalise({"path": "", "content": "x"})[1]))

# The message has to be actionable, or it is the same dead end in new words.
_, _trouble = _w.normalise({"alpha": "x"})
check("the complaint names what was sent", "alpha" in _trouble, _trouble)
check("the complaint names what is expected",
      "path" in _trouble and "content" in _trouble, _trouble)

# Each skill's aliases are exercised with EVERY required parameter present —
# testing one alias at a time made a partially-filled call look like a broken
# alias when it was a correctly-reported missing parameter.
_ALIAS_CASES = (
    ("read_file", {"file_path": "x"}),
    ("write_file", {"file_path": "x", "text": "c"}),
    ("delete_file", {"filename": "x"}),
    ("create_dir", {"directory": "x"}),
    ("file_info", {"filepath": "x"}),
    ("move_file", {"src": "x", "dst": "y"}),
    ("copy_file", {"src": "x", "destination": "y"}),
)
for _name, _sent in _ALIAS_CASES:
    _sk = skill_registry.get(_name)
    _norm, _err = _sk.normalise(dict(_sent))
    check(f"{_name} accepts {sorted(_sent)}", not _err, f"{_norm} / {_err}")

# Ordering: a malformed call must be refused BEFORE an approval is raised,
# or the user is asked to authorise a call that cannot run either way.
_reg_src = (Path(ROOT) / "backend" / "skills" / "registry.py").read_text(
    encoding="utf-8", errors="replace")
check("parameters are normalised before approval is requested",
      _reg_src.index("skill.normalise(params)") <
      _reg_src.index("await self._execute_gated("),
      "a bad call would raise a permission card and then fail anyway")

print("\nA session grant suppresses the prompt on the skill path")
# `_is_granted` alias-listed only `is_always_allowed`, which reads the PERMANENT
# list. "Allow for session" is the only grant a content-classified skill can
# have, so the answer was stored, the dashboard showed it as granted, and the
# very next call asked again — a button that visibly does nothing.
check("the registry's grant check consults session grants too",
      "is_allowed_for_session" in inspect.getsource(
          skill_registry._is_granted),
      "a session grant would never take effect here")

from backend.approvals import policy as _pol  # noqa: E402
_pol.clear_session()
_pol.allow_for_session(_pol.SKILL, "move_file")
check("a session grant makes _is_granted true",
      skill_registry._is_granted("move_file") is True,
      "the prompt would return on the next call")
_pol.revoke_session(_pol.SKILL, "move_file")
check("revoking it brings the prompt back",
      skill_registry._is_granted("move_file") is False)

print("\nA test cannot corrupt the live settings")
# check_code_editor.py writes workspace.root to exercise containment. It used to
# do that on the REAL settings.json, restoring in a `finally` — so an
# interrupted run left the installed app bound to a temp folder a test created.
# That happened, and ~200 temp workspaces accumulated. The redirect must exist
# and must not be a no-op.
from backend.config import config as _cfg  # noqa: E402
from backend.config import settings_path, use_settings_file  # noqa: E402
_real = settings_path()
_tmp_cfg = Path(tempfile.mkdtemp(prefix="cfgcheck_")) / "settings.json"
use_settings_file(_tmp_cfg)
check("the redirect moves the write path",
      settings_path() == _tmp_cfg, f"still {settings_path()}")
_cfg.set("workspace", "root", value="/a/test/path")
check("and a write lands in the redirected file, not the real one",
      "a/test/path" in _tmp_cfg.read_text(encoding="utf-8"),
      "the write did not reach the temp file")
check("the real settings file was not touched",
      (not _real.exists()
       or "/a/test/path" not in _real.read_text(
           encoding="utf-8", errors="replace")),
      "the live settings were modified by a test")
use_settings_file(None)
check("clearing the redirect restores the real path",
      settings_path() == _real, f"stuck at {settings_path()}")
_cfg._data = {}
_cfg.load()          # put the real settings back in memory
import shutil as _shutil  # noqa: E402
_shutil.rmtree(_tmp_cfg.parent, ignore_errors=True)

# ---- the seam: the classifier must actually be REACHED ----------------------
# Everything above tests the classifier in isolation. The bug the user hit was
# that `run_command` never got there: the skill's `requires_approval=True` sent
# every call to the approval path, so a read-only `Get-Command` asked for
# permission and the destructive path was the only one exercised. A gate check
# that only asks the gate cannot see that.
#
# Both directions are asserted, because either one alone is passed by a broken
# build: "no prompt" is also true if nothing is gated, and "prompts" is also
# true if everything is.
print()
print("The skill path reaches the gate (and still stops the dangerous ones)")
import asyncio as _aio  # noqa: E402
from backend.skills.registry import skill_registry as _reg  # noqa: E402


def _run(name, params):
    return _aio.new_event_loop().run_until_complete(_reg.execute(name, params))


_safe = _run("run_command", {"command": "Get-Command ffmpeg -ErrorAction SilentlyContinue"})
check("a read-only command RUNS through the registry",
      getattr(_safe, "success", False) is True,
      f"success={getattr(_safe, 'success', None)} -- the gate should have cleared it")
check("and it did not raise a permission card",
      not (getattr(_safe, "data", None) or {}).get("requires_approval"),
      str(getattr(_safe, "data", None))[:160])

_danger = _run("run_command", {"command": "Remove-Item C:\\__no_such_probe__ -Recurse -Force"})
check("a destructive command is REFUSED with requires_approval",
      (getattr(_danger, "data", None) or {}).get("requires_approval") is True,
      f"data={str(getattr(_danger, 'data', None))[:160]}")
check("and it did not run",
      getattr(_danger, "success", None) is False,
      "a gated command must not report success")

# A name whose danger is not in its arguments keeps asking, so the fix cannot be
# mistaken for "the flag no longer matters".
_uncond = _run("delete_file", {"path": "C:\\__no_such_probe__.txt"})
check("a skill gated by NAME still asks",
      (getattr(_uncond, "data", None) or {}).get("requires_approval") is True,
      f"data={str(getattr(_uncond, 'data', None))[:160]}")

print()
if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("All command-gate checks passed.")