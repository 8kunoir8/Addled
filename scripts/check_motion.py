"""Verify the emotion motion: that it moves, in the right direction, and stops.

The pose layer is checked in `check_emotions.py` — where an emotion SITS. This
covers where it MOVES, which is a separate mechanism with separate failure
modes, and all three of these were live at some point while it was written:

  * The motion was computed correctly and never applied, so every emotion was
    still. Nothing in the pose checks could see that.

  * A motion could reverse its direction, which at these amplitudes turns a
    bounce into a jitter rather than an obviously wrong picture.

  * A held emotion moved continuously and never rested, which is the difference
    between a character that looks alive and one you want to switch off.

The last one is why the rest windows are asserted rather than assumed: the
amplitudes are small by design, so nothing here is caught by eye at a glance.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_motion.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character import motion as motion_module  # noqa: E402
from backend.character.animation import Animator  # noqa: E402
from backend.character.avatar import CharacterWidget  # noqa: E402
from backend.character.emotions import emotion_names  # noqa: E402
from backend.character.states import CharacterState  # noqa: E402

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        suffix: str = f"  <- {detail}" if detail else ""
        print(f"  FAIL {label}{suffix}")
        fails.append(label)


def eye_centre(emotion: str, frames: int, size: int = 128):
    """The centroid of the lit eye pixels after `frames` of animation.

    The gaze and the idle drift are pinned, so the only thing that can move the
    eyes is the motion layer itself. Without that the cursor gaze would swamp a
    two-pixel bounce and every emotion would look identical.
    """
    w = CharacterWidget({"shape": "blob", "color": "#39435c", "eyes": True,
                         "size": size})
    w.set_emotion(emotion)
    for _ in range(frames):
        w._animator.update(CharacterState.IDLE, 0.033)
    a = w._animator
    a.look_x = a.look_y = 0.0
    a.gaze_drift_x = a.gaze_drift_y = 0.0
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w._draw_eyes(p, size / 2, size / 2, a)
    p.end()
    pts = [(x, y) for y in range(size) for x in range(size)
           if img.pixelColor(x, y).lightness() > 150]
    if not pts:
        return None
    return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))


def travel(emotion: str):
    """(horizontal, vertical) travel in pixels over a long run."""
    xs, ys = [], []
    for frames in range(20, 420, 3):
        c = eye_centre(emotion, frames)
        if c:
            xs.append(c[0])
            ys.append(c[1])
    if len(xs) < 5:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)


def main() -> int:
    print("every emotion has a motion")

    for name in emotion_names():
        check(f"{name} has a motion", name in motion_module.MOTIONS, "-")

    # A motion with a zero period would divide by zero in `sample`, and this is
    # called every frame.
    bad = [n for n, m in motion_module.MOTIONS.items() if m.period <= 0]
    check("no motion has a zero period", not bad, str(bad))

    # A motion that never rests and never varies is not a motion.
    dead = [n for n, m in motion_module.MOTIONS.items()
            if m.bounce == 0 and m.shake == 0 and m.pulse == 0 and m.squint == 0]
    check("no motion is entirely empty", not dead, str(dead))

    # The amplitude budget. Above roughly 2% of the body's size, sustained
    # movement stops reading as life and starts reading as jitter.
    loud = {n: (m.bounce, m.shake) for n, m in motion_module.MOTIONS.items()
            if abs(m.bounce) > 0.035 or abs(m.shake) > 0.02}
    check("no motion exceeds the amplitude budget for a long hold",
          not loud, str(loud))

    print("the motion is bounded and settles")

    # Every curve must start at zero, whatever the emotion. Otherwise an emotion
    # that arrives mid-motion makes the character jump, which is worse than no
    # motion at all.
    for name, m in motion_module.MOTIONS.items():
        b, s, p, q = motion_module.sample(m, 0.0)
        if any(abs(v) > 1e-9 for v in (b, s, p, q)):
            check(f"{name} starts from rest", False, f"{(b, s, p, q)}")
    check("every motion starts from rest", True)

    # And every motion with a rest must actually reach zero during it, which is
    # the property that makes a held emotion calm rather than continuous.
    resting = {n: m for n, m in motion_module.MOTIONS.items() if m.rest > 0}
    check("some motions have a rest window", bool(resting), str(len(resting)))
    for name, m in resting.items():
        # Sample the middle of the rest, where there is least doubt.
        at = m.beats * m.period + m.rest / 2.0
        b, s, p, q = motion_module.sample(m, at)
        check(f"{name} is still during its rest",
              all(abs(v) < 1e-9 for v in (b, s, p, q)), f"{(b, s, p, q)}")

    # A motion must not be constant: if every sample is identical there is no
    # motion, only an offset, and the offset belongs in the pose.
    #
    # Measured across ALL four axes, not just the bounce. Anger, fear and
    # surprise move by shaking or pulsing while their bounce stays at zero, so
    # sampling the bounce alone reported them as motionless — the check was
    # wrong, not the motions.
    for name, m in motion_module.MOTIONS.items():
        vals = {tuple(round(v, 9) for v in motion_module.sample(m, t))
                for t in [i * m.period / 40.0 for i in range(40)]}
        if len(vals) < 3:
            check(f"{name} actually varies over a cycle", False,
                  f"{len(vals)} distinct value(s)")
    check("every motion varies over its cycle", True)

    print("the offsets reach the renderer")

    # The failures this catches are the ones a pose check cannot: the values can
    # be right in the animator and still never reach the eyes.
    a = Animator()
    a.set_emotion("joy")
    seen = set()
    for _ in range(150):
        a.update(CharacterState.IDLE, 0.033)
        seen.add(round(a.motion_bounce, 6))
    check("the animator produces motion offsets", len(seen) > 5,
          f"{len(seen)} distinct values")

    a = Animator()
    a.set_emotion("neutral")
    for _ in range(120):
        a.update(CharacterState.IDLE, 0.033)
    check("a neutral emotion produces no motion",
          a.motion_bounce == 0 and a.motion_shake == 0,
          f"{a.motion_bounce} {a.motion_shake}")

    print("the motion moves the drawn eyes")

    # THE load-bearing check. Everything above can be right while `_draw_eyes`
    # ignores the offsets and the character sits perfectly still — which is
    # exactly what shipped first.
    still = travel("neutral")
    check("a neutral character's eyes do not wander",
          still is not None and still[1] < 0.5,
          f"vertical travel {still[1] if still else None:.2f}px")

    joy = travel("joy")
    check("joy's eyes actually move",
          joy is not None and joy[1] > 1.5,
          f"vertical travel {joy[1] if joy else None}")

    # Direction matters. A bounce that moves sideways is a different emotion,
    # and a magnitude-only check cannot tell them apart.
    check("joy moves vertically, not sideways",
          joy is not None and joy[1] > joy[0],
          f"h={joy[0]:.2f} v={joy[1]:.2f}" if joy else "none")

    anger = travel("anger")
    check("anger's eyes actually move",
          anger is not None and anger[0] > 1.0,
          f"horizontal travel {anger[0] if anger else None}")

    check("anger shakes horizontally, as a tremble",
          anger is not None and anger[0] > anger[1],
          f"h={anger[0]:.2f} v={anger[1]:.2f}" if anger else "none")

    # Joy and anger must not look the same: if both moved the same way, the
    # motion would be a single generic wobble with per-emotion amplitudes.
    check("joy and anger move differently",
          joy is not None and anger is not None
          and (joy[0] / max(joy[1], 0.01)) < (anger[0] / max(anger[1], 0.01)),
          f"joy={joy} anger={anger}")

    print("the motion survives the other axes")

    # The state machine and the emotion are independent axes, and the motion
    # must not throw or vanish under any combination of them.
    crashed = []
    for state in CharacterState:
        for emo in ("neutral", "joy", "anger", "sleepy", "excitement"):
            try:
                w = CharacterWidget({"size": 64, "eyes": True})
                w.set_emotion(emo)
                for _ in range(30):
                    w._animator.update(state, 0.033)
                img = QImage(80, 80, QImage.Format.Format_ARGB32)
                img.fill(QColor(0, 0, 0, 0))
                p = QPainter(img)
                w._draw_eyes(p, 40, 40, w._animator)
                p.end()
            except Exception as e:  # noqa: BLE001
                crashed.append(f"{state.name}/{emo}: {e}")
    check("every state and emotion combination draws with a motion",
          not crashed, str(crashed[:3]))

    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the emotion motion moves, in the right direction, and settles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
