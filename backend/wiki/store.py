"""
Wiki page storage.

Pages are markdown files with a small YAML frontmatter block:

    ---
    title: SQLite
    slug: sqlite
    tags: [storage, database]
    sources: ["C:/dev/notes/sqlite.md", "https://sqlite.org/whentouse.html"]
    created: 2026-09-18T10:00:00+00:00
    updated: 2026-09-18T10:20:00+00:00
    ---
    SQLite is an embedded relational database. See [[wal-mode]] and [[fts5]].

The body is the knowledge; ``[[links]]`` make it a graph. Everything is plain
files, so the user can edit a page in any editor and the next read picks it up —
:func:`list_pages` rescans rather than trusting a cache, and the index is only a
convenience for the dashboard.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("addled.wiki")

LINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
SLUG_RE = re.compile(r"[^a-z0-9]+")
MAX_BODY = 200_000


# -- locations ----------------------------------------------------------------


def base_dir() -> Path:
    """Where the wiki lives. Configurable so it can sit in a synced folder."""
    custom = ""
    try:
        from backend.config import config
        custom = str(config.get("wiki", "dir", default="") or "").strip()
    except Exception:
        custom = ""
    if custom:
        return Path(custom)
    from backend import app_paths
    return app_paths.subdir("wiki")


def pages_dir() -> Path:
    return base_dir() / "pages"


def index_path() -> Path:
    return base_dir() / "index.json"


def enabled() -> bool:
    try:
        from backend.config import config
        return bool(config.get("wiki", "enabled", default=True))
    except Exception:
        return True


# -- slugs --------------------------------------------------------------------


def slugify(text: str) -> str:
    """A filesystem-safe slug. Stable for a given title."""
    slug = SLUG_RE.sub("-", str(text or "").strip().lower()).strip("-")
    return (slug or "page")[:80]


def page_path(slug: str) -> Path:
    return pages_dir() / f"{slugify(slug)}.md"


# -- frontmatter --------------------------------------------------------------


def _dump_frontmatter(meta: dict) -> str:
    """Hand-written YAML for a flat mapping of str/list values.

    Avoiding a yaml dependency here keeps page writing dependency-free even if
    the bundle is trimmed; reading falls back to a line parser if yaml is absent.
    """
    lines = ["---"]
    for key in ("title", "slug", "created", "updated"):
        value = str(meta.get(key) or "").replace("\n", " ")
        lines.append(f"{key}: {json.dumps(value)}")
    for key in ("tags", "sources"):
        values = meta.get(key) or []
        rendered = ", ".join(json.dumps(str(v)) for v in values)
        lines.append(f"{key}: [{rendered}]")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw, body = match.group(1), text[match.end():]
    meta: dict = {}
    try:
        import yaml
        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    if not meta:
        # Fallback: "key: value" per line, JSON-ish values.
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip() for v in value[1:-1].split(",") if v.strip()]
                meta[key.strip()] = [i.strip().strip('"') for i in items]
            else:
                meta[key.strip()] = value.strip().strip('"')
    for key in ("tags", "sources"):
        value = meta.get(key)
        if isinstance(value, str):
            meta[key] = [v.strip() for v in value.split(",") if v.strip()]
        elif not isinstance(value, list):
            meta[key] = []
    return meta, body


# -- links in bodies ----------------------------------------------------------


def parse_links(body: str) -> list[str]:
    """Slugs referenced by ``[[links]]``, deduplicated, order preserved."""
    out: list[str] = []
    for match in LINK_RE.finditer(body or ""):
        slug = slugify(match.group(1))
        if slug and slug not in out:
            out.append(slug)
    return out


def strip_link_markup(body: str) -> str:
    """``[[slug|label]]`` -> ``label``, ``[[slug]]`` -> ``slug`` for display."""
    def _sub(match: re.Match) -> str:
        return (match.group(2) or match.group(1) or "").strip()
    return LINK_RE.sub(_sub, body or "")


# -- reads --------------------------------------------------------------------


def exists(slug: str) -> bool:
    try:
        return page_path(slug).is_file()
    except Exception:
        return False


def read(slug: str) -> dict | None:
    """One page, or None when it does not exist."""
    path = page_path(slug)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    resolved = slugify(meta.get("slug") or slug)
    return {
        "slug": resolved,
        "title": str(meta.get("title") or resolved.replace("-", " ").title()),
        "tags": meta.get("tags") or [],
        "sources": meta.get("sources") or [],
        "created": str(meta.get("created") or ""),
        "updated": str(meta.get("updated") or ""),
        "body": body.strip(),
        "text": strip_link_markup(body).strip(),
        "links": parse_links(body),
        "chars": len(body or ""),
        "path": str(path),
    }


def list_pages() -> list[dict]:
    """Every page's metadata, newest-updated first. Rescans the directory."""
    directory = pages_dir()
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for path in sorted(directory.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = _parse_frontmatter(text)
        slug = slugify(meta.get("slug") or path.stem)
        out.append({
            "slug": slug,
            "title": str(meta.get("title") or slug.replace("-", " ").title()),
            "tags": meta.get("tags") or [],
            "sources": meta.get("sources") or [],
            "updated": str(meta.get("updated") or ""),
            "chars": len(body or ""),
            "links": parse_links(body),
        })
    out.sort(key=lambda p: p.get("updated") or "", reverse=True)
    return out


def backlinks(slug: str) -> list[str]:
    """Pages that link *to* this one."""
    target = slugify(slug)
    return [p["slug"] for p in list_pages() if target in (p.get("links") or [])]


def outgoing(slug: str) -> list[str]:
    page = read(slug)
    return page["links"] if page else []


# -- writes -------------------------------------------------------------------

def write(slug: str, title: str = "", body: str = "",
          tags: list | None = None, sources: list | None = None,
          merge_sources: bool = True) -> dict | None:
    """Create or update a page. Returns the stored page.

    Provenance is preserved rather than overwritten: new ``sources`` are merged
    into the existing ones, because a page that has absorbed several documents
    must keep citing all of them.
    """
    if not enabled():
        return None
    resolved = slugify(slug or title)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = read(resolved)

    previous_sources = list(existing["sources"]) if existing else []
    incoming = [str(s) for s in (sources or []) if s]
    if merge_sources:
        merged = previous_sources + [s for s in incoming
                                     if s not in previous_sources]
    else:
        merged = incoming

    meta = {
        "title": (title or (existing or {}).get("title") or
                  resolved.replace("-", " ").title()),
        "slug": resolved,
        "tags": list(dict.fromkeys(list(tags or (existing or {}).get("tags")
                                        or []))),
        "sources": merged,
        "created": (existing or {}).get("created") or now,
        "updated": now,
    }
    text = str(body or "").strip()[:MAX_BODY]
    payload = _dump_frontmatter(meta) + ("\n" + text + "\n" if text else "\n")
    try:
        pages_dir().mkdir(parents=True, exist_ok=True)
        tmp = page_path(resolved).with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(page_path(resolved))
    except OSError as e:
        log.warning("could not write wiki page '%s': %s", resolved, e)
        return None
    _touch_index(resolved)
    reindex_links()
    return read(resolved)


def delete(slug: str) -> bool:
    resolved = slugify(slug)
    path = page_path(resolved)
    if not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as e:
        log.debug("could not delete wiki page '%s': %s", resolved, e)
        return False
    try:
        from backend.memory.links import link_store
        link_store.forget("wiki", resolved)
    except Exception:
        pass
    _touch_index(resolved, removed=True)
    reindex_links()
    return True


def _touch_index(slug: str, removed: bool = False) -> None:
    """Keep a small index for the dashboard. Not authoritative — pages are."""
    try:
        data = {}
        if index_path().is_file():
            data = json.loads(index_path().read_text(encoding="utf-8")) or {}
        if removed:
            data.pop(slug, None)
        else:
            data[slug] = {"updated": time.time()}
        base_dir().mkdir(parents=True, exist_ok=True)
        index_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("wiki index update failed: %s", e)


# -- links into the shared graph ----------------------------------------------

def reindex_links() -> dict:
    """Mirror ``[[links]]`` and local-path ``sources`` into memory/links.db.

    URLs are deliberately *not* linked: they are not files, and putting them in
    the file graph would create nodes that can never be resolved.
    """
    try:
        from backend.memory.links import link_store
    except Exception:
        return {"available": False, "wiki_links": 0, "source_links": 0}
    if not link_store.available:
        return {"available": False, "wiki_links": 0, "source_links": 0}

    pages = list_pages()
    known = {p["slug"] for p in pages}
    wiki_links = 0
    source_links = 0
    for page in pages:
        for target in page.get("links") or []:
            # Only link to pages that exist; a link to a missing page stays in
            # the markdown (so lint can report it) but is not a graph edge.
            if target in known:
                if link_store.link("wiki", page["slug"], "links_to", "wiki",
                                   target, source="wiki",
                                   note=f"[[{target}]]") is not None:
                    wiki_links += 1
        for source in page.get("sources") or []:
            if re.match(r"^\w+://", str(source)):
                continue
            if link_store.link("wiki", page["slug"], "sourced_from", "file",
                               source, source="wiki",
                               note=f"source: {Path(str(source)).name}") \
                    is not None:
                source_links += 1
    return {"available": True, "pages": len(pages),
            "wiki_links": wiki_links, "source_links": source_links}


# -- search + lint ------------------------------------------------------------

def search(query: str, limit: int = 10) -> list[dict]:
    """Rank pages against a query: title beats tags beats body."""
    terms = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 1]
    if not terms:
        return []
    scored: list[tuple[float, dict]] = []
    for page in list_pages():
        full = read(page["slug"])
        if not full:
            continue
        title = page["title"].lower()
        tags = " ".join(str(t).lower() for t in page.get("tags") or [])
        body = full["text"].lower()
        score = 0.0
        for term in terms:
            if term in title:
                score += 3.0
            if term in tags:
                score += 2.0
            score += min(body.count(term), 5) * 0.5
            # A page whose title matches every term should outrank a long page
            # that merely mentions one of them often.
        if score > 0:
            scored.append((score, {**page, "score": round(score, 2),
                                   "excerpt": _excerpt(full["text"], terms)}))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _score, item in scored[:limit]]


def _excerpt(text: str, terms: list[str], width: int = 240) -> str:
    lowered = (text or "").lower()
    position = min((lowered.find(t) for t in terms if lowered.find(t) >= 0),
                   default=0)
    start = max(0, position - 60)
    return " ".join((text or "")[start:start + width].split())


def lint() -> dict:
    """Consistency report: broken links, orphans, empty pages, duplicates."""
    pages = list_pages()
    known = {p["slug"] for p in pages}
    broken: list[dict] = []
    orphans: list[str] = []
    empty: list[str] = []
    for page in pages:
        for target in page.get("links") or []:
            if target not in known:
                broken.append({"slug": page["slug"], "missing": target})
        if not backlinks(page["slug"]):
            orphans.append(page["slug"])
        if page["chars"] < 20:
            empty.append(page["slug"])
    by_title: dict[str, list[str]] = {}
    for page in pages:
        by_title.setdefault(page["title"].strip().lower(), []).append(page["slug"])
    duplicates = [slugs for slugs in by_title.values() if len(slugs) > 1]
    return {
        "pages": len(pages),
        "broken_links": broken,
        "orphans": orphans,
        "empty": empty,
        "duplicate_titles": duplicates,
        "healthy": not (broken or empty or duplicates),
    }
