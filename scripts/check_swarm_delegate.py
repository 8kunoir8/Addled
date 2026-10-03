"""A chat or code turn can delegate work to a swarm agent, safely.

What this closes: a Chat or Code turn had no way to hand work to a swarm agent.
The four swarm skills were `swarm_note`, `swarm_notes`, `swarm_roster` and
`swarm_learn` — coordination and listing only. The capability existed
(`swarm.run_agent`) but only the dashboard could reach it, so asking the agent
to "have the Reviewer check this" got a polite refusal and a trip to another
page.

`swarm_delegate` closes that. Three things have to be true for it to be safe,
and each is checked here:

1. **It cannot recurse.** `SwarmAgent.run_task` goes through
   `run_chat_pipeline`, so an agent IS a chat turn and holds this same skill. A
   depth counter in `chat_context` refuses a delegation from inside one; without
   it, agent delegates to agent, forever.

2. **It does not block the turn.** A swarm task is a full reasoning turn —
   minutes. Blocking the user's chat on it would look like a hang, so the skill
   starts the agent and returns "started"; the answer arrives as a message.

3. **A wrong agent name says what is available.** "No agent 'X'" is useless on
   its own; the message names the saved agents, because the user cannot guess
   whether the desk exists or they mistyped it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_swarm_delegate.py
"""

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    from backend import chat_context
    from backend.skills.registry import skill_registry
    from backend.swarm import roster

    print("=== the skill exists and is offered to a turn ===")
    skill = skill_registry.get("swarm_delegate")
    check("swarm_delegate is registered", skill is not None)
    if skill is None:
        print("\nFAIL: nothing else can be checked without it")
        return 1
    check("it is in the system category (reachable by chat and code)",
          getattr(skill, "category", "") == "system",
          str(getattr(skill, "category", "")))
    check("it requires an agent and a task",
          set(getattr(skill, "parameters", {}).get("required", []))
          == {"agent", "task"},
          str(getattr(skill, "parameters", {}).get("required")))
    # The description is what the MODEL reads to decide when to call it, so a
    # description that omits "arrives later" produces a model that apologises
    # for not being able to wait.
    desc = str(getattr(skill, "description", "")).lower()
    check("the description says the answer comes back later",
          "later" in desc, desc[:80])

    print("\n=== the depth brake ===")
    check("an ordinary turn is not in a delegation",
          chat_context.in_delegation() is False)
    check("the allowance is exactly one level",
          chat_context.MAX_DELEGATION_DEPTH == 1,
          str(chat_context.MAX_DELEGATION_DEPTH))
    chat_context.enter_delegation()
    check("inside a delegation it reports so", chat_context.in_delegation() is True)
    check("and the depth is 1", chat_context.delegation_depth() == 1)
    chat_context.exit_delegation()
    check("leaving restores an ordinary turn",
          chat_context.in_delegation() is False)
    chat_context.exit_delegation()
    check("a stray exit cannot go negative (which would permit nesting)",
          chat_context.delegation_depth() == 0,
          str(chat_context.delegation_depth()))

    print("\n=== calling it from inside a delegation is refused ===")
    handler = getattr(skill, "handler", None) or getattr(skill, "func", None)
    check("the skill exposes a callable", callable(handler))
    if callable(handler):
        chat_context.enter_delegation()
        try:
            out = asyncio.run(handler({"agent": "anything", "task": "x"}))
        finally:
            chat_context.exit_delegation()
        check("it refuses rather than delegating again",
              out.get("success") is False, str(out)[:120])
        check("and explains why, so the agent does the work itself",
              "cannot delegate" in str(out.get("error", "")).lower(),
              str(out.get("error"))[:120])

    print("\n=== a missing or unknown agent is explained, not dropped ===")
    if callable(handler):
        out = asyncio.run(handler({"task": "do something"}))
        check("no agent named -> refused", out.get("success") is False)
        check("and it points at swarm_roster",
              "swarm_roster" in str(out.get("error", "")),
              str(out.get("error"))[:120])

        out = asyncio.run(handler({"agent": "NoSuchAgent_zzz", "task": "x"}))
        check("unknown agent -> refused", out.get("success") is False)
        err = str(out.get("error", ""))
        check("and the error names it", "NoSuchAgent_zzz" in err, err[:120])
        # The saved agents are listed so the user can see whether the desk
        # exists or they mistyped it.
        saved = [str(e.get("name") or "") for e in roster.definitions()]
        check("and it says what IS available",
              ("Saved agents" in err) or ("none yet" in err),
              err[:160])
        if saved:
            print(f"      (saved agents on this machine: {', '.join(saved[:5])})")

    print("\n=== the result reaches the surfaces that promised it ===")
    source = (open(os.path.join(ROOT, "backend", "skills", "registry.py"),
                   encoding="utf-8").read())
    check("the result is broadcast", 'broadcast_nowait("swarm.agentResult"' in source)
    check("the swarm page is refreshed too",
          'broadcast_nowait("swarm.updated"' in source)
    check("a phone gets it as well", 'broadcast_nowait("bot.notify"' in source)
    check("the agent's turn does not claim the character",
          "enter_delegation()" in source)

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: a chat or code turn can delegate to a swarm agent, one level deep")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
