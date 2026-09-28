"""Install the researched skill set through Addled's own market.

Uses `market.install_from_github` — the same path `skills.installFrom` uses —
so the installed skills land where the registry expects them, get registered,
and are subject to the approval/digest system.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\install_skills.py [tier|role|all]
"""

from __future__ import annotations

import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.skills.market import market  # noqa: E402

# Tier 1 — method skills. Addled has the tools; these supply the discipline.
TIER1 = [
    ("obra/superpowers", "skills/systematic-debugging"),
    ("obra/superpowers", "skills/writing-plans"),
    ("obra/superpowers", "skills/executing-plans"),
    ("obra/superpowers", "skills/verification-before-completion"),
    ("obra/superpowers", "skills/test-driven-development"),
    ("obra/superpowers", "skills/dispatching-parallel-agents"),
    ("obra/superpowers", "skills/subagent-driven-development"),
    ("obra/superpowers", "skills/brainstorming"),
    ("obra/superpowers", "skills/diagnosing-superpowers"),
]

# Tier 2 — capability gaps. Paths are guesses to be resolved by search where a
# repo layout is unknown; each is verified by whether the install returns a name.
TIER2 = [
    ("obra/superpowers", "skills/requesting-code-review"),
    ("obra/superpowers", "skills/receiving-code-review"),
    ("obra/superpowers", "skills/using-git-worktrees"),
    ("obra/superpowers", "skills/finishing-a-development-branch"),
    ("obra/superpowers", "skills/writing-skills"),
]


def install(pairs: list[tuple[str, str]]) -> tuple[int, int]:
    ok = failed = 0
    for repo, path in pairs:
        try:
            meta = market.install_from_github(repo, path)
            name = meta.get("name") if isinstance(meta, dict) else "?"
            scripts = len(meta.get("scripts") or []) if isinstance(meta, dict) else 0
            print(f"  ok    {repo} {path} -> {name} ({scripts} file(s))")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {repo} {path} -> {type(e).__name__}: {e}")
            failed += 1
    return ok, failed


def main() -> int:
    which = (sys.argv[1] if len(sys.argv) > 1 else "tier1").lower()
    pairs: list[tuple[str, str]] = []
    if which in ("tier1", "all"):
        pairs += TIER1
    if which in ("tier2", "all"):
        pairs += TIER2

    print(f"Installing {len(pairs)} skill(s) [{which}]")
    ok, failed = install(pairs)
    print(f"\n{ok} installed, {failed} failed")

    # Show what the registry now holds, so the result is not just a claim.
    try:
        from backend.skills.market import MARKET_DIR
        dirs = sorted(d.name for d in MARKET_DIR.glob("*") if d.is_dir())
        print(f"market_skills now holds {len(dirs)}: {dirs}")
    except Exception as e:  # noqa: BLE001
        print("could not list the market folder:", e)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
