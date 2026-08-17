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

import json
import re
import shutil
import time
from pathlib import Path

from backend.config import SETTINGS_PATH, config

SKINS_DIR = SETTINGS_PATH.parent / "skins"
SKINS_DIR.mkdir(parents=True, exist_ok=True)

MAX_GIF_BYTES = 8 * 1024 * 1024  # 8 MB

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
            "active": d.name == active,
        })
    return skins


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
    }
    _save_meta(skin_dir, meta)
    return {
        "id": skin_dir.name,
        "name": meta["name"],
        "files": [clip_name],
        "states": STATE_KEYS,
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
