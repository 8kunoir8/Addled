"""Install Playwright + Chromium into a target Python (opt-in capability).

Run from the repo root:
  python scripts/fetch_playwright.py --python C:\\Users\\...\\resources\\python\\python.exe

Never added to requirements-core.txt — Playwright stays an optional
runtime capability. Addled's browser engine degrades gracefully without it
(CDP attach and HTTP fallback still work).

Note: `playwright install chromium` downloads ~170 MB per user.
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

    # 1. pip install playwright (skip when already importable)
    r = subprocess.run([py, "-s", "-c", "import playwright"],
                       capture_output=True)
    if r.returncode != 0:
        print("installing playwright ...")
        subprocess.run([py, "-s", "-m", "pip", "install", "playwright",
                        "--no-warn-script-location"], check=True)
    else:
        print("playwright already installed")

    # 2. download the Chromium build
    print("downloading chromium (~170 MB) ...")
    subprocess.run([py, "-s", "-m", "playwright", "install", "chromium"],
                   check=True)

    print("PLAYWRIGHT FETCH OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
