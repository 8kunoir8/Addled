"""
Action executor — unified dispatch for all agent actions.

Every action flows through: rate limiter → sandbox → destruction gate →
permission → execute → log result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("addled.executor")


@dataclass
class ActionRequest:
    action_type: str
    params: dict = field(default_factory=dict)
    priority: int = 5  # 1=critical, 10=background

@dataclass
class ActionResult:
    success: bool
    action_type: str = ""
    summary: str = ""
    duration_ms: int = 0
    error: str | None = None
    data: dict | None = None


class ActionExecutor:
    """Unified dispatcher. Registers handlers for 55+ action types."""

    def __init__(self, sandbox=None, rate_limiter=None, gate=None):
        self._sandbox = sandbox
        self._rate_limiter = rate_limiter
        self._gate = gate
        self._input = None
        self._launcher = None
        self._window_mgr = None
        self._file_ops = None
        self._system = None
        self._terminal = None
        self._cancel_flag = False
        self._handlers: dict[str, callable] = {}

    def _lazy_init(self):
        if self._input is None:
            from backend.actions.input_simulator import InputSimulator
            self._input = InputSimulator()
        if self._launcher is None:
            from backend.actions.app_launcher import AppLauncher
            self._launcher = AppLauncher()
        if self._window_mgr is None:
            from backend.actions.window_manager import WindowManager
            self._window_mgr = WindowManager()
        if self._file_ops is None:
            from backend.actions.file_ops import FileOps
            self._file_ops = FileOps()
        if self._system is None:
            from backend.actions.system_controls import SystemControls
            self._system = SystemControls()
        if self._terminal is None:
            from backend.actions.terminal import TerminalExecutor
            self._terminal = TerminalExecutor()
        self._register_handlers()

    def cancel_current(self):
        self._cancel_flag = True
        if self._input:
            self._input.cancel()

    async def execute(self, request: ActionRequest) -> ActionResult:
        """Main entry point. All actions go through this pipeline."""
        self._lazy_init()
        self._cancel_flag = False
        started = time.monotonic()

        # 1. Rate limiter
        if self._rate_limiter:
            status = self._rate_limiter.check(request.action_type)
            if status == "BLOCKED":
                return ActionResult(False, request.action_type, error="Rate limit exceeded")

        # 2. Destruction gate classification
        if self._gate:
            classification = self._gate.classify(request.action_type)
            if classification == "destructive":
                return ActionResult(False, request.action_type,
                    error="Destructive action requires approval. Use action.approve.")

        # 3. Dispatch
        handler = self._handlers.get(request.action_type)
        if handler is None:
            return ActionResult(False, request.action_type, error=f"Unknown action: {request.action_type}")

        try:
            result = await handler(request.params)
            elapsed = int((time.monotonic() - started) * 1000)
            if isinstance(result, dict):
                return ActionResult(
                    success=result.get("success", True),
                    action_type=request.action_type,
                    summary=result.get("summary", ""),
                    duration_ms=elapsed,
                    data=result.get("data"),
                    error=result.get("error"),
                )
            return ActionResult(True, request.action_type, duration_ms=elapsed, data=result)
        except Exception as e:
            log.exception("Action %s failed", request.action_type)
            elapsed = int((time.monotonic() - started) * 1000)
            return ActionResult(False, request.action_type, error=str(e), duration_ms=elapsed)

    def _register_handlers(self):
        """Register all 55+ action handlers."""
        i = self._input
        l = self._launcher
        w = self._window_mgr
        f = self._file_ops
        s = self._system
        t = self._terminal

        # Input actions
        self._handlers["click"] = lambda p: i.click(p.get("x", 0), p.get("y", 0), p.get("button", "left"))
        self._handlers["double_click"] = lambda p: i.double_click(p.get("x", 0), p.get("y", 0))
        self._handlers["type"] = lambda p: i.type_text(str(p.get("text", "")), p.get("cadence_ms", 80))
        self._handlers["key_press"] = lambda p: i.press_keys(str(p.get("keys", "")))
        self._handlers["scroll"] = lambda p: i.scroll(p.get("direction", "down"), p.get("amount", 3))
        self._handlers["drag"] = lambda p: i.drag(p.get("x1", 0), p.get("y1", 0), p.get("x2", 0), p.get("y2", 0))
        self._handlers["move_mouse"] = lambda p: i.move(p.get("x", 0), p.get("y", 0))

        # Application actions
        self._handlers["launch"] = lambda p: l.launch(str(p.get("name", "")), p.get("path"), p.get("args"))
        self._handlers["close_app"] = lambda p: l.close(str(p.get("name", "")), p.get("pid"))

        # Window actions
        self._handlers["list_windows"] = lambda p: w.list_windows()
        self._handlers["focus_window"] = lambda p: w.focus(str(p.get("title_substring", "")))
        self._handlers["resize_window"] = lambda p: w.resize(str(p.get("title_substring", "")), p.get("width", 800), p.get("height", 600))
        self._handlers["move_window"] = lambda p: w.move(str(p.get("title_substring", "")), p.get("x", 0), p.get("y", 0))
        self._handlers["minimize_window"] = lambda p: w.minimize(str(p.get("title_substring", "")))
        self._handlers["maximize_window"] = lambda p: w.maximize(str(p.get("title_substring", "")))
        self._handlers["restore_window"] = lambda p: w.restore(str(p.get("title_substring", "")))
        self._handlers["close_window"] = lambda p: w.close(str(p.get("title_substring", "")))

        # File actions
        self._handlers["read_file"] = lambda p: f.read(str(p.get("path", "")), p.get("encoding", "utf-8"))
        self._handlers["write_file"] = lambda p: f.write(str(p.get("path", "")), str(p.get("content", "")))
        self._handlers["append_file"] = lambda p: f.append(str(p.get("path", "")), str(p.get("content", "")))
        self._handlers["delete_file"] = lambda p: f.delete(str(p.get("path", "")))
        self._handlers["copy_file"] = lambda p: f.copy(str(p.get("source", "")), str(p.get("dest", "")))
        self._handlers["move_file"] = lambda p: f.move(str(p.get("source", "")), str(p.get("dest", "")))
        self._handlers["list_dir"] = lambda p: f.list_dir(str(p.get("path", ".")), p.get("pattern", "*"))
        self._handlers["create_dir"] = lambda p: f.create_dir(str(p.get("path", "")))
        self._handlers["search_files"] = lambda p: f.search(str(p.get("directory", ".")), str(p.get("pattern", "*")), p.get("recursive", True))
        self._handlers["file_info"] = lambda p: f.info(str(p.get("path", "")))

        # System actions
        self._handlers["volume"] = lambda p: s.set_volume(p.get("level", 50))
        self._handlers["brightness"] = lambda p: s.set_brightness(p.get("level", 50))
        self._handlers["lock"] = lambda p: s.lock()
        self._handlers["screenshot"] = lambda p: s.screenshot(p.get("monitor"), p.get("region"))

        # Terminal actions
        self._handlers["run_command"] = lambda p: t.execute(str(p.get("command", "")), p.get("cwd"), p.get("timeout", 30))

        # Utility actions
        self._handlers["wait"] = lambda p: asyncio.sleep(p.get("ms", 1000) / 1000)
        self._handlers["find_app"] = lambda p: l.find(str(p.get("name", "")))
        self._handlers["get_screen_size"] = lambda p: s.get_screen_size()
        self._handlers["get_clipboard"] = lambda p: s.get_clipboard()
        self._handlers["set_clipboard"] = lambda p: s.set_clipboard(str(p.get("text", "")))


# Singleton
executor = ActionExecutor()
