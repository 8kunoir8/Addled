"""Does a forged dependency actually install into a WRITABLE place, and import?

The bug: `forge.install` ran a plain `pip install`, which writes to the
interpreter's own site-packages — `C:\\Program Files\\...` for an installed
Addled, and therefore read-only. Every dependency failed with WinError 5 and the
skill was written without it.

This proves the fix end to end with a real, tiny, pure-Python package: the argv
must carry `--target`, pip must succeed, the file must land in pylibs, and a
FRESH interpreter must be able to import it.

The package must be one that is in NEITHER the bundled site-packages NOR pylibs
to begin with, or the test proves nothing. The first version used `six`, which
ships inside the app's own `Lib/site-packages` — so the install "worked", the
probe said OK, and every assertion passed while `_import_probe` was in fact
broken: it imported `backend` to find pylibs, that import fails under `-c`, and
the swallowed error meant pylibs was never searched. A dependency installed into
pylibs was then reported MISSING. `six` hid it because the bundled copy already
satisfied the probe. Hence: pick a package that is absent everywhere, and
assert directly that the probe can see pylibs.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\Kunoir\Codeground\Clicky\Addled")

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


from backend import app_paths
from backend.skills.forge import _import_probe, importable_name, pip_argv

# Tiny, pure python, no build step, and NOT shipped with the app. `six` was the
# original choice and it was wrong — it is in the app's bundled site-packages,
# so the probe found it without looking in pylibs and the real bug hid.
PACKAGE = "humanize"
IMPORT_NAME = "humanize"

# The bundled interpreter's site-packages: the other place a package could hide,
# and the place `--target` exists to avoid writing to.
_bundled = Path(sys.prefix, "Lib", "site-packages")

# Remove any earlier copy from pylibs so the precondition holds.
#
# The check needs the package ABSENT or the install proves nothing — and it
# installs the package itself, so it consumes its own precondition. A previous
# run, or the forge itself, therefore left it present and the next run failed
# with "a package that is already present cannot prove an install happened".
# A check that only passes on a pristine machine is not a check.
_removed = []
if app_paths.PYLIBS_DIR.is_dir():
    for _p in list(app_paths.PYLIBS_DIR.glob(f"{PACKAGE}*")):
        try:
            if _p.is_dir():
                import shutil
                shutil.rmtree(_p, ignore_errors=True)
            else:
                _p.unlink(missing_ok=True)
            _removed.append(_p.name)
        except OSError:
            pass
if _removed:
    print(f"  (removed an earlier {PACKAGE} from pylibs: {_removed})")
    import importlib
    importlib.invalidate_caches()

# The precondition the whole test rests on: the package must be absent, in BOTH
# pylibs and the bundled site-packages, or a pass would be meaningless.
# `_import_probe` returns "" when the package IS importable and a message when
# it is not — so absence is a NON-EMPTY result. (Getting this backwards made the
# check fail on a correctly absent package.)
check(f"'{PACKAGE}' is not already importable", _import_probe(IMPORT_NAME) != "",
      "a package that is already present cannot prove an install happened")
check(f"'{PACKAGE}' is absent from the bundled site-packages",
      not (_bundled / PACKAGE).exists()
      and not (_bundled / f"{IMPORT_NAME}.py").exists(),
      f"implausible for {_bundled} to contain it; pick another package")

print("=== the probe can actually SEE pylibs ===")
# The specific regression: the probe told the child to `from backend import
# app_paths`, which fails under `-c`, and the `except` hid it — so pylibs was
# never on the child's path. Dropping an importable file straight into pylibs
# and asking the probe about it fails if the probe cannot see that directory,
# regardless of what the package is.
_probe_sentinel = app_paths.PYLIBS_DIR / "addled_probe_sentinel_xyz.py"
_probe_sentinel.parent.mkdir(parents=True, exist_ok=True)
try:
    _probe_sentinel.write_text("VALUE = 1\n", encoding="utf-8")
    check("a module placed ONLY in pylibs is found by the probe",
          _import_probe("addled_probe_sentinel_xyz") == "",
          "the probe is not searching pylibs — it will reject working installs")
finally:
    _probe_sentinel.unlink(missing_ok=True)

print("=== the argv targets a writable directory ===")
argv = pip_argv(f"pip install {PACKAGE}")
print("  argv:", " ".join(argv or []))
check("the command was recognised", argv is not None)
check("it runs the CURRENT interpreter's pip",
      argv[:3] == [sys.executable, "-s", "-m"], str(argv[:4]))
check("it installs with --target", "--target" in argv, str(argv))
check("the target is the app's own pylibs dir",
      str(app_paths.PYLIBS_DIR) in argv, str(argv))
# `-s` matters: the app runs with it, and pip writing to a user-site the app
# does not search installs the package invisibly.
check("it passes -s, matching how the app runs", "-s" in argv, str(argv))

print()
print("=== the import name is derived for verification ===")
check("a plain name", importable_name("pip install six") == "six")
check("a pinned name", importable_name("pip install six==1.16.0") == "six")
check("an extras name", importable_name("pip install foo[bar]") == "foo")
check("not an install command",
      importable_name("npm install x") == "")

print()
print("=== it really installs, into pylibs, and imports ===")
before = app_paths.PYLIBS_DIR.exists()
print(f"  pylibs before: {app_paths.PYLIBS_DIR} (exists={before})")

# Record what was there, so everything this test adds can be removed. Without
# this the suite pollutes the working tree: `check_packaging` caught a leftover
# `six-1.17.0.dist-info` that the installer would otherwise have shipped.
preexisting = {p.name for p in app_paths.PYLIBS_DIR.glob("*")} \
    if app_paths.PYLIBS_DIR.exists() else set()

try:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300,
                          encoding="utf-8", errors="replace")
    check("pip exited 0", proc.returncode == 0, (proc.stderr or "")[-300:])
    landed = list(app_paths.PYLIBS_DIR.glob(f"{PACKAGE}*"))
    check(f"'{PACKAGE}' landed in pylibs", bool(landed),
          f"nothing matching {PACKAGE}* in {app_paths.PYLIBS_DIR}")
    # Honest now, because PACKAGE is absent everywhere to begin with: nothing
    # of its name may appear under the bundled site-packages. (The old `six`
    # version had to weaken this with `or bool(landed)` precisely because `six`
    # already existed there — which is what let the probe bug hide.)
    check("it did NOT go to the read-only site-packages",
          not (_bundled / PACKAGE).exists()
          and not (_bundled / f"{IMPORT_NAME}.py").exists(),
          "installed in the interpreter instead of the target")

    # The verification the forge now performs: a FRESH interpreter, with pylibs
    # on its path, must import it. pip's exit code alone is not proof.
    # Asked about the IMPORT name, the way install() asks.
    problem = _import_probe(IMPORT_NAME)
    check("a fresh interpreter can import it", problem == "", problem)
finally:
    # Remove exactly what this test added, and nothing else. pylibs is shared
    # with the vision and browser installers, so a blanket wipe would take out
    # packages the user needs.
    for path in list(app_paths.PYLIBS_DIR.glob("*")):
        if path.name not in preexisting:
            try:
                if path.is_dir():
                    import shutil
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    left = {p.name for p in app_paths.PYLIBS_DIR.glob("*")} - preexisting \
        if app_paths.PYLIBS_DIR.exists() else set()
    check("the test cleaned up after itself", not left,
          f"left behind: {sorted(left)}")

print()
print("=== and the check NOTICES a package that is not there ===")
missing = _import_probe("definitely_not_a_real_package_xyz")
check("a missing package is reported", bool(missing), repr(missing))
check("and the message explains it would fail on first use",
      "first call" in missing or "cannot be imported" in missing,
      missing[:150])

print()
print("=== a compiled package broken by an interpreter swap reads as MISSING ===")
# pylibs SURVIVES a release update, but it was filled by whichever interpreter
# was current. A release can ship a different `resources\python`. Pure-Python
# packages survive; one with a compiled extension does not, because the
# extension carries the interpreter's ABI tag (`cp314-win_amd64.pyd`).
#
# What matters is which way the probe fails. If `find_spec` matched on the
# filename alone, a wrong-ABI package would report OK and the skill would die on
# its first call — the exact silent failure this whole change removes. It does
# not: the importer filters by ABI, so the package reads as MISSING and the
# probe says so. This asserts that, since it is a property of the interpreter
# the fix is relying on rather than anything the fix controls.
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402

# Build a package in the test's OWN temp dir, not in pylibs: this must not
# touch the directory the app shares, and a stray entry there would ship.
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    pkg = Path(tmp) / "addled_abi_probe_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    here = importlib.machinery.EXTENSION_SUFFIXES[0]      # .cp314-win_amd64.pyd
    tag = here.split("-")[0]                              # .cp314
    other = here.replace(tag, ".cp313")                   # another release
    (pkg / f"_sonly{here}").write_bytes(b"")
    (pkg / f"_sodd{other}").write_bytes(b"")

    sys.path.insert(0, tmp)
    importlib.invalidate_caches()

    def _sees(mod):
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ModuleNotFoundError):
            return False

    try:
        check("an extension for THIS interpreter is found",
              _sees("addled_abi_probe_pkg._sonly"),
              "the experiment is not measuring what it claims")
        check("an extension for a DIFFERENT interpreter is NOT found",
              not _sees("addled_abi_probe_pkg._sodd"),
              "find_spec matched on filename alone, so after an interpreter "
              "swap the probe would report OK for a package that cannot load")
    finally:
        sys.path.remove(tmp)
        importlib.invalidate_caches()

print()
print("FAILED: " + ", ".join(fails) if fails
      else "all forge-install checks passed")
raise SystemExit(1 if fails else 0)
