"""Where the character's hands are, per state.

The character has no arms and no fingers. Its hands are two small rounded nubs
at the sides of the body — stubs, in the same spirit as the rest of the
silhouette, which is a blob with eyes and nothing else.

They earn their place because the state machine currently reads only from the
eyes. THINKING and LISTENING and IDLE differ in lid angle and glow, which is a
lot to ask of two small shapes; a hand at the chin or cupped at the ear says
the same thing a second time, in a channel the user is not already reading.

What this module is NOT
-----------------------
It is not a rig. There is no skeleton, no IK, no hand-to-target solve. A pose
is four numbers per hand, and a state picks a pose from a table. Anything more
would be a lot of machinery for two blobs, and this avatar is a shape with a
face rather than a character with a body.

Where the numbers come from
---------------------------
`height` and `reach` are fractions of the character's SIZE, so a pose looks the
same at 32px and at 256px. The body's silhouette is a blob of horizontal radius
0.44*size, so a hand at `reach` 0.44 sits exactly on the edge, and anything less
is tucked behind the body — which is what most resting poses want, because the
body is drawn ON TOP of the hands and hides the join.

The `behind` flag is the one structural idea here: hands are normally drawn
before the body so the body covers the shoulder joint, but a pose that crosses
the arms in front has to draw after, or the crossing arm disappears behind the
body. BLOCKED is the reason that flag exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from backend.character.states import CharacterState


@dataclass(frozen=True)
class Hands:
    """One pose, as the position of each hand.

    Each hand is `(angle, reach, height)`:
      angle   degrees, 0 pointing straight out sideways and 90 straight down.
              Positive swings the hand downward. The left and right hands are
              mirrored automatically, so a positive angle means the same visual
              thing on both sides.
      reach   horizontal distance from the centre, as a fraction of `size`.
              0.44 is exactly on the silhouette edge.
      height  vertical distance from the centre, as a fraction of `size`.
              Positive is downward.

    `wobble` is a per-frame movement in degrees, so a resting pose is not frozen
    solid. `behind` draws the hands before the body rather than after.
    """

    left: tuple[float, float, float]
    right: tuple[float, float, float]
    wobble: float = 0.0
    wobble_period: float = 0.0
    behind: bool = True
    # How much of the hand's width is hidden behind the body. Larger values tuck
    # the hand further in; the visible part is what reads as a hand.
    size: float = 0.135


# A rest pose used by every state that has not earned its own. Low and slightly
# out, sitting on the silhouette edge.
_REST = Hands(
    left=(26.0, 0.42, 0.20),
    right=(26.0, 0.42, 0.20),
    wobble=2.0,
    wobble_period=3.4,
)

# ---------------------------------------------------------------------------
# The pose table
# ---------------------------------------------------------------------------
#
# A note on `reach`, because it is the number that decides whether a pose is
# visible at all. The body is a blob whose half-width shrinks toward the top and
# bottom — at mid-height it is 0.44*size, but at head height (0.14 above centre)
# it is only 0.42, and near the base (0.34 below) just 0.27. A hand at a fixed
# 0.30 reach is therefore out in open space when it hangs low and buried behind
# the body when it is raised.
#
# Every reach below is set so the hand sits just past the silhouette at its own
# height. The values are not round numbers because the silhouette is not a
# circle; they were read off it.

HANDS: dict[CharacterState, Hands] = {
    # Standing still. The arms hang, and they sway slowly enough to be seen only
    # if you look for it, which is the point — idle should breathe, not wave.
    CharacterState.IDLE: _REST,

    # Cupped at the ear: both hands raised level with the head, out at the sides
    # where the ears would be. The reach is large because the silhouette is
    # still wide that high up, so a smaller value would tuck them out of sight.
    CharacterState.LISTENING: Hands(
        left=(14.0, 0.49, -0.12),
        right=(14.0, 0.49, -0.12),
        wobble=1.2,
        wobble_period=4.0,
    ),

    # Watching. Rest, but a little higher and a little further forward, so
    # OBSERVING differs from IDLE at a glance without becoming a gesture.
    CharacterState.OBSERVING: Hands(
        left=(20.0, 0.44, 0.12),
        right=(20.0, 0.44, 0.12),
        wobble=1.5,
        wobble_period=3.0,
    ),

    # One hand to the chin, the other relaxed. Asymmetric on purpose: a thinking
    # pose is a single gesture, and two hands at the chin reads as praying.
    #
    # The asymmetry has to show up in the RENDERED centroid, not just in the
    # table. An earlier version had the right hand out at reach 0.50 and the
    # left at 0.42, which sounds lopsided but renders within 4px of centre —
    # both hands end up roughly the same distance out, so the silhouette looks
    # balanced and the pose reads as neither hand doing anything. The right hand
    # now reaches well INWARD, past the body's edge toward the centre, which is
    # what a hand at the chin actually does; the left stays out and low.
    CharacterState.THINKING: Hands(
        left=(30.0, 0.42, 0.16),
        right=(-18.0, 0.16, -0.04),
        wobble=0.8,
        wobble_period=5.0,
    ),

    # Raised and offering. Both hands up and out, the most open pose in the
    # table — the character has something to give and is not hiding it.
    CharacterState.HAS_SUGGESTION: Hands(
        left=(-10.0, 0.50, -0.14),
        right=(-10.0, 0.50, -0.14),
        wobble=5.0,
        wobble_period=0.9,
    ),

    # At work. Both hands out and busy, with a fast small movement so they read
    # as occupied rather than as placed.
    CharacterState.ACTING: Hands(
        left=(30.0, 0.46, 0.18),
        right=(30.0, 0.46, 0.18),
        wobble=4.0,
        wobble_period=0.5,
    ),
    # WORKING is the same idea held longer, so the movement is slower and the
    # hands settle a little closer in.
    CharacterState.WORKING: Hands(
        left=(32.0, 0.44, 0.20),
        right=(32.0, 0.44, 0.20),
        wobble=3.0,
        wobble_period=0.8,
    ),

    # Talking with the hands, out of phase. The wobble is the widest in the
    # table and the two hands are deliberately against each other, so the
    # gesture reads as someone gesturing rather than as a twitch.
    CharacterState.SPEAKING: Hands(
        left=(16.0, 0.46, 0.04),
        right=(16.0, 0.46, 0.04),
        wobble=7.0,
        wobble_period=0.7,
    ),

    # Asleep: hands low and settled, just resting at the base. Not hidden — a
    # character whose hands have vanished reads as broken, not as asleep; low
    # and still is enough.
    CharacterState.SLEEPING: Hands(
        left=(48.0, 0.36, 0.34),
        right=(48.0, 0.36, 0.34),
        wobble=0.6,
        wobble_period=6.0,
    ),

    # Drifting. Loose, low, and slow, with a long period so the movement is
    # dreamlike rather than deliberate.
    CharacterState.DREAMING: Hands(
        left=(40.0, 0.42, 0.28),
        right=(40.0, 0.42, 0.28),
        wobble=2.5,
        wobble_period=7.0,
    ),

    # Crossed in front. The one pose drawn AFTER the body, because an arm across
    # the chest that is painted underneath the body is simply not there.
    #
    # The hands sit LOW — over the belly rather than the chest — and cross only
    # slightly. Two things drove that. Placed at chest height they land on the
    # eyes, the one place on this character that has to stay readable. And a
    # deep cross puts each hand on the far side of the body, which from outside
    # just looks like two hands near the middle: the crossing only reads if each
    # hand is still recognisably on its own side while reaching across the
    # centreline. Hence a small negative reach — the nubs are inside the
    # silhouette, so they need the rim to be seen at all — and a low height.
    CharacterState.BLOCKED: Hands(
        left=(80.0, -0.05, 0.40),
        right=(80.0, -0.05, 0.40),
        wobble=0.9,
        wobble_period=4.5,
        behind=False,
        size=0.15,
    ),

    # Palms out. High and wide, the opposite of BLOCKED: nothing is hidden and
    # nothing is being held. This is the widest reach in the table because the
    # pose only reads as "hands up" if the hands clear the silhouette clearly.
    CharacterState.ERROR: Hands(
        left=(-30.0, 0.52, -0.16),
        right=(-30.0, 0.52, -0.16),
        wobble=3.0,
        wobble_period=0.4,
    ),
}


def pose_for(state: CharacterState) -> Hands:
    """The hands for a state, falling back to the rest pose."""
    return HANDS.get(state, _REST)


def hand_position(hands: Hands, side: int, cx: float, cy: float, size: float,
                  frame: int) -> tuple[float, float, float]:
    """Where one hand is, in pixels, and how big to draw it.

    `side` is -1 for the left hand and +1 for the right. Returns
    ``(x, y, radius)``.

    The wobble is a sine of the frame counter rather than of wall-clock time, so
    it is identical for two runs of the same length and therefore testable. A
    pose whose wobble would be random could not be asserted on at all.
    """
    angle, reach, height = hands.left if side < 0 else hands.right

    # Mirror the horizontal reach for the two sides. The angle is NOT mirrored:
    # it was written to mean the same visual rotation on both hands, so a
    # positive angle swings both downward together, which is what a pose wants.
    x = cx + side * reach * size
    y = cy + height * size

    wobble = 0.0
    if hands.wobble and hands.wobble_period > 0:
        phase = (frame / 30.0) / hands.wobble_period
        wobble = hands.wobble * math.sin(phase * math.tau)
        # Opposite hands move against each other, which reads as a natural
        # sway. In phase they read as one rigid object rocking.
        wobble *= -1.0 if side > 0 else 1.0

    # The wobble has to move the hand in PIXELS, not just in angle. An earlier
    # version added it to the angle and then multiplied by fixed fractions of
    # the character's size, so a 7-degree wobble and a 1-degree wobble landed
    # within a tenth of a pixel of each other — the whole table of per-state
    # wobble values was decorative. The hand now swings along the arc by an
    # offset proportional to the wobble itself.
    if wobble:
        swing = math.radians(angle + wobble) - math.radians(angle)
        x += side * swing * size * 0.55
        y += abs(swing) * size * 0.30

    return x, y, hands.size * size


def draw_hands(painter, state: CharacterState, cx: float, cy: float,
               size: float, color, frame: int, opacity: float = 1.0,
               point_angle: float | None = None) -> None:
    """Draw both hands for a state.

    Deliberately takes raw Qt types rather than importing them: this keeps the
    geometry above testable without a widget, and the module importable without
    Qt, which matters because `states.py` is imported in places that have no
    display.

    `point_angle` overrides the pose for ACTING when the character is pointing
    at something, since a point is the one gesture whose direction is data
    rather than a constant.
    """
    from PyQt6.QtCore import QRectF, Qt
    from PyQt6.QtGui import QColor, QPainterPath, QPen

    hands = pose_for(state)

    if point_angle is not None and state == CharacterState.ACTING:
        # Both hands follow the point, so the gesture has a direction.
        hands = Hands(
            left=(point_angle, hands.left[1], hands.left[2]),
            right=(point_angle, hands.right[1], hands.right[2]),
            wobble=hands.wobble,
            wobble_period=hands.wobble_period,
            behind=hands.behind,
            size=hands.size,
        )

    painter.save()
    for side in (-1, 1):
        x, y, r = hand_position(hands, side, cx, cy, size, frame)
        fill = QColor(color)
        fill.setAlpha(int(255 * opacity))
        painter.setBrush(fill)

        # A rounded capsule, taller than wide. Not a circle: a circle reads as a
        # dot, and at this size a dot is indistinguishable from the particles
        # that already float around the character.
        w = r * 1.6
        h = r * 2.0
        path = QPainterPath()
        path.addRoundedRect(QRectF(x - w / 2, y - h / 2, w, h), w / 2, w / 2)

        # A faint darker rim. Without it the hands vanish whenever they are
        # drawn over the body, because they are the same colour as the body —
        # which is exactly the crossed-arms pose, the one pose that has to be
        # drawn on top. A rim one shade darker is enough to read as a separate
        # limb while staying the same material.
        rim = QColor(color)
        rim = rim.darker(135)
        rim.setAlpha(int(255 * opacity))
        pen = QPen(rim)
        pen.setWidthF(max(1.0, r * 0.16))
        painter.setPen(pen)
        painter.drawPath(path)
    painter.restore()
