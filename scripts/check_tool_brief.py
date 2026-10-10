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

    # ---- RULE 1b: every CAPABILITY the app has is described ----------------
    # The direction the original check missed. It proved nothing was named that
    # does not exist, but nothing proved that what exists is named -- so five
    # `meeting_*` skills shipped, the prose never mentioned meetings, and the
    # model holding them answered "there is no list meetings function wired
    # up". A capability that reaches the schema but not the prose is a
    # capability the model will deny having.
    #
    # These are the families the block is expected to account for. A new family
    # added to the registry without a fact here is the bug this catches.
    expected_families = {
        "meeting": "meeting_save",
        "calendar": "calendar_add",
        "browser": "browser_navigate",
        "memory": "memory_set",
        "desktop": "desktop_click",
        "schedule": "task_schedule",
        "files": "read_file",
        "shell": "run_command",
    }
    facts = {name for name, _ in tool_brief._FACTS}
    undescribed = {
        family: member for family, member in expected_families.items()
        if member not in facts
    }
    check("every capability family has a fact in the table", not undescribed,
          f"registered but never described to the model: {undescribed}")

    # An unfiltered block must actually SAY it can do these things. Naming the
    # tool is not enough on its own: check_tool_brief's own bug was a block
    # that listed `run_command` while reading as though the model had no shell.
    everything0 = tool_brief.capabilities_block(None)
    for family, word in [("meetings", "meeting"), ("calendar", "calendar"),
                         ("browser", "browser"), ("files", "file"),
                         ("memory", "memory")]:
        check(f"an unfiltered block mentions {family}",
              word in everything0.lower(),
              f"the word '{word}' never appears")

    # The CLOSING SENTENCE specifically, not the block as a whole. Asserting
    # "the block mentions meetings" is too weak: the fact prose says
    # "meeting_save" too, so a hand-written sentence that omitted meetings
    # still passed. That was found by revert-verifying this very check -- the
    # restored old sentence slipped through. The sentence is derived from
    # _DOMAINS, so it must name every domain for the tools on offer.
    def closing_sentence(block: str) -> str:
        for line in block.splitlines():
            if line.startswith("When asked"):
                return line
        return ""

    sentence = closing_sentence(everything0)
    check("the block has a closing capability sentence", bool(sentence),
          "no 'When asked…' line at all")
    for name, noun in tool_brief._DOMAINS:
        check(f"the closing sentence names '{noun}' ({name})",
              noun in sentence,
              f"derived from _DOMAINS but missing: {sentence[:200]}")
    check("the closing sentence mentions meetings",
          "meeting" in sentence.lower(),
          "meetings missing from the sentence: " + sentence[:200])
    check("the closing sentence mentions the PC",
          "pc" in sentence.lower(),
          "the shell capability is missing: " + sentence[:200])

    # The closing sentence is derived from _DOMAINS, so the two tables have to
    # agree: a fact with no domain is a capability the sentence silently drops,
    # which is exactly how meetings went missing from both.
    domains = {name for name, _ in tool_brief._DOMAINS}
    no_domain = sorted(facts - domains)
    check("every fact has a domain entry for the closing sentence",
          not no_domain, f"facts with no domain: {no_domain}")
    orphan_domain = sorted(domains - facts)
    check("every domain entry describes a fact that exists",
          not orphan_domain, f"domains with no fact: {orphan_domain}")

    # A meeting turn must be told about meetings, in prose, not merely handed
    # the schema.
    meeting_turn = tool_brief.capabilities_block(["meeting_list"])
    check("a meeting-only turn is told it can list meetings",
          "meeting" in meeting_turn.lower(),
          meeting_turn[:220])
    check("and is told the tool exists rather than left to guess",
          "meeting_list" in meeting_turn or "meeting" in meeting_turn.lower(),
          meeting_turn[:220])

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
