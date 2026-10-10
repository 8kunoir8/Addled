"""A tool round must say what it is doing, and must never say something untrue.

The defect this exists to catch is silence. A tool round is the long part of a
turn - a web search, a model load, a screenshot - and until now the loop emitted
nothing at all during it. The surface showed a spinner for twenty seconds with
no way to tell "working" from "hung", which is what makes a capable assistant
feel dead.

The trap is the other direction: it is easy to fill that silence with prose the
model generated, and that prose can be wrong about what is happening. These
notices are derived from the tool name the loop already has, so the assert below
that matters most is the one that says each notice is built from the tool, with
no provider call anywhere in the path.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_wait_notice.py
"""

import asyncio
import inspect
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


def main() -> int:
    print("check_wait_notice")

    # Imported inside a guard on purpose. A syntax error or a bad import used to
    # let this file die on an uncaught traceback, and `check_all` read the
    # process as having run - so a check that could not run at all looked like
    # one that had passed. Failing here, loudly, is the only safe behaviour.
    try:
        from backend.skills import tool_loop
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the tool loop: {e}")
        print()
        print("FAILED (1): could not import the tool loop")
        return 1

    # ------------------------------------------------------------- the helper
    check("web_search describes a search",
          "Searching" in tool_loop._activity_for("web_search"),
          tool_loop._activity_for("web_search"))
    check("read_file describes a read",
          "Reading" in tool_loop._activity_for("read_file"),
          tool_loop._activity_for("read_file"))
    check("write_file describes a write",
          "Writing" in tool_loop._activity_for("write_file"),
          tool_loop._activity_for("write_file"))
    check("terminal_run describes a run",
          "Running" in tool_loop._activity_for("terminal_run"),
          tool_loop._activity_for("terminal_run"))

    # The fallback must be TRUE, not empty. A user's own market or forged skill
    # has a name this table has never seen, and a blank status is worse than a
    # raw one.
    for unknown in ("my_custom_thing", "frobnicate", "zzz_9"):
        out = tool_loop._activity_for(unknown)
        check(f"an unknown tool is still named ({unknown})",
              bool(out) and out.strip() != "",
              repr(out))

    check("an empty name still yields something",
          bool(tool_loop._activity_for("").strip()),
          repr(tool_loop._activity_for("")))

    # A synthetic name must not leak junk into the status line.
    out = tool_loop._activity_for("search_q2_tmp_9182")
    check("digits and fragments do not leak into the status",
          "9182" not in out and "q2" not in out,
          repr(out))

    check("the helper never raises",
          all(isinstance(tool_loop._activity_for(x), str)
              for x in (None, "", " ", "a", "X" * 200)))

    # --------------------------------------------------------------- wiring
    sig = inspect.signature(tool_loop.chat_with_tools)
    check("on_activity defaults to None",
          sig.parameters["on_activity"].default is None,
          repr(sig.parameters["on_activity"].default))

    src = inspect.getsource(tool_loop)
    check("the notice fires before the tool runs",
          src.index("on_activity(_activity_for") <
          src.index("exec_result = await execute_skill("),
          "announcing after execution would show the wrong thing during the wait")

    # No model call may be anywhere near it. This is the assertion that keeps
    # the notices honest: the moment someone generates this text, it can lie.
    i = src.index("def _activity_for")
    body = src[i:i + 2000]
    for forbidden in ("provider", "await ", "chat(", "complete("):
        check(f"_activity_for does not call {forbidden.strip()}",
              forbidden not in body,
              "the status line must be derived, never generated")

    ws = open(os.path.join(ROOT, "backend", "ws_server.py"),
              encoding="utf-8").read()
    check("the server broadcasts chat.activity",
          '"chat.activity"' in ws)
    check("activity is behind the same opt-in as streaming",
          '_on_activity = _emit_activity if params.get("stream") else None' in ws,
          "a headless caller must get no broadcasts")

    # ------------------------------------------- end to end, through a turn
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
        provider_id = "fake"
        has_native_tools = False

        def __init__(self, replies):
            self._replies = list(replies)

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            return self._replies.pop(0) if self._replies else _Result("done")

    async def one_tool_turn():
        import json as _json
        call_text = ("```tool\n"
                     + _json.dumps({"name": "list_files", "params": {}})
                     + "\n```")
        p = FakeProvider([_Result(call_text), _Result("there is one file")])
        seen: list[str] = []
        await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "what is there"}],
            tools=[], on_activity=seen.append)
        return seen

    seen = asyncio.run(one_tool_turn())
    check("a tool round emitted at least one notice", len(seen) >= 1,
          f"notices={seen}")
    check("the notice is non-empty text",
          all(isinstance(s, str) and s.strip() for s in seen), repr(seen))

    # And a caller that passes nothing must stay silent, with no callback
    # machinery running at all.
    async def headless():
        p = FakeProvider([_Result("just text")])
        return await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "hi"}], tools=[])

    out = asyncio.run(headless())
    check("a headless turn still answers normally",
          isinstance(out.get("response"), str), repr(out.get("response")))

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all wait-notice checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
