"""Plan mode must be read-only, reachable, and must leave an artefact.

Three properties, and the first is the one with teeth: a plan that writes while
it is being made is not a plan. The danger is specific - a read-only tool list
DERIVED from a name pattern picked up `memory_link` and `memory_unlink`, both of
which write, so the list here is explicit and this check asserts that every name
in it exists and that none of the writing skills got in.

The other two: `/plan` has to work from any surface, not just the Code page, and
the plan has to survive the conversation, because the reason to plan a risky
change is to follow the plan later.

The revert-verification this was built against: adding a writing tool to
`_PLAN_TOOLS` makes the read-only assertion fail, and removing the prefix strip
makes the reachability assertion fail.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_plan_mode.py
"""

import inspect
import os
import sys

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


# Verbs that mean a tool can change something, matched on WORD boundaries.
#
# Word boundaries, not substrings: a substring match flags `skill_view` (which
# contains "kill") and `skill_search` (which contains "set"), and the failure is
# a false alarm that would get the rule deleted. The first version of this check
# did exactly that and reported two innocent tools.
WRITING_VERBS = ("write", "create", "delete", "edit", "move", "remove",
                 "install", "add", "set", "run", "exec", "link", "unlink",
                 "patch", "replace", "append", "rename", "kill", "stop",
                 "start", "launch", "send", "post", "put", "upload")


def _writes(name: str) -> bool:
    """Whether a tool name contains a writing verb as a whole word."""
    import re as _re
    words = _re.split(r"[^a-z0-9]+", name.lower())
    return any(w in WRITING_VERBS for w in words if w)


def main() -> int:
    print("check_plan_mode")

    try:
        from backend.ws_server import _PLAN_TOOLS, _PLAN_PROMPT, _write_plan_file
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL could not import the plan-mode pieces: {e}")
        print()
        print("FAILED (1): could not import the plan-mode pieces")
        return 1

    # Deriving the read-only set was the original mistake, so this asserts
    # against the derivation rather than trusting it.
    bad = [t for t in _PLAN_TOOLS if _writes(t)]
    check("no tool in plan mode can write",
          not bad,
          f"these look like they change something: {bad}")
    check("the list is not empty",
          len(_PLAN_TOOLS) > 5, f"{len(_PLAN_TOOLS)} tools")
    check("the list has no duplicates",
          len(_PLAN_TOOLS) == len(set(_PLAN_TOOLS)))

    # Every name must exist, or the planner is offered a tool that is not there
    # and will report itself unable to research.
    try:
        from backend.skills.registry import skill_registry
        missing = [t for t in _PLAN_TOOLS if skill_registry.get(t) is None]
        check("every plan tool exists in the registry",
              not missing, f"unknown: {missing}")
        # And the specific pair the derivation let through.
        check("memory_link is NOT offered in plan mode",
              "memory_link" not in _PLAN_TOOLS,
              "it writes; deriving the list from a name pattern let it in")
        check("memory_unlink is NOT offered in plan mode",
              "memory_unlink" not in _PLAN_TOOLS)
    except Exception as e:  # noqa: BLE001
        print(f"       registry unavailable: {e}")

    # ------------------------------------------------------------- the prompt
    check("the prompt says the tools are read-only",
          "READ-ONLY" in _PLAN_PROMPT or "read-only" in _PLAN_PROMPT)
    check("the prompt says not to modify the system",
          "must not try" in _PLAN_PROMPT)
    # The craft rule from Hermes and Claude Code — the reason a plan contains
    # literal commands instead of a summary.
    check("the prompt asks for a plan a stranger could follow",
          "never seen" in _PLAN_PROMPT and "literally" in _PLAN_PROMPT)
    check("the prompt asks for unknowns rather than guesses",
          "Unknowns" in _PLAN_PROMPT)

    # ------------------------------------------------------------ the artefact
    import tempfile
    from backend import app_paths

    # Point the data dir at a real temporary directory rather than stubbing
    # `subdir`. `subdir` CREATES the directory it returns (that is its whole
    # job), so a stub that only builds a Path makes every write here fail - and
    # the failure looks like a bug in `_write_plan_file` rather than in the
    # check. The first version of this check did that and reported the plan
    # writer as broken.
    tmp = tempfile.mkdtemp(prefix="addled_plan_")
    original = app_paths.MEMORY_DIR
    app_paths.MEMORY_DIR = __import__("pathlib").Path(tmp)
    try:
        path = _write_plan_file("Refactor the payment flow!", "1. Do the thing.")
        check("a plan is written to disk", bool(path) and os.path.exists(path),
              repr(path))
        if path and os.path.exists(path):
            body = open(path, encoding="utf-8").read()
            check("the plan file names the request",
                  "Refactor the payment flow!" in body, body[:60])
            check("the plan body is in the file",
                  "1. Do the thing." in body)
            check("the filename is a safe slug",
                  "!" not in os.path.basename(path)
                  and " " not in os.path.basename(path),
                  os.path.basename(path))
            check("the filename carries a date",
                  os.path.basename(path)[:4].isdigit(),
                  os.path.basename(path))
        check("an empty request still yields a usable file",
              bool(_write_plan_file("", "steps")))
    finally:
        app_paths.MEMORY_DIR = original
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------- reachable
    src = inspect.getsource(
        __import__("backend.ws_server", fromlist=["x"]).run_chat_pipeline)
    check("plan mode is entered by a /plan prefix",
          '"/plan"' in src, "it must be reachable from any surface")
    check("the prefix is stripped from the request",
          "_stripped[len(_prefix):]" in src,
          "leaving it in sends the literal text to the model")
    check("plan mode restricts the tools",
          "tools = list(_PLAN_TOOLS)" in src)
    check("plan mode forces the plan role",
          'force_role = "plan"' in src)
    check("plan mode writes the artefact on the way out",
          "_write_plan_file(message, text)" in src)
    check("the plan path is reported back to the surface",
          '"plan_path"' in src)

    # The role has to exist, or forcing it is a no-op that silently falls
    # through to chat.
    from backend.providers.router import ROLES
    check("plan is a real routing role", "plan" in ROLES, repr(ROLES))
    check("judge is a real routing role", "judge" in ROLES, repr(ROLES))

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + "; ".join(fails))
        return 1
    print("all plan-mode checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
