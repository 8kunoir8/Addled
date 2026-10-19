"""Verification suite for native passage chunking and retrieval.

Asserts:
1. chunk_lines produces correct line-anchored windows with stride.
2. PassageIndex accurately ranks relevant code blocks above distant mentions.
3. search_passages skill executes cleanly on workspace directories.
4. read_file and code_read correctly respect start_line and end_line bounds.
"""
from __future__ import annotations

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.passage import chunk_lines, PassageIndex
from backend.skills.registry import skill_registry


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  OK: {label}")
    else:
        print(f"  FAIL: {label} ({detail})")
        raise AssertionError(f"Check failed: {label} ({detail})")


def main() -> int:
    print("=" * 70)
    print("PASSAGE CHUNKING & RETRIEVAL VERIFICATION")
    print("=" * 70)

    # 1. Unit test chunk_lines
    sample_text = "\n".join(f"line {i}" for i in range(1, 101))
    chunks = chunk_lines(sample_text, window=30, stride=15)
    check("chunk_lines returns non-empty list for 100-line text", len(chunks) > 0)
    first_start, first_end, first_body = chunks[0]
    check("first chunk starts at line 1 and ends at 30", first_start == 1 and first_end == 30, f"{first_start}-{first_end}")
    check("first chunk has 30 lines", len(first_body.splitlines()) == 30)

    second_start, second_end, _ = chunks[1]
    check("second chunk starts at line 16 (stride 15)", second_start == 16, f"start={second_start}")

    last_start, last_end, last_body = chunks[-1]
    check("last chunk ends at line 100", last_end == 100, f"end={last_end}")

    # 2. PassageIndex ranking test
    sample_passages = [
        ("fileA.py", 1, 30, "def calculate_orbit(trajectory):\n    pass\n    return trajectory.orbit()"),
        ("fileB.py", 1, 30, "import os\n# unrelated helper code\nprint('hello world')"),
        ("fileC.py", 100, 130, "def draw_gui():\n    # GUI layout buttons\n    pass"),
    ]
    idx = PassageIndex(sample_passages)
    results = idx.search("calculate_orbit trajectory", limit=2)
    check("PassageIndex returns matching passage at rank 1", len(results) > 0 and results[0]["filePath"] == "fileA.py")
    check("Top result has correct line numbers", results[0]["startLine"] == 1 and results[0]["endLine"] == 30)
    check("Exact match gets high score", results[0]["score"] > 5.0, f"score={results[0]['score']}")

    # 3. search_passages skill integration
    sp = skill_registry.get("search_passages")
    check("search_passages skill registered", sp is not None)

    res_sp = asyncio.run(sp.handler({"query": "def invalidate_index", "directory": "backend/skills"}))
    check("search_passages finds invalidate_index in backend/skills", res_sp.get("success") is True and len(res_sp.get("matches", [])) > 0)
    top_match = res_sp["matches"][0]
    check("top match is registry.py", "registry.py" in top_match["filePath"], top_match["filePath"])
    check("top match snippet contains definition", "def invalidate_index" in top_match["snippet"])

    # 4. read_file with line range
    rf = skill_registry.get("read_file")
    res_rf = asyncio.run(rf.handler({
        "path": "backend/passage.py",
        "start_line": 15,
        "end_line": 25,
    }))
    check("read_file succeeds with line range", res_rf.get("success") is True)
    check("read_file returns exactly 11 lines", len(res_rf.get("content", "").splitlines()) == 11, f"len={len(res_rf.get('content', '').splitlines())}")
    check("read_file records start_line and end_line in response", res_rf.get("start_line") == 15 and res_rf.get("end_line") == 25)

    # 5. code_read with line range
    cr = skill_registry.get("code_read")
    res_cr = asyncio.run(cr.handler({
        "path": "backend/passage.py",
        "start_line": 20,
        "end_line": 30,
    }))
    check("code_read succeeds with line range", res_cr.get("success") is True)
    check("code_read returns exactly 11 lines", len(res_cr.get("content", "").splitlines()) == 11)
    check("code_read detects language as python", res_cr.get("language") == "python")

    print("\nAll passage retrieval checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
