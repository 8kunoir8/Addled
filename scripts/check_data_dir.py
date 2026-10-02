"""The backend must be able to start from a read-only install directory.

The bug this closes: every writable path in the app was computed as
``Path(__file__).parent.parent / "memory"`` — beside the code. That is only true
for a per-user install. A per-machine install lands in ``C:\\Program Files``,
where a normal user cannot write, and the backend died on its very first act:

    PermissionError: [Errno 13] Permission denied:
    'C:\\Program Files\\Addled\\resources\\backend\\memory\\addled.log'

The character then sat there saying "Backend not running", with nothing in the
UI explaining why. Installing to Program Files is a normal thing to choose, so
this had to stop being an assumption.

What is checked:
  * the resolver prefers the install directory when it IS writable, so existing
    portable and per-user installs keep their data where it always was;
  * it falls back to the user's own directory when the install directory is not
    writable, and says so;
  * a real write probe is used, not ``os.access`` — on Windows the latter reports
    ``Program Files`` as writable, which is exactly the trap that hid this;
  * no module still computes its own ``memory/`` path, which is how 19 separate
    copies of the assumption accumulated;
  * migration copies an old install-local tree across without overwriting, and
    is idempotent.

Run from the project root:
    .\\python-bundle\\python.exe -s .\\scripts\\check_data_dir.py
"""

import os
import pathlib
import shutil
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
    # Imported with the override cleared, so the module resolves for real.
    os.environ.pop("ADDLED_DATA_DIR", None)
    from backend import app_paths

    print("=== a writable install keeps its data beside itself ===")
    check("the dev tree is treated as portable",
          app_paths.describe()["portable"],
          str(app_paths.describe()))
    check("and it resolves to the install's own memory/",
          pathlib.Path(app_paths.DATA_DIR).name == "memory",
          str(app_paths.DATA_DIR))

    print("\n=== the writability probe is a real write, not os.access ===")
    # `os.access` lies about Program Files on Windows; this is the trap the
    # original code would have fallen into had it tried to be careful.
    program_files = pathlib.Path(r"C:\Program Files")
    if program_files.exists():
        probe = app_paths._writable(
            program_files / "Addled" / "resources" / "backend" / "memory")
        check("Program Files is reported NOT writable", not probe,
              "os.access-based checks report it as writable")
    else:
        print("  --  (no C:\\Program Files on this machine; skipped)")

    print("\n=== an unwritable install falls back to the user's directory ===")
    original_legacy = app_paths.LEGACY_DIR
    try:
        app_paths.LEGACY_DIR = pathlib.Path(
            r"C:\Program Files\Addled\resources\backend\memory"
        ) if os.name == "nt" else pathlib.Path("/usr/lib/addled/memory")
        os.environ.pop("ADDLED_DATA_DIR", None)
        chosen = app_paths._resolve_data_dir()
        check("it does not stay in the install directory",
              str(chosen) != str(app_paths.LEGACY_DIR), str(chosen))
        check("it is writable", app_paths._writable(chosen), str(chosen))
        if os.name == "nt":
            local = os.environ.get("LOCALAPPDATA", "")
            check("and it is under LOCALAPPDATA",
                  local and str(chosen).lower().startswith(local.lower()),
                  str(chosen))
    finally:
        app_paths.LEGACY_DIR = original_legacy
        os.environ.pop("ADDLED_DATA_DIR", None)

    print("\n=== an explicit override is honoured as given ===")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ADDLED_DATA_DIR"] = tmp
        try:
            resolved = app_paths._resolve_data_dir()
            check("the override wins", pathlib.Path(resolved) == pathlib.Path(tmp),
                  str(resolved))
        finally:
            os.environ.pop("ADDLED_DATA_DIR", None)

    print("\n=== no module computes its own state path any more ===")
    # The 19 copies are how this drifted; one resolver is the fix, so a
    # regression is a new copy appearing.
    #
    # Three shapes to catch, and each was missed in turn before being added
    # here — which is the argument for listing them:
    #   Path(__file__).parent.parent / "memory" / thing   (modules climbing out
    #                                                      of backend/)
    #   Path(__file__).parent / "chat_history.json"       (state files INSIDE
    #                                                      backend/memory/, which
    #                                                      never mention "memory")
    #   Path(__file__).parent / "journal"                 (a state DIRECTORY —
    #                                                      no extension to match,
    #                                                      so a suffix rule
    #                                                      cannot see it)
    # The third shipped and only showed up as a WARNING in the live log
    # ("journal save failed: [WinError 5] ..."), because the journal is written
    # best-effort and nothing waits on it.
    import re

    offenders = []
    backend = pathlib.Path(ROOT) / "backend"
    # A __file__-relative path that ends in something writable: a data file, or
    # a bare lowercase directory name that is not a package (packages ship).
    state_suffix = re.compile(
        r"""Path\(__file__\)[^\n]*?/\s*"""
        r"""(?:"[^"]+\.(?:json|db|db-wal|db-shm|log|jsonl|txt|csv)"|"""
        r"""'[^']+\.(?:json|db|db-wal|db-shm|log|jsonl|txt|csv)')"""
    )
    # A bare quoted directory on the end of a __file__ expression, e.g.
    # `... / "journal"`. Package directories contain an __init__.py, so a name
    # that resolves to a package is skipped rather than flagged.
    bare_dir = re.compile(
        r"""Path\(__file__\)[^\n]*?/\s*["']([A-Za-z0-9_-]+)["']\s*$""")
    #
    # A bare directory is only a fault when it is where state is written. A
    # module may legitimately name a read-only location beside the code:
    #   * `sprite_skin.DEFAULT_SKINS_DIR` — starter skins copied OUT of it
    #   * `embedding.MODEL_DIR`, `voice.tts.MODELS_DIR` — bundled weights, read
    #   * `tools.rtk.app_dir` / `tools.uv.app_dir` — the install's copy, tried
    #     first and abandoned for `user_dir()` when it is not writable
    # Flagging those would be a check that cries wolf on correct code, so they
    # are named here with the reason, the same way the packaging check names its
    # deliberate exceptions. A new one has to be added on purpose.
    READ_ONLY_BESIDE_CODE = {
        ("character/sprite_skin.py", "default_skins"),
        ("memory/embedding.py", "minilm-l6-v2"),
        ("voice/tts.py", "models"),
        ("tools/rtk.py", "rtk"),
        ("tools/uv.py", "uv"),
    }
    for path in backend.rglob("*.py"):
        if path.name == "app_paths.py":
            continue          # it defines the resolver; it may name the paths
        rel = path.relative_to(backend).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "Path(__file__)" in line and '"memory"' in line:
                offenders.append(f"{rel}:{lineno} (memory dir)")
            elif state_suffix.search(line):
                offenders.append(f"{rel}:{lineno} (state file)")
            else:
                m = bare_dir.search(line)
                if not m:
                    continue
                name = m.group(1)
                if (path.parent / name / "__init__.py").exists():
                    continue      # a package: code, ships with the app
                if (rel, name) in READ_ONLY_BESIDE_CODE:
                    continue      # named above as a deliberate read
                offenders.append(f"{rel}:{lineno} (state dir '{name}')")
    check("no file computes a writable path from __file__", not offenders,
          ", ".join(offenders[:6]))

    print("\n=== migration brings an old install-local tree across ===")
    with tempfile.TemporaryDirectory() as tmp:
        dest = pathlib.Path(tmp) / "data"
        dest.mkdir()
        source = pathlib.Path(tmp) / "legacy"
        (source / "sub").mkdir(parents=True)
        (source / "settings.json").write_text('{"a": 1}', encoding="utf-8")
        (source / "sub" / "facts.json").write_text('[]', encoding="utf-8")
        # `backend/memory/` holds the memory modules next to the state they use,
        # so a migration that copies everything would litter the data directory
        # with a second copy of the app's own code.
        (source / "recall.py").write_text("# module, not state\n",
                                          encoding="utf-8")
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "recall.cpython-314.pyc").write_bytes(b"\x00")

        original_legacy, original_data = app_paths.LEGACY_DIR, app_paths.DATA_DIR
        try:
            app_paths.LEGACY_DIR = source
            app_paths.DATA_DIR = dest
            copied = app_paths.migrate()
            check("only the state files were copied", copied == 2,
                  f"copied={copied} (expected 2: settings.json + facts.json)")
            check("the nested file arrived",
                  (dest / "sub" / "facts.json").exists())
            check("no module was copied",
                  not (dest / "recall.py").exists(),
                  "a .py file reached the data directory")
            check("no bytecode was copied",
                  not (dest / "__pycache__").exists())
            check("the source is left in place",
                  (source / "settings.json").exists(),
                  "a downgrade must still find its data")

            # An existing destination file is never overwritten by a stale one.
            (dest / "settings.json").write_text('{"live": true}', encoding="utf-8")
            (dest / ".migrated-from-install").unlink()
            app_paths.migrate()
            check("the live file is not overwritten",
                  '"live"' in (dest / "settings.json").read_text(encoding="utf-8"),
                  (dest / "settings.json").read_text(encoding="utf-8"))

            check("a second run is a no-op", app_paths.migrate() == 0)
        finally:
            app_paths.LEGACY_DIR, app_paths.DATA_DIR = original_legacy, original_data

    print()
    if fails:
        print(f"FAIL: {len(fails)}: {fails}")
        return 1
    print("PASS: the backend starts from a read-only install directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
