"""Does the procedure matcher pick the right recipe — and refuse when unsure?

`find()` decides which stored procedure is offered before a task starts. A wrong
offer is worse than no offer: the model follows a confident recipe for something
else. So this scores the matcher on a fixed table that includes the cases the
code's own comments say the embedder got wrong, and it scores the OLD formula
alongside the new one so a change has to be shown to be an improvement rather
than asserted to be one.

Run from the project root, with ADDLED_ROOT CLEARED:

    .\\python-bundle\\python.exe -s .\\scripts\\check_sop_scoring.py
"""

import os
import sys

sys.path.insert(0, "E:/Kunoir/Codeground/Clicky/Addled")

from backend.sop import match as M  # noqa: E402
from backend.sop import seeds  # noqa: E402

# (task, expected procedure title substring, or None for "no procedure")
# The None rows are the important half. A matcher that always returns its best
# guess passes every positive row and is still useless.
TABLE: list[tuple[str, str | None]] = [
    # -- positives: the task is what the recipe is for -----------------------
    ("read a file before you rewrite it", "Search, then write"),
    ("create a new notes file for the project", "Search, then write"),
    ("write the config file again with the new port", "Search, then write"),
    ("edit the parser so it handles tabs", "Change code safely"),
    ("refactor this function and run the tests", "Change code safely"),
    ("look up the population of Peru", "Search, then read"),
    ("find the answer to this question online", "Search, then read"),
    ("go to the page and see what it says", "Read a page before acting"),
    ("what do you know about the user's setup", "Check what is already known"),
    ("book a meeting for tuesday at 3", "Confirm before writing to a calendar"),
    ("read this pdf and tell me what it says", "Handle a document"),
    ("convert this word file to a pdf", "Handle a document"),
    # Not "run the tests in this project" — that is genuinely part of "Change
    # code safely", which says so in its own steps, so expecting the command
    # procedure there was a badly-written row rather than a matcher mistake.
    ("execute this shell command for me", "Look before running a command"),
    # -- synonyms: the word in the request is not the word in the recipe ------
    # These all scored ZERO before the verb map, because no procedure contained
    # the word used. A ranking change cannot fix a word that exists in neither
    # side; only normalising the vocabulary can.
    ("rewrite the config file", "Search, then write"),
    ("update the config file", "Search, then write"),
    ("modify the config file", "Search, then write"),
    ("overwrite the config file", "Search, then write"),
    # -- negatives: nothing stored is for this --------------------------------
    # Straight from the code's own notes about the embedder.
    ("how tall is mount everest", None),
    ("what is the capital of peru", None),
    ("sing me a song", None),
    ("what is 17 times 3", None),
    ("who won the world cup in 1998", None),
    # Near-miss negatives: these share words with a procedure but are NOT it.
    ("delete a file", None),
    ("how do i change a tyre", None),
    ("plan a birthday party", None),
]

# No known misses. The one that used to be here — "write the config file again"
# matching the wrong procedure — was not a scoring fault and was never going to
# be fixed by one: the two file seeds were the same job split in half, so the
# task honestly fitted both. Merging them removed the ambiguity instead of
# ranking it. Kept as an empty set so the mechanism (and its output) stays.
KNOWN_MISSES: set[str] = set()


def install_seeds() -> list[dict]:
    """The seed procedures as plain dicts, without touching the store."""
    out = []
    for i, s in enumerate(seeds.SEEDS):
        out.append({"id": f"seed_{i}", "source": "seed", "uses": 0,
                    "successes": 0, **s})
    return out


def old_find(task: str, sops: list[dict], limit: int = 1) -> dict | None:
    """The matcher as it was: rank by score, offer the top one.

    Kept here so the new formula is compared against it on the same table. It
    is deliberately the REAL old behaviour (no field-of-candidates gate), which
    is what made unrelated tasks return a confident wrong answer.
    """
    scored = []
    for sop in sops:
        value, method = M.score(task, sop, None)
        scored.append({"sop": sop, "score": value, "method": method})
    scored.sort(key=lambda x: -x["score"])
    top = scored[0] if scored else None
    if top is None:
        return None
    bar = M.threshold(top["method"])
    return top if top["score"] >= bar else None


def evaluate(find_fn) -> tuple[int, list[str]]:
    right, wrong = 0, []
    for task, expected in TABLE:
        got = find_fn(task)
        title = str((got or {}).get("sop", {}).get("title", "")) if got else ""
        if expected is None:
            ok = got is None
        else:
            ok = expected.lower() in title.lower()
        if ok:
            right += 1
        elif task in KNOWN_MISSES:
            # Counted as a known miss, not a silent pass: it still shows in the
            # total so a fix is visible, and it cannot be forgotten.
            wrong.append(f"KNOWN: {task!r} -> got {title!r}")
        else:
            wrong.append(f"{task!r} -> expected {expected!r}, got {title!r}")
    return right, wrong


def main() -> int:
    sops = install_seeds()
    print(f"table: {len(TABLE)} cases against {len(sops)} seed procedures")
    print(f"  ({sum(1 for _, e in TABLE if e is None)} of them expect NO procedure)\n")

    # A case that names a procedure which does not exist can never pass, and
    # reading the run output would not say so — the row just shows as a miss.
    # That is how the table came to reference seed titles that had been renamed.
    titles = {str(s.get("title") or "").lower() for s in sops}
    stale = [(t, e) for t, e in TABLE
             if e is not None and not any(e.lower() in x for x in titles)]
    if stale:
        print("!! these cases name a procedure that does not exist:")
        for task, want in stale:
            print(f"     {want!r}  (for {task!r})")
        print("   the seed set and this table have drifted apart\n")
        return 1

    old_right, old_wrong = evaluate(lambda t: old_find(t, sops))
    new_right, new_wrong = evaluate(lambda t: M.find_among(t, sops, limit=1))

    print(f"  OLD formula: {old_right}/{len(TABLE)}")
    for w in old_wrong:
        print(f"     miss: {w}")
    print(f"\n  NEW formula: {new_right}/{len(TABLE)}")
    for w in new_wrong:
        print(f"     miss: {w}")

    # A known miss is reported and tolerated, but everything else must be right.
    new_wrong = [w for w in new_wrong if not w.startswith("KNOWN:")]
    known = [w for w in evaluate(
        lambda t: M.find_among(t, sops, limit=1))[1] if w.startswith("KNOWN:")]

    print()
    if new_right < old_right:
        print("FAIL: the new formula is WORSE than the one it replaces")
        return 1
    if new_wrong:
        print(f"FAIL: {len(new_wrong)} case(s) wrong")
        return 1
    print(f"PASS: new formula {new_right}/{len(TABLE)}, "
          f"never worse than the old ({old_right}/{len(TABLE)})")
    if known:
        print(f"      {len(known)} known ambiguity recorded, not hidden:")
        for k in known:
            print(f"        {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
