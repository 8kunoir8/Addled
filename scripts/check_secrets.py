"""Nothing secret may reach git — working tree, history, or a commit message.

The question this answers is "is any of my key being pushed?". A `.gitignore`
answers it for the files it covers and for nothing else: it does not help a key
pasted into a script, a test fixture, a README, a commit message or a tag, and it
says nothing about what is already in history.

So this checks four places, because a secret can hide in each without being in
the others:

  1. **the working tree**, for tracked files — the state that would be pushed by
     the next commit;
  2. **every commit in every ref**, by content, including commits that have been
     built on since — a key that was committed once is in the history forever,
     even if the file was deleted afterwards;
  3. **commit messages and tag messages**, which are pushed as objects in their
     own right;
  4. **unreachable objects**, which is where a key that someone amended away
     still sits locally.

It reports which file and which commit, never the value: the match has to be
shown to be useful and the value must not be, so the token is masked.

What it cannot do, and does not pretend to: the patterns are shapes, not
proof. A credential with an unusual format will not match, and a long random
string that looks like a key but is a fixture will. `check_packaging.py` covers
the other half — that state files and configs never ship or travel.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_secrets.py
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []

# name -> pattern. Shapes of credentials this project or its integrations could
# plausibly hold. Deliberately conservative about length so a stub like
# `tskey-auth-abc` in a test does not read as a leak.
PATTERNS = {
    "provider API key (sk-)": r"sk-[A-Za-z0-9_-]{20,}",
    "tailscale auth key": r"tskey-auth-[A-Za-z0-9]{20,}",
    "huggingface token": r"hf_[A-Za-z0-9]{25,}",
    "github token": r"gh[pousr]_[A-Za-z0-9]{25,}",
    "slack token": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "aws access key id": r"AKIA[0-9A-Z]{16}",
    "telegram bot token": r"[0-9]{8,10}:[A-Za-z0-9_-]{34,}",
    "private key block": r"BEGIN [A-Z ]*PRIVATE KEY",
    "password hash with a value": r'"password_hash"\s*:\s*"[A-Za-z0-9$./+=]{30,}"',
}

# Files that hold credentials by nature. None of them may ever be tracked, which
# is what `.gitignore` is for and what is asserted here rather than assumed.
NEVER_TRACKED = (
    "backend/memory/settings.json",
    "backend/memory/chat_history.json",
    "backend/memory/user_profile.json",
    "backend/memory/egress.jsonl",
    ".env",
    "bots/auth",
)

MASK = re.compile(r"(sk-|tskey-|hf_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{8,}")


def git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def mask(text: str) -> str:
    """Hide any token-shaped run so a report never becomes the leak."""
    return MASK.sub(lambda m: m.group(1) + "<redacted>", text).strip()[:180]


def next_commit_files() -> list[str]:
    """What `git add -A` would stage.

    The working copies of tracked files, plus untracked files git does not
    ignore. Ignored files are deliberately not included: they are where this
    machine's real credentials live (`backend/memory/settings.json`), they cannot
    be committed by accident, and scanning them would make this suite fail on
    every developer's machine for a reason that is not a leak. Whether they can
    travel is `check_packaging.py`'s question.
    """
    files: list[str] = []
    for args in (("ls-files",), ("ls-files", "--others", "--exclude-standard")):
        code, out = git(*args)
        files.extend(line for line in out.splitlines() if line.strip())
    return sorted(set(files))


def read_text(path: str) -> str:
    full = os.path.join(ROOT, path)
    try:
        if os.path.getsize(full) > 4 * 1024 * 1024:
            return ""          # a big binary or vendored blob cannot hold a line
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def main() -> int:
    code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    check("this is a git repository", code == 0, branch.strip()[:120])

    # ---- 1. the working tree, as the next commit would see it ------------
    staged = next_commit_files()
    check("the repository has files to scan", len(staged) > 20, f"{len(staged)}")
    bodies = {path: read_text(path) for path in staged}

    for name, pattern in PATTERNS.items():
        regex = re.compile(pattern)
        hits = [path for path, body in bodies.items() if regex.search(body)]
        check(f"no {name} in the files git tracks", not hits,
              f"e.g. {hits[0]}" if hits else "")

    # ---- 2. every commit in every ref, by content -------------------------
    for name, pattern in PATTERNS.items():
        # -G, not -S: it matches the diff lines, so it finds the commit that
        # introduced the string without re-grepping every snapshot.
        code, out = git("log", "--all", "--oneline", f"-G{pattern}")
        hits = [line for line in out.splitlines() if line.strip()]
        check(f"no {name} anywhere in history", not hits,
              f"{len(hits)} commit(s), newest {hits[0][:120]}" if hits else "")

    # ---- 3. messages, which are objects of their own ----------------------
    code, messages = git("log", "--all", "--format=%B")
    for name, pattern in PATTERNS.items():
        matched = [line for line in messages.splitlines() if re.search(pattern, line)]
        check(f"no {name} in commit messages", not matched,
              mask(matched[0]) if matched else "")

    code, tags = git("for-each-ref", "refs/tags", "--format=%(contents)")
    for name, pattern in PATTERNS.items():
        matched = [line for line in tags.splitlines() if re.search(pattern, line)]
        check(f"no {name} in tag messages", not matched,
              mask(matched[0]) if matched else "")

    # ---- 4. unreachable objects ------------------------------------------
    code, out = git("fsck", "--unreachable", "--no-progress")
    blobs = [line.split()[2] for line in out.splitlines()
             if line.startswith("unreachable blob")]
    bad = []
    for sha in blobs:
        code, body = git("cat-file", "-p", sha)
        if any(re.search(pattern, body) for pattern in PATTERNS.values()):
            bad.append(sha)
    check("no secret survives in an unreachable object", not bad,
          f"blob(s) {bad[:3]}")

    # ---- 5. the files that hold credentials are not tracked ---------------
    code, out = git("ls-files")
    tracked = set(out.splitlines())
    for path in NEVER_TRACKED:
        offenders = sorted(p for p in tracked
                           if p == path or p.startswith(path.rstrip("/") + "/"))
        check(f"'{path}' is not tracked", not offenders, f"tracked: {offenders[:3]}")

    # ---- 6. the remote is what decides who can read it --------------------
    # Not a failure either way, but it changes what a leak means, so it is
    # reported rather than left to be assumed.
    code, remotes = git("remote", "-v")
    if remotes.strip():
        print("  remote(s): " + " ".join(
            sorted({line.split()[1] for line in remotes.splitlines() if line.strip()})))

    if fails:
        print(f"FAIL: {len(fails)} secret check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print(f"PASS: no secret-shaped string in {len(PATTERNS)} patterns across "
          f"tracked files, all history, all messages and unreachable objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
