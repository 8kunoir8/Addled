"""When may Addled put a Chromium window on the user's screen?

The reported defect, 2026-10-17: the user asked *"how much is ASUS ProArt P16
priced in indonesia?"* and got a Chromium window over their screen. The log for
that turn contains two `web_search` calls and NO `browser_*` tool call at all -
the model never chose to browse. The window came from inside `web_search`,
which ended with a last-resort leg that drove a real browser at Bing.

So the contract this file pins is not "the model should not browse for
questions". It is:

    Code with a tool-shaped name may start a browser.
    Code whose job is to FIND SOMETHING OUT may not.

`browser_navigate` and `browser_task` ARE the browse - the user asked, a window
is the point. `web_search` and `web_fetch` are lookups; they answer over plain
HTTP or they say they could not. The forge's research step is a lookup too.

Everything here is asserted by RECORDING calls, not by reading source, so it
cannot pass while the behaviour is wrong. No network, no model, no flake.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_browser_launch_policy.py
"""

from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.browser.browser_engine import PlaywrightBrowser  # noqa: E402
from backend.skills.registry import SkillRegistry  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}"
          + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


class Recorder:
    """Stands in for the browser singleton and records what was asked of it.

    Only `navigate` is real enough to answer; it is what the search and fetch
    paths call. Every launch starts here, so counting the calls counts the
    launches.
    """

    def __init__(self, success: bool = True):
        self.calls: list[tuple[str, bool]] = []
        self.success = success

    async def navigate(self, url: str, *, explicit: bool = True) -> dict:
        self.calls.append((url, explicit))
        if not self.success:
            return {"success": False, "error": "no source"}
        return {"success": True, "url": url, "title": "stub", "engine": "http"}

    async def extract(self, selector: str | None = None) -> dict:
        return {"success": True, "url": "", "title": "stub",
                "text": "stub page text", "engine": "http"}

    @property
    def launched(self) -> list[tuple[str, bool]]:
        return list(self.calls)


def _patch(recorder: Recorder) -> None:
    import backend.browser.browser_engine as be
    import backend.skills.registry as sr
    be.browser = recorder
    # The handlers import the singleton lazily, inside the function body, so
    # patching the module attribute is enough for every one of them.
    #
    # Every engine is stood down too. They are found by global lookup inside
    # `web_search`, so replacing the module attributes makes the whole chain
    # fail here instead of on the wire - the suite stays offline and cannot
    # flake on whether one feed happens to answer today.
    async def dead(query):
        return {"success": False, "error": "stubbed out for this suite"}

    for fn in ("_search_news_and_reference", "_search_bing_news",
               "_search_bing", "_search_ddg_html"):
        setattr(sr, fn, dead)


def main() -> int:
    # ── 1. A LOOKUP never starts a browser ──────────────────────────────────
    #
    # This is the reported defect. On the pre-fix code the browser leg at the
    # end of the search chain recorded a call to `navigate`, and to a Bing URL.
    reg = SkillRegistry()
    rec = Recorder(success=False)          # every engine fails
    _patch(rec)
    handler = reg.get("web_search").handler
    result = asyncio.run(handler({"query": "ASUS ProArt P16 harga Indonesia"}))
    check("web_search on a price question launches no browser",
          not rec.launched, f"launched {rec.launched}")
    check("web_search says it found nothing rather than browsing",
          result.get("success") is False, f"success={result.get('success')}")

    # The model must be pointed at the browse tools, not left to conclude it
    # has no internet. The search chain cannot answer everything, and the
    # difference between "I could not" and "I can, if you want me to look at
    # the page" is the whole point of removing the silent fallback.
    #
    # Asserted through `_tool_result_message`, NOT by reading the dict key: the
    # first version of this check looked for a `suggestion` key, which the
    # formatter never forwards, so it would have passed on guidance no model
    # ever saw.
    from backend.skills.tool_loop import _tool_result_message
    content = str(_tool_result_message("web_search", result).get("content") or "")
    check("web_search names the tools that CAN browse, in what the model reads",
          "browser_navigate" in content and "browser_task" in content,
          content[-110:])
    # The whole result is wrapped as untrusted tool output (it is web data),
    # so the failure line is inside that envelope rather than at the start.
    check("...and that naming reaches the model on the failure path",
          "Tool 'web_search' failed" in content
          and "UNTRUSTED TOOL OUTPUT" in content,
          content[:70])

    # The same wording must survive the real skill seam, not only the raw dict.
    from backend.skills.tool_loop import execute_skill
    _patch(Recorder(success=False))
    outer = asyncio.run(execute_skill("web_search", {"query": "price in Bali"}))
    outer_content = str(
        _tool_result_message("web_search", outer).get("content") or "")
    check("the guidance survives execute_skill",
          "browser_navigate" in outer_content, outer_content[-110:])

    # ── 2. web_fetch is a lookup too ────────────────────────────────────────
    #
    # Before this, `web_fetch` called `navigate()` with no flag and Playwright
    # was attempted BEFORE the plain fetch - so fetching any ordinary page
    # raised a window.
    reg2 = SkillRegistry()
    rec2 = Recorder(success=True)
    _patch(rec2)
    fetch = reg2.get("web_fetch").handler
    asyncio.run(fetch({"url": "https://example.com/article"}))
    check("web_fetch marks itself as not-a-browse",
          all(explicit is False for _, explicit in rec2.launched),
          f"called with {rec2.launched}")

    # ── 3. The EXPLICIT paths keep the browser ──────────────────────────────
    #
    # The other half of the contract. If this stops being true the feature is
    # gone rather than quieter, which is a different bug.
    reg3 = SkillRegistry()
    rec3 = Recorder(success=True)
    _patch(rec3)
    nav = reg3.get("browser_navigate").handler
    asyncio.run(nav({"url": "https://example.com"}))
    check("browser_navigate is an explicit browse",
          len(rec3.launched) == 1 and rec3.launched[0][1] is True,
          f"called with {rec3.launched}")

    # ── 4. Ordering: the cheap path is tried before the browser ─────────────
    #
    # Asserted on the real `navigate`, because the ordering IS the behaviour:
    # an implicit fetch must reach for HTTP first and only escalate when that
    # yielded nothing readable.
    class Page:
        is_closed = lambda self: False                      # noqa: E731

    async def http_ok(self, url):
        self._http_text = "x" * 500                         # readable
        self._using_http = True
        self._state.url = url
        return {"success": True, "url": url, "title": "t", "engine": "http"}

    async def http_empty(self, url):
        self._http_text = ""
        self._using_http = False
        self._state.url = url
        return {"success": True, "url": url, "title": "t", "engine": "http"}

    # A browser that is detectable if it is reached.
    reached = []

    async def ensure_browser(self):
        reached.append("playwright")
        return False                                        # unavailable

    B = PlaywrightBrowser()
    B._fetch_http = http_ok.__get__(B)
    B._ensure_browser = ensure_browser.__get__(B)
    got = asyncio.run(B.navigate("https://en.wikipedia.org/wiki/ASUS",
                                 explicit=False))
    check("an implicit fetch serves a readable page over HTTP",
          got.get("engine") == "http", f"engine={got.get('engine')}")
    check("...and never reaches for the browser",
          not reached, f"reached {reached}")

    reached.clear()
    B2 = PlaywrightBrowser()
    B2._fetch_http = http_empty.__get__(B2)
    B2._ensure_browser = ensure_browser.__get__(B2)
    B2._page = Page()          # a browser is notionally up, so nothing closes
    asyncio.run(B2.navigate("https://www.google.com/search?q=x",
                            explicit=False))
    check("an implicit fetch escalates when HTTP yields no usable text",
          reached == ["playwright"], f"reached {reached}")

    # An EXPLICIT browse reaches for the browser FIRST. It still falls back to
    # HTTP when there is no browser to be had - that fallback is pre-existing
    # and correct - so what is asserted is the ORDER, which is the bug.
    order: list[str] = []

    async def browser_first(self):
        order.append("browser")
        return False                                        # unavailable

    async def http_tracked(self, url):
        order.append("http")
        return {"success": True, "url": url, "title": "t", "engine": "http"}

    B3 = PlaywrightBrowser()
    B3._fetch_http = http_tracked.__get__(B3)
    B3._ensure_browser = browser_first.__get__(B3)
    asyncio.run(B3.navigate("https://example.com"))
    check("an explicit browse reaches for the browser before HTTP",
          order[:1] == ["browser"], f"order={order}")

    # The implicit case is the mirror image, and the fix.
    order.clear()
    B3b = PlaywrightBrowser()
    B3b._fetch_http = http_tracked.__get__(B3b)
    B3b._ensure_browser = browser_first.__get__(B3b)
    asyncio.run(B3b.navigate("https://example.com", explicit=False))
    check("an implicit fetch reaches for HTTP before the browser",
          order[:1] == ["http"], f"order={order}")

    # ── 5. An implicitly-started browser does not stay resident ─────────────
    #
    # `close()` is only reachable from the `browser.close` RPC, so before this
    # a single fallback left Chromium running for the rest of the day.
    B4 = PlaywrightBrowser()
    closed = []

    async def fake_close(self):
        closed.append(True)
        self._page = None
        return {"success": True}

    B4.close = fake_close.__get__(B4)
    B4._page = Page()
    asyncio.run(B4._release_implicit_browser())
    check("an implicitly started browser is closed again",
          closed == [True], f"closed={closed}")

    # ...but a browser the user already had open is NOT ours to close.
    B5 = PlaywrightBrowser()
    closed.clear()
    B5.close = fake_close.__get__(B5)
    B5._page = Page()
    B5._cdp = object()          # attached to the user's own browser
    asyncio.run(B5._release_implicit_browser())
    check("a user's own CDP browser is never closed by us",
          not closed, f"closed={closed}")

    # ── 6. The windowing refusal is about launching, not about reading ──────
    #
    # `headless: False` is deliberate and must stay: the browser is visible on
    # purpose, so that a window on screen always means a browse was asked for.
    # check_browser_extract.py asserts the same thing; this states why.
    from backend.browser.browser_engine import _LAUNCH_OPTIONS
    check("the browser stays visible when it is launched",
          _LAUNCH_OPTIONS.get("headless") is False, str(_LAUNCH_OPTIONS))

    # ── 7. The check above is not vacuous ───────────────────────────────────
    #
    # A recorder that is never wired up would pass section 1 for the wrong
    # reason. So put the REMOVED leg back - a `web_search` that drives the
    # browser at Bing, exactly as it used to - and confirm the same assertion
    # catches it. Without this, deleting the recorder call would make the
    # suite greener.
    async def search_that_browses(params: dict) -> dict:
        from backend.browser.browser_engine import browser
        await browser.navigate(
            "https://www.bing.com/search?q=" + str(params.get("query", "")))
        return {"success": False, "error": "nothing found"}

    reg4 = SkillRegistry()
    rec4 = Recorder(success=True)
    _patch(rec4)
    reg4._skills["web_search"].handler = search_that_browses
    asyncio.run(reg4.get("web_search").handler({"query": "a price question"}))
    check("a web_search that DOES browse is caught by the check above",
          bool(rec4.launched), f"launched {rec4.launched}")
    check("...and it is caught reaching for Bing specifically",
          any("bing.com/search" in url for url, _ in rec4.launched),
          f"launched {rec4.launched}")

    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("PASS: a lookup never launches a browser, a browse always can")
    return 0


if __name__ == "__main__":
    sys.exit(main())
