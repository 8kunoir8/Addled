"""
Skills for the wiki.

The wiki is the factual layer: pages Addled maintains from the user's own
sources. These skills let the model read it, search it, write to it on request,
and fold a new document into it without duplicating a page that already covers
the topic.
"""

from __future__ import annotations

import logging

log = logging.getLogger("addled.skills.wiki")


def _store():
    from backend.wiki import store
    return store


async def _wiki_search(params: dict) -> dict:
    """Find wiki pages by keyword."""
    query = str(params.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "Pass a 'query' to search for."}
    try:
        limit = max(1, min(int(params.get("limit") or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "The wiki is turned off in settings."}
    hits = store.search(query, limit=limit)
    return {
        "success": True,
        "count": len(hits),
        "pages": hits,
        "summary": (f"{len(hits)} page(s) match '{query}': "
                    + ", ".join(h["title"] for h in hits[:5])
                    if hits else f"No wiki page matches '{query}'."),
    }


async def _wiki_read(params: dict) -> dict:
    """Read one wiki page in full."""
    slug = str(params.get("slug") or "").strip()
    if not slug:
        return {"success": False, "error": "Pass the page 'slug' to read."}
    store = _store()
    page = store.read(slug)
    if not page:
        options = [p["slug"] for p in store.list_pages()][:15]
        return {"success": False,
                "error": f"No wiki page '{slug}'.",
                "known_slugs": options}
    return {
        "success": True,
        "page": page,
        "backlinks": store.backlinks(page["slug"]),
        "outgoing": store.outgoing(page["slug"]),
        "summary": f"{page['title']} ({page['chars']} chars, "
                   f"{len(page['sources'])} source(s)).",
    }


async def _wiki_write(params: dict) -> dict:
    """Create or replace a wiki page."""
    slug = str(params.get("slug") or params.get("title") or "").strip()
    body = str(params.get("body") or "").strip()
    if not slug:
        return {"success": False, "error": "Pass a 'slug' or 'title'."}
    if not body:
        return {"success": False, "error": "Pass the page 'body'."}
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "The wiki is turned off in settings."}
    existed = store.exists(store.slugify(slug))
    tags = params.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    sources = params.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    page = store.write(slug, title=str(params.get("title") or slug),
                       body=body, tags=list(tags), sources=list(sources))
    if not page:
        return {"success": False, "error": "The page could not be written."}
    return {
        "success": True,
        "slug": page["slug"],
        "updated": existed,
        "chars": page["chars"],
        "links": page["links"],
        "summary": (f"Updated wiki page '{page['title']}'."
                    if existed else f"Created wiki page '{page['title']}'."),
    }


async def _wiki_ingest(params: dict) -> dict:
    """Fold a document or a block of text into the wiki."""
    text = str(params.get("text") or "").strip()
    path = str(params.get("path") or "").strip()
    if not text and not path:
        return {"success": False,
                "error": "Pass 'text' to distil, or 'path' to a file."}
    from backend.wiki import ingest
    tags = params.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    result = (await ingest.ingest_file(path, tags=list(tags)) if path
              else await ingest.ingest_text(text,
                                            title=str(params.get("title") or ""),
                                            source_ref=str(params.get("source")
                                                           or ""),
                                            tags=list(tags)))
    if not result.get("success"):
        return result
    pages = result.get("pages") or []
    result["summary"] = (f"{result.get('created', 0)} new page(s), "
                         f"{result.get('updated', 0)} updated: "
                         + ", ".join(p["title"] for p in pages[:5]))
    return result


async def _wiki_links(params: dict) -> dict:
    """A page's links in both directions, plus its graph edges."""
    slug = str(params.get("slug") or "").strip()
    if not slug:
        return {"success": False, "error": "Pass the page 'slug'."}
    store = _store()
    page = store.read(slug)
    if not page:
        return {"success": False, "error": f"No wiki page '{slug}'."}
    result = {
        "success": True,
        "slug": page["slug"],
        "outgoing": store.outgoing(page["slug"]),
        "backlinks": store.backlinks(page["slug"]),
    }
    try:
        from backend.memory.links import link_store
        edges = link_store.neighbors("wiki", page["slug"])
        result["edges"] = [
            {"rel": e["rel"], "kind": e["kind"], "ref_id": e["ref_id"],
             "direction": e["direction"],
             "exists": e["kind"] != "file" or _file_exists(e["ref_id"])}
            for e in edges]
    except Exception as e:
        log.debug("wiki link edges unavailable: %s", e)
    result["summary"] = (f"{page['title']}: {len(result['outgoing'])} outgoing, "
                         f"{len(result['backlinks'])} backlink(s), "
                         f"{len(result.get('edges', []))} graph edge(s).")
    return result


def _file_exists(path: str) -> bool:
    import os
    try:
        return os.path.isfile(str(path))
    except OSError:
        return False


async def _wiki_lint(params: dict) -> dict:
    """Report broken links, orphan pages and stubs."""
    store = _store()
    if not store.enabled():
        return {"success": False, "error": "The wiki is turned off in settings."}
    report = store.lint()
    problems = (len(report["broken_links"]) + len(report["orphans"])
                + len(report["empty"]))
    report["summary"] = (
        f"{report['pages']} page(s), {problems} problem(s): "
        f"{len(report['broken_links'])} broken link(s), "
        f"{len(report['orphans'])} orphan(s), {len(report['empty'])} stub(s)."
        if problems else f"{report['pages']} page(s), wiki is clean.")
    return {"success": True, **report}


def register(registry) -> None:
    """Attach the wiki skills to a SkillRegistry."""
    from backend.skills.registry import SkillDefinition

    registry.register(SkillDefinition(
        "wiki_search",
        "Search Addled's wiki — the maintained knowledge pages distilled from "
        "the user's own documents and notes. Use this before answering a "
        "question about the user's material, or when asked what Addled knows "
        "about a topic.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Words to search for."},
                "limit": {"type": "integer",
                          "description": "Maximum pages to return."},
            },
            "required": ["query"],
        },
        _wiki_search,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "wiki_read",
        "Read one wiki page in full, with its sources, outgoing links and "
        "backlinks. Pass the page slug (from wiki_search).",
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": "The page slug to read."},
            },
            "required": ["slug"],
        },
        _wiki_read,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "wiki_write",
        "Create or update a wiki page. Use [[other-slug]] inside the body to "
        "link related pages, and pass 'sources' to record where the claims came "
        "from. Updating an existing slug keeps its earlier sources.",
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": "Kebab-case page id."},
                "title": {"type": "string",
                          "description": "Human-readable title."},
                "body": {"type": "string",
                         "description": "Markdown body."},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "Optional tags."},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "File paths or URLs this came from."},
            },
            "required": ["slug", "body"],
        },
        _wiki_write,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "wiki_ingest",
        "Fold a document into the wiki: pass 'path' to a text file or 'text' "
        "directly, and Addled reads the relevant existing pages and rewrites "
        "them to incorporate the new material, creating pages only for new "
        "topics. Use when the user says to remember, file or add a document to "
        "their wiki.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "File to ingest (.md, .txt, .rst, "
                                        ".json, .csv, .yaml, .log)."},
                "text": {"type": "string",
                         "description": "Raw text to ingest instead of a file."},
                "title": {"type": "string",
                          "description": "Optional title for pasted text."},
                "source": {"type": "string",
                           "description": "Optional citation to record."},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "Tags to apply to the new pages."},
            },
        },
        _wiki_ingest,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "wiki_links",
        "Show how a wiki page connects: what it links to, what links back to "
        "it, and which items in the wider memory graph it is attached to "
        "(sources on disk, related facts and triples).",
        {
            "type": "object",
            "properties": {
                "slug": {"type": "string",
                         "description": "The page slug."},
            },
            "required": ["slug"],
        },
        _wiki_links,
        category="memory",
    ))

    registry.register(SkillDefinition(
        "wiki_lint",
        "Check the wiki for problems: links pointing at pages that do not "
        "exist, orphan pages nothing links to, and stub pages with almost no "
        "content. Use when the user asks to tidy or audit their wiki.",
        {"type": "object", "properties": {}},
        _wiki_lint,
        category="memory",
    ))
