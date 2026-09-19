"""`uv` — the runtime MCP servers packaged for PyPI are launched with.

The MCP registry advertises two kinds of local package: npm (launched with
`npx`, which Node ships) and PyPI (launched with `uvx`, which almost nothing
ships). On a machine without `uv`, every PyPI-packaged server in the market was
reported as `needs 'uvx' on PATH (install uv)` and could not be added — a whole
category of the market was visible and unusable, with no way to act on the
reason it gave.

So it is a download the same way the token saver is: resolved through GitHub's
API, verified against the published sha256, unpacked into this machine's tools
directory, and — unlike the token saver — put on PATH for the servers Addled
starts, so `uvx` resolves whether or not a user shell would find it.

Nothing depends on it. Without it the npm half of the market still works and
PyPI entries still say what they need.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
import urllib.error
import zipfile
from pathlib import Path

log = logging.getLogger("addled.tools.uv")

# Module level, so the suite can drive the whole download with the network
# stubbed — the same seam the token saver's suite uses.
from backend.tools.download import (extract_members,  # noqa: E402
                                    fetch_bytes, resolve_asset,
                                    verify_download)

REPO = "astral-sh/uv"
# The Windows x86_64 build, which holds uv.exe and uvx.exe.
TARGET = "x86_64-pc-windows-msvc.zip"
MEMBERS = ("uv.exe", "uvx.exe")

# A far lower floor than rtk's 400 KB: uv.exe is a normal-sized binary but
# uvx.exe is a small launcher that forwards to it, and measuring both with rtk's
# floor refused a perfectly good release. 64 KB still rejects an HTML error page
# written out as an executable, which is what the floor is for.
MIN_MEMBER_BYTES = 64 * 1024

# What the registry's launchers look for: `uvx` for PyPI packages, `uv` for
# anything that shells out to it directly.
LAUNCHERS = ("uv.exe", "uvx.exe")

PROVENANCE = "installed.json"

_installing = False
_task: asyncio.Task | None = None
_phase = ""
_pct = 0
_detail = ""
_error = ""
_probed_version: str | None = None


# -- where it lives ------------------------------------------------------------
# Module level so a test can point them at a temporary directory.


def app_dir() -> Path:
    """`tools/uv` beside the backend — the checkout, or the install's resources."""
    return Path(__file__).resolve().parents[2] / "tools" / "uv"


def user_dir() -> Path:
    """A per-user fallback for an install directory that is not writable."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return Path(base) / "Addled" / "tools" / "uv"


def install_dir() -> Path:
    """Where an install would go: the app directory, else the per-user one."""
    preferred = app_dir()
    try:
        if preferred.is_dir() and os.access(preferred, os.W_OK):
            return preferred
        parent = preferred.parent
        if not preferred.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
            if os.access(parent, os.W_OK):
                return preferred
    except OSError:
        pass
    return user_dir()


def search_dirs() -> list[Path]:
    """Every directory it may be in, in the order it is looked for."""
    out: list[Path] = []
    seen: set[str] = set()
    for directory in (app_dir(), user_dir()):
        key = os.path.normcase(str(directory))
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out


def find(exe: str = "uvx.exe") -> str | None:
    """The path to `uv`/`uvx`, or None. PATH wins over our copies."""
    stem = exe[:-4] if exe.endswith(".exe") else exe
    found = shutil.which(stem)
    if found:
        return found
    for directory in search_dirs():
        candidate = directory / exe
        try:
            if (candidate.is_file()
                    and candidate.stat().st_size >= MIN_MEMBER_BYTES):
                return str(candidate)
        except OSError:
            continue
    return None


def path_entries() -> list[str]:
    """Directories to put on PATH for a spawned server so `uvx` resolves.

    Only a directory holding a plausible uvx.exe counts, measured with the same
    floor `find()` uses: an empty leftover directory, or the stub a failed
    download was cleaned up from, must not shadow a real uvx further along PATH.
    """
    out: list[str] = []
    for directory in search_dirs():
        candidate = directory / "uvx.exe"
        try:
            if (candidate.is_file()
                    and candidate.stat().st_size >= MIN_MEMBER_BYTES):
                out.append(str(directory))
        except OSError:
            continue
    return out


def provenance() -> dict:
    """What was installed, when, and the digest it was verified against."""
    for directory in search_dirs():
        path = directory / PROVENANCE
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def probe_version(exe: str | None = None) -> str:
    """Ask the copy what version it is; cached, because the dashboard polls."""
    global _probed_version
    if _probed_version is not None:
        return _probed_version
    target = exe or find("uv.exe")
    if not target:
        return ""
    try:
        import subprocess
        done = subprocess.run([target, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=5)
        _probed_version = " ".join((done.stdout or done.stderr or "").split())[:60]
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read uv's version: %s", e)
        _probed_version = ""
    return _probed_version


def status() -> dict:
    """Everything the dashboard needs to draw the row."""
    path = find("uvx.exe")
    known = provenance()
    source = "absent"
    if path:
        source = "path" if shutil.which("uvx") == path else "installed"
    in_use = probe_version(find("uv.exe")) if path else ""
    return {
        "available": bool(path),
        "path": path,
        "source": source,
        # Both names, because the market's reason names `uvx` specifically.
        "uv": find("uv.exe"),
        "uvx": find("uvx.exe"),
        "version": in_use or str(known.get("tag") or ""),
        "own": {
            "installed": bool(known),
            "version": str(known.get("tag") or ""),
            "installed_at": str(known.get("installed_at") or ""),
            "size_bytes": int(known.get("total_bytes") or 0),
        },
        "installing": _installing,
        "phase": _phase,
        "percent": _pct,
        "detail": _detail,
        "error": _error,
        "supported": sys.platform == "win32",
        "install_dir": str(install_dir()),
        "sources": [REPO],
        # What the market's "needs 'uvx' on PATH" reason turns into once this is
        # installed: the launchers Addled can now offer.
        "launchers": list(LAUNCHERS),
    }


# -- the install ---------------------------------------------------------------


def _emit(phase: str, pct: int = 0, detail: str = "") -> None:
    global _phase, _pct, _detail
    _phase, _pct, _detail = phase, max(0, min(100, int(pct))), detail
    log.info("uv install: %s %d%% %s", phase, _pct, detail)
    try:
        from backend.ws_server import get_server
        get_server().broadcast_nowait("system.uvProgress", status())
    except Exception as e:  # noqa: BLE001
        log.debug("Could not broadcast uv progress: %s", e)


def installing() -> bool:
    return _installing


async def wait(timeout: float = 600) -> dict:
    """Await the running install. For tests and for a caller that wants to know."""
    if _task is None:
        return status()
    try:
        await asyncio.wait_for(asyncio.shield(_task), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("uv install did not finish within %.0fs", timeout)
    return status()


async def install(force: bool = False, remote: bool = False) -> dict:
    """Start the download. Returns immediately; progress is broadcast."""
    global _installing, _task, _error
    if remote:
        return {"success": False,
                "error": "Installing has to be done on the machine itself. "
                         "Remote access can use the servers, but not add a "
                         "runtime."}
    if sys.platform != "win32":
        return {"success": False,
                "error": "This download is the Windows build of uv. Install uv "
                         "on this machine and it will be used from PATH."}
    if _installing:
        return {"success": False, "already_running": True,
                "message": "An install is already in progress.", **status()}
    if find("uvx.exe") and not force:
        return {"success": False, "already_installed": True,
                "error": "uv is already installed.", **status()}

    _installing = True
    _error = ""
    _emit("resolving", 0, "Looking up the latest uv release")
    _task = asyncio.create_task(asyncio.to_thread(download_all))
    return {"success": True, "started": True,
            "message": "Downloading uv…"}


def download_all() -> None:
    """The whole download, synchronously.

    Public because the suite drives it directly, the same way it drives rtk's —
    one implementation of the verification, called from two places.
    """
    global _installing, _error
    destination = install_dir()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        asset = resolve_asset(REPO, TARGET)
        _emit("downloading", 5, f"{asset['name']} ({asset['tag']})")

        def on_progress(received: int, expected: int) -> None:
            pct = 5 + int(90 * received / expected) if expected else 50
            _emit("downloading", pct,
                  f"{asset['name']} — {received / 1048576:.1f} MB"
                  + (f" of {expected / 1048576:.1f} MB" if expected else ""))

        archive = fetch_bytes(asset["url"], on_progress=on_progress)
        digest = verify_download(archive, asset)
        _emit("installing", 96, "Unpacking uv.exe and uvx.exe")
        written = extract_members(archive, MEMBERS, destination,
                                  min_bytes=MIN_MEMBER_BYTES)
        total = sum(p.stat().st_size for p in written.values())

        # Provenance last: its presence is what makes the install complete, so a
        # half-finished download does not read as a successful one.
        (destination / PROVENANCE).write_text(json.dumps({
            "tag": asset["tag"],
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_bytes": total,
            "binaries": {name: {"repo": REPO, "tag": asset["tag"],
                                "asset": asset["name"], "sha256": digest,
                                "bytes": path.stat().st_size,
                                "url": asset["url"]}
                         for name, path in written.items()},
        }, indent=2), encoding="utf-8")

        _installing = False
        _emit("done", 100, f"Installed into {destination}")
    except (urllib.error.URLError, ValueError, OSError,
            zipfile.BadZipFile) as e:
        _error = str(e)
        log.warning("uv install failed: %s", e)
        _installing = False
        # Remove what this run created, so `find()` cannot pick up a binary we
        # could not verify.
        for name in MEMBERS:
            try:
                (destination / name).unlink(missing_ok=True)
            except OSError:
                pass
        _emit("failed", 0, str(e))
    except Exception as e:  # noqa: BLE001
        _error = f"{type(e).__name__}: {e}"
        log.exception("uv install failed unexpectedly")
        _installing = False
        _emit("failed", 0, _error)
