"""Fetching a release asset and proving it is the one we asked for.

Two tools now arrive this way — the token saver (`rtk.py`) and the `uv` runtime
that MCP servers packaged for PyPI are launched with (`uv.py`) — so the part that
decides whether bytes are trustworthy lives in one place instead of two.

What this guarantees, and what it does not, because the difference matters:

* The release is resolved through GitHub's own API over HTTPS, and the asset is
  fetched from the URL that API returned.
* The bytes are checked against the `sha256` digest the API publishes for the
  asset. If the API publishes none, the byte count it declares must match
  exactly. Neither present means the download is refused — never accepted
  unchecked — and nothing is put in place.
* This is **not** signature verification. There is no Authenticode check, so it
  does not defend against a compromised upstream release; it defends against a
  bad transfer and against a URL that is not the one we asked for.
  `backend/tailscale/installer.py` can afford to require a valid signature
  because it downloads from the vendor's own site; these are community releases
  on GitHub, and pretending otherwise would be worse than saying so.
* Nothing here runs on its own. No startup path installs, and nothing downloads
  without a click.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

log = logging.getLogger("addled.tools.download")

API_ROOT = "https://api.github.com/repos"
# Redirects from a release asset land on objects.githubusercontent.com, so both
# hosts have to be allowed. Anything else is refused rather than fetched.
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com"}

USER_AGENT = "Addled/1.0 (tool installer)"

# A debug-symbols-free rtk is ~9 MB, ripgrep ~4 MB and the uv bundle ~20 MB. The
# floor is what stops an HTML error page being written out as an executable; the
# ceiling stops a runaway download.
MIN_EXE_BYTES = 400 * 1024
MAX_EXE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024


def require_allowed(url: str) -> None:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing a non-https download: {url}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing a download from {parsed.hostname}")


def fetch_json(url: str, timeout: int = 60) -> dict:
    require_allowed(url)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_bytes(url: str, on_progress=None, timeout: int = 300) -> bytes:
    require_allowed(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError(f"archive is {total} bytes, larger than allowed")
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if received > MAX_ARCHIVE_BYTES:
                raise ValueError("archive exceeded the allowed size mid-download")
            if on_progress:
                on_progress(received, total)
    return b"".join(chunks)


def resolve_asset(repo: str, suffix: str) -> dict:
    """The release asset to use: url, size, digest, tag."""
    release = fetch_json(f"{API_ROOT}/{repo}/releases/latest")
    assets = release.get("assets") or []
    asset = next((a for a in assets if str(a.get("name", "")).endswith(suffix)),
                 None)
    if not asset:
        raise ValueError(f"no {suffix} asset in the latest {repo} release")
    url = str(asset.get("browser_download_url") or "")
    require_allowed(url)
    return {
        "repo": repo,
        "tag": str(release.get("tag_name") or ""),
        "name": str(asset.get("name") or ""),
        "url": url,
        "size": int(asset.get("size") or 0),
        # GitHub publishes `sha256:<hex>` here for newer releases. Absent on
        # older ones, in which case the declared byte count is all there is.
        "digest": str(asset.get("digest") or ""),
    }


def verify_download(archive: bytes, asset: dict) -> str:
    """Check the bytes against what the API said, and return the digest."""
    digest = hashlib.sha256(archive).hexdigest()
    published = asset.get("digest") or ""
    if published.startswith("sha256:"):
        expected = published.split(":", 1)[1].strip().lower()
        if expected != digest:
            raise ValueError(
                f"the download does not match the sha256 GitHub publishes for "
                f"it (expected {expected[:16]}…, got {digest[:16]}…)")
    elif asset.get("size"):
        if len(archive) != int(asset["size"]):
            raise ValueError(
                f"the download is {len(archive)} bytes, and the release says "
                f"{asset['size']}")
    else:
        # No digest and no size: still refuse to install something we cannot
        # describe. This has not been seen, and it fails closed if it happens.
        raise ValueError("the release gives no size or digest to verify against")
    return digest


def extract_members(archive: bytes, names: tuple[str, ...],
                    destination: Path,
                    min_bytes: int = MIN_EXE_BYTES) -> dict[str, Path]:
    """Lift named executables out of the archive into `destination`.

    Each is written to a `.part` beside its target and moved into place, so a
    failure never leaves a half-written executable for `find()` to hand out.
    All of them are checked before any is written, so a bundle that is missing
    one member changes nothing on disk.

    `min_bytes` is per-call because the floor that suits a 9 MB rtk is wrong for
    a launcher shim: uvx.exe is a few hundred kilobytes, and rejecting it would
    refuse a perfectly good install.
    """
    with zipfile.ZipFile(BytesIO(archive)) as bundle:
        found: dict[str, bytes] = {}
        for wanted in names:
            matches = [n for n in bundle.namelist()
                       if n.endswith(f"/{wanted}") or n == wanted]
            if not matches:
                raise ValueError(f"{wanted} is not in the archive")
            data = bundle.read(matches[0])
            if len(data) < min_bytes:
                raise ValueError(
                    f"{wanted} is only {len(data)} bytes, below the {min_bytes} "
                    "byte floor")
            if len(data) > MAX_EXE_BYTES:
                raise ValueError(
                    f"{wanted} is {len(data)} bytes, larger than allowed")
            found[wanted] = data

    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for wanted, data in found.items():
        target = destination / wanted
        staging = target.with_suffix(".part")
        staging.write_bytes(data)
        os.replace(staging, target)
        written[wanted] = target
    return written


def extract_exe(archive: bytes, exe_name: str, destination: Path) -> Path:
    """Take one exe out of the archive, refusing anything implausible."""
    return extract_members(archive, (exe_name,), destination.parent)[exe_name]
