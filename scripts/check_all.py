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
    "check_voice.py",
    "check_links.py",
    "check_wiki.py",
    "check_workspace.py",
    "check_sop.py",
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
    "check_reply_language.py",
    "check_packaging.py",
    "check_rtk.py",
    "check_uv.py",
    "check_secrets.py",
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


def main() -> int:
    python = sys.executable
    failed = []
    for name in SUITES:
        path = os.path.join(ROOT, "scripts", name)
        if not os.path.exists(path):
            print(f"missing  {name}")
            failed.append(name)
            continue
        proc = subprocess.run([python, "-s", path], cwd=ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        verdict = ""
        for line in (proc.stdout or "").splitlines():
            if line.startswith(("PASS:", "FAIL:")):
                verdict = line
        if not verdict:
            verdict = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
            verdict = verdict[0] if isinstance(verdict, list) else verdict
        status = "ok  " if proc.returncode == 0 else "FAIL"
        # A suite's verdict is its own sentence and may contain a character the
        # console's code page cannot encode (an em-dash, a check mark). Printing
        # it raw crashed the whole runner on cp1252, which read as "the suite
        # failed" when it had passed. Down-convert, never raise.
        print(f"{status}  {name:<22} {_safe(verdict)}")
        if proc.returncode != 0:
            failed.append(name)
            tail = (proc.stdout or "") + (proc.stderr or "")
            for line in tail.splitlines()[-8:]:
                # Same down-conversion as the verdict line. A traceback or log
                # line can carry any character at all, and printing one raw
                # killed the runner mid-report — so the user saw a Python
                # encoding crash instead of the list of failing suites.
                print(f"        {_safe(line)}")

    print()
    if failed:
        print(f"{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print(f"All {len(SUITES)} suites passed.")
    return 0


sys.exit(main())
