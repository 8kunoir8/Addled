"""
Turn the wiki into factual knowledge for the chat prompt.

The point of the pattern is that the answer already exists as maintained pages,
so this does not search the raw sources — it searches the wiki and hands the
model the relevant pages, with their citations and a list of what else the page
links to (so the model knows what it could look up next).
"""

from __future__ import annotations

import logging

from backend.wiki import store

log = logging.getLogger("addled.wiki.query")

PAGE_CHARS = 1400
DEFAULT_TOP_K = 3
DEFAULT_MAX_CHARS = 4000


def _trim(text: str, limit: int) -> tuple[str, bool]:
    """Cut at a paragraph boundary so a page never ends mid-sentence."""
    if len(text) <= limit:
        return text, False
    cut = text.rfind("\n\n", 0, limit)
    if cut < limit // 3:
        cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip(), True


def build_wiki_context(query: str, top_k: int | None = None,
                       max_chars: int | None = None) -> str | None:
    """A ``[Wiki]`` block of the most relevant pages, or None when there is
    nothing relevant. Never raises."""
    try:
        if not store.enabled():
            return None
        from backend.config import config
        top_k = int(top_k or config.get("wiki", "inject_top_k",
                                       default=DEFAULT_TOP_K))
        max_chars = int(max_chars or config.get("wiki", "max_chars",
                                               default=DEFAULT_MAX_CHARS))
    except Exception:
        top_k = top_k or DEFAULT_TOP_K
        max_chars = max_chars or DEFAULT_MAX_CHARS

    try:
        hits = store.search(query, limit=top_k)
    except Exception as e:
        log.debug("wiki search failed: %s", e)
        return None
    if not hits:
        return None

    sections: list[str] = []
    used = 0
    for hit in hits:
        page = store.read(hit["slug"])
        if not page or not page["body"].strip():
            continue
        body, truncated = _trim(page["body"], min(PAGE_CHARS,
                                                  max(60, max_chars - used)))
        if used + len(body) > max_chars and sections:
            break
        parts = [f"### {page['title']}"]
        if page.get("tags"):
            parts.append("tags: " + ", ".join(str(t) for t in page["tags"]))
        parts.append(body)
        if truncated:
            parts.append(f"[truncated — full page at {page['path']}]")
        if page.get("links"):
            parts.append("Related pages: "
                         + ", ".join(f"[[{link}]]" for link in page["links"]))
        if page.get("sources"):
            parts.append("Sources: " + "; ".join(str(s)
                                                 for s in page["sources"][:4]))
        sections.append("\n\n".join(parts))
        used += len(body)
    if not sections:
        return None

    return (
        "[Wiki] Maintained knowledge pages relevant to this request. They are "
        "the user's own notes, already distilled from their sources, so prefer "
        "them over guessing. If they do not cover the question, say so rather "
        "than inventing detail.\n\n" + "\n\n".join(sections)
    )


def answer_sources(query: str, limit: int = 5) -> list[dict]:
    """Which pages would be used to answer a query — for the dashboard."""
    try:
        return store.search(query, limit=limit)
    except Exception:
        return []
