"""Workspace indexer — scan code roots, chunk files, embed into vector store.

Incremental: a state file records (path, mtime, size) per indexed file so
each pass only processes changed files. Runs as a scheduler housekeeping
job; query via project.search (chat pipeline can inject matches).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("addled.project.indexer")

from backend import app_paths

STATE_PATH = app_paths.subdir("integrations") \
    / "project_index_state.json"

CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".md",
             ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
             ".bat", ".ps1", ".sql", ".java", ".go", ".rs", ".c", ".h",
             ".cpp", ".hpp", ".cs", ".rb", ".php", ".kt", ".swift"}
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
               "dist", "build", ".next", "out", "python-bundle"}


def _load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {"files": {}, "last_scan": 0.0}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _roots() -> list[Path]:
    from backend.config import config
    roots = config.get("project", "roots", default=[]) or []
    if not roots:
        # No explicit project roots, so fall back to the workspace. Otherwise the
        # Code page's "index this folder" button indexes nothing for the very
        # folder the user just picked as their workspace.
        try:
            from backend.workspace import root as workspace_root
            workspace = workspace_root()
            if workspace:
                roots = [workspace]
        except Exception:
            pass
    result = []
    for r in roots:
        p = Path(r).expanduser()
        if p.is_dir():
            result.append(p)
    return result


def _walk_files(root: Path, max_files: int) -> list[Path]:
    files = []
    for p in root.rglob("*"):
        if len(files) >= max_files:
            break
        if p.is_file() and p.suffix.lower() in CODE_EXTS \
                and not p.name.startswith("_") \
                and not any(part in IGNORE_DIRS for part in p.parts):
            files.append(p)
    return files


def _chunk(text: str, lines: int = 160) -> list[str]:
    raw = text.splitlines()
    chunks = []
    for i in range(0, len(raw), lines):
        block = "\n".join(raw[i:i + lines]).strip()
        if block:
            chunks.append(block)
    return chunks or [text[:2000]]


def index_roots() -> dict:
    """Index changed files across configured roots. Returns stats."""
    from backend.config import config
    if not config.get("project", "enabled", default=False):
        return {"skipped": "project indexing disabled"}
    roots = _roots()
    if not roots:
        return {"skipped": "no project.roots configured"}

    from backend.memory.embedding import embed_text
    from backend.memory.vector_store import vector_store

    state = _load_state()
    max_files = int(config.get("project", "max_files", default=500))
    indexed = 0
    errors = 0
    files_map = state.get("files", {})

    for root in roots:
        for path in _walk_files(root, max_files):
            key = str(path)
            try:
                stat = path.stat()
                prev = files_map.get(key)
                if prev and prev.get("mtime") == int(stat.st_mtime) \
                        and prev.get("size") == stat.st_size:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                errors += 1
                continue
            try:
                for ci, block in enumerate(_chunk(text)):
                    emb = embed_text(block)
                    vector_store.add(
                        emb, category="project",
                        metadata={"path": key, "chunk": ci,
                                  "text": block[:800]})
                files_map[key] = {"mtime": int(stat.st_mtime),
                                  "size": stat.st_size}
                indexed += 1
            except Exception as e:
                log.debug("index chunk failed %s: %s", key, e)
                errors += 1

    state["files"] = files_map
    state["last_scan"] = time.time()
    _save_state(state)
    log.info("Project index pass: %d files, %d errors", indexed, errors)
    return {"indexed": indexed, "errors": errors}


def search_project(query: str, top_k: int = 6) -> list[dict]:
    """Semantic search over the project index."""
    from backend.memory.embedding import embed_text
    from backend.memory.vector_store import vector_store
    if not query.strip():
        return []
    try:
        emb = embed_text(query)
    except Exception:
        return []
    hits = vector_store.search(emb, category="project", top_k=top_k,
                               min_similarity=0.0)
    results = []
    seen = set()
    for h in hits:
        meta = h.get("metadata") or {}
        path = meta.get("path", "?")
        if path in seen:
            continue
        seen.add(path)
        results.append({"path": path, "snippet": meta.get("text", "")[:300],
                        "similarity": round(h.get("similarity", 0), 3)})
    return results


def status() -> dict:
    state = _load_state()
    return {"enabled": bool(_roots()) and _enabled(),
            "roots": [str(r) for r in _roots()],
            "indexed_files": len(state.get("files", {})),
            "last_scan": state.get("last_scan", 0.0)}


def _enabled() -> bool:
    try:
        from backend.config import config
        return bool(config.get("project", "enabled", default=False))
    except Exception:
        return False
