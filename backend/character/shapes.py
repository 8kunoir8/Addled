"""
Shape library — pure QPainter geometric shapes for the character.

All shapes are centered in a square bounding box of `size` pixels.
Rendered via QPainterPath for crisp DPI-independent vector graphics.

Shapes: triangle, circle, diamond, hexagon, star, square.
"""

from __future__ import annotations

import math
from typing import Callable

from PyQt6.QtCore import QPointF
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
    painter.setPen(QPainter.PenStyle.NoPen)
    margin = size * 0.06
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)


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
    painter.setPen(QPainter.PenStyle.NoPen)
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
}

DEFAULT_SHAPE = "triangle"
