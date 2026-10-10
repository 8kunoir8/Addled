# Meeting notes and summaries — research and plan

What Addled already has, what is genuinely missing, and a plan in phases. The
short version: the transcription is already built and good, the summarisation
surface does not exist, and the hard problem is **live capture**, not
transcription.

---

## 1. What is already there

Measured from the tree, not assumed. This is a large fraction of the feature.

| Piece | Where | State |
|---|---|---|
| Speech to text | `backend/voice/stt.py` | Working. `faster-whisper` local, plus SenseVoice. Nothing uploaded |
| File transcription | `stt.transcribe_file()` | Working, and careful — magic-byte sniffing, 0-byte and mislabelled-file detection |
| Dashboard transcription | `ws_server` `_transcribe_voice_note` | Working. Decodes base64 audio, temp file deleted in `finally` |
| The skill | `transcribe_audio` (category `integrations`) | Working. Its own description already says "Use this for a meeting" |
| Voice activity detection | `backend/voice/vad.py` | Working. Silero VAD, with `SegmentGrabber` — captures the preroll before speech starts |
| Continuous listener | `VoiceListener._run()` | Working, **wake-word gated** |
| Journal | `backend/memory/journal.py` | Working. Per-day entries, timeline composition, pruning |
| Session summaries | `backend/memory/session_summary.py` | Working. Save/list/compose |
| Long-term memory | `vector_store.py`, `facts.py`, `knowledge_graph.py`, `links.db` | Working |
| SOP store | `backend/sop/` | Working. Procedures learned and matched |
| Scheduler | `backend/tasks/scheduler.py` | Working. Recurring tasks, housekeeping hooks |
| Wiki | `backend/wiki/` | Working. Ingest/query/store |

So: **audio in, text out, and somewhere to put it, all exist.** The gap is a
meeting-shaped surface over the top.

## 2. What is actually missing

1. **No capture session.** Everything is one file in, one transcript out. There
   is no notion of "a meeting is happening, keep collecting until I say stop."
2. **The listener is wake-word bound.** `VoiceListener` listens for "Hey Addled"
   and hands the *command* to the engine. A meeting has no wake word; it needs
   to record and transcribe continuously, and not treat speech as instructions.
3. **No timestamps.** `transcribe_file` joins `segment.text` and discards
   `segment.start`. `segments` is right there, so this is cheap — but without it
   nothing can say who spoke when, or "...as we discussed 20 minutes ago."
4. **No speaker separation.** Whisper does not diarise. One microphone in a room
   of five produces a single undifferentiated stream.
5. **No speaker labels in the transcript, so no "what did *I* commit to".**
6. **No notes surface.** No page, no store, no skill. `journal` is per-day prose
   and `session_summary` is per-chat; neither is a meeting with a title,
   attendees, decisions and action items.
7. **No way to act on the output.** Action items should become tasks
   (`backend/tasks/`) and decisions should become SOPs (`backend/sop/`).
   Today a summary would be a dead end.

## 3. The hard parts, named honestly

**a. Live capture is the real feature, and its risk is now measured — and it is
smaller than expected.** I assumed Whisper `small` on CPU (`device="cpu"`,
`compute_type="int8"` — see `stt.py`) would be roughly real-time at best, and
that falling behind would be the hard problem. Measured on this machine with a
30-second file: **5.0x real-time cold, 29x warm.** Warm is the number that
matters for a meeting, because the model loads once and stays loaded.

So the budget is not tight, and a full-hour recording transcribes in a couple of
minutes. Live capture is viable. This does not remove the need for care — the
measurement is on a synthetic signal and real speech is slower, and a busy
machine is slower again — but the design no longer has to be built around a
scarcity that is not there.

The `SegmentGrabber` in `vad.py` is still the right structure, and it already
exists: VAD segments speech on silence, so each utterance is a small independent
chunk with the preroll captured. Transcribe chunks as they close, in a worker,
and the transcript appears while the meeting is still happening. The value is
latency, not throughput — a failure is noticed in the first minute instead of at
the end.

**b. A meeting is not a conversation with Addled.** The listener currently
routes recognised speech to the engine as a command. During a meeting that
would mean Addled *answering* things people say to each other. Capture mode must
be a separate mode that records and transcribes and does nothing else until
asked.

**c. Context length.** A one-hour meeting is ~8,000–10,000 words. The local model
is `qwen3-8b` with an unknown context size (`context_size: None`). A single
"summarise this" call will either overflow or truncate silently. This needs
map-reduce over segment blocks, not one prompt.

`backend/memory/compaction.py` looks reusable and is **not**: it is rolling
*chat* compaction (summarise the oldest third of a conversation, inject the
summary into later turns). It solves keeping a conversation in context, which is
a different problem from summarising one long document in a single pass. So the
blocking-and-reducing has to be written, not borrowed. Checked rather than
assumed, because "there is probably a helper" is how a phase gets scoped wrong.

**d. Can the model summarise? RE-MEASURED, and the answer is YES — decisively.**
This was the biggest open risk in the plan, so it was the first thing tested
against the installed app. The first answer was "yes, once, then unclear" — and
**that answer was wrong**, for a reason worth keeping (see "the mistake" below).
It is now settled.

**The mistake that produced the earlier doubt.** The failures were never the
model. `chat_stream` swallowed every exception and yielded `""`, and an empty
stream was accepted as a successful empty answer, so the batch fallback that
reported the real reason never ran. A dead or slow server and a model that
cannot summarise therefore looked *identical* from a chat client — both
produced nothing. "The model hung on long input" was a conclusion about the
model drawn from a bug in the transport. Fixed and revert-verified; see §5.3.

**Re-measured, with the provider confirmed up first.** Provider `9router`
(`localhost:20128`), model `Voxagent`, 10-segment synthetic meeting (1,035
chars) built with known ground truth planted in it. Call the provider directly
and echo `OK` before trusting a summarise result — which is the rule the first
round broke.

**Result: it recovered every planted item, in 4.25 s, one block, no reduce.**

| Planted | Recovered |
|---|---|
| launch → Sep 22, freeze Sep 15 | ✓ exact, `[01:35]` |
| Tom → migration plan by Friday | ✓ owner **Tom**, `[02:30]` |
| Sarah → vendor contracts | ✓ owner **Sarah**, `[03:50]` |
| Priya → support rota | ✓ owner **Priya**, `[04:40]` |
| review the post-mortem | ✓ owner left `""` — correct, transcript said "I'll" |
| beta decision parked | ✓ filed as an open question, `[03:10]` |
| old API / legal unanswered | ✓ filed as an open question, `[06:10]` |

Nothing invented. Timestamps survived into the output and are what makes each
claim checkable against the transcript — a decision with a timestamp can be
verified; a bare sentence cannot. Owner inference worked on three of four, and
the fourth was left blank rather than guessed.

**Caveats that remain, stated plainly.**

- **This is `9router`, not the local model.** `local_llm.declined` is `true` on
  this machine and the model was never downloaded, so the local path is
  *untested*, not disproven. Whether a local 8B at `ctx: 8192` does as well is
  still open — but it no longer gates the feature, because the pipeline uses
  whichever provider is active.
- **Client-visible privacy consequence.** For this user the active provider is a
  cloud endpoint, so summarising a meeting sends other people's words off the
  machine. That is the §5.2 privacy decision, and it is now live rather than
  hypothetical. It should be surfaced in the UI before this ships, not after.
- **A synthetic transcript is not a real meeting.** Ten clean turns with no
  crosstalk, no false starts and no "sorry, go on" is an easy input. The
  measurement says the pipeline works and the prompt is sound; it does not say
  what happens on an hour of messy speech with `Speaker` labels missing.

**Design rule this confirms:** the summariser must degrade visibly. The code
keeps "the summariser did not answer" distinct from "nothing was found", and
flags a partial result as partial rather than presenting it as the meeting.

**One distinction that still holds:** this model will not call a tool, and that
did not stop it summarising. Those are different tasks, so the summariser asks
in words for a JSON object and never depends on a tool call succeeding. The
reply is parsed out of prose if it arrives wrapped, because models wrap JSON
even when told not to.

**d2. A turn produced a blank reply with no error — FIXED, and the diagnosis
was wrong.** Found while measuring §3d, and worth recording both because it is
worse than the thing it blocked and because the first explanation was incorrect.

**What was actually wrong.** Not the model server. `chat_stream` on the
OpenAI-compatible providers catches every exception and yields `""`, and
`_streamed_answer` then returned a plain `{"response": "", "tokens": 0}` — no
`stream_failed` flag — for a stream that had yielded nothing at all. The caller
read that as a successful empty answer and skipped the batch fallback that
reported the real reason. So the failure was invisible on the streaming path,
which is the one the dashboard chat page uses, and visible on the headless path:

| Path | Before | After |
|---|---|---|
| headless (`on_delta=None`) | `[Provider error: The model did not finish within 6s …]` | unchanged |
| dashboard chat page (`on_delta` set) | `''` — blank, no error | the same named error |

Measured against a server that accepts the connection and never replies. A
**refused** connection is a different case and always failed loudly and fast
(`ConnectError: All connection attempts failed`, ~2 s) — which is why
"the model is not running" never explained the symptom.

**The three measurement rounds lost to this were lost to a transport bug, not to
the model**, and the conclusion first written down — "the model cannot summarise
long transcripts" — was withdrawn. The lesson survives intact and is the reason
§3d now leads with "confirm the provider is up": **a request that returns
nothing is not evidence about anything until you have shown the thing under
test was actually reachable.** Twice in this workstream a failure was diagnosed
from testing internals rather than the path the user takes, and both diagnoses
were wrong.

For this feature it matters twice over. A meeting recorder that silently stops
producing a transcript because a background model died is worse than one that
refuses to start. Live capture must surface the model's state, and a summarise
call must fail loudly rather than return nothing — which the summariser now
does, keeping "the summariser did not answer" distinct from "nothing was found".

**e. Privacy.** This is the most sensitive data Addled will ever hold: other
people's voices, who did not install Addled and did not consent. Recording must
be explicit, visible while active, and stopping must be one action. This is a
requirement, not a nice-to-have.

**f. Consent and legality.** Recording a meeting has legal weight in many
jurisdictions (two-party-consent states, GDPR). Addled should make the state
obvious rather than silently capture. Not a code problem — a design constraint
that shapes the UI.

## 4. Plan

Phases ordered so each one is useful alone and testable. Every phase gets a
revert-verified check suite, per the doctrine already used in this repo.

### Phase 1 — timestamps and speaker turns in transcription

- Return `segments: [{start, end, text}]` from `transcribe_file` alongside the
  joined `text`. Keep `text` exactly as it is; every existing caller
  (`ws_server`, `transcribe_audio`) keeps working untouched.
- Add `start`/`end` formatting as `MM:SS` for readability.
- **Why first:** it is small, it is a pure addition, and every later phase needs
  it. Doing it later would mean redoing the transcript format.

### Phase 2 — a meeting store

- New `backend/meetings/store.py`, following `journal.py`/`session_summary.py`
  conventions (JSON on disk under the data dir).
- A meeting is: `id`, `title`, `started_at`, `ended_at`, `source`
  (mic | file), `segments[]`, `transcript`, `summary`, `decisions[]`,
  `actions[]`, `attendees[]`.
- Skill `meeting_save` / `meeting_list` / `meeting_get`, read-only ones free.
- **Why before capture:** a capture that cannot be stored usefully is a
  recording, not notes.

### Phase 3 — summarise a transcript — DONE

- Skill `meeting_summarise(meeting_id)` producing: a short summary, decisions,
  action items (with owner if inferable), and open questions.
- **Map-reduce, not one prompt.** Split into ~2,000-token blocks, summarise each
  with timestamps preserved, then reduce. The blocking is new code — see §3c for
  why `compaction.py` is not it.
- Must degrade honestly: if the transcript is too long for the model, say which
  part was summarised rather than quietly dropping the rest.
- **Check first:** does `qwen3-8b` summarise acceptably at all? Measure on a real
  transcript before building the UI on top of it.

### Phase 4 — file-based meetings (the safe, complete path) — DONE

- Skill flow: point at a recording → transcribe → save meeting → summarise →
  offer actions. `transcribe_audio` already does the first half.
- A **Meetings** dashboard page: list, transcript view with timestamps, summary,
  and the action list.
- **Why before live capture:** it delivers the full value with none of the
  capture risk, and it is the fallback when live capture fails. If live capture
  never ships, this is still a complete feature.

### Phase 5 — live capture (researched, not built)

> Researched 2026-10-09 against the installed app. Every claim below is measured
> or read from the code; the raw notes are in
> `docs/research/meetings-phase-5-6-notes.md`.

**What already exists, and the sketch did not know about.**

- **Meeting detection is already implemented.** `backend/safety/presence_guard.py`
  has `_detect_meeting()`, which matches the *foreground window title* against
  `zoom meeting`, `microsoft teams`, `google meet`, `webex`, `discord`, `skype`,
  `slack huddle`, `gotomeeting`, `bluejeans`, `whereby`. This is the natural
  "a meeting is starting — offer to record?" trigger, and it means Phase 5 does
  not need to invent one.
- `SileroVAD.feed_and_collect()` returns `(event, segment)`, and
  `SegmentGrabber` keeps a ring buffer whose `mark_start`/`mark_end` return the
  **exact sample span** of a speech segment. That span is the timestamp source:
  `start_sample / SAMPLE_RATE` is a real time offset, which is what Phase 1's
  `segments` format was built to carry.

**The measurement that decides the design.** Per-call transcription cost is
almost entirely **fixed overhead**, not proportional to audio length:

| segment | wall | speedup |
|---|---|---|
| 3 s | 0.98 s | 3.1x |
| 5 s | 1.00 s | 5.0x |
| 15 s | 1.00 s | 15.0x |
| 30 s | 1.05 s | 28.9x |

That 29x figure from Phase 1 is the *file* case and it is the best case. Live
capture feeds short utterances, which are the worst case. Worse, the fixed cost
is model-dependent:

| model | fixed | 1 s utterance | 5 s utterance |
|---|---|---|---|
| `tiny` | ~0.19 s | 5.4x | 25x |
| `small` | ~0.97 s | **1.0x** | 5.1x |

**`stt_model=small` cannot keep up with live speech.** A one-second utterance
takes one second to transcribe — exactly break-even, with no headroom for the
next one. `tiny` is comfortable. So Phase 5 needs its own model setting, and the
live path should default to `tiny`; the file path can stay on `small`.

This is also why "runs in a worker" is necessary but not sufficient: a worker
that falls behind grows an unbounded queue and transcribes the meeting minutes
after it ended. The design needs a **bounded queue with a stated drop policy**
(§3d's rule: degrade visibly, never silently).

**Mic ownership — the central question, answered by measurement.**
`VoiceListener._run()` opens its own `sd.InputStream` and is a module-level
singleton started once at startup. The obvious worry is that a recorder cannot
open a second stream. **Measured: it can.** With a listener-style stream held
open on the microphone, a second `InputStream` on the same device opened and
read successfully — WASAPI runs in shared mode.

That said, it is device-dependent (exclusive-mode drivers, ASIO, some USB and
Bluetooth devices will refuse). So:

- **Ship:** `MeetingRecorder` opens its own stream. Simple, and it works here.
- **Keep understood:** if the open raises `PortAudioError`, fall back to a
  *share* mode where the listener hands completed segments to the recorder.
  Do not build the fallback until a device actually needs it, but do surface the
  failure rather than dying silently.

**A naming trap to avoid.** `character/states.py:37` maps `"in_meeting"` →
`SLEEPING`. That is the *presence guard's* meaning: a meeting is happening, so
Addled should nap. A meeting being **recorded** is the opposite — the character
must be visibly awake and listening. Two different concepts share one word. Add
a new `RECORDING` state; do not reuse `in_meeting`.

The state chain is small and well-defined:
`engine.sig_agent_state.emit` → `state.changed` → `sharedCharacter`
(`useWS.ts`) → `STATE_LABELS` (dashboards `layout.tsx`). Adding a state means
four edits: the `CharacterState` enum, an `AGENT_TO_CHARACTER` entry, an
`animation.py` drawing case, and a `STATE_LABELS` label.

**Live transcription is missing Phase 1's fix.** `VoiceListener._transcribe()`
does `" ".join(s.text for s in segments)` and returns `(text, lang)` — it
**discards `start`/`end`**, the exact defect fixed in `transcribe_file`. Live
capture therefore cannot produce timestamps today, and fixing that is a
prerequisite, not a detail.

**Consent and failure, as requirements not niceties.** §3e and §3f stand:
recording must be explicit, visibly active, and one action to stop. Phase 5 adds
two more that the sketch omits:

- **Stopping must be reachable when the window is not.** If the user switches to
  Zoom full-screen, the stop control must still exist (tray, hotkey, or the
  floating character). A recorder that can only be stopped by finding a hidden
  window is a privacy bug.
- **A dead model must not end the meeting silently.** The transcript stops
  growing, and the user finds out at the end. The partial transcript must be
  visible while recording, and a stalled worker must say so.

**Plan of work.**

1. Return timestamps from the live path (`_transcribe` → segments), mirroring
   `transcribe_file`. Small, and it unblocks everything.
2. `backend/meetings/recorder.py`: `MeetingRecorder` — own stream, VAD-driven,
   bounded worker queue, partial transcript, explicit start/stop, `RECORDING`
   state. Reuses `SileroVAD` + `SegmentGrabber` unchanged.
3. WS surface: `meetings.record.start` / `.stop` / `.status`, plus a
   `meetings.transcript` progress notification so the page can grow the
   transcript live.
4. The page: a record control, the growing transcript, and an unmistakable
   recording indicator.
5. Offer to start when `presence_guard` says a meeting began — **offer, never
   auto-start.** Consent is the whole point.

**Measurement gate before building 2–5:** transcribe 60 s of continuous speech
through the live path (short VAD segments, `tiny`) and show the worker keeps up
with margin. If it does not at `tiny`, this phase's premise is wrong and it
should be re-planned rather than built on hope.

### Phase 6 — act on the output (BUILT 2026-10-10; SOPs cut by design)

> Researched 2026-10-09. The sketch's three bullets turn out to be three
> different sizes, and one is not worth doing.

**Bullet 1 — summaries into recall. Do this; it is the one that pays off.**
There are **two separate recall mechanisms** and the sketch conflates them:

- **The vector store** — `vector_store.add(embedding, category=...)`, where
  `category` is a free-form `TEXT` column with no registry and no validation.
  Indexing a meeting under `category="meeting"` works **today**, with no
  registry change.
- **The link graph** — `memory/links.py:40`, where `KINDS` is a **closed tuple**
  and `normalise_ref()` **raises** on anything outside it. Adding meetings here
  needs a `KINDS` entry, an existence resolver (the `journal` branch at
  `links.py:436` is the pattern), and a store lookup.

So: **do the vector-store half, skip the link graph.** Semantic recall is what
"what did we decide about X last month?" actually needs. The link graph is for
provenance — *which file did this come from* — and a meeting has no file to link
to. Deferring it keeps Phase 6 small and loses nothing the user asked for.

The catch, and it is the real work: **`recall()` filters on
`category="conversation"`** (`recall.py:51,69,87,146`). Indexing meetings under
their own category makes them invisible until a second search path exists. So
the work is: index meetings, add the search, and gate it the way
`timeline_block`/`summaries_block` already are (`relevance.py:134,148`) so an
unrelated meeting is not injected into an unrelated turn.

The assembly point is `ws_server.py:1491-1545`, which already builds the system
prompt from facts → profile → timeline → wiki → SOP → summaries → rolling →
recall. A `meetings_block(query)` slots in beside `summaries_block`.

**Bullet 2 — action items into tasks. Propose, never create.**
`parse_natural_task(text)` is a **regex parser over natural language**
("every <day> at <time>", "remind me to …") and returns `None` when nothing
matched. An action item is `{what, who}`: often no date, and `who` is
**frequently a third party**. "Tom to send the migration plan" is not the user's
task, and silently turning it into one would put someone else's job on the
user's list.

So Phase 6 offers actions as suggestions the user accepts, one at a time,
matching how `forge_skill` asks before acting. It does not write to the task
store on its own.

**Bullet 3 — decisions into SOPs. Not worth doing as described; recommend
cutting it.** A SOP is `{category, title, steps, tools, guards}`.
`learn.record_run()` — the automatic path — only fires on a **successful
tool-using run** and refuses when fewer than `MIN_TOOLS = 2` tools were used.
`store.upsert()` exists for manual writes but still wants steps and tools.

A decision is one sentence with no tools and no steps. Turning it into a
procedure means a **model call inventing the steps** — a guess about how to do
something, presented as a procedure. That is the opposite of what a procedure is
for, and it would poison the SOP store that `build_sop_context` currently trusts
to be learned-from-doing. Cut it, or make it a deliberate user action with the
drafted steps clearly marked as a draft.

**Plan of work.**

1. Index a meeting's summary into `vector_store` under `category="meeting"`
   when `meeting_summarise` succeeds. Include the id and title in metadata.
2. `relevance.meetings_block(query)` — the `summaries_block` pattern: read
   recent meetings, keep only the related ones, compose. Wire into
   `ws_server.py` beside `summaries_block`.
3. `meeting_actions` skill: return the action items as **proposals** with the
   meeting id; a second, confirmed call creates the chosen ones as tasks, with
   `who` preserved rather than assumed to be the user.
4. Delete the summary's vector row when the meeting is deleted (the
   `session_summary` comment records the bug where `delete_category` wiped every
   other row — do not repeat it).


**Verified by experiment**, not just read from the code:

| action | result |
|---|---|
| `add(category="meeting")` | row created, **no exception** — no registry change needed |
| `search(category="conversation")` | **0 hits** — the meeting is invisible to today's recall |
| `search(category="meeting")` | 1 hit, metadata `{id, title}` intact |

**One caveat found while measuring — since corrected.** The process logged
`transformers embedder 'minilm' failed (No module named 'transformers')` and
fell back to a hash embedder. That message was real but it was about the *test
harness*, not the app: it was produced by a process started without its
`site-packages`, where `transformers` genuinely is absent. Run the way the app
runs, the embedder loads — see the correction later in this document. The
conclusion drawn here ("ranking quality is unverified") was therefore wrong in
the direction that mattered least: the ranking is fine, and the *storage* was
what had a bug.

**Measurement gate — BUILT AND RUN 2026-10-09. IT PASSES.**

Planted a meeting summary containing a fact that exists nowhere else (renewal
deadline 14 November, pricing cap 42,000) and asked the live chat for it:

```
Before the fix : "March 14, 2025" — hallucinated, and the same answer with
                 and without the meeting in context
After the fix  : "Per your 2026-10-09 Northwind contract review, the renewal
                 deadline is 14 November and the pricing cap is 42,000 per
                 year."
```

Correct date, correct figure, and it cited the meeting it came from. **Phase 6's
recall half works end to end on the live app.**

**The gate could not pass until a provider defect was found and worked around.**
First run failed, and the cause was not this code: the active endpoint
(`9router`) **discards the `system` role entirely**, so every memory block,
instruction and tool catalogue was being thrown away before the model saw it.
Proven by token accounting — a 200-word system message added exactly 0
`prompt_tokens` while the same text in a user turn added 403 — and located in
9router's shipped message splitter. Neither of its token-saver toggles changes
it. Full write-up: `docs/research/system-prompt-dropped-by-9router.md`.

The workaround is `backend/providers/system_role.py`: detect the drop from the
response's own token count (no extra request) and, only for a provider known to
do it, fold the system text into the first user turn. Per provider, persisted,
and providers that honour the system role are untouched. Checked by
`scripts/check_system_role.py`, revert-verified.

**A separate, unfinished consequence.** With the catalogue now reaching the
model, it began calling tools for the first time — but writes each call in its
own format (`<run_command>…</run_command>`, then `[[list_dir]] …`), not the
fenced JSON the catalogue asks for, and it refused an explicit request to use
that form. XML-shaped calls are now parsed; the bracket form is not, and other
formats will follow. This is a **provider-behaviour workstream, not part of this
plan** — see `docs/research/tool-call-format-model-forever-invents.md`. It does
not affect meetings: summarising and recall are reached through
`skill_registry.execute` and the WS handlers, both verified live.

**Actions half — BUILT 2026-10-10.** `meeting_actions` (`backend/skills/registry.py`)
turns a meeting's action items into tasks in **two steps**, and the split is the
feature:

```
meeting_actions(id)              -> proposals, writes nothing
meeting_actions(id, accept=[0,2]) -> creates only items 0 and 2
```

A bare call returns `{index, what, who}` for each item and touches nothing —
verified with the task file absent afterwards, not merely equal. `who` is
**preserved in the task title** (`"Tom: send the migration plan"`), because a
task reading "send the migration plan" is a different thing from "Tom: send the
migration plan", and silently filing a third party's job on the user's own list
is the failure this shape exists to prevent. An out-of-range index is
**reported** in `skipped`, never quietly dropped: an index that vanishes looks
identical to one that was created. `accept=true` is **refused** rather than
guessed — it does not say *which*.

Checked by `scripts/check_meeting_actions.py` (33 checks), revert-verified four
ways: proposals that create, `true` that creates everything, the owner dropped,
and a silently-skipped index. The fourth **initially passed** — the assertion
read `not success or skipped`, and the `or` accepted a silent drop. Tightened to
require the index be reported, and it now fails as it should.

No date is invented: an action item is `{what, who}` and the meeting rarely gave
a time, so each created task is a 09:00 reminder for the next day with a note
saying so. `parse_natural_task` was **not** used — it is a regex parser over the
user's own phrasing, and re-parsing a third party's sentence through it would
manufacture a date that was never said.

**Bullet 3 (decisions → SOPs) stays CUT**, as reasoned above.

**Open questions I could not settle from the code**

- **Does VAD work on meeting audio?** `SileroVAD` is tuned for one person
  talking into a mic (`threshold=0.5`, `min_silence_ms=200`). A room with
  crosstalk, or worse, a laptop playing a call, is a different signal. The
  segment boundaries — and therefore every timestamp — depend on this. Needs
  measuring on a real recording.
- **How long can recording run?** `SegmentGrabber` holds 30 s
  (`max_s=30.0`), which is fine per-utterance, but a two-hour meeting means a
  very long `segments[]`. Worth deciding whether to bound transcript length or
  page it.
- **What happens to a recording in progress if Addled is closed?** The store
  writes a meeting at the end. A crash mid-meeting loses it. No decision made.
- **Is "offer to record when a meeting starts" welcome or creepy?** A design
  question, not a code one, and the answer is probably "ask once, remember the
  answer".

### Status — Phases 1–4 built, verified and deployed (2026-10-09)

Everything below runs in the installed app at `C:\Program Files\Addled\resources`
and was exercised against the **running** app, not just in tests.

| Piece | Where | Verified by |
|---|---|---|
| Transcript timestamps | `voice/stt.py:_collect_segments`, `_clock` | `check_meetings.py`, revert-verified |
| Meeting store | `meetings/store.py` | `check_meetings.py`, revert-verified |
| Map-reduce summariser | `meetings/summarise.py` | `check_meeting_summarise.py`, revert-verified (6 reverts) |
| Skills | `skills/registry.py`: `meeting_save/_list/_get/_summarise` | driven through the real registry |
| WS surface | `ws_server.py`: `meetings.list/_get/_transcribe/_summarise/_delete/_rename` | driven over a live socket |
| Dashboard page | `dashboard/src/app/meetings/page.tsx` + nav entry | built, deployed, HTTP 200 live |

**The end-to-end result on real audio**, against the running installed app: a
speech recording was transcribed locally into 5 timestamped segments, saved as a
meeting, summarised in ~2 s, and the summary correctly attributed "send the
migration plan by Friday" to **Tom**. Then deleted, leaving no trace.

Two things are asserted specifically because they are the failure modes that
look like success: a **partial** summary is labelled partial rather than
presented as the whole meeting, and a summariser that returns nothing is
reported as "did not answer" rather than as a meeting with no decisions.

`check_meetings.py` also asserts that every `meetings.*` method the page calls is
registered by the backend — a rename on one side only is a dead page that
breaks nothing else, and it was revert-verified by breaking exactly that.

**Still open:** Phase 5 (live capture only — Phase 6 is now built on both
halves). And the privacy caveat in §3d — for this user the active provider is a
cloud endpoint, so summarising sends the meeting text off the machine. That
should be surfaced in the UI before this is offered as a finished feature.

### Deliberately not in this plan

- **Diarisation.** Real speaker separation needs a separate model (pyannote or
  similar) and is a workstream of its own. Label speakers as `Speaker 1/2` from
  VAD turn boundaries — often good enough for notes, and honest about being
  approximate. "Who said this" is the feature most likely to be missed, so it
  should be named as a limit, not faked.
- **System-audio capture** — see the note below; deferred on measured grounds.
- **Real-time summarisation during the meeting.** Tempting, expensive, and it
  competes with transcription for the same CPU. Phase 5's measurement makes this
  worse than it looked: the live path has almost no headroom on short segments,
  so there is nothing left to give a second model.

**System audio — deferred, and now with evidence rather than a hunch.**
Measured 2026-10-09 on the installed app: `sounddevice 0.5.5` is present with
PortAudio 19.7.0 and the WASAPI host API, but **WASAPI loopback is not reachable
through it.** The output devices report `max_input_channels=0`, so PortAudio
will not open them for capture, and there is no loopback device entry to open.
Capturing a call's audio would need `soundcard`, `pyaudio`, or the WASAPI
loopback API directly — **a new dependency**, not a setting.

It is also the harder consent question: recording the *room* captures people
who are physically present, while recording the *call* captures a remote party's
audio that Addled was never given. Keeping it out of Phase 5 is right; when it
is done it deserves its own phase and its own disclosure.

## 5. The decisions to make first

These changed the design, so they were worth answering before Phase 1.
**1–3 are now settled**; the rest are answered where research settled them.

1. **Live capture, or recordings only? — BOTH, and in that order.** Phases 1–4
   deliver meetings-from-files with no capture risk and are shipped. Phase 5
   adds the room, and is planned but not built.
2. **Does the model summarise well enough? — YES, measured.** Re-measured with
   the provider confirmed up first, on a transcript with known ground truth; it
   recovered every planted item in 4.25 s. See §3d. **It is a cloud provider on
   this machine**, so the privacy half of the question is live: summarising
   sends other people's words off the machine. That should be surfaced in the
   UI before live capture ships, not after.
3. **Should the local-model hang be fixed first? — DONE, and the diagnosis was
   wrong.** The transport, not the model, was eating the error: the turn did not
   hang because the model was down, it hung because
   **`chat_stream` swallows every error and yields `""`**, and an empty stream
   was then accepted as a successful empty answer. `_streamed_answer` returned
   `{"response": "", "tokens": 0}` with no `stream_failed` flag, so
   `_call_native_tools` skipped the batch fallback that would have reported the
   reason — and the user got a blank message with no error at all.

   Measured against a server that accepts the connection and never replies
   (the shape a wedged or half-loaded model actually has — *not* the same as a
   refused connection, which fails fast with `ConnectError`):

   | Path | Before | After |
   |---|---|---|
   | headless (`on_delta=None`) | `[Provider error: The model did not finish within 6s …]` | unchanged |
   | dashboard chat page (`on_delta` set) | `''` — blank, no error | same named error |

   That is why the failure looked like a hang: the 120s provider timeout is
   real and does bound the wait, but the streaming path then threw away the
   reason. Fixed in `tool_loop._streamed_answer` — a stream that yields nothing
   is a failure, not an empty answer. Revert-verified, and locked into
   `check_streaming.py` with a wedge provider that fails exactly this way.

   **Corrected in passing:** `providers.active` is `9router` on this machine,
   not `local`, and `local_llm.declined` is `true` — the local model was never
   downloaded. So no measurement was ever blocked by "the local model being
   down"; it was blocked by this streaming bug, and the earlier conclusion that
   the local model "cannot summarise" remains unsupported rather than false.
4. **How much can Addled say about a meeting?** With one mic and no diarisation,
   the honest output is "this was said at 14:32", not "Bob said this". The plan
   should not promise the second.
5. **Where do meetings live?** The Meetings page is the obvious answer, but they
   could instead live inside the existing Memory or Wiki surfaces. A separate
   page is clearer; reusing Memory is less new surface.

## 6. What to do next

**Shipped:** Phases 1–4, deployed and exercised against the running app.

**Next, in order:**

1. **Phase 6's recall half first.** It is the smallest of the remaining work and
   the one that makes the feature *feel* finished — "what did we decide about
   the launch date?" answering from a meeting is the thing a user will actually
   notice. It needs no new model and no new permission. See the Phase 6 plan for
   why it is smaller than the sketch implied (vector store only, not the link
   graph).
2. **Phase 6's action proposals second.** Small, and gated on user confirmation,
   so it cannot surprise anyone.
3. **Phase 5 last, and only after its measurement gate passes.** Live capture is
   the riskiest remaining piece — consent weight, a model that may not keep up,
   and a mic-ownership question — and everything it delivers is already
   available through the file path. Do not build it on an unmeasured premise.

**Explicitly dropped:** decisions → SOPs. A decision has no steps and no tools,
so "becoming a procedure" means a model inventing them. That would put guesses
into the store `build_sop_context` treats as learned-from-doing. Cut rather than
built badly.

**The two things to fix before this is called finished, and neither is a new
feature:**

- **Surface the privacy consequence.** The active provider is a cloud endpoint,
  so summarising uploads the meeting. A user recording a real meeting is
  entitled to know that before they do it, not afterwards.
- **Offer, never auto-record.** `presence_guard` already knows when a meeting
  starts. That is a good moment to *ask*; it is not a reason to start capturing
  other people's voices without being told.

Measured facts this plan is built on, so they can be re-checked:

| Claim | Value | How |
|---|---|---|
| STT speed, cold | 5.0x real-time | 30 s file, `stt_model=small` |
| STT speed, warm | 29x real-time — **on a 30 s file, the BEST case** | second run, model loaded. See the live table below: on 3 s input it is 3.1x |
| Local model context | 8192 | `local_llm.ctx` |
| Local model VM | 4344 MB resident | backend process |
| Whisper segments carry `start`/`end`/`text` | yes | `faster_whisper.transcribe.Segment` |
| `compaction.py` is chat-only, not transcript | — | read the module |
| Model server port | 8090 | `local_llm.port` — **but see below** |
| Active provider (2026-10-09) | `9router` @ `localhost:20128` | `providers.active` in the live settings |
| Local model on this machine | declined, never downloaded | `local_llm.declined: true`, `download_approved: false` |
| Live STT, fixed per-call cost (`small`) | ~0.97 s | 3/5/8/15/30 s segments all land ~1.0 s |
| Live STT, fixed per-call cost (`tiny`) | ~0.19 s | same method |
| Live STT, 1 s utterance (`small`) | **1.0x — break-even** | cannot keep up |
| Second mic stream alongside the listener | opens OK | two InputStreams, same device, WASAPI shared |
| WASAPI loopback via sounddevice | **NOT possible** | output devices report `max_input_channels=0` |
| Audio libs present | sounddevice, numpy, faster_whisper, torch, funasr | `soundcard` and `pyaudio` MISSING |
| `presence_guard._detect_meeting()` | exists, matches window titles | zoom/teams/meet/webex/discord/… |
| `links.KINDS` | closed tuple, raises | `meetings` must be added for the link graph |
| `recall()` category filter | hard-coded `"conversation"` | a `meeting` category needs a second search path |
| `sop.learn.MIN_TOOLS` | 2 | a decision has no tools, so it cannot become a SOP |

The provider rows were added after the first draft claimed
`providers.active = "local"` — true when written, false when re-checked.
`local_llm.declined: true` means the
model is not installed at all, so **summarisation cannot be measured against the
local model on this machine**; any measurement has to go through the active
provider, which is a *cloud* endpoint. That makes §5.2 a privacy decision for
this user, not a benchmark.

One fact worth keeping separate because it cost an hour: a **refused** connection
is not a hang. `ConnectError: All connection attempts failed` arrives in ~2 s. A
wedged server — accepts, never replies — is the hang, and it is bounded by the
provider timeout (120 s hosted, 600 s local).

**The live gate for the actions half did NOT prove what it was written to
prove — recorded because reporting it as a pass would be wrong.**

`.livetest/gate_actions.py` plants a summarised meeting (with one third-party
action item), asks the live app to turn the items into tasks, and checks that
nothing is scheduled without the user's say-so. It reported `GATE: PASS` twice.

Both runs passed for the wrong reason. The model never found the planted
meeting. Its reply was *"I don't see a meetings tool in what I have available
here — there's no 'list meetings' function wired up"*, and it then described a
transcript of "items 0 through 22, one per second" that the gate never created.

That transcript is real, and the cause is test data, not a bug in the feature:

- Rows 233–242 of the live conversation memory hold **synthetic transcripts from
  earlier live summariser tests** ("[00:00] Alex: Let us review item 0 about the
  roadmap…") and their summaries. They are in semantic recall, so the model
  recalled them as the user's own history.
- The gate's own questions then became memory too (rows 290–293), so its second
  run could recall its first.

**The feature behaved correctly in both runs** — nothing was scheduled, the
third party's item was never filed as the user's, and the model explicitly
refused to invent action items ("I can't, without inventing them"). But the
*reason* it could not act was that it could not see a meeting skill at all, so
the run does not demonstrate that the proposal path works end to end.

**What this does establish**, and what remains unproven:

| Claim | Status |
|---|---|
| Nothing is scheduled without confirmation | Held, both runs |
| A third party's item is never silently made the user's | Held, both runs |
| The model proposes rather than creates | **Not shown** — it never found the meeting |
| The planted meeting is reachable by the live model | **Not shown** |

The deterministic half is covered by `scripts/check_meeting_actions.py`
(33 checks, revert-verified four ways), which drives the skill directly and does
not depend on recall. The live, model-driven half needs a clean memory to be
measured on — which is a precondition, not a detail: a semantic-recall feature
whose recall contains previous test runs is being tested against itself.

My own four rows (290–293) were deleted afterwards. **Rows 233–242 were left in
place**: they predate this session, and deleting more of the user's memory is
not a decision for a test cleanup to make. They should be removed deliberately
before any future live measurement.

**Also observed, and unexplained:** the model reported that no meeting tool was
advertised, although `meeting_save/list/get/summarise/actions` are registered
and enabled. Either the relevance gate dropped them for these queries, or the
catalogue it saw was narrower than the registry. The gated path *does* return
`meeting_actions` for "turn the meeting action items into tasks" when measured
in isolation, so this needs a live look at the catalogue actually sent — not
assumed either way.

**Live gate re-run after the tool_brief fix — PASSES, and the reason matters.**

With clean memory the model found the planted meeting and answered correctly:

```
**Meeting:** 2026-10-10 — Phoenix migration review
**Decision:** Cut over on the 14th.
**Action items:**
1. Send the migration plan to the platform team — assigned to Tom
2. Sign off the pricing cap — assigned to you (Steru)
…
Want me to set specific times for either, or should I just create them as
open tasks?
```

It found the meeting, kept Tom as Tom, and **asked before creating anything**.
The earlier denials ("there's no list meetings function wired up") came from the
`tool_brief` prose gap fixed above — same model, same question, one prose block.

**But `tools used: (none)`, and that is the honest part of this result.** The
model did not call `meeting_list` or `meeting_actions`. It answered from the
**recall block** — the Phase 6 half built earlier, which puts recent meeting
summaries into the system prompt:

```
[Meeting notes] …records of meetings the user recorded:
- 2026-10-10 — Phoenix migration review: …
    decision: Cut over on the 14th.
    action: Send the migration plan to the platform team (Tom)
```

So recall is working well enough that the model does not need the tool to
*describe* a meeting. That is a success for Phase 6's first half and it means:

| path | status |
|---|---|
| meetings reachable via recall | **verified live** |
| `meeting_actions` proposal path | **verified deterministically** (33 checks) |
| `meeting_actions` chosen by the model in chat | **NOT shown** — recall pre-empted it |

A gate that asked the model to *act* was answered by the model *knowing*, and
the two are easy to confuse. To exercise the proposal path live the turn must
need something recall cannot supply — a meeting old enough to fall outside the
recall window, or an action on a meeting the model has not been handed.

This is recorded rather than smoothed over: the run is a genuine pass for
*reachability*, and it is **not** evidence that a user can get their action items
turned into tasks by asking.

**The vector index was WRITE-ONLY for a phase — found and fixed 2026-10-10.**

The recall half built earlier wrote vector rows for every summarised meeting
(`store.index()` → `category="meeting"`, `meeting_id` in metadata) and every
check passed. Nothing read them.

```
recall.py:86   vector_store.search(…, category="conversation", …)
recall.py:145  vector_store.search(…, category="conversation", …)
                ↑ both hardcoded; nothing anywhere searched "meeting"
```

Recall worked anyway, because `meetings_block` read the **file listing** and
gated each meeting on keyword/semantic similarity. So meetings *were* reachable
— by word overlap — and the index contributed nothing. Verified live: the
meeting planted by the gate was answered from a `[Meeting notes]` block sourced
from the listing, with `tools used: (none)`.

**Why every check missed it.** `check_meeting_recall.py` asserted that a row
exists, is in the right category, and is findable by
`search(category="meeting")` — all true, and all about the *mechanism*. Nothing
asserted the **app** ever calls it. A capability nothing consumes is not
delivered, however green its tests.

**The fix.** `meetings_block` now retrieves in two passes:

1. `_meetings_by_index()` — `vector_store.search(category="meeting")`, so a
   meeting whose summary shares *meaning* but not words with the question is
   reachable ("when does the subscription expire?" → a summary saying "renewal
   deadline").
2. `_meetings_by_listing()` — the recent-meetings listing, filling the remaining
   slots. This is also the whole path when the embedder is the hash one, which
   is the live configuration here.

The `keep()` gate still runs on whatever the two passes return, so retrieval got
broader and precision did not change.

**A correction, because the previous version of this paragraph was wrong.** It
claimed this machine's embedder is **`hash`** and that recall is keyword-only.
That was measured with `python.exe -s`, and **`-s` suppresses `site-packages`**,
so `transformers` could not import and every check ran against the fallback.
Measured without `-s`, which is how the app actually runs:

* `transformers 5.14.1` and `torch 2.13.0+cpu` **are** installed;
* `embedder_kind()` returns `transformers`, `embedder_id()` is
  `transformers:minilm:v1`, and `embed_text` returns real 384-dim MiniLM
  vectors (MiniLM is fetched from the Hub at runtime; `backend/memory/models/`
  was never the path).

So the index is live, and it is load-bearing. With twelve unrelated standups
present, the query *"when does the subscription expire and what is the maximum
we pay?"* — which shares **no content words** with the summary *"We agreed the
renewal terms and settled the annual pricing ceiling with the vendor."* — reaches
the right meeting through the index and excludes all twelve. The listing path
cannot do that; it matches words, and there are none to match.

**The real defect the investigation then exposed was an embedder mismatch, and
it was ours.** `store.index()` imported `embed_text` from
`backend.memory.recall`, which defines it as the **legacy hash** embedder
("kept for session summaries"). Every meeting row was written as a hash vector
while `_meetings_by_index` searched with MiniLM. Both are 384-dim, so the store
accepted the mixture without complaint: a stored row scored cosine **0.109**
against a fresh embed of *its own text*. `reembed.py`'s docstring describes
exactly this failure — *"cosine similarity between vectors from two different
models is meaningless, so a half-migrated store returns confident nonsense
rather than an error."* The same bug was present, pre-existing, in
`session_summary.py`. Both writers now use `backend.memory.embedding`, and the
four untagged `session_summary` rows were re-embedded; all 169 rows in the live
store now match a fresh embed of their own text.

The retrieval floor was the second half. `_meetings_by_index` searched at
`SEMANTIC_MIN` (0.35, the *gate*) instead of the retrieval floor (0.25) —
reintroducing the bug `recall.py:118-135` documents at length ("using the gate
for both was measured on this machine to admit ~0 semantic candidates... so
every recall came from BM25 alone and 'hybrid search' was a description rather
than a fact"). It now reads `retrieval_min_similarity`, clamped below the gate.

`check_meeting_recall.py` gained behavioural checks for this (it spies on the
two passes and captures the search category, rather than grepping the source).
The first attempt *did* grep the source and was worthless: `category="meeting"`
also appears in a comment in that file, so a revert that discarded the index
still passed on its own prose. Revert-verified against a discarded index result.
