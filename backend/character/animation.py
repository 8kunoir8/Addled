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
from backend.character.emotions import NEUTRAL, Emotion, resolve_emotion


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

        # Idle gaze drift. The real gaze comes from the cursor, but a character
        # that only looks where the mouse is stares motionless whenever the
        # mouse is still, which reads as broken rather than as calm. These add
        # a slow wandering saccade underneath, so the eyes are always alive.
        self._drift_phase: float = random.uniform(0.0, 6.28)
        self._drift_x: float = 0.0
        self._drift_y: float = 0.0
        self._drift_hold: float = 0.0
        self._drift_target_x: float = 0.0
        self._drift_target_y: float = 0.0
        # The drift's own contribution, kept apart from `look_x`/`look_y` so the
        # cursor gaze and the idle wander do not overwrite each other. The
        # renderer adds the two.
        self.gaze_drift_x: float = 0.0
        self.gaze_drift_y: float = 0.0

        # Mood tint (0.0 = normal, 1.0 = full effect)
        self.mood_warmth: float = 0.0
        self.mood_brightness: float = 0.0

        # Progress ring (for THINKING and WORKING states)
        self.progress_angle: float = 0.0
        self.progress_pct: float = 0.0  # 0..1 for determinate progress

        # Emotion layer — how the character FEELS, independent of what it is
        # doing. Its own continuously-blended values rather than a fold into
        # `eye_state`, because a state and an emotion must be able to hold at
        # once: THINKING while excited is two answers, not one. `set_emotion`
        # sets only the TARGET; `update` walks these toward it at the emotion's
        # own `attack` rate, which is what stops every emotion settling at the
        # same speed.
        self._emotion: Emotion = NEUTRAL
        self.emotion_eye_squint: float = 0.0
        self.emotion_eye_open: float = 0.0
        self.emotion_eye_tilt: float = 0.0
        self.emotion_eye_tilt_mirrored: bool = True
        self.emotion_eye_offset: float = 0.0
        self.emotion_pupil_scale: float = 1.0
        self.emotion_glow_delta: float = 0.0
        self.emotion_tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
        self.emotion_name: str = "neutral"

        # The emotion's glow contribution, kept apart from the state's own
        # `glow_intensity` so the two never feed back into each other. Combined
        # in `render_glow` at the point of use.
        self._emotion_glow_delta: float = 0.0

        # Emotion MOTION — the movement the emotion makes, on top of the pose
        # it holds. `emotion_eye_*` above are where the emotion sits; these are
        # how far it moves from there right now, and they are added to the pose
        # at the point of use rather than folded into it.
        #
        # Kept separate for the same reason the glow delta is: the pose is
        # blended toward the emotion's target by `_blend_emotion`, and writing
        # the motion into those same fields would make the blend chase a moving
        # target and never settle.
        #
        # Units are fractions of the character's size, so a motion looks the
        # same at every size the character is drawn. `motion_bounce` is negative
        # upward, matching screen coordinates.
        self.motion_bounce: float = 0.0
        self.motion_shake: float = 0.0
        self.motion_pulse: float = 0.0
        self.motion_squint: float = 0.0
        # The motion's own clock, advanced by `dt` and never reset. A clock that
        # restarted on every state change would make the character jump to the
        # start of its motion at unpredictable moments.
        self._motion_clock: float = 0.0
        # How present the current emotion's motion is, 0..1, faded in and out on
        # a change so an emotion arrives smoothly instead of snapping on.
        self._motion_amount: float = 0.0

    @property
    def frame(self) -> int:
        """Frames elapsed, for animations that move on a fixed cadence.

        Public because the renderer needs it: the hands sway on a sine of the
        frame count, and the count is the animator's. Exposed rather than
        duplicated so there is one clock and two readers, rather than two
        clocks that drift apart.
        """
        return self._frame

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

        # Idle gaze drift, added on top of whatever the state left in
        # `look_x`/`look_y`. Applied without overwriting them: the cursor gaze
        # is the primary signal and this is a small offset, so the character
        # still looks at the pointer and merely does not stare when it stops.
        self._update_gaze_drift(dt)

        # Emotion blend, LAST so it can adjust what the state just wrote.
        # Deliberately after the state's own animation: the state owns the
        # baseline and the emotion is a modifier on top of it, so blending
        # first would let the state overwrite the expression colouring it.
        #
        # The emotion works on eye geometry and on a separate glow delta; it
        # does not write the state's own parameters back, because those states
        # ease toward their targets by reading themselves.
        self._blend_emotion(dt)

        # Emotion motion, after the blend. It reads `emotion_name`, which the
        # blend has just settled, and writes only its own offset fields, so the
        # order matters in one direction only: the pose is decided first and
        # the movement is applied on top of it.
        self._update_motion(dt)

    def _blend_emotion(self, dt: float):
        """Walk the emotion values toward the current target.

        Each emotion carries its own `attack` rate, so surprise snaps in over a
        frame or two while sorrow seeps in over a second. Blending everything at
        one speed is most of what makes stock avatar animation look like stock
        avatar animation.

        Nothing here touches the state's own `glow_intensity`; the emotion's
        contribution is accumulated separately and combined at render time.
        """
        target = self._emotion
        rate = max(0.01, target.attack) * dt
        rate = min(1.0, rate)

        def approach(current: float, goal: float) -> float:
            return current + (goal - current) * rate

        self.emotion_eye_squint = approach(self.emotion_eye_squint,
                                           target.eye_squint)
        self.emotion_eye_open = approach(self.emotion_eye_open,
                                         target.eye_open)
        self.emotion_eye_tilt = approach(self.emotion_eye_tilt,
                                         target.eye_tilt)
        self.emotion_eye_offset = approach(self.emotion_eye_offset,
                                           target.eye_offset)
        self.emotion_pupil_scale = approach(self.emotion_pupil_scale,
                                            target.pupil_scale)
        self.emotion_glow_delta = approach(self.emotion_glow_delta,
                                           target.glow_delta)
        self.emotion_tint = tuple(
            approach(current, goal)
            for current, goal in zip(self.emotion_tint, target.tint)
        )
        self.emotion_eye_tilt_mirrored = target.eye_tilt_mirrored

        # The emotion does NOT write `glow_intensity`. That field belongs to the
        # state, and several of the state animations EASE toward their target by
        # reading their own current value (`_animate_idle` does exactly this).
        # Writing the emotion's result back into it would feed the emotion into
        # the state's easing, which ramps a +0.25 delta to full white and drags
        # a -0.20 delta to black over a few seconds.
        #
        # Instead the contribution is kept here and added at the point of use
        # (see `render_glow`), so the state's own number stays whatever the
        # state decided and the two are combined exactly once.
        self._emotion_glow_delta = self.emotion_glow_delta

    def _update_motion(self, dt: float):
        """Move the eyes the way the current emotion moves.

        Writes only the `motion_*` offsets. The held pose is `emotion_eye_*` and
        belongs to `_blend_emotion`; the renderer adds the two. Keeping them in
        different fields is what stops the blend from chasing a moving target.
        """
        from backend.character.motion import FADE_IN, FADE_OUT, blend, motion_for

        self._motion_clock += dt

        wanted = 1.0 if self.emotion_name != "neutral" else 0.0
        # Ease toward the target at a rate set by which way we are moving: an
        # emotion arrives a little faster than it leaves, so a change feels
        # deliberate rather than as though the face is lagging.
        step = dt / (FADE_IN if wanted > self._motion_amount else FADE_OUT)
        if abs(wanted - self._motion_amount) <= step:
            self._motion_amount = wanted
        else:
            self._motion_amount += step * (1.0 if wanted > self._motion_amount
                                           else -1.0)

        motion = motion_for(self.emotion_name)
        bounce, shake, pulse, squint = blend(self._motion_amount, motion,
                                             self._motion_clock)
        self.motion_bounce = bounce
        self.motion_shake = shake
        self.motion_pulse = pulse
        self.motion_squint = squint

    def _update_gaze_drift(self, dt: float):
        """Add a slow wandering offset to the eyes, so they are never still.

        Real eyes are never stationary: they fixate, then flick to a new point.
        A character whose gaze is driven only by the cursor freezes completely
        whenever the user stops moving the mouse, which reads as a frozen image
        rather than as calm. This reproduces the fixate-then-saccade pattern.

        Deliberately ADDED to `look_x`/`look_y` rather than replacing them, and
        stored in separate fields, because the caller sets the cursor gaze every
        frame. Writing into those fields directly would either be overwritten on
        the next tick or would fight the cursor.
        """
        # How long to hold the current fixation before flicking to the next.
        self._drift_hold -= dt
        if self._drift_hold <= 0.0:
            # A new fixation somewhere in the middle of the range. The extremes
            # are avoided: staring at the very edge of the eye looks like the
            # pupil has stuck, and it also collides with the lid during a squint.
            self._drift_target_x = random.uniform(-0.35, 0.35)
            self._drift_target_y = random.uniform(-0.18, 0.18)
            self._drift_hold = random.uniform(0.6, 2.4)

        # Move toward the target quickly (a saccade is fast), which is what
        # makes it read as a glance rather than as a slow slide.
        rate = min(1.0, 6.0 * dt)
        self._drift_x += (self._drift_target_x - self._drift_x) * rate
        self._drift_y += (self._drift_target_y - self._drift_y) * rate

        # One slow sinusoid on top of the saccades, so even during a long
        # fixation there is a faint drift rather than total stillness.
        self._drift_phase += dt * 0.7
        wobble_x = math.sin(self._drift_phase) * 0.04
        wobble_y = math.cos(self._drift_phase * 0.8) * 0.03

        # A sleeping or blocked character should NOT be glancing around — that
        # would contradict the state outright. The values are ZEROED rather than
        # merely skipped: returning early without clearing them left the last
        # awake value frozen on the renderer, so a character that fell asleep
        # kept staring off to wherever it last glanced.
        if self._state in (CharacterState.SLEEPING, CharacterState.DREAMING,
                           CharacterState.BLOCKED, CharacterState.ERROR):
            self.gaze_drift_x = 0.0
            self.gaze_drift_y = 0.0
            return

        self.gaze_drift_x = max(-1.0, min(1.0, self._drift_x + wobble_x))
        self.gaze_drift_y = max(-1.0, min(1.0, self._drift_y + wobble_y))

    @property
    def render_glow(self) -> float:
        """The glow to actually draw: the state's figure plus the emotion's.

        The renderer reads this instead of `glow_intensity`, so an emotion can
        lift or dim a state without the state losing track of its own baseline.

        A state that has deliberately dimmed itself to nothing stays dark.
        SLEEPING, BLOCKED and ERROR all set their glow to zero, and two of them
        also drop opacity — they are meant to recede, and an emotion lighting
        them back up would undo the state for as long as it was held. An emotion
        adds to a state that has a glow; it does not resurrect one that does not.
        """
        if self.glow_intensity <= 0.01:
            return 0.0
        return max(0.0, min(1.0, self.glow_intensity + self._emotion_glow_delta))

    # ---- emotion API ---------------------------------------------------------

    def set_emotion(self, name: str | None):
        """Set how the character feels. Unknown names fall back to neutral."""
        resolved = resolve_emotion(name)
        if resolved is self._emotion:
            return
        self._emotion = resolved
        self.emotion_name = resolved.name

    @property
    def emotion(self) -> Emotion:
        return self._emotion

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
