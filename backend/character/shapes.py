"""
Shape library — pure QPainter geometric shapes for the character.

All shapes are centered in a square bounding box of `size` pixels.
Rendered via QPainterPath for crisp DPI-independent vector graphics.

Shapes: triangle, circle, diamond, hexagon, star, square.
"""

from __future__ import annotations

import math
from typing import Callable

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QPainterPath, QColor, QPainter


def draw_triangle(painter: QPainter, size: float, color: QColor):
    """Equilateral triangle, point up, rotated -35 (Addled-style)."""
    cx, cy = size / 2, size / 2
    r = size * 0.42
    angle_offset = math.radians(-35)
    points = []
    for i in range(3):
        a = angle_offset + i * math.radians(120)
        points.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    path = QPainterPath()
    path.moveTo(points[0])
    for p in points[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.fillPath(path, color)


def draw_circle(painter: QPainter, size: float, color: QColor):
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    margin = size * 0.06
    # PyQt6 drawEllipse requires ints (PyQt5 accepted floats)
    painter.drawEllipse(int(margin), int(margin),
                        int(size - 2 * margin), int(size - 2 * margin))


def draw_blob(painter: QPainter, size: float, color: QColor):
    """A soft round blob — the circle's friendlier cousin.

    Built as a superellipse (|x/a|^n + |y/b|^n = 1) rather than a plain
    ellipse, with the exponent controlling how boxy the silhouette is. An
    ellipse is exactly n = 2; the blob uses a little over 2, which keeps it
    round but gives it shoulders and a base instead of looking like a
    geometric primitive.

    A superellipse is used here rather than hand-placed Bézier control
    points because it cannot go wrong in the way those did: an earlier version
    placed the curve's extremes on the corners, which flattened the sides into
    a rectangle with rounded ends. The closed form has no such failure mode,
    and the exponent is a single number to reason about.
    """
    cx, cy = size / 2, size / 2
    # Slightly wider than tall: a circle reads as rigid, this reads as settled.
    rx = size * 0.44
    ry = size * 0.44 * 0.94
    # The shape exponent. 2.0 is an exact ellipse; higher is more rectangular.
    # 2.2 sits just past circular — enough to give it shoulders and a base
    # rather than reading as a disc, while keeping the sides from flattening
    # into a rounded rectangle. Measured at 64px it also holds its full width
    # for fewer rows than 2.35 does, so it stays visibly round.
    n = 2.2
    # Enough points that the curve is smooth at the largest size the character
    # supports, with no visible faceting.
    steps = 96

    path = QPainterPath()
    for i in range(steps + 1):
        t = 2.0 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        # The superellipse parametrisation. Guard against a zero base: at the
        # exact axes, cos or sin is 0 and 0 ** (2/n) is a legitimate 0.
        x = cx + rx * math.copysign(abs(ct) ** (2.0 / n), ct)
        y = cy + ry * math.copysign(abs(st) ** (2.0 / n), st)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    painter.fillPath(path, color)


def draw_diamond(painter: QPainter, size: float, color: QColor):
    cx, cy = size / 2, size / 2
    r = size * 0.42
    points = [
        QPointF(cx, cy - r), QPointF(cx + r, cy),
        QPointF(cx, cy + r), QPointF(cx - r, cy),
    ]
    path = QPainterPath()
    path.moveTo(points[0])
    for p in points[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.fillPath(path, color)


def draw_hexagon(painter: QPainter, size: float, color: QColor):
    cx, cy = size / 2, size / 2
    r = size * 0.40
    points = []
    for i in range(6):
        a = math.radians(60 * i - 30)
        points.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    path = QPainterPath()
    path.moveTo(points[0])
    for p in points[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.fillPath(path, color)


def draw_star(painter: QPainter, size: float, color: QColor):
    cx, cy = size / 2, size / 2
    outer_r = size * 0.42
    inner_r = size * 0.18
    points = []
    for i in range(10):
        a = math.radians(36 * i - 90)
        r = outer_r if i % 2 == 0 else inner_r
        points.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    path = QPainterPath()
    path.moveTo(points[0])
    for p in points[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.fillPath(path, color)


def draw_square(painter: QPainter, size: float, color: QColor):
    margin = size * 0.15
    painter.setBrush(color)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(int(margin), int(margin),
                            int(size - 2 * margin), int(size - 2 * margin),
                            size * 0.08, size * 0.08)


SHAPE_REGISTRY: dict[str, Callable] = {
    "triangle": draw_triangle,
    "circle": draw_circle,
    "diamond": draw_diamond,
    "hexagon": draw_hexagon,
    "star": draw_star,
    "square": draw_square,
    "blob": draw_blob,
}

# The blob is the default: a round character reads as a creature, where a
# triangle reads as a logo. The other shapes remain available by name.
DEFAULT_SHAPE = "blob"
