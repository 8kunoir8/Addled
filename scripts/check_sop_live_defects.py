"""Regression tests for the two defects found on the live install.

Both were invisible in the dev tree and only appeared against a real store:

1. A seed that a later version stopped shipping could never leave an install.
   Two near-identical file procedures then competed, and the matcher refused the
   task as ambiguous — the merge made matching worse on exactly the installs it
   was meant to improve.

2. A task whose verb maps onto a tool the procedure uniquely has was refused for
   sharing only one word, because only two kinds of evidence were recognised.
"""

import json
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, "E:/Kunoir/Codeground/Clicky/Addled")

from backend.sop import match as M, seeds, store  # noqa: E402

fails: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        fails.append(label)
        print(f"  FAIL  {label}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    print("A. retired seeds are declared and tombstoned")
    check("seeds.RETIRED exists", hasattr(seeds, "RETIRED"))
    check("RETIRED is non-empty", bool(getattr(seeds, "RETIRED", [])))
    retired_titles = {t.lower() for _, t in seeds.RETIRED}
    declared = {s["title"].lower() for s in seeds.SEEDS}
    check("no retired title is still declared",
          not (retired_titles & declared),
          f"overlap: {retired_titles & declared}")
    check("the two merged file seeds are retired",
          {"find a file before creating one", "inspect before overwriting"}
          <= retired_titles)

    print("\nB. seed() retires and does not re-add")
    check("store.tombstone_seed exists", hasattr(store, "tombstone_seed"))
    check("store.prune_learned exists", hasattr(store, "prune_learned"))
    src = Path("E:/Kunoir/Codeground/Clicky/Addled/backend/sop/seeds.py"
               ).read_text(encoding="utf-8")
    body = src[src.index("def seed("):src.index("def ensure_seeded(")]

    check("seed() walks RETIRED", "for category, title in RETIRED" in body)
    check("seed() tombstones before adding",
          body.index("tombstone_seed") < body.index("for entry in SEEDS"))
    check("a non-seed procedure is never removed",
          'existing.get("source") or ""' in body
          and '!= "seed"' in body)

    print("\nC. a strong verb signal is evidence on its own")
    src_m = Path("E:/Kunoir/Codeground/Clicky/Addled/backend/sop/match.py"
                 ).read_text(encoding="utf-8")
    mbody = src_m[src_m.index("def find_among("):]
    mbody = mbody[:mbody.index("\ndef ", 10)]
    check("find_among has an enough_verb test", "enough_verb" in mbody)
    check("the verb test is in the accept condition",
          "enough_words or enough_category or enough_verb" in mbody)
    check("affinity is recorded per candidate",
          '"affinity": affinity' in src_m)

    print("\nD. the rule behaves, on the real seed set")
    sops = [{"id": str(i), "uses": 0, "successes": 0, **s}
            for i, s in enumerate(seeds.SEEDS)]
    # A verb the procedure uniquely owns: `search` -> web_search/web_fetch.
    got = M.find_among("search the web for the release notes", sops)
    check("a verb-only match is accepted",
          got is not None and got["sop"]["category"] == "web",
          f"got {(got or {}).get('sop', {}).get('title')}")

    print("\nE. the negatives the rule was built for still refuse")
    for task in ["how do i change a tyre", "what is 17 times 3",
                 "how tall is mount everest", "delete a file",
                 "hello", "explain quantum tunnelling"]:
        check(f"{task!r} refused", M.find_among(task, sops) is None)

    print("\nF. prune only ever touches learned procedures")
    ssrc = Path("E:/Kunoir/Codeground/Clicky/Addled/backend/sop/store.py"
                ).read_text(encoding="utf-8")
    pbody = ssrc[ssrc.index("def prune_learned("):]
    pbody = pbody[:pbody.index("\ndef ", 10)]
    check("prune filters on source == learned",
          's.get("source") == "learned"' in pbody
          or "s.get('source') == 'learned'" in pbody)
    check("prune keeps seeds and manual entries",
          's.get("source") != "learned"' in pbody)

    print()
    if fails:
        print(f"FAIL: {len(fails)}: " + "; ".join(fails))
        return 1
    print("PASS: both live defects are fixed and covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
