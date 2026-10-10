"""The console — a terminal that shows what Addled ran, and that you can type into.

Reported 2026-10-10: asked to check for whisper, Addled said "On it - checking
now", ran nothing, and the NEXT turn invented "I asked to run two commands and
they're still waiting on your approval". Nothing had run; nothing was pending.

Two defects make that possible, and both are guarded here:

  * the commands were invisible, so a claim about them could not be checked, and
  * a command the model NEVER ISSUED left no trace at all, so the absence of a
    row was indistinguishable from a row nobody looked at.

The checks below therefore cover the successes AND the states where nothing ran
(awaiting / denied) - a console that only recorded output would pass a test
suite about output and still have proved nothing about the 16:20 turn.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_console.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import asyncio  # noqa: E402

fails = []


def check(label, ok, detail=""):
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        fails.append(label)


from backend.actions import console_log as console  # noqa: E402
from backend.actions.terminal import TerminalExecutor  # noqa: E402

# A fresh slate: the store is module-level and outlives one check.
console.reset()


def run(coro):
    return asyncio.run(coro)


print("Capture — a command that ran")

console.reset()
_out = run(TerminalExecutor().execute("echo console-check-ok"))
_snap = console.snapshot()
check("a shell command is recorded", _snap["count"] == 1, f"count={_snap['count']}")
if _snap["count"]:
    _e = _snap["entries"][0]
    check("with its command text", "console-check-ok" in _e["command"], _e["command"])
    check("and its output", "console-check-ok" in _e["stdout"], repr(_e["stdout"]))
    check("and its exit code", _e["exit_code"] == 0, str(_e["exit_code"]))
    check("and a duration", isinstance(_e["duration_ms"], int), str(_e["duration_ms"]))
    check("marked ok", _e["status"] == "ok", _e["status"])

print("\nCapture — argv, the path a per-caller approach would miss")

console.reset()
run(TerminalExecutor().execute_argv(["cmd", "/c", "echo", "argv-check"]))
_snap = console.snapshot()
check("an argv command is recorded", _snap["count"] == 1, f"count={_snap['count']}")
if _snap["count"]:
    _e = _snap["entries"][0]
    check("marked as argv", _e["kind"] == "argv", _e["kind"])
    check("argv is preserved", "argv-check" in " ".join(_e["argv"] or []), str(_e["argv"]))

print("\nCapture — a command that failed")

console.reset()
run(TerminalExecutor().execute("exit 3"))
_e = console.snapshot()["entries"][0]
check("a non-zero exit is recorded as failed", _e["status"] == "failed", _e["status"])
check("with the exit code kept", _e["exit_code"] == 3, str(_e["exit_code"]))

print("\nCapture — a command that timed out")

console.reset()
run(TerminalExecutor().execute("Start-Sleep -Seconds 5", timeout=1))
_e = console.snapshot()["entries"][0]
check("a timeout is its own status, not a silent success",
      _e["status"] == "timeout", _e["status"])
check("and the reason is kept", bool(_e["stderr"].strip()), "stderr empty")

print("\nThe states that make a lying model visible")

# A command that never ran never reaches the terminal, so these are the only
# evidence that the model's claim about it is false. This is the 16:20 guard.
console.reset()
_pending = console.make_entry(command="Remove-Item -Recurse -Force ./build",
                              status=console.AWAITING, approval_id="appr_probe_1")
console.record(_pending)
_snap = console.snapshot()
check("a command waiting for approval is recorded",
      _snap["count"] == 1 and _snap["entries"][0]["status"] == "awaiting",
      f"{_snap['count']}/{_snap['entries'][0]['status'] if _snap['count'] else '-'}")

# Approving it later must UPDATE that row, not add a second one. Two ids for one
# command leaves a row stuck on "waiting" forever, which is the very claim the
# console exists to refute.
console.update("appr_probe_1", status=console.OK, stdout="deleted",
               exit_code=0, duration_ms=12)
_snap = console.snapshot()
check("approving it updates the SAME row", _snap["count"] == 1,
      f"count={_snap['count']} — a queued command and its execution split in two")
check("which now reads ok", _snap["entries"][0]["status"] == "ok",
      _snap["entries"][0]["status"])
check("and kept the command it held", "Remove-Item" in _snap["entries"][0]["command"],
      _snap["entries"][0]["command"])

console.reset()
console.record(console.make_entry(command="Remove-Item x", status=console.AWAITING,
                                  approval_id="appr_probe_2"))
console.update("appr_probe_2", status=console.DENIED, duration_ms=0)
_e = console.snapshot()["entries"][0]
check("a denied command stays visible as denied", _e["status"] == "denied",
      "a row that vanishes is the silence the model exploited")

print("\nThe gate itself records the pending state")

console.reset()
from backend.actions.executor import ActionExecutor  # noqa: E402
from backend.safety.destruction_gate import DestructionGate  # noqa: E402

_ex = ActionExecutor(gate=DestructionGate())
_ex._lazy_init()
_res = run(_ex.execute_for_chat("run_command",
                                {"command": "Remove-Item -Recurse -Force ./build"}))
_snap = console.snapshot()
check("a gated action records its command", _snap["count"] == 1,
      f"count={_snap['count']}")
if _snap["count"]:
    _aid = (_res.data or {}).get("approval_id")
    _e = _snap["entries"][0]
    check("as awaiting", _e["status"] == "awaiting", _e["status"])
    check("keyed on the approval id, so the later run updates this row",
          _e["id"] == _aid, f"{_e['id']} vs {_aid}")
    _ex.deny(_aid)
    _after = console.snapshot()
    check("denying turns that same row to denied",
          _after["count"] == 1 and _after["entries"][0]["status"] == "denied",
          f"{_after['count']}/{_after['entries'][0]['status']}")

print("\nRedaction — a secret must not reach the screen")

console.reset()
console.record(console.make_entry(
    command='curl -H "Authorization: Bearer abcdefghijkl" https://x'))
_e = console.snapshot()["entries"][0]
check("a secret in the command is masked", "[redacted]" in _e["command"], _e["command"])
check("and the badge says so", console.redacted_fields(_e) == ["command"],
      str(console.redacted_fields(_e)))

# The output is the bigger risk: a command dumps its environment and the values
# are printed rather than typed. Masking only the command would miss it.
console.reset()
console.record(console.make_entry(command="env"))
console.update(console.snapshot()["entries"][0]["id"], status=console.OK,
               stdout="API_KEY=sk-abcd\nsafe line here")
_e = console.snapshot()["entries"][0]
check("a secret PRINTED by a command is masked", "[redacted]" in _e["stdout"],
      repr(_e["stdout"]))
check("and ordinary output is untouched", "safe line here" in _e["stdout"],
      repr(_e["stdout"]))
check("and the badge names the field", "stdout" in console.redacted_fields(_e),
      str(console.redacted_fields(_e)))

console.reset()
_clean = console.make_entry(command="Get-Command whisper")
console.record(_clean)
_e = console.snapshot()["entries"][0]
check("a clean command is not falsely flagged",
      console.redacted_fields(_e) == [] and _e["command"] == "Get-Command whisper",
      f"{console.redacted_fields(_e)} / {_e['command']}")

print("\nOutput is capped, and says so")

console.reset()
console.record(console.make_entry(command="big"))
console.update(console.snapshot()["entries"][0]["id"], status=console.OK,
               stdout="x" * 50000)
_e = console.snapshot()["entries"][0]
check("an enormous output is trimmed", len(_e["stdout"]) <= console._STREAM_CAP,
      f"len={len(_e['stdout'])} cap={console._STREAM_CAP}")
check("and marked truncated", _e["truncated"] is True,
      "a silent cut reads as the whole output")

print("\nThe buffer is bounded")

console.reset()
for _i in range(console._MAX_ENTRIES + 20):
    console.record(console.make_entry(command=f"cmd-{_i}"))
_snap = console.snapshot()
check("it does not grow past its cap", _snap["count"] == console._MAX_ENTRIES,
      f"count={_snap['count']} cap={console._MAX_ENTRIES}")
check("the newest are the ones kept",
      _snap["entries"][-1]["command"] == f"cmd-{console._MAX_ENTRIES + 19}",
      _snap["entries"][-1]["command"])
check("the index does not leak the evicted ones",
      len(console._by_id) == console._MAX_ENTRIES, str(len(console._by_id)))

print("\nReplay — a reload must not read as 'nothing happened'")

console.reset()
for _i in range(3):
    console.record(console.make_entry(command=f"replay-{_i}",
                                      conversation="conv_a"))
console.record(console.make_entry(command="other-chat", conversation="conv_b"))
_snap = console.snapshot(conversation="conv_a")
check("a conversation can be replayed on its own", _snap["count"] == 3,
      f"count={_snap['count']}")
check("and another chat's commands are not mixed in",
      all("conv_b" != _e["conversation"] for _e in _snap["entries"]),
      "filter leaked")
check("ids are unique, so a replay cannot duplicate a row",
      len({_e["id"] for _e in console.snapshot()["entries"]})
      == console.snapshot()["count"],
      "duplicate ids would double the list on every reload")

print("\nSessions — typing into the console runs on the same panel")

from backend.actions.session import sessions as _sessions  # noqa: E402

console.reset()
# One event loop for the whole block. A session holds a subprocess bound to the
# loop that created it, and `asyncio.run` tears its loop down on return, so
# opening on one loop and sending on the next fails with a closed transport.
async def _session_checks():
    console.reset()
    opened = await _sessions.open("consolecheck")
    check("a session opens", opened.get("success") is True, str(opened)[:120])
    sent = await _sessions.send("consolecheck", "echo session-check-ok")
    check("a command sent to it succeeds", sent.get("success") is True,
          str(sent)[:120])
    snap = console.snapshot()
    check("the session command is recorded", snap["count"] >= 1,
          f"count={snap['count']}")
    if snap["count"]:
        entry = snap["entries"][-1]
        check("marked as a session", entry["kind"] == "session", entry["kind"])
        check("with its command", "session-check-ok" in entry["command"],
              entry["command"])
        check("and its output", "session-check-ok" in entry["stdout"],
              repr(entry["stdout"][:60]))
    await _sessions.close("consolecheck")


asyncio.run(_session_checks())

print("\nThe websocket surface exists")

_ws_src = open(os.path.join(ROOT, "backend", "ws_server.py"),
               encoding="utf-8", errors="replace").read()
for _method in ("console.list", "console.clear", "console.run", "console.input"):
    check(f"{_method} is registered", f'"{_method}"' in _ws_src,
          "the dashboard has no way to call it")
check("the console is wired to the socket at startup",
      "set_broadcast" in _ws_src, "entries would never reach the panel")
check("chat.command is what the panel listens for",
      "chat.command" in open(os.path.join(ROOT, "backend", "actions",
                                          "console_log.py"),
                             encoding="utf-8").read(),
      "the broadcast name the UI subscribes to must match")

# Leave the user's environment as it was found. The buffer is in-memory, but a
# stray session is not.
run(_sessions.close_all())
console.reset()

print()
if fails:
    print(f"{len(fails)} FAILED")
    for _f in fails:
        print("  -", _f)
    sys.exit(1)
print("All console checks passed.")
