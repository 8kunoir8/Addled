"""A tool call with no prose is a valid answer, not an empty response.

The bug this exists to catch, measured on the live gateway:

    RAW: finish='tool_calls' content=None tools=2

`deepseek-v4.1-flash`, reached through `9router`, routinely returns tool calls
with `content: null` and `finish_reason: "tool_calls"`. That is valid under the
OpenAI schema — `content` is nullable and a tool call is carried in its own
field — and it is what a model does when it has nothing to say before acting.

`openai_provider.chat` tested `if not content.strip()` BEFORE looking at
`message["tool_calls"]`, so it discarded the tool calls and returned
`ok=False, error="The model returned an empty response..."`. In a live turn the
caller then got a provider error instead of an answer: measured over eight
runs of the same question, 2 failed at round 2 having already called
`meeting_list` once. The turn died mid-sequence, losing the tool it had just
asked for — and the error text blamed the model for not being loaded.

This is not a provider quirk to tolerate. Discarding a parsed, valid tool call
is a defect in the parser, and the failure is invisible: three of four runs
succeed, so it reads as flakiness.

Tested by calling `chat()` against a stub transport that returns the exact
shapes the gateway was observed to send. No network, no model, no fixture that
can pass by accident — the assertion is on what the provider RETURNS.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_tool_calls.py
"""

import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


# ---------------------------------------------------------------------------
# A transport that returns a canned body, so the parsing is what is under test.
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, text=None):
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.status_code = 200

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):  # noqa: A002 - mirrors httpx
        self.last_payload = json
        return _FakeResponse(self._payload)


def _body(message: dict, finish: str = "tool_calls") -> dict:
    return {
        "model": "deepseek-v4.1-flash",
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _provider(payload):
    from backend.providers.openai_provider import OpenAIProvider
    p = OpenAIProvider({"provider_id": "stub", "base_url": "http://stub",
                        "api_key": "x", "default_model": "stub-model"})
    p._get_client = lambda: _FakeClient(payload)
    return p


def _ask(payload, ):
    return asyncio.run(_provider(payload).chat(
        [{"role": "user", "content": "list my meetings"}],
        tools=[{"type": "function",
                "function": {"name": "meeting_list",
                             "description": "List meetings",
                             "parameters": {"type": "object", "properties": {}}}}],
    ))


TOOL_CALL = {"id": "call_abc", "type": "function",
             "function": {"name": "meeting_list", "arguments": "{}"}}

print("A tool call with NO prose is a valid answer")

# The exact shape the live gateway sends, captured from the raw response log.
res = _ask(_body({"role": "assistant", "content": None,
                  "tool_calls": [TOOL_CALL]}, finish="tool_calls"))

check("content=None with tool_calls is not an error", res.ok,
      f"ok=False, error={getattr(res, 'error', '')!r} — the tool call was "
      f"discarded because no prose accompanied it")
check("the tool call survives parsing",
      bool(getattr(res, "tool_calls", None)),
      "tool_calls came back empty, so the call the model made was dropped")
check("the tool call is the one the model made",
      bool(getattr(res, "tool_calls", None))
      and ((res.tool_calls[0].get("function") or {}).get("name")
           == "meeting_list"),
      f"got {getattr(res, 'tool_calls', None)!r}")

# An empty string means the same thing as None here; both are "no prose".
res2 = _ask(_body({"role": "assistant", "content": "",
                   "tool_calls": [TOOL_CALL]}, finish="tool_calls"))
check("content='' with tool_calls is also not an error", res2.ok,
      f"ok=False, error={getattr(res2, 'error', '')!r}")

print()
print("A genuinely empty reply is STILL an error")

# The check above must not be fixed by removing the empty-response guard
# entirely. A response with no prose AND no tool call is nothing, and saying so
# is the whole point of that branch.
res3 = _ask(_body({"role": "assistant", "content": None}, finish="stop"))
check("no content and no tool calls is still an error", not res3.ok,
      "an entirely empty reply was accepted as success")
check("and the error still names the cause",
      "empty" in str(getattr(res3, "error", "")).lower(),
      f"error was {getattr(res3, 'error', '')!r}")

# Prose with no tool call is a normal answer and must stay ok.
res4 = _ask(_body({"role": "assistant", "content": "You have no meetings."},
                  finish="stop"))
check("prose with no tool call is still a normal answer", res4.ok,
      f"ok=False, error={getattr(res4, 'error', '')!r}")

# Reasoning-only (a thinking model that spent its budget) must still be
# reported, and must NOT be silently converted into an answer.
res5 = _ask(_body({"role": "assistant", "content": "",
                   "reasoning_content": "The user wants me to..."},
                  finish="stop"))
check("reasoning with no answer is still reported as a failure", not res5.ok,
      "a reasoning-only reply was accepted as an answer")

print()
print("A tool call on a STREAM is reported, not discarded")

# The streaming sibling of the bug above, measured live on 2026-10-10 against
# 9router/Voxagent: the turn streamed `content="I'll check that."` AND a real
# `tool_calls` array. `chat_stream` reads only `delta["content"]`, so the call
# was dropped, the announcement was emitted as the FINAL ANSWER, and the user's
# "check if whisper is installed" ran nothing — no permission card, nothing on
# the console. A `str` iterator has no field for a call, so the provider reports
# one with a sentinel; this asserts it does.

def _stream_lines(*deltas):
    import json as _json
    return [_json.dumps({"choices": [{"delta": d, "finish_reason": None}]})
            for d in deltas] + ["[DONE]"]


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield "data: " + line


class _FakeStreamClient:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *a, **kw):
        client = self

        class _Ctx:
            async def __aenter__(self):
                return _FakeStreamResponse(client._lines)

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


async def _collect_stream(lines):
    from backend.providers.openai_provider import OpenAIProvider
    from backend.skills.tool_loop import STREAM_TOOL_CALL
    p = OpenAIProvider({"provider_id": "stub", "base_url": "http://stub",
                        "api_key": "x", "default_model": "stub-model"})
    p._get_client = lambda: _FakeStreamClient(lines)
    out = []
    async for piece in p.chat_stream([{"role": "user", "content": "check"}],
                                     model="stub-model"):
        out.append(piece)
    return out, STREAM_TOOL_CALL


_pieces, _sentinel = asyncio.run(_collect_stream(_stream_lines(
    {"content": "I'll check that.", "tool_calls": None},
    {"content": "", "tool_calls": [{"index": 0, "id": "call_x",
                                    "type": "function",
                                    "function": {"name": "run_command",
                                                 "arguments": "{\"command\""}}]},
)))
check("the prose still streams", "I'll check that." in "".join(_pieces),
      f"pieces={_pieces!r}")
check("a native tool call on the stream is signalled, not dropped",
      _sentinel in _pieces,
      "the provider read only delta['content'] and threw the call away - the "
      "announcement would be emitted as the answer with nothing run")

_plain, _sentinel2 = asyncio.run(_collect_stream(_stream_lines(
    {"content": "Just an answer.", "tool_calls": None},
)))
check("a plain stream carries no false tool-call marker",
      _sentinel2 not in _plain,
      "the marker appeared on a text-only stream, which would make every "
      "ordinary answer fall back to the batch path")

print()
print("The tool loop does not lose the call either")

# The provider fix is only half of it. _call_native_tools checked `not
# result.ok` BEFORE reading result.tool_calls, so a provider that reported a
# failure while still carrying parsed calls had them discarded -- which is how
# the live outage presented.
#
# Execution does NOT happen in _call_native_tools; it only parses and hands the
# calls back. chat_with_tools is what runs them, so this is asserted there.
# Driving the parse function and looking for an execution was the first attempt
# and it would have passed for the wrong reason.
from backend.skills import tool_loop as _tl  # noqa: E402

_executed: list = []


class _StubResult:
    """A round that reports failure while still carrying parsed tool calls."""

    def __init__(self, payload):
        self.ok = False
        self.error = "The model returned an empty response."
        self.response = ""
        self.model = "stub"
        self.tokens_in = 1
        self.tokens_out = 1
        self.duration_ms = 1
        self.reasoning_content = ""
        self.tool_calls = payload


class _StubProvider:
    provider_id = "stub"
    has_native_tools = True

    def __init__(self, payload):
        self._payload = payload
        self.rounds = 0

    async def chat(self, messages, *a, **kw):
        self.rounds += 1
        if self.rounds == 1:
            return _StubResult(self._payload)
        return _StubResult(None)


_orig_exec = _tl.execute_skill


async def _spy_exec(name, params, provider=None):
    _executed.append(name)
    return {"success": True, "data": {}, "summary": "", "error": None}


_tl.execute_skill = _spy_exec
try:
    asyncio.run(_tl.chat_with_tools(
        _StubProvider([dict(TOOL_CALL)]),
        [{"role": "user", "content": "list my meetings"}],
        system_prompt="You have tools.",
    ))
    check("a failed round that carried a tool call still runs the tool",
          "meeting_list" in _executed,
          f"the tool was never executed; ran={_executed!r}")

    # The converse must stay true: a round with neither prose nor calls is
    # still a reported failure, not a silent success.
    _executed.clear()
    out = asyncio.run(_tl.chat_with_tools(
        _StubProvider(None),
        [{"role": "user", "content": "list my meetings"}],
        system_prompt="You have tools.",
    ))
    check("a round with neither prose nor calls is still a failure",
          not _executed and "error" in str(out.get("response") or "").lower(),
          f"ran={_executed!r}, response={str(out.get('response'))[:80]!r}")
finally:
    _tl.execute_skill = _orig_exec
print()
print("A tool call written as DSML text is read, not discarded")

# Caught live through the WS gate. The model answered with DeepSeek's DSML
# markup as literal text and nothing else, the parser returned [] for it, and
# -- worse -- did not even mark it malformed, so a real call vanished and the
# raw markup was shown to the user as prose. These are the exact bytes.
from backend.skills.tool_loop import _parse_tool_response  # noqa: E402

_F = "\uff5c"  # U+FF5C, the fullwidth bar the model actually emitted
DSML_TEXT_PLAIN = (
    "Let me pull up your recorded meetings.\n\n"
    f"<{_F}{_F}DSML{_F}{_F} calls>\n"
    f"<{_F}{_F}DSML{_F}{_F} invoke name=\"meeting_list\">\n"
    f"</{_F}{_F}DSML{_F}{_F} invoke>\n"
    f"</{_F}{_F}DSML{_F}{_F} calls>"
)

_live = (
    "Let me pull up your recorded meetings.\n\n"
    f"<{_F}{_F}DSML{_F}{_F} calls>\n"
    f"<{_F}{_F}DSML{_F}{_F} invoke name=\"meeting_list\">\n"
    f"</{_F}{_F}DSML{_F}{_F} invoke>\n"
    f"</{_F}{_F}DSML{_F}{_F} calls>"
)
r = _parse_tool_response(_live)
check("the live DSML reply recovers its tool call",
      [c["name"] for c in r["calls"]] == ["meeting_list"],
      f"parsed {r['calls']!r} from the exact bytes the live model sent")
check("the markup is swallowed so it cannot reach the user",
      bool(r["blocks"]),
      "blocks was empty, so the raw DSML would be shown as the answer")

_with_param = (
    f"<{_F}{_F}DSML{_F}{_F} calls>\n"
    f"<{_F}{_F}DSML{_F}{_F} invoke name=\"meeting_get\">\n"
    f"<{_F}{_F}DSML{_F}{_F} parameter name=\"meeting_id\" string=\"true\">M1"
    f"</{_F}{_F}DSML{_F}{_F} parameter>\n"
    f"</{_F}{_F}DSML{_F}{_F} invoke>\n"
    f"</{_F}{_F}DSML{_F}{_F} calls>"
)
r2 = _parse_tool_response(_with_param)
check("a DSML parameter becomes a keyword argument",
      bool(r2["calls"]) and r2["calls"][0]["params"].get("meeting_id") == "M1",
      f"parsed {r2['calls']!r}")

_ascii = ('<||DSML|| calls>\n<||DSML|| invoke name="meeting_list">\n'
          "</||DSML|| invoke>\n</||DSML|| calls>")
r3 = _parse_tool_response(_ascii)

# Note on case: an UPPERCASE name is refused, and that is pre-existing.
# `_normalise_tool_name` is case-sensitive and `_call_from_xml` behaves
# identically, so this parser matches the existing convention rather than
# inventing a second one. Recorded rather than silently 'fixed' in passing:
# a case change touches every tool path and belongs in its own change.
_upper = (
    f"<{_F}{_F}DSML{_F}{_F} calls>\n"
    f"<{_F}{_F}DSML{_F}{_F} invoke name=\"MEETING_LIST\">\n"
    f"</{_F}{_F}DSML{_F}{_F} invoke>\n"
    f"</{_F}{_F}DSML{_F}{_F} calls>"
)
check("an uppercase name is refused, as the existing parsers do",
      not _parse_tool_response(_upper)["calls"],
      "uppercase matched, putting this parser out of step with "
      "_call_from_xml and _normalise_tool_name")
check("the ASCII-pipe spelling is accepted too",
      [c["name"] for c in r3["calls"]] == ["meeting_list"],
      f"parsed {r3['calls']!r}")

# Only a real skill name may be accepted -- the same rule `_call_from_xml`
# follows, because a wrong name starts the market-and-forge path.
_bogus = (f"<{_F}{_F}DSML{_F}{_F} calls>\n"
          f"<{_F}{_F}DSML{_F}{_F} invoke name=\"definitely_not_a_skill\">\n"
          f"</{_F}{_F}DSML{_F}{_F} invoke>\n"
          f"</{_F}{_F}DSML{_F}{_F} calls>")
check("a DSML block naming a non-skill is ignored",
      not _parse_tool_response(_bogus)["calls"],
      "a bogus name was turned into a call")

# ---- the model's markdown habit, caught live on 2026-10-10 --------------
# These are the exact bytes from the user's saved conversation.
_md_live = (
    "[neutral] On it bro \u2014 running the check now.\n\n"
    "`run_command`: `ffmpeg -version`\n\n"
    "I want to run `ffmpeg -version` on your PC to check whether ffmpeg is "
    "installed and what version you've got. Go ahead and **Allow** it and I'll "
    "report back right away."
)
_md = _parse_tool_response(_md_live)
check("the live markdown reply recovers its tool call",
      [c["name"] for c in _md["calls"]] == ["run_command"],
      f"parsed {_md['calls']!r} from the exact bytes the live model sent -- "
      "an empty list here is the bug the user reported")
check("and the argument is bound to the declared parameter",
      (_md["calls"] or [{}])[0].get("params") == {"command": "ffmpeg -version"},
      f"params were {( _md['calls'] or [{}])[0].get('params')!r}")

# The dangerous direction: ordinary prose that mentions a tool in backticks
# must NOT become a call, or every explanation of a tool would run it.
for _prose in ("You can use `run_command` for that.",
               "The `run_command` skill runs PowerShell on your PC.",
               "I'll use `run_command`: `dir` soon, once you confirm.",
               "`ffmpeg -version` is the command to check."):
    check(f"prose is not read as a call: {_prose[:34]!r}",
          not _parse_tool_response(_prose)["calls"],
          "an ordinary sentence was turned into a tool call")

# A shape the parser will not BIND must still be REPORTED. Refusing to parse
# is right; refusing to mention it is how a call becomes a paragraph.
for _near in ("`run_command`: `dir` and `whoami`",
              "`run_command`: `dir` extra words here"):
    _r = _parse_tool_response(_near)
    check(f"an unbindable call is reported, not dropped: {_near[:30]!r}",
          not _r["calls"] and bool(_r["malformed"]),
          f"calls={_r['calls']!r} malformed={_r['malformed']!r} -- "
          "it vanished, which is the bug")
# One unreadable call is ONE thing to report. A fenced block already reports
# it, so the bare-text fallback must not add a second entry -- that would offer
# the model two corrective rounds for a single mistake.
_trunc = ('Saya telah membuat filenya.\n```tool\n{"tool": "write_file", '
          '"params": {"path": "a.txt", "content": "isi panj')
_tr = _parse_tool_response(_trunc)
check("a truncated fenced call is reported exactly once",
      len(_tr["malformed"]) == 1 and not _tr["calls"],
      f"malformed had {len(_tr['malformed'])} entries, so a duplicate report "
      "reached the corrective round")

check("prose that mentions a tool is still not 'malformed'",
      not _parse_tool_response(
          "You can use `run_command` for that.")["malformed"],
      "an ordinary sentence was reported as a broken call")

# A name that is not a skill must be refused, like every other reader does --
# a guessed name starts the market-and-forge path and writes a file.
check("a markdown call naming a non-skill is ignored",
      not _parse_tool_response("`not_a_skill`: `whatever`")["calls"],
      "a bogus name was turned into a call")

# And the formats that already worked must keep working.
check("plain prose still parses as no call",
      not _parse_tool_response("You have no meetings.")["calls"],
      "prose was read as a call")
check("a fenced json call still parses",
      [c["name"] for c in _parse_tool_response(
          '```tool\n{"tool": "meeting_list", "params": {}}\n```')["calls"]]
      == ["meeting_list"],
      "the original fenced format stopped working")

# The parser fix is only useful if the LOOP acts on it. Drive the whole
# path with a provider that answers in DSML text and assert the tool ran.
class _DsmlProvider:
    provider_id = "stub"
    has_native_tools = True

    def __init__(self):
        self.rounds = 0

    async def chat(self, messages, *a, **kw):
        self.rounds += 1
        if self.rounds == 1:
            return _DsmlResult(DSML_TEXT_PLAIN)  # text, no tool_calls
        return _DsmlResult("You have no meetings yet.")


class _DsmlResult:
    def __init__(self, text, calls=None):
        self.ok = True
        self.error = ""
        self.response = text
        self.model = "stub"
        self.tokens_in = 1
        self.tokens_out = 1
        self.duration_ms = 1
        self.reasoning_content = ""
        self.tool_calls = calls


_dsml_ran: list = []
_orig_exec2 = _tl.execute_skill


async def _spy_exec2(name, params, provider=None):
    _dsml_ran.append(name)
    return {"success": True, "data": {}, "summary": "", "error": None}


_tl.execute_skill = _spy_exec2
try:
    _out = asyncio.run(_tl.chat_with_tools(
        _DsmlProvider(),
        [{"role": "user", "content": "list my meetings"}],
        system_prompt="You have tools.",
    ))
    check("a DSML reply actually RUNS the tool it names",
          "meeting_list" in _dsml_ran,
          f"the loop never executed it; ran={_dsml_ran!r}")
    check("and the raw markup is not shown as the answer",
          "DSML" not in str(_out.get("response") or ""),
          f"reply was {str(_out.get('response'))[:90]!r}")
finally:
    _tl.execute_skill = _orig_exec2

print("A call written in <tool_call> tags is read, not discarded")

# Caught live by the gate a second time, AFTER the DSML fix:
#
#     <tool_call>meeting_list</tool_call>
#
# The JSON form inside this tag already parsed; the bare-name form did not, and
# was not flagged malformed either -- the same silent-discard failure as DSML.
# The tag was then shown to the user as prose.
_tc_text = ("Got it - let me pull up that meeting.\n\n"
            "<tool_call>meeting_list</tool_call>")
_r = _parse_tool_response(_tc_text)
check("the live <tool_call> reply recovers its tool call",
      [c["name"] for c in _r["calls"]] == ["meeting_list"],
      "parsed %r from the exact bytes the gate caught" % (_r["calls"],))
check("and that markup is hidden from the user too",
      bool(_r["blocks"]),
      "blocks was empty, so the tag would be shown as the answer")

_tc_forms = {
    "bare name": "<tool_call>meeting_list</tool_call>",
    "with parens": "<tool_call>meeting_list()</tool_call>",
    "self-closing": '<tool_call name="meeting_list"/>',
}
for _label, _t in _tc_forms.items():
    check("the %s spelling is accepted" % _label,
          [c["name"] for c in _parse_tool_response(_t)["calls"]]
          == ["meeting_list"],
          "parsed %r" % (_parse_tool_response(_t)["calls"],))

check("a <tool_call> naming a non-skill is ignored",
      not _parse_tool_response(
          "<tool_call>definitely_not_a_skill</tool_call>")["calls"],
      "a bogus name was turned into a call")

check("prose that merely SAYS tool_call is not a call",
      not _parse_tool_response(
          "I will use a tool_call to do that.")["calls"],
      "a mention of the word was read as a call")
print()

# ---------------------------------------------------------------------------
# A plain fence holding a real command is a call, not an illustration.
#
# Reported 2026-10-10: "its not running anything on terminal". The model wrote
# the command in an untagged fence, no JSON and no tag, so every reader here
# returned `[]` -- the command was shown to the user as prose and nothing ran.
# Three of the four measured instances were in the Flux search the user reported.
# ---------------------------------------------------------------------------

_fence_body = ('Get-ChildItem -Path "E:\\Kunoir\\Codeground\\docker\\aethelgard" '
               '-Recurse -Include *.safetensors,*.gguf,*.ckpt '
               '-ErrorAction SilentlyContinue | '
               'Where-Object { $_.Name -match "flux" }')
_live_fence = "Running it now.\n\n```\n" + _fence_body + "\n```"

_r = _parse_tool_response(_live_fence)
check("the live fenced-command reply recovers a run_command call",
      [c["name"] for c in _r["calls"]] == ["run_command"],
      "parsed %r from the exact fence the model wrote" % (_r["calls"],))
check("and the fence body is bound to the command parameter",
      bool(_r["calls"]) and _r["calls"][0]["params"].get("command") == _fence_body,
      "params were %r" % (_r["calls"][0]["params"] if _r["calls"] else None,))
check("and the fence is not also shown to the user",
      bool(_r["blocks"]),
      "blocks was empty, so the command would be left visible as prose")

# A fence that OPENS with a comment. Measured live on 2026-10-10 in the whisper
# check: the model wrote `# Check for whisper CLI command` above the command it
# meant to run, the reader rejected the whole block on that first line, and the
# command was shown as the answer with nothing run - the same "its not running
# anything" report, one comment line over.
_commented_fence = (
    "I'll check if Whisper is installed on this machine.\n\n"
    "```bash\n"
    "# Check for whisper CLI command\n"
    "which whisper\n\n"
    "# Check for Python package\n"
    'python -c "import whisper; print(whisper.__version__)" 2>&1\n'
    "```")
_rc = _parse_tool_response(_commented_fence)
check("a fence that opens with a comment still recovers the call",
      [c["name"] for c in _rc["calls"]] == ["run_command"],
      "parsed %r from the commented fence the model wrote" % (_rc["calls"],))
check("and the whole commented body is bound as the command",
      bool(_rc["calls"]) and "which whisper" in _rc["calls"][0]["params"].get("command", ""),
      "params were %r" % (_rc["calls"][0]["params"] if _rc["calls"] else None,))

# The other direction must not weaken: a fence that is ONLY comments is not a
# command, and folding it would "run" a note.
_all_comment = "Sure.\n\n```bash\n# a note\n# nothing to run here\n```"
check("a fence of only comments is not a command",
      _parse_tool_response(_all_comment)["calls"] == [],
      "a comment-only block was read as a command to run")

# The other direction, and the reason the reader is strict: a fence is ALSO how
# a model shows a command it is not running. Folding every fence would execute
# commands the user was only being shown.
_not_calls = {
    "a bare path": "E:\\Kunoir\\Codeground\\foo.txt",
    "a sentence": "This command lists the directory and shows the files.",
    "a bare verb": "dir",
    "a python block": "import os\nprint(os.getcwd())",
    "json that is not a call": '{"model": "flux", "steps": 20}',
    "an empty block": "   ",
}
for _label, _body in _not_calls.items():
    _t = "Here it is:\n\n```\n" + _body + "\n```"
    check("a fence holding %s is not run" % _label,
          not _parse_tool_response(_t)["calls"],
          "parsed %r -- a fence being explained would have been executed"
          % (_parse_tool_response(_t)["calls"],))

check("a tagged ```python fence is left to the language reader",
      not _parse_tool_response(
          "```python\nimport os\nprint(os.getcwd())\n```")["calls"],
      "a tagged non-shell fence was folded into a run_command")

# The forms already handled must still parse. Regression guard.
_already = {
    "the XML form": "<run_command>\n<command>dir</command>\n</run_command>",
    "the tool-tag form": "<tool_call>meeting_list</tool_call>",
    "the fenced JSON form": '```tool\n{"tool": "run_command", "params": {"command": "dir"}}\n```',
}
for _label, _t in _already.items():
    check("the %s still parses" % _label,
          bool(_parse_tool_response(_t)["calls"]),
          "a previously-working form was broken")
print()

# ---------------------------------------------------------------------------
# The model's invented control tags must not reach the user.
#
# The reply that admitted the search never ran carried
# `<budget:token_budget>2000</budget:token_budget>` -- a tag that appears
# nowhere in this codebase and which the model writes from training. Nothing
# stripped it, so it was shown as part of the answer.
# ---------------------------------------------------------------------------
from backend.skills.tool_loop import _strip_control_tags as _strip

_leaked = ("I don't have results yet - I never actually got the command back.\n\n"
           "Let me run it properly now.\n\n"
           "<budget:token_budget>2000</budget:token_budget>")
_clean = _strip(_leaked)
check("the leaked <budget:...> tag is gone from the reply",
      "budget" not in _clean and "2000" not in _clean,
      "reply still reads %r" % (_clean,))
check("and the model's actual words survive",
      "I never actually got the command back" in _clean,
      "stripping ate the answer: %r" % (_clean,))

check("a URL is not mistaken for a control tag",
      _strip("see <http://example.com/page> now") == "see <http://example.com/page> now",
      "a URL was stripped")
check("a comparison is not mistaken for a control tag",
      _strip("a < b and c > d") == "a < b and c > d",
      "plain text was stripped")
check("a real tool-call tag is left for the parser",
      _strip("<run_command><command>dir</command></run_command>")
      == "<run_command><command>dir</command></run_command>",
      "the XML call form was stripped before it could be read")
check("a reply with no tags is untouched",
      _strip("Just a plain answer.") == "Just a plain answer.",
      "an untagged reply was altered")
print()

# ---------------------------------------------------------------------------
# A promise with no call is a dead end, and must be caught rather than answered.
#
# The other half of the same report -- "after doing its task, it is not
# reporting back". The reply was "On it - searching for Flux model files ..."
# with no call, no fence and no tag: unreadable by construction, because there
# is no syntax to catch. Detection is structural and the phrase alone is not
# enough, so both directions are checked here.
# ---------------------------------------------------------------------------
from backend.skills.tool_loop import _announced_work as _announces

_promises = [
    "On it - searching for Flux model files under the aethelgard tree now.",
    "Running it now.",
    "Let me actually fire the command off and see what comes back.",
    "Let me check that for you.",
    # The phrasings the live install actually produced on 2026-10-10, driving
    # the running app over its WebSocket. Each one defeated the verb whitelist
    # this code used to carry - "Deleting" and "Firing" were simply not in it -
    # which is why detection is now grammatical rather than a word list.
    "Deleting that temp folder now - it's destructive.",
    "Firing the delete now; it's destructive so expect an approval prompt.",
    "Going ahead with the delete now.",
    "Executing the command now.",
    "Understood - your folder, your call. Firing the delete now; it's "
    "destructive so expect an approval prompt.",
    "I'll carry it out and confirm it's gone.",
]
for _p in _promises:
    check("a promise reads as announced work: %r" % _p[:40],
          _announces(_p),
          "the dropped-call reply was not recognised and would pass as an answer")

_not_promises = [
    "I'm here to help! What would you like me to assist you with?",
    "CapCut 9.5.0.4050 is installed. Found it in the registry.",
    "I'll explain how the parser works: it scans for fences.",
    "Here is the answer: 42.",
    "There's nothing to delete - that path doesn't exist.",
    "I can't emit a call for a tool I don't have.",
    # A gerund is not a promise on its own: the immediacy marker is what makes
    # one, and these have none.
    "Deleting files is dangerous, so be careful.",
    "Removing a folder recursively can lose data.",
    "The running total is 42.",
]
for _p in _not_promises:
    check("an ordinary answer is not read as a promise: %r" % _p[:40],
          not _announces(_p),
          "a normal reply would have been nagged for a call it never intended")

check("a long answer containing the phrase is not a promise",
      not _announces("Let me run through the options. " + "Detail. " * 120),
      "length alone should exempt a real answer")

# ---------------------------------------------------------------------------
# The nudge has to FIRE on the dashboard's own configuration.
#
# Recognising a promise is only half of it: on 2026-10-10 the dashboard sent
# `tools=None`, the model answered a destructive request with "Running it now -
# it may come back asking you to approve", called nothing, and nothing ran -
# no permission card, nothing on the console. `_announced_work` returned True,
# but the nudge was gated on `only`, which is None for every enabled skill, so
# it was skipped on the one surface that needed it. Detection alone is not the
# guard; the guard is the retry it triggers.
# ---------------------------------------------------------------------------

def _scripted_reply(text):
    class R:
        pass
    r = R()
    r.ok = True
    r.response = text
    r.model = "fake"
    r.tokens_in = 1
    r.tokens_out = 1
    r.duration_ms = 1
    r.error = None
    r.tool_calls = None
    r.reasoning_content = ""
    return r


def _nudge_probe(tools_arg, reply):
    import asyncio
    from backend.skills import tool_loop as _tl

    class P:
        provider_id = "fake"
        has_native_tools = True

        def __init__(self):
            self.n = 0

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            self.n += 1
            if self.n == 1:
                return _scripted_reply(reply)
            return _scripted_reply("done")

        async def chat_stream(self, *a, **k):
            return
            yield  # pragma: no cover

    async def go():
        p = P()
        real = _tl.execute_skill

        async def spy(name, params, provider):
            return {"success": True, "data": {}}

        _tl.execute_skill = spy
        try:
            out = await _tl.chat_with_tools(
                p, [{"role": "user", "content": "delete the folder ./build"}],
                tools=tools_arg, on_delta=None)
        finally:
            _tl.execute_skill = real
        return p, out
    return asyncio.run(go())


_announcement = ("Running it now — it may come back asking you to approve, "
                 "since it's destructive.")

_p1, _ = _nudge_probe(None, _announcement)
check("the promise nudge fires with tools=None (the dashboard default)",
      _p1.n > 1,
      "the model announced a destructive action and called nothing; with no "
      "retry the announcement IS the turn - no permission card, nothing run")

_p2, _ = _nudge_probe(["run_command"], _announcement)
check("the promise nudge also fires with a tool filter",
      _p2.n > 1,
      "a restricted catalogue must not change whether the guard runs")

_p3, _ = _nudge_probe([], _announcement)
check("the promise nudge does NOT fire when no tools were offered",
      _p3.n == 1,
      "a caller with no tools was nagged to call one it was never given")

# The live install's own phrasings, which the earlier verb list missed. Both
# were observed on 2026-10-10 driving the real app over its WebSocket.
_live_promises = [
    "Deleting that temp folder now — it's a destructive action, so it'll "
    "trigger an approval prompt. Please tap Allow on the card above the "
    "composer and I'll carry it out and confirm it's gone.",
    "Removing it right now.",
    "I'll carry it out and confirm it's gone.",
]
for _p in _live_promises:
    check("the install's own promise phrasing is caught: %r" % _p[:44],
          _announces(_p),
          "a promise the model actually writes would pass as an answer")

_descriptions = [
    "Deleting files is dangerous, so be careful.",
    "Removing a folder recursively can lose data.",
]
for _p in _descriptions:
    check("a description of an action is NOT a promise: %r" % _p[:44],
          not _announces(_p),
          "explaining what an action does must not be nagged as a promise")


def _stream_nudge_probe(reply):
    """Does a STREAMED promise round reach the nudge, or is it returned?"""
    import asyncio
    from backend.skills import tool_loop as _tl

    _call = [{"id": "c1", "type": "function",
              "function": {"name": "run_command",
                           "arguments": '{"command": "Remove-Item x"}'}}]

    def _reply(text, calls=None):
        class R:
            pass
        r = R()
        r.ok = True
        r.response = text
        r.model = "fake"
        r.tokens_in = 1
        r.tokens_out = 1
        r.duration_ms = 1
        r.error = None
        r.tool_calls = calls
        r.reasoning_content = ""
        return r

    class P:
        provider_id = "fake"
        has_native_tools = True

        def __init__(self):
            self.streams = 0

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            return _reply("", _call)

        async def chat_stream(self, messages, model=None, max_tokens=4096,
                              temperature=0.7):
            self.streams += 1
            if self.streams == 1:
                yield reply
            return

    async def go():
        p = P()
        ran = []
        real = _tl.execute_skill

        async def spy(name, params, provider):
            ran.append(name)
            return {"success": True, "data": {}}

        _tl.execute_skill = spy
        try:
            await _tl.chat_with_tools(
                p, [{"role": "user", "content": "delete ./x"}],
                tools=None, on_delta=lambda _t: None)
        finally:
            _tl.execute_skill = real
        return p, ran
    return asyncio.run(go())


_sp, _sran = _stream_nudge_probe(
    "Deleting that folder now — approve the card.")
check("a STREAMED promise round reaches the nudge and re-runs",
      _sp.streams > 1,
      "the streamed early-return answered with the promise and skipped the "
      "nudge entirely - the guard was unreachable on the dashboard's path")
check("a STREAMED promise round actually runs the tool",
      "run_command" in _sran,
      "the retry must produce a call; a nudge that re-narrates is no fix")

# ---------------------------------------------------------------------------
# Every tool result must be PAIRED with an assistant tool_call carrying the
# same id. An id-less call - a text-parsed one, or a native one from a gateway
# that omits the id - used to echo no assistant message but still emit a
# `role:"tool"` message with no `tool_call_id`. The live 9router rejects that
# with HTTP 400 {"code":11133,"extError":{"code":"model_param_invalid"}},
# reproduced 2026-10-10 by sending the id-less form (400) and the paired form
# (200) to http://localhost:20128/v1. This check asserts the pairing invariant
# without needing the gateway, so it runs anywhere.
# ---------------------------------------------------------------------------

def _pair_probe(provider_tool_call):
    """Run one tool round and return the follow-up messages that were sent."""
    import asyncio
    from backend.skills import tool_loop as _tl

    def _r(text, tc=None):
        class R:
            pass
        r = R()
        r.ok = True
        r.response = text
        r.model = "fake"
        r.tokens_in = 1
        r.tokens_out = 1
        r.duration_ms = 1
        r.error = None
        r.tool_calls = tc
        r.reasoning_content = ""
        return r

    class P:
        provider_id = "fake"
        has_native_tools = True

        def __init__(self):
            self.n = 0
            self.sent = None

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            self.n += 1
            self.sent = [dict(m) for m in messages]
            if self.n == 1:
                return _r("", provider_tool_call)
            return _r("done")

    async def go():
        p = P()
        real = _tl.execute_skill

        async def spy(name, params, provider):
            return {"success": True, "data": {"stdout": "hi"}}

        _tl.execute_skill = spy
        try:
            await _tl.chat_with_tools(
                p, [{"role": "user", "content": "run echo hi"}],
                tools=None, on_delta=None)
        finally:
            _tl.execute_skill = real
        return p
    return asyncio.run(go())


# The exact shape the live gateway produced: a call with no id.
_p = _pair_probe([{"type": "function",
                   "function": {"name": "run_command",
                                "arguments": '{"command": "echo hi"}'}}])
_tool_msgs = [m for m in _p.sent if m.get("role") == "tool"]
_asst_msgs = [m for m in _p.sent
              if m.get("role") == "assistant" and m.get("tool_calls")]
check("an ID-LESS tool call gets an assistant message with a REAL id",
      len(_asst_msgs) == 1
      and all(tc.get("id") for tc in _asst_msgs[0]["tool_calls"]),
      "the assistant call must carry an id the tool result can answer; an "
      "absent one is what the gateway rejects as model_param_invalid")
check("every tool result carries a non-empty tool_call_id",
      bool(_tool_msgs) and all(m.get("tool_call_id") for m in _tool_msgs),
      "a role:tool message without an id is HTTP 400 code 11133 on 9router")
check("the tool_call_id matches the assistant call id",
      bool(_asst_msgs) and bool(_tool_msgs)
      and _asst_msgs[0]["tool_calls"][0].get("id")
      and _asst_msgs[0]["tool_calls"][0].get("id")
      == _tool_msgs[0].get("tool_call_id"),
      "the pair must share one non-empty id, not two unrelated (or two "
      "absent) ones")

# The other end: an id the gateway DID supply must be reused, not replaced.
_p2 = _pair_probe([{"id": "call_xyz", "type": "function",
                    "function": {"name": "run_command",
                                 "arguments": '{"command": "echo hi"}'}}])
_t2 = [m for m in _p2.sent if m.get("role") == "tool"]
check("a supplied call id is reused verbatim",
      bool(_t2) and _t2[0].get("tool_call_id") == "call_xyz",
      "rewriting a real id would break the pair the gateway already knows")

# A call the model WRITES as text (the fallback path, and providers without
# native tools) is parsed from prose and carries no id at all - the deeper
# source of id-less pairs. It must be given one too.
def _text_pair_probe(reply):
    import asyncio
    from backend.skills import tool_loop as _tl

    def _r(text):
        class R:
            pass
        r = R()
        r.ok = True
        r.response = text
        r.model = "fake"
        r.tokens_in = 1
        r.tokens_out = 1
        r.duration_ms = 1
        r.error = None
        r.tool_calls = None
        r.reasoning_content = ""
        return r

    class P:
        provider_id = "fake"
        has_native_tools = True

        def __init__(self):
            self.n = 0
            self.sent = None

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            self.n += 1
            self.sent = [dict(m) for m in messages]
            if self.n == 1:
                return _r(reply)   # no native tool_calls - written in text
            return _r("done")

    async def go():
        p = P()
        real = _tl.execute_skill

        async def spy(name, params, provider):
            return {"success": True, "data": {"stdout": "hi"}}

        _tl.execute_skill = spy
        try:
            await _tl.chat_with_tools(
                p, [{"role": "user", "content": "run echo hi"}],
                tools=None, on_delta=None)
        finally:
            _tl.execute_skill = real
        return p
    return asyncio.run(go())


_p3 = _text_pair_probe('```tool\nrun_command({"command": "echo hi"})\n```')
_t3 = [m for m in _p3.sent if m.get("role") == "tool"]
_a3 = [m for m in _p3.sent
       if m.get("role") == "assistant" and m.get("tool_calls")]
check("a TEXT-WRITTEN call is paired with a real id too",
      bool(_t3) and all(m.get("tool_call_id") for m in _t3) and bool(_a3)
      and _a3[0]["tool_calls"][0].get("id") == _t3[0].get("tool_call_id"),
      "prose-parsed calls have no id of their own, so one must be made or the "
      "follow-up is rejected the moment the model writes its call in text")

# The case that actually fires in production. 9router is NOT in
# NATIVE_TOOL_PROVIDERS, so `_call_prompt_tools` serves EVERY turn
# (tool_loop.py says so in its own comment), and a prompt-path call is written
# in text and has no id. Measured live on 2026-10-10: the messages this path
# builds carried `tool_call_id: None` and http://localhost:20128/v1 answered
# HTTP 400 code 11133; with the id made, 200. The provider here mirrors that
# exactly - id "9router", no native tools - so a regression on THIS path fails
# the suite, not just on a native-only fixture.
def _prompt_path_probe():
    import asyncio
    from backend.skills import tool_loop as _tl

    def _r(text):
        class R:
            pass
        r = R()
        r.ok = True
        r.response = text
        r.model = "fake"
        r.tokens_in = 1
        r.tokens_out = 1
        r.duration_ms = 1
        r.error = None
        r.tool_calls = None
        r.reasoning_content = ""
        return r

    class P:
        provider_id = "9router"      # outside NATIVE_TOOL_PROVIDERS
        has_native_tools = False     # -> _call_prompt_tools every turn

        def __init__(self):
            self.n = 0
            self.sent = None

        async def chat(self, messages, model=None, max_tokens=4096,
                       temperature=0.7, tools=None):
            self.n += 1
            self.sent = [dict(m) for m in messages]
            if self.n == 1:
                return _r('```tool\n{"tool": "run_command", '
                          '"params": {"command": "echo hi"}}\n```')
            return _r("done")

        async def chat_stream(self, *a, **k):
            return
            yield  # pragma: no cover

    async def go():
        p = P()
        real = _tl.execute_skill

        async def spy(name, params, provider):
            return {"success": True, "data": {"stdout": "hi"}}

        _tl.execute_skill = spy
        try:
            await _tl.chat_with_tools(
                p, [{"role": "user", "content": "run echo hi"}],
                tools=None, on_delta=lambda _t: None)
        finally:
            _tl.execute_skill = real
        return p
    return asyncio.run(go())


_p4 = _prompt_path_probe()
_t4 = [m for m in _p4.sent if m.get("role") == "tool"]
_a4 = [m for m in _p4.sent
       if m.get("role") == "assistant" and m.get("tool_calls")]
check("the 9router PROMPT path pairs its tool result (the live 11133 case)",
      bool(_t4) and all(m.get("tool_call_id") for m in _t4)
      and bool(_a4)
      and _a4[0]["tool_calls"][0].get("id") == _t4[0].get("tool_call_id"),
      "9router takes the prompt path every turn and its calls have no id; "
      "unpaired, the follow-up is HTTP 400 code 11133 against the real gateway")

if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All tool-call parsing checks passed.")
