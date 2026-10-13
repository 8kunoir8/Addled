# How broken is skill file access, and what it costs

Measurement, 2026-10-13. Run against the live install, the live app
(cwd `E:\Kunoir\Codeground`), and the running backend. Nothing here is
inferred; each number came out of a command.

---

## 1. The measured damage

```
installed skills        : 15
bundled files total     : 23
skills naming own files : 6
bytes unreachable       : 137,219
```

| Skill | Named files | Bytes |
|---|---|---|
| `writing-skills` | 5 | 75,594 |
| `subagent-driven-development` | 3 | 20,127 |
| `brainstorming` | 2 | 14,294 |
| `systematic-debugging` | 3 | 12,487 |
| `test-driven-development` | 1 | 8,268 |
| `requesting-code-review` | 1 | 6,449 |

6 of 15 skills — **40%** — instruct the model to read a file the model
cannot locate. 137 KB of shipped, stored, referenced content that is
unreachable through any supported path.

`skill_view` returns only `body`, `description`, `name`, `success`,
`truncated` (verified by calling the real handler). No directory. No file
list. So the model is told *"read `visual-companion.md` in this directory"*
(`brainstorming/SKILL.md:224`) and is never told which directory.

---

## 2. Is there a workaround? Yes, and it is worse than none

`search_files` runs `glob` from `os.path.join(directory, pattern)` with
`directory` defaulting to `"."` — **the process cwd**. So a model *can* find
the file by name. What it gets back is the problem.

Called it through the live app:

```
search_files {"pattern": "visual-companion.md"}
-> E:\Kunoir\Codeground\Clicky\Addled\backend\memory\market_skills\
       brainstorming\visual-companion.md
```

The live app's cwd is `E:\Kunoir\Codeground`, and the skill it found is a
**third, unrelated copy** from a different project. Meanwhile the running
install serves its skills from:

```
C:\Users\Steru\AppData\Local\Addled\market_skills\brainstorming\
```

and the repo I am working in has a fourth at
`glaucus\backend\memory\market_skills\brainstorming\`.

**And the copies have diverged.** The same skill, two locations:

| Location | Files present |
|---|---|
| `...\AppData\Local\Addled\market_skills\brainstorming\` | `builder-check-prompt.md` |
| `...\Clicky\Addled\backend\memory\market_skills\brainstorming\` | `spec-document-reviewer-prompt.md` |

So the search-based workaround can hand the model **a different project's
version of the skill's attachment**, with no signal that it happened. The
model then follows instructions from the wrong file.

That is worse than a clean failure, because:
- a clean failure is visible and the model can say so;
- this success is *plausible* — a file with the right name, the wrong
  contents, and nothing anywhere to indicate the substitution.

### It is also non-deterministic

The result depends on the **working directory of whichever process answers**.
Different install, different launcher, different cwd → different file. Nothing
in the pipeline pins it. `FileOps.search` (`file_ops.py:204`) globs the raw
string; there is no anchor.

---

## 3. Blast radius beyond skills

| Consumer | Affected? | Evidence |
|---|---|---|
| `skill_view` | **yes** — no path exposed | handler returns 5 keys, no `dir` |
| Skills page (dashboard) | **yes** — `skills_list` has no file field | `ws_server.py:6329-6341` |
| `search_files` / `list_dir` | **yes** — cwd-relative defaults | `executor.py:736,738` |
| `read_file` | exposed, but unaimable | defaults `.`; bare name fails |
| **Forge** (`forged_skills/`) | **no** — anchors correctly | loads by absolute path |
| Console / trajectory / history | no | unrelated stores |

The forge comparison is the important one. `forge.py` loads skills with
`spec_from_file_location(..., str(f))` where `f` comes from
`app_paths.subdir("forged_skills")` — an **absolute** path from a single
resolved data dir. It also has `_missing_dependency()` (forge.py:247), which
reads a `# Package:` header and **refuses to register** a skill whose
dependency is absent, with a message naming the package.

So Addled already contains both things the market path lacks:
1. absolute-path resolution from `app_paths`, and
2. an install-time dependency check that fails loudly.

The market path simply never got them. That makes this a **consistency bug**,
not a design gap — the correct pattern is already in the codebase, twice
(`forge.py`, `codemode.resolve_in_workspace`).

---

## 4. How bad is it, honestly

**Severity: moderate, not critical. Systematic, not random.**

- It never loses user data and never corrupts a store.
- It degrades exactly the skills that brought extra material — i.e. the
  *good* ones. A skill that ships templates is a skill someone invested in.
- The failure is the quiet kind: the model reads a procedure that references a
  file, cannot resolve it, and either skips the step or **improvises the
  contents**. `subagent-driven-development` is the clearest case — its three
  prompt templates (20 KB) are meant to be dispatched verbatim to subagents.
  A model that cannot open them writes its own instead, and the subagent gets
  a paraphrase of a template the author refined, with no indication.
- It has been invisible because **nothing errors**. No exception, no log line,
  no dashboard hint. The skills list payload does not even carry a file count,
  so the UI cannot show one is there.

Why it stayed hidden: every observable surface says the skill loaded fine.
`_register_dir` succeeds, the body is stored, `skill_view` returns it, the
dashboard lists the skill. Only the *second* step of a multi-step procedure
breaks, and only for 6 of 15 skills.

---

## 5. What is NOT broken (so the fix stays small)

- Skills without attachments: 9 of 15, entirely unaffected.
- Script execution for forged skills: absolute paths, correct.
- Approval fingerprinting: `folder_digest` walks `rglob("*")`, so it already
  **sees** the files even though the model cannot. Permissions are sound.
- The registry, prompt catalogue, and enable/disable: unaffected.

So the blast radius is one capability wide — *a skill reaching its own files* —
and it is missing, not broken.

---

## 6. Plan

Four steps, ordered by dependency. Steps 1–2 fix a live defect; steps 3–4 are
the original feature, which is only meaningful once 1–2 exist.

### Step 1 — Anchor the skill's directory (fixes the wrong-file bug)

- `SkillDefinition` gains `folder: Path | None` (default `None`).
- `market._register_dir` sets `folder=d.resolve()` (it already has `meta["dir"]`).
- An accessor `skill_files_path(name)` in market, built on the existing
  `installed_dir()`, so callers never reconstruct a path.

Why: `installed_dir()` (market.py:45) already returns the resolved folder and
`fingerprint()` already uses it. This makes that reachable.

### Step 2 — Expose the files so the model can read them

- `skill_view` returns `files: [...]` (relative paths) and `read` guidance.
- New `skill_read` tool: `(name, path)` → resolved **under the skill's folder**
  via `codemode.resolve_in_workspace`, refusing escapes.
- `skills_list` (dashboard) gains `files: int` so the UI can show attachments.
- Cap: reuse `_SKILL_BODY_CHARS`; refuse non-text suffixes (`.dot`/`.js` are
  text; reuse the existing text check rather than a new list).

Why: this is the whole fix for the 6 skills. It is the same progressive
disclosure `body` already made, one level deeper.

### Step 3 — Recursive script detection (fixes the gate tripwire)

- `market.py:289` `d.iterdir()` → `rglob("*.py")`, excluding the new
  `folder`-relative depth check.
- Keep detection and execution separate: `requires_approval` becomes correct;
  `_script_handler`'s opt-in is unchanged.

Why: today a `scripts/x.py` is invisible to the gate. Latent now (no skill has
one), live the moment a zip is accepted.

### Step 4 — `install_from_zip`

- Mirror `install_from_github` (market.py:401).
- Self-contained `_safe_member(info)` rejecting: absolute, `..` components,
  drive-relative `C:`, symlink mode, reserved names, duplicate case-folded
  names, plus count/byte caps.
- Strip a single wrapper directory.
- After extraction, run the **forge-style** check: unknown Addled tools and
  missing `shutil.which` binaries reported as warnings, never auto-granted.
- No overwrite without an explicit flag.
- Validation is reported as *checked*, never as *tested*.

### Sequencing

1 → 2 alone fixes the live 6-skill defect and makes the dashboard honest.
3 is small and removes a tripwire. 4 is the feature and should come last,
because a zip full of unreachable files reproduces the exact bug we are
fixing.

### Tests to add

- `check_skill_files.py`: a skill with an attachment is readable via
  `skill_read`; a `../` path is refused; `search_files` from a foreign cwd
  cannot reach it (the regression that started this).
- Extend the trajectory-style guard set: pin `folder=` being set, and pin
  `skill_view` returning `files`.

---

## 7. Unverified

- Whether any of the 6 skills' steps is *optional*. I read `brainstorming` and
  `subagent-driven-development` closely; for the other four I confirmed the
  file is named in the body, not that the step is mandatory. So "6 skills
  affected" is a file-level fact; the per-skill severity may be lower.
- Whether production users have all 23 files. I measured the live install and
  the repo; the `E:` copy had already diverged, so copies **can** differ and I
  only sampled three.
- No end-to-end model-in-the-loop test of the failure (that needs a turn that
  actually attempts the step). The mechanism is proven; the model's behaviour
  on hitting it is not.
