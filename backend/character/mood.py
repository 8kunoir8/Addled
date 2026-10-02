"""Mood & emotion state — the character's internal feeling layer.

A small persistent state machine driven by events (outcomes, presence,
conversation). Maps to:
  - Animator.set_mood(warmth, brightness) — visual tint (avatar.py)
  - TTS speech speed (voice/tts.py)
  - WS broadcast `character.mood` for the dashboard

Axes: valence (-1..1, grumpy→happy) and energy (0..1, tired→charged).
Both decay toward neutral over time so moods pass naturally.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("addled.mood")

from backend import app_paths

MOOD_PATH = app_paths.MEMORY_DIR / "mood.json"


def _mood_name(valence: float, energy: float) -> str:
    if energy < 0.45:
        return "tired" if valence >= -0.35 else "grumpy"
    if valence >= 0.35:
        return "excited" if energy >= 0.65 else "happy"
    if valence >= 0.05:
        return "content"
    if valence >= -0.35:
        return "neutral"
    return "thoughtful"


class MoodEngine:
    """Singleton mood state with event feed and decay."""

    def __init__(self):
        self._lock = threading.Lock()
        self.valence = 0.0   # -1..1
        self.energy = 0.5    # 0..1
        self.updated = 0.0
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> None:
        try:
            if MOOD_PATH.exists():
                data = json.loads(MOOD_PATH.read_text(encoding="utf-8"))
                self.valence = max(-1.0, min(1.0, float(data.get("valence", 0))))
                self.energy = max(0.0, min(1.0, float(data.get("energy", 0.5))))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            MOOD_PATH.parent.mkdir(parents=True, exist_ok=True)
            MOOD_PATH.write_text(json.dumps({
                "valence": round(self.valence, 3),
                "energy": round(self.energy, 3),
                "updated": self.updated,
            }), encoding="utf-8")
        except OSError:
            pass

    # ---- events ---------------------------------------------------------------

    _EVENTS = {
        "task_success": (0.08, 0.03),
        "task_failure": (-0.10, -0.05),
        "chat_user": (0.05, 0.04),
        "chat_reply": (0.02, 0.01),
        "user_return": (0.10, 0.10),
        "user_away": (-0.05, -0.08),
        "goal_done": (0.15, 0.05),
        "goal_stuck": (-0.12, -0.06),
    }

    def event(self, name: str) -> None:
        """Apply a mood event. Unknown events are ignored."""
        dv, de = self._EVENTS.get(name, (0.0, 0.0))
        if dv == 0.0 and de == 0.0:
            return
        with self._lock:
            self._decay_locked()
            self.valence = max(-1.0, min(1.0, self.valence + dv))
            self.energy = max(0.0, min(1.0, self.energy + de))
            self.updated = time.time()
            self._save()

    # ---- decay -----------------------------------------------------------------

    def _decay_locked(self) -> None:
        from backend.config import config
        decay_h = float(config.get("mood", "decay_h", default=6.0) or 6.0)
        elapsed_h = (time.time() - self.updated) / 3600.0
        if elapsed_h <= 0 or decay_h <= 0:
            return
        factor = max(0.0, 1.0 - elapsed_h / decay_h)
        self.valence *= factor
        self.energy = 0.5 + (self.energy - 0.5) * factor
        self.updated = time.time()

    def tick(self) -> None:
        """Decay pass; safe to call from the engine tick."""
        with self._lock:
            self._decay_locked()
            self._save()

    # ---- outputs ----------------------------------------------------------------

    @property
    def name(self) -> str:
        with self._lock:
            self._decay_locked()
            return _mood_name(self.valence, self.energy)

    def speech_speed(self) -> float:
        """TTS speed 0.9–1.1 derived from mood."""
        with self._lock:
            self._decay_locked()
            speed = 1.0 + (self.energy - 0.5) * 0.25 + self.valence * 0.05
            return round(max(0.85, min(1.15, speed)), 3)

    def visuals(self) -> tuple[float, float]:
        """(warmth, brightness) for Animator.set_mood."""
        with self._lock:
            self._decay_locked()
            warmth = max(0.0, min(1.0, (self.valence + 1.0) / 2.0))
            brightness = max(0.0, min(1.0, self.energy))
            return round(warmth, 3), round(brightness, 3)

    def state(self) -> dict:
        return {
            "name": self.name,
            "valence": round(self.valence, 3),
            "energy": round(self.energy, 3),
            "warmth": self.visuals()[0],
            "brightness": self.visuals()[1],
            "speech_speed": self.speech_speed(),
        }


# Module-level singleton.
mood_engine = MoodEngine()
