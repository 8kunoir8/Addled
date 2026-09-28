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

    # Kept as an alias because `_execute_gated` reads better naming the thing
    # it is asking about, and because a caller should not have to know which
    # of the two spellings the registry grew first.
    _is_granted = is_always_allowed

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

    def filter_for_query(self, query: str, max_tools: int = 10) -> set[str]:
        """Select a concise subset of relevant skills for prompt-based tool calling.

        Instead of injecting 50+ tool schemas (~4,700 tokens) into the context
        of local models, selects core utilities plus skills relevant to the
        user's query.

        ``max_tools`` defaults to 10: 4 core utilities are always included,
        leaving up to 6 slots for wiki, SOP, MCP, or domain tools that match
        the query. At ~80 tokens per schema that is ~800 tokens total — well
        within an 8,192-token window even before budget pruning.
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

        terms = set(_significant_terms(query or ""))
        q_lower = (query or "").lower()
        asks_for_tools = bool(terms & {
            "tool", "tools", "skill", "skills", "mcp", "server", "servers",
            "forge", "forged", "market", "registry", "capability", "capabilities",
            "search", "trust", "trusted", "verify", "verification",
        })
        asks_for_knowledge = bool(terms & knowledge_terms)
        asks_for_procedures = bool(terms & procedure_terms)

        scores: dict[str, float] = {}
        for name, skill in enabled.items():
            score = 1.5 if name in core_tools else 0.0
            name_lower = name.lower()
            name_parts = set(name_lower.split("_"))
            category = skill.category.lower()

            if asks_for_tools and name in discovery_tools:
                score += 6.0
            if asks_for_tools and category in {"mcp", "market", "forged", "meta"}:
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
            return {name for name in core_tools if name in enabled}

        sorted_tools = sorted(scores.keys(), key=lambda n: scores[n], reverse=True)
        selected = list(sorted_tools[:max_tools])
        if asks_for_tools:
            for name in discovery_tools:
                if name in enabled and name not in selected:
                    selected.append(name)
        # Force-include core utilities so the model can always read, write,
        # search the web, and run a command — they are the four things every
        # turn might need regardless of the query's topic.
        for c in core_tools:
            if c in enabled and c not in selected:
                selected.append(c)
        return set(selected[:max_tools])

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

        if getattr(skill, "requires_approval", False):
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
            # "agent_7f3a21bc".
            if not roster.get(agent_id):
                lowered = agent_id.strip().lower()
                match = next((e for e in roster.definitions()
                              if str(e.get("name", "")).lower() == lowered), None)
                if match:
                    agent_id = match["id"]
                else:
                    return {"success": False,
                            "error": (f"No agent '{agent_id}'. Use swarm_roster "
                                      "to see the saved agents.")}
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
            session_send, "system",
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
        ))

        async def file_info(params: dict) -> dict:
            from backend.actions.file_ops import FileOps
            return await FileOps().info(str(params.get("path", "")))
        self.register(SkillDefinition(
            "file_info", "Get file metadata (size, modified time, type)",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "File path"},
            }, "required": ["path"]},
            file_info, "files",
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
            """Learn a new capability on demand."""
            from backend.skills.forge import skill_forge
            from backend.providers.registry import get_provider

            task = str(params.get("task", params.get("description", "")))
            if not task:
                return {"success": False, "error": "No task description provided"}

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
            "Use when you need to do something that no existing tool can do.",
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

            result = await market.acquire_for(task)
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
            "covers, then use its tools. Searches the official MCP registry.",
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
