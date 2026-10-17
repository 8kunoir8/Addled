"""Restore the pre-hardening files. Thin wrapper over backup_security.

    python scripts/rollback_security.py            # restore the dev tree
    python scripts/rollback_security.py --verify   # dry run
    python scripts/rollback_security.py --install  # restore the installed app
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import backup_security  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], *sys.argv[1:]]
    raise SystemExit(backup_security.main())
