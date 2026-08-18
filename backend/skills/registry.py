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
        """Plain-text description for providers without native tool support."""
        params_desc = json.dumps(self.parameters.get("properties", {}), indent=2)
        return (
            f"Tool: {self.name}\n"
            f"Description: {self.description}\n"
            f"Parameters: {params_desc}\n"
        )


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

    lines = []
    for i, r in enumerate(results[:5]):
        lines.append(f"{i + 1}. {r['title']} — {r['url']}")
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

    def get(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def list_all(self) -> list[SkillDefinition]:
        return list(self._skills.values())

    def list_category(self, category: str) -> list[SkillDefinition]:
        return [s for s in self._skills.values() if s.category == category]

    def to_openai_tools(self) -> list[dict]:
        return [s.to_openai_tool() for s in self._skills.values()]

    def to_claude_tools(self) -> list[dict]:
        return [s.to_claude_tool() for s in self._skills.values()]

    def to_prompt_tools(self) -> str:
        """For providers without native tool support: append to system prompt."""
        lines = ["\n## Available Tools\n"]
        lines.append("You can call these tools by responding with a JSON block:")
        lines.append('```tool\n{"tool": "tool_name", "params": {...}}\n```\n')
        for skill in self._skills.values():
            lines.append(skill.to_prompt_desc())
            lines.append("")
        return "\n".join(lines)

    async def execute(self, name: str, params: dict) -> SkillResult:
        """Execute a skill by name. Returns result for the AI provider."""
        skill = self._skills.get(name)
        if not skill:
            return SkillResult(False, name, error=f"Unknown skill: {name}")

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
            "Returns stdout/stderr of the command.",
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
            "web_search", "Search the web (DuckDuckGo) and return result titles, URLs and snippets",
            {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
            }, "required": ["query"]},
            web_search, "web",
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


# ── Singleton ────────────────────────────────────────────────────────────

skill_registry = SkillRegistry()
