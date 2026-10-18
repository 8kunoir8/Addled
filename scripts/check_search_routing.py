"""Which tool does a search request get offered - and which one must it not.

The goal this pins: Chromium opens when the user asks to OPERATE a page, not
when they ask to FIND SOMETHING OUT. Addled has two ways to reach the web and
they are not interchangeable:

  web_search     cheap, keyless, news feeds + Wikipedia + scraped HTML.
                 It DOES answer price/availability questions - measured
                 2026-10-17 on "ASUS ProArt P16 harga Indonesia", it returned
                 Indonesian listings with real rupiah prices over plain HTTP.
                 An earlier version of this comment claimed the opposite
                 ("the free engines return junk for them") and that claim is
                 what justified putting a Chromium window at the end of the
                 search chain. Corrected, because the code followed it.
  browser_*      a real Chromium, slow, visible. Used when the user asks to
                 OPERATE a page, or when a page cannot be read without one

## What was measured

Driven against the live provider (9router / deepseek-v4.1-flash) via the real
`filter_for_query`, 2026-10-16:

  "google who won the world cup"          -> web_search      (browser OFFERED, not chosen)
  "look online for python tutorials"      -> web_search, web_search
  "how much is the Odin 2 Mini..."        -> web_search, web_search
  "browse the web and find the cheapest flight..."  -> browser_task   <- Chromium
  "open google.com and search for pizza"  -> browser_navigate          <- Chromium
  "use the browser to check the price..." -> browser_navigate          <- Chromium

Two conclusions, and this file encodes both.

FIRST: the model is not being fooled. "google who won the world cup" was
offered all five browser tools and still chose `web_search`. So the fix is not
to hide the browser from it - it is to stop PRESENTING an instruction to browse
when the user only asked a question. "browse the web", "open google.com" and
"use the browser" are literal commands, and the model obeys them correctly.

SECOND: `browser_task` is the one genuine over-reach. For an open-ended research
ask ("cheapest flight ... list the top 3") it was chosen over the free path,
twice out of two. An open-ended question should try `web_search` first because
the free engines answer many of them, and the browser is ~100x the cost.

## Why the split is by verb, not by topic

"open the website example.com" and "find the price of X" are both about the web,
so they cannot be told apart by subject. They are told apart by what the user
wants DONE: navigate/click/type/log in is an operation on a page; look up /
search / google / browse is a question. Hence the intent table splits into an
"operate" group and a "look up" group, and only the first exposes the browser
tools that drive a page.

This is a routing contract, so it is asserted on the OFFERED SET, not on what a
model then picks. No network, no model, no flake.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_search_routing.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.skills.registry import SkillRegistry  # noqa: E402

FAILS: list[str] = []

PAGE_TOOLS = {"browser_navigate", "browser_click", "browser_type",
              "browser_extract"}
TASK_TOOL = "browser_task"


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    reg = SkillRegistry()

    def offered(q: str) -> set[str]:
        return reg.filter_for_query(q)

    def page_tools(q: str) -> set[str]:
        return offered(q) & (PAGE_TOOLS | {TASK_TOOL})

    # ── 1. A QUESTION must not come with an instruction to drive a page ──────
    #
    # Each of these either names the act of looking up, or is a plain question.
    # The first two are the reported cases: they were offered the full browser
    # set because of a literal word, and the model then browsed on request.
    for q in (
        "google who won the world cup",
        "look online for python tutorials",
        "search the internet for the best pizza in Rome",
        "what is the capital of France",
        "what happened in the news today",
        "search the web for rust news",
    ):
        got = page_tools(q)
        check(f"a question offers no page-driving tool: {q!r}",
              not got, f"offered {sorted(got)}")
        check(f"a question still offers web_search: {q!r}",
              "web_search" in offered(q))

    # ── 2. An OPERATION on a page keeps its tools ───────────────────────────
    #
    # The other half of the contract. Narrowing the routing must not make the
    # browser unreachable, or the feature is gone rather than quieter - the
    # reachability suite guards the same thing, and this states the intent.
    for q, must in (
        ("open the website example.com", "browser_navigate"),
        ("go to google.com", "browser_navigate"),
        ("click the login button on the page", "browser_click"),
        ("type my email into the form", "browser_type"),
        ("get the text from this page", "browser_extract"),
    ):
        check(f"an operation still offers {must}: {q!r}",
              must in offered(q), f"offered {sorted(page_tools(q))}")

    # ── 3. An open-ended research ask must TRY the cheap path first ─────────
    #
    # Measured: "cheapest flight ... list the top 3" chose browser_task 2/2.
    # The assertion is that web_search is at least offered, so a model that
    # prefers it can; refusing to offer it removes the choice.
    for q in (
        "find the cheapest flight from NYC to Tokyo next Friday and list the top 3",
        "compare prices for the Odin 2 Mini",
    ):
        check(f"a research ask offers web_search as well: {q!r}",
              "web_search" in offered(q), f"offered {sorted(offered(q))}")

    # ── 4. web_fetch belongs to looking things up, not to driving a page ────
    #
    # It is a fetch, not an interaction: it belongs with the read path. Kept
    # as its own assertion because it sits in the browser category in the
    # registry, which is a filing choice, not a routing one.
    for q in ("fetch the page at example.com", "get the contents of this url"):
        check(f"web_fetch is offered for a fetch: {q!r}",
              "web_fetch" in offered(q))

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} routing assertion(s) failed")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("PASS: questions route to web_search, operations route to the browser, "
          "and an open-ended ask can still choose the cheap path")
    return 0


if __name__ == "__main__":
    sys.exit(main())
