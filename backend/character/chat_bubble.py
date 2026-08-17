"""
Chat-bubble reply popup for the floating character.

Frameless, translucent, always-on-top speech bubble with a tail pointing
at the character. Used when the user asks the character something via
left click. Auto-dismisses, fades out, and supports click-to-close.
"""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF
from PyQt6.QtGui import QPainter, QColor, QPen, QPainterPath, QPolygonF, QFont

log = logging.getLogger("addled.bubble")

TAIL_H = 14          # tail height in px (reserved at the bottom)
MAX_LABEL_WIDTH = 360
MAX_TEXT = 1200


class ChatBubble(QWidget):
    """Speech bubble anchored above the floating character."""

    def __init__(self, parent=None, agent_name: str = "Fox"):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

        self._anchor = QPoint(0, 0)
        self._tail_x = 40
        self._thinking = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, TAIL_H + 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel(agent_name)
        self._title.setStyleSheet(
            "color:#3380FF; font-weight:600; font-size:11px; background:transparent;")
        header.addWidget(self._title)
        header.addStretch()
        self._close = QPushButton("✕")
        self._close.setFixedSize(18, 18)
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.setStyleSheet(
            "QPushButton { color:#8b949e; background:transparent; border:none;"
            " font-size:11px; border-radius:9px; }"
            "QPushButton:hover { color:#f85149; background:#21262d; }")
        self._close.clicked.connect(self.fade_out)
        header.addWidget(self._close)
        layout.addLayout(header)

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setFixedWidth(MAX_LABEL_WIDTH)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setStyleSheet(
            "color:#e8eaed; font-size:13px; background:transparent;")
        layout.addWidget(self._label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.fade_out)

        # Platform-independent fade: paint-time alpha driven by a timer
        # (windowOpacity animation is not supported by every platform).
        self._opacity = 1.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._fade_step)

    # ---- public API ----------------------------------------------------------

    def show_thinking(self, anchor: QPoint):
        """Show a 'thinking' bubble while the LLM processes the question."""
        self._thinking = True
        self._label.setText("Thinking…")
        self._label.setStyleSheet(
            "color:#8b949e; font-style:italic; font-size:13px; background:transparent;")
        self._show(anchor, duration_s=None)

    def show_message(self, text: str, anchor: QPoint):
        """Show the final reply as a chat bubble."""
        self._thinking = False
        self._label.setText((text or "").strip()[:MAX_TEXT])
        self._label.setStyleSheet(
            "color:#e8eaed; font-size:13px; background:transparent;")
        # Reading time: at least 8s, +1s per ~80 chars, capped at 30s
        duration = max(8, min(30, 6 + len(self._label.text()) // 80))
        self._show(anchor, duration_s=duration)

    def fade_out(self):
        """Fade the bubble away."""
        if not self.isVisible():
            return
        self._timer.stop()
        self._fade_timer.start()

    def _fade_step(self):
        self._opacity = max(0.0, self._opacity - 0.09)
        self.update()
        if self._opacity <= 0.0:
            self._fade_timer.stop()
            self.hide()
            self._opacity = 1.0

    def _show(self, anchor: QPoint, duration_s: int | None):
        self._anchor = anchor
        self.adjustSize()
        self._place(anchor)
        self._fade_timer.stop()
        self._timer.stop()
        self._opacity = 1.0
        if duration_s is not None:
            self._timer.start(int(duration_s * 1000))
        self.show()
        self.raise_()

    def _place(self, anchor: QPoint):
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        sg = screen.geometry()
        w, h = self.width(), self.height()
        x = int(anchor.x() - w / 2)
        y = int(anchor.y() - h - 16)
        x = max(sg.left() + 8, min(x, sg.left() + sg.width() - w - 8))
        y = max(sg.top() + 8, min(y, sg.top() + sg.height() - h - 8))
        self.move(x, y)
        # Tail points toward the character (clamped inside the bubble body)
        self._tail_x = int(min(max(anchor.x() - x, 30), w - 30))
        self.update()

    # ---- interaction + paint -------------------------------------------------

    def mousePressEvent(self, event):
        self.fade_out()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        body = QRectF(1, 1, w - 2, h - TAIL_H - 1)

        path = QPainterPath()
        path.addRoundedRect(body, 14, 14)
        tail = QPolygonF([
            QPointF(self._tail_x - 9, body.bottom() + 1),
            QPointF(self._tail_x + 9, body.bottom() + 1),
            QPointF(self._tail_x, body.bottom() + TAIL_H - 1),
        ])
        path.addPolygon(tail)

        # Bubble body + border in dashboard dark theme
        if self._thinking:
            fill = QColor(22, 27, 34, 220)
            border = QColor(48, 54, 61, 220)
        else:
            fill = QColor(22, 27, 34, 248)
            border = QColor(51, 128, 255, 130)
        fill.setAlpha(int(fill.alpha() * self._opacity))
        border.setAlpha(int(border.alpha() * self._opacity))
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawPath(path)
        painter.end()
