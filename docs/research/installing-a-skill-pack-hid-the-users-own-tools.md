# Installing a skill pack hid the user's own tools

Found 2026-10-10, immediately after installing the `obra/superpowers` skill pack
to fix `check_email_skills`. Two suites that had been passing --
`check_cli_consumers` and `check_parity` -- started failing on the same
assertion: a tool-discovery question no longer surfaced a `cli`-category tool.

## What happened

`skill_registry.filter_for_query` scores candidate skills and keeps the top 16.
A tool-discovery question ("what tools do you have?") gives a flat **+4.0** to
these categories:

    {"mcp", "market", "forged", "meta", "cli"}

`cli` is the category for a tool **the user built**. The comment above that line
explains the intent: such a tool is a capability that extends what Addled can do,
so the question that asks what is available should surface it.

Before the install, `market` held **0** skills, so the single `cli` tool had the
+4.0 band to itself. After installing the pack, `market` holds **14** skills --
every one of them scoring the same +4.0. Fourteen equal scores filled the 16
slots, and the lone `cli` tool lost the tiebreak. It went from "callable but
invisible" straight to invisible.

## Why this is worth writing down

Nothing in `install_skills.py` is wrong. Nothing in the scoring is wrong on its
own. The bug is the **interaction**: a category that is fine when empty becomes
a budget-waster when it grows, and it takes the budget from a category that was
never meant to compete with it.

`check_cli_consumers` predicted this exactly, in its own docstring:

> Asking what tools exist surfaces the `cli` category, which is the one place a
> per-category allow-list could silently drop it.

That check existed for this failure mode and caught it on the first install.

## The fix

Treat a user-built capability like the acquisition tools: force it into the
selection (`must`) rather than hoping it outscores a pile of prose skills. The
discovery tools were already protected this way for the same reason.

`cli`, `forged` and `mcp` joined the force-added set; `market` stayed scored
only, because a market skill is content, and fourteen of them should not each
claim budget. On a fresh install those categories are empty, so the selection is
byte-identical to before.

## Generalisation

A per-category boost is a budget claim. It is only safe while the category is
small, and nothing enforces that. Any category that can grow without bound
(installed packs, connected servers, user-built tools) needs either a reserved
slot, a cap, or a check that its growth does not evict something the user made.
