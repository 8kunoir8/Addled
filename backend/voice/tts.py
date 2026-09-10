"""TTS — Kokoro (local, offline) with edge-tts fallback.

Engine dispatch via config voice.tts_engine:
  - "kokoro": local Kokoro 82M ONNX (models/ fetched by
    scripts/fetch_voice_models.py). Offline, private, ~real-time on CPU.
  - "edge":   edge-tts neural voices (online, free). Used automatically as
    fallback when Kokoro is unavailable or fails.

speak() now blocks until playback FINISHES and returns the true audio
duration, so callers that `await speak()` (character SPEAKING animation in
main.py / ws_server.py) stay in sync with the actual audio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("addled.tts")

MODELS_DIR = Path(__file__).resolve().parent / "models"
KOKORO_MODEL = MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS_DIR / "voices-v1.0.bin"
DEFAULT_EDGE_VOICE = "en-US-JennyNeural"
DEFAULT_KOKORO_VOICE = "af_heart"

# Kokoro caps each synthesis at MAX_PHONEME_LENGTH (510); long chat replies
# must be split into sentence-sized pieces and concatenated.
_KOKORO_MAX_CHARS = 180

_kokoro = None
_kokoro_lock = threading.Lock()


# ---- engine selection --------------------------------------------------------

async def speak(text: str, voice: str | None = None,
                rate: str = "+0%", speed: float | None = None) -> dict:
    """Speak text. Returns {success, duration_ms, engine}.

    speed: TTS playback speed (Kokoro). None → mood-derived speed so the
    character's tone follows its emotional state.
    """
    from backend.config import config

    if not (text and text.strip()):
        return {"success": True, "duration_ms": 0, "engine": "none"}

    if speed is None:
        try:
            from backend.character.mood import mood_engine
            speed = mood_engine.speech_speed()
        except Exception:
            speed = 1.0

    engine = config.get("voice", "tts_engine", default="edge") or "edge"
    if engine == "kokoro":
        kokoro_voice = voice or config.get(
            "voice", "kokoro_voice", default=DEFAULT_KOKORO_VOICE)
        try:
            return await _speak_kokoro(text, kokoro_voice, speed)
        except Exception as e:
            log.warning("Kokoro TTS failed (%s) — falling back to edge-tts", e)
    edge_voice = voice or DEFAULT_EDGE_VOICE
    return await _speak_edge(text, edge_voice, rate)


def engine_available(engine: str) -> bool:
    """True if the named engine can run right now (imports + models)."""
    if engine == "kokoro":
        try:
            _get_kokoro()
            return True
        except Exception:
            return False
    if engine == "edge":
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:
            return False
    return False


# ---- Kokoro ------------------------------------------------------------------

def _get_kokoro():
    """Lazy singleton Kokoro session (thread-safe)."""
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            if not KOKORO_MODEL.exists():
                raise FileNotFoundError(
                    f"Kokoro model missing: {KOKORO_MODEL}")
            _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        return _kokoro


def _chunk_text(text: str, max_chars: int = _KOKORO_MAX_CHARS) -> list[str]:
    """Split long text on sentence boundaries into synth-able pieces."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text]
    parts = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for part in parts:
        if len(part) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(part), max_chars):
                chunks.append(part[i:i + max_chars].strip())
            continue
        if current and len(current) + 1 + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def _synth_kokoro(text: str, voice: str, speed: float = 1.0):
    """Synthesize (blocking, executor-thread) → float32 array + sample rate."""
    kokoro = _get_kokoro()
    pieces = _chunk_text(text)
    audio = None
    sr = 24000
    for piece in pieces:
        samples, sr = kokoro.create(piece, voice=voice, speed=speed)
        arr = np.asarray(samples, dtype=np.float32)
        audio = arr if audio is None else np.concatenate([audio, arr])
    return audio, sr


def _play_and_wait(path_or_audio, sample_rate: int | None = None) -> float:
    """Play audio, block until finished, return duration in seconds."""
    import sounddevice as sd
    if isinstance(path_or_audio, (str, os.PathLike)):
        with wave.open(str(path_or_audio), "rb") as wf:
            sr = wf.getframerate()
            pcm = np.frombuffer(wf.readframes(wf.getnframes()),
                                dtype=np.int16).astype(np.float32) / 32767.0
    else:
        pcm = np.asarray(path_or_audio, dtype=np.float32)
        sr = sample_rate or 24000
    if pcm.size == 0:
        return 0.0
    sd.play(pcm, sr)
    sd.wait()
    return pcm.size / sr


async def _speak_kokoro(text: str, voice: str,
                        speed: float | None = None) -> dict:
    loop = asyncio.get_running_loop()
    speed = speed or 1.0

    def _work() -> dict:
        started = time.monotonic()
        audio, sr = _synth_kokoro(text, voice, speed=speed)
        synth_s = time.monotonic() - started
        if audio is None or len(audio) == 0:
            raise RuntimeError("kokoro produced no audio")
        play_s = _play_and_wait(audio, sr)
        return {"success": True, "duration_ms": int(play_s * 1000),
                "engine": "kokoro", "synth_ms": int(synth_s * 1000),
                "speed": speed}

    return await loop.run_in_executor(None, _work)


# ---- Edge (online fallback) ---------------------------------------------------

def _play_mp3_sync(path: str) -> float:
    """Play an MP3 file, block until done, return duration in seconds."""
    import subprocess
    import sys
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_SYNC)
    elif sys.platform == "darwin":
        subprocess.run(["afplay", path], check=True)
    else:
        subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel",
                        "quiet", path], check=True)
    return 0.0


async def _speak_edge(text: str, voice: str, rate: str) -> dict:
    import edge_tts

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp_path = f.name
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(tmp_path)
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        await loop.run_in_executor(None, _play_mp3_sync, tmp_path)
        elapsed = int((time.monotonic() - started) * 1000)
        return {"success": True, "duration_ms": elapsed, "engine": "edge"}
    except ImportError:
        log.warning("edge-tts not installed. Install with: pip install edge-tts")
        return {"success": False, "error": "edge-tts not installed"}
    except Exception as e:
        log.error("TTS error: %s", e)
        return {"success": False, "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
