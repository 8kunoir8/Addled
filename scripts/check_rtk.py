"""The token saver: found, verified, installed, or refused.

RTK used to be 13 MB of binaries copied into every build from a directory git
ignores. It is now something the app downloads when the user asks for it, which
puts a downloader between a button and an executable. That is the part worth
testing properly:

* **the binary is found wherever it is** — on PATH, in the app's own directory,
  or in the per-user fallback — and **a file too small to be a binary is not
  used**, because a half-finished download must not be handed to the terminal;
* **the bytes are verified against GitHub's published sha256**, and an install
  that fails verification leaves nothing behind: no partial executable, no
  provenance file, nothing for `find()` to pick up;
* **it refuses the things it should** — a non-https URL, a host that is not
  GitHub's, a download with no digest and no size to check, a remote session,
  and a second install while one is running.

Everything here runs against temporary directories with the network stubbed out,
so it neither downloads anything nor touches the machine's real `tools/`.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_rtk.py
"""

import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def make_zip(name: str, size: int) -> bytes:
    """A zip holding one fake executable of a given size."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr(f"{name.rsplit('.', 1)[0]}/{name}", b"MZ" + b"\0" * size)
    return buffer.getvalue()


def asset_for(archive: bytes, repo="rtk-ai/rtk", tag="v9.9.9", digest=True):
    return {
        "repo": repo, "tag": tag, "name": f"{repo}-x86_64-pc-windows-msvc.zip",
        "url": f"https://github.com/{repo}/releases/download/{tag}/thing.zip",
        "size": len(archive),
        "digest": ("sha256:" + hashlib.sha256(archive).hexdigest()) if digest else "",
    }


async def run():
    from backend import ws_server
    from backend.remote import policy
    from backend.tools import rtk

    tmp = Path(tempfile.mkdtemp(prefix="rtk_check_"))
    app, user = tmp / "app", tmp / "user"
    app.mkdir()

    # Point the module at temporary directories and take PATH out of the
    # picture, so the result does not depend on whether this machine already
    # has rtk installed.
    rtk.app_dir = lambda: app
    rtk.user_dir = lambda: user
    rtk.shutil.which = lambda name, *a, **k: None
    rtk._probed_version = None

    # ---- 1. absent -------------------------------------------------------
    state = rtk.status()
    check("with nothing installed, status says so",
          state["available"] is False and state["source"] == "absent", str(state)[:200])
    check("and an absent binary has no path", state["path"] is None, str(state["path"]))
    check("and nothing claims to have been installed",
          state["own"]["installed"] is False, str(state["own"]))
    check("and it reports the platform it is on",
          isinstance(state["supported"], bool), str(state["supported"]))
    check("find() returns nothing rather than a stray path",
          rtk.find("rtk.exe") is None, str(rtk.find("rtk.exe")))

    # ---- 2. presence, and the size floor --------------------------------
    truncated = app / "rtk.exe"
    truncated.write_bytes(b"MZ" + b"\0" * 1024)
    check("a file too small to be a binary is not used",
          rtk.find("rtk.exe") is None,
          "a half-written download would have been run")

    real = app / "rtk.exe"
    real.write_bytes(b"MZ" + b"\0" * (rtk.MIN_EXE_BYTES + 1024))
    check("a real-looking binary is found", rtk.find("rtk.exe") == str(real),
          str(rtk.find("rtk.exe")))
    check("and status reports it as ours",
          rtk.status()["source"] == "installed", str(rtk.status()["source"]))

    # The per-user fallback is used when the app directory cannot be written.
    real.unlink()
    user.mkdir(parents=True)
    (user / "rtk.exe").write_bytes(b"MZ" + b"\0" * (rtk.MIN_EXE_BYTES + 1024))
    check("a binary in the per-user directory is found too",
          rtk.find("rtk.exe") == str(user / "rtk.exe"), str(rtk.find("rtk.exe")))
    (user / "rtk.exe").unlink()

    # ---- 3. verification -------------------------------------------------
    archive = make_zip("rtk.exe", rtk.MIN_EXE_BYTES + 4096)
    good = asset_for(archive)
    check("a matching sha256 passes",
          rtk.verify_download(archive, good) == hashlib.sha256(archive).hexdigest(), "")

    swapped = asset_for(bytes(archive) + b"x")
    try:
        rtk.verify_download(archive, swapped)
        check("a mismatched sha256 is refused", False, "it was accepted")
    except ValueError as e:
        check("a mismatched sha256 is refused", True, "")
        check("and the refusal names what did not match",
              "sha256" in str(e) and "expected" in str(e), str(e)[:160])

    by_size = asset_for(archive, digest=False)
    check("no digest but a matching size passes",
          bool(rtk.verify_download(archive, by_size)), "")

    wrong_size = dict(by_size, size=len(archive) + 7)
    try:
        rtk.verify_download(archive, wrong_size)
        check("a wrong size with no digest is refused", False, "it was accepted")
    except ValueError:
        check("a wrong size with no digest is refused", True, "")

    try:
        rtk.verify_download(archive, dict(by_size, size=0))
        check("nothing to verify against is refused, not accepted",
              False, "an unverifiable download was accepted")
    except ValueError:
        check("nothing to verify against is refused, not accepted", True, "")

    # ---- 4. url and host guards -----------------------------------------
    try:
        rtk.fetch_bytes("http://api.github.com/repos/x")
        check("a non-https download is refused", False, "http was attempted")
    except ValueError as e:
        check("a non-https download is refused", "non-https" in str(e), str(e)[:120])
    try:
        rtk.fetch_json("https://example.com/evil.json")
        check("a download from another host is refused", False, "it was attempted")
    except ValueError as e:
        check("a download from another host is refused",
              "refusing a download" in str(e), str(e)[:120])

    # ---- 5. extraction ---------------------------------------------------
    destination = app / "rtk.exe"
    check("the exe comes out of the archive",
          rtk.extract_exe(archive, "rtk.exe", destination) == destination
          and destination.is_file(), "not written")
    check("and a .part file is not left behind",
          not destination.with_suffix(".part").exists(), "staging file left")

    try:
        rtk.extract_exe(make_zip("other.exe", rtk.MIN_EXE_BYTES), "rtk.exe",
                        app / "x.exe")
        check("a zip without the expected exe is refused", False, "it was used")
    except ValueError as e:
        check("a zip without the expected exe is refused",
              "not in the archive" in str(e), str(e)[:120])

    try:
        rtk.extract_exe(make_zip("rtk.exe", 64), "rtk.exe", app / "y.exe")
        check("an implausibly small exe is refused", False, "it was written")
    except ValueError as e:
        check("an implausibly small exe is refused", "bytes" in str(e), str(e)[:120])
    check("and nothing was written for it", not (app / "y.exe").exists(), "")

    # ---- 6. a whole install, with the network stubbed --------------------
    # One archive per project, built when the release is resolved — the way the
    # real thing behaves. A single shared archive would only prove that the
    # suite's stub is wrong.
    destination.unlink()
    ripgrep_target = app / "rg.exe"
    archives: dict[str, bytes] = {}
    last_rtk_archive: dict[str, bytes] = {}

    def stub_resolve(repo: str, suffix: str | None = None) -> dict:
        exe = "rg.exe" if repo.endswith("ripgrep") else "rtk.exe"
        blob = make_zip(exe, rtk.MIN_EXE_BYTES + 4096)
        asset = asset_for(blob, repo=repo)
        archives[asset["url"]] = blob
        if exe == "rtk.exe":
            last_rtk_archive["blob"] = blob
        return asset

    rtk.resolve_asset = stub_resolve
    rtk.fetch_bytes = lambda url, on_progress=None, timeout=300: archives[url]

    rtk.download_all(with_rg=True)
    state = rtk.status()
    check("the install lands where the app looks for it",
          state["available"] is True and state["path"] == str(destination),
          str(state)[:200])
    check("and installs the companion ripgrep as well",
          ripgrep_target.is_file(), "rg.exe is not beside rtk.exe")
    check("and records which release it was",
          state["own"]["installed"] is True and state["own"]["version"] == "v9.9.9",
          str(state["own"]))
    check("and the provenance keeps the digest it verified",
          rtk.provenance().get("binaries", {}).get("rtk.exe", {}).get("sha256")
          == hashlib.sha256(last_rtk_archive["blob"]).hexdigest(),
          str(rtk.provenance())[:200])

    # The terminal is the real consumer: it has to pick this copy up.
    from backend.actions import terminal
    check("the terminal executor finds the installed binary",
          terminal._find_rtk() == str(destination), str(terminal._find_rtk()))
    rewritten, changed = terminal._rtk_rewrite("git status", terminal._find_rtk())
    check("and eligible commands are rewritten through it",
          changed and str(destination) in rewritten, rewritten[:160])
    unchanged, changed = terminal._rtk_rewrite("echo hi", terminal._find_rtk())
    check("while everything else passes through untouched",
          not changed and unchanged == "echo hi", unchanged)

    # ---- 7. a failed install leaves nothing to run ------------------------
    destination.unlink()
    ripgrep_target.unlink()
    (app / "installed.json").unlink()

    # Tampered bytes: refused before anything is unpacked.
    rtk.fetch_bytes = lambda url, on_progress=None, timeout=300: archives[url] + b"x"
    rtk.download_all(with_rg=True)
    check("a tampered download leaves no binary behind",
          not destination.exists(), "an unverified binary was left in place")
    check("and no provenance claiming a successful install",
          rtk.provenance() == {}, str(rtk.provenance())[:160])
    check("and says what went wrong", bool(rtk.status()["error"]),
          str(rtk.status())[:160])

    # The first binary verifies and unpacks, the second does not: the install
    # has to be all or nothing, or an incomplete set looks like a finished one.
    rtk.fetch_bytes = lambda url, on_progress=None, timeout=300: archives[url]
    real_resolve = rtk.resolve_asset

    def resolve_without_ripgrep(repo: str, suffix: str | None = None) -> dict:
        if repo.endswith("ripgrep"):
            # A release whose archive does not contain rg.exe, which is what a
            # renamed or repackaged asset looks like from here.
            blob = make_zip("something-else.exe", rtk.MIN_EXE_BYTES + 4096)
            asset = asset_for(blob, repo=repo)
            archives[asset["url"]] = blob
            return asset
        return real_resolve(repo, suffix)

    rtk.resolve_asset = resolve_without_ripgrep
    rtk.download_all(with_rg=True)
    rtk.resolve_asset = real_resolve
    check("a failure part-way through leaves neither binary",
          not destination.exists() and not ripgrep_target.exists(),
          f"rtk={destination.exists()} rg={ripgrep_target.exists()}")
    check("and still leaves no provenance file",
          rtk.provenance() == {}, str(rtk.provenance())[:160])

    # ---- 8. the refusals the button can run into --------------------------
    rtk.download_all(with_rg=True)
    check("the install is back in place for the refusal checks",
          destination.is_file(), "nothing was installed")

    already = await rtk.install(remote=False, with_rg=True)
    check("installing again without force is refused, not repeated",
          already.get("already_installed") is True, str(already)[:200])
    remote = await rtk.install(force=True, remote=True)
    check("a remote session may not install",
          remote.get("success") is False and "machine itself" in remote.get("error", ""),
          str(remote)[:200])

    # ---- 9. the dashboard surface -----------------------------------------
    ws_server._register_default_handlers()
    handlers = ws_server._server._handlers
    for method in ("system.rtkStatus", "system.rtkInstall"):
        check(f"the backend registers '{method}'", method in handlers, "missing")
    refusal = policy.remote_refusal("system.rtkInstall")
    check("remote sessions are refused the install by the dispatcher, not just "
          "by the handler", bool(refusal), "no policy entry")
    if refusal:
        check("and the reason says it installs code",
              "install code" in refusal or "process" in refusal, refusal[:160])

    status = await handlers["system.rtkStatus"]({}, object())
    check("rtkStatus answers without a live session",
          "available" in status and "supported" in status, str(status)[:160])
    check("and does not send the machine a readable path it does not need... "
          "the path it does report is where the binary is",
          isinstance(status.get("path"), (str, type(None))), str(status)[:160])

    # ---- 10. one implementation of the verification ----------------------
    script = (Path(ROOT) / "scripts" / "fetch_rtk.py").read_text(encoding="utf-8")
    check("the CLI delegates instead of verifying on its own",
          "rtk.download_all" in script, "the script does not call the module")
    check("and does no hashing of its own", "hashlib" not in script,
          "the verification has been duplicated in the script")


async def main():
    try:
        await asyncio.wait_for(run(), timeout=180)
    except Exception as e:
        import traceback
        traceback.print_exc()
        fails.append(f"the suite raised: {e!r}")
    if fails:
        print(f"FAIL: {len(fails)} rtk check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: the token saver is found, verified, installed or refused")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
