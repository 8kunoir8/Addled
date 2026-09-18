"""Checks for the bot bridge's request budget and compaction yielding.

Two defects met in the same place. The bot bridge capped every request at 30s
while the backend's slowest path takes minutes, so a turn the backend was still
working on was abandoned and its answer thrown away — that reached the user as
"Request chat.send timed out" on the second Telegram message. And the background
compaction job the backend starts after every turn summarizes with the *same*
provider, so on a single-generation local model the user's next message queued
behind a large summarization prompt.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bot_chat.py
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------
# The bot bridge itself (Node)
# --------------------------------------------------------------------------

def run_node_probe() -> None:
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        check("node is available to probe the bot bridge", False, "node not on PATH")
        return
    probe = os.path.join(ROOT, "scripts", "bot_client_probe.js")
    try:
        proc = subprocess.run([node, probe], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=180)
    except subprocess.TimeoutExpired:
        check("bot client probe (node)", False, "probe did not finish in 180s")
        return
    for line in (proc.stdout or "").splitlines():
        print("    " + line)
    if proc.stderr and proc.stderr.strip():
        print("    stderr: " + proc.stderr.strip()[:400])
    check("bot client probe (node)", proc.returncode == 0,
          f"exit {proc.returncode}")


# --------------------------------------------------------------------------
# Compaction yielding to the user (Python)
# --------------------------------------------------------------------------

class _FakeHistory:
    current_conversation_id = "conv_check"

    def __init__(self, count: int) -> None:
        self._messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
            for i in range(count)
        ]

    def get_context(self, max_messages: int = 20) -> list[dict]:
        return self._messages[:max_messages]


class _FakeConfig:
    def __init__(self, threshold: int) -> None:
        self._threshold = threshold

    def get(self, section: str, key: str, default=None):
        if section == "memory" and key == "compaction_threshold":
            return self._threshold
        return default


def run_compaction(compaction, message_count: int, *, activity_age: float,
                   threshold: int = 40, quiet_window: float = 30.0,
                   wait_cap: float = 0.2) -> tuple[bool, list[int]]:
    """Run maybe_compact against fakes. Returns (result, chunk sizes asked for).

    The window and the cap are shortened to fractions of a second so the check
    does not have to wait out the real ones. A long window against a short cap
    is the shape of a live conversation: a turn happened moments ago, so
    compaction stands down instead of waiting for a gap that never comes.
    """
    chunks: list[int] = []

    async def fake_summarize(messages: list[dict]) -> str:
        chunks.append(len(messages))
        return "a summary"

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(compaction, "ROLLING_PATH",
                               Path(tmp) / "rolling_summary.json"), \
             mock.patch("backend.config.config", _FakeConfig(threshold)), \
             mock.patch("backend.memory.chat_history.chat_history",
                        _FakeHistory(message_count)), \
             mock.patch.object(compaction, "_summarize_chunk", fake_summarize), \
             mock.patch.object(compaction, "QUIET_WINDOW_S", quiet_window), \
             mock.patch.object(compaction, "QUIET_WAIT_MAX_S", wait_cap):
            # Age the last activity so "the user is active" is expressed as an
            # age rather than by patching time itself.
            compaction._last_activity = time.time() - activity_age
            result = asyncio.run(compaction.maybe_compact())
    return bool(result), chunks


def run_compaction_checks() -> None:
    from backend.memory import compaction

    before = compaction._last_activity
    compaction.note_activity()
    check("note_activity records a turn",
          compaction._last_activity > before,
          f"{before} -> {compaction._last_activity}")

    # A live conversation: compaction must not spend the model at all.
    result, chunks = run_compaction(compaction, 120, activity_age=0.0)
    check("compaction yields while the user is active",
          result is False and not chunks,
          f"result={result} summarised={len(chunks)}")

    # Quiet: it proceeds, once, with a bounded chunk.
    result, chunks = run_compaction(compaction, 120, activity_age=60.0)
    check("compaction proceeds once the conversation is quiet",
          result is True and len(chunks) == 1,
          f"result={result} chunks={chunks}")

    # A long backlog must be folded in capped pieces, not one huge prompt.
    result, chunks = run_compaction(compaction, 400, activity_age=60.0)
    check("a chunk stays within the cap",
          bool(chunks) and max(chunks) <= compaction.MAX_CHUNK,
          f"chunks={chunks} cap={compaction.MAX_CHUNK}")

    # The chunk cap must actually be tighter than the old "a third of the
    # backlog" rule, or it would not bound anything on a long conversation.
    check("the cap is tighter than a third of a long backlog",
          compaction.MAX_CHUNK < 400 // 3,
          f"cap={compaction.MAX_CHUNK} third={400 // 3}")

    # Below the threshold there is nothing to fold, and no model call is made.
    result, chunks = run_compaction(compaction, 20, activity_age=60.0)
    check("a short conversation is left alone",
          result is False and not chunks,
          f"result={result} summarised={len(chunks)}")

    # The yielding only matters if the chat pipeline is what marks the activity.
    source = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    body = re.search(r"async def run_chat_pipeline\(.*?\n(?=\nasync def |\ndef )",
                     source, re.S)
    check("the chat pipeline marks activity",
          bool(body) and "note_activity" in body.group(0))


def main() -> int:
    run_node_probe()
    run_compaction_checks()
    print()
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s) failed")
        return 1
    print("PASS: bot chat request budget and compaction yielding")
    return 0


if __name__ == "__main__":
    sys.exit(main())
