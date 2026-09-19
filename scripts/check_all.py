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
    "check_web_search.py",
    "check_skill_market.py",
    "check_mcp_market.py",
    "check_code_editor.py",
    "check_guide.py",
    "check_parity.py",
    "check_chat_scope.py",
    "check_reply_language.py",
    "check_packaging.py",
    "check_rtk.py",
    "check_uv.py",
    "check_secrets.py",
]


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
        print(f"{status}  {name:<22} {verdict}")
        if proc.returncode != 0:
            failed.append(name)
            tail = (proc.stdout or "") + (proc.stderr or "")
            for line in tail.splitlines()[-8:]:
                print(f"        {line}")

    print()
    if failed:
        print(f"{len(failed)} suite(s) failed: {', '.join(failed)}")
        return 1
    print(f"All {len(SUITES)} suites passed.")
    return 0


sys.exit(main())
