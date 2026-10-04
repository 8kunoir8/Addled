"""Does a package in `pylibs` lose to an older copy bundled with the app?

The bug, seen in the log on every start:

    transformers embedder 'minilm' failed (cannot import name 'httpx' from
    'huggingface_hub.utils' (...site-packages\\huggingface_hub\\utils\\...))

`huggingface_hub` existed in two places: the bundled site-packages had 1.27.0
(no `httpx` export) and `pylibs` had 1.33.0 (has it). `transformers` exists ONLY
in `pylibs` and needs the newer one, so it failed to import entirely and the
embedding backend silently fell back to hashed n-grams — which its own code
describes as "meaningless for semantic recall".

`add_pylibs_to_path` appended `pylibs`, so the bundled site-packages (earlier on
sys.path) always won. The fix puts `pylibs` ahead of it when a name exists in
both.

The assertion that matters is BEHAVIOURAL: after the call, every package present
in both directories resolves to the `pylibs` copy. Asserting on `sys.path` order
alone would pass while a stale import was already cached.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\Kunoir\Codeground\Clicky\Addled")

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


from backend import app_paths  # noqa: E402

print("=== the two directories ===")
print(f"  pylibs  : {app_paths.PYLIBS_DIR}")
print(f"  bundled : {sys.prefix}\\Lib\\site-packages")

bundled_names = app_paths._dist_info_names(
    __import__("pathlib").Path(sys.prefix) / "Lib" / "site-packages")
pylibs_names = app_paths._dist_info_names(app_paths.PYLIBS_DIR)
print(f"  names in bundled : {len(bundled_names)}")
print(f"  names in pylibs  : {len(pylibs_names)}")
print(f"  in BOTH          : {len(bundled_names & pylibs_names)}")

# The condition this check exists for needs BOTH directories populated. A dev
# tree has an empty `pylibs` (packages are installed into it on demand by the
# vision and browser installers, and the checks that install them clean up
# after themselves), so there is nothing to rank and no overlap to judge. The
# precedence rule cannot be exercised there. Report that plainly and pass —
# failing would blame the code for an environment that cannot test it, and
# `check_all.py` runs everything with one interpreter.
overlap = bundled_names & pylibs_names
if not bundled_names or not pylibs_names or not overlap:
    print()
    print("  SKIP: this needs the installed app.")
    print(f"        bundled={len(bundled_names)} pylibs={len(pylibs_names)} "
          f"overlap={len(overlap)}")
    print("        The rule matters only when a name exists in both, which")
    print("        happens on an install that has fetched optional packages.")
    print("        Run this with the installed interpreter to exercise it:")
    print(r"          C:\Program Files\Addled\resources\python\python.exe -s"
          " scripts\\check_pylibs_precedence.py")
    print()
    print("all pylibs-precedence checks passed (skipped: nothing to compare)")
    raise SystemExit(0)

print()
print("=== _duplicated_in_pylibs reports exactly the overlap ===")
dup = app_paths._duplicated_in_pylibs()
check("it returns the intersection", dup == (bundled_names & pylibs_names),
      f"got {len(dup)}, want {len(bundled_names & pylibs_names)}")

print()
print("=== after add_pylibs_to_path(), pylibs wins each of them ===")
app_paths.add_pylibs_to_path()

import importlib.util  # noqa: E402

# Where a duplicated name actually resolves. `find_spec` raises for a missing
# parent package, so a failure to find counts as "not found", not a crash.
def resolves_to(name):
    try:
        spec = importlib.util.find_spec(name.replace("-", "_"))
    except (ImportError, ModuleNotFoundError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    origin = str(spec.origin)
    if "pylibs" in origin:
        return "pylibs"
    if "site-packages" in origin:
        return "bundled"
    return "other"


# Only names that are importable at all can be judged; a dist-info can exist for
# something whose module is not installed under a matching name.
checked = 0
shadowed = []
for name in sorted(dup):
    where = resolves_to(name)
    if where is None:
        continue
    checked += 1
    if where != "pylibs":
        shadowed.append(f"{name}->{where}")

check("some duplicated packages were importable to check", checked > 0,
      "none resolved, so this proved nothing")
check("no duplicated package resolves to the bundled copy", not shadowed,
      f"{len(shadowed)} shadowed: {shadowed[:6]}")

print()
print("=== pylibs comes BEFORE the bundled site-packages ===")
def first_index(pred):
    for i, p in enumerate(sys.path):
        if pred(p):
            return i
    return -1

i_pylibs = first_index(lambda p: "pylibs" in p)
i_bundled = first_index(lambda p: "site-packages" in p and "pylibs" not in p)
print(f"  pylibs at {i_pylibs}, bundled site-packages at {i_bundled}")
check("pylibs is ahead", i_pylibs != -1 and i_pylibs < i_bundled,
      f"pylibs={i_pylibs} bundled={i_bundled}")

print()
print("=== it is idempotent ===")
# Called once per startup by main.py, and again by the forge and the installers.
before_count = sys.path.count(str(app_paths.PYLIBS_DIR))
app_paths.add_pylibs_to_path()
app_paths.add_pylibs_to_path()
after_count = sys.path.count(str(app_paths.PYLIBS_DIR))
check("calling it repeatedly does not duplicate the entry",
      after_count == 1, f"appeared {after_count} times (was {before_count})")

print()
print("=== the specific case from the log ===")
# `transformers` exists only in pylibs and needs the newer huggingface_hub. If
# the precedence is wrong, this import fails — which is exactly the log line.
try:
    import transformers  # noqa: F401
    check("transformers imports", True)
except Exception as e:
    check("transformers imports", False, f"{type(e).__name__}: {e}")

try:
    from backend.memory import embedding
    embedding._load()
    kind = embedding.embedder_kind()
    print(f"  embedding backend: {kind}")
    check("the embedder did NOT fall back to hashing", kind != "hash",
          "semantic recall is degraded to hashed n-grams")
except Exception as e:
    check("the embedding backend loads", False, f"{type(e).__name__}: {e}")

print()
print("FAILED: " + ", ".join(fails) if fails
      else "all pylibs-precedence checks passed")
raise SystemExit(1 if fails else 0)
