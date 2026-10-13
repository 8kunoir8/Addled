"""Pipeline resilience — a finished task still produces an answer.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_pipeline_resilience.py

Four ways a completed task used to end with nothing shown to the user, each
pinned here by the property that fixes it rather than by the line that changed:

1. The WebSocket dropping mid-turn cancelled the pipeline. The tools had run,
   the reply was thrown away before it was written, and the user saw a task
   finish in silence. `chat.send` is shielded now, and history is written before
   the reply is sent, so a reconnect can recover it.
2. The dashboard gave up at 180s of total elapsed time, even while the server
   was still streaming progress, and discarded the reply that then arrived.
   Progress restarts that clock.
3. Eight tool rounds with a failed summary produced "I've completed the
   requested actions." — a sentence that is not true. It summarises the real
   results now.
4. A `run_command` that timed out left its process running and threw away the
   output it had produced.

Two of these are behaviour, not text, so they are exercised for real: the
shield is run against a cancelled task, and the timeout is run against a
process that outlives it.
"""

import asyncio
import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8",
              errors="replace") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. A disconnect must not cancel the turn.
# ---------------------------------------------------------------------------

async def run_shield_behaviour():
    """The real thing: cancel the outer task, the inner work must finish."""
    wrote_history = {"done": False}

    async def pipeline():
        await asyncio.sleep(0.05)
        wrote_history["done"] = True       # the last thing the real one does
        return {"response": "the answer"}

    async def serve():
        return await asyncio.shield(pipeline())

    task = asyncio.create_task(serve())
    await asyncio.sleep(0.01)
    task.cancel()
    cancelled = False
    try:
        await task
    except asyncio.CancelledError:
        cancelled = True
    for _ in range(40):
        if wrote_history["done"]:
            break
        await asyncio.sleep(0.01)

    check("a cancelled turn still finishes its own work",
          wrote_history["done"],
          "shield() is what keeps the tools' results from being discarded")
    check("the outer task is still cancellable", cancelled,
          "shielding must not make the connection task itself unstoppable")


def run_disconnect_source():
    src = _read("backend/ws_server.py")
    check("chat_send shields the pipeline",
          "asyncio.shield(run_chat_pipeline(" in src,
          "an unshielded await is cancelled by the disconnect handler")
    check("_serve handles cancellation rather than leaking it",
          "except asyncio.CancelledError:" in src,
          "CancelledError is a BaseException, so `except Exception` misses it")

    # The cancellation itself must stay: it is what stops an approval prompt
    # waiting forever on a socket nobody is reading. Only chat.send is spared.
    check("in-flight tasks are still cancelled on disconnect",
          "task.cancel()" in src,
          "the approval gate depends on this")


# ---------------------------------------------------------------------------
# 2. The dashboard clock follows progress.
# ---------------------------------------------------------------------------

def run_dashboard():
    src = _read("dashboard/src/lib/useWS.ts")
    check("progress notifications restart the request clock",
          "noteActivity" in src,
          "without it a slow turn is rejected while it is still working")
    for method in ("chat.delta", "chat.activity", "chat.tools"):
        check(f"'{method}' counts as progress", f"'{method}'" in src,
              "all three mean the turn is alive")
    check("the clock is only reset for chat requests",
          re.search(r"entry\.method\.startsWith\('chat\.'\)", src) is not None,
          "a quiet status poll is a genuine failure and must still time out")


# ---------------------------------------------------------------------------
# 3. The round-cap fallback reports what happened.
# ---------------------------------------------------------------------------

def run_fallback():
    from backend.skills.tool_loop import _summarize_tool_results

    placeholder = "I've completed the requested actions."
    src = _read("backend/skills/tool_loop.py")
    # The string may survive in prose that explains why it was removed. What
    # must not survive is a return of it, so strip comments and docstrings and
    # look for what is left.
    stripped = re.sub(r'"""[\s\S]*?"""', "", src)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    live = [ln.strip() for ln in stripped.splitlines()
            if placeholder in ln and not ln.lstrip().startswith("#")]
    check("the placeholder reply is no longer returned", not live,
          "it claimed success the model never reported; still at " + str(live[:1]))

    text = _summarize_tool_results([
        {"tool": "run_command", "success": True,
         "result": {"stdout": "build ok"}, "error": None},
        {"tool": "browser_extract", "success": False, "result": {},
         "error": "No text was extracted for selector body"},
    ])
    check("the fallback names the tools that ran",
          "run_command" in text and "browser_extract" in text, text[:120])
    check("the fallback keeps a successful tool's output",
          "build ok" in text, "the result of a real tool call was dropped")
    check("the fallback reports the failure reason",
          "No text was extracted" in text, text[:160])
    check("the fallback counts what happened", "2 tool call" in text, text[:80])
    check("the fallback is honest about not summarising",
          "could not produce a final summary" in text,
          "it must not imply the model answered")

    empty = _summarize_tool_results([])
    check("an empty tool list still says something useful",
          "try again" in empty.lower(), empty[:120])


# ---------------------------------------------------------------------------
# 4. A timed-out command is stopped, and keeps its output.
# ---------------------------------------------------------------------------

def run_terminal_source():
    src = _read("backend/actions/terminal.py")
    check("a timeout stops the process", "taskkill" in src,
          "proc.kill() alone leaves the shell's child running on Windows")
    check("the whole process tree is stopped", '"/T"' in src,
          "without /T only the shell dies and the pipe never closes")
    check("the output is read before the process is waited on",
          "ensure_future(proc.stdout.read())" in src,
          "a read started after the kill races the transport teardown")
    check("a timed-out command reports that it timed out",
          '"timed_out": True' in src, "the caller needs to tell this apart")


async def run_terminal_behaviour():
    from backend.actions import terminal

    ex = terminal.TerminalExecutor()
    ok = await ex.execute("echo pipeline-check-ok", timeout=30)
    check("a normal command still succeeds",
          ok.get("success") is True and "pipeline-check-ok" in (ok.get("stdout") or ""),
          str(ok)[:160])

    cmd = ('python -c "import sys,time; print(\'PARTIAL-MARK\'); '
           'sys.stdout.flush(); time.sleep(30)"')
    out = await ex.execute(cmd, timeout=3)
    check("a timed-out command reports failure", out.get("success") is False,
          str(out)[:120])
    check("a timed-out command is flagged as a timeout",
          out.get("timed_out") is True, str(out)[:120])
    check("the output it produced before the timeout is kept",
          "PARTIAL-MARK" in (out.get("stdout") or ""),
          "the partial log is the only evidence of how far it got")


if __name__ == "__main__":
    print("— the turn survives a disconnect")
    asyncio.run(run_shield_behaviour())
    run_disconnect_source()

    print("— the dashboard clock follows progress")
    run_dashboard()

    print("— the round-cap fallback tells the truth")
    run_fallback()

    print("— a timed-out command is stopped and reported")
    run_terminal_source()
    asyncio.run(run_terminal_behaviour())

    if fails:
        print(f"\n{len(fails)} FAILED")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("\nAll pipeline-resilience checks passed.")
