"""
Resumable HTTPS downloads for local models and runtimes.

Streams into ``<dest>.part`` and renames atomically on success, so an interrupted
download never leaves a half-written file that looks complete. Re-running resumes
from the existing ``.part`` offset with an HTTP Range request (and detects servers
that ignore Range by falling back to a clean restart).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("addled.download")

CHUNK = 1024 * 256
PROGRESS_INTERVAL_S = 0.5
DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=15.0, read=None)

# (phase, downloaded_bytes, total_bytes) with phase in
# "downloading" | "cached" | "done"
ProgressCb = Callable[[str, int, int], None]


class DownloadCancelled(Exception):
    """Raised when the caller's cancel event is set mid-download."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def download(
    url: str,
    dest: Path | str,
    *,
    expected_sha256: str = "",
    on_progress: ProgressCb | None = None,
    resume: bool = True,
    cancel: asyncio.Event | None = None,
) -> Path:
    """Download ``url`` to ``dest``, resuming a previous ``.part`` when possible."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.is_file() and dest.stat().st_size > 0:
        if not expected_sha256 or sha256_of(dest) == expected_sha256:
            size = dest.stat().st_size
            if on_progress:
                on_progress("cached", size, size)
            return dest
        log.warning("Checksum mismatch on existing %s — re-downloading", dest.name)
        dest.unlink(missing_ok=True)

    offset = 0
    if part.is_file():
        if resume:
            offset = part.stat().st_size
        else:
            part.unlink(missing_ok=True)

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    if offset:
        log.info("Resuming %s at %.1f MB", dest.name, offset / 1024 / 1024)
    else:
        log.info("Downloading %s from %s", dest.name, url)

    async with httpx.AsyncClient(follow_redirects=True,
                                 timeout=DEFAULT_TIMEOUT) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            if offset and resp.status_code == 200:
                # Server ignored Range — start clean to avoid a corrupt file.
                log.info("Server ignored Range for %s — restarting download", dest.name)
                offset = 0
                headers = {}

            total = int(resp.headers.get("Content-Length") or 0) + offset
            done = offset
            last_emit = 0.0
            mode = "ab" if offset else "wb"
            with part.open(mode) as handle:
                async for chunk in resp.aiter_bytes(CHUNK):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    handle.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if on_progress and (now - last_emit >= PROGRESS_INTERVAL_S
                                        or (total and done >= total)):
                        last_emit = now
                        on_progress("downloading", done, total)

    if expected_sha256:
        actual = sha256_of(part)
        if actual != expected_sha256:
            raise ValueError(
                f"Checksum mismatch for {dest.name}: expected "
                f"{expected_sha256[:12]}…, got {actual[:12]}…"
            )

    os.replace(part, dest)
    size = dest.stat().st_size
    log.info("Download complete: %s (%.1f MB)", dest.name, size / 1024 / 1024)
    if on_progress:
        on_progress("done", size, size)
    return dest


def has_space(needed_mb: int, margin_mb: int = 500) -> tuple[bool, int]:
    """Return (ok, free_mb) for a download that needs ``needed_mb``."""
    from backend.local_models.paths import disk_free_mb
    free = disk_free_mb()
    return free >= (int(needed_mb) + int(margin_mb)), free
