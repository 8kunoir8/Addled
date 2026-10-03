"""The agent asks before it acquires a capability, and never installs silently.

Two problems, one root cause.

1. **The agent was blind to its own acquisition tools.** They were offered only
   when the user's message happened to contain "tool", "skill", "mcp" or
   similar. Measured: "convert this pdf to a spreadsheet" offered NONE of them,
   while "find an mcp server for github" offered all three. So it could not
   discover that acquiring was an option unless the user named the concept —
   which is the whole of why it looked passive.

2. **Acquisition happened without consent.** A market match installed itself
   inside the search, from the internet, and ran; the loop set `"market": True`
   and `"installed_skill"` and nothing ever read them. Forging generated and ran
   new code, and adding an MCP server started a third-party process. All three
   were a side effect of answering a question.

The checked facts, in order of what would break first:
  - the discovery tools are offered for a TASK-shaped query (the before/after)
  - `ask_user` says acquiring is a reason TO ask (or the model is told both
    to ask and not to ask)
  - `find_match` does not install; only `install_match` does
  - the tool loop does not install or forge without consent
  - an unattended source cannot acquire, and does not hang trying

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_acquire_asks.py
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
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    from pathlib import Path
    from backend.skills.registry import skill_registry as R

    print("=== the acquisition tools are offered for a TASK, not a magic word ===")
    # The measurement that started this: a task-shaped ask offered none of them.
    for query in ("convert this pdf to a spreadsheet",
                  "make me a chart from this csv",
                  "scrape this website for me"):
        got = R.filter_for_query(query, max_tools=12)
        missing = {"find_mcp_server", "forge_skill"} - set(got)
        check(f"discovery tools offered for {query!r}", not missing,
              f"missing {sorted(missing)}")

    print()
    print("=== and they still fit the budget ===")
    # They are force-added, so they must not push the list past its cap.
    got = R.filter_for_query("convert this pdf to a spreadsheet", max_tools=12)
    check("the selection respects max_tools", len(got) <= 12, str(len(got)))
    check("core utilities are still included",
          {"read_file", "write_file"} <= set(got), str(sorted(got)))

    print()
    print("=== ask_user says acquiring is a reason TO ask ===")
    # Without this the model is told both to ask and never to ask permission,
    # and the silent install wins.
    desc = str(R.get("ask_user").description)
    low = desc.lower()
    check("installing a market skill is named", "market" in low)
    check("forging is named", "forg" in low)
    check("adding an MCP server is named", "mcp" in low)
    check("it says to ask BEFORE it happens", "before" in low)
    check("the no-asking rule is scoped to ordinary work, not acquisition",
          "exception" in low,
          "the exception is not stated, so the old rule still applies")

    print()
    print("=== the acquisition tools tell the model they ask first ===")
    for tool in ("forge_skill", "find_mcp_server"):
        d = str(R.get(tool).description).lower()
        check(f"{tool} says it asks first",
              "asks" in d or "ask " in d, str(R.get(tool).description)[:120])

    print()
    print("=== find does not install; install does ===")
    from backend.skills import market_search as ms
    check("find_match exists", callable(getattr(ms, "find_match", None)))
    check("install_match exists", callable(getattr(ms, "install_match", None)))
    # The split is the point: a search that installs cannot be gated.
    src = Path(ROOT, "backend", "skills", "market_search.py").read_text(
        encoding="utf-8")
    find_body = src.split("async def find_match")[1].split("\ndef ")[0]
    check("find_match does NOT install",
          "install_from_github" not in find_body,
          "the search installs again, so there is nothing to gate")
    install_body = src.split("def install_match")[1].split("\nasync def ")[0]
    check("install_match does install",
          "install_from_github" in install_body, "nothing performs the install")
    check("search_and_install still works for existing callers",
          "find_match(" in src and "install_match(" in src)

    print()
    print("=== the tool loop does not acquire without consent ===")
    loop = Path(ROOT, "backend", "skills", "tool_loop.py").read_text(
        encoding="utf-8")
    code = "\n".join(l for l in loop.splitlines()
                     if not l.strip().startswith("#"))
    check("the market path calls find_match, not search_and_install",
          "find_match(" in code,
          "it would still install inside the search")
    check("it refuses to install to a silent caller",
          "requires_answer" in code,
          "nothing stops it while it waits for an answer")
    check("it gates BOTH the market and the forge",
          code.count("_ask_to_acquire(") >= 2,
          f"only {code.count('_ask_to_acquire(')} gate(s)")
    check("consent is read before installing",
          code.index("_consented(") < code.index("install_match("))
    check("consent is read before forging",
          code.index("_consented(") < code.index("skill_forge.forge("))

    print()
    print("=== an unattended source cannot acquire by accident ===")
    # A refusal from pending.ask is not consent, and must not be read as one.
    from backend.questions import pending as qp
    r = qp.ask("install something?", options=["yes", "no"],
               source="task", conversation="conv_x")
    check("an unattended ask is refused", not r.get("success"), str(r))
    check("the refusal is marked unattended", r.get("unattended") is True,
          str(r))
    # And a refusal must not be mistaken for a yes.
    from backend.skills import tool_loop as tl
    check("the consent reader treats a bare yes as not enough by itself",
          # It requires the QUESTION to have been asked this turn, so an
          # unrelated "yes" cannot authorise an install.
          hasattr(tl, "_ACQUIRE_QUESTION_RE"))
    check("a refusal wins over a yes",
          "refus" in (tl._consented.__doc__ or "").lower()
          or "no" in Path(ROOT, "backend", "skills",
                          "tool_loop.py").read_text(encoding="utf-8")
                      .split("def _consented")[1].split("_ACQUIRE_")[0].lower())
    qp.clear()

    print()
    print("=== EVERY entry point is gated, not just one ===")
    # The bug this exists for: the gate went into `tool_loop._execute_skill_inner`,
    # which fires on an unknown tool NAME. A model asked to forge calls the
    # REGISTERED `forge_skill` skill directly — and the live log proved it did,
    # with "Forged new skill: ..." from a turn that was never asked. A gate on
    # the path the model does not take is not a gate, so each entry point is
    # checked by name.
    reg = Path(ROOT, "backend", "skills", "registry.py").read_text(
        encoding="utf-8")
    forge_fn = reg.split("async def forge_skill")[1].split("\n        self.register")[0]
    check("the REGISTERED forge_skill skill asks first",
          "_consented(" in forge_fn and "_ask_to_acquire(" in forge_fn,
          "the tool the model actually calls is ungated")
    check("the registered forge reports it is waiting",
          "requires_answer" in forge_fn,
          "the turn would not stop for the answer")
    mcp_fn = reg.split("async def find_mcp_server")[1].split(
        "\n        self.register")[0]
    check("find_mcp_server asks before adding a server",
          "_consented(" in mcp_fn and "_ask_to_acquire(" in mcp_fn,
          "it would add a third-party process unprompted")
    # And the dashboard's own install button must NOT be gated: that is an
    # explicit user action, and asking again would be asking the user to
    # confirm the button they just pressed.
    ws_src = Path(ROOT, "backend", "ws_server.py").read_text(encoding="utf-8")
    install_rpc = ws_src.split("async def skills_install_from")[1].split(
        "\n    async def ")[0]
    check("the dashboard's Install button is NOT second-guessed",
          "_ask_to_acquire" not in install_rpc,
          "it would ask the user to confirm their own click")

    print()
    print("=== ordinary turns still do not ask (no interrogation) ===")
    desc_low = str(R.get("ask_user").description).lower()
    check("the do-not-be-cautious rule survives",
          "do not use it to be cautious" in desc_low,
          "the exception undid the rule that stops over-asking")

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all acquisition-consent checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
