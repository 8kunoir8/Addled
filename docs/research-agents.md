# How Hermes, Claude Code and omp work — and what Addled gets wrong

Research date: 2026-10-09. All three were inspected on this machine directly.

| | Hermes | Claude Code | omp |
|---|---|---|---|
| Version | source checkout | 2.1.138 | 18.5.1 |
| Form | Python source, 241 agent modules | 226MB compiled binary | 224MB Bun single-file |
| Home | `~/AppData/Local/hermes/` | `~/.claude/` | `~/.omp/` |
| Model roles | provider+model per task | opus/sonnet/haiku + subagent | **11 roles** |
| Loop cap | `max_iterations=500`, budget object | `--max-turns` | `task.softRequestBudget=200` |
| Context | 3-tier prompt, compress at 50% | auto-compact | compact at 85%, 5 strategies |
| Learning | **post-turn review fork** | plugins (`remember`, `episodic-memory`) | `agent.db` + skills |
| Subagents | `delegate_task`, full agents | typed agents (`Explore`) | `task` wire, depth 2 |

---

## 1. Hermes — the learning agent

Hermes is the most interesting of the three because it is the only one that
**deliberately learns from every conversation, and does so without costing the
next turn anything.**

### 1.1 The post-turn review fork (the big idea)

`agent/background_review.py` — after every turn, Hermes forks the agent and
asks it *"should any skill/memory be saved or updated?"*:

> "After every turn `AIAgent.run_conversation` may spawn a daemon thread that
> replays the conversation snapshot in a forked `AIAgent` and asks 'should any
> skill/memory be saved or updated?'. Writes go straight to the memory + skill
> stores; the main conversation and prompt cache are never touched."

Three properties make this work and are each deliberate:

1. **It hits the same prefix cache** — "The fork inherits the parent's live
   runtime (provider, model, credentials, cached system prompt) so it hits the
   same prefix cache."
2. **It cannot corrupt the live turn** — runs under a "dispatch-side tool
   whitelist", on a daemon thread, with cancellation handshakes.
3. **It defers to idle when the machine is busy** (`agent/review_idle_queue.py`):
   > "On the managed llama-server the post-turn review fork monopolizes the GPU
   > the next prompt needs and the next live turn cancels it (decode cost paid,
   > learning lost). Reviews bound for the managed endpoint are therefore queued
   > and dispatched when the machine is quiet."

   With a 15s settle window, 30min max age, and **coalescing — newest snapshot
   wins** ("a review replays the whole conversation, so coalescing is dedup, not
   loss").

**This is the single best idea in all three codebases.** It is how an agent
accumulates competence without the user ever paying for it in latency.

### 1.2 Skills-first memory policy

`agent/prompt_builder.py:191` `build_memory_guidance()` draws a hard line:

> "**Skills come first**: when you learn something while doing a task — a
> procedure, a pitfall, and the user's preferences and corrections for that kind
> of work — record it in the skill you used or built for the task
> (`skill_manage`), where it loads only when relevant."
>
> "Memory is the narrow exception for facts that apply to EVERY session
> regardless of task ... it has a hard character budget, so when it fills,
> replace or consolidate stale entries."

And a subtle rule about *phrasing*:

> "Write entries as declarative facts, not instructions to yourself:
> 'User prefers concise responses' ✓ — 'Always respond concisely' ✗ (imperative
> phrasing gets re-read as a directive in later sessions and can override the
> user's current request)."

### 1.3 Three-tier cache-ordered prompt

`agent/system_prompt.py:659` `build_system_prompt_parts`:
- **stable** — identity (SOUL.md), guidance, coding brief
- **context** — caller system_message, project context files, workspace
- **volatile** — skills *index*, memory, user profile, timestamp, runtime hints

The **skills index** is in volatile — bodies are *not* in the prompt. Skills are
loaded on demand via `skill_view`. Plugin sections are **frozen once per
session** for byte stability.

### 1.4 The loop

`agent/conversation_loop.py:1535`:
```python
while (s.api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0) or agent._budget_grace_call:
```

Every step is a named *phase helper* returning an `action` ∈
`{fallthrough, continue, break, return}`, dispatched by `_run_phase` which
signature-inspects each helper to pass only the state it names. A budget warning
can be injected into the prompt at a configured ratio, and there is a
**grace call** — one extra model call after the budget is exhausted.

### 1.5 Verification gate

`agent/verify_hooks.py` — a `pre_verify` round-end gate with
`DEFAULT_MAX_VERIFY_NUDGES = 3`. The turn is not allowed to end until there is
*fresh verification evidence*, and the nudge is bounded so it cannot loop.

---

## 2. Claude Code — the disciplined planner

### 2.1 Plan mode is a real artifact

`~/.claude/plans/*.md` — 4 plans on disk, 3–17KB each. The one I read is a
**complete, file-by-file implementation spec**: new tool schemas as literal
Python, the exact helper function bodies, a table of files to change, and a
verification checklist. It references `_MAX_ITERATIONS = 5` in the target
project's own ReAct loop.

This is the same idea as Hermes' `/plan` (`agent/plan_prompt.py`), which is a
prompt injected *as a normal turn* (prompt-cache safe) writing to
`.hermes/plans/YYYY-MM-DD_HHMMSS-<slug>.md`, with the anti-guessing rule:

> "Write the plan for an implementer with zero context for the codebase and
> questionable taste. A good plan makes implementation obvious — if someone has
> to guess, the plan is incomplete."

### 2.2 Roles by configuration, not code

`settings.json` maps Anthropic's tiers onto whatever backend you like:
```
ANTHROPIC_DEFAULT_OPUS_MODEL   = deepseek-v4-pro[1m]
ANTHROPIC_DEFAULT_HAIKU_MODEL  = deepseek-v4-flash
CLAUDE_CODE_SUBAGENT_MODEL     = deepseek-v4-flash
CLAUDE_CODE_EFFORT_LEVEL       = max
```
**Subagents run on the cheap model; the main loop runs on the expensive one.**
The role abstraction is what makes that a config line rather than a code change.

### 2.3 Hooks — 13 lifecycle events

`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`,
`SessionStart`, `UserPromptSubmit`, `Stop`, `StopFailure`, `SubagentStart`,
`SubagentStop`, `TeammateIdle`, `PostCompact` — with matchers and timeouts.

### 2.4 Subagents are first-class and typed

`projects/<proj>/<session>/subagents/agent-<id>.{jsonl,meta.json}`:
```json
{"agentType":"Explore","description":"Trace Monthly MA pipeline flow",
 "toolUseId":"call_00_pB6m0MSJFDfGaOZw4fZ06574"}
```
Each has its own transcript, linked to the parent by `toolUseId`.

---

## 3. omp — the latency engineer

omp is the most sophisticated of the three on *cost and latency*, and it is the
one whose mechanisms most directly answer "make it alive and smooth".

### 3.1 Eleven model roles

`~/.omp/agent/config.yml`:
```yaml
modelRoles:
  default, vision, smol, slow, plan, commit, memory, tiny, task, advisor, judge
```

Every one of these exists to put a *cheap* model where a cheap model suffices.
The four most instructive:

- **`plan`** — a strong model writes the plan; `--prewalk` then **switches to a
  cheap model at the first edit**:
  > "`task.agentPrewalk` arms prewalk, which starts the spawn on its resolved
  > model and hands off to a cheaper one at the first edit or write."
- **`advisor`** — `--advisor` "passively reviews each turn and injects notes".
  A second model watches the first.
- **`judge`** — used for evaluations where a `contains: "PASS"` check would be
  fooled by `"NOT PASSED"`.
- **`tiny`** — a local model for session titles, memory, and word completion.

### 3.2 The between-turns machinery

- **Text-prediction daemon** — a persistent subprocess (`__omp_worker_text_predict`)
  listening on a named pipe, ingesting history from omp's *and Codex's* history
  files, persisted cursor, engines `ngram` + `smollm`.
- **Daemon broker** — `run/daemons/<hash>/` with `broker.pid`, a 64-byte
  `broker.token`, newline-delimited JSON protocol, auth failure string
  "Daemon broker authentication failed", restart backoff 1s→30s.
- **Auto-compaction at 85%** with five strategies:
  `["remote","snapcompact","handoff","shake","soft"]`, plus *speculative*
  compaction ("Speculative compaction armed" → "Applying armed speculative
  compaction" → "Mid-run compaction ran between provider calls").

### 3.3 TTSR — rules injected at edit time

`omp ttsr`: regex `condition` + `scope:"tool:edit(*.ext), tool:write(*.ext)"`,
e.g. `ts-no-any`, Go `io/ioutil`→`io`/`os`, `LazyLock`. **Rules fire on the
edit, not in the prompt** — zero context cost until the moment they matter.

### 3.4 Subagent task wire

One call is `{context, tasks: [...]}`; an item is
`{name?, agent?, task, effort?, outputSchema?, schemaMode?, isolated?, tools?}`.
Budgets: `maxConcurrency=32`, `maxRecursionDepth=2`,
`softRequestBudget=200` (force-stop at 1.5×), `agentIdleTtlMs=420000`.
Plan mode restricts spawns to `read, grep, glob, web_search` and clears
`spawns`/`prewalk`.

---

## 4. What Addled is missing or doing wrong

Ranked by the gap between how much it costs and how much it buys.

### WRONG 1 — Chat does not stream, and the cursor lies

**Evidence.** `chat_stream` exists on `BaseProvider`
(`backend/providers/base.py:97`) and **nine providers implement it** (DeepSeek,
OpenAI, Claude, Gemini, Copilot, Ollama, LM Studio, HF-local) — with **zero call
sites**. `run_chat_pipeline` waits for the whole turn. The dashboard draws
`{content:'', streaming:true}` (`page.tsx:414`) and swaps in the full reply
(`page.tsx:444`).

**How the others do it.** Claude Code: `--include-partial-messages` +
`--output-format stream-json`. omp: `--mode rpc` / `rpc-ui`. Hermes: a real
`stream_callback` plumbed per text delta, plus
`agent/chat_completion_wait_notice.py`, which changes *what the status line
says* during a long silence ("{n}s waiting for the first provider event")
rather than showing a fake cursor.

**Verdict.** This is the largest felt defect and the cheapest to fix: the code
is already written and unused.

### WRONG 2 — Memory is undifferentiated

Addled has one memory store. Hermes splits knowledge into **skills** (loaded only
when relevant, unlimited) and **memory** (universal facts, hard character
budget), with an explicit prompt teaching the model to prefer skills. Addled's
`to_prompt_tools` emits a one-line description per skill
(`registry.py:909-925`) and never loads a skill body on demand — there is no
`skill_view` equivalent, so a skill can never teach anything longer than a line.

**Verdict.** Addled has skills as *tools*, not as *knowledge*. That is the
difference between 55 tools and 58 loadable procedures.

### WRONG 3 — Memory is written but never curated

Addled records to memory. Nobody ever prunes it, merges it, or asks "is this
still true?". Hermes delegates this to the post-turn review fork; Claude Code
delegates it to the `remember` / `episodic-memory` plugins.

### MISSING 4 — No background review / no learning between turns

Nothing in Addled forks the agent to ask "should anything be saved?" after a
turn. The engine observes, but never *learns*.

### MISSING 5 — The turn is a black box

Up to 8 tool rounds (`tool_loop.py:705`) with only `THINKING` shown. Hermes
solves this with `announce_api_call` and the wait-notice phases; omp with a
rich TUI status line; Claude Code with `--include-partial-messages`.

### MISSING 6 — Model roles: 5 vs 11

| Addled | Hermes | omp |
|---|---|---|
| `chat` | chat | `default` |
| `reasoning` | — | `slow` |
| `vision` | vision | `vision` |
| `long` | — | — |
| `utility` | utility/extraction | `smol` |
| — | — | `plan` |
| — | — | `advisor` |
| — | — | `judge` |
| — | — | `memory` |
| — | — | `tiny` |
| — | — | `commit` |

The missing ones (`plan`, `advisor`, `judge`) are not model choices — they are
*architectural behaviours* that need a model slot to exist.

### MISSING 7 — No plan mode

Both Hermes and Claude Code treat planning as a **first-class mode with its own
prompt, its own read-only tool restriction, and a persisted artifact**
(`.hermes/plans/`, `~/.claude/plans/`). Addled's `code.plan` is a two-call
pipeline inside the code page, not a mode the user can enter for any task.
Hermes' rule is worth copying: plan-mode spawns are restricted to
`read, grep, glob, web_search`; no writes, no spawns, no prewalk.

### MISSING 8 — Verification is a nudge, not a gate

`_final_answer()` forces one plain-text round *when tools failed*. There is no
evidence gate: a turn that claims "I fixed it" with no test run ends normally.
Hermes refuses to end the turn without fresh verification evidence, bounded at 3
nudges.

### MISSING 9 — No hook surface

Claude Code exposes 13 lifecycle events; Hermes has plugin hooks at
`pre_tool_call`/`post_tool_call`/`pre_verify`, frozen per session. Addled has no
extension point between "the model decided" and "the tool ran".

### WRONG 10 — Subagents are desks, not typed agents

Addled's swarm desks are long-lived personas. Claude Code's subagents are
**disposable and typed** (`Explore`, with a description and its own transcript);
omp's accept `outputSchema`, `effort`, `isolated`, `tools`, and a depth limit of
2. Neither has Addled's resumption complexity for a one-shot research task.

---

## 5. The three things I would take

If only three ideas transfer, these are they, in order of value.

1. **The post-turn review fork** (Hermes). Learn after every turn, on a daemon
   thread, against the parent's prefix cache, deferred to idle and coalesced.
   This is what makes an agent *accumulate* competence.
2. **Real streaming plus honest wait notices** (all three). Addled already has
   the generators; wire them. Then make the status line tell the truth during
   silence rather than animating a cursor that is not connected to anything.
3. **Roles as configuration** (omp). Eleven roles is overkill for Addled, but
   `plan`, `advisor` and `judge` each unlock a behaviour that is impossible
   without them — and adding the *slot* is a config line once the routing
   already exists, which it does.

## 6. Incidental security finding (unrelated to the above, but worth fixing)

While reading the tools' config directories I found live-looking credentials in
cleartext:

- `~/.claude/plans/generic-leaping-raven.md` — a Telegram bot token and chat_id.
- `~/.claude.json` — a GitHub PAT (`ghp_…` in a project's
  `lastSessionFirstPrompt`), and an OpenAI-compatible provider URL.
- `~/.claude/settings.json` — an `ANTHROPIC_AUTH_TOKEN` (`sk-…`).

Claude Code writes plan files and session prompts verbatim, so anything pasted
into a prompt lands on disk in the clear. These are that tool's business, not
Addled's — but the same hazard applies to Addled's own memory/journal files, and
worth a look.
