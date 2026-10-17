"""Atomic, corruption-preserving JSON writes.

Every memory store used to write its file in place:

    path.write_text(json.dumps(data))

A process killed mid-write leaves a half-written file, which `json.load` then
rejects, and the loader's `except` returns the empty default. The next save
writes that empty default plus the one new item over the file — so a crash at
the wrong instant lost the entire history rather than the turn in progress.
Measured on `facts.py`: a truncated file loaded as `[]`, and the following
`_save([one_item])` replaced four facts with one.

Three properties fix that:

* `write_json` writes a temp file in the same directory and `os.replace`s it,
  which is atomic on Windows and POSIX. A reader sees the old file or the new
  one, never a torn one.
* `read_json` moves an unreadable file aside to `.corrupt` instead of treating
  it as empty. Damaged data is recoverable; deleted data is not.
* `write_json_safe` reports whether the write landed, so a caller can decide
  not to lose the in-memory copy.

None of this is a lock. Two writers still race, and last-write-wins still
applies; that is a separate problem and pretending otherwise here would hide
it.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("addled.atomic")


def _corrupt(path: Path) -> Path:
    """Move an unreadable file aside and return where it went.

    Timestamped, so a second corruption does not overwrite the first copy —
    the file most worth reading is the one from before the crash that caused
    the next one.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        shutil.move(str(path), str(dest))
        log.warning("corrupt state file moved aside: %s -> %s",
                    path.name, dest.name)
    except Exception as e:  # noqa: BLE001
        log.warning("could not move corrupt state file %s aside: %s", path, e)
    return dest


def read_json(path, default=None):
    """Load JSON, or `default` if it is absent or unreadable. Never raises.

    An unreadable file is moved to `.corrupt-<stamp>` first, so the damaged
    bytes survive for inspection and the next write cannot silently overwrite
    them.
    """
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning("could not parse %s: %s", p.name, e)
        _corrupt(p)
        return default
    except Exception as e:  # noqa: BLE001
        log.warning("could not read %s: %s", p.name, e)
        return default


def write_json(path, data, *, indent: int = 2) -> bool:
    """Write JSON atomically. Returns True if it landed.

    The temp file is created in the same directory because `os.replace` is
    only atomic within a filesystem.
    """
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("could not write %s: %s", p.name, e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return False


def write_json_safe(path, data, *, indent: int = 2,
                    _fail_after: bool = False) -> bool:
    """`write_json`, with a test hook to prove a failed write is harmless.

    Returns False rather than raising so callers can keep the in-memory copy
    and try again, instead of losing the update because a disk was full.
    """
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False),
                       encoding="utf-8")
        if _fail_after:
            # Simulates the crash point: the temp file exists and is complete,
            # but the rename never happens. The original must be untouched.
            raise OSError("simulated failure before replace")
        os.replace(tmp, p)
        return True
    except Exception as e:  # noqa: BLE001
        log.error("could not write %s: %s", p.name, e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return False
