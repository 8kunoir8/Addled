# Making Addled Feel Alive — Design Plan

Status: **proposal, awaiting approval.** No code written yet.

## The finding that shapes everything

Addled's brain is already capable. The architecture map confirms:

- **5 routed model roles** — `chat`, `reasoning`, `vision`, `long`, `utility`
  (`backend/providers/router.py:38`), chosen by heuristics with no extra model
  call. "debug", "architect", a 12-line code block, or a long message all route
  to the reasoning model automatically.
- **An 8-round agentic tool loop** (`backend/skills/tool_loop.py:705`), native
  function-calling for OpenAI/DeepSeek/Gemini/OpenRouter, prompt-injected tools
  for everything else.
- **Goals that re-plan** — `MAX_ROUNDS = 3` (`backend/goals/executor.py:35`),
  with an agent that reads its own failure and retries.
- **Swarm desks** that run the same pipeline; a **code plan → edit → verify**
  flow; **6 background loops** doing observation.

So "can the model do everything it needs" is largely **already true**. The gap
is not capability. It is that the user cannot *see* any of it, and the
capability is silent.

Three concrete causes, each verified in code.

---

## Cause 1 — Chat does not stream, and the cursor is a lie

**`chat_stream` is dead code.** `BaseProvider` declares it
(`backend/providers/base.py:97-107`) and **nine providers implement it** —
DeepSeek, OpenAI, Claude, Gemini, Copilot, Ollama, LM Studio, HF-local. A
repo-wide search finds **zero call sites**.

The live path calls the batch `chat()` instead. The dashboard draws
`{role:'assistant', content:'', streaming:true}` (`page.tsx:414-416`), waits on
a single `await send('chat.send', …)` (`page.tsx:441-444`), then swaps the whole
reply in at once (`fillReply`, `page.tsx:48-58`). The blinking cursor is
decoration on a request that has not started streaming.

This is the single largest contributor to "not smooth".

### Design: final-answer-only streaming

Per the decision, stream **only the last round's prose**, never tool-call
rounds. Half-formed tool JSON in the UI is worse than a clean pause.

**Why final-round-only is the right cut, not a compromise:** during tool rounds
the model's text is often narration that gets discarded ("I'll check that
file"). Streaming it would show text that later vanishes. The final round is the
only one whose prose is the actual answer.

**Mechanism.**
1. New `chat.delta` broadcast, emitted via the existing `broadcast_nowait`
   (`ws_server.py:221-252`) — already thread-safe and already fans out to the
   floating character through `_notify_ui` (`ws_server.py:323-331`).
2. A streaming-capable variable `on_delta` threaded from the pipeline into
   `chat_with_tools`, used **only** on the round that will be the last one
   (i.e. the round where the model returned no tool calls — detected after the
   fact).
3. The hard part: you cannot know a round is final until it finishes. Two
   options, decided at build time by measurement:
   - **(a) Peek-and-stream:** call batch `chat()`; if it has no tool calls,
     re-issue as a stream. Costs one extra call. Rejected.
   - **(b) Stream-then-suppress:** stream every round, buffer the deltas, and
     only *emit* them once the round is known to be final. Costs nothing and is
     always correct. **Chosen.**
4. If the provider has `supports_streaming = False` (local HF), fall back to the
   existing batch behaviour silently. No regression for local models.
5. Dashboard: subscribe `onNotification('chat.delta', …)` (`useWS.ts:230`) and
   append to the placeholder already in state. `fillReply` stays as the
   terminator, so a delta stream that dies mid-way still resolves correctly.

**Risk:** a stream that errors after emitting deltas. Mitigation: deltas are
additive; `fillReply` on the final result overwrites, so the worst case is a
visible correction, never a lost answer.

---

## Cause 2 — The turn is a black box

During up to 8 tool rounds the user sees `THINKING` and nothing else. Push
channels already exist and already work — `chat.tools` (`ws_server.py:1468`),
`chat.activity` (`ws_server.py:1656`), `memory.anchors` (`ws_server.py:1459`) —
but only the pre-run catalogue and attachments are reported. The loop itself is
silent.

### Design: emit the loop as it runs

1. Add a `chat.step` broadcast from inside the tool loop at the two places that
   already have the data:
   - **round start:** `{"round": n, "of": max_rounds}`
   - **tool executed:** reuse the existing per-round `tool_results` shape
     (`tool_loop.py:769-783` — already carries `tool`, `success`, `result`,
     `error`, `forged`, `requires_approval`).
2. Justification for the seam: `tool_loop.py:769-783` is the **single place**
   every tool result is appended, for both the native and prompt paths. One
   emit site covers all providers.
3. A `_step_reporter` callback is passed into `chat_with_tools` the same way
   `on_delta` is — a no-op default, so the tool loop stays usable with no server
   attached (it is exercised by many checks).
4. Presentation: a compact, human line per step — the tool *name*, not raw JSON.
   `chat.step` also feeds the floating character, so the character can react to
   its own work (it already has `ACTING` and `WORKING` states).

**Explicitly rejected:** narrating steps in prose. That costs one model call per
step and replaces facts with plausible-sounding text.

---

## Cause 3 — Nothing happens between turns

The engine ticks every 5 s (`observation.light_interval_s`, config.py:601) and
does presence, observation, one goal step, memory maintenance, scheduler, and
mood. But it only *watches*. It has no budget to **act on its own initiative**
beyond insights.

### Design: a bounded initiative budget

This is the part that risks feeling like an annoying assistant, so it is
constrained by construction.

1. **A single new subsystem**, `backend/initiative.py`, that the engine's tick
   consults. It may propose at most one action per tick, and only when every
   gate below passes.
2. **Existing throttles it must respect** (all already in the code):
   - presence guard: no speaking during `meeting`, `gaming`, `quiet_hours`,
     `away` (`presence_guard.py` precedence order, engine.py:247-259)
   - insight cooldown: 600 s for non-deep observations (`engine.py:422`)
   - greeting cooldown: `initiative.greeting_cooldown_min`, default 30
     (`config.py:486`)
   - deep observation interval: `observation.deep_interval_s`, default 300
     (`config.py:604`)
3. **New settings** (new `initiative` group, all defaulted OFF or
   conservative so nothing changes until asked):
   - `initiative.enabled` — default **False**. Nothing happens unless enabled.
   - `initiative.daily_budget` — max self-started actions per day (default 5).
   - `initiative.min_gap_min` — minimum minutes between them (default 20).
   - `initiative.allow_speech` — may it put words on screen, or only prepare
     silently (default False = prepare only).
4. **What it may do**, in ascending invasiveness:
   - *observe and remember* — always allowed, no user-visible effect
   - *prepare* — pre-compute a suggestion, surfaced only if the user asks
   - *speak* — requires `allow_speech`
   - *act* — requires explicit per-action approval through the existing
     approval gate (`actions/approval_notice.py`), never autonomous by default
5. **Continuity with goals:** a goal already persists as JSON
   (`backend/goals/store.py:45-56`) with `status`, `plan`, `checkpoints`, so
   initiative may advance an existing goal rather than invent work. This is
   preferred over inventing new goals.

**Explicitly rejected:** an unbounded "curiosity loop". The observed failure
mode of such systems is a stream of low-value interruptions, and there is no
way to test for "annoying" other than the user turning it off.

---

## Cause 4 (smallest) — Verification before replying

`_final_answer()` (`tool_loop.py:665-698`) already forces one plain-text round
when tools failed. The gap is that a *successful* tool run is never
self-checked. Low priority; included for completeness and to be built last.

---

## Build order

1. **Streaming** (`chat.delta`) — highest felt impact, most contained.
2. **Visible loop** (`chat.step`) — shares the callback plumbing with (1).
3. **Initiative budget** — new subsystem, new settings, most risk, gated off.
4. **Verification** — only if the first three land well.

## Verification approach

Each step gets a standalone `scripts/check_*.py` registered in
`scripts/check_all.py:SUITES`, per house convention, and **each check is
revert-verified** (break the code, watch it fail) before it is trusted.

- Streaming: a fake provider whose `chat_stream` yields known chunks; assert the
  deltas are emitted in order, that tool rounds emit none, and that a
  non-streaming provider still returns the full text.
- Visible loop: assert one `chat.step` per executed tool, matching the
  `tool_results` entries one-for-one.
- Initiative: assert every gate blocks independently — presence, cooldown,
  budget exhaustion, `enabled=False` — and that the default configuration
  produces zero actions.

## What I will NOT do

- Not stream tool-call rounds.
- Not narrate steps with extra model calls.
- Not enable initiative by default.
- Not let initiative act without the existing approval gate.
- Not touch `run_chat_pipeline`'s role in being the single funnel for every
  surface — everything is added as callbacks with safe defaults.
