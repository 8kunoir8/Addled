"""Verify the bubble's decision state machine, headlessly.

Two audits found six ways a decision could vanish, get stuck, or leave stale
state — and the second round found that the FIRST round's fixes had introduced
two more (an immortal prompt, and messages silently dropped). Every case below
is one of those, driven against the real widget rather than a mock, because the
bugs were all in the interaction between methods rather than in any one of them.

Qt needs a widget, so this runs with an offscreen QApplication. It imports the
project from the repo it lives in, so it exercises the DEV tree like every other
suite — being the only check that needs a display is not a reason to point it
somewhere else.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_bubble_state.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# The bubble's close button is a non-ASCII glyph, and a Windows console defaults
# to cp1252 — without this the run crashed while printing the reason it failed,
# which is the worst possible time to lose the output.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import QPoint  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.chat_bubble import ChatBubble  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    anchor = QPoint(400, 400)

    print("=== a decision survives an ordinary message (audit F4) ===")
    b = ChatBubble(agent_name="T")
    fired = []
    b.show_decision("Permission", "Allow it?", [
        {"label": "Allow", "callback": lambda: fired.append("allow")},
        {"label": "Deny", "callback": lambda: fired.append("deny"), "danger": True},
    ], anchor)
    check("the decision is open", b._decision_open is True)
    # Two answers plus the `closable` "Not now" route, which is offered by
    # default because a decision can always be left for the dashboard.
    check("it has its buttons plus the way out", len(b._action_buttons) == 3,
          str([x.text() for x in b._action_buttons]))
    # A proactive insight arrives mid-prompt.
    b.show_message("By the way, your build finished.", anchor)
    check("the decision is STILL open", b._decision_open is True,
          "an ordinary message must not destroy a pending prompt")
    check("its buttons are still there", len(b._action_buttons) == 3)
    check("the decision text is unchanged",
          "Allow it?" in b._label.text(), repr(b._label.text())[:60])

    print("\n=== the buttons still work after that message ===")
    b._on_action(lambda: fired.append("allow"))
    check("the callback ran", fired == ["allow"], str(fired))
    check("the decision closed", b._decision_open is False)
    check("the buttons went", len(b._action_buttons) == 0)

    print("\n=== the close button cannot escape a decision (audit F5) ===")
    b2 = ChatBubble(agent_name="T")
    b2.show_decision("Permission", "Allow it?", [
        {"label": "Allow", "callback": lambda: None},
    ], anchor)
    b2.fade_out()
    check("fade_out is refused while a decision is open",
          b2._decision_open is True,
          "a silent way out of a permission prompt, leaving live buttons")
    # The answer plus the way out; neither may be destroyed by the refusal.
    check("the buttons were not orphaned", len(b2._action_buttons) == 2,
          str([x.text() for x in b2._action_buttons]))

    print("\n=== a thinking bubble does not destroy a decision (audit r2 F2) ===")
    # Round 1 said "thinking must clear a decision" because a grey bubble still
    # holding live buttons was a contradiction. Round 2 pointed out that
    # clearing DESTROYS the prompt — the callback is dropped and the action
    # stays queued. Both are true, so the resolution is that the thinking bubble
    # is what yields: a decision is what needs attention, and `show_thinking`
    # leaves it alone rather than replacing it.
    b2.show_thinking(anchor)
    check("the decision survives a thinking request", b2._decision_open is True,
          "clearing it dropped Allow/Deny with nothing sent")
    check("its buttons survive too", len(b2._action_buttons) == 2,
          str([x.text() for x in b2._action_buttons]))
    check("and it did not become a thinking bubble",
          b2._thinking is False,
          "a grey bubble with live buttons was the state round 1 objected to")
    # Once the decision is answered, thinking works normally again.
    b2._on_action(lambda: None)
    b2.show_thinking(anchor)
    check("thinking works once no decision is open", b2._thinking is True)
    check("no buttons survive into the thinking bubble",
          len(b2._action_buttons) == 0)

    print("\n=== a second decision is queued, not dropped (audit F6) ===")
    b3 = ChatBubble(agent_name="T")
    ran = []
    b3.show_decision("First", "Approve A?", [
        {"label": "A", "callback": lambda: ran.append("A")},
    ], anchor)
    b3.show_decision("Second", "Approve B?", [
        {"label": "B", "callback": lambda: ran.append("B")},
    ], anchor)
    check("the first is still the one shown", "Approve A?" in b3._label.text(),
          repr(b3._label.text())[:50])
    check("the second is queued", len(b3._queued) == 1, str(len(b3._queued)))
    b3._on_action(lambda: ran.append("A"))
    check("answering shows the queued one",
          b3._decision_open is True and "Approve B?" in b3._label.text(),
          f"open={b3._decision_open} label={b3._label.text()[:40]!r}")
    check("and it has its own button", len(b3._action_buttons) == 2,
          str([x.text() for x in b3._action_buttons]))

    print("\n=== the queue drains in order, each answer once ===")
    ran.clear()
    b3._on_action(lambda: ran.append("B"))
    check("the second answer ran", ran == ["B"], str(ran))
    check("no decision is open once the queue is empty",
          b3._decision_open is False)
    check("nothing is queued", len(b3._queued) == 0)
    check("the first answer was not re-run", "A" not in ran,
          "advancing the queue must not replay the decision that just finished")

    print("\n=== an open-ended decision offers a way out, not nothing ===")
    b4 = ChatBubble(agent_name="T")
    b4.show_decision("Question", "What should I name it?", [], anchor,
                     closable=True)
    check("an open-ended question still has a control",
          len(b4._action_buttons) == 1,
          "a card with no buttons and no close is a dead end")
    check("the control is 'Not now'",
          "Not now" in b4._action_buttons[0].text())

    print("\n=== a message after the decision works normally again ===")
    b4._on_later()
    check("the decision is closed", b4._decision_open is False)
    b4.show_message("All done.", anchor)
    check("an ordinary message displays once no decision is open",
          b4._label.text() == "All done.", repr(b4._label.text()))

    print("\n=== an unanswered decision is not immortal (audit round 2) ===")
    b5 = ChatBubble(agent_name="T")
    b5.show_decision("Permission", "Allow it?", [
        {"label": "Allow", "callback": lambda: None},
    ], anchor, closable=False)  # no "Not now" — the destructive-prompt shape
    check("the decision armed its timeout",
          b5._timer.isActive(),
          "duration_s=None left no timer, and fade_out and the click both "
          "refused, so an unanswered prompt had NO exit at all")
    # Firing the timeout must release the bubble, not be silently refused.
    b5._timeout()
    check("the timeout releases the decision", b5._decision_open is False,
          "the deadline was decorative if fade_out refused it")
    check("its buttons are gone", len(b5._action_buttons) == 0)

    print("\n=== the close button is not dead on a decision ===")
    b6 = ChatBubble(agent_name="T")
    b6.show_decision("Permission", "Allow it?", [
        {"label": "Allow", "callback": lambda: None},
    ], anchor)
    b6._on_close()
    check("✕ does not close the decision", b6._decision_open is True)
    check("but it changes the title, so the click has an effect",
          b6._title.text() != "Permission",
          "a button that looks pressable and does nothing reads as broken")

    print("\n=== a message during a decision is queued, not dropped (F5) ===")
    b7 = ChatBubble(agent_name="T")
    b7.show_decision("Permission", "Allow it?", [
        {"label": "Allow", "callback": lambda: None},
    ], anchor)
    b7.show_message("Your build finished.", anchor)
    check("the message was kept", len(b7._queued_messages) == 1,
          "it used to be discarded outright — no bubble, no queue")
    b7._on_action(lambda: None)
    check("it is shown once the decision clears",
          b7._label.text() == "Your build finished.",
          repr(b7._label.text())[:50])

    print("\n=== the queues are bounded ===")
    b8 = ChatBubble(agent_name="T")
    b8.show_decision("D0", "first", [{"label": "x", "callback": lambda: None}],
                     anchor)
    for i in range(10):
        b8.show_decision(f"D{i}", "more", [], anchor)
        b8.show_message(f"m{i}", anchor)
    check("queued decisions are capped", len(b8._queued) <= b8.MAX_QUEUED_DECISIONS,
          str(len(b8._queued)))
    check("queued messages are capped",
          len(b8._queued_messages) <= b8.MAX_QUEUED_MESSAGES,
          str(len(b8._queued_messages)))

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the bubble's decision state machine behaves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
