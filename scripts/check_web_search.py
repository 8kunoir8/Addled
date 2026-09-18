"""Checks for web_search: it must find things, and must not invent relevance.

Reported symptom: asking what was happening in Jakarta got "I cannot access
social media", and asking about the Nintendo 3DS got "I did not find relevant
information". Both were caused here. The primary engine, DuckDuckGo, is
unreachable on that network - every endpoint times out - and the fallback,
scraped Bing HTML, answers with results for a different query entirely (APA
citation generators for a Jakarta question, Japanese song lyrics for a game
console one). The model was handed that and correctly reported finding nothing.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_web_search.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


# Real result sets recorded from the machine this was reported on.
JAKARTA_QUERY = "apa yang sedang ramai dibicarakan di Jakarta hari ini"
APA_RESULT = {
    "success": True,
    "engine": "bing",
    "snippet": (
        "1. American Psychological Association (APA) - https://www.apa.org/\n"
        "   The American Psychological Association (APA) is a scientific and "
        "professional organization that represents psychologists.\n"
        "2. Free APA Citation Generator [Updated for 2026] - MyBib\n"
        "   Generate APA style citations quickly and accurately."
    ),
    "results": [
        {"title": "American Psychological Association (APA)",
         "url": "https://www.apa.org/"},
        {"title": "Free APA Citation Generator [Updated for 2026] - MyBib",
         "url": "https://www.mybib.com/tools/apa-citation-generator"},
    ],
}

CONSOLE_QUERY = "Nintendo 3DS vs retro handheld performance comparison"
LYRICS_RESULT = {
    "success": True,
    "engine": "bing",
    "snippet": (
        "1. \u3084\u3082\u308a (\u68ee\u5c71\u826f\u5b50\u3068\u77e2\u91ce\u9855\u5b50) "
        "\u98a8\u306e\u30d6\u30e9\u30f3\u30b3 \u6b4c\u8a5e\n"
        "   https://www.uta-net.com/song/98257/\n"
        "2. \u98a8\u306e\u30d6\u30e9\u30f3\u30b3 - YouTube\n"
        "   https://www.youtube.com/watch?v=fJrBi2cVmNc"
    ),
    "results": [
        {"title": "\u3084\u3082\u308a \u98a8\u306e\u30d6\u30e9\u30f3\u30b3 \u6b4c\u8a5e",
         "url": "https://www.uta-net.com/song/98257/"},
    ],
}

GOOD_RESULT = {
    "success": True,
    "engine": "google-news",
    "snippet": ("1. Jakarta Barat Darurat Begal - VIVA.co.id\n"
                "   VIVA.co.id Tue, 19 May 2026 07:00:00 GMT\n"
                "2. Pesawat Tempur di Langit Jakarta - detikNews"),
    "results": [
        {"title": "Jakarta Barat Darurat Begal - VIVA.co.id",
         "url": "https://www.viva.co.id/", "source": "VIVA.co.id"},
    ],
}

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>Jakarta Barat Darurat Begal - VIVA.co.id</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <pubDate>Tue, 19 May 2026 07:00:00 GMT</pubDate>
  <source url="https://www.viva.co.id">VIVA.co.id</source>
</item>
<item>
  <title>Older item - Kompas</title>
  <link>https://news.google.com/rss/articles/def</link>
  <pubDate>Thu, 10 Sep 2020 02:22:09 GMT</pubDate>
  <source url="https://kompas.com">Kompas.com</source>
</item>
</channel></rss>"""


def relevance_checks(registry) -> None:
    check("an APA result set is rejected for a Jakarta question",
          registry._looks_relevant(JAKARTA_QUERY, APA_RESULT) is False)
    check("song lyrics are rejected for a console question",
          registry._looks_relevant(CONSOLE_QUERY, LYRICS_RESULT) is False)
    check("a genuine Jakarta result set is accepted",
          registry._looks_relevant(JAKARTA_QUERY, GOOD_RESULT) is True)
    check("a query of only stopwords is not judged at all",
          registry._looks_relevant("apa yang dan ini", APA_RESULT) is True)
    check("recency wording is recognised",
          registry._wants_current("apa yang ramai hari ini") is True)
    check("a reference question is not treated as recency",
          registry._wants_current("python asyncio tutorial") is False)
    check("'hot' does not fire on 'hotel'",
          registry._wants_current("hotel near Jakarta airport") is False)


def rss_checks(registry) -> None:
    items = registry._rss_items(RSS_SAMPLE, limit=5)
    check("the feed parser returns every item", len(items) == 2, len(items))
    check("the feed parser reads title, link, source and date",
          bool(items) and items[0]["source"] == "VIVA.co.id"
          and items[0]["published"].startswith("Tue, 19 May 2026")
          and items[0]["url"].endswith("abc"),
          items[0] if items else None)
    check("the feed parser survives junk",
          registry._rss_items("<not xml", limit=5) == [])
    ordered = sorted(items, key=registry._published_key, reverse=True)
    check("newest first", ordered and ordered[0]["source"] == "VIVA.co.id",
          [i["source"] for i in ordered])


def engine_health_checks(registry) -> None:
    registry._ENGINE_FAILURES.clear()
    check("a fresh engine is usable", registry._engine_ready("test-engine"))
    registry._record_engine("test-engine", False)
    registry._record_engine("test-engine", False)
    check("two failures stand an engine down",
          registry._engine_ready("test-engine") is False)
    registry._record_engine("test-engine", True)
    check("a success restores it",
          registry._engine_ready("test-engine") is True)
    registry._ENGINE_FAILURES.clear()


class _FakeBrowser:
    """Stands in for the real browser, returning the same junk Bing served."""

    async def navigate(self, url: str) -> dict:
        return {"success": True, "url": url, "title": "Bing"}

    async def extract(self) -> dict:
        return {"text": APA_RESULT["snippet"]}


def chain_checks(registry) -> None:
    """The chain must stop at the first usable source, and never hand the model
    a scraped result set that does not answer the question."""
    handler = registry.skill_registry.get("web_search").handler
    scrapers_called: list[str] = []

    async def fail_primary(query):
        return {"success": False, "error": "no results"}

    async def fail_news(query):
        return {"success": False, "error": "no results"}

    async def irrelevant_bing(query):
        scrapers_called.append("bing")
        return dict(APA_RESULT)

    async def tracked_ddg(query, timeout=8.0):
        scrapers_called.append("ddg")
        return {"success": False, "error": "timed out"}

    registry._ENGINE_FAILURES.clear()
    with mock.patch.object(registry, "_search_news_and_reference", fail_primary), \
         mock.patch.object(registry, "_search_bing_news", fail_news), \
         mock.patch.object(registry, "_search_bing", irrelevant_bing), \
         mock.patch.object(registry, "_search_ddg_html", tracked_ddg), \
         mock.patch("backend.browser.browser_engine.browser", _FakeBrowser()), \
         mock.patch.object(registry, "_engine_ready", lambda name: True):
        result = asyncio.run(handler({"query": JAKARTA_QUERY}))

    check("an irrelevant scraped result is never returned",
          result.get("success") is False,
          f"success={result.get('success')} engine={result.get('engine')}")
    check("both useless engines are recorded as failed",
          {"bing", "ddg-html"} <= set(registry._ENGINE_FAILURES),
          sorted(registry._ENGINE_FAILURES))
    check("the failure explains itself",
          "did not match the query" in str(result.get("error")),
          result.get("error"))
    check("every path was actually tried",
          scrapers_called == ["bing", "ddg"], scrapers_called)
    registry._ENGINE_FAILURES.clear()

    # And with a working primary source, the scrapers are not touched at all.
    scrapers_called.clear()

    async def good_primary(query):
        return dict(GOOD_RESULT)

    with mock.patch.object(registry, "_search_news_and_reference", good_primary), \
         mock.patch.object(registry, "_search_bing", irrelevant_bing), \
         mock.patch.object(registry, "_search_ddg_html", tracked_ddg), \
         mock.patch.object(registry, "_engine_ready", lambda name: True):
        result = asyncio.run(handler({"query": JAKARTA_QUERY}))

    check("a working source short-circuits the scrapers",
          result.get("success") is True and not scrapers_called,
          f"engine={result.get('engine')} scrapers={scrapers_called}")

    check("an empty query is refused",
          asyncio.run(handler({"query": "   "})).get("success") is False)


def main() -> int:
    from backend.skills import registry

    relevance_checks(registry)
    rss_checks(registry)
    engine_health_checks(registry)
    chain_checks(registry)
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: web_search sources, relevance gate and fallback chain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
