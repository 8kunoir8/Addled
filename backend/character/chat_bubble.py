"""
Chat-bubble reply popup for the floating character.

Frameless, translucent, always-on-top speech bubble with a tail pointing
at the character. Used when the user asks the character something via
left click. Auto-dismisses, fades out, and supports click-to-close.

It also carries *decisions*. A permission request or a question used to reach
the character as plain text — the bubble said "permission needed" and the user
had to go and find the dashboard to answer it, which is a prompt with no answer
attached to it. `show_decision` adds a row of buttons and suppresses the
auto-dismiss, because a prompt that answers itself by fading away is worse than
no prompt: the work it was blocking stays blocked and the user never saw why.

Clicking anywhere else on the bubble still dismisses it. That is deliberate for
a message, and wrong for a decision, so the decision path passes
`closable=True` only where closing without answering is a legitimate outcome
(Skip / Not now). A permission request with a destructive action behind it
offers Allow and Deny and no silent way out.
"""

from __future__ import annotations

import logging
from typing import Callable

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
        self._close.clicked.connect(self._on_close)
        header.addWidget(self._close)
        layout.addLayout(header)

        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setFixedWidth(MAX_LABEL_WIDTH)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setStyleSheet(
            "color:#e8eaed; font-size:13px; background:transparent;")
        layout.addWidget(self._label)

        # The answer row. Empty and hidden for an ordinary message, filled by
        # `show_decision`. Built once and reused so a bubble that has already
        # carried a decision does not accumulate dead buttons.
        self._actions = QHBoxLayout()
        self._actions.setSpacing(6)
        self._actions.setContentsMargins(0, 2, 0, 0)
        layout.addLayout(self._actions)
        self._action_buttons: list[QPushButton] = []
        # A decision is not dismissed by clicking the bubble body; only the
        # buttons, or the explicit close, end it.
        self._decision_open = False
        # The record behind the decision on screen — a question's id, source
        # and conversation — so the character prompt can ANSWER it rather than
        # only display it. Cleared with the buttons.
        self._open_decision: dict = {}
        # Decisions raised while one was already open, oldest first. Small and
        # short-lived by construction: only one turn at a time can raise them,
        # and each is shown as soon as the one before it is answered.
        self._queued: list[tuple] = []
        # Ordinary messages that arrived while a decision was on screen, so they
        # are shown afterwards instead of being dropped. Capped: a bubble is a
        # glance, not a log, and holding the last few is all that is useful.
        self._queued_messages: list[tuple] = []

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        # `_timeout`, not `fade_out`: a decision refuses user dismissal but must
        # still honour its own deadline, and one slot cannot do both.
        self._timer.timeout.connect(self._timeout)

        # Platform-independent fade: paint-time alpha driven by a timer
        # (windowOpacity animation is not supported by every platform).
        self._opacity = 1.0
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._fade_step)

    # ---- public API ----------------------------------------------------------

    def show_thinking(self, anchor: QPoint):
        """Show a 'thinking' bubble while the LLM processes the question.

        An open decision is left alone. It used to be cleared here, which
        destroyed a permission prompt with nothing sent — the user left-clicked
        the character while an approval was on screen and Allow/Deny vanished,
        the action staying queued with nothing to say why. That also stranded
        anything in `_queued`, so the module ended up with two contradictory
        policies for the same state. A decision waits; the thinking bubble is
        what can wait instead.
        """
        if self._decision_open:
            log.debug("a decision is open; not replacing it with thinking")
            return
        self._thinking = True
        self._label.setText("Thinking…")
        self._label.setStyleSheet(
            "color:#8b949e; font-style:italic; font-size:13px; background:transparent;")
        self._show(anchor, duration_s=None)

    def show_message(self, text: str, anchor: QPoint):
        """Show the final reply as a chat bubble.

        An open decision is not replaced — and the message is not lost either.
        It used to be dropped outright: the method returned early and stored
        nothing, so an insight or a reply arriving while a prompt was on screen
        disappeared for good. It is kept and shown when the decision clears,
        which is what "an ordinary message waits its turn" has to mean to be
        true.
        """
        if self._decision_open:
            self._queued_messages.append((str(text or ""), anchor))
            del self._queued_messages[:-self.MAX_QUEUED_MESSAGES]
            log.debug("a decision is open; queued a message until it clears")
            return
        self._thinking = False
        self._label.setText((text or "").strip()[:MAX_TEXT])
        self._label.setStyleSheet(
            "color:#e8eaed; font-size:13px; background:transparent;")
        # Reading time: at least 8s, +1s per ~80 chars, capped at 30s
        duration = max(8, min(30, 6 + len(self._label.text()) // 80))
        self._show(anchor, duration_s=duration)

    def show_decision(self, title: str, text: str, buttons: list[dict],
                      anchor: QPoint, *, closable: bool = True,
                      payload: dict | None = None) -> None:
        """Show a prompt the user can answer without leaving the bubble.

        ``buttons`` is a list of ``{"label", "callback", "danger"?}``. The
        bubble does **not** auto-dismiss while a decision is open: fading away
        would leave the work behind it blocked with no visible reason, which is
        exactly the failure this replaces.

        ``closable`` adds a "later" route out. A genuine decision can be left
        for the dashboard, so the default allows it; a caller that needs an
        answer can pass False.

        ``payload`` is the record this was built from — for a question, its id,
        source and conversation. Kept so `open_question()` can hand the
        character prompt enough to ANSWER it rather than only display it.

        **A decision already open is not replaced.** One turn can raise both an
        approval and a question — the approval during the tool loop, the
        question on the way out — and both arrive here. Clearing the first
        silently dropped a permission prompt with nothing sent, which is the
        exact failure this surface exists to prevent. The newcomer waits in
        `_queued` and is shown when the open one is answered.
        """
        if self._decision_open:
            self._queued.append((title, text, list(buttons), dict(
                closable=closable), dict(payload or {})))
            # Bounded. "Only one turn at a time raises these" is an assumption
            # about the caller, not a property of this class — and an unbounded
            # list that only a click can drain is a leak, however slow.
            del self._queued[:-self.MAX_QUEUED_DECISIONS]
            log.debug("a decision is open; queued %r until it is answered",
                      title)
            return
        self._show_decision_now(title, text, buttons, anchor, closable, payload)

    # How long an unanswered decision stays on screen before it gives up.
    #
    # A decision must NOT be immortal. Refusing to auto-dismiss AND refusing the
    # ✕ left no exit at all when the user did not press a button, so the bubble
    # sat on top of the character for the rest of the session — worse than the
    # behaviour it replaced, which at least faded. The deadline is much longer
    # than a message's (a prompt is meant to wait) and the answer is still
    # answerable from the dashboard afterwards, because the backend's own TTL is
    # what actually owns the question.
    DECISION_TIMEOUT_S = 120

    # Bounds on what can pile up behind an unanswered decision. Small on
    # purpose: a bubble is a glance, and anything older than the last few is
    # already visible on the dashboard.
    MAX_QUEUED_DECISIONS = 3
    MAX_QUEUED_MESSAGES = 3

    def _show_decision_now(self, title: str, text: str, buttons: list[dict],
                           anchor: QPoint, closable: bool,
                           payload: dict | None = None) -> None:
        """Draw a decision. The caller has already checked none is open.

        ``payload`` is the original record — the question id, its source and its
        conversation — kept so the character prompt can answer this exact
        question. The display fields alone are not enough: an answer has to name
        the conversation it belongs to, and `text` is only the wording.
        """
        self._clear_actions()
        self._thinking = False
        self._decision_open = True
        self._open_decision = dict(payload or {})
        self._title.setText(title)
        self._title.setStyleSheet(
            "color:#d29922; font-weight:600; font-size:11px; background:transparent;")
        self._label.setText((text or "").strip()[:MAX_TEXT])
        self._label.setStyleSheet(
            "color:#e8eaed; font-size:13px; background:transparent;")

        for spec in buttons[:4]:  # four is the most that stays readable
            button = QPushButton(str(spec.get("label") or "OK"))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            danger = bool(spec.get("danger"))
            button.setStyleSheet(
                "QPushButton { color:%s; background:#21262d; border:1px solid %s;"
                " border-radius:6px; padding:4px 10px; font-size:11px; }"
                "QPushButton:hover { background:#30363d; }"
                % ("#f85149" if danger else "#e8eaed",
                   "#5a2a2a" if danger else "#30363d"))
            # Default-arg binding: without it every button closes over the last
            # callback, so pressing "Deny" would send the last verb.
            callback = spec.get("callback")
            if callable(callback):
                button.clicked.connect(
                    lambda _checked=False, fn=callback: self._on_action(fn))
            self._actions.addWidget(button)
            self._action_buttons.append(button)

        if closable:
            later = QPushButton("Not now")
            later.setCursor(Qt.CursorShape.PointingHandCursor)
            later.setStyleSheet(
                "QPushButton { color:#8b949e; background:transparent;"
                " border:1px solid #30363d; border-radius:6px;"
                " padding:4px 10px; font-size:11px; }"
                "QPushButton:hover { color:#e8eaed; background:#21262d; }")
            later.clicked.connect(self._on_later)
            self._actions.addWidget(later)
            self._action_buttons.append(later)

        # It waits for an answer — but not forever.
        #
        # `duration_s=None` armed no timer at all, and with `fade_out` and the
        # click both refusing while a decision is open, an unanswered prompt had
        # NO exit: the bubble stayed on top of the character for the rest of the
        # session. The long-but-finite deadline is what keeps "do not dismiss
        # this out from under the user" from becoming "the user cannot get rid
        # of it". Two minutes is long enough to read and decide, and the
        # question is still answerable from the dashboard afterwards.
        self._show(anchor, duration_s=self.DECISION_TIMEOUT_S)

    def _on_action(self, callback: Callable[[], None]) -> None:
        """Run an answer, then get out of the way."""
        try:
            callback()
        except Exception as e:  # noqa: BLE001
            log.warning("bubble action failed: %s", e)
        self._clear_actions()
        self._advance()

    def _on_later(self) -> None:
        self._clear_actions()
        self._advance()

    def _advance(self) -> None:
        """Show the next queued decision, then a queued message, or fade.

        Called wherever a decision ends, so nothing queued behind it can be
        stranded by the path that finished the last one.
        """
        if self._queued:
            title, text, buttons, opts, payload = self._queued.pop(0)
            self._show_decision_now(title, text, buttons, self._anchor,
                                    bool(opts.get("closable", True)), payload)
            return
        if self._queued_messages:
            text, anchor = self._queued_messages.pop(0)
            self.show_message(text, anchor)
            return
        self.fade_out()

    def _clear_actions(self) -> None:
        """Remove the answer row and forget what it was about.

        Safe on a bubble that has none. `_open_decision` goes with the buttons:
        a stale payload would let the character prompt offer to answer a
        question that is no longer on screen.
        """
        self._decision_open = False
        self._open_decision = {}
        for button in self._action_buttons:
            try:
                self._actions.removeWidget(button)
                button.setParent(None)
                button.deleteLater()
            except Exception as e:  # noqa: BLE001
                log.debug("could not remove a bubble button: %s", e)
        self._action_buttons = []

    def open_question(self) -> dict | None:
        """The QUESTION currently on screen, for the character prompt to answer.

        None when no decision is open, or when the one that is open is a
        permission prompt. That distinction matters: answering a permission
        request with free text is not a thing the backend accepts, so a typed
        message while an approval is up must be handled as an ordinary request
        rather than silently swallowed as an "answer".
        """
        if not self._decision_open or not self._open_decision:
            return None
        if not str(self._title.text()).startswith("❓"):
            return None
        return dict(self._open_decision)

    def fade_out(self):
        """Fade the bubble away.

        A decision is not dismissed *by the user* while it is on screen: neither
        a body click nor the ✕ may drop a permission prompt with nothing sent,
        because the action behind it stays queued and nothing says why it went.
        It ends through its buttons, or by timing out — `_timeout` handles that
        case, and this method is what an ordinary message's timer calls.
        """
        if self._decision_open:
            log.debug("a decision is open; not dismissing it")
            return
        if not self.isVisible():
            return
        self._timer.stop()
        self._fade_timer.start()

    def _on_close(self):
        """The ✕ was pressed.

        It does not close a decision, but it must not be a dead control either:
        a button that looks pressable and does nothing reads as a broken app.
        The title says what to do instead, so the click has a visible effect and
        the user learns the way out.
        """
        if self._decision_open:
            self._title.setText("Answer above, or it will expire shortly")
            log.debug("✕ pressed on a decision; pointing at the buttons")
            return
        self.fade_out()

    def _timeout(self):
        """The auto-dismiss timer fired.

        Split from `fade_out` because a decision is exempt from user dismissal
        but *not* from its own deadline. Without this the decision armed a timer
        that refused to act, so the deadline was decorative.
        """
        if self._decision_open:
            log.info("a decision went unanswered on the character; moving on")
            self._clear_actions()
            self._advance()
            return
        self.fade_out()

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
        # A click anywhere dismisses an ordinary message, which is the point of
        # a bubble. While a decision is open it must not: the prompt would
        # disappear, the work behind it would stay blocked, and nothing would
        # say why. The buttons are the way out.
        if self._decision_open:
            event.ignore()
            return
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

        # Bubble body + border in dashboard dark theme.
        #
        # A decision gets an amber border, the same cue the dashboard's card
        # uses. It is the one thing that says "this is waiting on you" from
        # across the screen, which is what a bubble the user is not looking at
        # needs to do — the point of putting the prompt here at all.
        if self._thinking:
            fill = QColor(22, 27, 34, 220)
            border = QColor(48, 54, 61, 220)
        elif self._decision_open:
            fill = QColor(22, 27, 34, 250)
            border = QColor(210, 153, 34, 210)
        else:
            fill = QColor(22, 27, 34, 248)
            border = QColor(51, 128, 255, 130)
        fill.setAlpha(int(fill.alpha() * self._opacity))
        border.setAlpha(int(border.alpha() * self._opacity))
        painter.setPen(QPen(border, 1.2))
        painter.setBrush(fill)
        painter.drawPath(path)
        painter.end()
