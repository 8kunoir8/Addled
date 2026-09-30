"""Code page wiring: does the dashboard actually USE what the backend returns?

The bug this exists for is not a crash. `code.git_status`, `code.git_diff`,
`code.git_revert` and `code.verify` were all implemented on the backend and
`code.applyPlan` was already returning `git: {sha}` and `verify: {verdict}` on
every apply — and the Code page called none of them and read neither field. The
result was a page that could write a file but could not answer "what did you
just change?", "can I undo it?" or "did it pass?". Nothing failed; a capability
was simply never connected, which no runtime test would notice.

So this asserts the CONNECTION, by reading the page source. It is deliberately
about presence, not behaviour: behaviour for the backend half is covered by
`check_gitops.py` and `check_verify.py`, and the page's own behaviour needs a
browser. What can be checked here is that the wiring exists at all, because
"present but unused" is the exact failure that happened.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_code_page.py
"""

import os
import re
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

PAGE = os.path.join(ROOT, "dashboard", "src", "app", "code", "page.tsx")
TYPES = os.path.join(ROOT, "dashboard", "src", "lib", "ws-types.ts")
SERVER = os.path.join(ROOT, "backend", "ws_server.py")
# The two modules the rebuilt page depends on. Checked here rather than in a
# suite of their own because they exist FOR this page.
CSTORE = os.path.join(ROOT, "dashboard", "src", "lib", "codeSessionStore.ts")
ATTS = os.path.join(ROOT, "dashboard", "src", "lib", "attachments.ts")

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{label}: {detail}" if detail else label)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    for path in (PAGE, TYPES, SERVER, CSTORE, ATTS):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2
    page = read(PAGE)
    types = read(TYPES)
    server = read(SERVER)
    cstore = read(CSTORE)
    atts = read(ATTS)

    # -- the four methods that were implemented and never called --------------
    # The names are the REGISTERED WIRE names, which are dot-separated and do
    # NOT match the Python function names (`code_git_status` registers as
    # `code.git.status`). Calling the Python spelling is a silent no-op — the
    # dispatch table has no such method — which was the first bug this check
    # found, in the very change that added the calls.
    for method in ("code.git.status", "code.git.diff", "code.git.revert",
                   "code.verify"):
        check(f"the page calls {method}", method in page,
              "implemented on the backend but not reachable from the UI")
    # Guard the specific mistake rather than trusting the reader to notice.
    #
    # Only the CALL SITES count. A plain substring search would also match the
    # explanatory comments in the page, which is how an earlier version of this
    # check failed on prose while the code was already correct. Everything sent
    # goes through `send('...')`, so that is what is inspected.
    called = set(re.findall(r"send\(\s*['\"]([a-zA-Z0-9_.]+)['\"]", page))
    for wrong in ("code.git_status", "code.git_diff", "code.git_revert"):
        check(f"the page does not send the non-existent {wrong}",
              wrong not in called,
              "the Python function name is not the wire method name")
    for right in ("code.git.status", "code.git.diff", "code.git.revert"):
        check(f"the page sends {right}", right in called,
              f"send() call site missing; saw {sorted(called)}")

    # The strongest form of the same check: every code.* method the page sends
    # must actually be registered by the backend. This is what catches a typo
    # that a reader would miss, and it needs no list to be kept in step.
    registered = set(re.findall(
        r"_server\.register\(\s*['\"]([a-zA-Z0-9_.]+)['\"]", server))
    check("the registration table was found", len(registered) > 50,
          f"only {len(registered)} methods parsed from ws_server.py")
    for method in sorted(m for m in called if m.startswith("code.")):
        check(f"{method} is registered by the backend", method in registered,
              "the dashboard sends a method the dispatch table does not have")

    # -- what applyPlan already returns must not be discarded -----------------
    # `r.git` is the checkpoint; `r.verify` is the verdict. Both were returned
    # and both were ignored, which is why an applied change was irreversible.
    check("the page reads the git result of an apply",
          re.search(r"\br\.git\b|\br\.git\?\.", page) is not None,
          "the sha that makes an apply undoable is being dropped")
    check("the page reads the verify result of an apply",
          re.search(r"\br\.verify\b|\br\.verify\?\.", page) is not None,
          "the check the backend ran is being dropped")

    # -- the checkpoint has both undo paths -----------------------------------
    check("a checkpoint is held after an apply", "setCheckpoint(" in page)
    check("the checkpoint records the commit sha", "sha: r.git?.used" in page,
          "without the sha the undo cannot use git revert")
    check("the checkpoint records the per-file backups", "backup: d.backup" in page,
          "without a backup a non-repo workspace has no undo at all")
    check("undo is offered in the UI", "undoLastChange" in page)
    check("undo prefers git revert",
          "code.git.revert" in page and "viaGit" in page)
    # The non-repo path restores from the backup read back through code.read,
    # so it goes through the same containment as every other write.
    check("the fallback restores through code.read + code.write",
          "f.backup" in page and "filePath: f.filePath, content" in page)

    # -- the verify verdict distinguishes written from verified ---------------
    check("a verify badge is rendered", "verifyBadge" in page)
    # The honesty rule: a check that never ran must not render as a pass.
    check("an un-run check is not shown as a pass",
          "if (!v.ran)" in page,
          "a green tick over nothing is worse than no tick")
    check("the not-verified state is labelled as such",
          "not verified" in page)

    # -- the git panel ---------------------------------------------------------
    # The panel moved from a tab under the editor to a tab in the drawer when
    # the page was rebuilt, but it must still exist: "what did Addled change" is
    # the question the checkpoint work answered.
    check("the drawer has a changes tab",
          "drawerTab === 'changes'" in page or "changesPanel" in page,
          "the git panel was lost in the layout rebuild")
    check("a non-repo workspace is stated, not treated as an error",
          "Not a git repository." in page)
    check("the non-repo path still promises undo",
          ".bak" in page and "Undo still works" in page)
    # Search survived the rebuild too.
    check("the drawer has a search tab",
          "drawerTab === 'search'" in page and "searchPanel" in page,
          "workspace search was lost in the layout rebuild")

    # -- the shell: drawer, transcript, composer ------------------------------
    # The layout is a sidebar plus a message stream plus a composer, which is
    # what was asked for after the tree/editor split read as broken.
    check("there is a sessions drawer", "sessionRow" in page and "Sessions" in page)
    check("the transcript is rendered as a stream",
          "activeSession.messages.map" in page,
          "the session's messages are not shown")
    check("an empty session says what to do", "Describe a change" in page)
    check("the composer is not gated on a file being open",
          "no file open" in page.lower() or "No file open" in page,
          "the old page showed a dead tree when nothing was open")
    check("there is a tree/chat view switch",
          "setView('chat')" in page and "setView('editor')" in page)
    check("context chips state the workspace and branch",
          "contextChips" in page and "gitStatus.branch" in page)

    # -- the shell must survive a narrow window -------------------------------
    # Measured in the running app at a 288px viewport: the 248px drawer is
    # `shrink-0`, so it won the whole width and the working column rendered at
    # ZERO. The old page auto-collapsed the sidebar and the rebuild dropped the
    # guard while keeping the fixed width, so the failure it documented came
    # straight back. Asserted as behaviour, because this is invisible at the
    # width a developer normally tests at.
    check("the sidebar auto-collapses on a narrow window",
          "window.innerWidth >= 720" in page,
          "a fixed 248px drawer with shrink-0 starves the composer to 0 width")
    check("a manual toggle is remembered against the auto-collapse",
          "drawerToggled" in page,
          "otherwise resizing would undo a deliberate choice")
    check("the drawer is capped as a fraction of the viewport",
          "max-w-[60vw]" in page,
          "between the collapse threshold and ~420px the drawer still crowds")
    check("the dead tree-toggle state is gone",
          "setTreeOpen" not in page and "treeToggled" not in page,
          "leftover state from the previous layout")

    # -- sessions -------------------------------------------------------------
    check("sessions are persisted, not just held in memory",
          "localStorage" in cstore,
          "a code session spans days; losing it on reload defeats naming it")
    check("a session is created for a workspace on bind",
          "sessionForWorkspace" in page,
          "binding should land in the work you were doing there")
    check("a session is titled from the first prompt",
          "titleFromPrompt" in page)
    check("sessions can be pinned and deleted",
          "togglePinned" in page and "deleteSession" in page)

    # -- attachments ----------------------------------------------------------
    check("pasted files become attachments", "onPaste={onComposerPaste}" in page)
    check("paste reads the clipboard for files",
          "filesFromPaste" in atts)
    check("dropping files attaches them", "onDrop={onDrop}" in page)
    check("images are downscaled before sending",
          "downscaleImage" in atts,
          "a full screenshot as base64 is slow to send and more than is needed")
    check("attachments are sent to the planner",
          "attachments: sentAttachments" in page,
          "the composer accepts files the request never carries")
    check("the backend reads attachments for a plan",
          'params.get("attachments")' in server)
    check("the backend no longer invents its own attachment handling",
          "_analyze_attachments(" in server,
          "code.plan should reuse the chat's analyser, not duplicate it")
    check("object URLs are released when an attachment is removed",
          "releaseAttachment" in atts and "releaseAttachment(prev[index])" in page,
          "a long session that pasted many screenshots would leak every one")

    # -- right-click ---------------------------------------------------------
    check("the editor offers a selection menu", "selMenu" in page)
    check("the selection menu can explain / refactor / test",
          "askAboutSelection('explain')" in page
          and "askAboutSelection('refactor')" in page
          and "askAboutSelection('test')" in page)
    check("a file can be attached to the prompt from the tree",
          "attachFileToPrompt" in page)
    check("copy path is offered", "copyPath" in page)
    check("the tree background has its own menu", "treeMenu" in page)
    check("a folder can be searched from its menu",
          "Search inside this folder" in page)
    check("reveal degrades honestly when it cannot open a file manager",
          "Reveal in file manager" in page and "Path copied" in page,
          "a reveal that silently does nothing is worse than saying so")

    # -- @-mentions travel as a hint, and are sent ----------------------------
    check("the composer sends contextFiles", "contextFiles:" in page)
    check("the selection is read from the editor",
          "currentSelection" in page and "state.selection.main" in page)
    # The backend must actually consume the hint, or the chips are decorative.
    check("the backend reads contextFiles",
          "params.get(\"contextFiles\")" in server,
          "the page sends a hint the backend ignores")
    check("a named file is a STARTING POINT, not the answer",
          "start from them" in server,
          "a mention must not tell the planner that only that file changes")

    # -- whole-file review is stated, because it is not per-change ------------
    check("the diff states its review granularity",
          "whole file" in page,
          "Claude Code offers per-change review; this does not, and "
          "silence would imply it does")

    # -- the safety property is unchanged -------------------------------------
    # Only an editId that code.edit produced may be applied. If this ever
    # stops being true, the review step stops meaning anything.
    check("apply still keys on the reviewed editId",
          "editId: p.editId" in page)

    # -- the types exist so an unread field is visible ------------------------
    for name in ("CodeGitStatus", "CodeGitDiff", "CodeGitRevert",
                 "CodeVerifyResult", "CodeApplyPlanResult", "CodeAppliedFile"):
        check(f"{name} is declared", f"interface {name}" in types)

    if fails:
        print(f"FAIL: {len(fails)} problem(s)")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: the Code page uses the checkpoint, the verify verdict and git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
