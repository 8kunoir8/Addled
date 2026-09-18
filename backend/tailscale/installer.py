"""
Installing Tailscale, on the user's explicit instruction.

Everything else in `backend/tailscale/` manages an install that already exists.
This module exists for the one unavoidable admin step, so the user can start it
from the dashboard instead of being sent off to a website to find the right
build for their architecture.

It is deliberately the most careful code in the project, because its whole job
ends in running an installer **with elevation**. That shapes it:

* **winget first.** `Tailscale.Tailscale` is the vendor's own published package,
  so Microsoft's client resolves and verifies the download rather than us. The
  direct download exists only for machines without winget.
* **The download is verified before it is run.** Signature status must be Valid
  and the signer must actually be Tailscale — otherwise the file is deleted and
  nothing is launched. Downloading an executable and running it on the strength
  of a URL alone would be the weak point of the whole feature.
* **It refuses for remote sessions.** The install raises a UAC prompt on the
  machine; a prompt nobody is sitting in front of hangs forever. And in practice
  the button is only reachable locally anyway, since remote access is what
  Tailscale provides.
* **It is never automatic.** Nothing here runs without a click, and there is no
  code path that installs on startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from backend.tailscale import manager as ts

log = logging.getLogger("addled.tailscale.install")

# The stable "latest" URL. It 302s to the current versioned build on the same
# host, so the host check below still holds.
INSTALLER_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
INSTALLER_HOST = "pkgs.tailscale.com"
SIGNER_MUST_CONTAIN = "tailscale"

# The vendor's own winget package.
WINGET_ID = "Tailscale.Tailscale"

# The installer is a small bootstrapper (~1.4 MB) that pulls the real payload,
# so the floor is low and the ceiling is only there to stop a runaway download.
MIN_BYTES = 256 * 1024
MAX_BYTES = 80 * 1024 * 1024

METHODS = ("auto", "winget", "download")
WAIT_FOR_CLI_S = 600
POLL_S = 3.0

# ShellExecuteW returns a value <= 32 on failure.
SE_ERR_ACCESSDENIED = 5
ERROR_CANCELLED = 1223


def _cfg(key: str, default=None):
    try:
        from backend.config import config
        return config.get("tailscale", key, default=default)
    except Exception:
        return default


# -- IO primitives -------------------------------------------------------------
# Module-level so the test suite can drive the whole flow without winget, a
# network, or a UAC prompt anywhere near it.


def winget_path() -> str | None:
    """winget, if this machine has it. Detected by presence, not by version."""
    found = shutil.which("winget")
    if found:
        return found
    # winget lives in the WindowsApps alias directory, which is not always on
    # PATH for a service or a packaged app.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            pass
    return None


def signature_shells() -> list[str]:
    """PowerShell hosts to try, best first.

    PowerShell 7 is preferred because Windows PowerShell 5.1 does not reliably
    work here: its `Microsoft.PowerShell.Security` module fails to load, and
    `Get-AuthenticodeSignature` then returns nothing while exiting 0. That is a
    silent failure, which for a signature check is the worst possible behaviour,
    so the script below is also written so it cannot fail quietly.
    """
    found: list[str] = []
    seen: set[str] = set()
    for name in ("pwsh.exe", "pwsh", "powershell.exe", "powershell"):
        path = shutil.which(name)
        if not path:
            continue
        # Windows is case-insensitive, so `pwsh` and `pwsh.EXE` resolve to the
        # same file; running the same shell twice would only duplicate errors.
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _powershell() -> str | None:
    shells = signature_shells()
    return shells[0] if shells else None


# Written defensively: `$s.Status.ToString()` on a null $s throws, and a thrown
# error with an empty stdout is indistinguishable from a clean verdict. Every
# path emits JSON, and the Python side treats anything without a `status` as a
# failure to verify rather than as a pass.
_SIGNATURE_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { $s = Get-AuthenticodeSignature -LiteralPath '{path}' } "
    "catch {{ "
    "  Write-Output ('{{\"error\":\"' + ($_.Exception.Message -replace '[\"\\r\\n]', ' ') + '\"}}'); "
    "  exit 0 }}; "
    "if ($null -eq $s) {{ Write-Output '{{\"error\":\"no signature information\"}}'; exit 0 }}; "
    "$subj = ''; "
    "if ($s.SignerCertificate) {{ $subj = [string]$s.SignerCertificate.Subject }}; "
    "Write-Output ([pscustomobject]@{{ status = [string]$s.Status; subject = $subj }} "
    "| ConvertTo-Json -Compress)"
)


def _signature_script(path: Path) -> str:
    """PowerShell that reports a verdict as `STATUS=` / `SUBJECT=` lines.

    Deliberately not JSON: the script has enough braces of its own, and a plain
    `KEY=value` protocol needs no escaping on either side. Every path emits
    something, including the failure paths, so a broken check can never look
    like a clean one.
    """
    quoted = str(path).replace("'", "''")
    return (
        "$ErrorActionPreference = 'Stop'; "
        "$s = $null; "
        "try { $s = Get-AuthenticodeSignature -LiteralPath '" + quoted + "' } "
        "catch { Write-Output ('ERR=' + $_.Exception.Message); exit 0 }; "
        "if ($null -eq $s) { Write-Output 'ERR=no signature information'; exit 0 }; "
        "$subj = ''; "
        "if ($s.SignerCertificate) { $subj = [string]$s.SignerCertificate.Subject }; "
        "Write-Output ('STATUS=' + [string]$s.Status); "
        "Write-Output ('SUBJECT=' + $subj)"
    )


def _parse_signature_output(text: str) -> tuple[str, str, str]:
    """(status, subject, error) from the script's output."""
    status = subject = error = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("STATUS="):
            status = line[len("STATUS="):].strip()
        elif line.startswith("SUBJECT="):
            subject = line[len("SUBJECT="):].strip()
        elif line.startswith("ERR="):
            error = line[len("ERR="):].strip()
    return status, subject, error


def verify_signature(path: Path) -> tuple[bool, str]:
    """Check the Authenticode signature. (ok, detail).

    `ok` is False both when the signature is bad and when it could not be
    checked at all — the caller must never run the file either way. The detail
    says which, so the UI can point at winget when verification is simply
    unavailable on the machine.
    """
    shells = signature_shells()
    if not shells:
        return False, ("Could not verify the download's signature: no "
                       "PowerShell was found on this machine. Use winget "
                       "instead, or install Tailscale yourself.")

    script = _signature_script(path)
    # A file path rather than a command line: PowerShell's parser mangles a
    # script this long when it arrives as a single -Command argument.
    problems: list[str] = []
    for shell in shells:
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                 "Bypass", "-Command", script],
                capture_output=True, text=True, timeout=60,
                creationflags=ts.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as e:
            problems.append(f"{Path(shell).name}: {e}")
            continue

        status, subject, error = _parse_signature_output(proc.stdout or "")
        if error:
            problems.append(f"{Path(shell).name}: {error[:120]}")
            continue
        if not status:
            detail = (proc.stderr or "").strip().splitlines()
            problems.append(f"{Path(shell).name}: "
                            + (detail[0][:120] if detail else "no verdict reported"))
            continue

        if status.lower() != "valid":
            return False, (f"The downloaded file's signature is not valid "
                           f"({status}).")
        if SIGNER_MUST_CONTAIN not in subject.lower():
            return False, ("The downloaded file is signed by someone other than "
                           f"Tailscale ({subject or 'no signer named'}).")
        return True, subject

    return False, ("Could not verify the download's signature: "
                   + "; ".join(problems[:2]))


def launch_elevated(path: Path) -> tuple[bool, str]:
    """Run the installer, raising the Windows admin prompt.

    The user completes the wizard themselves — that is the "manual" part of a
    manual install, and it is why this does not try to pass a silent-install flag
    whose spelling varies by installer version.
    """
    if os.name != "nt":
        return False, "Installing Tailscale is only supported on Windows."
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        result = shell32.ShellExecuteW(None, "runas", str(path), None, None, 1)
    except (OSError, AttributeError) as e:
        return False, f"Could not start the installer: {e}"

    code = int(result or 0)
    if code > 32:
        return True, ""
    if code == ERROR_CANCELLED:
        return False, "You cancelled the Windows administrator prompt, so nothing was installed."
    if code == SE_ERR_ACCESSDENIED:
        return False, "Windows refused the administrator prompt."
    return False, f"Windows could not start the installer (code {code})."


async def fetch(url: str, dest: Path, on_progress=None) -> int:
    """Download to `dest`. Returns the byte count."""
    import httpx

    written = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            # A redirect that leaves the vendor's host would mean we are about
            # to run something Tailscale never published.
            host = (response.url.host or "").lower()
            if host != INSTALLER_HOST:
                raise ValueError(
                    f"The download redirected to {host}, which is not "
                    f"{INSTALLER_HOST}.")
            declared = int(response.headers.get("content-length") or 0)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as handle:
                async for chunk in response.aiter_bytes(65536):
                    handle.write(chunk)
                    written += len(chunk)
                    if written > MAX_BYTES:
                        raise ValueError(
                            f"The download exceeded {MAX_BYTES // (1024 * 1024)} MB "
                            "and was stopped.")
                    if on_progress:
                        on_progress(written, declared)
    return written


async def wait_for_cli(timeout: float = WAIT_FOR_CLI_S) -> bool:
    """Wait for the installer to finish and the CLI to appear."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ts.cli() is not None:
            return True
        await asyncio.sleep(POLL_S)
    return False


# -- the installer -------------------------------------------------------------


class TailscaleInstaller:
    """Runs at most one install at a time and reports progress as it goes."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self.phase = "idle"     # idle|checking|downloading|verifying|launching|waiting|done|failed
        self.pct = 0
        self.detail = ""
        self.error = ""
        self.method = ""

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _preferred_method(self) -> str:
        """What `install()` would actually do, so the UI can say so up front."""
        configured = str(_cfg("install_method", "auto") or "auto").lower()
        if configured == "download":
            return "download"
        if configured == "winget":
            return "winget" if winget_path() else "download"
        return "winget" if winget_path() else "download"

    def status(self) -> dict:
        return {
            "phase": self.phase,
            "pct": self.pct,
            "detail": self.detail,
            "error": self.error,
            "method": self.method,
            "preferred": self._preferred_method(),
            "running": self.running(),
            "installed": ts.installed(),
            "winget": bool(winget_path()),
            "can_install": not ts.installed() and not self.running(),
        }

    def _broadcast(self, method: str, params: dict) -> None:
        try:
            from backend.ws_server import get_server
            get_server().broadcast_nowait(method, params)
        except Exception as e:  # noqa: BLE001
            log.debug("Could not broadcast %s: %s", method, e)

    def _emit(self, phase: str, pct: int, detail: str = "") -> None:
        self.phase = phase
        self.pct = max(0, min(100, int(pct)))
        self.detail = detail
        log.info("Tailscale install: %s %d%% %s", phase, self.pct, detail)
        self._broadcast("tailscale.installProgress", self.status())

    def _fail(self, message: str) -> None:
        self.error = message
        self._emit("failed", 0, message)

    # -- entry point

    async def install(self, method: str = "auto", remote: bool = False) -> dict:
        """Start the install. Returns immediately; progress is broadcast.

        Returns a result dict rather than raising, because every one of these
        refusals is something the user needs to read.
        """
        if ts.installed():
            return {"success": False, "already_installed": True,
                    "error": "Tailscale is already installed."}
        if remote:
            return {"success": False,
                    "error": "Installing has to be done on the machine itself — "
                             "it raises a Windows administrator prompt there, and "
                             "nobody is at that machine to accept it."}
        if self.running():
            return {"success": True, "started": False,
                    "message": "An install is already in progress.",
                    **self.status()}
        if os.name != "nt":
            return {"success": False,
                    "error": "Installing Tailscale is only supported on Windows."}

        chosen = str(method or "auto").strip().lower()
        if chosen not in METHODS:
            return {"success": False, "error": f"Unknown method '{method}'."}

        self.phase = "checking"
        self.pct = 0
        self.detail = ""
        self.error = ""
        self.method = chosen
        self._task = asyncio.create_task(self._run(chosen))
        return {"success": True, "started": True, "method": chosen,
                "message": "Starting the Tailscale installer…"}

    async def _run(self, method: str) -> None:
        try:
            use_winget = False
            if method in ("auto", "winget"):
                path = winget_path()
                if path:
                    use_winget = True
                elif method == "winget":
                    self._fail("winget is not available on this machine. Use the "
                               "direct download instead.")
                    return

            if use_winget:
                await self._via_winget(winget_path())
            else:
                await self._via_download()

            if self.phase == "failed":
                return

            self._emit("waiting", 95,
                       "Waiting for the installer to finish. Complete the setup "
                       "window if it is open.")
            if await wait_for_cli():
                self.error = ""
                self._emit("done", 100, "Tailscale is installed.")
                try:
                    await ts.refresh()
                except Exception as e:  # noqa: BLE001
                    log.debug("Post-install refresh: %s", e)
            else:
                self._fail("The installer did not finish. If the setup window is "
                           "still open, complete it and press Refresh.")
        except Exception as e:  # noqa: BLE001
            log.warning("Tailscale install failed: %s", e)
            self._fail(str(e))
        finally:
            self._broadcast("tailscale.status", self.status())

    async def _via_winget(self, winget: str | None) -> None:
        if not winget:
            self._fail("winget is not available on this machine.")
            return
        self.method = "winget"
        self._emit("downloading", 10,
                   "Asking winget for the official Tailscale package…")

        argv = [winget, "install", "--id", WINGET_ID, "--exact",
                "--accept-source-agreements", "--accept-package-agreements",
                "--disable-interactivity"]
        log.info("Running: %s", " ".join(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=ts.CREATE_NO_WINDOW,
            )
        except (OSError, ValueError) as e:
            self._fail(f"Could not run winget: {e}")
            return

        self.pct = 30
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            # winget reports its own progress bar with carriage returns, which
            # is noise; the phase matters more than the percentage here.
            while True:
                chunk = await asyncio.wait_for(proc.stdout.readline(),
                                               timeout=900)
                if not chunk:
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                lines.append(line)
                if len(lines) > 200:
                    lines.pop(0)
                self.detail = line[:200]
                self.pct = min(90, self.pct + 2)
                self._broadcast("tailscale.installProgress", self.status())
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            self._fail("winget timed out.")
            return
        except Exception as e:  # noqa: BLE001
            self._fail(f"winget failed: {e}")
            return

        code = await proc.wait()
        if code != 0:
            tail = " ".join(lines[-3:]) or "no output"
            self._fail(f"winget exited with {code}: {tail[:300]}")
            return
        self._emit("launching", 92, "winget finished.")

    async def _via_download(self) -> None:
        self.method = "download"
        self._emit("downloading", 5, "Downloading the official installer…")
        dest = Path(tempfile.gettempdir()) / "tailscale-setup-latest.exe"

        def on_progress(written: int, declared: int) -> None:
            if declared > 0:
                pct = 5 + int(60 * min(1.0, written / declared))
            else:
                pct = min(60, 5 + written // (256 * 1024))
            self.pct = pct
            self.detail = f"{written / (1024 * 1024):.1f} MB"
            self._broadcast("tailscale.installProgress", self.status())

        try:
            written = await fetch(INSTALLER_URL, dest, on_progress)
        except Exception as e:  # noqa: BLE001
            self._cleanup(dest)
            self._fail(f"Could not download the installer: {e}")
            return

        if written < MIN_BYTES:
            self._cleanup(dest)
            self._fail(f"Only {written} bytes arrived — that is not the installer.")
            return

        self._emit("verifying", 72, "Checking the download's signature…")
        try:
            ok, detail = await asyncio.to_thread(verify_signature, dest)
        except Exception as e:  # noqa: BLE001
            self._cleanup(dest)
            self._fail(f"Could not verify the download: {e}")
            return
        if not ok:
            # Nothing unverified ever gets executed.
            self._cleanup(dest)
            self._fail(detail)
            return
        log.info("Installer verified (%s)", detail)
        self._emit("verifying", 78, "Signature verified.")

        self._emit("launching", 82,
                   "Waiting for you to accept the Windows administrator prompt…")
        try:
            ok, error = await asyncio.to_thread(launch_elevated, dest)
        except Exception as e:  # noqa: BLE001
            self._cleanup(dest)
            self._fail(f"Could not start the installer: {e}")
            return
        if not ok:
            self._cleanup(dest)
            self._fail(error)
            return

        # The installer keeps its own copy once running, so the download can go.
        self._cleanup(dest)

    @staticmethod
    def _cleanup(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


installer = TailscaleInstaller()
