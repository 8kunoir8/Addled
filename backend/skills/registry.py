"""
Unified Skill Registry — provider-agnostic function-calling layer.

Every AI provider (DeepSeek, OpenAI, Claude, Gemini, Copilot, Ollama, LM Studio)
can invoke any skill defined here. Skills wrap the existing action executor,
browser, file ops, code engine, and all other agent capabilities.

Architecture:
  AI Provider → tool_call(name, args) → SkillRegistry.execute() → result → Provider
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

log = logging.getLogger("addled.skills")

# How much of a skill's description survives into a text prompt. Providers with
# native tool support get the full schema; providers without it get the whole
# catalogue pasted into the user's message, and there the pretty-printed JSON is
# mostly punctuation. Measured at 56 skills, the verbose form was 18,734
# characters (~4,700 tokens) — more than the local model's entire remaining
# context once its reply budget is set aside.
PROMPT_DESC_CHARS = 110

# Search engine health. Scraped engines rot and networks block them: on the
# machine this was reported from, every DuckDuckGo endpoint timed out, so each
# search spent 15s failing on it before falling through to something that
# worked. Remember which engine just failed and skip it for a while instead.
_ENGINE_FAILURES: dict[str, tuple[int, float]] = {}
_ENGINE_FAIL_LIMIT = 2
_ENGINE_COOLDOWN_S = 600.0


def _engine_ready(name: str) -> bool:
    count, when = _ENGINE_FAILURES.get(name, (0, 0.0))
    if count < _ENGINE_FAIL_LIMIT:
        return True
    return (time.time() - when) >= _ENGINE_COOLDOWN_S


def _record_engine(name: str, ok: bool) -> None:
    if ok:
        _ENGINE_FAILURES.pop(name, None)
        return
    count, _ = _ENGINE_FAILURES.get(name, (0, 0.0))
    _ENGINE_FAILURES[name] = (count + 1, time.time())


@dataclass
class SkillDefinition:
    """Defines a skill that any AI provider can call."""
    name: str
    description: str
    parameters: dict  # JSON Schema for parameters
    handler: Callable[..., Awaitable[dict]]
    category: str = "general"
    requires_approval: bool = False

    # Extra accepted spellings for a parameter, keyed by the real name:
    # `{"path": ("file_path", "filepath", "filename")}`.
    #
    # A local model reliably invents a plausible-but-wrong key — `file_path` for
    # `path` — and the handler then reads `params.get("path", "")`, gets "", and
    # fails with "No path given." That is a recoverable slip reported as a dead
    # end: the model is told nothing it can act on, so it either gives up or
    # invents an explanation. Naming the aliases here turns it back into a call
    # that works, and the schema shown to the model is unchanged.
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # The skill's own instructions, in prose, for a skill that is knowledge
    # rather than code.
    #
    # Until now a skill could only teach something that fitted in `description`:
    # one line, because that is all the prompt catalogue carries. A market skill
    # that documented a five-step procedure had that procedure stored
    # (`market.py` keeps the SKILL.md body) but reachable only by CALLING the
    # skill - and the model decides to call it from the one-line description it
    # already read. So the guidance was there and unusable, which is worse than
    # absent, because it looks like it works.
    #
    # `body` is not put in the prompt. The catalogue stays one line per skill;
    # the body is fetched on demand through `skill_view`, so a long procedure
    # costs a round trip only when the model has decided it wants one. That is
    # the same progressive disclosure the prompt catalogue has always used for
    # the skills themselves.
    body: str = ""

    def normalise(self, params: dict | None) -> dict:
        """Fill real parameter names from their aliases, and report problems.

        Returns `(params, error)`. `error` is a message naming what is missing
        or what was not recognised, so a caller that cannot proceed can say so
        usefully instead of failing on an empty string.

        Never renames a key the caller already supplied correctly, and never
        overwrites a real parameter with an alias value.
        """
        given = dict(params or {})
        props = (self.parameters or {}).get("properties", {}) or {}
        required = list((self.parameters or {}).get("required", []) or [])

        # Alias -> real name, only for parameters this skill actually declares.
        for real, alternatives in (self.aliases or {}).items():
            if real not in props or real in given:
                continue
            for alt in alternatives:
                if alt in given:
                    given[real] = given.pop(alt)
                    break

        # Coerce single scalars into the declared array shape.
        #
        # A model asked for `sources` ("the PDFs to join") very naturally sends
        # one path as a string, because that is how it would say it in English.
        # The handler then iterates the string character by character, the
        # "path" becomes "E", and validation reports "'(no extension)' is not
        # an Office or PDF format" — an error about a file the caller never
        # mentioned. Coercing here means one fix covers every array parameter
        # in every skill, driven by the schema they already declare.
        for name, spec in props.items():
            if (spec or {}).get("type") != "array" or name not in given:
                continue
            value = given[name]
            if value is None or isinstance(value, list):
                continue
            if not (isinstance(value, str)
                    and spec.get("items", {}).get("type") == "string"):
                given[name] = [value]
                continue
            # Splitting a single string into several items is only safe when it
            # cannot be one real path: "Report, Final.pdf" is a legal filename,
            # and splitting it would invent two files that do not exist. Comma
            # and semicolon are the usual spoken separators, so honour them —
            # but only when the whole string is not itself a path. A newline is
            # never part of a filename, so it always splits.
            if "\n" in value and not os.path.exists(value):
                given[name] = [p.strip() for p in value.splitlines() if p.strip()]
            elif re.search(r"[,;]", value) and not os.path.exists(value):
                given[name] = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
            else:
                given[name] = [value]

        # A parameter that arrived under a name nobody knows is worth naming:
        # it is the difference between "No path given" and "you sent file_path,
        # I expected path". Only reported when something required is missing,
        # so a harmless extra key does not block a working call.
        unrecognised = [k for k in given if k not in props]
        missing = [k for k in required if k not in given or given.get(k) in ("", None)]
        if missing:
            trouble = f"missing required parameter(s): {', '.join(missing)}"
            if unrecognised:
                trouble += (f". You sent: {', '.join(sorted(unrecognised))}"
                            f" — this skill takes: {', '.join(sorted(props))}")
            return given, trouble
        return given, ""

    def to_openai_tool(self) -> dict:
        """OpenAI/DeepSeek function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_claude_tool(self) -> dict:
        """Anthropic Claude tool-use format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_prompt_desc(self) -> str:
        """One line per tool, for providers with no native tool support.

        Only the parameter *names* affect what the model can do, so the
        schemas are not spelled out; the description is clipped so that one
        verbose skill cannot crowd the rest out of the window. The full
        schema still reaches providers that take a ``tools`` argument.
        """
        props = self.parameters.get("properties", {}) or {}
        required = set(self.parameters.get("required", []) or [])
        args = ", ".join(name if name in required else f"{name}?"
                         for name in props)
        desc = " ".join((self.description or "").split())
        if len(desc) > PROMPT_DESC_CHARS:
            desc = desc[:PROMPT_DESC_CHARS].rstrip() + "…"
        return f"{self.name}({args}) — {desc}"


# How much of a skill body `skill_view` will return in one go.
#
# A body is prose a person wrote to instruct a model, so the useful ones are
# short; a 200k-character SKILL.md is almost always a bundled reference document
# that belongs in a file, not a tool result. The cap is a backstop against a
# single call eating the whole window, not a target.
_SKILL_BODY_CHARS = 24000

@dataclass

class SkillResult:
    success: bool
    skill_name: str
    data: dict = field(default_factory=dict)
    error: str | None = None
    summary: str = ""


async def _search_ddg_html(query: str, timeout: float = 8.0) -> dict:
    """Scrape DuckDuckGo's HTML endpoint with stdlib only (no Playwright)."""
    import asyncio
    import html as _html
    import re
    import urllib.parse
    import urllib.request

    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)

    def _fetch() -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    try:
        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _fetch)
    except Exception as e:
        return {"success": False, "error": f"Search request failed: {e}"}

    titles = []
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S):
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if title:
            titles.append({"title": title, "url": _html.unescape(m.group(1))})

    snippets = [
        _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        for s in re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    ]

    if not titles:
        return {"success": False, "error": "Search returned no results"}

    lines = []
    for i, t in enumerate(titles[:5]):
        lines.append(f"{i + 1}. {t['title']} — {t['url']}")
        if i < len(snippets) and snippets[i]:
            lines.append(f"   {snippets[i]}")
    return {
        "success": True,
        "query": query,
        "engine": "ddg-html",
        "snippet": "\n".join(lines)[:2000],
        "results": titles[:5],
    }


def _decode_bing_url(href: str) -> str:
    """Bing wraps result links in /ck/a redirects — decode to the real URL."""
    import base64
    import urllib.parse
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        u = q.get("u", [""])[0]
        if u.startswith("a1"):
            decoded = base64.urlsafe_b64decode(u[2:] + "==")
            return decoded.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return href


async def _search_bing(query: str) -> dict:
    """Scrape Bing's HTML results (fallback for networks that block DDG)."""
    import asyncio
    import html as _html
    import re
    import urllib.parse
    import urllib.request

    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)

    def _fetch() -> str:
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36"),
        })
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    try:
        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(None, _fetch)
    except Exception as e:
        return {"success": False, "error": f"Bing request failed: {e}"}

    results = []
    for m in re.finditer(
            r'<li class="b_algo".*?<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            page, re.S):
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if title:
            results.append({"title": title,
                            "url": _decode_bing_url(_html.unescape(m.group(1)))})
    if not results:
        return {"success": False, "error": "Bing returned no results"}

    # Capture result snippets (shown under each title) for context
    caps = [
        _html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
        for c in re.findall(
            r'<div class="b_caption".*?<p[^>]*>(.*?)</p>', page, re.S)
    ]

    lines = []
    for i, r in enumerate(results[:5]):
        lines.append(f"{i + 1}. {r['title']} — {r['url']}")
        if i < len(caps) and caps[i]:
            lines.append(f"   {caps[i]}")
    return {
        "success": True,
        "query": query,
        "engine": "bing",
        "snippet": "\n".join(lines)[:2000],
        "results": results[:5],
    }


# ── Sources that do not depend on scraping ──────────────────────────────

_SEARCH_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Words that carry no signal when deciding whether a result answers a question.
_SEARCH_STOPWORDS = {
    "the", "and", "for", "with", "what", "whats", "who", "how", "why", "when",
    "where", "which", "this", "that", "these", "those", "from", "about",
    "are", "was", "were", "does", "did", "can", "could", "get", "any", "some",
    "apa", "yang", "dan", "untuk", "dengan", "adalah", "itu", "ini", "saja",
    "juga", "tidak", "bisa", "hari", "sedang", "lagi", "dari", "akan", "ada",
    "saya", "kamu", "kita", "mereka", "atau", "karena", "kalau", "mau",
}


def _http_get(url: str, timeout: float = 10.0) -> str:
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": _SEARCH_UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _rss_items(xml: str, limit: int = 5) -> list[dict]:
    """Pull title/link/source/date out of an RSS feed."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source_el = item.find("source")
        items.append({
            "title": title,
            "url": (item.findtext("link") or "").strip(),
            "source": ((source_el.text or "").strip()
                       if source_el is not None else ""),
            "published": (item.findtext("pubDate") or "").strip(),
        })
        if len(items) >= limit:
            break
    return items


def _published_key(item: dict) -> float:
    """Sort key for an RSS date. Undated and unparseable items sort last."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(item.get("published") or "").timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _search_payload(query: str, engine: str, items: list[dict]) -> dict:
    """Shape the results the way _tool_result_message hands them to the model."""
    lines: list[str] = []
    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item.get('title', '')}")
        meta = " ".join(part for part in
                        (item.get("source"), item.get("published")) if part)
        if meta:
            lines.append(f"   {meta}")
        if item.get("context"):
            lines.append(f"   {item['context']}")
        if item.get("url"):
            lines.append(f"   {item['url']}")
    return {
        "success": True,
        "query": query,
        "engine": engine,
        "results": items,
        "snippet": "\n".join(lines)[:2000],
    }


async def _search_google_news(query: str) -> dict:
    """Google News search feed: a real feed, so no scraping and no bot wall.

    Both editions are asked because they index different things - the local
    edition carries fresher local coverage, the US one the English write-ups.
    Merging them newest-first is what a "what is happening there" question
    needs: asked on its own, the US edition answered a Jakarta question with
    articles from 2016 and 2018.
    """
    import urllib.parse

    loop = asyncio.get_running_loop()

    def _one(hl: str, gl: str) -> list[dict]:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(query)
               + f"&hl={hl}&gl={gl}&ceid={gl}:{hl.split('-')[0]}")
        return _rss_items(_http_get(url, timeout=10.0), limit=6)

    batches = await asyncio.gather(
        *[loop.run_in_executor(None, _one, hl, gl)
          for hl, gl in (("id-ID", "ID"), ("en-US", "US"))],
        return_exceptions=True)

    merged: list[dict] = []
    seen: set[str] = set()
    for batch in batches:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            key = " ".join(item["title"].lower().split())[:70]
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if not merged:
        return {"success": False, "error": "Google News returned no results"}
    merged.sort(key=_published_key, reverse=True)
    return _search_payload(query, "google-news", merged[:5])


async def _search_bing_news(query: str) -> dict:
    """Bing News RSS - the same idea against a different index."""
    import urllib.parse

    url = ("https://www.bing.com/news/search?q=" + urllib.parse.quote(query)
           + "&format=RSS")
    try:
        xml = await asyncio.get_running_loop().run_in_executor(
            None, _http_get, url, 10.0)
    except Exception as e:
        return {"success": False, "error": f"Bing News request failed: {e}"}
    items = _rss_items(xml, limit=5)
    if not items:
        return {"success": False, "error": "Bing News returned no results"}
    return _search_payload(query, "bing-news", items)


async def _search_wikipedia(query: str) -> dict:
    """MediaWiki search API - plain JSON, no key and nothing to scrape."""
    import re
    import urllib.parse

    url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
           "&srsearch=" + urllib.parse.quote(query) + "&format=json&srlimit=5")
    try:
        body = await asyncio.get_running_loop().run_in_executor(
            None, _http_get, url, 10.0)
        hits = json.loads(body).get("query", {}).get("search", [])
    except Exception as e:
        return {"success": False, "error": f"Wikipedia request failed: {e}"}
    if not hits:
        return {"success": False, "error": "Wikipedia returned no results"}
    items = [{
        "title": str(hit.get("title", "")),
        "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
            str(hit.get("title", "")).replace(" ", "_")),
        "source": "Wikipedia",
        "published": "",
        "context": re.sub(r"<[^>]+>", "", str(hit.get("snippet", "")))[:160],
    } for hit in hits]
    return _search_payload(query, "wikipedia", items)


def _significant_terms(query: str) -> list[str]:
    import re

    words = re.findall(r"[\w']{3,}", query.lower())
    return [word for word in words if word not in _SEARCH_STOPWORDS]


# Wording that means "something happening now" rather than "tell me about a
# thing". It only decides which source's results are listed first.
_RECENCY_HINTS = (
    "hari ini", "terbaru", "terkini", "sekarang", "ramai", "trending",
    "viral", "berita", "today", "latest", "news", "current", "recent",
    "this week", "this month", "this year", "right now", "hot", "update",
)


def _wants_current(query: str) -> bool:
    import re

    lowered = query.lower()
    # Word-boundary matched: a substring test let "hot" fire on "hotel".
    return any(re.search(rf"\b{re.escape(hint)}\b", lowered)
               for hint in _RECENCY_HINTS)


async def _search_news_and_reference(query: str) -> dict:
    """Ask the news feed and the encyclopedia at once, and return both.

    Choosing between them would mean guessing what kind of question this is,
    and a wrong guess is invisible to the user: a news index answered a
    tutorial question with unrelated articles, and an encyclopedia has nothing
    to say about what happened this morning. Ordering only decides which rows
    are listed first; both are always offered.
    """
    news, reference = await asyncio.gather(
        _search_google_news(query), _search_wikipedia(query),
        return_exceptions=True)

    def _items(result: object) -> tuple[list[dict], str]:
        if isinstance(result, Exception) or not isinstance(result, dict):
            return [], "failed"
        if not result.get("success"):
            return [], str(result.get("error") or "failed")
        return list(result.get("results") or []), ""

    news_items, news_err = _items(news)
    ref_items, ref_err = _items(reference)
    if not news_items and not ref_items:
        return {"success": False,
                "error": f"news: {news_err}; wikipedia: {ref_err}"}

    if _wants_current(query):
        items = news_items[:4] + ref_items[:3]
    else:
        items = ref_items[:4] + news_items[:3]
    engine = "+".join(
        name for name, got in (("google-news", news_items),
                               ("wikipedia", ref_items)) if got)
    return _search_payload(query, engine, items)


def _looks_relevant(query: str, result: dict) -> bool:
    """True when the results actually mention what was asked.

    Bing's HTML endpoint sometimes answers with results for a different query
    entirely: a question about Jakarta came back as APA citation generators,
    and one about the Nintendo 3DS as Japanese song lyrics. Handing that to the
    model made it tell the user it could not find out, so a result set that
    barely shares a word with the question is treated as a failure instead.
    """
    terms = _significant_terms(query)
    if not terms:
        return True
    parts = [str(result.get("snippet") or "")]
    for item in result.get("results") or []:
        parts.append(str(item.get("title") or ""))
        parts.append(str(item.get("context") or ""))
    haystack = " ".join(parts).lower()
    hits = sum(1 for term in terms if term in haystack)
    return hits >= max(1, len(terms) // 2)


class SkillRegistry:
    """Central registry for all agent skills — callable by any provider."""

    def __init__(self):
        self._skills: dict[str, SkillDefinition] = {}
        self._register_all()

    def register(self, skill: SkillDefinition):
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def list_all(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def is_enabled(self, name: str) -> bool:
        """Per-skill off switch (Settings → Skills / skills.setState)."""
        try:
            from backend.config import config
            disabled = config.get("skills", "disabled", default=[]) or []
        except Exception:
            disabled = []
        return name not in disabled

    @staticmethod
    def is_always_allowed(name: str) -> bool:
        """Has the user granted this skill standing permission?

        Never raises, and answers False when it cannot tell: the fallback for
        an unreadable policy has to be the prompt, not the permission.
        """
        try:
            from backend.approvals import policy
            return policy.is_always_allowed(policy.SKILL, name)
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the approval policy for %s: %s", name, e)
            return False

    @staticmethod
    def _is_granted(name: str) -> bool:
        """Has the user already answered for this skill — permanently OR for
        this session?

        The session half is not an optimisation. "Allow for session" is the
        ONLY grant a content-classified skill can have, and `move_file`,
        `write_file` and `delete_file` are all granted that way. Checking only
        the permanent list meant the answer was stored, the dashboard showed it
        as granted, and the very next call asked again — a button that visibly
        does nothing, which teaches the user to stop reading the card.

        The session check comes first for the same reason it does in the
        executor: it is the only one some names can carry.
        """
        try:
            from backend.approvals import policy
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the approval policy for %s: %s", name, e)
            return False
        try:
            if policy.is_allowed_for_session(policy.SKILL, name):
                return True
        except Exception as e:  # noqa: BLE001
            log.debug("could not read session grants for %s: %s", name, e)
        try:
            return policy.is_always_allowed(policy.SKILL, name)
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the approval policy for %s: %s", name, e)
            return False

    def enabled_list_all(self) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if self.is_enabled(s.name)]

    def list_category(self, category: str) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if s.category == category]

    def to_openai_tools(self, only: set[str] | None = None) -> list[dict]:
        """`only` narrows the catalogue to those skill names; None means all."""
        return [s.to_openai_tool() for s in self.enabled_list_all()
                if only is None or s.name in only]

    def to_claude_tools(self) -> list[dict]:
        return [s.to_claude_tool() for s in self.enabled_list_all()]

    # The two lines that say how to call a tool. Kept here, next to the
    # catalogue that uses them, because the question's message repeats them:
    # a format stated in two places drifts, and a drift there is a model that
    # asks for a tool in a shape nothing parses.
    PROMPT_CALL_FORMAT = ('Call one with: ```tool\n'
                          '{"tool": "name", "params": {}}\n```')

    def prompt_tool_count(self, only: set[str] | None = None) -> int:
        """How many tools `to_prompt_tools(only)` would actually list.

        An empty set means no tools, and a turn with no tools must not be told
        how to call one — the model would invent a call to something that is
        not there.
        """
        if only is None:
            return len(self.enabled_list_all())
        return sum(1 for s in self.enabled_list_all() if s.name in only)

    def filter_for_query(self, query: str, max_tools: int = 16) -> set[str]:
        """Select a concise subset of relevant skills for prompt-based tool calling.

        Instead of injecting 50+ tool schemas (~4,700 tokens) into the context
        of local models, selects core utilities plus skills relevant to the
        user's query.

        ``max_tools`` is 16, raised from 10 when the acquisition tools stopped
        being gated behind a magic word. Reserving slots for them at 10 pushed
        SEVEN real skills — `browser_extract`, `pdf_redact`, `memory_link`,
        `wiki_ingest`, `wiki_write`, `self_propose`, `swarm_notes` — out of reach
        entirely, which `check_reachability` caught. Stealing from real tools to
        pay for the discovery ones is not a trade worth making.

        Measured on the local model against its 8,192-token window: the
        catalogue costs ~459 tokens at 10 and ~776 at 16. The extra 317 tokens
        are under 4% of the window, which buys the seven skills back and keeps
        the discovery tools always present.
        """
        import re

        enabled = {s.name: s for s in self.enabled_list_all()}
        if not enabled:
            return set()

        core_tools = {"web_search", "read_file", "write_file", "run_command"}
        discovery_tools = {"find_mcp_server", "forge_skill", "list_forged"}
        # Wiki/knowledge intent: when the user asks about docs, notes, or what
        # Addled knows, the wiki tools should be offered — not only when the
        # literal word "wiki" appears.
        knowledge_terms = {
            "wiki", "doc", "docs", "documentation", "notes", "note",
            "article", "knowledge", "reference", "manual", "page", "pages",
            "about", "explain", "understand",
        }
        wiki_tools = {"wiki_search", "wiki_read", "wiki_links", "wiki_lint"}
        # Procedure/SOP intent: "how do we do X", "standard procedure", "recipe".
        procedure_terms = {
            "procedure", "procedures", "sop", "sops", "recipe", "recipes",
            "guide", "guideline", "guidelines", "workflow", "runbook",
            "standard", "standards", "steps", "rules", "rule", "how",
        }
        sop_tools = {"sop_lookup", "sop_list", "sop_save"}

        # Intent -> the tools that serve it, matched on words a user actually
        # types rather than on the tool's own name.
        #
        # This exists because the scoring above is lexical: a tool is found only
        # if the question shares a word with its NAME or description. Measured
        # with scripts/check_reachability.py, that left whole groups reachable
        # only by typing the name:
        #   "summarise this report"    -> no document reader at all
        #   "open the website X"       -> not browser_navigate (name has neither word)
        #   "what do you remember"     -> memory_SET (the writer), not memory_get
        #   "grep for the word token"  -> word_read, because of the literal "word"
        #
        # Two rules keep this honest: a phrase only fires when the words are
        # present, and a tool listed under an intent is a candidate — not a
        # forced pick — so a genuinely better lexical match still wins.
        intents: tuple[tuple[set[str], set[str]], ...] = (
            # Documents: the words people use, not the extensions.
            ({"document", "documents", "doc", "docx", "word", "report",
              "letter", "essay", "write-up", "writeup"},
             {"word_read", "word_create", "word_edit"}),
            ({"spreadsheet", "spreadsheets", "excel", "xlsx", "xls", "workbook",
              "sheet", "sheets", "cell", "cells", "numbers", "table", "tables"},
             {"excel_read", "excel_write", "excel_sheets"}),
            ({"presentation", "presentations", "powerpoint", "pptx", "slide",
              "slides", "deck", "decks"},
             {"pptx_read", "pptx_create", "pptx_add_slide"}),
            ({"pdf", "pdfs"}, {"pdf_read", "pdf_create", "pdf_edit",
                               "pdf_merge", "pdf_pages", "pdf_extract"}),
            # "turn X into Y" is how conversion is asked for.
            ({"convert", "conversion", "turn", "transform", "export"},
             {"convert_to_pdf", "convert_from_pdf"}),
            # Recall vs save: read and write are different asks, and the writer
            # used to win the reader's question.
            ({"remember", "recall", "memory", "memories", "know", "knew",
              "remembered", "forget", "profile"},
             {"memory_get", "memory_related", "memory_graph", "memory_files"}),
            ({"save", "store", "note", "remember", "memorise", "memorize",
              "preference", "preferences"},
             {"memory_set"}),
            # Browsing: "website", "link", "url" are not "browser_navigate".
            ({"website", "websites", "site", "sites", "webpage", "webpages",
              "url", "link", "links", "browse", "browser", "google", "online"},
             {"browser_navigate", "browser_extract", "browser_click",
              "browser_type", "browser_task", "web_fetch"}),
            # Searching inside files, as distinct from finding files.
            ({"grep", "contains", "containing", "mentions", "mentions",
              "inside", "within", "occurrences", "usages"},
             {"search_in_files"}),
            ({"find", "locate", "which", "list"}, {"search_files", "list_dir"}),
            # Windows and desktop.
            ({"window", "windows", "app", "application", "applications",
              "minimise", "minimize", "maximise", "maximize", "switch"},
             {"list_windows", "focus_window", "resize_window", "close_window"}),
            ({"desktop", "mouse", "cursor", "keyboard", "keystroke", "hotkey",
              "shortcut"},
             {"desktop_click", "desktop_type", "desktop_hotkey",
              "desktop_scroll", "desktop_move_mouse", "desktop_drag"}),
            # System settings.
            ({"volume", "sound", "audio", "mute", "loud", "speaker"},
             {"set_volume"}),
            ({"brightness", "dim", "bright", "screen"}, {"set_brightness",
                                                         "screenshot"}),
            ({"clipboard", "paste", "copied"}, {"get_clipboard", "set_clipboard"}),
            ({"code", "source", "function", "script", "python", "bug", "test",
              "tests", "compile", "build", "refactor"},
             {"code_read", "code_edit", "verify_code"}),
        )

        terms = set(_significant_terms(query or ""))
        q_lower = (query or "").lower()
        asks_for_tools = bool(terms & {
            "tool", "tools", "skill", "skills", "mcp", "server", "servers",
            "forge", "forged", "market", "registry", "capability", "capabilities",
            "search", "trust", "trusted", "verify", "verification",
        })
        asks_for_knowledge = bool(terms & knowledge_terms)
        asks_for_procedures = bool(terms & procedure_terms)

        # Which intents this query fires, resolved once rather than per skill.
        # 4.0 sits between a name overlap (3.0) and an exact name mention (5.0):
        # an intent match should beat a stray shared word, but not override a
        # tool the user named outright.
        intent_hits: dict[str, float] = {}
        for words, tools in intents:
            if terms & words:
                for tool in tools:
                    intent_hits[tool] = max(intent_hits.get(tool, 0.0), 4.0)

        scores: dict[str, float] = {}
        for name, skill in enabled.items():
            score = 1.5 if name in core_tools else 0.0
            name_lower = name.lower()
            name_parts = set(name_lower.split("_"))
            category = skill.category.lower()

            if asks_for_tools and name in discovery_tools:
                score += 6.0
            # `cli` and `forged` belong here for the same reason `mcp` does: a
            # tool the user built is a capability that extends what Addled can
            # do, so "what tools do you have?" should surface it. Leaving it out
            # meant a built tool was callable but invisible to the very question
            # that asks what is available.
            #
            # These are ALSO force-added below (see `must`). The score alone was
            # not enough: once a skill pack lands, `market` holds many skills
            # with the same score, and a single built tool loses the tiebreak to
            # them. A capability the user built is not one interchangeable
            # candidate among a pile of prose skills, so it is guaranteed a slot
            # rather than left to outscore them.
            if asks_for_tools and category in {"mcp", "market", "forged", "meta",
                                               "cli"}:
                score += 4.0

            # Knowledge/wiki intent: boost wiki tools and the memory category.
            if asks_for_knowledge:
                if name in wiki_tools:
                    score += 6.0
                if category == "memory" and name in wiki_tools:
                    score += 2.0

            # Procedure/SOP intent: boost SOP tools.
            if asks_for_procedures:
                if name in sop_tools:
                    score += 6.0

            # Plain-language intent: the words a user types for this job, which
            # are usually not the words in the tool's own name.
            if name in intent_hits:
                score += intent_hits[name]

            # MCP domain-term matching: an MCP tool named
            # ``mcp__postgres__query`` should surface when the user asks about
            # "database", "sql", or "postgres" — not only when they say "mcp".
            if category == "mcp" and name.startswith("mcp__"):
                # mcp__<server>__<tool> → extract server and tool parts
                parts = name_lower.split("__")
                if len(parts) >= 3:
                    server_parts = set(parts[1].split("_"))
                    tool_parts = set(parts[2].split("_"))
                    if (server_parts & terms) or (tool_parts & terms):
                        score += 5.0
                    # Also check description for domain overlap
                    desc_words = set(re.findall(
                        r"[\w']{3,}",
                        (skill.description or "").lower()))
                    if desc_words & terms:
                        score += min(len(desc_words & terms), 3) * 1.5

            if name_lower in q_lower:
                score += 5.0
            elif name_parts & terms:
                score += 3.0

            if category in terms:
                score += 2.0

            desc_words = set(re.findall(r"[\w']{3,}", (skill.description or "").lower()))
            overlap = desc_words & terms
            if overlap:
                score += min(len(overlap), 3) * 1.0

            if score > 0:
                scores[name] = score

        if not scores:
            # Nothing scored: still offer the utilities and the acquisition
            # tools, for the same reason they are force-added below — a turn
            # that matched nothing is exactly the turn that needs to be able to
            # look for a capability.
            return {n for n in (set(core_tools) | discovery_tools)
                    if n in enabled}

        sorted_tools = sorted(scores.keys(), key=lambda n: scores[n], reverse=True)
        selected = list(sorted_tools[:max_tools])
        # The acquisition tools are ALWAYS offered, not only when the user says
        # a magic word.
        #
        # They used to be gated behind `asks_for_tools`, which fires on "tool",
        # "skill", "mcp", "search" and similar. Measured consequence: asking for
        # a TASK left them out entirely —
        #     'convert this pdf to a spreadsheet' -> none offered
        #     'find an mcp server for github'     -> all three
        # — so the agent could only discover that it may acquire capability if
        # the user happened to name the concept. That is the whole of why it
        # looked passive: it was never shown the option. The gate saved ~240
        # tokens and cost the feature its visibility, which is the wrong trade.
        # `must` is never truncated. The slice below is what enforces the budget,
        # and anything appended after it is lost — which is exactly what happened
        # to the discovery tools on the first attempt at this fix: for "convert
        # this pdf to a spreadsheet" the scored list alone filled all ten slots,
        # so the two tools force-added afterwards were cut off by `[:max_tools]`
        # and the query STILL offered none of them. Keeping them out of the
        # scored pool and taking the budget from what is left is the only
        # arrangement where "always offered" survives contact with a full list.
        # A capability this install has and another does not: the user's own
        # built tools (cli/forged) and any connected MCP server. These are what
        # a tool-discovery question is really asking about, and they are few, so
        # reserving them costs a normal query nothing.
        own_tools = {n for n, s in enabled.items()
                     if s.category.lower() in {"cli", "forged", "mcp"}}

        must: list[str] = []
        for name in list(discovery_tools) + list(core_tools) + sorted(own_tools):
            if name in enabled and name not in must and name not in selected:
                must.append(name)
        room = max(0, max_tools - len(must))
        return set(selected[:room] + must)

    def to_prompt_tools(self, only: set[str] | None = None) -> str:
        """For providers without native tool support: the catalogue.

        Deliberately terse. It travels as its own message immediately before the
        user's question, and its call-format line is repeated on that question
        (see `tool_loop._call_prompt_tools`), so every token here is a token the
        model cannot spend on the question.

        `only` narrows the catalogue to those skill names; None means every
        enabled skill. An empty set is honoured as "no tools" rather than being
        treated as "no filter" — the difference matters to a caller that has
        deliberately given an agent a restricted set.
        """
        lines = ["\n## Tools", self.PROMPT_CALL_FORMAT,
                 "A trailing '?' marks an optional argument.", ""]
        for skill in self.enabled_list_all():
            if only is not None and skill.name not in only:
                continue
            lines.append(skill.to_prompt_desc())
        return "\n".join(lines)

    async def execute(self, name: str, params: dict) -> SkillResult:
        """Execute a skill by name. Returns result for the AI provider.

        Two gates are enforced here, at the single seam every tool call goes
        through:

        - the enable/disable switch (`is_enabled`), and
        - `requires_approval`, which until now was declared on a skill and
          **read by nothing but the dashboard listing**.

        That second one was a real hole, not a formality: `delete_file` and
        `write_file` are registered with `requires_approval=True`, and they call
        `FileOps` directly rather than through `ActionExecutor`, so the
        `DestructionGate` never saw them either. A model could delete or
        overwrite a file from a plain chat turn with no prompt at all — the
        gate's own `DESTRUCTIVE_ACTIONS` set names `delete_file` while no code
        path consulted it for that skill.

        A skill that needs approval now goes through the same waiting path
        `run_command` uses, so the answer is the user's and it arrives on the
        turn that asked.
        """
        skill = self._skills.get(name)
        if not skill:
            return SkillResult(False, name, error=f"Unknown skill: {name}")
        if not self.is_enabled(name):
            return SkillResult(False, name,
                               error=f"Skill '{name}' is disabled")

        # Alias the parameters BEFORE asking for approval, and refuse a call
        # that is missing something required.
        #
        # Order matters twice over. A model sending `{"file_path": ...}` to a
        # skill that declares `path` used to fail as "No path given." — true,
        # and useless: nothing told it which name it got wrong, so it retried
        # the same shape or gave up. And asking must come second, or the user is
        # shown a permission card for a call that cannot work either way: they
        # approve, and it fails on an empty string. Nobody should be asked to
        # authorise a call that was never going to run.
        if hasattr(skill, "normalise"):
            params, trouble = skill.normalise(params)
            if trouble:
                log.info("Skill %s called with wrong parameters: %s", name,
                         trouble)
                # `success` is mirrored into the data dict as well as the
                # SkillResult. Every skill reports its own failures that way
                # (`{"success": False, "error": ...}`), and a caller reading
                # `result.data` — which is what the dashboards and the check
                # suites do — would otherwise see an empty dict and treat a
                # refused call as a silent success.
                return SkillResult(False, name, error=trouble,
                                   data={"success": False, "error": trouble,
                                         "invalid_params": True})

        if getattr(skill, "requires_approval", False):
            # The flag says "this skill is one that can ask", not "ask every
            # time". For a skill whose danger depends on its ARGUMENTS --
            # `run_command` and `session_send` are both in
            # `DESTRUCTIVE_ACTIONS` for this reason -- the gate has already
            # decided, and letting its answer through is what stops
            # `Get-Command ffmpeg` from raising a permission card.
            #
            # A skill the gate says is safe here is one whose danger cannot be
            # read from the request at all; those still ask below.
            #
            # Falling through, NOT `return None`: this function IS the caller,
            # and returning here would skip the handler entirely -- a safe
            # command would run nothing and report nothing.
            if self._needs_approval(name, params):
                result = await self._execute_gated(name, skill, params)
                if result is not None:
                    return result

        try:
            result = await skill.handler(params)
            if isinstance(result, dict):
                return SkillResult(
                    success=result.get("success", True),
                    skill_name=name,
                    data=result,
                    error=result.get("error"),
                    summary=result.get("summary", ""),
                )
            return SkillResult(True, name, data={"result": result})
        except Exception as e:
            log.exception("Skill %s failed", name)
            return SkillResult(False, name, error=str(e))

    @staticmethod
    def _needs_approval(name: str, params: dict) -> bool:
        """Does the gate want to ask for THIS call?

        True for anything the gate cannot clear: a name in `DESTRUCTIVE_ACTIONS`
        that is not content-classified, an unreadable gate, or a call whose
        arguments the classifier reads as destructive.

        Never raises, and an error means ASK rather than proceed -- a gate that
        cannot be consulted must not be treated as permission, which is the same
        direction `_is_granted` already takes.
        """
        try:
            from backend.actions.executor import executor
            executor._lazy_init()
            gate = executor._gate
            if gate is None:
                return True
            # The skill's name IS the action type the gate classifies;
            # `run_command` and `session_send` are matched by name there.
            return bool(gate.requires_approval(name, params or {}))
        except Exception as e:  # noqa: BLE001
            log.debug("gate could not classify %s (%s); asking", name, e)
            return True

    async def _execute_gated(self, name: str, skill,
                             params: dict) -> SkillResult | None:
        """Ask for approval before running a destructive skill.

        Returns None when the caller should proceed (approved, or no approval
        machinery is running) and a SkillResult when the turn must be told to
        wait or that the user said no.

        Never raises: a failure to reach the approval path must not silently
        grant permission, so it falls back to refusing.

        A skill the user granted standing permission proceeds without asking.
        The check is here as well as inside `request_approval` so a build whose
        executor predates the policy still honours the grant — and so the
        reason a skill did not prompt is visible at the seam that decided it.
        """
        if self._is_granted(name):
            return None
        try:
            from backend.actions.executor import executor as _exec
        except Exception as e:  # noqa: BLE001
            log.warning("approval path unavailable for %s: %s", name, e)
            return SkillResult(
                False, name,
                error=(f"'{name}' needs your approval, but the approval path "
                       "is not running. Use the dashboard."),
                data={"requires_approval": True})
        try:
            # AWAITED. This is an `async def`; calling it without `await`
            # returned a coroutine object, which is neither True nor False, so
            # every destructive skill fell through to "waiting for approval"
            # and never ran even after the user agreed.
            pending = await _exec.request_approval(name, params)
        except AttributeError:
            # The executor has no such helper (older build): refuse rather
            # than run a destructive action ungated.
            return SkillResult(
                False, name,
                error=f"'{name}' needs approval before it can run.",
                data={"requires_approval": True})
        except Exception as e:  # noqa: BLE001
            log.warning("could not queue approval for %s: %s", name, e)
            return SkillResult(
                False, name,
                error=f"'{name}' needs approval before it can run.",
                data={"requires_approval": True})
        if pending is True:
            return None            # approved — run it
        if pending is False:
            return SkillResult(False, name,
                               error="The user denied this action.",
                               data={"denied": True})
        # A dict: still waiting. Report the id so the turn can say so.
        info = pending if isinstance(pending, dict) else {}
        return SkillResult(
            False, name,
            data={"requires_approval": True, **info},
            error=(info.get("message")
                   or f"'{name}' is waiting for your approval."))

    def _register_guideline_skills(self):
        """External guideline packs (ponytail review, status)."""
        try:
            from backend.skills.guidelines import register
            register(self)
        except Exception as e:
            log.debug("guideline skills unavailable: %s", e)

    def _register_memory_skills(self):
        """The memory link graph, the wiki, and the procedure library."""
        try:
            from backend.skills.memory_links import register
            register(self)
        except Exception as e:
            log.debug("memory link skills unavailable: %s", e)
        try:
            from backend.skills.wiki import register
            register(self)
        except Exception as e:
            log.debug("wiki skills unavailable: %s", e)
        try:
            from backend.skills.sop import register
            register(self)
        except Exception as e:
            log.debug("procedure skills unavailable: %s", e)

    def _register_all(self):
        """Register all 55+ agent skills."""
        self._register_system_skills()
        self._register_file_skills()
        self._register_office_skills()
        self._register_window_skills()
        self._register_browser_skills()
        self._register_code_skills()
        self._register_calendar_skills()
        self._register_email_skills()
        self._register_web_skills()
        self._register_meta_skills()
        self._register_guideline_skills()
        self._register_memory_skills()
        self._register_swarm_skills()
        self._register_clarification_skills()

    def _register_clarification_skills(self):
        """Asking the user a question, when the request genuinely cannot be read.

        One skill, and it is deliberately narrow. The failure mode of a cheap
        `ask_user` is worse than the ambiguity it solves: a model that finds
        asking easy stops making reasonable assumptions, and the user ends up
        answering more questions than they would have typed instructions. The
        description below is the main lever for that — it says when *not* to
        call this as much as when to.
        """
        async def ask_user(params: dict) -> dict:
            from backend.questions import pending
            from backend.skills import tool_loop

            question = params.get("question") or params.get("ask") or ""
            options = params.get("options") or []
            if isinstance(options, str):
                # A model sometimes sends one string instead of a list. Taking
                # it would make the card show one choice with no way to pick a
                # different one, so it is treated as a single option.
                options = [options]
            if not isinstance(options, list):
                options = []

            ctx = tool_loop.current_turn()
            result = pending.ask(
                question,
                options=options,
                source=ctx.get("source") or "",
                conversation=ctx.get("conversation") or "",
                context=params.get("context") or "",
            )
            # The marker `tool_loop` stops on. Set only for a queued question:
            # a refusal (unattended, duplicate, too long) must NOT end the turn,
            # because there is no card and nothing for the user to answer. The
            # model has to read the refusal and carry on, which is exactly what
            # the refusal text tells it to do.
            if result.get("success"):
                result["requires_answer"] = True
            return result

        self.register(SkillDefinition(
            "ask_user",
            "Ask the user a question when you genuinely cannot tell what they "
            "meant, and guessing wrong would waste real work.\n\n"
            "Use it for a decision only they can make: which of two files with "
            "the same name, which account or recipient, which of two readings "
            "of a contradictory request, or a choice between approaches with "
            "different trade-offs.\n\n"
            "Do NOT use it to be cautious. If a reasonable default exists, take "
            "it and say what you assumed — a user correcting one line is faster "
            "than answering a question. Do not ask what you could discover by "
            "reading or searching, and do not ask for information already in "
            "the conversation.\n\n"
            "THE EXCEPTION, and it is the important one: DO ask before "
            "changing what Addled can do. Installing a market skill, forging "
            "new code, and adding an MCP server all fetch or generate code that "
            "then runs on the user's machine, so consent is required rather "
            "than good manners. The rule above about not asking permission "
            "covers ordinary work; it does not cover acquiring a new "
            "capability. Ask BEFORE it happens — installing and then reporting "
            "takes the decision away from the user.\n\n"
            "The question goes to the user's dashboard as a card and your turn "
            "ends there; their answer arrives as the next message. Never call "
            "this and then carry on as if it had been answered.\n\n"
            "Pass `options` when there are a few concrete choices — they are "
            "shown as buttons and are much faster to answer than free text. "
            "Leave it out when the answer is open-ended.",
            {"type": "object", "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, one sentence, as short as it "
                                   "can be while still being unambiguous."},
                "options": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Up to six concrete choices, when the "
                                   "decision is between known answers. Omit "
                                   "for an open-ended question."},
                "context": {
                    "type": "string",
                    "description": "Optional: one line on why you are asking, "
                                   "so the user understands what it blocks."},
            }, "required": ["question"]},
            ask_user, "system",
        ))

    def _register_swarm_skills(self):
        """Tools a swarm agent uses to coordinate with its peers.

        Only meaningful inside a flow — a note is delivered to the other agents
        working the same wave. Called from a normal chat turn it is a no-op
        that says so, rather than an error.
        """
        async def swarm_note(params: dict) -> dict:
            from backend.swarm.orchestrator import swarm
            text = str(params.get("note") or params.get("message") or "").strip()
            if not text:
                return {"success": False,
                        "error": "Pass the note text in 'note'."}
            return swarm.note(
                from_agent=str(params.get("from") or "an agent"),
                text=text,
                to=str(params.get("to") or ""))
        self.register(SkillDefinition(
            "swarm_note",
            "Leave a short note for the other agents working on this flow right "
            "now — a warning, a decision that affects them, or a hand-off. Use "
            "it when you discover something another agent's work depends on "
            "(a name you changed, a constraint you hit). Keep it to one or two "
            "sentences; it is read by people, not filed.",
            {"type": "object", "properties": {
                "note": {"type": "string",
                         "description": "The note, one or two sentences."},
                "to": {"type": "string",
                       "description": "Optional: the name of one agent it is "
                                      "for. Omit to send to the whole wave."},
            }, "required": ["note"]},
            swarm_note, "system",
        ))

        async def swarm_notes(params: dict) -> dict:
            from backend.swarm.orchestrator import swarm
            # Reading without consuming: the wave delivers notes itself, so this
            # is for an agent that wants to look rather than be handed them.
            seen = swarm._notes_seen.setdefault("__read__", set())
            lines = []
            for i, entry in enumerate(swarm._notes):
                if i in seen:
                    continue
                seen.add(i)
                lines.append(f"[{entry['from']}] {entry['text']}")
            return {"success": True, "notes": lines, "count": len(lines)}
        self.register(SkillDefinition(
            "swarm_notes",
            "Read the notes the other agents on this flow have left. They are "
            "also handed to you automatically before you start, so this is for "
            "checking again partway through.",
            {"type": "object", "properties": {}},
            swarm_notes, "system",
        ))

        async def swarm_roster(params: dict) -> dict:
            """List the saved agent desks, and the built-in types."""
            from backend.swarm import roster
            if params.get("types"):
                return {"success": True, "types": roster.type_catalogue()}
            return {"success": True, "agents": roster.definitions(),
                    "types": [t["type"] for t in roster.type_catalogue()]}
        self.register(SkillDefinition(
            "swarm_roster",
            "List the saved swarm agents (their names, roles, briefs, skills "
            "and models). Pass types=true to list the built-in agent types and "
            "the default skills each one gets.",
            {"type": "object", "properties": {
                "types": {"type": "boolean",
                          "description": "List built-in types instead."},
            }},
            swarm_roster, "system",
        ))

        async def swarm_learn(params: dict) -> dict:
            """Record a correction so an agent does it right next time.

            Split into standing rules (apply to every future task) and one-offs
            (about the task that earned them). This is the loop that makes an
            agent get better at this user's work specifically.
            """
            from backend.swarm import roster
            agent_id = str(params.get("agent") or params.get("agentId") or "")
            text = str(params.get("rule") or params.get("correction") or "").strip()
            if not agent_id:
                return {"success": False,
                        "error": "Name the agent this applies to."}
            if not text:
                return {"success": False,
                        "error": "Give the rule or correction text."}
            # Accept a name as well as an id — a user says "the reviewer", not
            # "agent_7f3a21bc". `roster.find` owns that rule now, so this tool,
            # `swarm_delegate` and `swarm_rename` cannot disagree about what a
            # name means.
            entry = roster.find(agent_id)
            if not entry:
                collided = roster.find_ambiguous(agent_id)
                if len(collided) > 1:
                    return {"success": False,
                            "error": (f"'{agent_id}' could match more than one "
                                      "agent: " + ", ".join(collided)
                                      + ". Use the exact name or the id.")}
                return {"success": False,
                        "error": (f"No agent '{agent_id}'. Use swarm_roster "
                                  "to see the saved agents.")}
            agent_id = str(entry.get("id") or agent_id)
            kind = str(params.get("kind") or "standing").lower()
            if kind in ("oneoff", "one-off", "one_off", "task"):
                return roster.add_one_off(agent_id, text)
            return roster.add_rule(agent_id, text)
        self.register(SkillDefinition(
            "swarm_learn",
            "Record how a swarm agent should do something differently next "
            "time. Use 'standing' (default) for a rule that applies to every "
            "future task — 'proposals are always one page' — and 'one-off' for "
            "a note about the task that just ran. Use this when the user "
            "corrects a result rather than waiting to be told.",
            {"type": "object", "properties": {
                "agent": {"type": "string",
                          "description": "The agent id or name."},
                "rule": {"type": "string",
                         "description": "The rule, in one sentence."},
                "kind": {"type": "string",
                         "description": "'standing' or 'one-off'."},
            }, "required": ["agent", "rule"]},
            swarm_learn, "system",
        ))

        async def swarm_delegate(params: dict) -> dict:
            """Hand a task to a swarm agent and report back when it is done.

            Non-blocking ON PURPOSE. A swarm agent runs a full reasoning turn —
            with tools, memory and possibly several tool rounds — which takes
            minutes. Making the user's chat turn wait for that would look like a
            hang, and the character would sit on "thinking" for work it is not
            doing. So this starts the agent and returns immediately; the answer
            arrives later as a message, the same way a scheduled reminder does
            (`backend/tasks/actions.py`).

            Refused inside a delegation. `SwarmAgent.run_task` goes through
            `run_chat_pipeline`, so an agent IS a chat turn and holds this same
            skill — without the brake, an agent could delegate to an agent
            forever. One level is the whole allowance; see
            `backend/chat_context.MAX_DELEGATION_DEPTH`.
            """
            from backend import chat_context
            from backend.swarm import roster
            from backend.swarm.orchestrator import swarm

            if chat_context.in_delegation():
                return {
                    "success": False,
                    "error": ("You are a swarm agent working a delegated task, "
                              "so you cannot delegate again. Do the task "
                              "yourself with your own tools, and say what you "
                              "found."),
                }

            wanted = str(params.get("agent") or params.get("agentId")
                         or params.get("name") or "").strip()
            task = str(params.get("task") or params.get("brief")
                       or params.get("request") or "").strip()
            if not wanted:
                return {"success": False,
                        "error": "Name the agent to delegate to. Use "
                                 "swarm_roster to see the saved agents."}
            if not task:
                return {"success": False,
                        "error": "Give the agent a task in 'task'."}

            # Which agent did they mean — the ONE place that is decided.
            # `roster.find` takes an id, an exact name, or a loose name ("the
            # QA agent"), and returns None when the loose form is ambiguous
            # rather than guessing. This used to be a copy of the same logic
            # that also sat in `swarm_learn`; two copies is how one of them
            # ends up accepting a name the other refuses.
            entry = roster.find(wanted)
            if not entry:
                # Distinguish "no such agent" from "two agents matched", because
                # the user's next move is different: create one, or say which.
                collided = roster.find_ambiguous(wanted)
                if len(collided) > 1:
                    return {
                        "success": False,
                        "error": (f"'{wanted}' could match more than one agent: "
                                  + ", ".join(collided)
                                  + ". Use the exact name or the id."),
                    }
                names = [str(e.get("name") or "?") for e in roster.definitions()]
                return {
                    "success": False,
                    "error": (f"No swarm agent called '{wanted}'. Saved "
                              "agents: " + (", ".join(names) if names
                                            else "(none yet - the Swarm page "
                                                  "creates them)")),
                }

            agent_id = str(entry.get("id") or "")
            agent_name = str(entry.get("name") or agent_id)

            # Reuse a live agent rather than spawning a second copy of the same
            # desk: two agents with one name would both write that desk's
            # notebook, and `swarm.stop` could only cancel one of them.
            agent = swarm.get_agent(agent_id)
            if agent is None:
                agent = swarm.spawn_from_roster(agent_id)
            if agent is None:
                return {"success": False,
                        "error": f"Could not start '{agent_name}'."}

            # Anything the caller wants the agent to know that is not the task
            # itself — a file path, a decision already made, a constraint.
            context = str(params.get("context") or "").strip()
            if context:
                task = f"{task}\n\nContext you need:\n{context}"

            async def _run_and_report() -> None:
                """Run the agent's turn, then tell every surface it finished.

                The delegation depth is raised HERE, inside the task, so it
                applies to the agent's own turn and not to the chat turn that
                started it. `ContextVar` follows the task, so this does not leak
                back to the caller.
                """
                from backend.ws_server import get_server
                chat_context.enter_delegation()
                try:
                    from backend.providers.registry import get_provider
                    provider = get_provider()
                except Exception:  # noqa: BLE001
                    provider = None
                try:
                    result = await swarm.run_agent(agent_id, task, provider)
                except Exception as e:  # noqa: BLE001
                    log.warning("Delegated task for %s failed: %s", agent_name, e)
                    result = {"success": False, "error": str(e)}
                finally:
                    chat_context.exit_delegation()

                text = str((result or {}).get("response") or "").strip()
                ok = bool((result or {}).get("success")) and bool(text)
                payload = {
                    "agent_id": agent_id,
                    "agent": agent_name,
                    "task": task,
                    "success": ok,
                    "text": text or str((result or {}).get("error")
                                        or "The agent did not return an answer."),
                    # The chat that asked, so the result can be filed back into
                    # the right conversation rather than every open page.
                    "source": chat_context.source(),
                    "conversation": chat_context.conversation(),
                }
                server = get_server()
                if server is None:
                    return
                try:
                    server.broadcast_nowait("swarm.agentResult", payload)
                    # The Swarm page draws running/finished state from this.
                    server.broadcast_nowait("swarm.updated",
                                            {"agents": swarm.list_agents()})
                    # The remote companion: a phone that asked for this deserves
                    # the answer too, and it is the same shape a reminder uses.
                    server.broadcast_nowait("bot.notify", {
                        "text": (f"🤖 {agent_name}: {payload['text'][:600]}"
                                 if ok else
                                 f"⚠ {agent_name} could not finish: "
                                 f"{payload['text'][:400]}"),
                        "platforms": [],
                    })
                except Exception as e:  # noqa: BLE001
                    log.debug("could not announce the delegated result: %s", e)

            try:
                asyncio.create_task(_run_and_report())
            except Exception as e:  # noqa: BLE001
                return {"success": False,
                        "error": f"Could not start the task: {e}"}

            return {
                "success": True,
                "status": "started",
                "agent": agent_name,
                "message": (f"{agent_name} is working on that now. The answer "
                            "will arrive as a message when it is done."),
            }
        self.register(SkillDefinition(
            "swarm_delegate",
            "Hand a longer piece of work to one of the saved swarm agents, "
            "which has its own role, brief and learned rules. Use it when the "
            "task suits a specialist better than you — a review, a research "
            "pass, a second opinion — or when it is long enough that you would "
            "rather not hold the conversation open. You get an acknowledgement "
            "straight away and the agent's answer arrives later as a message, "
            "so tell the user that is what will happen. Not available to a "
            "swarm agent itself. When the user NAMES an agent — 'ask Scout', "
            "'have the Reviewer look at this' — pass that name as `agent`; a "
            "name refers to a saved desk, and the match tolerates loose wording "
            "like 'the Scout agent'. If you are not sure the desk exists, call "
            "swarm_roster first rather than guessing a name.",
            {"type": "object", "properties": {
                "agent": {"type": "string",
                          "description": "The agent's name or id. Use "
                                         "swarm_roster to see them."},
                "task": {"type": "string",
                         "description": "What to do, in full sentences. The "
                                        "agent does not see this conversation, "
                                        "so include everything it needs."},
                "context": {"type": "string",
                            "description": "Optional extra facts: file paths, "
                                           "decisions already made, "
                                           "constraints."},
            }, "required": ["agent", "task"]},
            swarm_delegate, "system",
        ))

        async def swarm_create(params: dict) -> dict:
            """Create a named swarm agent the user can then call by name.

            This is the gap that made "make me an agent called Scout" a refusal
            with a trip to another page: naming existed, but only the Swarm page
            could write a definition, and no skill reached it. A user talks to
            the chat, so the chat has to be able to name a desk.

            Goes through `roster.upsert` + `spawn_from_roster`, the same pair
            `swarm.define` uses on the WebSocket. Written that way on purpose:
            two creation paths would drift, and whichever ran second would
            overwrite the other's fields.

            Refuses a name that already belongs to an agent. Two desks with one
            name make every later lookup ambiguous, and `roster.find` answers
            ambiguity by refusing — so a duplicate name would be created here
            and then be unusable everywhere else.
            """
            from backend.swarm import roster
            from backend.swarm.orchestrator import swarm

            name = str(params.get("name") or params.get("agent") or "").strip()
            if not name:
                return {"success": False,
                        "error": "Give the agent a name in 'name'."}

            existing = roster.find(name)
            if existing and not params.get("replace"):
                return {
                    "success": False,
                    "error": (f"An agent called '{existing.get('name')}' already "
                              f"exists (id {existing.get('id')}). Use its name to "
                              "delegate to it, swarm_rename to rename it, or pass "
                              "a different name."),
                }

            # An explicit id means UPDATE that agent; no id means create one.
            # Passing a NAME here would make `normalise` invent an id and
            # `upsert` would then store a second entry, which is exactly the
            # duplication this guards against.
            agent_id = str(params.get("id") or "").strip()
            if not agent_id and existing and params.get("replace"):
                agent_id = str(existing.get("id") or "")

            entry = {
                "id": agent_id,
                "name": name,
                "type": str(params.get("type") or "general"),
                "role": str(params.get("role") or ""),
                "does": str(params.get("does") or ""),
                "prompt": str(params.get("prompt") or ""),
                # `brief` is what the user means by "who it is" — a few standing
                # sentences read before every task. Accept it under either name.
                "brief": str(params.get("brief") or params.get("instructions")
                             or ""),
                "tools": params.get("tools"),
                "skills": params.get("skills"),
                "model": str(params.get("model") or ""),
                "provider": str(params.get("provider") or ""),
            }
            saved = roster.upsert(entry)
            # Respawn so the new desk is usable in this session without a
            # restart, and so an edited brief takes effect immediately.
            swarm.stop(saved["id"])
            agent = swarm.spawn_from_roster(saved["id"])
            if agent is None:
                return {"success": False,
                        "error": (f"Saved '{name}' but could not start it. It "
                                  "will be there after a restart.")}
            return {
                "success": True,
                "agentId": saved["id"],
                "name": saved["name"],
                "type": saved["type"],
                "message": (f"Created the swarm agent '{saved['name']}'. Ask it "
                            "for something by name whenever you like."),
            }
        self.register(SkillDefinition(
            "swarm_create",
            "Create a named swarm agent, or update the one with this name. Use "
            "it when the user asks for a new specialist — 'make me an agent "
            "called Scout who reviews my writing' — or wants to change what an "
            "existing desk does. Say the name back to the user once it exists, "
            "because the point of naming it is that they can ask for it by "
            "name afterwards. Refuses a name that is already taken.",
            {"type": "object", "properties": {
                "name": {"type": "string",
                         "description": "What the agent is called. This is how "
                                        "the user will ask for it later."},
                "type": {"type": "string",
                         "description": "One of the built-in types (general, "
                                        "coder, reviewer, analyst, researcher, "
                                        "writer, planner, devops, qa) or omit "
                                        "for general."},
                "brief": {"type": "string",
                          "description": "Standing instructions in a few "
                                         "sentences: tone, red lines, what "
                                         "good looks like."},
                "role": {"type": "string",
                         "description": "One-line role, if the type default is "
                                        "not right."},
                "skills": {"type": "array", "items": {"type": "string"},
                           "description": "Optional tool skills to narrow it to. "
                                          "Omit for all enabled skills."},
                "model": {"type": "string",
                          "description": "Optional model to pin this desk to."},
            }, "required": ["name"]},
            swarm_create, "system",
        ))

        async def swarm_rename(params: dict) -> dict:
            """Give an agent a different name, keeping everything else.

            Kept separate from `swarm_create` because "rename Scout to Lookout"
            is a different intent from "make Scout", and a single tool that
            guessed between them would sometimes create a second desk when the
            user meant to move one.

            The id is deliberately unchanged, which is what makes the learned
            rules survive: `roster.rules_for` reads
            `feedback/<agent_id>.md`, so editing the name on the same id carries
            every correction the agent has earned straight across. A rename that
            minted a new id would look identical to the user and silently throw
            away everything the desk had learned.
            """
            from backend.swarm import roster
            from backend.swarm.orchestrator import swarm

            handle = str(params.get("agent") or params.get("agentId")
                         or params.get("from") or "").strip()
            new_name = str(params.get("name") or params.get("to") or "").strip()
            if not handle:
                return {"success": False,
                        "error": "Name the agent to rename in 'agent'."}
            if not new_name:
                return {"success": False,
                        "error": "Give the new name in 'name'."}

            entry = roster.find(handle)
            if not entry:
                collided = roster.find_ambiguous(handle)
                if len(collided) > 1:
                    return {"success": False,
                            "error": (f"'{handle}' could match more than one "
                                      "agent: " + ", ".join(collided)
                                      + ". Use the exact name or the id.")}
                names = [str(e.get("name") or "?") for e in roster.definitions()]
                return {"success": False,
                        "error": (f"No swarm agent called '{handle}'. Saved "
                                  "agents: " + (", ".join(names) if names
                                                else "(none yet)") )}

            taken = roster.find(new_name)
            agent_id = str(entry.get("id") or "")
            if taken and str(taken.get("id")) != agent_id:
                return {"success": False,
                        "error": (f"'{taken.get('name')}' is already an agent. "
                                  "Pick a name that is not in use.")}

            moved = dict(entry)
            moved["name"] = new_name
            saved = roster.upsert(moved)

            rules = roster.rules_for(agent_id)
            # Respawn so the running session answers to the new name at once.
            swarm.stop(agent_id)
            agent = swarm.spawn_from_roster(agent_id)
            return {
                "success": True,
                "agentId": saved["id"],
                "name": saved["name"],
                "previousName": entry.get("name"),
                "rulesKept": len(rules),
                "message": (f"Renamed '{entry.get('name')}' to "
                            f"'{saved['name']}'"
                            + (f", keeping its {len(rules)} learned rule(s)"
                               if rules else "")
                            + "."),
                "warning": None if agent is not None else
                           "Saved, but the running session will pick it up "
                           "after a restart.",
            }
        self.register(SkillDefinition(
            "swarm_rename",
            "Rename a swarm agent, keeping its brief, learned rules and "
            "notebook. Use it when the user wants a desk called something else "
            "— 'rename Scout to Lookout'. This does not create a new agent and "
            "does not lose what the existing one has learned.",
            {"type": "object", "properties": {
                "agent": {"type": "string",
                          "description": "The agent's current name or id."},
                "name": {"type": "string",
                         "description": "The new name."},
            }, "required": ["agent", "name"]},
            swarm_rename, "system",
        ))

    # ── System ───────────────────────────────────────────────────────────

    def _register_system_skills(self):
        async def run_command(params: dict) -> dict:
            """Run a PowerShell command, asking in-band when it is destructive.

            This used to construct a TerminalExecutor and call it directly,
            which skipped the action executor and with it the destruction gate's
            approval handle: a gated command came back as "requires approval"
            with no approval_id, and nothing could ever approve it. Going
            through `execute_for_chat` means a safe command runs immediately and
            a destructive one waits for the user's answer on the same turn.
            """
            from backend.actions.executor import executor
            from dataclasses import asdict
            result = await executor.execute_for_chat("run_command", {
                "command": str(params.get("command", "")),
                "cwd": params.get("cwd"),
                "timeout": params.get("timeout", 30),
            })
            payload = asdict(result)
            if result.data:
                payload.update(result.data)
            if not result.success:
                # A failed command reported only a non-zero exit code and its
                # stderr, leaving `error` empty: the model saw success=false
                # with nothing to act on and invented an explanation. stderr is
                # where the shell says what it objected to.
                if not payload.get("error"):
                    payload["error"] = (str((result.data or {}).get("stderr")
                                            or "").strip()
                                        or f"Command exited with code "
                                           f"{(result.data or {}).get('exit_code')}")
            # A command that needed a live shell fails here in a way that reads
            # like a broken tool. Name the tool that does have state, so the
            # model opens a session instead of retrying the same stateless call.
            try:
                from backend.actions.session import looks_interactive
                cmd = str(params.get("command", ""))
                if not result.success and looks_interactive(cmd):
                    payload["hint"] = (
                        "This command needs a shell that keeps its state "
                        "between calls. Open one with session_open, then "
                        "session_send the command into it.")
            except Exception:  # noqa: BLE001
                pass
            return payload
        self.register(SkillDefinition(
            "run_command",
            "Run a command in Windows PowerShell 5.1 on the user's PC. "
            "Use PowerShell syntax: ';' to chain commands (NOT '&&'), "
            "$env:USERPROFILE instead of '~', 'Test-Path' to check paths, "
            "'New-Item -ItemType Directory -Path X' to create folders. "
            "You have real access to the user's machine through this tool — "
            "safe commands run immediately and a destructive one waits for the "
            "user to approve it, so when that happens tell the user it is "
            "awaiting their approval instead of saying you cannot run it. "
            "Returns stdout/stderr of the command. High-output commands "
            "(git, pip, pytest, npm, ruff, gh, docker, kubectl, cargo) are "
            "auto-compressed by RTK; for other long outputs prefix with "
            "`rtk ` (e.g. `rtk read file.txt`, `rtk log app.log`).",
            {"type": "object", "properties": {
                "command": {"type": "string", "description": "The PowerShell command to execute"},
                "cwd": {"type": "string", "description": "Working directory"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            }, "required": ["command"]},
            run_command, "system", True,
        ))

        # ---- interactive sessions ------------------------------------------
        # A shell that stays alive between calls, for the work a one-shot
        # command cannot do: a `cd` that sticks, an env var the next command
        # reads, a REPL (python -i), an open ssh. Opt-in — `run_command` stays
        # the stateless default, because a live shell that outlives the call is
        # exactly the statefulness that makes a runaway hard to reason about.
        async def session_open(params: dict) -> dict:
            from backend.actions import session as sessions
            cwd = params.get("cwd")
            if cwd and not str(cwd).strip():
                cwd = None
            return await sessions.sessions.open(
                str(params.get("session") or "main"), cwd=cwd)
        self.register(SkillDefinition(
            "session_open",
            "Open a long-lived interactive shell (PowerShell on Windows) that "
            "keeps its working directory and environment between calls. Use it "
            "when a task needs state a one-shot command cannot keep: changing "
            "directory and running several commands there, setting an env var "
            "for later commands, running a REPL (python -i), or holding an ssh "
            "connection open. Safe: it only starts a shell, runs nothing. "
            "After it, use session_send to type into it.",
            {"type": "object", "properties": {
                "session": {"type": "string",
                            "description": "Name for the session, e.g. 'main'. "
                                           "Reopening the same name reuses it."},
                "cwd": {"type": "string",
                        "description": "Working directory to start in."},
            }},
            session_open, "system",
        ))

        async def session_send(params: dict) -> dict:
            from backend.actions import session as sessions
            return await sessions.sessions.send(
                str(params.get("session") or "main"),
                str(params.get("command") or ""),
                wait_s=float(params.get("wait") or 30))
        self.register(SkillDefinition(
            "session_send",
            "Type a command into an open interactive session and read its "
            "output. The session keeps its state, so a `cd` here affects the "
            "next session_send. If the command is still running when the wait "
            "ends (a REPL, a server), the result says so and you read more with "
            "session_read. Requires session_open first.",
            {"type": "object", "properties": {
                "session": {"type": "string", "description": "Session name."},
                "command": {"type": "string",
                            "description": "The line to type into the shell."},
                "wait": {"type": "integer",
                         "description": "Seconds to wait for it to finish.",
                         "default": 30},
            }, "required": ["command"]},
            # `requires_approval=True`, and it is the whole point of this
            # skill's entry. It types a line into a live PowerShell - the same
            # shell `run_command` runs - so leaving the flag at its default
            # made the shell reachable through a door that never asked, one
            # line away from a door that asked every time. Measured before
            # this: `session_send` with `Remove-Item <path> -Force` deleted
            # the file, returned success, and raised no prompt.
            #
            # It is also in `approvals.policy.CONTENT_CLASSIFIED`, which is the
            # part that decides WHAT the dashboard may offer. Without that, the
            # skills page draws its permanent "always allow" switch, and a
            # permanent grant keyed on the name cannot tell a safe line from
            # `Remove-Item -Recurse -Force` - so one click would disarm the
            # shell for every command typed into it afterwards. Session-only is
            # the answer that matches the risk, exactly as for `run_command`.
            session_send, "system", True,
        ))

        async def session_read(params: dict) -> dict:
            from backend.actions import session as sessions
            return await sessions.sessions.read(
                str(params.get("session") or "main"),
                wait_s=float(params.get("wait") or 2))
        self.register(SkillDefinition(
            "session_read",
            "Read whatever an interactive session has printed since the last "
            "read. Use it after session_send reported that a command was still "
            "running.",
            {"type": "object", "properties": {
                "session": {"type": "string", "description": "Session name."},
                "wait": {"type": "integer",
                         "description": "Seconds to wait before reading.",
                         "default": 2},
            }},
            session_read, "system",
        ))

        async def session_close(params: dict) -> dict:
            from backend.actions import session as sessions
            return await sessions.sessions.close(
                str(params.get("session") or "main"))
        self.register(SkillDefinition(
            "session_close",
            "Close an interactive session and stop its shell. Do this when the "
            "work that needed it is done — sessions are also closed "
            "automatically after 30 minutes idle.",
            {"type": "object", "properties": {
                "session": {"type": "string", "description": "Session name."},
            }},
            session_close, "system",
        ))

        async def session_list(params: dict) -> dict:
            from backend.actions import session as sessions
            live = sessions.sessions.list_sessions()
            return {"success": True, "sessions": live, "count": len(live)}
        self.register(SkillDefinition(
            "session_list",
            "List the interactive sessions that are currently open.",
            {"type": "object", "properties": {}},
            session_list, "system",
        ))

        async def get_screen_size(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().get_screen_size()
        self.register(SkillDefinition(
            "get_screen_size", "Get the current screen/monitor resolution",
            {"type": "object", "properties": {}},
            get_screen_size, "system",
        ))

        async def screenshot(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().screenshot(
                params.get("monitor"), params.get("region"))
        self.register(SkillDefinition(
            "screenshot", "Take a screenshot of the screen",
            {"type": "object", "properties": {
                "monitor": {"type": "integer", "description": "Monitor index"},
                "region": {"type": "array", "description": "[x,y,w,h] region"},
            }},
            screenshot, "system",
        ))

        async def clipboard_read(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            s = SystemControls()
            text = await s.get_clipboard()
            return {"success": True, "text": text}
        self.register(SkillDefinition(
            "get_clipboard", "Read the contents of the system clipboard",
            {"type": "object", "properties": {}},
            clipboard_read, "system",
        ))

        async def clipboard_write(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().set_clipboard(str(params.get("text", "")))
        self.register(SkillDefinition(
            "set_clipboard", "Write text to the system clipboard",
            {"type": "object", "properties": {
                "text": {"type": "string", "description": "Text to copy"},
            }, "required": ["text"]},
            clipboard_write, "system",
        ))

        # ---- Gated desktop input (mouse/keyboard) ---------------------------

        async def desktop_click(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.click(
                int(params.get("x", 0)), int(params.get("y", 0)),
                params.get("button", "left"))
        self.register(SkillDefinition(
            "desktop_click",
            "Click at screen coordinates (needs Desktop Control permission — "
            "disabled by default)",
            {"type": "object", "properties": {
                "x": {"type": "integer", "description": "Screen X coordinate"},
                "y": {"type": "integer", "description": "Screen Y coordinate"},
                "button": {"type": "string", "default": "left"},
            }, "required": ["x", "y"]},
            desktop_click, "system", True,
        ))

        async def desktop_type(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.type_text(str(params.get("text", "")))
        self.register(SkillDefinition(
            "desktop_type",
            "Type text via the keyboard at the current focus (needs Desktop "
            "Control permission — disabled by default)",
            {"type": "object", "properties": {
                "text": {"type": "string", "description": "Text to type"},
            }, "required": ["text"]},
            desktop_type, "system", True,
        ))

        async def desktop_hotkey(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.hotkey(str(params.get("combo", "")))
        self.register(SkillDefinition(
            "desktop_hotkey",
            "Press a keyboard shortcut like ctrl+c (whitelisted combos only; "
            "needs Desktop Control permission)",
            {"type": "object", "properties": {
                "combo": {"type": "string", "description": "e.g. ctrl+c"},
            }, "required": ["combo"]},
            desktop_hotkey, "system", True,
        ))

        async def desktop_scroll(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.scroll(
                str(params.get("direction", "down")),
                int(params.get("amount", 3)))
        self.register(SkillDefinition(
            "desktop_scroll",
            "Scroll the mouse wheel (needs Desktop Control permission)",
            {"type": "object", "properties": {
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "integer", "default": 3},
            }},
            desktop_scroll, "system", True,
        ))

        async def desktop_move_mouse(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.move_mouse(
                int(params.get("x", 0)), int(params.get("y", 0)))
        self.register(SkillDefinition(
            "desktop_move_mouse",
            "Move the mouse cursor (needs Desktop Control permission)",
            {"type": "object", "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            }, "required": ["x", "y"]},
            desktop_move_mouse, "system", True,
        ))

        async def desktop_drag(params: dict) -> dict:
            from backend.actions.desktop_control import desktop_control
            return await desktop_control.drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)))
        self.register(SkillDefinition(
            "desktop_drag",
            "Drag from (x1,y1) to (x2,y2) (needs Desktop Control permission)",
            {"type": "object", "properties": {
                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                "x2": {"type": "integer"}, "y2": {"type": "integer"},
            }, "required": ["x1", "y1", "x2", "y2"]},
            desktop_drag, "system", True,
        ))

        async def volume(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().set_volume(params.get("level", 50))
        self.register(SkillDefinition(
            "set_volume", "Set system volume level (0-100)",
            {"type": "object", "properties": {
                "level": {"type": "integer", "description": "Volume 0-100", "default": 50},
            }, "required": ["level"]},
            volume, "system",
        ))

        async def brightness(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().set_brightness(params.get("level", 50))
        self.register(SkillDefinition(
            "set_brightness", "Set screen brightness (0-100)",
            {"type": "object", "properties": {
                "level": {"type": "integer", "description": "Brightness 0-100", "default": 50},
            }, "required": ["level"]},
            brightness, "system",
        ))

        async def lock_screen(params: dict) -> dict:
            from backend.actions.system_controls import SystemControls
            return await SystemControls().lock()
        self.register(SkillDefinition(
            "lock_screen", "Lock the computer screen",
            {"type": "object", "properties": {}},
            lock_screen, "system",
        ))

    # ── File Operations ──────────────────────────────────────────────────

    def _register_file_skills(self):
        async def read_file(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().read(
                str(params.get("path", "")),
                params.get("encoding", "utf-8"),
            )
        self.register(SkillDefinition(
            "read_file", "Read the contents of a file",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Absolute or relative file path"},
                "encoding": {"type": "string", "description": "File encoding", "default": "utf-8"},
            }, "required": ["path"]},
            read_file, "files",
            aliases={"path": ("file_path", "filepath", "filename", "file")},
        ))

        async def write_file(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().write(
                str(params.get("path", "")),
                str(params.get("content", "")),
            )
        self.register(SkillDefinition(
            "write_file", "Write content to a file (creates or overwrites)",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
            }, "required": ["path", "content"]},
            write_file, "files", True,
            aliases={"path": ("file_path", "filepath", "filename", "file"),
                     "content": ("text", "data", "contents", "body")},
        ))

        async def list_dir(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().list_dir(
                str(params.get("path", ".")),
                params.get("pattern", "*"),
            )
        self.register(SkillDefinition(
            "list_dir", "List files and folders in a directory",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Directory path", "default": "."},
                "pattern": {"type": "string", "description": "File pattern e.g. *.py", "default": "*"},
            }, "required": ["path"]},
            list_dir, "files",
        ))

        async def search_files(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().search(
                str(params.get("directory", ".")),
                str(params.get("pattern", "*")),
                params.get("recursive", True),
            )
        self.register(SkillDefinition(
            "search_files", "Search for files matching a pattern",
            {"type": "object", "properties": {
                "directory": {"type": "string", "description": "Directory to search"},
                "pattern": {"type": "string", "description": "Glob pattern e.g. **/*.py"},
                "recursive": {"type": "boolean", "description": "Search subdirectories", "default": True},
            }, "required": ["directory", "pattern"]},
            search_files, "files",
        ))

        async def search_in_files(params: dict) -> dict:
            """Find which files CONTAIN a piece of text.

            `search_files` above matches file *names* against a glob, so it
            cannot answer "where is this function defined?" — the question that
            has to be answered before editing anything. Without this the agent's
            only options were guessing a path from a description and reading it,
            which is how a change ends up proposed against the wrong file.

            Confined to the bound workspace the same way the code.* methods are,
            so a search cannot be used to read outside it.
            """
            import os as _os
            from backend.codemode import (OutsideWorkspace,
                                          resolve_in_workspace)
            from backend.workspace import root as _workspace_root

            needle = str(params.get("query") or "").strip()
            if len(needle) < 2:
                return {"matches": [], "error": "Use at least two characters."}

            where = str(params.get("directory") or ".").strip() or "."
            limit = max(1, min(int(params.get("limit") or 60), 200))
            # Default to no filter. `extensions` is accepted as a string OR a
            # list: the schema says string, but a model reliably sends
            # ["py"] and the old code called `.split(",")` on it, which is how a
            # search silently came back with nothing.
            suffix = params.get("extensions") or ""
            if isinstance(suffix, (list, tuple)):
                suffix = ",".join(str(x) for x in suffix)
            suffix = str(suffix).strip()

            try:
                root = resolve_in_workspace(_workspace_root(), ".")
            except OutsideWorkspace as exc:
                return {"matches": [], "error": str(exc)}

            # A directory that does not exist must be reported, not treated as
            # an empty tree. Returning "no matches" for a bogus path taught the
            # model that the symbol was absent from a project it never searched,
            # and it then reported that to the user as a finding.
            if not _os.path.isdir(_os.path.join(root, where)):
                here = sorted(
                    d for d in _os.listdir(root)
                    if _os.path.isdir(_os.path.join(root, d))
                    and d not in {".git", "node_modules", "__pycache__"}
                )
                return {
                    "matches": [], "scanned": 0,
                    "error": (f"There is no folder '{where}' in the workspace. "
                              f"Omit 'directory' to search the whole workspace, "
                              f"or use one of: {', '.join(here[:20]) or '(none)'}"),
                }
            try:
                start = resolve_in_workspace(_workspace_root(), where)
            except OutsideWorkspace as exc:
                return {"matches": [], "error": str(exc)}

            wanted = tuple(
                s if s.startswith(".") else "." + s
                for s in (x.strip() for x in suffix.split(",")) if s
            )
            skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
                    "dist", "build", ".next"}
            needle_cf = needle.casefold()
            matches, scanned, truncated = [], 0, False
            for dirpath, dirnames, filenames in _os.walk(start):
                dirnames[:] = sorted(d for d in dirnames if d not in skip)
                for name in sorted(filenames):
                    if wanted and not name.endswith(wanted):
                        continue
                    path = _os.path.join(dirpath, name)
                    try:
                        if _os.path.getsize(path) > 1_000_000:
                            continue
                        with open(path, "r", encoding="utf-8",
                                  errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        continue
                    if "\x00" in text:          # binary
                        continue
                    scanned += 1
                    rel = _os.path.relpath(path, root).replace(_os.sep, "/")
                    for line_no, line in enumerate(text.splitlines(), 1):
                        if needle_cf in line.casefold():
                            matches.append({
                                "filePath": rel,
                                "line": line_no,
                                "text": line.strip()[:200],
                            })
                            if len(matches) >= limit:
                                truncated = True
                                break
                    if truncated:
                        break
                if truncated:
                    break
            return {"matches": matches, "scanned": scanned,
                    "truncated": truncated,
                    "error": "" if matches else
                             "No file in the workspace contains that text."}

        self.register(SkillDefinition(
            "search_in_files",
            "Search the workspace for files CONTAINING a piece of text. Use this "
            "to find where a function, class or string is defined or used before "
            "editing it — searching beats guessing a filename.",
            {"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "Text to look for, e.g. a function name"},
                "directory": {"type": "string",
                              "description": ("Folder to search, relative to the "
                                              "workspace root. Omit it to search "
                                              "everything — safer than guessing a "
                                              "folder name."),
                              "default": "."},
                "extensions": {"type": "string",
                               "description": ("Optional file filter, e.g. '.py' or "
                                               "'.py,.ts'. Leave empty to search "
                                               "all text files.")},
                "limit": {"type": "integer", "description": "Max matches", "default": 60},
            }, "required": ["query"]},
            search_in_files, "files",
        ))

        async def delete_file(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().delete(str(params.get("path", "")))
        self.register(SkillDefinition(
            "delete_file", "Delete a file",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "File to delete"},
            }, "required": ["path"]},
            delete_file, "files", True,
            aliases={"path": ("file_path", "filepath", "filename", "file")},
        ))

        # Renaming and moving, as distinct from delete and write.
        #
        # `FileOps.move` and `FileOps.copy` existed and were reachable through
        # the ActionExecutor, but neither was registered as a skill — so from a
        # chat turn the capability was invisible. Asked to rename a file, the
        # model looked at its tool list, correctly reported "none of the listed
        # tools can rename files", and fell back to shelling out to
        # `Rename-Item`. A file operation that basic belongs in the catalogue.
        #
        # Renaming IS a move: within one folder, a move is the rename. That is
        # why there is no separate `rename_file` skill — a second entry point to
        # the same operation would be a second thing to keep in step.
        async def move_file(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().move(
                str(params.get("source", "")),
                str(params.get("dest", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "move_file",
            "Move or rename a file or folder. To rename, give the same folder "
            "with a new name in `dest` (e.g. source 'notes.txt' -> dest "
            "'draft.txt'). The destination must not already exist unless you "
            "pass overwrite=true, and the result reports the path it actually "
            "used — moving onto an existing folder puts the source INSIDE it.",
            {"type": "object", "properties": {
                "source": {"type": "string",
                           "description": "Path to move or rename"},
                "dest": {"type": "string",
                         "description": ("New path. For a rename, the same "
                                         "folder with a new filename.")},
                "overwrite": {"type": "boolean",
                              "description": ("Replace an existing destination. "
                                              "Refused by default, and a .bak "
                                              "of the old file is kept when it "
                                              "is allowed."),
                              "default": False},
            }, "required": ["source", "dest"]},
            move_file, "files", True,
            aliases={"source": ("src", "from", "path", "file", "file_path",
                                "old_path", "old_name"),
                     "dest": ("destination", "dst", "to", "target", "new_path",
                              "new_name", "new")},
        ))

        async def copy_file(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().copy(
                str(params.get("source", "")),
                str(params.get("dest", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "copy_file",
            "Copy a file or folder, leaving the original in place. Refuses an "
            "existing destination unless overwrite=true, and keeps a .bak of "
            "the file it replaces.",
            {"type": "object", "properties": {
                "source": {"type": "string", "description": "Path to copy"},
                "dest": {"type": "string", "description": "Where to copy it to"},
                "overwrite": {"type": "boolean",
                              "description": ("Replace an existing destination. "
                                              "Refused by default."),
                              "default": False},
            }, "required": ["source", "dest"]},
            copy_file, "files", True,
            aliases={"source": ("src", "from", "path", "file", "file_path"),
                     "dest": ("destination", "dst", "to", "target", "new_path")},
        ))

        async def create_dir(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().create_dir(str(params.get("path", "")))
        self.register(SkillDefinition(
            "create_dir", "Create a new directory",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Directory path to create"},
            }, "required": ["path"]},
            create_dir, "files",
            aliases={"path": ("dir_path", "directory", "dir", "folder", "folder_path")},
        ))

        async def file_info(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().info(str(params.get("path", "")))
        self.register(SkillDefinition(
            "file_info",
            "Get file metadata (size, modified time, type)",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "File path"},
            }, "required": ["path"]},
            file_info, "files",
            aliases={"path": ("file_path", "filepath", "filename", "file")},
        ))

    # ── Office documents ─────────────────────────────────────────────────
    #
    # Separate from the plain file skills because these formats are not text.
    # `.docx`, `.xlsx` and `.pptx` are ZIP archives of XML and a `.pdf` is a
    # binary container, so `write_file` produces a corrupt file for any of them:
    # it opens with `encoding="utf-8"` and writes TEXT. These use a library that
    # knows the format.
    #
    # Reads are ungated, writes are gated. Reading a document tells Addled no
    # more than reading a text file it is already trusted with; writing one can
    # discard a document the user was working in. The `overwrite` flag is the
    # same rule the plain file skills follow.
    def _register_office_skills(self):
        async def word_read(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.word_read(str(params.get("path", "")))
        self.register(SkillDefinition(
            "word_read",
            "Read a Word document (.docx) — its text, its headings, and any "
            "tables. Use this rather than read_file, which cannot open a .docx.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .docx"},
            }, "required": ["path"]},
            word_read, "files",
            aliases={"path": ("file_path", "filepath", "filename", "file")},
        ))

        async def word_create(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.word_create(
                str(params.get("path", "")),
                title=str(params.get("title", "")),
                content=str(params.get("content", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "word_create",
            "Create a Word document (.docx) from text. Lay `content` out with "
            "plain markers: '# ', '## ' and '### ' become headings, and '- ' "
            "becomes a bullet. Refuses to replace an existing file unless "
            "overwrite=true, so use word_edit to change a document that is "
            "already there.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path for the .docx"},
                "title": {"type": "string", "description": "Document title."},
                "content": {"type": "string",
                            "description": ("Body text. '# ' heading, "
                                            "'- ' bullet, blank line separates.")},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Replace an existing file."},
            }, "required": ["path"]},
            word_create, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def word_edit(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.word_edit(
                str(params.get("path", "")),
                find=str(params.get("find", "")),
                replace=str(params.get("replace", "")),
                append=str(params.get("append", "")),
                add_heading=str(params.get("add_heading", "")),
            )
        self.register(SkillDefinition(
            "word_edit",
            "Change an existing Word document (.docx) in place. find/replace "
            "rewrites every paragraph containing the phrase; append adds "
            "paragraphs at the end; add_heading adds a heading. Give at least "
            "one of them. Fails if the phrase is not found, rather than "
            "reporting a save that changed nothing.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .docx"},
                "find": {"type": "string", "description": "Text to find."},
                "replace": {"type": "string", "description": "Text to put there."},
                "append": {"type": "string",
                           "description": "Paragraph(s) to add at the end."},
                "add_heading": {"type": "string",
                                "description": "A heading to add at the end."},
            }, "required": ["path"]},
            word_edit, "files", True,
            aliases={"path": ("file_path", "filepath", "filename"),
                     "find": ("search", "old", "old_text"),
                     "replace": ("new", "new_text", "with")},
        ))

        async def excel_read(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.excel_read(
                str(params.get("path", "")),
                sheet=str(params.get("sheet", "")),
                max_rows=params.get("max_rows", 500),
            )
        self.register(SkillDefinition(
            "excel_read",
            "Read a spreadsheet (.xlsx/.xlsm). Returns rows as lists, with "
            "formulas as their computed values. Name a sheet to read one, or "
            "omit it for the active sheet. Use excel_sheets first when you do "
            "not know the sheet names.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .xlsx"},
                "sheet": {"type": "string", "description": "Sheet name."},
                "max_rows": {"type": "integer", "default": 500},
            }, "required": ["path"]},
            excel_read, "files",
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def excel_write(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.excel_write(
                str(params.get("path", "")),
                sheet=str(params.get("sheet", "")),
                grid=params.get("grid"),
                cells=params.get("cells"),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "excel_write",
            "Write values into a spreadsheet (.xlsx), creating it if needed. "
            "Pass `grid` as a list of rows (written from A1) or `cells` as "
            "[{row, col, value}]. An existing file is UPDATED — other sheets "
            "and rows are kept. Pass overwrite=true only when you mean to "
            "replace the whole workbook with a new one.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .xlsx"},
                "sheet": {"type": "string", "description": "Sheet name."},
                "grid": {"type": "array",
                         "description": "Rows, e.g. [[\"Item\",\"Cost\"],[...]]",
                         "items": {"type": "array"}},
                "cells": {"type": "array",
                          "description": "[{row, col, value}] — 1-based.",
                          "items": {"type": "object"}},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Start a new workbook instead."},
            }, "required": ["path"]},
            excel_write, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def excel_sheets(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.excel_sheets(str(params.get("path", "")))
        self.register(SkillDefinition(
            "excel_sheets",
            "List the sheet names and their sizes in a spreadsheet (.xlsx). "
            "Read-only. Use it to find the right sheet before excel_read.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .xlsx"},
            }, "required": ["path"]},
            excel_sheets, "files",
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pptx_read(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pptx_read(str(params.get("path", "")))
        self.register(SkillDefinition(
            "pptx_read",
            "Read a PowerPoint deck (.pptx) — the title and body text of each "
            "slide, in order.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .pptx"},
            }, "required": ["path"]},
            pptx_read, "files",
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pptx_create(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pptx_create(
                str(params.get("path", "")),
                title=str(params.get("title", "")),
                slides=params.get("slides"),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "pptx_create",
            "Create a PowerPoint deck (.pptx). Give a title and a list of "
            "slides, each {\"title\": ..., \"bullets\": [...]} — or a plain "
            "string for a slide that is a title alone. Refuses to replace an "
            "existing deck unless overwrite=true.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path for the .pptx"},
                "title": {"type": "string", "description": "Deck title."},
                "slides": {"type": "array",
                           "description": ("[{\"title\":..., \"bullets\":[...]}] "
                                           "or a string per slide."),
                           "items": {}},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path"]},
            pptx_create, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pptx_add_slide(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pptx_add_slide(
                str(params.get("path", "")),
                title=str(params.get("title", "")),
                bullets=params.get("bullets"),
            )
        self.register(SkillDefinition(
            "pptx_add_slide",
            "Append one slide to an existing PowerPoint deck (.pptx).",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .pptx"},
                "title": {"type": "string", "description": "Slide title."},
                "bullets": {"type": "array", "description": "Bullet lines.",
                            "items": {"type": "string"}},
            }, "required": ["path"]},
            pptx_add_slide, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pdf_read(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_read(
                str(params.get("path", "")),
                pages=str(params.get("pages", "")),
                max_pages=params.get("max_pages", 500),
            )
        self.register(SkillDefinition(
            "pdf_read",
            "Read a PDF — its text, page count and metadata. Use `pages` to "
            "read part of a long document instead of all of it: \"1-5\", \"2\" "
            "or \"1,3,7\". A PDF with no text layer (a scan) is reported as "
            "such rather than returned as empty. This reads; it cannot edit an "
            "existing PDF.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .pdf"},
                "pages": {"type": "string",
                          "description": "e.g. '1-5', '2', '1,3,7'. Omit for all."},
                "max_pages": {"type": "integer", "default": 500},
            }, "required": ["path"]},
            pdf_read, "files",
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pdf_create(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_create(
                str(params.get("path", "")),
                title=str(params.get("title", "")),
                content=str(params.get("content", "")),
                page_size=str(params.get("page_size", "a4")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "pdf_create",
            "Create a new PDF from text. Lay `content` out with the same "
            "markers as word_create: '# ', '## ' and '### ' become headings, "
            "'- ' a bullet, and a blank line a paragraph break. This makes a "
            "new document — it cannot edit or merge an existing PDF. Text "
            "outside the PDF core fonts (non-Latin scripts, emoji) is written "
            "as '?' and reported, because rendering it needs an embedded font.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path for the .pdf"},
                "title": {"type": "string", "description": "Document title."},
                "content": {"type": "string",
                            "description": ("Body text. '# ' heading, '- ' "
                                            "bullet, blank line separates.")},
                "page_size": {"type": "string", "default": "a4",
                              "description": "a4, letter, legal or a3."},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Replace an existing file."},
            }, "required": ["path"]},
            pdf_create, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pdf_pages(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_pages(str(params.get("path", "")))
        self.register(SkillDefinition(
            "pdf_pages",
            "List the pages of a PDF, each with a short text preview and its "
            "size. Read-only. Take this before pdf_edit when you need to "
            "rearrange or delete pages — a count alone does not say which page "
            "is which, and moving the wrong one is not recoverable from the "
            "result.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .pdf"},
            }, "required": ["path"]},
            pdf_pages, "files",
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pdf_edit(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_edit(
                str(params.get("path", "")),
                remove_pages=params.get("remove_pages", ""),
                keep_pages=params.get("keep_pages", ""),
                order=params.get("order", ""),
                rotate=params.get("rotate", 0),
                rotate_pages=params.get("rotate_pages", ""),
                title=str(params.get("title", "")),
                author=str(params.get("author", "")),
                subject=str(params.get("subject", "")),
            )
        self.register(SkillDefinition(
            "pdf_edit",
            "Change the PAGES and metadata of an existing PDF, in place. Select "
            "with remove_pages or keep_pages ('1-3,7'), reorder with order, turn "
            "pages with rotate (90/180/270), and set title/author/subject. "
            "IMPORTANT: this cannot rewrite the TEXT inside a page — a PDF "
            "stores positioned glyphs, not paragraphs. To change wording, "
            "re-create the document or produce a new one.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to the .pdf"},
                "remove_pages": {"type": "string",
                                 "description": "Pages to drop, e.g. '2,5-7'."},
                "keep_pages": {"type": "string",
                               "description": "Pages to keep. Overrides remove."},
                "order": {"type": "string",
                          "description": ("The wanted page sequence, e.g. "
                                          "'3,1,2'. Pages left out are dropped.")},
                "rotate": {"type": "integer", "default": 0,
                           "description": "Degrees: 90, 180 or 270."},
                "rotate_pages": {"type": "string",
                                 "description": "Which pages to rotate."},
                "title": {"type": "string", "description": "Set the PDF title."},
                "author": {"type": "string", "description": "Set the author."},
                "subject": {"type": "string", "description": "Set the subject."},
            }, "required": ["path"]},
            pdf_edit, "files", True,
            aliases={"path": ("file_path", "filepath", "filename")},
        ))

        async def pdf_merge(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_merge(
                str(params.get("path", "")),
                sources=params.get("sources"),
                keep_source=params.get("keep_source", True),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "pdf_merge",
            "Combine PDFs into one file, in the order given. `sources` are "
            "joined onto `path`, so this also appends files to an existing "
            "document. The sources are kept by default; pass keep_source=false "
            "to remove them afterwards. Refuses to overwrite the destination "
            "unless overwrite=true.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Destination PDF (created, or appended to)."},
                "sources": {"type": "array",
                            "description": "PDF paths to join, in order.",
                            "items": {"type": "string"}},
                "keep_source": {"type": "boolean", "default": True,
                                "description": "Keep the source files."},
                "overwrite": {"type": "boolean", "default": False,
                              "description": "Replace an existing destination."},
            }, "required": ["path", "sources"]},
            pdf_merge, "files", True,
            aliases={"path": ("file_path", "filepath", "filename"),
                     "sources": ("files", "inputs", "from")},
        ))

        async def pdf_extract(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_extract(
                str(params.get("path", "")),
                pages=str(params.get("pages", "")),
                output=str(params.get("output", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "pdf_extract",
            "Copy pages out of a PDF into a NEW file, leaving the original "
            "untouched. Use `pages` to choose them ('1-3,7'; omit for all), and "
            "`output` for the destination — without it the name is derived from "
            "the source. Different from pdf_edit: this keeps the original, so it "
            "is the right tool when the user only wants a subset.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Source .pdf"},
                "pages": {"type": "string",
                          "description": "e.g. '1-3,7'. Omit for every page."},
                "output": {"type": "string",
                           "description": "Destination path for the new PDF."},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path"]},
            pdf_extract, "files", True,
            aliases={"path": ("file_path", "filepath", "filename"),
                     "output": ("dest", "destination", "out", "to")},
        ))

        async def pdf_redact(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_redact(
                str(params.get("path", "")),
                terms=params.get("terms"),
                pages=str(params.get("pages", "")),
                output=str(params.get("output", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "pdf_redact",
            "Permanently REMOVE text from a PDF — an account number, a name, an "
            "address. `terms` lists the exact strings to find and delete; the "
            "text is replaced, not covered, so it cannot be recovered by "
            "copying the page or extracting the text. Writes a NEW file by "
            "default, because redaction cannot be undone. Always say what you "
            "are about to remove and let the user confirm before calling this.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Source .pdf"},
                "terms": {"type": "array",
                          "description": "Exact strings to remove.",
                          "items": {"type": "string"}},
                "pages": {"type": "string",
                          "description": "Limit to pages, e.g. '1-3'. Omit for all."},
                "output": {"type": "string",
                           "description": ("Where to write the redacted file. "
                                           "Defaults to <name>_redacted.pdf — "
                                           "never the original.")},
                "overwrite": {"type": "boolean", "default": False,
                              "description": ("Replace the output if it exists.")},
            }, "required": ["path", "terms"]},
            pdf_redact, "files", True,
            aliases={"path": ("file_path", "filepath", "filename"),
                     "terms": ("text", "search", "find", "values"),
                     "output": ("dest", "destination", "out", "to")},
        ))

        async def pdf_redact_verify(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.pdf_redact_verify(str(params.get("path", "")),
                                            terms=params.get("terms"))
        self.register(SkillDefinition(
            "pdf_redact_verify",
            "Check that text really is gone from a PDF — that it cannot be "
            "extracted any more. Read-only. Run this after pdf_redact when the "
            "document matters: a black box drawn over text leaves it "
            "extractable, and only a search can tell the difference between "
            "removed and merely covered.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "The redacted .pdf"},
                "terms": {"type": "array",
                          "description": "The strings that should be gone.",
                          "items": {"type": "string"}},
            }, "required": ["path", "terms"]},
            pdf_redact_verify, "files",
            aliases={"path": ("file_path", "filepath", "filename"),
                     "terms": ("text", "search", "find")},
        ))

        async def convert_to_pdf(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.convert_to_pdf(
                str(params.get("path", "")),
                output=str(params.get("output", "")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "convert_to_pdf",
            "Convert a document to PDF, keeping its layout: .docx, .pptx, "
            ".xlsx, .txt, .md or .csv -> .pdf. Word keeps headings and fonts; "
            "PowerPoint gives one page per slide; a spreadsheet becomes a table "
            "of its values. Use this to hand someone a single readable file, or "
            "to make a PDF out of a document Addled just wrote.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "The document to convert."},
                "output": {"type": "string",
                           "description": ("Where to write the PDF. Defaults to "
                                           "the same name with .pdf.")},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path"]},
            convert_to_pdf, "files", True,
            aliases={"path": ("file_path", "filepath", "filename", "source"),
                     "output": ("dest", "destination", "out", "to")},
        ))

        async def convert_from_pdf(params: dict) -> dict:
            from backend.actions import office_ops as office
            return office.convert_from_pdf(
                str(params.get("path", "")),
                output=str(params.get("output", "")),
                to=str(params.get("to", "text")),
                overwrite=bool(params.get("overwrite")),
            )
        self.register(SkillDefinition(
            "convert_from_pdf",
            "Turn a PDF into text (.txt), HTML (.html) or a Word document "
            "(.docx) — choose with `to`. This EXTRACTS the text; it does not "
            "rebuild the layout, because a PDF stores positioned glyphs and "
            "tables and columns do not survive being reflowed. A scanned PDF "
            "has no text to extract and is reported rather than written out "
            "empty.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "The .pdf to convert."},
                "to": {"type": "string", "default": "text",
                       "description": "text, html or docx."},
                "output": {"type": "string",
                           "description": ("Where to write it. Defaults to the "
                                           "same name with the new extension.")},
                "overwrite": {"type": "boolean", "default": False},
            }, "required": ["path"]},
            convert_from_pdf, "files", True,
            aliases={"path": ("file_path", "filepath", "filename", "source"),
                     "to": ("format", "target", "as"),
                     "output": ("dest", "destination", "out")},
        ))

    # ── Window Management ────────────────────────────────────────────────

    def _register_window_skills(self):
        async def list_windows(params: dict) -> dict:
            from backend.actions.window_manager import WindowManager
            return await WindowManager().list_windows()
        self.register(SkillDefinition(
            "list_windows", "List all open application windows",
            {"type": "object", "properties": {}},
            list_windows, "windows",
        ))

        async def focus_window(params: dict) -> dict:
            from backend.actions.window_manager import WindowManager
            return await WindowManager().focus(
                str(params.get("title_substring", "")))
        self.register(SkillDefinition(
            "focus_window", "Bring a window to front by title match",
            {"type": "object", "properties": {
                "title_substring": {"type": "string", "description": "Part of window title to match"},
            }, "required": ["title_substring"]},
            focus_window, "windows",
        ))

        async def resize_window(params: dict) -> dict:
            from backend.actions.window_manager import WindowManager
            return await WindowManager().resize(
                str(params.get("title_substring", "")),
                params.get("width", 800),
                params.get("height", 600),
            )
        self.register(SkillDefinition(
            "resize_window", "Resize a window by title match",
            {"type": "object", "properties": {
                "title_substring": {"type": "string"},
                "width": {"type": "integer", "default": 800},
                "height": {"type": "integer", "default": 600},
            }, "required": ["title_substring", "width", "height"]},
            resize_window, "windows",
        ))

        async def close_window(params: dict) -> dict:
            from backend.actions.window_manager import WindowManager
            return await WindowManager().close(
                str(params.get("title_substring", "")))
        self.register(SkillDefinition(
            "close_window", "Close a window by title match",
            {"type": "object", "properties": {
                "title_substring": {"type": "string"},
            }, "required": ["title_substring"]},
            close_window, "windows", True,
        ))

    # ── Browser ──────────────────────────────────────────────────────────

    def _register_browser_skills(self):
        async def browser_navigate(params: dict) -> dict:
            from backend.browser.browser_engine import browser
            return await browser.navigate(str(params.get("url", "")))
        self.register(SkillDefinition(
            "browser_navigate", "Open a URL in the built-in browser",
            {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to navigate to"},
            }, "required": ["url"]},
            browser_navigate, "browser",
        ))

        async def browser_extract(params: dict) -> dict:
            from backend.browser.browser_engine import browser
            return await browser.extract(params.get("selector"))
        self.register(SkillDefinition(
            "browser_extract", "Extract text content from a web page",
            {"type": "object", "properties": {
                "selector": {"type": "string", "description": "CSS selector (optional, defaults to body)"},
            }},
            browser_extract, "browser",
        ))

        async def browser_click(params: dict) -> dict:
            from backend.browser.browser_engine import browser
            return await browser.click(
                params.get("selector"), params.get("x"), params.get("y"))
        self.register(SkillDefinition(
            "browser_click", "Click an element on a web page",
            {"type": "object", "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "x": {"type": "integer", "description": "X coordinate"},
                "y": {"type": "integer", "description": "Y coordinate"},
            }},
            browser_click, "browser",
        ))

        async def browser_type(params: dict) -> dict:
            from backend.browser.browser_engine import browser
            return await browser.type_text(
                str(params.get("selector", "body")),
                str(params.get("text", "")),
            )
        self.register(SkillDefinition(
            "browser_type", "Type text into a web page input",
            {"type": "object", "properties": {
                "selector": {"type": "string", "description": "CSS selector for input"},
                "text": {"type": "string", "description": "Text to type"},
            }, "required": ["selector", "text"]},
            browser_type, "browser",
        ))

        async def browser_task(params: dict) -> dict:
            from backend.browser.browser_engine import browser
            return await browser.route_task(
                str(params.get("task", "")), params.get("max_steps"))
        self.register(SkillDefinition(
            "browser_task",
            "Delegate an open-ended multi-step web task (e.g. 'find the "
            "cheapest flight from NYC to Tokyo next Friday and list the "
            "top 3'). Addled picks the best backend automatically and "
            "falls back to a simple search when the AI framework or LLM "
            "is unavailable.",
            {"type": "object", "properties": {
                "task": {"type": "string",
                         "description": "Natural-language browsing task"},
                "max_steps": {"type": "integer",
                              "description": "Max framework steps",
                              "default": 10},
            }, "required": ["task"]},
            browser_task, "browser",
        ))

    # ── Code ─────────────────────────────────────────────────────────────

    def _register_code_skills(self):
        async def self_read(params: dict) -> dict:
            """Read one of Addled's own core files, under backend/."""
            from backend.codemode import selfmod
            path, err = selfmod._resolve(str(params.get("path") or ""))
            if err:
                return {"success": False, "error": err}
            if not path.is_file():
                return {"success": False,
                        "error": f"{params.get('path')} does not exist."}
            return {"success": True, "path": params.get("path"),
                    "content": path.read_text(encoding="utf-8",
                                              errors="replace")}
        self.register(SkillDefinition(
            "self_read",
            "Read one of Addled's own source files under backend/, to understand "
            "how a feature works before proposing a change to it. Read-only.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Path relative to backend/, e.g. "
                                        "'skills/tool_loop.py'."},
            }, "required": ["path"]},
            self_read, "system",
        ))

        async def self_propose(params: dict) -> dict:
            """Stage a change to Addled's own code. Writes nothing."""
            from backend.codemode import selfmod
            return selfmod.propose(
                str(params.get("path") or ""),
                str(params.get("content") or ""),
                reason=str(params.get("reason") or ""))
        self.register(SkillDefinition(
            "self_propose",
            "Propose a change to Addled's OWN source (a file under backend/). "
            "This does NOT change anything: it stages the change and returns a "
            "diff and a token. Show the user the diff and get their agreement "
            "before calling self_apply. Files that decide what is permitted "
            "(safety/, config.py, main.py, the executor) are refused. Only .py "
            "files under backend/ can be changed; a new module goes through "
            "forge_skill instead.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Path relative to backend/."},
                "content": {"type": "string",
                            "description": "The complete new contents of the "
                                           "file."},
                "reason": {"type": "string",
                           "description": "Why this change is wanted."},
            }, "required": ["path", "content"]},
            self_propose, "system", True,
        ))

        async def self_apply(params: dict) -> dict:
            """Apply a staged self-change. Requires explicit confirmation."""
            from backend.codemode import selfmod
            return selfmod.apply(
                str(params.get("token") or ""),
                confirm=bool(params.get("confirm")),
                allow_dirty=bool(params.get("allow_dirty")))
        self.register(SkillDefinition(
            "self_apply",
            "Apply a staged change to Addled's own code. You MUST have shown "
            "the user the diff and they MUST have agreed, then pass confirm=true. "
            "The change does not take effect until Addled restarts. Undo it with "
            "self_revert, which needs the same token.",
            {"type": "object", "properties": {
                "token": {"type": "string",
                          "description": "The token self_propose returned."},
                "confirm": {"type": "boolean",
                            "description": "Set true only after the user has "
                                           "agreed to this exact diff."},
                "allow_dirty": {"type": "boolean",
                                "description": "Apply even though the file "
                                               "changed since it was staged."},
            }, "required": ["token", "confirm"]},
            self_apply, "system", True,
        ))

        async def self_revert(params: dict) -> dict:
            """Undo a staged or applied self-change by token."""
            from backend.codemode import selfmod
            return selfmod.revert(str(params.get("token") or ""))
        self.register(SkillDefinition(
            "self_revert",
            "Undo a change made to Addled's own code, restoring the original "
            "file. Works whether or not it was applied yet. A restart applies "
            "the restore.",
            {"type": "object", "properties": {
                "token": {"type": "string",
                          "description": "The token from self_propose."},
            }, "required": ["token"]},
            self_revert, "system", True,
        ))

        async def self_pending(params: dict) -> dict:
            from backend.codemode import selfmod
            items = selfmod.list_pending()
            return {"success": True, "pending": items, "count": len(items)}
        self.register(SkillDefinition(
            "self_pending",
            "List the changes to Addled's own code that are staged or were "
            "applied and can still be reverted.",
            {"type": "object", "properties": {}},
            self_pending, "system",
        ))

        async def verify_code(params: dict) -> dict:
            """Run the project's own check, so "done" means something.

            The command is discovered, never invented (see
            backend/codemode/verify.py). `path` defaults to the bound workspace.
            """
            from backend.codemode import verify as verify_mod
            root = str(params.get("path") or "").strip()
            if not root:
                try:
                    from backend.workspace import root as ws_root
                    root = str(ws_root() or "")
                except Exception:  # noqa: BLE001
                    root = ""
            if not root:
                return {"success": False, "ran": False,
                        "error": ("No workspace is bound and no path was given, "
                                  "so there is nothing to verify.")}
            result = await verify_mod.run_verification(
                root, command=str(params.get("command") or ""),
                timeout=int(params.get("timeout") or 300))
            result["success"] = bool(result.get("ok"))
            result["verdict"] = verify_mod.verdict_line(result)
            return result
        self.register(SkillDefinition(
            "verify_code",
            "Run the project's own test/verify command and report whether it "
            "passed, so a change can be called verified rather than merely "
            "finished. The command is taken from the project (a verify/test "
            "script, a Makefile target, or the test runner its layout implies) "
            "and never invented. Use it after making changes. Returns the exit "
            "code, a pass/fail summary and the output.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Project root. Defaults to the "
                                        "bound workspace."},
                "command": {"type": "string",
                            "description": "Override the detected command."},
                "timeout": {"type": "integer",
                            "description": "Seconds before the check is killed.",
                            "default": 300},
            }},
            verify_code, "code",
        ))

        async def code_read(params: dict) -> dict:
            import os
            path = str(params.get("path", ""))
            if not os.path.isfile(path):
                return {"success": False, "error": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            from backend.codemode.lang_detect import detect
            return {"success": True, "content": content,
                    "language": detect(path), "path": path}
        self.register(SkillDefinition(
            "code_read", "Read source code from a file with language detection",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path to source file"},
            }, "required": ["path"]},
            code_read, "code",
        ))

        async def code_edit(params: dict) -> dict:
            import os
            path = str(params.get("path", ""))
            instruction = str(params.get("instruction", ""))
            if not os.path.isfile(path):
                return {"success": False, "error": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()
            # Return current content with instruction — provider handles the actual edit
            return {
                "success": True,
                "path": path,
                "original": original[:5000],
                "instruction": instruction,
                "message": "Review the original code and provide the modified version.",
            }
        self.register(SkillDefinition(
            "code_edit", "Read a file and propose edits. Provider returns modified code.",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "File to edit"},
                "instruction": {"type": "string", "description": "What to change"},
            }, "required": ["path", "instruction"]},
            code_edit, "code",
        ))

    # ── Calendar ─────────────────────────────────────────────────────────

    def _register_calendar_skills(self):
        async def calendar_add(params: dict) -> dict:
            from backend.integrations.calendar_integration import calendar
            ev = calendar.add_event(
                title=str(params.get("title", "Untitled")),
                start=str(params.get("start", "")),
                end=params.get("end"),
                description=str(params.get("description", "")),
                location=str(params.get("location", "")),
            )
            return {"success": True, "event": ev}
        self.register(SkillDefinition(
            "calendar_add", "Add an event to the calendar",
            {"type": "object", "properties": {
                "title": {"type": "string", "description": "Event title"},
                "start": {"type": "string", "description": "Start date/time (YYYY-MM-DD or ISO)"},
                "end": {"type": "string", "description": "End date/time"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            }, "required": ["title", "start"]},
            calendar_add, "integrations",
        ))

        async def calendar_list(params: dict) -> dict:
            from backend.integrations.calendar_integration import calendar
            events = calendar.get_events(
                params.get("start"), params.get("end"))
            return {"success": True, "events": events, "count": len(events)}
        self.register(SkillDefinition(
            "calendar_list", "List calendar events, optionally filtered by date",
            {"type": "object", "properties": {
                "start": {"type": "string", "description": "Start date filter (YYYY-MM-DD)"},
                "end": {"type": "string", "description": "End date filter (YYYY-MM-DD)"},
            }},
            calendar_list, "integrations",
        ))

        async def calendar_delete(params: dict) -> dict:
            from backend.integrations.calendar_integration import calendar
            target = str(params.get("id") or params.get("title", "")).strip().lower()
            if not target:
                return {"success": False, "error": "id or title is required"}
            for ev in calendar.get_events():
                if ev.get("id", "").lower() == target or ev.get("title", "").strip().lower() == target:
                    ok = calendar.delete_event(ev.get("id", ""))
                    return {"success": ok, "message": f"Deleted event '{ev.get('title')}'"}
            return {"success": False, "error": f"no event matching '{target}'"}
        self.register(SkillDefinition(
            "calendar_delete", "Delete a calendar event by ID or title",
            {"type": "object", "properties": {
                "id": {"type": "string", "description": "Event ID to delete"},
                "title": {"type": "string", "description": "Event title to delete"},
            }},
            calendar_delete, "integrations",
        ))

    # ── Email ────────────────────────────────────────────────────────────
    #
    # These existed as WebSocket RPCs and nothing else, so the dashboard could
    # read and send mail while the agent could not — the model had no way to
    # reach any of it. The integration itself was already written; this is what
    # makes it something the agent can decide to use.

    def _register_email_skills(self):
        def _not_configured() -> bool:
            """Whether email has been set up at all.

            Worth the call: "nothing unread" and "no account configured" are
            very different answers, and an agent told "no mail" on an
            unconfigured account reports a false all-clear.
            """
            try:
                from backend.integrations.email_integration import email_client
                cfg = email_client._load_config()
                return not (cfg.get("imap_server") and cfg.get("email"))
            except Exception:  # noqa: BLE001
                return True

        _NO_ACCOUNT = ("Email is not set up. Add the address and the IMAP/SMTP "
                       "server in Settings first.")

        async def email_list(params: dict) -> dict:
            """Read unread mail, newest first."""
            from backend.integrations.email_integration import email_client
            try:
                limit = int(params.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 50))
            if _not_configured():
                return {"success": False, "emails": [], "count": 0,
                        "error": _NO_ACCOUNT}
            mails = email_client.fetch_unread(limit=limit)
            if not mails:
                return {"success": True, "emails": [], "count": 0,
                        "message": "No unread mail."}
            return {"success": True, "emails": mails, "count": len(mails)}
        self.register(SkillDefinition(
            "email_list",
            "Read the unread email in the inbox, newest first. Use this when "
            "the user asks what is in their inbox, whether anything needs a "
            "reply, or wants their mail summarised. Returns sender, subject "
            "and a body preview rather than the whole message — use "
            "email_search to find a specific one.",
            {"type": "object", "properties": {
                "limit": {"type": "integer",
                          "description": "How many to fetch (default 10, max 50)."},
            }},
            email_list, "integrations",
        ))

        async def email_search(params: dict) -> dict:
            """Search the mailbox for a sender, subject or body text."""
            from backend.integrations.email_integration import email_client
            query = str(params.get("query") or "").strip()
            if not query:
                return {"success": False, "emails": [], "count": 0,
                        "error": "What should I search for? Pass it as 'query'."}
            try:
                limit = int(params.get("limit") or 20)
            except (TypeError, ValueError):
                limit = 20
            limit = max(1, min(limit, 50))
            if _not_configured():
                return {"success": False, "emails": [], "count": 0,
                        "error": _NO_ACCOUNT}
            hits = email_client.search(query, limit=limit)
            return {"success": True, "emails": hits, "count": len(hits)}
        self.register(SkillDefinition(
            "email_search",
            "Search the user's mailbox for a sender, subject or body text. Use "
            "this to find a specific message or thread instead of listing the "
            "whole inbox.",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "Text to look for."},
                "limit": {"type": "integer", "description": "Max results."},
            }, "required": ["query"]},
            email_search, "integrations",
        ))

        async def email_send(params: dict) -> dict:
            """Send mail. Gated: it leaves the machine and cannot be recalled."""
            from backend.integrations.email_integration import email_client
            to = str(params.get("to") or "").strip()
            subject = str(params.get("subject") or "").strip()
            body = str(params.get("body") or "")
            if not to:
                return {"success": False, "error": "'to' is required."}
            if "@" not in to:
                return {"success": False,
                        "error": f"'{to}' does not look like an email address."}
            if not subject and not body:
                return {"success": False,
                        "error": "An email needs a subject or a body."}
            if _not_configured():
                return {"success": False, "error": _NO_ACCOUNT}
            result = email_client.send(to=to, subject=subject, body=body,
                                       html=bool(params.get("html")))
            if not result.get("success"):
                return {"success": False,
                        "error": result.get("error") or "sending failed"}
            return {"success": True, "message": f"Sent to {to}."}
        self.register(SkillDefinition(
            "email_send",
            "Send an email. The message is gone once sent and cannot be "
            "recalled, so state the recipient, subject and body in the "
            "conversation before calling this — never send on an assumption "
            "about who the user meant.",
            {"type": "object", "properties": {
                "to": {"type": "string", "description": "Recipient address."},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "html": {"type": "boolean",
                         "description": "Send the body as HTML (default false)."},
            }, "required": ["to"]},
            email_send, "integrations",
            requires_approval=True,
        ))

        async def transcribe_audio(params: dict) -> dict:
            """Transcribe a recording locally, for notes or summaries."""
            path = str(params.get("path") or "").strip()
            if not path:
                return {"success": False,
                        "error": "Which file? Pass its path as 'path'."}
            from backend.voice.stt import transcribe_file
            result = await asyncio.to_thread(transcribe_file, path)
            if not result.get("success"):
                return result
            text = result.get("text") or ""
            if not text:
                return {"success": True, "text": "",
                        "message": ("The file transcribed to nothing — it may "
                                    "contain no speech, or be silent.")}
            return result
        self.register(SkillDefinition(
            "transcribe_audio",
            "Transcribe an audio or video recording of speech into text, "
            "using the local model (nothing is uploaded). Use this for a "
            "meeting or voice-note recording the user points you at, then "
            "summarise it or pull out the action items yourself. It returns "
            "the raw transcript, so the reading is still yours to do.",
            {"type": "object", "properties": {
                "path": {"type": "string",
                         "description": "Path to the audio/video file."},
            }, "required": ["path"]},
            transcribe_audio, "integrations",
        ))

        # ---- meetings ------------------------------------------------------
        #
        # A meeting is a transcript plus what came out of it. The read-only
        # ones are ungated on purpose: listing and reading meetings is exactly
        # like reading the journal, and gating a lookup teaches the user to
        # approve without reading. Nothing here reaches a third party, so none
        # of it belongs in CONTENT_CLASSIFIED.

        async def meeting_save(params: dict) -> dict:
            """Save a transcript as a meeting, transcribing a file if given."""
            from backend.meetings import store

            title = str(params.get("title") or "").strip()
            transcript = str(params.get("transcript") or "")
            segments = params.get("segments") or None
            language = ""
            path = str(params.get("path") or "").strip()

            # Pointing at a file transcribes it first, which is the flow the
            # plan calls the safe complete path: one call, no capture risk.
            if path and not transcript:
                from backend.voice.stt import transcribe_file
                result = await asyncio.to_thread(transcribe_file, path)
                if not result.get("success"):
                    return result
                transcript = result.get("text") or ""
                segments = result.get("segments") or None
                language = result.get("language") or ""
                if not title:
                    from pathlib import Path
                    title = Path(path).stem.replace("_", " ").replace("-", " ").strip()

            if not transcript.strip():
                return {"success": False,
                        "error": ("Nothing to save — pass a 'transcript', or a "
                                  "'path' to a recording to transcribe.")}

            meeting = store.create(title or "Meeting",
                                   source="file" if path else "manual")
            meeting = store.set_transcript(meeting["id"], transcript,
                                           segments=segments, language=language)
            return {
                "success": True,
                "id": meeting["id"],
                "title": meeting["title"],
                "segments": len(meeting.get("segments") or []),
                "chars": len(meeting.get("transcript") or ""),
                "message": (f"Saved as '{meeting['title']}'. Summarise it with "
                            "meeting_summarise."),
            }
        self.register(SkillDefinition(
            "meeting_save",
            "Save a meeting: either a transcript you already have, or the path "
            "to a recording to transcribe first. Use this when the user points "
            "you at a meeting recording or hands you a transcript to keep. "
            "Returns the meeting id, which the other meeting tools take. After "
            "saving, offer to summarise it.",
            {"type": "object", "properties": {
                "title": {"type": "string", "description": "What the meeting is called."},
                "transcript": {"type": "string",
                               "description": "The transcript text, if you have it."},
                "path": {"type": "string",
                         "description": "Recording to transcribe, if you do not."},
                "segments": {"type": "array",
                             "description": "Timestamped segments, if you have them."},
            }},
            meeting_save, "integrations",
        ))

        async def meeting_list(params: dict) -> dict:
            """List saved meetings, newest first."""
            from backend.meetings import store
            limit = int(params.get("limit") or 20)
            meetings = store.list_meetings(limit=limit)
            if not meetings:
                return {"success": True, "meetings": [], "count": 0,
                        "message": "No meetings saved yet."}
            return {
                "success": True,
                "count": len(meetings),
                "meetings": [{
                    "id": m["id"],
                    "title": m.get("title", ""),
                    "started_at": m.get("started_at"),
                    "minutes": round(((m.get("ended_at") or 0)
                                      - (m.get("started_at") or 0)) / 60, 1)
                    if m.get("ended_at") else None,
                    "segment_count": m.get("segment_count", 0),
                    "summarised": bool(m.get("summary")),
                    "summary": (m.get("summary") or "")[:200],
                    "actions": len(m.get("actions") or []),
                } for m in meetings],
            }
        self.register(SkillDefinition(
            "meeting_list",
            "List saved meetings, newest first, with whether each has been "
            "summarised. Use this when the user asks about their meetings or "
            "wants to find one by name.",
            {"type": "object", "properties": {
                "limit": {"type": "integer", "description": "How many to list (default 20)."},
            }},
            meeting_list, "integrations",
        ))

        async def meeting_get(params: dict) -> dict:
            """Read one meeting: its transcript, summary, actions and decisions."""
            from backend.meetings import store
            meeting_id = str(params.get("id") or "").strip()
            if not meeting_id:
                return {"success": False, "error": "Which meeting? Pass its 'id'."}
            meeting = store.get(meeting_id)
            if meeting is None:
                return {"success": False,
                        "error": f"No meeting with id '{meeting_id}'."}
            # The full transcript is large. A caller asking for the reading
            # wants the summary; the transcript is there when asked for.
            full = bool(params.get("include_transcript"))
            out = {
                "success": True,
                "id": meeting["id"],
                "title": meeting.get("title", ""),
                "summary": meeting.get("summary", ""),
                "decisions": meeting.get("decisions") or [],
                "actions": meeting.get("actions") or [],
                "open_questions": meeting.get("open_questions") or [],
                "segments": meeting.get("segments") or [],
                "chars": len(meeting.get("transcript") or ""),
                "summarised": bool(meeting.get("summary")),
            }
            if full:
                out["transcript"] = meeting.get("transcript", "")
            else:
                out["note"] = ("Transcript omitted to keep this small — ask "
                               "again with include_transcript true to read it.")
            return out
        self.register(SkillDefinition(
            "meeting_get",
            "Read a saved meeting: its summary, decisions, action items and "
            "open questions, plus the timestamps. The transcript is left out "
            "unless include_transcript is true, because it is long. Pass "
            "include_transcript when the user asks what was actually said.",
            {"type": "object", "properties": {
                "id": {"type": "string", "description": "The meeting id."},
                "include_transcript": {"type": "boolean",
                                       "description": "Include the full transcript (default false)."},
            }, "required": ["id"]},
            meeting_get, "integrations",
        ))

        async def meeting_summarise(params: dict) -> dict:
            """Summarise a saved meeting into decisions, actions and questions."""
            from backend.meetings import store
            from backend.meetings import summarise as summariser
            meeting_id = str(params.get("id") or "").strip()
            if not meeting_id:
                return {"success": False, "error": "Which meeting? Pass its 'id'."}
            meeting = store.get(meeting_id)
            if meeting is None:
                return {"success": False,
                        "error": f"No meeting with id '{meeting_id}'."}
            transcript = meeting.get("transcript") or ""
            if not transcript.strip():
                return {"success": False,
                        "error": "That meeting has no transcript to summarise."}

            result = await summariser.summarise(
                transcript, segments=meeting.get("segments") or None)
            if not result.get("success"):
                return {"success": False, "error": result.get("error"),
                        "hint": ("The meeting is saved and unchanged — "
                                 "summarising can be retried.")}

            stored = store.set_summary(
                meeting_id, result["summary"],
                decisions=result["decisions"],
                actions=result["actions"],
                open_questions=result["open_questions"])
            # Make it answerable later: the summary goes into semantic recall
            # so a future "what did we decide about X?" reaches it. Failing
            # to index must not fail the summary the user waited for.
            try:
                store.index(meeting_id)
            except Exception as e:  # noqa: BLE001
                log.debug("could not index meeting %s: %s", meeting_id, e)
            out = {
                "success": True,
                "id": meeting_id,
                "title": stored.get("title", ""),
                "summary": result["summary"],
                "decisions": result["decisions"],
                "actions": result["actions"],
                "open_questions": result["open_questions"],
                "minutes": round(((stored.get("ended_at") or 0)
                                  - (stored.get("started_at") or 0)) / 60, 1)
                if stored.get("ended_at") else None,
            }
            # Partial is surfaced, never smoothed over: a summary of 3 of 8
            # parts must not read as the whole meeting.
            if result.get("partial"):
                out["partial"] = True
                out["warning"] = (
                    f"Only {result['parts_ok']} of {result['blocks']} parts of "
                    "the transcript could be summarised, so the summary is "
                    "incomplete. Say so when you present it.")
            return out
        self.register(SkillDefinition(
            "meeting_summarise",
            "Summarise a saved meeting into a short summary, the decisions "
            "made, the action items (with owners where the transcript names "
            "them) and anything left open. Long meetings are summarised in "
            "parts and combined. Use this after meeting_save, or when the user "
            "asks what came out of a meeting. If it reports 'partial', tell the "
            "user the summary is incomplete rather than presenting it as whole.",
            {"type": "object", "properties": {
                "id": {"type": "string", "description": "The meeting id."},
            }, "required": ["id"]},
            meeting_summarise, "integrations",
        ))

        async def meeting_actions(params: dict) -> dict:
            """Propose a meeting's action items as tasks, or create chosen ones.

            Two modes, and the split is the whole point. Called without
            `accept`, it PROPOSES: it lists the action items and writes nothing.
            Only a second call that names the ones the user agreed to creates
            tasks. `who` is frequently a third party ("Tom to send the migration
            plan"), and silently filing that on the user's own list is putting
            someone else's job in their scheduler.
            """
            from backend.meetings import store
            from backend.tasks.recurrence import next_run
            from backend.tasks.store import ScheduledTask, task_store

            meeting_id = str(params.get("id") or "").strip()
            if not meeting_id:
                return {"success": False, "error": "Which meeting? Pass its 'id'."}
            meeting = store.get(meeting_id)
            if meeting is None:
                return {"success": False,
                        "error": f"No meeting with id '{meeting_id}'."}
            actions = [a for a in (meeting.get("actions") or []) if a]
            if not actions:
                return {"success": False,
                        "error": ("That meeting has no action items recorded. "
                                  "Summarise it first with meeting_summarise."),
                        "hint": "meeting_summarise extracts the action items."}

            accept = params.get("accept")

            # ---- propose (default) ------------------------------------------
            if not accept:
                return {
                    "success": True,
                    "mode": "proposal",
                    "id": meeting_id,
                    "title": meeting.get("title", ""),
                    "actions": [
                        {"index": i,
                         "what": str(a.get("what") or a.get("action") or
                                     a.get("task") or "").strip(),
                         "who": str(a.get("who") or "").strip()}
                        for i, a in enumerate(actions)],
                    "note": ("These are proposals, nothing was scheduled. Ask "
                             "the user which to add, then call meeting_actions "
                             "again with accept=[the indexes]. Items owned by "
                             "someone other than the user should be left to "
                             "them rather than added to the user's list."),
                }

            # ---- confirm: create only what was named -------------------------
            if isinstance(accept, bool):
                # accept=true is ambiguous about WHICH items; refusing is
                # better than guessing and creating all of them.
                return {"success": False,
                        "error": ("Pass accept as the list of indexes to add, "
                                  "e.g. accept=[0, 2]. A bare true is ambiguous "
                                  "and nothing was scheduled.")}
            if not isinstance(accept, list):
                return {"success": False,
                        "error": "accept must be a list of action indexes."}
            wanted: list[int] = []
            for v in accept:
                try:
                    wanted.append(int(v))
                except (TypeError, ValueError):
                    return {"success": False,
                            "error": f"accept contains a non-number: {v!r}"}

            from datetime import datetime, timedelta
            title = meeting.get("title") or "meeting"
            created, failed, skipped = [], [], []
            for idx in wanted:
                if idx < 0 or idx >= len(actions):
                    skipped.append({"index": idx,
                                    "reason": "no such action item"})
                    continue
                item = actions[idx]
                what = str(item.get("what") or item.get("action") or
                           item.get("task") or "").strip()
                if not what:
                    skipped.append({"index": idx, "reason": "empty action text"})
                    continue
                who = str(item.get("who") or "").strip()
                # The owner is kept in the title, not silently dropped: a task
                # reading "send the migration plan" is a different thing from
                # "Tom: send the migration plan".
                task_title = f"{who}: {what}" if who else what
                now = datetime.now()
                task = ScheduledTask(
                    id="", title=task_title, kind="task", action="notify",
                    payload=task_title,
                    time="09:00",
                    date=now.strftime("%Y-%m-%d"),
                    recurrence={"type": "none", "weekdays": []},
                    source="llm",
                )
                if task.time <= now.strftime("%H:%M"):
                    task.date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                task.next_run = next_run(task)
                added, err = task_store.add(task)
                if added is None:
                    failed.append({"index": idx, "what": what, "error": err})
                else:
                    created.append({"index": idx, "task_id": added.id,
                                    "title": added.title, "who": who})
            return {
                "success": bool(created),
                "mode": "created",
                "id": meeting_id,
                "created": created,
                "failed": failed,
                "skipped": skipped,
                "count": len(created),
                "note": ("Each created task is a 09:00 reminder for the day "
                         "after it was added — the meeting gave no date, so "
                         "the user should set a real one if they need it."),
            }
        self.register(SkillDefinition(
            "meeting_actions",
            "A meeting's action items as tasks. Omit accept to PROPOSE only "
            "(schedules nothing); never create without the user's say-so.",
            {"type": "object", "properties": {
                "id": {"type": "string", "description": "The meeting id."},
                "accept": {
                    "type": "array", "items": {"type": "integer"},
                    "description": ("Indexes of the proposed action items to "
                                    "create as tasks. Omit to get proposals.")},
            }, "required": ["id"]},
            meeting_actions, "integrations",
        ))

        # ---- messaging a person through a bot bridge -----------------------
        #
        # Gated, and deliberately. Every other bot action replies to someone who
        # already spoke to us; this one puts a message in front of a THIRD PARTY
        # who never asked for it and cannot un-read it. That is the same class of
        # act as `email_send`, which asks for the same reason.
        async def send_message(params: dict) -> dict:
            """Send a chat message through a running bot bridge.

            The bot owns the platform's connection — the socket lives in its own
            process — so this asks it over the WebSocket and waits for the
            answer. A failure names which part failed, because from the chat page
            "nothing happened" is the hardest thing to act on.
            """
            from backend.bots import manager as bots

            platform = str(params.get("platform") or "whatsapp").strip().lower()
            to = str(params.get("to") or "").strip()
            text = str(params.get("text") or "").strip()

            if not text:
                return {"success": False,
                        "error": ("What should it say? Pass the words as "
                                  "'text'.")}
            if not to:
                return {"success": False,
                        "error": ("Who should it go to? Pass a phone number in "
                                  "international form, e.g. 6281234567890, or a "
                                  "chat id ending in @g.us for a group.")}
            return await bots.send(platform, to, text)

        self.register(SkillDefinition(
            "send_message",
            "Send a chat message to someone through a running bot bridge "
            "(WhatsApp, Telegram or Discord). Use the phone number the user "
            "gives you, in international form without a leading plus — "
            "'6281234567890'. The message is delivered and cannot be recalled, "
            "so say who you are about to message and what you will say before "
            "calling this, and never guess a number the user did not give you. "
            "The bot must be running; check with bot_status if unsure.",
            {"type": "object", "properties": {
                "to": {"type": "string",
                       "description": ("Recipient: a phone number in "
                                       "international form (6281234567890) or a "
                                       "group id ending in @g.us.")},
                "text": {"type": "string",
                         "description": "The message to send."},
                "platform": {"type": "string",
                             "description": ("Which bridge: whatsapp, telegram "
                                             "or discord. Defaults to whatsapp."),
                             "default": "whatsapp"},
            }, "required": ["to", "text"]},
            send_message, "integrations",
            requires_approval=True,
            aliases={"to": ("recipient", "number", "phone", "chat_id", "jid"),
                     "text": ("message", "body", "content", "msg")},
        ))

        async def bot_status(params: dict) -> dict:
            """Are the bot bridges running, and where would a message go?

            Reading, not sending: no approval. It exists because every failure
            of `send_message` is easier to act on with this in hand, and because
            a model that cannot check will otherwise guess.
            """
            from backend.bots import manager as bots

            status = bots.status()
            out = {}
            for name, info in (status.get("platforms") or {}).items():
                out[name] = {
                    "running": bool(info.get("running")),
                    "ready": bool(info.get("ready")),
                    "blockers": info.get("blockers") or [],
                    "needs_qr": bool(info.get("qr")),
                }
            return {"success": True, "platforms": out,
                    "running": [n for n, v in out.items() if v["running"]],
                    "node": status.get("node") or ""}

        self.register(SkillDefinition(
            "bot_status",
            "Check which chat bots are running and whether they are ready "
            "(WhatsApp, Telegram, Discord). Read-only. Use this before "
            "send_message, and to explain to the user which bot to start or "
            "which still needs its QR code scanned.",
            {"type": "object", "properties": {}},
            bot_status, "integrations",
        ))

        async def chat_history(params: dict) -> dict:
            """Recent turns of a bot conversation.

            The backend records what a bridge sent and received, so this reads
            the record rather than asking the platform: WhatsApp does not serve
            history for a linked device without an explicit sync store, which
            this bridge does not configure.
            """
            from backend.memory.bot_history import recent

            platform = str(params.get("platform") or "").strip().lower()
            conversation = str(params.get("conversation") or "").strip()
            limit = params.get("limit", 20)
            try:
                limit = max(1, min(int(limit), 100))
            except (TypeError, ValueError):
                limit = 20
            turns = recent(platform=platform or None,
                           conversation=conversation or None, limit=limit)
            if not turns:
                where = f" for {conversation}" if conversation else ""
                return {"success": True, "turns": [], "count": 0,
                        "message": (f"Nothing has been exchanged{where} yet. "
                                    f"History starts from when a bot first "
                                    f"spoke with someone.")}
            return {"success": True, "turns": turns, "count": len(turns)}

        self.register(SkillDefinition(
            "chat_history",
            "Read the recent messages exchanged through a chat bot bridge — "
            "what a contact asked and what Addled replied. Use it to answer "
            "'what did they say', to follow up on a conversation, or to check "
            "whether a message was actually sent. Optionally narrow it with a "
            "platform (whatsapp/telegram/discord) or a conversation id.",
            {"type": "object", "properties": {
                "platform": {"type": "string",
                             "description": "whatsapp, telegram or discord."},
                "conversation": {"type": "string",
                                 "description": ("A specific chat: the phone "
                                                 "number or chat id.")},
                "limit": {"type": "integer",
                          "description": "How many recent turns (1-100).",
                          "default": 20},
            }, "required": []},
            chat_history, "integrations",
        ))

    # ── Web Search ───────────────────────────────────────────────────────

    def _register_web_skills(self):
        async def web_search(params: dict) -> dict:
            query = str(params.get("query", "")).strip()
            if not query:
                return {"success": False, "error": "No search query"}

            # Ordered by what still works. These are real feeds and APIs
            # rather than scraped HTML, so there is no bot wall in the way and
            # no silently irrelevant result set coming back.
            problems: list[str] = []
            for name, fn in (("news+reference", _search_news_and_reference),
                             ("bing-news", _search_bing_news)):
                if not _engine_ready(name):
                    problems.append(f"{name}: skipped after repeated failures")
                    continue
                result = await fn(query)
                ok = bool(result.get("success"))
                _record_engine(name, ok)
                if ok:
                    return result
                problems.append(f"{name}: {result.get('error')}")

            # Scraped engines last, and only when the results can show they
            # answer the question - see _looks_relevant.
            for name, fn in (("bing", _search_bing),
                             ("ddg-html", _search_ddg_html)):
                if not _engine_ready(name):
                    problems.append(f"{name}: skipped after repeated failures")
                    continue
                result = await fn(query)
                if not result.get("success"):
                    _record_engine(name, False)
                    problems.append(f"{name}: {result.get('error')}")
                    continue
                if not _looks_relevant(query, result):
                    _record_engine(name, False)
                    problems.append(f"{name}: results did not match the query")
                    continue
                _record_engine(name, True)
                return result

            # Last resort: drive a real browser. Slow, but nothing blocks it -
            # and it is still Bing underneath, so its output needs the same
            # relevance check as the scraped path or the model is handed the
            # same irrelevant page it was just protected from.
            try:
                from urllib.parse import quote
                from backend.browser.browser_engine import browser
                nav = await browser.navigate(
                    f"https://www.bing.com/search?q={quote(query)}")
                if nav.get("success"):
                    extract = await browser.extract()
                    text = (extract or {}).get("text", "") if isinstance(extract, dict) else str(extract or "")
                    if text:
                        candidate = {"success": True, "query": query,
                                     "engine": "browser", "url": nav.get("url"),
                                     "snippet": text[:2000]}
                        if _looks_relevant(query, candidate):
                            return candidate
                        problems.append(
                            "browser: results did not match the query")
            except Exception as e:
                log.debug("Browser search fallback failed: %s", e)

            # Say why, rather than leaving the model to tell the user it has
            # no internet access.
            return {"success": False,
                    "error": ("no search source returned usable results ("
                              + "; ".join(problems[:4]) + ")")}

        self.register(SkillDefinition(
            "web_search",
            "Search news sites and Wikipedia. Use this for anything current, "
            "local, or outside your training data - including what people in "
            "a place are talking about. Results carry titles, sources, dates "
            "and URLs. If a query comes back empty, retry with different "
            "keywords instead of telling the user you cannot find out.",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
            }, "required": ["query"]},
            web_search, "web",
        ))

        async def web_fetch(params: dict) -> dict:
            """Fetch a URL. If the site blocks automated access, fall back
            to searching for that page's content instead."""
            url = str(params.get("url", "")).strip()
            if not url:
                return {"success": False, "error": "No URL provided"}
            # 1) Direct fetch (browser or HTTP fallback)
            try:
                from backend.browser.browser_engine import browser
                nav = await browser.navigate(url)
                if nav.get("success"):
                    ex = await browser.extract()
                    text = (ex or {}).get("text", "") if isinstance(ex, dict) else ""
                    if text:
                        return {"success": True, "url": url,
                                "title": nav.get("title", ""),
                                "via": "direct", "text": text[:6000]}
            except Exception as e:
                log.debug("web_fetch direct fetch failed: %s", e)
            # 2) Search fallback: find the page's content via search results.
            #
            # Gated exactly like `web_search`, because it IS `web_search`'s
            # engine: without `_engine_ready` a cooling-down engine was still
            # hit here, without `_looks_relevant` the model was handed the
            # off-topic page set that check exists to reject (Bing's HTML
            # endpoint answers a different question often enough to matter),
            # and without `_record_engine` this path could never contribute to
            # the failure count that drives the cooldown — so the counter was
            # half-fed and a blocked page kept paying the 12s timeout.
            try:
                from urllib.parse import urlparse, unquote
                p = urlparse(url)
                domain = (p.netloc or "").replace("www.", "")
                slug = unquote(p.path).strip("/").split("/")[-1]
                slug = slug.replace("-", " ").replace("_", " ")
                query = f"{domain} {slug}".strip()

                if not _engine_ready("bing"):
                    return {"success": False,
                            "error": ("The site blocked automated access, and "
                                      "the search fallback is cooling down "
                                      "after repeated failures. Try again in a "
                                      "few minutes.")}
                res = await _search_bing(query)
                if not res.get("success"):
                    res = await _search_bing(f"{domain} {slug} latest")
                if res.get("success") and not _looks_relevant(query, res):
                    _record_engine("bing", False)
                    res = {"success": False,
                           "error": "results did not match the page"}
                else:
                    _record_engine("bing", bool(res.get("success")))
                if res.get("success"):
                    return {"success": True, "url": url, "via": "search-fallback",
                            "snippet": res.get("snippet"),
                            "results": res.get("results"),
                            "note": ("Direct fetch was blocked; these are search "
                                     "results about the page.")}
            except Exception as e:
                log.debug("web_fetch search fallback failed: %s", e)
            return {"success": False,
                    "error": "Direct fetch blocked and search fallback failed"}

        self.register(SkillDefinition(
            "web_fetch",
            "Fetch a web page's text content. If the site blocks automated "
            "access, it automatically falls back to search results about "
            "the page (e.g. from a wiki or aggregator).",
            {"type": "object", "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
            }, "required": ["url"]},
            web_fetch, "web",
        ))

    # ── Meta: Self-extending skills ──────────────────────────────────────

    def _register_meta_skills(self):
        async def forge_skill(params: dict) -> dict:
            """Learn a new capability on demand, after asking the user.

            The consent check belongs HERE, not only in
            `tool_loop._execute_skill_inner`. That path fires on an unknown tool
            NAME, which is the rarer route; a model asked to forge calls this
            registered skill directly, and it did: the log showed
            "Forged new skill: convert_a_colour_name__e_g___c (package:
            webcolors)" from a turn that was never asked. A gate on the path the
            model does not take is not a gate.
            """
            from backend.skills.forge import skill_forge
            from backend.providers.registry import get_provider

            task = str(params.get("task", params.get("description", "")))
            if not task:
                return {"success": False, "error": "No task description provided"}

            from backend.skills import tool_loop as _tl
            if not _tl._consented(None):
                asked = await _tl._ask_to_acquire(
                    "write and test a new skill",
                    f"a tool for: {task[:120]}",
                    "It searches the web for a library, generates the code, and "
                    "runs it to check that it works.",
                    ["Write it", "Skip this"])
                return {
                    "success": False,
                    "requires_answer": True,
                    "question_id": asked.get("question_id"),
                    "error": asked.get("error") or
                             ("This needs your permission first: forging writes "
                              "new code and runs it, and can install packages. "
                              "Ask the user and wait for their answer before "
                              "generating anything."),
                }

            provider = None
            try:
                provider = get_provider()
            except Exception:
                pass

            result = await skill_forge.forge(
                task_description=task,
                provider=provider,
                auto_validate=params.get("validate", True),
            )
            return {
                "success": result.success,
                "skill_name": result.skill_name,
                "action": result.action,
                "detail": result.detail,
            }

        self.register(SkillDefinition(
            "forge_skill",
            "Learn a new capability by searching the web and generating code. "
            "Use when you need to do something that no existing tool can do.\n\n"
            "Forging writes new code and runs it, so this asks the user first "
            "and their answer arrives as the next message. Call it, then wait — "
            "do not claim a skill was created until you are told it was.",
            {"type": "object", "properties": {
                "task": {"type": "string",
                         "description": "What you need to accomplish (e.g., 'convert a PDF to text')"},
                "validate": {"type": "boolean",
                             "description": "Test the new skill after creation", "default": True},
            }, "required": ["task"]},
            forge_skill, "meta",
        ))

        async def list_forged(params: dict) -> dict:
            """List all dynamically learned skills."""
            from backend.skills.forge import skill_forge
            forged = skill_forge.list_forged()
            return {"success": True, "skills": forged, "count": len(forged)}

        self.register(SkillDefinition(
            "list_forged",
            "List all skills that have been dynamically learned/forged",
            {"type": "object", "properties": {}},
            list_forged, "meta",
        ))

        # ---------------------------------------------------------- skill_view
        async def skill_view(params: dict) -> dict:
            """Read a skill's own instructions.

            The catalogue the model reads carries one line per skill, which is
            the right size for DECIDING whether a skill is relevant and far too
            small to tell it HOW to use one. A market skill documenting a
            five-step procedure had its procedure stored and unreachable: the
            only way to see it was to call the skill, which you do not do until
            you already know you want it.

            So the body is fetched on demand, by name - the same progressive
            disclosure the catalogue has always used for the skills themselves.
            """
            name = str(params.get("name") or params.get("skill") or "").strip()
            if not name:
                return {"success": False, "error": "No skill name given"}
            skill = skill_registry.get(name)
            if skill is None:
                # Never a dead end: the closest names are what the model needs
                # to correct itself, and it is already reading the catalogue.
                # Matched in BOTH directions - the wanted name inside a known
                # one, and a known one inside the wanted name. One direction
                # only catches a shortened name ("probe" for "probe_skill") and
                # misses the commoner slip, a name with a word appended
                # ("probe_skill_bodies_nope" for "probe_skill_bodies"), because
                # then the known name is the shorter string.
                low = name.lower()
                pool = [s.name for s in skill_registry.enabled_list_all()]
                near = [n for n in pool if low in n.lower()][:8]
                if not near:
                    near = [n for n in pool if n.lower() in low][:8]
                if not near:
                    # Last resort: share a word, so a miss in the middle of a
                    # name still lands on something useful.
                    words = set(low.replace("-", "_").split("_")) - {""}
                    near = [n for n in pool
                            if words & set(n.lower().replace("-", "_").split("_"))
                            ][:8]
                return {"success": False,
                        "error": f"No skill named {name!r}",
                        "close_matches": near}
            body = (getattr(skill, "body", "") or "").strip()
            if not body:
                # Say so plainly. An empty answer reads as "this skill has no
                # instructions", which is true; inventing a summary would not be.
                return {"success": True, "name": skill.name,
                        "description": skill.description,
                        "body": "",
                        "note": ("This skill has no written instructions. Its "
                                 "one-line description is all there is; call "
                                 "it if it looks relevant.")}
            return {"success": True, "name": skill.name,
                    "description": skill.description, "body": body,
                    "truncated": len(body) > _SKILL_BODY_CHARS,
                    }

        self.register(SkillDefinition(
            "skill_view",
            "Read a skill's full instructions before using it. The tool list "
            "shows one line per skill; this returns what that skill actually "
            "says to do. Use it when a skill's description sounds relevant but "
            "you need to know the steps, the order, or the caveats.",
            {"type": "object", "properties": {
                "name": {"type": "string",
                         "description": "The skill name, exactly as listed"}},
             "required": ["name"]},
            skill_view, "meta",
        ))

        # -------------------------------------------------------- skill_search
        async def skill_search(params: dict) -> dict:
            """Find skills by what they do, when the name is not known.

            The catalogue is already in the prompt, so this earns its place only
            for the case the catalogue cannot serve: a task described in the
            user's words, where the right skill exists but under a name the
            model would not have guessed. Returns descriptions, not bodies -
            this is a finder, and `skill_view` is the reader.
            """
            query = str(params.get("query") or params.get("q") or "").strip()
            if not query:
                return {"success": False, "error": "No search query given"}
            words = [w for w in query.lower().replace("_", " ").split()
                     if len(w) > 2]
            hits = []
            for skill in skill_registry.enabled_list_all():
                hay = f"{skill.name} {skill.description}".lower()
                score = sum(1 for w in words if w in hay)
                if score:
                    hits.append((score, skill))
            hits.sort(key=lambda pair: (-pair[0], pair[1].name))
            return {"success": True, "query": query,
                    "count": len(hits),
                    "skills": [{"name": s.name, "description": s.description,
                                "category": s.category,
                                "has_instructions": bool(
                                    getattr(s, "body", ""))}
                               for _, s in hits[:12]]}

        self.register(SkillDefinition(
            "skill_search",
            "Search the available skills by what they do, when you are unsure "
            "of a skill's name. Returns matching names and one-line "
            "descriptions; use skill_view to read one in full.",
            {"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "What you want to do, in plain words"}},
             "required": ["query"]},
            skill_search, "meta",
        ))

        async def find_mcp_server(params: dict) -> dict:
            """Find an MCP server that can do this, and start it if it can."""
            from backend.config import config
            from backend.mcp_client import market

            if not config.get("mcp", "enabled", default=True):
                return {"success": False,
                        "error": "MCP support is switched off in Settings"}
            if not config.get("mcp", "market_enabled", default=True):
                return {"success": False,
                        "error": "the MCP market is switched off in Settings"}

            task = str(params.get("task") or params.get("description")
                       or "").strip()
            if not task:
                return {"success": False,
                        "error": "No task description provided"}

            wants_install = bool(params.get("install", True))
            if not wants_install or not config.get("mcp", "auto_acquire",
                                                   default=True):
                candidates = await market.suggest(task, limit=6)
                return {"success": bool(candidates),
                        "candidates": candidates,
                        "note": ("Nothing was installed. Add one from "
                                 "Settings, MCP if it looks right.")}

            # Ask before adding a server. An MCP server is a third-party program
            # that runs as a local child process — the most consequential thing
            # this tool can do, and it used to happen silently as a side effect
            # of answering a question.
            #
            # The suggest pass runs first so the question can name what would be
            # added rather than asking about something the model has not seen.
            candidates = await market.suggest(task, limit=6)
            usable = [c for c in candidates if c.get("runnable")]
            if not usable:
                names = "; ".join(
                    f"{c.get('name')} ({c.get('blocked_reason')})"
                    for c in candidates[:3])
                return {"success": False,
                        "error": ("nothing usable without more setup: " + names)
                                 if candidates else "no MCP server matched that task",
                        "candidates": candidates}
            best = usable[0]
            from backend.skills import tool_loop as _tl
            if not _tl._consented(None):
                asked = await _tl._ask_to_acquire(
                    "add an MCP server",
                    str(best.get("title") or best.get("name") or "a server"),
                    "It is a third-party program that will run locally and "
                    "offer new tools to Addled.",
                    ["Add it", "Skip this"])
                return {"success": False,
                        "data": {"requires_answer": True,
                                 "candidate": best,
                                 **{k: v for k, v in asked.items()
                                    if k == "question_id"}},
                        "error": asked.get("error") or
                                 ("This needs your permission first: an MCP "
                                  "server is a third-party program that runs on "
                                  "the user's machine. Ask them and wait for "
                                  "their answer before adding anything."),
                        "candidates": [c.get("name") for c in candidates[:5]]}

            result = await market.install(str(best.get("name") or ""), auto=True)
            if not result.get("success"):
                return {"success": False,
                        "error": result.get("error"),
                        "candidate": result.get("candidate"),
                        "candidates": result.get("candidates")}

            server_id = str(result.get("server_id") or "")
            tools: list[str] = []
            try:
                from backend.mcp_client.manager import mcp_manager
                status = mcp_manager.server_status(server_id)
                tools = [str(t.get("name")) for t in (status.get("tools") or [])]
            except Exception as e:
                log.debug("could not list new MCP tools: %s", e)

            return {
                "success": True,
                "server": server_id,
                "server_name": ((result.get("candidate") or {}).get("title")
                                or server_id),
                "tools": tools[:40],
                "note": ("It is connected. Its tools are named "
                         "mcp__<server>__<tool> - call one to do the task. "
                         "It is switched off again once it has been idle."),
            }

        self.register(SkillDefinition(
            "find_mcp_server",
            "Find and start an MCP server for a capability no current tool "
            "covers, then use its tools. Searches the official MCP registry.\n\n"
            "Adding a server changes what runs on the user's machine, so this "
            "asks them first and their answer arrives as the next message. Call "
            "it, then wait — do not claim anything was added until you are told.",
            {"type": "object", "properties": {
                "task": {"type": "string",
                         "description": "What needs doing, in a few words"},
                "install": {"type": "boolean",
                            "description": "Start it if a usable one is found",
                            "default": True},
            }, "required": ["task"]},
            find_mcp_server, "meta",
        ))

        async def memory_get(params: dict) -> dict:
            """Read the durable facts saved about the user."""
            from backend.memory.facts import get_facts
            facts = get_facts(50)
            return {"success": True, "facts": [f["text"] for f in facts]}

        self.register(SkillDefinition(
            "memory_get",
            "Read durable facts you have saved about the user (preferences, "
            "decisions, context). Use before answering personal questions.",
            {"type": "object", "properties": {}},
            memory_get, "memory",
        ))

        async def memory_set(params: dict) -> dict:
            """Save a durable fact about the user."""
            from backend.memory.facts import add_fact
            fact = add_fact(str(params.get("fact", "")).strip(),
                            source="agent")
            if fact is None:
                return {"success": False, "error": "Empty or duplicate fact"}
            return {"success": True, "fact": fact["text"]}

        self.register(SkillDefinition(
            "memory_set",
            "Save a durable fact about the user so you remember it in future "
            "sessions (e.g. 'User prefers PowerShell over CMD').",
            {"type": "object", "properties": {
                "fact": {"type": "string",
                         "description": "One short fact about the user"}},
             "required": ["fact"]},
            memory_set, "memory",
        ))

        # ── Scheduled tasks ────────────────────────────────────────────────

        async def task_schedule(params: dict) -> dict:
            """Schedule a task or reminder for later execution."""
            from backend.tasks.recurrence import next_run
            from backend.tasks.store import ScheduledTask, task_store
            from backend.config import config

            title = str(params.get("title", "")).strip()
            if not title:
                return {"success": False,
                        "error": "title is required"}
            action = str(params.get("action", "notify"))
            allowed = config.get("scheduling", "llm_actions",
                                 default=["notify", "chat"])
            if action not in allowed or action not in ("notify", "chat"):
                return {"success": False,
                        "error": f"action '{action}' not schedulable"}
            recurrence = params.get("recurrence") or {"type": "none"}
            recurrence.setdefault("weekdays", [])
            if recurrence.get("type") not in ("none", "daily", "weekly",
                                              "monthly"):
                recurrence = {"type": "none", "weekdays": []}
            task = ScheduledTask(
                id="", title=title,
                kind=params.get("kind", "task"),
                action=action,
                payload=str(params.get("payload", title)),
                time=params.get("time", "09:00"),
                date=params.get("date", ""),
                recurrence=recurrence,
                source="llm",
            )
            if not task.date and recurrence["type"] == "none":
                # no date given: assume today (or tomorrow if time passed)
                from datetime import datetime, timedelta
                now = datetime.now()
                day = now.strftime("%Y-%m-%d")
                if task.time <= now.strftime("%H:%M"):
                    day = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                task.date = day
            task.next_run = next_run(task)
            added, err = task_store.add(task)
            if added is None:
                return {"success": False, "error": err}
            from backend.tasks.recurrence import humanize
            return {"success": True, "task_id": added.id,
                    "message": f"Scheduled '{added.title}' — "
                               f"{humanize(added)}"}

        self.register(SkillDefinition(
            "task_schedule",
            "Schedule a task or reminder to run later. Use when the user "
            "says 'remind me to X at 3pm', 'every friday at 9am do Y', or "
            "asks you to schedule something for the future. time is HH:MM; "
            "date is optional YYYY-MM-DD; recurrence: none|daily|weekly|"
            "monthly with weekdays 0-6 (0=Monday).",
            {"type": "object", "properties": {
                "title": {"type": "string",
                          "description": "Short task/reminder title"},
                "time": {"type": "string",
                         "description": "HH:MM local time, default 09:00"},
                "date": {"type": "string",
                         "description": "YYYY-MM-DD for one-shot tasks"},
                "action": {"type": "string",
                           "description": "notify (reminder) or chat "
                                          "(run a prompt later)"},
                "payload": {"type": "string",
                            "description": "Reminder text or chat prompt"},
                "recurrence": {"type": "object",
                               "description": "{type, weekdays}"},
            }, "required": ["title"]},
            task_schedule, "meta",
        ))

        async def task_list(params: dict) -> dict:
            from backend.tasks.scheduler import scheduler
            tasks = scheduler.status()["tasks"]
            lines = [f"{t['title']} — {t['rule']}"
                     + ("" if t["enabled"] else " (paused)")
                     for t in tasks]
            return {"success": True, "tasks": lines,
                    "count": len(lines)}

        self.register(SkillDefinition(
            "task_list",
            "List currently scheduled tasks and reminders.",
            {"type": "object", "properties": {}},
            task_list, "meta",
        ))

        async def task_cancel(params: dict) -> dict:
            from backend.tasks.store import task_store
            target = str(params.get("title", "")).strip().lower()
            if not target:
                return {"success": False, "error": "title is required"}
            for t in task_store.list_all():
                if t.title.strip().lower() == target:
                    task_store.delete(t.id)
                    return {"success": True,
                            "message": f"Cancelled '{t.title}'"}
            return {"success": False,
                    "error": f"no scheduled task named '{target}'"}

        self.register(SkillDefinition(
            "task_cancel",
            "Cancel a scheduled task by its exact title.",
            {"type": "object", "properties": {
                "title": {"type": "string",
                          "description": "Exact task title to cancel"}},
             "required": ["title"]},
            task_cancel, "meta",
        ))


# ── Singleton ────────────────────────────────────────────────────────────

skill_registry = SkillRegistry()
