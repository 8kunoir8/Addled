"""An approval answered on one surface clears the others.

The problem this closes: a permission request is drawn by EVERY surface that is
watching — the chat page, the code page, a bot, and the bubble over the
character — because each is a separate client of the same request. Answering one
of them removed the entry from the pending list and told nobody, so the other
cards stayed on screen waiting for a decision that had already been made. And
pressing their button then failed with "no such approval", which reads as a
broken button rather than a finished request.

Three things have to hold, and each is checked here:

1. **A resolution is announced.** `remember` is what every answer path funnels
   through, so the broadcast is emitted from there rather than from each of the
   four handlers — approve, deny, always-allow and expiry — which could
   otherwise disagree about whether the others should be told.

2. **Only the matching card is cleared.** A bubble can be showing a DIFFERENT
   decision from the one resolved; one turn can raise an approval and then a
   question. Clearing "whatever is on screen" would bury a prompt still needing
   an answer, so the match is on the id.

3. **The outcome survives.** A prompt that was denied must not read as allowed
   on the surface that did not answer it.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_approval_sync.py
"""

import asyncio
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


async def main() -> int:
    from backend.actions import approval_notice
    from backend.ws_server import get_server

    print("=== the store is empty to begin with ===")
    # Start from a known state: a leftover record would make the "announced"
    # check pass without the code under test doing anything.
    approval_notice._pending.clear()
    check("no pending announcements", approval_notice._pending == [],
          str(approval_notice._pending))

    print()
    print("=== a resolution is broadcast, not just forgotten ===")
    # Capture what would go out rather than needing a live socket.
    sent: list[tuple[str, dict]] = []
    server = get_server()
    original = server.broadcast_nowait

    def _capture(method, params=None):
        sent.append((method, params or {}))
        return None

    server.broadcast_nowait = _capture
    try:
        approval_notice.publish("appr_test_1", "run_command",
                                {"command": "echo hi"})
        requested = [m for m, _ in sent if m == "action.approvalRequest"]
        check("the request was announced", requested == ["action.approvalRequest"],
              str(requested))
        # Read `_pending` directly, NOT `pending()`. The public view prunes
        # entries the executor no longer holds, and this test's id is synthetic,
        # so `pending()` would filter the very record under test as stale. That
        # is correct behaviour for a dashboard restore and the wrong lens here.
        check("it is remembered as pending",
              any(r["approval_id"] == "appr_test_1"
                  for r in approval_notice._pending),
              str(approval_notice._pending))

        sent.clear()
        # What an answer on ANOTHER surface looks like from here.
        approval_notice.set_outcome("appr_test_1", "allowed")
        approval_notice.remember("appr_test_1")

        resolved = [(m, p) for m, p in sent if m == "action.approvalResolved"]
        check("answering broadcasts a resolution", len(resolved) == 1,
              str([m for m, _ in sent]))
        if resolved:
            payload = resolved[0][1]
            check("it names the approval",
                  payload.get("approval_id") == "appr_test_1", str(payload))
            # The outcome is what lets a surface say WHY the card went away.
            check("it carries the outcome", payload.get("outcome") == "allowed",
                  str(payload.get("outcome")))
            check("it names the action for the message",
                  payload.get("action_type") == "run_command", str(payload))
        check("it is no longer pending",
              not any(r["approval_id"] == "appr_test_1"
                      for r in approval_notice._pending),
              str(approval_notice._pending))

        print()
        print("=== a denial reads as a denial ===")
        sent.clear()
        approval_notice.publish("appr_test_2", "delete_file", {})
        approval_notice.set_outcome("appr_test_2", "denied")
        approval_notice.remember("appr_test_2")
        resolved2 = [p for m, p in sent if m == "action.approvalResolved"]
        check("a denial is announced as denied",
              resolved2 and resolved2[0].get("outcome") == "denied",
              str(resolved2))

        print()
        print("=== resolving twice does not double-announce ===")
        # A second broadcast would clear a card another surface may have just
        # redrawn, so an unknown id must announce nothing.
        sent.clear()
        approval_notice.remember("appr_test_2")
        check("an already-resolved id announces nothing",
              not [m for m, _ in sent if m == "action.approvalResolved"],
              str([m for m, _ in sent]))

        print()
        print("=== only the matching card is cleared ===")
        # Rebuilt through the real bubble, because the id match is the part
        # worth testing and a mirror of it would prove nothing.
        from PyQt6.QtWidgets import QApplication
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QApplication.instance() or QApplication([])
        from PyQt6.QtCore import QPoint
        from backend.character.chat_bubble import ChatBubble

        bubble = ChatBubble()
        bubble.show_decision(
            "Permission needed", "run a command?",
            [{"label": "Allow once", "callback": lambda: None}],
            QPoint(100, 100), closable=True,
            payload={"approval_id": "appr_visible"},
        )
        check("a decision is on screen", bubble._decision_open)
        check("it carries the approval id it was built from",
              str(bubble._open_decision.get("approval_id")) == "appr_visible",
              str(bubble._open_decision))

        # A DIFFERENT approval resolved elsewhere must not clear this one.
        cleared = bubble.resolve_decision("appr_other")
        check("an unrelated resolution clears nothing", cleared is False,
              "it cleared a card for a different request")
        check("the card is still on screen", bubble._decision_open,
              "the visible prompt was removed by an unrelated answer")

        # The matching one clears it.
        cleared = bubble.resolve_decision("appr_visible")
        check("the matching resolution clears the card", cleared is True)
        check("no decision remains open", not bubble._decision_open,
              "the card stayed on screen")
        check("the payload went with it",
              not bubble._open_decision, str(bubble._open_decision))

        print()
        print("=== resolving when nothing is open is harmless ===")
        # The engine's notifier is called for every broadcast, including ones
        # this bubble has nothing to do with.
        check("clearing an empty bubble returns False",
              bubble.resolve_decision("appr_visible") is False)
        check("and leaves it closable", not bubble._decision_open)
    finally:
        server.broadcast_nowait = original
        approval_notice._pending.clear()

    print()
    print("=== the wiring is present ===")
    from pathlib import Path
    main_src = Path(ROOT, "backend", "main.py").read_text(encoding="utf-8")
    check("the bubble is told about resolutions",
          "decision_resolved" in main_src, "nothing clears the bubble")
    check("the notifier handles the resolution broadcast",
          "action.approvalResolved" in main_src,
          "the broadcast is never consumed")
    # The approval record must carry its id, or the id match has nothing to
    # match on and the bubble would have to guess.
    check("the approval record carries its id",
          '"approval_id": approval_id' in main_src)

    ws = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    check("approve records the outcome before announcing",
          'set_outcome(approval_id, "allowed")' in ws,
          "the broadcast could not say it was allowed")
    check("deny records the outcome before announcing",
          'set_outcome(approval_id, "denied")' in ws,
          "a denial would read as an answer")

    layout = Path(ROOT, "dashboard", "src", "app", "layout.tsx").read_text(
        encoding="utf-8")
    check("the dashboard listens for resolutions",
          "action.approvalResolved" in layout,
          "a card in the dashboard would never clear")
    check("and drops the matching card",
          "dropApproval" in layout)

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all approval-sync checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
