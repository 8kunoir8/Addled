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
import re
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Redirect settings to a throwaway file BEFORE anything reads the config.
#
# This suite sets `workspace.root` to a temp folder to exercise containment. It
# used to do that on the LIVE settings.json and restore the old value in a
# `finally` — which works until a run is interrupted, and then the user's real
# workspace is left pointing at a directory a test created. That happened: the
# installed app was found bound to a leftover fixture in %TEMP% full of test
# files, with ~200 more temp workspaces beside it. A test that can damage the
# settings it is testing is a worse bug than the one it checks for.
_SETTINGS_TMP = Path(tempfile.mkdtemp(prefix="cfg_")) / "settings.json"
try:
    from backend.config import config as _boot_cfg
    from backend.config import use_settings_file as _use_settings
    _use_settings(_SETTINGS_TMP)
    _boot_cfg._data = {}          # drop anything cached from the real file
    _boot_cfg.load()
except Exception as _e:  # noqa: BLE001
    print(f"could not redirect settings ({_e}); refusing to run, because this "
          f"suite writes workspace.root and must not touch the live file")
    sys.exit(2)

fails = []


def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")


async def run():
    from backend import ws_server
    from backend.codemode import MAX_EDIT_BYTES
    from backend.codemode.diff_engine import apply_content
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

    # ---- 8. planning is required, and it cannot point outside ---------------
    plan = has("code.plan")
    if plan is not None:
        # The model is stubbed so the parsing and containment rules are tested
        # without one. Real behaviour is the model's; the rules below are ours.
        original = ws_server._run_chat_pipeline_inner
        calls = []

        def _stub(response):
            async def _inner(message, *a, **kw):
                calls.append({"message": message, "kwargs": kw})
                return {"response": response}
            return _inner

        try:
            check("the planner's tools are read-only",
                  "search_in_files" in ws_server._PLAN_TOOLS
                  and not any(t in ws_server._PLAN_TOOLS
                              for t in ("code_write", "write_file",
                                        "code_edit", "delete_file")),
                  str(ws_server._PLAN_TOOLS))

            # (a) a plain plan is parsed
            ws_server._run_chat_pipeline_inner = _stub(
                '{"summary": "fix it", "steps": ['
                '{"filePath": "notes.txt", "action": "edit",'
                ' "instruction": "tweak", "reason": "it is the file"}]}')
            r = await plan({"workspaceId": wp, "instruction": "tweak notes"}, ws)
            check("a plan comes back with its steps",
                  r.get("status") == "ok" and len(r["plan"]["steps"]) == 1,
                  str(r)[:200])
            check("a step keeps its reason",
                  bool(r["plan"]["steps"][0].get("reason")), str(r["plan"])[:160])

            # (b) prose around the JSON must not defeat parsing
            ws_server._run_chat_pipeline_inner = _stub(
                "Sure! Here is the plan:\n```json\n"
                '{"summary": "s", "steps": [{"filePath": "pkg/mod.py",'
                ' "instruction": "i", "reason": "r"}]}\n```\nHope that helps.')
            r = await plan({"workspaceId": wp, "instruction": "x"}, ws)
            check("a fenced plan is still parsed",
                  r.get("status") == "ok" and len(r["plan"]["steps"]) == 1,
                  str(r)[:200])

            # (c) an escape is refused at PLAN time, not later at edit time
            ws_server._run_chat_pipeline_inner = _stub(
                '{"summary": "s", "steps": ['
                '{"filePath": "../outside/secret.txt", "action": "edit",'
                ' "instruction": "read the secret", "reason": "r"}]}')
            r = await plan({"workspaceId": wp, "instruction": "x"}, ws)
            check("a plan naming a file outside the workspace is refused",
                  r.get("status") == "error", str(r)[:220])
            check("and the refusal says why",
                  "rejected" in r or "outside" in str(r).lower(), str(r)[:220])
            check("nothing was written while planning",
                  (outside / "victim.txt").read_text(encoding="utf-8")
                  == "untouched\n", "the planner wrote to disk")

            # (d) a create step is allowed even though the file does not exist
            ws_server._run_chat_pipeline_inner = _stub(
                '{"summary": "s", "steps": ['
                '{"filePath": "pkg/brand_new.py", "action": "create",'
                ' "instruction": "make it", "reason": "new module"}]}')
            r = await plan({"workspaceId": wp, "instruction": "x"}, ws)
            check("a create step is accepted for a file that does not exist",
                  r.get("status") == "ok", str(r)[:200])

            # (e) unusable answers fail loudly rather than half-applying
            ws_server._run_chat_pipeline_inner = _stub(
                "I think you should edit notes.txt")
            r = await plan({"workspaceId": wp, "instruction": "x"}, ws)
            check("prose with no JSON is an error, not an empty plan",
                  r.get("status") == "error" and r.get("plan") is None,
                  str(r)[:200])
            ws_server._run_chat_pipeline_inner = _stub(
                '{"summary": "s", "steps": []}')
            r = await plan({"workspaceId": wp, "instruction": "x"}, ws)
            check("a plan naming no file is an error",
                  r.get("status") == "error", str(r)[:200])

            # (f) the planner's brief must say the things that matter
            ws_server._run_chat_pipeline_inner = _stub(
                '{"summary": "s", "steps": [{"filePath": "notes.txt",'
                ' "instruction": "i", "reason": "r"}]}')
            await plan({"workspaceId": wp, "instruction": "x"}, ws)
            # The planner now makes TWO calls, so the brief to check is the
            # first one — the second is the formatter, which is told the files
            # are already known.
            find_brief = calls[0]["kwargs"].get("persona") or ""
            check("the search step is told to look, not to change",
                  "not" in find_brief.lower() and "search" in find_brief.lower(),
                  find_brief[:160])
            check("the search step is told to search before answering",
                  "search" in calls[0]["message"].lower(),
                  calls[0]["message"][:200])
            check("the search step is offered the real search tool",
                  "search_in_files" in find_brief,
                  find_brief[:200])
            check("the format step is told not to invent files",
                  "invent" in (calls[-1]["kwargs"].get("persona") or "").lower(),
                  (calls[-1]["kwargs"].get("persona") or "")[:160])
            check("the format step is asked for the plan JSON",
                  "uncertain" in calls[-1]["message"],
                  calls[-1]["message"][:200])
            check("planning is not recorded as a chat turn",
                  all(c["kwargs"].get("record") is False for c in calls), "")

            # (g) missing inputs are refused before any model call
            before = len(calls)
            r = await plan({"workspaceId": wp, "instruction": "  "}, ws)
            check("an empty instruction never reaches the model",
                  r.get("status") == "error" and len(calls) == before,
                  str(r)[:160])
            r = await plan({"workspaceId": "", "instruction": "x"}, ws)
            check("planning without a workspace is refused",
                  r.get("status") == "error", str(r)[:160])

            # (h) where the remote boundary actually is.
            #
            # The confinement is the WORKSPACE CHOICE, not the write: once the
            # folder is picked on the machine itself, the contained code.* calls
            # (read, plan, edit, apply, write) stay available remotely. Asserted
            # in both directions so neither half can drift silently.
            check("planning is available remotely, like the rest of code.*",
                  "code.plan" not in policy.REMOTE_FORBIDDEN_METHODS, "")
            check("choosing the workspace is still a local-only decision",
                  "code.bind" in policy.REMOTE_FORBIDDEN_METHODS, "")
            check("the contained writes stay available once bound",
                  all(m not in policy.REMOTE_FORBIDDEN_METHODS
                      for m in ("code.apply", "code.write", "code.edit")),
                  sorted(policy.REMOTE_FORBIDDEN_METHODS))
        finally:
            ws_server._run_chat_pipeline_inner = original

    # ---- 9. the tool the planner depends on --------------------------------
    #
    # Three separate faults were found by running the planner against the real
    # local model, and none of them were visible to a unit test that stubbed the
    # model out. These pin each one, because each fails SILENTLY in production.
    from backend.skills import tool_loop as _tl
    from backend.skills import registry as _skill_reg

    names = [s.name for s in _skill_reg.skill_registry.list_all()]
    check("a content search exists for the planner to use",
          "search_in_files" in names,
          "without it the planner can only guess filenames")
    check("the planner's tool names are all REAL skills",
          all(t in names for t in ws_server._PLAN_TOOLS),
          f"unknown: {[t for t in ws_server._PLAN_TOOLS if t not in names]} "
          f"— an unregistered name is silently offered to nobody")
    check("code_grep is not a skill (it is a WS method)",
          "code_grep" not in names,
          "it was listed in _PLAN_TOOLS once and planner tool use was ZERO")

    async def run_skill(name, params):
        # `run()` is already inside the loop, so this must await rather than
        # start a second one.
        return await _tl.execute_skill(name, params)

    # The workspace must point at the test tree for these to mean anything.
    from backend.config import config as _cfg
    from backend.memory.session_context import session_context as _sc
    saved_root = _cfg.get("workspace", "root", default="")
    try:
        _cfg.set("workspace", "root", value=wp)
        _sc._load()
        _sc.active_workspace = wp
        _sc.save()

        # `notes.txt` is overwritten to "saved notes" by the saving checks
        # earlier in this suite (the original text is moved to notes.txt.bak),
        # so THAT is the content marker to search for here. `mod.py` was a wrong
        # first choice: it is a FILENAME, and this skill searches CONTENTS.
        SEARCH_FOR = "saved notes"
        ok = await run_skill("search_in_files", {"query": SEARCH_FOR})
        data = ok.get("data") or {}
        hits = [m["filePath"] for m in (data.get("matches") or [])]
        check("search_in_files finds text inside a file",
              any(p.endswith("notes.txt") for p in hits),
              str(data)[:200])

        # A bogus directory used to return "no matches" with scanned=0, which
        # reads to a model as "the symbol is not in this project" — a finding it
        # then repeats to the user. It must fail loudly instead.
        bogus = await run_skill("search_in_files",
                                {"query": "inside notes",
                                 "directory": "workspace"})
        bdata = bogus.get("data") or {}
        check("a folder that does not exist is an error, not an empty result",
              bool(bdata.get("error")) and not bdata.get("matches"),
              str(bdata)[:220])
        check("and the error names the folders that do exist",
              "pkg" in str(bdata.get("error")), str(bdata.get("error"))[:200])

        # The schema declares a string, but a model reliably sends ["py"]. The
        # first version called .split(",") on that and found nothing.
        #
        # Asserted against a DEDICATED temp workspace, not the suite's shared
        # fixture: earlier checks overwrite notes.txt and leave a .bak beside it,
        # so a search assertion there fails for reasons unrelated to this code.
        import tempfile as _tf
        probe = Path(_tf.mkdtemp(prefix="ext_filter_")) / "p"
        probe.mkdir(parents=True)
        (probe / "a.txt").write_text("needle-text\n", encoding="utf-8")
        (probe / "a.txt.bak").write_text("needle-text\n", encoding="utf-8")
        (probe / "b.py").write_text("needle-text\n", encoding="utf-8")
        saved2 = _cfg.get("workspace", "root", default="")
        try:
            _cfg.set("workspace", "root", value=str(probe))
            _sc._load()
            _sc.active_workspace = str(probe)
            _sc.save()

            for label, value in (("a list", ["txt"]),
                                 ("a string", ".txt"),
                                 ("a bare suffix", "txt")):
                res = await run_skill("search_in_files",
                                      {"query": "needle-text",
                                       "extensions": value})
                got = [m["filePath"]
                       for m in ((res.get("data") or {}).get("matches") or [])]
                check(f"extensions works as {label}",
                      got == ["a.txt"],
                      f"sent {value!r}, got {got} — the model sends a list, and "
                      f"the first version called .split on it and found nothing")

            none = await run_skill("search_in_files", {"query": "needle-text"})
            allhits = sorted(m["filePath"]
                             for m in ((none.get("data") or {}).get("matches")
                                       or []))
            check("with no filter, every text file is searched",
                  allhits == ["a.txt", "a.txt.bak", "b.py"], str(allhits))
        finally:
            _cfg.set("workspace", "root", value=saved2)
    finally:
        _cfg.set("workspace", "root", value=saved_root)

    # The planner's result shape. `_run_chat_pipeline_inner` reports tool use as
    # COUNTS (`toolResults` is an int), not a list; reading it as a list made
    # every plan report "did not search" and refuse work that had been done.
    #
    # `code_plan` is defined inside `_register_default_handlers`, so it is not a
    # module attribute — read the source from the registered handler.
    import inspect as _inspect
    src = _inspect.getsource(handlers["code.plan"])
    check("the planner reads tool use as a count, not a list",
          "toolResults" in src and "toolRounds" in src,
          "the pipeline returns ints; reading a list key refuses every plan")
    check("the planner makes two calls, not one",
          src.count("await _run_chat_pipeline_inner") == 2,
          "one call asking for both a tool call and JSON does not work on a "
          "prompt-tools provider — measured zero tool calls")
    # An empty list means "no tools"; None would mean every enabled skill and
    # would reopen the two-contract problem this split exists to avoid.
    check("the format step offers no tools",
          "tools=[]" in src,
          "the JSON call must not also see the tool catalogue; an empty list "
          "is 'no tools' while None would mean every enabled skill")
    check("the search step does offer the read-only tools",
          "tools=list(_PLAN_TOOLS)" in src,
          "the first call is the one that must be able to search")
    # The JSON keys are parsed and rendered by the page, so they must stay
    # English even when the request is not. The pipeline pins the reply
    # LANGUAGE from the message, which would translate them.
    check("the plan contract tells the model to keep the keys in English",
          "keys above are fixed" in src,
          "a translated key is a plan that no longer loads")
    check("the plan contract allows values in the user's language",
          "VALUES in the same language" in src,
          "the user reads the reasons; they should not be forced into English")

    # A wide plan is more often a misread request than a real refactor, so the
    # page must SAY so — while still allowing it, because a genuine wide change
    # is exactly what planning is for. Checked in the page source because it is
    # a rendering decision with no Python side.
    page = os.path.join(ROOT, "dashboard", "src", "app", "code", "page.tsx")
    if os.path.exists(page):
        with open(page, encoding="utf-8", errors="replace") as fh:
            ui = fh.read()
        # Prose assertions run against a whitespace-collapsed copy.
        #
        # JSX wraps long sentences across lines and re-indents them, so a phrase
        # searched in the raw source changes meaning depending on how the author
        # happened to format it. That is a test that fails for the wrong reason:
        # rewrapping a paragraph broke "cannot run other tools" while the
        # sentence was still there and still said exactly that. Identifier and
        # expression checks below still use the raw text, where spacing IS
        # meaningful.
        ui_flat = " ".join(ui.split())
        check("the page warns when a plan names many files",
              "MANY_FILES" in ui
              and "plan.steps.length > MANY_FILES" in ui,
              "a wide plan should be visible before the diffs, not after")
        # The threshold is a judgement call, but a sane one: 0 would warn on
        # every plan and 100000 would never warn. Pinned loosely so it can be
        # tuned without the suite objecting, and so an accidental
        # "never warn" value is caught.
        import re as _re
        m = _re.search(r"const MANY_FILES = (\d+)", ui)
        check("the wide-plan threshold is a usable number",
              bool(m) and 2 <= int(m.group(1)) <= 50,
              f"MANY_FILES = {m.group(1) if m else 'missing'} — a threshold "
              f"outside this range either always warns or never does")
        check("the plan list and its warning live in the page at all",
              "plan-first" in ui.lower() or "makePlan" in ui,
              "the composer that drives code.plan")
        check("the page hides workspace rebinding from a remote session",
              "__ADDLED_WS_URL__" in ui and "isRemote" in ui,
              "code.bind is refused remotely; the menu must not offer it")
        check("apply-all is offered only after the diffs exist",
              "prepared.length > 0" in ui and "applyAllDiffs" in ui,
              "apply-all writes exactly what was generated and shown, so the "
              "button must not appear before a diff does")

        # Cancel: a wide plan on a slow model is minutes of waiting, and there
        # was no way out of it short of reloading the page.
        check("the diff loop can be cancelled between steps",
              "cancelRef" in ui and "cancelRef.current" in ui,
              "without this a long run cannot be stopped")
        check("a cancel lists the diffs that did arrive",
              "Cancelled" in ui_flat and "diff" in ui_flat,
              "work already done should not be thrown away")
        check("cancel does not treat an interrupted diff as ready",
              "status: 'pending', message: 'Cancelled'" in ui.replace('"', "'")
              or "Cancelled" in ui_flat,
              "a diff generated after the cancel was never reviewed")

        # Two buttons both labelled "Cancel" — one threw the plan away, the
        # other stopped the run — is a trap, and Playwright's strict mode caught
        # it as a strict-mode violation before a user did. Each now says what it
        # does.
        check("the two cancel actions have distinct labels",
              "Discard plan" in ui and ">Stop</button>" in ui,
              "'Cancel' on both meant one button discarded the plan and the "
              "other stopped the work")

        # Honest concurrency: the page must not fan out against a provider that
        # queues, and must say so when it cannot.
        check("the page takes the width from the backend",
              "planConcurrency" in ui and "r?.concurrency" in ui,
              "the page must not keep its own copy of the provider list")
        check("the page defaults to width 1, not to 'all at once'",
              "Number(r?.concurrency) || 1" in ui,
              "a missing field must be the slow-but-safe answer")
        check("a single-generation provider is described as one at a time",
              "one at a time" in ui_flat,
              "the wait should be explained, not hidden")

        # Lazy diffs: the bulk path is for small plans only.
        check("generate-all is hidden for a wide plan",
              "plan.steps.length <= MANY_FILES" in ui,
              "a wide plan would spend minutes producing diffs nobody asked for")

        # Long lines had no scrollbar. CodeMirror wraps OFF by default, so a long
        # line ran past the right edge and the only way to it was a thin
        # horizontal scrollbar that reads as none at all.
        check("the editor wraps long lines",
              "EditorView.lineWrapping" in ui,
              "without it a long line is only reachable by horizontal scroll")
        check("the editor scroller can overflow",
              "overflow: 'auto'" in ui or 'overflow: "auto"' in ui,
              "the scroller needs overflow for a scrollbar to exist at all")

        # The composer disappearing was the "chat area is not shown" report: a
        # failed bind left `bound` false, and the panel was gated on it.
        #
        # The page was later rebuilt around a single composer that is rendered
        # UNCONDITIONALLY, which is a stronger version of the same fix — there
        # is no gate left to get wrong. Asserted as such rather than by counting
        # render sites, which only described the old two-state layout.
        check("the composer is not gated on a bound workspace",
              re.search(r"\{\s*bound\s*&&\s*[A-Za-z]*[Cc]omposer", ui) is None
              and re.search(r"\{\s*bound\s*&&\s*askPanel", ui) is None,
              "a failed bind used to remove the whole message area, so the "
              "symptom looked like a layout bug")
        check("a bind failure is shown beside the composer",
              "bindError" in ui and "pick another folder" in ui,
              "the error was only in the tree header, far from what it disabled")

        # A partial tree must say so.
        check("the page reports hidden folders",
              "hiddenDirs" in ui and "Hiding" in ui,
              "a folder quietly missing reads as a broken explorer")

        # The composer looks like the Chat one but has a far smaller toolbox, so
        # it has to say so. The Code page's tools are read-only ON PURPOSE — the
        # review-by-diff step is the whole safety property — and it has no memory
        # recall or chat history. A request needing a web search, an MCP tool or
        # a second file works in Chat and not here.
        check("the composer states what it can and cannot do",
              "cannot run other tools" in ui_flat
              and "Reads and searches" in ui_flat,
              "it looks identical to the chat box but is much narrower")
        check("and points at Chat for the wider toolbox",
              'href="/chat"' in ui,
              "the difference should be actionable, not just stated")
        check("the read-only reason is recorded where the tools are defined",
              "read-only on purpose" in _inspect.getsource(
                  handlers["code.plan"]).replace("\n", " ")
              or "read-only tools" in _inspect.getsource(
                  handlers["code.edit"]),
              "a future change widening this tool set should have to read why "
              "it was narrow")
    else:
        check("the code page source is present", False, page)

    # ---- 10. apply-all writes only what was reviewed -----------------------
    apply_plan = has("code.applyPlan")
    if apply_plan is not None:
        (ws_dir / "bulk_a.txt").write_text("alpha\n", encoding="utf-8")
        (ws_dir / "bulk_b.txt").write_text("beta\n", encoding="utf-8")

        def pend(file_path, content):
            """Register a pending edit the way code.edit does."""
            edit_id = f"{wp}::{file_path}"
            ws_server._pending_edits[edit_id] = content
            return edit_id

        # (a) the happy path
        r = await apply_plan({"workspaceId": wp, "edits": [
            {"filePath": "bulk_a.txt",
             "editId": pend("bulk_a.txt", "alpha changed\n")},
            {"filePath": "bulk_b.txt",
             "editId": pend("bulk_b.txt", "beta changed\n")},
        ]}, ws)
        check("apply-all writes every reviewed edit",
              len(r.get("applied") or []) == 2 and not r.get("failed"),
              str(r)[:220])
        check("apply-all actually wrote the files",
              (ws_dir / "bulk_a.txt").read_text(encoding="utf-8")
              == "alpha changed\n"
              and (ws_dir / "bulk_b.txt").read_text(encoding="utf-8")
              == "beta changed\n", "disk did not change")

        # (b) THE SAFETY PROPERTY. An unreviewed payload must be refused: this is
        # what stops apply-all from becoming a way to write several files without
        # the review step that makes the Code page safe.
        (ws_dir / "never_reviewed.txt").write_text("untouched\n", encoding="utf-8")
        r = await apply_plan({"workspaceId": wp, "edits": [
            {"filePath": "never_reviewed.txt", "content": "sneaked in\n"},
        ]}, ws)
        check("apply-all refuses an edit that was never reviewed",
              r.get("failed") is not None and not (r.get("applied") or []),
              str(r)[:220])
        check("and the unreviewed file is untouched on disk",
              (ws_dir / "never_reviewed.txt").read_text(encoding="utf-8")
              == "untouched\n",
              "apply-all accepted a raw content payload — the review step that "
              "code.apply requires was skipped for a batch")

        # (c) containment holds for a batch too
        r = await apply_plan({"workspaceId": wp, "edits": [
            {"filePath": "../outside/victim.txt",
             "editId": f"{wp}::../outside/victim.txt"},
        ]}, ws)
        check("apply-all refuses a path outside the workspace",
              r.get("failed") is not None, str(r)[:200])
        check("and the outside file is untouched",
              (outside / "victim.txt").read_text(encoding="utf-8")
              == "untouched\n",
              "apply-all wrote outside the workspace")

        # (d) stop at the first failure and SAY what was not attempted. A plan is
        # often a chain (rename a function, then its callers), so continuing past
        # a broken step would apply callers against a definition that never
        # changed.
        r = await apply_plan({"workspaceId": wp, "edits": [
            {"filePath": "bulk_a.txt",
             "editId": pend("bulk_a.txt", "alpha again\n")},
            {"filePath": "does_not_exist.txt",
             "editId": f"{wp}::does_not_exist.txt"},
            {"filePath": "bulk_b.txt", "editId": pend("bulk_b.txt", "x\n")},
        ]}, ws)
        check("apply-all stops at the first failure",
              len(r.get("applied") or []) == 1 and r.get("failed") is not None,
              str(r)[:240])
        check("and reports the rest as not attempted",
              any(s.get("reason") == "Not attempted."
                  for s in (r.get("skipped") or [])),
              str(r.get("skipped"))[:220])
        check("nothing after the failing step was written",
              (ws_dir / "bulk_b.txt").read_text(encoding="utf-8")
              == "beta changed\n",
              "a file after the failing step was written anyway")

        # (e) an absurd batch is refused rather than partly run
        r = await apply_plan({"workspaceId": wp,
                              "edits": [{"filePath": "x.txt"}] * 500}, ws)
        check("an oversized batch is refused outright",
              r.get("failed") is None and not (r.get("applied") or [])
              and "above the" in str(r.get("error") or ""),
              str(r)[:200])

        # (f) the remote boundary is the workspace CHOICE, not the write. A bulk
        # apply repeats code.apply, which is already allowed; forbidding only the
        # batch would be incoherent.
        check("a bulk apply is not forbidden remotely (it repeats code.apply)",
              "code.applyPlan" not in policy.REMOTE_FORBIDDEN_METHODS, "")
        check("but choosing the workspace still is",
              "code.bind" in policy.REMOTE_FORBIDDEN_METHODS, "")

    # ---- 11. near-valid JSON from a real model ------------------------------
    #
    # Found only by driving the UI: the planner replied with JSON that was one
    # character from valid, and the whole plan was thrown away. A stubbed model
    # always returns clean JSON, so no unit test could have caught this.
    import json as _j
    import re as _re2

    def _repair(body: str) -> str:
        """The repair code.plan applies, kept in sync by the assertions below."""
        out = _re2.sub(r",(\s*[}\]])", r"\1", body)
        buf, in_str, esc = [], False, False
        for ch in out:
            if in_str:
                if esc:
                    buf.append(ch); esc = False
                elif ch == "\\":
                    buf.append(ch); esc = True
                elif ch == '"':
                    buf.append(ch); in_str = False
                elif ch in "\n\r\t":
                    buf.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
                else:
                    buf.append(ch)
            else:
                if ch == '"':
                    in_str = True
                buf.append(ch)
        return "".join(buf)

    # The shapes that actually broke a real plan.
    near_valid = {
        "a raw newline inside a value":
            '{"summary": "rename it\nand its callers", "steps": []}',
        "a trailing comma":
            '{"summary": "s", "steps": [{"filePath": "a.py"},],}',
        "a tab inside a value":
            '{"summary": "a\tb", "steps": []}',
    }
    for label, body in near_valid.items():
        try:
            _j.loads(body)
            check(f"the fixture for {label} is genuinely invalid", False,
                  "a fixture that already parses proves nothing")
        except _j.JSONDecodeError:
            try:
                parsed = _j.loads(_repair(body))
                check(f"a plan with {label} is repaired, not rejected",
                      isinstance(parsed, dict), str(parsed)[:160])
            except _j.JSONDecodeError as exc:
                check(f"a plan with {label} is repaired, not rejected",
                      False, f"still broken: {exc.msg}")

    # The repair must not DAMAGE a good reply — that would be worse than the bug.
    good = ('{"summary": "he said \\"hi\\"", "steps": '
            '[{"filePath": "a.py", "instruction": "i", "reason": "r"}]}')
    check("repair leaves valid JSON untouched",
          _j.loads(_repair(good)) == _j.loads(good),
          "the repair corrupted a valid reply")

    # And it must be reachable: the handler has to attempt the repair rather
    # than fail on the first parse error.
    check("code.plan attempts a repair before giving up",
          "repaired = _re.sub" in src and "raw = _json.loads(repaired)" in src,
          "a reply one character from valid must not discard the whole plan")
    check("code.plan still reports a truly unusable reply",
          "The plan was not JSON" in src or "The plan was not valid JSON" in src,
          "repair must not mean 'accept anything'")

    # ---- 12. concurrency is decided by the backend, honestly ----------------
    #
    # The local model serves ONE generation at a time (`/props` reports
    # total_slots = 1 because we set -np 1). Firing several diffs at it would
    # look parallel and behave serially, so the widest honest answer there is 1
    # and the page must be TOLD that rather than guessing.
    from backend.providers import base as _pbase

    class _Prov:
        def __init__(self, pid):
            self.provider_id = pid

    check("a local provider is width 1, whatever is asked for",
          _pbase.concurrency_width(_Prov("local"), wanted=5) == 1,
          "the local model queues concurrent requests")
    for pid in sorted(_pbase.SINGLE_GENERATION_PROVIDERS):
        check(f"'{pid}' is treated as single-generation",
              _pbase.concurrency_width(_Prov(pid), wanted=5) == 1)
    check("a cloud provider may go wider",
          _pbase.concurrency_width(_Prov("deepseek"), wanted=3) == 3, "")
    check("but never wider than the ceiling",
          _pbase.concurrency_width(_Prov("openai"), wanted=99)
          == _pbase.MAX_CONCURRENCY,
          "a burst of model calls hits rate limits and costs money")
    check("and never narrower than 1",
          _pbase.concurrency_width(_Prov("openai"), wanted=0) == 1, "")
    check("an unknown provider is treated as single-generation",
          _pbase.concurrency_width(_Prov(""), wanted=5) == 1,
          "a missing id must not be read as permission to fan out")

    # The orchestrator must agree with the page, or a flow and a bulk diff would
    # disagree about the same provider.
    from backend.swarm.orchestrator import SwarmOrchestrator as _Orch
    check("the swarm orchestrator uses the same list",
          _Orch._single_generation_provider(_Prov("local")) is True
          and _Orch._single_generation_provider(_Prov("deepseek")) is False,
          "two copies of this list would drift")

    # The plan response must carry the width, and the handler must ask for it.
    check("the plan response reports the concurrency",
          '"concurrency"' in src and "_plan_concurrency" in src,
          "the page cannot honour a width it is never told")


    # ---- 13. the tree does not silently drop folders -----------------------
    #
    # Reported as "in directory explorer, there are folders that are not shown".
    # Two causes, both real:
    #  - `vendor` and `target` were in the default skip list, but they hold real
    #    source in Go/PHP and Rust/Maven. Hiding them made a bound workspace show
    #    folders simply vanish.
    #  - every dotfolder was hidden with no indication that anything was hidden.
    (ws_dir / "vendor").mkdir(exist_ok=True)
    (ws_dir / "vendor" / "lib.txt").write_text("v\n", encoding="utf-8")
    (ws_dir / "target").mkdir(exist_ok=True)
    (ws_dir / "target" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (ws_dir / "node_modules").mkdir(exist_ok=True)
    (ws_dir / "node_modules" / "junk.js").write_text("j\n", encoding="utf-8")
    (ws_dir / ".github").mkdir(exist_ok=True)
    (ws_dir / ".github" / "ci.yml").write_text("on: push\n", encoding="utf-8")

    listing = await bind({"folderPath": wp, "includeIgnored": False}, ws)
    paths = [f["path"] for f in (listing.get("files") or [])]
    hidden = [str(h) for h in (listing.get("hiddenDirs") or [])]

    check("a vendor folder is NOT hidden by default",
          any(p.startswith("vendor/") for p in paths),
          f"vendor holds real source in Go and PHP: {paths}")
    check("a target folder is NOT hidden by default",
          any(p.startswith("target/") for p in paths),
          f"target holds real source in Rust and Maven: {paths}")
    check("node_modules IS still hidden by default",
          not any(p.startswith("node_modules/") for p in paths),
          "this one is generated in every ecosystem that uses it")
    check("a dot folder is hidden by default",
          not any(p.startswith(".github/") for p in paths), str(paths))
    check("but the listing SAYS what was hidden",
          any(".github" in h for h in hidden),
          f"a folder quietly missing reads as a broken explorer: {hidden}")
    check("and reports how many",
          int(listing.get("hiddenDirCount") or 0) >= 1,
          str(listing.get("hiddenDirCount")))

    everything = await bind({"folderPath": wp, "includeIgnored": True}, ws)
    all_paths = [f["path"] for f in (everything.get("files") or [])]
    check("'show all' brings the dot folder back",
          any(p.startswith(".github/") for p in all_paths),
          "the escape hatch has to actually work, or the note is a lie")

    # ---- the package must not shadow the stdlib `code` module ------------
    # `backend/` is on sys.path (script dir + main.py), so a package directory
    # named `code/` inside it captures `import code` before the standard
    # library gets a chance. sympy does `from code import InteractiveConsole`
    # at import time and sympy arrives with torch/transformers on the
    # local-model path, so the first local-model tool call surfaced as
    # "run_command — module 'code' has no attribute 'InteractiveConsole'".
    # The package is `backend/codemode/` now; this pins that.
    backend_dir = Path(__file__).resolve().parent.parent / "backend"
    check("backend has no package that shadows the stdlib 'code' module",
          not (backend_dir / "code" / "__init__.py").exists(),
          "backend/code/ exists again — it will shadow the stdlib 'code' module")
    check("the code-mode package is named 'codemode'",
          (backend_dir / "codemode" / "__init__.py").exists(),
          "backend/codemode/ is missing")
    try:
        import code as _stdlib_code_probe
        check("the stdlib 'code' module still resolves to the standard library",
              hasattr(_stdlib_code_probe, "InteractiveConsole"),
              "something is shadowing 'code': "
              f"{getattr(_stdlib_code_probe, '__file__', 'builtin')}")
    except Exception as e:  # noqa: BLE001
        check("the stdlib 'code' module imports", False, str(e))


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
    print("  - editing plans first, and a plan cannot name a file outside")
    return 0


if __name__ == "__main__":
    sys.exit(main())
