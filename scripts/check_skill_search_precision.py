"""Precision and recall assertions for skill_search with BM25 SkillIndex.

Verifies:
1. Stopwords do not flood results: 'what is the capital of France' returns 0 matches.
2. Recall on core tasks reaches >= 15 / 16 in top-5.
3. Index build time is sub-25ms.
4. Mutation invalidation updates search results immediately.
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.skills.registry import SkillDefinition, skill_registry


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  OK: {label}")
    else:
        print(f"  FAIL: {label} ({detail})")
        raise AssertionError(f"Check failed: {label} ({detail})")


def main() -> int:
    print("=" * 70)
    print("SKILL SEARCH BM25 PRECISION & RECALL SUITE")
    print("=" * 70)

    # 1. Stopword / false-positive suppression
    search_tool = skill_registry.get("skill_search")
    assert search_tool is not None, "skill_search tool not registered"

    res_cap = asyncio.run(search_tool.handler({"query": "what is the capital of France"}))
    check(
        "query with only stopwords and unrelated nouns yields zero matches",
        res_cap.get("count") == 0 and len(res_cap.get("skills", [])) == 0,
        f"got {res_cap.get('count')} skills"
    )

    # 2. Gold queries recall
    GOLD = [
        ("debug why my tests fail", "systematic-debugging"),
        ("find out why my tests are red", "systematic-debugging"),
        ("review a pull request", "requesting-code-review"),
        ("plan a multi step feature", "writing-plans"),
        ("run several agents at once", "dispatching-parallel-agents"),
        ("run these in parallel", "dispatching-parallel-agents"),
        ("clean up a branch after finishing", "finishing-a-development-branch"),
        ("figure out why the last session went wrong", "diagnosing-superpowers"),
        ("it took too long and ignored my plan", "diagnosing-superpowers"),
        ("write documentation for a new skill", "writing-skills"),
        ("find out what the user actually wants", "brainstorming"),
        ("is this review suggestion actually right", "receiving-code-review"),
        ("keep my main checkout clean while working", "using-git-worktrees"),
        ("verify before claiming it is done", "verification-before-completion"),
        ("execute the plan without subagents", "executing-plans"),
        ("start with red tests then make them pass", "test-driven-development")
    ]

    r1, r5 = 0, 0
    for q, target in GOLD:
        res = asyncio.run(search_tool.handler({"query": q}))
        names = [s["name"] for s in res.get("skills", [])]
        if names and names[0] == target:
            r1 += 1
        if target in names[:5]:
            r5 += 1

    check("recall@5 reaches at least 15 of 16 targets", r5 >= 15, f"r5={r5}/16")
    check("recall@1 reaches at least 10 of 16 targets", r1 >= 10, f"r1={r1}/16")

    # 3. Dynamic invalidation on register / unregister
    probe_name = "test-probe-precision-skill"
    async def _dummy(params): return {"success": True}

    skill_registry.register(SkillDefinition(
        name=probe_name,
        description="Quantum telemetry flippatron for orbital mechanics calculation",
        parameters={"type": "object", "properties": {}},
        handler=_dummy,
        category="meta",
        body="Detailed instructions on how to use orbital mechanics flippatron."
    ))

    try:
        found_probe = asyncio.run(search_tool.handler({"query": "orbital mechanics telemetry"}))
        found_names = [s["name"] for s in found_probe.get("skills", [])]
        check("newly registered skill is immediately searchable", probe_name in found_names)
    finally:
        skill_registry.unregister(probe_name)

    after_unreg = asyncio.run(search_tool.handler({"query": "orbital mechanics telemetry"}))
    after_names = [s["name"] for s in after_unreg.get("skills", [])]
    check("unregistered skill is immediately removed from search", probe_name not in after_names)

    # 4. Latency performance
    skill_registry.invalidate_index()
    t0 = time.perf_counter()
    _ = skill_registry.get_index()
    t_build = (time.perf_counter() - t0) * 1000
    check("index build time is under 25ms", t_build < 25.0, f"{t_build:.2f}ms")

    print("\nAll skill search precision checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
