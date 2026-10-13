# Trajectory and observability: what Addled lacks, measured against DeepSeek Harness

Research date: 2026-10-13. Addled findings verified by reading this checkout;
dsh findings verified against `deepseek-ai/deepseek-harness` @ `master`.

This is a **notes** document: what exists, what does not, and what would break.
It is the input to a plan, not the plan. Every claim below carries a file:line.

---

## 0. Why this research exists

The question was: does Addled lose answers, and separately — comparing against
`deepseek-harness` — what does Addled lack that would be good on the dashboard
console? The first half was answered and fixed (five disconnect/timeout defects,
see the pipeline-resilience work). This document answers the second half.

The comparison target is real: dsh ships a **Trajectory tab** — a turn-aware
event ledger with a timing overview — rendered beside Chat in the same
conversation. Addled has nothing equivalent. The question is what that would
take *here*, given how Addled is actually built.

---

## 1. What dsh has (the reference design)

Read from `docs/architecture.md`, `docs/subsystems/session.md`,
`docs/subsystems/conversation.md`, `packages/client/ui-trajectory/README.md`.

### 1.1 The model: turn → step → tool call

dsh names three levels explicitly and logs a boundary event for each:

```
turn/start → step/start → (request/header, user/message, system/message)
  → tool/call → tool/result → step/end → turn/end
```

- A **step** is one model request plus the tools it calls.
- A **turn** is zero or more steps.
- `SessionEventMap` (session.md) is the append-only vocabulary; it is
  **merge-extensible** so a plugin adds event types by declaration merging.

### 1.2 What the Trajectory view renders

From `ui-trajectory/README.md`:

| Feature | Detail |
|---|---|
| Turn/step boundaries | marked in the ledger |
| Grouping | User, Assistant, Tool, **nested Subtool**, compaction |
| Timing overview | fixed band, real start/duration per record |
| TTFT vs decode split | assistant spans divide first-token from decode time |
| Token usage | cumulative + per-request, in an inspector |
| Record inspector | tokens, duration, Input, Output, Timing, images, attachments |
| `run_code` source | numbered, highlighted, `{}` toggle, exact copy |
| Virtualization | opens at tail, pages older history on demand |
| Follow-the-tail | streaming follows; scrolling up **suspends** following |
| Honest in-flight | start marker, **no invented elapsed time** |
| `Between turns` | standalone compaction outside any turn |

The last two matter most: they are the same principle as the disconnect fixes —
**never fabricate what you do not know.**

### 1.3 The persistence discipline

- `tool/call` is logged **before** execution; `tool/result` after.
- `assistant/message` carries `usage` (tokens) *and* the exact stream.
- Every event is lossless JSON; `seq` is contiguous.
- History is **derived** from the log (`deriveMessages()`), never stored twice.

---

## 2. What Addled has today (verified)

### 2.1 The pipeline is a flat function, not an event log

```
chat.send → chat_send (ws_server.py:2625)
         → asyncio.shield(run_chat_pipeline(message, params))   (2639)
         → run_chat_pipeline (1186)  ← takes NO ws argument
         → _run_chat_pipeline_inner (1441)
         → chat_with_tools (tool_loop.py:1078)
             loop: while rounds < max_tool_rounds   (1197)
             per tool: execute_skill → tool_results.append({...})  (1451)
         → back in _run_chat_pipeline_inner: persist + broadcast
         → return a flat dict  (2130-2162)
```

There is **no turn id, no step id, and no boundary event**. Work is a straight
line that terminates in one return dict. (Verified: `run_chat_pipeline` has no
`ws` argument — the socket is used only in `chat_send` to send the reply.)

### 2.2 The tool-result record exists — and is thin

`tool_loop.py:1451`:

```python
tool_results.append({
    "tool": tc["name"],
    "success": exec_result["success"],
    "result": exec_result.get("data", {}),
    "error": exec_result.get("error"),
    "forged": exec_result.get("forged", False),
    "requires_approval": bool((exec_result.get("data") or {}).get("requires_approval")),
})
```

No start time, no end time, no duration, no arguments retained separately.
The call *id* does exist and is well-handled — `call_id` is made when the
provider omits one (`tool_loop.py:1497`) and pairs every result to a call by
construction. **That id is the natural spine for a trajectory row.**

### 2.3 The records are collapsed at the boundary

`tool_results` reaches `ws_server.py:2075`, and the return dict keeps only a
**count** and the **names**:

```python
"toolResults": len(tool_results),                        # 2136
"toolsUsed": [str(r.get("tool") or "") for r in tool_results ...],  # 2142
```

The full records are discarded. This is a second, larger gap than the missing
timing: the arguments and results of every tool call are **available and thrown
away** today.

### 2.4 Persistence sinks and their shapes

| Sink | Writer | Shape | Timing? |
|---|---|---|---|
| `chat_history.json` | `chat_history.py:61` | `{role, content, timestamp, tokens?}` | timestamp only |
| journal (`journal_YYYY-MM-DD.json`) | `journal.py:72` | `{ts, role, text[:500]}` | ts only |
| vector store | `remember_async` | embeddings | no |
| console (`console_log.py`) | `record()` | see below | **duration_ms** |
| telemetry | `tool_loop.py:41-49` | per-skill counters | no |

### 2.5 The console is the one subsystem that already does this right

`backend/actions/console_log.py` is the proven model, and it exists because of
the *same class of bug* this research is about (its docstring: the model claimed
to have run commands it never ran; "a lie and a truth are indistinguishable when
neither is on screen").

Its design, which a trajectory store should copy:

- **A bounded ring**: `_MAX_ENTRIES = 500` (console_log.py:49).
- **A lock**: `threading.Lock` (64) — reached from worker threads, not only the loop.
- **One row per command, keyed by `id`**, updated **in place** (`_by_id` and
  `_entries` hold the same dict, deliberately — merging a copy broke the live view).
- **A durable replay**: `console.list` restores everything recorded before the
  dashboard opened.
- **Redaction at the source**, before broadcast — a secret that never leaves the
  process cannot leak through the renderer.

The dashboard mirrors it: `dashboard/src/lib/consoleStore.ts` keeps the same
500 cap, `upsert`s by id, and replays via `console.list`.

### 2.6 Two confirmed data defects in what already exists

1. **Output tokens are never recorded.** `ws_server.py:2118` writes
   `tokens={"in": result.get("tokens", 0), "out": 0}` — `out` is hardcoded 0.
   `tool_loop` returns one flat `tokens` number, never a split.
2. **The dashboard declares the token type and never reads it.**
   `ws-types.ts:60` has `tokens?: { in: number; out: number }`; a grep for
   `.tokens` across `dashboard/src/` returns **nothing**. The field is written,
   typed, and never displayed.

### 2.7 What the dashboard renders per surface

| Page | Renders about agent activity |
|---|---|
| chat (781 lines) | bubbles, live activity line, tool-name chips, memory anchors, approvals, questions, swarm |
| code (2866 lines) | PLAN step list, diff review, git panel, verify badge — **no** tool calls, timing, tokens |
| console | durable command log **with `duration_ms`, `exit_code`, stdout/stderr** |
| browser (112 lines) | minimal |

Only the **console** shows timing, and only for shell commands. Nothing shows
per-step timing, token usage, tool arguments, or tool results in the chat flow.

---

## 3. The gap, stated plainly

| Capability | dsh | Addled | Gap type |
|---|---|---|---|
| Turn/step identity | first-class events | **absent** | structural |
| Boundary events | `turn/*`, `step/*` | **absent** | structural |
| Tool call logged before run | yes | broadcast only, then discarded | ordering |
| Tool args retained | `tool/call.arguments` | **discarded** (recoverable at 1451) | wiring |
| Tool results retained | `tool/result` + `meta` | **discarded** (count only) | wiring |
| Per-tool timing | yes | **absent** (console has it for shell only) | capture |
| Per-step timing | yes | **absent** | capture |
| Token usage | full, per request | in-count only; out hardcoded 0; not displayed | defect + dead type |
| Trajectory view | dedicated tab | **absent** | UI |
| Long-history virtualization | yes | **absent** | UI |
| Honest in-flight display | start marker, no invented duration | has the spirit (activity strings) | ✅ |

**Three of these are defects in existing code, not missing features**: the `out`
token, the dead `tokens` type, and the discarded tool records. Those are the
cheapest and highest-value.

---

## 4. Implementation feasibility — where each gap lands, and what breaks

### 4.1 The constraint that decides the whole design

`chat_history._save()` (`chat_history.py:41-44`) rewrites the **entire**
`chat_history.json` — currently **321 KB** — on **every** `add_message`. And
`add_message` is called twice per turn (`ws_server.py:2116-2118`).

Therefore: **do not put tool calls, timing, or step data inside chat messages.**
Doing so re-serializes all of it twice per turn, and the cost grows with the
conversation's length, not the turn's size. A long conversation would get
quadratically slower. This is the single most important constraint in the
document, and it rules out the "obvious" implementation.

The correct shape is the one the console already uses: **a separate bounded
ring store**, keyed by an id, with its own cap, replayed on demand.

### 4.2 Gap-by-gap landing points

**A. Capture timing per tool call (smallest useful change).**
- Lands at `tool_loop.py:1441-1460`. The activity notice at 1441 already runs
  immediately *before* execution, so the start timestamp has a natural home.
- Wrap `execute_skill` (1446) with `time.monotonic()` before/after; add
  `started`, `ended`, `duration_ms` to the `tool_results` entry (1451).
- Touches: one function. Risk: **low**. It is additive to a dict that is
  already built and already has a test-covered consumer.
- Blast radius: `_tool_usage_snapshot` (ws_server.py:896) reads only `tool`;
  `_delegation_ack` (1409) reads known keys; the summarizer from the earlier job
  reads `tool`/`success`/`result`. Adding keys is safe.

**B. Retain the full tool records instead of collapsing to a count.**
- The records are already in hand at `ws_server.py:2075`. Today only
  `len()` and names escape (2136, 2142).
- Add them to the return dict, or better, record them into a new ring store
  rather than the return payload (keeps the reply contract small).
- Risk: **medium** — the return dict is a contract several callers read
  (dashboard, bots, swarm, tasks, code page). Additive keys are safe; changing
  existing keys is not. `check_bugfixes.py` and `check_ws_methods.py` cover
  parts of this.
- **`toolResults` MUST stay an int.** Verified: five call sites read it
  numerically — `swarm/orchestrator.py:212` and `:224`, `ws_server.py:4372`
  (`int((found or {}).get("toolResults")...)`, with a comment at 4368 stating it
  is an int), and `ws_server.py:4760`/`:4784` (mapped to `toolCalls`). Replacing
  the int with a list of records would break all five. The full records must go
  to a **new** key — or better, only to the ring store — leaving `toolResults`
  untouched.

**C. Token usage done properly.**
- Two bugs to fix, both small: `out` at `ws_server.py:2118`; and `tool_loop`
  must return the split it already receives from the provider.
- Then the dashboard can start reading the type it already declares.
- Risk: **low** if the change is additive (`tokens` stays, gains a real `out`).
  Verify against the live 9router that a real `tokens_out` is available — see
  open questions.

**D. A turn/step identity and a trajectory store.**
- New module (mirroring `console_log.py`): bounded ring, lock, `record()`
  keyed by a generated turn id, `list()` for replay, an RPC method to fetch.
- Turn id minted in `_run_chat_pipeline_inner` (ws_server.py:1441), passed down
  to `chat_with_tools` as a keyword-only argument with a default of `None` so
  every existing caller is unaffected.
- Risk: **medium**. `chat_with_tools` has many callers (swarm, tasks, voice,
  bots, code). A keyword-only param with a `None` default is the safe shape;
  changing a positional signature is not.
- **Do not** retrofit dsh's full event-sourcing (lossless JSON contracts, seq
  contiguity, versioned migrations). Addled's budget and shape do not justify it,
  and a half-done event log is worse than a simple ring.

**E. The UI.**
- New panel on the chat page, or a new view beside it, reading the store.
- Must follow the console pattern: in-memory renderer store with a matching cap,
  replay via RPC on mount, `upsert` by id.
- Risk: **medium-high** — chat/page.tsx is 781 lines and its footer strip is
  dense. Adding to it is where a regression is most likely. Consider a separate
  route or a collapsible panel rather than expanding the footer.

### 4.3 Constraints that make naive implementations break

1. **Never put step data in chat messages** — see 4.1, the whole-file rewrite.
2. **Never use a positional parameter to thread a turn id** through
   `chat_with_tools`; it has many callers. Keyword-only, defaulted.
3. **Never assume the reply path succeeded.** `write_history` is gated on
   `response_text` being truthy and not a provider error (`ws_server.py:2107`).
   A trajectory record must be written **independently** of that gate, or it
   inherits the same "answer lost" failure this whole line of work is about.
   (Note the ordering: dsh logs the call *before* execution. Addled should
   record the call at execution time, not at reply time.)
4. **The pipeline is socket-agnostic and must stay so.** `run_chat_pipeline`
   takes no `ws`. Trajectory recording must not reintroduce one — record to the
   store, broadcast separately.
5. **Thread-safety.** `journal.py` carries a comment about exactly this: swarm
   workers and scheduled tasks reach the same writer concurrently, and without
   the lock the slower writer overwrote the faster one with no error anywhere.
   A trajectory store is reached from the same places. Use a
   `threading.Lock`, not an asyncio one.
6. **Bounded, with agreeing caps** on both sides (backend ring and renderer),
   or a reload shows more than the live view — consoleStore.ts:39-42 already
   states this reasoning.
7. **Redaction.** Tool results can contain secrets (the console redacts at the
   source). A trajectory store retaining raw tool output must make the same
   decision deliberately, not by omission.
8. **`_announce_turn` ordering.** It announces "two pushes, because they are two
   bubbles" (ws_server.py:932-944) and deliberately skips dashboard-sent turns.
   Any new broadcast must not double-announce a turn.

---

## 5. Open questions for the plan

1. ~~Does the active provider report output tokens?~~ **RESOLVED — yes.**
   Every provider adapter already reads the split: `deepseek_provider.py:82-83`,
   `copilot_provider.py:75-76`, `claude_provider.py:73-74`,
   `gemini_provider.py:58-59`, `lmstudio_provider.py:126-127` all set
   `tokens_in`/`tokens_out` from `usage.prompt_tokens`/`completion_tokens`.
   `tool_loop.py:1894` and `:2073` **already hold** `result.tokens_in` and
   `result.tokens_out` and then collapse them to one number. So the data exists
   and the discard is downstream, at `ws_server.py:2118`. Only remaining caveat:
   the streamed path estimates `len(text)//4` (tool_loop.py:1761) — a token
   display must prefer the reported split and mark an estimate as an estimate,
   not present it as fact.
2. **Where should the trajectory live in the UI?** Options: (a) a panel on the
   chat page, (b) a new route, (c) extend the console page to cover all tools,
   not just shell. (c) is the smallest surface but the console's docstring scopes
   it to commands deliberately.
3. **Retention.** The console cap is 500 entries. Should trajectory share that
   ring, have its own, or be larger? Memory in a long session is the constraint
   its docstring names.
4. **Should the journal merge with this?** The journal is already a daily
   timeline of chat turns (`journal.py:72`). Two timelines (journal and
   trajectory) may be one too many — or they may be correctly different
   (episodic memory vs. work record). Decide explicitly.
5. ~~Does anything already consume `toolResults` as a number?~~ **RESOLVED —
   yes, five call sites do**, so the key is frozen as an int. See 4.2B.
6. **Sequencing.** Recommended: A and C first (small, low-risk, immediate value,
   and C fixes a visible defect), then B, then D, then E. E is the only phase
   that should be considered separately, because it is the one that can regress
   a working surface.

---

## 6. What *not* to copy from dsh

- **The Cordis plugin architecture** and profile/bundle composition. Addled is
  a Python backend with a different maturity budget; this is a large
  restructure for a benefit that is mostly theoretical here.
- **Full event sourcing**: lossless-JSON contracts, contiguous `seq`,
  generation-versioned JSONL with migration packages. A partially-implemented
  event log is worse than a simple bounded ring done well.
- **The merge-extensible `SessionEventMap`**. Addled's equivalent would be a
  dict schema with no compiler to enforce it; the discipline would be
  aspirational.

What *is* worth copying, in order: the **principle** that a tool call is recorded
before it runs (so an interrupted call still has a record); the **honesty** rules
(no invented durations, start markers for in-flight work); and the **view** —
a timing band and a per-record inspector.

---

## 7. Verification obligations for whoever implements this

1. A tool call that fails, times out, or is denied still produces a record.
   (This is the console's `awaiting`/`denied` reasoning, applied to tools.)
2. A record is written even when the reply is a provider error — i.e. it is
   **not** behind the `write_history` gate at ws_server.py:2107.
3. Concurrent writers (swarm + scheduled task) do not lose records.
4. A reload shows the same rows the live view showed (caps agree).
5. No secret reaches the renderer unredacted.
6. `chat_history.json` does not grow per-tool data (assert the file's shape).
7. The new keyword-only parameter changes no existing caller's behaviour —
   run `scripts/check_all.py` and confirm 112+ pass.
