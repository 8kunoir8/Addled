"""
Fold a source into the wiki, incrementally.

This is the part that makes it a wiki rather than a pile of summaries: before
writing, the relevant existing pages are pulled in and handed to the model, which
returns *merged* page bodies. A second document about the same topic updates the
page instead of creating a rival one, and its citation is added to the page's
sources.

The model is asked for whole page bodies rather than diffs — applying a diff to
markdown through an LLM is a reliable way to corrupt a page, and pages here are
small enough that returning the body is cheap.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from backend.wiki import store

log = logging.getLogger("addled.wiki.ingest")

MAX_SOURCE_CHARS = 12000
MAX_EXISTING_PAGES = 3
MAX_PAGES_PER_RUN = 6

_SYSTEM = """You maintain the user's personal wiki.

Given a source and any existing pages that look related, return the pages that \
should now exist. Update an existing page by returning its full body rewritten \
to incorporate the new material — do not append, and do not duplicate a page \
that already covers the topic. Create a new page only for a distinct topic.

Rules:
- Write factual, neutral prose. No preamble, no "this document says".
- Link related topics with [[wiki-links]] using the other page's slug.
- Every page must carry the claims it makes, not a summary of the source.
- Keep each page under 3000 characters. Split rather than growing one page.
- Never invent facts that are not in the source or the existing pages.

Reply with ONLY a JSON object, no prose and no code fences:
{"pages": [{"slug": "kebab-case-slug", "title": "Title",
            "tags": ["tag"], "body": "markdown body", "links": ["other-slug"]}]}"""


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not match:
        return None
    try:
        loaded = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _candidate_pages(source_text: str) -> list[dict]:
    try:
        hits = store.search(source_text[:3000], limit=MAX_EXISTING_PAGES)
    except Exception:
        return []
    pages = []
    for hit in hits:
        page = store.read(hit["slug"])
        if page:
            pages.append(page)
    return pages


def _build_prompt(source_text: str, title: str, source_ref: str,
                  existing: list[dict]) -> str:
    parts = []
    if existing:
        parts.append("EXISTING PAGES (update these if relevant):")
        for page in existing:
            parts.append(f"--- slug: {page['slug']} | title: {page['title']} "
                         f"| tags: {', '.join(str(t) for t in page['tags'])}"
                         f"\n{page['body'][:2000]}")
    parts.append(f"SOURCE ({source_ref or title or 'pasted text'}):")
    parts.append(source_text[:MAX_SOURCE_CHARS])
    parts.append("Now return the JSON object described in your instructions.")
    return "\n\n".join(parts)


async def ingest_text(text: str, title: str = "", source_ref: str = "",
                      tags: list | None = None, provider=None) -> dict:
    """Update the wiki from a block of text. Never raises."""
    text = (text or "").strip()
    if len(text) < 40:
        return {"success": False, "error": "source is too short to distil"}
    if not store.enabled():
        return {"success": False, "error": "the wiki is disabled"}

    if provider is None:
        try:
            from backend.providers.registry import get_provider
            provider = get_provider()
        except Exception as e:
            return {"success": False, "error": f"no provider: {e}"}

    existing = _candidate_pages(text)
    model = None
    try:
        from backend.providers import router
        model = router.for_provider(provider, "reasoning")
    except Exception as e:
        log.debug("wiki ingest routing failed: %s", e)

    try:
        result = await provider.chat(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _build_prompt(text, title,
                                                       source_ref, existing)}],
            model=model, max_tokens=3000, temperature=0.2)
    except Exception as e:
        return {"success": False, "error": f"provider call failed: {e}"}
    if not result.ok:
        return {"success": False, "error": result.error or "provider error"}

    plan = _extract_json(result.response or "")
    if not plan or not isinstance(plan.get("pages"), list):
        return {"success": False,
                "error": "the model did not return a usable page plan",
                "raw": (result.response or "")[:400]}

    written: list[dict] = []
    for spec in plan["pages"][:MAX_PAGES_PER_RUN]:
        if not isinstance(spec, dict):
            continue
        body = str(spec.get("body") or "").strip()
        slug = str(spec.get("slug") or spec.get("title") or "").strip()
        if not slug or len(body) < 20:
            continue
        page = store.write(
            slug,
            title=str(spec.get("title") or slug),
            body=body,
            tags=list(spec.get("tags") or []) + list(tags or []),
            sources=[source_ref] if source_ref else [],
        )
        if page:
            written.append({"slug": page["slug"], "title": page["title"],
                            "chars": page["chars"],
                            "updated": page["slug"] in {p["slug"]
                                                        for p in existing}})
    if not written:
        return {"success": False, "error": "no pages were written",
                "raw": (result.response or "")[:400]}
    return {"success": True, "pages": written,
            "updated": sum(1 for w in written if w["updated"]),
            "created": sum(1 for w in written if not w["updated"]),
            "model": result.model or model or "", "sources": [source_ref] if
            source_ref else []}


async def ingest_file(path: str, provider=None, tags: list | None = None) -> dict:
    """Read a file and fold it into the wiki."""
    target = Path(str(path)).expanduser()
    if not target.is_file():
        return {"success": False, "error": f"not a file: {path}"}
    suffix = target.suffix.lower()
    if suffix not in (".md", ".txt", ".markdown", ".rst", ".org", ".json",
                      ".csv", ".yaml", ".yml", ".log"):
        return {"success": False,
                "error": f"unsupported file type '{suffix}' for the wiki"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"success": False, "error": f"could not read {path}: {e}"}
    result = await ingest_text(text, title=target.stem,
                              source_ref=str(target), tags=tags,
                              provider=provider)
    result["file"] = str(target)
    return result
