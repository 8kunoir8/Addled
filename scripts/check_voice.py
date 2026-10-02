"""Voice catalogue and voice-selection checks.

Three things worth guarding, none of which had a test before:

  * the Kokoro voice list is read without loading the 82 MB ONNX model;
  * the Edge list degrades to a usable fallback instead of an empty dropdown;
  * the configured voice actually reaches the engine — ``voice.tts_voice`` was
    written by Settings and read by nobody, so this half of the file fails on
    the pre-fix code.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_voice.py
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


# The rendered dropdown groups by these; a missing one means a language the
# picker cannot show even though the pack has voices for it.
EXPECTED_KOKORO_LANGUAGES = {"en", "en-gb", "es", "fr", "hi", "it", "ja", "pt",
                             "zh"}


def run_kokoro_tests():
    from backend.voice import voices
    from backend.voice.tts import KOKORO_VOICES

    check("the Kokoro voice pack is installed", KOKORO_VOICES.exists(),
          str(KOKORO_VOICES))
    listed = voices.kokoro_voices()
    check("voices were listed at all", len(listed) > 40, f"{len(listed)} found")
    print(f"   Kokoro voices found: {len(listed)}")

    ids = [v["id"] for v in listed]
    check("ids are unique", len(ids) == len(set(ids)),
          f"{len(ids)} ids, {len(set(ids))} unique")
    for name in ("af_heart", "am_adam", "bf_emma", "ff_siwis", "hf_alpha",
                 "if_sara", "jf_alpha", "pf_dora", "zf_xiaoxiao"):
        check(f"{name} is present", name in ids, "")
    check("no Indonesian voice exists (Kokoro v1.0 has none)",
          not any(v["language"] == "id" for v in listed),
          str([v["id"] for v in listed if v["language"] == "id"]))

    languages = {v["language"] for v in listed}
    check("every language is covered", languages == EXPECTED_KOKORO_LANGUAGES,
          f"missing {EXPECTED_KOKORO_LANGUAGES - languages}, "
          f"unexpected {languages - EXPECTED_KOKORO_LANGUAGES}")
    check("kokoro_languages() agrees",
          set(voices.kokoro_languages()) == EXPECTED_KOKORO_LANGUAGES,
          str(voices.kokoro_languages()))

    check("each voice carries what the picker needs",
          all(v.get("id") and v.get("language") and v.get("language_label")
              and v.get("label") for v in listed), "")
    check("gender is normalised",
          all(v["gender"] in ("female", "male") for v in listed),
          str({v["gender"] for v in listed}))

    # Reading the pack must not build the model.
    check("listing voices did not load the ONNX session",
          sys.modules.get("onnxruntime") is None,
          "onnxruntime was imported just to list voices")

    # af_heart is American English female; bm_george is British English male.
    by_id = {v["id"]: v for v in listed}
    check("af_heart is filed as en/female",
          by_id.get("af_heart", {}).get("language") == "en"
          and by_id.get("af_heart", {}).get("gender") == "female",
          str(by_id.get("af_heart")))
    check("bm_george is filed as en-gb/male",
          by_id.get("bm_george", {}).get("language") == "en-gb"
          and by_id.get("bm_george", {}).get("gender") == "male",
          str(by_id.get("bm_george")))


def run_edge_tests():
    from backend.voice import voices
    import edge_tts

    static = voices.static_edge_voices()
    check("the offline fallback has voices", len(static) >= 10, str(len(static)))
    check("the offline fallback includes Indonesian",
          any(v["id"] == "id-ID-GadisNeural" for v in static), str(static[:3]))
    check("fallback entries carry id/locale/language/label",
          all(v.get("id") and v.get("locale") and v.get("language")
              and v.get("label") for v in static), str(static[:2]))

    # --- offline: the service call fails --------------------------------
    original = edge_tts.list_voices

    async def boom(*a, **k):
        raise OSError("network is unreachable")

    edge_tts.list_voices = boom
    voices._EDGE_CACHE.update({"voices": [], "ts": 0.0, "error": None})
    try:
        result = asyncio.run(voices.edge_voices(force=True))
    finally:
        edge_tts.list_voices = original

    check("a failed fetch still yields a usable list",
          len(result["voices"]) >= 10, str(len(result["voices"])))
    check("a failed fetch is reported as the fallback",
          result["source"] == "fallback", str(result["source"]))
    check("the failure is reported, not swallowed",
          "network is unreachable" in (result["error"] or ""),
          str(result["error"]))

    # --- online: a live list is fetched, then served from cache ---------
    fake = [
        {"ShortName": "id-ID-GadisNeural", "Locale": "id-ID", "Gender": "Female"},
        {"ShortName": "en-US-JennyNeural", "Locale": "en-US", "Gender": "Female"},
        {"ShortName": "en-GB-RyanNeural", "Locale": "en-GB", "Gender": "Male"},
    ]

    async def fake_list(*a, **k):
        return fake

    edge_tts.list_voices = fake_list
    voices._EDGE_CACHE.update({"voices": [], "ts": 0.0, "error": None})
    try:
        live = asyncio.run(voices.edge_voices(force=True))
        cached = asyncio.run(voices.edge_voices())
    finally:
        edge_tts.list_voices = original
        voices._EDGE_CACHE.update({"voices": [], "ts": 0.0, "error": None})

    check("a live fetch is used", live["source"] == "live", str(live["source"]))
    check("the locale becomes a language code",
          {v["language"] for v in live["voices"]} == {"id", "en"},
          str(live["voices"]))
    check("the Neural suffix is stripped from the label",
          any(v["label"] == "Jenny" for v in live["voices"]),
          str([v["label"] for v in live["voices"]]))
    check("the second call is served from cache",
          cached["source"] == "cache", str(cached["source"]))


def run_selection_tests():
    """The configured voice must actually reach the engine."""
    from backend.config import config
    from backend.voice import tts

    config._ensure_loaded()
    original = dict(config._data.get("voice") or {})
    calls = []

    async def fake_edge(text, voice, rate="+0%"):
        calls.append(("edge", voice))
        return {"success": True, "duration_ms": 1, "engine": "edge"}

    async def fake_kokoro(text, voice, speed=1.0):
        calls.append(("kokoro", voice))
        return {"success": True, "duration_ms": 1, "engine": "kokoro"}

    real_edge, real_kokoro = tts._speak_edge, tts._speak_kokoro
    tts._speak_edge, tts._speak_kokoro = fake_edge, fake_kokoro
    try:
        # 1. The configured edge voice is used. On the pre-fix code this
        #    asserted DEFAULT_EDGE_VOICE instead.
        config._data.setdefault("voice", {}).update(
            {"tts_engine": "edge", "tts_voice": "en-GB-RyanNeural",
             "language": "en"})
        calls.clear()
        asyncio.run(tts.speak("hello", speed=1.0))
        check("the configured edge voice reaches the engine",
              calls == [("edge", "en-GB-RyanNeural")],
              f"got {calls} (the setting was previously ignored)")

        # 2. A caller-supplied voice still wins over the setting.
        calls.clear()
        asyncio.run(tts.speak("hello", voice="ja-JP-NanamiNeural", speed=1.0))
        check("an explicit voice overrides the setting",
              calls == [("edge", "ja-JP-NanamiNeural")], str(calls))

        # 3. The configured Kokoro voice is used.
        config._data["voice"].update({"tts_engine": "kokoro",
                                      "kokoro_voice": "bm_george"})
        calls.clear()
        asyncio.run(tts.speak("hello", speed=1.0))
        check("the configured kokoro voice reaches the engine",
              calls == [("kokoro", "bm_george")], str(calls))

        # 4. Kokoro cannot speak Indonesian, so the language must win over
        #    the engine setting rather than emit English-accented Indonesian.
        config._data["voice"].update({"language": "id", "tts_engine": "kokoro"})
        calls.clear()
        asyncio.run(tts.speak("halo", speed=1.0))
        check("an engine that cannot speak the language is not used",
              calls and calls[0][0] == "edge", str(calls))
        check("it falls back to the language's own voice",
              calls == [("edge", "id-ID-GadisNeural")], str(calls))

        # 5. An explicit voice is still respected even then.
        calls.clear()
        asyncio.run(tts.speak("halo", voice="af_heart", engine="kokoro",
                              speed=1.0))
        check("a pinned voice bypasses the language fallback",
              calls == [("kokoro", "af_heart")], str(calls))

        check("engine_speaks knows Kokoro lacks Indonesian",
              tts.engine_speaks("kokoro", "id") is False)
        check("engine_speaks knows Kokoro has Japanese",
              tts.engine_speaks("kokoro", "ja") is True)
        check("engine_speaks knows edge has Indonesian",
              tts.engine_speaks("edge", "id") is True)
    finally:
        tts._speak_edge, tts._speak_kokoro = real_edge, real_kokoro
        config._data["voice"] = original


def run_payload_test():
    from backend.voice import voices

    payload = asyncio.run(voices.describe())
    for key in ("kokoro", "edge", "kokoro_languages", "edge_source",
                "edge_error", "defaults"):
        check(f"the payload carries '{key}'", key in payload, str(list(payload)))
    check("the payload's kokoro list matches the reader",
          len(payload["kokoro"]) == len(voices.kokoro_voices()),
          str(len(payload["kokoro"])))


def run_empty_audio_tests():
    """A recording with nothing to read must say which kind of nothing.

    Live log evidence:
        file transcription failed for ...\\tmpbvs4z4gn.ogg:
        [Errno 541478725] End of file
    That message is misleading. ffmpeg returns the SAME errno for a 0-byte
    file and for bytes that are not a container at all (verified: 16 bytes of
    b"not audio at all" gives it too). Reported verbatim it reads as a decoder
    fault, so each case is named here instead.

    The second half matters just as much: the guard must not reject real
    audio. It checks container headers only, so every format the app can
    receive still reaches the model.
    """
    import tempfile
    from pathlib import Path
    from backend.voice.stt import _looks_like_media, transcribe_file

    # ---- real containers must pass the guard ----------------------------
    containers = {
        "Ogg/Opus (WhatsApp voice note)": b"OggS\x00\x02\x00\x00",
        "WebM/Matroska": b"\x1a\x45\xdf\xa3\x01\x00\x00\x00",
        "MP4/M4A": b"\x00\x00\x00\x20ftypM4A ",
        "WAV": b"RIFF\x24\x00\x00\x00WAVEfmt ",
        "MP3 (ID3)": b"ID3\x03\x00\x00\x00",
        "MP3 (bare frame)": b"\xff\xfb\x90\x00",
        "FLAC": b"fLaC\x00\x00\x00\x22",
        "AMR": b"#!AMR\x0a\x00",
    }
    for label, head in containers.items():
        fd, p = tempfile.mkstemp(suffix=".bin")
        os.write(fd, head)
        os.close(fd)
        try:
            check(f"the guard accepts {label}", _looks_like_media(p))
        finally:
            os.unlink(p)

    # ---- non-media must be rejected -------------------------------------
    for label, head in {"text named .ogg": b"not audio at all",
                        "JSON": b'{"a":1}',
                        "PNG": b"\x89PNG\r\n\x1a\n"}.items():
        fd, p = tempfile.mkstemp(suffix=".ogg")
        os.write(fd, head)
        os.close(fd)
        try:
            check(f"the guard rejects {label}", not _looks_like_media(p))
        finally:
            os.unlink(p)

    # ---- the two failures must be distinguishable -----------------------
    fd, empty = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        r_empty = transcribe_file(empty)
    finally:
        os.unlink(empty)
    empty_err = r_empty.get("error") or ""
    check("an empty recording fails", r_empty.get("success") is False)
    check("and says it is empty", "empty (0 bytes)" in empty_err, empty_err[:70])
    check("and does not leak an ffmpeg errno", "541478725" not in empty_err)

    fd, junk = tempfile.mkstemp(suffix=".ogg")
    os.write(fd, b"not audio at all")
    os.close(fd)
    try:
        r_junk = transcribe_file(junk)
    finally:
        os.unlink(junk)
    junk_err = r_junk.get("error") or ""
    check("undecodable bytes fail", r_junk.get("success") is False)
    check("and are named as undecodable, not as a decoder fault",
          "not audio or video" in junk_err, junk_err[:70])
    check("and report the byte count", "16 bytes" in junk_err, junk_err[:70])
    check("and do not leak an ffmpeg errno", "541478725" not in junk_err)
    check("the two causes are distinguishable", empty_err != junk_err)

    # ---- a missing file is a third, separate message --------------------
    missing = transcribe_file(
        str(Path(tempfile.gettempdir()) / "definitely_absent_clip.ogg"))
    check("a missing file says it is missing",
          "no such file" in (missing.get("error") or "").lower(),
          str(missing.get("error"))[:70])

def main():
    run_kokoro_tests()
    run_edge_tests()
    run_payload_test()
    run_selection_tests()
    run_empty_audio_tests()
    print()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
