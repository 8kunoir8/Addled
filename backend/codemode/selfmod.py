"""
Self-modification — changing Addled's own source, safely.

Why this is separate from the Skill Forge
-----------------------------------------
The forge writes a NEW module into `memory/forged_skills/`. That is additive, it
is loaded by name, and a broken one simply fails to register. Nothing there can
stop Addled starting.

Changing `backend/` itself is the opposite: it is the code that is already
running, it is loaded before any of this, and a bad edit does not degrade a
feature — it stops the app from starting at all. So this module is built around
three rules, and every one of them is enforced in code rather than asked for in
a prompt:

1. **Nothing is applied from a tool call.** `propose()` writes a patch to a
   staging area and returns a token. `apply()` requires that token AND an
   explicit confirmation, so a model cannot rewrite its own runtime in the
   middle of a turn even if it tries.
2. **Nothing is applied without a backup and a revert path.** The original file
   is copied into the staging area first, and `revert()` puts it back. `apply()`
   also syntax-checks the result — a file that will not parse is never written,
   because that is precisely the change that would make Addled unstartable.
3. **A guard list is refused outright.** Files that decide what is allowed
   (`safety/`, the config loader, this module) cannot be self-edited at all. An
   agent that can rewrite its own permission checks does not have any.

Everything is confined to `backend/`, and every path is contained — `..` cannot
escape, and an absolute path is refused. The change is also recorded in the
episodic journal, so what Addled did to itself is in its own history.
"""

from __future__ import annotations

import ast
import json
import logging
import shutil
import sys
import time
import uuid
from pathlib import Path

log = logging.getLogger("addled.selfmod")

# Staged proposals live here, next to the memory they are recorded in.
from backend import app_paths

STAGE_DIR = app_paths.subdir("selfmod")

# Paths that may never be self-edited, whatever the instruction says. These are
# the files that decide what is permitted, or that load everything else — a
# change to any of them can turn "denied" into "allowed".
_GUARDED = (
    "safety/",            # the destruction gate and the rate limiter
    "selfmod.py",         # this module — the rules themselves
    "config.py",          # decides which providers/settings exist
    "main.py",            # the entry point that loads all of it
    "actions/executor.py",  # the approval path for dangerous actions
)

MAX_PATCH_BYTES = 400_000

def _backend_root() -> Path:
    # `backend/` is the package this module lives in.
    return Path(__file__).resolve().parent.parent

def _resolve(rel_path: str) -> tuple[Path | None, str]:
    """Resolve a path under `backend/`, refusing anything outside it.

    Returns (path, error). `error` is non-empty when the path is not allowed —
    the caller turns it into the tool result.
    """
    rel = str(rel_path or "").strip().replace("\\", "/")
    if not rel:
        return None, "No file named."
    if rel.startswith("/") or ":" in rel.split("/")[0]:
        return None, "Give a path relative to backend/, not an absolute one."
    root = _backend_root()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, "That path is outside backend/."
    norm = rel.lstrip("./")
    for guard in _GUARDED:
        if guard.endswith("/"):
            # A directory guard covers everything beneath it.
            if norm.startswith(guard):
                return None, (f"'{norm}' is under '{guard}', which is on the "
                              "no-self-edit list: it decides what is permitted. "
                              "Edit it by hand.")
        elif norm == guard or norm.endswith("/" + guard):
            return None, (f"'{norm}' is on the no-self-edit list: it decides "
                          "what is permitted, so it cannot be changed by the "
                          "agent. Edit it by hand.")
    if candidate.suffix != ".py":
        return None, "Only .py files under backend/ can be self-edited."
    return candidate, ""

def propose(rel_path: str, new_content: str, reason: str = "") -> dict:
    """Stage a change to a core file. Writes nothing to the real file.

    Returns a token the caller must present to `apply()`. The proposal is on
    disk in the staging area so it survives a restart and so the user can look
    at it before agreeing.
    """
    target, err = _resolve(rel_path)
    if err:
        return {"success": False, "error": err}
    if not target.is_file():
        return {"success": False,
                "error": f"{rel_path} does not exist under backend/. Creating "
                         "new core modules is not supported — use the forge."}
    if len(new_content) > MAX_PATCH_BYTES:
        return {"success": False,
                "error": f"The proposed file is {len(new_content):,} bytes, "
                         f"above the {MAX_PATCH_BYTES:,} limit."}

    # A proposal that will not parse must never reach the disk: this is the one
    # change guaranteed to make Addled unstartable.
    try:
        ast.parse(new_content)
    except SyntaxError as e:
        return {"success": False,
                "error": f"The proposed file does not parse: {e.msg} "
                         f"(line {e.lineno}). Refused before anything is "
                         "written."}

    original = target.read_text(encoding="utf-8", errors="replace")
    if original.strip() == new_content.strip():
        return {"success": False, "error": "The proposal is identical to the "
                                           "file — nothing to do."}

    token = uuid.uuid4().hex[:12]
    work = STAGE_DIR / token
    work.mkdir(parents=True, exist_ok=True)
    (work / "original.py").write_text(original, encoding="utf-8")
    (work / "proposed.py").write_text(new_content, encoding="utf-8")
    (work / "meta.json").write_text(json.dumps({
        "token": token,
        "relPath": str(target.relative_to(_backend_root())).replace("\\", "/"),
        "absPath": str(target),
        "reason": reason,
        "created": time.time(),
        "originalBytes": len(original),
        "proposedBytes": len(new_content),
        "originalSha": _sha(original),
    }, indent=2), encoding="utf-8")

    import difflib
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel_path}", tofile=f"b/{rel_path}"))
    return {
        "success": True,
        "token": token,
        "path": rel_path,
        "reason": reason,
        "diff": diff[:20_000],
        "added": sum(1 for l in diff.splitlines()
                     if l.startswith("+") and not l.startswith("+++")),
        "removed": sum(1 for l in diff.splitlines()
                       if l.startswith("-") and not l.startswith("---")),
        "message": ("Staged, not applied. This changes Addled's own code, so it "
                    "takes effect only after the user confirms and the app is "
                    "restarted. Show them the diff and ask."),
    }

def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]

def list_pending() -> list[dict]:
    """Every staged proposal, newest first."""
    out: list[dict] = []
    if not STAGE_DIR.is_dir():
        return out
    for entry in STAGE_DIR.iterdir():
        meta = entry / "meta.json"
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["ageSeconds"] = int(time.time() - float(data.get("created") or 0))
        out.append(data)
    out.sort(key=lambda d: d.get("created") or 0, reverse=True)
    return out

def revert(token: str) -> dict:
    """Put a staged change's original file back and drop the proposal.

    Usable both before applying (to abandon it) and after (to undo it), which is
    why the original is kept until the proposal is discarded explicitly.
    """
    work = STAGE_DIR / str(token or "")
    meta_path = work / "meta.json"
    if not meta_path.is_file():
        return {"success": False, "error": f"No staged change '{token}'."}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"success": False, "error": f"Unreadable proposal: {e}"}

    target = Path(meta.get("absPath") or "")
    original = (work / "original.py").read_text(encoding="utf-8")
    try:
        target.write_text(original, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": f"Could not restore {target}: {e}"}
    shutil.rmtree(work, ignore_errors=True)
    log.info("Self-mod reverted: %s", meta.get("relPath"))
    _journal("reverted", meta)
    return {"success": True, "path": meta.get("relPath"),
            "message": (f"Restored {meta.get('relPath')} to its original "
                        "contents. A restart applies it.")}

def discard(token: str) -> dict:
    """Throw a proposal away without touching the real file."""
    work = STAGE_DIR / str(token or "")
    meta_path = work / "meta.json"
    if not meta_path.is_file():
        return {"success": False, "error": f"No staged change '{token}'."}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    shutil.rmtree(work, ignore_errors=True)
    return {"success": True, "path": meta.get("relPath"),
            "message": "Proposal discarded. The file was never changed."}

def apply(token: str, confirm: bool = False,
          allow_dirty: bool = False) -> dict:
    """Apply a staged change to the real file.

    Requires `confirm=True`. That flag is the whole point of the staging step:
    the model cannot reach this without the user having said yes, because the
    proposal carries the diff and the diff is what the user is shown.

    The file's contents are re-checked against what was staged. If the file has
    changed since the proposal was made, the apply is refused unless
    `allow_dirty` is set — silently overwriting an edit the user just made is
    the one outcome worse than not applying.
    """
    if not confirm:
        return {"success": False,
                "error": ("Refused: applying changes to Addled's own code needs "
                          "explicit confirmation. Show the diff and ask the "
                          "user first.")}
    work = STAGE_DIR / str(token or "")
    meta_path = work / "meta.json"
    if not meta_path.is_file():
        return {"success": False, "error": f"No staged change '{token}'."}
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"success": False, "error": f"Unreadable proposal: {e}"}

    target = Path(meta.get("absPath") or "")
    proposed_path = work / "proposed.py"
    if not proposed_path.is_file() or not target.is_file():
        return {"success": False, "error": "The staged proposal is incomplete."}
    proposed = proposed_path.read_text(encoding="utf-8")

    current = target.read_text(encoding="utf-8", errors="replace")
    if not allow_dirty and _sha(current) != meta.get("originalSha"):
        return {"success": False,
                "error": ("The file changed after this proposal was made, so "
                          "applying it would overwrite a newer edit. Re-propose "
                          "against the current contents.")}

    # Belt and braces: the proposal was checked when staged, but a staged file
    # could have been edited on disk since.
    try:
        ast.parse(proposed)
    except SyntaxError as e:
        return {"success": False,
                "error": f"The staged file does not parse: {e.msg}. Not applied."}

    backup = work / "applied_backup.py"
    try:
        shutil.copy2(target, backup)
        target.write_text(proposed, encoding="utf-8")
    except OSError as e:
        return {"success": False, "error": f"Could not write {target}: {e}"}

    log.warning("Self-mod applied: %s (restart to load)", meta.get("relPath"))
    _journal("applied", meta)
    return {
        "success": True,
        "path": meta.get("relPath"),
        "restartRequired": True,
        "token": token,
        "message": (f"Applied to {meta.get('relPath')}. It does NOT take effect "
                    "until Addled restarts. Use `selfrevert` with this token to "
                    "undo it, then restart again."),
    }

def _journal(kind: str, meta: dict) -> None:
    """Record the change in the episodic journal, best-effort."""
    try:
        from backend.memory.journal import record as journal_record
        journal_record("system",
                       f"[self-mod {kind}] {meta.get('relPath')} "
                       f"(token {meta.get('token')})")
    except Exception as e:  # noqa: BLE001
        log.debug("could not journal self-mod: %s", e)
