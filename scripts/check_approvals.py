"""Approval checks — standing permission, and the guard that bounds it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_approvals.py

The thing worth testing here is not that a grant is remembered. It is the two
directions that would be silently wrong:

* a name the destruction gate owns must be refused **by the policy itself**,
  not merely hidden in the dashboard, so no caller can make `delete_file` or a
  destructive shell command permanent; and
* when the permission store cannot be read, the answer must be "ask", never
  "proceed".

The config is saved and restored around the run, because the policy writes
through to the real `settings.json` — that is the point of it, so the test has
to put back what it found.
"""

import json
import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.approvals import policy
from backend.mcp_client import approval as mcp_approval

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    from backend.config import config

    saved = config.get("safety", "always_allow", default=None)

    try:
        # ---- the guard, before anything else -------------------------------
        # `run_command` is the one that must never be remembered. Its danger is
        # in the command it carries, and a grant is keyed by name, so a stored
        # answer could not tell a safe command from `format`.
        for guarded in ("run_command", "rm -rf", "format", "diskpart"):
            r = policy.always_allow(policy.SKILL, guarded)
            check(f"'{guarded}' cannot be granted standing permission",
                  r.get("success") is False and r.get("protected") is True,
                  str(r))
            check(f"and '{guarded}' is not allowed afterwards",
                  policy.is_always_allowed(policy.SKILL, guarded) is False,
                  "the grant went through")

        # The actions whose danger is inherent ARE grantable now: the danger is
        # the action itself, so remembering the answer is answering the actual
        # question. They stay gated per call either way.
        for grantable in ("delete_file", "close_app", "close_window"):
            check(f"'{grantable}' can be granted standing permission",
                  policy.is_permanently_grantable(grantable) is True,
                  "refused, so the card would offer a switch that cannot work")
            r = policy.always_allow(policy.SKILL, grantable)
            check(f"and '{grantable}' is then allowed",
                  r.get("success") is True
                  and policy.is_always_allowed(policy.SKILL, grantable) is True,
                  str(r))
            policy.revoke(policy.SKILL, grantable)

        # A skill whose name merely *starts with* a dangerous command is not
        # that command. `delete_file` starts with `del`; before the match was
        # made exact, that coincidence was the real reason the delete skill
        # could not be granted.
        check("a name that merely prefixes a command is not treated as one",
              policy.is_permanently_grantable("delete_file") is True,
              "a prefix match is shadowing the delete skill")

        # ---- session grants -------------------------------------------------
        # The only grant a content-classified action can have, and the reason
        # `run_command` is usable at all: one answer for the sitting.
        store_before = json.dumps(
            config.get("safety", "always_allow", default={}), sort_keys=True)
        r = policy.allow_for_session(policy.SKILL, "run_command")
        check("a session grant is accepted for a protected action",
              r.get("success") is True, str(r))
        check("and it takes effect",
              policy.is_allowed_for_session(policy.SKILL, "run_command") is True,
              "the session grant did not apply")
        check("but the permanent grant is still refused",
              policy.is_always_allowed(policy.SKILL, "run_command") is False,
              "a session grant leaked into the permanent one")

        # The important one: a session grant must never reach the disk, or it
        # would outlive the sitting it was given in.
        store_after = json.dumps(
            config.get("safety", "always_allow", default={}), sort_keys=True)
        check("a session grant is never written to settings",
              store_before == store_after,
              "the session grant was persisted")
        stored = config.get("safety", "always_allow", default={}) or {}
        check("and 'run_command' is not in the stored list",
              "run_command" not in (stored.get("skills") or []),
              "it reached the config file")

        check("session grants are visible to the UI",
              "run_command" in policy.session_grants().get("skills", []),
              "an invisible grant is one the user forgets making")

        r = policy.revoke_session(policy.SKILL, "run_command")
        check("a session grant can be revoked before it expires",
              r.get("success") is True
              and policy.is_allowed_for_session(policy.SKILL, "run_command")
              is False, str(r))

        policy.allow_for_session(policy.SKILL, "run_command")
        policy.clear_session()
        check("clearing the session drops every grant",
              policy.is_allowed_for_session(policy.SKILL, "run_command")
              is False, "a grant survived the clear")

        check("the guard reports the names the gate owns",
              "run_command" in policy.protected_names(),
              str(sorted(policy.protected_names()))[:200])

        # ---- a grant round-trips through the config ------------------------
        policy.clear()
        r = policy.always_allow(policy.SKILL, "send_email")
        check("an ordinary skill can be granted", r.get("success") is True,
              str(r))
        check("and reads back as allowed",
              policy.is_always_allowed(policy.SKILL, "send_email") is True,
              "the grant did not persist")
        check("it appears in the list",
              "send_email" in policy.list_allowed()["skills"],
              str(policy.list_allowed()))

        # Re-read from the config, the way a restart would see it: the list has
        # to be on disk, not in a module-level set.
        on_disk = config.get("safety", "always_allow", default={}) or {}
        check("the grant is written to the config, so a restart keeps it",
              "send_email" in (on_disk.get("skills") or []),
              str(on_disk))

        # ---- revoke --------------------------------------------------------
        r = policy.revoke(policy.SKILL, "send_email")
        check("revoking succeeds", r.get("success") is True, str(r))
        check("and it asks again afterwards",
              policy.is_always_allowed(policy.SKILL, "send_email") is False,
              "still allowed after revoke")

        # ---- kinds are checked, not created on the fly ---------------------
        r = policy.always_allow("widget", "anything")
        check("an unknown kind is refused rather than stored",
              r.get("success") is False, str(r))
        check("and nothing was written for it",
              "widgets" not in (config.get("safety", "always_allow",
                                           default={}) or {}),
              "an unknown kind created a new config key")

        for bad in ("", "   ", None):
            r = policy.always_allow(policy.SKILL, bad)
            check(f"an empty name ({bad!r}) is refused",
                  r.get("success") is False, str(r))

        # ---- an unreadable store must mean "ask" ---------------------------
        # The failure direction matters: a policy that cannot answer has to
        # leave the prompt in place, because the other way round is a silent
        # permanent grant.
        real_read = policy._read

        def broken_read():
            raise RuntimeError("simulated failure")

        policy._read = broken_read
        try:
            check("an unreadable store does not grant",
                  policy.is_always_allowed(policy.SKILL, "send_email") is False,
                  "a broken store reported allowed")
        finally:
            policy._read = real_read

        # ---- MCP tools: session approval, and standing approval -------------
        policy.clear()
        mcp_approval._approved.clear()
        key = mcp_approval.key("demo-server", "do_thing")
        check("the approval key is server::tool", key == "demo-server::do_thing",
              key)

        check("an MCP tool starts unapproved",
              mcp_approval.is_approved("demo-server", "do_thing") is False,
              "approved before it was granted")
        check("and has no standing approval",
              mcp_approval.is_standing("demo-server", "do_thing") is False,
              "standing before it was granted")

        mcp_approval.approve("demo-server", "do_thing")
        check("a session approval is approved",
              mcp_approval.is_approved("demo-server", "do_thing") is True,
              "session approval did not take")
        check("but is not standing",
              mcp_approval.is_standing("demo-server", "do_thing") is False,
              "a session approval was reported as standing")

        r = mcp_approval.approve_always("demo-server", "do_thing")
        check("approve_always succeeds", r.get("success") is True, str(r))
        check("and it is now standing",
              mcp_approval.is_standing("demo-server", "do_thing") is True,
              "a standing approval was not recorded")
        check("it survives losing the session set",
              (mcp_approval._approved.clear() or True)
              and mcp_approval.is_approved("demo-server", "do_thing") is True,
              "the standing half was not consulted")

        # Revoking has to clear both halves, or the tool keeps running without
        # a prompt — the opposite of what revoking is for.
        mcp_approval.approve("demo-server", "do_thing")
        mcp_approval.revoke("demo-server", "do_thing")
        check("revoking clears the session approval",
              mcp_approval.is_approved("demo-server", "do_thing") is False,
              "the session approval survived")
        check("and the standing approval",
              mcp_approval.is_standing("demo-server", "do_thing") is False,
              "the standing approval survived revoke")

        # Revoking a whole server takes every tool on it.
        mcp_approval.approve_always("demo-server", "one")
        mcp_approval.approve_always("demo-server", "two")
        mcp_approval.approve_always("other-server", "three")
        mcp_approval.revoke("demo-server")
        check("revoking a server clears its tools",
              mcp_approval.is_approved("demo-server", "one") is False
              and mcp_approval.is_approved("demo-server", "two") is False,
              "a tool on the revoked server is still approved")
        check("and leaves other servers alone",
              mcp_approval.is_approved("other-server", "three") is True,
              "revoking one server cleared another")

        # ---- the turn must not wait for an approval ------------------------
        # It used to block in-band for APPROVAL_WAIT_S, which the dashboard's
        # own socket timeout matched and a local-model turn usually exceeded —
        # so the user saw "Request timed out" instead of the card they were
        # meant to answer. A permission prompt is not a failure, and the turn
        # now ends so nothing can expire.
        import asyncio
        import time as _time
        from backend.actions import executor as executor_module
        executor = executor_module.executor
        executor._lazy_init()
        executor._pending_approvals.clear()
        executor._approval_waiters.clear()

        async def _gate_and_time():
            t0 = _time.time()
            res = await executor.execute_for_chat(
                "run_command", {"command": "del /q C:\\nope\\x"})
            return res, _time.time() - t0

        res, elapsed = asyncio.run(_gate_and_time())
        check("a gated action returns without waiting",
              elapsed < 5, f"took {elapsed:.1f}s — it is still blocking")
        check("and it says approval is needed",
              bool((res.data or {}).get("requires_approval")), str(res.data))
        check("and it does not tell the user to ask again",
              "ask again" not in (res.error or ""),
              "the action runs on approval, so re-asking is wrong")
        queued = (res.data or {}).get("approval_id") in executor._pending_approvals
        check("and the request is left queued for the user to answer",
              bool(queued), "nothing was left to approve")
        check("and no turn is registered as waiting",
              not executor._approval_waiters,
              "a waiter would be a turn that could still time out")

        # Approving must actually run it — that is the other half of "the turn
        # ended". Without this the request would be queued and never actioned.
        from backend.actions.executor import ActionRequest
        aid = (res.data or {}).get("approval_id")
        executor._pending_approvals[aid] = ActionRequest(
            action_type="run_command", params={"command": "echo hi"})
        check("approving a deferred request runs it",
              asyncio.run(executor.approve(aid)).success is True,
              "the approved action did not run")
        executor._pending_approvals.clear()
        executor._approval_waiters.clear()

        # ---- a session grant remembers the COMMAND, not the skill -----------
        # Exercised through the real handler, because the defect was in what the
        # BUTTON records -- `approvals_always_allow` was storing the skill name
        # under the skill bucket, and `_gated("run_command", ...)` then released
        # every command for the rest of the sitting.
        policy.clear_session()
        import backend.ws_server as _W
        from backend.approvals import pending as _origins
        from backend.actions.executor import executor as _ex2
        from backend import chat_context as _ctx

        async def _grant_for_one_command():
            _origins.clear()
            _ex2._pending_approvals.clear()
            _ctx.set_origin("dashboard", "conv_grant_probe")
            _r = await _ex2.request_approval(
                "run_command", {"command": "Get-ChildItem C:"})
            _aid = _r["approval_id"]
            _W._register_default_handlers()
            _h = _W._server._handlers["approvals.alwaysAllow"]
            _e = _origins.get(_aid) or {}
            _res = await _h({"kind": _e.get("kind"), "name": "run_command",
                             "approvalId": _aid, "session": True}, None)
            return _res

        _granted = asyncio.run(_grant_for_one_command())
        check("the grant is keyed to the command, not the skill",
              _granted.get("kind") == "tool"
              and "Get-ChildItem" in str(_granted.get("name")),
              f"granted {_granted.get('kind')}={_granted.get('name')!r}")

        check("the command the user allowed is remembered",
              _ex2._gated("run_command",
                          {"command": "Get-ChildItem C:"}) is False,
              "the approved command prompts again, so the grant is useless")
        # NOT "another safe command is gated": a harmless command is never
        # gated whatever the grants, because the gate reads the CONTENT. What
        # the grant must not do is cover a command the gate WOULD have asked
        # about -- which is the destructive set asserted below.
        check("a harmless command is not gated either way",
              _ex2._gated("run_command",
                          {"command": "Get-Command ffmpeg"}) is False,
              "a safe command is being gated")
        for _danger in ("Remove-Item C:\\x -Recurse -Force", "format C:",
                        "rm -rf /tmp/x"):
            check(f"and a destructive one is still gated: {_danger!r}",
                  _ex2._gated("run_command", {"command": _danger}) is True,
                  "the destructive command was released by a name-keyed grant")
        policy.clear_session()

        # ---- an approval id is not reusable across a restart ----------------
        # The checks above all pass with an id that repeats after every
        # restart, because they only ever look inside one process. A restart is
        # two executors, and the id has to differ between them or a card from
        # before the restart addresses a request it never saw.
        from backend.actions.executor import ActionExecutor as _AE
        _old, _new = _AE(), _AE()
        _old_ids = {_old._new_approval_id() for _ in range(3)}
        _new_ids = {_new._new_approval_id() for _ in range(3)}
        check("approval ids do not repeat across a restart",
              not (_old_ids & _new_ids),
              f"a fresh process reissued {_old_ids & _new_ids}")

        # A stale id must be UNKNOWN, not "some other live request".
        _stale = sorted(_old_ids)[0]
        _kept = _new._new_approval_id()
        from backend.actions.executor import ActionRequest as _AR
        _new._pending_approvals[_kept] = _AR(
            action_type="run_command", params={"command": "echo hi"})
        _res = asyncio.run(_new.approve(_stale))
        check("a stale approval id resolves to nothing",
              _res.success is False and "No pending approval" in (_res.error or ""),
              f"success={_res.success} error={_res.error}")
        check("and it did not consume the live request",
              _kept in _new._pending_approvals,
              "a stale id released a request it did not name")
        _new._pending_approvals.clear()

        # ---- the executor consults the policy ------------------------------
        from backend.actions.executor import executor
        policy.clear()
        policy.clear_session()
        check("an ungranted action is not treated as granted",
              executor._is_granted("delete_file", {}) is False,
              "delete_file reported as granted")

        # A content-classified action is reachable through the SESSION grant,
        # which is the whole point of having one: without it `run_command`
        # would prompt on every single call and the user would learn to approve
        # without reading.
        policy.allow_for_session(policy.SKILL, "run_command")
        check("a session grant is honoured by the executor",
              executor._is_granted("run_command", {"command": "echo hi"})
              is True,
              "the session grant did not reach the gate")
        check("and it does not arrive as a permanent one",
              policy.is_always_allowed(policy.SKILL, "run_command") is False,
              "the session grant was stored")
        policy.clear_session()
        check("and it stops applying once cleared",
              executor._is_granted("run_command", {"command": "echo hi"})
              is False,
              "a cleared session grant still applied")

        policy.always_allow(policy.SKILL, "read_the_news")
        check("a granted skill is treated as granted",
              executor._is_granted("read_the_news", {}) is True,
              "a granted skill was not recognised")

        # A destructive command must not become grantable by being passed as a
        # plain action name either — the executor repeats the guard.
        policy.always_allow(policy.SKILL, "rm")
        check("a destructive name is refused through the executor too",
              executor._is_granted("rm", {"command": "rm -rf /"}) is False,
              "a destructive command was treated as granted")

        # ---- the announcement says what it is -------------------------------
        from backend.actions import approval_notice
        # An inherent-danger action IS grantable now — the card should offer
        # the switch rather than a control that cannot work.
        check("an inherent-danger action is announced as grantable",
              approval_notice._grantable("delete_file") is True,
              "delete_file was announced as not grantable")
        # A content-classified one is not, but must be offered the session
        # control instead, or the card has no usable answer for it at all.
        check("a content-classified action is not announced as permanent",
              approval_notice._grantable("run_command") is False,
              "run_command was announced as permanently grantable")
        check("but it is announced as session-grantable",
              approval_notice._session_grantable("run_command") is True,
              "run_command would have no usable control on the card")
        check("a plain unknown action is not grantable",
              approval_notice._grantable("some_unknown_thing") is False,
              "an unknown action was announced as grantable")
        check("and it is not offered a session grant either",
              approval_notice._session_grantable("some_unknown_thing") is False,
              "an unknown action was offered a session grant")

        # Every action the gate really gates must offer SOMETHING that
        # remembers the answer. `close_app` is the case that was broken: it is a
        # bare executor handler rather than a registered skill, so it classified
        # as `action`, and the card offered neither switch — leaving "Allow
        # once" and Deny as the only answers to a question the user will be
        # asked again on the next call. The kind the dashboard sends back has to
        # be one the policy accepts, or the button that IS shown would fail.
        from backend.safety.destruction_gate import DESTRUCTIVE_ACTIONS
        from backend.approvals import policy as _policy
        for _action in sorted(DESTRUCTIVE_ACTIONS):
            g = approval_notice._grantable(_action)
            s = approval_notice._session_grantable(_action)
            check(f"'{_action}' offers a memory control",
                  g or s,
                  f"neither permanent nor session grant offered for {_action}")
            if s:
                _kind = approval_notice._kind_for(_action)
                check(f"'{_action}' session answer is storable",
                      _policy._valid_kind(_kind) is not None,
                      f"card would send kind {_kind!r}, which the policy rejects")
                check(f"'{_action}' announcement kind is storable",
                      _policy._valid_kind(approval_notice._kind_for(_action))
                      is not None,
                      "the announced kind is one the policy would refuse")

        # ---- a stale announcement is not restored as a live button ---------
        # A request the executor has already dropped (timed out, or answered
        # from another surface) must not come back, or the restored card offers
        # a button whose only possible answer is "no such approval".
        approval_notice._pending.clear()
        approval_notice.publish("appr_stale", "delete_file", {"path": "x"})
        approval_notice.publish("appr_live", "write_file", {"path": "y"})

        real_pending = None
        try:
            from backend.actions.executor import executor
            real_pending = executor.pending_approvals
            executor.pending_approvals = lambda: [
                {"approval_id": "appr_live", "action_type": "write_file",
                 "params": {}}]
            live = {p["approval_id"] for p in approval_notice.pending()}
            check("an answer the executor no longer holds is not restored",
                  "appr_stale" not in live, str(live))
            check("a request it still holds is",
                  "appr_live" in live, str(live))
        finally:
            if real_pending is not None:
                from backend.actions.executor import executor
                executor.pending_approvals = real_pending
            approval_notice._pending.clear()

        # And if the executor cannot be asked at all, the list is left alone
        # rather than emptied — the failure direction is "keep showing it".
        approval_notice.publish("appr_keep", "write_file", {"path": "z"})
        real_import = approval_notice._live_ids
        approval_notice._live_ids = lambda: approval_notice._ALL
        try:
            kept = {p["approval_id"] for p in approval_notice.pending()}
            check("an unreachable executor does not empty the list",
                  "appr_keep" in kept, str(kept))
        finally:
            approval_notice._live_ids = real_import
            approval_notice._pending.clear()

        # ------------------------------------------------- the prompt's truth
        # The model cannot see the approval queue, so on 2026-10-10 it invented
        # one: "two commands are still waiting on your approval" when nothing
        # had run and nothing was pending, sending the user to look for a card
        # that did not exist. The turn now carries the real state, and a count
        # of zero is stated as zero so the fabricated queue contradicts the
        # prompt it is answering under.

        from backend import ws_server

        # The note has to actually reach the prompt. A correct function nobody
        # calls is the failure mode this guards against - the same shape as a
        # guard that is defined and never consulted.
        _ws_src = open(os.path.join(ROOT, "backend", "ws_server.py"),
                       encoding="utf-8", errors="replace").read()
        check("the approval state is wired into the turn's prompt",
              "sys_prompt = sys_prompt + _approval_state_note(params)" in _ws_src,
              "the note is built but never added to the prompt - it would "
              "change nothing the model sees")

        empty = ws_server._approval_state_note({"source": "chat",
                                                "conversation": "conv_probe_none"})
        check("with nothing pending, the prompt says nothing is waiting",
              "Nothing is waiting" in empty,
              f"got {empty!r} — a false 'still pending' claim would go uncorrected")
        check("and it forbids claiming a queue",
              "Do not tell the user a command is queued" in empty,
              f"got {empty!r}")

        # With something really queued, the note names it — so a genuine wait is
        # reported rather than denied, which is the other direction of the same
        # fact and just as important.
        from backend.approvals import pending as _pend
        _real_for_conv = _pend.for_conversation
        _pend.for_conversation = lambda source, conversation: [{
            "approval_id": "appr_probe_queue", "action_type": "run_command",
            "kind": "tool", "grantable": False,
            "command": "Remove-Item -Recurse -Force ./build",
        }]
        try:
            queued = ws_server._approval_state_note(
                {"source": "chat", "conversation": "conv_probe_q"})
        finally:
            _pend.for_conversation = _real_for_conv
        check("with something pending, the prompt names it",
              "Remove-Item" in queued and "NOT run" in queued,
              f"got {queued!r}")
        check("and a real queue is not denied",
              "Nothing is waiting" not in queued,
              f"got {queued!r}")

        # The note must never cost the turn: an unreadable queue yields "".
        _real2 = _pend.for_conversation

        def _boom(source, conversation):
            raise RuntimeError("queue unreadable")

        _pend.for_conversation = _boom
        try:
            broken = ws_server._approval_state_note(
                {"source": "chat", "conversation": "conv_probe_q2"})
        finally:
            _pend.for_conversation = _real2
        check("an unreadable approval queue does not break the turn",
              broken == "", f"got {broken!r}")
        check("and an unreadable queue is NOT reported as 'nothing pending'",
              "Nothing is waiting" not in broken,
              f"got {broken!r} — unknown state must not be stated as empty")
    finally:
        # Put the real list back, whatever the checks did to it.
        policy.clear()
        if saved is not None:
            config.set("safety", "always_allow", value=saved)
        else:
            config.set("safety", "always_allow",
                       value={"skills": [], "tools": []})

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: approvals — standing permission is granted and revoked, "
          "inherent-danger actions are remembered, content-classified ones are "
          "session-only and never written to disk")
    return 0

if __name__ == "__main__":
    sys.exit(main())
