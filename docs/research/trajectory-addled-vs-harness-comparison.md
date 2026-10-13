# Trajectory and observability: Addled vs DeepSeek Harness

Comparison date: 2026-10-13. Both sides verified against source, not notes.

- **Addled**: read from this checkout, at the state where the trajectory work
  landed (`backend/actions/trajectory_log.py`, `backend/skills/tool_loop.py`,
  `backend/ws_server.py`, `dashboard/src/components/TrajectoryView.tsx`).
- **dsh**: `deepseek-ai/deepseek-harness` @ `master`, pushed 2026-10-09
  (clone: `docs/subsystems/session.md`, `packages/client/ui-trajectory/README.md`).

The prior notes document (`trajectory-observability-vs-deepseek-harness.md`)
was written **before** any of this existed. This one measures what was actually
built against what dsh actually does.

---

## The one-line version

| | DeepSeek Harness | Addled |
|---|---|---|
| **What it is** | An event-sourced session runtime | A bounded in-process ring |
| **Scope of the ledger** | The whole session: prompts, requests, compaction, attachments, images, hooks | Tool calls only |
| **History model** | LLM history is *derived* from the log | History is stored independently; the ledger is a side-channel |
| **The ledger is** | The source of truth | A view for the human |

Both now answer "what did the agent actually do". They stop being comparable
at the word *actually*.

---

## How each one is put together

### dsh — the log is the system

From `docs/subsystems/session.md:5`:

> A `Session` is an **append-only log** of typed `SessionEvent`s — the single
> source of truth for an agent's whole interaction history. The LLM message
> history is *derived* from the log, never stored separately; replay is
> re-derivation from the same events.

Events carry an explicit three-level vocabulary (`session.md:34-47,107-121`):

```
turn/start → step/start → request/header, user/message, system/message
  → tool/call {turn, step, callId, name, arguments}
  → tool/result
  → step/end → turn/end {reason}
```

`tool/call` is logged **before execution** and carries the full `arguments`
string. The log type map is **merge-extensible** via declaration merging, so a
plugin adds event types (`compaction/start`, `hook/invoked`) without editing
core — `session.md:11`.

### Addled — a bounded ring beside the pipeline

`backend/actions/trajectory_log.py` (303 lines) is a ring of **500 rows**, one
row per tool call, guarded by a `threading.Lock`, capped at 8000 chars per
field, redacted at the source by importing `console_log.redact`.

A row (`trajectory_log.py:127-142`):

```python
{"id", "turn_id", "seq", "tool", "arguments", "status", "success",
 "result", "error", "duration_ms", "forged", "requires_approval",
 "conversation", "ts"}
```

Note `"status": "running"` is the *initial* value — the row is written **before**
the call and merged in place after, so a crash, denial, or timeout stays
visible rather than vanishing.

It deliberately is **not** the source of truth. History still lives in
`chat_history.json`; the ledger is written at execution time, outside the
`write_history` gate.

---

## Feature by feature

| Capability | dsh | Addled | Who wins |
|---|---|---|---|
| Turn id | `turn/start` event | `turn_id` on each row, minted per turn | tie |
| Step id | `step/start`/`step/end` events | `seq`, contiguous within turn | tie |
| Tool args retained | `tool/call.arguments`, full | `arguments`, capped 8000 | tie |
| Tool result retained | `tool/result` + `meta` | `result`, capped 8000 | tie |
| Per-tool duration | yes | yes, `time.monotonic()` | tie |
| Logged before execution | yes | **yes** (`status:"running"`) | tie |
| Token in/out split | per request | fixed this session; per step still no | dsh |
| TTFT vs decode split | yes, measured | **none** | **dsh** |
| Cumulative token usage | per request + cumulative | turn total only | dsh |
| Non-tool activity in ledger | prompts, requests, hooks, compaction | **tool calls only** | **dsh** |
| Compaction records | first-class, incl. "Between turns" | absent | dsh |
| Attachments / images in inspector | durable, per-session cache | absent | dsh |
| Nested subtool grouping | yes | flat | dsh |
| Long-history virtualization | yes, pages to disk | ring of 500 rows, no paging | dsh |
| Follow-the-tail / suspend on scroll | yes | not implemented | dsh |
| Honest in-flight (no invented elapsed) | start marker | `status:"running"`, `duration_ms:None` | tie |
| Survives restart | yes, durable | **no**, in-memory | **dsh** |
| History derived from log | yes (single writer) | no (two stores) | dsh by design, see below |
| Cost to run | session-length log, disk, re-derivation | 500 rows in RAM | **Addled** |
| Time to add a field | schema + catalog + surface metadata | add a dict key | **Addled** |
| Blast radius of a bug | can corrupt history | cannot; history is separate | **Addled** |

---

## Where Addled is genuinely better

Not everything is a gap. Three real advantages, and they are not accidents:

1. **The ledger cannot corrupt the conversation.** In dsh the log *is* history —
   a bug in the event model is a bug in what the model sees. In Addled the ring
   is a side-channel: worst case the view is wrong, the conversation is intact.
   The `write_history` gate is deliberately *not* in the recording path, so the
   two fail independently.

2. **Cheap and bounded.** No disk growth, no re-derivation cost, ~300 lines. At
   Addled's size (single user, desktop) a 500-row ring is the right size — the
   whole reason dsh needs virtualization is that it keeps everything forever.

3. **A new observation is a dict key.** `duration_ms` this session was one
   field. In dsh a new event type means the schema, the persistence catalog, and
   surface metadata (`session.md:294` governs exactly this).

---

## Where the gap is real, and what it would cost

Ranked by value per unit of work, using what I now know from building it.

### Cheap, high value

- **TTFT.** dsh splits first-token from decode. Addled has `duration_ms` for the
  tool but nothing for the model. The streaming path already sees the first
  token; capturing the timestamp is a few lines in `_streamed_answer`. This is
  the single biggest honest-timing gain left.

- **Token columns on the existing rows.** `tokenUsage` now exists per turn
  (`{in, out}`) and is not shown on the trajectory rows. The plumbing is done;
  it needs a column.

### Medium

- **Persist the ring.** Today a restart empties it. dsh's durability comes free
  from being event-sourced; Addled would need an explicit flush. Worth it only
  if post-hoc forensics matter — the console already has a durable log, so this
  may be redundant.

- **Non-tool rows.** Approval requests, questions, compaction, and the system
  prompt are invisible in Addled's ledger. dsh renders them. This is where
  Addled's view stops explaining a confusing turn: the tool calls are there, but
  "why did it not call anything" is not.

### Expensive, and probably wrong here

- **Virtualization / paging.** Only needed because dsh never forgets. A 500-row
  RAM ring does not need it. Borrowing this would add the harness's complexity
  without its scale.

- **Deriving history from the ledger.** This is dsh's central design bet and it
  is not obviously right for Addled. Addled's `chat_history.json` is a 321 KB
  file rewritten per message; making it derived would mean a rewrite of the
  persistence layer. The benefit (single source of truth) is real but the cost
  is a subsystem, and the failure mode it prevents — two stores disagreeing — is
  already handled by keeping them disjoint.

---

## The honest summary

**What changed this session**: Addled went from *"the tool records exist and are
thrown away"* to *"every tool call is visible with its arguments, result, and
duration, live, and the token split is no longer a lie"*. That is the
`tool args retained`, `tool results retained`, `per-tool timing`, and
`token out hardcoded 0` rows of the old gap table — all closed, verified live.

**What is still a real gap**: the model side. TTFT, decode timing, per-request
token accounting, and any non-tool activity. If a turn is confusing, Addled now
shows you the tools and still cannot show you the *thinking* — including the
common case where there are no tools because the model chose not to call any.

**What is not a gap, despite looking like one**: virtualization, durable logs,
and derived history are consequences of dsh's *scale and its central bet*, not
free-standing features. Copying them without the bet would add cost and no
capability. Addled's ring is the correct design at this size — it is just a
smaller claim: **a view of what happened, not the definition of it.**
