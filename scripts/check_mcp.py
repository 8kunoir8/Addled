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
        first = await skill_registry.execute(f"mcp__{SID}__echo",
                                            {"text": "hello"})
        print("unapproved call:", first.success,
              str(first.data.get("requires_approval")))
        check("unapproved call is refused", first.success is False,
              str(first.data)[:200])
        check("refusal asks for approval",
              first.data.get("requires_approval") is True, str(first.data)[:200])
        check("refusal tells the model to confirm",
              "confirm=true" in str(first.data.get("message")), "no hint")
        check("refusal did not reach the server",
              not approval.is_approved(SID, "echo"))

        # "user agreed" -> model retries with confirm
        second = await skill_registry.execute(
            f"mcp__{SID}__echo", {"text": "hello", "confirm": True})
        print("confirmed call:", second.success, repr(second.data.get("text")))
        check("confirmed call succeeds", second.success, str(second.data)[:200])
        check("echo returned the text", second.data.get("text") == "hello",
              str(second.data.get("text")))
        check("confirm was stripped before forwarding",
              "confirm" not in str(second.data.get("structured", {})), "leaked")

        third = await skill_registry.execute(f"mcp__{SID}__add",
                                             {"a": 17, "b": 25})
        check("approval is per tool, not per server",
              third.success is False and bool(
                  third.data.get("requires_approval")),
              str(third.data)[:200])
        approved_add = await skill_registry.execute(
            f"mcp__{SID}__add", {"a": 17, "b": 25, "confirm": True})
        check("confirming the second tool works", approved_add.success,
              str(approved_add.data)[:200])
        check("add returned 42", str(approved_add.data.get("text")) == "42",
              str(approved_add.data.get("text")))

        # ---- 4. tool-level errors are soft ------------------------------
        boom = await skill_registry.execute(f"mcp__{SID}__boom", {})
        check("a failing tool reports failure", boom.success is False,
              str(boom.data)[:200])
        check("failure carries the server message",
              "boom" in str(boom.data.get("error")), str(boom.data)[:200])

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
        slow = await skill_registry.execute(f"mcp__{SID}__slow",
                                           {"secs": 2, "confirm": True})
        check("slow tool times out instead of hanging", slow.success is False,
              str(slow.data)[:200])
        check("timeout is reported clearly",
              "timed out" in str(slow.data.get("error")), str(slow.data)[:200])
        # The fixture is single-threaded and still sleeping off that call, so
        # let it finish and then confirm the connection recovered.
        mcp_manager.update(SID, {"timeout_s": 10})
        await asyncio.sleep(2.5)
        after = await skill_registry.execute(
            f"mcp__{SID}__echo", {"text": "still here", "confirm": True})
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

            # An approval refusal must reach the model with its instruction
            # attached, and the loop must run the retry round instead of
            # summarizing early — the reason a local or hybrid model reported
            # the trust gate and then gave up.
            from backend.skills.tool_loop import _tool_result_message
            refusal = approval.refusal(SID, "test fixture", "echo")
            note = _tool_result_message(echo_tool, refusal)
            check("a refusal carries its instruction to the model",
                  "confirm=true" in note["content"], note["content"][:200])
            check("the refusal names this exact tool",
                  echo_tool in note["content"], note["content"][:200])

            approval.revoke(SID)
            mcp_manager.update(SID, {"trusted": False})
            soft = approval.refusal(SID, "test fixture", "echo")["message"]
            check("the refusal steers away from trust-check tools",
                  "trust-check" in soft, soft[:200])

            # The loop must allow the confirm retry rather than ending the turn
            # on the first refusal. A provider scripted to refuse-then-confirm
            # is the observable form of that behaviour.
            class _ConfirmStubProvider:
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
                    # Only round 3 is allowed to answer in plain text.
                    if self.round >= 3:
                        return ProviderResult(ok=True, model="stub",
                                              response="done")
                    return ProviderResult(
                        ok=True, model="stub", response="",
                        tool_calls=[{
                            "id": f"call_{self.round}", "type": "function",
                            "function": {"name": self.tool_name,
                                         "arguments": arguments},
                        }])

            approval.revoke(SID)
            confirmer = _ConfirmStubProvider(f"mcp__{SID}__echo")
            res3 = await chat_with_tools(
                confirmer, [{"role": "user", "content": "echo it"}],
                system_prompt="test", max_tool_rounds=4)
            rounds_used = [t.get("tool") for t in res3.get("tool_results") or []]
            print("confirm retry rounds:", rounds_used, confirmer.round)
            check("the loop retries after an approval refusal",
                  confirmer.round >= 3, f"rounds={confirmer.round}")
            check("the confirmed call actually ran",
                  any(t.get("success") for t in (res3.get("tool_results") or [])),
                  json.dumps(res3.get("tool_results"))[:200])
            check("the turn did not end on the refusal",
                  "couldn't run the tool" not in str(res3.get("response")),
                  str(res3.get("response"))[:150])
        finally:
            forge_module.skill_forge.forge = original_forge
            config._data["skills"]["market_search"] = market

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
