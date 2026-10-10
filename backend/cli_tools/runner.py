"""Run a CLI tool and turn what it printed into a skill result.

Three properties this module exists to guarantee, in the order they matter:

1. **Nothing is ever passed through a shell.** The argv is a list, built by
   `CliToolSpec.argv_for`, and handed to `subprocess.run(shell=False)`. A tool
   that takes a URL, a filename or a sentence from the model cannot turn any of
   them into a second command, because there is no shell to interpret them.

2. **A hanging tool cannot hang Addled.** Every run has a timeout, and the
   process is killed on expiry. A local model with no network will sit on a
   socket until the OS gives up; a tool that does the same would freeze the chat
   turn that called it with no way out.

3. **Output is bounded before it reaches a model.** A tool that dumps 40 MB of
   text would be pasted into a prompt. The caps mirror `actions/terminal.py` so
   a log line means the same thing in both places.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from backend.cli_tools.spec import CliToolSpec

log = logging.getLogger("addled.cli_tools")

# Same numbers as the shell action's, deliberately: a truncated tool result and
# a truncated command result should look alike, and the person reading a log
# should not have to remember which subsystem used which cap.
MAX_STDOUT = 50 * 1024
MAX_STDERR = 10 * 1024


def _configured_timeout() -> int:
    """The user's ceiling on how long any tool may run, or 0 for none.

    Read here rather than passed in so there is one place the setting is
    honoured: every caller of `run_cli` — the skill adapter, the builder's smoke
    test, the Settings page's Test button — gets the same limit without having
    to remember it. A setting that only some call paths respected would be worse
    than none, since it would look enforced and not be.
    """
    try:
        from backend.config import config
        return max(0, int(config.get("cli_tools", "timeout_s", default=0) or 0))
    except Exception:  # noqa: BLE001
        return 0


def _subprocess_env() -> dict:
    """The environment a tool runs in.

    Inherited, minus the data-directory override: a tool that happens to import
    `backend.app_paths` must not resolve the user's real data directory when it
    was only meant to read the argument it was given. `PYTHONUNBUFFERED` keeps a
    tool that prints before it finishes from having its output held in a pipe
    buffer that the timeout then discards.
    """
    import os
    env = dict(os.environ)
    env.pop("ADDLED_DATA_DIR", None)
    env["PYTHONUNBUFFERED"] = "1"
    # Nothing a tool does should be able to reach the app's own interpreter
    # configuration files.
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _run_sync(argv: list[str], cwd: Path, timeout: int) -> dict:
    """The blocking half, run in a worker thread."""
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            shell=False,          # explicit: the whole safety story rests on it
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return {"success": False,
                "error": f"the tool did not finish within {timeout}s and was stopped"}
    except FileNotFoundError as exc:
        # The interpreter named in `entry` is missing, which is a setup fault
        # rather than a tool fault — say which one so it can be fixed.
        return {"success": False,
                "error": f"could not start the tool: {exc}"}
    except OSError as exc:
        return {"success": False, "error": f"could not run the tool: {exc}"}

    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    truncated_out = len(out) > MAX_STDOUT
    truncated_err = len(err) > MAX_STDERR
    out = out[:MAX_STDOUT]
    err = err[:MAX_STDERR]

    if proc.returncode != 0:
        return {
            "success": False,
            "error": (err.strip() or
                      f"the tool exited with code {proc.returncode}"),
            "stdout": out,
            "exit_code": proc.returncode,
        }

    # A tool that prints JSON gives the model a structure to reason about; one
    # that prints prose is still perfectly usable. `parsed` says which happened
    # so a caller can tell "empty result" from "we could not read it".
    data: dict
    text = out.strip()
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
                data.setdefault("success", True)
            else:
                data = {"success": True, "result": parsed}
            data["parsed"] = True
        except (ValueError, TypeError):
            data = {"success": True, "stdout": out, "parsed": False}
    else:
        data = {"success": True, "stdout": out, "parsed": False}

    if err.strip():
        data.setdefault("stderr", err)
    if truncated_out:
        data["truncated"] = True
    if truncated_err:
        data["stderr_truncated"] = True
    return data


async def run_cli(spec: CliToolSpec, source_dir: Path, params: dict,
                  *, argv_override: list[str] | None = None) -> dict:
    """Execute one tool. Always returns a dict; never raises for a tool fault.

    A tool failing is a normal outcome the model needs to be told about, not an
    exception that unwinds a chat turn — so every failure is a returned
    `{"success": False, "error": ...}`.
    """
    if argv_override is not None:
        argv = list(argv_override)
        warnings: list[str] = []
    else:
        argv, warnings = spec.argv_for(params or {})

    if not argv:
        return {"success": False,
                "error": "nothing to run — the call did not produce any arguments"}

    # The interpreter named in `entry` is resolved here rather than shelled out
    # to PATH: on Windows a bare `python` in a fresh process is as likely to be
    # the Microsoft Store stub (which opens the Store and exits 9009) as the
    # bundled interpreter.
    entry = [str(p) for p in (spec.entry or ["python", "tool.py"])]
    if entry and entry[0].lower() in ("python", "python3", "py"):
        import sys
        entry[0] = sys.executable
    if len(entry) > 1 and not Path(entry[1]).is_absolute():
        entry[1] = str(source_dir / entry[1])

    # `entry` launches the program; `argv` is what the program is told. They are
    # one command line, and passing only `entry` ran every tool with no
    # arguments at all — argparse then reported its own "--name is required",
    # which reads as the model sending nothing rather than as the runner
    # dropping what it sent.
    command = entry + list(argv)

    # The spec's own timeout wins, because a tool that knows it needs five
    # minutes should get them. `cli_tools.timeout_s` is a ceiling for tools that
    # did not say — a single knob for "nothing runs longer than this", which is
    # what a user setting it is asking for. Taking the minimum means the setting
    # can only ever shorten, never silently extend a tool past its own limit.
    ceiling = _configured_timeout()
    timeout = int(spec.timeout_s or 60)
    if ceiling:
        timeout = min(timeout, ceiling)

    log.info("CLI tool %s: %s", spec.slug, " ".join(command))
    result = await asyncio.get_running_loop().run_in_executor(
        None, _run_sync, command, source_dir, timeout)
    return result
