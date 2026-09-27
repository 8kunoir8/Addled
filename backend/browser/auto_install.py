"""
Auto-activation of optional browser backends (Playwright, browser-use).

Policy (config browser.auto_install): off | ask | auto
  off  — never install; user runs scripts/fetch_*.py manually
  ask  — broadcast browser.installRequest; the dashboard banner offers
         Allow → browser.installApprove runs the install (default)
  auto — install silently in the background (single-flight)

Installs run in a background subprocess against sys.executable (the
backend's own interpreter), never block the event loop, broadcast
browser.installProgress, and back off for an hour after failures.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time

log = logging.getLogger("addled.browser_autoinstall")

_installing: dict[str, bool] = {"playwright": False, "framework": False}
_fail_until: dict[str, float] = {"playwright": 0.0, "framework": 0.0}
BACKOFF_S = 3600


def is_installed(backend: str) -> bool:
    if backend == "playwright":
        from backend.browser.browser_engine import PlaywrightBrowser
        return PlaywrightBrowser._playwright_available()
    if backend == "framework":
        from backend.browser.framework_agent import available
        return available()
    return False


def installing(backend: str) -> bool:
    return bool(_installing.get(backend, False))


def can_retry(backend: str) -> bool:
    return time.time() >= _fail_until.get(backend, 0.0)


def _progress(backend: str, stage: str, detail: str = "") -> None:
    try:
        from backend.ws_server import get_server
        asyncio.get_running_loop().create_task(
            get_server().broadcast(
                "browser.installProgress",
                {"backend": backend, "stage": stage, "detail": detail}))
    except Exception:
        pass


async def _run(backend: str) -> bool:
    if installing(backend):
        return False
    _installing[backend] = True
    _progress(backend, "starting")
    try:
        if backend == "playwright":
            cmds = [
                [sys.executable, "-s", "-m", "pip", "install", "playwright",
                 "--no-warn-script-location"],
                [sys.executable, "-s", "-m", "playwright", "install",
                 "chromium"],
            ]
        else:
            cmds = [
                [sys.executable, "-s", "-m", "pip", "install", "browser-use",
                 "--no-warn-script-location"],
            ]
        for i, cmd in enumerate(cmds):
            _progress(backend, "installing", str(i))
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
            if proc.returncode != 0:
                _fail_until[backend] = time.time() + BACKOFF_S
                tail = (out or b"")[-300:].decode("utf-8", "replace")
                _progress(backend, "failed", tail)
                log.warning("%s auto-install failed: %s", backend, tail[:120])
                return False
        if backend == "framework":
            from backend.browser.framework_agent import refresh
            refresh()
        _progress(backend, "done")
        log.info("%s installed automatically", backend)
        return True
    except Exception as e:
        _fail_until[backend] = time.time() + BACKOFF_S
        _progress(backend, "failed", str(e))
        log.warning("%s auto-install error: %s", backend, e)
        return False
    finally:
        _installing[backend] = False


def maybe_trigger(backend: str) -> str:
    """Apply the auto_install policy. Returns: skip | off | ask | auto."""
    if backend not in ("playwright", "framework"):
        return "skip"
    if is_installed(backend) or installing(backend) or not can_retry(backend):
        return "skip"
    from backend.config import config
    policy = config.get("browser", "auto_install", default="ask")
    if policy == "off":
        return "off"
    if policy == "auto":
        try:
            asyncio.get_running_loop().create_task(_run(backend))
            return "auto"
        except RuntimeError:
            return "off"
    # ask — surface a dashboard banner (broadcast failure is non-fatal)
    try:
        from backend.ws_server import get_server
        asyncio.get_running_loop().create_task(
            get_server().broadcast("browser.installRequest",
                                   {"backend": backend}))
    except Exception:
        pass
    return "ask"


async def approve(backend: str) -> dict:
    """Dashboard-approved install. Returns {success, status}.

    Clears the failure backoff first: the backoff exists to stop the app
    retrying on its own, and a person pressing the button is not that. Doing it
    before the early returns matters too — otherwise the backoff lingered past
    the press and the next automatic attempt was still suppressed, which is the
    state a user cannot see or clear.
    """
    if backend not in ("playwright", "framework"):
        return {"success": False, "error": "unknown backend"}
    _fail_until[backend] = 0.0
    if installing(backend):
        return {"success": True, "status": "installing"}
    if is_installed(backend):
        return {"success": True, "status": "installed"}
    ok = await _run(backend)
    return {"success": ok, "status": "installed" if ok else "failed"}

def _version_of(package: str) -> str:
    """The installed version of a package, or "" when it is not there.

    Read from metadata rather than importing the package: importing
    browser-use pulls in a large dependency tree, and the Settings page only
    needs a version string to show.
    """
    try:
        from importlib import metadata
        return metadata.version(package)
    except Exception:  # noqa: BLE001
        return ""

def status(backend: str) -> dict:
    """What the Settings page needs to draw one backend's row.

    Reports installed / installing / retryable, plus the version and what the
    install will actually do, so the button can say so before it is pressed
    rather than after. A failure within the backoff window reports when it may
    be tried again, because "failed" with a live button that silently does
    nothing is the worst of both.
    """
    if backend not in ("playwright", "framework"):
        return {"backend": backend, "known": False}
    installed = is_installed(backend)
    busy = installing(backend)
    package = "playwright" if backend == "playwright" else "browser-use"
    retry_in = max(0, int(_fail_until.get(backend, 0.0) - time.time()))
    return {
        "backend": backend,
        "known": True,
        "installed": installed,
        "installing": busy,
        "retryInSeconds": retry_in,
        "version": _version_of(package) if installed else "",
        "package": package,
        # What pressing the button runs, said plainly. The browser download is
        # the large part, so a user is not surprised by it.
        "installs": (["playwright (pip)", "the Chromium browser (~150 MB)"]
                     if backend == "playwright"
                     else ["browser-use (pip)"]),
    }

def status_all() -> dict:
    """Both backends at once, for one round trip from the page."""
    from backend.config import config
    policy = "ask"
    try:
        policy = str(config.get("browser", "auto_install", default="ask"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "policy": policy,
        "backends": [status("playwright"), status("framework")],
    }

def reset_backoff(backend: str) -> dict:
    """Clear the failure backoff so the user can retry immediately.

    The backoff exists to stop the app retrying an install by itself every few
    seconds. A person pressing "Install" again is a different thing, and
    refusing them for an hour because the network was briefly down is wrong.
    """
    if backend not in ("playwright", "framework"):
        return {"success": False, "error": "unknown backend"}
    _fail_until[backend] = 0.0
    return {"success": True, "backend": backend}
