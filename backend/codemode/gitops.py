"""
Git safety net — stage, commit, and revert the workspace around code edits.

Why this exists
---------------
The Code page writes real files. It has backups (`.bak` next to each file) and a
review step, but neither is a history: a `.bak` tells you the previous contents
of ONE file and nothing about a change that spanned three, and once two edits
land the older `.bak` is simply gone.

Git is the tool for this, and it is already on nearly every developer's machine.
Rather than a second, weaker mechanism, Addled uses it when it is there:

- **Before a plan is applied**, if the workspace is a git repo, the current
  state is committed (or at least recorded) so the whole plan is one revert away.
- **After a plan is applied**, a commit is made describing what changed, so
  "undo the agent's last change" is `git revert`, and the user's own history
  shows what the agent did and when.

Everything here is best-effort. A workspace that is not a repo, or a machine
without git, is not an error — the `.bak` backups still apply, and the caller is
told git was not used rather than being blocked. The one rule that IS enforced:
Addled never rewrites the user's existing history. It commits on top, and it
never force-pushes or resets anything the user did not ask it to.

This module shells out to `git` with argument lists (never a shell string), so a
filename with a space or a quote cannot turn into a second command.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("addled.codemode.git")

# Git can be slow on a large repo's first status; a longer wait than a shell
# command gets, but still bounded so a hung git cannot hang a code turn.
_TIMEOUT = 20

def _run(repo: str | Path, *args: str,
         raw: bool = False) -> tuple[bool, str]:
    """Run `git <args>` in `repo`. Returns (ok, output).

    Never raises: a missing git binary, a permission problem and a non-repo
    directory all come back as (False, reason), because every caller treats git
    as optional.

    `raw=True` keeps the output exactly as git wrote it. That matters because
    the default strips it, and `git status --porcelain` uses a FIXED TWO-COLUMN
    status field — a leading space is the "unchanged in the index" column, not
    padding. Stripping turned `" M app.py"` into `"M app.py"`, which shifted
    every field by one and reported the file as `"pp.py"`: the first two
    characters of the path, gone, and only ever on the first line. Only the
    callers that parse fixed columns need this; everything else wants the
    trimmed text.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        return False, f"git {args[0] if args else ''} timed out"
    except OSError as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    out = proc.stdout or ""
    if raw:
        # Trailing newlines only; the LEADING whitespace is data.
        return True, out.rstrip("\r\n")
    return True, out.strip()

def available() -> bool:
    """Whether a usable git is on PATH."""
    ok, _ = _run(".", "--version")
    return ok

def is_repo(path: str | Path) -> bool:
    """Whether `path` is inside a git work tree."""
    ok, out = _run(path, "rev-parse", "--is-inside-work-tree")
    return ok and out.strip() == "true"

def repo_root(path: str | Path) -> Path | None:
    """The top of the work tree containing `path`, or None."""
    ok, out = _run(path, "rev-parse", "--show-toplevel")
    if not ok or not out:
        return None
    return Path(out.strip())

def status(path: str | Path) -> dict:
    """A short summary of what is uncommitted, for the UI to show."""
    if not is_repo(path):
        return {"isRepo": False, "available": available()}
    # `raw=True`: the porcelain format is `XY<space>path` with a FIXED-width
    # two-character status field, so the leading space is data. Trimming it made
    # `" M app.py"` parse as `"M app.py"` and the path come back as `"pp.py"`.
    ok, out = _run(path, "status", "--porcelain", raw=True)
    if not ok:
        return {"isRepo": True, "error": out}
    changed, untracked = [], []
    for line in out.splitlines():
        if not line.strip():
            continue
        # Renames are reported as `old -> new`; the NEW name is the one that
        # exists now, so that is what the user should be shown and what a later
        # `git restore`/`read` needs.
        name = line[3:]
        if " -> " in name:
            name = name.rsplit(" -> ", 1)[-1]
        name = name.strip().strip('"')
        if not name:
            continue
        (untracked if line[:2] == "??" else changed).append(name)
    branch_ok, branch = _run(path, "rev-parse", "--abbrev-ref", "HEAD")
    return {
        "isRepo": True,
        "branch": branch if branch_ok else "",
        "changed": changed,
        "untracked": untracked,
        "clean": not changed and not untracked,
    }

def head_sha(path: str | Path) -> str:
    """The current commit, or "" when there is none (fresh repo)."""
    ok, out = _run(path, "rev-parse", "HEAD")
    return out if ok else ""

def snapshot(path: str | Path, message: str) -> dict:
    """Commit the current state so a later change is one revert away.

    Only tracked files are committed (`add -u`). New files are NOT added — a
    snapshot exists to protect what is already there, and sweeping unrelated
    untracked files into a commit the user did not ask for is not that.

    Returns {"ok", "sha", "reason"}. `ok` False with a reason is the normal
    outcome on a non-repo workspace and is not treated as an error by callers.
    """
    if not is_repo(path):
        return {"ok": False, "sha": "", "reason": "not a git repository"}
    root = repo_root(path) or path
    # `-u` updates tracked files only. New files are deliberately left alone:
    # a snapshot protects what is already there, and sweeping unrelated
    # untracked files into a commit is not that. `commit()` adds new files
    # when the caller explicitly names them.
    _run(root, "add", "-u")
    # Nothing staged means there was nothing to record.
    staged_ok, staged = _run(root, "diff", "--cached", "--name-only")
    if staged_ok and not staged.strip():
        sha = head_sha(root)
        return {"ok": True, "sha": sha, "reason": "nothing to commit"}
    ok, out = _run(root, "commit", "-m", message, "--no-verify")
    if not ok:
        return {"ok": False, "sha": "", "reason": out}
    return {"ok": True, "sha": head_sha(root), "reason": ""}

def commit(path: str | Path, message: str,
           files: list[str] | None = None) -> dict:
    """Commit the given files (or everything tracked) with `message`.

    Unlike `snapshot`, this does stage new files when `files` is given, because
    it is called after an approved plan and the plan may legitimately create a
    file — `code.applyPlan` reports `create` per entry for exactly this reason.
    """
    if not is_repo(path):
        return {"ok": False, "sha": "", "reason": "not a git repository"}
    root = repo_root(path) or path
    if files:
        add_ok, add_out = _run(root, "add", "--", *files)
        if not add_ok:
            return {"ok": False, "sha": "", "reason": add_out}
    else:
        _run(root, "add", "-u")
    staged_ok, staged = _run(root, "diff", "--cached", "--name-only")
    if staged_ok and not staged.strip():
        return {"ok": True, "sha": head_sha(root),
                "reason": "nothing to commit"}
    ok, out = _run(root, "commit", "-m", message, "--no-verify")
    if not ok:
        return {"ok": False, "sha": "", "reason": out}
    return {"ok": True, "sha": head_sha(root), "reason": ""}

def diff(path: str | Path, files: list[str] | None = None,
         staged: bool = False) -> str:
    """The unified diff of the workspace (or of `files`)."""
    if not is_repo(path):
        return ""
    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if files:
        args.extend(["--", *files])
    # `raw=True` because a diff's leading whitespace is content: a stripped
    # leading blank line or indentation would show the user a diff that is not
    # the one git produced, and the page renders these lines verbatim.
    ok, out = _run(path, *args, raw=True)
    return out if ok else ""

def restore(path: str | Path, files: list[str]) -> dict:
    """Discard uncommitted changes to `files` — `git restore`.

    Deliberately narrow: it takes explicit paths and refuses an empty list, so
    it can never be a bare "throw away everything" that loses work the user made
    in a file the agent never touched.
    """
    if not files:
        return {"ok": False, "reason": "No files named; refusing to discard "
                                       "everything."}
    if not is_repo(path):
        return {"ok": False, "reason": "not a git repository"}
    ok, out = _run(path, "restore", "--", *files)
    return {"ok": ok, "reason": "" if ok else out}

def revert_commit(path: str | Path, sha: str) -> dict:
    """Undo a commit made by Addled, as a new commit on top.

    Never rewrites history: `git revert` adds an inverse commit, which is safe
    on a branch the user may have pushed or based work on.
    """
    if not sha:
        return {"ok": False, "reason": "no commit given"}
    if not is_repo(path):
        return {"ok": False, "reason": "not a git repository"}
    ok, out = _run(path, "revert", "--no-edit", sha)
    if not ok:
        # Leave no half-done revert behind for the user to discover.
        _run(path, "revert", "--abort")
        return {"ok": False, "reason": out}
    return {"ok": True, "sha": head_sha(path), "reason": ""}

def describe_change(applied: list[dict], summary: str = "") -> str:
    """A commit message for a set of applied edits.

    Reads the way a person writes one: what changed, in which files, and that
    the agent made the change — so `git log` is honest about provenance and a
    later `git blame` does not attribute machine edits to the user.
    """
    count = len(applied)
    headline = summary.strip() or f"apply {count} file change(s)"
    if not headline.lower().startswith(("add", "fix", "update", "refactor",
                                        "rename", "remove", "move", "apply",
                                        "feat", "chore", "docs", "test")):
        headline = "apply: " + headline
    lines = [headline[:200], ""]
    for entry in applied[:40]:
        fp = entry.get("filePath") or ""
        tag = "create" if entry.get("created") else "edit"
        lines.append(f"- {tag} {fp}")
    if count > 40:
        lines.append(f"- … and {count - 40} more")
    lines.append("")
    lines.append("Applied by Addled from a reviewed plan.")
    return "\n".join(lines)
