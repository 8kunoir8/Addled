# Plan — the trajectory view (what Addled did, and how long it took)

Status: **NOT STARTED.** This plan is derived from
[`docs/research/trajectory-observability-vs-deepseek-harness.md`](docs/research/trajectory-observability-vs-deepseek-harness.md),
which is the evidence base. Every file:line in that document was verified
against this checkout. Nothing in this plan has been written yet.

**Decisions taken (§7): D3 — a new route, not the chat page. D5 — do NOT merge
the journal; the trajectory ships standalone, with no `turn_id` work up front.**
Both reduce risk: D3 keeps Phase E off the busiest file in the dashboard, and D5
removes a whole piece of cross-feature work that turned out to be unnecessary.
D1, D2, D4 and D6 are still open.
Companion reading: `PLAN-command-console.md` — the console is the direct
precedent, and this plan deliberately copies its architecture rather than
inventing a second one.

---

## 1. What this is

A view of **what Addled actually did during a turn** — each tool call, its
arguments, its result, and how long it took — instead of only what it said.

Three motivations, in the order they matter:

1. **The data already exists and is thrown away.** `tool_loop.py:1451` builds a
   record for every tool call; `ws_server.py:2136` keeps only
   `len(tool_results)`. The arguments and results of every tool call are
   available at the moment they are discarded.
2. **Timing is invisible, and the console proves it is valuable.** The console
   shows `duration_ms` per shell command (`CommandConsole.tsx:240-249`, via
   `fmtDuration` at `:47`) and is the only place in the product with any timing
   at all. A turn that takes 40 seconds is currently unexplainable.
3. **Token usage is a confirmed defect.** `ws_server.py:2118` writes
   `{"in": ..., "out": 0}` — output tokens are hardcoded to zero — and
   `dashboard/src/lib/ws-types.ts:60` declares a `tokens` type that a grep for
   `.tokens` across `dashboard/src/` shows is **never read**.

This is not primarily a "build a new feature" job. It is mostly wiring,
plus two small defect fixes.

---

## 2. The design decision that shapes everything

### 2.1 The constraint: `chat_history.json` is rewritten whole, on every message

`chat_history._save()` (`chat_history.py:41-44`) rewrites the **entire file** —
currently **321 KB** — on **every** `add_message`. And `add_message` is called
**twice per turn** (`ws_server.py:2116-2118`: once for the user, once for the
assistant).

So the obvious implementation — attach tool records and timing to the chat
messages — would re-serialize all of it twice per turn, and the cost would grow
with the **length of the conversation**, not the size of the turn. A long
conversation would get quadratically slower. **This rules out the obvious
approach**, and it is the single most important decision in this plan.

### 2.2 The chosen shape: a separate bounded ring, exactly like the console

Tool-call records go into **a new bounded in-memory ring store**, keyed by an
id, filtered by conversation, with its own cap, replayed on demand over an RPC.
That is precisely what `backend/actions/console_log.py` does, and it does it for
the same reason.

Copying it is not laziness — it is the correct call. The console has already
solved every sub-problem this feature has:

| Problem | Console's solution | Reuse? |
|---|---|---|
| Unbounded growth | `_MAX_ENTRIES = 500` ring (`console_log.py:49`) | yes |
| Concurrent writers | `threading.Lock` (`:64`), reached from swarm + tasks | yes |
| One row observed at many points | `_entries` + `_by_id` hold the same dict, updated in place (`:234-237`) | yes |
| Empty panel after reload | `console.list` replay (`ws_server.py:2809`) | yes |
| Panel empty beside a busy conversation | `snapshot(conversation=...)` filter (`:307-324`) | yes |
| Secrets in captured text | `redact()` (`:110`), applied before broadcast | yes |
| Renderer escaping the backend cap | `consoleStore.ts` mirrors 500 (`:39-42`) | yes |

### 2.3 The one place this differs from the console

The console records **commands**. This records **tool calls** — a superset that
includes `web_search`, `browser_extract`, `swarm_delegate`, and every MCP tool,
not only the ones that spawn a process. The console stays as it is; this is a
different lens (a turn's work) over overlapping data. §8.3 addresses the
duplication honestly.

---

## 3. What already exists (grounded in the code, not guessed)

Verified facts this plan relies on:

1. **A tool record is already built, per call** — `tool_loop.py:1451`:
   `{tool, success, result, error, forged, requires_approval}`.
2. **A call id already exists** — `tool_loop.py:1497` mints
   `"addled_call_%d"` when the provider omits one, and pairs every result to a
   call by construction. **This is the natural row key.**
3. **The activity notice already fires immediately before execution** —
   `tool_loop.py:1441-1445`, with the tool name. The start timestamp has a
   natural home one line above `execute_skill` (`:1446`).
4. **`tool_loop` already receives the token split** — `tool_loop.py:1894` and
   `:2073` compute `result.tokens_in + result.tokens_out`, then collapse it.
   Five provider adapters populate both halves (`deepseek_provider.py:82-83`,
   `copilot_provider.py:75-76`, `claude_provider.py:73-74`,
   `gemini_provider.py:58-59`, `lmstudio_provider.py:126-127`).
5. **`chat_with_tools` has exactly ONE call site** — `ws_server.py:2052`.
   (The "many callers" concern applies to `run_chat_pipeline`, which has five:
   `main.py:559`, `main.py:833`, `swarm/orchestrator.py:162`,
   `tasks/actions.py:131`, `ws_server.py:125`. Those are *callers of the
   wrapper*, not of the inner function.)
6. **`run_chat_pipeline` is a wrapper over `_run_chat_pipeline_inner`**
   (`ws_server.py:1186` → `:1441`). So a turn id minted in the **inner**
   function covers every surface — swarm, tasks, voice, bots, dashboard — with
   no per-caller change. This is the correct seam.
7. **`toolResults` is read as an int in five places** —
   `swarm/orchestrator.py:212`, `:224`; `ws_server.py:4372` (with a comment at
   `:4368` stating it is an int), `:4760`, `:4784` (mapped to `toolCalls`).
   **This key is frozen.** It must not become a list.
8. **The pipeline is socket-agnostic** — `run_chat_pipeline` takes no `ws`. All
   broadcast goes through the `get_server()` singleton.
9. **`redact()` is public and importable** — `console_log.py:110`.

---

## 4. Design

Five phases. Each is independently shippable and independently testable.

### 4.1 Phase A — capture per-tool timing (backend, one function)

**Where:** `backend/skills/tool_loop.py`, inside the tool loop (`:1430-1460`).

**What:**
- Read `time.monotonic()` immediately before `execute_skill` (`:1446`) — beside
  the existing activity notice at `:1441`.
- Read it again after.
- Add `started`, `ended`, `duration_ms` to the `tool_results.append({...})` at
  `:1451`.

**Why `monotonic`, not `time.time`:** durations must not jump if the clock is
adjusted. The console already uses `time.monotonic()` this way
(`terminal.py:206/306`).

**Blast radius:** one dict, additive keys. The existing consumers read named
keys only — `_tool_usage_snapshot` reads `tool` (`ws_server.py:896-929`),
`_delegation_ack` reads known keys (`:1409`), and the round-cap summarizer reads
`tool`/`success`/`result`. Adding keys cannot break a named-key reader.

**Risk: low.** No signature change, no new module, no contract change.

### 4.2 Phase C — token usage, done honestly

Do this early: it is a **defect fix**, not a feature, and it is small.

**Two bugs:**

1. **`out` is hardcoded to 0.** `ws_server.py:2118`:
   `tokens={"in": result.get("tokens", 0), "out": 0}`. The value is available
   and discarded.
2. **`tool_loop` collapses the split.** It returns one flat `tokens`
   (`:1182`, `:1257`, `:1289`, `:1367`, `:1423`). `:1894`/`:2073` already have
   `result.tokens_in` and `result.tokens_out` in hand.

**What:**
- Return `tokens_in`/`tokens_out` alongside `tokens` from `chat_with_tools`
  (additive keys; keep `tokens` as the sum for the five existing readers).
- Fix `ws_server.py:2118` to persist the real split.
- **The streamed path estimates.** `tool_loop.py:1761` returns
  `len(text) // 4` — a guess. Where a real count is unavailable, the record must
  say so. Add an `estimated: true` marker rather than presenting a guess as
  fact. This repo has a standing rule about that kind of lie, and this is
  exactly the case it exists for.

**Risk: low**, provided it is additive. The one thing to check is
`providers/budget.py:135` (`reply_budget` takes `prompt_tokens`), which reads a
number and must keep getting one.

### 4.3 Phase D — the trajectory store (new module)

**New file:** `backend/actions/trajectory_log.py` (name TBD in §7-D1),
modelled directly on `console_log.py`.

**Shape:**

```
record(entry) -> dict        # upsert by id; one row per tool call
snapshot(conversation=None)  # the replay, filtered like console.snapshot
new_id() -> str              # or reuse the provider call_id
_MAX_ENTRIES = <cap>         # agreed with the renderer
_lock = threading.Lock()
```

**Row shape (proposed):**

```
id            — the call_id from tool_loop.py:1497 (stable, already unique)
turn_id       — minted per turn (§4.4); groups rows of one turn
conversation  — for the same filtering console does
tool          — the tool name
args          — the arguments, REDACTED
status        — running | ok | failed | denied
success       — bool
duration_ms   — from Phase A
result        — a capped summary, REDACTED
error         — the failure reason, if any
ts / ts_updated
```

**Two rules, both learned from the console's own docstring:**
- **Upsert by id, in place.** `_entries` and `_by_id` hold the *same* dict.
  The console's comment at `:234-237` records that storing a merged copy only in
  `_by_id` broke the live view — `_entries` kept pointing at the original.
- **Redact at the source**, before broadcast, by calling the existing
  `console_log.redact()`. A secret that never leaves the process cannot leak
  through the renderer, a screenshot, or devtools.

**Risk: medium.** A new module is low-risk in itself; the risk is in how it is
reached (§4.4) and capped (§6).

### 4.4 Phase D2 — thread the turn id and record the call

**Mint the turn id** in `_run_chat_pipeline_inner` (`ws_server.py:1441`). This
covers every surface, because all five `run_chat_pipeline` callers funnel
through it (fact 6 in §3).

**Pass it down** to `chat_with_tools` (`ws_server.py:2052` → `tool_loop.py:1078`)
as a **keyword-only parameter with a default of `None`**, so the single
call site changes and nothing else can.

**Record at execution time, not at reply time.** This is the constraint that
matters most, and it comes straight from the bug this whole line of work is
about:

> `write_history` is gated on the reply being non-empty and not a provider error
> (`ws_server.py:2107`). A trajectory record must be written **independently of
> that gate**, or it inherits the same "the work happened and nothing was kept"
> failure.

dsh logs `tool/call` *before* execution; that is why an interrupted call still
has a record there. Addled should do the same: write the row when the call
starts, update it when it finishes. **A tool call that times out, is denied, or
crashes must still produce a row.**

**Risk: medium.** Two touch points, both small; the caution is entirely about
the ordering rule above and the signature shape.

### 4.5 Phase B — stop discarding the records

This is the smallest change in the plan and could be folded into D2, but it is
listed separately because it has the one hard constraint.

**Where:** `ws_server.py:2075` already has `tool_results` in hand; `:2136` keeps
only `len()`.

**What:** nothing needs to escape in the return payload at all — the store is
the destination. If a payload field is wanted for other consumers, it must be a
**new key** (`toolTrajectory`, say).

**THE CONSTRAINT — `toolResults` stays an int.** Five call sites read it
numerically (§3 fact 7). Replacing `len(tool_results)` with a list would break
`swarm/orchestrator.py:212,224`, `ws_server.py:4372`, and the two `toolCalls`
mappings at `:4760,4784` — silently, because they use `.get(..., 0)` defaults.

**Risk: low** if the new key is additive; **high** if anyone "improves"
`toolResults` in place.

### 4.6 Phase E — the RPC and the view

**RPC** (mirroring `console.list`, `ws_server.py:2809`): register
`trajectory.list` next to the console handlers (`:6397-6400`). Replay on mount,
because without it a panel opened after a turn shows nothing — the console's
docstring at `:2810-2816` names this exactly: an empty panel reads as "Addled
has done nothing".

**Renderer store:** `dashboard/src/lib/trajectoryStore.ts`, copying
`consoleStore.ts` — same cap as the backend, `upsert` by id.

**View — the phase with the most regression risk, now much reduced.**
§7-D3 settled it: a **new route** (beside `/console`), not an addition to the
chat page. `dashboard/src/app/chat/page.tsx` is 781 lines of dense, working UI
and is the surface the user looks at most; the new route touches none of it.
Follow the console's split — a page under `app/<name>/page.tsx` plus the shared
renderer store in `lib/`, so the same panel can later be embedded elsewhere
without moving code.

**Honesty rules for the view** (from the dsh comparison, and consistent with
this repo's existing behaviour):
- An in-flight call shows a **start marker and no duration** — never an invented
  elapsed time.
- An estimated token count is **labelled as estimated**.
- A failed call shows its failure reason, not a generic "failed".

**Risk: medium-high.** See §8.

---

## 5. Tests

Every check must be **revert-verified** — break the fix, watch the check fail —
following the pattern of `scripts/check_pipeline_resilience.py`.

A new `scripts/check_trajectory.py`, registered in `scripts/check_all.py` next
to `check_pipeline_resilience.py`.

**Obligations (from the notes' §7, made concrete):**

1. A tool call that **succeeds** produces a row with a non-zero `duration_ms`.
2. A tool call that **fails** produces a row with `success=False` and a reason.
3. A tool call that is **denied** (`requires_approval`) produces a row. *This is
   the console's `awaiting`/`denied` reasoning applied to tools.*
4. A tool call that **times out** produces a row — and given Phase 4 work
   already made timeouts return partial output, the row should show it.
5. **A record is written even when the reply is a provider error** — i.e. it is
   NOT behind the `write_history` gate at `ws_server.py:2107`. This is the
   single most important test; it is the property dsh gets right and the one
   this codebase has been repeatedly wrong about.
6. **`toolResults` is still an int** — assert `isinstance(..., int)` so nobody
   turns it into a list later.
7. **`chat_history.json` does not grow per-tool data** — assert the message
   shape is unchanged, protecting against §2.1.
8. **Cap agreement** — backend ring and renderer store report the same cap.
9. **Redaction** — a tool result containing an `sk-...` key does not reach the
   snapshot in clear.
10. **`tokens.out` is real**, or explicitly marked estimated. Assert it is not
    the constant `0` for a turn where the provider reported output.
11. **No existing caller of `chat_with_tools` changed behaviour** — the
    keyword-only default holds.

Run: `./python-bundle/python.exe -s scripts/check_all.py` — expect
**113 pass, 1 skip** (112 currently + the new one).

---

## 6. The cap, and why it is a real decision

Two caps must agree: the backend ring and the renderer's store. If the backend
keeps 500 and the renderer keeps 200, a reload shows more than the live view
did — which is worse than showing less, because it contradicts what the user
just saw. `consoleStore.ts:39-42` states this reasoning; the same applies.

The trajectory is **more voluminous per turn than the console** (a turn can make
8 tool calls, each with arguments and results). So 500 entries is either ~60
turns (fine) or ~8 turns (useless), depending on the cap's unit. §7-D1 asks
whether the cap is rows or turns.

---

## 7. Decisions I need

**D1. Store name and cap unit.** `trajectory_log.py` vs `tool_trace.py`? And is
the cap **rows** (500 tool calls) or **turns** (200 turns, each holding its
rows)? Rows are simpler and match the console; turns are what a user reads.
*Recommendation: rows, cap 500, matching the console — and revisit if it proves
fewer turns than useful.*

**D2. What `result` retains.** The full tool result (capped) or a summary?
Full results are the point of the feature ("what did it actually get back") but
they are the bulk of the memory and the likeliest place for a secret to hide.
*Recommendation: capped full result (console's `_STREAM_CAP = 8000` is the
precedent), redacted at the source, with `raw(id)` as the one deliberate escape
hatch — exactly as the console did it.*

**D3. Where the view lives. — DECIDED: (b), a new route.** Your call. A new
route beside `/console`, additive, and linkable from the console. This also
takes Phase E off the busiest file in the dashboard (§8.1), which was the main
regression risk in the plan.

**D4. Does `trajectory.list` filter by conversation?** The console does
(`console_log.py:307-324`), and its comment names the trap: without it you show
"an empty console beside a conversation full of work".
*Recommendation: yes, filter the same way, defaulting to the current
conversation.*

**D5. Does the journal merge in? — DECIDED: no merge; separate storage, and
the trajectory ships standalone.**

This was researched properly rather than left as a preference. The finding that
settles it: **the journal's entries never reach the model.** The path is

```
journal.record(role, text[:500])      journal.py:72-91     entries
  -> nightly_summarize()              main.py:453     -> a per-day `summary`
       -> list_days() -> timeline_block()   relevance.py:134
            -> compose_timeline()      journal.py:172    -> "- <date>: <summary>"
                 -> sys_prompt         ws_server.py:1583-1585
```

What is injected is the **`summary`**, never the `entries`. So the journal has
two consumers with different needs — the model reads summaries, the user reads
entries — and a trajectory touches neither.

Consequences, stated plainly:

- **The journal's prompt role cannot be degraded by this feature.** A trajectory
  is never a prompt input; that is the definition of the feature, not a
  limitation of any option (§11).
- **Neither feature goes dead if separated.** They have disjoint consumers.
- **Joining them in the UI is therefore a display choice only** — it would
  never have changed what the model sees. That is why it is safe to defer.

**Decision: separate storage, standalone view (no `turn_id` work in this
plan).** The console already proves a work record is useful with no narrative
at all. Grouping a turn's journal prose with its tool rows is a nice-to-have
that would require adding a `turn_id` to journal entries (additive and low-risk,
but still work). Do **not** do it up front. Revisit only if using the
standalone view shows the narrative is actually missed.

If that day comes, the shape is decided: add `turn_id` to the journal entry dict
(a new key — no prompt reads `entries`), mint it in `_run_chat_pipeline_inner`
alongside the trajectory's turn id (§4.4), and join on it. **Never join on
timestamp proximity** — that is fragile and would be wrong under concurrency.

**D6. Scope of Phase E.** Does E ship in the same job as A–D, or separately?
*Recommendation: separately.* A–D are backend and low-risk; E is the only phase
that can regress a working surface.

---

## 8. Risks, stated honestly

**8.1 The UI is the risk, not the backend — and D3 has mostly retired it.** The
backend changes are additive and testable. The residual risk was
`chat/page.tsx` (781 lines of dense, working UI). D3 chose a **new route**, so
Phase E no longer touches that file at all; the remaining UI risk is ordinary
new-page risk. D6 still recommends shipping E after A–D are proven, because a
new surface built on an unproven data shape tends to be rebuilt.

**8.2 The `toolResults` int is a trap for a future reader.** Nothing in the
current code *prevents* someone turning it into a list; five call sites would
break silently. Mitigation: a test that asserts the type (§5.6), and a comment
at `ws_server.py:2136` recording why it is an int.

**8.3 Duplication with the console is real.** A `run_command` call will appear in
both the console and the trajectory. Defensible (different lenses: the console
is the machine's command history, the trajectory is a turn's work) but it should
be deliberate, stated in both docstrings, and probably cross-linked in the UI
rather than left for someone to "fix" by deleting one.

**8.4 Volumetrics are unproven.** The memory argument (`_MAX_ENTRIES`) assumes a
turn makes a handful of tool calls. A swarm turn could make many more. The cap
must be a constructor argument, not a constant, so it can be tuned without a
rewrite.

**8.5 Honesty is a live risk, not a theoretical one.** The streamed token path
estimates `len(text)//4`. If the view displays that as a count, it is a number
the system made up — the same failure mode as the fixed "I've completed the
requested actions." Mitigation is in §4.2 (the `estimated` marker) and §4.6 (the
view must render it as an estimate).

**8.6 This does not fix anything by itself.** Unlike the disconnect work, none
of A–E repairs a user-visible bug — except C, which fixes token output being
permanently 0. The rest is making visible what currently happens invisibly. That
is worth doing, and it is a different kind of worth; it should not be scheduled
as if it were an outage.

---

## 9. Order of work

1. **Phase A** — per-tool timing. One function, no signature change. Prove it
   with a test that fails when the timing lines are removed.
2. **Phase C** — the token defects. Small, and it fixes a real bug, so it earns
   its place early. Include the `estimated` marker.
3. **Phase D + D2** — the store, the turn id, and recording at execution time.
   The `write_history`-independence test (§5.5) is the gate for this phase.
4. **Phase B** — the additive payload key, with the int-type test.
5. **Stop and review.** A–D are the backend. Show the diff, confirm the store
   shape and the cap (D1/D2) against real recorded data before building UI on
   top of a shape that might be wrong.
6. **Phase E** — the RPC, the store, the view. Separately, and after the data
   shape has been seen working.

Each phase: implement → test → revert-verify → run `check_all.py` → report.

---

## 10. What I will not do without asking

- **Not commit** anything. The standing rule holds.
- **Not change `toolResults`** from an int, or any other existing return key.
- **Not put step data into chat messages** (§2.1) even if it looks simpler.
- **Not touch the journal.** D5 is decided: no merge, no `turn_id` on its
  entries. Its storage and its prompt path are out of scope for this plan.
- **Not deploy** the dashboard, or touch the live install, without approval.
- **Not retrofit dsh's event-sourcing discipline.** A half-done event log —
  lossless-JSON contracts, contiguous `seq`, versioned migrations — is worse
  than a simple bounded ring done properly. The ring is the right shape for
  Addled's size, and the notes explain why in full.

---

## 11. What this plan deliberately does not include

- **Long-history virtualization.** dsh virtualizes the ledger; Addled's cap of
  500 rows makes it unnecessary at this size. Add it if the cap ever grows.
- **A timing *overview* band** (dsh's left-to-right timeline). Attractive, but
  it is a second UI problem on top of the ledger; get the ledger right first.
- **Nested subtool grouping** (dsh nests a sub-call under its parent). Addled's
  `swarm_delegate` and MCP calls could use this, but it needs a parent id in the
  row shape. Defer until the flat shape has been seen in use.
- **Any change to how the model behaves.** This is observation only. Nothing
  here alters a prompt, a tool result the model sees, or a turn's outcome.
