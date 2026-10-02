"""
Voice input (STT) — wake word + command listening.

Records via sounddevice (PortAudio wheel, no system deps) and transcribes
with faster-whisper (local, offline). The whisper model downloads on first
use (~75 MB for 'tiny').

Turn detection: Silero VAD (backend/voice/vad.py) segments real speech so
transcription only runs on complete utterances — replacing the old RMS
energy gates. If the VAD model is missing, the listener falls back to the
legacy RMS-gated behavior automatically.

Pipeline: wake word ("hey addled") → listen for a command → callback(text).
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("addled.stt")

SAMPLE_RATE = 16000
FRAME = 512  # VAD frame size @16 kHz

# One model, shared by the listener and by file transcription. Loading a
# second copy would double the RAM for no benefit, and the model is large
# enough that doing so is worth avoiding.
_FILE_MODEL = None
_FILE_MODEL_LOCK = threading.Lock()

def transcribe_file(path: str) -> dict:
    """Transcribe an audio or video file with the local whisper model.

    Separate from the live listener: that one is built around a microphone
    stream and a wake word, and has no way to take a file off disk. This
    loads the same model once and reuses it.

    Returns ``{"success", "text", "language"}``, or an ``error`` explaining
    what to install when the model is missing — a stack trace here would just
    read as "transcription is broken".
    """
    global _FILE_MODEL
    from pathlib import Path

    target = Path(str(path or "")).expanduser()
    if not target.is_file():
        return {"success": False, "error": f"No such file: {target}"}

    # Two different problems reach ffmpeg as the SAME message — "[Errno
    # 541478725] End of file" — and neither is really "end of file":
    #
    #   * a 0-byte file (a recording that captured nothing), and
    #   * bytes that are not a media container at all (a corrupt download, a
    #     mislabelled attachment, a text file renamed .ogg).
    #
    # Verified: 16 bytes of b"not audio at all" and an empty file both produce
    # that identical errno. Reporting it verbatim sends you hunting a decoder
    # bug that does not exist, so both cases are named here instead. The size
    # check has to come first — there is nothing to sniff in 0 bytes.
    try:
        size = target.stat().st_size
    except OSError as e:  # noqa: BLE001
        return {"success": False, "error": f"could not read {target.name}: {e}"}
    if size == 0:
        return {"success": False,
                "error": (f"{target.name} is empty (0 bytes) — there is no "
                          "audio to transcribe. The recording captured "
                          "nothing.")}

    if not _looks_like_media(target):
        return {"success": False,
                "error": (f"{target.name} is not audio or video this can "
                          f"decode ({size} bytes, no recognisable media "
                          "header). The file may be corrupt, or named for "
                          "audio while holding something else.")}

    try:
        from backend.config import config
        size = config.get("voice", "stt_model", default="tiny") or "tiny"
    except Exception:  # noqa: BLE001
        size = "tiny"

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"success": False,
                "error": ("Local transcription needs faster-whisper. Install "
                          "it from Settings, then try again.")}

    try:
        with _FILE_MODEL_LOCK:
            if _FILE_MODEL is None:
                _FILE_MODEL = WhisperModel(size, device="cpu",
                                           compute_type="int8")
            model = _FILE_MODEL
        segments, info = model.transcribe(
            str(target), beam_size=1, vad_filter=True)
        text = " ".join(s.text for s in segments).strip()
        return {"success": True, "text": text,
                "language": getattr(info, "language", "") or ""}
    except Exception as e:  # noqa: BLE001
        log.warning("file transcription failed for %s: %s", target, e)
        return {"success": False, "error": f"could not transcribe: {e}"}


# Real media containers start with one of these. ffmpeg sniffs the same way;
# doing it here means a mislabelled file is reported as mislabelled instead of
# as a decoder error. Only headers are listed, so an unusual but valid file
# still passes through to ffmpeg, which remains the final authority.
_MEDIA_MAGIC = (
    b"OggS",           # Ogg / Opus / Vorbis  (WhatsApp and Telegram voice notes)
    b"\x1a\x45\xdf\xa3",  # Matroska / WebM     (Ogg-adjacent "video note" case)
    b"fLaC",           # FLAC
    b"RIFF",           # WAV / AVI
    b"ID3",            # MP3 with an ID3 tag
    b"\xff\xfb",       # MP3 frame sync (no tag)
    b"\xff\xf3",       # MP3 frame sync variant
    b"\xff\xf2",       # MP3 frame sync variant
    b"ADIF",           # AAC (ADIF)
    b"#!AMR",          # AMR narrowband
    b"#!AMR-WB",       # AMR wideband
    b"MThd",           # MIDI
)
# ISO base media (MP4 / M4A / MOV / 3GP): the size field, then 'ftyp'.
# Checked separately because 'ftyp' is not at offset 0.
_ISOBMFF_BRANDS = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide")

def _looks_like_media(path) -> bool:
    """Whether the first bytes look like a container ffmpeg can open.

    Deliberately permissive: a false positive costs nothing (ffmpeg still
    decides), while a false negative would reject audio we could have read.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return True  # unreadable here; let ffmpeg produce the real error
    if head.startswith(_MEDIA_MAGIC):
        return True
    if head[4:8] in _ISOBMFF_BRANDS:
        return True
    return False

def _contains_wake_word(text: str, wake_word: str) -> bool:
    words = {w.strip(",.!?") for w in (text or "").lower().split()}
    wake = wake_word.lower().strip()
    if " " in wake:
        return wake in (text or "").lower()
    return wake in words


class VoiceListener:
    """Background listener: energy gate → wake word → command."""

    def __init__(self, wake_word: str | None = None, model_size: str | None = None):
        from backend.config import config
        self._wake_word = wake_word or config.get(
            "voice", "wake_word", default="hey addled")
        self._model_size = model_size or config.get(
            "voice", "stt_model", default="small")
        self._engine = config.get("voice", "stt_engine", default="sensevoice")
        self._stt_kind = "whisper"  # set in _load_model
        self._thread: threading.Thread | None = None
        self._running = False
        self._callbacks: list = []
        self._wake_callbacks: list = []
        self.enabled = False
        self.model_loaded = False
        self._vad = None
        self._use_vad = False
        self._last_lang: str | None = None  # detected language of last command

    def on_command(self, callback) -> None:
        self._callbacks.append(callback)

    def on_wake(self, callback) -> None:
        self._wake_callbacks.append(callback)

    def _trigger_wake(self) -> None:
        for cb in list(self._wake_callbacks):
            try:
                cb()
            except Exception:
                pass

    def _trigger_command(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        log.info("Voice command: %s (lang=%s)", text, self._last_lang)
        for cb in list(self._callbacks):
            try:
                cb(text, self._last_lang)  # new-style: (text, lang)
            except TypeError:
                cb(text)  # legacy 1-arg callback
            except Exception as e:
                log.warning("Voice callback failed: %s", e)

    def _load_model(self):
        """Load the configured STT engine (sensevoice → funasr, else whisper).

        funasr is unavailable on Python 3.14 (kaldi-native-fbank has no
        wheel), so 'sensevoice' falls back to faster-whisper with a warning.
        """
        if self._engine == "sensevoice":
            try:
                from funasr import AutoModel
                self._model = AutoModel(
                    model="iic/SenseVoiceSmall",
                    trust_remote_code=True,
                    disable_update=True,
                    device="cpu",
                )
                self._stt_kind = "sensevoice"
                self.model_loaded = True
                log.info("STT engine: SenseVoice (funasr)")
                return
            except Exception as e:
                log.warning(
                    "SenseVoice unavailable (%s) — falling back to whisper", e)
        from faster_whisper import WhisperModel
        self._model = WhisperModel(self._model_size, device="cpu",
                                   compute_type="int8")
        self._stt_kind = "whisper"
        self.model_loaded = True
        log.info("STT engine: faster-whisper '%s'", self._model_size)

    def start(self) -> bool:
        """Start listening in a background thread. Returns True if enabled."""
        if self._running:
            return True
        try:
            import sounddevice as sd  # noqa: F401
            import faster_whisper  # noqa: F401
        except ImportError as e:
            log.warning("Voice input disabled: %s. "
                        "Run: pip install sounddevice faster-whisper", e)
            return False
        self._init_vad()
        self._running = True
        self.enabled = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="voice-listener")
        self._thread.start()
        return True

    def _init_vad(self) -> None:
        """Optional Silero VAD for turn detection (RMS fallback otherwise)."""
        try:
            from backend.config import config
            if not config.get("voice", "vad_enabled", default=True):
                return
            from backend.voice.vad import SileroVAD
            threshold = config.get("voice", "vad_threshold", default=0.5)
            self._vad = SileroVAD(threshold=threshold)
            self._use_vad = self._vad.load()
        except Exception as e:
            log.warning("VAD init failed (%s) — RMS fallback", e)

    def stop(self) -> None:
        self._running = False

    # ---- internals ------------------------------------------------------------

    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd
        try:
            self._load_model()
        except Exception as e:
            log.warning("Whisper model failed to load (%s) — voice input disabled", e)
            self.enabled = False
            self._running = False
            return

        block = int(SAMPLE_RATE * 0.5)  # 0.5 s blocks, framed to 512 for VAD
        silence_blocks = 0
        mode = "wake"              # "wake" | "command"
        command_start = 0.0
        use_vad = self._use_vad and self._vad is not None
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="float32", blocksize=block) as stream:
                log.info("Voice listener active (wake word: '%s', vad: %s)",
                         self._wake_word, "silero" if use_vad else "rms")
                while self._running:
                    data, _ = stream.read(block)
                    rms = float(np.sqrt(np.mean(np.square(data))))
                    if rms < 0.01:
                        silence_blocks += 1
                        if silence_blocks > 600:  # ~5 min of silence: micro-nap
                            time.sleep(0.5)
                    else:
                        silence_blocks = 0

                    if use_vad:
                        mode, command_start = self._process_vad(
                            data, mode, command_start)
                    else:
                        mode = self._process_rms(stream, data, block, mode)
        except Exception as e:
            log.warning("Voice listener stopped: %s", e)
        finally:
            self._running = False
            self.enabled = False

    # ---- VAD path --------------------------------------------------------------

    def _process_vad(self, data, mode: str, command_start: float):
        """Feed one block to the VAD; act on completed speech segments."""
        segment = None
        for off in range(0, len(data), FRAME):
            ev, seg = self._vad.feed_and_collect(data[off:off + FRAME])
            # Barge-in: if the user starts talking while Addled speaks,
            # stop the current TTS playback immediately.
            if ev and "start" in ev:
                try:
                    from backend.voice import tts
                    if tts.is_speaking():
                        tts.stop_playback()
                        log.info("Barge-in: user speech interrupted TTS")
                except Exception:
                    pass
            if seg is not None and len(seg) > int(SAMPLE_RATE * 0.25):
                segment = seg
        if segment is None:
            if mode == "command" and time.monotonic() - command_start > 8:
                return "wake", command_start  # timed out waiting for command
            return mode, command_start

        text, lang = self._transcribe(segment)
        if lang:
            self._last_lang = lang
        if not text:
            return mode, command_start
        if mode == "wake":
            if _contains_wake_word(text, self._wake_word):
                log.info("Wake word heard")
                self._trigger_wake()
                command = self._strip_wake_word(text)
                if command:
                    self._trigger_command(command)
                else:
                    mode = "command"
                    command_start = time.monotonic()
        else:  # mode == "command"
            self._trigger_command(self._strip_wake_word(text))
            mode = "wake"
        return mode, command_start

    # ---- legacy RMS path ---------------------------------------------------------

    def _process_rms(self, stream, data, block: int, mode: str) -> str:
        """Legacy behavior when VAD is unavailable (pre-1.0.13)."""
        import numpy as np
        rms = float(np.sqrt(np.mean(np.square(data))))
        if rms < 0.01:
            return mode
        text, lang = self._transcribe(data)
        if lang:
            self._last_lang = lang
        if not text:
            return mode
        if _contains_wake_word(text, self._wake_word):
            log.info("Wake word heard")
            self._trigger_wake()
            command = self._listen_command(stream, block)
            if command:
                self._trigger_command(command)
        return "wake"

    def _strip_wake_word(self, text: str) -> str:
        """Remove the wake word from a transcription if repeated in it."""
        text = (text or "").strip()
        if _contains_wake_word(text, self._wake_word):
            lowered = text.lower()
            idx = lowered.find(self._wake_word.lower())
            if idx >= 0:
                return text[idx + len(self._wake_word):].strip()
        return text

    def _transcribe(self, audio) -> tuple[str, str | None]:
        """Transcribe audio → (text, detected_language_code)."""
        try:
            if self._stt_kind == "sensevoice":
                result = self._model.generate(
                    input=audio, cache={}, language="auto",
                    use_itn=True, batch_size_s=60)
                return " ".join(
                    r.get("text", "") for r in result).strip(), None
            from backend.config import config
            lang_hint = config.get("voice", "language", default="auto")
            language = None if lang_hint in ("auto", "") else lang_hint
            segments, info = self._model.transcribe(
                audio, language=language, beam_size=1, vad_filter=True)
            text = " ".join(s.text for s in segments).strip()
            detected = getattr(info, "language", None) or None
            return text, detected
        except Exception:
            return "", None

    def _listen_command(self, stream, block: int) -> str:
        """After wake word, capture up to ~6 s of speech."""
        import numpy as np
        chunks = []
        silent = 0
        for _ in range(24):  # 24 * 0.25s? use block halves via stream.read(block//2)
            data, _ = stream.read(block // 2)
            rms = float(np.sqrt(np.mean(np.square(data))))
            if rms < 0.008:
                silent += 1
            else:
                silent = 0
                chunks.append(data)
            if silent > 6 and chunks:
                break
        if not chunks:
            return ""
        audio = np.concatenate(chunks)
        text, lang = self._transcribe(audio)
        if lang:
            self._last_lang = lang
        return self._strip_wake_word(text)


voice_listener = VoiceListener()
