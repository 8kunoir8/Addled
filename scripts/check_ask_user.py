"""The model can ask the user a question — and only when that is meaningful.

`ask_user` is an escape hatch for the case a turn genuinely cannot resolve:
which of two same-named files, which account, which of three readings of a
one-line request. The failure mode of a cheap one is worse than the ambiguity
it solves — a model that finds asking easy stops making reasonable assumptions,
and the user answers more questions than they would have typed instructions — so
most of this file is about the limits rather than the feature.

The rules worth guarding, each of which broke something when it was missing:

* **Only ask when someone can answer.** A scheduled task, a swarm desk or a
  voice turn has nobody watching. A question there parks the work forever. The
  attended/unattended split is derived from SOURCES so a new source cannot land
  in a gap, and `attendance_gaps()` must stay empty.
* **The turn ENDS on the question.** `ask_user` returns success, so without an
  explicit branch the tool loop carries on and hands the result back to the
  model — which then answers its own question. Measured: the provider must be
  called exactly once.
* **A refusal must not end the turn.** There is no card when the question is
  refused, so stopping would leave the model waiting on nothing. Only a queued
  question carries `requires_answer`.
* **Origin is recorded.** An answer typed in one chat must not settle a question
  raised in another.
* **A question expires,** and the outcome is recorded rather than silently
  forgotten, so the next turn can be told it went unanswered.
* **A repeat is refused.** Two prompts for one decision trains the user to
  dismiss them unread, which makes the real one useless.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_ask_user.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)

def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()

# ---- 1. who may ask ---------------------------------------------------------

def test_attendance():
    from backend import chat_sources as cs

    gaps = cs.attendance_gaps()
    check("every source is classified attended or unattended",
          not gaps["unclassified"], str(gaps["unclassified"]))
    check("no classification names an unknown source",
          not gaps["unknown_id"], str(gaps["unknown_id"]))
    check("no source is both attended and unattended",
          not gaps["both"], str(gaps["both"]))

    # The default source is what an unlabelled turn gets, and that is the
    # floating character — a person looking at their own screen.
    check("the default source is attended",
          cs.is_attended(cs.DEFAULT),
          f"{cs.DEFAULT} is the fallback for a turn that declared nothing")

    for src in ("dashboard", "telegram", "discord", "whatsapp", "code",
                "character", "remote"):
        check(f"{src} is attended", cs.is_attended(src))
    for src in ("task", "swarm", "voice", "approval"):
        check(f"{src} is unattended", not cs.is_attended(src))

    # A typo must not become a licence to ask. `normalise` maps it to DEFAULT,
    # which is attended, so this documents that a bad *source id* is treated as
    # the local user rather than crashing or asking into the void.
    check("an unknown source resolves to the attended default",
          cs.is_attended("definitely-not-a-source"))

    from backend.questions import policy
    allowed, _ = policy.may_ask("dashboard")
    check("policy agrees an attended source may ask", allowed)
    blocked, why = policy.may_ask("swarm")
    check("policy refuses an unattended source", not blocked)
    # The wording matters as much as the verdict: a bare refusal leaves the
    # model with nothing to do, and a model with nothing to do asks anyway.
    check("the refusal tells the model what to do instead",
          "assumption" in why.lower(), why[:80])


# ---- 2. the queue -----------------------------------------------------------

def test_queue():
    from backend.questions import pending, policy

    pending.clear()
    r = pending.ask("Which file?", options=["a.py", "b.py"],
                    source="dashboard", conversation="c1")
    check("a question queues", bool(r.get("success")), str(r)[:80])
    check("it gets an id", bool(r.get("question_id")))
    check("options are kept", r.get("options") == ["a.py", "b.py"])
    check("a ttl phrase is returned", bool(r.get("ttl")))

    dup = pending.ask("which file?", options=["a.py", "b.py"],
                      source="dashboard", conversation="c1")
    check("a repeat of the same question is refused", not dup.get("success"),
          "two cards for one decision teaches the user to dismiss them")
    check("the repeat refusal tells the model to continue",
          "assumption" in (dup.get("error") or "").lower())
    other = pending.ask("Which file?", options=["x.py", "y.py"],
                        source="dashboard", conversation="c1")
    check("the same words offering different choices is a different question",
          bool(other.get("success")))

    # The rewording case, observed live: re-entered with an answer, the model
    # asked the SAME decision with its own sentence extended. An equality test
    # let it through, the second card replaced the first, and the answer was
    # never used — the turn ended on a question the user had answered already.
    reworded = pending.ask("Which file? a.py or b.py",
                           source="dashboard", conversation="c1")
    check("a reworded repeat of one decision is refused",
          not reworded.get("success"),
          "a looping model rewrites its question, so equality is not enough")
    fresh = pending.ask("What is the staging server codename?",
                        source="dashboard", conversation="c1")
    check("a genuinely different question is allowed",
          bool(fresh.get("success")))

    blocked = pending.ask("Should I?", source="swarm", conversation="c2")
    check("an unattended question is refused at the queue",
          not blocked.get("success"))
    check("and marked unattended", blocked.get("unattended") is True)

    check("empty wording is refused",
          not pending.ask("", source="dashboard")["success"])
    check("over-long wording is refused",
          not pending.ask("x" * 700, source="dashboard")["success"])
    pending.clear()


def test_origin_isolation():
    from backend.questions import pending

    pending.clear()
    a = pending.ask("For c1", source="dashboard", conversation="c1")
    pending.ask("For c2", source="telegram", conversation="c2")
    mine = pending.open_questions(conversation="c1")
    check("a conversation sees only its own question", len(mine) == 1,
          str([q.get("question") for q in mine]))
    check("and it is the right one",
          mine and mine[0]["question"] == "For c1")

    pending.answer(a["question_id"], "one")
    check("answering settles it", pending.get(a["question_id"]) is None)
    check("a second answer is refused",
          not pending.answer(a["question_id"], "two").get("success"))
    pending.clear()


def test_expiry():
    from backend.questions import pending, policy

    pending.clear()
    q = pending.ask("Will this lapse?", source="dashboard", conversation="c9")
    pending._open[q["question_id"]]["created"] = (
        time.time() - policy.ttl_seconds() - 5)

    check("an aged question is not offered", pending.get(q["question_id"]) is None)
    late = pending.answer(q["question_id"], "too late")
    check("and cannot be answered", not late.get("success"))
    check("the caller is told it was closed, not that it never existed",
          late.get("expired") is True)

    outcomes = pending.recent_outcomes(source="dashboard", conversation="c9")
    check("the expiry is recorded so the next turn can be told",
          any(o.get("outcome") == "expired" for o in outcomes),
          "a question that vanished silently would just be asked again")
    pending.forget_outcomes(conversation="c9")
    check("outcomes can be consumed once",
          pending.recent_outcomes(conversation="c9") == [])
    pending.clear()


def test_dismissal():
    from backend.questions import pending

    pending.clear()
    q = pending.ask("Skip me?", source="dashboard", conversation="c3")
    check("dismissal works", pending.dismiss(q["question_id"]).get("success"))
    check("dismissing twice is refused",
          not pending.dismiss(q["question_id"]).get("success"))
    check("dismissal is recorded as itself, not as an answer",
          any(o.get("outcome") == "dismissed"
              for o in pending.recent_outcomes(conversation="c3")))
    pending.clear()


# ---- 3. the skill and the loop ---------------------------------------------

def test_skill_registered():
    from backend.skills.registry import skill_registry
    skill = skill_registry.get("ask_user")
    check("ask_user is registered", skill is not None)
    if skill is None:
        return None, None
    check("it is a system skill",
          getattr(skill, "category", "") == "system",
          str(getattr(skill, "category", "")))
    check("it does NOT require approval",
          not getattr(skill, "requires_approval", False),
          "asking the user a question is not a destructive action")
    params = (skill.schema or {}).get("properties", {}) if hasattr(skill, "schema") \
        else {}
    if not params:
        # Fall back to whatever attribute holds the JSON schema.
        for attr in ("params", "parameters", "input_schema"):
            if isinstance(getattr(skill, attr, None), dict):
                params = getattr(skill, attr).get("properties", {})
                break
    check("it takes a question", "question" in params, str(list(params)))
    check("it accepts optional choices", "options" in params)
    return skill, params


async def test_loop_stops(skill):
    """The turn must end on the question, and the model must not answer it."""
    from backend import chat_context
    from backend.providers.base import ProviderResult
    from backend.questions import pending
    from backend.skills import tool_loop

    class Scripted:
        provider_id = "scripted"
        supports_vision = False
        has_native_tools = False

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, model=None, **kw):
            self.calls += 1
            if self.calls == 1:
                # The prompt-tools format: this provider is not native, so the
                # call has to be written as text the parser reads.
                return ProviderResult(ok=True, model=model, response=(
                    '<tool_call>\n{"name": "ask_user", "arguments": '
                    '{"question": "Which config?", "options": ["a", "b"]}}\n'
                    '</tool_call>'))
            return ProviderResult(ok=True, model=model,
                                  response="I picked a and carried on anyway.")

    pending.clear()
    chat_context.set_origin("dashboard", "loopcheck")
    provider = Scripted()
    result = await tool_loop.chat_with_tools(
        provider, [{"role": "user", "content": "update it"}],
        system_prompt="s", max_tool_rounds=4)

    check("the provider was called exactly once",
          provider.calls == 1,
          f"called {provider.calls} times — the loop continued past the "
          "question and the model answered it itself")
    check("the result is marked awaiting_answer",
          result.get("awaiting_answer") is True)
    check("a question id is returned", bool(result.get("question_id")))
    check("the question is put to the user",
          "config" in (result.get("response") or "").lower(),
          repr(result.get("response"))[:80])
    check("the model did not answer its own question",
          "anyway" not in (result.get("response") or ""),
          repr(result.get("response"))[:80])
    check("the question is queued, not just narrated",
          len(pending.open_questions(conversation="loopcheck")) == 1)
    pending.clear()


async def test_refusal_does_not_stop(skill):
    """A refusal has no card, so the turn must continue rather than stop."""
    from backend import chat_context
    from backend.questions import pending
    from backend.skills import tool_loop

    pending.clear()
    chat_context.set_origin("swarm", "swarmdesk")
    result = await tool_loop.execute_skill(
        "ask_user", {"question": "Should I?"})
    check("an unattended call is refused", not result.get("success"))
    data = result.get("data") or {}
    check("the refusal is marked unattended", data.get("unattended") is True)
    check("a refused question does NOT carry requires_answer",
          not data.get("requires_answer"),
          "stopping with no card would leave the turn waiting on nothing")
    check("nothing was queued", len(pending.open_questions()) == 0)
    pending.clear()


# ---- 4. the surfaces -------------------------------------------------------

def test_backend_surface():
    server = read(os.path.join("backend", "ws_server.py"))
    for method in ("questions.list", "question.answer", "question.dismiss"):
        check(f"{method} is registered",
              f'_server.register("{method}"' in server)
    check("a queued question is broadcast",
          'broadcast("question.ask"' in server)
    check("settling one is broadcast, so no surface keeps a dead card",
          'broadcast("question.settled"' in server)
    check("there is no RPC that raises a question",
          not re.search(r'_server\.register\("question\.ask"', server),
          "a question must come from a turn, not from a caller")
    check("the origin is carried into the skill",
          "chat_context" in read(os.path.join("backend", "skills", "tool_loop.py")))


def test_dashboard_surface():
    store = read(os.path.join("dashboard", "src", "lib", "questionsStore.ts"))
    card = read(os.path.join("dashboard", "src", "components", "QuestionCard.tsx"))
    layout = read(os.path.join("dashboard", "src", "app", "layout.tsx"))

    check("the store can send an answer",
          "question.answer" in store)
    check("the store can dismiss", "question.dismiss" in store)
    # The part that makes non-blocking work: answering has to resume the work.
    check("answering resumes the turn",
          "chat.send" in store,
          "a settled question that does not resume leaves the task half done")
    check("the resume is sent after the answer is recorded",
          store.index("question.answer") < store.index("resume("),
          "the turn would otherwise carry an answer the backend has not stored")
    check("the card does not clear itself before the backend confirms",
          re.search(r"if \(r\?\.success\)\s*removeQuestion", store) is not None,
          "the card must survive a refused or expired answer, with an error")
    check("the card renders choices as buttons",
          "options.map" in card)
    check("the card renders a text box when there are no choices",
          "Type your answer" in card)
    check("the card shows the expiry",
          "question.ttl" in card)
    check("the layout owns the question.ask subscription",
          "onNotification('question.ask'" in layout,
          "one handler per method: a page subscriber would take it from the "
          "always-mounted layout")
    check("the layout restores questions on connect",
          "questions.list" in layout)
    check("restoring replaces rather than merges",
          "setQuestions(" in layout,
          "an expired question must disappear, not linger as a dead card")


def main() -> int:
    test_attendance()
    test_queue()
    test_origin_isolation()
    test_expiry()
    test_dismissal()
    skill, _ = test_skill_registered()
    test_backend_surface()
    test_dashboard_surface()
    if skill is not None:
        asyncio.run(test_loop_stops(skill))
        asyncio.run(test_refusal_does_not_stop(skill))
    else:
        check("the loop could be exercised", False, "ask_user was not registered")

    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
