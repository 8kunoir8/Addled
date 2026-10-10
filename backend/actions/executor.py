"""
Action executor — unified dispatch for all agent actions.

Every action flows through: rate limiter → sandbox → destruction gate →
permission → execute → log result.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
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
# There is no longer an approval wait window. A gated action used to block the
# turn for APPROVAL_WAIT_S, which a local model routinely exceeded before the
# user could answer — so the turn lost the race and reported a timeout instead
# of showing the prompt. The turn now ends and the request waits for the user
# however long that takes; see `execute_for_chat`.


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


def _console_note_pending(approval_id: str,
                          request: "ActionRequest") -> None:
    """Record a queued command as `awaiting` on the console. Never raises.

    A module function so the three queue sites cannot drift in how they report;
    the panel reads the same shape whichever path raised the request.

    Kept out of the console module because it needs the request's shape, and the
    console deliberately knows nothing about actions - it takes plain strings so
    it can be exercised without an executor.
    """
    try:
        from backend.actions import console_log as console
        console.record(console.make_entry(
            command=ActionExecutor._console_command(request),
            kind="shell",
            tool=request.action_type,
            status=console.AWAITING,
            conversation=ActionExecutor._console_conversation(),
            approval_id=approval_id,
        ))
    except Exception as e:  # noqa: BLE001
        log.debug("could not record pending command: %s", e)


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
        self._pending_approvals: dict[str, ActionRequest] = {}
        self._approval_counter = 0
        # Always empty now. Kept, and popped from in `_resolve_waiter`, so an
        # out-of-tree caller from a build that had a waiting turn finds the
        # attribute it expects rather than an AttributeError.
        self._approval_waiters: dict[str, asyncio.Future] = {}

    def _new_approval_id(self) -> str:
        """A fresh approval id, unique beyond this process.

        The counter alone was not enough: it starts at 0 with the process, so
        `appr_1` was handed out again after every restart. A permission card is
        answered BY ID, so a card left over from before a restart carried an id
        that now named a different request -- and at best resolved to nothing,
        which is the failure the user saw reported as "the approval never
        arrived".

        The counter is kept because it makes the log readable and ordered; the
        random suffix is what stops a restart from reissuing a live id.
        """
        self._approval_counter += 1
        return f"appr_{self._approval_counter}_{secrets.token_hex(4)}"

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
        """Approve and run a pending destructive action, then RESUME the task.

        The turn that asked ended when it asked — that part is deliberate, and
        the docstring on `execute_for_chat` explains why (waiting in-band lost a
        race with the dashboard's socket timeout, so the user saw "Request
        timed out" instead of a permission prompt).

        What was missing is the other half. Approving ran the command and pushed
        a receipt, but the model was never handed the result, so a multi-step
        task stopped dead at the gated step: "delete this file, then list the
        folder" deleted the file and never listed anything. The user saw a
        standalone "✅ Approved and ran X" and no continuation.

        So the continuation is the point, not a nicety. It is not a new request
        about the approval — it is the tool result the model was already waiting
        for, delivered late. The turn picks up where it stopped.
        """
        # The origin has to be read BEFORE `_forget_origin` clears it, because
        # it is what identifies the conversation to continue.
        origin = self._origin_of(approval_id)
        request = self._pending_approvals.pop(approval_id, None)
        if request is None:
            return ActionResult(False, error=f"No pending approval: {approval_id}")
        self._forget_origin(approval_id)
        # Clearing the gate for this one call is the WHOLE of what approval
        # does. There used to be a `_approved_run` flag passed into the
        # terminal's `allow_dangerous` parameter, which the terminal accepted
        # and never read — so it looked like a second guard was being lifted
        # when nothing was there to lift.
        gate = self._gate
        self._gate = None
        try:
            result = await self.execute(request)
        finally:
            self._gate = gate
        self._resume_after_decision(approval_id, origin, request, result,
                                    approved=True)
        return result

    @staticmethod
    def _report_to_chat(approval_id: str, request: ActionRequest,
                        result: ActionResult) -> None:
        """Tell the conversation what an approved action did.

        Never raises: the command has already run by the time this is called,
        and a reporting failure must not turn a completed action into an error
        the user sees. The push carries the action's own summary, so the chat
        shows what happened rather than only that something was approved.
        """
        try:
            from backend.ws_server import get_server
            server = get_server()
            if server is None:
                return
            from backend import chat_sources
            what = request.action_type
            command = str((request.params or {}).get("command") or "").strip()
            if command:
                what = f"`{command}`"
            if result.success:
                detail = str(result.summary or "").strip()
                body = f"✅ Approved and ran {what}."
                if detail:
                    body += f"\n\n{detail}"
            else:
                body = (f"⚠️ Approved {what}, but it failed: "
                        f"{result.error or 'no reason given'}")
            server.broadcast_nowait("chat.push", {
                "role": "assistant",
                "content": body,
                "insight": True,
                **chat_sources.describe("approval"),
            })
        except Exception as e:  # noqa: BLE001
            log.debug("could not report approval %s to chat: %s",
                      approval_id, e)

    def deny(self, approval_id: str) -> ActionResult:
        """Deny a pending destructive action.

        Also resumes, for the same reason an approval does: the agent stopped
        mid-task to ask, and going silent after a refusal leaves it with no idea
        what happened. Told plainly that it was refused, it can carry on without
        that step or say why it cannot.
        """
        origin = self._origin_of(approval_id)
        request = self._pending_approvals.pop(approval_id, None)
        if request is None:
            return ActionResult(False, error=f"No pending approval: {approval_id}")
        self._forget_origin(approval_id)
        # Updates the row the gate already created, keyed on the same approval
        # id. A refusal must be VISIBLE: a row that simply disappears is the
        # silence that let a model claim a denied command was still pending.
        try:
            from backend.actions import console_log as _console
            _console.update(approval_id, status=_console.DENIED,
                            stderr="You denied this command.",
                            duration_ms=0)
        except Exception as e:  # noqa: BLE001
            log.debug("could not record denial: %s", e)
        outcome = ActionResult(True, request.action_type,
                               summary="Action denied by user",
                               data={"approval_id": approval_id})
        self._resume_after_decision(approval_id, origin, request, outcome,
                                    approved=False)
        return outcome

    @staticmethod
    def _origin_of(approval_id: str) -> dict:
        """Which conversation raised this approval, before it is forgotten."""
        try:
            from backend.approvals import pending
            return pending.get(approval_id) or {}
        except Exception as e:  # noqa: BLE001
            log.debug("could not read the origin of %s: %s", approval_id, e)
            return {}

    @staticmethod
    def _resume_after_decision(approval_id: str, origin: dict,
                               request: ActionRequest,
                               result: ActionResult | None,
                               *, approved: bool) -> None:
        """Continue the task the interrupted turn was working on.

        Fire and forget, on the server's loop: the approval RPC must answer
        immediately, and a resumed turn can take minutes.

        Falls back to the old receipt when there is no conversation to continue.
        A gated action raised by a bare dashboard button belongs to nobody's
        task, so there is nothing to resume — and reporting it is better than
        the silence this whole change is about.
        """
        conversation = str(origin.get("conversation") or "").strip()
        if not conversation:
            ActionExecutor._report_to_chat(approval_id, request,
                                           result or ActionResult(False))
            return
        try:
            from backend.ws_server import resume_after_decision
            resume_after_decision(conversation,
                                  ActionExecutor._continuation(request, result,
                                                               approved))
        except Exception as e:  # noqa: BLE001
            # Warning, not debug. This branch means the agent was left mid-task
            # with no idea what happened — the exact failure this exists to fix
            # — so it must be visible rather than buried.
            log.warning("could not resume after %s: %s", approval_id, e)
            try:
                ActionExecutor._report_to_chat(approval_id, request,
                                               result or ActionResult(False))
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _continuation(request: ActionRequest, result: ActionResult | None,
                      approved: bool) -> str:
        """What the model is told so it can carry on.

        Phrased as a tool result, not as a new instruction: the turn stopped
        only because this answer was not known yet, so handing it over is what
        lets the model continue rather than start something.

        Forbids repeating the identical call on purpose. The model asked for
        this action, and telling it "approved" without saying "do not ask again"
        invites it to re-issue the same call, which would raise a second
        approval for something the user just allowed.
        """
        what = request.action_type
        command = str((request.params or {}).get("command") or "").strip()
        label = f"`{command}`" if command else what

        if approved and result is not None and result.success:
            detail = str(result.summary or "").strip()
            body = (f"The {label} you asked for was APPROVED and ran "
                    f"successfully.")
            if detail:
                body += f"\n\nResult:\n{detail[:4000]}"
        elif approved:
            body = (f"The {label} you asked for was APPROVED, but it failed: "
                    f"{(result.error if result else 'no reason given')}")
        else:
            body = (f"The {label} you asked for was DENIED by the user. Do not "
                    "attempt it again. Continue the rest of the task without "
                    "it, or say what you cannot do because of the refusal.")

        return (body + "\n\nContinue the task this was part of, from where it "
                       "stopped. Do not repeat this same call — it has been "
                       "answered.")

    def _resolve_waiter(self, approval_id: str, verdict) -> bool:
        """Retired: no chat turn waits on an approval any more.

        Kept as a no-op so an out-of-tree caller from an older build does not
        raise `AttributeError` on a path that used to work. It never returns
        True, because there is nothing left to wake.
        """
        self._approval_waiters.pop(approval_id, None)
        return False

    async def execute_for_chat(self, action_type: str,
                               params: dict) -> ActionResult:
        """Run an action from a chat turn, handing a gated one back unanswered.

        This used to wait in-band for the user's decision, and the wait was the
        problem. The turn blocked inside the tool call for up to
        `APPROVAL_WAIT_S`, while the dashboard's own socket timeout was the same
        180 seconds and a local-model turn had already spent minutes thinking —
        so the turn lost the race almost every time, and what the user saw was
        "Request timed out" rather than the permission prompt they were meant to
        answer. Asking for permission is not a failure and must not be reported
        as one.

        So the turn ENDS here and the request is left queued. `approve()` runs it
        when the user answers — that path already existed for requests the old
        timeout gave up on — and reports the outcome back into the conversation,
        so approving is not a silent act.
        """
        self._lazy_init()
        request = ActionRequest(action_type=action_type, params=params or {})
        if not self._gated(action_type, request.params):
            return await self.execute(request)

        approval_id = self._new_approval_id()
        self._pending_approvals[approval_id] = request
        self._note_origin(approval_id, request)
        _console_note_pending(approval_id, request)
        try:
            self._broadcast_approval_request(approval_id, request)
        except Exception as e:  # noqa: BLE001
            log.debug("approval broadcast failed: %s", e)

        # Deliberately NOT registered as a waiter: nothing is blocked on this
        # decision, so there is no future to wake and no window to expire. The
        # request stays in `_pending_approvals`, which is exactly what
        # `approve()` looks in when it finds no waiter.
        return ActionResult(
            False, action_type,
            error=("This needs your permission before it can run. Approve it "
                   "in the dashboard and it will run straight away."),
            data={"approval_id": approval_id, "requires_approval": True,
                  "deferred": True})

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
            # The session grant is checked first because it is the only one a
            # content-classified action can have. `run_command` is granted for
            # the sitting and not permanently, so looking for the permanent
            # answer first would leave that grant unable to take effect.
            if policy.is_allowed_for_session(policy.SKILL, action_type):
                return True
            if policy.is_always_allowed(policy.SKILL, action_type):
                return True
            # A destructive command carried on a non-destructive action type
            # still has to be recognised, so the gate's content check is
            # repeated here rather than trusted to the caller's action name.
            name = str(params.get("command", "")).strip() if params else ""
            if name and policy.is_allowed_for_session(policy.TOOL, name):
                return True
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
          {"approval_id": ..., "message": ...} — queued and unanswered. The
                   turn ends here; the request waits for the user and runs when
                   they answer it.

        **It never waits.** Holding the turn open for `APPROVAL_WAIT_S` meant the
        answer usually arrived after the caller had given up, because that
        window is not longer than the dashboard's socket timeout once a
        local-model turn has spent minutes thinking — so the user saw a timeout
        rather than the prompt. Approving later runs the action through
        `approve()`, which is the path the old timeout already fell back to.

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
        approval_id = self._new_approval_id()
        self._pending_approvals[approval_id] = request
        self._note_origin(approval_id, request)
        _console_note_pending(approval_id, request)
        try:
            self._broadcast_approval_request(approval_id, request)
        except Exception as e:  # noqa: BLE001
            log.debug("approval broadcast failed: %s", e)

        # Deliberately NOT registered as a waiter: nothing is blocked on this
        # decision, so there is no future to wake and no window to expire. The
        # request stays in `_pending_approvals`, which is exactly what
        # `approve()` looks in when it finds no waiter.
        return {"approval_id": approval_id, "requires_approval": True,
                "deferred": True,
                "message": ("This needs your permission before it can run. "
                            "Approve it in the dashboard and it will run "
                            "straight away.")}

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
    def _console_command(request: ActionRequest) -> str:
        """The human-readable command behind a gated action.

        A gated action is usually `run_command` and carries `command`; the
        other content-classified one is a session write. Falling back to the
        params keeps an unexpected action type from reporting an empty row.
        """
        params = request.params or {}
        for key in ("command", "input", "text"):
            value = str(params.get(key) or "").strip()
            if value:
                return value
        return str(request.action_type)

    @staticmethod
    def _console_conversation() -> str | None:
        """Which conversation asked, so the panel can filter to this chat."""
        try:
            from backend import chat_context
            return chat_context.origin() or None
        except Exception:  # noqa: BLE001
            return None

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
            approval_id = self._new_approval_id()
            self._pending_approvals[approval_id] = request
            # Recorded for the same reason the two entry points do it: an
            # approval with no origin cannot be answered from the chat that
            # asked, which is the only way a bot can answer at all. This site
            # queued the request and left no trace of who raised it.
            self._note_origin(approval_id, request)
            _console_note_pending(approval_id, request)
            return ActionResult(False, request.action_type,
                error="Destructive action awaiting approval",
                data={"approval_id": approval_id, "action": request.action_type,
                      "params": request.params, "requires_approval": True})

        # 3. Dispatch
        handler = self._handlers.get(request.action_type)
        if handler is None:
            # A gated SKILL has no executor handler: the approval was raised by
            # `SkillRegistry._execute_gated`, which queued the skill's NAME, and
            # this is where that name is run. Without this fallback every gated
            # skill that is not also an executor action was answered and then
            # failed with "Unknown action: <name>" — the card worked, the user
            # approved, and nothing happened. `pdf_create`, `word_create`,
            # `word_edit`, `pptx_create` and `pptx_add_slide` are all in that
            # set, and so is any skill added later.
            #
            # The skill's own handler is called rather than the registry's
            # `execute`, because `execute` would gate it again and ask the user
            # a second time for the thing they just approved.
            skill = None
            try:
                from backend.skills.registry import skill_registry
                skill = skill_registry.get(request.action_type)
            except Exception as e:  # noqa: BLE001
                log.debug("could not look up skill %s: %s",
                          request.action_type, e)
            if skill is None:
                return ActionResult(False, request.action_type,
                                    error=f"Unknown action: {request.action_type}")
            try:
                result = await skill.handler(request.params or {})
                elapsed = int((time.monotonic() - started) * 1000)
                if isinstance(result, dict):
                    known_meta = {"success", "summary", "data", "error"}
                    extra = {k: v for k, v in result.items() if k not in known_meta}
                    return ActionResult(
                        success=result.get("success", True),
                        action_type=request.action_type,
                        summary=result.get("summary", ""),
                        duration_ms=elapsed,
                        data=result.get("data") or extra or None,
                        error=result.get("error"),
                    )
                return ActionResult(True, request.action_type,
                                    duration_ms=elapsed, data=result)
            except Exception as e:  # noqa: BLE001
                log.exception("Approved skill %s failed", request.action_type)
                elapsed = int((time.monotonic() - started) * 1000)
                return ActionResult(False, request.action_type, error=str(e),
                                    duration_ms=elapsed)

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
        # No `allow_dangerous` argument: the terminal never had a guard to lift,
        # and passing `_approved_run` into a parameter nothing read implied one
        # existed. An approved command runs because `approve()` clears
        # `self._gate` for the call, which is the single place approval acts.
        self._handlers["run_command"] = lambda p: t.execute(
            str(p.get("command", "")), p.get("cwd"), p.get("timeout", 30))

        # Utility actions
        self._handlers["wait"] = lambda p: asyncio.sleep(p.get("ms", 1000) / 1000)
        self._handlers["find_app"] = lambda p: l.find(str(p.get("name", "")))
        self._handlers["get_screen_size"] = lambda p: s.get_screen_size()
        self._handlers["get_clipboard"] = lambda p: s.get_clipboard()
        self._handlers["set_clipboard"] = lambda p: s.set_clipboard(str(p.get("text", "")))

        # Excel actions are NOT registered here.
        #
        # `excel_read`, `excel_write` and `excel_sheets` are all skills, and both
        # paths reach them through the skill fallback above. The handlers that
        # used to sit here called the older `excel_ops`, which takes a different
        # argument shape (`cells` before `grid`) and hands the path straight to
        # the OS with no workspace guard.
        #
        # Two implementations sharing a name is worse than either one alone: an
        # approved write went to the unguarded one, so a relative path failed
        # with "No such file or directory" while the same call through the skill
        # worked, and a path outside the workspace would have been allowed. One
        # implementation is the only version of this that stays correct.


# Singleton (with destruction gate: destructive actions await approval)
from backend.safety.destruction_gate import DestructionGate
executor = ActionExecutor(gate=DestructionGate())
