"""The browser auto-install must target a writable, importable directory.

The bug this closes: the Settings → Browser "Install Playwright" and "Install
browser-use" buttons ran

    pip install playwright --no-warn-script-location

with NO target specified. Under a per-machine install the bundled interpreter
defaults to its own `site-packages` inside ``C:\\Program Files``, which is
read-only. pip downloaded the packages, then died with

    ERROR: Could not install packages due to an OSError: [WinError 5] Access is denied: ...

The UI simply returned to "Install" with the generic error:
"the install did not finish. check the network and try again — the reason is in the log".

The fix pins `--target` to `app_paths.PYLIBS_DIR`, runs the playwright CLI
through an inline script that sees that target, and verifies the import before
saying "done".

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_browser_autoinstall.py
"""

import os
import pathlib
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


def main() -> int:
    source = pathlib.Path(ROOT, "backend", "browser", "auto_install.py").read_text(
        encoding="utf-8")

    print("=== the pip command targets a writable directory ===")
    check("app_paths is imported", "from backend import app_paths" in source,
          "needs app_paths for the writable target")
    check("pylibs is added to path", "app_paths.add_pylibs_to_path()" in source,
          "packages must be importable without a restart")
    check("playwright install targets pylibs",
          '"--target", target' in source and '"playwright"' in source,
          "pip command has no --target, so it writes to the install dir")
    check("browser-use install targets pylibs",
          '"--target", target' in source and '"browser-use"' in source,
          "pip command has no --target, so it writes to the install dir")

    print("\n=== the playwright CLI step can find its package ===")
    # `playwright install chromium` must not be a bare `-m playwright`: with
    # `-s` the fresh subprocess ignores PYTHONPATH and user site-packages, so
    # it cannot find playwright in pylibs unless it inserts the path inline.
    check("playwright CLI uses an inline script with the target",
          'sys.path.insert(0, {target!r})' in source
          and 'from playwright.__main__ import main' in source,
          "a bare '-m playwright' subprocess cannot import from pylibs under -s")

    print("\n=== success is verified, not assumed from the exit code ===")
    check("playwright import is verified before 'done'",
          'find_spec(check) is None' in source,
          "the install relies on pip's exit code alone")
    check("the failure names the reason",
          'still cannot be imported' in source,
          "the UI would not say why it failed")

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the browser auto-install targets a writable, importable directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
