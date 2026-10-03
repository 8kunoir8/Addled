"""Live proof: a chat turn delegates to a swarm agent and the result comes back.

Runs entirely against the running app over its WebSocket, so the agent's turn
happens in the APP's process and its `swarm.agentResult` broadcast is observable.

An earlier version of this test called the skill handler in its own Python
process. That looked like a failure — no broadcast arrived — but the broadcast
had nowhere to go: `swarm.run_agent` ran in the test's process, not the app's,
and the process exited before the agent finished. Testing from outside, through
the real surface, is the only version that means anything.
"""
import asyncio
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import websockets

fails = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label
          + ("  <- " + str(detail) if detail and not ok else ""))
    if not ok:
        fails.append(label)


async def main():
    async with websockets.connect("ws://127.0.0.1:9876",
                                  max_size=16 * 1024 * 1024) as ws:
        mid = 0

        # Every non-reply message seen while waiting for a reply is kept, not
        # dropped. The delegated agent finishes in ~30s and the chat turn's own
        # ack can take longer than that, so `swarm.agentResult` frequently
        # arrives BEFORE the ack. A loop that only looked for its own id threw
        # those pushes away and the wait afterwards found nothing — a working
        # feature reported as "no broadcast, the answer would be lost".
        pending: list[dict] = []

        async def call(method, params, timeout=300):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": mid,
                                      "method": method, "params": params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if msg.get("id") == mid:
                    return msg
                pending.append(msg)

        async def wait_for_push(method, timeout=300):
            """Return the first `method` notification, checking the backlog first."""
            for i, msg in enumerate(pending):
                if msg.get("method") == method:
                    return pending.pop(i)
            end = time.time() + timeout
            while time.time() < end:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
                except asyncio.TimeoutError:
                    continue
                if msg.get("id") is not None:
                    continue
                if msg.get("method") == method:
                    return msg
                pending.append(msg)
            return None

        print("=== ask chat to delegate, with the wording a model really uses ===")
        # Unique per run so a result left over from a previous run cannot be
        # mistaken for this one's.
        conv = f"delegate-live-{int(time.time())}"
        t0 = time.time()
        r = await call("chat.send", {
            "message": "Use swarm_delegate to ask the QA agent to reply with "
                       "exactly the word: bananas. Then say you have asked it.",
            # 'dashboard' is the chat page's own source id, and the one this
            # result has to come back as. It must be a name from
            # `backend/chat_sources.SOURCES`: anything else is normalised to the
            # 'addled' default, which is correct behaviour but not what this
            # check is about.
            "source": "dashboard",
            "conversation": conv,
        })
        elapsed = time.time() - t0
        res = r.get("result") or {}
        check("the turn returned", "error" not in r, str(r.get("error"))[:200])
        print(f"      turn took {elapsed:.1f}s")
        check("it did not block on the agent", elapsed < 150, f"{elapsed:.1f}s")
        reply = str(res.get("response") or "")
        print(f"      reply: {reply[:160]!r}")
        low = reply.lower()
        check("the turn did not report the agent as unavailable",
              "not available" not in low and "isn't accessible" not in low,
              reply[:160])
        # The model is asked to relay the skill's acknowledgement and live
        # testing showed it often does not — it calls the tool, gets the ack,
        # and answers with an unrelated greeting while an agent is working.
        # `chat_send` now appends the skill's own sentence when the reply fails
        # to name the agent, so the user is always told. This is that guarantee.
        check("the reply tells the user the agent is working on it",
              "qa" in low, reply[:200])

        print()
        print("=== the result broadcast ===")
        # Checked against the backlog too, because the agent often finishes
        # before the chat turn's own ack comes back.
        m = await wait_for_push("swarm.agentResult")
        got = m.get("params") if m else None
        check("a swarm.agentResult arrived", got is not None,
              "no broadcast, so the answer would be lost")
        if got:
            print(f"      agent  : {got.get('agent')}")
            print(f"      success: {got.get('success')}")
            print(f"      text   : {str(got.get('text'))[:200]!r}")
            check("it names the agent", bool(got.get("agent")))
            check("it carries text", bool(str(got.get("text") or "")))
            check("it says where it came from", got.get("source") == "dashboard",
                  str(got.get("source")))
            # A result left over from an earlier run would satisfy the checks
            # above. This run's conversation is the only one that proves the
            # answer reached the page that asked for it.
            check("it belongs to this conversation",
                  got.get("conversation") == conv, str(got.get("conversation")))
            # The agent used to answer "my tools aren't accessible" because its
            # persona replaced the system prompt and dropped the capability
            # text. `tool_brief` now appends that block on the persona path too,
            # and `run_task` corrects the reply if the model refuses anyway.
            # Either way the agent must not come back claiming it is helpless.
            body = str(got.get("text") or "").lower()
            refused = any(s in body for s in (
                "tools aren't accessible", "tools are not accessible",
                "not a valid action with any of the available tools",
                "none of the available tools", "no tools available",
                "cannot access the tools", "can't access the tools",
            ))
            check("the agent did not claim it has no tools", not refused,
                  str(got.get("text"))[:220])

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all live delegation checks passed")
    return 1 if fails else 0


raise SystemExit(asyncio.run(main()))
