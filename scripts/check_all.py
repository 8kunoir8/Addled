"""Run every check suite and report by exit code.

Exit codes rather than output text, because the suites do not print verdicts in
a consistent shape — `verify_python.py` says "All Python files clean" and
`check_mcp.py` writes an MCP error line before its verdict. Grepping for PASS
once made a suite that had crashed look fine.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_all.py
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUITES = [
    "verify_python.py",
    "check_tools.py",
    "check_tool_calls.py",
    "check_voice.py",
    "check_links.py",
    "check_wiki.py",
    "check_workspace.py",
    "check_sop.py",
    "check_sop_scoring.py",
    "check_sop_store.py",
    "check_sop_wiring.py",
    "check_sop_live_defects.py",
    "check_reachability.py",
    "check_meetings.py",
    "check_meeting_summarise.py",
    "check_meeting_recall.py",
    "check_system_role.py",
    "check_console.py",
    "check_meeting_actions.py",
    "check_remote.py",
    "check_remote_gateway.py",
    "check_remote_policy.py",
    "check_tailscale.py",
    "check_tailscale_install.py",
    "check_wiring.py",
    "check_routing.py",
    "check_mcp.py",
    "check_ws_methods.py",
    "check_bot_chat.py",
    "check_bot_mirroring.py",
    "check_bot_approval.py",
    "check_web_search.py",
    "check_skill_market.py",
    "check_skill_grants.py",
    "check_swarm_memory.py",
    "check_goal_swarm.py",
    "check_goal_detail.py",
    "check_model_api.py",
    "check_email_skills.py",
    "check_mcp_market.py",
    "check_code_editor.py",
    "check_code_page.py",
    "check_code_learning.py",
    "check_dashboard_server.py",
    "check_bot_media.py",
    "check_vision_deps.py",
    "check_vision_feedback.py",
    "check_ask_user.py",
    "check_decision_surfaces.py",
    "check_bubble_state.py",
    "check_open_question.py",
    "check_data_dir.py",
    "check_vision_install.py",
    "check_browser_autoinstall.py",
    "check_swarm_delegate.py",
    "check_swarm_naming.py",
    "check_tool_brief.py",
    "check_anchored.py",
    "check_gitops.py",
    "check_session.py",
    "check_verify.py",
    "check_selfmod.py",
    "check_swarm_roster.py",
    "check_wizard.py",
    "check_browser_install.py",
    "check_approvals.py",
    "check_bugfixes.py",
    "check_guide.py",
    "check_parity.py",
    "check_chat_scope.py",
    "check_memory_rag.py",
    "check_local_llm_flags.py",
    "check_gaming_pause.py",
    "check_approval_sync.py",
    "check_agent_resumes.py",
    "check_acquire_asks.py",
    "check_forge_install.py",
    "check_forge_validation.py",
    "check_param_inference.py",
    "check_thinking_reply.py",
    "check_pylibs_precedence.py",
    "check_reply_language.py",
    "check_packaging.py",
    "check_rtk.py",
    "check_uv.py",
    "check_secrets.py",
    "check_command_gate.py",
    "check_bot_qr.py",
    "check_bot_messaging.py",
    "check_office.py",
    "check_cli_tools.py",
    "check_cli_priority.py",
    "check_cli_needs_tool.py",
    "check_cli_build.py",
    "check_cli_page.py",
    "check_cli_consumers.py",
    "check_emotions.py",
    "check_trigger.py",
    "check_motion.py",
    "check_shapes.py",
    "check_hands.py",
    "check_streaming.py",
    "check_wait_notice.py",
    "check_skill_bodies.py",
    "check_review.py",
    "check_verify_gate.py",
    "check_plan_mode.py",
    "check_gate_hygiene.py",
    "check_screen_privacy.py",
]


def _safe(text: str) -> str:
    """Text that the console can always print.

    A verdict line comes from a child process and is arbitrary; on a Windows
    console that is cp1252, an em-dash is unencodable and `print` raises. That
    turned a passing suite into a runner crash. Encode to the console's code
    page and replace what it cannot hold.
    """
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(enc, errors="replace").decode(enc, errors="replace")
    except (LookupError, UnicodeError):
        return text.encode("ascii", errors="replace").decode("ascii")


def _needs_real_env(path: str) -> bool:
    """Whether this suite declares that it needs the app's real environment.

    Read from the file rather than a list here, so a suite that changes what it
    measures carries its own requirement instead of depending on someone
    remembering to update the runner.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return False
    return "NEEDS_REAL_ENV = True" in head


def main() -> int:
    python = sys.executable
    failed = []
    skipped = []
    for name in SUITES:
        path = os.path.join(ROOT, "scripts", name)
        if not os.path.exists(path):
            print(f"missing  {name}")
            failed.append(name)
            continue
        # A suite that loads an optional dependency must run with the same
        # environment the app uses. `-s` suppresses site-packages, so such a
        # check silently measures a Python that cannot import the dependency --
        # this is how an embedder check once reported a hash fallback as the
        # shipped model. Suites opt out with `NEEDS_REAL_ENV = True`; the rest
        # stay hermetic under `-s`.
        flags = [] if _needs_real_env(path) else ["-s"]
        proc = subprocess.run([python, *flags, path], cwd=ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        verdict = ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith(("PASS:", "FAIL:")):
                verdict = line
        if not verdict:
            verdict = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
            verdict = verdict[0] if isinstance(verdict, list) else verdict
        # Exit 2 is a suite's deliberate "cannot run here", not a failure.
        # See check_office (no workspace chosen) and check_code_editor (will not
        # run unless it can avoid writing the live settings file).
        if proc.returncode == 0:
            status = "ok  "
        elif proc.returncode == 2:
            status = "skip"
        else:
            status = "FAIL"
        # A suite's verdict is its own sentence and may contain a character the
        # console's code page cannot encode (an em-dash, a check mark). Printing
        # it raw crashed the whole runner on cp1252, which read as "the suite
        # failed" when it had passed. Down-convert, never raise.
        print(f"{status}  {name:<22} {_safe(verdict)}")
        if proc.returncode == 2:
            skipped.append(name)
        elif proc.returncode != 0:
            failed.append(name)
            tail = (proc.stdout or "") + (proc.stderr or "")
            for line in tail.splitlines()[-8:]:
                # Same down-conversion as the verdict line. A traceback or log
                # line can carry any character at all, and printing one raw
                # killed the runner mid-report — so the user saw a Python
                # encoding crash instead of the list of failing suites.
                print(f"        {_safe(line)}")

    print()
    if skipped:
        # Named, never hidden: a suite that declined to run is not a pass, and
        # it is not a failure either. Saying nothing would make "could not run"
        # look like "ran clean".
        print(f"{len(skipped)} suite(s) skipped (cannot run here): "
              f"{', '.join(skipped)}")
    if failed:
        print(f"{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    if skipped:
        print(f"All {len(SUITES) - len(skipped)} runnable suite(s) passed; "
              f"{len(skipped)} skipped.")
        return 0
    print(f"All {len(SUITES)} suites passed.")
    return 0


sys.exit(main())
