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

        # ---- "ran but found nothing" is not "the tests failed" -------------
        #
        # Most runners exit non-zero when they collect no tests, so a check that
        # only looked at the exit code would tell the user their change broke
        # the tests when the project simply has none. The Code page renders the
        # verdict directly, so the wording is asserted, not just the flag.
        empty_proj = tmp / "empty_tests"
        (empty_proj / "tests").mkdir(parents=True)
        (empty_proj / "tests" / "test_none.py").write_text(
            "import unittest\n", encoding="utf-8")
        det_e = verify.detect_command(empty_proj)
        if det_e.get("command"):
            res_e = await verify.run_verification(
                empty_proj, command=det_e["command"])
            if res_e.get("ran") and not res_e.get("ok"):
                # A run that produced no counts is either "found no tests" or
                # "the runner could not start" (a broken plugin raises at import
                # time). Both are honest and neither blames the code, which is
                # the property this check exists to protect -- so assert THAT,
                # rather than one of the two environmental outcomes. A genuine
                # environmental failure should not read as a bug in the code.
                line = verify.verdict_line(res_e)
                recognised = (res_e.get("noTests") is True
                              or res_e.get("startFailed") is True)
                check("a run that collects nothing is flagged",
                      recognised,
                      "neither noTests nor startFailed was set: "
                      + str(res_e)[:300])
                check("and the verdict names a real reason, not a failure",
                      "failed with exit code" not in line.lower(),
                      line)
                if res_e.get("noTests"):
                    check("a runner that ran says 'no tests'",
                          "no tests" in line.lower(), line)
                else:
                    check("a runner that could not start says so",
                          "could not start" in line.lower(), line)
            else:
                print("  (empty project unexpectedly passed; nothing to assert)")
        else:
            print("  (no runner detected for an empty project; skipped)")

        # A genuine failure must NOT be labelled "no tests", or the distinction
        # would be worse than useless.
        fake = {"ran": True, "ok": False, "command": "pytest",
                "exit_code": 1, "summary": {"failed": 2}}
        check("a real failure is not labelled 'no tests'",
              "no tests" not in verify.verdict_line(fake).lower(),
              verify.verdict_line(fake))
        check("a passing run is never flagged as no-tests",
              verify._found_no_tests("3 passed in 0.2s", {"passed": 3}) is False,
              "a pass must be unreachable by the no-tests path")

        # ---- a runner that CRASHED is not a test failure either -----------
        #
        # Seen on a real machine: a broken pytest plugin raises at import time,
        # so pytest prints a traceback and exits 1 with no counts. The verdict
        # used to say "failed with exit code 1", telling the user their change
        # broke the tests when pytest never started. Three outcomes, not two.
        crashed = (
            "Traceback (most recent call last):\n"
            '  File "<frozen runpy>", line 203, in _run_module_as_main\n'
            "ModuleNotFoundError: No module named 'pytest_xprocess'\n"
        )
        check("a crashed runner is not called a test failure",
              "failed with exit code" not in
              verify.verdict_line({"ran": True, "ok": False, "command": "pytest",
                                   "exit_code": 1, "summary": {},
                                   "startFailed": True}).lower(),
              verify.verdict_line({"ran": True, "ok": False, "command": "pytest",
                                   "exit_code": 1, "summary": {},
                                   "startFailed": True}))
        check("a start-up crash is recognised from its traceback",
              verify._found_start_failure(crashed, {}) is True,
              "a runner that never started would be reported as a failure")
        check("a genuine test failure is NOT called a start-up crash",
              verify._found_start_failure(
                  "Traceback (most recent call last):\n"
                  "  File 't.py', line 1, in test_x\n"
                  "assert 1 == 2\n"
                  "1 failed in 0.1s\n",
                  {"failed": 1}) is False,
              "a real failure was mislabelled as the runner crashing")
        check("a passing run is not a start-up crash",
              verify._found_start_failure("3 passed in 0.2s", {"passed": 3})
              is False,
              "a pass must be unreachable by the crash path")

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
