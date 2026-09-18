"""Workspace confinement checks.

The interesting cases are the boundaries, not the happy path: a relative path
has to land in the workspace rather than next to the process, an absolute path
outside it has to be refused with a readable reason, and a symlink pointing out
of the workspace has to be refused too — that last one is what proves the path
is resolved before it is compared.

Config is mutated in memory and restored in a finally, so no settings file is
touched.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_workspace.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


def set_cfg(root_path, extra=None, mode="workspace_only"):
    from backend.config import config
    config._data.setdefault("workspace", {})
    config._data["workspace"]["root"] = str(root_path or "")
    config._data["workspace"]["extra_dirs"] = list(extra or [])
    config._data.setdefault("safety", {})
    config._data["safety"]["file_access_mode"] = mode
    config._data["safety"]["allowed_folders"] = []


async def run():
    from backend.actions.file_ops import FileOps
    from backend.config import config
    from backend import workspace

    config._ensure_loaded()
    original_workspace = dict(config._data.get("workspace") or {})
    original_safety = dict(config._data.get("safety") or {})

    tmp = Path(tempfile.mkdtemp())
    inside = tmp / "inside"
    inside.mkdir()
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (inside / "notes.txt").write_text("hello", encoding="utf-8")

    ops = FileOps()
    try:
        # ---- 1. nothing configured: behave exactly as before --------------
        set_cfg("")
        cfg = workspace.describe()
        check("an unset workspace reports itself as unconfigured",
              cfg["configured"] is False, str(cfg))
        check("and says so in plain words", bool(cfg["note"]), str(cfg))
        check("nothing is enforced while it is unset",
              cfg["enforced"] is False, str(cfg))
        allowed, reason = workspace.resolve(str(outside / "secret.txt"))
        check("an unset workspace does not block existing behaviour",
              reason is None and allowed is not None, str(reason))

        # ---- 2. configured: relative paths land in the workspace ----------
        set_cfg(inside)
        cfg = workspace.describe()
        check("a configured workspace reports itself as configured",
              cfg["configured"] is True, str(cfg))
        check("and is enforced", cfg["enforced"] is True, str(cfg))
        resolved, reason = workspace.resolve("notes.txt")
        check("a relative path resolves inside the workspace",
              reason is None and resolved == (inside / "notes.txt").resolve(),
              f"{resolved} {reason}")
        check("a nested relative path too",
              workspace.resolve("sub/dir/f.txt")[0]
              == (inside / "sub" / "dir" / "f.txt").resolve(), "")

        # ---- 3. outside is refused, with a reason worth reading -----------
        resolved, reason = workspace.resolve(str(outside / "secret.txt"))
        check("an absolute path outside is refused", resolved is None, str(resolved))
        check("the refusal explains itself",
              reason and "outside the workspace" in reason and "Settings" in reason,
              str(reason))
        check("traversal out of the workspace is refused",
              workspace.resolve("../outside/secret.txt")[0] is None, "")
        check("a sibling with a shared prefix is not a false positive",
              workspace.resolve(str(inside) + "-extra/x.txt")[0] is None,
              "prefix matching accepted a non-child path")

        # ---- 4. the guard actually stops the tools ------------------------
        blocked = await ops.read(str(outside / "secret.txt"))
        check("read is blocked", blocked.get("success") is False, str(blocked))
        check("and is marked as blocked", blocked.get("blocked") is True, str(blocked))
        blocked = await ops.write(str(outside / "new.txt"), "x")
        check("write is blocked", blocked.get("success") is False, str(blocked))
        check("nothing was created outside",
              not (outside / "new.txt").exists(), "")
        allowed = await ops.read("notes.txt")
        check("a relative read inside still works",
              allowed.get("success") is True and allowed.get("content") == "hello",
              str(allowed))
        written = await ops.write("made.txt", "written")
        check("a relative write inside still works",
              written.get("success") is True, str(written))
        check("and landed in the workspace",
              (inside / "made.txt").read_text(encoding="utf-8") == "written", "")
        listed = await ops.list_dir(".")
        check("listing the workspace works",
              listed.get("success") is True and listed.get("count", 0) >= 1,
              str(listed))
        blocked = await ops.delete(str(outside / "secret.txt"))
        check("delete is blocked", blocked.get("success") is False, str(blocked))
        check("the outside file survived", (outside / "secret.txt").exists(), "")

        # ---- 5. a symlink out of the workspace must not be a way round ----
        link = inside / "shortcut"
        made_link = False
        try:
            if os.name == "nt":
                os.symlink(str(outside), str(link), target_is_directory=True)
            else:
                link.symlink_to(outside)
            made_link = True
        except (OSError, NotImplementedError) as e:
            print(f"   (symlink not available here: {e})")
        if made_link:
            resolved, reason = workspace.resolve(str(link / "secret.txt"))
            check("a symlink pointing outside is refused",
                  resolved is None, f"resolved to {resolved}")

        # ---- 6. mode semantics -------------------------------------------
        other = tmp / "other"
        other.mkdir()
        set_cfg(inside, extra=[other], mode="workspace_only")
        check("workspace_only ignores extra folders",
              workspace.resolve(str(other / "x.txt"))[0] is None, "")
        set_cfg(inside, extra=[other], mode="custom")
        check("custom allows the extra folders",
              workspace.resolve(str(other / "x.txt"))[0] is not None, "")
        check("custom still allows the workspace",
              workspace.resolve("notes.txt")[0] is not None, "")
        set_cfg(inside, extra=[], mode="unrestricted")
        check("unrestricted allows anywhere",
              workspace.resolve(str(outside / "secret.txt"))[0] is not None, "")
        allowed = await ops.read(str(outside / "secret.txt"))
        check("and the tools agree",
              allowed.get("success") is True, str(allowed))
        set_cfg(inside, extra=[], mode="nonsense")
        check("an unrecognised mode falls back to workspace_only",
              workspace.mode() == "workspace_only", workspace.mode())

        # ---- 7. the legacy allowed_folders list is honoured ---------------
        set_cfg(inside, extra=[], mode="custom")
        config._data["safety"]["allowed_folders"] = [str(other)]
        check("safety.allowed_folders still works as an extra folder",
              workspace.resolve(str(other / "x.txt"))[0] is not None,
              "the list the Safety page has always had is being ignored")
        config._data["safety"]["allowed_folders"] = []

        # ---- 8. bad input -------------------------------------------------
        check("an empty path is refused clearly",
              workspace.resolve("")[1] == "No path given.", "")
        resolved, reason = workspace.resolve("~/definitely-not-a-real-dir/x")
        check("an expandable ~ path is handled without raising",
              reason is not None or resolved is not None, str(reason))
    finally:
        config._data["workspace"] = original_workspace
        config._data["safety"] = original_safety
        import shutil
        try:
            if os.path.islink(inside / "shortcut"):
                os.unlink(inside / "shortcut")
        except OSError:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


asyncio.run(run())
print()
print(f"{'FAIL' if fails else 'PASS'}: {len(fails)} failure(s)")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
