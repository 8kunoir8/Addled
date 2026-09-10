"""
Floating character widget — frameless, always-on-top, transparent QWidget.

Renders via QPainter. Shape, color, glow, eyes — all from settings.
Position updated by movement engine at 30fps.
Supports drag to reposition and click for context menu.
"""

from __future__ import annotations

import math

from PyQt6.QtWidgets import QWidget, QApplication, QMenu
from PyQt6.QtCore import Qt, QTimer, QPointF, QPoint, QRectF, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QPen, QBrush, QPainterPath, QRadialGradient, QFont,
    QCursor, QMovie, QPixmap,
)

from backend.character.shapes import SHAPE_REGISTRY, DEFAULT_SHAPE
from backend.character.movement import Mover
from backend.character.animation import Animator
from backend.character.states import StateMachine, CharacterState
from backend.character.particles import ParticleSystem, Particle
from backend.character import sprite_skin


# Addled blue as fallback
CURSOR_BLUE = QColor(0x33, 0x80, 0xFF)


def _default_settings() -> dict:
    return {
        "shape": "triangle",
        "color": "#3380FF",
        "glow_intensity": 0.6,
        "eyes": True,
        "size": 64,
        "movement_speed": "medium",
        "idle_wander_range": 300,
        "preferred_corner": "top-right",
    }


class CharacterWidget(QWidget):
    """Free-floating character. Procedural shapes by default; sprite-skin
    (codex-pet style GIF) when one is active. Draggable, clickable, top-most."""

    # Emitted from any thread → queued to the GUI thread (thread-safe skin swap)
    skin_apply_requested = pyqtSignal(object)

    def __init__(self, settings: dict | None = None, parent=None):
        super().__init__(parent)
        self._settings = settings or _default_settings()

        # Sprite skin state
        self._skin_id: str | None = None
        self._skin_meta: dict | None = None
        self._movie: QMovie | None = None
        self._movie_file: str | None = None
        self._movie_state: CharacterState | None = None
        self.skin_apply_requested.connect(self.apply_skin)

        # Frameless, transparent, always on top — accepts mouse for drag/click
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._size = self._settings.get("size", 64)
        self.setFixedSize(self._size + 40, self._size + 40)  # Extra for glow

        # Core systems
        self._mover = Mover(self._settings)
        self._animator = Animator()
        self._animator._base_glow = self._settings.get("glow_intensity", 0.6)
        self._animator.glow_intensity = self._settings.get("glow_intensity", 0.6)
        self._state_machine = StateMachine()
        self._particles = ParticleSystem()
        self._color = self._parse_color(self._settings.get("color", "#3380FF"))
        self._show_eyes = self._settings.get("eyes", True)

        # Position — start near top-right corner
        screen = QApplication.primaryScreen().geometry()
        self._mover.warp(int(screen.width() * 0.85), int(screen.height() * 0.15))
        self._sync_position()

        # Startup greeting
        self._startup_frame = 0

        # Drag state
        self._dragging = False
        self._drag_start = QPoint()
        self._last_global = QPoint()

        # Interaction callbacks
        self._on_ask_callback = None
        self._on_menu_callback = None

        # 30fps timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

        # Cursor hints
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- public API -----------------------------------------------------------

    def set_agent_state(self, agent_state: str):
        """Called by engine: 'idle', 'observing', 'in_meeting', etc."""
        self._state_machine.transition(agent_state)
        self._maybe_reload_skin()
        self.update()

    def point_at(self, x: float, y: float):
        """Move character near (x, y) on screen."""
        self._mover.set_target(x - 40, y - 40)

    def set_settings(self, settings: dict):
        self._settings = settings
        self._size = settings.get("size", 64)
        self.setFixedSize(self._size + 40, self._size + 40)
        self._color = self._parse_color(settings.get("color", "#3380FF"))
        self._show_eyes = settings.get("eyes", True)
        self._mover._size = self._size
        glow = settings.get("glow_intensity", 0.6)
        self._animator.glow_intensity = glow
        self._animator._base_glow = glow
        self.update()

    def set_interaction_callbacks(self, on_ask=None, on_menu=None):
        """Set callbacks for click and right-click."""
        self._on_ask_callback = on_ask
        self._on_menu_callback = on_menu

    # ---- sprite skin (codex-pet style) ---------------------------------------

    def request_apply_skin(self, skin_id: str | None):
        """Thread-safe skin swap — call from the WS thread."""
        self.skin_apply_requested.emit(skin_id)

    def apply_skin(self, skin_id: str | None):
        """Activate a skin (GUI thread). None/'' → procedural shapes."""
        self._skin_id = (skin_id or "") or None
        self._skin_meta = None
        self._movie_state = None
        if not self._skin_id:
            self._stop_movie()
            self.update()
            return
        path = sprite_skin.resolve_clip_path(self._skin_id, "idle")
        if not path:
            # Skin folder vanished — revert to procedural body
            self._skin_id = None
            self._stop_movie()
            self.update()
            return
        try:
            with open(sprite_skin.SKINS_DIR / self._skin_id / "skin.json",
                      "r", encoding="utf-8") as f:
                import json
                self._skin_meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._skin_meta = {}
        self._maybe_reload_skin()
        self.update()

    def _stop_movie(self):
        if self._movie is not None:
            try:
                self._movie.stop()
                self._movie.deleteLater()
            except Exception:
                pass
            self._movie = None
            self._movie_file = None

    def _clip_for(self, state_key: str) -> str | None:
        """Resolve the GIF path for a state from the cached skin meta."""
        if not self._skin_id:
            return None
        states = (self._skin_meta or {}).get("states", {})
        clip = states.get(state_key) or states.get("idle")
        if clip:
            path = sprite_skin.SKINS_DIR / self._skin_id / clip
            if path.is_file():
                return str(path)
        gifs = sorted(sprite_skin.SKINS_DIR.joinpath(self._skin_id).glob("*.gif"))
        return str(gifs[0]) if gifs else None

    def _maybe_reload_skin(self):
        """Swap the movie when the agent state's clip changes."""
        if not self._skin_id:
            return
        state = self._state_machine.current
        if state == self._movie_state and self._movie is not None:
            return
        path = self._clip_for(state.name.lower())
        if not path:
            return
        self._movie_state = state
        if path == self._movie_file and self._movie is not None:
            return
        self._stop_movie()
        movie = QMovie(path, parent=self)
        movie.setCacheMode(QMovie.CacheMode.CacheAll)
        movie.frameChanged.connect(lambda _i: self.update())
        movie.start()
        self._movie = movie
        self._movie_file = path

    # ---- mouse interaction ----------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.position().toPoint()
            self._dragging = False
            self._mover._hold = True
            self._last_global = event.globalPosition().toPoint()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())

    def mouseMoveEvent(self, event):
        if self._drag_start.isNull():
            return
        delta = event.position().toPoint() - self._drag_start
        if delta.manhattanLength() > 4:  # 4px dead zone
            if not self._dragging:
                self._dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            gdelta = event.globalPosition().toPoint() - self._last_global
            self.move(self.pos() + gdelta)
            self._last_global = event.globalPosition().toPoint()
            self._mover.warp(self.pos().x() + self._size // 2 + 20,
                             self.pos().y() + self._size // 2 + 20)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._dragging:
                self._dragging = False
                self._mover._hold = False
                self.setCursor(Qt.CursorShape.PointingHandCursor)
            else:
                self._on_click()

    def _on_click(self):
        """Left click without drag = open prompt."""
        if self._on_ask_callback:
            self._on_ask_callback()
        else:
            self._default_ask()

    def _show_context_menu(self, global_pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #1a1d22; color: #e8eaed; border: 1px solid #2a2d33;
                    border-radius: 6px; padding: 4px; font-size: 12px; }
            QMenu::item { padding: 6px 24px; border-radius: 4px; }
            QMenu::item:selected { background: #1f6feb; }
            QMenu::separator { height: 1px; background: #2a2d33; margin: 4px 8px; }
        """)

        ask = menu.addAction("Ask Addled...")
        ask.triggered.connect(self._on_click)

        sleep_action = menu.addAction("Sleep")
        sleep_action.triggered.connect(lambda: self.set_agent_state("sleeping"))

        wake_action = menu.addAction("Wake")
        wake_action.triggered.connect(lambda: self.set_agent_state("idle"))

        menu.addSeparator()

        settings_action = menu.addAction("Settings...")
        settings_action.triggered.connect(self._default_settings_dialog)

        menu.addSeparator()

        quit_action = menu.addAction("Quit Addled")
        quit_action.triggered.connect(QApplication.instance().quit)

        menu.exec(global_pos)

    def _default_ask(self):
        """Default left-click behavior: prompt and send through the wired
        callback (main.py), or show a placeholder if not wired."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        text, ok = QInputDialog.getText(
            self, "Ask Addled", "What would you like to know?",
        )
        if ok and text.strip():
            if self._on_ask_callback:
                self._on_ask_callback(text.strip())
            else:
                QMessageBox.information(
                    self, "Addled",
                    f"Addled is thinking about: {text[:100]}...\n\n"
                    "(Full chat available in the dashboard)"
                )

    def _default_settings_dialog(self):
        """Open Settings in the Addled GUI window (not an external browser tab)."""
        import pathlib
        import sys
        import webbrowser
        backend_dir = pathlib.Path(sys.argv[0]).resolve().parent
        packaged = backend_dir.parent.name == "resources"
        url = "http://127.0.0.1:3001" if packaged else "http://localhost:3000"

        # 1) tell the dashboard (Electron window) to navigate to /settings
        try:
            from backend.ws_server import get_server
            server = get_server()
            if server is not None:
                server.broadcast_nowait("ui.navigate", {"path": "/settings"})
        except Exception:
            pass

        # 2) bring the Addled window to the front
        try:
            import asyncio
            from backend.actions.window_manager import WindowManager

            async def _focus():
                return await WindowManager().focus("Addled")

            asyncio.run(_focus())
            return
        except Exception:
            pass

        # 3) fallback: open in the default browser
        try:
            webbrowser.open(url)
        except Exception:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self, "Settings",
                                    f"Open the dashboard at {url}")

    # ---- internal tick --------------------------------------------------------

    def _tick(self):
        state = self._state_machine.current
        dt = 0.033
        self._mover.update(state, dt)
        self._animator.update(state, dt)
        self._sync_position()

        # Particle system
        cx = self.width() / 2
        cy = self.height() / 2
        self._particles.update(state, dt, cx, cy)

        # Track cursor for eye gaze
        cursor_pos = QCursor.pos()
        widget_center = self.geometry().center()
        dx = (cursor_pos.x() - widget_center.x()) / 200
        dy = (cursor_pos.y() - widget_center.y()) / 200
        self._animator.look_at(dx, dy)

        self.update()

    def _sync_position(self):
        pos = self._mover.pos
        half = self.width() // 2
        new_pos = QPoint(int(pos.x() - half), int(pos.y() - half))
        if new_pos != self.pos():
            self.move(new_pos)

    # ---- paint -----------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            self._paint(painter)
        except Exception:
            # PyQt6 aborts the app on exceptions inside paintEvent — never
            # let a draw bug kill the whole backend.
            import logging
            logging.getLogger("addled.avatar").exception("Paint error")
        finally:
            painter.end()

    def _paint(self, painter: QPainter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        state = self._state_machine.current
        a = self._animator
        center_x = self.width() / 2
        center_y = self.height() / 2
        draw_size = self._size * a.pulse_scale

        # Apply shake offset for error state
        offset_x = a.shake_offset if state == CharacterState.ERROR else 0

        painter.save()

        # Glow effect
        if a.glow_intensity > 0.01:
            gradient = QRadialGradient(
                center_x + offset_x, center_y, self._size * 1.2
            )
            glow_color = QColor(self._color)
            glow_color.setAlpha(int(a.glow_intensity * 80))
            gradient.setColorAt(0, glow_color)
            glow_color.setAlpha(int(a.glow_intensity * 40))
            gradient.setColorAt(0.6, glow_color)
            gradient.setColorAt(1, QColor(0, 0, 0, 0))
            painter.setBrush(gradient)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(
                int(center_x - self._size * 1.2 + offset_x),
                int(center_y - self._size * 1.2),
                int(self._size * 2.4),
                int(self._size * 2.4),
            )

        # ---- sprite body (codex-pet skin) or procedural shape ----------------
        if self._movie is not None:
            self._draw_sprite(painter, center_x, center_y, draw_size,
                              offset_x, state, a)
        else:
            # Main shape
            shape_name = self._settings.get("shape", DEFAULT_SHAPE)
            draw_fn = SHAPE_REGISTRY.get(shape_name, SHAPE_REGISTRY[DEFAULT_SHAPE])

            # Color with mood tint
            color = QColor(self._color)
            if a.mood_warmth > 0.01:
                warm = QColor(0xFF, 0xD7, 0x00)
                color = self._blend_color(color, warm, a.mood_warmth)
            if a.mood_brightness > 0.01:
                color = color.lighter(int(100 + a.mood_brightness * 60))

            color.setAlpha(int(255 * a.opacity))

            painter.save()
            # Center the shape box on the widget center (widget = size + 40 glow
            # padding) and pulse-scale about the shape's own center.
            offset = (self.width() - self._size) / 2
            painter.translate(offset + offset_x, offset)
            painter.translate(self._size / 2, self._size / 2)
            painter.scale(a.pulse_scale, a.pulse_scale)
            painter.translate(-self._size / 2, -self._size / 2)

            draw_fn(painter, self._size, color)

            painter.restore()

            # Eyes
            if self._show_eyes and a.eye_state != "closed":
                self._draw_eyes(painter, center_x + offset_x, center_y, a)

        # Progress ring (THINKING or WORKING)
        if state in (CharacterState.THINKING, CharacterState.WORKING):
            self._draw_progress_ring(painter, center_x + offset_x, center_y, a)

        # Particles
        self._draw_particles(painter, center_x + offset_x, center_y, state)

        painter.restore()

    def _draw_sprite(self, painter: QPainter, cx: float, cy: float,
                     draw_size: float, offset_x: float, state: CharacterState,
                     a: Animator):
        """Draw the current GIF frame with state overlays.
        Keeps agent-state cues on top of the sprite: pulse, shake, tint,
        dim, progress ring and particles all still apply."""
        movie = self._movie
        if movie is None:
            return
        pixmap: QPixmap = movie.currentPixmap()
        if pixmap.isNull():
            return

        scale = (self._skin_meta or {}).get("scale", 1.0)
        side = draw_size * 1.4 * float(scale)  # sprite box (square)
        target = QRectF(cx + offset_x - side / 2, cy - side / 2, side, side)

        painter.save()
        painter.setOpacity(a.opacity)
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))

        # State overlays (tint/dim on top of the sprite body)
        if state == CharacterState.ERROR:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 40, 40, 45))
            painter.drawRoundedRect(target, 12, 12)
        elif state == CharacterState.BLOCKED:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 90))
            painter.drawRoundedRect(target, 12, 12)
        elif state == CharacterState.SLEEPING:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(20, 30, 60, 40))
            painter.drawRoundedRect(target, 12, 12)
        painter.restore()

    def _draw_eyes(self, painter: QPainter, cx: float, cy: float, a: Animator):
        eye_y = cy - self._size * 0.1
        eye_spacing = self._size * 0.15
        eye_radius = self._size * 0.08
        pupil_radius = self._size * 0.04

        for side in (-1, 1):
            ex = cx + side * eye_spacing

            # White of eye
            if a.eye_state in ("x",):
                continue
            painter.setBrush(QColor(255, 255, 255, int(200 * a.opacity)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(int(ex - eye_radius), int(eye_y - eye_radius),
                              int(eye_radius * 2), int(eye_radius * 2))

            # Pupil (tracks cursor)
            px = ex + a.look_x * eye_radius * 0.5
            py = eye_y + a.look_y * eye_radius * 0.5
            painter.setBrush(QColor(20, 20, 30, int(220 * a.opacity)))
            painter.drawEllipse(int(px - pupil_radius), int(py - pupil_radius),
                              int(pupil_radius * 2), int(pupil_radius * 2))

        # X-shaped eyes for error
        if a.eye_state == "x":
            painter.setPen(QPen(QColor(255, 60, 60, int(200 * a.opacity)), 2))
            for side in (-1, 1):
                ex = cx + side * eye_spacing
                r = eye_radius * 0.8
                painter.drawLine(int(ex - r), int(eye_y - r), int(ex + r), int(eye_y + r))
                painter.drawLine(int(ex + r), int(eye_y - r), int(ex - r), int(eye_y + r))

    def _draw_progress_ring(self, painter: QPainter, cx: float, cy: float, a: Animator):
        """Draw circular progress arc during THINKING/WORKING."""
        radius = self._size * 0.55
        pen = QPen(QColor(self._color.red(), self._color.green(), self._color.blue(), 180), 2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Draw arc from top (270°)
        span = int(a.progress_angle * 16)  # Qt uses 1/16th degree
        painter.drawArc(
            int(cx - radius), int(cy - radius),
            int(radius * 2), int(radius * 2),
            90 * 16, -span,
        )

    def _draw_particles(self, painter: QPainter, cx: float, cy: float, state: CharacterState):
        """Render active particles (zzz, sparkle, gear, glow_burst)."""
        for p in self._particles.get_particles():
            alpha = int(255 * p.life)
            if alpha <= 0:
                continue
            color = QColor(255, 255, 255, alpha)

            if p.kind == "zzz":
                painter.setPen(QPen(color, 1.5))
                # PyQt6 QFont is strict: pointSize must be an int
                painter.setFont(QFont("Segoe UI", max(6, int(p.size))))
                painter.drawText(int(p.x - 5), int(p.y + 5), "z")
            elif p.kind == "sparkle":
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 220, 100, alpha))
                r = p.size * p.life
                painter.drawEllipse(int(p.x - r), int(p.y - r), int(r * 2), int(r * 2))
            elif p.kind == "glow_burst":
                painter.setPen(Qt.PenStyle.NoPen)
                glow = QRadialGradient(p.x, p.y, p.size * (1 - p.life))
                c = QColor(100, 180, 255, alpha)
                glow.setColorAt(0, c)
                c.setAlpha(0)
                glow.setColorAt(1, c)
                painter.setBrush(glow)
                r = p.size * (1 - p.life)
                painter.drawEllipse(int(p.x - r), int(p.y - r), int(r * 2), int(r * 2))
            elif p.kind == "lightbulb":
                painter.setPen(QPen(QColor(255, 255, 150, alpha), 1))
                painter.setBrush(QColor(255, 255, 200, int(alpha * 0.3)))
                painter.drawEllipse(int(p.x - p.size), int(p.y - p.size),
                                    int(p.size * 2), int(p.size * 2))

        # Gear particles for WORKING state
        if state == CharacterState.WORKING:
            gear_angle = self._particles.gear_angle
            painter.save()
            painter.translate(cx, cy)
            for i in range(4):
                angle = gear_angle + i * math.pi / 2
                gx = math.cos(math.radians(angle)) * 20
                gy = math.sin(math.radians(angle)) * 20
                painter.setPen(QPen(QColor(255, 255, 255, 120), 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(int(gx - 4), int(gy - 4), 8, 8)
            painter.restore()

    @staticmethod
    def _parse_color(hex_str: str) -> QColor:
        c = QColor(hex_str)
        return c if c.isValid() else CURSOR_BLUE

    @staticmethod
    def _blend_color(c1: QColor, c2: QColor, t: float) -> QColor:
        t = max(0.0, min(1.0, t))
        return QColor(
            int(c1.red() + (c2.red() - c1.red()) * t),
            int(c1.green() + (c2.green() - c1.green()) * t),
            int(c1.blue() + (c2.blue() - c1.blue()) * t),
        )
