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
        "shape": "blob",
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
        # Hands default ON: they are part of the character's silhouette rather
        # than a decoration, and the poses are what carry the state for someone
        # who is not looking closely at two small eyes. The setting exists so
        # anyone who prefers the plain blob can have it back.
        self._show_hands = self._settings.get("hands", True)

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

    def set_emotion(self, emotion: str | None):
        """Set how the character FEELS, independent of what it is doing.

        Kept separate from `set_agent_state` on purpose: the state is driven by
        the app ("thinking", "speaking") and the emotion is not, so the caller
        can express excitement about a result, or boredom during a long wait,
        without inventing a state for it. Unknown names are ignored rather than
        raised on — this is reachable from settings and from the model.
        """
        self._animator.set_emotion(emotion)
        self.update()

    @property
    def emotion(self) -> str:
        return self._animator.emotion_name

    def point_at(self, x: float, y: float):
        """Move character near (x, y) on screen."""
        self._mover.set_target(x - 40, y - 40)

    def set_settings(self, settings: dict):
        self._settings = settings
        self._size = settings.get("size", 64)
        self.setFixedSize(self._size + 40, self._size + 40)
        self._color = self._parse_color(settings.get("color", "#3380FF"))
        self._show_eyes = settings.get("eyes", True)
        self._show_hands = settings.get("hands", True)
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
                server.set_nav_intent("/settings")
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

        # Glow effect. `render_glow` is the state's own figure combined with the
        # emotion's contribution; reading `glow_intensity` directly here would
        # drop the emotion's glow change while leaving its eye change in place.
        glow = a.render_glow
        if glow > 0.01:
            gradient = QRadialGradient(
                center_x + offset_x, center_y, self._size * 1.2
            )
            glow_color = QColor(self._color)
            glow_color.setAlpha(int(glow * 80))
            gradient.setColorAt(0, glow_color)
            glow_color.setAlpha(int(glow * 40))
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
        # Hands are procedural only. A sprite skin is a GIF the user chose and
        # already has whatever it has; drawing nubs onto artwork that does not
        # expect them would put two odd bumps on someone else's character.
        if self._movie is not None:
            self._draw_sprite(painter, center_x, center_y, draw_size,
                              offset_x, state, a)
        else:
            # Main shape
            shape_name = self._settings.get("shape", DEFAULT_SHAPE)
            draw_fn = SHAPE_REGISTRY.get(shape_name, SHAPE_REGISTRY[DEFAULT_SHAPE])

            # The body keeps the colour the user chose. The mood used to blend
            # it toward a warm yellow, which on the default blue passes straight
            # through GREEN — so the character appeared to change colour on its
            # own for no visible reason. That was removed on request. The mood
            # still drives the expression through the emotion layer, which is
            # the part a person can actually interpret as a mood.
            color = QColor(self._color)

            color.setAlpha(int(255 * a.opacity))

            # Hands that belong BEHIND the body. Drawn first so the body covers
            # the shoulder, which is what makes them read as attached rather
            # than as two blobs resting on the belly.
            if self._show_hands:
                self._draw_hands(painter, state, center_x + offset_x,
                                 center_y, a, behind=True)

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

            # Hands that belong IN FRONT of the body — the crossed-arms pose,
            # where an arm painted underneath the body would simply not exist.
            if self._show_hands:
                self._draw_hands(painter, state, center_x + offset_x,
                                 center_y, a, behind=False)

            # Eyes
            if self._show_eyes and a.eye_state != "closed":
                self._draw_eyes(painter, center_x + offset_x, center_y, a)

        # Progress ring (THINKING or WORKING)
        if state in (CharacterState.THINKING, CharacterState.WORKING):
            self._draw_progress_ring(painter, center_x + offset_x, center_y, a)

        # Particles
        self._draw_particles(painter, center_x + offset_x, center_y, state)

        painter.restore()

    def _draw_hands(self, painter: QPainter, state: CharacterState,
                    cx: float, cy: float, a: Animator, behind: bool) -> None:
        """Draw the hands that belong on this side of the body.

        Called twice per frame with opposite `behind` values: once before the
        body and once after. Only the hands whose pose asks for that side are
        drawn on each pass, so the normal case costs one call and one skip, and
        the crossed-arms case is the only one that draws both.
        """
        from backend.character.hands import draw_hands, pose_for

        if pose_for(state).behind != behind:
            return

        draw_hands(painter, state, cx, cy, self._size, self._color,
                   a.frame, opacity=a.opacity)

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

        # Emotion wash. A GIF's eyes are baked into the image, so there is no
        # eye geometry to reshape and `_draw_eyes` is never reached on this
        # path — without a sprite treatment every emotion is invisible to anyone
        # using a skin, which is the default on this machine.
        #
        # Applied as a wash over the upper part of the sprite, where a face
        # reads on nearly all character art. Deliberately weak: a sprite is
        # somebody else's artwork, and a strong wash stops looking like an
        # expression and starts looking like a broken image.
        if a.emotion_name != "neutral":
            self._draw_sprite_emotion(painter, target, a)
        painter.restore()

    def _draw_sprite_emotion(self, painter: QPainter, target: QRectF,
                             a: Animator):
        """Express an emotion on a sprite skin.

        Only two channels survive on a bitmap: a tinted wash, and how high the
        wash sits. Both are subtle on purpose — the sprite's own art has to
        remain recognisable, and the character should still look like the same
        fox whether it is pleased or puzzled.
        """
        tint = a.emotion_tint
        # How far the tint departs from neutral (1,1,1).
        strength = max(abs(1.0 - tint[0]), abs(1.0 - tint[1]),
                       abs(1.0 - tint[2]))
        # Whether the tint warms or cools decides the wash colour, so joy and
        # sadness are distinguishable and not merely "tinted".
        warmth = tint[0] - tint[2]

        # Some emotions carry no colour at all — confusion and surprise are
        # expressed entirely by eye shape, which a sprite does not have. Without
        # a fallback those would be invisible on a skin, so the shape signals
        # drive a neutral-grey wash instead. The strength comes from how far the
        # lid has moved from open, which is exactly what the sprite cannot show.
        if strength <= 0.01:
            shape = max(abs(a.emotion_eye_squint), abs(a.emotion_eye_open),
                        abs(a.emotion_eye_tilt) / 28.0,
                        abs(a.emotion_eye_offset) * 4.0)
            if shape <= 0.05:
                return
            strength = min(0.5, shape)

        # Squint and openness move the wash down or up, as the only available
        # stand-in for the eye shape a sprite does not have. A narrowed eye
        # lifts it; a wide one drops it.
        lift = (a.emotion_eye_squint - a.emotion_eye_open) * 0.06

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        if warmth > 0.02:
            wash = QColor(255, 208, 150, int(min(80, strength * 320)))
        elif warmth < -0.02:
            wash = QColor(140, 176, 255, int(min(80, strength * 320)))
        else:
            # A shape-only emotion: grey, so it does not invent a colour the
            # emotion never had, but still visibly registers.
            wash = QColor(240, 240, 250, int(min(60, strength * 220)))

        height = target.height() * (0.45 + lift)
        top = target.top() - target.height() * lift * 0.5
        face = QRectF(target.left(), top, target.width(), height)
        painter.setBrush(wash)
        painter.drawRoundedRect(face, target.width() * 0.4,
                                target.height() * 0.25)
        painter.restore()

    def _draw_eyes(self, painter: QPainter, cx: float, cy: float, a: Animator):
        """Draw the eyes as tall white capsules, as in Web-Eye-Animation.

        The reference library's eyes are its whole identity: two white capsules
        2.5x TALLER than they are wide, spaced widely, on a dark body. This
        draws the same proportions rather than the small round pupils used
        before, which read as dots on a shape rather than as a face.

        The geometry follows the reference's CSS directly:
            .eye  { width: 10vw; height: 25vh; border-radius: 50% }
            #leftEye { left: 20% }   #rightEye { right: 20% }
        which is a 1 : 2.5 width-to-height ratio with the centres 60% of the
        width apart. Those ratios are what the code below reproduces, scaled to
        whatever `self._size` the character is set to, because 10vw x 25vh is
        meaningless on a 64px floating widget.

        The emotion still shapes the geometry — openness, lid angle, vertical
        offset — and the STATE still owns the blink. A blink wins outright:
        a blink the emotion could hold open would read as the character
        ignoring you.
        """
        # Where the eyes sit on the body. The reference has no body, so there is
        # no offset to copy; these place the pair in the upper half, which is
        # where a face reads on a round shape.
        #
        # The MOTION is a displacement, never an absolute position: the
        # emotion's held pose decides where the eyes ARE, and the motion layer
        # says how far they have wandered from there this frame. Scaled by
        # `_size` so the amplitudes in `motion.py` mean the same thing at every
        # character size.
        eye_y = cy - self._size * 0.08
        eye_y += a.emotion_eye_offset * self._size * 0.08
        eye_y += a.motion_bounce * self._size
        eye_dx = a.motion_shake * self._size

        # Widely spaced, per the reference's 20% / 80% placement. On a compact
        # body the full 60% separation would push the eyes onto the silhouette
        # edge, so this is tightened to 46% — still clearly wide-set, still
        # reading as two separate eyes rather than one mask.
        eye_spacing = self._size * 0.23

        # The capsule. Half-width and half-height of the un-scaled eye: the
        # 1 : 2.5 ratio is the thing that makes these read as the reference's
        # eyes and not as dots, so it is expressed once here.
        base_rx = self._size * 0.055
        base_ry = base_rx * 2.5

        # Openness. `eye_open` widens and `eye_squint` narrows, and they are
        # separate fields because surprise and disgust pull in opposite
        # directions and one signed value cannot express both.
        #
        # The motion's own squint is added here rather than folded into
        # `emotion_eye_squint`: that field is what the blend walks toward a
        # target, and writing a moving value into it would leave the blend
        # chasing something that never settles.
        squint = a.emotion_eye_squint + a.motion_squint
        squint = max(0.0, min(1.0, squint))
        if a.eye_state == "closed":
            height_scale = 0.06
        else:
            height_scale = (1.0 + a.emotion_eye_open) * (1.0 - squint)
        height_scale = max(0.04, height_scale)
        # Widening moves the capsule's aspect toward square, which is what a
        # startled eye does; it is applied to width so the height stays stable.
        # The motion's pulse is a separate, smaller breath applied to both, so a
        # pulsing emotion grows rather than only widening.
        width_scale = (1.0 + a.emotion_eye_open * 0.35) * (1.0 + a.motion_pulse)
        height_scale = height_scale * (1.0 + a.motion_pulse)

        rx = base_rx * width_scale
        ry = base_ry * height_scale
        # A hard squint must not make the capsule thinner than a line, or it
        # vanishes and the eye reads as missing rather than as closed.
        ry = max(ry, base_rx * 0.18)

        # The tint multiplies the white rather than replacing it, so an emotion
        # can warm or cool the eye without the character's colour being
        # overwritten. The reference eyes are pure white on black, which is the
        # neutral case here.
        tint = getattr(a, "emotion_tint", (1.0, 1.0, 1.0))
        white = QColor(
            max(0, min(255, int(255 * tint[0]))),
            max(0, min(255, int(255 * tint[1]))),
            max(0, min(255, int(255 * tint[2]))),
            int(255 * a.opacity),
        )

        if a.eye_state == "x":
            self._draw_error_eyes(painter, cx, cy, eye_spacing, eye_y,
                                  base_rx, a)
            return

        for side in (-1, 1):
            # The shake displaces BOTH eyes the same way, so a tremble reads as
            # the whole face vibrating rather than as the two eyes crossing.
            ex = cx + side * eye_spacing + eye_dx
            painter.save()
            painter.translate(ex, eye_y)
            painter.setPen(Qt.PenStyle.NoPen)

            # The capsule itself: a fully rounded rectangle. Radius is
            # half the WIDTH, which rounds the short ends into a stadium
            # shape — that is what `border-radius: 50%` does on a box this
            # tall, and it is why the eyes are lozenge-shaped rather than
            # oval.
            painter.setBrush(white)
            x0 = -rx
            y0 = -ry
            w = rx * 2
            h = ry * 2
            radius = rx
            path = QPainterPath()
            path.addRoundedRect(QRectF(x0, y0, w, h), radius, radius)
            painter.drawPath(path)

            # The lid. A positive tilt drops the INNER corner — the inner
            # corner faces the other eye, which is right for the left eye and
            # left for the right, hence the `side` flip.
            if abs(a.emotion_eye_tilt) > 0.01 and self._lid_applies(a):
                self._draw_lid(painter, a, side, rx, ry)

            # The pupil tracks the cursor. The reference has no pupil at all —
            # just a plain white capsule — so this is an addition, and it is
            # kept small so it does not fight the flat, graphic look.
            #
            # It must still MOVE visibly. The first version sized the travel
            # from the room left beside the pupil, which on a capsule this
            # narrow came to barely a pixel, and because the ellipse was drawn
            # at integer coordinates the gaze was measured to be completely
            # frozen: identical to the sub-pixel across the whole -1..+1 range.
            # The travel is therefore taken as a fraction of the EYE, and the
            # pupil is small enough to have somewhere to go.
            pupil_radius = min(rx * 0.42, ry * 0.3) * a.emotion_pupil_scale
            # Travel is the eye's own half-width minus the pupil, but floored
            # so that even a narrow capsule gives the pupil real room to move.
            travel_x = max(rx * 0.45, rx - pupil_radius)
            travel_y = max(ry * 0.3, ry - pupil_radius)
            # The cursor gaze and the idle wander are summed here. Neither
            # overwrites the other: the cursor is the primary signal and the
            # drift keeps the eye alive when the cursor is still.
            look_x = max(-1.0, min(1.0, a.look_x + a.gaze_drift_x))
            look_y = max(-1.0, min(1.0, a.look_y + a.gaze_drift_y))
            px = look_x * travel_x
            py = look_y * travel_y
            painter.setBrush(QColor(18, 18, 26, int(235 * a.opacity)))
            painter.drawEllipse(QRectF(px - pupil_radius, py - pupil_radius,
                                       pupil_radius * 2, pupil_radius * 2))
            painter.restore()

    def _draw_lid(self, painter: QPainter, a: Animator, side: int,
                  rx: float, ry: float):
        """Cover part of the eye to angle its lid.

        The lid is a wedge filled in the character's own colour, laid over the
        top of the eye. Because it is a straight line crossing a small ellipse,
        its slant survives at sizes where rotating the ellipse does not: the
        lid moves by a fraction of the eye's own height rather than by the
        ellipse's perimeter.

        Two details that took measuring to get right. The wedge is anchored
        a little ABOVE the eye and its sloped edge is kept within the eye's own
        width, otherwise the slope falls outside the ellipse and the visible
        result is a flat bar — which is what the first version drew. And the
        covered height is bounded, so a strong tilt thins the eye without ever
        erasing it.

        `side` is -1 for the left eye and +1 for the right. When `mirrored` is
        false the two eyes take OPPOSITE slopes, which is what makes a wince or
        a puzzled look asymmetric rather than looking like a rendering fault.
        """
        tint = getattr(a, "emotion_tint", (1.0, 1.0, 1.0))
        lid_color = QColor(
            max(0, min(255, int(self._color.red() * tint[0]))),
            max(0, min(255, int(self._color.green() * tint[1]))),
            max(0, min(255, int(self._color.blue() * tint[2]))),
            int(235 * a.opacity),
        )
        # `inner` means the corner facing the other eye. The painter is
        # translated to the eye's own centre, so `+x` is screen-right for BOTH
        # eyes and the local frame never flips: "inner" is `+x` on the left eye
        # and `-x` on the right.
        #
        # There are two independent things to get right here, and applying
        # either one twice cancels the other out. `inner_dir` places the drop on
        # the correct CORNER of each eye. `side` decides whether the second eye
        # AGREES with the first (a symmetric scowl) or OPPOSES it (a confused
        # wince). Both must be applied exactly once. Doing the second by
        # multiplying `lean` in the same frame that `inner_dir` already
        # reflected produced two reflections, and the non-mirrored emotions came
        # out symmetric while the mirrored ones came out opposed.
        inner_dir = 1 if side < 0 else -1
        lean = max(-1.0, min(1.0, a.emotion_eye_tilt / 28.0))
        if not a.emotion_eye_tilt_mirrored:
            # Opposed: the second eye leans the other way. Checked as an eye
            # flip rather than folded into `lean`, so there is one reflection
            # and one sign, not two.
            if side > 0:
                lean = -lean

        # How much of the eye the lid covers, measured DOWN from the eye's own
        # top, as a fraction of the eye's half-height.
        #
        # Defined relative to the eye rather than as an absolute offset. An
        # earlier version anchored a wedge a fixed distance ABOVE the eye and
        # swung its corners by a large drop, which for the right eye produced a
        # thin sliver floating clear of the eye and, on a symmetric scowl, gave
        # the two eyes opposite slopes. Measured, not guessed.
        #
        # The base cover of 0.16*ry gives every lid-using emotion something to
        # see even at a small tilt, and the slanted part adds on top. Measured
        # at 96px with the earlier 0.10 base, a 20-degree scowl produced a
        # one-pixel difference between the corners — technically a slant, and
        # invisible in practice.
        cover = ry * 0.55 * abs(lean)
        base = ry * 0.16
        # The slant: the inner corner is covered more than the outer, which is
        # what makes a scowl a scowl. The sign of `lean` decides which corner
        # drops, so a positive tilt always drops the INNER corner.
        if lean >= 0:
            cover_in, cover_out = base + cover, base
        else:
            cover_in, cover_out = base, base + cover

        # The sloped edge runs from `y_in` to `y_out`, both measured down from
        # the eye's top (-ry). Everything ABOVE this line is covered.
        y_in = -ry + cover_in
        y_out = -ry + cover_out

        # The wedge is a rectangle above the eye, closed by the sloped edge.
        # Its top is far above the eye and its sides are wider than the eye;
        # both are trimmed by the clip below, so nothing it draws can land
        # outside the capsule. That is the property the previous version
        # lacked, and the reason it left fragments on screen.
        wide = rx * 2.0
        lid = QPainterPath()
        lid.moveTo(-wide, -ry * 2.0)
        lid.lineTo(wide, -ry * 2.0)
        lid.lineTo(inner_dir * wide, y_in)
        lid.lineTo(-inner_dir * wide, y_out)
        lid.closeSubpath()

        # Trim to the eye's own capsule. This is what makes it a lid: the fill
        # can only ever be eye area, so no part of it can spill onto the body
        # or the desktop behind.
        capsule = QPainterPath()
        capsule.addRoundedRect(QRectF(-rx, -ry, rx * 2, ry * 2), rx, rx)
        lid = lid.intersected(capsule)

        painter.setBrush(lid_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(lid)

    def _lid_applies(self, a: Animator) -> bool:
        """Whether a lid makes sense for the current eye state.

        A closed eye is already fully covered, so a lid on top of it is wasted
        work; every other state wants one.
        """
        return a.eye_state != "closed"

    def _draw_error_eyes(self, painter: QPainter, cx: float, cy: float,
                         eye_spacing: float, eye_y: float, eye_radius: float,
                         a: Animator):
        """X-shaped eyes for the ERROR state, tinted by the emotion."""
        tint = getattr(a, "emotion_tint", (1.0, 1.0, 1.0))
        painter.setPen(QPen(QColor(
            max(0, min(255, int(255 * tint[0]))),
            60,
            max(0, min(255, int(60 * tint[2]))),
            int(200 * a.opacity)), 2))
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
