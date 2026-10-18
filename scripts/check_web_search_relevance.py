"""A search that returns unrelated rows as SUCCESS leaves the model no way to
tell it apart from a real answer - so it repeats the junk to the user.

The second half of the announced-but-not-called investigation, 2026-10-11.

## What was reported

On the chat page, asking for a price that needs a live web search:

  1. first turn  - a promise ("aku cari sekarang. Aku buka browser..."),
                   nothing ran
  2. asked again - "tool browser aku belum jalan di sesi ini" (the browser
                   tool is not running in this session)
  3. asked again - an answer that read as the finished task, while the console
                   terminal showed nothing happening

Three symptoms, one investigation. (1) and the "announced but nothing called"
bug are the same defect and are covered by
`scripts/check_announced_not_called.py`. This file is about (3).

## What was measured

`web_search` was called directly on the live machine. For the query
"Odin 2 Mini price Indonesia" it returned:

    success: true
    engine:  wikipedia
    results: List of Falcon 9 and Falcon Heavy launches
             IKEA
             Citroen 2CV
             Frozen (2013 film)

Those rows are there because the query contains "odin" (the Norse god) and
"mini" (a Starlink satellite in one of the snippets). Four of the six query
terms are significant - odin, mini, price, indonesia - and the relevance rule
required only half of them, so two coincidental words were enough to pass. The
tool reported success, the model was handed the rows as a real search result,
and it repeated them as an answer. That is symptom (3): something that looks
like a completed task, and no useful work behind it.

Driving the same episode through the live 9router model showed the model is
NOT at fault here - it called `web_search` (twice), `browser_navigate` and
`browser_fetch`, and then told the user honestly that every source came back
empty. The model was reasoning correctly from a result set the tool had
mislabeled.

## What was fixed, and what was not

FIXED, and provable: `_search_news_and_reference` is the FIRST source tried,
and it returned on `success` alone without the relevance gate that the scraped
engines below it already applied. An irrelevant set from it short-circuited
every later engine - including the scraped Bing and DuckDuckGo paths that would
have searched for real. (The chain then ended in a browser leg too, since
removed: a lookup must not launch a browser - see
check_browser_launch_policy.py.) The gate now applies there too, so a set that
fails relevance is marked failed and the chain continues.

FIXED (the labelling): `_tool_result_message` told the model the tool
"succeeded" and handed over the rows with nothing to distinguish them from an
answer, so the model presented them as one. A search result now carries which
query words it covers and which it misses, and the message reports that.

NOT FIXED, and recorded rather than hidden: relevance itself is not graded.
Four discriminators were tested against these exact row sets - longest-term-
as-subject, hit ratio, per-document co-occurrence, and term-occurrence count -
and none separated the irrelevant Odin set from a legitimate thin one (a
Jakarta news snippet matching only "jakarta"). A rule tuned to reject the
first also rejects the second, and suppressing real results is worse than the
bug. So the tool states coverage, and the model judges. The assertions below
hold that line: the Jakarta set is treated the same as the Odin one.

Tests here need no network: the row sets are the recorded ones, replayed
through mocked engines.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_web_search_relevance.py
"""

import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f" — {detail}" if not cond and detail else ""))


# The live query and the live result set, kept verbatim: this is what the tool
# returned and what the model repeated to the user.
ODIN_QUERY = "Odin 2 Mini price Indonesia"
ODIN_GARBAGE = {
    "success": True,
    "engine": "wikipedia",
    "results": [
        {"title": "List of Falcon 9 and Falcon Heavy launches",
         "context": "SpaceX launches Starlink V2 Mini satellites"},
        {"title": "IKEA", "context": "a song dedicated to Odin and IKEA"},
        {"title": "Citro\u00ebn 2CV", "context": "Odin, L.C. World in Motion"},
        {"title": "Frozen (2013 film)", "context": "a Disney animated film"},
    ],
}

ODIN_GOOD = {
    "success": True,
    "engine": "bing",
    "results": [
        {"title": "AYN Odin 2 Mini price Indonesia - Tokopedia",
         "context": "harga Odin 2 Mini di Indonesia"},
    ],
}

# A legitimate result set that a stricter rule was found to break. Kept because
# it is the evidence for why the rule was not tightened.
JAKARTA_QUERY = "apa yang sedang ramai dibicarakan di Jakarta hari ini"
JAKARTA_RESULT = {
    "success": True,
    "engine": "google-news",
    "snippet": "1. Jakarta Barat Darurat Begal - VIVA.co.id\n"
               "2. Pesawat Tempur di Langit Jakarta - detikNews",
    "results": [{"title": "Jakarta Barat Darurat Begal - VIVA.co.id"}],
}


def _handler(registry):
    return registry.skill_registry.get("web_search").handler


def relevance_state(registry) -> None:
    """The state of the rule, asserted as it is - see the docstring."""
    check("the Odin row set is NOT rejected by the current rule "
          "(the unfixed half, recorded)",
          registry._looks_relevant(ODIN_QUERY, ODIN_GARBAGE) is True,
          "if this now fails, the rule was tightened - update the docstring "
          "and keep the new behaviour, but check the Jakarta set below still "
          "passes")
    check("a genuine Odin 2 Mini result passes",
          registry._looks_relevant(ODIN_QUERY, ODIN_GOOD) is True)
    check("the legitimate Jakarta set still passes, so the rule is not "
          "over-tightened",
          registry._looks_relevant(JAKARTA_QUERY, JAKARTA_RESULT) is True,
          "a rule that rejects this is worse than the bug it fixes")


def coverage_is_reported(registry) -> None:
    """The fixed half: the model is told what the results cover, instead of
    being told the tool succeeded and left to assume the rest."""
    payload = registry._search_payload(
        ODIN_QUERY, "wikipedia", ODIN_GARBAGE["results"])

    check("an incomplete result set is marked partial, not strong",
          payload.get("quality") == "partial",
          f"quality={payload.get('quality')}")
    check("the note names the words that appear and those that do not",
          "odin" in str(payload.get("note")) and "price" in str(payload.get("note")),
          payload.get("note"))
    check("the note does not assert that the results are wrong",
          "do not present" not in str(payload.get("note")).lower(),
          "grading was abandoned - see the docstring; the note states "
          "coverage and lets the model decide")

    full = registry._search_payload(
        "RTX 4090 price", "bing",
        [{"title": "RTX 4090 price", "context": "price for the RTX 4090"}])
    check("a result set covering every word is strong",
          full.get("quality") == "strong",
          f"quality={full.get('quality')}")
    check("a strong set carries no note",
          not full.get("note"), full.get("note"))

    # And the legitimate thin set is treated the SAME as the irrelevant one -
    # the whole point of not grading. Both are partial; neither is singled out.
    jk = registry._search_payload(
        JAKARTA_QUERY, "google-news",
        [{"title": "Jakarta Barat Darurat Begal - VIVA.co.id",
          "context": "Jakarta"}])
    check("the legitimate Jakarta set is partial exactly like the Odin set",
          jk.get("quality") == "partial",
          f"quality={jk.get('quality')} - grading it down would suppress a "
          f"real result")


def the_message_stops_asserting_success(registry) -> None:
    """The seam the model actually reads."""
    from backend.skills.tool_loop import _tool_result_message

    weak = registry._search_payload(
        ODIN_QUERY, "wikipedia", ODIN_GARBAGE["results"])
    msg = _tool_result_message("web_search", {"success": True, "data": weak})
    content = msg["content"]

    check("a partial result does not reach the model as plain 'succeeded'",
          not content.startswith("Tool 'web_search' succeeded"),
          content[:80])
    check("the message says the results only partly cover the query",
          "partly cover" in content, content[:120])
    check("the coverage note is carried into the message",
          "price" in content, "the model must see what was missing")

    strong = registry._search_payload(
        "RTX 4090 price", "bing",
        [{"title": "RTX 4090 price", "context": "price for the RTX 4090"}])
    smsg = _tool_result_message("web_search", {"success": True, "data": strong})
    # Tool output is now wrapped in the untrusted envelope, so the claim is
    # looked for inside it rather than at position zero.
    check("a strong result still reads as a success",
          "Tool 'web_search' succeeded." in smsg["content"],
          smsg["content"][:80])

    plain = _tool_result_message(
        "run_command", {"success": True, "data": {"stdout": "hi"}})
    check("a skill that sets no quality is unaffected",
          "Tool 'run_command' succeeded." in plain["content"],
          plain["content"][:80])


def chain_applies_the_gate(registry) -> None:
    """The fixed half: the first source no longer short-circuits the chain with
    a set that fails relevance."""
    handler = _handler(registry)
    called: list[str] = []

    async def irrelevant_first(query):
        return dict(ODIN_GARBAGE)

    async def no_news(query):
        return {"success": False, "error": "no results"}

    async def good_bing(query):
        called.append("bing")
        return dict(ODIN_GOOD)

    async def not_reached(query, timeout=8.0):
        called.append("ddg")
        return {"success": False, "error": "timed out"}

    # Force the FIRST source's set to fail relevance, so the chain must go on.
    # A rule-strict enough to reject it is patched in, because the real rule
    # (asserted above) does not - this isolates the WIRING, which is the fix.
    def reject_odin_rows(query, result):
        return result is not ODIN_GARBAGE and result.get("engine") != "wikipedia"

    registry._ENGINE_FAILURES.clear()
    with mock.patch.object(registry, "_search_news_and_reference",
                           irrelevant_first), \
         mock.patch.object(registry, "_search_bing_news", no_news), \
         mock.patch.object(registry, "_search_bing", good_bing), \
         mock.patch.object(registry, "_search_ddg_html", not_reached), \
         mock.patch.object(registry, "_looks_relevant", reject_odin_rows), \
         mock.patch.object(registry, "_engine_ready", lambda name: True):
        result = asyncio.run(handler({"query": ODIN_QUERY}))

    check("a first source that fails relevance does not end the search",
          result.get("engine") == "bing",
          f"engine={result.get('engine')} - the later engines never ran, which "
          f"is exactly what happened live")
    check("the later engine was actually called",
          called == ["bing"], called)
    check("the irrelevant rows are not in what the model receives",
          "Falcon" not in str(result) and "IKEA" not in str(result))
    check("the first source is recorded as failed, not as a success",
          "news+reference" in registry._ENGINE_FAILURES,
          sorted(registry._ENGINE_FAILURES))
    registry._ENGINE_FAILURES.clear()


def main() -> int:
    from backend.skills import registry

    print("web_search relevance against the live episode:")
    relevance_state(registry)
    print()
    print("results report their coverage instead of a bare success:")
    coverage_is_reported(registry)
    print()
    print("the message the model reads no longer overstates the evidence:")
    the_message_stops_asserting_success(registry)
    print()
    print("the chain no longer stops at an irrelevant first source:")
    chain_applies_the_gate(registry)
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: web_search coverage reporting, first-source fallthrough "
          "and the state of the relevance rule")
    return 0


if __name__ == "__main__":
    sys.exit(main())
