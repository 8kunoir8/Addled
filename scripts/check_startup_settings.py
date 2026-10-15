"""Verify that the character's Settings action always reaches *somewhere*.

The action has three steps: tell the dashboard to navigate, bring the Addled
window to the front, and — if that does not work — open the dashboard in a
browser. The third step is the one that matters when the app is running without
its Electron shell (which is exactly what a backend-only launch, e.g. an
autostart entry pointing at main.py, produces).

It was unreachable. `WindowManager.focus()` reports failure by *returning*
`{"success": False, ...}` — it catches its own errors — so a bare `return`
after the call ran every time and the fallback was dead code. The user-visible
symptom was that Settings did nothing at all: no window to focus, and no
browser tab either.

So these checks assert the OUTCOME (did we end up somewhere?), not the
mechanism, and they stub the two things that would otherwise make real noise:
focusing a window and opening a browser.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_startup_settings.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from unittest import mock  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])

from backend.character.avatar import CharacterWidget  # noqa: E402
from backend.actions.window_manager import WindowManager  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def run_settings(widget, focus_result=None, focus_raises=None, url=None):
    """Call the action with focus and the browser stubbed.

    Returns (opened_urls, broadcast_paths) so a check can say what happened
    rather than what was intended.
    """
    opened = []
    broadcast = []

    async def fake_focus(self, title):
        if focus_raises is not None:
            raise focus_raises
        return focus_result

    with mock.patch.object(WindowManager, "focus", fake_focus), \
            mock.patch.dict(sys.modules, {}), \
            mock.patch("webbrowser.open",
                       lambda u, *a, **k: opened.append(u)):
        # The action imports `backend.ws_server` for the nav intent; stub the
        # server so no real one is needed and the intent is observable.
        fake_server = mock.Mock()
        fake_server.set_nav_intent = lambda p: broadcast.append(p)
        fake_server.broadcast_nowait = lambda m, p=None: None
        with mock.patch("backend.ws_server.get_server",
                        lambda: fake_server):
            if url is not None:
                # Force the URL branch deterministically rather than depend on
                # how the test happens to be launched.
                with mock.patch("pathlib.Path.resolve") as res:
                    res.return_value.parent.name = "resources"
                    widget._default_settings_dialog()
            else:
                widget._default_settings_dialog()
    return opened, broadcast


print("A failed focus must still open the dashboard")
widget = CharacterWidget({"size": 64, "eyes": True})

# The exact shape WindowManager.focus() returns when nothing matches. This is
# the case the report is about: backend running, no Electron window.
opened, _ = run_settings(
    widget, focus_result={"success": False,
                          "error": "No open window matched 'Addled'."})
check("no window found -> the browser fallback runs",
      len(opened) == 1, f"opened={opened}")
check("and it opens the dashboard, not something else",
      bool(opened) and opened[0].startswith("http://127.0.0.1:")
      or bool(opened) and opened[0].startswith("http://localhost"),
      f"opened={opened}")

print()
print("A successful focus must NOT open a browser tab")
opened, _ = run_settings(
    widget, focus_result={"success": True, "summary": "Focused: Addled"})
check("window found -> no browser tab", opened == [], f"opened={opened}")

print()
print("A focus that raises must also fall through, not swallow the action")
opened, _ = run_settings(widget, focus_raises=RuntimeError(
    "asyncio.run() cannot be called from a running event loop"))
check("focus raising -> the browser fallback still runs",
      len(opened) == 1, f"opened={opened}")

print()
print("A focus that returns nothing (or a bare truthy) is not success")
opened, _ = run_settings(widget, focus_result=None)
check("focus returning None -> treated as failure, falls back",
      len(opened) == 1, f"opened={opened}")

print()
print("The nav intent is set regardless, so a live shell still navigates")
opened, broadcast = run_settings(
    widget, focus_result={"success": False, "error": "no window"})
check("the dashboard is told to navigate to /settings",
      broadcast == ["/settings"], f"broadcast={broadcast}")

print()
if fails:
    print(f"FAIL: {len(fails)} check(s) failed")
    for f in fails:
        print(f"  - {f}")
    raise SystemExit(1)
print("PASS: the character's Settings action always ends somewhere")
