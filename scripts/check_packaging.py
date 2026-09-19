"""What a release may contain, asserted before anything is built.

`.gitignore` keeps the developer's own state out of git. Nothing kept it out of
the *installer*: `extraResources` has `from: backend, filter: ['**/*']`, and
electron-builder does not read `.gitignore`, so the 1.0.17 installer contained

  * `backend/memory/settings.json` — the developer's live config, including a
    provider API key and the remote gateway's password hash,
  * `backend/memory/chat_history.json` — their actual conversation,
  * `vectors.db`, `triples.db`, `links.db` — their memory and knowledge graph,
  * `addled.log`, `egress.jsonl` — their log and their egress audit trail,

and a new install seeded itself from all of it. A review of the repository
showed none of this, because git was doing its job.

`scripts/deploy_to_install.ps1` had the same fault in a smaller way: its
hand-written exclude list had drifted, missing `*.db`, `*.log`, `*.jsonl` and
`facts.json`, so it copied three of those files over the *installed* app's own
(moved to a git-derived list in the same change).

This suite is the thing that stops the two from drifting apart again. It reads
`.gitignore`, reads the packaging configuration, and fails if anything git
ignores would ship — with one declared exception, so that "ignore" is not
silently treated as "never ship".

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_packaging.py
"""

import fnmatch
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def read(relative: str) -> str:
    with open(os.path.join(ROOT, relative), "r", encoding="utf-8") as handle:
        return handle.read()


def git(*args: str) -> list[str]:
    proc = subprocess.run(["git", "-C", ROOT, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return [line for line in proc.stdout.splitlines() if line]


# Paths that are ignored by git and still ship, with the reason. Each one is
# also asserted to be *present* in the shipped set, because an entry here that
# no longer matches anything is how a real exclusion gets mistaken for it.
SHIPPED_DESPITE_IGNORED = {
    "voice/models":
        "the offline voice models, fetched once by "
        "scripts/fetch_voice_models.py and bundled on purpose",
}

# Nothing that looks like these may ship at all, whatever the filters say.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(settings\.json|chat_history\.json|user_profile\.json|"
    r"models_catalog\.json|maintenance_state\.json|session_context\.json|"
    r"[^/]*\.db|[^/]*\.db-(wal|shm)|[^/]*\.log|[^/]*\.jsonl)$")


def extra_resource_filters(yaml_text: str, source: str) -> list[str]:
    """The filter patterns for one extraResources `from:` entry.

    Hand-parsed rather than loading a YAML dependency: the block is four keys
    deep and the shape is fixed, and a mis-parse shows up as an empty filter
    list, which the assertions below then report.
    """
    lines = yaml_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lstrip("- ").strip() == f"from: {source}":
            start = index
            break
    if start is None:
        raise RuntimeError(f"no extraResources entry for {source}")
    patterns: list[str] = []
    in_filter = False
    for line in lines[start + 1:]:
        stripped = line.strip()
        if stripped.startswith("- from:"):
            break
        if stripped.startswith("filter:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline.startswith("["):
                return [p.strip().strip("'\"") for p in
                        inline.strip("[]").split(",") if p.strip()]
            in_filter = True
            continue
        if in_filter:
            if stripped.startswith("- "):
                patterns.append(stripped[2:].strip().strip("'\""))
            elif stripped and not stripped.startswith("#"):
                break
    return patterns


def matches(path: str, patterns: list[str]) -> bool:
    """electron-builder semantics: any `!` match excludes, otherwise include.

    Patterns are relative to the entry's `from:` folder, and `**/` may stand
    for zero directories — `**/*` has to match `ws_server.py` at the top level
    as well as `memory/recall.py`.
    """
    included = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        regex = _glob_to_regex(pattern[1:] if negated else pattern)
        if regex.match(path):
            if negated:
                return False
            included = True
    return included


def _glob_to_regex(pattern: str):
    """minimatch, restricted to the shapes the config actually uses."""
    out = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(out) + "$")


def shipped_backend_files(patterns: list[str]) -> set[str]:
    """Files under backend/ the installer includes, relative to backend/."""
    shipped: set[str] = set()
    for folder, dirs, files in os.walk(os.path.join(ROOT, "backend")):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in files:
            full = os.path.join(folder, name)
            relative = os.path.relpath(full, os.path.join(ROOT, "backend"))
            relative = relative.replace(os.sep, "/")
            if matches(relative, patterns):
                shipped.add(relative)
    return shipped


def main() -> int:
    yaml_text = read("electron-builder.yml")
    patterns = extra_resource_filters(yaml_text, "backend")

    check("the backend extraResources filter is readable and non-trivial",
          len(patterns) >= 2 and patterns[0] == "**/*", str(patterns)[:200])
    check("and it excludes bytecode",
          any(p.startswith("!") and "__pycache__" in p for p in patterns),
          "no __pycache__ exclusion")

    shipped = shipped_backend_files(patterns)
    check("the backend tree ships its code",
          "ws_server.py" in shipped, f"{len(shipped)} files matched")
    check("and its package entry point",
          "main.py" in shipped, "backend/main.py is not shipped")
    check("and a nested module",
          "memory/recall.py" in shipped, "backend/memory/recall.py is not shipped")
    check("and its non-code assets",
          "voice/models/kokoro-v1.0.onnx" in shipped,
          "the voice models the offline voice needs are not shipped")

    ignored = git("ls-files", "--others", "--ignored", "--exclude-standard",
                  "--directory", "--", "backend")
    ignored = {p.replace(os.sep, "/") for p in ignored}
    check("git reports something as ignored under backend/",
          bool(ignored),
          "no ignored paths found — is this the right repository?")

    for ignored_path in sorted(ignored):
        stripped = ignored_path.rstrip("/")
        # git reports these relative to the repository; the filter patterns are
        # relative to backend/.
        relative = stripped[len("backend/"):] if stripped.startswith("backend/") else stripped
        allowed = next((prefix for prefix in SHIPPED_DESPITE_IGNORED
                        if relative == prefix or relative.startswith(prefix + "/")),
                       None)
        if allowed:
            check(f"'{allowed}' is still shipped, not excluded by accident",
                  any(p == allowed or p.startswith(allowed + "/") for p in shipped),
                  "the declared exception no longer matches anything")
            continue
        leaked = sorted(p for p in shipped
                        if p == relative or p.startswith(relative + "/"))
        check(f"the installer does not ship {ignored_path}",
              not leaked,
              f"would ship {len(leaked)} file(s), e.g. {leaked[:3]}")

    forbidden = sorted(p for p in shipped if FORBIDDEN_NAMES.search(p))
    check("nothing that looks like state or a credential ships",
          not forbidden, f"e.g. {forbidden[:5]}")

    # ---- the deploy script must exclude the same set ----------------------
    script = read(os.path.join("scripts", "deploy_to_install.ps1"))
    check("the deploy derives its excludes from git rather than a list",
          "--ignored" in script and "--exclude-standard" in script,
          "the script no longer asks git what to exclude")

    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", os.path.join(ROOT, "scripts", "deploy_to_install.ps1"),
         "-ListExclusions"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=ROOT)
    listed = [line.strip() for line in (proc.stdout or "").splitlines()
              if line.strip()]
    check("the deploy's exclude list can be read", proc.returncode == 0,
          (proc.stderr or proc.stdout or "").strip()[-300:])

    deploy_dirs, deploy_files = set(), set()
    for line in listed:
        if line.startswith("DIR "):
            deploy_dirs.add(os.path.relpath(line[4:].strip(), ROOT)
                            .replace(os.sep, "/"))
        elif line.startswith("FILE "):
            name = line[5:].strip()
            if os.path.isabs(name):
                name = os.path.relpath(name, ROOT).replace(os.sep, "/")
            deploy_files.add(name)

    check("the deploy excludes directories as well as files",
          bool(deploy_dirs) and bool(deploy_files),
          f"{len(deploy_dirs)} dirs, {len(deploy_files)} files")

    for ignored_path in sorted(ignored):
        stripped = ignored_path.rstrip("/")
        covered = bool({stripped} & deploy_files)
        if not covered:
            covered = any(stripped == d or stripped.startswith(d + "/")
                          for d in deploy_dirs)
        check(f"the deploy excludes {ignored_path}", covered,
              "a deploy would copy it over the installed app")

    # The three files the old hand-written list had drifted past. Named
    # explicitly because they are what the bug actually looked like.
    for name in ("backend/memory/vectors.db", "backend/memory/triples.db",
                 "backend/memory/addled.log", "backend/memory/egress.jsonl",
                 "backend/memory/facts.json", "backend/memory/settings.json"):
        check(f"the deploy excludes {name}", name in deploy_files,
              "not covered by the derived list")

    if fails:
        print(f"FAIL: {len(fails)} packaging check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print(f"PASS: {len(shipped)} packaged files, {len(ignored)} ignored paths "
          "correctly excluded from both the installer and the deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
