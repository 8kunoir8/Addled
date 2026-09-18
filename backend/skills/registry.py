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


async def _search_ddg_html(query: str) -> dict:
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
        with urllib.request.urlopen(req, timeout=15) as resp:
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
        """The memory link graph and the wiki."""
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
            # Primary: DuckDuckGo HTML endpoint (stdlib only, no Playwright)
            result = await _search_ddg_html(query)
            if result.get("success"):
                return result
            # Fallback 1: Bing (DuckDuckGo is blocked on some networks)
            result = await _search_bing(query)
            if result.get("success"):
                return result
            # Fallback 2: Playwright browser (if installed)
            try:
                from urllib.parse import quote
                from backend.browser.browser_engine import browser
                nav = await browser.navigate(
                    f"https://www.bing.com/search?q={quote(query)}")
                if nav.get("success"):
                    extract = await browser.extract()
                    text = (extract or {}).get("text", "") if isinstance(extract, dict) else str(extract or "")
                    if text:
                        return {"success": True, "query": query,
                                "engine": "browser", "url": nav.get("url"),
                                "snippet": text[:2000]}
            except Exception as e:
                log.debug("Browser search fallback failed: %s", e)
            return result

        self.register(SkillDefinition(
            "web_search",
            "Search the web (DuckDuckGo, falls back to Bing) and return "
            "result titles, URLs and snippets. If one query gives nothing "
            "useful, try again with different keywords (add site:, wiki, "
            "chapter number, or the site name).",
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
