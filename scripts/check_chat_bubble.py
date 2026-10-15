"""Verify that no bubble can stay on screen forever.

The character's bubble is the only surface that appears without the user
asking, so a bubble that never leaves is worse than one that never appeared:
it sits on top of the character for the rest of the session and the only way
out is a click the user has no reason to try.

That is not hypothetical. Two bubbles were shown with `duration_s=None`, which
arms no timer at all:

  * a **decision**, fixed when the comment in `_show_decision_now` was written;
  * **"Thinking…"**, which was missed — and a turn that raised never sent a
    reply to replace it, so the text stayed until the app was restarted.

The rule these checks pin down is: every bubble that shows is either dismissed
by its own deadline, or has a path that replaces it. "No exit" is the bug.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_chat_bubble.py
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtCore import QPoint, QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.chat_bubble import ChatBubble  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def pump_until(predicate, timeout_s=6.0, step_ms=20):
    """Run the Qt loop until `predicate()` or the timeout. Returns whether."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(step_ms / 1000.0)
    return predicate()


ANCHOR = QPoint(500, 500)


print("Thinking is armed with a deadline, so a silent turn cannot strand it")
b = ChatBubble(agent_name="Fox")
b.show_thinking(ANCHOR)
check("the bubble is shown", b.isVisible())
check("it arms the auto-dismiss timer",
      b._timer.isActive(),
      "no timer means a turn that never replies leaves it up forever")
check("the deadline is a real, finite number of seconds",
      isinstance(b.THINKING_TIMEOUT_S, int) and b.THINKING_TIMEOUT_S > 0,
      f"THINKING_TIMEOUT_S={getattr(b, 'THINKING_TIMEOUT_S', None)!r}")
check("and it is longer than a message's, because a model is slow",
      b.THINKING_TIMEOUT_S >= 60, f"{b.THINKING_TIMEOUT_S}s")
check("the timer was armed with exactly that deadline",
      b._timer.interval() == b.THINKING_TIMEOUT_S * 1000,
      f"interval={b._timer.interval()} deadline={b.THINKING_TIMEOUT_S * 1000}")
check("it is not a decision, so nothing else refuses to dismiss it",
      b._decision_open is False)
b.hide()

print()
print("The deadline actually takes it off the screen")
# The real wait is minutes; the deadline is shortened here so the check runs in
# seconds. What is under test is the mechanism, not the number.
b2 = ChatBubble(agent_name="Fox")
b2.show_thinking(ANCHOR)
b2._timer.stop()
b2._timer.start(150)
app.processEvents()
check("it is on screen first", b2.isVisible())
check("fired -> it fades and hides",
      pump_until(lambda: not b2.isVisible()),
      f"still visible after 6s (fade_active={b2._fade_timer.isActive()})")
check("and the thinking flag is cleared, so the next bubble draws normally",
      b2._thinking is False)
check("opacity is restored for the next show", b2._opacity == 1.0)

print()
print("A click still dismisses it, for a user who thinks to try")
b3 = ChatBubble(agent_name="Fox")
b3.show_thinking(ANCHOR)
b3.fade_out()
check("fade_out on a thinking bubble takes effect",
      pump_until(lambda: not b3.isVisible()))

print()
print("Messages and decisions keep their own deadlines")
b4 = ChatBubble(agent_name="Fox")
b4.show_message("a short reply", ANCHOR)
check("a message arms a timer", b4._timer.isActive())
check("its deadline is between 8 and 30 seconds",
      8000 <= b4._timer.interval() <= 30000,
      f"{b4._timer.interval()}ms")
b4.hide()

b5 = ChatBubble(agent_name="Fox")
b5.show_decision("Allow?", "run a command?", [{"label": "Allow",
                                               "callback": lambda: None}],
                 ANCHOR)
check("a decision arms a timer too", b5._timer.isActive())
check("on the decision deadline, which is longer than a message's",
      b5._timer.interval() == b5.DECISION_TIMEOUT_S * 1000,
      f"{b5._timer.interval()}ms vs {b5.DECISION_TIMEOUT_S * 1000}ms")
b5.hide()

print()
print("The pipeline's failure reaches the bubble instead of ending in silence")
# Read the source: the only guarantee that matters is that a raise from
# run_chat_pipeline emits something to the bubble rather than returning.
src_path = os.path.join(ROOT, "backend", "main.py")
with open(src_path, encoding="utf-8", errors="replace") as fh:
    main_src = fh.read()
start = main_src.find("async def _handle_prompt")
end = main_src.find("def _on_prompt", start)
body = main_src[start:end] if start != -1 and end != -1 else ""
check("_handle_prompt wraps the pipeline call in a try", "try:" in body)
# The except block, not everything after the last "except" (which included
# the success path and made the check pass on the wrong thing).
import re as _re
_handler = _re.search(
    r"except Exception as e:[^\n]*\n(.*?)\n\s*return\b", body, _re.S)
check("and reports the failure to the bubble on the way out",
      bool(_handler) and "bridge.reply.emit" in _handler.group(1),
      "a failed turn must not leave 'Thinking...' on screen")
check("and the failure path returns, so nothing after it runs on a broken turn",
      bool(_handler))
check("the turn still emits the reply on success",
      "bridge.reply.emit(reply)" in body)
check("and it does not read a bracketed failure aloud",
      'startswith("[Not connected:")' in body, body[:0])

print()
if fails:
    print(f"FAIL: {len(fails)} check(s) failed")
    for f in fails:
        print(f"  - {f}")
    raise SystemExit(1)
print("PASS: no bubble can stay on screen forever")
