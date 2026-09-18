"""Tailscale installer checks.

This is the one place in the project that downloads and runs an elevated
installer, so the assertions that matter are the refusals: it must not run when
Tailscale is already there, must not run from a remote session, must not run a
download whose signature does not check out, and must not leave a downloaded
executable lying around.

The IO seams (`fetch`, `verify_signature`, `launch_elevated`, `wait_for_cli`,
`winget_path`) are patched, so nothing here touches the network or raises a UAC
prompt; `winget` is a real `.bat`, so the actual subprocess path is exercised
rather than stubbed.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_tailscale_install.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def make_winget(tmp: Path, exit_code: int, message: str) -> str:
    """A real .bat that stands in for winget.

    Verified separately that `create_subprocess_exec` runs .bat files on Windows
    and propagates their exit code, so this drives the real plumbing.
    """
    path = tmp / f"winget_{exit_code}.bat"
    path.write_text(
        f"@echo off\r\necho {message}\r\nexit /b {exit_code}\r\n",
        encoding="ascii")
    return str(path)


async def run_install(inst, **kwargs):
    """install() returns immediately; wait for the background task."""
    result = await inst.install(**kwargs)
    if result.get("started") and inst._task is not None:
        await asyncio.wait_for(inst._task, timeout=60)
    return result


async def main():
    from backend.config import config
    from backend.tailscale import installer as inst_mod
    from backend.tailscale import manager as manager_mod

    config._ensure_loaded()
    original = dict(config._data.get("tailscale") or {})
    config._data["tailscale"] = {
        "enabled": True, "hostname": "", "serve_enabled": False,
        "serve_port": 443, "funnel": False, "poll_seconds": 20,
        "auth_key": "", "install_method": "auto",
    }

    tmp = Path(tempfile.mkdtemp())
    saved = {name: getattr(inst_mod, name) for name in
             ("winget_path", "fetch", "verify_signature", "launch_elevated",
              "wait_for_cli")}

    try:
        # ---- 1. refusals ---------------------------------------------------
        inst = inst_mod.TailscaleInstaller()
        real_installed = manager_mod.installed
        manager_mod.installed = lambda: True
        try:
            out = await inst.install()
            check("refuses when Tailscale is already installed",
                  out.get("success") is False and out.get("already_installed") is True,
                  str(out))
            check("and does not spawn a task", inst._task is None, "")
        finally:
            manager_mod.installed = real_installed

        inst = inst_mod.TailscaleInstaller()
        out = await inst.install(remote=True)
        check("refuses for a remote session", out.get("success") is False, str(out))
        check("and explains why a remote install cannot work",
              "machine itself" in out.get("error", ""), str(out.get("error")))
        check("nothing was spawned for the remote attempt", inst._task is None, "")

        out = await inst.install(method="teleport")
        check("refuses an unknown method",
              out.get("success") is False and "Unknown method" in out.get("error", ""),
              str(out))

        # ---- 2. which method would be chosen --------------------------------
        inst_mod.winget_path = lambda: r"C:\fake\winget.exe"
        inst = inst_mod.TailscaleInstaller()
        check("winget is preferred when available",
              inst.status()["preferred"] == "winget", str(inst.status()))
        config._data["tailscale"]["install_method"] = "download"
        check("the setting can force the download path",
              inst.status()["preferred"] == "download", str(inst.status()))
        config._data["tailscale"]["install_method"] = "winget"
        inst_mod.winget_path = lambda: None
        check("asking for winget without winget falls back to download",
              inst.status()["preferred"] == "download", str(inst.status()))
        config._data["tailscale"]["install_method"] = "auto"

        # ---- 3. winget success, through a real subprocess -------------------
        events: list[tuple] = []
        inst_mod.winget_path = lambda: make_winget(tmp, 0, "Successfully installed")
        inst_mod.wait_for_cli = lambda timeout=0: _true()
        inst = inst_mod.TailscaleInstaller()
        inst._broadcast = lambda m, p: events.append((m, p))
        out = await run_install(inst)
        check("the winget install starts", out.get("started") is True, str(out))
        check("the winget install succeeds", inst.phase == "done",
              f"{inst.phase}: {inst.error}")
        check("progress reaches 100", inst.pct == 100, str(inst.pct))
        check("progress was broadcast",
              any(m == "tailscale.installProgress" for m, _ in events),
              str(events[:3]))
        check("the install method is recorded", inst.method == "winget", inst.method)

        # ---- 4. winget failure surfaces the CLI's own words -----------------
        inst_mod.winget_path = lambda: make_winget(tmp, 3, "No applicable installer found")
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("a failing winget is reported", inst.phase == "failed", inst.phase)
        check("the winget exit code is in the error", "exited with 3" in inst.error,
              inst.error)
        check("winget's own output is in the error",
              "No applicable installer found" in inst.error, inst.error)

        # ---- 5. the download path -------------------------------------------
        config._data["tailscale"]["install_method"] = "download"
        launch_calls: list = []
        dest_seen: list = []

        def fake_fetch(url, dest, on_progress=None):
            dest_seen.append(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"MZ" + b"\0" * 600_000)
            if on_progress:
                on_progress(200_000, 600_000)
                on_progress(600_000, 600_000)

            async def _done():
                return 600_000
            return _done()

        inst_mod.winget_path = lambda: None
        inst_mod.fetch = fake_fetch
        inst_mod.verify_signature = lambda p: (True, "CN=Tailscale Inc., O=Tailscale Inc.")
        inst_mod.launch_elevated = lambda p: (launch_calls.append(p) or (True, ""))
        inst_mod.wait_for_cli = lambda timeout=0: _true()

        inst = inst_mod.TailscaleInstaller()
        events = []
        inst._broadcast = lambda m, p: events.append((m, p))
        await run_install(inst)
        check("the download install succeeds", inst.phase == "done",
              f"{inst.phase}: {inst.error}")
        check("the installer was launched", len(launch_calls) == 1, str(launch_calls))
        check("the launch target was the downloaded file",
              dest_seen and str(launch_calls[0]) == str(dest_seen[0]), str(launch_calls))
        check("the download is cleaned up afterwards",
              dest_seen and not Path(dest_seen[0]).exists(),
              "the downloaded installer was left on disk")
        phases = [p.get("phase") for m, p in events if m == "tailscale.installProgress"]
        for expected in ("downloading", "verifying", "launching", "waiting", "done"):
            check(f"phase '{expected}' was reported", expected in phases, str(phases))

        # ---- 6. a bad signature must never be run ---------------------------
        launch_calls = []
        dest_seen = []
        inst_mod.verify_signature = lambda p: (False, "The downloaded file is signed by someone other than Tailscale (CN=Evil).")
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("an unverified download fails the install", inst.phase == "failed",
              inst.phase)
        check("the signer problem is in the error",
              "someone other than Tailscale" in inst.error, inst.error)
        check("an unverified installer is NEVER launched", launch_calls == [],
              "a file with a bad signature was executed")
        check("an unverified download is deleted",
              dest_seen and not Path(dest_seen[0]).exists(),
              "the unverified download was left on disk")

        # ---- 7. a download that is not the installer ------------------------
        def tiny_fetch(url, dest, on_progress=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"nope")

            async def _done():
                return 5
            return _done()

        launch_calls = []
        inst_mod.fetch = tiny_fetch
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("a truncated download is refused", inst.phase == "failed", inst.phase)
        check("and says so clearly", "not the installer" in inst.error, inst.error)
        check("a truncated download is not launched", launch_calls == [], "")

        # ---- 8. a download that fails outright ------------------------------
        def broken_fetch(url, dest, on_progress=None):
            async def _boom():
                raise OSError("connection reset by peer")
            return _boom()

        inst_mod.fetch = broken_fetch
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("a failed download is reported", inst.phase == "failed", inst.phase)
        check("the network error is surfaced", "connection reset" in inst.error,
              inst.error)

        # ---- 9. the user declining the admin prompt --------------------------
        config._data["tailscale"]["install_method"] = "download"
        inst_mod.fetch = fake_fetch
        inst_mod.verify_signature = lambda p: (True, "CN=Tailscale Inc.")
        inst_mod.launch_elevated = lambda p: (False, "You cancelled the Windows administrator prompt, so nothing was installed.")
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("a declined admin prompt is reported, not swallowed",
              inst.phase == "failed", inst.phase)
        check("and reads as a cancellation", "cancelled" in inst.error, inst.error)

        # ---- 10. the installer never finishing ------------------------------
        inst_mod.launch_elevated = lambda p: (True, "")
        inst_mod.wait_for_cli = lambda timeout=0: _false()
        inst = inst_mod.TailscaleInstaller()
        await run_install(inst)
        check("an installer that never finishes is reported",
              inst.phase == "failed", inst.phase)
        check("and points at the setup window", "did not finish" in inst.error,
              inst.error)

        # ---- 11. the real signature checker, on a file that is not signed ----
        inst_mod.verify_signature = saved["verify_signature"]
        result = inst_mod.verify_signature(Path(tmp) / "definitely-not-here.exe")
        check("verify_signature returns a pair for a missing file",
              isinstance(result, tuple) and len(result) == 2, str(result))
        check("and refuses it", result[0] is False, str(result))

        unsigned = tmp / "unsigned.bin"
        unsigned.write_bytes(b"not signed")
        ok, detail = inst_mod.verify_signature(unsigned)
        check("an unsigned file is refused", ok is False, f"{ok} {detail}")
        # The distinction matters: this used to pass because the PowerShell script
        # was erroring out, not because a verdict was reached. A refusal has to
        # name a signature status, or the check may not have run at all.
        check("the refusal is a real verdict, not a tooling failure",
              "not valid" in detail.lower(),
              f"verification did not actually run: {detail}")

        # A positive control: a genuinely signed file from another publisher.
        # This proves the verifier runs AND that the signer check is real.
        signed_elsewhere = Path(r"C:\Windows\System32\notepad.exe")
        if signed_elsewhere.is_file():
            ok, detail = inst_mod.verify_signature(signed_elsewhere)
            check("a valid signature from another publisher is refused",
                  ok is False, f"{ok} {detail}")
            check("and names the other signer",
                  "someone other than Tailscale" in detail, detail)

        # ---- 12. nothing sensitive in status --------------------------------
        config._data["tailscale"]["install_method"] = "auto"
        blob = json.dumps(inst_mod.TailscaleInstaller().status())
        check("status reports whether install is possible",
              "can_install" in json.loads(blob), blob)
        check("status carries no paths or secrets",
              "sk-" not in blob and "tailscale.exe" not in blob, blob)

        # ---- 13. wiring ------------------------------------------------------
        source = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
        check("the install handler passes the remote flag",
              "installer.install(method=method, remote=remote)" in source,
              "the remote refusal would never fire")
        check("the install handler reads the configured method",
              '"install_method"' in source, "")
        check("the handler is registered", '"tailscale.install"' in source, "")

        main_src = Path(ROOT, "backend", "main.py").read_text(encoding="utf-8")
        check("nothing installs Tailscale on startup",
              "installer.install(" not in main_src,
              "an install could run without the user asking for it")
    finally:
        for name, fn in saved.items():
            setattr(inst_mod, name, fn)
        manager_mod.installed = real_installed
        config._data["tailscale"] = original
        shutil.rmtree(tmp, ignore_errors=True)


async def _true():
    return True


async def _false():
    return False


asyncio.run(main())
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
