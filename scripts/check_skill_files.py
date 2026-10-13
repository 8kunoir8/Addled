"""A skill can reach the files it ships, and only those files.

Six of the fifteen installed market skills tell the model to read a file they
ship - `brainstorming` says "read `visual-companion.md` in this directory",
`subagent-driven-development` names three prompt templates it dispatches to
subagents. 137 KB of shipped, stored, and named content that could not be
opened, because nothing told the model which directory "this" was.

The failure was not merely absent. `search_files` globs the PROCESS WORKING
DIRECTORY, so a model that tried the obvious fallback got whichever copy of
the skill happened to be nearest - and the copies had diverged. Measured on
this machine: the live install had `builder-check-prompt.md` where a different
project's copy had `spec-document-reviewer-prompt.md`. Right filename, wrong
contents, no signal. That is worse than an error, which is what this check
exists to keep from coming back.

What has to hold:

  ADDRESSABLE. `skill_view` names the files, so the model learns they exist,
  and `skill_read` opens one by that name.

  FIXED ROOT. Resolution is against the skill's own folder, never the cwd.
  The check runs the resolver from a foreign working directory and requires
  the same answer, because that is exactly the bug.

  CONTAINED. `..`, an absolute path, a drive-relative name and a missing file
  are all refused, and a refusal names the files that do exist rather than
  leaving a dead end.

  HONEST. A skill with no folder says so instead of inventing a path.

Revert-verification this was built against: dropping `folder=str(d.resolve())`
from `market._register_dir` fails the addressability checks; replacing
`resolve_file`'s containment with a plain join fails the containment checks.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_skill_files.py
"""

import ast
import os
import sys
import textwrap
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")
        fails.append(label)


def main() -> int:
    print("check_skill_files")

    import asyncio
    import tempfile

    try:
        from backend.skills.registry import (
            SkillDefinition, skill_registry, _SKILL_MD_NAMES,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the registry: {e}")
        print()
        print("FAILED (1): could not import the registry")
        return 1

    # ---------------------------------------------------------- addressable
    # A skill folder built here rather than read from the install: the check
    # must not depend on which market skills the user happens to have.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "demo-skill"
        (root / "references").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "SKILL.md").write_text(
            "---\nname: demo\ndescription: d\n---\nread "
            "`references/tpl.md` in this directory\n", encoding="utf-8")
        (root / "references" / "tpl.md").write_text("TEMPLATE BODY",
                                                    encoding="utf-8")
        (root / "scripts" / "run.py").write_text("print('hi')\n",
                                                 encoding="utf-8")

        skill = SkillDefinition("demo", "d", {}, None, folder=str(root))

        files = skill.list_files()
        check("list_files sees a nested file",
              "references/tpl.md" in files, f"got {files}")
        check("list_files sees a script",
              "scripts/run.py" in files, f"got {files}")
        check("list_files hides the skill's own markdown",
              "SKILL.md" not in files,
              "the body already reaches the model via skill_view")
        check("_SKILL_MD_NAMES covers the candidate entry points",
              {"skill.md", "readme.md"} <= set(_SKILL_MD_NAMES))

        # ------------------------------------------------------- fixed root
        got = skill.resolve_file("references/tpl.md")
        check("a named file resolves",
              got is not None and got.read_text(encoding="utf-8") == "TEMPLATE BODY",
              f"got {got}")

        # Run the SAME resolution from an unrelated working directory. Before
        # the fix this is what changed the answer, because the fallback was a
        # cwd glob. Resolution is rooted at the skill, so cwd cannot matter.
        previous = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            again = skill.resolve_file("references/tpl.md")
            check("resolution does not depend on the working directory",
                  again is not None and again == got,
                  f"cwd changed the answer: {got} vs {again}")
        finally:
            os.chdir(previous)

        # -------------------------------------------------------- contained
        #
        # A real file planted OUTSIDE the skill folder, so a refusal cannot be
        # mistaken for "the file was not there anyway". Without this the check
        # passed against a resolver with no containment at all: every relative
        # escape happened to land on a path that did not exist, and only the
        # absolute one was caught. The trap is what makes the others real.
        outside = root.parent / "outside-secret.md"
        outside.write_text("SHOULD NEVER BE READABLE", encoding="utf-8")
        for bad, why in [
            ("../outside-secret.md", "a real file one level up"),
            ("../../outside-secret.md", "a real file two levels up"),
            ("references/../../outside-secret.md", "an escape after a valid prefix"),
            ("..", "the parent itself"),
            (r"..\..\settings.json", "windows-style escape"),
            ("/etc/hosts", "absolute posix"),
            (str(outside), "the absolute path of a real outside file"),
            ("C:/Windows/win.ini", "absolute windows"),
            ("nope.md", "a file that is not there"),
            ("", "an empty path"),
        ]:
            resolved = skill.resolve_file(bad)
            check(f"refused: {why}", resolved is None,
                  f"{bad!r} resolved to {resolved}")

        # A path that traverses out and back IN is contained, so it is allowed
        # - the rule is where it lands, not whether `..` appears. Worth pinning
        # because the obvious fix (reject any `..`) would be wrong here.
        check("a path that leaves and returns is still inside",
              skill.resolve_file("../demo-skill/references/tpl.md") is not None,
              "containment is about the destination, not the spelling")

        # ----------------------------------------------------------- honest
        bare = SkillDefinition("bare", "d", {}, None)
        check("a skill with no folder lists nothing", bare.list_files() == [])
        check("a skill with no folder resolves nothing",
              bare.resolve_file("x.md") is None)

    # ------------------------------------------------- the tools actually say it
    # The folder being right is not enough: if `skill_view` does not NAME the
    # files, the model still cannot know they exist. That was half the defect.
    view = skill_registry.get("skill_view")
    read = skill_registry.get("skill_read")
    check("skill_view is registered", view is not None)
    check("skill_read is registered", read is not None)

    if view is not None:
        import inspect
        src = inspect.getsource(view.handler)
        check("skill_view returns the file list", "list_files()" in src,
              "without it the body names a file the model cannot discover")
    if read is not None:
        src = inspect.getsource(read.handler)
        check("skill_read resolves through the skill, not the cwd",
              "resolve_file" in src,
              "a cwd join is the bug this replaces")
        check("skill_read refuses a skill with no files",
              "no files of its own" in src)

    # ------------------------------------------------------------- the guard
    # `_register_dir` must set the folder, or every market skill loses its
    # files and nothing above can work. Pinned because it is one keyword in a
    # constructor and was silently dropped once while this was written.
    try:
        import importlib
        mk = importlib.import_module("backend.skills.market")
        register_src = inspect.getsource(mk.SkillMarket._register_dir)
        check("_register_dir sets folder on the skill",
              "folder=str(d.resolve())" in register_src,
              "without it no market skill can reach its own files")
        check("_register_dir still registers the skill",
              "skill_registry.register(skill)" in register_src,
              "an edit that drops this registers nothing and logs success")
    except Exception as e:  # noqa: BLE001
        check("could read market._register_dir", False, str(e))

    # ------------------------------------------------- scripts at any depth
    # `_register_dir` decided `requires_approval` from a NON-recursive scan,
    # so a skill following the conventional layout (`scripts/x.py`) was
    # registered as instruction-only and the approval gate never saw it.
    # Confirmed against a real skill zip before this was fixed.
    try:
        import importlib
        mk = importlib.import_module("backend.skills.market")
        src = inspect.getsource(mk.SkillMarket._register_dir)
        check("scripts are found at any depth",
              'rglob("*.py")' in src,
              "a non-recursive scan misses scripts/ and skips the gate")
        check("__pycache__ is not mistaken for a shipped script",
              "__pycache__" in src)

        # A nested entry point must still be chosen as the entry point.
        pick = mk._pick_script
        check("a nested main.py is picked as the entry point",
              pick(["scripts/main.py", "scripts/other.py"], "x") == "scripts/main.py",
              f"got {pick(['scripts/main.py', 'scripts/other.py'], 'x')}")
        check("a nested <skill>.py is picked over an unrelated root file",
              pick(["zzz.py", "scripts/demo.py"], "demo") == "scripts/demo.py",
              "basename matching is what lets a nested entry point be found")
    except Exception as e:  # noqa: BLE001
        check("could read the script scan", False, str(e))

    # ------------------------------------------------------ zip intake guards
    # Every one of these is a real escape, tested before it was refused:
    # `extractall` wrote `../` and `/abs/` entries outside the destination on
    # this machine, and a zip can carry a symlink and a drive-relative name
    # (`C:x.txt`) that a naive containment check passes.
    try:
        import importlib
        mk = importlib.import_module("backend.skills.market")
        for bad, why in [
            ("../x.txt", "relative escape"),
            ("../../x.txt", "two-level escape"),
            ("a/../../x.txt", "escape after a valid prefix"),
            ("..\\x.txt", "windows backslash escape"),
            ("/abs/x.txt", "absolute posix"),
            ("C:/Windows/x.txt", "absolute windows"),
            ("C:x.txt", "drive-relative (passes a naive containment check)"),
            ("x.txt:ads", "alternate data stream"),
            ("CON", "reserved device name"),
            ("con.txt", "reserved device name with extension"),
            ("LPT1.log", "reserved device name, other class"),
            (".env", "hidden config file"),
            ("", "empty name"),
            ("a/", "a directory entry"),
        ]:
            try:
                got = mk._safe_zip_member(bad)
                check(f"zip refuses {why}", False, f"{bad!r} accepted as {got!r}")
            except mk.UnsafeZipMember:
                check(f"zip refuses {why}", True)

        for good in ["SKILL.md", "scripts/run.py", "references/a/b.md",
                     ".gitignore"]:
            try:
                mk._safe_zip_member(good)
                check(f"zip allows {good!r}", True)
            except mk.UnsafeZipMember as e:
                check(f"zip allows {good!r}", False, str(e))

        # Parse the function and look for a real `extractall` CALL. A plain
        # substring search reads the docstring, which explains why extractall
        # is unsafe and therefore contains the word - so the naive version of
        # this check fails on correct code.
        _tree = ast.parse(textwrap.dedent(
            inspect.getsource(mk.SkillMarket.install_from_zip)))
        _calls = {
            node.func.attr for node in ast.walk(_tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        check("zip extraction is manual, never extractall",
              "extractall" not in _calls,
              "extractall writes ../ and /abs/ entries outside the destination")
        check("zip install refuses to overwrite by default",
              "overwrite: bool = False" in inspect.getsource(
                  mk.SkillMarket.install_from_zip))
    except Exception as e:  # noqa: BLE001
        check("could exercise the zip guards", False, str(e))

    # A real upload must work, wrapper folder and all.
    try:
        import io
        import shutil
        import tempfile
        import zipfile
        import importlib

        mk = importlib.import_module("backend.skills.market")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            # The shape a real skill zip has: one wrapper folder, a script in
            # scripts/, a template in references/.
            z.writestr("wrapped/SKILL.md",
                       "---\nname: wrapped-skill\ndescription: d\n---\nbody")
            z.writestr("wrapped/scripts/run.py", "print(1)\n")
            z.writestr("wrapped/references/t.md", "TEMPLATE\n")
        buf.seek(0)

        tmp = Path(tempfile.mkdtemp())
        previous_dir = mk.MARKET_DIR
        try:
            mk.MARKET_DIR = tmp
            meta = mk.SkillMarket().install_from_zip("wrapped.zip", buf.getvalue())
            check("a real skill zip installs", meta["name"] == "wrapped-skill")
            check("the wrapper folder is stripped",
                  (Path(meta["dir"]) / "SKILL.md").is_file(),
                  "registering the extract root would find nothing")
            check("a nested script is detected through the upload",
                  meta["scripts"] == ["scripts/run.py"],
                  f"got {meta['scripts']}")
            check("the uploaded skill is gated",
                  skill_registry.get("wrapped-skill").requires_approval is True,
                  "a shipped script must not install as instruction-only")
            check("the uploaded skill can reach its template",
                  skill_registry.get("wrapped-skill").resolve_file(
                      "references/t.md") is not None)
        finally:
            mk.MARKET_DIR = previous_dir
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        check("a real skill zip installs", False, str(e))

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all skill-file checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
