"""
Sprite-skin store — codex-pet style skins for the floating character.

A skin is a folder under memory/skins/ containing GIF files and a skin.json
that maps character states to clips. A single uploaded GIF works for every
state (community pets are usually one looping animation); multi-clip skins
can be created by dropping per-state GIFs into the folder manually.

All functions are plain file/JSON operations (no Qt objects) so they are
safe to call from any thread. QMovie handling happens in avatar.py on the
GUI thread.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path

from backend.config import SETTINGS_PATH, config

log = logging.getLogger("addled.skin")

SKINS_DIR = SETTINGS_PATH.parent / "skins"
SKINS_DIR.mkdir(parents=True, exist_ok=True)

# Starter skins shipped with the app (copied into SKINS_DIR on first run)
DEFAULT_SKINS_DIR = Path(__file__).parent / "default_skins"

MAX_GIF_BYTES = 8 * 1024 * 1024  # 8 MB
MAX_ZIP_BYTES = 25 * 1024 * 1024  # 25 MB archive
MAX_ZIP_TOTAL = 20 * 1024 * 1024  # 20 MB of extracted GIFs
MAX_ZIP_ENTRY = 5 * 1024 * 1024  # 5 MB per GIF inside the ZIP

# CharacterState.name.lower() → clip key used in skin.json
STATE_KEYS = [
    "idle", "listening", "observing", "thinking", "has_suggestion",
    "acting", "speaking", "sleeping", "blocked", "error", "working",
    "dreaming",
]

GIF_MAGIC = (b"GIF87a", b"GIF89a")


def _sanitize_id(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_").lower()
    return slug or f"skin_{int(time.time())}"


def _unique_skin_dir(slug: str) -> Path:
    path = SKINS_DIR / slug
    n = 2
    while path.exists():
        path = SKINS_DIR / f"{slug}_{n}"
        n += 1
    return path


def _load_meta(skin_dir: Path) -> dict:
    meta_path = skin_dir / "skin.json"
    if meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Rebuild meta from folder contents (manual multi-clip skins)
    files = sorted(p.name for p in skin_dir.glob("*.gif"))
    if not files:
        return {"name": skin_dir.name, "states": {}}
    idle = next((f for f in files if f.lower().startswith("idle")), files[0])
    states = {key: idle for key in STATE_KEYS}
    # Map files named after states
    for key in STATE_KEYS:
        match = next((f for f in files
                      if f.lower().split(".")[0] == key), None)
        if match:
            states[key] = match
    return {"name": skin_dir.name, "states": states}


def _save_meta(skin_dir: Path, meta: dict):
    with open(skin_dir / "skin.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def get_active_skin_id() -> str:
    return config.get("character", "skin", default="") or ""


def set_active_skin(skin_id: str):
    config.set("character", "skin", value=skin_id or "")


def list_skins() -> list[dict]:
    active = get_active_skin_id()
    skins = []
    for d in sorted(SKINS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = _load_meta(d)
        files = sorted(p.name for p in d.glob("*.gif"))
        if not files:
            continue
        skins.append({
            "id": d.name,
            "name": meta.get("name", d.name),
            "files": files,
            "states": list(meta.get("states", {}).keys()),
            "mapped": meta.get("mapped", []),
            "active": d.name == active,
        })
    return skins


def install_default_skins() -> list[str]:
    """Copy bundled starter skins into the runtime skins folder.
    Skips skins that already exist (never overwrites user data).
    Returns the ids of newly installed skins."""
    installed = []
    if not DEFAULT_SKINS_DIR.is_dir():
        return installed
    for d in DEFAULT_SKINS_DIR.iterdir():
        if not d.is_dir() or not any(d.glob("*.gif")):
            continue
        target = SKINS_DIR / d.name
        if target.exists():
            continue
        shutil.copytree(d, target)
        installed.append(d.name)
    if installed:
        log.debug("Installed default skins: %s", installed)
    return installed


def save_uploaded_skin(name: str, filename: str, data: bytes) -> dict:
    """Save an uploaded GIF as a new skin. Returns the skin dict."""
    if not data:
        raise ValueError("No data provided")
    if len(data) > MAX_GIF_BYTES:
        raise ValueError("File too large (max 8 MB)")
    if data[:6] not in GIF_MAGIC:
        raise ValueError("Only GIF files are supported")

    slug = _sanitize_id(name)
    skin_dir = _unique_skin_dir(slug)
    skin_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(filename).suffix.lower() or ".gif"
    clip_name = f"{skin_dir.name}{ext}"
    (skin_dir / clip_name).write_bytes(data)

    meta = {
        "name": name.strip() or skin_dir.name,
        "states": {key: clip_name for key in STATE_KEYS},
        "scale": 1.0,
        "created": int(time.time()),
        "source_file": filename,
        "mapped": [],  # single GIF covers everything
    }
    _save_meta(skin_dir, meta)
    return {
        "id": skin_dir.name,
        "name": meta["name"],
        "files": [clip_name],
        "states": STATE_KEYS,
        "mapped": [],
        "active": False,
    }


def save_uploaded_zip(name: str, filename: str, data: bytes) -> dict:
    """Save a ZIP of per-state GIFs as a new skin.

    GIFs are matched to states by file name: idle.gif, thinking.gif,
    speaking.gif, ... Unmatched GIFs are kept as extra clips; unmapped
    states fall back to idle.gif (or any available clip).
    """
    if not data:
        raise ValueError("No data provided")
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("ZIP too large (max 25 MB)")
    if data[:2] != b"PK":
        raise ValueError("Not a ZIP file")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")

    slug = _sanitize_id(name)
    skin_dir = _unique_skin_dir(slug)
    skin_dir.mkdir(parents=True, exist_ok=True)

    clips: dict[str, str] = {}   # state_key -> saved filename
    extras: list[str] = []
    total = 0
    try:
        for info in zf.infolist():
            if info.is_dir():
                continue
            fname = Path(info.filename).name
            if not fname.lower().endswith(".gif"):
                continue
            if info.file_size > MAX_ZIP_ENTRY:
                continue
            raw = zf.read(info)
            if not raw or raw[:6] not in GIF_MAGIC:
                continue
            total += len(raw)
            if total > MAX_ZIP_TOTAL:
                raise ValueError("ZIP contents too large (max 20 MB of GIFs)")
            key = fname.lower()[:-4]
            # avoid overwriting an already-mapped state
            out_name = fname
            if (skin_dir / out_name).exists():
                out_name = f"{key}_{len(total)}_{fname}"
            (skin_dir / out_name).write_bytes(raw)
            if key in STATE_KEYS and key not in clips:
                clips[key] = out_name
            else:
                extras.append(out_name)
    finally:
        zf.close()

    if not clips and not extras:
        shutil.rmtree(skin_dir, ignore_errors=True)
        raise ValueError("No GIF files found in the ZIP")

    # Fallback clip for unmapped states: idle → first mapped → first extra
    fallback = clips.get("idle") or (
        next(iter(clips.values())) if clips else None) or (
        extras[0] if extras else None)
    states_map = {key: clips.get(key) or fallback for key in STATE_KEYS}

    meta = {
        "name": name.strip() or skin_dir.name,
        "states": states_map,
        "scale": 1.0,
        "created": int(time.time()),
        "source_file": filename,
        "mapped": sorted(clips.keys()),
        "extra": extras,
    }
    _save_meta(skin_dir, meta)
    return {
        "id": skin_dir.name,
        "name": meta["name"],
        "files": sorted(p.name for p in skin_dir.glob("*.gif")),
        "states": STATE_KEYS,
        "mapped": meta["mapped"],
        "extra": extras,
        "active": False,
    }


def delete_skin(skin_id: str) -> bool:
    skin_dir = SKINS_DIR / skin_id
    if not skin_dir.is_dir():
        return False
    shutil.rmtree(skin_dir, ignore_errors=True)
    if get_active_skin_id() == skin_id:
        set_active_skin("")
    return True


def resolve_clip_path(skin_id: str, state_key: str) -> str | None:
    """Return the absolute path of the GIF clip for a state, or None."""
    if not skin_id:
        return None
    skin_dir = SKINS_DIR / skin_id
    if not skin_dir.is_dir():
        return None
    meta = _load_meta(skin_dir)
    states = meta.get("states", {})
    clip = states.get(state_key) or states.get("idle")
    if clip:
        path = skin_dir / clip
        if path.is_file():
            return str(path)
    gifs = sorted(skin_dir.glob("*.gif"))
    return str(gifs[0]) if gifs else None
