"""The post-turn review must be bounded, deferred, and honest about doing nothing.

Four things have to hold, and every one of them is a way this feature could go
wrong in the user's hands rather than in the tests:

  OFF BY DEFAULT. It spends model calls on its own initiative. A feature like
  that must be declinable before it is ever correct.

  BOUNDED. One model call per review, a daily cap, and a floor on how small a
  turn may be. Without the floor the cap is spent reviewing "what time is it".

  COALESCED. A review replays a conversation to judge it, so two reviews of one
  conversation are the same review twice. Newest wins - that is dedup, not loss.

  HONEST. Most turns teach nothing, and the commonest correct outcome is to save
  neither a skill nor a fact. A check that only proved "it can save" would be
  satisfied by a review that saves junk every time.

The revert-verification this was built against: dropping the `enabled()` gate
makes the first assertion fail, and removing the daily cap makes the third fail.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_review.py
"""

import asyncio
import inspect
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []

LONG_TRACE = "tools: " + ", ".join(["read_file"] * 40)


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")
        fails.append(label)


def main() -> int:
    print("check_review")

    try:
        from backend import review
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the review module: {e}")
        print()
        print("FAILED (1): could not import the review module")
        return 1

    # ------------------------------------------------------- off by default
    check("the review is off unless the config turns it on",
          review.enabled() is False,
          "it spends model calls on its own initiative and must be declinable")
    review._reset_for_tests()
    check("a disabled review queues nothing",
          review.queue_review("c1", LONG_TRACE) is False,
          "queuing while disabled would spend the cap on a feature nobody asked for")
    check("nothing is pending while disabled", review.pending() == 0)

    # ------------------------------------------------------------ the floor
    # Turn the feature on for the rest, without touching the user's settings.
    original_enabled = review.enabled
    review.enabled = lambda: True
    try:
        review._reset_for_tests()
        check("a trivial turn is not queued",
              review.queue_review("c1", "tools: read_file") is False,
              "one-line turns teach nothing and would spend the whole cap")
        check("a substantial turn is queued",
              review.queue_review("c1", LONG_TRACE) is True)
        check("the queue reports it", review.pending() == 1)

        # ------------------------------------------------------- coalescing
        review._reset_for_tests()
        for i in range(5):
            review.queue_review("same", LONG_TRACE + f" {i}")
        check("five turns in one conversation is one review",
              review.pending() == 1,
              f"pending={review.pending()}")
        item = review._take_due({})
        check("the newest turn wins",
              item is not None and item["trace"].endswith(" 4"),
              repr((item or {}).get("trace", "")[-6:]))

        # ------------------------------------------------------------ age-out
        review._reset_for_tests()
        review.queue_review("old", LONG_TRACE)
        for key in list(review._queue):
            review._queue[key]["at"] -= (review.MAX_AGE_S + 10)
        check("an aged-out review is dropped, not run",
              review._take_due({}) is None,
              "a stale review would write skills the next turn no longer needs")

        # --------------------------------------------------------- the queue cap
        review._reset_for_tests()
        for i in range(review.MAX_QUEUE + 20):
            review.queue_review(f"c{i}", LONG_TRACE)
        check("the queue cannot grow without bound",
              review.pending() <= review.MAX_QUEUE,
              f"pending={review.pending()}")

        # --------------------------------------------------------- daily cap
        state = {"day": __import__("time").strftime("%Y-%m-%d"),
                 "count": review.DAILY_CAP}
        check("the daily cap is enforced",
              review._used_today(state) >= review.DAILY_CAP)

        # Asserted on the code path rather than on disk: with the cap reached
        # the function must return before any provider is touched, and a check
        # that needed the real settings file would be testing the machine
        # rather than the code.
        review._reset_for_tests()
        src = inspect.getsource(review.run_due_reviews)
        check("the cap is checked before any model call",
              src.index("daily cap") < src.index("run_review("),
              "a cap checked after the call is not a cap")

        # -------------------------------------------------------- honesty
        # The common answer is "nothing to save", and it must be represented.
        check("an empty decision parses to nothing",
              review.parse_decision("") == {})
        check("prose with no JSON parses to nothing",
              review.parse_decision("Nothing worth saving here.") == {})
        check("both-null is a valid decision",
              review.parse_decision(
                  '{"skill": null, "fact": null, "reason": "nothing new"}')
              .get("reason") == "nothing new")
        check("fenced JSON is read",
              review.parse_decision(
                  'Sure:\n```json\n{"fact": "the build needs -s"}\n```')
              .get("fact") == "the build needs -s")
        check("a junk reply never raises",
              review.parse_decision("{not json") == {})

        # --------------------------------------------------- declarative facts
        # The prompt has to forbid imperatives: an instruction written to memory
        # is re-read later as an order and overrides the user.
        msgs = review.build_prompt(
            {"message": "how do I build?", "trace": LONG_TRACE,
             "outcome": "done"}, "- existing_skill: does a thing")
        system = msgs[0]["content"]
        check("the prompt asks for declarative facts, not instructions",
              "never as instructions" in system,
              "an imperative in memory overrides the user later")
        check("the prompt forbids duplicating an existing skill",
              "already" in system and "existing_skill" in system,
              "without the index the review writes near-copies of what exists")
        check("the prompt says null is the normal answer",
              "normal" in system)

        # ------------------------------------------------- no writes when null
        wrote = review.apply_decision({"skill": None, "fact": None})
        check("a null decision writes nothing",
              wrote == {"fact": False, "skill": ""}, repr(wrote))

        # ---------------------------------------------------- write path safety
        check("a learned skill goes through the registry, not a file",
              "_write_skill" in inspect.getsource(review)
              and "skill_registry.register" in inspect.getsource(review),
              "writes must use the store APIs so dedup and expiry still apply")
        check("a built-in skill is never overwritten by a review",
              "declined to overwrite" in inspect.getsource(review),
              "a review must not be able to change a built-in's behaviour")

        # ------------------------------------------------------- wiring
        loop_src = open(os.path.join(ROOT, "backend", "skills", "tool_loop.py"),
                        encoding="utf-8").read()
        check("the turn tail queues a review",
              "_queue_review(" in loop_src)
        check("the queue helper is beside the learning it complements",
              "_queue_review" in loop_src and "_learn_procedure" in loop_src)

        eng = open(os.path.join(ROOT, "backend", "engine.py"),
                   encoding="utf-8").read()
        check("the review runs from the engine tick, not inline",
              "run_due_reviews" in eng,
              "an inline review is cancelled by the next prompt on a "
              "single-generation provider")
        check("the review respects the presence guard",
              "_presence_guard" in eng and "_last_review" in eng)
    finally:
        review.enabled = original_enabled
        review._reset_for_tests()

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all review checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
