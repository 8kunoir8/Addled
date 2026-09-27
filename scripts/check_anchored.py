"""Anchored-edit checks — the engine the Code page edits through.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_anchored.py

Pins the behaviour that makes anchored edits safer than whole-file rewrites:
an exact match is used when there is one, ambiguity is refused rather than
guessed, indentation/whitespace drift still finds the anchor, and a chain of
edits stops at the first failure without throwing away the ones that worked.
"""

import os
import sys

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.codemode.anchored import (EditOp, apply_anchor, apply_edits,
                                       find_anchor, parse_edit_ops)

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

ORIG = "def run():\n    return 1\n\ndef helper():\n    return 2\n"

def run():
    # ---- matching -------------------------------------------------------
    m = find_anchor(ORIG, "return 1")
    check("an exact match is found", m.found and m.mode == "exact", str(m))
    check("and reports one occurrence", m.count == 1, f"count={m.count}")

    m2 = find_anchor(ORIG, "return")
    check("an ambiguous anchor is still reported as found",
          m2.found, "the caller needs count to decide")
    check("with its occurrence count", m2.count == 2, f"count={m2.count}")

    m3 = find_anchor(ORIG, "def run():\nreturn 1")
    check("indentation drift still matches",
          m3.found and "indentation" in m3.mode, str(m3))

    m4 = find_anchor(ORIG, "def   helper():\n\treturn   2")
    check("whitespace drift still matches",
          m4.found and m4.mode == "whitespace-insensitive", str(m4))

    m5 = find_anchor(ORIG, "NOT IN THE FILE")
    check("a missing anchor is not found", not m5.found, str(m5))
    check("and says so readably", "not found" in m5.error.lower(), m5.error)

    # ---- applying -------------------------------------------------------
    out, r = apply_anchor(ORIG, "return 1", "return 42")
    check("an exact replacement is applied", r.found and "return 42" in out,
          out)
    check("and nothing else moved",
          out.count("\n") == ORIG.count("\n"), out)

    out2, r2 = apply_anchor(ORIG, "return", "return", occurrence=1)
    check("replacing text with itself is refused",
          not r2.found, "no-op edits must not be reported as applied")

    # Ambiguity: no occurrence given -> refuse.
    out3, r3 = apply_anchor(ORIG, "return", "RETURN")
    check("an ambiguous anchor without an occurrence is refused",
          not r3.found and r3.count == 2, str(r3))

    # Ambiguity: occurrence given -> apply that one.
    out4, r4 = apply_anchor(ORIG, "return", "RETURN", occurrence=2)
    check("an occurrence selects the right one",
          r4.found and out4.count("RETURN") == 1
          and out4.splitlines()[1].strip() == "return 1", out4)

    out5, r5 = apply_anchor(ORIG, "return ", "RETURN ", replace_all=True)
    check("replace_all replaces every exact hit",
          r5.found and out5.count("RETURN ") == 2, out5)

    out6, r6 = apply_anchor(ORIG, "def run():\nreturn 1",
                            "def run():\n    return 7", occurrence=1)
    check("a loose match with an occurrence applies",
          r6.found and "return 7" in out6, out6)

    # ---- chains ---------------------------------------------------------
    ops = [EditOp("def helper():", "def util():"),
           EditOp("def util():\n    return 2", "def util():\n    return 99")]
    out7, rep7 = apply_edits(ORIG, ops)
    check("a chain applies in order",
          all(r["ok"] for r in rep7), str(rep7))
    check("and later edits can anchor on earlier ones",
          "def util():" in out7 and "return 99" in out7, out7)

    ops_bad = [EditOp("def run():", "def go():"),
               EditOp("THIS IS NOT THERE", "x")]
    out8, rep8 = apply_edits(ORIG, ops_bad)
    check("a failing edit stops the chain",
          rep8[0]["ok"] and not rep8[1]["ok"], str(rep8))
    check("earlier successful edits are kept, not silently reverted",
          "def go()" in out8, out8)
    check("and the failed edit changed nothing", "x" not in out8, out8)

    # ---- parsing --------------------------------------------------------
    p1, e1 = parse_edit_ops({"edits": [{"find": "a", "replace": "b"}]})
    check("find/replace is understood", len(p1) == 1 and not e1, e1)

    p2, e2 = parse_edit_ops({"edits": [{"old": "a", "new": "b"}]})
    check("old/new is understood", len(p2) == 1 and not e2, e2)

    p3, e3 = parse_edit_ops({"edits": [{"anchor": "a", "replacement": "b"}]})
    check("anchor/replacement is understood", len(p3) == 1 and not e3, e3)

    p4, e4 = parse_edit_ops({"anchor": "a", "replacement": "b"})
    check("a single edit at the top level is understood",
          len(p4) == 1 and not e4, e4)

    p5, e5 = parse_edit_ops({"edits": []})
    check("an empty edits array is refused", not p5 and bool(e5), e5)

    p6, e6 = parse_edit_ops({"edits": [{"anchor": "a", "replacement": "a"}]})
    check("a no-op edit is refused", not p6 and bool(e6), e6)

    p7, e7 = parse_edit_ops({"nonsense": True})
    check("a reply with no edits is refused with a usable message",
          not p7 and "edits" in e7, e7)

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: anchored edits — exact, tolerant, ambiguity-safe, chainable")
    return 0

if __name__ == "__main__":
    sys.exit(main())
