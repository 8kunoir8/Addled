# The TTS system: model, function, dashboard, bundling, and bugs

## What it is

`backend/voice/tts.py` — Kokoro (local, offline) with edge-tts (online) as a
fallback. `backend/voice/voices.py` describes the voices the pickers offer.

- **Kokoro 82M ONNX**, the local engine. Model files:
  `backend/voice/models/kokoro-v1.0.onnx` (310 MB, 325 MB installed) and
  `voices-v1.0.bin` (27 MB, 28 MB installed). 54 voices across 9 languages
  (`en`, `en-gb`, `es`, `fr`, `hi`, `it`, `ja`, `pt`, `zh`). **Measured working
  offline:** 2.67 s of audio in 3.4 s cold on this machine.
- **edge-tts**, the online engine. Every voice available, needs network.
- Routing: `pick_voice(lang)` prefers Kokoro where it has the language, else
  Edge. `engine_speaks(engine, lang)` is the guard that stops "Kokoro +
  Indonesian" from silently speaking the wrong language.

## Is it bundled on the install? Yes.

Both engines' model files ship. Verified in
`C:/Program Files/Addled/resources/backend/voice/models/`:

```
kokoro-v1.0.onnx  325,532,387 bytes
silero_vad.onnx     2,327,524 bytes
voices-v1.0.bin    28,214,398 bytes
```

This is deliberate, and guarded. The models are **gitignored**
(`.gitignore:40`) — they are not in the repository — and fetched once by
`scripts/fetch_voice_models.py`. `scripts/check_packaging.py` records
`backend/voice/models` in `BUNDLED_ON_PURPOSE` with the reason, and asserts
present-but-ignored paths stay consistent, because "an entry here that no
longer matches anything is how a real exclusion gets mistaken for it". So the
combination "gitignored and shipped on purpose" is checked rather than
assumed, which is the right shape — this is exactly where a silent packaging
regression would hide.

The **dependency** pin is not guarded the same way, and that is the bug below.

## Bugs found

### 1. Edge TTS is broken right now, and the default engine is Edge

Measured on this machine: **every** Edge voice fails.

```
en-US-JennyNeural: FAIL 403, message='Invalid response status'
en-GB-SoniaNeural: FAIL 403, message='Invalid response status'
```

Not clock skew — the system clock is 1 second off the network's. It is the
`Sec-MS-GEC` token the service now rejects.

The version matters. Tested in isolation:

| edge-tts | result |
|---|---|
| **7.2.8** (what ships, in the install and the repo bundle) | **403** |
| **7.3.1** (current) | **OK, 12240 bytes** |

So this is a **stale dependency**, not an upstream outage. And the pin is
`edge-tts>=6.1.0` (`requirements.txt:35`, `requirements-core.txt:42`) — an
open range that happily resolved to a version that no longer works. Nothing
holds it at a working version, and nothing tests that Edge can actually
synthesize.

**This is live.** The installed app has `voice.tts_engine = "edge"`, and the
user's log has 300 matching lines over 24 days.

### 2. The fallback is one-way, so a broken Edge means silence

`speak()` falls back **Kokoro → Edge** when Kokoro *raises*:

```python
if engine == "kokoro":
    try:
        return await _speak_kokoro(...)
    except Exception as e:
        log.warning("Kokoro TTS failed (%s) — falling back to edge-tts", e)
# ... falls through to _speak_edge
```

There is no Edge → Kokoro path. `_speak_edge` returns
`{"success": False, "error": ...}` rather than raising, and that return value
is passed straight back to the caller.

Proven with the real `speak()`:

| engine | result |
|---|---|
| `edge` (the default) | `success=False`, nothing spoken |
| `kokoro` | `success=True`, 1617 ms synth |
| unset (config default = edge) | `success=False`, nothing spoken |

A working offline model is installed, needs no network, and takes 1.6 s — and
it is never reached, because the fallback only points the other way. The
engine that is broken is the one the app defaults to on both sides (config
`"tts_engine": "edge"` and the dashboard's `v.tts_engine||'edge'`).

### 3. The failure is silent and the log actively claims success

`chat_send` fires the speech without waiting for the result
(`ws_server.py:~2951`):

```python
if reply and not reply.startswith(("[Not connected:", "[Provider")):
    _spawn(_speak_reply(reply))
    log.info("Auto-TTS: speaking chat reply (%d chars)", len(reply))
```

`_spawn` discards the `{"success": False}`. Nothing reports it to the user.
The log line is written *before* the attempt and says "speaking" regardless.

In the live log, a second apart:

```
21:24:41 INFO  addled.ws: Auto-TTS: speaking chat reply (58 chars)
21:24:42 ERROR addled.tts: TTS error: 403, Invalid response status
```

So the app tells the user Auto TTS is on and its log says it is speaking,
while the user hears nothing and has no way to find out why. A person
debugging this would conclude their speakers were wrong.

### 4. `voice.stt_model` has two different backend defaults

The same key is read with two defaults:

- `backend/voice/stt.py:88` (one-shot file transcription) — `default="tiny"`
- `backend/voice/stt.py:216` (`VoiceListener`, live mic) — `default="small"`

`DEFAULT_SETTINGS["voice"]["stt_model"]` is `"small"`. So with the key unset,
transcribing a file and transcribing live speech disagree about the model. A
bigger model is more accurate and slower; the two paths silently differ.

### 5. The dashboard's Language fallback disagrees with the backend

`dashboard/src/app/settings/page.tsx:1584`: `const language=v.language||'auto'`
— but `DEFAULT_SETTINGS["voice"]["language"]` is `"en"`. Every other fallback
in `VoiceSection` matches the backend exactly (engine, both voices, STT
engine, STT model, wake word, both toggles); Language is the one that does not.
It only shows when the key is absent, so it is a first-run/fresh-profile
disagreement rather than a constant one.

### 6. `en-gb` can be spoken by Kokoro but never selected

`kokoro_languages()` returns `en-gb` (8 voices, incl. `bm_george`, the default
for British English in `LANG_TO_KOKORO`), but the dashboard's `LANGUAGES`
list has no `en-gb` entry, so the Language dropdown cannot select it. The
warning logic compares against a list containing a value the user cannot pick.
`pick_voice("en-gb")` works if called — the gap is only in the picker.

## What is *not* wrong

Checked rather than assumed, because each looked suspicious:

- **Bundling.** The models ship and `check_packaging.py` guards them.
- **The Kokoro catalogue.** 54 voices, 9 languages, ids unique, letters
  consistent with `KOKORO_LANG_CODES`, gender normalised, Indonesian correctly
  absent. `check_voice.py` covers this well.
- **`tts_voice` / `kokoro_voice` are read.** The dashboard writes both and
  `tts.py` reads both (`:151`, `:161`). An earlier narrower grep suggested
  otherwise; it was the grep, not the code.
- **`auto_tts` / `vad_enabled` defaults.** The dashboard's `!==false` matches
  the backend's `True`.
- **Language routing.** `pick_voice` and `engine_speaks` behave correctly for
  every language tested, including the Indonesian case they were written for.

## Root cause of the live outage, in one line

The app ships and defaults to an online TTS engine whose pinned range
(`>=6.1.0`) now resolves to a version Microsoft's service rejects, and the
fallback only runs in the direction that cannot help — so a fully working
offline model sits unused while the user hears nothing, silently.

## Suggested fixes (research only — nothing changed)

1. **Pin and verify Edge.** Raise the floor to a version that works
   (`>=7.3.1`) and add a check that Edge can actually synthesize, so the next
   upstream break is caught rather than shipped. A range this open on a
   network service is a standing hazard.
2. **Make the fallback two-way.** Treat `{"success": False}` from Edge the
   same as a raise: try the other engine before giving up. This is the change
   that turns the live outage into a non-event, because Kokoro is installed
   and working.
3. **Stop claiming success.** Log "speaking" after the attempt, and let a
   failure reach the user — the bubble or a `voice.speak` error is enough.
   The current log line actively misleads.
4. **One default for `stt_model`**, and a dashboard fallback matching the
   backend. Small, but they are the kind of drift that gets believed.
5. **Add `en-gb` to the Language list** (or drop it from Kokoro's advertised
   set), so the picker and the voice pack agree.
