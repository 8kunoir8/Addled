"""
Filesystem locations for Addled's on-demand local models.

Everything lives under the app's writable data directory, so the app stays
portable when it can be, and falls back to the user's own folder when it is
installed somewhere it cannot write (a per-machine install under
``Program Files``). `backend.app_paths` decides which; this module only derives
from its answer, and must not compute a path of its own.
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

from backend.app_paths import MEMORY_DIR
from backend.config import config

MODELS_DIR = MEMORY_DIR / "models"
LLAMAFILE_DIR = MODELS_DIR / "llamafile"
HF_HOME_DIR = MODELS_DIR / "hf"

MIN_PLAUSIBLE_BYTES = 1_000_000


def ensure_dirs() -> None:
    for path in (MODELS_DIR, LLAMAFILE_DIR, HF_HOME_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def llamafile_exe() -> Path:
    name = config.get("local_llm", "runtime_name", default="llamafile.exe") \
        or "llamafile.exe"
    return LLAMAFILE_DIR / name


def gguf_path() -> Path:
    name = config.get("local_llm", "model_file", default="") or "model.gguf"
    return LLAMAFILE_DIR / name


def _plausible(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= MIN_PLAUSIBLE_BYTES
    except OSError:
        return False


def runtime_exists() -> bool:
    return _plausible(llamafile_exe())


def model_exists() -> bool:
    return _plausible(gguf_path())


def installed() -> bool:
    return runtime_exists() and model_exists()


def installed_bytes() -> int:
    total = 0
    for path in (llamafile_exe(), gguf_path()):
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def disk_free_mb() -> int:
    base = MEMORY_DIR if MEMORY_DIR.exists() else Path.home()
    try:
        return int(shutil.disk_usage(str(base)).free / (1024 * 1024))
    except OSError:
        return 0


def resolve_free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Return the first free TCP port at or after ``preferred``."""
    for candidate in range(int(preferred), int(preferred) + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return int(preferred)
