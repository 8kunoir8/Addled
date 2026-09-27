"""
Animation engine — frame-based state renderer with transition blending.

12 states: idle float, thinking pulse, sleep zzz, suggestion glow, blocked dim,
acting fly, speaking float, listening glow, observing patrol, error shake,
working gear-spin, dreaming sparkle.
"""

from __future__ import annotations

import math
import random

from backend.character.states import CharacterState


class Animator:
    """Tracks animation frame and provides visual parameters for each state."""

    def __init__(self):
        self._state: CharacterState = CharacterState.IDLE
        self._frame: int = 0
        self._transition_progress: float = 1.0
        self._transition_from: CharacterState | None = None

        # Visual parameters driven by animation
        self.pulse_scale: float = 1.0
        self.glow_intensity: float = 0.6
        self._base_glow: float = 0.6
        self.opacity: float = 1.0
        self.eye_state: str = "open"
        self.shake_offset: float = 0.0
        self._blink_timer: float = 0.0

        # Idle life animations
        self._breathe_phase: float = 0.0
        self._stretch_timer: float = 0.0
        self._stretch_scale: float = 1.0
        self._stretching: bool = False

        # Gaze / cursor tracking
        self.look_x: float = 0.0  # -1..1, horizontal eye direction
        self.look_y: float = 0.0  # -1..1, vertical eye direction

        # Mood tint (0.0 = normal, 1.0 = full effect)
        self.mood_warmth: float = 0.0
        self.mood_brightness: float = 0.0

        # Progress ring (for THINKING and WORKING states)
        self.progress_angle: float = 0.0
        self.progress_pct: float = 0.0  # 0..1 for determinate progress

    def update(self, state: CharacterState, dt: float = 0.033):
        if state != self._state:
            self._transition_from = self._state
            self._transition_progress = 0.0
            self._state = state

        self._transition_progress = min(1.0, self._transition_progress + dt * 3.0)
        self._frame += 1

        # Blink logic
        self._blink_timer += dt
        if self._state not in (CharacterState.SLEEPING, CharacterState.DREAMING,
                                CharacterState.ERROR):
            if self._blink_timer > random.uniform(3.0, 6.0):
                self.eye_state = "closed"
                self._blink_timer = -0.15
            elif -0.15 < self._blink_timer < 0:
                self.eye_state = "closed"
            else:
                self.eye_state = "open"
        elif self._state == CharacterState.ERROR:
            self.eye_state = "x"  # X-shaped eyes
        elif self._state == CharacterState.DREAMING:
            self.eye_state = "rem"  # REM flicker
        else:
            self.eye_state = "closed"

        # State-specific animations
        match self._state:
            case CharacterState.IDLE:
                self._animate_idle(dt)
            case CharacterState.LISTENING:
                self._animate_listening(dt)
            case CharacterState.OBSERVING:
                self._animate_observing(dt)
            case CharacterState.THINKING:
                self._animate_thinking(dt)
            case CharacterState.HAS_SUGGESTION:
                self._animate_suggestion(dt)
            case CharacterState.ACTING:
                self._animate_acting(dt)
            case CharacterState.SPEAKING:
                self._animate_speaking(dt)
            case CharacterState.SLEEPING:
                self._animate_sleeping(dt)
            case CharacterState.BLOCKED:
                self._animate_blocked(dt)
            case CharacterState.ERROR:
                self._animate_error(dt)
            case CharacterState.WORKING:
                self._animate_working(dt)
            case CharacterState.DREAMING:
                self._animate_dreaming(dt)

    # ---- per-state animation functions ---------------------------------------

    def _animate_idle(self, dt: float):
        # Breathing: gentle scale oscillation
        self._breathe_phase += dt * 1.2
        breathe = math.sin(self._breathe_phase) * 0.015
        self.pulse_scale = 1.0 + breathe

        # Occasional stretch (every 30-60s)
        self._stretch_timer += dt
        if not self._stretching and self._stretch_timer > random.uniform(30, 60):
            self._stretching = True
            self._stretch_timer = 0.0
        if self._stretching:
            self._stretch_timer += dt
            t = self._stretch_timer / 0.6
            if t < 0.5:
                self._stretch_scale = 1.0 + t * 0.06
            elif t < 1.0:
                self._stretch_scale = 1.03 + (1.0 - t) * 0.06
            else:
                self._stretch_scale = 1.0
                self._stretching = False
        self.pulse_scale *= self._stretch_scale

        target = getattr(self, "_base_glow", 0.6)
        self.glow_intensity += (target - self.glow_intensity) * dt * 2.0
        self.opacity = 1.0

    def _animate_listening(self, dt: float):
        self.pulse_scale = 1.0 + math.sin(self._frame * 0.15) * 0.04
        self.glow_intensity = min(1.0, self.glow_intensity + dt * 3.0)
        self.opacity = 1.0
        self.eye_state = "wide"

    def _animate_observing(self, dt: float):
        self.pulse_scale = 1.0
        self.glow_intensity = 0.4
        self.opacity = 1.0

    def _animate_thinking(self, dt: float):
        self.pulse_scale = 1.0 + math.sin(self._frame * 0.12) * 0.05
        self.glow_intensity = 0.3
        self.opacity = 1.0
        # Indeterminate progress ring
        self.progress_angle = (self._frame * 3) % 360

    def _animate_suggestion(self, dt: float):
        self.pulse_scale = 1.0 + math.sin(self._frame * 0.15) * 0.03
        self.glow_intensity = min(1.0, self.glow_intensity + dt * 1.5)
        self.opacity = 1.0

    def _animate_acting(self, dt: float):
        self.pulse_scale = 1.0
        self.glow_intensity = 0.5
        self.opacity = 1.0

    def _animate_speaking(self, dt: float):
        self.pulse_scale = 1.0 + math.sin(self._frame * 0.18) * 0.04
        self.glow_intensity = 0.2
        self.opacity = 1.0

    def _animate_sleeping(self, dt: float):
        self.pulse_scale = 1.0
        self.glow_intensity = 0.0
        self.opacity = max(0.25, self.opacity - dt * 1.5)
        self.eye_state = "closed"

    def _animate_blocked(self, dt: float):
        self.pulse_scale = 1.0
        self.glow_intensity = 0.0
        self.opacity = max(0.3, self.opacity - dt * 2.0)

    def _animate_error(self, dt: float):
        self.shake_offset = math.sin(self._frame * 0.8) * 5
        self.pulse_scale = 1.0
        self.glow_intensity = 0.0
        self.opacity = 1.0

    def _animate_working(self, dt: float):
        self.pulse_scale = 1.0 + math.sin(self._frame * 0.1) * 0.03
        self.glow_intensity = 0.7
        self.opacity = 1.0
        # A determinate ring only when a caller has actually supplied progress.
        # `progress_pct` was never set by anything (`set_progress` had no
        # callers), so this line always wrote 0.0 and the WORKING ring sat
        # frozen at zero degrees — invisible, while THINKING's ring spun and
        # worked. An indeterminate working state now spins too, so the ring
        # moves whether or not anything reports progress.
        if self.progress_pct > 0.0:
            self.progress_angle = self.progress_pct * 360
        else:
            self.progress_angle = (self._frame * 3) % 360

    def _animate_dreaming(self, dt: float):
        self.pulse_scale = 1.0
        self.glow_intensity = 0.15
        self.opacity = 0.9
        self.eye_state = "rem"

    # ---- public API ----------------------------------------------------------

    @property
    def current_state(self) -> CharacterState:
        return self._state

    @property
    def is_transitioning(self) -> bool:
        return self._transition_progress < 1.0

    @property
    def transition_blend(self) -> float:
        return _ease_in_out(self._transition_progress)

    def look_at(self, x: float, y: float):
        """Set where the character's eyes should point (-1..1 range)."""
        self.look_x = max(-1.0, min(1.0, x))
        self.look_y = max(-1.0, min(1.0, y))

    def set_mood(self, warmth: float = 0.0, brightness: float = 0.0):
        """Set mood visual parameters (warmth=golden, brightness=energized)."""
        self.mood_warmth = max(0.0, min(1.0, warmth))
        self.mood_brightness = max(0.0, min(1.0, brightness))

    def set_progress(self, pct: float):
        """Set determinate progress for working state (0.0 to 1.0)."""
        self.progress_pct = max(0.0, min(1.0, pct))


def _ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)
