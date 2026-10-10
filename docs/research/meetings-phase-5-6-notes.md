# Phase 5/6 research notes (measured, not assumed)

## Phase 5 — live capture

### Building blocks that exist
- `backend/voice/vad.py`: `SileroVAD` (feed_and_collect -> (event, segment)),
  `SegmentGrabber` (ring buffer, `mark_start`/`mark_end` return the exact
  sample span of a speech segment, `preroll_s=1.0`, `max_s=30.0`). Both real,
  both already used by VoiceListener.
- `backend/safety/presence_guard.py` ALREADY DETECTS MEETINGS:
  `_detect_meeting()` greps the FOREGROUND WINDOW TITLE for
  "zoom meeting", "microsoft teams", "google meet", "webex", "discord",
  "skype", "slack huddle", "gotomeeting", "bluejeans", "whereby".
  -> `PresenceGuard.check()` returns (True, "meeting").
  **This is the auto-start trigger, and the original sketch never mentions it.**
- `backend/character/states.py`: `"in_meeting" -> CharacterState.SLEEPING`.
  **CONFLICT**: during a *recorded* meeting the character must be visibly
  ACTIVE, not asleep. Needs a new state (e.g. RECORDING) or a re-map.

### Mic ownership — the hard constraint
- `VoiceListener._run()` opens `sd.InputStream` INSIDE its own thread
  (`stt.py:338`) and holds it for the whole loop.
- `voice_listener = VoiceListener()` is a MODULE-LEVEL SINGLETON (stt.py:482).
- Started once at `backend/main.py:608` when `voice.mic_enabled` (default True).
- `stop()` only sets `_running = False`; the stream closes in the `finally`.
- **No WS method toggles the mic** — it is config-gated at startup only.
=> A recorder cannot open a second stream on the same device safely, and
   cannot share VoiceListener's stream without a refactor. THIS IS THE
   CENTRAL DESIGN DECISION OF PHASE 5.

### Live transcription is missing the timestamp fix
- `VoiceListener._transcribe()` (stt.py:438-456) does
  `" ".join(s.text for s in segments)` and returns `(text, lang)`.
  It DISCARDS start/end — the exact bug just fixed in `transcribe_file`.
  So live capture currently cannot produce timestamps at all.

### System audio (what the plan defers to "Phase 7")
- `sounddevice 0.5.5` installed, PortAudio 19.7.0, WASAPI host API present.
- **Measured: WASAPI loopback is NOT usable through sounddevice.**
  Output devices [22]-[27] report `max_input_channels=0`, so they cannot be
  opened for capture. There is no loopback device entry to open.
- Capture devices that DO open ([28][29][30]) are real mics, peak 0.00000
  (nothing was playing).
- => System audio needs `soundcard`, `pyaudio`, or raw WASAPI loopback:
  a NEW DEPENDENCY. The deferral is correct; now it has evidence.

### Installed: sounddevice 0.5.5, numpy 2.4.3, faster_whisper 1.2.1,
### torch 2.13.0+cpu, funasr 1.4.0, onnxruntime 1.28.0. (soundcard, pyaudio MISSING)

### MEASURED: live transcription throughput (the Phase 5 gate)
Per-call cost is essentially FIXED, dominated by overhead not audio length:

| seg | wall | speedup |
|-----|------|---------|
|  3s | 0.98 |  3.1x |
|  5s | 1.00 |  5.0x |
|  8s | 1.00 |  8.0x |
| 15s | 1.00 | 15.0x |
| 30s | 1.05 | 28.9x |   <- the 29x "warm file" figure

Model matters more than segment length:

| model | fixed cost | 1s seg | 5s seg | 30s seg |
|-------|-----------|--------|--------|---------|
| tiny  | ~0.19s    |  5.4x  | 25.0x  | 128.4x  |
| small | ~0.97s    |  1.0x  |  5.1x  |  29.1x  |

**`small` at 1s speech = 1.0x real-time: it CANNOT keep up on short turns.**
`tiny` is comfortable. So Phase 5's design rule is a minimum segment size
AND/OR a model choice for the live path — not "reuse whatever stt_model is".
The file path can stay on `small` (30s files: 29x). The live path should
default to `tiny` and treat `small` as opt-in with a warning.

## Phase 6 — act on the output

### TWO SEPARATE recall mechanisms, and the sketch conflates them

**(a) The vector store — free-form category, no registry.**
`vector_store.add(embedding, category="meeting", metadata={...})` works today
with NO change to any registry: `category` is an unvalidated TEXT column
(vector_store.py:43).

BUT `recall()` searches with `category="conversation"` — a HARD FILTER
(recall.py:51,69,87,146). So indexing meetings as `category="meeting"` makes
them invisible to recall until a second search is added. That is the real work.

**(b) The link graph — CLOSED registry, raises on unknown kinds.**
`links.py:40` `KINDS = ("fact","triple","memory","summary","journal","wiki","file")`
`normalise_ref()` raises ValueError for anything else (links.py:72).
Adding meetings to the LINK GRAPH needs: KINDS += "meeting", plus a resolver
branch in `_ref_exists`-style code (the "journal" branch at links.py:436 is the
pattern), plus an existence check against the meeting store.

**Recommendation: do (a) and skip (b) in Phase 6.** Semantic recall is what
the feature actually promises ("what did we decide about X last month?").
The link graph is for provenance ("which file did this come from") and meetings
have no file provenance worth linking. (b) can be a later addition — and
noting this keeps Phase 6 small.

### The context assembly point (where a meetings block goes)
`ws_server.py:1491-1545` builds the system prompt from, in order:
  facts_block, profile, timeline_block, wiki, sop, summaries_block, rolling,
  then long-term recall.
A `relevance.meetings_block(query)` slots in beside `summaries_block`, which is
the exact precedent (relevance.py:148). Model:
  - read recent meeting summaries,
  - keep only those semantically related to the query (`keep()`),
  - compose a block.
That mirrors `timeline_block`/`summaries_block` almost line for line.

### Tasks
- `backend/tasks/store.py`: `TaskStore`, `ScheduledTask` dataclass.
- `backend/tasks/parse.py`: `parse_natural_task(text) -> dict | None`, a
  best-effort NATURAL LANGUAGE parser (regex over "every <day> at <time>",
  prefixes like "remind me to"). Returns None when nothing matched.
- Consequence: an action item `{what: "send the migration plan", who: "Tom"}`
  is NOT a task. There is no due date, and "who" is a THIRD PARTY — the user's
  task list is the user's. Feeding "Tom" into a task assigned to the user would
  be wrong.
- => Phase 6 must PROPOSE, not create. And must not silently assign the user
  someone else's action item.

### SOPs — a decision is not a procedure
`store._clean_sop()` needs: category, title, **steps**, **tools**, guards.
`learn.record_run()` is the automatic path and it only fires on a SUCCESSFUL
TOOL-USING run, with `MIN_TOOLS = 2`; it refuses below that.
`store.upsert(raw)` is the manual write path, but still wants steps+tools.

A meeting decision ("we agreed to ship on the 22nd") has NO tools and NO steps.
So "decisions become SOPs" is NOT a mapping — it needs a MODEL CALL to expand
the decision into a procedure, and that procedure is a guess about intent.
=> Recommend: Phase 6 does NOT write SOPs from decisions. Offer it as an
explicit, user-confirmed action, later, and be clear the steps are drafted.
This is the weakest of the three Phase 6 bullets and should be cut or deferred.

### MEASURED: can a second mic stream coexist with the listener's?
YES on this machine. With `VoiceListener`'s-style InputStream (device
"Microphone (Web Camera)", 48kHz, WASAPI) held open, a SECOND InputStream on
the same device opened and read successfully. WASAPI runs in shared mode by
default, so two clients can capture at once.
=> `MeetingRecorder` can hold its OWN stream; no refactor of VoiceListener.
   BUT this is device/driver dependent (exclusive-mode drivers, ASIO, some
   USB mics, and some Bluetooth headsets will refuse). The design must handle
   `PortAudioError` on open by falling back to SHARING: a mode flag on
   VoiceListener that hands completed segments to the recorder. Plan both;
   ship the simple one, keep the fallback understood.

### Character/dashboard state — the chain is small and well-defined
`engine.sig_agent_state.emit(str)` → ws `state.changed` → `sharedCharacter` in
useWS.ts → `STATE_LABELS[characterState]` in layout.tsx.
Adding "recording" means: a CharacterState enum member, an AGENT_TO_CHARACTER
entry, a drawing case (animation.py), and a STATE_LABELS entry.
`states.py:37` maps `"in_meeting" -> SLEEPING` — that is the PRESENCE-GUARD
meaning (a meeting is happening and Addled should nap). A RECORDED meeting is
the opposite. Two different concepts sharing one word: do not reuse it.

### VERIFIED BY EXPERIMENT: the Phase 6 vector-store claim
```
add(category='meeting')            -> row id 1, NO exception (no registry change)
search(category='conversation')    -> 0 hits  (the meeting is INVISIBLE to recall)
search(category='meeting')         -> 1 hit, metadata {id, title} intact
```
Both halves of the plan's claim confirmed: the write needs no registry change,
and the read needs a second search path. The link graph (`links.KINDS`) is a
separate, closed registry and is NOT needed for this.

**Caveat found while measuring — since corrected.** The process logged
`transformers embedder 'minilm' failed (No module named 'transformers')` and
fell back to the hash embedder (similarity 0.355 for a clearly related query).
That logged line was real, and it was *accurate for that process* — it had been
started without its `site-packages`, where `transformers` genuinely is not
importable. It was not a fact about the app. Run the way the app runs, the
embedder loads: `transformers 5.14.1` and `torch 2.13.0+cpu` are installed,
`embedder_id()` is `transformers:minilm:v1`, and vectors are real 384-dim
MiniLM output.

So the inference drawn here — "MECHANISM verified, ranking QUALITY not" — was
wrong, and it was wrong in a way that cost real time later. The ranking is
fine. What actually had a bug was the *storage*: meetings were being indexed
with `backend.memory.recall`'s legacy **hash** embedder while recall searched
with MiniLM, so every stored row was incomparable to every query. See the
correction in `docs/plan-meetings.md`.

## Phase 6 measurement gate — RUN, and BLOCKED BY THE PROVIDER (2026-10-09)

Built `docs/plan-meetings.md` Phase 6's recall half and ran its gate end-to-end
against the real provider. The result is a finding about the *provider*, not
about the code:

```
A. fact in SYSTEM prompt : 'I don't have information about a "Northwind
                            renewal deadline" ...'        <- IGNORED
B. fact in USER turn     : 'The Northwind renewal deadline is 14 November,
                            with a pricing cap of 42,000 per year.'  <- WORKS
```

Three control probes confirm it is the system prompt being dropped, not the
content:
1. "answer with exactly BANANA" in the system prompt -> "Hello! How can I
   help you today?"
2. "the secret code is ZEBRA-7741" in the system prompt -> "there isn't one
   hidden in this conversation or in my instructions"
3. The meeting block itself -> ignored in the system prompt, answered correctly
   from the user turn.

**So the active provider (`9router` @ `Voxagent`) does not honour system
prompts at all.** This is the SAME root cause as the pre-existing "no tool is
ever called live" limitation. Echoing it back wrongly would have looked like the
model hallucinating; it is the prompt being discarded before the model sees it.

**Consequence for this project:** `meetings_block` is built correctly and
gating works, but on THIS machine the block cannot reach the model through the
system prompt. The block is not wasted — a provider that honours system prompts
(any mainstream hosted API, or a local model) would use it. But the gate cannot
PASS here, and it would be dishonest to report it as passing.

**What would make the gate pass:** measure against a provider that keeps the
system role. Not available on this machine today (`local_llm.declined = true`).
