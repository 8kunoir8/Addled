"""uv: found, verified, installed, or refused — and on the PATH the servers get.

Every PyPI-packaged server in the MCP market is launched with `uvx`, and almost
no machine has it, so those entries said "needs 'uvx' on PATH (install uv)" and
could not be added. This is the download behind the button that fixes that, plus
the two places that have to notice an installed copy: the market's runtime check,
and the PATH a spawned server is given.

What is asserted, and why each one is worth it:

* **a bundle is all-or-nothing.** uv ships as one zip holding uv.exe and uvx.exe;
  a run that unpacks one and fails on the other must leave neither, or `find()`
  hands out a half of a runtime;
* **the bytes are verified against GitHub's published sha256**, and a tampered
  download leaves no binary and no provenance claiming success;
* **`path_entries()` lists only a directory that really holds uvx.exe**, so an
  empty leftover directory cannot shadow a working copy on PATH;
* **a remote session cannot install it** — remote access may use the servers, not
  add a runtime to the machine.

Network is stubbed; everything runs in temporary directories.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_uv.py
"""

import asyncio
import hashlib
import io
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


def make_zip(members: dict, size: int | None = None) -> bytes:
    """A zip holding the named fake executables."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name in members:
            bundle.writestr(name, b"MZ" + b"\0" * (size or 200 * 1024))
    return buffer.getvalue()


def asset_for(archive: bytes, digest: bool = True) -> dict:
    return {
        "repo": "astral-sh/uv", "tag": "0.9.9",
        "name": "uv-x86_64-pc-windows-msvc.zip",
        "url": "https://github.com/astral-sh/uv/releases/download/0.9.9/uv.zip",
        "size": len(archive),
        "digest": (("sha256:" + hashlib.sha256(archive).hexdigest())
                   if digest else ""),
    }


async def run():
    from backend.tools import uv

    tmp = Path(tempfile.mkdtemp(prefix="uv_check_"))
    app, user = tmp / "app", tmp / "user"
    app.mkdir()

    # Temporary directories, and PATH out of the picture, so the result does not
    # depend on whether this machine already has uv.
    uv.app_dir = lambda: app
    uv.user_dir = lambda: user
    uv.shutil.which = lambda name, *a, **k: None
    uv._probed_version = None

    # ---- 1. absent -------------------------------------------------------
    state = uv.status()
    check("with nothing installed, status says so",
          state["available"] is False and state["source"] == "absent",
          str(state)[:200])
    check("and nothing claims to have been installed",
          state["own"]["installed"] is False, str(state["own"]))
    check("and it reports the platform it is on",
          isinstance(state["supported"], bool), str(state["supported"]))
    check("find() returns nothing rather than a stray path",
          uv.find("uvx.exe") is None, str(uv.find("uvx.exe")))
    check("and nothing is offered on PATH for a spawned server",
          uv.path_entries() == [], str(uv.path_entries()))

    # ---- 2. presence, and the size floor --------------------------------
    (app / "uvx.exe").write_bytes(b"MZ" + b"\0" * 1024)
    check("a file too small to be a binary is not used",
          uv.find("uvx.exe") is None,
          "a half-written download would have been launched")
    check("and is not offered on PATH either",
          uv.path_entries() == [], str(uv.path_entries()))

    size = 400 * 1024 + 1024
    for name in ("uv.exe", "uvx.exe"):
        (app / name).write_bytes(b"MZ" + b"\0" * size)
    check("a real-looking pair is found",
          uv.find("uv.exe") == str(app / "uv.exe")
          and uv.find("uvx.exe") == str(app / "uvx.exe"), "")
    check("and the directory is offered on PATH for a server",
          uv.path_entries() == [str(app)], str(uv.path_entries()))
    check("and status reports it as ours",
          uv.status()["source"] == "installed", str(uv.status()["source"]))

    # ---- 3. extraction is all-or-nothing --------------------------------
    good = make_zip(["uv.exe", "uvx.exe"], size=size)
    target = tmp / "unpacked"
    written = uv.extract_members(good, ("uv.exe", "uvx.exe"), target)
    check("both executables come out of the bundle",
          set(written) == {"uv.exe", "uvx.exe"}
          and all(p.is_file() for p in written.values()), str(list(written)))
    check("and no .part staging file is left behind",
          not any(target.glob("*.part")), "staging file left")

    try:
        uv.extract_members(make_zip(["uv.exe"], size=size),
                           ("uv.exe", "uvx.exe"), tmp / "partial")
        check("a bundle missing a member is refused", False, "it was accepted")
    except ValueError as e:
        check("a bundle missing a member is refused",
              "not in the archive" in str(e), str(e)[:120])
    check("and nothing was written for it",
          not (tmp / "partial").exists()
          or not any((tmp / "partial").iterdir()),
          "a half-unpacked runtime was left on disk")

    try:
        uv.extract_members(make_zip(["uv.exe", "uvx.exe"], size=64),
                           ("uv.exe", "uvx.exe"), tmp / "tiny")
        check("an implausibly small member is refused", False, "it was written")
    except ValueError as e:
        check("an implausibly small member is refused",
              "bytes" in str(e), str(e)[:120])

    # ---- 4. verification -------------------------------------------------
    asset = asset_for(good)
    check("a matching sha256 passes",
          uv.verify_download(good, asset)
          == hashlib.sha256(good).hexdigest(), "")
    try:
        uv.verify_download(good + b"x", asset)
        check("a mismatched sha256 is refused", False, "it was accepted")
    except ValueError:
        check("a mismatched sha256 is refused", True, "")
    try:
        uv.verify_download(good, asset_for(good, digest=False) | {"size": 0})
        check("nothing to verify against is refused", False, "it was accepted")
    except ValueError:
        check("nothing to verify against is refused", True, "")

    # ---- 5. a whole install, with the network stubbed --------------------
    for name in ("uv.exe", "uvx.exe"):
        (app / name).unlink()
    (app / uv.PROVENANCE).unlink(missing_ok=True)

    uv.resolve_asset = lambda repo, suffix=None: asset
    uv.fetch_bytes = lambda url, on_progress=None, timeout=300: good
    uv.download_all()

    state = uv.status()
    check("the install lands where the app looks for it",
          state["available"] is True and state["path"] == str(app / "uvx.exe"),
          str(state)[:200])
    check("with both binaries present",
          (app / "uv.exe").is_file() and (app / "uvx.exe").is_file(),
          str(sorted(p.name for p in app.iterdir())))
    check("and records the release it came from",
          state["own"]["installed"] is True
          and state["own"]["version"] == "0.9.9", str(state["own"]))
    check("and the provenance keeps the digest it verified",
          uv.provenance().get("binaries", {}).get("uvx.exe", {}).get("sha256")
          == hashlib.sha256(good).hexdigest(), str(uv.provenance())[:200])
    check("and the market can now see uvx as available",
          __import__("backend.mcp_client.market", fromlist=["x"])
          ._runtime_available("uvx") is True,
          "the market would still refuse every PyPI server")

    # ---- 6. a failed install leaves nothing to run -----------------------
    for name in ("uv.exe", "uvx.exe", uv.PROVENANCE):
        (app / name).unlink(missing_ok=True)
    uv.fetch_bytes = lambda url, on_progress=None, timeout=300: good + b"x"
    uv.download_all()
    check("a tampered download leaves no binary behind",
          not (app / "uvx.exe").exists(),
          "an unverified binary was left in place")
    check("and no provenance claiming a successful install",
          uv.provenance() == {}, str(uv.provenance())[:160])
    check("and says what went wrong", bool(uv.status()["error"]),
          str(uv.status())[:160])

    # ---- 7. it refuses what it should ------------------------------------
    out = await uv.install(remote=True)
    check("a remote session cannot install a runtime",
          out.get("success") is False and "machine itself" in str(out.get("error", "")),
          str(out)[:160])

    uv.fetch_bytes = lambda url, on_progress=None, timeout=300: good
    out = await uv.install()
    if out.get("success"):
        await uv.wait(timeout=60)
        out = await uv.install()
    check("installing again reports it is already there",
          out.get("success") is False and out.get("already_installed") is True,
          str(out)[:160])


def main() -> int:
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=300))
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: the suite raised: {e!r}")
        return 1
    if fails:
        print(f"FAIL: {len(fails)} uv check(s) failed")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: uv found, verified, installed as a pair, or refused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
