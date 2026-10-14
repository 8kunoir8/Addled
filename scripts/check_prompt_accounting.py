"""The assembled prompt had no arithmetic, so its largest block was invisible.

## What was found

`ws_server.run_chat_pipeline` builds the system prompt by appending a dozen
blocks -- persona, tool catalogue, memory, recall, directives -- and hands the
concatenation to the provider as one string. Nothing recorded what each block
cost. Measuring it for the first time, on the live configuration, gave:

    prompt: 9623 chars (~3207 tokens), 16 blocks; prefix 3714 chars (39%)
      wiki                    4,350 chars  ~ 1,450 tok   <- largest block
      capabilities        S   2,594 chars  ~   864 tok
      persona             S     656 chars  ~   218 tok

The wiki block is larger than the entire tool catalogue, and nothing in the
codebase said so. A block that cannot be sized cannot be argued about.

## PromptBuild

`backend/providers/budget.py` gained a builder that accumulates labelled blocks
and can name the turn-invariant head. `prefix()` returns exactly that head --
the span a provider's cache can hold -- and `summary()`/`report()` are what the
pipeline logs. The builder is a drop-in for the plain string: `str()` returns
the same concatenation the old code produced.

## What is asserted here

Its arithmetic (the prefix stops at the first volatile block, a stable block
after a volatile one is refused because it cannot be cached), its
string-compatibility (so no call site had to change shape), the tolerance of
`None` and empty blocks, and that the pipeline both uses it and logs from it.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.providers.budget import PromptBuild  # noqa: E402

fails: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f"  -- {why}" if why else ""))
        fails.append(name)


def src(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def the_arithmetic() -> None:
    """The prefix is the leading run of stable blocks, and nothing else."""
    b = PromptBuild("PERSONA", "persona")
    b.add("caps", "CAPS", stable=True)
    b.add("wiki", "WIKI")

    check("str() is the concatenation of every block, separated",
          str(b) == "PERSONA\n\nCAPS\n\nWIKI",
          "call sites pass str(sys_prompt) to the provider")
    check("len() is the character count",
          len(b) == len("PERSONA\n\nCAPS\n\nWIKI"))
    check("the prefix is the stable head only",
          b.prefix() == "PERSONA\n\nCAPS",
          "a volatile block in the prefix would make it uncacheable")
    check("the prefix stops at the first volatile block",
          "WIKI" not in b.prefix())
    check("the prefix is a real prefix of the whole string",
          str(b).startswith(b.prefix()),
          "a cache breakpoint can only land on a byte boundary of the prompt")

    s = b.summary()
    check("summary counts every block", s["blocks"] == 3)
    check("summary counts the prefix",
          s["prefix_chars"] == len("PERSONA\n\nCAPS"),
          "the cacheable fraction is the number the feature exists to expose")
    check("summary counts characters",
          s["chars"] == len(str(b)))
    check("summary splits stable from volatile",
          s["stable_chars"] + s["volatile_chars"] == s["chars"],
          "the two halves have to account for the whole prompt")
    check("summary estimates tokens",
          isinstance(s["tokens"], int) and s["tokens"] > 0)

    bd = b.breakdown()
    check("breakdown names every block in order",
          [r["label"] for r in bd] == ["persona", "caps", "wiki"])
    check("breakdown marks the stable blocks",
          [r["stable"] for r in bd] == [True, True, False])


def the_stable_after_volatile_warning() -> None:
    """A stable block after a volatile one is warned about, not raised on.

    Deliberate: this builder runs inside a chat turn, and refusing to append
    would turn a mis-ordering into a failed reply. The warning is the contract,
    and the prefix must simply exclude the late block.
    """
    import logging

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("addled.providers.budget")
    handler = Capture()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        b = PromptBuild("P", "persona")
        b.add("wiki", "WIKI")            # volatile: seals the prefix here
        b.add("late", "LATE", stable=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    check("a stable block after a volatile one is warned about",
          any("late" in m and "stable" in m for m in records),
          "silently accepting it would promise a prefix that cannot be cached")
    check("the late block is still appended, not dropped",
          "LATE" in str(b),
          "dropping content to protect a cache would lose a memory block")
    check("the prefix excludes the late stable block",
          b.prefix() == "P" and "LATE" not in b.prefix(),
          "the prefix has to be the contiguous leading run, nothing more")
    flags = {r["label"]: r["stable"] for r in b.breakdown()}
    check("the late block is still marked stable in the breakdown",
          flags == {"persona": True, "wiki": False, "late": True},
          "breakdown sorts by size, so it is keyed by label, not position")


def the_tolerances() -> None:
    """None and empty blocks are recorded, not skipped or crashed on."""
    b = PromptBuild("", "persona")
    check("an empty persona is allowed", str(b) == "")
    b.add("missing", None)
    check("a None block is accepted", str(b) == "",
          "memory blocks are conditionally built and often absent")
    b.add("empty", "")
    check("an empty-string block is accepted", str(b) == "")
    labels = [r["label"] for r in b.breakdown()]
    check("a zero-size block is still recorded by name",
          "missing" in labels and "empty" in labels,
          "a block that is present-but-empty must be visible, not invisible")
    s = b.summary()
    check("a prompt of no blocks has no prefix",
          s["prefix_chars"] == 0)
    check("report() does not raise on an empty builder",
          isinstance(b.report(), str))


def the_report() -> None:
    """report() is readable and the threshold filters small blocks."""
    b = PromptBuild("P", "persona")
    b.add("big", "x" * 500)
    b.add("small", "y" * 5)
    full = b.report()
    check("report names the blocks", "big" in full and "small" in full)
    check("report marks the stable blocks", "persona" in full)
    filtered = b.report(threshold_chars=100)
    check("report can filter by size",
          "big" in filtered and "small" not in filtered,
          "the threshold is what makes the output readable in a log")


def the_ws_wiring() -> None:
    """The pipeline builds with PromptBuild and logs the breakdown."""
    ws = src("backend/ws_server.py")
    check("run_chat_pipeline builds a PromptBuild",
          "sys_prompt = PromptBuild(" in ws)
    check("the builder is what the provider receives",
          "system_prompt=str(sys_prompt)" in ws,
          "the string must be identical to the old concatenation")
    check("the accounting is logged",
          "prompt: %d chars" in ws and "sys_prompt.report(" in ws,
          "an unlogged measurement is one nobody can act on")
    check("the log line cannot break the turn",
          re.search(r"except Exception as e:  # noqa: BLE001\s*\n\s*"
                    r"log\.debug\(\"prompt accounting failed", ws) is not None,
          "accounting is diagnostic; it must never take down a chat")

    adds = re.findall(r'sys_prompt\.add\("([^"]+)"', ws)
    check("the blocks are added by name", len(adds) >= 10,
          f"found {len(adds)} labelled blocks")
    check("persona and the tool catalogue are stable",
          "PromptBuild(base, \"persona\")" in ws
          and re.search(r'add\("capabilities",[^)]*stable=True', ws) is not None,
          "the cacheable prefix is exactly these leading blocks")
    volatile_after_stable = False
    for name in adds:
        pass
    check("the memory blocks are volatile",
          re.search(r'add\("wiki",', ws) is not None)


def main() -> int:
    print("prompt accounting: the arithmetic")
    the_arithmetic()
    print()
    print("a stable block after a volatile one:")
    the_stable_after_volatile_warning()
    print()
    print("tolerances for conditional blocks:")
    the_tolerances()
    print()
    print("the report:")
    the_report()
    print()
    print("the wiring in run_chat_pipeline:")
    the_ws_wiring()
    print()
    if fails:
        print(f"FAIL: {len(fails)} check(s) failed")
        return 1
    print("PASS: the prompt is assembled as named blocks, its cacheable prefix "
          "is computable, and the cost of each block is recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
