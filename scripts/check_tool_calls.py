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

if fails:
    print(f"{len(fails)} FAILED")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("All tool-call parsing checks passed.")
