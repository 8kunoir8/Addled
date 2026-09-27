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
    from backend.swarm.orchestrator import SwarmOrchestrator as _SwarmOrchestrator
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

    # The call format rides the question as well as the catalogue. One message
    # away was far enough for a small model to stop calling tools: the same five
    # questions called one 4/10 times with the format only in the catalogue and
    # 10/10 with it on the question.
    format_provider = FakeProvider(["hello"], provider_id="mock")
    await tool_loop.chat_with_tools(
        provider=format_provider,
        messages=[{"role": "user", "content": "hi"}], tools=["read_file"])
    last = format_provider.payloads[0][-1]["content"]
    check("the call format is repeated on the question itself",
          "```tool" in last, last[-200:])
    check("with the question still first in that message",
          last.startswith("hi"), last[:80])

    tool_less = FakeProvider(["hello"], provider_id="mock")
    await tool_loop.chat_with_tools(
        provider=tool_less, messages=[{"role": "user", "content": "hi"}],
        tools=[])
    check("but a turn with no tools is told nothing about calling one",
          "```tool" not in tool_less.payloads[0][-1]["content"],
          str(tool_less.payloads[0][-1])[:200])

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

        # ---- 7b. a flow: agents collaborate in order ---------------------
        # Agents used to be islands. Each run_agent went through the pipeline
        # with record=False, so no history and no shared memory, and although
        # `agent.results` was recorded nothing ever read it — so even a
        # hand-off had no channel. run_flow is what makes "collaborate" true.
        # Cleared first: memory from an earlier test would otherwise arrive as
        # "work from earlier in this session" and make these assertions pass
        # for the wrong reason.
        swarm.clear_memory()
        seen: dict[str, str] = {}
        order: list[str] = []

        def _make_agent(name: str, reply: str):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                seen[name] = task
                order.append(name)
                return {"success": True, "response": reply, "agent": name}

            made.run_task = scripted
            return made

        planner = _make_agent("flow-planner", "PLAN-OUTPUT")
        coder = _make_agent("flow-coder", "CODE-OUTPUT")

        flow = await swarm.run_flow(
            [{"agentId": planner.id, "task": "plan the feature"},
             {"agentId": coder.id, "task": "implement the plan"}],
            provider=agent_provider, goal="ship the feature")
        check("a flow reports success", flow.get("success") is True,
              str(flow)[:300])
        check("it runs the steps in the order given",
              order == ["flow-planner", "flow-coder"], str(order))
        check("and returns one entry per step",
              len(flow.get("steps") or []) == 2, str(flow.get("steps"))[:200])
        check("the flow carries a final response from the last step",
              flow.get("response") == "CODE-OUTPUT", str(flow.get("response")))

        check("the first step is not shown anyone else's work",
              "PLAN-OUTPUT" not in seen.get("flow-planner", ""),
              seen.get("flow-planner", "")[:200])
        check("the second step sees the first step's output",
              "PLAN-OUTPUT" in seen.get("flow-coder", ""),
              seen.get("flow-coder", "")[:200])
        check("and knows which agent produced it",
              "flow-planner" in seen.get("flow-coder", ""),
              seen.get("flow-coder", "")[:200])
        check("the overall goal reaches every step",
              all("ship the feature" in text for text in seen.values()),
              str(seen)[:300])

        # A failure must stop the flow rather than let a later step build on
        # nothing.
        order.clear()
        failing = swarm.spawn("flow-breaks", "general")

        async def explode(task, provider=None):
            order.append("flow-breaks")
            return {"success": False, "error": "model exploded", "agent": "flow-breaks"}

        failing.run_task = explode
        after = swarm.spawn("flow-after", "general")

        async def should_not_run(task, provider=None):
            order.append("flow-after")
            return {"success": True, "response": "ran anyway", "agent": "flow-after"}

        after.run_task = should_not_run
        broken = await swarm.run_flow(
            [{"agentId": failing.id, "task": "doom"},
             {"agentId": after.id, "task": "build on doom",
              "dependsOn": [1]}],
            provider=agent_provider)
        check("a failing step fails the flow", broken.get("success") is False,
              str(broken)[:200])
        check("and nothing depending on it runs",
              order == ["flow-breaks"], str(order))
        check("the failure names the step that broke",
              "flow-breaks" in str(broken.get("error")), str(broken.get("error")))
        check("the steps that did run are still reported",
              len(broken.get("steps") or []) == 1,
              str(broken.get("steps"))[:200])

        # Bad input is refused with a reason, not a crash.
        for label, bad_steps in (
                ("an empty flow", []),
                ("a step with no task", [{"agentId": planner.id, "task": ""}]),
                ("an unknown agent", [{"agentId": "nope", "task": "x"}]),
        ):
            refused = await swarm.run_flow(bad_steps, provider=agent_provider)
            check(f"{label} is refused with a reason",
                  refused.get("success") is False and bool(refused.get("error")),
                  str(refused)[:200])

        # The shared transcript must be bounded, or a long flow crowds out the
        # step's own instructions on a small model.
        from backend.swarm import orchestrator as orch_mod
        loud = swarm.spawn("flow-loud", "general")

        async def verbose(task, provider=None):
            return {"success": True, "response": "Z" * 40000, "agent": "flow-loud"}

        loud.run_task = verbose
        await swarm.run_flow([{"agentId": loud.id, "task": "a"},
                              {"agentId": loud.id, "task": "b"}],
                             provider=agent_provider)
        check("the shared transcript is bounded",
              len(seen.get("flow-loud", "")) < orch_mod.FLOW_TRANSCRIPT_CHARS + 500,
              f"{len(seen.get('flow-loud', ''))} chars")

        # ---- 7c. shared memory survives across flows ---------------------
        # A flow's transcript lived only inside run_flow, so a *second* flow
        # ("now review what we built") could not see the first one's work.
        swarm.clear_memory()
        mem_plan = _make_agent("mem-planner", "MEM-PLAN-OUTPUT")
        await swarm.run_flow([{"agentId": mem_plan.id, "task": "plan v1"}],
                             provider=agent_provider)
        check("finished work is remembered", len(swarm._memory) == 1,
              f"{len(swarm._memory)} entries")

        seen.clear()
        mem_review = _make_agent("mem-reviewer", "MEM-REVIEW")
        await swarm.run_flow([{"agentId": mem_review.id, "task": "review it"}],
                             provider=agent_provider)
        check("a later flow can see an earlier flow's work",
              "MEM-PLAN-OUTPUT" in seen.get("mem-reviewer", ""),
              seen.get("mem-reviewer", "")[:200])
        check("and knows which agent produced it",
              "mem-planner" in seen.get("mem-reviewer", ""),
              seen.get("mem-reviewer", "")[:200])

        dropped = swarm.clear_memory()
        check("the memory can be cleared", dropped >= 1 and not swarm._memory,
              f"dropped={dropped} left={len(swarm._memory)}")

        # ---- 7d. fan-out and merge --------------------------------------
        # A step may name peers to work it at the same time. Two faults were
        # found by testing this: `len(peers) > 1` meant a single peer silently
        # fell through to the sequential branch and the two never learned about
        # each other, and the local branch skipped the peers entirely.
        import time as _time
        swarm.clear_memory()
        seen.clear()
        order.clear()

        def _timed_agent(name: str, reply: str, delay: float):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                seen[name] = task
                order.append(name)
                await asyncio.sleep(delay)
                return {"success": True, "response": reply, "agent": name}

            made.run_task = scripted
            return made

        class CloudProvider(FakeProvider):
            provider_id = "mock-cloud"

        cloud = CloudProvider([], provider_id="mock-cloud")
        lead = _timed_agent("fan-lead", "LEAD-ANSWER", 0.35)
        peer = _timed_agent("fan-peer", "PEER-ANSWER", 0.35)
        merger = _make_agent("fan-merger", "MERGED-ANSWER")

        started = _time.monotonic()
        fan = await swarm.run_flow(
            [{"agentId": lead.id, "task": "analyse",
              "parallel": [peer.id], "merge": merger.id}],
            provider=cloud, goal="fan out")
        elapsed = _time.monotonic() - started

        check("a fan-out flow succeeds", fan.get("success") is True,
              str(fan)[:300])
        check("one peer is enough to run in parallel",
              "fan-peer" in order, f"order={order}")
        check("the peers actually run concurrently",
              elapsed < 0.6, f"{elapsed:.2f}s for two 0.35s tasks")
        check("the lead is told who it is working alongside",
              "fan-peer" in seen.get("fan-lead", ""),
              seen.get("fan-lead", "")[:200])
        check("and the peer is told too",
              "fan-lead" in seen.get("fan-peer", ""),
              seen.get("fan-peer", "")[:200])
        check("the merge agent sees every parallel answer",
              "LEAD-ANSWER" in seen.get("fan-merger", "")
              and "PEER-ANSWER" in seen.get("fan-merger", ""),
              seen.get("fan-merger", "")[:300])
        check("the merged answer becomes the flow's result",
              fan.get("response") == "MERGED-ANSWER", str(fan.get("response")))
        check("the merge is marked apart from the raw answers",
              any(s.get("merged") for s in (fan.get("steps") or [])),
              str(fan.get("steps"))[:200])

        # A single-generation provider must still run every peer — in order.
        order.clear()
        seen.clear()

        class LocalProvider(FakeProvider):
            provider_id = "local"

        local = LocalProvider([], provider_id="local")
        l1 = _timed_agent("loc-1", "L1-ANSWER", 0.0)
        l2 = _timed_agent("loc-2", "L2-ANSWER", 0.0)
        sequential = await swarm.run_flow(
            [{"agentId": l1.id, "task": "same step", "parallel": [l2.id]}],
            provider=local)
        check("a local provider still runs the peers, in order",
              order == ["loc-1", "loc-2"], f"order={order}")
        check("and the local fan-out succeeds",
              sequential.get("success") is True, str(sequential)[:200])
        check("with no merge agent named nothing is merged",
              not any(s.get("merged") for s in (sequential.get("steps") or [])),
              str(sequential.get("steps"))[:200])

        swarm.clear_memory()

        # ---- 7e. a gate: rejected work goes back -------------------------
        # A step with `until` + `retry` is a review loop. Building it surfaced
        # a bug worth recording: the loop ran the producer only, so the
        # condition was compared against the producer's own output and
        # "contains PASS" could never be satisfied.
        swarm.clear_memory()
        gate_calls: list[str] = []

        def _gate_agent(name: str, replies: list[str]):
            made = swarm.spawn(name, "general")
            queue = list(replies)

            async def scripted(task, provider=None):
                gate_calls.append(name)
                text = queue.pop(0) if len(queue) > 1 else queue[-1]
                if name in ("gate-coder", "gate-coder2"):
                    seen[name] = task
                return {"success": True, "response": text, "agent": name}

            made.run_task = scripted
            return made

        # Passes on the first attempt: the producer must run exactly once.
        gate_calls.clear()
        seen.clear()
        g_prod = _gate_agent("gate-coder", ["code v1"])
        g_rev = _gate_agent("gate-reviewer", ["looks good PASS"])
        ok_flow = await swarm.run_flow(
            [{"agentId": g_prod.id, "task": "code it", "retry": g_rev.id,
              "until": {"contains": "PASS", "agent": g_rev.id,
                        "maxAttempts": 3}}],
            provider=agent_provider)
        check("a satisfied gate passes the flow", ok_flow.get("success") is True,
              str(ok_flow)[:200])
        check("and the producer runs only once",
              gate_calls.count("gate-coder") == 1, str(gate_calls))
        check("with the gate agent actually consulted",
              "gate-reviewer" in gate_calls, str(gate_calls))
        check("both the work and the verdict are reported",
              len(ok_flow.get("steps") or []) == 2,
              str(ok_flow.get("steps"))[:200])

        # Rejected, then accepted: the producer runs again with the feedback.
        gate_calls.clear()
        seen.clear()
        r_prod = _gate_agent("gate-coder2", ["bad code", "good code"])
        r_rev = _gate_agent("gate-reviewer2",
                            ["FAIL: no tests written", "good PASS"])
        retry_flow = await swarm.run_flow(
            [{"agentId": r_prod.id, "task": "code it", "retry": r_rev.id,
              "until": {"contains": "PASS", "agent": r_rev.id,
                        "maxAttempts": 3}}],
            provider=agent_provider)
        check("a rejected step is retried and can then pass",
              retry_flow.get("success") is True, str(retry_flow)[:200])
        check("the producer ran twice", gate_calls.count("gate-coder2") == 2,
              str(gate_calls))
        check("the reviewer's verdict reached the producer",
              "no tests written" in seen.get("gate-coder2", ""),
              seen.get("gate-coder2", "")[:200])
        check("the retry is marked with its attempt number",
              any((s.get("attempt") or 0) >= 2
                  for s in (retry_flow.get("steps") or [])),
              str(retry_flow.get("steps"))[:200])

        # Never satisfied: bounded, and reported with the real verdict.
        gate_calls.clear()
        x_prod = _gate_agent("gate-coder3", ["bad code"])
        x_rev = _gate_agent("gate-reviewer3", ["FAIL: still broken"])
        dead = await swarm.run_flow(
            [{"agentId": x_prod.id, "task": "code it", "retry": x_rev.id,
              "until": {"contains": "PASS", "agent": x_rev.id,
                        "maxAttempts": 2}}],
            provider=agent_provider)
        check("a gate that never passes fails the flow",
              dead.get("success") is False, str(dead)[:200])
        check("it stops at maxAttempts rather than looping",
              gate_calls.count("gate-coder3") == 2, str(gate_calls))
        check("and reports the reviewer's verdict, not the producer's",
              "still broken" in str(dead.get("error")),
              str(dead.get("error"))[:200])
        check("every attempt is kept for inspection",
              len(dead.get("steps") or []) == 4,
              str(dead.get("steps"))[:200])

        # notContains inverts the condition.
        n_prod = _gate_agent("gate-coder4", ["clean code"])
        n_rev = _gate_agent("gate-reviewer4", ["no problems found"])
        inverted = await swarm.run_flow(
            [{"agentId": n_prod.id, "task": "code it", "retry": n_rev.id,
              "until": {"notContains": "FAIL", "agent": n_rev.id,
                        "maxAttempts": 2}}],
            provider=agent_provider)
        check("notContains passes when the word is absent",
              inverted.get("success") is True, str(inverted)[:200])

        # A step with no gate must be untouched by all of this.
        gate_calls.clear()
        plain_a = _gate_agent("plain-a", ["P"])
        plain_b = _gate_agent("plain-b", ["C"])
        plain = await swarm.run_flow(
            [{"agentId": plain_a.id, "task": "plan"},
             {"agentId": plain_b.id, "task": "code"}],
            provider=agent_provider)
        check("a flow with no gate still works", plain.get("success") is True,
              str(plain)[:200])
        check("and is not marked with gate or attempt fields",
              not any(s.get("gate") or s.get("attempt")
                      for s in (plain.get("steps") or [])),
              str(plain.get("steps"))[:200])

        # Invalid gates are refused before any agent runs.
        guard_calls: list[str] = []

        def _spy_agent(name: str):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                guard_calls.append(name)
                return {"success": True, "response": "PASS", "agent": name}

            made.run_task = scripted
            return made

        guard_prod = _spy_agent("guard-producer")
        guard_gate = _spy_agent("guard-gate")
        for label, until in (
                ("both contains and notContains",
                 {"contains": "PASS", "notContains": "FAIL"}),
                ("neither contains nor notContains", {}),
                ("no retry agent",
                 {"contains": "PASS"}),
        ):
            bad_step = {"agentId": guard_prod.id, "task": "code",
                        "until": until}
            if label != "no retry agent":
                bad_step["retry"] = guard_gate.id
            refused = await swarm.run_flow([bad_step], provider=agent_provider)
            check(f"a gate with {label} is refused",
                  refused.get("success") is False and bool(refused.get("error")),
                  str(refused)[:200])
        check("and no agent ran while the input was invalid",
              guard_calls == [], str(guard_calls))

        # The gate shares the transcript, so the bound must still hold after
        # several attempts — 120k of output must not reach the next prompt.
        loud_prod = _gate_agent("gate-loud", ["Z" * 40000])
        loud_rev = _gate_agent("gate-loud-rev", ["FAIL"])
        await swarm.run_flow(
            [{"agentId": loud_prod.id, "task": "code", "retry": loud_rev.id,
              "until": {"contains": "PASS", "agent": loud_rev.id,
                        "maxAttempts": 3}}],
            provider=agent_provider)
        check("the transcript stays bounded across a retry loop",
              len(swarm.memory_text()) <= orch_mod.FLOW_TRANSCRIPT_CHARS,
              f"{len(swarm.memory_text())} chars")

        swarm.clear_memory()

        # ---- 7g. dependencies ------------------------------------------
        # Steps ran strictly in the order given. `dependsOn` (1-based step
        # numbers) lets a step wait for exactly the ones it needs, so an
        # independent step no longer waits behind an unrelated one above it.
        swarm.clear_memory()
        dep_order: list[str] = []

        def _dep_agent(name: str, ok: bool = True):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                dep_order.append(name)
                if not ok:
                    return {"success": False, "error": "boom", "agent": name}
                return {"success": True, "response": f"{name}-OUT",
                        "agent": name}

            made.run_task = scripted
            return made

        # A flow with no dependsOn must behave exactly as before.
        dep_order.clear()
        d_a = _dep_agent("dep-a")
        d_b = _dep_agent("dep-b")
        d_c = _dep_agent("dep-c")
        linear = await swarm.run_flow(
            [{"agentId": d_a.id, "task": "1"},
             {"agentId": d_b.id, "task": "2"},
             {"agentId": d_c.id, "task": "3"}], provider=agent_provider)
        check("a flow with no dependencies still runs in order",
              linear.get("success") is True
              and dep_order == ["dep-a", "dep-b", "dep-c"],
              f"{dep_order} {linear.get('error')}")

        # Listed out of order, dependencies force the real order.
        # step1 = chain-c (depends on 3), step2 = chain-b (depends on 1),
        # step3 = chain-a (no deps)  =>  chain-a, chain-c, chain-b
        dep_order.clear()
        c1 = _dep_agent("chain-c")
        c2 = _dep_agent("chain-b")
        c3 = _dep_agent("chain-a")
        chained = await swarm.run_flow(
            [{"agentId": c1.id, "task": "child", "dependsOn": [3]},
             {"agentId": c2.id, "task": "grandchild", "dependsOn": [1]},
             {"agentId": c3.id, "task": "root"}], provider=agent_provider)
        check("a dependency chain runs in dependency order, not listing order",
              chained.get("success") is True
              and dep_order == ["chain-a", "chain-c", "chain-b"],
              f"{dep_order} {chained.get('error')}")

        # A diamond: both middles wait for the root, the join waits for both.
        dep_order.clear()
        root = _dep_agent("dia-root")
        left = _dep_agent("dia-left")
        right = _dep_agent("dia-right")
        join = _dep_agent("dia-join")
        diamond = await swarm.run_flow(
            [{"agentId": root.id, "task": "root"},
             {"agentId": left.id, "task": "left", "dependsOn": [1]},
             {"agentId": right.id, "task": "right", "dependsOn": [1]},
             {"agentId": join.id, "task": "join", "dependsOn": [2, 3]}],
            provider=agent_provider)
        check("a diamond flow succeeds", diamond.get("success") is True,
              str(diamond.get("error"))[:200])
        check("the root runs before both middles",
              dep_order[0] == "dia-root", str(dep_order))
        check("and the join runs last, after both",
              dep_order[-1] == "dia-join", str(dep_order))

        # Cycles and bad references are refused before any agent runs.
        guard2: list[str] = []

        def _guard2(name: str):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                guard2.append(name)
                return {"success": True, "response": "x", "agent": name}

            made.run_task = scripted
            return made

        g1 = _guard2("cyc-1")
        g2 = _guard2("cyc-2")
        cycle = await swarm.run_flow(
            [{"agentId": g1.id, "task": "a", "dependsOn": [2]},
             {"agentId": g2.id, "task": "b", "dependsOn": [1]}],
            provider=agent_provider)
        check("a dependency cycle is refused",
              cycle.get("success") is False
              and "cycle" in str(cycle.get("error")).lower(),
              str(cycle.get("error"))[:200])
        check("and no agent ran while the cycle was detected",
              guard2 == [], str(guard2))

        missing = await swarm.run_flow(
            [{"agentId": g1.id, "task": "a", "dependsOn": [9]}],
            provider=agent_provider)
        check("a dependency on a step that does not exist is refused",
              missing.get("success") is False
              and "does not exist" in str(missing.get("error")),
              str(missing.get("error"))[:200])

        self_dep = await swarm.run_flow(
            [{"agentId": g1.id, "task": "a", "dependsOn": [1]}],
            provider=agent_provider)
        check("a step cannot depend on itself",
              self_dep.get("success") is False
              and "itself" in str(self_dep.get("error")),
              str(self_dep.get("error"))[:200])

        # An optional step's failure skips its dependents, not the flow.
        dep_order.clear()
        o_root = _dep_agent("opt-root")
        o_opt = _dep_agent("opt-optional", ok=False)
        o_needs = _dep_agent("opt-needs")
        o_free = _dep_agent("opt-free")
        optional_flow = await swarm.run_flow(
            [{"agentId": o_root.id, "task": "root"},
             {"agentId": o_opt.id, "task": "flaky", "dependsOn": [1],
              "optional": True},
             {"agentId": o_needs.id, "task": "needs it", "dependsOn": [2]},
             {"agentId": o_free.id, "task": "independent"}],
            provider=agent_provider)
        check("an optional step's failure does not fail the flow",
              optional_flow.get("success") is True,
              str(optional_flow.get("error"))[:200])
        check("its dependent is skipped, not run",
              "opt-needs" not in dep_order, str(dep_order))
        check("and an independent step still runs",
              "opt-free" in dep_order, str(dep_order))
        check("the skipped steps are reported",
              len(optional_flow.get("skipped") or []) == 2,
              str(optional_flow.get("skipped")))

        # A step that is not optional still stops the flow. Under concurrency
        # the *wave* finishes, so a step with no dependency on the failing one
        # may already have run — but nothing after the wave does.
        dep_order.clear()
        f_root = _dep_agent("stop-root")
        f_dead = _dep_agent("stop-dead", ok=False)
        f_never = _dep_agent("stop-never")
        stopped = await swarm.run_flow(
            [{"agentId": f_root.id, "task": "root"},
             {"agentId": f_dead.id, "task": "must work", "dependsOn": [1]},
             {"agentId": f_never.id, "task": "never", "dependsOn": [2]}],
            provider=agent_provider)
        check("a non-optional failure still stops the flow",
              stopped.get("success") is False, str(stopped.get("error"))[:200])
        check("and nothing that depends on it runs",
              "stop-never" not in dep_order, str(dep_order))
        check("the stopping step is named",
              stopped.get("stoppedAt") in (2, 3),
              str(stopped.get("stoppedAt")))

        swarm.clear_memory()

        # ---- 7i. independent branches run concurrently -------------------
        # A dependency wave used to run one step at a time, so two unrelated
        # branches cost their sum. They overlap now when the provider can
        # serve more than one request.
        import time as _t
        wave_order: list[str] = []

        def _slow_agent(name: str, delay: float, ok: bool = True):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                wave_order.append(name)
                await asyncio.sleep(delay)
                if not ok:
                    return {"success": False, "error": "boom", "agent": name}
                return {"success": True, "response": f"{name}-OUT",
                        "agent": name}

            made.run_task = scripted
            return made

        class _Cloud(FakeProvider):
            provider_id = "mock-cloud"

        cloud = _Cloud([], provider_id="mock-cloud")

        w_a = _slow_agent("wave-a", 0.35)
        w_b = _slow_agent("wave-b", 0.35)
        w_join = _slow_agent("wave-join", 0.0)
        started = _t.monotonic()
        # Both middles depend only on step 3 (the join is listed last so it can
        # depend on both). Two plain steps are implicitly ordered, so overlap
        # has to be expressed as a shared dependency, not just adjacency.
        wave = await swarm.run_flow(
            [{"agentId": w_a.id, "task": "branch a", "dependsOn": [3]},
             {"agentId": w_b.id, "task": "branch b", "dependsOn": [3]},
             {"agentId": w_join.id, "task": "root"}],
            provider=cloud)
        elapsed = _t.monotonic() - started
        check("independent branches succeed", wave.get("success") is True,
              str(wave.get("error"))[:200])
        check("and run concurrently, not one after the other",
              elapsed < 0.55, f"{elapsed:.2f}s for two 0.35s branches "
                              f"(sequential would be about 0.7s)")
        check("both branches ran", len(wave_order) == 3, str(wave_order))

        # A single-generation provider stays sequential but still correct.
        wave_order.clear()

        class _Local(FakeProvider):
            provider_id = "local"

        local = _Local([], provider_id="local")
        l_a = _slow_agent("lwave-a", 0.2)
        l_b = _slow_agent("lwave-b", 0.2)
        l_join = _slow_agent("lwave-join", 0.0)
        started = _t.monotonic()
        lwave = await swarm.run_flow(
            [{"agentId": l_a.id, "task": "branch a"},
             {"agentId": l_b.id, "task": "branch b"},
             {"agentId": l_join.id, "task": "join", "dependsOn": [1, 2]}],
            provider=local)
        l_elapsed = _t.monotonic() - started
        check("a local provider runs the branches in order",
              lwave.get("success") is True
              and wave_order[-1] == "lwave-join",
              f"{wave_order} {lwave.get('error')}")
        check("and does not attempt to overlap them",
              l_elapsed >= 0.35, f"{l_elapsed:.2f}s")

        # A failure inside a concurrent wave still stops the flow.
        wave_order.clear()
        f_ok = _slow_agent("cwave-ok", 0.1)
        f_bad = _slow_agent("cwave-bad", 0.1, ok=False)
        f_join = _slow_agent("cwave-join", 0.0)
        cfail = await swarm.run_flow(
            [{"agentId": f_ok.id, "task": "ok"},
             {"agentId": f_bad.id, "task": "fails"},
             {"agentId": f_join.id, "task": "join", "dependsOn": [1, 2]}],
            provider=cloud)
        check("a failure in a concurrent wave fails the flow",
              cfail.get("success") is False, str(cfail.get("error"))[:200])
        check("and the dependent never runs",
              "cwave-join" not in wave_order, str(wave_order))

        # ---- 7j. an agent may judge the gate -----------------------------
        # Substring matching is cheap but not a parser: "FAIL, does not pass"
        # contains PASS. A judge asks a model instead.
        check("a verdict of 'FAIL, does not pass' is not read as a pass",
              _SwarmOrchestrator._verdict_is_pass("FAIL, does not pass") is False,
              "the substring trap is live")
        check("a plain PASS is still a pass",
              _SwarmOrchestrator._verdict_is_pass("PASS, looks good") is True, "")
        check("and an unrecognised verdict is not a pass",
              _SwarmOrchestrator._verdict_is_pass("no problems found") is False,
              "")

        swarm.clear_memory()
        judge_calls: list[str] = []

        def _judge_agent(name: str, replies: list[str]):
            made = swarm.spawn(name, "general")
            queue = list(replies)

            async def scripted(task, provider=None):
                judge_calls.append(name)
                text = queue.pop(0) if len(queue) > 1 else queue[-1]
                return {"success": True, "response": text, "agent": name}

            made.run_task = scripted
            return made

        # The judge accepts immediately.
        judge_calls.clear()
        j_prod = _judge_agent("j-prod", ["some code"])
        j_judge = _judge_agent("j-judge", ["PASS"])
        j_ok = await swarm.run_flow(
            [{"agentId": j_prod.id, "task": "code", "retry": j_judge.id,
              "until": {"judge": j_judge.id, "criterion": "has tests",
                        "maxAttempts": 2}}],
            provider=agent_provider)
        check("a judge gate can pass", j_ok.get("success") is True,
              str(j_ok.get("error"))[:200])
        check("and the judge was actually asked",
              "j-judge" in judge_calls, str(judge_calls))

        # The judge rejects, then accepts.
        judge_calls.clear()
        jr_prod = _judge_agent("jr-prod", ["draft", "better"])
        jr_judge = _judge_agent("jr-judge", ["FAIL no tests", "PASS"])
        j_retry = await swarm.run_flow(
            [{"agentId": jr_prod.id, "task": "code", "retry": jr_judge.id,
              "until": {"judge": jr_judge.id, "criterion": "has tests",
                        "maxAttempts": 3}}],
            provider=agent_provider)
        check("a judge that rejects sends the work back",
              j_retry.get("success") is True
              and judge_calls.count("jr-prod") == 2, str(judge_calls))

        # Missing criterion and an unknown judge are refused before any run.
        guard3: list[str] = []

        def _spy3(name: str):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                guard3.append(name)
                return {"success": True, "response": "PASS", "agent": name}

            made.run_task = scripted
            return made

        s_prod = _spy3("judge-guard-prod")
        s_judge = _spy3("judge-guard-judge")
        no_criterion = await swarm.run_flow(
            [{"agentId": s_prod.id, "task": "code", "retry": s_judge.id,
              "until": {"judge": s_judge.id, "maxAttempts": 2}}],
            provider=agent_provider)
        check("a judge gate without a criterion is refused",
              no_criterion.get("success") is False
              and "criterion" in str(no_criterion.get("error")),
              str(no_criterion.get("error"))[:200])
        unknown_judge = await swarm.run_flow(
            [{"agentId": s_prod.id, "task": "code", "retry": s_judge.id,
              "until": {"judge": "nope", "criterion": "x",
                        "maxAttempts": 2}}],
            provider=agent_provider)
        check("an unknown judge agent is refused",
              unknown_judge.get("success") is False
              and "judge" in str(unknown_judge.get("error")).lower(),
              str(unknown_judge.get("error"))[:200])
        check("and no agent ran while the judge input was invalid",
              guard3 == [], str(guard3))

        swarm.clear_memory()

        # ---- 7l. conditional branching ----------------------------------
        # `until`+`retry` repeats a step's own producer. `onPass`/`onFail` send
        # control somewhere else — a review loop that goes *back* to an earlier
        # step, or a flow that stops early.
        swarm.clear_memory()
        branch_order: list[str] = []

        def _branch_agent(name: str, replies: list[str], ok: bool = True):
            made = swarm.spawn(name, "general")
            queue = list(replies)

            async def scripted(task, provider=None):
                branch_order.append(name)
                if not ok:
                    return {"success": False, "error": "boom", "agent": name}
                text = queue.pop(0) if len(queue) > 1 else queue[-1]
                return {"success": True, "response": text, "agent": name}

            made.run_task = scripted
            return made

        # A review loop: the reviewer sends rejected work back to the coder.
        br_coder = _branch_agent("br-coder", ["code v1", "code v2"])
        br_rev = _branch_agent("br-rev", ["FAIL needs tests", "PASS"])
        review = await swarm.run_flow(
            [{"agentId": br_coder.id, "task": "code"},
             {"agentId": br_rev.id, "task": "review",
              "until": {"contains": "PASS", "agent": br_rev.id,
                        "maxAttempts": 1},
              "retry": br_rev.id, "onPass": "done", "onFail": 1}],
            provider=agent_provider)
        check("a branch review loop succeeds",
              review.get("success") is True, str(review.get("error"))[:200])
        check("and goes back to the producer when rejected",
              branch_order == ["br-coder", "br-rev", "br-coder", "br-rev"],
              str(branch_order))
        check("the jumps taken are reported",
              [j.get("to") for j in (review.get("jumps") or [])]
              == [1, "done"],
              str(review.get("jumps")))

        # onPass "done" must stop the flow before later steps run.
        branch_order.clear()
        d_check = _branch_agent("br-check", ["good enough"])
        d_after = _branch_agent("br-after", ["should not run"])
        early = await swarm.run_flow(
            [{"agentId": d_check.id, "task": "check",
              "until": {"contains": "good", "agent": d_check.id,
                        "maxAttempts": 1},
              "retry": d_check.id, "onPass": "done"},
             {"agentId": d_after.id, "task": "after"}],
            provider=agent_provider)
        check("branching to 'done' finishes successfully",
              early.get("success") is True, str(early.get("error"))[:200])
        check("and stops the flow before the next step",
              "br-after" not in branch_order, str(branch_order))

        # onFail "stop" ends it as a failure.
        branch_order.clear()
        s_work = _branch_agent("br-work", ["done"])
        s_gate = _branch_agent("br-gate", ["never passes"])
        halted = await swarm.run_flow(
            [{"agentId": s_work.id, "task": "work"},
             {"agentId": s_gate.id, "task": "check",
              "until": {"contains": "YES", "agent": s_gate.id,
                        "maxAttempts": 1},
              "retry": s_gate.id, "onPass": "done", "onFail": "stop"}],
            provider=agent_provider)
        check("branching to 'stop' fails the flow",
              halted.get("success") is False, str(halted.get("error"))[:200])

        # A branch that never resolves must terminate on the budget, not hang.
        branch_order.clear()
        loop_a = _branch_agent("br-loop-a", ["x"])
        loop_b = _branch_agent("br-loop-b", ["no"])
        looping = await swarm.run_flow(
            [{"agentId": loop_a.id, "task": "work"},
             {"agentId": loop_b.id, "task": "check",
              "until": {"contains": "NEVER", "agent": loop_b.id,
                        "maxAttempts": 1},
              "retry": loop_b.id, "onPass": "done", "onFail": 1}],
            provider=agent_provider)
        check("an endlessly-branching flow is stopped by the budget",
              looping.get("success") is False
              and "budget" in str(looping.get("error")).lower(),
              str(looping.get("error"))[:200])
        check("and it did not run forever",
              len(branch_order) <= 2 * (orch_mod.MAX_BRANCH_JUMPS + 2),
              f"{len(branch_order)} agent calls")

        # A flow with no branches must be untouched by all of this.
        branch_order.clear()
        plain_a = _branch_agent("nb-a", ["A-OUT"])
        plain_b = _branch_agent("nb-b", ["B-OUT"])
        plain_flow = await swarm.run_flow(
            [{"agentId": plain_a.id, "task": "a"},
             {"agentId": plain_b.id, "task": "b"}],
            provider=agent_provider)
        check("a flow with no branches still runs in order",
              plain_flow.get("success") is True
              and branch_order == ["nb-a", "nb-b"], str(branch_order))
        check("and reports no jumps",
              not plain_flow.get("jumps"), str(plain_flow.get("jumps")))

        # Invalid branch targets are refused before any agent runs.
        guard4: list[str] = []

        def _spy4(name: str):
            made = swarm.spawn(name, "general")

            async def scripted(task, provider=None):
                guard4.append(name)
                return {"success": True, "response": "x", "agent": name}

            made.run_task = scripted
            return made

        b_prod = _spy4("branch-guard")
        for label, extra in (
                ("a target that does not exist", {"onFail": 9}),
                ("a target of the step itself", {"onPass": 1}),
                ("a target that is not a number or keyword",
                 {"onFail": "sideways"}),
        ):
            bad = await swarm.run_flow(
                [{"agentId": b_prod.id, "task": "t", **extra}],
                provider=agent_provider)
            check(f"a branch with {label} is refused",
                  bad.get("success") is False and bool(bad.get("error")),
                  str(bad.get("error"))[:200])
        check("and no agent ran while the branch input was invalid",
              guard4 == [], str(guard4))

        swarm.clear_memory()

        # ---- 7m. the handlers exist -------------------------------------
        for method in ("swarm.flow", "swarm.memory"):
            check(f"the backend registers '{method}'",
                  method in handlers, "missing")
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
                  "code_edit", "set_clipboard", "wiki_write", "wiki_ingest",
                  "sop_save"}),
          "a writing tool reached the editor")
    unknown = [n for n in ws_server._CODE_EDIT_TOOLS
               if n not in {s.name for s in skill_registry.list_all()}]
    check("every tool it asks for is a real skill", not unknown,
          f"these would be dropped silently: {unknown}")
    check("the code editor can search wiki documentation",
          "wiki_search" in ws_server._CODE_EDIT_TOOLS, "wiki_search missing")
    check("the code editor can look up procedures",
          "sop_lookup" in ws_server._CODE_EDIT_TOOLS, "sop_lookup missing")

    # The planner also has tools:
    check("the planner's tool subset is read-only",
          not (set(ws_server._PLAN_TOOLS)
               & {"write_file", "delete_file", "create_dir", "code_apply",
                  "code_edit", "set_clipboard", "wiki_write", "wiki_ingest",
                  "sop_save"}),
          "a writing tool reached the planner")
    unknown_plan = [n for n in ws_server._PLAN_TOOLS
                    if n not in {s.name for s in skill_registry.list_all()}]
    check("every planner tool is a real skill", not unknown_plan,
          f"these would be dropped silently: {unknown_plan}")
    check("the planner can search wiki documentation",
          "wiki_search" in ws_server._PLAN_TOOLS, "wiki_search missing from plan")
    check("the planner can look up procedures",
          "sop_lookup" in ws_server._PLAN_TOOLS, "sop_lookup missing from plan")

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
          "system prompt", "anchor" in first_prompt and "replacement" in first_prompt,
          "the anchored-edit contract was not in the user turn")
    check("and it is not offered the whole catalogue",
          "screenshot(" not in first_prompt,
          "every skill was offered to the editor")

    # An oversized file used to be refused outright, because a whole-file reply
    # past the token cap read as a deletion. Anchored edits carry only the
    # change, so a big file is now edited — the reply just must not be the whole
    # file. This pins that the size path is "use anchors", not "refuse".
    big = ws_dir / "big.py"
    big.write_text("x = 1\n" * 4000, encoding="utf-8")
    big_anchor = json.dumps({
        "edits": [{"anchor": "x = 1", "replacement": "y = 2",
                   "replaceAll": True}],
        "summary": "rename x",
    })
    fake_big = FakeProvider([big_anchor], provider_id="mock")
    provider_registry.get_provider = lambda: fake_big
    ws_server._pending_edits.pop(f"{wp}::big.py", None)
    r = await edit({"workspaceId": wp, "filePath": "big.py",
                    "instruction": "rename x"}, ws)
    check("an oversized file is edited with anchors, not refused",
          r.get("status") == "pending", str(r)[:300])
    check("and the prompt told the model to use anchors only",
          "too long to return whole" in fake_big.payloads[0][-1]["content"],
          "the oversized note was missing from the prompt")
    check("no unusable edit is left pending for it",
          (ws_server._pending_edits.get(f"{wp}::big.py") or "").count("y = 2") == 4000,
          "the anchored edit was not stored")
    check("and the oversized file on disk is untouched",
          big.read_text(encoding="utf-8") == "x = 1\n" * 4000,
          "the file changed without approval")

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

    # ---- 11. what the chat page is told it can use -----------------------
    # Shown above the box, so "why did it not search the web for me" has an
    # answer: which skills exist, which MCP servers are connected, and whether
    # the last turn called anything at all.
    snapshot = ws_server._tool_usage_snapshot(
        [{"tool": "read_file", "success": True},
         {"tool": "read_file", "success": True},
         {"tool": "web_search", "success": False},
         {"tool": "", "success": True},
         None])
    check("a usage snapshot counts the enabled skills",
          snapshot["skills"] > 0, str(snapshot)[:200])
    check("and names each tool the turn called, once",
          snapshot["used"] == ["read_file", "web_search"],
          str(snapshot["used"]))
    check("ignoring entries with no tool name",
          "" not in snapshot["used"], str(snapshot["used"]))
    check("it carries the connected MCP servers and their tool count",
          isinstance(snapshot["mcpServers"], list)
          and isinstance(snapshot["mcpTools"], int), str(snapshot))
    ws_src = (Path(ROOT) / "backend" / "ws_server.py").read_text(
        encoding="utf-8", errors="replace")
    check("a turn sends it twice: what is available, then what was used",
          ws_src.count('broadcast_nowait("chat.tools"') == 2,
          str(ws_src.count('broadcast_nowait("chat.tools"')))


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
