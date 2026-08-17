"""
Voice input (STT) — wake word + command listening.

Records via sounddevice (PortAudio wheel, no system deps) and transcribes
with faster-whisper (local, offline). The whisper model downloads on first
use (~75 MB for 'tiny').

Pipeline: wake word ("hey addled") → listen for a command → callback(text).
"""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger("addled.stt")

SAMPLE_RATE = 16000


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
            "voice", "stt_model", default="tiny")
        self._thread: threading.Thread | None = None
        self._running = False
        self._callbacks: list = []
        self._wake_callbacks: list = []
        self.enabled = False
        self.model_loaded = False

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
        log.info("Voice command: %s", text)
        for cb in list(self._callbacks):
            try:
                cb(text)
            except Exception as e:
                log.warning("Voice callback failed: %s", e)

    def _load_model(self):
        from faster_whisper import WhisperModel
        self._model = WhisperModel(self._model_size, device="cpu",
                                   compute_type="int8")
        self.model_loaded = True

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
        self._running = True
        self.enabled = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="voice-listener")
        self._thread.start()
        return True

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

        block = int(SAMPLE_RATE * 2.5)  # 2.5 s chunks
        silence_blocks = 0
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                dtype="float32", blocksize=block) as stream:
                log.info("Voice listener active (wake word: '%s')", self._wake_word)
                while self._running:
                    data, _ = stream.read(block)
                    rms = float(np.sqrt(np.mean(np.square(data))))
                    if rms < 0.01:  # silence — skip transcription to save CPU
                        silence_blocks += 1
                        if silence_blocks > 120:  # ~5 min of silence: micro-nap
                            time.sleep(0.5)
                        continue
                    silence_blocks = 0
                    text = self._transcribe(data)
                    if not text:
                        continue
                    if _contains_wake_word(text, self._wake_word):
                        log.info("Wake word heard")
                        self._trigger_wake()
                        command = self._listen_command(stream, block)
                        if command:
                            self._trigger_command(command)
        except Exception as e:
            log.warning("Voice listener stopped: %s", e)
        finally:
            self._running = False
            self.enabled = False

    def _transcribe(self, audio) -> str:
        try:
            segments, _info = self._model.transcribe(
                audio, language=None, beam_size=1, vad_filter=True)
            text = " ".join(s.text for s in segments).strip()
            return text
        except Exception:
            return ""

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
        text = self._transcribe(audio)
        # Strip the wake word if the recognizer repeated it
        if _contains_wake_word(text, self._wake_word):
            lowered = text.lower()
            idx = lowered.find(self._wake_word.lower())
            if idx >= 0:
                text = text[idx + len(self._wake_word):].strip()
        return text


voice_listener = VoiceListener()
