"""Self-modification checks — the guards around editing Addled's own code.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_selfmod.py

The point of these is not that a change can be made — it is that the dangerous
outcomes are impossible: a guarded file is refused, an unparseable proposal is
refused, apply() needs explicit confirmation, and a file that changed under it is
not silently overwritten.

Every check runs against a throwaway copy so nothing here can touch the real
backend/.
"""

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.codemode import selfmod

fails = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def run():
    tmp = Path(tempfile.mkdtemp(prefix="selfmod_"))
    real_stage = selfmod.STAGE_DIR
    real_root = selfmod._backend_root
    try:
        # A fake backend tree, so nothing here writes to the real one.
        backend = tmp / "backend"
        (backend / "skills").mkdir(parents=True)
        (backend / "safety").mkdir(parents=True)
        (backend / "skills" / "tool_loop.py").write_text(
            "MAX = 8\n", encoding="utf-8")
        (backend / "config.py").write_text("X = 1\n", encoding="utf-8")
        (backend / "main.py").write_text("Y = 1\n", encoding="utf-8")
        (backend / "safety" / "destruction_gate.py").write_text(
            "DENY = True\n", encoding="utf-8")
        (backend / "actions").mkdir()
        (backend / "actions" / "executor.py").write_text("E = 1\n",
                                                         encoding="utf-8")

        selfmod.STAGE_DIR = tmp / "stage"
        selfmod._backend_root = lambda: backend

        # ---- guards -------------------------------------------------------
        r = selfmod.propose("config.py", "X = 2\n")
        check("the config loader cannot be self-edited",
              r.get("success") is False and "no-self-edit" in r.get("error", ""),
              str(r))

        r = selfmod.propose("main.py", "Y = 2\n")
        check("the entry point cannot be self-edited",
              r.get("success") is False, str(r))

        r = selfmod.propose("safety/destruction_gate.py", "DENY = False\n")
        check("the destruction gate cannot be self-edited",
              r.get("success") is False and "no-self-edit" in r.get("error", ""),
              str(r))

        r = selfmod.propose("actions/executor.py", "E = 2\n")
        check("the approval executor cannot be self-edited",
              r.get("success") is False, str(r))

        r = selfmod.propose("../outside.py", "x = 1\n")
        check("a path escaping backend/ is refused",
              r.get("success") is False, str(r))

        r = selfmod.propose("C:/Windows/system32/x.py", "x = 1\n")
        check("an absolute path is refused", r.get("success") is False, str(r))

        r = selfmod.propose("skills/tool_loop.py", "MAX = 8\n")
        check("a proposal identical to the file is refused",
              r.get("success") is False, str(r))

        r = selfmod.propose("skills/tool_loop.py", "def (broken:\n")
        check("an unparseable proposal is refused before it is written",
              r.get("success") is False and "parse" in r.get("error", "").lower(),
              str(r))
        check("and the real file was not touched",
              (backend / "skills" / "tool_loop.py").read_text(encoding="utf-8")
              == "MAX = 8\n", "the file changed")

        # ---- a real, valid proposal ---------------------------------------
        r = selfmod.propose("skills/tool_loop.py", "MAX = 12\n",
                            reason="allow more rounds")
        check("a valid proposal is staged", r.get("success") is True, str(r))
        token = r.get("token")
        check("with a token to apply it", bool(token), str(r))
        check("and a diff to show", "MAX" in (r.get("diff") or ""),
              str(r)[:200])
        check("the real file is still unchanged",
              (backend / "skills" / "tool_loop.py").read_text(encoding="utf-8")
              == "MAX = 8\n", "propose() wrote the file")

        # ---- apply needs confirmation -------------------------------------
        r = selfmod.apply(token, confirm=False)
        check("apply without confirmation is refused",
              r.get("success") is False and "confirm" in r.get("error", "").lower(),
              str(r))
        check("and still nothing was written",
              (backend / "skills" / "tool_loop.py").read_text(encoding="utf-8")
              == "MAX = 8\n", "an unconfirmed apply wrote the file")

        # ---- the file changing underneath --------------------------------
        (backend / "skills" / "tool_loop.py").write_text("MAX = 99\n",
                                                         encoding="utf-8")
        r = selfmod.apply(token, confirm=True)
        check("applying over a newer edit is refused by default",
              r.get("success") is False and "changed" in r.get("error", "").lower(),
              str(r))
        check("and the newer edit survives",
              (backend / "skills" / "tool_loop.py").read_text(encoding="utf-8")
              == "MAX = 99\n", "the user's edit was overwritten")

        # ---- a confirmed apply -------------------------------------------
        r = selfmod.apply(token, confirm=True, allow_dirty=True)
        check("a confirmed apply writes the file", r.get("success") is True,
              str(r))
        check("and reports that a restart is needed",
              r.get("restartRequired") is True, str(r))
        check("the file now holds the proposal",
              (backend / "skills" / "tool_loop.py").read_text(encoding="utf-8")
              == "MAX = 12\n", "the proposal was not applied")

        # ---- revert -------------------------------------------------------
        listed = selfmod.list_pending()
        check("the applied change is still listed for revert",
              any(p.get("token") == token for p in listed), str(listed))

        active = backend / "skills" / "tool_loop.py"
        snapshot = tmp / "pre_apply.py"
        snapshot.write_text("MAX = 99\n", encoding="utf-8")
        # Re-stage so revert restores the pre-proposal content, which is what
        # revert is for.
        r = selfmod.revert(token)
        check("revert succeeds", r.get("success") is True, str(r))
        check("the original file contents are restored",
              active.read_text(encoding="utf-8") == "MAX = 8\n",
              active.read_text(encoding="utf-8"))

        # ---- discard ------------------------------------------------------
        r = selfmod.propose("skills/tool_loop.py", "MAX = 13\n")
        tok2 = r.get("token")
        d = selfmod.discard(tok2)
        check("discarding a proposal succeeds", d.get("success") is True, str(d))
        check("and the file is untouched",
              active.read_text(encoding="utf-8") == "MAX = 8\n",
              active.read_text(encoding="utf-8"))
        check("reverting a discarded proposal reports it is gone",
              selfmod.revert(tok2).get("success") is False, "no such token")

        # ---- the real module is syntactically fine -------------------------
        ast.parse(Path(selfmod.__file__).read_text(encoding="utf-8"))
        check("selfmod.py itself parses", True)
    finally:
        selfmod.STAGE_DIR = real_stage
        selfmod._backend_root = real_root
        shutil.rmtree(tmp, ignore_errors=True)

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: self-modification — staged, confirmed, guarded, revertible")
    return 0

if __name__ == "__main__":
    sys.exit(main())
