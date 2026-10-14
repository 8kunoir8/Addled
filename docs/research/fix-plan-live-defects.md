# Fix Plan — Live Defects Found 2026-10-14

Companion to `bug-research-false-completions.md`, which holds the evidence.
Status of every item is stated plainly; nothing here is committed.

---

## What was fixed

### 1. `chat_stream` never received the tool catalogue — **FIXED, verified**

**Root cause of the incident.** `openai_provider.chat_stream` built its payload
without a `tools` key and its signature had no `tools` parameter. The live app
streams **every** round (`ws_server.py:2177` always passes `on_delta`), so every
tool-shaped question was asked of a model that had been shown no tools.

Measured live: asked to delete a file, the model answered *"I don't have a
`delete_file` tool available. The tools I have for filesystem work are
`run_command`, `read_file`, `write_file`…"* — **correct**, and the reason the
turn did nothing.

**Changes:**
- `providers/base.py` — `chat_stream` gains `tools: list[dict] | None = None`.
- `providers/openai_provider.py` — the payload is now a dict; `tools` and
  `tool_choice` are added **only when non-empty**, so a provider that does not
  understand the key gets a byte-identical request.
- `tool_loop._streamed_answer` — takes `tools`, forwards them behind a
  `TypeError` guard so an un-updated provider keeps working.
- `tool_loop._stream_round` — passes `skill_registry.to_openai_tools(only)`.

**Verified:** `_stream_round` now returns `None` (falls back to the batch path,
where tools run) instead of leaking text as the answer. Checked in
`scripts/check_streaming.py` — 6 new assertions on the signature and the call.

**Risk: low.** Absent tools ⇒ identical payload. Unknown keyword ⇒ `TypeError`,
caught and retried without it. Worst case is the previous behaviour.

### 2. No guard against a completion claimed but never run — **FIXED, verified**

`tool_loop.py:693` documented this gap in its own words: the existing guards
catch *"I'll run it"* and *"running it now"*, and *"I ran it" is a different
defect* — which had no guard, and reached the user four times.

**Change:** a third guard, mirroring the promise nudge.
- `_claimed_done(text)` — completed-action frames (`I deleted`, `successfully
  written`, `reported success`, `it's gone`, `Done —`, `confirmed deleted`),
  vetoed by `_CANT_PAT` (refusals and honest failures) and by `_announced_work`
  (a reply still promising is not claiming completion).
- `MAX_CLAIM_NUDGES = 1` — one retry, then the claim is replaced with
  `_CLAIMED_NOTHING_ANSWER` (translated for the 6 languages the app answers in).

**Measured precision:** 8/8 fabrications caught; 9/9 honest replies untouched
(5 refusals/failures, 4 promise shapes). The promise cases **must** not fire, or
the two guards fight and one turn costs two extra model calls.

**One shape deliberately out of scope:** the stock-hosts fabrication presented
invented *content* rather than claiming an action, so there is no completion
frame to match. Forcing a match would mean keying on the topic — the whitelist
failure bug 2 documented. Recorded, not guessed at.

### 3. `set_brightness` always lied; `set_volume` could not work — **FIXED, verified**

- `set_brightness` ran PowerShell, **discarded the output**, and returned
  `{"success": True, "level": n}` on every path. Measured: exit code 1, `Not
  supported`, tool still claimed success. **Wrong 100% of the time.**
- `set_volume` shelled out to `nircmd` — never bundled — behind an
  unconditional `{"success": True}`.

**Changes:**
- New `backend/actions/audio.py` — pure-`ctypes` Windows Core Audio, because
  `pycaw`/`comtypes` are not guaranteed present. `set_volume` sets **and reads
  back**, reporting failure if the endpoint disagrees.
- `system_controls.set_volume` delegates to it.
- `system_controls.set_brightness` emits a `FAIL`/`OK` marker on stdout,
  checks it, and returns the display's real answer.

**Verified live:** 30/65/0/100/45 all round-trip exactly; brightness reports
`Not supported` with a clean message. Checked in `check_system_controls.py`.

**A note on the first attempt.** The initial fix used `SendMessage`/`VK_VOLUME_UP`,
exited 0, and **left the volume at 100%** — caught only by reading the endpoint
back. That is why the shipped version reads back, and why the check asserts the
readback rather than the return code.

### 4. `write_file` could not create an empty file — **FIXED, verified**

`registry.py:198` treated `""` as missing, so `write_file(content="")` was
rejected before reaching `FileOps.write`, which would have written the file.

**Change:** a schema-declared `x-allow-empty` list, so the next skill that needs
this declares it rather than editing the validator. `write_file` declares
`["content"]`. **`path` is not listed** — an empty path is genuinely missing,
and the check asserts that too.

**Verified:** a real 0-byte file is created; empty path and missing content are
still refused.

---

## What was NOT a bug

**The guidelines "0 chars" report.** The live turn said both packs were stale
and unfetched. Running the real handler in a fresh process shows `ponytail:
2721 chars`, `karpathy: 2345 chars`, both `cached, not stale`. **The live report
was fabricated** — an instance of item 2, not a cache defect. Nothing to fix.

---

## The finding that changes the picture

**The provider was failing during the entire investigation.**

`9router` (`localhost:20128/v1`, `default_model: Voxagent`) routes to
`deepseek-v4.1-flash` via OpenRouter. Measured:

| Test | Result |
|---|---|
| `model=Voxagent`, small | **works** |
| `model=deepseek-v4.1-flash` | **402** — "can only afford 49 tokens" |
| `provider.chat()` × 12 | **12/12 empty** (`ok=True`, no content) |
| Same, minutes later × 18 | **18/18 OK** |

The gateway floors requests to 300 tokens; the account had ~49. The 402 arrives
as **HTTP 503**, and mid-stream it yields `ok=True` with empty content.

**Consequence:** some live turns ran on a provider returning nothing, which is
why the failures were erratic and some turns in the same run were flawless.

**User action needed:** switch the active provider (Settings → Providers) to one
with credit, or point `9router` at a model it can fund. **No code change can fix
an unfunded account** — and until it is fixed, live behaviour will stay
intermittent no matter what ships.

---

## Verification status

| Item | Isolated | Regression check | Live |
|---|---|---|---|
| 1. stream gets tools | verified | `check_streaming.py` | **pending — provider down** |
| 2. completion guard | verified (8/8, 9/9) | `check_announced_not_called.py` (90) | **pending — provider down** |
| 3. volume/brightness | verified on the real machine | `check_system_controls.py` | pending |
| 4. empty write | verified (real 0-byte file) | `check_announced_not_called.py` | pending |

**Nothing is committed.** Last commit remains `ea05fad`.
