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

# extraResources sources (relative to the repository) that git ignores and that
# are still shipped, each with the reason it is allowed. A source not named here
# may not be ignored by git: that combination is how `tools/rtk` shipped 13 MB
# of binaries that were in no repository and in every installer.
DELIBERATE_SOURCES = {
    "dashboard/out":
        "the built dashboard, which is a build output by definition",
    "python-bundle":
        "the embedded Python runtime from scripts/bundle_python.py, which is "
        "what makes the installer self-contained",
}

# Sources that must never be shipped, however they are declared.
FORBIDDEN_SOURCES = {"tools", "tools/rtk", "backend/memory", "dist"}

# Nothing that looks like these may ship at all, whatever the filters say.
# Nothing that looks like these may ship at all, whatever the filters say.
#
# The trailing group covers the save-time artifacts as well as the files
# themselves: a `.json.bak` or `.json.tmp` is still the user's settings, and
# `!memory/*.json` in electron-builder does not match either (a single `*` does
# not cross the extra extension). That gap is why this pattern, not the YAML,
# is what has to be complete — the check is what catches the next such file.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(settings\.json|chat_history\.json|user_profile\.json|"
    r"models_catalog\.json|maintenance_state\.json|session_context\.json|"
    r"[^/]*\.db|[^/]*\.db-(wal|shm)|[^/]*\.log|[^/]*\.jsonl)"
    r"(\.(tmp|bak|old|orig|swp))?$")


def extra_resource_sources(yaml_text: str) -> list[str]:
    """Every `from:` path in extraResources, in order."""
    sources: list[str] = []
    inside = False
    for line in yaml_text.splitlines():
        if line.startswith("extraResources:"):
            inside = True
            continue
        if inside and line and not line.startswith((" ", "\t", "#")):
            break
        stripped = line.strip()
        if inside and stripped.startswith("- from:"):
            sources.append(stripped.split(":", 1)[1].strip().strip("'\""))
    return sources


def git_ignored(path: str) -> bool:
    """Whether git would ignore this path, whether or not it exists."""
    proc = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--", path],
                          capture_output=True, text=True)
    return proc.returncode == 0

# Content under `memory/` that is bundled ON PURPOSE rather than being user
# state. Fetched or authored once and shipped so the app works offline; an
# upgrade replacing these is the intent, not a fault.
BUNDLED_ON_PURPOSE = {
    "backend/voice/models",
    "backend/character/default_skins",
}

# Paths the discovery finds that hold CODE, not state. `backend/memory` is the
# folder the stores live in and the folder the memory modules are imported from;
# it must ship. A path landng here means the pattern matched a constant that
# names a container rather than a store.
CODE_DIRECTORIES = {
    "backend/memory",
}

# `app_paths` constants whose `DATA_DIR` root makes them a SIBLING of `backend/`
# in the user's profile, not a child of the install. A path anchored on one of
# these is not shipped and not shipped-over, so it needs no `.gitignore` rule to
# protect it — but it still needs to be listed here, because the "everything the
# app writes must be excluded" assertion below reads every discovered path and a
# silent skip is how a real leak would hide.
#
# `PYLIBS_DIR` is the precedent: on-demand `pip` installs land there, and they
# are deliberately unpinned (the folder is on the user's disk and git's own
# ignore rules do not reach it).
OUT_OF_TREE_ANCHORS = {
    "PYLIBS_DIR": "on-demand pip installs, under the user's own data directory",
    "CLI_TOOLS_DIR": "tools the user builds, under the user's own data directory",
}

# What a store's path looks like in this codebase. Every state module follows
# one of these shapes, so matching them keeps the check honest as files are
# added — the alternative is a hand-written list, which is the very thing that
# drifts, and the failure it drifts into is shipping a user's data over their
# own.
#
# `app_paths.MEMORY_DIR` is the shape everything uses now. It is resolved at
# runtime to whichever directory is writable (see backend/app_paths.py), and in
# the installer's own layout that is `<install>/resources/backend/memory` — the
# same place as before, which is why one anchor covers both. The older
# `Path(__file__).parent…` shapes stay in the pattern because nothing stops a new
# module from being written that way, and a store this check cannot see is a
# store that could ship.
#
# The `app_paths.<NAME>` anchor is deliberately NOT pinned to `MEMORY_DIR`.
# `app_paths` grew a second writable root — `CLI_TOOLS_DIR`, alongside
# `PYLIBS_DIR` — and a pattern naming one constant silently stops seeing
# anything anchored on the other. That is the exact failure this suite exists to
# catch, so the pattern covers every `app_paths.<CONST>` and the constants that
# live OUTSIDE the packaged tree are named in `OUT_OF_TREE_ANCHORS` below.
_STATE_EXPR = re.compile(
    r'^[A-Z_]+\s*=\s*'
    r'(?P<base>Path\(__file__\)(?:\.resolve\(\))?'
    r'(?:\.parent)+|SETTINGS_PATH\.parent|app_paths\.(?P<const>[A-Z_]+))'
    r'(?P<tail>(?:\s*/\s*"[^"]+")+)')

def written_state_paths() -> set[str]:
    """Repository-relative paths the backend WRITES user data to.

    Read out of the source rather than listed by hand, so a new store is
    covered the moment it exists instead of when someone remembers.

    The expression is reconstructed rather than pattern-matched piecemeal:
    `__file__.parent.parent / "memory" / "x"` is a *path*, and counting `parent`
    is the only way to resolve it. Guessing from the file's own directory put
    `memory/mood.json` under `backend/character/`, which is not where it is
    written and would have had the check guarding a path nothing uses.

    Paths anchored on an `OUT_OF_TREE_ANCHORS` constant are omitted: they live
    under the user's own data directory, so there is nothing to exclude from the
    installer and no `.gitignore` rule that could protect them. Omitting them
    here rather than in `main()` keeps every caller honest by construction.
    """
    backend = os.path.join(ROOT, "backend")
    found: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(backend):
        if "__pycache__" in dirpath:
            continue
        rel_dir = os.path.relpath(dirpath, ROOT).replace(os.sep, "/")
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, filename), "r",
                          encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for line in lines:
                m = _STATE_EXPR.match(line.strip())
                if not m:
                    continue
                base = m.group("base")
                const = m.group("const")
                # A constant declared out of the packaged tree contributes
                # nothing to exclude, and naming it here means adding another
                # one later cannot quietly widen what this check ignores.
                if const and const in OUT_OF_TREE_ANCHORS:
                    continue
                # Every `__file__` here is backend/<pkg>/<mod>.py, so start at
                # that file and walk up once per `.parent`.
                if base.startswith("Path(__file__)"):
                    here = f"{rel_dir}/{filename}"
                    ups = base.count(".parent")
                    node = here
                    for _ in range(ups):
                        node = os.path.dirname(node)
                    anchor = node.replace(os.sep, "/")
                else:
                    # `SETTINGS_PATH` lives at backend/memory/settings.json, and
                    # `app_paths.MEMORY_DIR` is that same directory — whichever
                    # writable location it resolves to at runtime, the *packaged*
                    # one is `<install>/resources/backend/memory`, which is what
                    # this check has to reason about.
                    anchor = "backend/memory"
                parts = re.findall(r'"([^"]+)"', m.group("tail"))
                if parts:
                    found.add(anchor.rstrip("/") + "/" + "/".join(parts))
    return found


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

    # ---- every packaged source is accounted for ---------------------------
    sources = extra_resource_sources(yaml_text)
    check("extraResources is readable", len(sources) >= 4, str(sources))
    check("and does not ship a hand-fetched binary directory",
          not (set(sources) & FORBIDDEN_SOURCES),
          f"declared: {sorted(set(sources) & FORBIDDEN_SOURCES)}")
    for source in sources:
        if not git_ignored(source):
            continue
        check(f"the ignored source '{source}' is declared, with a reason",
              source in DELIBERATE_SOURCES,
              "a gitignored source ships content git cannot show in review")
    for declared in DELIBERATE_SOURCES:
        check(f"'{declared}' is still packaged",
              declared in sources, "the declaration no longer matches anything")

    check("the backend extraResources filter is readable and non-trivial",
          len(patterns) >= 2 and patterns[0] == "**/*", str(patterns)[:200])
    check("and it excludes bytecode",
          any(p.startswith("!") and "__pycache__" in p for p in patterns),
          "no __pycache__ exclusion")

    # ---- a declared build target must have config to build it -------------
    #
    # `package.json` had `"build:all": "electron-builder --win --mac --linux"`
    # while `electron-builder.yml` defined only `win:`. electron-builder has no
    # targets for the other two, so the script produced a Windows build and
    # reported success — the flags read as "these platforms are supported" and
    # nothing contradicted them. A release script that quietly ignores half its
    # own arguments is worse than one that fails, so this ties the two together:
    # every platform flag a build script passes must have a matching section.
    #
    # It also catches the reverse, which is the reason it reads the npm scripts
    # rather than the yml alone: a `linux:` section added while no script passes
    # `--linux` is dead configuration that no build ever exercises.
    import json as _json

    # A malformed manifest is reported, not thrown. This suite exists to say
    # what is wrong in words — a stack trace from the check itself reads as the
    # check being broken, and hides the actual fault (unparseable JSON) behind
    # a traceback about `json.loads`.
    try:
        package = _json.loads(read("package.json"))
    except _json.JSONDecodeError as e:
        check("package.json is valid JSON", False,
              f"{e.msg} at line {e.lineno} column {e.colno}")
        package = {}
    check("package.json declares scripts",
          isinstance(package.get("scripts"), dict) and bool(package["scripts"]),
          "no scripts block — the build has no entry points")
    # electron-builder's own key for each CLI flag.
    SECTION_FOR_FLAG = {"--win": "win", "--mac": "mac", "--linux": "linux"}
    config_sections = {
        line.split(":", 1)[0].strip()
        for line in yaml_text.splitlines()
        # Top-level keys only: a nested `linux:` under another key would not
        # make electron-builder treat the platform as configured.
        if line and not line[0].isspace() and line.rstrip().endswith(":")
    }
    build_scripts = {name: cmd for name, cmd in package.get("scripts", {}).items()
                     if "electron-builder" in cmd}

    check("the packaging config is asserted against the build scripts",
          bool(build_scripts),
          "no npm script runs electron-builder — has the build moved?")

    for name, command in sorted(build_scripts.items()):
        passed = [flag for flag in SECTION_FOR_FLAG if flag in command]
        if not passed:
            # No platform flag: electron-builder builds for the host, so the
            # host's section is the one that has to exist.
            passed = ["--win"] if os.name == "nt" else []
        for flag in passed:
            section = SECTION_FOR_FLAG[flag]
            check(f"'{name}' passes {flag}, so the config defines '{section}:'",
                  section in config_sections,
                  f"electron-builder has no '{section}:' section, so {flag} "
                  f"builds nothing — remove the flag or add the section")

    for flag, section in sorted(SECTION_FOR_FLAG.items()):
        if section not in config_sections:
            continue
        check(f"the config's '{section}:' section has a script that builds it",
              any(flag in command for command in build_scripts.values()),
              f"'{section}:' is configured but no npm script passes {flag}, "
              f"so nothing ever builds it")
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

    # Tracking one of these is the other way this state reaches a clone —
    # `.gitignore` only prevents an *accidental* add, not a deliberate one.
    tracked_state = [p for p in git("ls-files", "--", "backend")
                     if FORBIDDEN_NAMES.search(p)]
    check("no database, log or config is tracked by git",
          not tracked_state, f"e.g. {tracked_state[:5]}")

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

    # ---- `-DryRun` must preview, and must not write ----------------------
    #
    # It returned before every copy step, so it printed nothing and exited 0.
    # The `/L` flag in the script's Invoke-Sync was unreachable, the closing
    # "Dry run - nothing was written" line could never run, and the one thing a
    # dry run is for — seeing what a deploy would change before running it —
    # did not happen. A silent empty preview is worse than none: it reads as
    # "nothing would change".
    #
    # Run WITHOUT elevation against whatever install the script auto-detects, so
    # a dry run that tries to write fails loudly here instead of on someone's
    # install. That it succeeds unelevated is the point: previewing a deploy
    # must not require admin rights. When no install is present the script says
    # so and exits non-zero, which is correct behaviour, not a failure of this
    # check — so the install is located first and the check is skipped without
    # one. (A dry run still needs a real target to diff against; it is the
    # *write* probe that -DryRun removes, not the target lookup.)
    any_install = [os.path.join(base, "Addled")
                   for base in (os.environ.get("ProgramFiles"),
                                os.environ.get("ProgramFiles(x86)"),
                                os.path.join(os.environ.get("LOCALAPPDATA") or "",
                                             "Programs"))
                   if base and os.path.isdir(
                       os.path.join(base, "Addled", "resources", "backend"))]
    if not any_install:
        print("  --   no installed app; skipping the dry-run checks")
    else:
        dry = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", os.path.join(ROOT, "scripts", "deploy_to_install.ps1"),
             "-BackendOnly", "-DryRun"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=ROOT)
        dry_out = (dry.stdout or "") + (dry.stderr or "")
        check("a dry run needs no elevation and succeeds", dry.returncode == 0,
              dry_out.strip()[-300:])
        check("a dry run says it is a dry run",
              "dry run" in dry_out.lower(), dry_out.strip()[-200:])
        check("a dry run does not claim to have deployed",
              "Deployed " not in dry_out,
              "a preview wrote for real, or reported writing")
        probe = os.path.join(any_install[0], ".deploy-write-probe")
        check("a dry run created no probe file in the install",
              not os.path.exists(probe),
              "the write probe ran despite -DryRun")

    for ignored_path in sorted(ignored):
        stripped = ignored_path.rstrip("/")
        covered = bool({stripped} & deploy_files)
        if not covered:
            covered = any(stripped == d or stripped.startswith(d + "/")
                          for d in deploy_dirs)
        check(f"the deploy excludes {ignored_path}", covered,
              "a deploy would copy it over the installed app")

    # The files the old hand-written list had drifted past. They are gone from
    # the working tree now, so a list of what exists cannot name them — what
    # matters is that git would ignore each one the moment it came back, because
    # git is what the deploy reads.
    for name in ("backend/memory/vectors.db", "backend/memory/triples.db",
                 "backend/memory/addled.log", "backend/memory/egress.jsonl",
                 "backend/memory/facts.json", "backend/memory/settings.json",
                 # Named because it was the one exception, and it was wrong: the
                 # calendar is the user's, not a seed for the installer to
                 # overwrite on every upgrade.
                 "backend/memory/integrations/calendar_events.json"):
        check(f"git ignores {name}", git_ignored(name),
              "the deploy derives its excludes from git, so this is the rule "
              "that has to hold")

    # ---- everything the app WRITES must be excluded from packaging --------
    #
    # Everything above proves the installer does not LEAK the developer's data.
    # This proves the other half, which nothing checked: that the data a user
    # ACCUMULATES is never shipped, so an upgrade cannot overwrite it.
    #
    # Why it matters. All user state lives inside the install directory
    # (`resources/backend/memory/`), so an upgrade preserves it only because the
    # installer carries no copy to replace it with. An NSIS upgrade adds and
    # replaces files; it does not wipe the folder. That means a state path which
    # is NOT excluded ships the developer's copy and overwrites the user's —
    # silently, and it looks like nothing happened.
    #
    # The gap this closes: the exclude list is hand-maintained, and it is only
    # compared against `.gitignore`. A new `memory/thing/` added without a
    # matching `.gitignore` line broke BOTH guarantees at once, and neither
    # check noticed. Here the paths are taken from the code that writes them,
    # so a new store is covered the moment it exists.
    state_paths = written_state_paths()
    check("state paths were found in the code", len(state_paths) >= 10,
          f"only found {len(state_paths)} — has the pattern changed?")

    # Every declared out-of-tree anchor must still exist in `app_paths.py`. An
    # entry here that names a constant which was renamed or removed is worse
    # than a missing one: it silently widens the skip, so the next store
    # anchored on the OLD name would never be checked.
    try:
        app_paths_src = read(os.path.join("backend", "app_paths.py"))
    except OSError as exc:  # pragma: no cover - only if the file is missing
        check("app_paths.py is readable", False, str(exc))
        app_paths_src = ""
    for const, why in sorted(OUT_OF_TREE_ANCHORS.items()):
        check(f"app_paths declares {const}",
              re.search(rf"^{const}\s*=", app_paths_src, re.M) is not None,
              f"listed as out-of-tree ({why}) but no such constant exists — "
              "either it was renamed or the entry is stale")

    for rel in sorted(state_paths):
        # Seed content deliberately bundled (voice models, default skins) is
        # not user state; it is named here so the exception is visible.
        if any(rel == s or rel.startswith(s + "/")
               for s in BUNDLED_ON_PURPOSE):
            continue
        # `backend/memory` is the folder the stores live IN, not a store: it
        # holds the memory modules, which are code and must ship. The module
        # that defines MEMORY_DIR means "the parent of settings.json", and
        # treating that as user state would demand we stop shipping the code.
        if rel in CODE_DIRECTORIES:
            continue
        check(f"git ignores the writable path {rel}", git_ignored(rel),
              "a new store was added without a .gitignore rule, so the "
              "installer will ship the developer's copy and an upgrade will "
              "overwrite the user's")

    # And the packaged filter must actually exclude it. git being right is not
    # enough on its own: the installer reads this list, not .gitignore.
    for rel in sorted(state_paths):
        if any(rel == s or rel.startswith(s + "/")
               for s in BUNDLED_ON_PURPOSE):
            continue
        if rel in CODE_DIRECTORIES:
            continue
        if not rel.startswith("backend/"):
            continue
        inner = rel[len("backend/"):]
        leaked = sorted(p for p in shipped
                        if p == inner or p.startswith(inner + "/"))
        check(f"the installer does not ship the writable path {inner}",
              not leaked,
              f"would ship {len(leaked)} file(s), e.g. {leaked[:3]}")

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
