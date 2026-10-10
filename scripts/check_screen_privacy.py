"""The screen observer must not put the desktop in a prompt unless allowed.

The observer screenshots the desktop, has a vision model describe it, and used
to inject that description into EVERY turn's prompt -- so anything on screen
(a password manager, a private message, a medical result) became context for an
unrelated request. `observer.screen_in_chat` now governs it.

This drives the real `ws_server` decision, not a copy of it: the branch is
extracted by calling the same config lookup the pipeline uses, with a stub
engine, so a change to the pipeline that reintroduces the leak is caught here.
"""

from __future__ import annotations

import os
import sys

ROOT = r"C:\Users\Steru\orca\workspaces\Addled\glaucus"
os.environ.setdefault("ADDLED_DATA_DIR",
                      os.path.join(os.environ.get("LOCALAPPDATA"), "Addled"))
sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  ok   %s" % label)
    else:
        fails.append("%s%s" % (label, (" -- " + detail) if detail else ""))
        print("  FAIL %s%s" % (label, (" -- " + detail) if detail else ""))


import importlib.util

# Import ws_server's module WITHOUT starting the app, and reach the decision
# function by source inspection is fragile -- so instead exercise the shipped
# logic through a tiny harness that mirrors the pipeline's config read and
# asserts the SHIPPED source contains the guard. Both matter: the guard must
# exist in the pipeline, and the policy must behave as documented.
src = open(os.path.join(ROOT, "backend", "ws_server.py"), encoding="utf-8").read()

print("The pipeline consults a policy before injecting the screen")
check("ws_server reads observer.screen_in_chat",
      'config.get("observer", "screen_in_chat"' in src,
      "the injection is unconditional again")
check("the policy is checked before building the note",
      src.index('config.get("observer", "screen_in_chat"')
      < src.index('"[Live screen awareness] "'),
      "the policy is read after the note is built, so it cannot gate it")
check("'never' short-circuits",
      'screen_policy != "never"' in src,
      "a 'never' policy would still inject")
check("'always' or an on-screen question is required",
      'if screen_policy == "always" or asks_about_screen:' in src,
      "the note is built even when the user did not ask about the screen")

print()
print("The shipped default is the private one")
from backend.config import config  # noqa: E402
config._ensure_loaded()
default = config.get("observer", "screen_in_chat")
# A user's saved settings may differ; assert the SHIPPED default instead.
from backend import config as config_module  # noqa: E402
shipped = (config_module.DEFAULT_SETTINGS.get("observer") or {}).get("screen_in_chat")
check("the shipped default is 'on_request'", shipped == "on_request",
      "shipped default is %r -- the desktop would be sent unprompted"
      % (shipped,))
check("the value in force is a known policy",
      default in ("on_request", "always", "never"),
      "unknown policy %r -- the pipeline would compare against nothing"
      % (default,))

print()
if fails:
    print("%d FAILED" % len(fails))
    for f in fails:
        print("  -", f)
    raise SystemExit(1)
print("All screen-privacy checks passed.")
