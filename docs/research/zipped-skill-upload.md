# Uploading a zipped skill: what exists, what is missing, what would break

Research date: 2026-10-13. Every claim carries a file:line, read from this
checkout. Written before any code, as the input to a plan.

The request was: accept an uploaded ZIP of a skill, read it, add it as
Addled's own skill, test that it works, take its name and description,
categorise it, decide whether it needs permission, check which tools it
needs, and add those tools.

The short answer: **about 70% of this exists already.** The parts that do
not exist are the ZIP intake, the tool-declaration enforcement (which is
currently parsed and thrown away), and any notion of testing a skill.

---

## 1. What already exists

### 1.1 A complete skill installer — for text, URLs and GitHub

`backend/skills/market.py` imports `SKILL.md`-format skills:

| Entry point | Line | What it does |
|---|---|---|
| `install_from_text` | 326 | Paste a `SKILL.md` body |
| `install_from_url` | 336 | Fetch one markdown file |
| `install_from_github` | 401 | Download a whole folder from a repo |
| `delete` | 487 | Remove a skill and unregister it |

There is **no `install_from_zip`**. That is the gap, and `install_from_github`
(401-483) is the exact template for it — it already does "download several
files into a folder, then register".

### 1.2 Name and description are already read

`parse_skill_md` (179-232) reads YAML frontmatter and returns:

```python
{"name", "description", "allowed_tools", "license", "version", "instructions"}
```

A real one, `market_skills/brainstorming/SKILL.md`:

```yaml
---
name: brainstorming
description: "You MUST use this before any creative work ..."
---
```

It even degrades gracefully: with no frontmatter it falls back to a `# H1`
heading (184-193). So "read the name and description" is **done**.

### 1.3 Categorisation is a single hardcoded string

`market.py:310` registers every market skill as `category="market"`. There is
no per-skill category from frontmatter. `SkillRegistry.list_category` exists
(`registry.py:747`), so the plumbing is there and unused for this case.

### 1.4 Permission is decided by file *type*, not by declaration

`market.py:294-300`:

```python
if scripts:
    handler = _script_handler(name, script_file, d)
    requires_approval = True
else:
    handler = _instruction_handler(meta["instructions"])
    requires_approval = False
```

So a `.py` file means approval; markdown alone does not. That is a real
distinction and it is the right one — but it is **all** it is. Which tools
the skill claims to need has no bearing on it.

### 1.5 Script gating exists and is already two-layered

- Opt-in: `skills.allow_script_skills` defaults **False**
  (`market.py:244`), so market scripts never run without an explicit yes.
- And the argv bug is already fixed: `execute_argv([...])` (259-261) with a
  comment recording that the previous f-string form was an
  **arbitrary-command primitive** through `powershell -Command`.

### 1.6 Uploads already have a proven pattern, including a safe ZIP

`ws_server.py:6438` `character_upload_skin` is the existing file-upload RPC:
base64 in → decode → branch on the `PK` magic (`6451`) → save → broadcast.

And `sprite_skin.save_uploaded_zip` (177) is a **working, safe zip
extractor** already in the codebase. Its safety comes from one line:

```python
fname = Path(info.filename).name    # sprite_skin.py:206
```

It flattens every entry to its basename and never rebuilds a path. That is
why it cannot be walked out of — see §3.

### 1.7 "Always allow" is bound to the skill's file digest

`approvals/policy.py:230` `fingerprint()` calls
`market.installed_dir()` then `folder_digest()`. A grant binds to **the code**,
not the name, so swapping a script after a grant loses it. `folder_digest`
(61) hashes every file's name and bytes.

**This matters for ZIP intake**: a zip-installed skill is fingerprinted for
free, because it lands in the same `MARKET_DIR` folder layout.

---

## 2. What is genuinely missing

### 2.1 ZIP intake

Nothing accepts a `.zip` of a skill. `skills.installFrom`
(`ws_server.py:6410`) handles a URL or a GitHub repo only. It has no
`data`/base64 branch, unlike `character_upload_skin` next to it.

### 2.2 `allowed_tools` is parsed, then discarded

This is the most interesting finding. `parse_skill_md` returns
`allowed_tools` (228) and **nothing reads it**:

```
$ grep -rn "allowed_tools" backend --include=*.py   # excluding __pycache__
market.py:197   "allowed_tools": [],      # the no-frontmatter default
market.py:228   "allowed_tools": list(allowed),   # the parse
```

Two hits, both in the parser. It is never stored on the `SkillDefinition`,
never enforced, never shown. The docstring at `market.py:7` advertises it as
supported — so the field is documented as working and is inert. **"Check
which tools it needs" has nothing to check against today.**

### 2.3 No tool validation at the install boundary

Nothing verifies that the tools a skill names actually exist. The registry
*can* answer this — `skill_registry.list_all()` returned **130** skills and
tools when loaded — but no caller asks.

### 2.4 No notion of testing a skill

There is no "does this skill work" step anywhere. The closest things are
`cli_tools/runner.py` and the check scripts, neither wired to skill install.

### 2.5 The `allowed-tools` word itself is a trap

`allowed-tools` is Claude Code / Copilot's spelling, and it is a *permission
allowlist for the agent running the skill*, not a dependency list. Reading it
as "tools this skill needs added to Addled" would be a misinterpretation of
the format. Worth stating plainly because it changes the design — see §4.2.

---

## 3. The ZIP is the dangerous part

A skill ZIP is not like a GIF ZIP. GIFs get flattened to a basename and
matched against a fixed state table; a skill ZIP must preserve **paths**
(`scripts/run.py` referenced from `SKILL.md`) and may contain **`.py`** —
i.e. code that will later be executed.

I tested what Python actually does rather than assuming. Three real results:

### 3.1 `extractall()` escaped the destination

A zip containing `../ESCAPED.txt`, `../../ESCAPED2.txt` and `/abs/ABS.txt`:

```
after extractall, files under dest2: [ABS.txt, ESCAPED.txt, ESCAPED2.txt ...]
```

Files landed **outside** the destination directory. So `extractall()` is not
usable, and neither is `dest / name` on an unvalidated name.

### 3.2 A ZIP can carry a symlink

`save_uploaded_zip` style flattening would drop it, but a path-preserving
extractor must handle it — a zip entry can be a symlink whose target is
`/etc/hosts`, and a *later* entry written "through" it escapes the folder.
Confirmed: an entry with `external_attr` mode `S_IFLNK` read back as
`b'/etc/hosts'`.

### 3.3 The obvious guard is not enough

Checking `resolved.parents` after joining:

| entry | naive "is inside" |
|---|---|
| `../x.txt` | False ✓ caught |
| `/abs/x.txt` | False ✓ caught |
| `ok/../../x.txt` | False ✓ caught |
| **`C:x.txt`** | **True** ✗ — drive-relative, means "x.txt in CWD of C:" |
| **`ok/x.txt:ads`** | **True** ✗ — Windows alternate data stream |
| `con` | True ✗ — reserved device name |

So a correct extractor must reject, per entry, independently:

1. absolute paths (leading `/` or a drive letter),
2. any `..` **component** (not just a resolved-parents check),
3. drive-relative names containing `:`,
4. symlink/reparse entries (`external_attr` mode),
5. reserved Windows device names,
6. `.`/empty names, and duplicates after case-folding (Windows is
   case-insensitive; `A.py` and `a.py` collide),
7. a count cap and a total extracted-bytes cap against a **zip bomb**
   (`info.file_size` is attacker-controlled but `zipfile` does bound reads;
   still, cap while writing).

`install_from_github` already needed half of this — see the hand-written
`../` and `d.resolve() not in target.parents` checks at `market.py:466-477`,
with a comment recording that a repo-supplied filename once "wrote outside
the skill folder into the Addled tree". A ZIP has a strictly larger attack
surface than a GitHub API listing, because it controls *nested* paths and can
carry symlinks.

---

## 4. Design questions this raises

These are the decisions, not the code.

### 4.1 Where does the ZIP get extracted, and does it preserve paths?

Two options, and the existing code argues for the conservative one:

- **Preserve paths** (needed for `scripts/`) — requires the full §3 guard
  list, and a subsequent decision about *running* nested code.
- **Flatten to the skill folder root** like `save_uploaded_zip` — trivially
  safe, but silently breaks any skill whose `SKILL.md` says
  `scripts/run.py`.

The honest middle: preserve **one** level (the skill folder's own files plus
a `scripts/` directory), reject anything else. Most real skills are a single
`SKILL.md`.

### 4.2 What does "tools it needs" mean?

Given §2.5, there are two different requests hiding here:

| Reading | Meaning | Feasible? |
|---|---|---|
| **Dependency** | "this skill needs `run_command` to exist" | Validate against the 130 registered names; report missing |
| **Permission** | "this skill may only use these tools" | Would need *enforcement* at call time — a new sandbox boundary |

The first is a **validation and a warning**. The second is a security feature
and much larger. They should not be conflated, and the first is the one the
request describes ("check those tools they need and add them").

"Add them" also needs care: Addled's tools are Python functions registered at
import (`registry.py`). A ZIP cannot *add a tool* by declaration — it can only
name tools that already exist, or ship a `scripts/*.py` that does the work
itself. Those are different things and the UI should not blur them.

### 4.3 "Test them if they work" — against what?

Options, cheapest first:

1. **Structural**: parses, has a name, has a description, names only
   registered tools, has no execute-bit surprises. Pure, fast, no side
   effects. This is really *validation*, and it is the honest label.
2. **Import/dry-run** a script skill: `python -c "import ast; ast.parse(...)"`
   at minimum, or a `--help` invocation with a short timeout.
3. **Live**: actually call it. Requires the opt-in flag, can execute
   arbitrary code, and cannot be automatic.

Recommendation: do (1) always, report it as *checked*, and never claim a
skill "works" from a structural pass. Calling it a test would repeat exactly
the failure the `console_log` docstring was written about — presenting an
absence as a verified truth.

### 4.4 Overwriting an existing skill

`install_from_text` and `install_from_github` both `shutil.rmtree(d)` first
(331, 453). For an upload, silently destroying a skill of the same name is
the wrong default — the user may not know it exists. Prefer reject, or
require an explicit `overwrite` flag.

---

## 5. Summary table

| Requested | Status | Where |
|---|---|---|
| Accept an uploaded ZIP | **missing** | needs `install_from_zip` + RPC |
| Read it | **missing** (no extractor) | pattern: `sprite_skin.py:177` |
| Add as Addled's own skill | **exists** | `_register_dir` 280, `SkillDefinition` 301 |
| Test that it works | **missing** | nothing; §4.3 proposes the shape |
| Take name + description | **exists** | `parse_skill_md` 179-232 |
| Categorise it | **partial** — hardcoded `"market"` | `market.py:310` |
| Decide if it needs permission | **exists**, but by file type only | `market.py:294-300` |
| Check tools it needs | **field parsed, never used** | `allowed_tools`, §2.2 |
| Add those tools | **not a real operation** | §4.2 |

---

## 6. The recommended shape

Smallest change that answers the request honestly:

1. `market.install_from_zip(name, filename, data)` in `market.py`, mirroring
   `install_from_github`'s structure, with the §3 guard list as a small
   standalone `_safe_member_path(info)` helper — testable in isolation, the
   way `sprite_skin` did it.
2. A `skills.installUpload` RPC next to `skills_install_from` (6410), taking
   base64 like `character_upload_skin` (6438). Caps: 25 MB zip, 20 MB
   extracted, 200 files.
3. **Store `allowed_tools` on `SkillDefinition`** and surface it — the parse
   already happens, so this is wiring, and it turns a documented-but-inert
   field into a real one.
4. Report unknown tools as a **warning**, never a failure, and never
   auto-grant them.
5. Call the structural pass what it is: **validation**, not a test. Do not
   execute uploaded code on install.
6. Do not overwrite an existing skill unless explicitly told to.

Steps 1-2 are the feature. Step 3 is the thing the docs already promise.
Step 5 is the one most likely to be got wrong under time pressure.
