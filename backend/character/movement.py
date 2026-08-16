"""
Movement engine — updates character position each frame.

Modes: wander (Brownian drift), seek (eased interpolation to target),
retreat (move to corner), patrol (orbit active window), hold (stay put).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from PyQt6.QtCore import QPointF, QRect
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication

from backend.character.states import CharacterState


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3


def _ease_in_out(t: float) -> float:
    return t * t * (3 - 2 * t)


@dataclass
class ScreenZone:
    """Region the character should avoid."""
    rect: QRect
    priority: int  # 1=soft, 2=hard (cursor), 3=blocked


class Mover:
    """Updates character position each frame (~30fps)."""

    def __init__(self, char_settings: dict):
        self._settings = char_settings
        self._pos = QPointF(200, 200)
        self._target = QPointF(200, 200)
        self._anchor = QPointF(200, 200)
        self._wander_angle = random.random() * math.pi * 2
        self._wander_timer = 0.0
        self._fly_start = QPointF(0, 0)
        self._fly_progress = 1.0
        self._float_offset = 0.0
        self._frame = 0
        self._size = int(char_settings.get("size", 64))
        self._speed = self._parse_speed(char_settings.get("movement_speed", "medium"))
        self._wander_range = int(char_settings.get("idle_wander_range", 300))
        self._hold = False  # Pause autonomous movement while user is dragging

        # Corner preferences for sleep/retreat
        corner_map = {
            "top-left": (0, 0),
            "top-right": (1, 0),
            "bottom-left": (0, 1),
            "bottom-right": (1, 1),
        }
        self._preferred_corner = corner_map.get(
            char_settings.get("preferred_corner", "top-right"), (1, 0)
        )

    @staticmethod
    def _parse_speed(speed: str) -> float:
        return {"slow": 20, "medium": 45, "fast": 90}.get(speed, 45)

    @property
    def pos(self) -> QPointF:
        return self._pos

    def set_target(self, x: float, y: float):
        self._fly_start = QPointF(self._pos)
        self._target = QPointF(x, y)
        self._fly_progress = 0.0

    def warp(self, x: float, y: float):
        self._pos = QPointF(x, y)
        self._target = QPointF(x, y)
        self._anchor = QPointF(x, y)
        self._fly_progress = 1.0

    def update(self, state: CharacterState, dt: float = 0.033):
        self._frame += 1

        # If user is dragging, skip autonomous movement
        if self._hold:
            self._float_offset *= 0.9
            return

        if state == CharacterState.SLEEPING:
            self._retreat_to_corner(dt)
        elif state in (CharacterState.ACTING, CharacterState.HAS_SUGGESTION):
            self._fly_to_target(dt)
        elif state == CharacterState.OBSERVING:
            self._patrol(dt)
        else:  # IDLE, THINKING, SPEAKING, BLOCKED, ERROR, WORKING, DREAMING
            self._wander(dt)

        # Idle float
        if state in (CharacterState.IDLE, CharacterState.SPEAKING, CharacterState.DREAMING):
            self._float_offset = math.sin(self._frame * 0.05) * 3
        else:
            self._float_offset *= 0.9

        self._clamp_to_screen()

    def _wander(self, dt: float):
        self._wander_timer += dt
        if self._wander_timer > random.uniform(2.0, 5.0):
            self._wander_timer = 0.0
            self._wander_angle += random.uniform(-1.0, 1.0)
            # Random target within wander range of anchor
            dx = math.cos(self._wander_angle) * self._wander_range * 0.5
            dy = math.sin(self._wander_angle) * self._wander_range * 0.5
            self._target = QPointF(
                self._anchor.x() + dx,
                self._anchor.y() + dy,
            )

        # Move toward target with easing
        dist = math.hypot(self._target.x() - self._pos.x(), self._target.y() - self._pos.y())
        if dist > 1:
            speed = min(self._speed * 0.8, dist * 0.5)
            t = speed / dist
            self._pos = QPointF(
                self._pos.x() + (self._target.x() - self._pos.x()) * t,
                self._pos.y() + (self._target.y() - self._pos.y()) * t,
            )

    def _fly_to_target(self, dt: float):
        self._fly_progress = min(1.0, self._fly_progress + dt * 3.0)
        t = _ease_out_cubic(self._fly_progress)
        self._pos = QPointF(
            self._fly_start.x() + (self._target.x() - self._fly_start.x()) * t,
            self._fly_start.y() + (self._target.y() - self._fly_start.y()) * t,
        )

    def _retreat_to_corner(self, dt: float):
        screen = QApplication.primaryScreen().geometry()
        corner_x = screen.width() - self._size - 20 if self._preferred_corner[0] else 20
        corner_y = 20 if self._preferred_corner[1] == 0 else screen.height() - self._size - 20
        target = QPointF(corner_x, corner_y)
        dist = math.hypot(target.x() - self._pos.x(), target.y() - self._pos.y())
        if dist > 5:
            speed = min(60, dist * 0.3)
            t = speed / dist
            self._pos = QPointF(
                self._pos.x() + (target.x() - self._pos.x()) * t,
                self._pos.y() + (target.y() - self._pos.y()) * t,
            )

    def _patrol(self, dt: float):
        # Simple patrol: orbit around a center point
        angle = self._frame * 0.02
        radius = 100
        self._pos = QPointF(
            self._anchor.x() + math.cos(angle) * radius,
            self._anchor.y() + math.sin(angle) * radius,
        )

    def _all_screens_bounds(self) -> QRect:
        """Union of every connected monitor (drag target is multi-screen)."""
        screens = QApplication.screens()
        if not screens:
            return QApplication.primaryScreen().geometry()
        left = min(s.geometry().left() for s in screens)
        top = min(s.geometry().top() for s in screens)
        right = max(s.geometry().right() for s in screens)
        bottom = max(s.geometry().bottom() for s in screens)
        return QRect(left, top, right - left, bottom - top)

    def _clamp_to_screen(self):
        bounds = self._all_screens_bounds()
        half = self._size / 2
        self._pos.setX(max(bounds.left() + half, min(bounds.right() - half, self._pos.x())))
        self._pos.setY(max(bounds.top() + half, min(bounds.bottom() - half, self._pos.y())))
