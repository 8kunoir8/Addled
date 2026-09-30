"""Git safety-net checks — the undo the Code page offers after a plan.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_gitops.py

These exercise a throwaway repo in a temp directory, so they need `git` on PATH.
When git is absent the suite reports that and passes, because every caller
treats git as optional — that is the behaviour being pinned as much as the happy
path.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.codemode import gitops

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True)

def run():
    if not gitops.available():
        print("  (git not found on PATH — skipping the live repo checks)")
        # Still assert the non-repo behaviour, which must never raise.
        check("status on a non-repo is not an error",
              gitops.status(tempfile.gettempdir()).get("isRepo") is False,
              "status should report isRepo False")
        check("a non-repo commit is refused, not raised",
              gitops.commit(tempfile.gettempdir(), "x").get("ok") is False,
              "commit should return ok False")
        check("revert with no sha is refused",
              gitops.revert_commit(tempfile.gettempdir(), "").get("ok") is False,
              "empty sha must be refused")
        check("restoring with no files is refused",
              gitops.restore(tempfile.gettempdir(), []).get("ok") is False,
              "an empty file list must be refused, never 'discard all'")
        return

    tmp = Path(tempfile.mkdtemp(prefix="gitops_"))
    try:
        repo = tmp / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")

        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "a.py")
        _git(repo, "commit", "-m", "initial")

        check("a repo is detected", gitops.is_repo(repo), "is_repo False")
        check("the root resolves", gitops.repo_root(repo) == repo.resolve(),
              str(gitops.repo_root(repo)))

        # ---- snapshot ---------------------------------------------------
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        snap = gitops.snapshot(repo, "snapshot before apply")
        check("a snapshot commits the tracked change", snap.get("ok"),
              str(snap))
        check("and it changed HEAD",
              gitops.head_sha(repo) == snap.get("sha"), str(snap))

        # ---- commit after a plan ----------------------------------------
        (repo / "a.py").write_text("x = 3\n", encoding="utf-8")
        (repo / "b.py").write_text("y = 1\n", encoding="utf-8")
        applied = [{"filePath": "a.py", "created": False},
                   {"filePath": "b.py", "created": True}]
        msg = gitops.describe_change(applied, "rename x to y")
        com = gitops.commit(repo, msg, files=["a.py", "b.py"])
        check("a plan commit succeeds", com.get("ok"), str(com))
        check("and stages a newly created file",
              subprocess.run(["git", "-C", str(repo), "ls-files",
                              "--error-unmatch", "b.py"],
                             capture_output=True).returncode == 0,
              "b.py was not committed")
        check("the commit message names the files",
              "a.py" in msg and "b.py" in msg, msg)

        # ---- status -----------------------------------------------------
        (repo / "c.py").write_text("z = 1\n", encoding="utf-8")
        st = gitops.status(repo)
        check("status reports the repo", st.get("isRepo") is True, str(st))
        check("status lists an untracked file", "c.py" in st.get("untracked", []),
              str(st))

        # A MODIFIED tracked file, which is the case that was broken.
        #
        # `git status --porcelain` pads the status to a fixed two columns, so a
        # file modified only in the working tree reports as `" M name"` — the
        # LEADING SPACE IS A STATUS COLUMN, not indentation. Stripping the whole
        # output (which `_run` used to do) turned that into `"M name"`, shifted
        # every field one place left, and made the path come back with its first
        # two characters missing: `app.py` was reported as `pp.py`. It only ever
        # bit the FIRST line, which is why it survived every other test here.
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        st_m = gitops.status(repo)
        check("a working-tree-modified file is listed as changed",
              "a.py" in st_m.get("changed", []),
              f"changed={st_m.get('changed')!r} — a leading-space status "
              f"(' M a.py') was being stripped to 'M a.py'")
        check("no reported path lost its first characters",
              all(not (len(p) < 4) for p in st_m.get("changed", [])),
              f"changed={st_m.get('changed')!r}")
        # The same shape for an index-only change (`M ` — trailing space), so
        # both columns are exercised rather than only the one that broke.
        _git(repo, "add", "a.py")
        st_i = gitops.status(repo)
        check("a staged-only change is also listed correctly",
              "a.py" in st_i.get("changed", []),
              f"changed={st_i.get('changed')!r}")
        _git(repo, "reset", "-q")
        _git(repo, "checkout", "--", "a.py")

        # The diff must be git's own text, not a trimmed approximation: the page
        # renders these lines verbatim and a stripped first line would show
        # something git never printed.
        (repo / "a.py").write_text("x = 7\n", encoding="utf-8")
        d0 = gitops.diff(repo)
        check("a diff keeps git's own first line",
              d0.lstrip().startswith("diff --git"), repr(d0[:60]))
        check("a diff is not stripped at the front",
              not d0.startswith("\n") and d0 == d0.rstrip("\r\n"),
              repr(d0[:40]))
        _git(repo, "checkout", "--", "a.py")

        # ---- revert -----------------------------------------------------
        before = (repo / "a.py").read_text(encoding="utf-8")
        rev = gitops.revert_commit(repo, com["sha"])
        check("reverting the agent's commit succeeds", rev.get("ok"), str(rev))
        after = (repo / "a.py").read_text(encoding="utf-8")
        check("and restores the previous contents", after != before
              and "x = 2" in after, f"{before!r} -> {after!r}")
        check("revert adds history rather than rewriting it",
              int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip()) >= 4,
              _git(repo, "rev-list", "--count", "HEAD").stdout)

        # ---- diff / restore ---------------------------------------------
        (repo / "a.py").write_text("x = 999\n", encoding="utf-8")
        d = gitops.diff(repo, files=["a.py"])
        check("a diff of a named file is returned", "999" in d, d[:120])
        res = gitops.restore(repo, ["a.py"])
        check("restore discards a named file's change", res.get("ok"), str(res))
        check("and the file is back", "999" not in
              (repo / "a.py").read_text(encoding="utf-8"),
              (repo / "a.py").read_text(encoding="utf-8"))

        check("restore refuses an empty file list",
              gitops.restore(repo, []).get("ok") is False,
              "an empty list must never mean 'everything'")

        # ---- describe_change --------------------------------------------
        headline = gitops.describe_change([{"filePath": "a.py"}], "").splitlines()[0]
        check("an empty summary still produces a usable headline",
              headline.startswith(("apply", "apply:")), headline)
        headline2 = gitops.describe_change(
            [{"filePath": "a.py"}], "fix the parser").splitlines()[0]
        check("a good summary is kept as-is", headline2 == "fix the parser",
              headline2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: git safety net — status, snapshot, commit, revert, restore")
    return 0

if __name__ == "__main__":
    sys.exit(main())
