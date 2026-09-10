"""Download RTK (Rust Token Killer) + ripgrep into tools/rtk/.

Idempotent — skips binaries that already exist (--force refreshes).
Run during build/packaging and optionally by hand. The app is safe
without it: TerminalExecutor falls back to raw command output when
rtk.exe is missing.

Usage:  python scripts/fetch_rtk.py [--force] [--no-rg]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

GH_API = "https://api.github.com/repos"
UA = {"User-Agent": "Addled/1.0 (rtk fetcher)"}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def _extract_exe(data: bytes, target: Path, exe_name: str) -> Path | None:
    with zipfile.ZipFile(BytesIO(data)) as zf:
        names = [n for n in zf.namelist() if n.endswith(f"/{exe_name}")
                 or n == exe_name]
        if not names:
            print(f"  ! {exe_name} not found in zip")
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(zf.read(names[0]))
        print(f"  -> {target} ({target.stat().st_size // 1024} KB)")
        return target


def fetch_rtk(dest: Path, force: bool) -> bool:
    print(f"fetch {dest}")
    if dest.exists() and not force:
        print("  skip rtk.exe (exists)")
        return True
    try:
        rel = _get_json(f"{GH_API}/rtk-ai/rtk/releases/latest")
        asset = next((a for a in rel.get("assets", [])
                      if a["name"].endswith("x86_64-pc-windows-msvc.zip")), None)
        if not asset:
            print("  ! no windows asset found")
            return False
        data = _download(asset["browser_download_url"])
        return _extract_exe(data, dest, "rtk.exe") is not None
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def fetch_rg(dest: Path, force: bool) -> bool:
    print(f"fetch {dest}")
    if dest.exists() and not force:
        print("  skip rg.exe (exists)")
        return True
    try:
        rel = _get_json(f"{GH_API}/BurntSushi/ripgrep/releases/latest")
        asset = next((a for a in rel.get("assets", [])
                      if a["name"].endswith("x86_64-pc-windows-msvc.zip")), None)
        if not asset:
            print("  ! no windows asset found (optional)")
            return True  # rg is optional — rtk still works without it
        data = _download(asset["browser_download_url"])
        return _extract_exe(data, dest, "rg.exe") is not None
    except Exception as e:
        print(f"  FAILED (optional): {e}")
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download binaries")
    ap.add_argument("--no-rg", action="store_true", help="skip ripgrep")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    out = root / "tools" / "rtk"

    ok = fetch_rtk(out / "rtk.exe", args.force)
    if not args.no_rg:
        ok = fetch_rg(out / "rg.exe", args.force) and ok

    print("RTK FETCH OK" if ok else "RTK FETCH INCOMPLETE (raw output fallback "
                                    "will be used)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
