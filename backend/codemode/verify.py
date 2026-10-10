"""
Verification — the "is it actually done" gate for a change.

Why this exists
---------------
Every agent loop in Addled ends when the model stops calling tools. That is a
guess: the model may have finished, or it may have given up, and nothing tells
the two apart. Codex and Claude Code close that gap by making "done" mean
something checkable — the tests pass, the build succeeds, the linter is clean.

This module finds the check a project already has and runs it, so a code change
can be reported as *verified* or *unverified* rather than merely *finished*.

How the check is chosen
-----------------------
Nothing is invented. The command comes from the project, in this order:

1. an explicit override the user configured (`code.verify_command`),
2. a `verify` / `test` script in `package.json`,
3. a `Makefile` `test:` target,
4. the test runner the repo's own layout implies — pytest, unittest, `cargo
   test`, `go test`, `dotnet test`, `npm test` — but only when the files that
   tool needs are actually present.

If none is found the result says so plainly. "No check is configured" is honest
and useful; a fabricated one that always passes is neither.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger("addled.verify")

# A verification run is bounded: a hung test suite must not hang a code turn.
DEFAULT_TIMEOUT_S = 300
OUTPUT_CAP = 20_000

def detect_command(root: str | Path) -> dict:
    """Find the project's own verification command. Never invents one.

    Returns {"command", "kind", "reason"} — `command` is "" when nothing was
    found, and `reason` says what was looked for.
    """
    root = Path(root)
    if not root.is_dir():
        return {"command": "", "kind": "", "reason": "Not a directory."}

    # 1. Explicit override wins.
    try:
        from backend.config import config
        override = str(config.get("code", "verify_command", default="") or "").strip()
        if override:
            return {"command": override, "kind": "configured",
                    "reason": "Set in Settings."}
    except Exception:  # noqa: BLE001
        pass

    # 2. package.json scripts.
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = data.get("scripts") or {}
            for key in ("verify", "test"):
                if isinstance(scripts.get(key), str) and scripts[key].strip():
                    return {"command": f"npm run {key}", "kind": "npm",
                            "reason": f"package.json has a \"{key}\" script."}
        except (OSError, json.JSONDecodeError) as e:
            log.debug("could not read package.json: %s", e)

    # 3. Makefile target.
    makefile = root / "Makefile"
    if makefile.is_file():
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^verify\s*:", text, re.M):
                return {"command": "make verify", "kind": "make",
                        "reason": "Makefile has a `verify` target."}
            if re.search(r"^test\s*:", text, re.M):
                return {"command": "make test", "kind": "make",
                        "reason": "Makefile has a `test` target."}
        except OSError as e:
            log.debug("could not read Makefile: %s", e)

    # 4. Language-native runners, only when the files they need are present.
    #
    # Quote the interpreter when its path has a space: this command is run
    # through `powershell -Command` on Windows, which splits unquoted paths at
    # the space -- so `C:\Program Files\...\python.exe -m pytest` ran as the
    # command `C:\Program`, collected nothing, and surfaced as "failed with
    # exit code 1 (no counts parsed)" rather than as the quoting bug it was.
    _py = f'"{sys.executable}"' if " " in str(sys.executable) else str(sys.executable)
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() \
            or (root / "tox.ini").is_file() or (root / "tests").is_dir() \
            or any(root.glob("test_*.py")):
        if _python_has_pytest():
            return {"command": f"{_py} -m pytest -q", "kind": "pytest",
                    "reason": "A Python project with pytest available."}
        if (root / "tests").is_dir() or any(root.glob("test_*.py")):
            return {"command": f"{_py} -m unittest discover -q",
                    "kind": "unittest",
                    "reason": "A Python project with a tests/ layout."}

    if (root / "Cargo.toml").is_file():
        return {"command": "cargo test", "kind": "cargo",
                "reason": "Cargo.toml present."}
    if (root / "go.mod").is_file():
        return {"command": "go test ./...", "kind": "go",
                "reason": "go.mod present."}
    if any(root.glob("*.csproj")) or any(root.glob("*.sln")):
        return {"command": "dotnet test", "kind": "dotnet",
                "reason": "A .NET project."}

    return {"command": "", "kind": "",
            "reason": ("No test command was found — no verify/test script in "
                       "package.json, no Makefile target, and no test runner "
                       "for the files present.")}

def _python_has_pytest() -> bool:
    import importlib.util
    return importlib.util.find_spec("pytest") is not None

def _summarise(output: str) -> dict:
    """Pull the pass/fail counts out of common runner output.

    Best-effort: this improves the message, it never decides success. The exit
    code is what decides.
    """
    summary: dict = {}
    # pytest: "3 failed, 12 passed in 0.4s"
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        summary["passed"] = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        summary["failed"] = int(m.group(1))
    m = re.search(r"(\d+)\s+error", output)
    if m:
        summary["errors"] = int(m.group(1))
    # unittest: "Ran 5 tests ... FAILED (failures=2)"
    m = re.search(r"Ran (\d+) test", output)
    if m and "ran" not in summary:
        summary["total"] = int(m.group(1))
    # cargo: "test result: FAILED. 2 passed; 1 failed"
    m = re.search(r"test result: \w+\.\s*(\d+) passed;\s*(\d+) failed", output)
    if m:
        summary["passed"] = int(m.group(1))
        summary["failed"] = int(m.group(2))
    return summary

async def run_verification(root: str | Path, command: str = "",
                           timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Run the project's check. Returns the outcome, never raises.

    `ok` is the exit code's verdict and nothing else — a summary that says
    "12 passed" is a detail, not the answer, because a suite can pass and the
    process still fail (a teardown error, a plugin crash).
    """
    root = Path(root)
    if not command:
        detected = detect_command(root)
        command = detected.get("command", "")
        kind = detected.get("kind", "")
        reason = detected.get("reason", "")
    else:
        kind, reason = "configured", "Passed explicitly."

    if not command:
        return {"ok": False, "ran": False, "command": "", "kind": "",
                "reason": reason, "output": "", "summary": {}}

    # A caller-supplied command is still the user's machine. `verify_code`
    # launches its own process rather than going through `run_command`, so the
    # destruction gate that guards a shell command does not see this one — and
    # `-ExecutionPolicy Bypass` makes it worse. A verification step is for
    # running a test suite, not for `format` or `rmdir /s`, so anything the gate
    # would ask about is refused here rather than executed unattended.
    try:
        from backend.safety.destruction_gate import DestructionGate
        if DestructionGate().requires_approval("run_command",
                                               {"command": command}):
            return {"ok": False, "ran": False, "command": command,
                    "kind": kind, "reason": (
                        "This command is on the destructive list, so it will "
                        "not run as a verification step. Run it yourself, or "
                        "ask through run_command where it can be approved."),
                    "output": "", "summary": {}}
    except Exception as e:  # noqa: BLE001
        # If the gate cannot be consulted, refuse rather than run blind — the
        # whole point of the check is that it is the safe path.
        log.debug("could not consult the destruction gate: %s", e)
        return {"ok": False, "ran": False, "command": command, "kind": kind,
                "reason": (f"Could not check whether this command is safe "
                           f"({e}), so it was not run."),
                "output": "", "summary": {}}

    log.info("Verifying with: %s (in %s)", command, root)
    try:
        if sys.platform == "win32":
            # PowerShell parses a leading quoted string as a LITERAL, not a
            # command, so `"C:\Program Files\...\python.exe" -m pytest` is a
            # syntax error at the quote. The call operator `&` makes it a
            # command. Applied here, not at the templates, because the command
            # can also arrive from config or auto-detection and every path
            # reaches Windows through this one spawn.
            ps_command = command
            if ps_command[:1] == '"':
                ps_command = "& " + ps_command
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoLogo", "-NoProfile",
                "-ExecutionPolicy", "Bypass", "-Command", ps_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(root))
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(root))
        try:
            out_b, _ = await asyncio.wait_for(proc.communicate(),
                                              timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"ok": False, "ran": True, "command": command, "kind": kind,
                    "reason": f"The check did not finish within {timeout}s.",
                    "output": "", "summary": {}, "timedOut": True}
    except (FileNotFoundError, OSError) as e:
        return {"ok": False, "ran": False, "command": command, "kind": kind,
                "reason": f"Could not run the check: {e}", "output": "",
                "summary": {}}

    output = out_b.decode("utf-8", errors="replace")
    ok = proc.returncode == 0
    summary = _summarise(output)
    return {
        "ok": ok,
        "ran": True,
        "command": command,
        "kind": kind,
        "reason": reason,
        "exit_code": proc.returncode,
        "output": output[:OUTPUT_CAP],
        "truncated": len(output) > OUTPUT_CAP,
        "summary": summary,
        # A run that collected nothing exits non-zero, exactly like a run whose
        # tests failed — and the two are not the same fact. Reported separately
        # so a caller can say "no tests were found" instead of implying the code
        # is broken, which is the worst possible confusion for a check whose
        # whole job is telling those apart.
        "noTests": (not ok) and _found_no_tests(output, summary),
        # A third case, distinct from both: the runner died before it could
        # collect anything. A user whose pytest has a broken plugin should not
        # be told their code failed the tests.
        "startFailed": (not ok) and _found_start_failure(output, summary),
    }

def _found_no_tests(output: str, summary: dict) -> bool:
    """Whether a non-zero run collected zero tests rather than failing them.

    Only ever consulted for a run that already failed, so a genuine pass can
    never be relabelled. Names the specific phrasings the common runners use,
    because guessing from "summary is empty" alone would also match a runner
    whose output this parser simply does not understand.
    """
    if any(summary.get(k) for k in ("passed", "failed", "errors")):
        return False
    if summary.get("total") == 0:
        return True
    text = output.lower()
    markers = (
        "no tests ran",            # pytest
        "ran 0 tests",             # unittest
        "no tests found",          # jest / vitest / go
        "no test files found",     # vitest
        "0 tests",                 # generic
        "collected 0 items",       # pytest -q
    )
    return any(m in text for m in markers)

def _found_start_failure(output: str, summary: dict) -> bool:
    """Whether the runner crashed before running any test.

    A traceback with no counts and no test-failure markers is a start-up crash
    (a missing plugin, an import error) -- not a verdict on the code under
    test. Requires an explicit traceback so a runner whose output this parser
    simply does not understand is not mislabelled as broken.
    """
    if any(summary.get(k) for k in ("passed", "failed", "errors", "total")):
        return False
    text = output or ""
    if "Traceback (most recent call last)" not in text:
        return False
    lower = text.lower()
    # If it collected anything at all, a crash during the run is a real result.
    if any(m in lower for m in ("collected ", "passed", "failed", "error")):
        # `error` alone is too loose -- pytest prints "errors" in results. Only
        # treat it as a result when it appears with a number.
        import re as _re
        if _re.search(r"\b\d+ (passed|failed|error|failed,)", lower):
            return False
    return ("modulenotfounderror" in lower or "importerror" in lower
            or "attributeerror" in lower or "cannot import" in lower)


def verdict_line(result: dict) -> str:
    """One line a user or a model can read at a glance."""
    if not result.get("ran"):
        return f"Not verified — {result.get('reason') or 'no check available.'}"
    summary = result.get("summary") or {}
    counts = ", ".join(f"{v} {k}" for k, v in summary.items()) \
        if summary else "no counts parsed"
    if result.get("ok"):
        return f"Verified — `{result['command']}` passed ({counts})."
    if result.get("timedOut"):
        return f"Not verified — `{result['command']}` timed out."
    # Distinct from a failure, and the distinction is the point: a project whose
    # tests do not run is not a project whose tests fail.
    if result.get("noTests"):
        return (f"Not verified — `{result['command']}` ran but found no tests. "
                f"It exits non-zero when it collects nothing, so this is not a "
                f"test failure.")
    if result.get("startFailed"):
        return (f"Not verified — `{result['command']}` could not start, so it "
                f"ran no tests. The runner itself failed to load; this is not a "
                f"result about the code.")
    return (f"Not verified — `{result['command']}` failed with exit code "
            f"{result.get('exit_code')} ({counts}).")
