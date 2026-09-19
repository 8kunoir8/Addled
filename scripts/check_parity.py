"""One catalogue, four ways in: chat, the character, swarm agents and code.

The four entry points used to differ, and the difference was invisible from the
dashboard. Chat, the floating character, voice and the bots all ran through
`run_chat_pipeline`, so they had tools. A swarm agent called `provider.chat()`
itself and had none — it displayed a tools badge reading "chat", which is not a
skill, because `spawn()` defaulted to `tools or ["chat"]` and `run_task` never
read `tools` at all. The Code page's "Ask the model" box also called the
provider directly, so it could not read a second file before answering.

What is asserted here is that all four go through the one pipeline, that a
caller's tool subset really narrows the catalogue it asks for, and that the
defaults still leave normal chat alone. Each assertion is about behaviour that
would silently regress: a filtered catalogue that quietly returns everything
looks identical from the chat page.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_parity.py
"""

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


class _Result:
    """The shape every provider's chat() returns."""

    def __init__(self, text):
        self.ok = True
        self.response = text
        self.tokens_in = 1
        self.tokens_out = 1
        self.tool_calls = []
        self.reasoning_content = ""
        self.error = ""


class FakeProvider:
    """Replies from a script and records what it was handed.

    `calls` keeps the kwargs of every chat() call, which is how the tool
    subset is observed: the filter has to reach the provider's payload, not
    just the registry.
    """

    supports_vision = False

    def __init__(self, replies, provider_id="mock"):
        self.provider_id = provider_id
        self.replies = list(replies)
        self.calls = []
        self.payloads = []

    async def chat(self, messages, **kwargs):
        self.payloads.append(messages)
        self.calls.append(kwargs)
        text = self.replies.pop(0) if self.replies else ""
        return _Result(text)


def tool_block(name, params):
    """A tool call in the shape a prompt-tools provider returns it."""
    return ('```tool\n%s\n```'
            % json.dumps({"tool": name, "params": params}))


async def run():
    from backend import ws_server
    from backend.providers import registry as provider_registry
    from backend.skills.registry import skill_registry
    from backend.skills import tool_loop
    from backend.swarm.orchestrator import swarm
    from backend.memory.chat_history import chat_history

    # Procedure learning writes to the user's SOP store from inside a real
    # reply. It is not what this suite is about.
    try:
        import backend.sop.learn as sop_learn
        sop_learn.record_run = lambda *a, **k: None
    except Exception:
        pass

    ws_server._register_default_handlers()
    handlers = ws_server._server._handlers
    ws = object()

    def history_count():
        chat_history._load()
        return sum(len(c.get("messages", []))
                   for c in chat_history._data["conversations"].values())

    # ---- 1. the registry filter ------------------------------------------
    everything = skill_registry.enabled_list_all()
    names_everything = [s.name for s in everything]
    check("the skill catalogue is populated", len(everything) > 5,
          f"{len(everything)} skills")

    def openai_names(payload):
        return [t.get("function", {}).get("name") for t in payload]

    filtered = skill_registry.to_openai_tools({"read_file"})
    check("a filtered catalogue returns only the named tool",
          openai_names(filtered) == ["read_file"], str(filtered)[:200])
    check("no filter still returns every enabled skill",
          len(skill_registry.to_openai_tools()) == len(everything),
          f"{len(skill_registry.to_openai_tools())} vs {len(everything)}")
    check("an empty filter returns no tools, not all of them",
          skill_registry.to_openai_tools(set()) == [],
          "an empty set was treated as no filter")

    other = next((n for n in names_everything if n != "read_file"), None)
    text = skill_registry.to_prompt_tools({"read_file"})
    check("a filtered prompt catalogue names the tool it kept",
          "read_file(" in text, text[:200])
    check("and does not name one it dropped",
          other is not None and f"\n{other}(" not in text,
          f"{other} was listed anyway")
    check("an empty prompt catalogue lists no tools",
          "\n" in skill_registry.to_prompt_tools(set())
          and "(" not in skill_registry.to_prompt_tools(set()),
          "tools were listed for an empty filter")

    # ---- 2. the filter reaches the provider ------------------------------
    native = FakeProvider(["hello"], provider_id="openai")
    await tool_loop.chat_with_tools(
        provider=native, messages=[{"role": "user", "content": "hi"}],
        system_prompt="sys", tools=["read_file"])
    check("a native provider is asked for the filtered catalogue",
          openai_names(native.calls[0].get("tools") or []) == ["read_file"],
          str(openai_names(native.calls[0].get("tools") or []))[:200])

    native_all = FakeProvider(["hello"], provider_id="openai")
    await tool_loop.chat_with_tools(
        provider=native_all, messages=[{"role": "user", "content": "hi"}])
    check("with no filter the native provider gets every enabled skill",
          len(native_all.calls[0].get("tools") or []) == len(everything),
          f"{len(native_all.calls[0].get('tools') or [])} tools")

    prompt_provider = FakeProvider(["hello"], provider_id="mock")
    await tool_loop.chat_with_tools(
        provider=prompt_provider, messages=[{"role": "user", "content": "hi"}],
        tools=["read_file"])
    # The catalogue is its own message before the question now, so the payload is
    # searched as a whole rather than only its last message.
    sent = "\n".join(str(m.get("content") or "")
                     for m in prompt_provider.payloads[0])
    check("a prompt-tools provider gets the filtered catalogue in its prompt",
          "read_file(" in sent, sent[-400:])
    check("and no skill outside the subset is offered to it",
          other is None or f"\n{other}(" not in sent, sent[-400:])

    # ---- 3. a tool really executes --------------------------------------
    # Whether a skill runs on this machine depends on the display and the
    # file-access mode, so execution is recorded rather than performed here;
    # one real skill is run at the end of the section so the recorder is not
    # the only evidence.
    executed = []
    real_execute = tool_loop.execute_skill

    async def spy(name, params, provider):
        executed.append((name, params))
        return {"success": True, "data": {"spied": name}, "error": None,
                "forged": False}

    tool_loop.execute_skill = spy
    try:
        spy_provider = FakeProvider(
            [tool_block("read_file", {"path": "anything"}),
             "I read it."], provider_id="mock")
        spy_result = await tool_loop.chat_with_tools(
            provider=spy_provider, messages=[{"role": "user", "content": "read"}])
        check("a tool the model asks for is executed",
              bool(executed) and executed[0][0] == "read_file",
              str(executed)[:200])
        check("and its result is reported back",
              len(spy_result.get("tool_results") or []) == 1,
              str(spy_result)[:200])
        check("the result reaches the provider",
              "spied" in json.dumps(spy_provider.payloads[-1]),
              "no tool output was sent back")
    finally:
        tool_loop.execute_skill = real_execute

    live = next((n for n in ("get_screen_size", "get_clipboard", "list_windows")
                 if skill_registry.get(n) and skill_registry.is_enabled(n)), None)
    if live is None:
        print("  (skipped the live skill run: no path-free probe is enabled)")
    else:
        live_provider = FakeProvider(
            [tool_block(live, {}), "Done."], provider_id="mock")
        live_result = await tool_loop.chat_with_tools(
            provider=live_provider, messages=[{"role": "user", "content": "go"}])
        first = (live_result.get("tool_results") or [{}])[0]
        check(f"the real skill '{live}' runs through the loop",
              first.get("success") is True, str(first)[:300])

    # ---- 3. chat's defaults are unchanged --------------------------------
    signature = inspect.signature(ws_server.run_chat_pipeline)
    inner = inspect.signature(ws_server._run_chat_pipeline_inner)
    for name, default in (("persona", None), ("tools", None), ("record", True),
                          ("announce", True), ("max_tool_rounds", None),
                          ("force_role", None)):
        param = signature.parameters.get(name)
        check(f"run_chat_pipeline has a '{name}' option",
              param is not None, "missing")
        if param is not None:
            check(f"'{name}' defaults to {default!r}",
                  param.default is default or param.default == default,
                  f"default is {param.default!r}")
            check(f"'{name}' is keyword-only, so positional calls still work",
                  param.kind is inspect.Parameter.KEYWORD_ONLY, str(param.kind))
    check("the inner pipeline keeps the same options",
          all(n in inner.parameters for n in
              ("persona", "tools", "record", "max_tool_rounds", "force_role")),
          "options missing from _run_chat_pipeline_inner")
    try:
        signature.bind("a message")
        signature.bind("a message", {"maxToolRounds": 5})
        check("existing call sites still bind positionally", True, "")
    except TypeError as e:
        check("existing call sites still bind positionally", False, str(e))

    # A real chat-shaped turn: no persona, no tool filter, nothing recorded.
    fake = FakeProvider(["a plain answer"], provider_id="mock")
    provider_registry.get_provider = lambda: fake
    before = history_count()
    result = await ws_server.run_chat_pipeline(
        "just a question", record=False, announce=False)
    check("the default path still answers",
          result.get("response") == "a plain answer", str(result)[:200])
    check("and is offered every enabled skill",
          any("read_file(" in str(m.get("content") or "")
              for m in fake.payloads[0]),
          "the chat path lost its tools")
    check("record=False keeps the turn out of the chat history",
          result.get("conversationId") is None, str(result)[:200])
    check("and appends nothing to it", history_count() == before,
          f"{history_count()} messages, was {before}")

    # ---- 5. the character path -------------------------------------------
    # Read rather than imported: backend.main builds the Qt character.
    main_src = (Path(ROOT) / "backend" / "main.py").read_text(
        encoding="utf-8", errors="replace")
    check("the character runs its prompt through the shared pipeline",
          "run_chat_pipeline(text)" in main_src,
          "the avatar path no longer uses the shared pipeline unchanged")

    # ---- 6. a swarm agent uses the pipeline, with tools ------------------
    executed.clear()
    tool_loop.execute_skill = spy
    try:
        agent_provider = FakeProvider(
            [tool_block("file_info", {"path": "helper.py"}),
             "The helper module exists."], provider_id="mock")
        # Deliberately empty: if run_task stopped passing its own provider
        # through, the agent would lose its script and fail loudly here.
        provider_registry.get_provider = lambda: FakeProvider([], provider_id="mock")

        agent = swarm.spawn("parity-checker", "analyst")
        check("a spawned agent defaults to every tool, not to a fake name",
              agent.tools is None, str(agent.tools))
        listing = [a for a in swarm.list_agents() if a["id"] == agent.id]
        check("and the Agents page can say so",
              bool(listing) and listing[0].get("allTools") is True,
              str(listing)[:200])

        before = history_count()
        ran = await swarm.run_agent(agent.id, "Does helper.py exist?",
                                    agent_provider)
        check("the swarm agent's task succeeds", ran.get("success") is True,
              str(ran)[:300])
        check("it ran a tool instead of only talking",
              bool(executed) and executed[0][0] == "file_info",
              str(executed)[:200])
        check("and reports how many it used", ran.get("toolResults", 0) >= 1,
              f"toolResults={ran.get('toolResults')}")
        check("the tool result reaches the agent's next request",
              "spied" in json.dumps(agent_provider.payloads[-1]),
              "no tool output was sent back")
        check("an agent's task is not a conversation",
              history_count() == before,
              f"{history_count()} messages, was {before}")

        # ---- 7. stopping an agent actually stops it ----------------------
        class SlowProvider(FakeProvider):
            async def chat(self, messages, **kwargs):
                await asyncio.sleep(30)
                return _Result("too late")

        slow = SlowProvider([], provider_id="mock")
        slow_agent = swarm.spawn("slow-coach", "general")
        running = asyncio.create_task(
            swarm.run_agent(slow_agent.id, "take your time", slow))
        await asyncio.sleep(0.3)
        check("a running task is registered so that it can be cancelled",
              slow_agent.id in swarm._tasks, "the task was never registered")
        swarm.stop(slow_agent.id)
        stopped = await asyncio.wait_for(running, timeout=10)
        check("stopping an agent returns instead of running on",
              stopped.get("error") == "Stopped", str(stopped)[:200])
        check("and the task table is cleaned up",
              slow_agent.id not in swarm._tasks, "still registered")
    finally:
        tool_loop.execute_skill = real_execute

    # ---- 8. the code editor reads with tools -----------------------------
    edit = handlers.get("code.edit")
    check("the backend registers 'code.edit'", edit is not None, "missing")
    if edit is None:
        return

    tmp = Path(tempfile.mkdtemp(prefix="parity_ws_"))
    ws_dir = tmp / "project"
    (ws_dir / "pkg").mkdir(parents=True)
    target = ws_dir / "main.py"
    target.write_text("def run():\n    return 1\n", encoding="utf-8")
    (ws_dir / "pkg" / "helper.py").write_text("def helper():\n    return 41\n",
                                               encoding="utf-8")
    # A second file, so the failure cases below cannot be confused with the
    # pending edit the successful case leaves behind.
    second = ws_dir / "second.py"
    second.write_text("value = 2\n", encoding="utf-8")
    wp = str(ws_dir)

    check("the code editor's tool subset is read-only",
          not (set(ws_server._CODE_EDIT_TOOLS)
               & {"write_file", "delete_file", "create_dir", "code_apply",
                  "code_edit", "set_clipboard"}),
          "a writing tool reached the editor")
    unknown = [n for n in ws_server._CODE_EDIT_TOOLS
               if n not in {s.name for s in skill_registry.list_all()}]
    check("every tool it asks for is a real skill", not unknown,
          f"these would be dropped silently: {unknown}")

    edited = "def run():\n    return 41\n"
    fake_code = FakeProvider(
        [tool_block("code_read", {"path": "pkg/helper.py"}), edited],
        provider_id="mock")
    provider_registry.get_provider = lambda: fake_code
    ws_server._pending_edits.pop(f"{wp}::main.py", None)

    executed.clear()
    tool_loop.execute_skill = spy
    try:
        r = await edit({"workspaceId": wp, "filePath": "main.py",
                        "instruction": "make run() return what helper() returns"},
                       ws)
    finally:
        tool_loop.execute_skill = real_execute
    check("the editor proposes a change", r.get("status") == "pending",
          str(r)[:300])
    check("with a diff to show", bool(r.get("diffs")), str(r)[:200])
    check("and an id to apply", bool(r.get("editId")), str(r)[:200])
    check("it can read the project before answering",
          bool(executed) and executed[0][0] == "code_read", str(executed)[:200])
    check("and reports how many tools it used", r.get("toolCalls", 0) >= 1,
          f"toolCalls={r.get('toolCalls')}")
    check("the proposal holds the new content",
          (ws_server._pending_edits.get(f"{wp}::main.py") or "").strip()
          == edited.strip(),
          str(ws_server._pending_edits.get(f"{wp}::main.py"))[:200])
    check("but nothing was written without approval",
          target.read_text(encoding="utf-8") == "def run():\n    return 1\n",
          "the file changed on its own")

    first_prompt = fake_code.payloads[0][-1]["content"]
    check("the editor is told which file it is changing",
          "main.py" in first_prompt, "no path in the prompt")
    check("its output contract arrives with the file, not buried in the "
          "system prompt", "nothing else" in first_prompt,
          "the contract was not in the user turn")
    check("and it is not offered the whole catalogue",
          "screenshot(" not in first_prompt,
          "every skill was offered to the editor")

    # An oversized file must be refused, not silently truncated: the old code
    # sent the first 3000 characters and diffed the answer against the whole
    # file, so everything past the cut read as deleted.
    big = ws_dir / "big.py"
    big.write_text("x = 1\n" * 4000, encoding="utf-8")
    r = await edit({"workspaceId": wp, "filePath": "big.py",
                    "instruction": "rename x"}, ws)
    check("an oversized file is refused rather than truncated",
          r.get("status") == "too_large", str(r)[:200])
    check("the message says what the limit is",
          "character" in (r.get("message") or ""), str(r)[:200])
    check("no unusable edit is left pending for it",
          f"{wp}::big.py" not in ws_server._pending_edits,
          "an edit with no content was stored")
    check("and the oversized file is untouched",
          big.read_text(encoding="utf-8") == "x = 1\n" * 4000,
          "the file changed")

    # The editor still refuses to work outside the workspace, now that its
    # tools can read: the tools inherit the same containment.
    r = await edit({"workspaceId": wp, "filePath": "../outside.py",
                    "instruction": "anything"}, ws)
    check("an outside path is still refused", r.get("status") == "refused",
          str(r)[:200])

    # ---- 9. a failed reply is an error, not a code proposal --------------
    down = FakeProvider(["[Not connected: nothing is configured]"],
                        provider_id="mock")
    provider_registry.get_provider = lambda: down
    ws_server._pending_edits.pop(f"{wp}::second.py", None)
    r = await edit({"workspaceId": wp, "filePath": "second.py",
                    "instruction": "do something"}, ws)
    check("a provider failure is an error, not a code proposal",
          r.get("status") == "error", str(r)[:200])
    check("and nothing is left pending from it",
          f"{wp}::second.py" not in ws_server._pending_edits, str(r)[:200])

    empty = FakeProvider([""], provider_id="mock")
    provider_registry.get_provider = lambda: empty
    r = await edit({"workspaceId": wp, "filePath": "second.py",
                    "instruction": "do something"}, ws)
    check("an empty reply is an error, not an empty file",
          r.get("status") == "error", str(r)[:200])
    check("and the file it would have replaced is untouched",
          second.read_text(encoding="utf-8") == "value = 2\n",
          "the file changed")

    unchanged = FakeProvider(["value = 2\n"], provider_id="mock")
    provider_registry.get_provider = lambda: unchanged
    ws_server._pending_edits.pop(f"{wp}::second.py", None)
    r = await edit({"workspaceId": wp, "filePath": "second.py",
                    "instruction": "do something"}, ws)
    check("an unchanged answer is reported as unchanged, not as an edit",
          r.get("status") == "unchanged", str(r)[:200])
    check("and nothing is left pending for an unchanged answer",
          f"{wp}::second.py" not in ws_server._pending_edits, str(r)[:200])

    # ---- 10. a restricted chat turn still works --------------------------
    limited = FakeProvider(["34 bytes"], provider_id="mock")
    provider_registry.get_provider = lambda: limited
    r = await ws_server.run_chat_pipeline("what size is this?",
                                          tools=["file_info"], record=False,
                                          announce=False)
    check("a restricted chat turn still answers",
          r.get("response") == "34 bytes", str(r)[:200])


async def main():
    try:
        await asyncio.wait_for(run(), timeout=600)
    except Exception as e:
        import traceback
        traceback.print_exc()
        fails.append(f"the suite raised: {e!r}")
    if fails:
        print(f"FAIL: {len(fails)} parity check(s) failed")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: chat, character, swarm and code share one tool catalogue")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
