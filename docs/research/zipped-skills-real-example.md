# Zipped skills, part 2: what a real one contains

Research date: 2026-10-13. Part 1 is `zipped-skill-upload.md` (the code
survey). This one is written against an **actual skill zip**
(`seedance-60s-video-generator.zip`, 10,550 bytes), which changed three of
part 1's conclusions.

Every claim below was run, not reasoned about.

---

## 1. The real file

```
$ python -m zipfile -l seedance-60s-video-generator.zip

name                                                 size    mode
seedance-60s-video-generator/                           0    0o40755  DIR
seedance-60s-video-generator/scripts/                   0    0o40755  DIR
seedance-60s-video-generator/scripts/merge_scenes.py  7197   0o100755  EXEC
seedance-60s-video-generator/SKILL.md                 7683   0o100644
seedance-60s-video-generator/references/                0    0o40755  DIR
.../references/seedance-prompt-template.md            2829   0o100644
.../references/continuity-bible-template.md           2762   0o100644
```

Seven entries. Four things to notice, and part 1 got three of them wrong:

| Feature | Present | Part 1 assumed |
|---|---|---|
| Single wrapping top folder | **yes** | not considered |
| `scripts/` subdirectory with **`0755` executable** | **yes** | "do not execute" |
| A second directory, `references/` | **yes** | assumed only `scripts/` |
| `SKILL.md` at the folder root | yes | correct |

### Frontmatter — only two fields

```yaml
---
name: seedance-60s-video-generator
description: Menghasilkan SATU video sinematik final ... (677 chars)
---
```

There is **no `allowed-tools`**, no `license`, no `version`. So for this
real-world skill, "check the tools it needs" has **nothing to read at all**.
The declared-tools idea in part 1 §4.2 describes a field these files do not
carry.

It is also **English metadata on an Indonesian body** — 677 characters of
description. The prompt catalogue carries one line per skill
(`registry.py:1129`), so most of that is discarded anyway.

---

## 2. Two real bugs this file exposes

I extracted it and ran the existing registration path against it.

### 2.1 Registering the extract root finds nothing

```
=== A. register the EXTRACT ROOT ===
   found md: None -> FAILS (SKILL.md is nested one level down)
```

`_find_candidate_file_in_dir` (market.py:126) is **not recursive** — it lists
`d.iterdir()`. Every real zip has a wrapper folder, so "extract and register"
silently produces *no skill*, with no error a user could act on.

### 2.2 A script in `scripts/` is invisible — so the approval gate is skipped

This is the serious one. `market.py:289`:

```python
scripts = sorted(p.name for p in d.iterdir()
                 if p.is_file() and p.suffix.lower() == ".py")
```

`d.iterdir()` again — **root only**. Run against the real file:

```
   scripts at ROOT (what _register_dir scans): []          <- EMPTY
   actual script: scripts/merge_scenes.py                  <- would be MISSED
```

And `requires_approval` is set purely by `if scripts:` (market.py:294). So:

> A skill shipping `scripts/merge_scenes.py` (mode `0755`) is registered as
> **instruction-only, `requires_approval=False`** — because the file is one
> directory down.

The gate that exists to make executable skills ask permission does not see
executable skills that follow the conventional layout. **This is true today,
before any ZIP support is added.** Installing this zip by any path, including
a flattening extractor, inherits it.

The fix is small and independent: scan recursively for the *detection*
(while still deciding deliberately what may be *run*).

---

## 3. What the script actually does

`scripts/merge_scenes.py`, read in full:

```
 27: DEFAULT_OUTPUT = "FINAL_60_SECONDS.mp4"
 30: def run(cmd):  ... subprocess.run(cmd, capture_output=True, text=True)
 36: def probe(path): ...
 97: def build_filter(infos, ref_w, ref_h, ref_fps): ...
131: def merge(workdir, output_path):
142:     cmd = ["ffmpeg", "-y", "-v", "error", ...]
155:     os.remove(tmp_out)
```

- It shells out to **`ffmpeg`**, an external binary, expected on `PATH`.
- `os.remove` touches only its own temp file on error — benign.
- Modes: `--workdir` and `--output`.
- Imports: `argparse json os subprocess sys tempfile` — standard library only.
- `ast.parse` under Addled's Python **3.14.7**: **OK**, no version-sensitive syntax.

So this is not malware. It is a normal utility that **depends on a tool that
may not exist**. That is the third tier of dependency, and it is one Addled
has no concept of.

---

## 4. Dependencies are actually three tiers, not two

Part 1 framed it as "Addled tools" vs "nothing". The real file shows three
distinct kinds, and they need three different answers:

| Tier | Example here | Can Addled satisfy it? | What to do |
|---|---|---|---|
| **Addled skills/tools** | none named | 130 exist; check by name | warn if unknown |
| **Bundled files** | `references/*.md`, `scripts/*.py` | must ship in the zip | **currently unreachable** — §5 |
| **External binaries** | `ffmpeg` | no — it is on `PATH` | check `shutil.which`, tell the user |

Part 1 collapsed tiers 2 and 3. They are different: tier 2 is a packaging
problem, tier 3 is an environment problem.

Useful data point: `ffmpeg` **is** on `PATH` on this machine
(`C:\Users\Steru\AppData\Local\Microsoft\WinGet\Links\ffmpeg.exe`), and
Addled already invokes it (`skills/tool_loop.py:2607`, `voice/stt.py`). So a
`shutil.which` check would have passed here — but on a machine without it,
this skill fails at merge time after generating four clips, which is the
worst possible moment.

---

## 5. The bundled files are unreachable — this is the biggest gap

`SKILL.md` says, three times, to use its own files:

```
references/continuity-bible-template.md
references/seedance-prompt-template.md
scripts/merge_scenes.py
```

`skill_view` (`registry.py:4428`) returns **only `skill.body`** — an in-memory
string:

```python
body = (getattr(skill, "body", "") or "").strip()
```

There is **no path access**. A skill cannot read its own folder through any
tool. So:

- the two templates are **dead weight** — shipped, stored, named in prose, and
  unreadable;
- the script is **unreachable** too unless the agent happens to construct the
  absolute path into `market_skills/<name>/scripts/` and use `run_command`.

That last point is the shape of the fix: **addressing**. `installed_dir()`
(market.py:45) already returns the folder, and `fingerprint()` already uses
it. What is missing is a skill-scoped way to reach a *named file inside it* —
the same progressive-disclosure move `body` already made, one level deeper.

Without it, "install a zipped skill" installs a `SKILL.md` and some invisible
attachments. The skill *appears* to work because its prose is read; it fails
at the step where it opens its template.

---

## 6. What the example changes about part 1's plan

Part 1 recommended: extract, guards, store `allowed_tools`, warn on unknown
tools, never execute, do not overwrite. That mostly stands. Four corrections:

1. **Strip the single wrapper folder.** Now a requirement, not an option —
   without it nothing registers (§2.1). Rule: if the archive has exactly one
   top-level directory and no root-level file, descend one level.

2. **Detect scripts recursively; run them deliberately.** Today's root-only
   scan is a gate bypass (§2.2). Detection should be recursive so
   `requires_approval` is *correct*; execution policy stays a separate,
   narrower decision.

3. **`allowed-tools` is not the dependency mechanism.** Real skills do not
   carry it. Dependencies are (a) file references in the prose, (b) external
   binaries. Parsing only `allowed-tools` would have reported "no tools
   needed" for a skill that needs `ffmpeg` and two templates.

4. **File access is not optional.** The zipped-skill feature is only useful
   if a skill can reach `references/` and `scripts/`. Otherwise it is a
   packaging feature with no runtime meaning.

## 7. What I did not verify

- Whether `ffmpeg` is present on a *fresh* Addled install (only this machine).
- Whether the Indonesian body survives `parse_skill_md` byte-for-byte through
  the WS transport — it should (UTF-8, no length trouble at 7.6 KB), but I did
  not round-trip it.
- Whether the same author's other skills use the same layout. One file is one
  sample; the wrapper + `scripts/` + `references/` shape is conventional
  enough that I would design for it, but I have not measured it.

---

## 8. Revised shape

1. `install_from_zip` — mirrors `install_from_github` (market.py:401).
2. **Descend the single wrapper folder**, then register.
3. Guards from part 1 §3 — and note the real file is *clean*: relative paths,
   no `..`, no symlink, no absolute entry. The guards are for the hostile
   case, not this one.
4. **Recursive script detection** so `requires_approval` is right.
5. **A skill-scoped file read** (`references/`, `scripts/` listing + read),
   which is what makes the bundled files mean anything.
6. **`shutil.which` check for external binaries** named in the prose, reported
   at install time as a warning.
7. Validation stays structural and is not called "tested".

Item 5 is the one part 1 missed, and on this example it is the difference
between the skill working and not.
