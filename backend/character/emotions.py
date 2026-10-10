"""Emotion layer — how the character FEELS, independent of what it is doing.

Separate from `CharacterState` on purpose. A state says what the app is doing
(thinking, speaking, working); an emotion says how the character feels about it.
They compose: the character can be THINKING while excited, or SPEAKING while
sad, and neither has to know about the other.

The vocabulary is borrowed from CyberAgentAILab/Web-Eye-Animation, which
expresses emotion purely through eye keyframes. Ten emotions are defined here,
matched to that library's list so the same names mean the same expressions.

What did NOT come across: that library drives its expressions with GSAP
timelines loaded from a CDN, and each one is a fire-and-forget animation that
plays once and returns to rest. Addled's character is a native PyQt6 QWidget
painted with QPainter in the backend process, so there is no DOM and no GSAP.
Emotions here are therefore CONTINUOUS PARAMETERS blended every frame, which
also means they hold their expression for as long as they are set — something
one-shot timelines cannot do, and the reason a state-driven avatar needs them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Emotion:
    """The visual signature of one emotion.

    Every field is a continuous value the renderer blends toward, never a
    discrete animation. That is what lets two emotions cross-fade, and what lets
    an emotion hold steady for minutes rather than snapping back to neutral.

    Ranges are all 0..1 except `eye_squint`, `eye_open`, `eye_tilt` and
    `brow_angle`, which are signed or scaled as noted.
    """

    name: str
    # How far the eyes narrow. 0 = fully open, 1 = shut.
    eye_squint: float = 0.0
    # Extra openness on top of the base. Negative narrows; 1 doubles. Distinct
    # from `eye_squint` because surprise and fear WIDEN the eye rather than
    # flattening it, and a single squint value cannot express both directions.
    eye_open: float = 0.0
    # Tilt of the eye shape in degrees. Mirror-applied so the two eyes can
    # converge (anger: inner corners down) or diverge (confusion: one up, one
    # down). Positive tilts the inner corner DOWN.
    eye_tilt: float = 0.0
    # Whether the tilt mirrors across the face. True makes a symmetric scowl or
    # a worried slant; False lets one eye tilt independently (confusion).
    eye_tilt_mirrored: bool = True
    # Vertical offset of the eye, as a fraction of eye radius. Negative raises
    # the eye (surprise), positive drops it (sadness).
    eye_offset: float = 0.0
    # Eye colour tint, as (r, g, b) multipliers. 1.0 means leave alone.
    # Deliberately gentle: a full colour swap reads as a different character,
    # not the same one feeling something.
    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    # Pupil dilation, as a multiplier on pupil size. Fear dilates, disgust
    # contracts.
    pupil_scale: float = 1.0
    # A glow added on top of the state's own, so joy lifts and sadness dims
    # without either fighting the state's baseline.
    glow_delta: float = 0.0
    # How fast the emotion settles in, in units per second. Sorrow is slow and
    # heavy; surprise is instant. Blending at one rate for all of them makes
    # every emotion feel the same, which is most of what makes stock animation
    # look like stock animation.
    attack: float = 6.0


# Neutral is the absence of an emotion and the state every blend starts from.
NEUTRAL = Emotion(name="neutral")

EMOTIONS: dict[str, Emotion] = {
    # --- the library's ten, in its own order -------------------------------
    "joy": Emotion(
        name="joy",
        # A smile lives in the lower lid, so joy squashes the eye from the
        # bottom while lifting the glow. Fully shut read as a blink, which is
        # why this is 0.45 rather than 0.9.
        eye_squint=0.45,
        eye_offset=-0.05,
        tint=(1.06, 1.04, 0.92),
        glow_delta=0.25,
        attack=8.0,
    ),
    "sadness": Emotion(
        name="sadness",
        # Outer corners DOWN, which is the mirror of anger's inner-corner drop.
        eye_tilt=-14.0,
        eye_squint=0.25,
        eye_offset=0.12,
        tint=(0.92, 0.96, 1.10),
        pupil_scale=1.08,
        glow_delta=-0.20,
        # Slow, because sorrow is not sudden. This is the clearest example of
        # why `attack` exists.
        attack=2.0,
    ),
    "surprise": Emotion(
        name="surprise",
        eye_open=0.75,
        eye_offset=-0.18,
        pupil_scale=1.25,
        tint=(1.05, 1.05, 1.05),
        glow_delta=0.15,
        attack=14.0,
    ),
    "anger": Emotion(
        name="anger",
        # Inner corners DOWN and hard. The squint is deliberately moderate:
        # at 0.5 the eye is only about two pixels tall at the default size, and
        # the lid then has no room to show its angle, so the scowl measured flat
        # in the right eye. The scowl is carried by the lid angle, not by
        # closing the eye, so the tilt gets the room and the squint stays light.
        eye_tilt=20.0,
        eye_squint=0.35,
        tint=(1.14, 0.86, 0.82),
        pupil_scale=0.85,
        glow_delta=0.10,
        attack=10.0,
    ),
    "fear": Emotion(
        name="fear",
        # Wide eyes with the pupil blown — the opposite trade to disgust.
        eye_open=0.95,
        eye_offset=-0.10,
        pupil_scale=1.5,
        tint=(0.95, 0.93, 1.08),
        glow_delta=-0.10,
        attack=12.0,
    ),
    "disgust": Emotion(
        name="disgust",
        # A wince: the two eyes lean OPPOSITE ways, so one screws up while the
        # other opens. The squint is kept light for the same reason as anger —
        # past about 0.4 the eye is too short for the lid angle to be visible at
        # the default size, and the wince renders as two flat slits.
        eye_squint=0.38,
        eye_tilt=-18.0,
        eye_tilt_mirrored=False,
        pupil_scale=0.8,
        tint=(0.94, 1.06, 0.90),
        glow_delta=-0.05,
        attack=9.0,
    ),
    "confusion": Emotion(
        name="confusion",
        # The one emotion that must NOT mirror: one eye tilts up while the
        # other tilts down, and a symmetric version of this looks like a bug in
        # the renderer rather than a puzzled character.
        eye_tilt=-16.0,
        eye_tilt_mirrored=False,
        eye_offset=0.04,
        pupil_scale=1.05,
        glow_delta=0.05,
        attack=5.0,
    ),
    "love": Emotion(
        name="love",
        # Soft, low-lidded, warm. The curve toward the viewer is carried by the
        # bright pink tint and the raised glow rather than by a shape change,
        # because a heart-shaped pupil at 64px reads as noise.
        eye_squint=0.4,
        eye_offset=-0.04,
        tint=(1.15, 0.88, 0.95),
        pupil_scale=1.15,
        glow_delta=0.3,
        attack=4.0,
    ),
    "sleepy": Emotion(
        name="sleepy",
        eye_squint=0.72,
        eye_offset=0.15,
        tint=(0.95, 0.95, 1.0),
        pupil_scale=1.1,
        glow_delta=-0.25,
        attack=1.6,
    ),
    "excitement": Emotion(
        name="excitement",
        eye_open=0.5,
        eye_offset=-0.08,
        pupil_scale=1.2,
        tint=(1.12, 1.02, 0.90),
        glow_delta=0.35,
        attack=12.0,
    ),
    # --- Addled's own, because the app has feelings the library does not ---
    # The app already distinguishes states the library has no word for. These
    # are the emotional counterparts, drawn in the same vocabulary.
    "curiosity": Emotion(
        name="curiosity",
        eye_open=0.3,
        eye_tilt=-6.0,
        pupil_scale=1.1,
        glow_delta=0.12,
        attack=6.0,
    ),
    "pride": Emotion(
        name="pride",
        eye_squint=0.3,
        eye_offset=-0.06,
        tint=(1.08, 1.04, 0.95),
        glow_delta=0.28,
        attack=5.0,
    ),
    "concern": Emotion(
        name="concern",
        eye_tilt=-10.0,
        eye_squint=0.2,
        eye_offset=0.08,
        tint=(0.96, 0.97, 1.04),
        glow_delta=-0.10,
        attack=3.5,
    ),
    "boredom": Emotion(
        name="boredom",
        eye_squint=0.55,
        eye_offset=0.10,
        pupil_scale=0.9,
        glow_delta=-0.30,
        # Very slow: boredom creeps rather than arrives.
        attack=1.2,
    ),

    # The MoodEngine's own vocabulary, as emotions. It already computes these
    # names from valence and energy, but only ever used them to choose a colour
    # tint. Giving each one a face is what lets the existing mood signal drive
    # the eyes without inventing a second source of truth.
    #
    # These are added rather than aliased onto the closest library emotion.
    # "happy" is not "joy" -- joy is a spike and happy is a plateau -- and a
    # caller asking for one should not be given the other.
    "happy": Emotion(
        name="happy",
        eye_squint=0.3,
        eye_offset=-0.04,
        tint=(1.05, 1.04, 0.94),
        glow_delta=0.15,
        attack=4.0,
    ),
    "content": Emotion(
        name="content",
        eye_squint=0.18,
        tint=(1.02, 1.02, 0.98),
        glow_delta=0.05,
        attack=2.5,
    ),
    "grumpy": Emotion(
        name="grumpy",
        # A low-energy scowl: the eyes narrow and tilt the same way anger's do,
        # but far more mildly and much more slowly. Grumpy is a mood that sits,
        # not a flash of temper.
        eye_tilt=10.0,
        eye_squint=0.35,
        tint=(1.06, 0.96, 0.94),
        pupil_scale=0.92,
        glow_delta=-0.12,
        attack=2.0,
    ),
    "thoughtful": Emotion(
        name="thoughtful",
        # Eyes slightly away and narrowed, with a mild inward tilt. The gaze
        # stays where the cursor tracking puts it; this only shapes the lids.
        eye_tilt=-6.0,
        eye_squint=0.22,
        eye_offset=-0.03,
        pupil_scale=0.95,
        glow_delta=0.02,
        attack=2.5,
    ),
}

# Spellings a caller might reasonably use, mapped onto the canonical names.
# Only unambiguous synonyms and spelling variants — `sad` for `sadness`,
# `excited` for `excitement`. Nothing semantic: mapping `happy` to `joy` would
# be inventing a meaning, and a caller asking for a feeling we do not have
# should be told so rather than given a different one.
EMOTION_ALIASES: dict[str, str] = {
    "sad": "sadness",
    "angry": "anger",
    "scared": "fear",
    "afraid": "fear",
    "disgusted": "disgust",
    "confused": "confusion",
    "loving": "love",
    "excited": "excitement",
    "sleepiness": "sleepy",
    "tired": "sleepy",
    "curious": "curiosity",
    "proud": "pride",
    "concerned": "concern",
    "bored": "boredom",
    "neutral": "neutral",
    "none": "neutral",
    "clear": "neutral",
}


def resolve_emotion(name: str | None) -> Emotion:
    """The emotion for `name`, or NEUTRAL when it is unknown.

    Unknown names return NEUTRAL rather than raising. An emotion arrives from a
    model, from settings, or from a websocket message, and none of those should
    be able to crash the paint loop — a bad string should mean "no expression",
    not a blank character.
    """
    if not name:
        return NEUTRAL
    key = str(name).strip().lower()
    key = EMOTION_ALIASES.get(key, key)
    return EMOTIONS.get(key, NEUTRAL)


def emotion_names() -> list[str]:
    """Every canonical emotion name, for the UI and for validation."""
    return sorted(EMOTIONS)
