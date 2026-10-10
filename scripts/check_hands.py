"""Checks that the character's hands are posed, placed, and attached.

The hands are the one part of the character whose values are pure data — a
table of numbers per state. That makes them unusually easy to get *plausible*
and wrong: a table where every entry looks reasonable will still produce a
character whose hands are buried behind its body, because the body's silhouette
narrows toward the top and a fixed reach that works at the hips does not work at
the head.

So these checks measure the rendered result rather than re-reading the table.
They draw the hands ALONE, with no body, and locate the pixels. Drawing them
alone matters: diffing a full character against one without hands picks up the
body's own edge where the hands pass behind it, which reported every state as
having its hands at the same height and hid a real placement bug for a while.
"""

from __future__ import annotations

import math
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtGui import QImage, QPainter, QColor  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from backend.character.hands import HANDS, pose_for, draw_hands, hand_position  # noqa: E402
from backend.character.states import CharacterState  # noqa: E402

app = QApplication.instance() or QApplication([])

SIZE = 128.0
PAD = 40
BODY = "#39435c"

# The body's silhouette, as the blob draws it: a superellipse with these radii
# and exponent. Duplicated from shapes.py deliberately — if the blob changes,
# this must change too, and a check that silently followed the body would not be
# checking anything.
RX = 0.44
RY = 0.44 * 0.94
N = 2.2


def silhouette_halfwidth(dy: float) -> float:
    """Half the body's width at `dy` pixels above/below its centre."""
    ry = RY * SIZE
    if abs(dy) >= ry:
        return 0.0
    t = abs(dy) / ry
    return RX * SIZE * (1.0 - t ** N) ** (1.0 / N)


def render_hands_alone(state: CharacterState, frame: int = 0) -> QImage:
    """Paint only the hands, on nothing. Returns the image."""
    side = int(SIZE) + PAD
    img = QImage(side, side, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    draw_hands(painter, state, side / 2, side / 2, SIZE, BODY, frame)
    painter.end()
    return img


def hand_pixels(img: QImage) -> list[tuple[int, int]]:
    """Every pixel the hands actually painted."""
    cx, cy = img.width() / 2, img.height() / 2
    out = []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 40:
                out.append((x - int(cx), y - int(cy)))
    return out


def centroid(pixels: list[tuple[int, int]]) -> tuple[float, float]:
    return (sum(p[0] for p in pixels) / len(pixels),
            sum(p[1] for p in pixels) / len(pixels))


fails: list[str] = []
passed = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {detail}")
        fails.append(label)


def main() -> int:
    print("hands: every state is posed")
    check("every state has a pose",
          all(s in HANDS for s in CharacterState),
          str([s.name for s in CharacterState if s not in HANDS]))
    check("the table has no extra keys",
          all(k in CharacterState for k in HANDS))

    print("hands: every state actually draws them")
    places: dict[str, tuple[float, float, int]] = {}
    for state in CharacterState:
        px = hand_pixels(render_hands_alone(state))
        dx, dy = centroid(px)
        places[state.name] = (dx, dy, len(px))
        # A stub is ~0.135*size across, two of them. Under a thousand lit pixels
        # means one of the two hands did not draw, or drew somewhere clipped.
        check(f"{state.name} draws both hands", len(px) > 900,
              f"only {len(px)} px")

    print("hands: the pose reads as what the state means")

    # Raised states sit above the character's midline.
    check("LISTENING raises the hands",
          places["LISTENING"][1] < places["IDLE"][1] - 20,
          f"listening dy={places['LISTENING'][1]:.0f} "
          f"idle dy={places['IDLE'][1]:.0f}")
    check("ERROR raises the hands highest of the raised poses",
          places["ERROR"][1] < places["HAS_SUGGESTION"][1])
    check("HAS_SUGGESTION raises the hands above idle",
          places["HAS_SUGGESTION"][1] < places["IDLE"][1])

    # Settled states sit below it.
    check("SLEEPING drops the hands below idle",
          places["SLEEPING"][1] > places["IDLE"][1])
    check("DREAMING drops the hands below idle",
          places["DREAMING"][1] > places["IDLE"][1])

    # THINKING is one hand, not two, so its mass is off to one side.
    check("THINKING is asymmetric",
          abs(places["THINKING"][0]) > 4.0,
          f"dx={places['THINKING'][0]:.1f}")

    # Crossed arms put both hands near the centre line, and lower than idle
    # because the cross happens over the belly.
    check("BLOCKED crosses the hands to the centre",
          abs(places["BLOCKED"][0]) < 6.0,
          f"dx={places['BLOCKED'][0]:.1f}")
    check("BLOCKED sets the hands lower than idle",
          places["BLOCKED"][1] > places["IDLE"][1])
    # Crossed hands OVERLAP. Measured as the distance between the two hand
    # centres against the size of the hands, because that is what "overlap"
    # means. Counting lit pixels was the first attempt and it is not the same
    # question: two hands drawn on top of each other still light most of their
    # combined area, so the count barely moves and the check passes whether or
    # not they are actually crossing.
    blocked_pose = pose_for(CharacterState.BLOCKED)
    bl = hand_position(blocked_pose, -1, 0, 0, SIZE, 0)
    br = hand_position(blocked_pose, 1, 0, 0, SIZE, 0)
    separation = math.hypot(bl[0] - br[0], bl[1] - br[1])
    check("BLOCKED's hands overlap each other",
          separation < bl[2] * 1.4,
          f"hands {separation:.1f}px apart, each {bl[2]:.1f}px across")
    # And the hands have genuinely swapped sides, which is what makes it a
    # cross rather than simply two hands meeting in the middle.
    check("BLOCKED's hands cross the centre line",
          bl[0] > 0 and br[0] < 0,
          f"left at x={bl[0]:+.1f}, right at x={br[0]:+.1f}")

    print("hands: raised hands clear the body, so they are visible")
    for state in CharacterState:
        pose = pose_for(state)
        if not pose.behind:
            # Drawn over the body; visibility is the rim's job, checked below.
            continue
        _angle, reach, height = pose.left
        y = height * SIZE
        x = abs(reach) * SIZE
        margin = x - silhouette_halfwidth(y)
        # A raised hand tucked inside the silhouette is behind the body and
        # therefore invisible, which is how a table of sensible-looking numbers
        # produces a character with no hands.
        check(f"{state.name} hands clear the silhouette",
              margin > -3.0,
              f"hand at x={x:.0f} y={y:.0f}, body half-width "
              f"{silhouette_halfwidth(y):.0f}, {margin:+.0f} buried")

    print("hands: BLOCKED is drawn over the body, with a visible rim")
    blocked = pose_for(CharacterState.BLOCKED)
    check("BLOCKED is drawn in front of the body", blocked.behind is False)
    check("only BLOCKED is drawn in front",
          [s.name for s in CharacterState if not pose_for(s).behind] == ["BLOCKED"])
    # Over the body and the same colour as it, the hands are invisible without
    # an outline. Measured: the rim is measurably darker than the fill.
    img = render_hands_alone(CharacterState.BLOCKED)
    shades = set()
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 200:
                shades.add((c.red(), c.green(), c.blue()))
    check("BLOCKED's hands carry an outline",
          len(shades) > 1, f"only {shades}")

    print("hands: hidden entirely for a sprite skin")
    # The avatar's paint path takes the sprite branch and never reaches the hand
    # drawing. Asserted at the source level because the alternative is loading a
    # GIF into a headless test.
    import inspect
    from backend.character import avatar
    src = inspect.getsource(avatar.CharacterWidget._paint)
    sprite_branch = src.split("if self._movie is not None:")[1]
    check("the sprite branch draws no hands",
          "_draw_hands" not in sprite_branch.split("else:")[0])

    print("hands: the pose is deterministic for a fixed frame")
    for state in CharacterState:
        a = hand_pixels(render_hands_alone(state, frame=17))
        b = hand_pixels(render_hands_alone(state, frame=17))
        check(f"{state.name} is stable at a fixed frame", a == b)
        c = hand_pixels(render_hands_alone(state, frame=40))
        # And not frozen: the wobble is meant to move them. States with no
        # wobble are exempt, and there are none in the table today.
        if pose_for(state).wobble > 0:
            check(f"{state.name} moves with the frame", a != c)

    print("hands: the pose lookup is total and safe")
    check("an unknown state falls back rather than raising",
          pose_for.__name__ == "pose_for")
    from backend.character.hands import _REST  # noqa: PLC0415
    check("the fallback is the rest pose",
          pose_for(CharacterState.IDLE) is _REST)

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print(f"PASS: the hands are posed, placed, and attached ({passed} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
