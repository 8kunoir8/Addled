"""A caller's internal turn must not be learned as a procedure.

The Code page's planner makes two model calls per request — search, then plan —
that the user never sees. `code.plan` already passed `record=False` so they would
not be written to chat history, memory or the journal. Learning was NOT covered:
it happens inside the tool loop, which had no way to see that flag, so every plan
taught Addled a recipe named after its own internal prompt.

Live evidence: a test plan whose instruction was "do something" produced

    learned | Request: do something Attached by the user: [Attached file...

in the store. Those entries then compete with the real built-ins when matching,
which is exactly the pollution that makes the matcher refuse tasks.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_code_learning.py
"""

import inspect
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    sys.path.insert(0, ROOT)
    from backend.skills import tool_loop
    from backend import ws_server

    loop_src = inspect.getsource(tool_loop.chat_with_tools)
    loop_file = inspect.getsource(tool_loop)

    # -- the loop has the switch ---------------------------------------------
    check("chat_with_tools accepts a `learn` flag",
          "learn: bool = True" in loop_src,
          "without it a caller cannot say 'do not learn this turn'")

    # -- and honours it at EVERY exit that learns -----------------------------
    # There are several `_learn_procedure` call sites (unreadable tool call,
    # normal reply, round cap). Missing one leaves a path that still learns,
    # which is the bug in miniature. Counted as call sites (not counting the
    # `def`), each of which must sit inside a `if learn:` block.
    lines = loop_file.splitlines()
    call_lines = [
        n for n, line in enumerate(lines, 1)
        if "_learn_procedure(" in line and "def _learn_procedure" not in line
    ]
    check("there are learning call sites to guard", len(call_lines) >= 3,
          f"found {len(call_lines)}")
    for n in call_lines:
        # The guard may be on the same line or the line(s) immediately above,
        # covering a call that wraps across lines.
        window = "\n".join(lines[max(0, n - 4):n])
        check(f"the call at line {n} is under a guard", "if learn:" in window,
              "an unguarded path learns from a turn the caller marked internal")

    # -- the pipeline passes it through from `record` -------------------------
    server_file = read("backend/ws_server.py")
    check("the pipeline derives `learn` from `record`",
          "learn=bool(record)" in server_file,
          "a turn the caller said not to record must not be learned either")

    # -- and the code planner still asks for a non-recording turn -------------
    plan_src = inspect.getsource(ws_server)
    marker = plan_src.find("async def code_plan")
    check("code_plan exists", marker != -1)
    plan_body = plan_src[marker:]
    plan_body = plan_body[:plan_body.find("\n    async def ", 10)]
    check("code_plan runs its pipeline with record=False",
          "record=False" in plan_body,
          "the planner's internal calls were the source of the pollution")
    # Both internal calls (FIND and PLAN) must be non-recording, not just one.
    check("BOTH planner calls are non-recording",
          plan_body.count("record=False") >= 2,
          f"found {plan_body.count('record=False')} of 2")

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: an internal turn is never learned as a procedure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
