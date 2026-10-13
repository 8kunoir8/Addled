# Do the zipped-skill bugs affect the current ecosystem?

Answer, 2026-10-13. Measured against the 15 live installed skills, the repo
copies, and the running app. Not reasoned about.

**Verdict: one is latent, one is live.**

| Bug | Affects today? |
|---|---|
| Wrapper folder breaks registration | **No** — nothing installs via zip yet |
| `scripts/` bypasses the approval gate | **Latent** — no current skill has a script |
| **Bundled files are unreachable** | **YES — live, affecting 7 skills now** |

---

## 1. The approval-gate bypass is latent, not live

The live install, all 15 skills:

```
brainstorming                    py_all=0   subdirs=0
diagnosing-superpowers           py_all=0   subdirs=0
...
writing-skills                   py_all=0   subdirs=0
```

**Zero Python files, zero subdirectories across every installed skill.** The
repo copies match. So there is no skill today whose `requires_approval`
should be `True` and is `False` — the bypass has no victim yet.

It becomes live the moment any skill ships `scripts/*.py`, which is exactly
what the example zip does and what the conventional layout is. So it is a
**tripwire, not a present breach.** Fixing it is still cheap and worth doing
before ZIP support makes it reachable.

Worth noting what the ecosystem is instead: **every skill is instruction-only
markdown.** The whole installed collection is prose, which is why the gate
has never been exercised.

---

## 2. The unreachable-files bug is live, and it is the bigger one

This one has victims. Seven skills ship extra files and **name them in their
own `SKILL.md`**:

```
brainstorming                 visual-companion.md, builder-check-prompt.md
requesting-code-review        code-reviewer.md
subagent-driven-development   implementer-prompt.md, re-review-prompt.md,
                              task-reviewer-prompt.md
systematic-debugging          condition-based-waiting.md, defense-in-depth.md,
                              root-cause-tracing.md, find-polluter.sh, ...
test-driven-development       writing-good-tests.md
writing-skills                anthropic-best-practices.md,
                              persuasion-principles.md, render-graphs.js, ...
llm-wiki-skills               (names scripts/wiki_lint.py — does not ship it)
```

I called the real `skill_view` handler:

```
returned keys : ['body', 'description', 'name', 'success', 'truncated']
has dir/path  : False
body names its own file: True
```

So the tool returns the prose, the prose says "read `visual-companion.md` in
this directory", and the tool **does not say where that directory is**.

### Why the obvious workaround fails

`brainstorming/SKILL.md:224` says, verbatim:

> If they accept, read `visual-companion.md` in this directory before
> starting the server.

Tested which paths resolve:

| Path a model would try | Exists |
|---|---|
| `visual-companion.md` | **No** |
| `market_skills/brainstorming/visual-companion.md` | **No** |
| `C:\Users\Steru\AppData\Local\Addled\market_skills\brainstorming\visual-companion.md` | Yes |

Only the **full absolute path** works, and nothing in the system hands the
model that path. The cwd is the repo root, unrelated to the skill folder.
`read_file` exists (130 tools registered, `read_file` among them) but it
cannot be aimed.

### What that means in practice

The skill reads as working — the prose is delivered, the procedure is clear —
and then stops exactly where it needs its attachment. The failure mode is a
model either:

- giving up on the step, or
- **inventing** the file's contents, because it has the instruction to use a
  template it cannot open.

The second is worse than the first and is the same class of failure the
`console_log` docstring was written about: a claim with nothing behind it.
`subagent-driven-development` names three prompt templates (6.3 KB, 4.8 KB,
9.0 KB) that are dispatched to subagents — none reachable.

### One more finding

`llm-wiki-skills` refers to `scripts/wiki_lint.py` **and does not ship it**.
So today's ecosystem already contains a dangling dependency, silently. That
is the tier-1 case from the previous note, happening now, with no warning.

---

## 3. So what actually needs fixing, and in what order

1. **A skill-scoped file read** — highest value, because it is already
   costing 7 skills their attachments. Not a ZIP feature; it is a missing
   capability the ZIP work merely made obvious.
2. **`skill_view` should list a skill's own files** (and its directory), so
   the model knows they exist. Cheapest possible version of #1.
3. **Recursive script detection** — a tripwire now, a bypass later. Small.
4. ZIP intake, wrapper-stripping, guards — the original feature. Its value is
   gated on #1: installing a zip full of unreachable files changes nothing.

The ordering matters. Building ZIP support first would ship a feature that
installs attachments nothing can open — and the example zip is exactly that
shape (two templates plus a script, all unreachable).

---

## 4. What I did not check

- Other skill sources Addled may load (only `market_skills` audited; `forge`
  writes elsewhere and had no live entries at the time).
- Whether any of the 15 skills' named files are *meant* to be optional. I
  read `brainstorming` and `subagent-driven-development` closely; the rest I
  checked for name-mentions, not for whether the step is mandatory.
- The `writing-skills` references (`references/codex-tools.md`,
  `scripts/tool.sh`) — these look like they describe *other* skills' layouts,
  i.e. examples in prose rather than its own files. I did not separate
  "references its own attachment" from "shows an example path" for it.
