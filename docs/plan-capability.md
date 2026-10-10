# Addled — Capability Plan

Derived from `docs/research-agents.md` (how Hermes, Claude Code and omp work) and
verified against the Addled source. Supersedes the "initiative budget" section of
`docs/plan-alive.md`, for the reason given in §3.

Status: IMPLEMENTED. See docs/report-capability.md for what was built, what
the checks found, and the two things the plan turned out to have wrong.

Two further corrections made during implementation, both from reading the code
rather than the plan:

  - Addled ALREADY HAS a skill forge (`backend/skills/forge.py`): it generates
    and registers Python skills, validates them, and asks consent first. So
    "cannot author skills" was wrong. What it cannot author is a skill that is
    GUIDANCE rather than code, which is what §2 actually needed - and that is
    what `body` plus `skill_view` added.
  - The verification gate keys on a `path` ARGUMENT, not on a list of tool
    names. A name list covers the built-ins and misses every skill a user
    installed or forged, which is exactly the set a particular person added to
    do their own work.

---

## 0. A correction to my own research

Before the plan, a correction, because it changes what should be built.

I claimed Addled has **"no learning between turns"**. That was wrong, and the
error was mine — I searched for the wrong words. Addled already has:

- **`backend/memory/maintenance.py`** — genuine idle-time memory maintenance, on
  the engine tick, each job independently throttled:
  `reembed` (5m), `compact` (5m), `facts` (30m), `triples` (30m), `dedup`,
  `expire`, `links`, `linkdup`, `wiki` (daily). It re-embeds, compacts, extracts
  facts, builds a knowledge graph, prunes dead links, and lints the wiki.
- **`backend/sop/learn.py`** — `record_run()` keeps the route a *successful* turn
  took, storing the goal, the tool trace, and the system-prompt context as a
  "reason". It compares runs **by goal, not by tool set**, which is a genuinely
  good decision: otherwise every task using `read_file` would collapse into one
  procedure titled after whichever ran first.
- **`backend/memory/links.py`** — a link graph with `forget`, `forget_all`,
  `prune`; **`vector_store.expire()``**; **`compaction.MAX_ENTRIES = 10`**;
  **`journal.MAX_ENTRIES_PER_DAY = 200`**.

So the honest position is: **Addled already does most of what Hermes does, in a
different shape.** The genuine gaps are narrower and more specific:

| | Hermes | Addled | Gap |
|---|---|---|---|
| Trigger | forks agent post-turn | `_learn_procedure` inline on the turn tail | no fork, runs inline |
| Judgement | **asks an LLM** "should any skill/memory be saved or updated?" | records the tool sequence **mechanically** if it succeeded | **no judgement** |
| Can write skills | yes (`skill_manage`) | **no** — procedures only | **cannot author skills** |
| Deferral | queued to idle, coalesced | runs inline immediately | no deferral |
| Memory policy | skills first, memory a budgeted exception | undifferentiated | no policy |

Three real gaps, not eight. The plan targets those, plus the two defects that
are outright *wrong* (streaming, skill reachability).

---

## 1. Streaming — the code exists and is never called

**Verified:** `chat_stream` is implemented by **9 providers** and has **zero call
sites** outside their own definitions. The dashboard draws
`{content:'', streaming:true}` (`page.tsx:414`) and swaps in the whole reply at
`page.tsx:444`.

**Design: final-answer-only streaming.**

Stream only the round whose prose *is* the answer. During tool rounds the model's
text is often narration it later discards ("I'll check that file"); streaming it
would show text that then vanishes.

The mechanism cannot know a round is final until it completes, so:

- **stream every round into a buffer, emit only once the round is known final.**
  (Rejected: peeking with a second call — costs an extra model call per turn.)
- New `chat.delta` broadcast via the existing `broadcast_nowait`
  (`ws_server.py:221-252`), which already fans out to the floating character
  through `_notify_ui`.
- Thread an `on_delta` callback into `chat_with_tools` with a **no-op default**,
  so every existing caller and check is unaffected.
- Providers with `supports_streaming = False` (HF-local) keep today's behaviour.
- Dashboard: `onNotification('chat.delta', …)` appends to the placeholder;
  `fillReply` stays as the terminator, so a dead stream still resolves.

**Files:** `backend/providers/base.py` (no change), `backend/skills/tool_loop.py`,
`backend/ws_server.py`, `dashboard/src/app/chat/page.tsx`.

---

## 2. Skill reachability — skills that cannot be read

**Verified.** `SkillDefinition` (`registry.py:58-65`) has `name`, `description`,
`parameters`, `handler` — and **no body field**. `to_prompt_tools` emits one
line per skill. Market skills *do* store their body
(`market.py:200` `"instructions": body`), but it is returned by
`_instruction_handler` (`market.py:235`) **as a tool result**.

So: a skill's guidance is invisible until the model calls it, and the model
decides to call it based on a one-line description. A skill cannot teach anything
longer than a sentence, because nothing longer is ever in front of the model.

Hermes solves this with an **index in the prompt and bodies loaded on demand**
(`skill_view`), with the index in the volatile prompt tier.

**Design: progressive disclosure.**

1. Add `body: str = ""` to `SkillDefinition`.
2. Add two meta-skills, matching Hermes' shape:
   - `skill_view(name)` → returns the body (market skills already carry one;
     forged skills get one written by §3).
   - `skill_search(query)` → returns matching names + descriptions, so the
     catalogue need not be fully enumerated.
3. The prompt carries the **index** (name + one-line description); bodies load
   only when the model asks. This is what makes a 40-line skill possible without
   paying for it every turn.
4. Bodies are capped (Hermes caps at 32k chars) and a `[SKILL_PRUNED]`-style
   marker marks a body dropped by compaction, so the model knows to re-view it.

**Files:** `backend/skills/registry.py`, `backend/skills/market.py`,
`backend/ws_server.py` (prompt assembly).

---

## 3. Post-turn review — judgement, not bookkeeping

This is the Hermes idea applied to Addled's *existing* learning system, not a
replacement for it.

**What exists:** `_learn_procedure` (`tool_loop.py:584`) fires inline on the turn
tail when `learn=True` and at least one tool succeeded. It records a *sequence*.
It never asks whether the turn taught anything worth keeping.

**What is missing:** judgement. A turn that ran `read_file` three times and
answered a trivia question currently writes a "procedure". A turn that
discovered a real pitfall — that the build needs `-s`, that the deploy script
must run elevated — writes nothing, because both used tools and succeeded.

**Design: a post-turn review, deferred to idle.**

1. New `backend/review.py`. After a turn, enqueue a snapshot; do not run inline.
2. On the engine tick, drain the queue **when the machine is quiet** — reusing
   the presence guard and the existing maintenance throttle. Follow Hermes:
   one slot per conversation, newest snapshot wins (a review replays the
   conversation, so coalescing is dedup, not loss), and an age-out so a stale
   review still runs.
3. The review is one model call using the `utility` role (cheap), asking a
   narrow question: *"should any skill or memory be saved or updated?"* — with
   the current skills index in scope so it can see what already exists.
4. It may **write or update a skill body** (§2) — this is what makes the
   learning loop close. It may consolidate memory entries.
5. Hard bounds: one call per review, a daily cap, and it writes only through the
   same store APIs the rest of Addled uses.

**Measured caveat on this machine.** The live settings
(`C:\Users\Steru\AppData\Local\Addled\settings.json`) have `providers.roles = {}`
— **empty** — with `auto_route: True` and `default_model: None`. So
`resolve_model` currently falls through to `_BUILTIN_ROLES` and then to the
provider default. Consequence for §3: the review is not guaranteed to run on a
*cheap* model until a `utility` role is actually configured. Hermes relies on the
review being cheap; here it would be the same model as chat, which is a
different cost profile. Seed a `utility` default, or state plainly that the
review runs on the main model when none is set — cheap-first was the whole point
of deferring it to idle, so seeding the role matters.

**Why deferred, not inline:** Hermes measured this — an inline review
"monopolizes the GPU the next prompt needs and the next live turn cancels it
(decode cost paid, learning lost)". Addled's local model runs `slots: 1`, so the
same applies exactly.

**Why this replaces the earlier initiative idea** (`docs/plan-alive.md` §3): an
initiative budget would have Addled *speak* unprompted, which has no way to be
tested for "annoying". A review fork writes to stores and improves with use, and
its worst case is a stale skill, not an interruption.

**Files:** new `backend/review.py`, `backend/engine.py` (tick step),
`backend/skills/tool_loop.py` (enqueue), settings.

---

## 4. Honest wait notices

Hermes' `chat_completion_wait_notice.py` changes *what the status line says*
during a long silence ("{n}s waiting for the first provider event") rather than
animating a cursor that is not connected to anything.

**Design.** During a turn, emit a `chat.activity` phase line at real transitions —
"thinking", "searching files", "reading executor.py", "writing", "verifying" —
from the same callback seam as §1. Not narration (no extra model call, no
generated prose); just the tool name the loop already knows.

This is the honest version of "visible reasoning". It costs nothing and never
claims something untrue.

**Files:** `backend/skills/tool_loop.py`, `backend/ws_server.py`,
`dashboard/src/app/chat/page.tsx`, `backend/character/avatar.py` (the floating
character already has `ACTING`/`WORKING` states).

---

## 5. Verification gate

**What exists:** `_final_answer` (`tool_loop.py:666`) forces one plain-text round
**only when tools failed**. A turn that claims "I fixed it" with no test run ends
normally.

Hermes refuses to end a turn without **fresh verification evidence**, bounded at
3 nudges.

**Design.** Before finishing a turn that changed files, require evidence: the
command that was run and its output. If absent, inject one bounded nudge
(*"you changed these files; run something that shows it works"*), capped at a
small number so it cannot loop. Never block a purely conversational turn.

**Files:** `backend/skills/tool_loop.py`.

---

## 6. Model roles — add the slots that unlock behaviour

**Verified:** `ROLES = ("chat", "reasoning", "vision", "long", "utility")`
(`router.py:38`); resolution precedence is explicit setting → `_BUILTIN_ROLES` →
`vision_model` → `default_model`; `pick()` accepts `force_role`.

Adding a role is a tuple entry plus a settings default. Three are worth adding,
each because it unlocks a *behaviour*, not a model choice:

- **`plan`** — used by plan mode (§7). A strong model plans; execution need not
  use it.
- **`utility`** — already exists, and is what the review (§3) runs on. Confirm
  it is configured; if not, the review falls back to the provider default rather
  than failing.
- **`judge`** — for decisions where a string check is not enough. Hermes uses it
  exactly here: `contains: "PASS"` also matches `"NOT PASSED"`. Addled's
  `check_*` suites have the same class of problem — see the revert-verification
  doctrine.

**Not** adding `smol`/`tiny`/`memory`/`commit`: without a second model to route
to, an unused role is a setting that does nothing, which is worse than no setting.

**Files:** `backend/providers/router.py`, `backend/config.py`.

---

## 7. Plan mode

Both Hermes and Claude Code make planning a **first-class mode**: its own prompt,
read-only tools, and a persisted artifact. Hermes restricts plan-mode spawns to
`read, grep, glob, web_search` and writes to `.hermes/plans/`.

Addled's `code.plan` is a two-call pipeline inside the code page
(`_PLAN_TOOLS` at `ws_server.py:628`), not a mode reachable for any task.

**Design: a plan mode any surface can enter.**

- A `/plan <task>` prefix, resolved in `run_chat_pipeline` before routing, so it
  works from chat, voice, or the character.
- Tools restricted to the read-only set; no writes, no spawns.
- `force_role="plan"` (§6).
- Output written to `memory/plans/YYYY-MM-DD-HHMM-<slug>.md` and quoted back.
- Borrow Hermes' craft rule verbatim in the prompt: *"Write the plan for an
  implementer with zero context for the codebase and questionable taste... if
  someone has to guess, the plan is incomplete."* That instruction is the reason
  the Claude Code plan I read contained literal code rather than a summary.

**Files:** `backend/ws_server.py`, `backend/providers/router.py`.

---

## Build order

Ordered by felt impact per unit of risk. Each step ships independently.

| # | Change | Risk | Why here |
|---|---|---|---|
| 1 | Streaming (§1) | low | code already exists; largest felt win |
| 2 | Wait notices (§4) | low | shares §1's seam |
| 3 | Skill bodies + `skill_view` (§2) | medium | unblocks §3 |
| 4 | Post-turn review (§3) | medium | needs §2 to be useful |
| 5 | Verification gate (§5) | low | small, isolated |
| 6 | Roles + plan mode (§6, §7) | medium | self-contained |

## Verification approach

House convention: a standalone `scripts/check_*.py` per feature, registered in
`scripts/check_all.py:SUITES`, and **revert-verified** — break the code, watch
the check fail — before it is trusted. Revert-verification is not optional here:
three checks written earlier this session passed while the bug was live, and the
`check_code_learning.py` docstring records a live defect that a passing suite had
not caught.

Specific asserts worth naming up front:

- **Streaming:** a fake provider whose `chat_stream` yields known chunks; assert
  deltas arrive in order, that tool rounds emit **none**, and that a
  non-streaming provider still returns the full text unchanged.
- **Skill bodies:** assert a body longer than the description actually reaches
  the model via `skill_view`, and that the prompt does **not** contain it.
- **Review:** assert it does nothing when disabled, that it cannot exceed its
  daily cap, that it coalesces (newest wins), and that it writes through the
  store APIs rather than directly.
- **Verification gate:** assert a conversational turn is never nudged, and that
  the nudge count is bounded.

## What this plan does NOT do

- Does not duplicate `maintenance.py` or `sop/learn.py`; §3 adds judgement to
  them, not a parallel system.
- Does not make Addled speak unprompted.
- Does not require a second model to be useful — every step degrades to today's
  behaviour when `utility`/`plan` are unconfigured.
- Does not narrate with extra model calls.
