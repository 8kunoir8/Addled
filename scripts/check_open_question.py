"""open_question(): what the character prompt is allowed to answer.

The bug this closes: with a question on the bubble, clicking the character and
typing sent a BRAND-NEW request. The question stayed open and expired while the
user believed they had answered it — a message that goes nowhere, with nothing
saying so. Clicking the character and typing is the obvious thing to try, so the
obvious thing has to be the right thing.

Two distinctions matter and both are checked here:
  * a permission prompt is NOT returned, because free text cannot answer one and
    swallowing the typed message as an "answer" would lose it; and
  * the record carries the id, source and conversation, because the backend
    refuses an answer that does not name the conversation it belongs to.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_open_question.py
"""

import importlib.util
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or r"E:\Kunoir\Codeground\Clicky\Addled"
sys.path.insert(0, ROOT)

from PyQt6.QtWidgets import QApplication  # noqa: E402
from PyQt6.QtCore import QPoint  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.chat_bubble import ChatBubble  # noqa: E402

fails = []
ANCHOR = QPoint(400, 400)


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    print("=== nothing open ===")
    b = ChatBubble(agent_name="T")
    check("no question when none is open", b.open_question() is None)

    print("\n=== a question is returned, with everything needed to answer ===")
    record = {"question_id": "q_1", "question": "Which file?",
              "source": "dashboard", "conversation": "c1",
              "options": ["a.py", "b.py"]}
    b.show_decision("❓ Addled needs to know", "Which file?",
                    [{"label": "a.py", "callback": lambda: None}],
                    ANCHOR, closable=True, payload=record)
    held = b.open_question()
    check("the question is offered", held is not None)
    if held:
        check("it carries the id", held.get("question_id") == "q_1", str(held))
        check("it carries the source", held.get("source") == "dashboard")
        check("it carries the conversation", held.get("conversation") == "c1")
        check("it carries the wording",
              held.get("question") == "Which file?", str(held))

    print("\n=== a permission prompt is NOT offered as a question ===")
    b2 = ChatBubble(agent_name="T")
    b2.show_decision("🔐 Permission needed", "Allow it?",
                     [{"label": "Allow", "callback": lambda: None}],
                     ANCHOR, closable=True,
                     payload={"approval_id": "appr_1"})
    check("open_question returns None for a permission prompt",
          b2.open_question() is None,
          "free text cannot answer an approval, so the typed message must be "
          "handled as an ordinary request rather than swallowed")

    print("\n=== a payload-less decision is not offered either ===")
    b3 = ChatBubble(agent_name="T")
    b3.show_decision("❓ Addled needs to know", "Which file?", [], ANCHOR)
    check("a question with no record is not offered",
          b3.open_question() is None,
          "answering needs the id and origin; without them the answer would "
          "be refused by the backend")

    print("\n=== the record does not outlive the card ===")
    b4 = ChatBubble(agent_name="T")
    b4.show_decision("❓ Addled needs to know", "Which file?",
                     [{"label": "x", "callback": lambda: None}],
                     ANCHOR, payload=record)
    check("offered while open", b4.open_question() is not None)
    b4._on_action(lambda: None)
    check("not offered once answered", b4.open_question() is None,
          "a stale record would let the prompt answer a question that is gone")
    b4.show_message("done", ANCHOR)
    check("still not offered after a message", b4.open_question() is None)

    print("\n=== the queued one becomes answerable when it is shown ===")
    b5 = ChatBubble(agent_name="T")
    second = {"question_id": "q_2", "question": "And the path?",
              "source": "code", "conversation": "c2"}
    b5.show_decision("❓ Addled needs to know", "First?",
                     [{"label": "x", "callback": lambda: None}], ANCHOR,
                     payload=record)
    b5.show_decision("❓ Addled needs to know", "Second?",
                     [{"label": "y", "callback": lambda: None}], ANCHOR,
                     payload=second)
    check("the first is the one offered",
          b5.open_question().get("question_id") == "q_1",
          str(b5.open_question()))
    b5._on_action(lambda: None)
    held2 = b5.open_question()
    check("the queued one is offered once shown",
          held2 is not None and held2.get("question_id") == "q_2",
          str(held2))
    check("and it kept its own origin",
          held2 is not None and held2.get("conversation") == "c2",
          str(held2))

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the character prompt can answer the question on screen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
