"""
TTS — text-to-speech using Edge TTS (free, high-quality neural voices).
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import os

log = logging.getLogger("addled.tts")


async def speak(text: str, voice: str = "en-US-JennyNeural", rate: str = "+0%") -> dict:
    """Speak text using Edge TTS. Returns {success, duration_ms}."""
    try:
        import edge_tts
        import time
        started = time.monotonic()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp_path = f.name

        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(tmp_path)

        # Play the audio
        _play_audio(tmp_path)

        # Clean up after a delay
        async def cleanup():
            await asyncio.sleep(5)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        asyncio.create_task(cleanup())

        elapsed = int((time.monotonic() - started) * 1000)
        return {"success": True, "duration_ms": elapsed}
    except ImportError:
        log.warning("edge-tts not installed. Install with: pip install edge-tts")
        return {"success": False, "error": "edge-tts not installed"}
    except Exception as e:
        log.error("TTS error: %s", e)
        return {"success": False, "error": str(e)}


def _play_audio(path: str):
    """Play audio file using platform-appropriate method."""
    import sys
    import subprocess

    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", path])
        else:
            subprocess.Popen(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path])
    except Exception as e:
        log.warning("Audio playback failed: %s", e)
