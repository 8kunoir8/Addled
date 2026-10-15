# Plan: optional Kokoro download + TTS fixes

Supersedes the first draft of this file. Builds on `docs/research/tts-system.md`.

**Decision taken:** the Kokoro models ship in **no** build. A fresh install has
no local voice until the user downloads one from Settings → Voice. **fp32 is the
default** (the recommendation was accepted); the quantised variants are noted at
the end as a follow-up, not part of this work.

---

## Part 1 — the model becomes an after-install download

### The win, measured

| | |
|---|---|
| Installer today | 589 MB |
| `kokoro-v1.0.onnx` (fp32) | 325.5 MB |
| `voices-v1.0.bin` | 28.2 MB |
| `silero_vad.onnx` | 2.3 MB |
| **Models total** | **356.1 MB** |
| ONNX via zlib, measured on real bytes | **92–93% of original** |

ONNX holds float32 weights and barely compresses, so this is a genuine
**~331 MB** — 589 MB → **~258 MB**, roughly a 56% smaller installer.

**`silero_vad.onnx` stays bundled.** It is 2.3 MB and drives microphone VAD,
which is not TTS and not this feature. Splitting it out would add a second
"missing model" state and a second download for no meaningful saving.

### Verified facts this plan rests on

Each was checked, not assumed:

1. **The install is read-only.** Writing
   `…\resources\backend\voice\models\_w.tmp` raises `PermissionError [Errno 13]`.
   The download therefore cannot land beside the code.
2. **Our local model files are byte-identical to upstream's.**
   `Content-Length` 325,532,387 matches exactly, and the first 1 MB of
   `voices-v1.0.bin` hashes identically. So the hashes below are genuinely
   upstream's and are safe to pin.
3. **Upstream publishes no `sha256` digest** for these assets (checked the
   release API). A pinned hash is the only integrity story available.
4. **`download.py` cannot be reused as-is.** `fetch_bytes` buffers the whole
   file in memory (`chunks: list[bytes]`) and enforces
   `MAX_ARCHIVE_BYTES = 96 MB`; the model is 325 MB. It would be refused twice
   over.
5. **There is an existing template for every hard part.** `backend/tools/rtk.py`
   downloads on demand with progress broadcast, an `installing()` guard, and a
   provenance file written last. `backend/tools/uv.py` is the same. The
   dashboard already renders its button (`page.tsx:2100-2195`), including the
   subtle fix for "the progress broadcast can arrive before the component has
   rendered" — solved there with a 1s poll while `installing`.
6. **`exit 2` is the suite's "cannot run here"** (`check_all.py:207`). This is
   what a network-dependent Edge check must use, so an offline run skips rather
   than fails.

### Where the model goes

```
backend/app_paths.py:  VOICE_MODELS_DIR = DATA_DIR / "voice" / "models"
```

`app_paths.py` exists for exactly this and already names "downloaded models"
among the things it owns; `PYLIBS_DIR`, `CLI_TOOLS_DIR` and `BROWSERS_DIR` are
the same shape. On a per-machine install `DATA_DIR` is
`%LOCALAPPDATA%\Addled`; in a source checkout it is `backend/memory`.

`tts.py:30-32` computes `MODELS_DIR`/`KOKORO_MODEL`/`KOKORO_VOICES` from
`__file__` as module constants, so they can only point into the install. They
become a **resolution function with a search order**, following `app_paths.py`'s
own documented rule ("reads fall back to the install directory"):

1. the install directory, if the file is present — so an existing install, and
   every source checkout, keeps working untouched;
2. otherwise `VOICE_MODELS_DIR`.

Both `tts.py` (`KOKORO_MODEL`, `KOKORO_VOICES`) and `voices.py:72`
(`KOKORO_VOICES`) use these, so both change together. `voices.kokoro_voices()`
already degrades to `[]` when the file is missing, which is the state the UI
needs.

### The download

New module `backend/voice/model_install.py`, matching `rtk.py`'s shape:

```
status() -> dict          # {installed, installing, percent, phase, detail,
                          #  error, bytes, total_bytes, models_dir, files}
async install(force=False, remote=False) -> dict   # starts, returns at once
async wait(timeout)       -> dict                  # for tests
```

- **Streaming fetch.** Add `fetch_to_file(url, dest, on_progress)` to
  `backend/tools/download.py`, beside `fetch_bytes`, so the trust rules
  (`require_allowed`, HTTPS-only, host allowlist) stay in one place. It must
  have its own ceiling — `MAX_MODEL_BYTES` — not the 96 MB archive cap.
- **Assets and pinned hashes.** From
  `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/`:

  | file | bytes | sha256 |
  |---|---|---|
  | `kokoro-v1.0.onnx` | 325,532,387 | `7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5` |
  | `voices-v1.0.bin` | 28,214,398 | `bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d` |

- **Verify, then move.** Download to `*.part` in the destination directory,
  hash it, and `os.replace` into place only on a match. An interrupted or
  corrupt download can never be mistaken for a complete model. This mirrors
  `rtk.py` writing its provenance file last for the same reason.
- **Provenance file written last** (`voice_models.json`): tag, hashes, bytes,
  timestamp. Its presence is what makes the install complete.
- **`remote=True` refuses**, as `rtk.install` does: a 325 MB download is a
  thing done at the machine, not over remote access.
- **Progress** on `system.voiceModelProgress`, status on
  `system.voiceModelStatus`, start on `system.voiceModelInstall` — the exact
  triple `rtk` uses.

### Packaging and the files that must change

- `electron-builder.yml:35` — under the `backend` `extraResources` entry, add
  `!voice/models/kokoro-v1.0.onnx` and `!voice/models/voices-v1.0.bin`.
  Precedent sits in the same block: `!memory/models/**` is already excluded as
  "Fetched or downloaded per machine, and re-downloaded on demand".
- `.gitignore:40` is **unchanged**. The deploy derives its exclusions from
  `.gitignore` (verified), so a single source of truth already exists and the
  deploy simply stops copying models.
- `scripts/fetch_voice_models.py` **stays** — it is how a developer fills a
  source checkout, and it is what CI and the old build used. Add the pinned
  hashes to it while we are here: today it verifies nothing, and it is the
  same two files.

### Checks that invert (deliberately, not deleted)

This is the point of doing the change carefully: the current checks assert the
model **is** bundled, and a future filter edit silently re-bundling 325 MB is
exactly the regression to prevent.

| check | today | becomes |
|---|---|---|
| `check_packaging.py:431` | asserts `voice/models/kokoro-v1.0.onnx` **is** shipped | asserts it is **not** |
| `check_packaging.py:63` `SHIPPED_DESPITE_IGNORED["voice/models"]` | reason to ship it | entry removed |
| `check_packaging.py:124` `BUNDLED_ON_PURPOSE["backend/voice/models"]` | declares it intentional | entry removed, keep `default_skins` |
| `check_voice.py:40` | `KOKORO_VOICES.exists()` on this machine | present in install **or** data dir (accepts both) |

Both packaging structures carry the warning that an entry matching nothing is
how a real exclusion gets mistaken for a deliberate one, so removing them is
required, not optional.

### Settings → Voice UI

`page.tsx` `VoiceSection` (~1581); the Kokoro row is ~1668 and today only says
`Not installed` with nothing to act on.

The **in-app button** is the primary path. The installer also offers a
**checkbox, default off** (Decision B), so an offline-bound user can take the
download at install time instead; both use the same module, hashes and
verification.

Mirror `page.tsx:2100-2195` (the rtk block, already written for this exact
situation):

- a **Download the local voice (~356 MB)** button when absent;
- progress bar while running, driven by `system.voiceModelProgress`;
- a **1s poll while installing**, because the progress broadcast can arrive
  before the component renders the previous one — the rtk comment says so;
- on success, re-run `voice.voices` so the voice dropdown fills in;
- on failure, the error text, not a silent revert to `Not installed`;
- helper prose in the same spirit as the rtk note ("This used to travel inside
  every Addled build; it does not any more…").

States that must all be handled, because all occur:

| state | UI |
|---|---|
| models in the install (existing user, dev checkout) | unchanged, works |
| absent, never downloaded | "Not installed" + Download |
| downloading | progress, button disabled |
| interrupted / failed | error + retry |
| installed in the data dir | works; voice list present |

**And the engine dropdown must stop offering Kokoro when it is absent**
(bug 2.7). Today selecting it yields silence; with the models gone by default,
that would become the normal case.

### Part 1 order

1. Path resolution (`app_paths` + `tts.py` + `voices.py`) — no user-visible
   change while everything is still bundled.
2. `fetch_to_file` + `model_install.py` + WS methods, driven directly by a
   script the way `scripts/fetch_rtk.py` drives rtk.
3. Settings UI.
4. Flip the packaging and the four checks. Only now does the installer shrink,
   and only now can a missing-model state be tested honestly.

---

## Part 2 — the bugs from the research

Ordered by cost to the user. Each gets a check, per this repo's standard.

### 2.1 Make the fallback two-way — do this first

Highest value, smallest change. `tts.py:153-163` falls back Kokoro → Edge on a
*raise*, but `_speak_edge` **returns** `{"success": False}` rather than raising,
and no Edge → Kokoro path exists. Measured with the real `speak()`: the default
engine `edge` returns `success=False` and speaks nothing, while Kokoro is
installed, works, and takes 1.6 s.

Treat a `success: False` return from either engine like an exception from it.
This alone ends today's live outage, because Kokoro is present and working.

*Check:* `_speak_edge` stubbed to fail, Kokoro real → `speak()` must return
`success=True, engine="kokoro"`. Plus the reverse direction.

### 2.2 Stop claiming success; surface the failure

`ws_server.py:~2951` does `_spawn(_speak_reply(reply))` then logs
`"Auto-TTS: speaking chat reply"` **before** the attempt. `_spawn` discards the
result — `add_done_callback` only removes the task from a set, and nothing reads
the result or the exception (verified). Live log, one second apart:

```
21:24:41 INFO  Auto-TTS: speaking chat reply (58 chars)
21:24:42 ERROR addled.tts: TTS error: 403
```

Log after the attempt with the outcome, and route a failure to the user on the
bubble (`bridge.reply.emit` already carries turn failures).

*Check:* a failing speak produces no "speaking" line, and reaches the bubble.

### 2.3 Re-pin Edge and actually verify it

`edge-tts>=6.1.0` (`requirements.txt:35`, `requirements-core.txt:42`) resolved
to **7.2.8**, which 403s every voice on this machine. **7.3.1 works** (tested in
isolation: 12240 bytes). Raise the floor.

Add a check that Edge really synthesizes — `check_voice.py` mocks both engines
(`fake_edge`/`fake_kokoro` always return `success: True`), so it tests routing
and could never have caught this. It is network-dependent, so it must **`exit 2`
when offline**, per `check_all.py:207`.

### 2.4 One default for `stt_model`

`stt.py:88` reads `default="tiny"`; `stt.py:216` reads `default="small"`;
`DEFAULT_SETTINGS` says `"small"`. With the key unset, file transcription and
live listening disagree. Settle on `"small"` and check the two sites agree.

### 2.5 Dashboard Language fallback

`page.tsx:1584` falls back to `'auto'`; the backend default is `'en'`. The only
fallback in `VoiceSection` that disagrees with the backend.

### 2.6 `en-gb` unreachable in the picker

`kokoro_languages()` returns `en-gb` (8 voices; `bm_george` is the British
default in `LANG_TO_KOKORO`), but `LANGUAGES` (`page.tsx:1575`) has no `en-gb`,
so it cannot be selected. `kokoroMissing` (`:1627`) also compares against a list
containing a value the user cannot pick. Make the two agree, either direction.

### 2.7 Kokoro selectable while absent

A Part 1 consequence, so fixed with Part 1: disable or redirect the engine
choice when the models are missing, rather than letting it produce silence.

---

## Risks: what further investigation changed

The first draft listed these as open risks. Looking at the codebase, most have
a precedent already solved here, and the assessment below replaces guesswork
with what was found.

### Offline installs lose local TTS — much smaller than it looked

A fresh install already needs network for more than this:

- the **Playwright Chromium build** (~150 MB, `backend/browser/auto_install.py`);
- **torch + transformers** (~2.5 GB, the "Install vision support" button);
- **SenseVoice STT**, which is *already* an optional download and is **not
  bundled** — `scripts/fetch_voice_models.py` fetches it behind `--sensevoice`,
  and `backend/voice/models/SenseVoiceSmall/` does not exist on this machine.

That last one settles it: **the sibling half of the voice feature already works
exactly this way.** Kokoro moving to a download does not introduce a new
category — it makes TTS consistent with STT, the browser, and vision. The
regression is real but narrow, and the release note already has a shape:
"SenseVoice and the Kokoro voice both download on first use."

### The saving is only for fresh installs; upgrades keep the old files

Confirmed. NSIS extracts **over** an existing install rather than wiping it, and
the installer is `perMachine: false` (`electron-builder.yml:205`), so an upgraded
user keeps their `resources/backend/voice/models/*.onnx` in place and the
resolution order deliberately uses them.

This was the one genuine **decision**, and it is settled: the installer
deletes them **only when a hash-verified copy already exists in the data
directory**, so it can never take away the user's only working voice. It has a
clean precedent — the installer already runs privileged work after extraction
(`customInstall` in `electron/installer-precompile.nsh`), for the same reason:
`Program Files` is read-only to the app but writable to the installer. See
**Decisions** for the elevated-context hazard that makes a naive implementation
of this wrong.

### A download that dies at 90% restarts

Still true, and now much less important: **every other on-demand download in
this app has the same property** (`rtk.py`, `uv.py`, the browser, torch). With
fp32 chosen, the exposure is 325 MB; and because the install is per-user and
`DATA_DIR` is `%LOCALAPPDATA%\Addled`, a completed download **survives upgrades
and is not removed on uninstall** (verified: no `deleteAppDataOnUninstall`).
So this is paid at most once per machine. Range resume stays a follow-up, not a
blocker.

### Two model copies can exist

Real but well-defined, and the fix is already in the plan: resolution prefers
the install directory, and `status()` must report which path is in use so the
UI never guesses. Worth a check, not a redesign.

### A wrong hash bricks the download

Mitigated as far as it can be: the hashes were computed from copies **proven**
byte-identical to upstream (size exact, first-MB hash identical), and
verification happens before the atomic rename, so a mismatch leaves the previous
state intact. The residual risk is upstream replacing the asset under a fixed
tag, which would break the download loudly rather than corrupt it silently —
the right failure direction.

### New consideration: download during install is possible, and precedented

`electron/installer-precompile.nsh` already does exactly this shape: strip
something from the bundle, put it back in the installer where an elevated writer
exists, with `IfFileExists` guards, a fallback path, and best-effort failure
("a slower first import is far better than a failed install"). It detects the
interpreter, runs it, ignores the result.

So a checkbox in the NSIS UI ("also download the offline voice, ~350 MB,
requires internet") is a **known-good mechanism here**, not an invention. It
also needs `fetch_voice_models.py` (or a small sibling) shipped into
`resources/scripts/`, exactly as `precompile_bundle.py` is. It is more work and
more installer surface; the alternative is the in-app button alone. See
**Decisions**.

## Decisions taken

### A. Upgrade cleanup: delete the old models only if a data-dir copy exists

Chosen. The installer removes `resources/backend/voice/models/*.onnx` **only
when a complete model is already present in the data directory**, so it can
never take away the user's only working copy.

**Implementation hazard, found while checking this is safe.** The installer runs
**elevated**, and `app_paths._resolve_data_dir()` decides by probing whether
`<install>/memory` is writable — which under elevation it *is*, so a naive call
would resolve the data dir to `<install>/memory`, not `%LOCALAPPDATA%\Addled`,
and the guard would look in the wrong place. The app, unelevated, resolves to
`%LOCALAPPDATA%\Addled`. **The installer must therefore not call `app_paths`**
and must not resolve; it must test both candidates directly:

| candidate | how it is reached |
|---|---|
| `<INSTDIR>\resources\backend\memory\voice\models` | a portable/per-user install that writes beside itself |
| `$LOCALAPPDATA\Addled\voice\models` | a per-machine install |

and delete the first only if the second holds a **complete, hash-verified**
model. The check for "complete" is the provenance file being present and valid,
not the file merely existing — a half-written download must not license a
delete.

`ADDLED_DATA_DIR` is a third possible location when a user has set it. It is an
env var the installer cannot see, so the guard covers the two real cases and
declines to delete when neither matches. **Declining is always safe here**: the
cost of a missed cleanup is 356 MB of disk, and the cost of a wrong delete is
the user's working voice.

### B. Installer checkbox (default off) + the in-app button

Chosen. Both paths, with the installer offering rather than assuming.

- The checkbox is **off by default**, so the installer stays small for everyone
  and the offline case is opt-in.
- `electron/installer-precompile.nsh` is the shape to copy: `IfFileExists`
  guards, a fallback path, and **best-effort failure that does not fail the
  install** — an offline install must complete with voice un-downloaded, and the
  in-app button remains the remedy.
- It needs the fetcher shipped to `resources/scripts/`, exactly as
  `precompile_bundle.py` is shipped today. Reuse the same hashes and the same
  verification; two paths, one set of pinned digests.
- The download lands in the **data directory**, not `<INSTDIR>` — and the
  installer runs elevated, so if a user chose a per-machine install the app
  itself still cannot write there. Writing to the data dir is what makes the
  download usable afterwards regardless of install location.

Note this makes the "two model copies" case more likely, not less: an installed-
with-models user has them in the data dir, an upgrading user has them in the
install dir. `status()` reporting which one is in use is now load-bearing for
the UI, not a nicety.

## Follow-ups (explicitly out of scope)

- **Quantised models.** `int8` (92.4 MB) and `fp16` (177.5 MB) against fp32's
  325.5 MB — roughly 30s versus 2 min on a slow line, at an audible quality
  cost. fp32 first, as decided; offer the choice later.
- **Byte-range resume**, to match nothing else in the app having it either.
- **Installer-time download** (Decision B), if the button alone proves annoying.
- **Verify the hashes in `scripts/fetch_voice_models.py`**, which currently
  trusts whatever it downloads.
