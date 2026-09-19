"""Download the token saver (rtk + ripgrep) ahead of time, from a terminal.

The app installs these itself, from Settings → Tools, and that is the normal
route. This exists for what a button cannot cover: a machine being prepared
before Addled is first opened, a CI image, or a session where the dashboard is
not reachable.

It is a thin wrapper on purpose. The verification — GitHub's published sha256
for the asset, and a refusal to install anything that does not match it — lives
in `backend/tools/rtk.py`, so there is one implementation rather than a script
that checks less than the app does.

Usage:  python scripts/fetch_rtk.py [--force] [--no-rg]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="download again even if it is already installed")
    parser.add_argument("--no-rg", action="store_true",
                        help="skip ripgrep (rtk works without it)")
    args = parser.parse_args()

    from backend.tools import rtk

    existing = rtk.find("rtk.exe")
    if existing and not args.force:
        print(f"already installed: {existing}")
        print(f"  version {rtk.provenance().get('tag') or 'unknown'}")
        print("  pass --force to replace it")
        return 0

    if os.name != "nt":
        print("The token saver is only published for Windows; nothing to do.")
        return 0

    print(f"installing into {rtk.install_dir()}")
    rtk.download_all(with_rg=not args.no_rg)

    state = rtk.status()
    ours = Path(state["install_dir"]) / "rtk.exe"
    if not ours.is_file():
        print(f"FAILED: {state['error'] or 'the binaries are not in place'}")
        return 1
    print(f"OK: {ours}")
    print(f"  version {state['own']['version'] or 'unknown'}, "
          f"{state['own']['size_bytes'] / 1048576:.1f} MB")
    print(f"  ripgrep {'present' if state['ripgrep'] else 'not installed'}")
    # PATH is searched first, so say so rather than letting the reader assume
    # the copy just downloaded is the one that will run.
    if state["path"] and Path(state["path"]) != ours:
        print(f"  note: {state['path']} is on PATH and takes precedence")
    return 0


if __name__ == "__main__":
    sys.exit(main())
