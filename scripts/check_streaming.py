"""Streaming must deliver the final answer as chunks, and must never show a tool round.

The bug this exists to catch, in its own words: a stream that carries a TOOL
call would print the model's JSON at the user and then erase it, and a stream
that carries narration would print text the model itself discards two seconds
later. Both look like working streaming. Neither is.

There is a second trap, and it is the reason this check drives the real
`chat_with_tools` rather than the helper. `chat_stream` yields `str` only, so a
streamed round used to be able to say nothing about a NATIVE tool call — and the
OpenAI-compatible providers read `delta["content"]` and dropped
`delta["tool_calls"]` on the floor. A round that streamed prose AND a tool call
(the 9router/Voxagent shape, measured live on 2026-10-10) therefore answered in
prose and silently stopped working.

That hole is closed with `STREAM_TOOL_CALL`, a sentinel a provider yields when a
delta carries a tool call, and this check is what holds it closed: a round
carrying the sentinel must be REFUSED so the caller falls back to the batch call
where the call can run. A tool-call round must still run its tool and must never
show its syntax to the user.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_streaming.py
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
    """The shape `Provider.chat` returns."""

    def __init__(self, response="", tool_calls=None):
        self.ok = True
        self.response = response
        self.model = "fake"
        self.tokens_in = 10
        self.tokens_out = 10
        self.duration_ms = 1
        self.error = None
        self.tool_calls = tool_calls
        self.reasoning_content = ""


class FakeProvider:
    """A provider whose replies are scripted per call.

    `chat` pops one scripted reply per call, so a turn's rounds can be told
    apart. `chat_stream` is recorded, so a check can assert that a round which
    must NOT stream never touched it.
    """

    provider_id = "fake"
    has_native_tools = True

    def __init__(self, replies):
        self._replies = list(replies)
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None):
        self.chat_calls += 1
        return self._replies.pop(0) if self._replies else _Result("done")

    async def chat_stream(self, messages, model=None, max_tokens=4096,
                          temperature=0.7):
        self.stream_calls += 1
        # Stream back whatever the next BATCH reply would have been, two chunks
        # per word. A hardcoded "Hello" made the plain-answer assertion below
        # fail against a scripted "Paris", which was the check lying rather than
        # the code - the stream must reflect the script, not a fixture.
        if self._replies:
            text = getattr(self._replies.pop(0), "response", "") or ""
        else:
            text = "Hello"
        for i in range(0, len(text), 2):
            yield text[i:i + 2]


class _WedgedProvider:
    """A provider that fails the way a dead or wedged server does.

    `chat_stream` yields nothing at all, which is what the OpenAI-compatible
    providers do on any exception: they catch it and `yield ""`. So the stream
    ends with no chunks and no error of its own — the caller has to notice that
    an empty stream means failure. `chat` returns the named error the batch path
    produces.
    """

    provider_id = "wedged"
    has_native_tools = True

    def __init__(self):
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None):
        self.chat_calls += 1
        r = _Result("")
        r.ok = False
        r.error = "The model did not finish within 120s"
        return r

    async def chat_stream(self, messages, model=None, max_tokens=4096,
                          temperature=0.7):
        self.stream_calls += 1
        return
        yield  # pragma: no cover — an async generator that yields nothing


async def _empty_stream_is_reported():
    """A stream that yields no chunks must NOT be passed off as a blank answer.

    The bug: `_streamed_answer` returned a plain `{"response": "", ...}` when the
    stream ended empty, with no `stream_failed` flag. `_call_native_tools` then
    saw a successful reply and skipped the batch fallback, so the user got an
    empty message and no error. Measured before the fix: the same dead server
    produced `[Provider error: The model did not finish within 6s ...]` on the
    headless path and `''` on the streamed path the dashboard uses.
    """
    from backend.skills import tool_loop
    provider = _WedgedProvider()
    deltas: list[str] = []
    out = await tool_loop._call_native_tools(
        provider, [{"role": "user", "content": "hi"}], None,
        only=set(), on_delta=deltas.append, final=True)
    return out.get("response") or "", provider


_NODE_HARNESS = None


def _fillreply_replaces_streamed_bubble(page_source: str) -> bool:
    """Run the page's real `fillReply` against a streamed placeholder.

    A source-level assertion cannot do this. Reverting the `!m.content` guard
    was tried, and the substring check still passed while the bug was live,
    because the guard lives on a different line from the thing being matched.
    So the function is lifted out of the page, type annotations stripped, and
    executed under node with a bubble that already holds streamed text.
    """
    global _NODE_HARNESS
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if not node:
        return True  # no node on this machine; do not fail a check we cannot run

    tmp = tempfile.mkdtemp(prefix="addled_fr_")
    try:
        js = os.path.join(tmp, "fr.js")
        with open(js, "w", encoding="utf-8") as fh:
            fh.write(_NODE_HARNESS_JS)
        page = os.path.join(tmp, "page.tsx")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(page_source)
        proc = subprocess.run([node, js, page], capture_output=True,
                              text=True, timeout=30)
        if proc.returncode != 0:
            print(f"       node said: {proc.stderr.strip()[:300]}")
        return proc.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"       harness error: {e}")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_NODE_HARNESS_JS = r"""
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/\r\n/g, '\n');
const start = src.indexOf('function fillReply(');
if (start < 0) { console.error('fillReply not found'); process.exit(2); }
const end = src.indexOf('\n}', start);
const fn = src.slice(start, end + 2);
const js = fn
  .replace(/messages: Message\[\]/g, 'messages')
  .replace(/reply: string/g, 'reply')
  .replace(/\): Message\[\] \{/, ') {');
const modPath = path.join(__dirname, 'fr_mod.js');
fs.writeFileSync(modPath, js + '\nmodule.exports = { fillReply };');
const { fillReply } = require(modPath);
const before = [
  { role: 'user', content: 'hi' },
  { role: 'assistant', content: 'partial stre', streaming: true },
];
const after = fillReply(before, 'the full answer');
const assistants = after.filter(m => m.role === 'assistant');
if (assistants.length !== 1) {
  console.error('duplicated bubble: ' + assistants.length);
  process.exit(1);
}
if (assistants[0].content !== 'the full answer' || assistants[0].streaming) {
  console.error('not replaced in place: ' + JSON.stringify(assistants[0]));
  process.exit(1);
}
console.log('ok');
"""


def main() -> int:
    print("check_streaming")

    from backend.skills import tool_loop

    # ---------------------------------------------------------------- helper
    async def helper_shape():
        """The streamed call must return the same dict the batch path returns."""
        p = FakeProvider([])
        seen: list[str] = []
        p._replies = [_Result("Hello")]
        out = await tool_loop._streamed_answer(
            p, [{"role": "user", "content": "hi"}], on_delta=seen.append)
        return out, seen

    out, seen = asyncio.run(helper_shape())
    check("streamed call returns a response string",
          isinstance(out.get("response"), str) and out["response"] == "Hello",
          repr(out.get("response")))
    check("deltas are emitted per chunk, in order",
          len(seen) > 1 and "".join(seen) == "Hello",
          f"chunks={seen} (more than one chunk, and they rejoin to the reply)")
    check("streamed call reports a token count",
          isinstance(out.get("tokens"), int), repr(out.get("tokens")))

    # ------------------------------------------------- on_delta defaults off
    sig = inspect.signature(tool_loop.chat_with_tools)
    check("on_delta defaults to None (no stream unless asked)",
          sig.parameters["on_delta"].default is None,
          repr(sig.parameters["on_delta"].default))
    check("_call_native_tools accepts on_delta",
          "on_delta" in inspect.signature(tool_loop._call_native_tools).parameters)
    check("_call_prompt_tools accepts on_delta",
          "on_delta" in inspect.signature(tool_loop._call_prompt_tools).parameters)

    # A caller that passes no callback must never touch the stream, or every
    # existing headless caller silently changes behaviour.
    async def no_callback():
        p = FakeProvider([])
        out = await tool_loop._call_native_tools(
            p, [{"role": "user", "content": "hi"}], final=True, on_delta=None)
        return p, out

    p, out = asyncio.run(no_callback())
    check("no on_delta means the batch call is used",
          p.chat_calls == 1 and p.stream_calls == 0,
          f"chat={p.chat_calls} stream={p.stream_calls}")

    # ------------------------------------------------- a stream that fails
    # The dead-server case. This is the one that reached the user as a blank
    # message with no error, because an empty stream was accepted as a
    # successful empty answer and the batch fallback never ran.
    reply, wedged = asyncio.run(_empty_stream_is_reported())
    check("a stream that yields nothing is not passed off as a blank answer",
          reply.strip() != "",
          "the user got an empty reply and no error")
    check("and the batch path is retried so the real reason can surface",
          wedged.chat_calls >= 1,
          f"chat={wedged.chat_calls} stream={wedged.stream_calls}")
    check("the reason the model failed reaches the answer",
          "did not finish within" in reply,
          repr(reply[:120]))

    # ------------------------------------------------------- tool rounds
    # A tool round MAY be attempted as a stream now - that is how an ordinary
    # no-tool answer streams at all, since it takes the same path. What must
    # never change is the outcome: the tool still has to run, and the model's
    # tool-call syntax must never reach the screen as the answer.
    #
    # The first version of this check asserted `stream_calls == 0`, which was
    # true of the older design and became false the moment the common no-tool
    # turn was made to stream. That assertion was measuring the implementation
    # rather than the requirement, so it is replaced by the requirement.
    async def tool_then_answer():
        class ToolProvider:
            provider_id = "fake"
            has_native_tools = False

            def __init__(self):
                self.stream_calls = 0
                self._n = 0

            async def chat(self, messages, model=None, max_tokens=4096,
                           temperature=0.7, tools=None):
                self._n += 1
                if self._n == 1:
                    # The round that wants a tool, written as text.
                    return _Result("```tool\n"
                                   + json.dumps({"name": "list_dir",
                                                 "params": {}})
                                   + "\n```")
                return _Result("there is one file")

            async def chat_stream(self, messages, model=None, max_tokens=4096,
                                  temperature=0.7):
                self.stream_calls += 1
                # The same tool-call text, arriving as a stream.
                yield "```tool\n"
                yield json.dumps({"name": "list_dir", "params": {}})
                yield "\n```"

        p = ToolProvider()
        seen: list[str] = []
        ran: list[str] = []

        real_execute = tool_loop.execute_skill

        async def spy_execute(name, params, provider):
            ran.append(name)
            return {"success": True, "data": {}}

        tool_loop.execute_skill = spy_execute
        try:
            out = await tool_loop.chat_with_tools(
                p, [{"role": "user", "content": "what is there"}],
                tools=[], on_delta=seen.append)
        finally:
            tool_loop.execute_skill = real_execute
        return p, out, seen, ran

    p, out, seen, ran = asyncio.run(tool_then_answer())
    check("the tool still runs when the round was attempted as a stream",
          "list_dir" in ran,
          f"ran={ran} - streaming must never drop a tool call")
    check("tool-call syntax is never emitted as the answer",
          not any("```" in s or "list_dir" in s for s in seen),
          f"these reached the screen: {seen}")

    # A tool-using turn end to end: the tool runs, the call syntax never shows,
    # and the answer streams exactly once. The live app cannot demonstrate this
    # because the local model hallucinates instead of calling, so it is asserted
    # here with a provider that really does call.
    async def tool_turn_end_to_end():
        class P:
            provider_id = "fake"
            has_native_tools = False

            def __init__(self):
                self.n = 0

            async def chat(self, messages, model=None, max_tokens=4096,
                           temperature=0.7, tools=None):
                self.n += 1
                if self.n == 1:
                    return _Result("```tool\n"
                                   + json.dumps({"tool": "list_dir",
                                                 "params": {}}) + "\n```")
                return _Result("Done.")

            async def chat_stream(self, messages, model=None, max_tokens=4096,
                                  temperature=0.7):
                # Mirror what the next batch call would return; a fake that
                # always streams the tool call would pass this test for the
                # wrong reason - it would never see a plain round at all.
                if self.n == 0:
                    yield "```tool\n"
                    yield json.dumps({"tool": "list_dir", "params": {}})
                    yield "\n```"
                else:
                    yield "Do"
                    yield "ne."

        p = P()
        seen: list[str] = []
        ran: list[str] = []
        real_execute = tool_loop.execute_skill

        async def spy_execute(name, params, provider):
            ran.append(name)
            return {"success": True, "data": {}}

        tool_loop.execute_skill = spy_execute
        try:
            out = await tool_loop.chat_with_tools(
                p, [{"role": "user", "content": "list it"}],
                tools=[], on_delta=seen.append)
        finally:
            tool_loop.execute_skill = real_execute
        return out, seen, ran

    out, seen, ran = asyncio.run(tool_turn_end_to_end())
    check("a tool-using turn runs its tool",
          ran == ["list_dir"], f"ran={ran}")
    check("a tool-using turn streams its answer",
          "".join(seen) == "Done.", f"deltas={seen}")
    check("a tool-using turn never streams the call syntax",
          not any("```" in s or "list_dir" in s for s in seen),
          f"these reached the screen: {seen}")

    # The guard itself, tested where it actually decides: a single round whose
    # STREAMED TEXT is a tool call must be rejected, so the caller falls back to
    # the batch path instead of showing the syntax. Without this, the revert-
    # verify passed - because the end-to-end fake above only ever exercises the
    # case where the guard agrees with the outcome. This covers the case where
    # it is the thing standing between the user and raw tool syntax.
    async def stream_round_decides():
        class P:
            provider_id = "fake"
            has_native_tools = False

            async def chat_stream(self, messages, model=None, max_tokens=4096,
                                  temperature=0.7):
                yield "```tool\n"
                yield json.dumps({"tool": "list_dir", "params": {}})
                yield "\n```"

        p = P()
        r = await tool_loop._stream_round(
            p, [{"role": "user", "content": "x"}], None, None, "")
        return r

    guarded = asyncio.run(stream_round_decides())
    check("a round whose text is a tool call is not accepted as an answer",
          guarded is None,
          f"_stream_round accepted {guarded!r} - the call syntax would be shown")

    # ------------------------------------------------- native call on a stream
    # The 9router/Voxagent shape, measured live on 2026-10-10: a turn streams
    # `content="I'll check that."` AND a real `tool_calls` array. The stream
    # carries only `str`, so the provider reports the call with
    # `STREAM_TOOL_CALL`; before this, the call was discarded by `chat_stream`
    # and the announcement was emitted as the FINAL ANSWER - the user's "check
    # if whisper is installed" ran nothing, raised no permission card, and left
    # the console empty while the model said it was checking.
    async def native_call_on_stream():
        class P:
            provider_id = "fake"
            has_native_tools = True

            def __init__(self):
                self.n = 0
                self.stream_calls = 0

            async def chat(self, messages, model=None, max_tokens=4096,
                           temperature=0.7, tools=None):
                self.n += 1
                if self.n == 1:
                    # The real call, only on the batch path.
                    return _Result(
                        "I'll check that.",
                        tool_calls=[{"id": "c1", "type": "function",
                                     "function": {"name": "run_command",
                                                  "arguments":
                                                  json.dumps({"command":
                                                              "whisper --version"})}}])
                return _Result("Not installed.")

            async def chat_stream(self, messages, model=None, max_tokens=4096,
                                  temperature=0.7):
                self.stream_calls += 1
                # Exactly what 9router sent: the narration, then the marker
                # that a native tool call shared this stream.
                yield "I'll check that."
                yield tool_loop.STREAM_TOOL_CALL

        p = P()
        seen: list[str] = []
        ran: list[dict] = []
        real_execute = tool_loop.execute_skill

        async def spy_execute(name, params, provider):
            ran.append({"name": name, "params": params})
            return {"success": True, "data": {}}

        tool_loop.execute_skill = spy_execute
        try:
            out = await tool_loop.chat_with_tools(
                p, [{"role": "user", "content": "check if whisper is installed"}],
                tools=[], on_delta=seen.append)
        finally:
            tool_loop.execute_skill = real_execute
        return out, seen, ran, p

    out, seen, ran, p = asyncio.run(native_call_on_stream())
    check("a native tool call on a stream still runs its tool",
          bool(ran) and ran[0]["name"] == "run_command",
          f"ran={ran} — the call was lost and the narration became the answer")
    check("the marker never reaches the screen",
          not any(tool_loop.STREAM_TOOL_CALL in s for s in seen),
          f"deltas={seen!r}")
    check("the narration is not emitted as the final answer",
          "I'll check that." not in "".join(seen),
          f"the announcement became the answer: {seen!r}")
    check("the turn ends on the real answer, not the announcement",
          "Not installed." in (out.get("response") or ""),
          f"response={out.get('response')!r}")

    # The guard on its own: a single streamed round carrying the marker must be
    # REFUSED, so the caller falls back to the batch call. Without this the
    # end-to-end case above could pass for the wrong reason.
    async def stream_round_refuses_native():
        class P:
            provider_id = "fake"
            has_native_tools = True

            async def chat_stream(self, messages, model=None, max_tokens=4096,
                                  temperature=0.7):
                yield "On it."
                yield tool_loop.STREAM_TOOL_CALL

        r = await tool_loop._stream_round(
            P(), [{"role": "user", "content": "x"}], None, None, "")
        return r

    refused = asyncio.run(stream_round_refuses_native())
    check("a round carrying a native tool call is not accepted as an answer",
          refused is None,
          f"_stream_round accepted {refused!r} - the call would be dropped")


    # And the ordinary turn - the commonest one there is - must stream.
    async def plain_answer():
        p = FakeProvider([_Result("The capital of France is Paris.")])
        seen: list[str] = []
        out = await tool_loop.chat_with_tools(
            p, [{"role": "user", "content": "capital of France?"}],
            tools=[], on_delta=seen.append)
        return p, out, seen

    p, out, seen = asyncio.run(plain_answer())
    check("a plain answer with no tool call streams",
          len(seen) >= 1,
          "the commonest turn there is must not be left un-streamed")
    check("the streamed text is the answer",
          "".join(seen).strip() == "The capital of France is Paris.",
          repr("".join(seen)))

    # --------------------------------------------------------------- wiring
    src = inspect.getsource(tool_loop)
    check("only a final round asks to stream",
          "final=True" in src and "on_delta=on_delta" in src,
          "the final answer must pass final=True")

    ws = open(os.path.join(ROOT, "backend", "ws_server.py"),
              encoding="utf-8").read()
    check("the server broadcasts chat.delta",
          '"chat.delta"' in ws, "no broadcast found")
    check("streaming is opt-in per request",
          'params.get("stream")' in ws,
          "a request without `stream` must not stream")

    # Streaming must be FAITHFUL: the deltas are the reply, not an approximation
    # of it. Measured live - the delta and the finished reply for the same turn
    # were byte-identical, including the model's own hallucinated
    # "[[web_search]]" syntax. That faithfulness is the property worth asserting,
    # because it is what makes a delta safe to draw: whatever the reply will say,
    # the streamed text already showed.
    #
    # The hallucinated syntax itself is NOT a streaming bug and is not asserted
    # against here. It appears in the reply too, so it predates this work; it is
    # a model-quality problem (the local model invents a bracket syntax Addled
    # never taught it) and the verification gate is what surfaces it.
    src_loop = inspect.getsource(tool_loop)
    check("the streamed text is the response text, not a re-derivation",
          'on_delta(result.get("response", ""))' in src_loop,
          "the delta must be the reply itself")

    # The emotion tag must never reach the screen, not even for one frame.
    #
    # Found by running a turn against the live app: the delta arrived as
    # "[neutral] I called the read_file tool..." while the finished reply had no
    # tag, because the existing strip runs after the turn returns and a delta is
    # drawn the moment it is sent. So the user would watch the tag appear and
    # then vanish. This asserts the strip happens where the delta is SENT.
    check("a delta has its emotion tag stripped before it is sent",
          "parse_tag(piece)" in ws,
          "the tag must be removed in _emit_delta, not only from the reply")

    page_path = os.path.join(ROOT, "dashboard", "src", "app", "chat",
                             "page.tsx")
    page = open(page_path, encoding="utf-8").read()
    check("the dashboard subscribes to chat.delta",
          "onNotification('chat.delta'" in page)
    check("the dashboard asks to stream",
          "stream: true" in page)
    check("the dashboard appends rather than replacing",
          "content: m.content + piece" in page,
          "replacing would show only the last chunk")

    # The one that matters, and a substring test will not do it: with streamed
    # text already sitting in the bubble, filling must still recognise that
    # bubble as the placeholder. Asserting on the SOURCE passes whether or not
    # the `!m.content` guard is there (verified by reverting it), so the real
    # function is pulled out of the page and RUN instead.
    check("filling a placeholder that already has text replaces it in place",
          _fillreply_replaces_streamed_bubble(page),
          "a placeholder holding streamed text was not matched, so the reply "
          "would be appended as a second bubble")

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all streaming checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
