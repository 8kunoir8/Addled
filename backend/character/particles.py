"""
Particle system for character visual effects.

Types: zzz (sleep), sparkle (dream), gear (working),
glow_burst (listening), trail (acting), lightbulb (dream).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field


@dataclass
class Particle:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    life: float = 1.0
    max_life: float = 1.0
    size: float = 4.0
    kind: str = "sparkle"


class ParticleSystem:
    """Manages active particles for a character state."""

    def __init__(self):
        self._particles: list[Particle] = []
        self._zzz_timer: float = 0.0
        self._sparkle_timer: float = 0.0
        self._gear_angle: float = 0.0
        self._lightbulb_timer: float = 0.0

    def update(self, state, dt: float, cx: float, cy: float):
        """Emit and update particles based on character state."""
        from backend.character.states import CharacterState

        # Update existing particles
        for p in self._particles[:]:
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.life -= dt / p.max_life
            if p.life <= 0:
                self._particles.remove(p)

        # Emit new particles based on state
        match state:
            case CharacterState.SLEEPING:
                self._zzz_timer += dt
                if self._zzz_timer > 2.0 and len([p for p in self._particles if p.kind == "zzz"]) < 3:
                    self._zzz_timer = 0.0
                    self._particles.append(Particle(
                        cx + random.uniform(-8, 8), cy - 15,
                        vx=random.uniform(-10, 10), vy=random.uniform(-30, -15),
                        life=3.0, max_life=3.0, size=random.uniform(6, 10), kind="zzz",
                    ))
            case CharacterState.DREAMING:
                self._sparkle_timer += dt
                if self._sparkle_timer > 5.0:
                    self._sparkle_timer = 0.0
                    for _ in range(random.randint(1, 3)):
                        self._particles.append(Particle(
                            cx + random.uniform(-20, 20), cy + random.uniform(-20, 20),
                            vx=0, vy=0,
                            life=1.5, max_life=1.5, size=random.uniform(2, 5), kind="sparkle",
                        ))
                self._lightbulb_timer += dt
                if self._lightbulb_timer > 15.0:
                    self._lightbulb_timer = 0.0
                    self._particles.append(Particle(
                        cx, cy - 25,
                        vx=0, vy=random.uniform(-5, 0),
                        life=3.0, max_life=3.0, size=8, kind="lightbulb",
                    ))
            case CharacterState.WORKING:
                self._gear_angle += dt * 90  # 90°/s rotation
            case CharacterState.LISTENING | CharacterState.HAS_SUGGESTION:
                if len([p for p in self._particles if p.kind == "glow_burst"]) < 1:
                    self._particles.append(Particle(
                        cx, cy,
                        vx=0, vy=0,
                        life=1.0, max_life=1.0, size=60, kind="glow_burst",
                    ))

        # Keep particle count reasonable
        if len(self._particles) > 20:
            self._particles = self._particles[-20:]

    def get_particles(self) -> list[Particle]:
        return self._particles

    @property
    def gear_angle(self) -> float:
        return self._gear_angle
