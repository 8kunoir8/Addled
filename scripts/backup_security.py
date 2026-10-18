"""Back up the files this hardening pass will change, and restore them.

Run BEFORE the first edit. The manifest records the SHA256 of each file as it
was, in both the dev tree and the install, so a rollback can prove it restored
the right bytes rather than merely overwrite with something.

    python scripts/backup_security.py            # take a backup
    python scripts/backup_security.py --list     # show existing backups
    python scripts/rollback_security.py          # restore the newest
    python scripts/rollback_security.py --verify # dry run, report only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_PARENT = ROOT / "_pn"
INSTALL_ROOT = Path(r"C:\Program Files\Addled\resources\backend")

# Every file the plan changes. Relative to `backend/`.
FILES = [
    "safety/destruction_gate.py",
    "safety/kill_switch.py",
    "safety/halt.py",              # new
    "actions/executor.py",
    "approvals/policy.py",
    "ws_server.py",
    "main.py",
    "cli_tools/runner.py",
    "safety/egress_monitor.py",     # may not change; cheap to include
    "safety/prompt_guard.py",
    "codemode/verify.py",
    "workspace.py",
    "perception/observer.py",
    "memory/_atomic.py",            # new
    "memory/chat_history.py",
    "memory/facts.py",
    "memory/journal.py",
    "memory/session_context.py",
    "memory/session_summary.py",
    "memory/user_profile.py",
    "tasks/store.py",
    "goals/store.py",
    "skills/tool_loop.py",
    "remote/policy.py",
    # Browser launch policy: the search leg that opens a window, the ordering
    # inside navigate(), and the forge's own browser research.
    "skills/forge.py",
    "skills/registry.py",
    "browser/browser_engine.py",
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def take() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_PARENT / f"backup_security_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {"stamp": stamp, "files": {}}
    for rel in FILES:
        src = ROOT / "backend" / rel
        entry = {"dev": sha(src), "install": sha(INSTALL_ROOT / rel),
                 "existed": src.exists()}
        if src.exists():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        manifest["files"][rel] = entry
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return dest


def newest() -> Path | None:
    found = sorted(BACKUP_PARENT.glob("backup_security_*"))
    return found[-1] if found else None


def restore(dest: Path, *, verify_only: bool, into_install: bool) -> int:
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    target_root = INSTALL_ROOT if into_install else ROOT / "backend"
    changed = missing = 0
    for rel, entry in manifest["files"].items():
        saved = dest / rel
        target = target_root / rel
        if not saved.exists():
            # Did not exist before this pass: remove anything we created.
            if entry.get("existed") is False and target.exists():
                print(f"  remove   {rel} (did not exist before)")
                if not verify_only:
                    target.unlink()
                changed += 1
            continue
        want = entry["install" if into_install else "dev"]
        if sha(target) == want:
            continue
        print(f"  restore  {rel}")
        changed += 1
        if not verify_only:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
    # Report any backup file that has no counterpart on disk.
    for rel in manifest["files"]:
        if not (target_root / rel).exists() and (dest / rel).exists():
            missing += 1
    print(f"{'would change' if verify_only else 'changed'}: {changed} "
          f"file(s); target={target_root}")
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--install", action="store_true",
                    help="restore into the installed app instead of the dev tree")
    ap.add_argument("--from", dest="source", default="",
                    help="a specific backup directory")
    args = ap.parse_args()

    if args.list:
        for d in sorted(BACKUP_PARENT.glob("backup_security_*")):
            n = len(json.loads((d / "manifest.json").read_text("utf-8"))["files"])
            print(f"{d.name}  ({n} files)")
        return 0

    if args.source:
        dest = Path(args.source)
    else:
        dest = newest()
    if dest is None:
        print("no backup found — run scripts/backup_security.py first")
        return 1
    print(f"using {dest}")
    return 0 if restore(dest, verify_only=args.verify,
                        into_install=args.install) >= 0 else 1


if __name__ == "__main__":
    sys.exit(take() and 0 if len(sys.argv) == 1 else main())
