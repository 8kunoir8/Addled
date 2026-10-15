"""Download the Kokoro voice models ahead of time, from a terminal.

The app downloads these itself, from Settings → Voice, and that is the normal
route. This exists for what a button cannot cover: a machine being prepared
before Addled is first opened, a CI image, or a session where the dashboard is
not reachable.

It is a thin wrapper on purpose. The verification — the pinned sha256 for each
file, and a refusal to install anything that does not match — lives in
`backend/voice/model_install.py`, so there is one implementation rather than a
script that checks less than the app does. That also means this exercises the
same code path the button does.

Usage:  python scripts/fetch_kokoro.py [--force]
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
    args = parser.parse_args()

    from backend.voice import model_install

    state = model_install.status()
    if state["available"] and not args.force:
        print(f"already available from {state['source']}: "
              f"{state['install_dir']}")
        print("  pass --force to download a fresh copy into the data directory")
        return 0

    if os.name != "nt":
        print("These builds are only published for Windows; nothing to do.")
        return 0

    print(f"downloading {state['total_bytes'] / 1048576:.0f} MB into "
          f"{state['install_dir']}")
    model_install.download_all()

    state = model_install.status()
    if not state["installed"]:
        print(f"FAILED: {state['error'] or 'the models are not in place'}")
        return 1
    print(f"OK: {state['install_dir']}")
    for model in model_install.MODELS:
        path = model_install.install_dir() / model["filename"]
        print(f"  {model['filename']}: {path.stat().st_size / 1048576:.1f} MB")
    print(f"  {state['size_bytes'] / 1048576:.1f} MB total, "
          f"installed {state['installed_at']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
