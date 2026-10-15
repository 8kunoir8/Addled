"""MCP client regression check, using scripts/mcp_test_server.py as the server.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_mcp.py

Covers connect/discovery, registration of tools as skills, the approval gate,
timeouts, error isolation and shutdown. Everything up to the last block is
offline; the final block drives a real provider and needs a working API key.
"""
import asyncio, json, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
sys.path.insert(0, ROOT)

from backend.config import config
from backend.mcp_client import approval
from backend.mcp_client.manager import mcp_manager
from backend.mcp_client import protocol as mcp_protocol
from backend.actions.executor import executor as _executor

fails = []
# The interpreter that runs THIS check, not a bundled path. The bundle exists
# only in a packaged install; hardcoding it made the MCP server unspawnable in
# a checkout, so nothing registered and the failures read as broken code.
PY = sys.executable
SERVER = os.path.join(ROOT, "scripts", "mcp_test_server.py")
SID = "addled_test_fixture"


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

async def approved(name, params):
    """Run an MCP tool the way a user does: allow it, then run it.

    The gate no longer accepts `confirm=true` from the model, so every call in
    this file that expects the tool to actually run has to pass through the
    user's decision. Written once here so each site reads as what it is.
    """
    from backend.skills.registry import skill_registry
    result = await skill_registry.execute(name, params)
    if result.success:
        return result
    approval_id = (result.data or {}).get("approval_id")
    if not approval_id:
        return result
    return await _executor.approve(approval_id)


class _NativeStubProvider:
    """A scripted provider for the native tool-calling path.

    It asks for one tool by name on the first round and answers in plain text
    on the next, so the plumbing between the model and the tool can be checked
    without depending on what a live model decides to call.
    """

    provider_id = "openai"  # impersonates a provider with native tool support
    provider_name = "stub"

    def __init__(self, tool_name: str, arguments: str = '{"text": "pineapple"}'):
        self.tool_name = tool_name
        self.arguments = arguments
        self.round = 0
        self.seen: list[list[dict]] = []

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None):
        from backend.providers.base import ProviderResult
        self.round += 1
        self.seen.append([dict(m) for m in messages])
        if self.round == 1:
            return ProviderResult(
                ok=True, response="", model="stub",
                tool_calls=[{
                    "id": "call_1", "type": "function",
                    "function": {"name": self.tool_name,
                                 "arguments": self.arguments},
                }],
                reasoning_content="thinking about the echo",
            )
        return ProviderResult(ok=True, model="stub",
                              response="The tool returned pineapple.")


async def main():
    original = config.get("mcp", "servers", default=[])
    config._data.setdefault("mcp", {})["enabled"] = True
    config._data["mcp"]["autoconnect"] = False
    try:
        # ---- 1. add + connect --------------------------------------------
        added = mcp_manager.add({
            "id": SID, "name": "Addled test fixture", "transport": "stdio",
            "command": [PY, "-s", SERVER], "enabled": True, "trusted": False,
            "timeout_s": 10,
        })
        check("add succeeds", added.get("success"), str(added))
        if not added.get("success"):
            return

        # invalid definitions are rejected, not crashed on
        bad = mcp_manager.add({"name": "no transport"})
        check("stdlib server without a command is rejected",
              bad.get("success") is False, str(bad))
        bad = mcp_manager.add({"name": "bad url", "transport": "http",
                               "url": "ftp://x"})
        check("non-http url is rejected", bad.get("success") is False, str(bad))

        # A failed request has to explain itself. What the card showed before
        # this was a raw JSON object, or a Cloudflare 530 page whose first 200
        # characters are a doctype.
        from backend.mcp_client.http import _explain

        note = _explain(530, '<!DOCTYPE html>\n<html class="no-js ie6 oldie">origi')
        check("a 5xx blames the other side, not the key",
              "their side" in note and "DOCTYPE" not in note
              and "<html" not in note, note)
        note = _explain(402, '{"error": {"code": "402", "message": '
                             '"Payment required"}}')
        check("a 402 says the server bills and cannot be charged here",
              "bills" in note and note.count("Payment required") == 1, note)
        note = _explain(401, '{"error":"invalid_token",'
                             '"error_description":"Invalid token"}')
        check("a 401 points at the credential, with a way to replace it",
              "credential" in note and "Forget" in note, note)
        note = _explain(418, "teapot")
        check("an unknown code still says something",
              "418" in note and "teapot" in note, note)

        out = await mcp_manager.connect(SID)
        print("connect:", out.get("success"), out.get("error"))
        check("connect succeeds", out.get("success"), str(out.get("error")))
        status = mcp_manager.server_status(SID)
        print("state:", status["state"], "| tools:", status["tool_count"],
              "| protocol:", status["protocol_version"],
              "| info:", status["server_info"])
        check("state is ready", status["state"] == "ready", status["state"])
        check("protocol version recorded",
              status["protocol_version"] == "2025-06-18",
              status["protocol_version"])
        check("server info captured",
              status["server_info"].get("name") == "addled-test-server",
              str(status["server_info"]))
        check("four tools discovered", status["tool_count"] == 4,
              str(status["tool_count"]))
        check("tool schemas retrieved",
              all(t.get("inputSchema") for t in mcp_manager.tools(SID)["tools"]))

        # "Reconnect all" is an explicit request. With autoconnect off it used
        # to reach start(), which skips, so the button did nothing and said
        # nothing — the handler still answered success.
        await mcp_manager.disconnect(SID)
        skipped = await mcp_manager.start()
        check("start() honours autoconnect being off",
              "skipped" in skipped, str(skipped)[:120])
        again = await mcp_manager.reload()
        check("but an explicit reload connects anyway",
              int((again or {}).get("ready") or 0) >= 1, str(again)[:200])

        # ---- 2. tools appear as skills -----------------------------------
        from backend.skills.registry import skill_registry
        mcp_skills = [s for s in skill_registry.list_all()
                      if s.category == "mcp"]
        names = {s.name for s in mcp_skills}
        print("mcp skills:", sorted(names)[:6])
        check("all four tools registered as skills", len(mcp_skills) == 4,
              str(sorted(names)))
        check("native tool payload includes them",
              names.issubset({t["function"]["name"]
                              for t in skill_registry.to_openai_tools()}))
        check("prompt payload includes them",
              all(n in skill_registry.to_prompt_tools() for n in names))
        echo_skill = skill_registry.get(f"mcp__{SID}__echo")
        if echo_skill is None:
            # The server did not start, so there is no schema to inspect. Say
            # that plainly: a traceback here reads as a defect in the code
            # under test when the real problem is the fixture.
            check("the test server started and registered mcp__*__echo", False,
                  f"no skill registered -- the MCP server never connected "
                  f"(interpreter {PY})")
        else:
            check("schema advertises confirm for an untrusted server",
                  "confirm" in (echo_skill.parameters.get("properties") or {}),
                  str(echo_skill.parameters)[:200])

        # ---- 3. approval gate -------------------------------------------
        #
        # The gate's contract changed. It used to be satisfied by the model:
        # the refusal said "call again with confirm=true", and the second call
        # WAS the approval, so a model that wanted a tool to run only had to
        # send the flag. These checks now hold the opposite — the model cannot
        # approve itself, the turn ends on the refusal, and the tool runs off
        # the user's answer.
        first = await skill_registry.execute(f"mcp__{SID}__echo",
                                            {"text": "hello"})
        print("unapproved call:", first.success,
              str(first.data.get("requires_approval")))
        check("unapproved call is refused", first.success is False,
              str(first.data)[:200])
        check("refusal asks for approval",
              first.data.get("requires_approval") is True, str(first.data)[:200])
        check("the refusal queues a real approval for the user",
              bool(first.data.get("approval_id")), str(first.data)[:200])
        check("the refusal does not read as an instruction to retry",
              "call this exact tool again" not in
              str(first.data.get("message")).lower(),
              "the refusal must not coach the model past the gate")
        check("refusal did not reach the server",
              not approval.is_approved(SID, "echo"))

        # THE ATTACK: the model sends confirm=true with nobody asked.
        second = await skill_registry.execute(
            f"mcp__{SID}__echo", {"text": "hello", "confirm": True})
        print("model sends confirm=true ->", second.success)
        check("the model cannot approve its own call with confirm=true",
              second.success is False, str(second.data)[:200])
        check("confirm=true did not record an approval",
              not approval.is_approved(SID, "echo"),
              "the model granted itself permission")
        check("the refused call did not run",
              second.data.get("text") != "hello", str(second.data)[:200])

        # The honest path: the USER approves, and then the tool runs.
        add_pending = await skill_registry.execute(
            f"mcp__{SID}__add", {"a": 17, "b": 25})
        check("a second tool is gated on its own, not by server",
              add_pending.success is False and bool(
                  add_pending.data.get("requires_approval")),
              str(add_pending.data)[:200])
        _aid = add_pending.data.get("approval_id")
        check("the queued approval can be found by its id",
              bool(_aid), str(add_pending.data)[:200])
        if _aid:
            _approved = await _executor.approve(_aid)
            print("after the user approves ->", _approved.success,
                  repr(_approved.data))
            check("the user's approval RUNS the tool",
                  _approved.success is True, str(_approved.data)[:200])
            check("the approved call returned the right answer",
                  str((_approved.data or {}).get("text")) == "42",
                  str(_approved.data)[:200])

        # And once approved, the same tool runs without asking again.
        again = await skill_registry.execute(f"mcp__{SID}__add",
                                             {"a": 1, "b": 2})
        check("an approved tool does not prompt a second time",
              again.success is True, str(again.data)[:200])
        check("the second call actually ran",
              str((again.data or {}).get("text")) == "3",
              str(again.data)[:200])
        check("the confirm argument never reaches the server",
              "confirm" not in str(again.data.get("structured", {})), "leaked")

        boom = await approved(f"mcp__{SID}__boom", {})
        # ---- 4. tool-level errors are soft ------------------------------
        check("a failing tool reports failure", boom.success is False,
              str(boom.data or boom.error)[:200])
        # The server's own words, on whichever field the path used.
        check("failure carries the server message",
              "boom failed on purpose" in f"{boom.data} {boom.error}",
              f"{boom.data} {boom.error}"[:200])

        # ---- 5. trusted servers skip the gate ---------------------------
        mcp_manager.update(SID, {"trusted": True})
        check("trusted schema drops confirm",
              "confirm" not in ((skill_registry.get(
                  f"mcp__{SID}__echo").parameters.get("properties")) or {}))
        # The tool card reads these to decide whether to offer a switch. A
        # trusted server never asks, so the switch must not be offered — an
        # inert control reads as "you can allow this" and is a lie.
        trusted_tools = mcp_manager.server_status(SID).get("tools") or []
        check("a trusted server reports its tools as not grantable",
              trusted_tools and all(t.get("grantable") is False
                                    for t in trusted_tools),
              str(trusted_tools)[:200])
        mcp_manager.update(SID, {"trusted": False})
        approval.revoke(SID)
        check("revoking clears approvals", approval.approved_list() == [])

        # ---- 5b. a standing tool approval survives losing the session ----
        untrusted_tools = mcp_manager.server_status(SID).get("tools") or []
        check("an untrusted server reports its tools as grantable",
              untrusted_tools and all(t.get("grantable") is True
                                      for t in untrusted_tools),
              str(untrusted_tools)[:200])
        check("and none of them standing before a grant",
              all(t.get("standing") is False for t in untrusted_tools),
              str(untrusted_tools)[:200])

        approval.approve_always(SID, "echo")
        after = {t["name"]: t for t in
                 (mcp_manager.server_status(SID).get("tools") or [])}
        check("a standing grant shows on the tool card",
              after.get("echo", {}).get("standing") is True,
              str(after.get("echo"))[:200])
        check("and is reported without the session set",
              (approval._approved.clear() or True)
              and approval.is_approved(SID, "echo") is True,
              "the standing half was not consulted")
        approval.revoke(SID, "echo")
        check("revoking the tool clears the standing grant",
              approval.is_standing(SID, "echo") is False,
              "the standing grant survived")

        # ---- 6. timeout -------------------------------------------------
        mcp_manager.update(SID, {"timeout_s": 1})
        slow = await approved(f"mcp__{SID}__slow", {"secs": 2})
        check("slow tool times out instead of hanging", slow.success is False,
              str(slow.data or slow.error)[:200])
        # The message can arrive either on the SkillResult (gated path) or in
        # `error` (the executor's ActionResult after an approval), so read both.
        _slow_text = f"{slow.data} {slow.error}"
        check("timeout is reported clearly",
              "timed out" in _slow_text.lower(), _slow_text[:200])
        # The fixture is single-threaded and still sleeping off that call, so
        # let it finish and then confirm the connection recovered.
        mcp_manager.update(SID, {"timeout_s": 10})
        await asyncio.sleep(2.5)
        after = await approved(f"mcp__{SID}__echo", {"text": "still here"})
        check("server still usable after a timeout", after.success,
              str(after.data)[:200])

        # ---- 7. discovery-free disconnect removes tools -----------------
        await mcp_manager.disconnect(SID)
        check("disconnect removes the tools",
              not [s for s in skill_registry.list_all()
                   if s.category == "mcp"])
        check("state resets", mcp_manager.server_status(SID)["state"]
              == "disconnected")

        # ---- 8. a bad command fails softly -----------------------------
        mcp_manager.update(SID, {"command": ["definitely-not-a-real-binary"],
                                 "transport": "stdio"})
        bad_conn = await mcp_manager.connect(SID)
        print("bad command:", bad_conn.get("success"), bad_conn.get("error"))
        check("bad command fails softly", bad_conn.get("success") is False,
              str(bad_conn))
        check("bad command records an error", bool(bad_conn.get("error")))
        mcp_manager.update(SID, {"command": [PY, "-s", SERVER]})

        # ---- 9. end to end through a real provider ----------------------
        conn = await mcp_manager.connect(SID)
        check("reconnect works", conn.get("success"), str(conn.get("error")))
        mcp_manager.update(SID, {"trusted": True})

        # Auto-forge/market would try to invent any tool name the model asks for
        # and write it into backend/memory/forged_skills; disable both so this
        # test cannot leave junk behind.
        import backend.skills.forge as forge_module
        original_forge = forge_module.skill_forge.forge

        async def _no_forge(*_args, **_kwargs):
            class _Result:
                success = False
                detail = "forge disabled during the MCP test"
            return _Result()

        forge_module.skill_forge.forge = _no_forge
        market = config.get("skills", "market_search", default=True)
        config._data.setdefault("skills", {})["market_search"] = False
        try:
            from backend.skills.tool_loop import chat_with_tools

            echo_tool = f"mcp__{SID}__echo"
            stub = _NativeStubProvider(echo_tool)
            result = await chat_with_tools(
                stub, [{"role": "user", "content": "echo pineapple"}],
                system_prompt="test", max_tool_rounds=3)
            used = [t.get("tool") for t in result.get("tool_results") or []]
            print("native loop used:", used)
            check("native loop dispatched the MCP tool", used == [echo_tool],
                  str(used))
            check("the MCP tool actually ran",
                  bool((result.get("tool_results") or [{}])[0].get("success")),
                  json.dumps(result.get("tool_results"))[:200])
            check("the tool loop reports what it called",
                  bool(result.get("tool_results")), "tool_results was empty")

            follow_up = json.dumps(stub.seen[-1])
            check("the tool result reached the model",
                  "pineapple" in follow_up, follow_up[:200])
            check("the assistant tool_call was kept in history",
                  any(m.get("tool_calls") for m in stub.seen[-1]),
                  "no tool_calls in the follow-up request")
            check("reasoning_content was echoed back",
                  any(m.get("reasoning_content") for m in stub.seen[-1]),
                  "thinking-mode field was dropped")

            # An invented tool name must not be echoed back to the API: the
            # request would be rejected and the answer lost.
            bogus = _NativeStubProvider("definitely_not_a_real_tool")
            res2 = await chat_with_tools(
                bogus, [{"role": "user", "content": "x"}],
                system_prompt="test", max_tool_rounds=2)
            check("an unknown tool name is not echoed to the API",
                  not any(m.get("tool_calls") for m in bogus.seen[-1]),
                  "the bogus call leaked into history")
            check("an unknown tool degrades gracefully",
                  "[Provider error" not in str(res2.get("response")),
                  str(res2.get("response"))[:150])

            # A refusal must NOT carry an instruction to retry. The old
            # contract was the reverse — the note told the model to call again
            # with confirm=true, and that call was taken as the approval.
            from backend.skills.tool_loop import _tool_result_message
            refusal = approval.refusal(SID, "test fixture", "echo")
            note = _tool_result_message(echo_tool, refusal)
            check("a refusal does not tell the model to pass confirm=true",
                  "call this exact tool again" not in note["content"].lower(),
                  note["content"][:200])
            check("the refusal names this exact tool",
                  echo_tool in note["content"], note["content"][:200])
            check("the refusal tells the model to stop",
                  "stop" in note["content"].lower(), note["content"][:200])

            approval.revoke(SID)
            mcp_manager.update(SID, {"trusted": False})
            soft = approval.refusal(SID, "test fixture", "echo")["message"]
            check("the refusal says the user is being asked",
                  "approval" in soft.lower() and "consent" in soft.lower(),
                  soft[:200])

            # A provider that keeps trying to get past the gate must not be
            # given a second round to do it in. It asks again from round 2 with
            # confirm=true — the exact self-approval attempt — and the turn
            # should already have ended after the first refusal.
            class _RetryStubProvider:
                provider_id = "openai"
                provider_name = "stub"

                def __init__(self, tool_name: str):
                    self.tool_name = tool_name
                    self.round = 0

                async def chat(self, messages, model=None, max_tokens=4096,
                               temperature=0.7, tools=None):
                    from backend.providers.base import ProviderResult
                    self.round += 1
                    arguments = '{"text": "pineapple"}'
                    if self.round > 1:
                        arguments = '{"text": "pineapple", "confirm": true}'
                    # It never answers in plain text: every round tries the
                    # tool again, so if the loop gave it another round it
                    # would take it.
                    return ProviderResult(
                        ok=True, model="stub", response="",
                        tool_calls=[{
                            "id": f"call_{self.round}", "type": "function",
                            "function": {"name": self.tool_name,
                                         "arguments": arguments},
                        }])

            approval.revoke(SID)
            retrier = _RetryStubProvider(f"mcp__{SID}__echo")
            res3 = await chat_with_tools(
                retrier, [{"role": "user", "content": "echo it"}],
                system_prompt="test", max_tool_rounds=4)
            rounds_used = [t.get("tool") for t in res3.get("tool_results") or []]
            print("self-approve rounds:", rounds_used, retrier.round)
            check("the loop does NOT give the model a second round",
                  retrier.round == 1, f"rounds={retrier.round}")
            check("only one call was attempted",
                  len(rounds_used) == 1, str(rounds_used))
            check("the turn ended waiting for the user",
                  res3.get("awaiting_answer") is True,
                  str(res3.get("awaiting_answer")))
            check("the turn reports it needs approval",
                  res3.get("requires_approval") is True,
                  str(res3.get("requires_approval"))[:150])
            check("no confirm=true call reached the server",
                  not any(t.get("success") for t in
                          (res3.get("tool_results") or [])),
                  json.dumps(res3.get("tool_results"))[:200])
        finally:
            forge_module.skill_forge.forge = original_forge
            config._data["skills"]["market_search"] = market


        # ---- 9b. SSE framing -------------------------------------------
        # The streamable-HTTP layer of the official MCP Python SDK is built on
        # `sse-starlette`, whose DEFAULT_SEPARATOR is CRLF. parse_sse split on
        # "\n\n" alone, so a CRLF-framed body had no separator: every event
        # collapsed into one block whose joined `data:` lines were not valid
        # JSON, and all of them were dropped in silence. A single-event stream
        # survived by accident, which is why a two-event one is the test.
        #
        # The fixtures are built the way `sse_starlette.ServerSentEvent.encode`
        # builds them (`data: <chunk><sep>` per line, then a bare `<sep>`), so
        # the test tracks that library rather than our idea of the format.
        def _sse(data, sep):
            out = ""
            for line in str(data).splitlines() or [""]:
                out += f"data: {line}{sep}"
            return out + sep

        _a = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"a": 1}})
        _n = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"})
        for _label, _sep in (("CRLF", "\r\n"), ("LF", "\n"), ("CR", "\r")):
            _body = _sse(_a, _sep) + _sse(_n, _sep)
            _got = mcp_protocol.parse_sse(_body)
            check(f"{_label}-framed SSE parses both messages",
                  len(_got) == 2,
                  f"got {len(_got)} of 2 — a server framing with {_label!r} "
                  f"would be unusable")

        _single = mcp_protocol.parse_sse(_sse(_a, "\r\n"))
        check("a single CRLF event still parses",
              len(_single) == 1 and _single[0].get("result") == {"a": 1},
              str(_single))

        check("a multi-line data payload is joined, not split",
              mcp_protocol.parse_sse(
                  'data: {"jsonrpc":"2.0",\r\n'
                  'data: "id":1,"result":{"a":1}}\r\n\r\n')[0]["id"] == 1,
              "SSE joins multiple data: lines in one event with a newline")

        # ---- 9c. environment isolation ---------------------------------
        # A stdio server is third-party code running with the user's
        # permissions, and the agent can acquire one on its own. It used to be
        # started with `dict(os.environ)` — every API key, cloud credential and
        # other server's secret in this process. These checks hold the
        # allowlist in place, since the failure is silent by nature: nothing
        # breaks when a server can read a key it should not have.
        from backend.mcp_client.stdio import McpStdioClient

        _probe = McpStdioClient("probe", {"command": ["cmd"], "env": {"DECLARED": "1"}})
        os.environ["ADDED_CHECK_CANARY"] = "must-not-reach-the-child"
        try:
            _child = _probe._env()
        finally:
            os.environ.pop("ADDED_CHECK_CANARY", None)

        check("a parent-only variable does not reach a stdio server",
              "ADDED_CHECK_CANARY" not in _child,
              "the child inherited the parent environment; a server can read "
              "this process's credentials")
        check("no credential-shaped variable reaches a stdio server",
              not [k for k in _child
                   if k.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))],
              f"leaked: {[k for k in _child if k.upper().endswith(('_API_KEY', '_TOKEN', '_SECRET'))]}")
        check("PATH still reaches the child",
              bool(_child.get("PATH") or _child.get("Path")),
              "a server cannot find its own runtime without PATH")

        # ---- 9d. an approval must not survive a repoint -----------------
        # The approval key is `server_id::tool`, and the id is a label the
        # user picked. It does not change when the thing behind it does, so
        # approving a tool on a trusted server and then repointing that server
        # at somebody else's left the grant standing for whatever now answered
        # there — and nothing asked again, because nothing about the name
        # changed. These hold the revocation in place, and just as importantly
        # hold it to *only* the endpoint: a rename or a timeout must not silently
        # throw away a standing approval the user meant to keep.
        from backend.mcp_client import approval as _approval

        check("a repoint produces a different endpoint",
              _approval.endpoint_of({"transport": "http",
                                     "url": "https://good.example"}) !=
              _approval.endpoint_of({"transport": "http",
                                     "url": "https://other.example"}),
              "the endpoint cannot tell two servers apart")
        check("a rename does not change the endpoint",
              _approval.endpoint_of({"transport": "http",
                                     "url": "https://a.example", "name": "x"}) ==
              _approval.endpoint_of({"transport": "http",
                                     "url": "https://a.example", "name": "y"}),
              "a rename would revoke a standing approval")
        check("a changed stdio argument changes the endpoint",
              _approval.endpoint_of({"transport": "stdio", "command": "node",
                                     "args": ["safe.js"]}) !=
              _approval.endpoint_of({"transport": "stdio", "command": "node",
                                     "args": ["evil.js"]}),
              "a swapped script keeps the approval")

        _saved_servers = mcp_manager._config_servers()
        try:
            mcp_manager.save_servers([
                {"id": "f4-http", "name": "F4", "transport": "http",
                 "url": "https://good.example/mcp", "timeout_s": 30},
                {"id": "f4-stdio", "name": "F4S", "transport": "stdio",
                 "command": "node", "args": ["safe.js"], "timeout_s": 30},
            ])
            _approval.approve("f4-http", "send")
            check("the tool starts approved",
                  _approval.is_approved("f4-http", "send"))
            mcp_manager.update("f4-http", {"url": "https://other.example/mcp"})
            check("repointing the server revokes the approval",
                  not _approval.is_approved("f4-http", "send"),
                  "an approval outlived the server it was granted to")

            _approval.approve("f4-http", "send")
            mcp_manager.update("f4-http", {"name": "Renamed"})
            check("a rename keeps the approval",
                  _approval.is_approved("f4-http", "send"),
                  "a cosmetic edit dropped a standing approval")

            _approval.approve("f4-stdio", "run")
            mcp_manager.update("f4-stdio", {"args": ["evil.js"]})
            check("swapping a stdio script revokes the approval",
                  not _approval.is_approved("f4-stdio", "run"),
                  "the approval followed the name, not the program")
        finally:
            mcp_manager.save_servers(_saved_servers)
            _approval.revoke("f4-http")
            _approval.revoke("f4-stdio")

        # ---- 9e. colliding tool names all stay reachable -----------------
        # `_sanitise` maps every punctuation character to `_` and cuts at 48,
        # so `my.tool`, `my/tool`, `my-tool` and `my_tool` all reduce to the
        # same string. Registering them under one name left one reachable and
        # no hint that the others existed — the model called a tool and a
        # different one ran, or none did, depending on registration order.
        # These hold the second tool in place, and hold it to the *right*
        # tool, since a distinct name that maps to the wrong handler is no
        # better than a collision.
        from backend.mcp_client.manager import (
            McpManager, McpServerState, skill_name, disambiguated_name)

        _taken: set = set()
        _first = disambiguated_name("s", "my.tool", _taken)
        _taken.add(_first)
        _second = disambiguated_name("s", "my/tool", _taken)
        check("a colliding tool gets a different name",
              _first != _second,
              f"both sanitised to {_first!r}")
        check("the first claimant keeps the plain name",
              _first == skill_name("s", "my.tool"), _first)
        check("the suffixed name is stable across calls",
              disambiguated_name("s", "my/tool", {_first}) == _second,
              "the name would change between reconnects")

        _mgr = McpManager()
        _state = McpServerState({"id": "f3", "name": "F3", "transport": "stdio",
                                 "command": "x", "trusted": True})
        _state.tools = [
            {"name": "my.tool", "description": "dotted"},
            {"name": "my/tool", "description": "slashed"},
            {"name": "my-tool", "description": "dashed"},
            {"name": "my_tool", "description": "plain"},
        ]
        try:
            _mgr._register_tools(_state)
            check("every colliding tool is registered",
                  len(_state.registered) == 4,
                  f"got {len(_state.registered)}: {_state.registered}")
            check("the registered names are distinct",
                  len(set(_state.registered)) == 4,
                  f"got {_state.registered}")
            _live = [s.name for s in skill_registry.list_all()
                     if s.category == "mcp" and s.name.startswith("mcp__f3__")]
            check("every colliding tool is reachable",
                  len(_live) == 4,
                  f"{len(_live)} of 4 reachable: {sorted(_live)}")
            # The name the model sees must dispatch to the tool it was given
            # for, not to whichever tool happened to win the collision.
            _back = {}
            for _n in _state.registered:
                _srv, _tool = _mgr._skill_tools[_n]
                _back[_tool] = _n
            check("each exposed name maps back to its own tool",
                  len(set(_back.values())) == 4 and len(_back) == 4,
                  f"map: {_back}")
        finally:
            for _n in _state.registered:
                skill_registry.unregister(_n)


        for _name in ("TEMP", "TMP"):
            if _name in os.environ:
                check(f"{_name} still reaches the child", _name in _child,
                      "a server cannot write temp files without it")
        check("a server's own declared env still reaches it",
              _child.get("DECLARED") == "1",
              "declaring env is the supported way to give a server a secret")
        check("the allowlist is much smaller than the parent environment",
              len(_child) < len(os.environ),
              f"child={len(_child)} parent={len(os.environ)}")

        # ---- 9f. colliding tools, end to end through a real server --------
        # 9e proved the naming in isolation. This proves it against a server
        # that really advertises colliding names, through `connect()` — the
        # path the app takes. The isolation test could pass while the connect
        # path still dropped them.
        _saved = config.get("mcp", "servers", default=[])
        try:
            _spec = {
                "id": "f3e2e", "name": "Collision fixture",
                "transport": "stdio",
                "command": [PY, "-s", SERVER], "enabled": True,
                "trusted": True, "timeout_s": 10,
                "env": {"MCP_FIXTURE_COLLIDE": "1"},
            }
            config.set("mcp", "servers", value=[_spec])
            _conn = await mcp_manager.connect("f3e2e")
            check("the colliding server connects", _conn.get("success"),
                  str(_conn.get("error")))
            _names = [s.name for s in skill_registry.list_all()
                      if s.category == "mcp"
                      and s.name.startswith("mcp__f3e2e__collide")]
            check("all three colliding tools are reachable end to end",
                  len(_names) == 3,
                  f"got {len(_names)}: {sorted(_names)}")
            # And each one dispatches to the tool whose name it carries.
            _replies = {}
            for _n in sorted(_names):
                _res = await skill_registry.execute(_n, {})
                _text = str((_res.data or {}).get("text")
                            or _res.summary or "")
                _replies[_n] = _text
                check(f"{_n} succeeded",
                      bool(_res.success),
                      f"{_res.error} — the name did not reach a tool")
            check("each exposed name reaches its own tool",
                  all("collide:" in t for t in _replies.values())
                  and len(set(_replies.values())) == 3,
                  f"{_replies}")
        finally:
            await mcp_manager.disconnect("f3e2e")
            config.set("mcp", "servers", value=_saved)

        # The escape hatch: a server needing something unanticipated can still
        # be given it, from Settings, without editing code.
        _before = config.get("mcp", "inherit_env", default=False)
        try:
            config.set("mcp", "inherit_env", value=True)
            _full = _probe._env()
            check("mcp.inherit_env restores the full environment",
                  len(_full) >= len(os.environ) - 1,
                  f"opted in but got {len(_full)} of {len(os.environ)}")
            config.set("mcp", "inherit_env", value=False)
            _filtered = _probe._env()
            check("turning inherit_env back off re-filters",
                  len(_filtered) < len(_full),
                  f"still {len(_filtered)} vars after opting out")
        finally:
            config.set("mcp", "inherit_env", value=bool(_before))

        # ---- 10. shutdown ----------------------------------------------
        state = mcp_manager._servers.get(SID)
        proc = getattr(state.client, "_proc", None) if state else None
        await mcp_manager.stop()
        check("tools removed on shutdown",
              not [s for s in skill_registry.list_all()
                   if s.category == "mcp"])
        if proc is not None:
            await asyncio.sleep(0.3)
            check("child process was reaped", proc.returncode is not None,
                  f"returncode={proc.returncode}")
    finally:
        config.set("mcp", "servers", value=original)

    print(f"\n{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)


asyncio.run(main())
