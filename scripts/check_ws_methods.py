"""Do the dashboard's WebSocket calls actually exist on the backend?

A typo here is invisible until a user clicks the thing: the dashboard sends
`settings.get` and gets "unknown method", or asks for a handler that was never
registered and silently shows an empty panel. This walks both sides and
compares them.

Registered handlers that no dashboard page calls are reported but are not
failures — the Electron shell, tray and tests call some of them.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_ws_methods.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "backend", "ws_server.py")
DASHBOARD = os.path.join(ROOT, "dashboard", "src")

REGISTER_RE = re.compile(r"_server\.register\(\s*[\"']([A-Za-z0-9_.]+)[\"']")
SEND_RE = re.compile(r"\bsend\(\s*[\"']([A-Za-z0-9_.]+)[\"']")

# Dynamic names (send(`...`) or a variable) cannot be checked statically.
SKIP_PREFIXES = ("$",)


def registered() -> set[str]:
    text = open(SERVER, encoding="utf-8").read()
    return set(REGISTER_RE.findall(text))


def called() -> dict[str, list[str]]:
    """method name -> the files that call it."""
    found: dict[str, list[str]] = {}
    for base, _dirs, files in os.walk(DASHBOARD):
        for name in files:
            if not name.endswith((".tsx", ".ts")):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT)
            try:
                text = open(path, encoding="utf-8").read()
            except OSError:
                continue
            for method in SEND_RE.findall(text):
                if method.startswith(SKIP_PREFIXES):
                    continue
                found.setdefault(method, []).append(rel)
    return found


def main() -> int:
    fails = []
    have = registered()
    want = called()

    if not have:
        fails.append("No `_server.register(...)` calls were found in ws_server.py — "
                     "the parser is wrong, not the backend.")
    if not want:
        fails.append("No `send('...')` calls were found under dashboard/src — "
                     "the parser is wrong, not the dashboard.")

    for method in sorted(want):
        if method not in have:
            files = ", ".join(sorted(set(want[method]))[:3])
            fails.append(f"dashboard calls '{method}' but the backend does not "
                         f"register it (called from {files})")

    unused = sorted(have - set(want))

    print(f"Backend handlers: {len(have)}")
    print(f"Dashboard calls:  {len(want)}")
    if unused:
        print(f"Registered but not called by the dashboard ({len(unused)}): "
              + ", ".join(unused))
    print()
    print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
    for f in fails:
        print("  -", f)
    return 1 if fails else 0


sys.exit(main())
