"""Install the browser-use framework into a target Python (opt-in).

Run from the repo root:
  python scripts/fetch_browser_use.py --python C:\\Users\\...\\resources\\python\\python.exe

Prerequisite: Playwright + Chromium in the same Python
(scripts/fetch_playwright.py) — browser-use launches its own Chromium.

Never added to requirements-core.txt. Addled's router only uses the
framework when it is installed AND an LLM is available; otherwise it
degrades to the deterministic backends.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True,
                    help="Path to the target python.exe (e.g. the installed "
                         "app python)")
    args = ap.parse_args()
    py = args.python

    r = subprocess.run([py, "-s", "-c", "import browser_use"],
                       capture_output=True)
    if r.returncode == 0:
        print("browser-use already installed")
        print("BROWSER-USE FETCH OK")
        return 0

    print("installing browser-use ...")
    subprocess.run([py, "-s", "-m", "pip", "install", "browser-use",
                    "--no-warn-script-location"], check=True)

    # sanity import
    subprocess.run([py, "-s", "-c", "import browser_use; "
                    "print('browser-use', getattr(browser_use, "
                    "'__version__', '?'))"], check=True)

    print("BROWSER-USE FETCH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
