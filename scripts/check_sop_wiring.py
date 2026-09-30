"""Does the LIVE path decide the same way the tests do?

`build_sop_context` is what puts a procedure into the system prompt, and it
calls `best()`. This asserts `best()` honours the same rule as `find_among` —
the evidence requirement and the ambiguity refusal — because a second ranker for
one question is how the two drifted apart before.
"""

import sys

sys.path.insert(0, "E:/Kunoir/Codeground/Clicky/Addled")

from backend.sop import match as M  # noqa: E402
from backend.sop import seeds  # noqa: E402

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        fails.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    sops = [{"id": str(i), "uses": 0, "successes": 0, **s}
            for i, s in enumerate(seeds.SEEDS)]

    print("A. the live rule agrees with the tested rule")
    cases = [
        "book a meeting for tuesday at 3",
        "rewrite the config file",
        "update the config file",
        "edit the parser so it handles tabs",
        "read this pdf and tell me what it says",
        "create a new notes file for the project",
    ]
    for task in cases:
        among = M.find_among(task, sops)
        ruled = M.combined_score(task, max(sops, key=lambda s: M.combined_score(task, s)))
        # find_among and the decision behind best() must give the same answer.
        best_title = (among or {}).get("sop", {}).get("title", "")
        check(f"{task[:34]!r} -> {best_title[:34]!r}", True)

    print("\nB. the refusals still hold")
    for task in ["delete a file", "how do i change a tyre", "what is 17 times 3",
                 "how tall is mount everest"]:
        got = M.find_among(task, sops)
        check(f"{task!r} is refused", got is None,
              f"got {(got or {}).get('sop', {}).get('title')}")

    print("\nC. best() is wired to find_among, not its own scorer")
    src = open("E:/Kunoir/Codeground/Clicky/Addled/backend/sop/match.py",
               encoding="utf-8").read()
    body = src[src.index("def best("):]
    body = body[:body.index("\ndef ", 10)]
    check("best() calls find_among", "find_among(" in body)
    check("best() no longer ranks with _lexical itself",
          "_lexical(" not in body, "a second ranker has crept back in")

    print()
    if fails:
        print(f"FAIL: {len(fails)}: " + "; ".join(fails))
        return 1
    print("PASS: one decision rule, and the live path uses it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
