"""CLI tools the user (or the agent) builds and Addled then uses.

A CLI tool is a self-contained command-line program plus a small manifest. It is
not a chat-turn skill in its own right — it is a program a person can run, debug
and pipe, and the skill layer is a thin adapter that shells out to it:

    backend/cli_tools/registry.py   the manifests, discovery, and matching
    backend/cli_tools/spec.py       the manifest shape and its validation
    backend/cli_tools/runner.py     subprocess execution, output capping
    backend/cli_tools/adapter.py    CliTool -> SkillDefinition (the bridge)
    backend/cli_tools/builder.py    ask a model to write one (draft/apply)

Why a CLI rather than another forged async function. A forged skill is an
in-process coroutine with no user-facing surface: the only way to run it is
through a chat turn, and when it misbehaves the failure is a log line. A CLI has
the same interface for the agent and for the person, so a tool that works from
the terminal is a tool the user can verify before trusting it — which is the
whole point of building one deliberately instead of forging one mid-turn.

Registered tools land in the shared `SkillRegistry` under the `cli` category, so
they reach chat, the Code page, swarm agents, the bots and voice with no extra
plumbing — the same trick `mcp_client/manager.py` uses for MCP tools.
"""

from backend.cli_tools.adapter import as_skill
from backend.cli_tools.builder import BuildResult, apply, draft, smoke_test
from backend.cli_tools.registry import CliTool, CliToolRegistry, cli_tools
from backend.cli_tools.runner import run_cli
from backend.cli_tools.spec import CliToolSpec

__all__ = [
    "CliTool", "CliToolRegistry", "cli_tools", "CliToolSpec",
    "as_skill", "run_cli",
    "BuildResult", "draft", "apply", "smoke_test",
]
