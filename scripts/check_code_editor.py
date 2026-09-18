"""Code editor checks: workspace containment, saving, and remote reach.

Two things went wrong here and neither is visible from the dashboard.

`code.read` and `code.edit` built their path with a plain `os.path.join`, so a
`..` chain walked out of the bound workspace and `code.apply` would then write
whatever the model produced to that outside path. `code.write` is new and could
have repeated the mistake, so every method that takes a file path is asserted
here, in both directions: the escape has to be refused *with a readable reason*
and the file it was aimed at has to be untouched. A refusal that is asserted
without checking the reason passes even when the check is broken, so each
refusal case also asserts the wording, and each is paired with a positive
control that proves the same call works inside the workspace.

`code.bind` decides which folder the rest are contained to, so a remote caller
that could bind `C:\\` would make the containment meaningless. It is asserted
forbidden remotely while the contained calls stay available.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_code_editor.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


async def run():
    from backend import ws_server
    from backend.code import MAX_EDIT_BYTES
    from backend.code.diff_engine import apply_content
    from backend.remote import policy

    tmp = Path(tempfile.mkdtemp(prefix="code_ws_"))
    ws_dir = tmp / "project"
    outside = tmp / "outside"
    (ws_dir / "pkg").mkdir(parents=True)
    outside.mkdir()
    (ws_dir / "notes.txt").write_text("inside notes\n", encoding="utf-8")
    (ws_dir / "pkg" / "mod.py").write_text("print('hi')\n", encoding="utf-8")
    (outside / "secret.txt").write_text("SECRET-MARKER\n", encoding="utf-8")
    (outside / "victim.txt").write_text("untouched\n", encoding="utf-8")

    # What is actually served. `_register_default_handlers` runs from
    # `start_ws_server`, not at import, so call it to populate the real
    # dispatch table rather than a copy of the logic.
    ws_server._register_default_handlers()
    handlers = ws_server._server._handlers
    ws = object()
    wp = str(ws_dir)

    def has(name):
        check(f"the backend registers '{name}'", name in handlers, "missing")
        return handlers.get(name)

    read = has("code.read")
    write = has("code.write")
    edit = has("code.edit")
    apply_ = has("code.apply")
    grep = has("code.grep")
    bind = has("code.bind")
    if any(h is None for h in (read, write, edit, apply_, grep, bind)):
        return

    # ---- 1. reading: inside works, outside is refused ----------------------
    r = await read({"workspaceId": wp, "filePath": "notes.txt"}, ws)
    check("reading a relative path returns the file",
          r.get("content") == "inside notes\n", str(r)[:160])
    check("and detects the language", r.get("language") == "text", str(r))

    r = await read({"workspaceId": wp, "filePath": "pkg/mod.py"}, ws)
    check("a nested path reads", r.get("content") == "print('hi')\n", str(r)[:160])
    check("a nested path detects Python", r.get("language") == "python", str(r))

    for label, target in (
        ("a .. chain", "../outside/secret.txt"),
        ("a deep .. chain", "pkg/../../../outside/secret.txt"),
        ("a windows .. chain", "..\\outside\\secret.txt"),
        ("an absolute path", str(outside / "secret.txt")),
        ("a system path", "C:\\Windows\\win.ini"),
        ("a unix root path", "/etc/passwd"),
    ):
        r = await read({"workspaceId": wp, "filePath": target}, ws)
        body = r.get("content") or ""
        check(f"reading outside via {label} is refused",
              body.startswith("// Refused:"), body[:120])
        check(f"the refusal for {label} explains why",
              "outside the bound workspace" in body, body[:160])
        check(f"{label} leaks nothing",
              "SECRET-MARKER" not in body, "the outside file's contents came back")

    r = await read({"workspaceId": "", "filePath": str(outside / "secret.txt")}, ws)
    check("reading with no bound workspace is refused",
          "SECRET-MARKER" not in (r.get("content") or ""), str(r)[:120])
    check("and says a workspace is needed",
          "No workspace is bound" in (r.get("content") or ""), str(r)[:160])

    # ---- 2. saving: inside works, outside is refused -----------------------
    r = await write({"workspaceId": wp, "filePath": "notes.txt",
                     "content": "saved notes\n"}, ws)
    check("saving an existing file succeeds", r.get("success") is True, str(r))
    check("it is not reported as a creation", r.get("created") is False, str(r))
    check("the bytes are on disk",
          (ws_dir / "notes.txt").read_text(encoding="utf-8") == "saved notes\n", "")
    check("a backup of the previous content is kept",
          (ws_dir / "notes.txt.bak").read_text(encoding="utf-8") == "inside notes\n",
          "no .bak beside the file")

    r = await write({"workspaceId": wp, "filePath": "pkg/fresh.py",
                     "content": "print('new')\n"}, ws)
    check("saving a new file is allowed", r.get("success") is True, str(r))
    check("and is reported as a creation", r.get("created") is True, str(r))
    check("the new file exists",
          (ws_dir / "pkg" / "fresh.py").read_text(encoding="utf-8") == "print('new')\n",
          "nothing was written")

    for label, target in (
        ("a .. chain", "../outside/victim.txt"),
        ("a deep .. chain", "pkg/../../../outside/victim.txt"),
        ("an absolute path", str(outside / "victim.txt")),
        ("a sibling prefix", str(tmp / "project-extra" / "x.txt")),
    ):
        r = await write({"workspaceId": wp, "filePath": target,
                         "content": "PWNED\n"}, ws)
        check(f"writing outside via {label} is refused",
              r.get("success") is False, str(r)[:160])
        check(f"the refusal for {label} explains why",
              "outside the bound workspace" in str(r.get("error") or ""), str(r)[:200])
    check("the outside file is untouched",
          (outside / "victim.txt").read_text(encoding="utf-8") == "untouched\n",
          "the traversal write went through")
    check("no stray directory was created beside the workspace",
          not (tmp / "project-extra").exists(), "")

    r = await write({"workspaceId": "", "filePath": "anything.txt",
                     "content": "x"}, ws)
    check("saving with no bound workspace is refused", r.get("success") is False, str(r))
    check("and says so plainly",
          "No workspace is bound" in str(r.get("error") or ""), str(r)[:200])

    r = await write({"workspaceId": wp, "filePath": "notes.txt", "content": None}, ws)
    check("saving with no content is refused", r.get("success") is False, str(r))

    r = await write({"workspaceId": wp, "filePath": "big.txt",
                     "content": "x" * (MAX_EDIT_BYTES + 1)}, ws)
    check("an oversized save is refused", r.get("success") is False, str(r)[:120])
    check("and names the limit",
          "limit" in str(r.get("error") or ""), str(r)[:200])
    check("nothing oversized was written", not (ws_dir / "big.txt").exists(), "")

    # ---- 3. the diff path is contained too --------------------------------
    r = await apply_({"workspaceId": "", "filePath": "notes.txt",
                      "editId": "nope"}, ws)
    check("applying with no bound workspace is refused", r.get("success") is False, str(r))
    check("and says so plainly",
          "No workspace is bound" in str(r.get("error") or ""), str(r)[:200])

    r = await apply_({"workspaceId": wp, "filePath": "../outside/victim.txt",
                      "editId": "nope"}, ws)
    check("applying to a path outside is refused", r.get("success") is False, str(r))
    check("and explains why",
          "outside the bound workspace" in str(r.get("error") or ""), str(r)[:200])

    # The original path was read with the same unchecked join, so an escape
    # here would leak the outside file into the diff the model is shown.
    r = await edit({"workspaceId": wp, "filePath": "../outside/secret.txt",
                    "instruction": "no-op"}, ws)
    check("editing a path outside is refused",
          r.get("status") == "refused", str(r)[:200])
    check("and the outside file's text is not echoed back",
          "SECRET-MARKER" not in str(r), str(r)[:200])

    # ---- 4. searching the workspace ---------------------------------------
    (ws_dir / "needle.txt").write_text("alpha\nHERE-IS-THE-NEEDLE\nomega\n",
                                       encoding="utf-8")
    r = await grep({"workspaceId": wp, "query": "here-is-the-needle"}, ws)
    hits = r.get("matches") or []
    check("search is case-insensitive and finds the line",
          any(m.get("filePath") == "needle.txt" and m.get("line") == 2 for m in hits),
          str(r)[:200])
    check("search reports the matched text",
          any("NEEDLE" in (m.get("text") or "") for m in hits), str(r)[:200])
    check("search never looks outside the workspace",
          not any("secret" in (m.get("filePath") or "") for m in hits), str(r)[:200])

    r = await grep({"workspaceId": wp, "query": "x"}, ws)
    check("a one-character search is refused", bool(r.get("error")), str(r)[:160])
    r = await grep({"workspaceId": "", "query": "needle"}, ws)
    check("searching with no workspace is refused", bool(r.get("error")), str(r)[:160])

    # ---- 5. binding ------------------------------------------------------
    (ws_dir / ".hidden").mkdir()
    (ws_dir / ".hidden" / "h.txt").write_text("h\n", encoding="utf-8")
    (ws_dir / "node_modules").mkdir()
    (ws_dir / "node_modules" / "dep.js").write_text("d\n", encoding="utf-8")

    r = await bind({"folderPath": wp}, ws)
    paths = {f["path"] for f in r.get("files") or []}
    check("binding lists the workspace", "notes.txt" in paths, str(sorted(paths))[:200])
    check("binding reports whether it truncated", "truncated" in r, str(r)[:120])
    check("binding hides build and vendor folders",
          not any(p.startswith("node_modules") for p in paths), str(sorted(paths))[:200])
    check("binding hides dot folders",
          not any(p.startswith(".hidden") for p in paths), str(sorted(paths))[:200])
    check("binding uses forward slashes so the paths match on every platform",
          "pkg/mod.py" in paths, str(sorted(paths))[:200])

    r = await bind({"folderPath": wp, "includeIgnored": True}, ws)
    paths = {f["path"] for f in r.get("files") or []}
    check("show-everything reveals the hidden folders",
          any(p.startswith("node_modules") for p in paths), str(sorted(paths))[:200])
    check("but .git is still avoided",
          not any(p.startswith(".git") for p in paths), "")

    r = await bind({"folderPath": str(tmp / "does-not-exist")}, ws)
    check("binding a missing folder reports an error", bool(r.get("error")), str(r)[:160])

    # ---- 6. a remote session -------------------------------------------
    reason = policy.remote_refusal("code.bind")
    check("binding is refused for a remote session", reason is not None, "it was allowed")
    check("and the reason is about the binding, not about installing code",
          "Binding a folder" in str(reason), str(reason)[:200])
    check("reading stays available remotely",
          policy.remote_refusal("code.read") is None, "contained reads were blocked")
    check("saving stays available remotely",
          policy.remote_refusal("code.write") is None, "contained saves were blocked")
    check("the workspace is still a local decision",
          "code.bind" in policy.REMOTE_FORBIDDEN_METHODS, "")

    # ---- 7. the helper the handlers rely on ------------------------------
    r = apply_content(str(ws_dir / "never.txt"), "x", create=False)
    check("apply_content still refuses a missing file by default",
          r.get("success") is False, str(r))
    check("and names the missing file",
          "File not found" in str(r.get("error") or ""), str(r)[:200])
    r = apply_content(str(ws_dir / "made.txt"), "x", create=True)
    check("apply_content can create when asked", r.get("success") is True, str(r))
    check("and reports the creation", r.get("created") is True, str(r))

    # A workspace that does not exist yet is still a valid binding target for
    # reads — they must not resolve to the process's own directory.
    r = await read({"workspaceId": str(tmp / "ghost"), "filePath": "notes.txt"}, ws)
    check("a path is not resolved against the process cwd",
          "inside notes" not in (r.get("content") or ""), str(r)[:160])


def main() -> int:
    asyncio.run(run())
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("Code editor checks passed.")
    print("  - reads, writes, diffs and searches are contained to the workspace")
    print("  - escapes are refused with a reason and leave the target untouched")
    print("  - binding is a local decision; contained calls stay remote-safe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
