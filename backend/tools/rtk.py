"""RTK (Rust Token Killer) and ripgrep, downloaded when the user asks for them.

RTK rewrites an eligible command — `git status`, `pytest`, `npm install` — into
its own compact form, so the model reads 60-90% fewer tokens for the same
answer. Nothing in Addled depends on it: without the binary, commands run
unchanged and their raw output goes to the model. That is why it is a download
rather than something the installer carries.

It used to be bundled. `tools/` is ignored by git, so the 13 MB of `rtk.exe` and
`rg.exe` were never in the repository — they were fetched by hand on the
developer's machine and copied into every build by `extraResources`. Two things
followed from that: a fresh clone could not produce the same installer, and a
release contained binaries that no review of the repository could see. Now the
button in Settings → Tools installs them into this machine, on request.

What the download does and does not guarantee, because the difference matters:

* The release is resolved through GitHub's own API over HTTPS, and the asset is
  fetched from the URL that API returned.
* The bytes are checked against the `sha256` digest the API publishes for the
  asset. If the API does not publish one, the byte count it declares must match
  exactly. Either way a truncated or substituted download is refused, and
  nothing is put in place.
* This is **not** signature verification. There is no Authenticode check here,
  so it does not defend against a compromised upstream release; it defends
  against a bad transfer and against a URL that is not the one we asked for.
  `backend/tailscale/installer.py` can afford to require a valid signature
  because it downloads from the vendor's own site; these are community releases
  on GitHub, and pretending otherwise would be worse than saying so.
* Nothing here runs on its own. There is no startup path that installs, and no
  code that downloads without a click.
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

log = logging.getLogger("addled.tools.rtk")

# The download, verification and extraction primitives are shared with the uv
# installer for MCP servers — one implementation of a security-relevant path,
# not two. See backend/tools/download.py for what they guarantee.
from backend.tools.download import (API_ROOT, ALLOWED_HOSTS,  # noqa: F401
                                    MAX_ARCHIVE_BYTES, MAX_EXE_BYTES,
                                    MIN_EXE_BYTES, USER_AGENT, extract_exe,
                                    fetch_bytes, fetch_json, resolve_asset,
                                    verify_download)

# Both projects publish Windows x86_64 builds as zips with the exe inside.
TARGET = "x86_64-pc-windows-msvc.zip"

# owner/repo, the exe to lift out of the archive, and whether the app needs it.
SOURCES = (
    ("rtk-ai/rtk", "rtk.exe", True),
    ("BurntSushi/ripgrep", "rg.exe", False),
)

PROVENANCE = "installed.json"

_installing = False
_task: asyncio.Task | None = None
_phase = ""
_pct = 0
_detail = ""
_error = ""
_probed_version: str | None = None


# -- where the binaries live ---------------------------------------------------
# Module level so a test can point them at a temporary directory.


def app_dir() -> Path:
    """`tools/rtk` beside the backend — the checkout, or the install's resources."""
    return Path(__file__).resolve().parents[2] / "tools" / "rtk"


def user_dir() -> Path:
    """A per-user fallback for an install directory that is not writable."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_DATA_HOME")
                or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return Path(base) / "Addled" / "tools" / "rtk"


def install_dir() -> Path:
    """Where an install would go.

    The app directory first, because that is where a checkout and the release
    both expect to find it. If it cannot be written to — a portable build on
    read-only media, or a machine-wide install — the per-user directory is used
    instead, and the binaries are found there just the same.
    """
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
    """Every directory a binary may be in, in the order it is looked for."""
    dirs = [app_dir(), user_dir()]
    seen: set[str] = set()
    out: list[Path] = []
    for directory in dirs:
        key = os.path.normcase(str(directory))
        if key not in seen:
            seen.add(key)
            out.append(directory)
    return out


def find(exe: str = "rtk.exe") -> str | None:
    """The path to one of the binaries, or None. PATH wins over our copies."""
    found = shutil.which(exe[:-4] if exe.endswith(".exe") else exe)
    if found:
        return found
    for directory in search_dirs():
        candidate = directory / exe
        try:
            if candidate.is_file() and candidate.stat().st_size >= MIN_EXE_BYTES:
                return str(candidate)
        except OSError:
            continue
    return None


# -- what we know about it ----------------------------------------------------


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
    """Ask a copy of rtk what version it is.

    A copy that someone installed themselves has no provenance file, so asking
    is the only way to say which one is in use. Cached for the life of the
    process, because the dashboard reads `status()` on every visit and this is a
    subprocess.
    """
    global _probed_version
    if _probed_version is not None:
        return _probed_version
    target = exe or find("rtk.exe")
    if not target:
        return ""
    try:
        import subprocess
        done = subprocess.run([target, "--version"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=5)
        _probed_version = " ".join((done.stdout or done.stderr or "").split())[:60]
    except Exception as e:  # noqa: BLE001
        log.debug("Could not read rtk's version: %s", e)
        _probed_version = ""
    return _probed_version


def status() -> dict:
    """Everything the dashboard needs to draw the row, and nothing readable back."""
    path = find("rtk.exe")
    ripgrep = find("rg.exe")
    known = provenance()
    source = "absent"
    if path:
        source = "path" if shutil.which("rtk") == path else "installed"
    enabled = True
    try:
        from backend.config import config
        enabled = bool(config.get("tools", "rtk_enabled", default=True))
    except Exception:
        pass
    in_use = probe_version(path) if path else ""
    return {
        "available": bool(path),
        "path": path,
        "source": source,
        "ripgrep": bool(ripgrep),
        "enabled": enabled,
        # The version of the copy that will actually run. Asked of the binary
        # itself, because a copy on PATH is someone else's and its version is
        # the one that matters; our own release tag is only a fallback for when
        # the binary cannot be asked.
        "version": in_use or str(known.get("tag") or ""),
        # What Addled installed itself. Kept apart from the copy in use, because
        # a version someone put on PATH is not something Addled installed and
        # should not be reported as if it were.
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
        # Named so the dashboard can say where they come from before anyone
        # clicks, rather than only after.
        "sources": [repo for repo, _exe, _needed in SOURCES],
    }


# -- the install --------------------------------------------------------------


def _emit(phase: str, pct: int = 0, detail: str = "") -> None:
    global _phase, _pct, _detail
    _phase, _pct, _detail = phase, max(0, min(100, int(pct))), detail
    log.info("RTK install: %s %d%% %s", phase, _pct, detail)
    try:
        from backend.ws_server import get_server
        get_server().broadcast_nowait("system.rtkProgress", status())
    except Exception as e:  # noqa: BLE001
        log.debug("Could not broadcast rtk progress: %s", e)


def installing() -> bool:
    return _installing


async def wait(timeout: float = 600) -> dict:
    """Await the running install. For tests and for a caller that wants to know."""
    if _task is None:
        return status()
    try:
        await asyncio.wait_for(asyncio.shield(_task), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("RTK install did not finish within %.0fs", timeout)
    return status()


async def install(force: bool = False, with_rg: bool = True,
                  remote: bool = False) -> dict:
    """Start the download. Returns immediately; progress is broadcast."""
    global _installing, _task, _error
    if remote:
        return {"success": False,
                "error": "Installing has to be done on the machine itself. "
                         "Remote access can use the tools, but not add one."}
    if sys.platform != "win32":
        return {"success": False,
                "error": "The token saver is only published for Windows. "
                         "Install rtk on this machine and it will be used from "
                         "PATH."}
    if _installing:
        return {"success": False, "already_running": True,
                "message": "An install is already in progress.", **status()}
    if find("rtk.exe") and not force:
        return {"success": False, "already_installed": True,
                "error": "The token saver is already installed.", **status()}

    _installing = True
    _error = ""
    _emit("resolving", 0, "Looking up the latest releases")
    _task = asyncio.create_task(asyncio.to_thread(download_all, with_rg))
    return {"success": True, "started": True,
            "message": "Downloading the token saver…"}


def download_all(with_rg: bool = True) -> None:
    """The whole download, synchronously.

    Public because `scripts/fetch_rtk.py` and the suite both drive it directly —
    one implementation of the verification, called from three places.
    """
    from backend.config import config
    global _installing, _error
    destination = install_dir()
    written: dict[str, dict] = {}
    total = 0
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for repo, exe_name, needed in SOURCES:
            if not needed and not with_rg:
                continue
            asset = resolve_asset(repo, TARGET)
            _emit("downloading", 5, f"{asset['name']} ({asset['tag']})")

            def on_progress(received: int, expected: int, _name=asset["name"]) -> None:
                if expected:
                    pct = 5 + int(90 * received / expected)
                else:
                    pct = 50
                _emit("downloading", pct,
                      f"{_name} — {received / 1048576:.1f} MB"
                      + (f" of {expected / 1048576:.1f} MB" if expected else ""))

            archive = fetch_bytes(asset["url"], on_progress=on_progress)
            digest = verify_download(archive, asset)
            _emit("installing", 96, f"Unpacking {exe_name}")
            target = extract_exe(archive, exe_name, destination / exe_name)
            written[exe_name] = {
                "repo": repo, "tag": asset["tag"], "asset": asset["name"],
                "sha256": digest, "bytes": target.stat().st_size,
                "url": asset["url"],
            }
            total += target.stat().st_size

        # Provenance last: its presence is what makes the install complete, so a
        # half-finished download does not read as a successful one.
        (destination / PROVENANCE).write_text(json.dumps({
            "tag": written.get("rtk.exe", {}).get("tag", ""),
            "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_bytes": total,
            "binaries": written,
        }, indent=2), encoding="utf-8")

        # rg.exe is only useful to rtk if rtk can see it; both are in the same
        # directory, and terminal.py puts that directory on PATH for the call.
        _installing = False
        _emit("done", 100, f"Installed into {destination}")
        try:
            config.set("tools", "rtk_enabled", True)
        except Exception as e:  # noqa: BLE001
            log.debug("Could not switch rtk on: %s", e)
    except (urllib.error.URLError, ValueError, OSError,
            zipfile.BadZipFile) as e:
        _error = str(e)
        log.warning("RTK install failed: %s", e)
        _installing = False
        # Remove what this run created, so a retry is not confused by a partial
        # install and `find()` cannot pick up a binary we could not verify.
        for exe_name in written:
            try:
                (destination / exe_name).unlink(missing_ok=True)
            except OSError:
                pass
        _emit("failed", 0, str(e))
    except Exception as e:  # noqa: BLE001
        _error = f"{type(e).__name__}: {e}"
        log.exception("RTK install failed unexpectedly")
        _installing = False
        _emit("failed", 0, _error)
