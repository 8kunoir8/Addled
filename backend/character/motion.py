"""The motion an emotion makes, on top of the pose it holds.

`emotions.py` gives each emotion a resting pose — how far the eyes squint, how
they tilt, how bright the character glows. That pose is held for as long as the
emotion lasts, which for a mood-driven character can be minutes.

A held pose alone is a photograph. This module adds the movement: the bounce in
joy, the tremble in anger, the slow droop in sadness. It is the part that makes
the character look alive rather than paused.

Why this is not a copy of the reference
---------------------------------------
CyberAgentAILab/Web-Eye-Animation defines each emotion as a GSAP timeline that
plays once and returns the eyes to rest — e.g. its joy is

    .to(eyes, {borderRadius:"0%", rotate:45, scaleY:0.1, duration:0.2})
    .to(eyes, {y:"-=10", duration:0.1, yoyo:true, repeat:3})
    .to(eyes, {borderRadius:"50%", rotate:0, scaleY:1, duration:0.2})

That is right for a demo page where an emotion is a button press: it fires, it
is seen, it is done. It is wrong here in two ways.

  * The emotion is HELD. Fire-and-forget means that a second and a half after
    the mood engine decides the character is grumpy, its face is neutral again.
    So the timeline has to be a loop, not a one-shot.

  * The values are not ours to write. Adding `y` to an element is additive in
    CSS; the equivalents here are fields the state animations also drive. So
    this layer produces OFFSETS, never absolute values, and `avatar.py` applies
    them on top of whatever the pose and the state decided.

What is kept from the reference is the character of each motion: joy bounces,
anger shakes, sadness sinks, surprise pops. The amplitudes are scaled to the
character's size and the periods are stretched well beyond the reference's, and
the reasons are recorded on each entry.

Bounded, so it stays alive instead of becoming a twitch
-------------------------------------------------------
The procedural-idle figures this is built to match are, for a hold this long,
roughly 1-3px of travel at a 3-4s period; sustained motion above about 2% of
the body's size reads as jitter rather than life. Every amplitude below is
therefore a small fraction of the character's size, and `rest` gives each
emotion quiet time between its beats so a loop does not shimmer continuously.

No motion ever starts from a non-zero offset: every curve is a full sine or a
full triangle that begins and ends at zero, so an emotion that arrives or
leaves mid-motion cannot make the character jump.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Motion:
    """One emotion's movement, as offsets added to its held pose.

    Every field is a small signed number, not an absolute position:
      bounce   vertical eye travel, as a fraction of the character's size.
               Negative is up, matching screen coordinates.
      shake    horizontal eye travel, same units. Symmetric, so it reads as a
               tremble rather than a lean.
      pulse    a scale multiplier deviation — 0.1 means the eyes grow 10%.
      squint   extra lid closure, 0..1, added to the pose. Blinks rapidly.
      period   seconds for one full cycle of the motion.
      rest     seconds of stillness between beats. 0 means continuous.
      beats    how many cycles to play before resting. 0 means loop forever.
    """

    bounce: float = 0.0
    shake: float = 0.0
    pulse: float = 0.0
    squint: float = 0.0
    period: float = 3.0
    rest: float = 0.0
    beats: float = 0.0


# ---------------------------------------------------------------------------
# The motions
# ---------------------------------------------------------------------------
#
# `period` and `rest` are the two knobs that decide whether a held emotion
# reads as alive or as irritating, and they are the numbers the reference does
# NOT give us — its timelines are all under two seconds because they never had
# to survive being held. Where a value is a judgement it is commented.

MOTIONS: dict[str, Motion] = {
    # A bounce. The reference does three quick 10px hops; here that becomes a
    # slow bob with a real pause, because a character that bounces forever is
    # the single most irritating thing this layer could do. Two beats, then
    # still for four seconds.
    "joy": Motion(bounce=-0.018, squint=0.18, period=0.6, rest=4.0, beats=2),

    # Sinking. The reference drops the eyes and stretches them; there is no
    # upward component at all, so this is a one-sided drift with a long period
    # and no sharp edges. Slowness IS the emotion here.
    "sadness": Motion(bounce=0.012, period=5.5, rest=0.0, beats=0.0),

    # Wide-eyed and still, with only a faint tremor. Surprise is arresting, so
    # the interest is in the held pose (the pose widens the eyes) and the
    # motion deliberately stays quiet — a big pop repeated is a jump scare.
    "surprise": Motion(pulse=0.05, period=1.4, rest=3.5, beats=1),

    # A hard, fast tremble. This is the one place a high frequency is right:
    # the reference shakes at roughly 10Hz, and a slow anger looks like
    # swaying. Kept tiny in amplitude so it reads as tension, not as a wobble.
    "anger": Motion(shake=0.006, squint=0.10, period=0.16, rest=1.2, beats=8),

    # Fast and small, the same shape as anger's tremble but quicker and
    # narrower — fear is a shiver rather than a shake.
    "fear": Motion(shake=0.004, pulse=0.04, period=0.12, rest=1.0, beats=10),

    # A slow recoil: a single pull away and a long pause. The pause is most of
    # it — disgust is a reaction that then wants nothing more to do with the
    # thing, so it should not keep moving afterwards.
    "disgust": Motion(bounce=0.010, squint=0.12, period=1.1, rest=5.0, beats=1),

    # The eyes disagree in the pose; the motion is a slow vertical bob, which
    # is what makes the disagreement look like puzzlement rather than a glitch.
    "confusion": Motion(bounce=-0.008, period=2.8, rest=0.0, beats=0.0),

    # A slow squash and release — the reference's "borderRadius 0 0 50% 50%",
    # which is the eyes going soft and half-lidded. Rendered here as a gentle
    # squint with a long period. Warm and unhurried.
    "love": Motion(squint=0.22, pulse=0.03, period=4.0, rest=0.0, beats=0.0),

    # Too tired to hold the eyes open: a long, heavy nod downward with a
    # second-long pause at the bottom. Periods this long are deliberate; the
    # reference's nod is half a second and would read as nodding off, not
    # sleepy.
    "sleepy": Motion(bounce=0.016, squint=0.30, period=6.0, rest=1.5, beats=1),

    # The liveliest of the set and the only one allowed a large amplitude. The
    # reference does five hops at 1.2x scale; kept at five beats but slowed and
    # then rested, so it stays a burst of energy rather than a seizure.
    "excitement": Motion(bounce=-0.028, pulse=0.08, period=0.45, rest=2.5,
                         beats=5),

    # --- Addled's own four -------------------------------------------------
    # Leaning in and looking about: a slow drift with a pause, so it reads as
    # attention rather than as idle restlessness.
    "curiosity": Motion(bounce=-0.010, period=2.2, rest=1.5, beats=1),
    # A slow, satisfied rise. Pride holds still: the pose says it.
    "pride": Motion(bounce=-0.014, pulse=0.05, period=3.2, rest=2.0, beats=1),
    # A small worried tremor, slower and softer than fear's shiver.
    "concern": Motion(shake=0.003, bounce=0.005, period=0.5, rest=3.0, beats=2),
    # A long, flat sigh of a motion. Nearly still, and that is the point.
    "boredom": Motion(bounce=0.008, period=7.0, rest=2.0, beats=1),

    # --- The mood engine's vocabulary --------------------------------------
    # These four are what the mood engine actually emits, so they are the ones
    # most likely to be held for a very long time. All are gentle.
    "happy": Motion(bounce=-0.014, squint=0.12, period=1.8, rest=3.0, beats=2),
    "content": Motion(bounce=-0.006, squint=0.10, period=5.0, rest=0.0, beats=0),
    "grumpy": Motion(shake=0.004, squint=0.14, period=0.30, rest=2.5, beats=4),
    "thoughtful": Motion(bounce=-0.007, period=3.6, rest=1.5, beats=1),
}

# Applied when an emotion has no entry above, and when the emotion is neutral.
STILL = Motion()

# How fast the motion fades in and out when the emotion changes, in seconds.
# An emotion that snaps on and off makes the character flinch on every turn.
FADE_IN = 0.45
FADE_OUT = 0.70


def motion_for(name: str) -> Motion:
    """The motion for an emotion, or STILL when it has none.

    Unknown names get STILL rather than raising, for the same reason
    `resolve_emotion` returns neutral: this is called from the 30fps paint path
    and a bad name must not be able to stop the character drawing.
    """
    if not name:
        return STILL
    return MOTIONS.get(str(name).strip().lower(), STILL)


def sample(motion: Motion, t: float) -> tuple[float, float, float, float]:
    """The offsets at `t` seconds into the motion.

    Returns ``(bounce, shake, pulse, squint)``, all zero on a still motion.

    Every curve starts and ends at zero, so the offsets can be faded in and out
    at any moment without the character jumping. `t` is expected to be a running
    clock rather than a per-frame delta: the caller advances it, so a stutter in
    the frame rate changes how fast time passes, never the shape of the motion.
    """
    if motion is STILL or motion.period <= 0:
        return 0.0, 0.0, 0.0, 0.0

    cycle = motion.period
    # A motion with `beats` plays that many cycles and then rests; one without
    # loops forever. The rest is the quiet half of the design — it is what
    # separates "alive" from "twitching".
    if motion.beats > 0 and motion.rest > 0:
        span = motion.beats * cycle + motion.rest
        local = t % span
        if local >= motion.beats * cycle:
            return 0.0, 0.0, 0.0, 0.0
    else:
        local = t

    phase = (local / cycle) % 1.0
    angle = phase * math.tau

    # A sine, so the movement is smooth and, importantly, still at its extremes
    # — an object that reverses instantly reads as mechanical.
    wave = math.sin(angle)

    return (
        motion.bounce * wave,
        motion.shake * wave,
        motion.pulse * wave,
        motion.squint * wave,
    )


def blend(amount: float, motion: Motion, t: float) -> tuple[float, float, float, float]:
    """`sample`, scaled by how present the emotion is (0..1).

    The caller fades `amount` in and out when the emotion changes, so a motion
    arrives and leaves smoothly instead of appearing at full amplitude.
    """
    if amount <= 0.0:
        return 0.0, 0.0, 0.0, 0.0
    b, s, p, q = sample(motion, t)
    return b * amount, s * amount, p * amount, q * amount
