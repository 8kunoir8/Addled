# The swarm roster names skills that do not exist

Found 2026-10-10 while acting on "for email, do install the pack". Installing
`obra/superpowers` cleared part of `check_email_skills` (35 -> 18 failures) and
exposed that the rest is not a missing install at all.

## What was fixed by installing

`scripts/install_skills.py all` installed 14 skills, all with real content
(`test-driven-development/SKILL.md` is 9.9 KB, not a stub). Registry went from
116 to 130 skills. That resolved every roster reference that had an upstream.

## What installing CANNOT fix

15 roster references remain, and **no installer carries them**:

| roster name | closest real skill | verdict |
|---|---|---|
| `read-file` | `read_file` | **typo** -- hyphen for underscore |
| `diagnosing-bugs` | `diagnosing-superpowers` | stale name, renamed upstream |
| `query` | `search_in_files` / `web_search` | vague; several real candidates |
| `research` | `web_search` / `wiki_search` | vague; several real candidates |
| `docx` / `pdf` / `pptx` / `xlsx` | `pdf_read`, `pptx_read`, `excel_read` | document *format* skills, never shipped |
| `webapp-testing` | -- | no source anywhere |
| `domain-modeling` | -- | no source anywhere |
| `doc-coauthoring` | -- | no source anywhere |
| `internal-comms` | -- | no source anywhere |
| `writing-guidelines` | -- | no source anywhere |
| `guidelines:karpathy` | -- | no source anywhere |
| `guidelines:ponytail` | -- | no source anywhere |

The `guidelines:*` names are especially odd: a colon is not used anywhere else
in the registry, so these look like a different naming scheme left in by
accident.

## Why this matters beyond the check

`check_email_skills` is not being pedantic. It asserts that a roster entry only
names skills that exist, because a roster is what tells a subagent what it can
use. A roster naming `read-file` and `webapp-testing` means the QA subagent is
pointed at capabilities that will never resolve -- it silently gets a smaller
toolset than the prompt implies, and nothing tells the user.

## The fix is a decision, not a patch

Three honest options, needing the owner's judgement:

1. **Correct the roster** -- `read-file` -> `read_file`, `diagnosing-bugs` ->
   `diagnosing-superpowers`, drop the `guidelines:*` entries and the format
   skills if they are not intended to exist. Small, truthful, and makes the
   check pass for the right reason.
2. **Ship the listed skills** -- write or install the document-format and
   comms skills so the roster is accurate as written. Much larger.
3. **Make the roster tolerant** -- filter unknown names at load. Rejected as a
   default: it hides the gap and the subagent still silently lacks the tools.

Option 1 is the smallest change that removes a real inconsistency. It was NOT
applied here because it edits what subagents are told they can do, and the
intended toolset is the owner's call, not a guess to make while fixing a check.
