# Code — code mode engine (workspace, indexer, diff, lang detect)

"""
Workspace containment for the code handlers.

Every path those handlers receive is meant to be relative to a workspace the
user bound. ``os.path.join`` does not enforce that: a ``filePath`` of
``..\\..\\..\\Windows\\System32\\drivers\\etc\\hosts`` resolves outside the
workspace and the read succeeds, and when no workspace was sent at all the
handlers used the path as-is, so an absolute path was honoured too. Both were
reachable from a remote (Tailscale) session — ``code.*`` is not in the remote
policy's forbidden list — and ``code.apply`` takes a content argument, which
made it an arbitrary-file-write primitive.
"""

from __future__ import annotations

import os
from pathlib import Path

# Refuse anything larger than this coming from the editor.
MAX_EDIT_BYTES = 2_000_000


class OutsideWorkspace(ValueError):
    """The requested path resolves outside the bound workspace."""


def resolve_in_workspace(folder: str | os.PathLike[str],
                         file_path: str) -> Path:
    """Resolve ``file_path`` under ``folder``, refusing anything that escapes.

    Both sides are resolved before they are compared, so ``..`` segments and
    symlinks pointing out of the tree are already collapsed — comparing the raw
    strings would miss both.
    """
    root = Path(folder).expanduser().resolve(strict=False)
    candidate = Path(str(file_path))
    resolved = (candidate if candidate.is_absolute()
                else root / candidate).resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise OutsideWorkspace(
            f"'{file_path}' is outside the bound workspace")
    return resolved


def relative_to_workspace(folder: str, target: Path) -> str:
    """The path to show the UI: forward slashes, relative to the workspace."""
    try:
        root = Path(folder).expanduser().resolve(strict=False)
        return str(target.relative_to(root)).replace(os.sep, "/")
    except ValueError:
        return str(target)

