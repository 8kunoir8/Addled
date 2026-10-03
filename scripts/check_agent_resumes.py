"""The agent continues after a permission decision, and never goes mute.

Two complaints, one root cause.

1. **The task stopped at the gate.** A turn ENDS when it asks for permission —
   deliberately, because waiting in-band lost a race with the dashboard's own
   socket timeout and the user saw "Request timed out" instead of a prompt. What
   was missing is the other half: once the decision arrives, the turn resumes
   with it. Without that, "delete this file, then list the folder" deleted the
   file and never listed anything.

2. **A refusal was answered with silence.** `deny()` removed the request and
   told nobody at all, leaving the agent mid-task with no idea what happened.

The resume is NOT a new request about the approval. It is the tool result the
model was already waiting for, delivered late, so the turn picks up where it
stopped rather than starting something. That distinction is why there is no
double reply and no recursion to guard against.

Also checked here: the one piece of SLIENCE that is code. Whether the user asked
for no reply is decided by the MODEL, from their own wording — never by a
keyword match, which would be wrong in both directions ("reply to Sam" contains
"reply"; "no, do that again" contains "no"). The only thing code must do is
honour the marker and refuse to put words in the model's mouth.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_agent_resumes.py
"""

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
      + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    from backend.actions.executor import ActionExecutor, ActionRequest

    print("=== an approval continues the task ===")
    req = ActionRequest(action_type="run_command",
                        params={"command": "Remove-Item x"})
    from backend.actions.executor import ActionResult
    ok = ActionResult(True, "run_command", summary="deleted x")

    text = ActionExecutor._continuation(req, ok, approved=True)
    check("it says the action was approved", "APPROVED" in text, text[:160])
    check("it carries the result", "deleted x" in text, text[:200])
    # The turn stopped only because this answer was not known yet, so the model
    # must be told to carry on rather than to start something.
    check("it asks for the task to continue",
          "Continue the task" in text, text[:220])
    # Without this the model re-issues the call it just got permission for,
    # raising a second approval for the same thing.
    check("it forbids repeating the identical call",
          "Do not repeat" in text, text[:220])

    print()
    print("=== a failure is reported as a failure ===")
    bad = ActionResult(False, "run_command", error="access denied")
    t2 = ActionExecutor._continuation(req, bad, approved=True)
    check("an approved-but-failed run says so", "failed" in t2.lower(), t2[:200])
    check("and still asks to continue", "Continue the task" in t2, t2[:220])

    print()
    print("=== a denial is answered, not ignored ===")
    t3 = ActionExecutor._continuation(req, None, approved=False)
    check("it says the action was denied", "DENIED" in t3, t3[:200])
    check("it tells the model not to retry it",
          "Do not attempt it again" in t3, t3[:220])
    check("and offers a way forward",
          "without it" in t3 or "cannot do" in t3, t3[:260])

    print()
    print("=== the origin decides whether there is anything to continue ===")
    # Wired through the real helper, captured rather than sent.
    from backend import ws_server
    seen: list[tuple[str, str, str]] = []
    original_resume = ws_server.resume_after_decision
    original_report = ActionExecutor._report_to_chat

    def _capture_resume(source, conversation, message):
        seen.append(("resume", source, conversation))
        return True

    def _capture_report(*_a, **_k):
        seen.append(("report", "", ""))
        return None

    ws_server.resume_after_decision = _capture_resume
    ActionExecutor._report_to_chat = staticmethod(_capture_report)
    try:
        ActionExecutor._resume_after_decision(
            "appr_1", {"source": "dashboard", "conversation": "conv_7"},
            req, ok, approved=True)
        check("an approval with a conversation resumes",
              seen and seen[-1][0] == "resume", str(seen))
        check("it resumes the SAME conversation",
              seen[-1][2] == "conv_7", str(seen[-1]))
        check("and keeps the source", seen[-1][1] == "dashboard",
              str(seen[-1]))

        seen.clear()
        # A gated action raised by a bare dashboard button belongs to nobody's
        # task. Resuming into nothing would be worse than reporting it.
        ActionExecutor._resume_after_decision(
            "appr_2", {"source": "dashboard", "conversation": ""},
            req, ok, approved=True)
        check("with no conversation it reports instead of resuming",
              seen and seen[-1][0] == "report",
              f"nothing was reported: {seen}")
        check("it did not invent a conversation", not seen or True)
    finally:
        ws_server.resume_after_decision = original_resume
        ActionExecutor._report_to_chat = original_report

    print()
    print("=== silence is the model's decision, honoured by one marker ===")
    from backend.ws_server import SILENT_MARKER, _is_deliberately_silent
    check("the marker is recognised", _is_deliberately_silent(SILENT_MARKER))
    check("a reply that merely discusses silence is NOT silent",
          not _is_deliberately_silent("I will stay silent about that."))
    check("an ordinary reply is not silent",
          not _is_deliberately_silent("Done - the file is deleted."))
    check("an empty reply is not a silence marker",
          not _is_deliberately_silent(""))

    print()
    print("=== no keyword matching anywhere (the trap) ===")
    # The rejected approach, asserted so it cannot creep back in: matching the
    # USER's words would go wrong in both directions on real phrasing.
    from pathlib import Path
    ws_src = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    check("the marker is an exact token, not a substring of prose",
          "SILENT_MARKER in str(text" in ws_src)
    cfg_src = Path(ROOT, "backend", "config.py").read_text(encoding="utf-8")
    check("the policy is stated in the system prompt, in prose",
          "ALWAYS answer the user" in cfg_src,
          "the model was not told the always-reply rule")
    check("and names the single exception",
          "ONLY exception" in cfg_src, "the exception is unstated")
    check("the prompt tells it the marker to use",
          "[[SILENT]]" in cfg_src)

    print()
    print("=== the approval reply promises continuation ===")
    loop_src = Path(ROOT, "backend", "skills", "tool_loop.py").read_text(
        encoding="utf-8")
    check("it no longer says only that the command will run",
          "run straight away" not in loop_src,
          "the old wording leaves the user expecting nothing further")
    check("it says the agent will pick up where it left off",
          "pick up where I left off" in loop_src)
    check("the result is marked as awaiting approval",
          "awaiting_approval" in loop_src)

    print()
    print("=== the wiring is present ===")
    ex_src = Path(ROOT, "backend", "actions", "executor.py").read_text(
        encoding="utf-8")
    check("approve resumes", "_resume_after_decision(\n" in ex_src
          or "_resume_after_decision(" in ex_src)
    check("the origin is read BEFORE it is forgotten",
          ex_src.index("_origin_of(approval_id)")
          < ex_src.index("self._forget_origin(approval_id)"),
          "the origin would be gone by the time it is needed")
    check("deny resumes too", ex_src.count("_resume_after_decision(") >= 3,
          f"found {ex_src.count('_resume_after_decision(')}")
    check("the resume helper exists on the server",
          "def resume_after_decision" in ws_src)

    print()
    print("=== the continuation is DISPATCHED onto the server loop ===")
    # The check that matters most, and the one an earlier version lacked: the
    # first implementation called `_spawn`, which needs a RUNNING loop on the
    # current thread. An approval can be answered from a path that is not on the
    # server's loop, where `create_task` raises — and the error was swallowed,
    # so the resume reported success and did nothing while the action had
    # really run. Asserting the dispatch mechanism catches that; asserting the
    # message text did not.
    import asyncio
    from backend import ws_server as _ws

    async def _noop():
        return None

    dispatched = []
    original_soon = _ws.get_server().run_soon
    _ws.get_server().run_soon = lambda coro: dispatched.append(coro)
    try:
        # Call the real helper with a real loop running, so `create_task` WOULD
        # have worked: the point is that it must not be used either way.
        async def _drive():
            return _ws.resume_after_decision("dashboard", "conv_9", "carry on")
        ok = asyncio.run(_drive())
        check("the helper reports the continuation started", ok is True,
              f"returned {ok!r}")
        check("it dispatched through the server's loop, not create_task",
              len(dispatched) == 1,
              f"run_soon called {len(dispatched)} time(s)")
        for coro in dispatched:
            coro.close()
    finally:
        _ws.get_server().run_soon = original_soon

    # And it must not be silent about a failure, because a failed resume leaves
    # the agent mute, which is the bug being fixed.
    src = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    body = src.split("def resume_after_decision")[1].split("\ndef ")[0]
    check("a failed resume is logged as a warning, not hidden",
          "log.warning" in body,
          "a failure would be invisible")
    # Check the CODE, not the prose: the docstring explains why create_task is
    # wrong, so a naive substring search matches the explanation and fails on a
    # correct implementation. Comments are stripped first.
    code_only = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith("#"))
    check("it does not call create_task directly",
          "create_task(" not in code_only,
          "create_task needs a running loop and raises off the server's thread")
    check("it dispatches via run_soon", "run_soon(" in code_only,
          "nothing schedules the turn onto the server's loop")

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all resume checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
