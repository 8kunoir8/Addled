"""Where Addled keeps the state it writes.

Why this module exists
----------------------

Every writable thing this app owns — the log, `settings.json`, the memory
databases, the wiki, downloaded models, learned skills — used to be resolved
independently as ``Path(__file__).parent.parent / "memory"``. That is correct
only while the app lives somewhere the user can write. It stopped being true the
moment the installer defaulted to ``C:\\Program Files\\Addled``: the backend
died on its first act, `_setup_logging()`:

    PermissionError: [Errno 13] Permission denied:
    'C:\\Program Files\\Addled\\resources\\backend\\memory\\addled.log'

and the character sat there saying "Backend not running". A per-machine install
is a normal thing for a user to choose, so "portable, writes beside itself" was
never a safe assumption — it was an assumption that happened to hold while the
only install location was per-user.

The rule
--------

Write where writing is allowed, preferring the install directory when it is
already writable so that an existing portable or per-user setup keeps working
exactly as it did:

1. ``ADDLED_DATA_DIR``, when set. This is the documented override, used by the
   tests and by anyone who wants state somewhere specific.
2. ``<install>/memory``, when it is actually writable. Existing users stay put.
3. ``<LOCALAPPDATA>/Addled`` on Windows, ``~/.local/share/addled`` elsewhere.

The check in step 2 is a real write, not ``os.access``. On Windows that
distinction is the whole bug: ``os.access`` consults the read-only bit and
reports ``Program Files`` as writable, because the denial comes from the ACL and
from UAC's virtualisation, neither of which it looks at. A test write is the only
answer that is true.

Reads fall back to the install directory
----------------------------------------

An install that upgrades from "writes beside itself" to "writes to
%LOCALAPPDATA%" must not appear to forget everything. ``migrate()`` copies the
old tree across once, on first run, and leaves the original in place so a
downgrade still finds it. Nothing is deleted.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger("addled.paths")

# The environment variable is named for what every module already wanted: the
# one place the per-instance data lives. It was previously *set* by main.py to
# the install path, which is what this module now decides instead.
ENV_VAR = "ADDLED_DATA_DIR"

# The install-time directory. Present in a source checkout too (``backend/``),
# which is what keeps the dev tree working unchanged.
INSTALL_DIR = Path(__file__).resolve().parent

# Where state used to live, before this module existed.
LEGACY_DIR = INSTALL_DIR / "memory"


def _writable(directory: Path) -> bool:
    """True only if a file can actually be created in `directory`.

    Not ``os.access``: see the module docstring. The probe file is removed
    either way, and a failure at any point means "not writable", which is the
    safe direction to be wrong in.
    """
    probe = directory / ".addled-write-probe"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _user_data_dir() -> Path:
    """The per-user location to fall back to."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "Addled"
    # Linux and macOS: XDG, then the plain home fallback.
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "addled"
    return Path(os.path.expanduser("~")) / ".local" / "share" / "addled"


def _resolve_data_dir() -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        # An explicit choice is honoured without a probe: if it is wrong, the
        # resulting error should name the path the user asked for rather than
        # silently redirecting somewhere they did not choose.
        return Path(override).expanduser().resolve()

    if _writable(LEGACY_DIR):
        return LEGACY_DIR

    fallback = _user_data_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    log.warning(
        "the install directory is not writable (%s); using %s for Addled's data",
        LEGACY_DIR, fallback,
    )
    return fallback


DATA_DIR = _resolve_data_dir()
MEMORY_DIR = DATA_DIR

# Publish it back so a child process, a bot bridge or a script sees the same
# answer this process computed. `setdefault` would be wrong here: this module is
# the authority, and a stale inherited value is exactly the confusion the
# resolver exists to remove.
os.environ[ENV_VAR] = str(DATA_DIR)


def migrate() -> int:
    """Copy a pre-existing install-local `memory/` into the resolved data dir.

    Only when they differ, and only once — a marker in the destination makes the
    second call a no-op, so a run that is interrupted midway can simply be run
    again. Existing files in the destination are never overwritten: the
    destination is the live one, and a stale copy must not clobber it.

    Returns the number of files copied, for the log and for tests.
    """
    source = LEGACY_DIR
    if source.resolve() == DATA_DIR.resolve() or not source.is_dir():
        return 0

    marker = DATA_DIR / ".migrated-from-install"
    if marker.exists():
        return 0

    copied = 0
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        target = DATA_DIR / item.relative_to(source)
        # Never take a file that is already there. The destination has been in
        # use since this version started; an older copy is not an improvement.
        if target.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
        except OSError as e:  # noqa: PERF203 - one bad file must not stop the rest
            log.debug("could not migrate %s: %s", item, e)

    try:
        marker.write_text(
            f"copied {copied} file(s) from {source}\n", encoding="utf-8"
        )
    except OSError as e:
        log.debug("could not write the migration marker: %s", e)

    if copied:
        log.info("migrated %d file(s) from %s to %s", copied, source, DATA_DIR)
    return copied


def subdir(*parts: str) -> Path:
    """A writable subdirectory of the data dir, created on demand.

    Every module that used to write ``memory/<something>`` should call this
    instead, so there is one definition of where that is.
    """
    path = MEMORY_DIR.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def describe() -> dict:
    """Diagnostics for the Settings page and for `addled.paths` in a bug report."""
    return {
        "data_dir": str(DATA_DIR),
        "install_dir": str(INSTALL_DIR),
        "legacy_dir": str(LEGACY_DIR),
        "writable": _writable(DATA_DIR),
        "portable": DATA_DIR == LEGACY_DIR,
        "override": os.environ.get("ADDLED_DATA_DIR") or "",
    }
