"""Verify the character's procedural shapes, and the blob in particular.

The shapes are drawn every frame at 30fps, so a mistake here is not a wrong
picture once — it is a wrong picture forever. They are also the one part of the
character that is pure geometry, which means they can be checked properly by
measuring the pixels they produce.

The blob earns its own cases because the first two attempts at it were both
wrong in ways that a casual look would pass:

  * The first used hand-placed Bezier control points that put the curve's
    extremes on the corners. The result was a rectangle with rounded ends: the
    width climbed to its maximum and then sat there for 27 straight rows. It
    looked plausible in a thumbnail and was measurably not a blob.

  * The second was a superellipse, which is the right construction, but the
    exponent was tuned by eye rather than measured.

So the cases below assert the SHAPE of the silhouette — that it tapers, that it
is symmetric, and that it stays round — rather than just that something was
drawn.

Qt needs a widget, so this runs offscreen.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_shapes.py
"""

import math
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

from backend.character import shapes  # noqa: E402
from backend.character.shapes import (  # noqa: E402
    DEFAULT_SHAPE,
    SHAPE_REGISTRY,
)

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        suffix = f"  <- {detail}" if detail else ""
        print(f"  FAIL {label}{suffix}")
        fails.append(label)


def _widths(fn, size: int) -> list[tuple[int, int, int]]:
    """Render one shape and return (y, left, right) for every opaque row."""
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fn(painter, size, QColor(255, 255, 255))
    painter.end()

    rows = []
    for y in range(size):
        xs = [x for x in range(size) if img.pixelColor(x, y).alpha() > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    return rows


def main() -> int:
    print("the shape library is intact")
    for name in ("blob", "circle", "triangle", "square", "diamond",
                 "hexagon", "star"):
        check(f"{name} is registered", name in SHAPE_REGISTRY)

    check("the default shape is one that exists",
          DEFAULT_SHAPE in SHAPE_REGISTRY, DEFAULT_SHAPE)
    check("the default is the blob", DEFAULT_SHAPE == "blob", DEFAULT_SHAPE)

    print()
    print("every shape draws inside its box")
    for name, fn in SHAPE_REGISTRY.items():
        for size in (32, 64, 128):
            rows = _widths(fn, size)
            if not rows:
                check(f"{name} draws at {size}px", False, "nothing rendered")
                continue
            y0, y1 = rows[0][0], rows[-1][0]
            left = min(r[1] for r in rows)
            right = max(r[2] for r in rows)
            # Inside the box, and not a speck. A shape that renders as a few
            # pixels would pass a "did it draw anything" test.
            ok = (left >= 0 and right < size and y0 >= 0 and y1 < size
                  and (y1 - y0) > size * 0.5 and (right - left) > size * 0.5)
            check(f"{name} fills its box at {size}px", ok,
                  f"x=[{left},{right}] y=[{y0},{y1}] size={size}")

    print()
    print("the blob is round, not a rounded rectangle")

    # Measured against a real ellipse at the same radius, because "does it look
    # round" is not a check. The blob must be close to an ellipse (it IS one,
    # slightly squared off) and clearly NOT a rectangle.
    blob = _widths(shapes.draw_blob, 64)
    blob_y0, blob_y1 = blob[0][0], blob[-1][0]
    blob_h = blob_y1 - blob_y0 + 1
    blob_w = max(r[2] for r in blob) - min(r[1] for r in blob) + 1

    check("the blob is wider than it is tall",
          blob_w >= blob_h, f"{blob_w} x {blob_h}")
    check("the blob is roughly as wide as it is tall",
          blob_w / blob_h < 1.2, f"ratio {blob_w / blob_h:.3f}")

    # The defining property: a blob tapers. Take the width at the vertical
    # middle and a quarter of the way up, and require the top to be narrower —
    # a rectangle would show no difference at all.
    def width_at(rows, y):
        for (yy, left, right) in rows:
            if yy == y:
                return right - left + 1
        return 0

    mid_y = blob_y0 + blob_h // 2
    quarter_y = blob_y0 + blob_h // 4
    w_mid = width_at(blob, mid_y)
    w_quarter = width_at(blob, quarter_y)
    check("the blob tapers toward the top", w_quarter < w_mid,
          f"quarter={w_quarter} mid={w_mid}")
    check("the taper is substantial, not a rounding artefact",
          w_mid - w_quarter >= 4, f"quarter={w_quarter} mid={w_mid}")

    # The regression that motivated this file: the broken version held its
    # maximum width across more than half the height. A real blob peaks only
    # briefly, because the widest point of a curve is a point, not a band.
    max_w = max(r[2] - r[1] + 1 for r in blob)
    flat = sum(1 for r in blob if r[2] - r[1] + 1 >= max_w - 1)
    check("the blob does not hold full width for half its height",
          flat < blob_h * 0.5, f"{flat} of {blob_h} rows at full width")

    # Symmetry, top to bottom and left to right. A shape built from a formula
    # should be exact here; an off-by-one would show as a lopsided character.
    top = [r[2] - r[1] + 1 for r in blob[:blob_h // 2]]
    bottom = [r[2] - r[1] + 1 for r in blob[blob_h - len(top):]][::-1]
    worst = max(abs(a - b) for a, b in zip(top, bottom))
    check("the blob is symmetric top to bottom", worst <= 2,
          f"worst mismatch {worst}px")

    off = [abs((r[1] + r[2]) / 2.0 - 31.5) for r in blob]
    check("the blob is centred horizontally", max(off) <= 1.0,
          f"worst centre offset {max(off):.1f}px")

    print()
    print("the blob differs from a plain circle")
    # A blob that measured identically to the circle would be a pointless
    # seventh shape. The difference is the squared-off shoulders, which show up
    # as a fuller width partway up the side.
    circle = _widths(shapes.draw_circle, 64)
    blob_q = width_at(blob, quarter_y)
    circle_q = width_at(circle, circle[0][0] + (circle[-1][0] - circle[0][0] + 1) // 4)
    check("the blob has fuller shoulders than the circle",
          blob_q > circle_q, f"blob={blob_q} circle={circle_q}")

    print()
    print("the blob holds up at other sizes")
    for size in (32, 48, 96, 128):
        rows = _widths(shapes.draw_blob, size)
        h = rows[-1][0] - rows[0][0] + 1
        m = width_at(rows, rows[0][0] + h // 2)
        q = width_at(rows, rows[0][0] + h // 4)
        check(f"the blob still tapers at {size}px", q < m, f"q={q} m={m}")
        flat_n = sum(1 for r in rows if r[2] - r[1] + 1
                     >= max(rr[2] - rr[1] + 1 for rr in rows) - 1)
        check(f"the blob stays round at {size}px", flat_n < h * 0.5,
              f"{flat_n} of {h} rows at full width")

    print()
    print("the default shape is the same everywhere it is declared")

    # The shape is declared in THREE places, and they must agree. An earlier
    # version of this file only checked that `SHAPE_REGISTRY[DEFAULT_SHAPE]`
    # appears in the avatar, which passed while the widget still shipped its own
    # separate `"shape": "triangle"` — so the blob was unreachable in the real
    # app despite every check being green. Assert the VALUES, not their presence.
    from backend.character.avatar import _default_settings  # noqa: E402
    from backend.config import DEFAULT_SETTINGS  # noqa: E402

    widget_shape = _default_settings().get("shape")
    check("the widget's own default is the blob", widget_shape == "blob",
          str(widget_shape))
    config_shape = DEFAULT_SETTINGS.get("character", {}).get("shape")
    check("the shipped config default is the blob", config_shape == "blob",
          str(config_shape))
    check("all three declarations agree",
          widget_shape == config_shape == DEFAULT_SHAPE == "blob",
          f"widget={widget_shape} config={config_shape} shapes={DEFAULT_SHAPE}")

    # And a widget built with no settings must actually render a blob. This is
    # the path a first run takes, and the one the stale default broke.
    from backend.character.avatar import CharacterWidget  # noqa: E402

    fresh = CharacterWidget({})
    # Render through the real paint path and measure the silhouette.
    from PyQt6.QtGui import QImage as _QImage, QPainter as _QPainter  # noqa: E402
    img = _QImage(140, 140, _QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    painter = _QPainter(img)
    painter.setRenderHint(_QPainter.RenderHint.Antialiasing)
    from backend.character.states import CharacterState  # noqa: E402
    fresh._animator.update(CharacterState.IDLE, 0.033)
    fresh._paint(painter)
    painter.end()
    painted = [(y, min(xs), max(xs)) for y in range(140)
               for xs in [[x for x in range(140)
                           if img.pixelColor(x, y).alpha() > 128]] if xs]
    check("a fresh widget paints something", len(painted) > 50,
          f"{len(painted)} rows")
    if painted:
        h = painted[-1][0] - painted[0][0] + 1
        m = width_at(painted, painted[0][0] + h // 2)
        q = width_at(painted, painted[0][0] + h // 4)
        # A triangle tapers to a point at the TOP, so its quarter-width is far
        # wider than a blob's; the blob's taper is gentle. This distinguishes
        # the two shapes without hard-coding either.
        ratio = q / m if m else 0
        check("a fresh widget paints a blob, not a triangle", ratio > 0.7,
              f"quarter/mid width ratio {ratio:.2f} (a triangle is near 0.5)")

    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the shapes draw, and the blob is round")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
