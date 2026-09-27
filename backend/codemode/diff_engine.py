"""
Diff engine — unified diff generation, apply, and revert.
"""

from __future__ import annotations

import difflib
import logging
import os
import shutil

log = logging.getLogger("addled.codemode.diff")

def generate_diff(original: str, modified: str, filepath: str = "file") -> dict:
    """Generate a unified diff between original and modified text."""
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        original_lines, modified_lines,
        fromfile=f"a/{filepath}", tofile=f"b/{filepath}",
        lineterm="",
    ))

    hunks = []
    current_hunk = None

    for line in diff:
        if line.startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"header": line, "lines": []}
        elif current_hunk is not None:
            current_hunk["lines"].append(line)

    if current_hunk:
        hunks.append(current_hunk)

    return {
        "file": filepath,
        "hunks": hunks,
        "added": sum(1 for l in diff if l.startswith("+") and not l.startswith("+++")),
        "removed": sum(1 for l in diff if l.startswith("-") and not l.startswith("---")),
        "raw_diff": "\n".join(diff),
    }

def apply_content(filepath: str, content: str, backup: bool = True,
                  create: bool = False) -> dict:
    """Replace a file's full content (with optional .bak backup).

    Pure Python — does not depend on the unix `patch` utility. ``create`` lets
    the editor save a file that does not exist yet; without it a missing path is
    an error, which is what the diff-review path wants.
    """
    try:
        exists = os.path.exists(filepath)
        if not exists and not create:
            return {"success": False, "error": f"File not found: {filepath}"}

        backup_path = None
        if backup and exists:
            backup_path = filepath + ".bak"
            shutil.copy2(filepath, backup_path)

        parent = os.path.dirname(filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(filepath, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return {"success": True, "backup": backup_path, "created": not exists}
    except Exception as e:
        return {"success": False, "error": str(e)}

def apply_diff(filepath: str, diff_text: str, backup: bool = True) -> dict:
    """Apply a unified diff to a file. Optionally creates a backup."""
    try:
        if not os.path.exists(filepath):
            return {"success": False, "error": f"File not found: {filepath}"}

        with open(filepath, "r", encoding="utf-8") as f:
            original = f.read()

        # Create backup
        if backup:
            backup_path = filepath + ".bak"
            shutil.copy2(filepath, backup_path)

        # Apply using patch utility or manual approach
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as tmp:
            tmp.write(diff_text)
            diff_path = tmp.name

        import subprocess
        result = subprocess.run(
            ["patch", "-u", "--force", filepath, diff_path],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        os.unlink(diff_path)

        if result.returncode == 0:
            return {"success": True, "backup": backup_path if backup else None}
        return {"success": False, "error": result.stderr.strip()}
    except Exception as e:
        return {"success": False, "error": str(e)}

def revert_diff(filepath: str, backup_path: str | None = None) -> dict:
    """Revert a file to its backup or original state."""
    try:
        bak = backup_path or filepath + ".bak"
        if not os.path.exists(bak):
            return {"success": False, "error": f"No backup found: {bak}"}
        shutil.copy2(bak, filepath)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
