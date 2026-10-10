"""A turn that changed files must show its work before it claims it worked.

The defect: a turn writes a file, runs nothing, and answers "done". Nothing in
Addled objected. The model's own confident sentence was the only account of
whether the change worked, and the user found out later.

Three things have to be true, and the second and third are what keep this from
becoming a nuisance rather than a safeguard:

  IT FIRES when files changed and nothing was run.

  IT DOES NOT FIRE on a conversational turn. There is nothing to verify, and the
  cost of asking would be paid on every question the user asks.

  IT IS BOUNDED. One nudge, not a loop. A model that will not verify must still
  be allowed to finish, or a turn hangs.

The revert-verification this was built against: making the gate unconditional
(removing the file check) makes the conversational assertion fail, and removing
the bound makes the loop assertion fail.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_verify_gate.py
"""

import asyncio
import inspect
import json
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")
        fails.append(label)


class _Result:
    def __init__(self, response="", tool_calls=None):
        self.ok = True
        self.response = response
        self.model = "fake"
        self.tokens_in = 1
        self.tokens_out = 1
        self.duration_ms = 1
        self.error = None
        self.tool_calls = tool_calls
        self.reasoning_content = ""


class FakeProvider:
    """Scripted replies; records every prompt it was given."""

    provider_id = "fake"
    has_native_tools = False

    def __init__(self, replies):
        self._replies = list(replies)
        self.seen: list[list[dict]] = []

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None):
        self.seen.append([dict(m) for m in messages])
        return self._replies.pop(0) if self._replies else _Result("done")


def _tool_block(name, params):
    return ("```tool\n"
            + json.dumps({"name": name, "params": params})
            + "\n```")


def main() -> int:
    print("check_verify_gate")

    try:
        from backend.skills import tool_loop
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the tool loop: {e}")
        print()
        print("FAILED (1): could not import the tool loop")
        return 1

    # --------------------------------------------------- what counts as a write
    check("a path argument counts as a file change",
          tool_loop._turn_changed_files(
              [{"tool": "write_file", "success": True,
                "result": {"path": "/tmp/x.txt"}}]) is True)
    check("a failed write does not count",
          tool_loop._turn_changed_files(
              [{"tool": "write_file", "success": False,
                "result": {"path": "/tmp/x.txt"}}]) is False,
          "a call that errored changed nothing")
    check("reading a file does not count as a change",
          tool_loop._turn_changed_files(
              [{"tool": "read_file", "success": True,
                "result": {"content": "hello"}}]) is False,
          "only an argument naming a destination is a write")
    # The reason this keys on the argument and not the tool name: a user's own
    # market or forged skill writes files under a name this file has never seen.
    check("an unknown tool that names a path still counts",
          tool_loop._turn_changed_files(
              [{"tool": "my_own_exporter", "success": True,
                "result": {"dest": "/tmp/out.csv"}}]) is True,
          "a tool-name list would miss every skill the user added")

    # ------------------------------------------------------- what counts as evidence
    check("running the terminal counts as verification",
          tool_loop._turn_verified(
              [{"tool": "terminal_run", "success": True, "result": {}}]) is True)
    check("a listing alone is not verification",
          tool_loop._turn_verified(
              [{"tool": "some_tool", "success": True,
                "result": {"command": "ls -la", "stdout": "a b c"}}]) is False,
          "a directory listing shows nothing about the change")

    # The write tool used below takes a `path` and needs no approval. That
    # matters: `write_file` requires one, and a turn waiting on approval ends
    # before the gate is reached, so a check built on it would assert on a path
    # the gate never sees. Verified by running it - the turn came back with
    # "needs your approval" and one model call.
    #
    # ------------------------------------------------------- end to end firing
    async def changed_and_silent():
        p = FakeProvider([
            _Result(_tool_block("create_dir", {"path": "/tmp/a_dir"})),
            _Result("I wrote the file, all good."),   # no evidence
            _Result("I wrote the file. It is unverified."),
        ])
        out = await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "write a file"}], tools=[])
        return p, out

    p, out = asyncio.run(changed_and_silent())
    nudged = any("ran nothing that shows" in (m.get("content") or "")
                 for msgs in p.seen for m in msgs)
    check("a file-changing turn with no evidence is nudged once", nudged,
          "the model claimed success without running anything")
    check("the turn still finishes", isinstance(out.get("response"), str))

    # ------------------------------------------------- bounded, not a loop
    async def never_verifies():
        replies = [_Result(_tool_block("create_dir", {"path": "/tmp/b_dir"}))]
        # It answers without evidence forever. The gate must give up.
        replies += [_Result("done, trust me")] * 12
        p = FakeProvider(replies)
        out = await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "write a file"}], tools=[])
        return p, out

    p, out = asyncio.run(never_verifies())
    nudge_count = sum(
        1 for msgs in p.seen for m in msgs
        if "ran nothing that shows" in (m.get("content") or ""))
    check("the nudge is bounded — it does not loop",
          nudge_count <= tool_loop.MAX_VERIFY_NUDGES,
          f"nudged {nudge_count} times")
    check("the turn terminates even without verification",
          isinstance(out.get("response"), str), repr(out.get("response"))[:60])

    # ------------------------------------------------- conversational turns
    async def conversational():
        p = FakeProvider([_Result("Paris is the capital of France.")])
        out = await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "capital of France?"}], tools=[])
        return p, out

    p, out = asyncio.run(conversational())
    nagged = any("ran nothing that shows" in (m.get("content") or "")
                 for msgs in p.seen for m in msgs)
    check("a conversational turn is never nudged", not nagged,
          "there is nothing to verify, so asking would be pure cost")
    check("a conversational turn takes one model call",
          len(p.seen) == 1, f"{len(p.seen)} calls")

    # --------------------------------------------------- verification present
    async def writes_then_runs():
        p = FakeProvider([
            _Result(_tool_block("create_dir", {"path": "/tmp/c_dir"})),
            _Result(_tool_block("terminal_run", {"command": "python /tmp/c.txt"})),
            _Result("Wrote it and ran it; output was clean."),
        ])
        out = await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "write and run"}], tools=[])
        return p, out

    p, out = asyncio.run(writes_then_runs())
    nudged = any("ran nothing that shows" in (m.get("content") or "")
                 for msgs in p.seen for m in msgs)
    check("a turn that wrote and then ran is not nudged", not nudged,
          "it already showed its work")

    # ------------------------------------------------------------- the bound
    check("the nudge cap is small",
          0 < tool_loop.MAX_VERIFY_NUDGES <= 3,
          f"MAX_VERIFY_NUDGES={tool_loop.MAX_VERIFY_NUDGES}")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all verification-gate checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
