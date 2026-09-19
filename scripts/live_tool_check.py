"""Live check: a real model, calling a real tool, through the swarm path.

The suites prove the wiring with a fake provider. This proves it with the model
the user actually runs, because "the agent has tools" is only true if a small
local model can be persuaded to use one — and the whole point of the change is
that swarm agents had none before.

It is deliberately not part of `check_all.py`. It needs a live provider, it
takes seconds, and a network or model problem would be reported as a product
failure.

Against the installed app's own copy of the code — which is what the user runs —
`ADDLED_ROOT` has to point at the install, because the local weights live under
its `resources/backend/memory/models/`, not in the repo:

    $env:ADDLED_ROOT = "$env:LOCALAPPDATA\\Programs\\Addled\\resources"
    & "$env:LOCALAPPDATA\\Programs\\Addled\\resources\\python\\python.exe" `
        -s scripts\\live_tool_check.py

Set ADDLED_PROVIDER to pick a provider explicitly (default: the active one).
"""

import asyncio
import os
import sys
import time

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TASK = ("Find this computer's screen resolution using the get_screen_size "
        "tool, then tell me the width and height in pixels.")


async def main() -> int:
    from backend.providers.registry import get_provider
    from backend.skills.registry import skill_registry
    from backend.skills.tool_loop import execute_skill
    from backend.swarm.orchestrator import swarm

    print(f"root: {ROOT}")

    # A successful tool run makes the pipeline record the route it took. That
    # is intended in the app and not something this check should leave behind.
    try:
        import backend.sop.learn as sop_learn
        sop_learn.record_run = lambda *a, **k: None
    except Exception:
        pass

    # Whatever the tool would answer without a model in the way, so the agent's
    # number can be checked rather than trusted.
    truth = await execute_skill("get_screen_size", {}, None)
    print(f"the tool itself says: {truth.get('data') or truth.get('error')}")

    try:
        provider = get_provider(os.environ.get("ADDLED_PROVIDER") or None)
    except Exception as e:
        print(f"FAIL: no provider: {e}")
        return 1
    print(f"provider: {getattr(provider, 'provider_id', '?')}")

    if getattr(provider, "provider_id", "") == "local":
        from backend.local_llm.manager import local_llm
        started = await local_llm.ensure_running()
        if started:
            print(f"local model: {started}")
        if not local_llm.is_running():
            print("FAIL: the local model is not running and could not be started")
            return 1
    else:
        print("note: this is not the local model, so it is not the small-model case")

    tools = len(skill_registry.to_openai_tools())
    print(f"the agent is offered {tools} tools")

    agent = swarm.spawn("live-checker", "general")
    began = time.time()
    try:
        result = await swarm.run_agent(agent.id, TASK, provider)
    finally:
        swarm.stop(agent.id)
    took = time.time() - began

    print(f"took {took:.1f}s")
    print("result: " + repr(result)[:1200])

    ok = bool(result.get("success"))
    used = int(result.get("toolResults") or 0)
    text = str(result.get("response") or result.get("error") or "")

    if not ok:
        print("FAIL: the agent's task did not succeed")
        return 1
    if used < 1:
        # The real question: a model that never calls a tool is the failure this
        # change was meant to fix, even though run_task reports success.
        print("FAIL: the agent answered without calling a tool")
        return 1
    if "width" not in text.lower() and "resolution" not in text.lower():
        print("warning: the answer does not mention the resolution it looked up")
    print(f"OK: the agent used {used} tool call(s) and answered")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
