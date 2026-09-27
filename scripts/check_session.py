"""Interactive-session checks — the state a one-shot command cannot keep.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_session.py

Pins the three things that make a session worth having: the working directory
survives from one call to the next, an environment variable set in one call is
readable in the next, and reopening a name reuses the running shell instead of
spawning a second one.
"""

import asyncio
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.actions import session as sess_mod

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

async def run():
    m = sess_mod.sessions
    try:
        opened = await m.open("check-session")
        check("a session opens", opened.get("success"), str(opened))
        if not opened.get("success"):
            return

        reused = await m.open("check-session")
        check("reopening the same name reuses the running shell",
              reused.get("reused") is True, str(reused))

        # ---- statefulness: cwd ------------------------------------------
        first = await m.send("check-session", "Set-Location $env:TEMP")
        check("a command runs and reports an exit code",
              first.get("exit_code") == 0, str(first)[:200])

        where = await m.send("check-session", "Write-Output (Get-Location).Path")
        check("the working directory survives to the next command",
              "Temp" in (where.get("stdout") or ""),
              (where.get("stdout") or "")[:200])

        # ---- statefulness: environment ----------------------------------
        await m.send("check-session",
                     "$global:ADDLED_PROBE = 'kept'; Write-Output set")
        read_back = await m.send("check-session",
                                 "Write-Output $global:ADDLED_PROBE")
        check("a variable set in one call is readable in the next",
              "kept" in (read_back.get("stdout") or ""),
              (read_back.get("stdout") or "")[:200])

        # ---- multiple commands in one line ------------------------------
        combined = await m.send(
            "check-session",
            "Write-Output 'one'; Write-Output 'two'")
        out = combined.get("stdout") or ""
        check("one send can carry several statements",
              "one" in out and "two" in out, out[:200])

        # ---- a unique marker must not collide ---------------------------
        tricky = await m.send("check-session",
                              "Write-Output '__ADDLED_DONE_fake__'")
        check("output that looks like a marker does not end the read early",
              "fake" in (tricky.get("stdout") or ""),
              (tricky.get("stdout") or "")[:200])

        # ---- listing and closing ----------------------------------------
        names = [s["name"] for s in m.list_sessions()]
        check("the open session is listed", "check-session" in names, str(names))

        closed = await m.close("check-session")
        check("a session closes", closed.get("success"), str(closed))
        check("and is gone from the list",
              "check-session" not in [s["name"] for s in m.list_sessions()],
              str(m.list_sessions()))

        # ---- sending to a closed session is refused, not a crash --------
        after = await m.send("check-session", "Write-Output hi")
        check("sending to a closed session is refused with a reason",
              after.get("success") is False and bool(after.get("error")),
              str(after))

        # ---- the one-shot nudge -----------------------------------------
        check("a bare REPL is recognised as needing a session",
              sess_mod.looks_interactive("python"), "python should be flagged")
        check("a one-liner python is NOT flagged",
              not sess_mod.looks_interactive("python -c 'print(1)'"),
              "python -c needs no session")
        check("a bare cd is flagged",
              sess_mod.looks_interactive("cd src"), "cd should be flagged")
        check("a chained cd is not flagged",
              not sess_mod.looks_interactive("cd src; ls"),
              "a chained command completes on its own")
    finally:
        try:
            await m.close_all()
        except Exception:  # noqa: BLE001
            pass

def main() -> int:
    asyncio.run(run())
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: interactive sessions — cwd and env survive between calls")
    return 0

if __name__ == "__main__":
    sys.exit(main())
