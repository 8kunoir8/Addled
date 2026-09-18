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

    def enabled_list_all(self) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if self.is_enabled(s.name)]

    def list_category(self, category: str) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if s.category == category]

    def to_openai_tools(self) -> list[dict]:
        return [s.to_openai_tool() for s in self.enabled_list_all()]

    def to_claude_tools(self) -> list[dict]:
        return [s.to_claude_tool() for s in self.enabled_list_all()]

    def to_prompt_tools(self) -> str:
        """For providers without native tool support: append to the prompt.

        Deliberately terse. This text is added to the user's own message, so
        every token here is a token the model cannot spend on the question.
        """
        lines = ["\n## Tools",
                 'Call one with: ```tool\n{"tool": "name", "params": {}}\n```',
                 "A trailing '?' marks an optional argument.", ""]
        for skill in self.enabled_list_all():
            lines.append(skill.to_prompt_desc())
        return "\n".join(lines)

    async def execute(self, name: str, params: dict) -> SkillResult:
        """Execute a skill by name. Returns result for the AI provider."""
        skill = self._skills.get(name)
        if not skill:
            return SkillResult(False, name, error=f"Unknown skill: {name}")
        if not self.is_enabled(name):
            return SkillResult(False, name,
                               error=f"Skill '{name}' is disabled")

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
        self._register_web_skills()
        self._register_meta_skills()
        self._register_guideline_skills()
        self._register_memory_skills()

    # ── System ───────────────────────────────────────────────────────────

    def _register_system_skills(self):
        async def run_command(params: dict) -> dict:
            from backend.actions.terminal import TerminalExecutor
            t = TerminalExecutor()
            return await t.execute(
                str(params.get("command", "")),
                params.get("cwd"),
                params.get("timeout", 30),
            )
        self.register(SkillDefinition(
            "run_command",
            "Run a command in Windows PowerShell 5.1 on the user's PC. "
            "Use PowerShell syntax: ';' to chain commands (NOT '&&'), "
            "$env:USERPROFILE instead of '~', 'Test-Path' to check paths, "
            "'New-Item -ItemType Directory -Path X' to create folders. "
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
        async def code_read(params: dict) -> dict:
            import os
            path = str(params.get("path", ""))
            if not os.path.isfile(path):
                return {"success": False, "error": f"File not found: {path}"}
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            from backend.code.lang_detect import detect
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
            # 2) Search fallback: find the page's content via search results
            try:
                from urllib.parse import urlparse, unquote
                p = urlparse(url)
                domain = (p.netloc or "").replace("www.", "")
                slug = unquote(p.path).strip("/").split("/")[-1]
                slug = slug.replace("-", " ").replace("_", " ")
                query = f"{domain} {slug}".strip()
                res = await _search_bing(query)
                if not res.get("success"):
                    res = await _search_bing(f"{domain} {slug} latest")
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
