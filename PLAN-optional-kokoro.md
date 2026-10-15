# Plan: make the Kokoro voice models an optional download, and fix the TTS bugs

Builds on `docs/research/tts-system.md`. Two pieces of work, done in this order
because the second is easier to test once the first has moved the model out of
the install:

1. **Optional model download** — drop ~331 MB from the installer, fetch on
   demand from Settings → Voice.
2. **Fix the bugs the research found** — the ones that make TTS fail silently
   today.

---

## Part 1 — the model becomes a download

### Why it is worth doing

Measured, not estimated:

| | |
|---|---|
| Installer today | 589 MB |
| `kokoro-v1.0.onnx` | 325.5 MB |
| `voices-v1.0.bin` | 28.2 MB |
| `silero_vad.onnx` | 2.3 MB |
| **Total models** | **356.1 MB** |
| ONNX compression (measured on real bytes) | ~92–93% of original |

ONNX stores float32 weights, so it barely compresses. Removing it is a real
**~331 MB** off the installer — 589 MB → **~258 MB**, a ~56% reduction.

`silero_vad.onnx` is 2.3 MB and is needed for microphone VAD, which is a
different feature from TTS. Keep it bundled; splitting out 2 MB is not worth
the failure mode.

### What must not regress

The research established that "gitignored but shipped on purpose" is currently
*checked*, and that check is what this change inverts. Both must be updated
deliberately, not deleted:

- `scripts/check_packaging.py:431` asserts `voice/models/kokoro-v1.0.onnx`
  **is** shipped. It must become the opposite assertion — that it is **not**
  shipped — because a future filter edit silently re-bundling 325 MB is the
  exact regression this plan exists to prevent.
- `scripts/check_packaging.py:124` (`BUNDLED_ON_PURPOSE`) and `:63`
  (`SHIPPED_DESPITE_IGNORED`) name `voice/models`. Both entries lose their
  reason and must be removed, or the "an entry that no longer matches anything
  is how a real exclusion gets mistaken for it" guard will itself become stale.
- `scripts/check_voice.py:40` asserts `KOKORO_VOICES.exists()` on this machine.
  It must become "present in *either* the install or the data dir", or it will
  fail on every fresh install — including CI.

### Where the model goes

**The install is read-only.** Verified: writing
`C:\Program Files\Addled\resources\backend\voice\models\_w.tmp` raises
`PermissionError [Errno 13]`. So the download cannot go beside the code.

Use `backend/app_paths.py`, which already exists for exactly this and already
names "downloaded models" as one of the things it owns:

```
VOICE_MODELS_DIR = DATA_DIR / "voice" / "models"
```

This follows the established pattern of `PYLIBS_DIR`, `CLI_TOOLS_DIR` and
`BROWSERS_DIR` — all artifacts that are downloaded per machine and must not
live in `Program Files`.

`tts.py:30-32` currently computes paths from `__file__` as module constants, so
they can only ever point into the install. They must become a **resolution
function with a search order**, matching `app_paths.py`'s own documented rule
("reads fall back to the install directory"):

1. the install directory, if the file is there — so every existing user, and
   every source checkout, keeps working with no change at all;
2. otherwise `VOICE_MODELS_DIR` in the data dir.

Both `tts.py` (`KOKORO_MODEL`, `KOKORO_VOICES`) and `voices.py:72`
(`KOKORO_VOICES`) read these, so both change together.

### The download

**Do not reuse `backend/tools/download.py`'s `fetch_bytes`.** It buffers the
whole file in memory (`chunks: list[bytes]`) and has
`MAX_ARCHIVE_BYTES = 96 * 1024 * 1024` — the model is 325 MB, so it would be
refused on both counts. Add a streaming-to-disk sibling in the same module so
the trust rules stay in one place.

Follow `backend/tools/rtk.py`, which already solves every hard part of this:

- background `asyncio` task, `installing()` guard, `wait()` for tests;
- progress broadcast to the UI (`system.rtkProgress` → a `voice.modelProgress`
  counterpart) rather than a silent 325 MB wait;
- **verify before placing**: upstream publishes **no `digest`** for these
  assets, so pin the hashes we computed from the copies we already have and
  trust:

  | file | sha256 |
  |---|---|
  | `kokoro-v1.0.onnx` | `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5` |
  | `voices-v1.0.bin` | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

  A hardcoded hash is the *only* integrity story available here, and it is a
  genuine one: the asset is a fixed release tag, so the hash cannot legitimately
  change.
- **verify then move**: download to a temp name in the same directory and
  rename only after the hash matches, so an interrupted download can never be
  mistaken for a complete one. `rtk.py` writes its provenance file last for the
  same reason.

Source: `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/`
(the URLs `scripts/fetch_voice_models.py` already uses). Confirmed reachable.

### Bundling, packaging, deploy

- `electron-builder.yml:35` — add `!voice/models/kokoro-v1.0.onnx` and
  `!voice/models/voices-v1.0.bin` under the `backend` `extraResources` entry.
  Precedent for this exists in the same block: `!memory/models/**` is already
  excluded as "Fetched or downloaded per machine, and re-downloaded on demand".
- `.gitignore:40` stays as-is. The deploy derives its exclusions from
  `.gitignore` (one source of truth, already verified), so nothing there needs
  editing.
- `scripts/fetch_voice_models.py` stays — it is how a **developer** gets the
  models for a source checkout, and it is what a bundled build used before.
  Remove the `(!) fetched by scripts/fetch_voice_models.py and bundled on
  purpose` justification from `check_packaging.py`, not the script.

### Settings → Voice UI

`dashboard/src/app/settings/page.tsx` `VoiceSection` (~1581). Today the Kokoro
row says only `Not installed` (`:1668`) with no way to act on it.

Add, beside the Kokoro Voice row:

- a **Download the local voice (~356 MB)** button when the models are absent;
- progress while running, driven by the new WS progress event;
- on completion, refresh the voice list so the dropdown fills in;
- on failure, the error text — not a silent revert to `Not installed`.

Must handle, because each is a real state on a real machine:

| state | shows |
|---|---|
| models in the install (existing user, dev checkout) | nothing new; works as today |
| no models, never downloaded | "Not installed" + Download button |
| downloading | progress, button disabled |
| failed / interrupted | error + Download retry |
| installed in the data dir | works; row shows the voice list |

The engine dropdown should also stop presenting Kokoro as a free choice when
it is not installed — today selecting it yields silence (bug 2 below).

### Sizing option worth considering

Upstream also publishes quantised weights:

| asset | size |
|---|---|
| `kokoro-v1.0.onnx` (fp32, current) | 325.5 MB |
| `kokoro-v1.0.fp16.onnx` | 177.5 MB |
| `kokoro-v1.0.int8.onnx` | 92.4 MB |

`int8` is **92 MB vs 325 MB**. As a download the user actually waits for, that
is the difference between ~30s and ~2 min on a slow line. It is a real quality
tradeoff (quantised audio is audibly rougher) so it should be a decision, not a
silent default — **recommend: ship fp32 first, revisit int8 as a follow-up.**
Worth noting the `.bin` voice pack is unaffected either way.

---

## Part 2 — fix the bugs the research found

Ordered by what they cost the user. Each has a check to prove it, per the
existing standard in this repo.

### 2.1 Make the fallback two-way (highest value)

`tts.py:153-163` falls back Kokoro → Edge on a *raise*, but `_speak_edge`
**returns** `{"success": False}` instead of raising, and there is no Edge →
Kokoro path. Measured with the real `speak()`: default engine `edge` returns
`success=False` and speaks nothing, while Kokoro is installed, works, and takes
1.6 s.

Change: treat a `success: False` return from either engine the same as an
exception from it — try the other engine before giving up. This is the change
that turns today's live outage into a non-event.

*Check:* with `_speak_edge` stubbed to fail and Kokoro real, `speak()` must
return `success=True, engine="kokoro"`.

### 2.2 Stop claiming success, and surface the failure

`ws_server.py:~2951` does `_spawn(_speak_reply(reply))` and then logs
`"Auto-TTS: speaking chat reply"` **before** the attempt. `_spawn` discards the
result (`add_done_callback` only removes the task from a set; nothing reads the
result or the exception — verified). Live log, one second apart:

```
21:24:41 INFO  Auto-TTS: speaking chat reply (58 chars)
21:24:42 ERROR addled.tts: TTS error: 403
```

Change: log after the attempt with the outcome, and let a failure reach the
user (a bubble is the natural surface — `bridge.reply.emit` already exists for
turn failures).

*Check:* a failing speak must produce a log line that does not say "speaking",
and must reach the bubble.

### 2.3 Re-pin and actually verify Edge

`edge-tts>=6.1.0` (`requirements.txt:35`, `requirements-core.txt:42`) resolved
to **7.2.8**, which 403s on every voice on this machine. Tested in isolation:
**7.3.1 works**. Raise the floor to a version that works, and add a check that
Edge can really synthesize — `check_voice.py` mocks the engines, so it tests
routing and would not have caught this.

Note this is network-dependent, so a check must skip cleanly offline rather
than fail.

### 2.4 One default for `stt_model`

`stt.py:88` reads `default="tiny"`, `stt.py:216` reads `default="small"`, and
`DEFAULT_SETTINGS` says `"small"`. With the key unset, file transcription and
live listening disagree. Pick one — `"small"`, matching config — and add a
check that both sites agree.

### 2.5 Dashboard Language fallback

`page.tsx:1584` falls back to `'auto'`; the backend default is `'en'`. It is
the only fallback in `VoiceSection` that disagrees with the backend. Low
impact, but it is drift of the kind that gets believed.

### 2.6 `en-gb` is unreachable in the picker

`kokoro_languages()` returns `en-gb` (8 voices, and `bm_george` is the British
default in `LANG_TO_KOKORO`), but `LANGUAGES` (`page.tsx:1575`) has no `en-gb`
entry, so it cannot be selected. Also affects the `kokoroMissing` warning at
`:1627`, which compares against a list containing a value the user cannot pick.
Either add `en-gb` to `LANGUAGES` or drop it from Kokoro's advertised set —
they must agree.

### 2.7 Kokoro selectable while absent (new, introduced by Part 1)

Once the models can be absent, the engine dropdown must not offer Kokoro as a
silently-failing choice. Either disable it with a reason, or make selecting it
offer the download. This is a Part 1 consequence, so it is fixed with Part 1.

---

## Order of work

1. **Model resolution first** (`app_paths` + `tts.py` + `voices.py`), with the
   install still bundled. Nothing changes for any user, and it is the piece
   everything else depends on.
2. **Download plumbing** (streaming fetch, hashes, progress, atomic rename),
   tested by driving it directly the way `scripts/fetch_rtk.py` does.
3. **Settings UI** — button, progress, states.
4. **Flip the packaging** — `electron-builder.yml`, the three checks. Only now
   does the installer actually shrink, and only now can a missing-model state
   be tested honestly.
5. **The bug fixes** (2.1–2.6), each with its check.

## Risks

- **A wrong hash bricks the download.** Mitigated by having computed them from
  working copies, and by verifying before the rename so a mismatch leaves the
  previous state intact rather than a corrupt file.
- **Enterprise/offline installs lose TTS.** Today Kokoro works with no network;
  after this a fresh install needs one download. **Edge already needs network**,
  so nothing working today worked offline-only except Kokoro-on-a-bundled-
  install. Worth stating in the release notes; the fp32/quantised choice and a
  "silent-install with models" switch would both be follow-ups.
- **Upgrade path.** An existing install has the models in `Program Files`; a new
  installer that omits them must not leave the old copy orphaned-but-used in a
  confusing way. Because resolution prefers the install directory, an upgrading
  user keeps working with their existing files and simply never needs the
  download — which is the behaviour we want, but it means the 331 MB saving
  only lands for **fresh** installs until the old files are removed. Decide
  deliberately whether an upgrade should clean them up.
- **Byte-range resume is not in this plan.** A 325 MB download that dies at 90%
  restarts. `rtk.py` has the same property; matching it is acceptable, but it
  is a visible papercut and a candidate follow-up.
