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
        # `backend/memory/` is unusual: it holds the memory *modules* (the .py
        # files that are part of the app) alongside the state they read and
        # write. Only the state should move. Copying the modules would litter
        # the data directory with a stale second copy of the code — harmless,
        # because nothing imports from there, but confusing to anyone who looks.
        if item.suffix in (".py", ".pyc", ".pyo"):
            continue
        if "__pycache__" in item.parts:
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


# ---- optional packages the app installs for itself ---------------------------

# Where an on-demand `pip install` puts what it downloads.
#
# The same trap as the data directory, one layer deeper: the "Install torch +
# transformers" button ran
#     pip install --target <install>/python/Lib/site-packages ...
# and under a per-machine install that target is `Program Files`, which is not
# writable. pip downloaded the whole ~2.5 GB, then died at the final copy:
#     PermissionError: [WinError 5] Access is denied: ...\site-packages\einops
# and the UI put the button back with no error, so it looked like nothing had
# happened. Installing into the writable data directory avoids that.
PYLIBS_DIR = DATA_DIR / "pylibs"


def _dist_info_names(directory: Path) -> set[str]:
    """Package names with a `*.dist-info` in `directory`, normalised.

    `-` and `_` mean the same thing to a wheel, so `huggingface_hub` and
    `huggingface-hub` are one name here.
    """
    out: set[str] = set()
    try:
        for entry in directory.glob("*-*.dist-info"):
            stem = entry.name[: -len(".dist-info")]
            name, _, version = stem.rpartition("-")
            if name and version:
                out.add(name.lower().replace("_", "-"))
    except OSError:
        pass
    return out

def _duplicated_in_pylibs() -> set[str]:
    """Packages present in BOTH `pylibs` and the bundled site-packages.

    These are the ones where precedence matters, and `pylibs` must win.

    Why `pylibs`, and not "whichever is newer". The bundled site-packages is a
    snapshot taken when the installer was built; `pylibs` is filled at runtime
    by the vision and browser installers, which resolve their own dependencies
    together. When the two disagree, the bundled copy is the one that predates
    the other installs — and it is not merely older, it CONTRADICTS them.
    Measured on a real install, every package where the bundled copy was newer
    was pinned by a `pylibs` package to the `pylibs` version:

        browser_use     pypdf==6.16.2        bundled has 6.19.0
        browser_use     requests==2.33.0     bundled has 2.34.2
        browser_use     ollama==0.6.1        bundled has 0.6.2
        browser_harness websockets==15.0.1   bundled has 17.0.1
        browser_use     google-auth==2.48.0  bundled has 2.56.3

    `google_genai` wants `websockets <17.0`, which the bundled 17.0.1 violates
    outright. So "newer wins" would pick the incompatible copy, and the older
    one is correct.

    The bug this exists to fix, in the log on every start:

        transformers embedder 'minilm' failed (cannot import name 'httpx' from
        'huggingface_hub.utils' (...site-packages\\huggingface_hub\\utils\\...))

    The bundled `huggingface_hub` 1.27.0 does not export `httpx`; the `pylibs`
    1.33.0 does. `transformers` lives ONLY in `pylibs` and needs the newer one,
    so it failed to import at all and the embedding backend silently degraded to
    hashed n-grams.

    This used to append `pylibs` unconditionally, on the principle that whatever
    ships must keep winning. That is the right instinct for a package the app
    genuinely depends on and a user fetch might downgrade — but it is the wrong
    answer when the shipped snapshot is the stale side of the pair.
    """
    bundled = _dist_info_names(Path(sys.prefix) / "Lib" / "site-packages")
    return _dist_info_names(PYLIBS_DIR) & bundled

def add_pylibs_to_path() -> bool:
    """Put `PYLIBS_DIR` on `sys.path` so an install there can be imported.

    Needed because the app runs its interpreter with ``-s``, which excludes user
    site-packages; the only directories it searches are inside the install, and
    under a per-machine install none of those can be written to. A directory pip
    can write into is therefore only half the fix — it must also be somewhere
    the interpreter looks.

    Normally APPENDED, so a package shipped with the app keeps winning over an
    optional one fetched later. The exception is a name that exists in both
    directories: there the `pylibs` copy must come first, or the bundled copy
    shadow it. See `_duplicated_in_pylibs`. Only when such a name exists is
    `PYLIBS_DIR` inserted ahead of the bundled site-packages — a fresh install
    with nothing installed yet is left exactly as it was.

    Idempotent. Returns True when the directory is on the path.
    """
    if not PYLIBS_DIR.is_dir():
        try:
            PYLIBS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False

    entry = str(PYLIBS_DIR)
    duplicated = _duplicated_in_pylibs()
    if duplicated:
        # A name in both places: the pylibs copy has to be found first. This
        # cannot be done per package — putting `pylibs/pkg/` on sys.path makes
        # Python look for `pkg` INSIDE it, which is a ModuleNotFoundError — so
        # the directory itself moves ahead of the bundled site-packages.
        if entry in sys.path:
            sys.path.remove(entry)
        # Ahead of the first bundled site-packages, but after the install root
        # and anything the caller put there deliberately.
        target = 0
        for i, p in enumerate(sys.path):
            if "site-packages" in p and "pylibs" not in p:
                target = i
                break
            target = i + 1
        sys.path.insert(target, entry)
        log.info("pylibs precedes the bundled site-packages for %d package(s): %s",
                 len(duplicated), ", ".join(sorted(duplicated)[:8]))
    elif entry not in sys.path:
        sys.path.append(entry)

    # `find_spec` caches its misses per (name, path), and an import that already
    # resolved to the bundled copy stays resolved — so the caches are dropped
    # whether or not the order changed.
    try:
        import importlib

        importlib.invalidate_caches()
    except Exception:  # noqa: BLE001 - never fatal
        pass
    return True


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
