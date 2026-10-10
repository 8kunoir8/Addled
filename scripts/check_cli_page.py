"""CLI Tools page wiring: is the feature actually reachable from the dashboard?

The bug this exists for is silent. Every RPC in this feature works, the page
component is complete, and the whole thing is still unusable because a tab was
never added to the section list or the component was never rendered — a second
implementation of the tab, or an icon missing from `SECTION_ICONS`, or worse, a
page that offers to save a tool without ever showing the source first.

The last one is the one worth guarding hardest. `draft` and `apply` are separate
RPCs specifically so a human reads the code before it is written. A page that
called `apply` with the model's source and never displayed it would satisfy
every backend test and defeat the entire point, so the review step is asserted
here as a presence requirement.

Like `check_code_page.py`, this is about CONNECTION, not behaviour: the
handlers' behaviour is covered by `check_cli_build.py` and `check_cli_tools.py`,
and a page's own behaviour needs a browser. What can be checked here is that the
wiring exists at all, because "present but unused" is the failure that happens.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_cli_page.py
"""

import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

SETTINGS = os.path.join(ROOT, "dashboard", "src", "app", "settings",
                        "page.tsx")
SECTION = os.path.join(ROOT, "dashboard", "src", "app", "settings",
                       "cli-tools-section.tsx")
TYPES = os.path.join(ROOT, "dashboard", "src", "lib", "ws-types.ts")
SERVER = os.path.join(ROOT, "backend", "ws_server.py")
CHAT = os.path.join(ROOT, "dashboard", "src", "app", "chat", "page.tsx")
CODE = os.path.join(ROOT, "dashboard", "src", "app", "code", "page.tsx")

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    for path in (SETTINGS, SECTION, TYPES, SERVER, CHAT, CODE):
        if not os.path.isfile(path):
            print(f"missing file: {path}")
            return 2
    settings = read(SETTINGS)
    section = read(SECTION)
    types = read(TYPES)
    server = read(SERVER)
    chat = read(CHAT)
    code = read(CODE)

    print("=== the section is reachable ===")
    check("the section has an icon, or it cannot be deep-linked",
          "'cli-tools':" in settings or '"cli-tools":' in settings)
    check("it is in the section list, or there is no tab",
          re.search(r"const sections = \[[^\]]*'cli-tools'", settings) is not None)
    check("the component is rendered for it",
          "activeSection==='cli-tools'" in settings
          and "<CliToolsSection" in settings)
    check("and it is imported",
          re.search(r"import CliToolsSection from", settings) is not None)

    print("=== every RPC the page needs is implemented on the backend ===")
    for method, handler in [
        ("cliTools.list", "cli_tools_list"),
        ("cliTools.get", "cli_tools_get"),
        ("cliTools.draft", "cli_tools_draft"),
        ("cliTools.apply", "cli_tools_apply"),
        ("cliTools.test", "cli_tools_test"),
        ("cliTools.remove", "cli_tools_remove"),
        ("cliTools.suggest", "cli_tools_suggest"),
    ]:
        check(f"ws_server implements {method}",
              f'"{method}"' in server and f"def {handler}" in server)

    print("=== the page calls them ===")
    for method in ["cliTools.list", "cliTools.draft", "cliTools.apply",
                   "cliTools.test", "cliTools.remove", "cliTools.get",
                   "cliTools.suggest"]:
        check(f"the page calls {method}", f"'{method}'" in section)

    print("=== the review step survives ===")
    # The property the whole feature rests on. `apply` must not be reached from
    # the draft path without the source being shown and editable first.
    check("there is a textarea showing the source before saving",
          "value={source}" in section and "onChange" in section)
    check("editing the source is what gets saved, not the model's copy",
          "source," in section or "source:" in section)
    check("saving is a separate action from writing",
          section.count("doDraft") >= 2 and section.count("doSave") >= 2)
    check("the page says the code runs on the user's machine",
          "run on your machine" in section.lower()
          or "what will run" in section.lower(),
          "the review must explain why it is being shown")

    print("=== the deep link from chat arrives filled in ===")
    check("the page reads ?prefill", "prefill" in section)
    check("and asks the backend to derive a name from it",
          "cliTools.suggest" in section)
    check("the settings page accepts a section deep link",
          "window.location.search" in settings and "section" in settings)

    print("=== nothing is fetched-and-run without review ===")
    check("the page never calls apply without a draft in hand",
          re.search(r"const doSave = async \(\) => \{\s*if \(!draft", section)
          is not None,
          "apply must be guarded by a draft")
    check("the draft is cleared after a successful save, so a second save "
          "cannot write a stale tool",
          re.search(r"setDraft\(null\)", section) is not None)

    print("=== the types the page relies on are declared ===")
    for name in ["CliToolSummary", "CliToolListResult", "CliToolDraftResult",
                 "CliToolApplyResult", "CliToolTestResult",
                 "CliToolSuggestResult"]:
        check(f"ws-types declares {name}", f"interface {name}" in types)

    print("=== the page does not use `any` ===")
    # The rest of the dashboard has some, but this page is new and typed on
    # purpose: the RPC boundary is exactly where an untyped value would let a
    # malformed result render as a working tool.
    check("no `: any` in the section", ": any" not in section)
    check("no `as any` in the section", "as any" not in section)

    print("=== the feature is discoverable from the pages it serves ===")
    # A capability the user does not know they can build is one they will never
    # build, and nothing else in the app names this page.
    check("the chat page points at it",
          "/settings?section=cli-tools" in chat,
          "chat is where a missing capability is felt")
    check("the code page points at it",
          "/settings?section=cli-tools" in code)
    check("both links import `Link`, or they render as plain anchors "
          "and reload the app",
          "from 'next/link'" in chat and "from 'next/link'" in code)
    check("the chat pointer only shows on an empty conversation",
          "messages.length === 0" in chat)

    print("=== the question card carries the offer ===")
    # The offer rides the EXISTING question mechanism, which is what makes it
    # appear without a new card component. `_needs_tool` must therefore return a
    # question-shaped result, and the loop reads these three keys at top level.
    tools_loop = read(os.path.join(ROOT, "backend", "skills", "tool_loop.py"))
    for key in ['"question":', '"options":', '"question_id":']:
        check(f"the offer result sets top-level {key}",
              key in tools_loop,
              "the loop only stops on requires_answer when these are present")
    check("and it is raised as a real question, not a bare error",
          "pending.ask" in tools_loop or "_ask_to_acquire" in tools_loop)

    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        for failure in fails:
            print(f"  - {failure}")
        return 1
    print("PASS: the CLI Tools page is wired to the backend that serves it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
