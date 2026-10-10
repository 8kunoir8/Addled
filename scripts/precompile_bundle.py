"""Compile the bundled interpreter's bytecode once, in place.

The installer ships `python-bundle/` WITHOUT its `__pycache__` (see the
`python-bundle` filter in `electron-builder.yml`). That cache was 654 MB of a
938 MB bundle, and every byte of it is regenerable: Python writes a `.pyc`
beside a `.py` the first time it imports the module.

Stripping it only works if something puts it back, and this is that something.
It runs once from the installer (see `build/installer-precompile.nsh`), while
the installer still has write access to `C:\\Program Files`. Without it, the
app would try to compile numpy and PyQt6 into a directory it cannot write to,
on every launch, forever.

Best-effort by design: a failure here prints and exits 0, because a slow first
import is worth far less than a failed install. The app runs either way.

Usage:
    python scripts/precompile_bundle.py <path-to-python-bundle>
    python scripts/precompile_bundle.py            # defaults to ./python-bundle
"""
from __future__ import annotations

import compileall
import os
import pathlib
import sys


def _log(msg: str) -> None:
    print(f"[precompile] {msg}", flush=True)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        bundle = pathlib.Path(argv[1])
    else:
        bundle = pathlib.Path(__file__).resolve().parent.parent / "python-bundle"

    if not (bundle / "python.exe").is_file():
        _log(f"no interpreter at {bundle} — nothing to compile")
        return 0

    # Compile the standard library and the installed packages. `lib` is absent
    # from the embeddable layout (its stdlib is in the zip and the Lib folder),
    # so it is collected only when present rather than assumed.
    # `Lib` and `lib` are the same directory on Windows (case-insensitive), so
    # they are collected by resolved path rather than by name — otherwise the
    # tree is compiled twice and the log says so, which is exactly the kind of
    # thing that reads as a bug the next time someone looks.
    targets: list[pathlib.Path] = []
    seen: set[str] = set()
    for candidate in (bundle / "Lib", bundle / "lib"):
        key = os.path.normcase(str(candidate))
        if key not in seen and candidate.is_dir():
            seen.add(key)
            targets.append(candidate)
    if not targets:
        _log(f"no Lib/ under {bundle} — nothing to compile")
        return 0

    total_ok = True
    for target in targets:
        _log(f"compiling {target} ...")
        # quiet=1 keeps the log to failures and a summary rather than one line
        # per module (there are tens of thousands). force=False skips files
        # whose .pyc is already current, so a re-run is cheap.
        #
        # workers=0 reuses this interpreter instead of spawning one per CPU;
        # the installer already runs under an elevated, single-threaded NSIS
        # process and spawning a pool inside it has more ways to fail than it
        # saves. The work is I/O bound enough that this is not the slow path.
        ok = compileall.compile_dir(
            str(target),
            quiet=1,
            force=False,
            workers=0,
            legacy=False,          # PEP 3147 __pycache__ layout, which is what
                                   # the interpreter reads back at runtime
        )
        total_ok = total_ok and bool(ok)

    if total_ok:
        _log("done — the interpreter has its bytecode cache back")
    else:
        _log("some modules did not compile; the app will compile them on "
             "first import instead (slower start, still correct)")
    # Always 0: this is an optimisation, never a gate.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # noqa: BLE001 - never fail the install
        _log(f"unexpected error ({exc!r}); continuing without a cache")
        sys.exit(0)
