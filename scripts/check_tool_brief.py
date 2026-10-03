"""Every tool name the capability block names must be a real skill.

The block this module replaces was hand-written and had drifted: it told the
model to call `browser_open`, `remember` and `recall`, none of which exist
(`browser_navigate`, `memory_set`, `memory_get` do). A model instructed to call
a tool that is not there is set up to fail, and the failure looks like the model
being bad rather than the prompt being wrong.

So the point of this suite is the name check. The rest guards the two rules the
block has to keep: it describes only the tools actually offered, and it says
nothing when nothing was offered.
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label
          + ("  <- " + str(detail) if detail and not ok else ""))
    if not ok:
        fails.append(label)


def main() -> int:
    from backend import tool_brief
    from backend.skills.registry import skill_registry

    real = {s.name for s in skill_registry.list_all()}
    check("the registry has skills to check against", len(real) > 20, len(real))

    # ---- RULE 1: every name in the table is real ---------------------------
    # This is the regression that matters. Adding a fact for a tool that does
    # not exist, or renaming a skill without updating the table, fails here.
    named = [name for name, _ in tool_brief._FACTS]
    missing = [n for n in named if n not in real]
    check("every tool named in the facts table is a real skill", not missing,
          f"not real: {missing}")

    # Each fact must actually name its tool in backticks. A fact that describes
    # a capability without saying which tool provides it is unusable: the model
    # reads prose it cannot act on. This is the check that caught the shell
    # entry describing PowerShell without ever writing `run_command`.
    unnamed = [n for n, fact in tool_brief._FACTS if f"`{n}`" not in fact]
    check("every fact names its own tool", not unnamed,
          f"describes but does not name: {unnamed}")

    # ---- RULE 2: the block is generated from what was offered --------------
    everything = tool_brief.capabilities_block(None)
    check("an unfiltered block lists the shell capability",
          "run_command" in everything)
    check("an unfiltered block mentions the browser",
          "browser_navigate" in everything)

    # A narrowed set must NOT be promised tools it was not given. This is the
    # rule that stops the fix repeating the original bug in the other direction.
    narrow = tool_brief.capabilities_block(["read_file"])
    check("a narrowed block does not promise run_command",
          "run_command" not in narrow, narrow[:200])
    check("a narrowed block does not promise the browser",
          "browser_navigate" not in narrow)
    check("a narrowed block still describes the tool it has",
          "read_file" in narrow, narrow[:200])

    # A set with no table entry still tells the model what it holds, rather
    # than leaving it to infer from a schema.
    odd = tool_brief.capabilities_block(["verify_code"])
    check("an unlisted tool is still named", "verify_code" in odd, odd[:200])

    # ---- RULE 3: nothing offered means nothing claimed ---------------------
    check("an empty tool set produces no block",
          tool_brief.capabilities_block([]) == "",
          repr(tool_brief.capabilities_block([])))

    # ---- No prompt may claim a gated tool it was not given -----------------
    # The permission paragraph mentions approvals. Saying it to a turn holding
    # only `read_file` invents a restriction the model then tells the user about.
    readonly = tool_brief.capabilities_block(["read_file"])
    check("a read-only turn is not told about approvals",
          "requires_approval" not in readonly, readonly[:300])
    check("a shell turn IS told about approvals",
          "requires_approval" in everything)

    # ---- ASCII only: this gets printed to a cp1252 console -----------------
    for label, block in (("unfiltered", everything), ("narrowed", narrow)):
        try:
            block.encode("ascii")
            ok, detail = True, ""
        except UnicodeEncodeError as e:
            ok, detail = False, str(e)
        check(f"the {label} block is ASCII", ok, detail)

    # ---- The refusal detector matches what agents actually said ------------
    # Both phrases were observed from a live QA agent refusing real work.
    for said in ("QA's tools aren't accessible in the current environment",
                 "I cannot reply with the token as it is not a valid action "
                 "with any of the available tools"):
        check(f"detects a real refusal: {said[:40]!r}",
              tool_brief.claims_no_tools(said))

    # And must not fire on an ordinary answer, or it would append a correction
    # to a reply that was already fine.
    for fine in ("Here is the review you asked for: the function is correct.",
                 "I checked the file and found two problems.",
                 "I have used the tool and the result is 42."):
        check(f"does not fire on a normal reply: {fine[:30]!r}",
              not tool_brief.claims_no_tools(fine))

    # ---- The correction names real tools -----------------------------------
    corr = tool_brief.correction(["read_file", "verify_code"])
    check("the correction names the tools held",
          "read_file" in corr and "verify_code" in corr, corr[:200])
    check("the correction is empty when nothing is held",
          tool_brief.correction([]) == "")

    print()
    print("FAILED: " + ", ".join(fails) if fails
          else "all tool-brief checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
