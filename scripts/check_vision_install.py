"""The "Install torch + transformers" button must be able to succeed.

The bug this closes: the button ran

    pip install --target <install>/python/Lib/site-packages torch transformers ...

Under a per-machine install that target is inside ``C:\\Program Files``, which a
normal user cannot write. pip downloaded the whole stack — several GB — and then
died at the final copy:

    PermissionError: [WinError 5] Access is denied: ...\\site-packages\\einops

Nothing surfaced. The button went back to "Install" and the packages were absent
both times it was pressed. Two separate faults made that invisible:

  * the destination was chosen without asking whether it could be written to, and
  * success was decided by pip's exit code alone, so a `--target` install into a
    directory the process cannot import from still counted as done.

The fix installs into the writable data directory and puts it on `sys.path`.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_vision_install.py
"""

import os
import pathlib
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = os.environ.get("ADDLED_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}"
          + (f"  <- {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(label)


def main() -> int:
    os.environ.pop("ADDLED_DATA_DIR", None)
    from backend import app_paths
    from backend.local_llm import manager as llm

    print("=== the install target is writable ===")
    target = llm._app_site_packages()
    check("a target was chosen", bool(target), "None means pip's default scheme")
    if target:
        check("and it can actually be written to",
              app_paths._writable(pathlib.Path(target)),
              f"{target} — this is the Program Files case")
        # The whole point: pip writes here, so the process must import from here.
        check("and the running process searches it",
              str(pathlib.Path(target)) in sys.path,
              f"{target} is not on sys.path, so an install would be invisible")
        if os.name == "nt":
            program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
            check("on Windows it is not inside Program Files",
                  not str(target).lower().startswith(program_files.lower()),
                  f"{target} — a per-machine install cannot write there")

    print("\n=== the data directory is what a read-only install falls back to ===")
    # Force the read-only case and confirm the target moves out of the install.
    #
    # The candidate list in `_app_site_packages` is derived from
    # `sys.executable`, so the test has to move BOTH: point the interpreter at a
    # fake install whose site-packages denies writes, and confirm the choice
    # leaves it. Setting only one of the two tests nothing.
    original_legacy = app_paths.LEGACY_DIR
    original_data = app_paths.DATA_DIR
    original_pylibs = app_paths.PYLIBS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        try:
            fake_exe = pathlib.Path(tmp) / "install" / "python" / "python.exe"
            fake_exe.parent.mkdir(parents=True)
            fake_exe.write_bytes(b"")           # only its *path* matters
            denied_site = fake_exe.parent / "Lib" / "site-packages"
            denied_site.mkdir(parents=True)     # exists, but denies writes

            data = pathlib.Path(tmp) / "data"
            data.mkdir()
            app_paths.LEGACY_DIR = pathlib.Path(tmp) / "install" / "memory"
            app_paths.DATA_DIR = data
            app_paths.PYLIBS_DIR = data / "pylibs"

            real_executable = sys.executable
            real_writable = app_paths._writable
            app_paths._writable = lambda p: False if pathlib.Path(p) == denied_site \
                else real_writable(p)
            sys.executable = str(fake_exe)
            try:
                chosen = llm._app_site_packages()
            finally:
                sys.executable = real_executable
                app_paths._writable = real_writable

            check("an unwritable site-packages is not used",
                  str(denied_site) != str(chosen), str(chosen))
            check("the writable data directory is used instead",
                  chosen is not None and str(data) in str(chosen), str(chosen))
            check("and it was added to sys.path",
                  chosen is not None and str(chosen) in sys.path, str(chosen))
        finally:
            app_paths.LEGACY_DIR = original_legacy
            app_paths.DATA_DIR = original_data
            app_paths.PYLIBS_DIR = original_pylibs

    print("\n=== success is not decided by pip's exit code alone ===")
    # A `--target` install into a directory the process cannot import from exits
    # 0 and leaves the button looking like it worked. The handler must therefore
    # import the packages before saying ok.
    source = pathlib.Path(ROOT, "backend", "local_llm", "manager.py").read_text(
        encoding="utf-8")
    # Matched on the mechanism, not on a message: the sentence is split across
    # two string literals in the source, so a search for the whole phrase finds
    # nothing even when the code is right. That is a check that fails for the
    # wrong reason, which is worse than no check.
    check("the handler imports what it installed",
          "importlib.util.find_spec(name) is None" in source,
          "the ok/not-ok decision is back on the exit code alone")
    check("and refuses to report success when the import fails",
          "raise RuntimeError" in source and "if absent:" in source,
          "nothing stops a silent no-op being reported as done")

    print("\n=== and the failure is visible rather than silent ===")
    check("a failed install broadcasts its phase",
          '"phase": "failed"' in source, "nothing would reach the UI")
    check("the error text is carried in the broadcast",
          '"detail": str(exc)' in source or "str(exc)" in source,
          "the UI could not say why")

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the vision install targets a writable, importable directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
