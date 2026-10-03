"""The one place a turn is told what tools it actually has.

This exists because a persona used to *replace* the system prompt, and the only
text saying "you have real tools, do not claim otherwise" lived on the other
branch. A swarm agent, the code planner and search all pass a persona, so all
of them lost it — and an agent holding a tool schema but no prose that the tools
exist answers "those tools aren't accessible in this environment" and refuses
work it could have done.

Two rules make this worth having as a function rather than a string:

1. It is generated from the tools actually offered. A block that promised
   `run_command` to a desk that was never given it would repeat the original
   bug in the opposite direction: the model would try, fail, and blame itself.

2. The names come from the skill registry, so they cannot go stale. The block
   this replaces was hand-written and had drifted: it named `browser_open`,
   `remember` and `recall`, none of which are skills (`browser_navigate`,
   `memory_set` and `memory_get` are). A model told to call a tool that does
   not exist is being set up to fail.

Nothing here raises. A prompt block is not worth failing a turn over, and the
honest fallback for "I cannot read the registry" is to say less, not to guess.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.tool_brief")

# Which fact belongs to which tool, so the prose is only as long as the
# catalogue warrants. Order is deliberate: the list reads as an inventory of
# what the assistant can do, most-load-bearing first.
#
# Keys must be real skill names, and `scripts/check_tool_brief.py` enforces it.
# That check is the whole reason the stale names above were caught; without it
# this table drifts exactly the way the string it replaces did.
_FACTS: list[tuple[str, str]] = [
    ("run_command",
     "Run Windows PowerShell 5.1 commands on the user's own PC with "
     "`run_command` - read and write files, inspect processes, run "
     "git/pip/npm/build commands. You DO have access to the user's system "
     "through this tool; never tell the user you cannot run commands or lack "
     "system access. Chain commands with ';' (not '&&'): safe ones run "
     "immediately, and a destructive one (delete, format, shutdown, restart) "
     "asks the user to approve it first, so say it is waiting for approval "
     "rather than that you are unable to do it."),
    ("task_schedule",
     "A background daemon runs 24/7. Schedule reminders and tasks "
     "(`task_schedule`), list them (`task_list`), cancel one (`task_cancel`)."),
    ("calendar_add",
     "Add calendar events (`calendar_add`), list them (`calendar_list`), delete "
     "one (`calendar_delete`)."),
    ("browser_navigate",
     "Use the built-in browser to open a page (`browser_navigate`), click "
     "(`browser_click`), type (`browser_type`) and extract content "
     "(`browser_extract`)."),
    ("read_file",
     "Read, write and search files with `read_file`, `write_file` and "
     "`search_in_files`."),
    ("memory_set",
     "Remember facts with `memory_set` and recall them with `memory_get`."),
    ("desktop_click",
     "Inspect and drive the screen: read it (`screen_read`), click "
     "(`desktop_click`), type (`desktop_type`), scroll, manage windows, and set "
     "volume or brightness."),
]

# Said once, not per tool. The permission rule is the one instruction that has
# to survive a model skimming the list, because getting it wrong means either
# claiming helplessness or silently retrying a blocked action.
_PERMISSION = (
    "Permission: a tool guarding something destructive will not run the first "
    "time. The result comes back marked `requires_approval` with a message. "
    "When that happens, say plainly what you want to do and ask the user to "
    "allow it - name the tool and what it will touch, in one or two sentences, "
    "and stop there. Do not claim you are unable to do it, and do not silently "
    "try another route. How they answer depends on where they are: in this app "
    "they click Allow on the card above the composer or on a button in your "
    "message; on a bot (Telegram, Discord, WhatsApp) they get a button or reply "
    '"yes". Ask, then wait. If it is not answered in time it stays queued, so '
    "do not repeat it forever - say it is still waiting once and let them come "
    "back to it."
)


def _offered(tools: list[str] | None) -> set[str] | None:
    """The tool set as a set, or None for "no filter" (every enabled skill)."""
    return set(tools) if tools is not None else None


def capabilities_block(tools: list[str] | None) -> str:
    """Prose describing the tools this turn may use. "" when there is nothing to say.

    ``tools`` None means every enabled skill - the same convention
    `chat_with_tools` uses, so the prose and the catalogue cannot disagree about
    what "no filter" means. An empty list is "no tools", and returns "".
    """
    only = _offered(tools)

    lines: list[str] = []
    for name, fact in _FACTS:
        if only is None or name in only:
            lines.append("- " + fact)

    # A narrowed set with no entry in the table still deserves the honest
    # version: name the tools rather than leaving the model to guess from a
    # schema. Without this a desk given only `verify_code` was told nothing.
    if not lines:
        if only:
            listed = ", ".join(f"`{n}`" for n in sorted(only))
            return (f"You have these tools available: {listed}. "
                    "Use them; do not say you lack access to them.")
        return ""

    head = ("You are deeply integrated into the Addled desktop app with real "
            "tools and background services:\n")
    body = "\n".join(lines)

    # The permission rule is only true when something in the catalogue can be
    # gated. Saying it to a turn holding only `read_file` would invent a
    # restriction the model will then mention to the user.
    blocks = [head + body]
    if only is None or only & _GATED:
        blocks.append(_PERMISSION)

    blocks.append(
        "When asked what you can do, or asked to check or manage the calendar, "
        "schedule, tasks, reminders, desktop, files, browser or memory, "
        "acknowledge these capabilities and call the appropriate tool.")
    return "\n\n".join(blocks)


# Skills whose first call may come back needing approval. Used only to decide
# whether the permission paragraph is worth saying; it is not the enforcement,
# which lives in the approvals policy.
_GATED = frozenset({
    "run_command", "write_file", "delete_file", "move_file", "copy_file",
    "desktop_click", "desktop_type", "desktop_hotkey", "browser_navigate",
})


def tool_names(tools: list[str] | None) -> list[str]:
    """The tools a turn was offered, for logging and for a persona to name.

    None means every enabled skill, resolved through the registry. Never raises:
    an unreadable registry returns the table's names rather than nothing, since
    under-reporting is the safer direction for a prompt.
    """
    only = _offered(tools)
    if only is not None:
        return sorted(only)
    try:
        from backend.skills.registry import skill_registry
        return sorted(s.name for s in skill_registry.enabled_list_all())
    except Exception as e:  # noqa: BLE001
        log.debug("could not list the tool catalogue: %s", e)
        return [name for name, _ in _FACTS]


def claims_no_tools(text: str) -> bool:
    """Does this reply tell the user it has no usable tools?

    Deliberately narrow. A false positive appends a correction to a reply that
    was fine, which is worse than missing one: the user reads a confident
    answer with a strange addendum. Every phrase here is one an agent actually
    used, observed live:

      "QA's tools aren't accessible in the current environment"
      "not a valid action with any of the available tools"

    Kept as phrases rather than a regex so the list is readable and each entry
    can be traced to a real occurrence.
    """
    low = (text or "").lower()
    signs = (
        "tools aren't accessible",
        "tools are not accessible",
        "don't have access to tools",
        "do not have access to tools",
        "no tools available",
        "not a valid action with any of the available tools",
        "none of the available tools",
        "i don't have access to the tools",
        "i do not have access to the tools",
        "cannot access the tools",
        "can't access the tools",
        "my tools aren't accessible",
        "my tools are not accessible",
    )
    return any(s in low for s in signs)


def correction(tools: list[str] | None) -> str:
    """A line to append when a reply wrongly claims it has no tools.

    Explains the tools are real and asks for the work to be attempted, without
    pretending the failed answer was correct.
    """
    names = tool_names(tools)
    if not names:
        return ""
    listed = ", ".join(f"`{n}`" for n in names[:12])
    more = "" if len(names) <= 12 else f" (and {len(names) - 12} more)"
    return (f"\n\nYou do have tools available for this: {listed}{more}. "
            "Please use them and give me the answer.")
