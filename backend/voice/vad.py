"""Silero VAD — voice activity detection for turn-taking.

Pure onnxruntime + numpy implementation of the Silero VAD v5 streaming
detector (no torch, no silero-vad package): feed 512-sample 16 kHz float32
frames, get {'start': sample} / {'end': sample} boundary events with the
same threshold/min-silence/speech-pad semantics as the reference
VADIterator.

A small ring-buffer `SegmentGrabber` reconstructs the audio span belonging
to a detected speech segment.

Model: backend/voice/models/silero_vad.onnx (fetched by
scripts/fetch_voice_models.py). If missing/unloadable, is_available() is
False and VoiceListener falls back to legacy RMS gating.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("addled.vad")

SAMPLE_RATE = 16000
FRAME = 512  # samples per VAD frame @16 kHz
CONTEXT = 64  # history samples prepended per reference OnnxWrapper


def _context_size(sample_rate: int = SAMPLE_RATE) -> int:
    return 64 if sample_rate == 16000 else 32


class SegmentGrabber:
    """Ring buffer that returns the audio span of a speech segment.

    VAD fires 'start' a bit late and 'end' slightly after speech stops;
    the ring keeps enough history to slice the real span.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, preroll_s: float = 1.0,
                 max_s: float = 30.0):
        self.sample_rate = sample_rate
        self.capacity = int(sample_rate * max_s)
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._pos = 0            # absolute sample position (start of buffer)
        self._written = 0        # total samples ever written
        self._seg_start: int | None = None

    def feed(self, chunk: np.ndarray) -> None:
        n = len(chunk)
        if n == 0:
            return
        if n > self.capacity:
            chunk = chunk[-self.capacity:]
        if self._written + n > self.capacity:
            drop = self._written + n - self.capacity
            self._buf[:-drop] = self._buf[drop:]
            self._pos += drop
            self._written -= drop
        self._buf[self._written:self._written + n] = chunk
        self._written += n

    def mark_start(self, start_sample: int) -> None:
        if self._seg_start is None:
            self._seg_start = max(self._pos, start_sample)

    def mark_end(self, end_sample: int) -> np.ndarray | None:
        if self._seg_start is None:
            return None
        start = min(self._seg_start, max(self._pos, end_sample))
        end = min(self._pos + self._written, end_sample)
        seg = None
        if end > start:
            s = max(0, start - self._pos)
            e = max(0, end - self._pos)
            if e > s:
                seg = self._buf[s:e].copy()
        self._seg_start = None
        return seg

    def reset(self) -> None:
        self._buf.fill(0.0)
        self._written = 0
        self._seg_start = None


class SileroVAD:
    """Streaming speech start/end detector on 512-sample float32 frames.

    Replicates the reference VADIterator state machine on top of the raw
    Silero ONNX model (input / state / sr → output / stateN).
    """

    def __init__(self, threshold: float = 0.5, min_silence_ms: int = 200,
                 speech_pad_ms: int = 60, model_path: str | None = None):
        self.threshold = threshold
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms
        self.min_silence_samples = int(SAMPLE_RATE * min_silence_ms / 1000)
        self.speech_pad_samples = int(SAMPLE_RATE * speech_pad_ms / 1000)
        self._model_path = model_path
        self._session = None
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        self._triggered = False
        self._temp_end = 0
        self._vad_count = 0
        self.grabber = SegmentGrabber(preroll_s=1.0)
        self.sample_pos = 0  # real samples fed (unpadded)
        self._lock = threading.Lock()
        self._load_error: str | None = None

    # ---- lifecycle -----------------------------------------------------------

    def load(self) -> bool:
        """Load the ONNX model (lazy, thread-safe). Returns success."""
        with self._lock:
            if self._session is not None:
                return True
            try:
                import onnxruntime as ort
                path = self._model_path or self._default_model_path()
                if not (path and Path(path).exists()):
                    raise FileNotFoundError(f"silero model missing: {path}")
                self._session = ort.InferenceSession(
                    str(path), providers=["CPUExecutionProvider"])
                self.reset()
                log.info("Silero VAD loaded (threshold=%.2f)", self.threshold)
                return True
            except Exception as e:
                self._load_error = str(e)
                log.warning("Silero VAD unavailable (%s) — RMS fallback", e)
                return False

    def is_available(self) -> bool:
        return self._session is not None

    def load_error(self) -> str | None:
        return self._load_error

    def reset(self) -> None:
        self.grabber.reset()
        self.sample_pos = 0
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT), dtype=np.float32)
        self._triggered = False
        self._temp_end = 0
        self._vad_count = 0

    # ---- detection -----------------------------------------------------------

    def feed_and_collect(self, chunk) -> tuple[dict | None, np.ndarray | None]:
        """Feed one frame; returns (event, segment_audio).

        segment_audio is the full speech span when an 'end' event fired
        (else None). The chunk's real samples are kept in the ring buffer.
        """
        if self._session is None:
            return None, None
        try:
            arr = np.asarray(chunk, dtype="float32")
            n = arr.shape[0]
            if n < FRAME:
                arr = np.pad(arr, (0, FRAME - n))
            elif n > FRAME:
                arr = arr[:FRAME]
            self.grabber.feed(arr[:n])  # keep only the real samples

            # The exported model consumes frame + 64-sample history context
            x = np.concatenate([self._context, arr.reshape(1, FRAME)], axis=1)
            prob, state = self._session.run(
                None,
                {
                    "input": x,
                    "state": self._state,
                    "sr": np.array(SAMPLE_RATE, dtype=np.int64),
                },
            )
            self._state = state
            self._context = x[:, -CONTEXT:]
            self._vad_count += n
            self.sample_pos += n
            p = float(np.asarray(prob).reshape(-1)[0])

            event = self._state_machine(p)
            segment = None
            if event and "start" in event:
                self.grabber.mark_start(int(event["start"]))
            elif event and "end" in event:
                segment = self.grabber.mark_end(int(event["end"]))
            return event, segment
        except Exception:
            return None, None

    def _state_machine(self, speech_prob: float) -> dict | None:
        """Mirror of silero-vad's VADIterator boundary logic."""
        if speech_prob >= self.threshold and self._temp_end:
            self._temp_end = 0
        if speech_prob >= self.threshold and not self._triggered:
            self._triggered = True
            start = max(0, self._vad_count - self.speech_pad_samples - FRAME)
            return {"start": start}
        if speech_prob < self.threshold - 0.15 and self._triggered:
            if not self._temp_end:
                self._temp_end = self._vad_count
            if self._vad_count - self._temp_end >= self.min_silence_samples:
                end = self._temp_end + self.speech_pad_samples - FRAME
                self._temp_end = 0
                self._triggered = False
                return {"end": end}
        return None

    # ---- helpers -------------------------------------------------------------

    @staticmethod
    def _default_model_path() -> str:
        root = Path(__file__).resolve().parent
        return str(root / "models" / "silero_vad.onnx")

    @staticmethod
    def rms(audio) -> float:
        """Root-mean-square of a float32 buffer (cheap pre-gate/micro-nap)."""
        arr = np.asarray(audio, dtype="float32")
        if arr.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(arr))))


# Module-level singleton used by VoiceListener.
_vad_instance: SileroVAD | None = None
_vad_lock = threading.Lock()


def get_vad(threshold: float = 0.5) -> SileroVAD:
    """Return the shared VAD instance (configured once)."""
    global _vad_instance
    with _vad_lock:
        if _vad_instance is None:
            _vad_instance = SileroVAD(threshold=threshold)
        return _vad_instance
