"""Verify the emotion layer: vocabulary, blending, and what the eyes actually draw.

The layer has two halves that fail differently. The DATA half (resolution and
aliases) is pure and easy to get right. The RENDER half is where the real bugs
were, and they were the kind that pass a naive test:

  * The glow blend first seeded from `_base_glow`, which is the USER'S SETTINGS
    value and not the state's own figure — so a joyful emotion lit up BLOCKED and
    SLEEPING, states an author had deliberately darkened.

  * Rewriting `glow_intensity` then fed the emotion's output back in as the
    state's input, because several state animations ease by reading their own
    current value. A +0.25 delta ramped to the clamp and a -0.20 delta to black.
    Measured.

  * Tilt was drawn by ROTATING the eye. A 10x5 pixel ellipse rotated 20 degrees
    moves its corners by about one pixel, so every emotion measured as flat.
    The tilt had to be redrawn as a lid before it existed on screen at all.

Every case below is one of those, checked against real rendered pixels rather
than against the parameters that feed them. Checking the parameters would have
passed while the screen showed nothing.

Qt needs a widget, so this runs offscreen.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_emotions.py
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

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QImage, QPainter, QColor  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.animation import Animator  # noqa: E402
from backend.character.avatar import CharacterWidget  # noqa: E402
from backend.character.emotions import (  # noqa: E402
    EMOTION_ALIASES,
    EMOTIONS,
    NEUTRAL,
    emotion_names,
    resolve_emotion,
)
from backend.character.states import CharacterState  # noqa: E402

# The ten emotions the reference library defines. Spelled out rather than
# imported, because this list IS the requirement — deriving it from EMOTIONS
# would let a typo'd or deleted emotion silently redefine the goal.
LIBRARY_EMOTIONS = [
    "joy", "sadness", "surprise", "anger", "fear",
    "disgust", "confusion", "love", "sleepy", "excitement",
]

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def settle(emotion, state=CharacterState.IDLE, frames=400, size=64):
    """A widget and animator with `emotion` fully blended in.

    The idle gaze drift is PINNED to zero. The drift is genuinely random by
    design — that is the point of it — but it moves the pupil between runs, and
    a pupil sitting one pixel nearer an edge changes a measured eye box or a
    lid edge enough to flip an assertion. That is not a rendering fault, so it
    must not be allowed to look like one. Checks that want to observe the drift
    drive it directly instead.
    """
    w = CharacterWidget({"size": size, "eyes": True})
    w._animator.set_emotion(emotion)
    for _ in range(frames):
        w._animator.update(state, 0.033)
    w._animator.look_x = 0.0
    w._animator.look_y = 0.0
    w._animator.gaze_drift_x = 0.0
    w._animator.gaze_drift_y = 0.0
    return w


def _kind_map(img, size):
    """Classify every pixel of an eye render once, with pupil rims cleaned up.

    The pupil's antialiased RIM is light-ish and would otherwise be counted as
    lid. That is not hypothetical: it made a lidless neutral eye report a
    confident "HIGHER" slope. A rim pixel always has a genuinely dark pixel
    within one pixel of it; a real lid pixel at the top of an eye never does.
    Removing those is what lets the lid edge be read at all.
    """
    raw = [[None] * size for _ in range(size)]
    for x in range(size):
        for y in range(size):
            px = img.pixel(x, y)
            if ((px >> 24) & 0xFF) <= 80:
                continue
            r, g, b = (px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF
            light = (r + g + b) / 3.0
            raw[x][y] = ("pupil" if light < 70
                         else "white" if light > 168 else "lid")
    # Drop "lid" pixels that are really the pupil's rim.
    clean = [[raw[x][y] for y in range(size)] for x in range(size)]
    for x in range(size):
        for y in range(size):
            if clean[x][y] != "lid":
                continue
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < size and 0 <= ny < size and raw[nx][ny] == "pupil":
                        clean[x][y] = "white"
                        break
                else:
                    continue
                break
    return clean


def lid_slopes(w):
    """For each eye, whether the lid's inner corner sits HIGHER or LOWER.

    Returns a two-element list ("LOWER" / "HIGHER" / "flat") for the left and
    right eye, or None when no lid was drawn.

    Reads REAL PIXELS. That matters: the first version drew the tilt by ROTATING
    the eye, which is mathematically correct and visually invisible at these
    sizes, so checking the parameter instead of the pixels would have declared
    it working. Where an eye is only a few pixels tall the slope can quantise to
    "flat", so callers assert on the emotions whose symmetry is their defining
    property rather than on every emotion.
    """
    size = w._size
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    a = w._animator
    a.eye_state = "open"
    a.look_x = a.look_y = 0.0
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    w._draw_eyes(p, size / 2, size / 2, a)
    p.end()

    kind = _kind_map(img, size)

    def at(x, y):
        return kind[x][y] if 0 <= x < size and 0 <= y < size else None

    # Find the eyes by their WHITE, not by the lid. Locating them from lid
    # pixels alone meant that when the lid was subtle the two eyes merged into
    # one run, or a single eye split in two, and the check reported None or a
    # bogus pair. The eye white is bright and unambiguous.
    xs = [x for x in range(size)
          if any(at(x, y) == "white" for y in range(size))]
    if not xs:
        return None
    runs = [[xs[0]]]
    for x in xs[1:]:
        if x - runs[-1][-1] <= 2:
            runs[-1].append(x)
        else:
            runs.append([x])
    # Exactly two eyes. Any other count means the render is malformed, and the
    # slope question cannot be asked of it.
    if len(runs) != 2:
        return None

    slopes = []
    for i, run in enumerate(runs):
        x0, x1 = run[0], run[-1]
        eye = [(x, y) for x in range(x0, x1 + 1) for y in range(size)
               if at(x, y) in ("white", "lid")]
        if not eye:
            return None
        top = min(y for _, y in eye)
        bottom = max(y for _, y in eye)
        # The eye's vertical midpoint. The lid only ever covers the TOP of an
        # eye, so restricting to the upper half stops the sample from catching
        # the eye's own bottom curve, which is not part of the lid and which
        # made a clean slant read as an alternating zig-zag.
        mid = (top + bottom) // 2

        # For each column, how far down the lid reaches within the upper half.
        edge = {}
        for x in range(x0, x1 + 1):
            ys = [y for y in range(top, mid + 1) if at(x, y) == "lid"]
            if ys:
                edge[x] = max(ys)
        # A real lid spans most of the eye's width. A couple of stray columns
        # is not a lid, and treating it as one is how a lidless eye once
        # reported a slope.
        if len(edge) < max(3, (x1 - x0 + 1) // 2):
            return None

        # Compare the OUTER third of the eye against the INNER third. Sampling
        # thirds instead of single columns is what makes this survive
        # antialiasing: one pixel of jitter cannot move an average of three.
        cols = sorted(edge)
        q = max(1, len(cols) // 3)
        outer = sum(edge[x] for x in cols[:q]) / q
        inner = sum(edge[x] for x in cols[-q:]) / q
        # Ask the same question of both eyes: does the lid edge sit lower at the
        # inner corner? The columns run left-to-right for both eyes, so the
        # right eye's inner corner is at the LOW-x end and its sign must flip.
        toward_centre = (inner - outer) * (1 if i == 0 else -1)
        # A sub-pixel difference is not a slope, it is quantisation. Treating it
        # as one is how a rounding artefact once read as a confident "HIGHER".
        # Three quarters of a pixel is the finest honest resolution here.
        if abs(toward_centre) < 0.75:
            slopes.append("flat")
        else:
            slopes.append("LOWER" if toward_centre > 0 else "HIGHER")
    return slopes


def main() -> int:
    print("emotion vocabulary")

    for name in LIBRARY_EMOTIONS:
        check(f"library emotion {name!r} exists and resolves",
              name in EMOTIONS and resolve_emotion(name).name == name)

    check("every emotion has a non-empty name",
          all(e.name for e in EMOTIONS.values()))
    check("EMOTIONS keys match their Emotion.name",
          all(k == v.name for k, v in EMOTIONS.items()),
          str([k for k, v in EMOTIONS.items() if k != v.name]))
    check("emotion_names() covers the whole dict",
          emotion_names() == sorted(EMOTIONS))
    check("at least the ten library emotions are present",
          len(EMOTIONS) >= 10, f"got {len(EMOTIONS)}")

    print("resolution and aliases")

    check("unknown emotion returns NEUTRAL",
          resolve_emotion("definitely-not-an-emotion") is NEUTRAL)
    # `neutral` is a legitimate alias target, so the claim is that every alias
    # lands on a name the dict or NEUTRAL knows — not that all of them are
    # canonical emotions.
    bad_aliases = [alias for alias in EMOTION_ALIASES
                   if resolve_emotion(alias) is not NEUTRAL
                   and resolve_emotion(alias).name not in EMOTIONS]
    check("every alias resolves to a known emotion or to neutral",
          not bad_aliases, str(bad_aliases))
    check("sad -> sadness", resolve_emotion("sad").name == "sadness")
    check("afraid -> fear", resolve_emotion("afraid").name == "fear")
    check("tired -> sleepy", resolve_emotion("tired").name == "sleepy")
    check("explicit neutral alias returns NEUTRAL",
          resolve_emotion("none") is NEUTRAL)

    # An emotion arrives from a model or a websocket, so a bad value must not
    # raise. A paint loop that throws is a blank character.
    try:
        for junk in ("", " ", "\n", "\t", "x" * 500, "🎉", "0", "None", "null"):
            resolve_emotion(junk)
        check("no input raises on any junk value", True)
    except Exception as exc:  # noqa: BLE001
        check("no input raises on any junk value", False, repr(exc))

    # The MoodEngine drives the expression through `Engine.sig_emotion`. Every
    # name it can produce must resolve to a real emotion, EXCEPT "neutral",
    # which correctly means "no expression" and returns the NEUTRAL sentinel.
    # Any other unmapped name means the character silently goes blank whenever
    # the user is in that mood, which reads as the feature being broken rather
    # than as a missing table entry.
    from backend.character.mood import _mood_name  # noqa: E402
    mood_names = set()
    for valence in (-1.0, -0.5, -0.2, 0.0, 0.2, 0.5, 1.0):
        for energy in (0.0, 0.2, 0.44, 0.5, 0.7, 1.0):
            mood_names.add(_mood_name(valence, energy))
    check("the mood engine produces several distinct names",
          len(mood_names) >= 5, str(sorted(mood_names)))

    unresolved = sorted(n for n in mood_names
                        if n != "neutral" and resolve_emotion(n) is NEUTRAL)
    check("every non-neutral mood maps to a real emotion",
          not unresolved, f"unmapped moods: {unresolved}")
    check("the engine's neutral mood resolves to no expression",
          resolve_emotion("neutral") is NEUTRAL)

    # Distinct moods must reach DISTINCT emotions, or the mood is being drawn
    # but the user cannot tell which one it is.
    resolved = {n: resolve_emotion(n).name for n in sorted(mood_names)}
    check("distinct moods produce distinct expressions",
          len(set(resolved.values())) == len(resolved), str(resolved))

    print("blending")

    a = Animator()
    check("an animator starts neutral",
          a.emotion is NEUTRAL and a.emotion_name == "neutral")

    a.set_emotion("surprise")
    check("set_emotion resolves to the named emotion",
          a.emotion.name == "surprise")
    a.set_emotion("nonsense")
    check("set_emotion ignores an unknown name",
          a.emotion.name == "neutral")

    # Surprise is fast, sadness is slow: the whole point of a per-emotion rate.
    fast = Animator()
    fast.set_emotion("surprise")
    fast.update(CharacterState.IDLE, 0.033)
    slow = Animator()
    slow.set_emotion("sadness")
    slow.update(CharacterState.IDLE, 0.033)
    fast_move = abs(fast.emotion_eye_open - 0.0)
    slow_move = abs(slow.emotion_eye_tilt - 0.0)
    check("a fast emotion moves further in one frame than a slow one",
          fast_move > slow_move / 10.0,
          f"fast={fast_move:.4f} slow={slow_move:.4f}")
    check("surprise attacks faster than sadness",
          EMOTIONS["surprise"].attack > EMOTIONS["sadness"].attack)

    settled = settle("surprise")
    check("blending settles on the target value",
          abs(settled._animator.emotion_eye_open
              - EMOTIONS["surprise"].eye_open) < 0.01,
          f"{settled._animator.emotion_eye_open:.3f}")

    back = Animator()
    back.set_emotion("joy")
    for _ in range(400):
        back.update(CharacterState.IDLE, 0.033)
    back.set_emotion(None)
    for _ in range(400):
        back.update(CharacterState.IDLE, 0.033)
    check("clearing the emotion returns to neutral values",
          abs(back.emotion_eye_squint) < 0.01 and abs(back.emotion_glow_delta) < 0.01,
          f"squint={back.emotion_eye_squint:.4f} delta={back.emotion_glow_delta:.4f}")

    print("glow composes with the state instead of replacing it")

    # BLOCKED forces its own glow to 0.0. An emotion must not relight it — that
    # is exactly what the first implementation did.
    blocked = settle("joy", CharacterState.BLOCKED)
    check("an emotion does not relight BLOCKED",
          blocked._animator.render_glow == 0.0,
          f"glow={blocked._animator.render_glow:.3f}")

    sleeping = settle("joy", CharacterState.SLEEPING)
    check("an emotion does not relight SLEEPING",
          sleeping._animator.render_glow == 0.0,
          f"glow={sleeping._animator.render_glow:.3f}")

    # THINKING wants a low, deliberate glow. Adding delight should raise it by
    # the delta, not overwrite it with the settings default.
    thinking = settle("neutral", CharacterState.THINKING)
    thinking_joy = settle("joy", CharacterState.THINKING)
    check("THINKING keeps its own baseline glow",
          abs(thinking._animator.render_glow - 0.3) < 0.02,
          f"glow={thinking._animator.render_glow:.3f}")
    check("a positive emotion lifts a state's glow by its delta",
          abs(thinking_joy._animator.render_glow - 0.55) < 0.03,
          f"glow={thinking_joy._animator.render_glow:.3f}")

    idle_neutral = settle("neutral", CharacterState.IDLE)
    idle_sad = settle("sadness", CharacterState.IDLE)
    check("a negative emotion dims a state's glow",
          idle_sad._animator.render_glow < idle_neutral._animator.render_glow,
          f"{idle_sad._animator.render_glow:.3f} vs "
          f"{idle_neutral._animator.render_glow:.3f}")
    check("sadness dims IDLE by its delta (0.6 - 0.2)",
          abs(idle_sad._animator.render_glow - 0.4) < 0.02,
          f"glow={idle_sad._animator.render_glow:.3f}")

    # Feeding output back in as input would ramp a delta to the clamp. Holding an
    # emotion for a long time must not drift.
    hold = Animator()
    hold.set_emotion("joy")
    for _ in range(400):
        hold.update(CharacterState.IDLE, 0.033)
    early = hold.render_glow
    for _ in range(400):
        hold.update(CharacterState.IDLE, 0.033)
    check("a held emotion does not compound its glow",
          abs(hold.render_glow - early) < 0.001,
          f"{early:.4f} -> {hold.render_glow:.4f}")
    # IDLE eases its own glow toward `_base_glow` by reading the current value,
    # so if the emotion ever wrote `glow_intensity` the state's easing would
    # chase the emotion's output and never settle. The state's figure must stay
    # put while only the delta moves.
    check("the state's own glow is untouched by the emotion",
          abs(hold.glow_intensity - 0.6) < 0.01,
          f"state glow={hold.glow_intensity:.4f}")
    check("the emotion's contribution is carried separately",
          abs(hold._emotion_glow_delta - EMOTIONS["joy"].glow_delta) < 0.05,
          f"delta={hold._emotion_glow_delta:.4f}")

    print("the eyes actually draw the emotion")

    # Neutral has no tilt, so no lid is drawn at all. This is the control for
    # the pixel checks below: without it, a lid that was ALWAYS drawn would make
    # every emotion look like it had one.
    neutral_lid = lid_slopes(settle("neutral"))
    check("neutral draws no lid", neutral_lid is None, str(neutral_lid))

    # Anger and sadness are SYMMETRIC: the two eyes agree. Confusion is the
    # opposite: its whole character is one eye up and one down, and a symmetric
    # confusion looks like a renderer fault. These three are asserted on real
    # pixels because the parameters can be right while the screen shows a flat
    # bar, which is exactly what shipped once.
    # Read at 128px, not the 64px default. At 64 the eye is about seven pixels
    # wide and a 20-degree tilt quantises to no visible slope at all, so both
    # emotions honestly read "flat" and a comparison between them says nothing.
    anger = lid_slopes(settle("anger", size=128))
    check("anger draws a lid", anger is not None, str(anger))
    check("anger is a symmetric scowl (both inner corners agree)",
          bool(anger) and anger[0] == anger[1], str(anger))

    sadness = lid_slopes(settle("sadness", size=128))
    check("sadness draws a lid", sadness is not None, str(sadness))
    check("sadness is symmetric (both eyes lean together)",
          bool(sadness) and sadness[0] == sadness[1], str(sadness))
    check("sadness leans the opposite way from anger",
          bool(anger) and bool(sadness) and "flat" not in anger + sadness
          and anger[0] != sadness[0],
          f"anger={anger} sadness={sadness}")

    confusion = lid_slopes(settle("confusion"))
    check("confusion draws a lid", confusion is not None, str(confusion))
    check("confusion is ASYMMETRIC (one eye up, one down)",
          bool(confusion) and confusion[0] != confusion[1], str(confusion))
    check("confusion's two eyes both point inward",
          bool(confusion) and "flat" not in confusion, str(confusion))

    check("confusion is declared non-mirrored",
          EMOTIONS["confusion"].eye_tilt_mirrored is False)
    check("anger is declared mirrored",
          EMOTIONS["anger"].eye_tilt_mirrored is True)
    # The defining properties have to survive at the sizes people actually run,
    # not just at one convenient size. A mirrored emotion's two eyes must AGREE;
    # a non-mirrored one's must OPPOSE. Asserting the agreement rather than a
    # particular direction keeps this honest when an eye is too short for the
    # slope to resolve — two flat eyes still agree, and that is still symmetric.
    for size in (64, 96, 128):
        mir = lid_slopes(settle("sadness", size=size))
        check(f"a mirrored emotion's eyes agree at size {size}",
              bool(mir) and mir[0] == mir[1], f"sadness={mir}")

        opp = lid_slopes(settle("confusion", size=size))
        check(f"a non-mirrored emotion's eyes oppose at size {size}",
              bool(opp) and opp[0] != opp[1], f"confusion={opp}")
        check(f"confusion's eyes are both non-flat at size {size}",
              bool(opp) and "flat" not in opp, str(opp))

    # Disgust is the second non-mirrored emotion. It is checked less strictly
    # than confusion because its heavier squint leaves the eye shorter, so at
    # the default size the slope can quantise away — but the eyes must never
    # come out AGREEING, which is the failure that would make a wince look like
    # a plain squint.
    disgust = lid_slopes(settle("disgust", size=128))
    check("disgust opposes its eyes (a wince, not a squint)",
          bool(disgust) and disgust[0] != disgust[1], str(disgust))
    check("disgust is declared non-mirrored",
          EMOTIONS["disgust"].eye_tilt_mirrored is False)

    # The user's actual complaint, checked directly: the lid must not paint
    # anything OUTSIDE the eye. The previous implementation anchored its wedge a
    # fixed distance above the eye and swung the corners by a large drop, so on
    # the right eye one corner flew clear of the eye and the wedge collapsed to
    # a thin diagonal sliver floating on the body, and on a symmetric scowl the
    # two eyes came out with opposite slopes.
    #
    # None of the slope checks above can see that: they sample only the eye's
    # own upper half, which is exactly where the overflow is NOT. This measures
    # the thing itself — every lid pixel must lie inside the eye it belongs to.
    print("the lid never paints outside the eye")

    def lid_overflow(emotion: str, size: int = 128) -> int:
        """Count lid pixels that fall outside every eye's rendered extent."""
        w = settle(emotion, size=size)
        w._animator.eye_state = "open"
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w._draw_eyes(p, size / 2, size / 2, w._animator)
        p.end()
        kind = _kind_map(img, size)

        # The eyes' extents are taken from their WHITE, which the lid never
        # covers away entirely, so each eye's box is known independently of
        # whatever the lid did.
        xs = [x for x in range(size)
              if any(kind[x][y] == "white" for y in range(size))]
        if not xs:
            return -1
        runs = [[xs[0]]]
        for x in xs[1:]:
            if x - runs[-1][-1] <= 2:
                runs[-1].append(x)
            else:
                runs.append([x])
        boxes = []
        for run in runs:
            ys = [y for x in range(run[0], run[-1] + 1) for y in range(size)
                  if kind[x][y] in ("white", "lid")]
            if ys:
                boxes.append((run[0], run[-1], min(ys), max(ys)))
        if len(boxes) != 2:
            return -1

        # Grow each box by one pixel: a lid pixel on the antialiased boundary
        # is legitimately part of that eye, and demanding exact containment
        # would fail on rounding rather than on geometry.
        outside = 0
        for x in range(size):
            for y in range(size):
                if kind[x][y] != "lid":
                    continue
                if not any(x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1
                           for x0, x1, y0, y1 in boxes):
                    outside += 1
        return outside

    for emo in ("anger", "sadness", "confusion", "disgust"):
        stray = lid_overflow(emo)
        check(f"every {emo} lid pixel is inside an eye", stray == 0,
              f"{stray} stray lid pixel(s)")

    print("openness and dilation stay inside the eye")

    # A fear pupil must not become a dark ring around a white hole.
    #
    # Drawn at 200px, and the WIDGET is 200px too. It used to build the widget
    # at the 64px default and paint it into a 200px image, so the eye was only
    # about seven pixels wide and a heavily squinted emotion such as `sleepy`
    # produced a pupil smaller than the antialiasing threshold — zero dark
    # pixels, and the check failed intermittently on nothing but rounding.
    W = 200
    for name in ("fear", "surprise", "joy", "sleepy"):
        w = settle(name, size=W)
        img = QImage(W, W, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        a = w._animator
        a.eye_state = "open"
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w._draw_eyes(p, W / 2, W / 2, a)
        p.end()
        dark = sum(1 for y in range(W) for x in range(W)
                   if ((img.pixel(x, y) >> 24) & 0xFF) > 100
                   and (img.pixel(x, y) >> 16) & 0xFF < 60)
        check(f"{name} draws a pupil", dark > 0, f"dark px={dark}")

    # Every emotion must be drawable in every state without raising: a paint
    # loop that throws is a blank character, and the states and emotions are
    # independent axes so this is a real cross-product.
    crashed = []
    w = CharacterWidget({"size": 64, "eyes": True})
    img = QImage(160, 160, QImage.Format.Format_ARGB32)
    for name in ["neutral"] + emotion_names():
        for state in CharacterState:
            w._animator.set_emotion(name)
            for _ in range(60):
                w._animator.update(state, 0.033)
            for eye_state in ("open", "closed", "x", "rem"):
                w._animator.eye_state = eye_state
                p = QPainter(img)
                try:
                    w._draw_eyes(p, 80, 80, w._animator)
                except Exception as exc:  # noqa: BLE001
                    crashed.append((name, state.name, eye_state, repr(exc)))
                finally:
                    p.end()
    check("every emotion draws in every state and eye state",
          not crashed, str(crashed[:3]))

    print("the widget exposes the layer")

    w = CharacterWidget({"size": 64, "eyes": True})
    check("a new widget reports neutral", w.emotion == "neutral")
    w.set_emotion("joy")
    check("set_emotion is visible through the widget",
          w.emotion == "joy")
    w.set_emotion("nonsense")
    check("the widget ignores an unknown emotion without raising",
          w.emotion == "neutral")

    print("a sprite skin still shows the emotion")

    # A sprite skin never reaches `_draw_eyes` — the GIF's eyes are baked in —
    # so the emotion has to survive as an overlay instead. This is the default
    # on a machine that has picked a skin, and without it every emotion is
    # simply invisible there.
    # QRectF, QImage and QColor come from the module-level imports above.

    def _sprite_band(emotion: str) -> tuple:
        """Average the wash band over a flat sprite, for one emotion."""
        w = CharacterWidget({"size": 64, "eyes": True})
        w.set_emotion(emotion)
        for _ in range(120):
            w._animator.update(CharacterState.IDLE, 0.033)
        img = QImage(140, 140, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        # A flat mid-grey stands in for the artwork, so anything measured is
        # the overlay itself rather than a property of some GIF.
        p.fillRect(20, 20, 100, 100, QColor(128, 128, 128))
        w._draw_sprite_emotion(p, QRectF(20, 20, 100, 100), w._animator)
        p.end()
        total = [0, 0, 0]
        n = 0
        for y in range(20, 65):
            for x in range(20, 120):
                c = img.pixelColor(x, y)
                total[0] += c.red()
                total[1] += c.green()
                total[2] += c.blue()
                n += 1
        return tuple(v // max(1, n) for v in total)

    flat = (128, 128, 128)
    neutral_band = _sprite_band("neutral")
    check("a neutral sprite is left alone", neutral_band == flat, str(neutral_band))

    # Every emotion must move the sprite. Being merely "not neutral" is not
    # enough on its own, but full pairwise distinctness is NOT achievable here
    # and asserting it would be a lie: several emotions differ only in eye
    # geometry, which a bitmap has no way to show, so they deliberately share a
    # tint. Measured, the overlay resolves 14 distinct washes across the 18
    # emotions, with four pairs colliding (happy/grumpy, joy/pride,
    # concern/sleepy, confusion/curiosity). Those pairs are still distinct on a
    # procedural body, where the eyes are drawn — on a sprite they are the
    # accepted cost of using somebody else's artwork.
    bands = {name: _sprite_band(name) for name in emotion_names()}
    unchanged = [n for n, b in bands.items() if b == flat]
    check("no emotion leaves the sprite untouched", not unchanged, str(unchanged))

    distinct = len({b for b in bands.values()})
    check("the overlay resolves most emotions to a distinct wash",
          distinct >= 14,
          f"{distinct} distinct of {len(bands)}")

    # The pairs that do collide must be the ones that only differ by eye shape.
    # If a pair with genuinely different colours collided, the tint is being
    # computed wrongly rather than the artwork being limited.
    colliding = sorted(
        tuple(sorted(n for n, b2 in bands.items() if b2 == b))
        for b in set(bands.values())
    )
    colliding = [c for c in colliding if len(c) > 1]
    check("only shape-only pairs share a wash",
          set(colliding) <= {
              ("grumpy", "happy"), ("joy", "pride"),
              ("concern", "sleepy"), ("confusion", "curiosity"),
          },
          str(colliding))

    # The direction has to match the emotion, not just be "some colour". Joy
    # warms, sadness cools — if these were swapped the check above would still
    # pass while the character felt exactly wrong.
    def _warmth(band: tuple) -> int:
        return band[0] - band[2]

    check("joy warms the sprite", _warmth(bands["joy"]) > _warmth(neutral_band),
          str(bands["joy"]))
    check("sadness cools the sprite",
          _warmth(bands["sadness"]) < _warmth(neutral_band), str(bands["sadness"]))
    check("anger and love warm, sadness and sleepy cool",
          _warmth(bands["anger"]) > 0 and _warmth(bands["love"]) > 0
          and _warmth(bands["sadness"]) < 0 and _warmth(bands["sleepy"]) < 0,
          f'anger={_warmth(bands["anger"])} love={_warmth(bands["love"])} '
          f'sadness={_warmth(bands["sadness"])} sleepy={_warmth(bands["sleepy"])}')

    print()
    print("the eyes match the reference's proportions and are alive")

    # Web-Eye-Animation's eyes are the whole point of the library: white
    # capsules 2.5x taller than wide. An earlier version of this code drew
    # small round pupils instead, which is a different design entirely — and
    # the checks all passed, because they only asked whether SOMETHING drew.
    def _eye_boxes(size: int):
        """Return (w, h) per eye, measured from rendered pixels."""
        w = CharacterWidget({"size": size, "eyes": True, "color": "#1a1a22"})
        w._animator.set_emotion("neutral")
        for _ in range(120):
            w._animator.update(CharacterState.IDLE, 0.033)
        # Pin the gaze. Without this the pupil can sit anywhere the drift left
        # it, and a pupil touching an edge changes the measured box by a pixel
        # or two — enough to move the ratio out of the band at 64px, where the
        # eye is only five or six pixels wide. That is what made this check fail
        # intermittently.
        w._animator.look_x = 0.0
        w._animator.look_y = 0.0
        w._animator.gaze_drift_x = 0.0
        w._animator.gaze_drift_y = 0.0
        # Eyes open, for the same reason as `_pupil_x` below: a blink
        # closes them into a thin line, and a closed eye measures about
        # 0.27 wide-to-tall instead of the ~2.5 an open one gives. The
        # comment above widened the tolerance for this; pinning the state
        # removes the cause instead, and the ratio then holds on its own.
        w._animator.eye_state = "open"
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w._draw_eyes(p, size / 2, size / 2, w._animator)
        p.end()
        cols = [x for x in range(size)
                if any(img.pixelColor(x, y).lightness() > 150
                       for y in range(size))]
        if not cols:
            return []
        runs, cur = [], [cols[0]]
        for x in cols[1:]:
            if x - cur[-1] <= 2:
                cur.append(x)
            else:
                runs.append(cur)
                cur = [x]
        runs.append(cur)
        boxes = []
        for r in runs:
            ys = [y for y in range(size)
                  for x in r if img.pixelColor(x, y).lightness() > 150]
            boxes.append((r[-1] - r[0] + 1, max(ys) - min(ys) + 1))
        return boxes

    for size in (64, 128):
        boxes = _eye_boxes(size)
        check(f"two eyes are drawn at {size}px", len(boxes) == 2, str(boxes))
        if len(boxes) != 2:
            continue
        ratios = [h / w for w, h in boxes]
        check(f"each eye is taller than it is wide at {size}px",
              all(r > 1.5 for r in ratios), f"{ratios}")
        # The reference is 25vh / 10vw = 2.5. A generous band, because the
        # measured ratio shifts with antialiasing at small sizes.
        check(f"the eyes are near the reference's 2.5 ratio at {size}px",
              all(2.0 < r < 3.2 for r in ratios), f"{ratios}")

    # Gaze must MOVE. Measured at a large size, and isolated to the eyes alone:
    # an earlier measurement scanned one row of the whole character, where body
    # pixels dominated and a working gaze read as a frozen one.
    def _pupil_x(look_x: float) -> float:
        size = 128
        w = CharacterWidget({"size": size, "eyes": True, "color": "#1a1a22"})
        w._animator.set_emotion("neutral")
        for _ in range(120):
            w._animator.update(CharacterState.IDLE, 0.033)
        w._animator.look_x = look_x
        w._animator.gaze_drift_x = 0.0
        w._animator.gaze_drift_y = 0.0
        # Force the eyes open, because a blink erases what is measured.
        #
        # The animator blinks on an unseeded `random.uniform(3.0, 6.0)`
        # redrawn EVERY frame, and the loop above runs 120 updates at
        # 0.033s - about four seconds, inside that window. So the blink
        # fires on some runs and not others, and when it does `eye_state`
        # is closed and no pupil is drawn at all. The assertion then fails
        # with "left=None", or "right=None", depending only on which eye
        # the coordinate scan happened to cover.
        #
        # It was flaky for real - roughly one run in ten. It is a defect in
        # the CHECK, not in the eyes: with the state pinned the gaze
        # measures 21 and 22 dark pixels on every run.
        w._animator.eye_state = "open"
        img = QImage(size, size, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w._draw_eyes(p, size / 2, size / 2, w._animator)
        p.end()
        dark = [(x, y) for y in range(size) for x in range(size // 2)
                if img.pixelColor(x, y).alpha() > 150
                and img.pixelColor(x, y).lightness() < 80]
        if not dark:
            return None
        return sum(x for x, _ in dark) / len(dark)

    left = _pupil_x(-1.0)
    right = _pupil_x(1.0)
    check("the pupil is drawn", left is not None and right is not None,
          f"left={left} right={right}")
    if left is not None and right is not None:
        travel = right - left
        check("the pupil looks left when told to", travel > 2.0,
              f"travel {travel:.2f}px")
        # Direction matters: a pupil that moves the wrong way is worse than a
        # still one, and a magnitude-only check would not catch it.
        check("the pupil moves in the correct direction", travel > 0,
              f"look_x=+1 gave x={right:.2f}, look_x=-1 gave x={left:.2f}")

    # The eyes must not be still when nothing is driving them.
    w = CharacterWidget({"size": 64, "eyes": True})
    w._animator.set_emotion("neutral")
    seen = set()
    for _ in range(200):
        w._animator.update(CharacterState.IDLE, 0.033)
        w._animator.look_x = 0.0
        w._animator.look_y = 0.0
        seen.add((round(w._animator.gaze_drift_x, 3),
                  round(w._animator.gaze_drift_y, 3)))
    check("the eyes wander on their own when the cursor is still",
          len(seen) > 20, f"{len(seen)} distinct gaze values")

    # ...but not while asleep. And the value must be CLEARED, not merely left
    # alone: an early return that skipped the update left the last awake value
    # frozen, so a character fell asleep still staring off to one side.
    w._animator.set_emotion("neutral")
    for _ in range(120):
        w._animator.update(CharacterState.IDLE, 0.033)
    awake = abs(w._animator.gaze_drift_x) + abs(w._animator.gaze_drift_y)
    for _ in range(60):
        w._animator.update(CharacterState.SLEEPING, 0.033)
    check("a sleeping character does not glance around",
          w._animator.gaze_drift_x == 0.0 and w._animator.gaze_drift_y == 0.0,
          f"drift=({w._animator.gaze_drift_x}, {w._animator.gaze_drift_y}) "
          f"after being awake (drift was {awake:.3f})")

    # Confusion carries no colour at all — it is pure eye shape — so it is the
    # one that a tint-only overlay silently drops.
    check("a shape-only emotion still registers on a sprite",
          bands["confusion"] != flat, str(bands["confusion"]))

    # And the overlay must be reachable at all: `_draw_sprite` has to call it,
    # or all of the above is testing a method nothing uses.
    sprite_src = open(os.path.join(ROOT, "backend", "character", "avatar.py"),
                      encoding="utf-8").read()
    check("the sprite path calls the emotion overlay",
          "_draw_sprite_emotion(painter, target, a)" in sprite_src)

    print("the engine delivers emotions to the character")

    # `Engine.set_emotion` is what the RPC and the mood tick both use, so it is
    # the seam where a change can silently stop reaching the widget. Driven
    # against the real signal, because the failure that matters is the wiring
    # between the two, not either half on its own.
    from backend.engine import Engine  # noqa: E402

    w = CharacterWidget({"size": 64, "eyes": True})
    engine = Engine(char_widget=w)
    engine.sig_emotion.connect(w.set_emotion)

    applied = []
    engine.sig_emotion.connect(lambda n: applied.append(n))

    check("the engine applies a known emotion",
          engine.set_emotion("joy") == "joy")
    check("the emotion reaches the widget through the signal",
          w.emotion == "joy", w.emotion)
    check("an alias is resolved before it is sent",
          engine.set_emotion("tired") == "sleepy" and w.emotion == "sleepy",
          w.emotion)
    check("an unknown emotion falls back to neutral rather than raising",
          engine.set_emotion("no-such-emotion") == "neutral",
          w.emotion)
    check("the engine reports what it actually applied",
          applied == ["joy", "sleepy", "neutral"], str(applied))

    # The RPC surface: the handler must exist and be reachable, because a
    # feature nothing can call is a feature nobody can test.
    import backend.ws_server as ws  # noqa: E402
    from backend.character.emotions import emotion_names as names  # noqa: E402
    check("character.emotions lists the vocabulary",
          "joy" in names() and "confusion" in names())

    src = open(os.path.join(ROOT, "backend", "ws_server.py"),
               encoding="utf-8").read()
    for method in ("character.setEmotion", "character.emotions"):
        check(f"{method} is registered",
              f'register("{method}"' in src)
    check("character.setState is still registered alongside it",
          'register("character.setState"' in src)

    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the emotion layer resolves, blends, and draws")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
