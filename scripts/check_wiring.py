"""Assert the routed call sites actually pass the model they should.

Two kinds of check:

* **Behavioural** for the call sites that accept a ``provider`` argument — goal
  planning and swarm agents. A recording stub stands in for the provider, so
  nothing depends on a live model, on which tool it chooses, or on the network.
* **Structural** for the background memory jobs and the code-generation paths.
  Those reach for a provider internally, and a behavioural test would have to
  touch the user's chat history, run a web search, or write a forged skill file.
  Instead each module is checked for the routing call plus the invariant that
  every ``provider.chat(`` in it is routed.

Swarm agents are the exception in both directions: they no longer call the
provider at all. An agent's task goes through ``run_chat_pipeline``, which is
what gives it the tools chat has, so the routing assertion has to follow the
role into that call rather than look for a ``for_provider`` beside a
``provider.chat`` that is no longer there.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_wiring.py
"""

import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

# module -> the role its chat calls must be routed through
STRUCTURAL = {
    "backend/memory/facts.py": "utility",
    "backend/memory/compaction.py": "utility",
    "backend/memory/session_summary.py": "utility",
    "backend/memory/knowledge_graph.py": "utility",
    "backend/memory/journal.py": "utility",
    "backend/skills/forge.py": "reasoning",
    "backend/goals/planner.py": "reasoning",
    "backend/wiki/ingest.py": "reasoning",
}


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


class _Stub:
    """A provider that records the model it was asked to use."""

    provider_id = "deepseek"  # so the router resolves real role models
    provider_name = "recording-stub"

    def __init__(self, response: str = "[]"):
        self.response = response
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, max_tokens=4096,
                   temperature=0.7, tools=None, **_extra):
        from backend.providers.base import ProviderResult
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return ProviderResult(ok=True, response=self.response,
                              model=model or "", tokens_in=1, tokens_out=1)


def _models(provider: _Stub) -> list:
    return [call["model"] for call in provider.calls]


async def behavioural():
    # ---- goal planning -----------------------------------------------------
    from backend.goals.planner import plan_goal
    planner_provider = _Stub(response=(
        '[{"description": "check the log", "action_type": "notify", '
        '"params": {}, "dependencies": []}]'))
    await plan_goal("Check the log", "look for errors", provider=planner_provider)
    check("goal planning asks for the reasoning model",
          _models(planner_provider) == ["deepseek-v4-pro"],
          str(_models(planner_provider)))

    # ---- swarm agents ------------------------------------------------------
    from backend.swarm.orchestrator import SwarmAgent
    agent = SwarmAgent("t1", "Test agent", "general", "You are a test.", [])
    swarm_provider = _Stub(response="done")
    await agent.run_task("do a thing", provider=swarm_provider)
    check("swarm agents ask for the reasoning model",
          _models(swarm_provider) == ["deepseek-v4-pro"],
          str(_models(swarm_provider)))


def _read(relative: str) -> str:
    path = os.path.join(ROOT, relative.replace("/", os.sep))
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _code_only(source: str) -> str:
    """Source with docstrings and comments removed.

    The counter below looks for ``provider.chat(``. Without this, a module that
    *describes* a call it no longer makes — "this used to call provider.chat()
    directly" — reads as an unrouted call site, and the fix looks like a bug.
    """
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    source = re.sub(r"'''(?:.|\n)*?'''", "", source)
    return re.sub(r"#[^\n]*", "", source)


def structural():
    for relative, role in STRUCTURAL.items():
        try:
            source = _read(relative)
        except OSError as e:
            check(f"{relative} readable", False, str(e))
            continue
        code = _code_only(source)
        calls = len(re.findall(r"provider\.chat\(", code))
        routed = len(re.findall(
            r'for_provider\(provider,\s*"%s"\)' % role, code))
        check(f"{relative} routes its chat calls ({role})",
              calls > 0 and calls == routed,
              f"{calls} provider.chat call(s), {routed} routed")

    # The swarm went the other way: it used to call the provider with a
    # reasoning-role model, and now it hands the task to the shared pipeline
    # with that role forced, so the role still has to be asserted somewhere.
    try:
        swarm_code = _code_only(_read("backend/swarm/orchestrator.py"))
    except OSError as e:
        check("backend/swarm/orchestrator.py readable", False, str(e))
        return
    check("swarm agents route through the shared pipeline",
          "run_chat_pipeline(" in swarm_code,
          "the swarm no longer uses the pipeline, so it lost its tools")
    check("and force the reasoning role rather than guessing it",
          'force_role="reasoning"' in swarm_code,
          "no reasoning role on the pipeline call")
    check("an agent no longer calls the provider itself",
          "provider.chat(" not in swarm_code,
          "a direct provider call came back")


def contract():
    """The utility role is only useful if it resolves and is overridable."""
    from backend.config import config
    from backend.providers import router

    check("deepseek utility resolves to a cheap non-thinking model",
          router.utility_model("deepseek") == "deepseek-v4.1-flash",
          str(router.utility_model("deepseek")))
    check("a provider without a utility role is untouched",
          router.utility_model("openai") is None,
          str(router.utility_model("openai")))
    check("an explicit utility value wins over the shipped default",
          _with_role("utility", "deepseek-v4-pro",
                     lambda: router.utility_model("deepseek"))
          == "deepseek-v4-pro")
    check("clearing the utility role restores the shipped default",
          _with_role("utility", "",
                     lambda: router.utility_model("deepseek"))
          == "deepseek-v4.1-flash")
    check("for_provider agrees with utility_model",
          router.for_provider(_Stub(), "utility") == "deepseek-v4.1-flash",
          str(router.for_provider(_Stub(), "utility")))
    check("auto_route off disables the utility role too",
          _with_top("auto_route", False,
                    lambda: router.for_provider(_Stub(), "utility")) is None)


def _with_role(role: str, value: str, fn):
    from backend.config import config
    roles = config._data["providers"]["builtin"]["deepseek"]["roles"]
    original = roles.get(role)
    roles[role] = value
    try:
        return fn()
    finally:
        roles[role] = original


def _with_top(key: str, value, fn):
    from backend.config import config
    providers = config._data["providers"]
    original = providers.get(key)
    providers[key] = value
    try:
        return fn()
    finally:
        providers[key] = original


async def main():
    contract()
    await behavioural()
    structural()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(asyncio.run(main()))
