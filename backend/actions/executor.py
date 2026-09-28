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

# How long an in-band approval stays queued while a chat turn waits on it.
#
# This is a wait the *turn* is willing to do, not a deadline for the user. When
# it elapses the turn is told the request is still pending rather than being
# left to look hung, and the request stays in `_pending_approvals` so it can
# still be answered from the dashboard — `approve()` runs it directly when no
# waiter is active.
#
# Raised from 60s because a local model routinely takes longer than that to
# produce the turn that follows a tool call, so the approval card could be
# raised and expire before it was ever rendered. The user is not slow; the
# turn they are answering is.
APPROVAL_WAIT_S = 180.0


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
        self._approved_run = False
        self._handlers: dict[str, callable] = {}
        self._pending_approvals: dict[str, ActionRequest] = {}
        self._approval_counter = 0
        # approval_id -> Future, for a chat turn waiting on the answer.
        self._approval_waiters: dict[str, asyncio.Future] = {}

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

    def pending_approvals(self) -> list[dict]:
        """List destructive actions waiting for user approval."""
        return [
            {"approval_id": aid, "action_type": req.action_type, "params": req.params}
            for aid, req in self._pending_approvals.items()
        ]

    async def approve(self, approval_id: str) -> ActionResult:
        """Approve and execute a pending destructive action."""
        request = self._pending_approvals.pop(approval_id, None)
        if request is None:
            return ActionResult(False, error=f"No pending approval: {approval_id}")
        self._forget_origin(approval_id)
        # A chat turn may be blocked on this decision. Wake it so the command
        # runs on that turn rather than being executed a second time here.
        waiter = self._resolve_waiter(approval_id, "approved")
        if waiter:
            return ActionResult(True, request.action_type,
                                summary="Approved — running now",
                                data={"approval_id": approval_id})
        gate = self._gate
        self._gate = None  # already approved — bypass the gate for this run
        self._approved_run = True  # let run_command execute the approved command
        try:
            return await self.execute(request)
        finally:
            self._gate = gate
            self._approved_run = False

    def deny(self, approval_id: str) -> ActionResult:
        """Deny a pending destructive action."""
        request = self._pending_approvals.pop(approval_id, None)
        if request is None:
            return ActionResult(False, error=f"No pending approval: {approval_id}")
        self._forget_origin(approval_id)
        self._resolve_waiter(approval_id, None)
        return ActionResult(True, request.action_type, summary="Action denied by user",
                            data={"approval_id": approval_id})

    def _resolve_waiter(self, approval_id: str, verdict) -> bool:
        """Wake a waiting chat turn. Returns True when one was waiting.

        Must never raise: it is called from approve/deny, and a scheduling
        detail there must not turn a working approval into an error.
        """
        waiter = self._approval_waiters.pop(approval_id, None)
        if waiter is None:
            return False
        try:
            loop = getattr(waiter, "get_loop", lambda: None)()
            if loop is not None and loop.is_closed():
                return False
            if waiter.done():
                return False
            waiter.set_result(verdict)
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("could not wake approval waiter %s: %s", approval_id, e)
            return False

    async def execute_for_chat(self, action_type: str,
                               params: dict) -> ActionResult:
        """Run an action from a chat turn, asking in-band when it is gated.

        `execute()` queues a gated action and returns immediately, which left a
        tool call with an approval id and no way to consume it — the console
        command then read as a permissions wall. Here the turn waits for the
        user's decision and runs the command on the same turn when they agree.
        """
        self._lazy_init()
        request = ActionRequest(action_type=action_type, params=params or {})
        if not self._gated(action_type, request.params):
            return await self.execute(request)

        self._approval_counter += 1
        approval_id = f"appr_{self._approval_counter}"
        self._pending_approvals[approval_id] = request
        self._note_origin(approval_id, request)
        try:
            self._broadcast_approval_request(approval_id, request)
        except Exception as e:  # noqa: BLE001
            log.debug("approval broadcast failed: %s", e)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return ActionResult(False, action_type,
                error="This action needs approval, which is only available "
                      "while the app is running.",
                data={"approval_id": approval_id, "requires_approval": True})

        waiter: asyncio.Future = loop.create_future()
        self._approval_waiters[approval_id] = waiter
        try:
            verdict = await asyncio.wait_for(waiter, APPROVAL_WAIT_S)
        except asyncio.TimeoutError:
            self._approval_waiters.pop(approval_id, None)
            # Leave the approval pending in _pending_approvals so the user
            # can still approve it asynchronously from the dashboard after
            # the chat turn gave up waiting.  `approve()` runs the action
            # directly when no waiter is active — but only if the request
            # is still queued.  Previously this `finally` deleted it on
            # every exit, including timeout, which made the dashboard's
            # Approve button return "No pending approval" for anything the
            # agent asked about longer ago than the wait window.
            return ActionResult(False, action_type,
                error=f"'{params.get('command', action_type)}' needs your "
                      f"approval before it can run. Approve it in the "
                      f"dashboard (Settings or the pending-action prompt) and "
                      f"ask again.",
                data={"approval_id": approval_id, "requires_approval": True})

        # The chat turn got a verdict.  NOW it is safe to dequeue — the
        # action either ran below (approved) or was denied, and the user
        # will not be approving this same id later.
        self._pending_approvals.pop(approval_id, None)

        if verdict != "approved":
            return ActionResult(False, action_type,
                error="The user denied this command, so it was not run.",
                data={"approval_id": approval_id, "denied": True})

        gate = self._gate
        self._gate = None            # already decided — do not queue it again
        self._approved_run = True
        try:
            return await self.execute(request)
        finally:
            self._gate = gate
            self._approved_run = False

    def _gated(self, action_type: str, params: dict) -> bool:
        self._lazy_init()
        if not self._gate:
            return False
        try:
            if not self._gate.requires_approval(action_type, params):
                return False
        except Exception as e:  # noqa: BLE001
            log.debug("gate classification failed: %s", e)
            return False
        # The gate says this would ask. The user may have already said yes for
        # good — a granted name skips the prompt, which is the whole point of
        # "Always allow". The policy refuses the names the gate owns, so this
        # can only ever short-circuit the ones that are safe to remember.
        if self._is_granted(action_type, params):
            return False
        return True

    @staticmethod
    def _is_granted(action_type: str, params: dict) -> bool:
        """Has the user granted this action standing permission?

        Never raises: a policy that cannot be read must mean "ask", not
        "proceed" — the same direction the skill registry takes.
        """
        try:
            from backend.approvals import policy
        except Exception as e:  # noqa: BLE001
            log.debug("approval policy unavailable: %s", e)
            return False
        try:
            if policy.is_always_allowed(policy.SKILL, action_type):
                return True
            # A destructive command carried on a non-destructive action type
            # still has to be recognised, so the gate's content check is
            # repeated here rather than trusted to the caller's action name.
            name = str(params.get("command", "")).strip() if params else ""
            if name and policy.is_always_allowed(policy.TOOL, name):
                return True
            return False
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the approval policy: %s", e)
            return False

    async def request_approval(self, action_type: str, params: dict):
        """Ask the user before a skill runs. The skill-side entry point.

        Returns:
          True   — approved, run it now
          False  — denied, do not run it
          {"approval_id": ..., "message": ...} — still waiting after the
                   in-band window; the request stays queued so the dashboard
                   can answer it later.

        Kept separate from `execute_for_chat` because the two answer different
        questions. That one RUNS the action for a chat turn; this one only
        decides permission, so a caller that routes skills (`SkillRegistry`)
        does not have to hand the registry a second dispatch path for the
        action itself. The `_gate` is bypassed deliberately — the caller has
        already decided this needs approval, and re-asking the gate could
        disagree with the `requires_approval` flag that got us here.

        A name the user has granted standing permission is answered `True`
        before anything is queued, which is what makes "Always allow" stick.
        """
        self._lazy_init()
        if self._is_granted(action_type, params or {}):
            return True
        request = ActionRequest(action_type=action_type, params=params or {})
        self._approval_counter += 1
        approval_id = f"appr_{self._approval_counter}"
        self._pending_approvals[approval_id] = request
        self._note_origin(approval_id, request)
        try:
            self._broadcast_approval_request(approval_id, request)
        except Exception as e:  # noqa: BLE001
            log.debug("approval broadcast failed: %s", e)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop means no way to wait. Keep the request queued so the
            # dashboard can still answer it, and tell the caller it is pending.
            return {"approval_id": approval_id, "requires_approval": True,
                    "message": (f"'{action_type}' needs approval, which is only "
                                "available while the app is running.")}

        waiter: asyncio.Future = loop.create_future()
        self._approval_waiters[approval_id] = waiter
        try:
            verdict = await asyncio.wait_for(waiter, APPROVAL_WAIT_S)
        except asyncio.TimeoutError:
            self._approval_waiters.pop(approval_id, None)
            # Left pending on purpose: the user can still approve it from the
            # dashboard, and `approve()` runs it directly when no waiter is
            # active. Deleting it here is what once made Approve return "No
            # pending approval" for anything older than the wait window.
            return {"approval_id": approval_id, "requires_approval": True,
                    "message": (f"'{action_type}' needs your approval before it "
                                "can run. Approve it in the dashboard and ask "
                                "again.")}
        self._pending_approvals.pop(approval_id, None)
        try:
            from backend.approvals import pending as _pending_origins
            _pending_origins.forget(approval_id)
        except Exception:  # noqa: BLE001
            pass
        return verdict == "approved"

    async def resolve_by_conversation(self, source: object, conversation: object):
        """The approval a chat conversation is waiting on, oldest first.

        A bot answers with a typed "yes", which has no id in it. This is how
        that answer finds the request *its own chat* raised, rather than
        whichever request happened to be queued first.
        """
        try:
            from backend.approvals import pending as _pending_origins
            found = _pending_origins.for_conversation(source, conversation)
        except Exception as e:  # noqa: BLE001
            log.debug("could not look up approvals for a conversation: %s", e)
            return None
        for entry in found:
            ident = entry.get("approval_id")
            # Only one that is still genuinely queued, so a stale record
            # cannot be answered into nothing.
            if ident and ident in self._pending_approvals:
                return entry
        return None

    @staticmethod
    def _broadcast_approval_request(approval_id: str, request: ActionRequest) -> None:
        """Tell the dashboard a decision is waiting, so it can be answered."""
        from backend.actions import approval_notice
        approval_notice.publish(approval_id, request.action_type, request.params)

    @staticmethod
    def _note_origin(approval_id: str, request: ActionRequest) -> None:
        """Record which conversation asked, so only it can answer.

        Never raises. An approval with no recorded origin is still answerable
        from the dashboard by id — the id is what a card carries — but it
        cannot be answered by a chat message, which is the safe direction: a
        missing record means "not answerable from a chat", not "anyone may".
        """
        try:
            from backend.approvals import pending
            from backend import chat_context
            params = request.params or {}
            # The tool's own arguments do not name the chat that asked, so the
            # turn's origin is read from the context it was set in. The params
            # are still checked first: a caller that supplies an explicit
            # conversation is more specific than the ambient one.
            ambient = chat_context.origin()
            kind = "action"
            try:
                from backend.actions import approval_notice
                kind = approval_notice._classify(request.action_type)
            except Exception:  # noqa: BLE001
                pass
            grantable = False
            try:
                from backend.actions import approval_notice
                grantable = approval_notice._grantable(request.action_type)
            except Exception:  # noqa: BLE001
                pass
            pending.record(
                approval_id,
                source=params.get("source") or ambient.get("source"),
                conversation=(params.get("conversation")
                              or params.get("conversationId")
                              or ambient.get("conversation")),
                action_type=request.action_type,
                kind=kind,
                grantable=grantable,
                command=str(params.get("command") or ""),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("could not note the origin of %s: %s", approval_id, e)

    @staticmethod
    def _forget_origin(approval_id: str) -> None:
        """Drop the origin once an approval is answered or denied."""
        try:
            from backend.approvals import pending
            pending.forget(approval_id)
        except Exception as e:  # noqa: BLE001
            log.debug("could not forget the origin of %s: %s", approval_id, e)

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
        if self._gate is not None and self._gated(request.action_type,
                                                  request.params):
            self._approval_counter += 1
            approval_id = f"appr_{self._approval_counter}"
            self._pending_approvals[approval_id] = request
            return ActionResult(False, request.action_type,
                error="Destructive action awaiting approval",
                data={"approval_id": approval_id, "action": request.action_type,
                      "params": request.params, "requires_approval": True})

        # 3. Dispatch
        handler = self._handlers.get(request.action_type)
        if handler is None:
            return ActionResult(False, request.action_type, error=f"Unknown action: {request.action_type}")

        try:
            result = await handler(request.params)
            elapsed = int((time.monotonic() - started) * 1000)
            if isinstance(result, dict):
                # Capture all non-meta keys as data
                known_meta = {"success", "summary", "data", "error"}
                extra_data = {k: v for k, v in result.items() if k not in known_meta}
                return ActionResult(
                    success=result.get("success", True),
                    action_type=request.action_type,
                    summary=result.get("summary", ""),
                    duration_ms=elapsed,
                    data=result.get("data") or extra_data or None,
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
        # `overwrite` is opt-in: without it these refuse an occupied
        # destination rather than silently replacing it.
        self._handlers["copy_file"] = lambda p: f.copy(
            str(p.get("source", "")), str(p.get("dest", "")),
            overwrite=bool(p.get("overwrite")))
        self._handlers["move_file"] = lambda p: f.move(
            str(p.get("source", "")), str(p.get("dest", "")),
            overwrite=bool(p.get("overwrite")))
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
        self._handlers["run_command"] = lambda p: t.execute(
            str(p.get("command", "")), p.get("cwd"), p.get("timeout", 30),
            allow_dangerous=bool(getattr(self, "_approved_run", False)))

        # Utility actions
        self._handlers["wait"] = lambda p: asyncio.sleep(p.get("ms", 1000) / 1000)
        self._handlers["find_app"] = lambda p: l.find(str(p.get("name", "")))
        self._handlers["get_screen_size"] = lambda p: s.get_screen_size()
        self._handlers["get_clipboard"] = lambda p: s.get_clipboard()
        self._handlers["set_clipboard"] = lambda p: s.set_clipboard(str(p.get("text", "")))

        # Excel actions
        from backend.actions.excel_ops import excel_ops
        self._handlers["excel_read"] = lambda p: excel_ops.read(
            str(p.get("path", "")), p.get("sheet"), p.get("max_rows", 500))
        self._handlers["excel_write"] = lambda p: excel_ops.write(
            str(p.get("path", "")), p.get("sheet", "Sheet1"),
            p.get("cells", []), p.get("grid"))


# Singleton (with destruction gate: destructive actions await approval)
from backend.safety.destruction_gate import DestructionGate
executor = ActionExecutor(gate=DestructionGate())
