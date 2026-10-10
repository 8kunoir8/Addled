"""Render the character to PNG files, and print measurements, headlessly.

This exists because there is no vision model available to the assistant: it
cannot look at the character on screen, and it cannot look at its own renders
either. The substitute is a PNG on disk plus numbers, which is enough to catch
the failures that matter — a shape that is the wrong size, an eye that is
missing, a lid on the wrong side, an animation that does not move.

It was written after a real miss. A hand-rolled one-off measurement scanned a
single row across the whole character, where body pixels dominated, and
reported a working gaze as completely frozen. Building the measurement once, in
one place, is what stops that being repeated.

Usage
    python render_character.py                      # a sheet of every emotion
    python render_character.py joy anger confusion  # just these, side by side
    python render_character.py --state thinking     # drive a state instead
    python render_character.py --sheet              # the full contact sheet

Output
    PNGs into the directory given by --out (default: a temp dir), and a report
    on stdout describing what was measured in each frame.

Everything runs offscreen, so it needs no display and does not disturb the
running app.
"""

from __future__ import annotations

import argparse
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt6.QtCore import QRectF, Qt  # noqa: E402
from PyQt6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.avatar import CharacterWidget  # noqa: E402
from backend.character.states import CharacterState  # noqa: E402

# A mid-tone body, so a render shows both the silhouette and the white eyes.
# Pure black would make the eyes the only thing visible, which hides half of
# what needs checking.
BODY = "#39435c"
BACKDROP = QColor(24, 26, 32, 255)


def make_widget(size: int, shape: str = "blob", color: str = BODY,
                eyes: bool = True) -> CharacterWidget:
    return CharacterWidget({"shape": shape, "color": color, "eyes": eyes,
                            "size": size})


def settle(widget: CharacterWidget, state: CharacterState, frames: int = 120):
    """Run the animator until an expression has stopped moving."""
    for _ in range(frames):
        widget._animator.update(state, 0.033)


def render(widget: CharacterWidget) -> QImage:
    """Paint the whole character onto a backdrop, exactly as the app does."""
    pad = 40
    size = widget._size + pad
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(BACKDROP)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    widget._paint(painter)
    painter.end()
    return img


def render_eyes_only(widget: CharacterWidget) -> QImage:
    """Paint just the eyes on a flat field.

    Needed when measuring the eyes themselves: on the full character the body
    fills most rows, and any measurement that scans them picks up the body
    instead of the pupils.
    """
    size = widget._size
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(BACKDROP)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    widget._draw_eyes(painter, size / 2, size / 2, widget._animator)
    painter.end()
    return img


# ---- measurement ------------------------------------------------------------

def eye_clusters(img: QImage, min_lightness: int = 150):
    """Find the white eye capsules as (x0, x1, y0, y1) boxes, left to right."""
    w, h = img.width(), img.height()
    cols = []
    for x in range(w):
        if any(img.pixelColor(x, y).lightness() > min_lightness
               for y in range(h)):
            cols.append(x)
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
    for run in runs:
        ys = [y for y in range(h) for x in run
              if img.pixelColor(x, y).lightness() > min_lightness]
        if ys:
            boxes.append((run[0], run[-1], min(ys), max(ys)))
    return boxes


def dark_centroid(img: QImage, x0: int, x1: int, max_lightness: int = 80):
    """Centre of the dark pixels (the pupil) inside a horizontal band."""
    pts = [(x, y) for y in range(img.height()) for x in range(x0, x1 + 1)
           if img.pixelColor(x, y).alpha() > 150
           and img.pixelColor(x, y).lightness() < max_lightness]
    if not pts:
        return None
    return (sum(x for x, _ in pts) / len(pts),
            sum(y for _, y in pts) / len(pts), len(pts))


def lid_coverage(img: QImage, box) -> tuple:
    """How much of each eye is covered, as (left_fraction, right_fraction).

    A lid is drawn in the body colour over the top of the eye, so "covered" is
    exactly "not white inside the eye's own bounding box". Comparing the two
    eyes is how an asymmetry shows up as a number.
    """
    x0, x1, y0, y1 = box
    out = []
    for lo, hi in ((x0, (x0 + x1) // 2), ((x0 + x1) // 2 + 1, x1)):
        total = covered = 0
        for y in range(y0, y1 + 1):
            for x in range(lo, hi + 1):
                total += 1
                if img.pixelColor(x, y).lightness() < 150:
                    covered += 1
        out.append(covered / total if total else 0.0)
    return tuple(out)


def report(tag: str, widget: CharacterWidget) -> dict:
    """Measure one rendered character and print what was found."""
    img = render(widget)
    eyes_img = render_eyes_only(widget)
    boxes = eye_clusters(eyes_img)
    info = {"tag": tag, "eyes": len(boxes)}
    parts = [f"{tag:14} eyes={len(boxes)}"]
    if len(boxes) == 2:
        left, right = boxes
        lw, lh = left[1] - left[0] + 1, left[3] - left[2] + 1
        rw, rh = right[1] - right[0] + 1, right[3] - right[2] + 1
        info.update(left_box=left, right_box=right,
                    left_ratio=round(lh / lw, 2), right_ratio=round(rh / rw, 2))
        parts.append(f"L={lw}x{lh}({lh/lw:.2f}) R={rw}x{rh}({rh/rw:.2f})")
        # Where each eye sits vertically — a lid or an offset moves one.
        parts.append(f"Ltop={left[2]} Rtop={right[2]}")
        # The gap between the two eyes, for spacing checks.
        parts.append(f"gap={right[0]-left[1]-1}")
    for side, box in zip(("L", "R"), boxes):
        c = dark_centroid(eyes_img, box[0], box[1])
        if c:
            parts.append(f"{side}pupil=({c[0]:.0f},{c[1]:.0f})")
    print("  " + "  ".join(parts))
    return info


def save(img: QImage, path: str):
    img.save(path)


def sheet(images: list, cols: int, path: str, label_h: int = 0):
    """Compose several frames into one contact sheet PNG."""
    if not images:
        return
    w, h = images[0].width(), images[0].height()
    rows = (len(images) + cols - 1) // cols
    out = QImage(w * cols, (h + label_h) * rows, QImage.Format.Format_ARGB32)
    out.fill(QColor(12, 13, 16, 255))
    p = QPainter(out)
    for i, im in enumerate(images):
        x, y = (i % cols) * w, (i // cols) * (h + label_h)
        p.drawImage(x, y, im)
    p.end()
    out.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("emotions", nargs="*",
                    help="emotions to render (default: a representative set)")
    ap.add_argument("--state", default="idle", help="agent state to hold")
    ap.add_argument("--size", type=int, default=96)
    ap.add_argument("--out", default=os.path.join(
        os.environ.get("TEMP", "/tmp"), "addled_render"))
    ap.add_argument("--sheet", action="store_true",
                    help="render every emotion into one sheet")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    state = CharacterState[args.state.upper()]

    from backend.character.emotions import emotion_names
    wanted = args.emotions or ["neutral", "joy", "anger", "sadness",
                               "fear", "disgust", "confusion", "love",
                               "surprise", "sleepy"]
    if args.sheet:
        wanted = ["neutral"] + emotion_names()

    print(f"state={state.name}  size={args.size}  out={args.out}")
    print()

    images = []
    for name in wanted:
        w = make_widget(args.size)
        w.set_emotion(name)
        settle(w, state)
        report(name, w)
        img = render(w)
        save(img, os.path.join(args.out, f"{name}.png"))
        images.append(img)

    if len(images) > 1:
        path = os.path.join(args.out, "sheet.png")
        sheet(images, cols=min(6, len(images)), path=path)
        print()
        print(f"contact sheet -> {path}")
    print()
    print(f"{len(images)} PNG(s) written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
