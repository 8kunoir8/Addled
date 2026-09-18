"""
One place that decides which files Addled may touch.

Until now `safety.file_access_mode` and `safety.allowed_folders` existed, the
Safety page showed a "File Access" dropdown for the mode, and **nothing read
any of it** — every file tool handed its path straight to the OS. There was
also no folder to be confined to: the Code page asked for one per session and
forgot it, and the indexer used a separate `project.roots` list.

The rule is deliberately conservative: confinement only applies once a folder is
actually configured. An unset workspace means "exactly as before" rather than
"deny everything", so adding this cannot break a working install.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("addled.workspace")

MODES = ("workspace_only", "custom", "unrestricted")

MODE_LABELS = {
    "workspace_only": "Workspace only",
    "custom": "Workspace + extra folders",
    "unrestricted": "Unrestricted",
}


def _expand(value) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Path(os.path.expandvars(os.path.expanduser(text))).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def root() -> Path | None:
    """The workspace folder, or None when it has not been chosen."""
    try:
        from backend.config import config
        return _expand(config.get("workspace", "root", default=""))
    except Exception:
        return None


def extra_dirs() -> list[Path]:
    out: list[Path] = []
    try:
        from backend.config import config
        values = list(config.get("workspace", "extra_dirs", default=[]) or [])
        # The Safety page has always had this list with nothing using it, so
        # honour it rather than making anyone re-enter what they typed there.
        values += list(config.get("safety", "allowed_folders", default=[]) or [])
    except Exception:
        values = []
    for value in values:
        path = _expand(value)
        if path:
            out.append(path)
    return out


def mode() -> str:
    try:
        from backend.config import config
        value = str(config.get("safety", "file_access_mode",
                               default="workspace_only") or "").strip().lower()
    except Exception:
        value = "workspace_only"
    return value if value in MODES else "workspace_only"


def allowed_roots() -> list[Path]:
    """Folders file tools may work in. Empty means nothing is configured."""
    primary = root()
    roots = [primary] if primary else []
    if mode() == "custom":
        roots += extra_dirs()
    elif not primary:
        # workspace_only, but no folder chosen: the extra list is all we have.
        roots += extra_dirs()
    seen: set[str] = set()
    unique: list[Path] = []
    for path in roots:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def check(path: Path) -> str | None:
    """Reason the path is out of bounds, or None if it is fine."""
    if mode() == "unrestricted":
        return None
    roots = allowed_roots()
    if not roots:
        # Nothing configured: behaving as before is safer than denying every
        # file operation to someone who never asked for confinement.
        return None
    for base in roots:
        try:
            if path == base or path.is_relative_to(base):
                return None
        except (OSError, ValueError):
            continue
    shown = ", ".join(str(r) for r in roots[:3]) + (" …" if len(roots) > 3 else "")
    return (f"Blocked: {path} is outside the workspace ({shown}). Set the "
            f"workspace folder in Settings → Workspace, add it there, or set "
            f"File Access to Unrestricted.")


def resolve(path) -> tuple[Path | None, str | None]:
    """Absolute path for a model- or user-supplied one, or an error string.

    Relative paths resolve against the workspace, so "notes.md" means the file
    in the folder the user picked rather than one next to the process.
    """
    text = str(path or "").strip()
    if not text:
        return None, "No path given."
    try:
        candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    except (OSError, RuntimeError, ValueError) as e:
        return None, f"Unusable path: {e}"

    if not candidate.is_absolute():
        base = root() or Path.cwd()
        candidate = base / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        resolved = Path(os.path.abspath(str(candidate)))

    reason = check(resolved)
    if reason:
        return None, reason
    return resolved, None


def describe() -> dict:
    """What the Settings page and diagnostics need to show."""
    primary = root()
    roots = allowed_roots()
    return {
        "root": str(primary) if primary else "",
        "configured": primary is not None,
        "mode": mode(),
        "mode_label": MODE_LABELS.get(mode(), mode()),
        "extra_dirs": [str(p) for p in extra_dirs()],
        "allowed": [str(p) for p in roots],
        "enforced": mode() != "unrestricted" and bool(roots),
        "note": ("" if primary else
                 "No workspace folder set — file tools are unrestricted and "
                 "relative paths resolve against the process directory."),
    }
