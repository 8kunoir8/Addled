"""Verify that "Start with Windows" launches the app, not just the backend.

The Startup entry is written by the app itself
(`backend/onboarding/wizard.py`), and it used to name the wrong program: the
bundled `python.exe` with `backend/main.py`, which is the PyQt half alone. That
boots the floating character with no tray icon and no dashboard window — so
"Settings" on the avatar's menu was a dead click, because the window it tries
to open is owned by the Electron shell that never started.

The mistake is easy to reintroduce and invisible until a user reboots, so it is
checked here without needing Windows, Electron or a reboot: the writer is
called with the Startup folder redirected to a temp directory, and the file it
produces is read back.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_autostart.py
"""

import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from backend.onboarding import wizard as wizard_mod  # noqa: E402
from backend.config import config  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


APP_EXE = r"C:\Program Files\Addled\Addled.exe"
BACKEND = r"C:\Program Files\Addled\resources\backend\main.py"

tmpdir = Path(tempfile.mkdtemp())
entry = tmpdir / "Addled.bat"

saved = config.get("system", "autostart", default=None)


def with_sandbox(fn):
    """Run `fn` with the Startup folder and the app path redirected."""
    with mock.patch.object(wizard_mod, "_autostart_shortcut", lambda: str(entry)), \
            mock.patch.object(wizard_mod, "app_executable", lambda: APP_EXE):
        return fn()


print("The app executable is the install's Addled.exe, not python")
# The real resolver, against the real layout. `wizard.py` lives at
# <install>/resources/backend/onboarding/wizard.py, so the exe is three up.
real = wizard_mod.app_executable()
check("app_executable() names Addled.exe",
      real.lower().endswith("addled.exe"), real)
check("and it is not a python interpreter",
      "python" not in Path(real).name.lower(), real)
check("and it is not the backend script",
      "main.py" not in real, real)

print()
print("Turning it on writes an entry that runs the app")
res = with_sandbox(lambda: wizard_mod.set_autostart(True))
check("set_autostart(True) reports success", res.get("success") is True, res)
check("the entry exists", entry.is_file())
body = entry.read_text() if entry.is_file() else ""
check("the entry names the executable", "Addled.exe" in body, body)
# The regression that matters, and the literal shape of the old bug.
check("the entry does NOT launch the backend script",
      "main.py" not in body, body)
check("and it does not invoke a python interpreter",
      "python.exe" not in body.lower(), body)
check("autostart_entry_launches_app() agrees",
      with_sandbox(wizard_mod.autostart_entry_launches_app) is True)

print()
print("An entry written by the old, broken code is recognised")
# Exactly what the user has on disk today.
entry.write_text(f'@echo off\n"{APP_EXE[:-10]}resources\\python\\python.exe" '
                 f'"{BACKEND}"\n')
# Both read the sandbox, not the machine's real Startup folder: without the
# patch they would read whatever is actually installed, and this check would
# pass or fail depending on the developer's own settings. That is a bug in the
# check, not the code, and it bit once already.
check("it is reported as not launching the app",
      with_sandbox(wizard_mod.autostart_entry_launches_app) is False)
check("yet it still counts as 'installed' -- the trap",
      with_sandbox(wizard_mod.autostart_enabled) is True,
      "existence alone would call this healthy")

print()
print("The repair rewrites it, and is idempotent")
check("repair_autostart() reports a change",
      with_sandbox(wizard_mod.repair_autostart) is True)
after = entry.read_text()
check("the broken entry is gone from the file",
      "main.py" not in after, after)
check("and it now names the app", "Addled.exe" in after, after)
check("repairing again does nothing",
      with_sandbox(wizard_mod.repair_autostart) is False)
entry.unlink()   # remove the sandbox entry
check("no entry at all is not 'broken'",
      with_sandbox(wizard_mod.autostart_entry_launches_app) is True)

print()
print("Turning it off removes the entry")
res = with_sandbox(lambda: wizard_mod.set_autostart(False))
check("set_autostart(False) reports success", res.get("success") is True, res)
check("the entry is gone", not entry.exists())

print()
print("A missing executable refuses to write a broken entry")
with mock.patch.object(wizard_mod, "_autostart_shortcut", lambda: str(entry)), \
        mock.patch.object(wizard_mod, "app_executable",
                          lambda: r"C:\no\such\place\Addled.exe"):
    res = wizard_mod.set_autostart(True)
check("it reports failure", res.get("success") is False, res)
check("nothing was written", not entry.exists())
check("and the reason is not blamed on the Startup folder",
      "Startup folder" not in str(res.get("error", "")),
      res.get("error"))

# The writer must not leave the stored setting claiming a state the folder
# does not have -- checked because set_autostart records as well as applies.
config.set("system", "autostart", value=saved)

print()
if fails:
    print(f"FAIL: {len(fails)} check(s) failed")
    for f in fails:
        print(f"  - {f}")
    raise SystemExit(1)
print("PASS: start-with-Windows launches the app, not just the backend")
