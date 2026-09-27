"""Verification checks — the "is it actually done" gate.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_verify.py

Pins that the command is *discovered from the project* and never invented, that
a passing check is reported as verified, a failing one as unverified, and a
project with no check says so rather than pretending.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.codemode import verify

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

async def run():
    tmp = Path(tempfile.mkdtemp(prefix="verify_"))
    try:
        # ---- nothing to discover -----------------------------------------
        empty = tmp / "empty"
        empty.mkdir()
        det = verify.detect_command(empty)
        check("a project with no check invents none",
              det["command"] == "", str(det))
        check("and says what it looked for",
              "no" in det["reason"].lower(), det["reason"])

        res = await verify.run_verification(empty)
        check("running with no check reports not-run, not a pass",
              res.get("ran") is False and res.get("ok") is False, str(res))
        check("and the verdict line is honest",
              "not verified" in verify.verdict_line(res).lower(),
              verify.verdict_line(res))

        # ---- package.json script ------------------------------------------
        npm = tmp / "node_proj"
        npm.mkdir()
        (npm / "package.json").write_text(json.dumps({
            "scripts": {"verify": "node -e \"console.log('ok')\""}
        }), encoding="utf-8")
        det = verify.detect_command(npm)
        check("a package.json verify script is found",
              det["command"] == "npm run verify", str(det))
        check("and the kind is npm", det["kind"] == "npm", str(det))

        # ---- a passing check ----------------------------------------------
        good = tmp / "good"
        good.mkdir()
        (good / "package.json").write_text(json.dumps({
            "scripts": {"verify": "node -e \"console.log('all good')\""}
        }), encoding="utf-8")
        res = await verify.run_verification(good)
        if res.get("ran") is False:
            # No node on PATH — the detection is still what matters.
            check("a check that could not run is reported as not-run",
                  res.get("ok") is False, str(res))
        else:
            check("a passing check is reported verified", res.get("ok") is True,
                  str(res))
            check("with exit code 0", res.get("exit_code") == 0, str(res))
            check("and the verdict says verified",
                  "verified" in verify.verdict_line(res).lower()
                  and "not verified" not in verify.verdict_line(res).lower(),
                  verify.verdict_line(res))

        # ---- a failing check ----------------------------------------------
        bad = tmp / "bad"
        bad.mkdir()
        (bad / "package.json").write_text(json.dumps({
            "scripts": {"verify": "node -e \"process.exit(3)\""}
        }), encoding="utf-8")
        res = await verify.run_verification(bad)
        if res.get("ran"):
            check("a failing check is reported unverified",
                  res.get("ok") is False, str(res))
            # `npm run` reports its own exit code 1 whatever the script exited
            # with, so a non-zero code is what can be asserted here — asserting
            # == 3 would be asserting npm's behaviour, not ours.
            check("with a non-zero exit code",
                  isinstance(res.get("exit_code"), int)
                  and res["exit_code"] != 0, str(res))
            check("and the verdict names the failure",
                  "not verified" in verify.verdict_line(res).lower(),
                  verify.verdict_line(res))

        # ---- a python project with tests/ ---------------------------------
        pyproj = tmp / "py_proj"
        (pyproj / "tests").mkdir(parents=True)
        (pyproj / "tests" / "test_x.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n",
            encoding="utf-8")
        det = verify.detect_command(pyproj)
        check("a Python tests/ layout is recognised",
              det["command"] != "", str(det))
        check("and points at a python runner",
              ("pytest" in det["command"] or "unittest" in det["command"]),
              str(det))

        # ---- output summarising -------------------------------------------
        s = verify._summarise("3 failed, 12 passed in 0.4s")
        check("pytest counts are read", s.get("passed") == 12
              and s.get("failed") == 3, str(s))
        s2 = verify._summarise("test result: FAILED. 2 passed; 1 failed")
        check("cargo counts are read", s2.get("passed") == 2
              and s2.get("failed") == 1, str(s2))
        s3 = verify._summarise("no counts here")
        check("an unparseable summary yields no counts", s3 == {}, str(s3))

        # ---- explicit command overrides detection --------------------------
        res = await verify.run_verification(
            empty, command='node -e "process.exit(0)"')
        if res.get("ran"):
            check("an explicit command is used when given",
                  res.get("ok") is True, str(res))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    asyncio.run(run())
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: verification — discovered, not invented; honest when absent")
    return 0

if __name__ == "__main__":
    sys.exit(main())
