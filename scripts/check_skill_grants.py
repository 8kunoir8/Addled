"""Checks for a grant being bound to the skill it was given for.

"Always allow" is keyed by name, and a name is not an identity. Removing
`pdf_tools` and installing a different `pdf_tools` from another repository
would inherit the permission without anyone being asked — so the grant is
stored against a digest of the skill's own files, and a skill whose code
changed asks again.

Three directions matter, and all three are tested here:

* the same skill, allowed, keeps its permission across a reload;
* the same *name* with different code loses it;
* a grant that cannot be checked is not honoured, because a failure to tell
  must fall back to the prompt rather than to permission.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_skill_grants.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.approvals import policy
# By module path: the package exports a singleton *named* `market`, so a
# `from backend.skills import market` would bind the loader instance instead of
# the module.
import importlib
market = importlib.import_module("backend.skills.market")

fails: list[str] = []

def check(label, cond, detail=""):
    if not cond:
        fails.append(f"{label}: {detail}")

def write_skill(folder: Path, name: str, script: str, body: str = "Do the thing.\n"):
    """An installed skill: a SKILL.md and the script beside it."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}",
        encoding="utf-8")
    if script is not None:
        (folder / "main.py").write_text(script, encoding="utf-8")

def run():
    from backend.config import config

    saved = None
    try:
        saved = config.get("safety", "always_allow", default=None)
    except Exception:  # noqa: BLE001
        config = None

    real_dir = market.MARKET_DIR
    tmp = Path(tempfile.mkdtemp(prefix="skill_grant_"))
    try:
        market.MARKET_DIR = tmp
        policy.clear()

        # ---- the digest notices a change ---------------------------------
        a = tmp / "pdf_tools"
        write_skill(a, "pdf_tools", "print('v1')\n")
        first = market.folder_digest(a)
        check("an installed skill can be digested", bool(first), repr(first))

        same = tmp / "pdf_tools_copy"
        write_skill(same, "pdf_tools", "print('v1')\n")
        check("identical contents digest the same",
              market.folder_digest(same) == first,
              "the digest depends on something other than the contents")

        changed = tmp / "pdf_tools_changed"
        write_skill(changed, "pdf_tools", "print('v2')\n")
        check("a changed script digests differently",
              market.folder_digest(changed) != first,
              "a swapped script produced the same digest")

        extra = tmp / "pdf_tools_extra"
        write_skill(extra, "pdf_tools", "print('v1')\n")
        (extra / "helper.py").write_text("print('extra')\n", encoding="utf-8")
        check("an added file digests differently",
              market.folder_digest(extra) != first,
              "a new file did not change the digest")

        md_changed = tmp / "pdf_tools_md"
        write_skill(md_changed, "pdf_tools", "print('v1')\n",
                    body="Do something ELSE entirely.\n")
        check("an edited SKILL.md digests differently",
              market.folder_digest(md_changed) != first,
              "changed instructions did not change the digest")

        # A digest that cannot read a file must not claim to have read it.
        check("a folder that does not exist has no digest",
              market.folder_digest(tmp / "no_such_folder") == "",
              "an absent folder produced a digest")

        # ---- a grant keeps working for the same skill --------------------
        policy.clear()
        r = policy.always_allow(policy.SKILL, "pdf_tools")
        check("an installed skill can be granted", r.get("success") is True,
              str(r))
        check("and is allowed while its code is unchanged",
              policy.is_always_allowed(policy.SKILL, "pdf_tools") is True,
              "the grant did not take")

        # ---- ...and stops for a different skill of the same name ---------
        # This is the whole point: the same name, different code.
        write_skill(a, "pdf_tools", "print('COMPLETELY DIFFERENT')\n")
        check("the same name with different code is NOT allowed",
              policy.is_always_allowed(policy.SKILL, "pdf_tools") is False,
              "a swapped skill inherited the permission")

        # Restoring the original code restores the grant: the digest is about
        # the code, not about the fact that it changed once.
        write_skill(a, "pdf_tools", "print('v1')\n")
        check("restoring the original code restores the grant",
              policy.is_always_allowed(policy.SKILL, "pdf_tools") is True,
              "the grant did not come back with the code")

        # ---- an unreadable store must mean "ask" -------------------------
        real_read = policy._read

        def broken():
            raise RuntimeError("simulated failure")

        policy._read = broken
        try:
            check("an unreadable store does not allow a skill",
                  policy.is_always_allowed(policy.SKILL, "pdf_tools") is False,
                  "a broken store reported allowed")
        finally:
            policy._read = real_read

        # ---- a grant with no digest is not honoured ----------------------
        # Everything granted before fingerprints existed is in this state.
        # Silently trusting it would keep the gap open for every grant already
        # on disk, so it asks once more.
        raw = policy._read()
        raw["fingerprints"].pop("pdf_tools", None)
        policy._write(raw)
        check("a grant predating fingerprints is not honoured",
              policy.is_always_allowed(policy.SKILL, "pdf_tools") is False,
              "an unfingerprinted grant was honoured")

        # ---- revoking clears the digest as well --------------------------
        policy.clear()
        policy.always_allow(policy.SKILL, "pdf_tools")
        policy.revoke(policy.SKILL, "pdf_tools")
        check("revoking removes the name", "pdf_tools" not in
              policy.list_allowed()["skills"], "still listed")
        check("revoking removes the fingerprint",
              "pdf_tools" not in policy._read()["fingerprints"],
              "the digest was left behind")
        policy.always_allow(policy.SKILL, "pdf_tools")
        check("and a fresh grant still works afterwards",
              policy.is_always_allowed(policy.SKILL, "pdf_tools") is True,
              "a second grant did not take")

        # ---- built-ins are unaffected ------------------------------------
        # They have no folder of their own, and their code ships with the app,
        # so a name is an identity for them and the grant is simply honoured.
        policy.clear()
        r = policy.always_allow(policy.SKILL, "some_builtin_skill")
        check("a skill with no folder can still be granted",
              r.get("success") is True, str(r))
        check("and stays granted, since there is no code to bind to",
              policy.is_always_allowed(policy.SKILL, "some_builtin_skill")
              is True, "a built-in lost its grant")

        # ---- tools are untouched by any of this --------------------------
        policy.clear()
        r = policy.always_allow(policy.TOOL, "server::tool")
        check("a tool grant still works", r.get("success") is True, str(r))
        check("and is honoured with no fingerprint at all",
              policy.is_always_allowed(policy.TOOL, "server::tool") is True,
              "a tool grant needed a fingerprint")

        # ---- the guard still holds over all of it ------------------------
        check("a destructive name is still refused",
              policy.always_allow(policy.SKILL, "delete_file")
              .get("success") is False, "delete_file was granted")
    finally:
        market.MARKET_DIR = real_dir
        shutil.rmtree(tmp, ignore_errors=True)
        policy.clear()
        if config is not None:
            if saved is not None:
                config.set("safety", "always_allow", value=saved)
            else:
                config.set("safety", "always_allow",
                           value={"skills": [], "tools": [],
                                  "fingerprints": {}})

def main() -> int:
    run()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("  - " + f)
        print(f"\n{len(fails)} failure(s)")
        return 1
    print("PASS: skill grants — a permission follows the code it was given "
          "for, not the name")
    return 0

if __name__ == "__main__":
    sys.exit(main())
