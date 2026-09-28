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
        # These are the names the destruction gate owns. Refusing them is the
        # whole reason this module exists rather than a plain list on a card.
        for guarded in ("delete_file", "close_app", "close_window"):
            r = policy.always_allow(policy.SKILL, guarded)
            check(f"'{guarded}' cannot be granted standing permission",
                  r.get("success") is False and r.get("protected") is True,
                  str(r))
            check(f"and '{guarded}' is not allowed afterwards",
                  policy.is_always_allowed(policy.SKILL, guarded) is False,
                  "the grant went through")

        r = policy.always_allow(policy.SKILL, "rm -rf")
        check("a destructive command cannot be granted",
              r.get("success") is False and r.get("protected") is True, str(r))

        check("the guard reports the names it protects",
              "delete_file" in policy.protected_names(),
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

        # ---- the wait has to outlast the turn that raised it ---------------
        # The card is rendered while a turn is still running, and a local model
        # can spend a minute producing that turn. A wait shorter than the turn
        # means the request expires before the user ever sees it, which is what
        # made the feature look broken.
        from backend.actions import executor as executor_module
        check("the approval wait is long enough for a slow local turn",
              executor_module.APPROVAL_WAIT_S >= 120,
              f"APPROVAL_WAIT_S is {executor_module.APPROVAL_WAIT_S}")

        # ---- the executor consults the policy ------------------------------
        from backend.actions.executor import executor
        policy.clear()
        check("an ungranted action is not treated as granted",
              executor._is_granted("delete_file", {}) is False,
              "delete_file reported as granted")

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
        check("a guarded action is announced as not grantable",
              approval_notice._grantable("delete_file") is False,
              "delete_file was announced as grantable")
        check("a plain unknown action is not grantable",
              approval_notice._grantable("some_unknown_thing") is False,
              "an unknown action was announced as grantable")
        check("nothing is grantable while the policy cannot be read",
              approval_notice._grantable("run_command") is False,
              "run_command was announced as grantable")

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
    print("PASS: approvals — standing permission is granted, revoked, and "
          "refused for anything destructive")
    return 0

if __name__ == "__main__":
    sys.exit(main())
