"""Make a CLI tool callable as a skill.

The whole integration is this file. A CLI tool is registered in the same
`SkillRegistry` every other capability lives in, under the `cli` category, so
chat, the Code page, swarm agents, the bots and voice all reach it with no
further plumbing — the same approach `mcp_client/manager.py` takes for MCP
tools, and for the same reason: there is one catalogue, and a second one would
be a second set of bugs.

The handler is deliberately thin. Everything that could go wrong — argument
rendering, the shell-free subprocess, the timeout, the output cap — belongs to
`runner`, and everything about who may call it and when belongs to
`SkillRegistry.execute`. This layer only connects the two.
"""

from __future__ import annotations

import logging

from backend.cli_tools.runner import run_cli
from backend.cli_tools.spec import CliToolSpec
from backend.skills.registry import SkillDefinition

log = logging.getLogger("addled.cli_tools")


def as_skill(spec: CliToolSpec, source_dir) -> SkillDefinition:
    """Build the `SkillDefinition` for one CLI tool.

    `aliases` is inverted here rather than at the call site: the manifest keeps
    `{real: alias}` because that is how it reads, and `SkillDefinition` wants
    `{real: (alias, ...)}` because a parameter can have several spellings. Doing
    the conversion once, here, is why neither side has to know the other's
    shape.
    """
    spec_aliases = dict(spec.aliases or {})
    skill_aliases: dict[str, tuple[str, ...]] = {}
    for real, alias in spec_aliases.items():
        if not alias:
            continue
        skill_aliases.setdefault(real, ())
        skill_aliases[real] = tuple(
            dict.fromkeys(skill_aliases[real] + (str(alias),)))

    async def handler(params: dict) -> dict:
        result = await run_cli(spec, source_dir, params or {})
        # Stats are recorded here rather than in the runner, so a call made
        # directly against a spec (a smoke test, a check suite) does not inflate
        # the count the user reads on the Settings page. Only calls that went
        # through the skill registry are real uses.
        try:
            from backend.cli_tools.registry import cli_tools
            cli_tools.bump_run(spec.slug, bool(result.get("success")),
                               result.get("error"))
        except Exception:  # noqa: BLE001
            pass
        return result

    return SkillDefinition(
        name=spec.name,
        description=spec.description,
        parameters=spec.params or {"type": "object", "properties": {}},
        handler=handler,
        category=spec.category or "cli",
        requires_approval=bool(spec.requires_approval),
        aliases=skill_aliases,
    )
